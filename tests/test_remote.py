import os
import sys
import time
from pathlib import Path

import pytest

from lockview import owners, probe, procs, remote

GB = 2**30


def _p(ppid, comm, argv, sid=0, rss=0):
    return {"ppid": ppid, "sid": sid, "comm": comm, "argv": argv, "uid": 1000, "ticks": 0, "start": 0, "rss": rss}


# 1 systemd; an ssh command's bash -c with a python and its worker; a tmux pane's shell running a
# trainer; a Claude session whose Bash tool runs pytest; a light editor.
PROCS = {
    1: _p(0, "systemd", ["/sbin/init"], sid=1),
    10: _p(1, "sshd", ["sshd: /usr/sbin/sshd -D"], sid=10),
    11: _p(10, "sshd-session", ["sshd-session: u@notty"], sid=11),
    12: _p(11, "bash", ["bash", "-c", "cd ~/x && python a.py"], sid=12),
    13: _p(12, "python", ["python", "a.py"], sid=12, rss=GB),
    14: _p(13, "python", ["python", "-c", "worker"], sid=12, rss=GB // 2),
    20: _p(1, "tmux: server", ["tmux"], sid=20),
    21: _p(20, "bash", ["-bash"], sid=21),
    22: _p(21, "python", ["python", "train.py"], sid=21, rss=3 * GB),
    30: _p(21, "claude", ["claude"], sid=21),
    31: _p(30, "bash", ["/bin/bash", "-c", "pytest"], sid=31),
    32: _p(31, "pytest", ["pytest", "-n", "4"], sid=31),
    40: _p(21, "vim", ["vim", "notes"], sid=21),
}


def test_a_job_is_the_tree_below_the_nearest_ssh_login_tmux_shell_or_claude_session():
    assert [probe.job_root(pid, PROCS) for pid in (13, 14, 22, 32, 40)] == [12, 12, 22, 31, 40]
    assert probe.job_root(30, PROCS) == 30  # a boundary that works hard is a job of its own


def test_only_heavy_jobs_are_sent_with_their_use_summed_and_their_heaviest_processes():
    cpu = {13: 0.4, 14: 0.3, 30: 0.6, 32: 0.2, 40: 0.1}
    sessions = {30: {"name": "drivers-a5", "cwd": "/home/u/work/drivers"}}
    jobs = {j["pid"]: j for j in probe.jobs(PROCS, cpu, {32: 900}, sessions, str, root="/nonexistent")}
    assert set(jobs) == {12, 22, 30, 31}  # cpu, RAM, the session itself, VRAM; not vim's tenth of a core
    assert (jobs[12]["cpu"], jobs[12]["rss"], jobs[12]["procs"]) == (0.7, GB + GB // 2, 3)
    assert [p["pid"] for p in jobs[12]["top"]] == [13, 14, 12]
    assert jobs[12]["command"] == "bash -c cd ~/x && python a.py"
    assert (jobs[30]["procs"], jobs[30]["cpu"]) == (1, 0.6)  # the session's own use, not its jobs'
    assert jobs[31]["vram_mb"] == 900 and jobs[31]["top"][0]["pid"] == 32
    assert jobs[31]["session"]["name"] == "drivers-a5" and jobs[22]["session"] is None
    assert jobs[12]["ssh"] is None and jobs[12]["cwd"] == ""  # nothing to read under /nonexistent


def test_a_processs_cores_are_its_cpu_ticks_over_the_seconds_between_readings():
    tick = probe.CLK_TCK
    before = {1: {"ticks": 0, "start": 5}, 2: {"ticks": 0, "start": 5}}
    now = {1: {"ticks": 3 * tick, "start": 5}, 2: {"ticks": tick, "start": 9}, 3: {"ticks": tick, "start": 9}}
    assert probe.cores_used(before, now, 2.0) == {1: 1.5}  # 2 is another process under a reused pid, 3 is new
    assert probe.cores_used(before, now, 0.0) == {}


def test_the_probe_reads_this_process_and_an_ssh_login_from_the_environment(tmp_path):
    me = probe.read_procs()[os.getpid()]
    argv = [a.decode() for a in Path("/proc/self/cmdline").read_bytes().rstrip(b"\0").split(b"\0")]
    assert (me["ppid"], me["argv"], me["uid"]) == (os.getppid(), argv, os.getuid()) and me["rss"] > 0
    (tmp_path / "7").mkdir()
    (tmp_path / "7" / "environ").write_bytes(b"HOME=/home/u\0SSH_CONNECTION=192.0.2.10 49460 192.0.2.20 22\0")
    assert probe.ssh_connection(7, str(tmp_path)) == ["192.0.2.10", 49460]
    assert probe.ssh_connection(8, str(tmp_path)) is None


def test_a_session_counts_only_while_its_registered_process_lives(tmp_path):
    (tmp_path / "30.json").write_text('{"pid": 30, "procStart": "0", "name": "drivers-a5", "cwd": "/k"}')
    (tmp_path / "31.json").write_text('{"pid": 31, "procStart": "999", "name": "reused-pid", "cwd": "/k"}')
    assert probe.load_sessions(PROCS, str(tmp_path)) == {30: {"name": "drivers-a5", "cwd": "/k"}}


def test_a_host_keeps_the_probes_last_reading_and_says_when_the_connection_closed():
    h = remote.Host("here", 0.2, command=["timeout", "1.5", sys.executable, "-u", "-", "0.2"])
    assert h.connect()
    r = h.as_dict(time.time(), {})
    assert not r["connected"] and r["error"] == "the connection closed (exit 124)" and r["age"] < 1
    assert r["reading"]["cpus"] == os.cpu_count() and r["reading"]["ram_total"] > r["reading"]["ram_available"] > 0
    assert isinstance(r["reading"]["jobs"], list)


def test_a_host_that_cannot_be_reached_says_why():
    fail = "import sys; sys.stdin.read(); sys.stderr.write('ssh: Could not resolve hostname nope\\n'); sys.exit(255)"
    h = remote.Host("nope", 3, command=[sys.executable, "-c", fail])
    assert not h.connect()
    assert h.as_dict(time.time(), {}) == {"alias": "nope", "connected": False, "error": "ssh: Could not resolve hostname nope",
                                          "age": None, "reading": None}


WRAPPED = ("/bin/bash -c source /home/u/.claude/shell-snapshots/snapshot-bash-1-x.sh 2>/dev/null || true && "
           "eval 'cd ~/x && pixi run test '\"'\"'-k gpu'\"'\"'' \\< /dev/null && pwd -P >| /tmp/claude-541a-cwd")


def test_a_claude_bash_command_is_shown_without_its_wrapper():
    assert remote.unwrap(WRAPPED) == "cd ~/x && pixi run test '-k gpu'"
    assert remote.unwrap(WRAPPED[:-20]) == "cd ~/x && pixi run test '-k gpu'"  # cut off after the command
    cut = WRAPPED[:WRAPPED.index("pixi")]
    assert remote.unwrap(cut) == cut  # cut off inside it
    assert remote.unwrap("bash -c cd x && eval 'y'") == "bash -c cd x && eval 'y'"


ME = remote._ip("192.0.2.10")
AGENT = owners.Owner("agent-locks", "agent-locks-22")


def _job(**kw):
    return {"pid": 5, "start": 1000.0, "cwd": "", "session": None, "ssh": None, **kw}


@pytest.mark.parametrize(("job", "clients", "owner", "via"), [
    (_job(session={"name": "drivers-a5", "cwd": "/home/u/work/drivers"}), {},
     owners.Owner("drivers", "drivers-a5"), "Claude session drivers-a5 on server"),
    (_job(ssh=["192.0.2.10", 49460]), {(ME, 49460): remote.Client(77, 990.0, 2000.0, AGENT)},
     AGENT, "ssh from here, pid 77"),
    (_job(ssh=["::ffff:192.0.2.10", 49460]), {(ME, 49460): remote.Client(77, 990.0, 1500.0, AGENT)},
     AGENT, "ssh from here, pid 77, which has since exited"),
    # the port's client had exited long before the job started: someone else's connection reused it
    (_job(ssh=["192.0.2.10", 49460], start=1600.0), {(ME, 49460): remote.Client(77, 990.0, 1500.0, AGENT)},
     owners.Owner(""), "ssh from here, by a client that exited before lockview saw it"),
    (_job(ssh=["192.0.2.30", 51000]), {(ME, 49460): remote.Client(77, 990.0, 2000.0, AGENT)},
     owners.Owner(""), "ssh from 192.0.2.30"),
    (_job(cwd="/home/u/work/drivers"), {}, owners.Owner("drivers"), ""),
    (_job(cwd="/home/u"), {}, owners.Owner(""), ""),
])
def test_who_started_a_remote_job(job, clients, owner, via):
    assert remote.owner_of(job, "/home/u", "server", clients, now=2000.0) == (owner, via)


def test_ssh_clients_are_found_by_their_sockets_and_remembered_for_a_day(tmp_path, monkeypatch):
    fd = tmp_path / "77" / "fd"
    fd.mkdir(parents=True)
    os.symlink("socket:[4242]", fd / "3")
    os.symlink("/dev/null", fd / "0")
    (tmp_path / "net").mkdir()
    head = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    (tmp_path / "net" / "tcp").write_text(head + "   0: 0A0200C0:C134 140200C0:0016 01 00000000:00000000 02:000A 00000000  1000        0 4242 1\n")
    (tmp_path / "net" / "tcp6").write_text(head + "   0: 0000000000000000FFFF00000A0200C0:C135 00000000000000000000000000000000:0000 0A 0 0 0 1000 0 9999 1\n")
    snap = procs.Snapshot({77: procs.Proc(77, 1, "ssh", ("ssh", "server"), 100), 78: procs.Proc(78, 1, "bash", ("bash",), 100)}, 0.0)
    assert remote.ssh_clients(snap, tmp_path) == {(ME, 49460): 77}

    r = remote.Remotes((), 3.0)
    monkeypatch.setattr(remote, "ssh_clients", lambda snap: {(ME, 49460): 77})
    r.note_clients(snap, {}, 1000.0)
    assert r.clients == {(ME, 49460): remote.Client(77, 100 / procs.CLK_TCK, 1000.0, owners.Owner(""))}
    monkeypatch.setattr(remote, "ssh_clients", lambda snap: {})
    r.note_clients(snap, {}, 1000.0 + remote.KEEP - 1)
    assert (ME, 49460) in r.clients
    r.note_clients(snap, {}, 1000.0 + remote.KEEP)
    assert r.clients == {}
