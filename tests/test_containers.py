"""Work in containers and under Bazel servers, linked to the process that drives it (probe.py)."""

import http.server
import json
import os
import socketserver
import threading
import time

import pytest

from lockview import gate, owners, probe, procs, remote

CID = "b6bd726c32c1951331b2a20c87f2edbc1797a73c7be42eda7fd7195a3e3303b5"
OTHER = "3c8a14e1a3fe0000000000000000000000000000000000000000000000000000"
WS = "/home/k/ws/planner"
ROOT = "/home/k/.cache/bazel/root"
TEST = ["timeout", "1700", "nice", "-n", "10", "bin/build", "test", "//a:b"]
GB = 2**30


def _p(ppid, comm, argv, start=100, sid=0, rss=0, **extra):
    return {"ppid": ppid, "sid": sid, "comm": comm, "argv": argv, "uid": 1000, "ticks": 0, "reaped": 0, "start": start,
            "rss": rss, "zombie": False, **extra}


def _shim(cid):
    return ["/usr/bin/containerd-shim-runc-v2", "-namespace", "moby", "-id", cid, "-address", "/run/containerd/containerd.sock"]


# The host: a Claude session runs with-lock, whose docker exec runs a test in the container dev, and a
# client for another container runs the same command. In dev: its init, a Bazel server that daemonized
# into it with a compile under it, and the exec'd test, whose build script runs bazelisk, which runs the Bazel client.
PROCS = {
    1: _p(0, "systemd", ["/sbin/init"], sid=1),
    30: _p(1, "claude", ["claude"], sid=30),
    31: _p(30, "bash", ["/bin/bash", "-c", "with-lock 0 -- docker exec -w /x dev ..."], sid=31),
    32: _p(31, "bash", ["/bin/bash", "with-lock", "0", "--", "docker", "exec", "-w", WS, "dev", *TEST], sid=31),
    33: _p(32, "docker", ["/usr/bin/docker", "exec", "-w", WS, "dev", *TEST], start=4990, sid=31),
    34: _p(1, "docker", ["docker", "exec", "-it", "other", *TEST], start=4995, sid=34),
    200: _p(1, "containerd-shim", _shim(CID), sid=200),
    201: _p(200, "sleep", ["sleep", "infinity"], sid=201, container_init=True),
    210: _p(201, "java", ["bazel(planner)", "-jar", f"{ROOT}/install/A-server.jar", f"--output_user_root={ROOT}",
                          f"--output_base={ROOT}/abc", f"--workspace_directory={WS}"], start=300, sid=210, rss=3 * GB),
    211: _p(210, "process-wrapper", ["process-wrapper", "--timeout=0", "gcc"], start=5200, sid=210),
    212: _p(211, "cc1plus", ["cc1plus", "-quiet", "x.cc"], start=5201, sid=210, rss=GB),
    220: _p(200, "timeout", TEST, start=5000, sid=220),
    221: _p(220, "python3", ["python3", "./bin/build", "test", "//a:b"], start=5050, sid=220),
    222: _p(221, "bazel", ["bazel", f"--output_user_root={ROOT}", "test", "//a:b"], start=5100, sid=220),
    223: _p(222, "bazel", ["/opt/bazelisk/bin/bazel", f"--output_user_root={ROOT}", "test", "//a:b"], start=5101, sid=220),
}
DRIVEN = {220: {"kind": "docker exec", "pid": 33, "container": "dev"}, 210: {"kind": "bazel client", "pid": 223}}


class FakeDocker:
    names = {CID: "dev", OTHER: "other"}
    execs = {(220, 5000): (CID, TEST)}
    error = ""

    @staticmethod
    def update(procs):
        pass


def _proc_dir(tmp_path, cwds=None, mnt=None, nspid=None):
    """A /proc with each process's cwd, mount namespace and NSpid line, as given."""
    for pid, cwd in (cwds or {}).items():
        d = tmp_path / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        os.symlink(cwd, d / "cwd")
    for pid, ns in (mnt or {}).items():
        (tmp_path / ns).touch()
        (tmp_path / str(pid) / "ns").mkdir(parents=True, exist_ok=True)
        os.symlink(tmp_path / ns, tmp_path / str(pid) / "ns" / "mnt")
    for pid, ids in (nspid or {}).items():
        (tmp_path / str(pid)).mkdir(parents=True, exist_ok=True)
        (tmp_path / str(pid) / "status").write_text("Name:\tx\n" + "NSpid:\t" + "\t".join(map(str, ids)) + "\n")
    return str(tmp_path)


def test_a_containers_init_is_found_by_its_pid_in_its_own_namespace(tmp_path):
    table = {k: {kk: vv for kk, vv in v.items() if kk != "container_init"} for k, v in PROCS.items()}
    root = _proc_dir(tmp_path, nspid={201: (201, 1), 220: (220, 87), 32: (32,)})
    probe.mark_container_inits(table, root)
    assert [pid for pid, p in table.items() if p.get("container_init")] == [201]


def test_a_containers_init_is_a_boundary_so_its_daemons_are_jobs_of_their_own():
    assert probe.is_boundary(201, PROCS[201]) and not probe.is_boundary(201, {**PROCS[201], "container_init": False})
    assert [probe.job_root(pid, PROCS) for pid in (212, 223, 33)] == [210, 220, 31]


@pytest.mark.parametrize("comm, argv, boundary", [
    ("docker-init", ["/sbin/docker-init", "--", "python3", "server.py"], True),
    ("tini", ["tini", "--", "python3", "server.py"], True),
    ("sleep", ["sleep", "infinity"], True),
    ("bash", ["/bin/bash"], True),  # an interactive shell: `docker run -it img bash`
    ("bash", ["bash", "-c", "make -j8"], False),
    ("make", ["make", "-j8"], False),  # `docker run img make -j8`: the init is the job
    ("python3", ["python3", "train.py"], False),
])
def test_a_containers_init_is_a_boundary_only_if_all_it_does_is_start_or_wait(comm, argv, boundary):
    init = _p(200, comm, argv, sid=201, container_init=True)
    table = {**PROCS, 201: init, 202: _p(201, "cc1plus", ["cc1plus", "x.cc"], sid=201)}
    assert (probe.is_boundary(201, init), probe.job_root(202, table)) == (boundary, 202 if boundary else 201)


def test_a_docker_exec_is_driven_by_its_client_and_a_bazel_server_by_its_client_in_its_workspace(tmp_path):
    # bazelisk runs the client, so both are `bazel` in the workspace: the client is the innermost.
    root = _proc_dir(tmp_path, cwds={222: WS + "/src", 223: WS + "/src", 221: WS}, mnt={210: "mnt-dev", 222: "mnt-dev", 223: "mnt-dev"})
    assert probe.links(PROCS, FakeDocker, root) == DRIVEN


def test_a_bazel_client_drives_the_server_of_the_innermost_workspace_it_is_in(tmp_path):
    outer = {**PROCS, 230: _p(201, "java", ["bazel(k)", f"--output_base={ROOT}/def", "--workspace_directory=/home/k/ws"], start=200)}
    root = _proc_dir(tmp_path, cwds={223: WS + "/src"}, mnt={210: "mnt-dev", 223: "mnt-dev", 230: "mnt-dev"})
    assert probe.links(outer, FakeDocker, root)[210] == DRIVEN[210] and 230 not in probe.links(outer, FakeDocker, root)


def test_a_bazel_client_installed_by_apt_runs_as_bazel_real(tmp_path):
    apt = {**PROCS, 223: _p(222, "bazel-real", ["/usr/bin/bazel-real", "test", "//a:b"], start=5101)}
    root = _proc_dir(tmp_path, cwds={223: WS}, mnt={210: "mnt-dev", 223: "mnt-dev"})
    assert probe.links(apt, FakeDocker, root)[210] == DRIVEN[210]


def test_a_bazel_client_drives_the_server_of_its_output_base_and_no_server_when_it_cant_tell(tmp_path):
    # Two servers for one workspace, under two output bases.
    two = {**PROCS, 230: _p(201, "java", ["bazel(planner)", "--output_base=/home/k/.cache/bazel/_bazel_k/f00",
                                          f"--workspace_directory={WS}"], start=200)}
    root = _proc_dir(tmp_path, cwds={223: WS}, mnt={210: "mnt-dev", 223: "mnt-dev", 230: "mnt-dev"})
    by_base = {**two, 223: _p(222, "bazel", ["bazel", "--output_base=/home/k/.cache/bazel/_bazel_k/f00", "build"], start=5101)}
    assert probe.links(by_base, FakeDocker, root)[230] == DRIVEN[210] and 210 not in probe.links(by_base, FakeDocker, root)
    plain = {**two, 223: _p(222, "bazel", ["bazel", "build"], start=5101)}
    assert not {210, 230} & set(probe.links(plain, FakeDocker, root))


def test_of_two_clients_for_one_server_the_oldest_drives_it_and_the_other_waits_its_turn(tmp_path):
    second = {**PROCS, 240: _p(1, "bazel", ["bazel", "build", "//c"], start=5150)}
    root = _proc_dir(tmp_path, cwds={223: WS, 240: WS}, mnt={210: "mnt-dev", 223: "mnt-dev", 240: "mnt-dev"})
    assert probe.links(second, FakeDocker, root)[210] == DRIVEN[210]


def test_a_bazel_server_is_not_driven_by_a_client_of_another_workspace_output_root_or_container(tmp_path):
    other_root = {**PROCS, 223: _p(222, "bazel", ["bazel", "--output_user_root=/elsewhere", "test", "//a:b"], start=5101)}
    root = _proc_dir(tmp_path, cwds={223: WS}, mnt={210: "mnt-dev", 223: "mnt-dev"})
    assert 210 not in probe.links(other_root, FakeDocker, root)
    elsewhere = _proc_dir(tmp_path / "b", cwds={223: "/home/k/ws/planner-2"}, mnt={210: "mnt-dev", 223: "mnt-dev"})
    assert 210 not in probe.links(PROCS, FakeDocker, elsewhere)
    another_container = _proc_dir(tmp_path / "c", cwds={223: WS}, mnt={210: "mnt-dev", 223: "mnt-host"})
    assert 210 not in probe.links(PROCS, FakeDocker, another_container)
    unreadable = _proc_dir(tmp_path / "d", cwds={223: WS})
    assert 210 not in probe.links(PROCS, FakeDocker, unreadable)


def test_of_two_clients_with_the_same_command_the_one_for_its_container_that_started_last_before_it_drives_it():
    table = {**PROCS, 35: _p(31, "docker", ["docker", "exec", "-w", WS, "dev", *TEST], start=4000),
             36: _p(31, "docker", ["docker", "exec", "dev", *TEST], start=6000)}  # started after it: not its client
    assert probe.links(table, FakeDocker, "/nonexistent")[220] == DRIVEN[220]


def test_two_execs_of_one_command_are_driven_by_a_client_each_in_the_order_they_started():
    # The second client started before the first exec did: each exec is still its own client's.
    table = {**PROCS, 35: _p(31, "docker", ["docker", "exec", "-it", CID[:12], *TEST], start=4995),
             225: _p(200, "timeout", TEST, start=5002, sid=225)}

    class Two(FakeDocker):
        execs = {(220, 5000): (CID, TEST), (225, 5002): (CID, TEST)}
    found = probe.links(table, Two, "/nonexistent")
    assert (found[220]["pid"], found[225]["pid"]) == (33, 35)


@pytest.mark.parametrize("argv, parsed", [
    (["docker", "exec", "-it", "-w", "/x", "--env=A=1", "-e", "B=2", "-uroot", "dev", "ls", "-l"], ("dev", ["ls", "-l"])),
    (["/usr/bin/docker", "container", "exec", "--workdir", "/x", "--detach", "dev", "ls"], ("dev", ["ls"])),
    (["docker", "exec", "-itw", "/x", "dev", "ls"], ("dev", ["ls"])),
    (["docker", "ps"], None),
    (["docker", "exec", "-it"], None),
])
def test_a_docker_exec_command_line_names_its_container_and_command(argv, parsed):
    assert probe.exec_args(argv) == parsed


def test_a_job_driven_from_outside_belongs_to_whoever_drives_it():
    cpu = {212: 6.0, 220: 0.1}
    sessions = {30: {"name": "rover-32", "cwd": "/home/u/rover"}}
    jobs = {j["pid"]: j for j in probe.jobs(PROCS, cpu, {}, sessions, str, root="/nonexistent", links=DRIVEN, names=FakeDocker.names)}
    server = jobs[210]  # the compile under the Bazel server
    assert (server["cpu"], server["session"]["name"], server["container"]) == (6.0, "rover-32", "dev")
    assert server["origin"] == {"pid": 33, "start": 4990, "cwd": ""}
    assert server["links"] == [DRIVEN[210], DRIVEN[220]]
    unnamed = {j["pid"]: j for j in probe.jobs(PROCS, cpu, {}, sessions, str, root="/nonexistent")}[210]
    assert (unnamed["links"], unnamed["origin"], unnamed["session"], unnamed["container"]) == ([], {"pid": 210, "start": 300, "cwd": ""}, None, CID[:12])


class _Api(http.server.BaseHTTPRequestHandler):
    routes: dict = {}
    asked: list = []

    def do_GET(self):
        self.asked.append(self.path)
        body = self.routes.get(self.path)
        self.send_response(200 if body is not None else 404)
        self.end_headers()
        self.wfile.write(json.dumps(body if body is not None else {"message": "no such object"}).encode())

    def log_message(self, *args):
        pass


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


@pytest.fixture
def api(tmp_path):
    _Api.routes = {
        f"/containers/{CID}/json": {"Id": CID, "Name": "/dev", "ExecIDs": ["e1", "e2"]},
        "/exec/e1/json": {"Running": True, "Pid": 220, "ProcessConfig": {"entrypoint": TEST[0], "arguments": TEST[1:]}},
        "/exec/e2/json": {"Running": False, "Pid": 0, "ProcessConfig": {"entrypoint": "true", "arguments": []}},
    }
    _Api.asked = []
    path = str(tmp_path / "docker.sock")
    server = _UnixServer(path, _Api)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield path
    server.shutdown()
    server.server_close()


def test_the_docker_api_names_the_containers_and_the_command_each_exec_was_given(api):
    docker = probe.Docker(api)
    docker.update(PROCS)
    assert (docker.names, docker.execs, docker.error) == ({CID: "dev"}, {(220, 5000): (CID, TEST)}, "")
    asked = len(_Api.asked)
    docker.update(PROCS)
    assert len(_Api.asked) == asked  # nothing new to ask about
    docker.update({k: v for k, v in PROCS.items() if k != 220})
    assert docker.execs == {}
    docker.update({k: v for k, v in PROCS.items() if k not in (200, 201, 220)})
    assert docker.names == {}


def test_an_exec_the_docker_api_doesnt_know_yet_is_asked_about_once_more(api):
    _Api.routes["/exec/e1/json"] = {"Running": True, "Pid": 0, "ProcessConfig": {"entrypoint": "x", "arguments": []}}
    docker = probe.Docker(api)
    for _ in range(3):
        docker.update(PROCS)
    assert docker.execs == {} and _Api.asked.count("/exec/e1/json") == 2


def test_a_container_gone_by_the_time_the_docker_api_is_asked_is_no_error(api):
    del _Api.routes[f"/containers/{CID}/json"]
    docker = probe.Docker(api)
    docker.update(PROCS)
    assert (docker.names, docker.execs, docker.error) == ({}, {}, "")


def test_a_command_line_keeps_its_empty_arguments_as_docker_exec_was_given_them():
    import subprocess
    import sys

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", "", "x"])
    try:
        deadline = time.time() + 10
        while len(procs.Snapshot.take().procs[p.pid].argv) < 5:
            assert time.time() < deadline
            time.sleep(0.01)
        assert procs.Snapshot.take().procs[p.pid].argv[-2:] == ("", "x") and probe.read_procs()[p.pid]["argv"][-2:] == ["", "x"]
    finally:
        p.kill()
        p.wait()


def test_without_the_docker_api_nothing_is_linked_and_it_says_why(tmp_path):
    docker = probe.Docker(str(tmp_path / "no.sock"))
    docker.update(PROCS)
    assert (docker.names, docker.execs) == ({}, {}) and "no.sock" in docker.error


SESSIONS = {30: owners.Session(30, "rover-32", "id", "rover", "", "busy")}
SCOPE = "/user.slice/user-1000.slice/user@1000.service/app.slice/agent-gate-31.scope"


def _outside(monkeypatch, links, gated=()):
    """outside() on this machine as two snapshots 3 s apart, over which the compile under the Bazel server
    used 2 cores; the processes in `gated` run in a gate job's scope."""
    def snap(seconds_in):
        table = {pid: procs.Proc(pid, p["ppid"], p["comm"], tuple(p["argv"]), p["start"], SCOPE if pid in gated else "",
                                 {}, p["sid"], 0, p["rss"]) for pid, p in PROCS.items() if pid != 34}
        table[212].ticks = round(2 * seconds_in * procs.CLK_TCK)
        return procs.Snapshot(table, 0.0, 1_000_000.0 + seconds_in)

    monkeypatch.setattr(probe, "mark_container_inits", lambda view, root="/proc": view[201].update(container_init=True))
    monkeypatch.setattr(probe, "links", lambda view, docker, root="/proc": links)
    monkeypatch.setattr(procs, "cwd", lambda pid: "")  # the host has none of these processes
    return {j["pid"]: j for j in gate.outside(snap(3.0), snap(0.0), {}, SESSIONS, FakeDocker)}


def test_outside_the_gate_lists_work_in_containers_as_owned_by_the_session_that_drives_it(monkeypatch):
    server = _outside(monkeypatch, DRIVEN)[210]
    assert (server["cpu"], server["owner"], server["container"]) == (2.0, owners.Owner("rover", "rover-32").as_dict(), "dev")
    assert server["via"] == "Claude session rover-32 → docker exec into dev (pid 33) → Bazel client (pid 223)"


def test_outside_the_gate_lists_a_containers_own_work_as_the_containers(monkeypatch):
    jobs = _outside(monkeypatch, {})
    assert (jobs[210]["owner"], jobs[210]["via"]) == (
        owners.Owner("dev").as_dict(), "container dev itself: no docker exec or Bazel client here drives it")


def test_outside_the_gate_leaves_out_work_in_containers_that_a_gate_job_drives(monkeypatch):
    assert 210 not in _outside(monkeypatch, DRIVEN, gated=(32, 33))


def test_a_remote_job_driven_from_outside_says_how():
    job = {"pid": 210, "start": 1000.0, "cwd": WS, "session": {"name": "rover-32", "cwd": "/home/u/rover"}, "ssh": None,
           "links": [DRIVEN[210], DRIVEN[220]], "container": "dev", "origin": {"pid": 33, "start": 5000.0, "cwd": "/home/u/rover"}}
    assert remote.owner_of(job, "/home/u", "server", {}, now=2000.0) == (
        owners.Owner("rover", "rover-32"), "Claude session rover-32 on server → docker exec into dev (pid 33) → Bazel client (pid 223)")
    alone = {**job, "session": None, "links": [], "origin": {"pid": 210, "start": 1000.0, "cwd": WS}}
    assert remote.owner_of(alone, "/home/u", "server", {}, now=2000.0) == (
        owners.Owner("dev"), "container dev itself: no docker exec or Bazel client here drives it")


def test_a_remote_job_driven_over_ssh_from_here_belongs_to_the_client_that_started_its_origin():
    # The Bazel server started hours before the ssh client here that runs the docker exec driving it.
    agent = owners.Owner("rover", "rover-32")
    job = {"pid": 210, "start": 1000.0, "cwd": WS, "session": None, "ssh": ["192.0.2.10", 51544], "container": "dev",
           "links": [DRIVEN[210], DRIVEN[220]], "origin": {"pid": 33, "start": 5000.0, "cwd": "/home/u/rover.worktrees/rate"}}
    clients = {(remote._ip("192.0.2.10"), 51544): remote.Client(48977, 4990.0, 6000.0, agent)}
    assert remote.owner_of(job, "/home/u", "server", clients, now=6000.0) == (
        agent, "ssh from here, pid 48977 → docker exec into dev (pid 33) → Bazel client (pid 223)")
    unknown = {**job, "ssh": None}  # nothing says who: the directory it was driven from names it
    assert remote.owner_of(unknown, "/home/u", "server", {}, now=6000.0)[0] == owners.Owner("rate")
