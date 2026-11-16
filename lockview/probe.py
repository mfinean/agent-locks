"""lockview's probe of another machine. lockview runs it there over ssh (`python3 -u - <interval>`, the
source on stdin), and it prints one JSON line of the machine's readings every interval until the
connection closes.

It stands alone: the standard library and Python 3.8. Like lockview, it only reads: /proc,
~/.claude/sessions, nvidia-smi and the Docker API.

A job is a process tree that something started: the topmost process below a boundary, which is an
init (a container's too, if all it does is start or wait), sshd, tmux, screen, a Claude session or an
interactive shell, together with its descendants. `ssh host 'cmd'` gives one job, its `bash -c cmd`, and so does each command a Claude
session runs. Only jobs that use a GPU, half a core or 2 GB are sent.

Some jobs are driven from another job, and belong to whoever started that one: a process `docker exec`
started in a container is driven by that docker client, and a Bazel server by the Bazel client whose
command it runs.

If lockview serves there too, the probe relays its timeline (its /api/timeline) every TIMELINE_EVERY seconds.
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import time

MIN_CPU = 0.5  # cores
MIN_RSS = 2 * 2**30
TOP = 5  # a job's heaviest processes sent
MAX_COMMAND = 2000
BOUNDARIES = {"init", "systemd", "tmux: server", "screen", "SCREEN", "claude", "containerd-shim", "containerd-shim-runc-v2"}
SHELLS = {"bash", "sh", "dash", "zsh", "fish"}
# A container's init that only starts or waits; an interactive shell is a boundary anyway.
INITS = {"docker-init", "tini", "dumb-init", "catatonit", "sleep", "tail", "s6-svscan", "runsvdir", "supervisord"}
BAZEL_CLIENTS = {"bazel", "bazel-real"}  # bazel-real: as apt's bazel runs it
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")
PF_KTHREAD = 0x00200000  # a kernel thread, in /proc/<pid>/stat's flags
DOCKER = "/var/run/docker.sock"
TIMELINE_EVERY = 20.0  # seconds between relays of lockview's timeline: it reads its history as often
EXEC_VALUES = {"-e", "--env", "--env-file", "-u", "--user", "-w", "--workdir", "--detach-keys"}  # docker exec's options with a value


def read_procs(root="/proc"):
    """pid -> its parent, session, comm, argv, owner's uid, CPU ticks so far (its own, and those of the
    children it has reaped), start (ticks after boot), RSS (bytes) and whether it is a zombie. Kernel threads
    are left out. A process that is exiting or a zombie has no argv, but stays: its time is not yet its
    parent's."""
    out = {}
    for name in os.listdir(root):
        if not name.isdigit():
            continue
        d = os.path.join(root, name)
        try:
            with open(d + "/stat") as f:
                stat = f.read()
            with open(d + "/cmdline", "rb") as f:
                raw = f.read()
            uid = os.stat(d).st_uid
        except OSError:
            continue  # it exited
        rest = stat[stat.rindex(")") + 2:].split()  # rest[0] is field 3 (state), so field n is rest[n - 3]
        if int(rest[6]) & PF_KTHREAD:
            continue
        zombie = rest[0] == "Z"
        # Fields 14 and 15 are its own user and system time, 16 and 17 its reaped children's.
        out[int(name)] = {
            "ppid": int(rest[1]), "sid": int(rest[3]), "comm": stat[stat.index("(") + 1:stat.rindex(")")],
            "argv": [a.decode(errors="replace") for a in raw.rstrip(b"\0").split(b"\0")] if raw else [], "uid": uid,
            "ticks": int(rest[11]) + int(rest[12]), "reaped": int(rest[13]) + int(rest[14]), "start": int(rest[19]),
            "rss": int(rest[21]) * PAGE, "zombie": zombie,
        }
    return out


def is_boundary(pid, p):
    """A process that starts jobs rather than being part of one."""
    if pid <= 1 or p["comm"] in BOUNDARIES or p["comm"].startswith("sshd") or p.get("boundary") \
            or (p.get("container_init") and p["comm"] in INITS):
        return True
    # An interactive shell: a session's leader, as in a tmux pane or an ssh login, that runs no -c script.
    return p["comm"] in SHELLS and p["sid"] == pid and "-c" not in p["argv"][1:]


def job_root(pid, procs):
    """The topmost process at or above pid that is still below the nearest boundary."""
    if is_boundary(pid, procs[pid]):
        return pid
    for _ in range(len(procs)):
        parent = procs[pid]["ppid"]
        if parent not in procs or is_boundary(parent, procs[parent]):
            break
        pid = parent
    return pid


def _shim_id(argv):
    """The id of the container a containerd shim runs, from its -id argument."""
    for i, a in enumerate(argv[:-1]):
        if a == "-id":
            return argv[i + 1]
    return None


def _shims(procs):
    """pid -> container id of each containerd shim."""
    out = {}
    for pid, p in procs.items():
        if p["comm"].startswith("containerd-shim") and _shim_id(p["argv"]):
            out[pid] = _shim_id(p["argv"])
    return out


def mark_container_inits(procs, root="/proc"):
    """Mark each container's init, container_init: the child of a containerd shim that is pid 1 in its own pid
    namespace. The shim's other children are processes docker exec started."""
    for pid, p in procs.items():
        parent = procs.get(p["ppid"])
        if not parent or not parent["comm"].startswith("containerd-shim"):
            continue
        try:
            with open(f"{root}/{pid}/status") as f:
                nspid = next((line.split()[1:] for line in f if line.startswith("NSpid:")), [])
        except OSError:
            continue
        p["container_init"] = nspid[-1:] == ["1"]


def container_of(pid, procs, names):
    """The name of the container pid runs in (its id, cut short, if the Docker API gave no name), or None."""
    for _ in range(len(procs)):
        p = procs.get(pid)
        if p is None:
            return None
        cid = _shim_id(p["argv"]) if p["comm"].startswith("containerd-shim") else None
        if cid:
            return names.get(cid) or cid[:12]
        pid = p["ppid"]
    return None


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class Docker:
    """The Docker API's names for the containers whose shims run here, and the command docker exec gave each
    process it started in one. It is asked only about what it hasn't been asked about yet."""

    def __init__(self, path=DOCKER):
        self.path = path
        self.names = {}  # container id -> name
        self.execs = {}  # (pid, start) of a process docker exec started -> (container id, the command it was given)
        self.tries = {}  # (pid, start) -> times the API was asked about it and knew no exec for it
        self.error = ""

    def get(self, url):
        """The API's JSON reply, or None for an object it doesn't have."""
        c = _UnixConnection(self.path, 2)
        try:
            c.request("GET", url)
            r = c.getresponse()
            body = r.read()
        finally:
            c.close()
        if r.status == 404:
            return None
        if r.status != 200:
            raise OSError(f"{url}: HTTP {r.status}")
        return json.loads(body)

    def update(self, procs):
        shims = _shims(procs)
        execd = {(pid, p["start"]): shims[p["ppid"]] for pid, p in procs.items()
                 if p["ppid"] in shims and not p.get("container_init")}
        self.names = {cid: n for cid, n in self.names.items() if cid in shims.values()}
        self.execs = {k: v for k, v in self.execs.items() if k in execd}
        self.tries = {k: n for k, n in self.tries.items() if k in execd}
        new = {k: cid for k, cid in execd.items() if k not in self.execs and self.tries.get(k, 0) < 2}
        try:
            for cid in (set(shims.values()) - set(self.names)) | set(new.values()):
                info = self.get(f"/containers/{cid}/json")
                if info is None:
                    continue  # it has since gone
                self.names[cid] = info["Name"].lstrip("/")
                if cid not in new.values():
                    continue
                for eid in info.get("ExecIDs") or []:
                    e = self.get(f"/exec/{eid}/json")
                    key = (e["Pid"], procs[e["Pid"]]["start"]) if e and e["Pid"] in procs else None
                    if key in new:
                        c = e["ProcessConfig"]
                        self.execs[key] = (cid, [c["entrypoint"], *c["arguments"]])
            for k in new:
                if k not in self.execs:
                    self.tries[k] = self.tries.get(k, 0) + 1
            self.error = ""
        except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError) as e:
            self.error = f"{self.path}: {e}"


def exec_args(argv):
    """(container, command) from a `docker exec` command line, or None for any other docker command."""
    i = 2 if argv[1:2] == ["exec"] else 3 if argv[1:3] == ["container", "exec"] else len(argv)
    while i < len(argv) and argv[i].startswith("-"):
        a = argv[i]
        if a in EXEC_VALUES:
            i += 1  # its value is the next word
        elif not a.startswith("--"):
            for j, ch in enumerate(a[1:], 1):  # -it, or -itw /x, or -uroot
                if "-" + ch in EXEC_VALUES:
                    i += j == len(a) - 1
                    break
        i += 1
    return (argv[i], argv[i + 1:]) if i < len(argv) else None


def _readlink(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def _flag(argv, name):
    """The value of a --name=value argument, or None."""
    prefix = f"--{name}="
    return next((a[len(prefix):] for a in argv if a.startswith(prefix)), None)


def _under(path, top):
    return path == top or path.startswith(top.rstrip("/") + "/")


def links(procs, docker, root="/proc"):
    """job root -> the link to the process that drives it from outside: {"kind": "docker exec", "pid": the docker
    client, "container": its name} or {"kind": "bazel client", "pid": the client}.

    A docker client drives the process its exec started: the client for that container whose command is the
    exec's. Of several, the newest exec takes the newest client that started before it, and so on back, as
    clients' execs start in the order the clients did. A Bazel client drives the server of the innermost
    workspace its directory is in, in its own mount namespace, under its --output_user_root and with its
    --output_base, if it gives them; a client that fits two servers drives neither, as nothing tells which.
    Of a server's clients, the oldest drives it, and the others wait for it. bazelisk runs the client as a
    `bazel` of its own, so a client that runs another is left out."""
    out = {}
    clients = []
    for pid, p in procs.items():
        parsed = exec_args(p["argv"]) if p["comm"] == "docker" else None
        if parsed:
            clients.append((p["start"], pid, parsed[0], parsed[1]))
    clients.sort()
    used = set()
    names = set(docker.names.values())
    for (pid, start), (cid, command) in sorted(docker.execs.items(), key=lambda kv: kv[0][1], reverse=True):
        if pid not in procs or procs[pid]["start"] != start:
            continue
        name = docker.names.get(cid, "")
        best = None
        for c_start, c_pid, ref, c_command in clients:  # oldest first, so the last match started last
            if c_pid not in used and c_start <= start and c_command == command and (
                    ref == name or (cid.startswith(ref) and ref not in names)):
                best = c_pid
        if best is not None:
            used.add(best)
            out[job_root(pid, procs)] = {"kind": "docker exec", "pid": best, "container": name or cid[:12]}

    servers = {}  # pid -> (workspace, output base, mount namespace)
    for pid, p in procs.items():
        workspace = _flag(p["argv"], "workspace_directory")
        if workspace and p["argv"][0].startswith("bazel("):
            servers[pid] = (workspace, _flag(p["argv"], "output_base") or "", _readlink(f"{root}/{pid}/ns/mnt"))
    bazels = {pid for pid, p in procs.items() if p["comm"] in BAZEL_CLIENTS}
    driving = {}  # server -> its clients, (start, pid)
    for pid in bazels - {procs[b]["ppid"] for b in bazels}:
        cwd, ns = _readlink(f"{root}/{pid}/cwd"), _readlink(f"{root}/{pid}/ns/mnt")
        user_root, own_base = _flag(procs[pid]["argv"], "output_user_root"), _flag(procs[pid]["argv"], "output_base")
        fits = [(len(workspace), server) for server, (workspace, base, server_ns) in servers.items()
                if cwd and ns and ns == server_ns and _under(cwd, workspace)
                and (user_root is None or _under(base, user_root)) and own_base in (None, base)]
        innermost = [server for length, server in fits if length == max(fits)[0]] if fits else []
        if len(innermost) == 1:
            driving.setdefault(innermost[0], []).append((procs[pid]["start"], pid))
    for server, found in driving.items():
        out[job_root(server, procs)] = {"kind": "bazel client", "pid": min(found)[1]}
    return out


def session_of(pid, procs, sessions):
    """The Claude session at or above pid, if one registered itself."""
    for _ in range(len(procs)):
        if pid in sessions:
            return sessions[pid]
        if pid not in procs:
            return None
        pid = procs[pid]["ppid"]
    return None


def load_sessions(procs, directory):
    """Registered Claude sessions whose process is still the one that registered (pids get reused)."""
    out = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name)) as f:
                d = json.load(f)
            pid = int(d["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if pid in procs and str(d.get("procStart", procs[pid]["start"])) == str(procs[pid]["start"]):
            out[pid] = {"name": d.get("name") or d.get("sessionId", "")[:8], "cwd": d.get("cwd", "")}
    return out


def lockview_timeline(procs, directory):
    """(the timeline of a lockview serving here, why there is none): (None, "") if no lockview serves here.
    directory: where lockview's servers say on which port they listen (server.announce())."""
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None, ""
    error = ""
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name)) as f:
                d = json.load(f)
            pid, port = int(d["pid"]), int(d["port"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if pid not in procs or procs[pid]["start"] != d.get("start"):
            continue  # that server has exited
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
        try:
            c.request("GET", "/api/timeline")
            r = c.getresponse()
            body = r.read()
            if r.status != 200:
                raise OSError(f"HTTP {r.status}")
            timeline = json.loads(body)
        except (OSError, http.client.HTTPException, ValueError) as e:
            error = f"lockview there didn't answer on port {port}: {e}"
            continue
        finally:
            c.close()
        if "error" in timeline:
            lines = [x for x in str(timeline["error"]).splitlines() if x.strip()]
            error = "lockview there failed: " + (lines[-1].strip() if lines else "")
            continue
        return timeline, ""
    return None, error


def ssh_connection(pid, root="/proc"):
    """The [client ip, client port] an ssh login gave pid's environment, if pid is ours to read."""
    try:
        with open(f"{root}/{pid}/environ", "rb") as f:
            env = f.read().split(b"\0")
    except OSError:
        return None
    for var in env:
        if var.startswith(b"SSH_CONNECTION="):
            words = var[15:].decode(errors="replace").split()
            return [words[0], int(words[1])] if len(words) == 4 else None
    return None


def cores_used(before, procs, seconds):
    """pid -> cores each process used since `before`, a reading `seconds` older. A process that started since
    used all its time since; a pid in both readings with another start time is another process.

    A process's own time misses children that start and end between readings, such as a make's compiles,
    so it is also charged the time of the children it reaped since, less what they had used by `before`,
    which was counted then. A boundary is charged its own time alone: the children it reaps were jobs of
    their own. So is a process that reaped a boundary since, with the jobs that boundary reaped: what it
    reaped can't be told apart from its own children's time. Reaped time never counts below nothing, as
    children reaped for a parent that ignores SIGCHLD add nothing to its count."""
    span = seconds * CLK_TCK
    if span <= 0:
        return {}
    alive = {pid for pid, p in procs.items() if pid in before and before[pid]["start"] == p["start"]}
    counted = {}  # a live process -> what its children that have since ended had used by `before`
    whole = set()  # live processes that reaped a boundary since
    for pid, p in before.items():
        if pid in alive:
            continue
        reaper, boundary = p["ppid"], is_boundary(pid, p)
        for _ in range(len(before)):  # a parent that ended too took the child's time with it to its own parent
            if reaper in alive or reaper not in before:
                break
            boundary = boundary or is_boundary(reaper, before[reaper])
            reaper = before[reaper]["ppid"]
        if reaper in alive:
            counted[reaper] = counted.get(reaper, 0) + p["ticks"] + p["reaped"]
            if boundary:
                whole.add(reaper)
    out = {}
    for pid, p in procs.items():
        was = before[pid] if pid in alive else {"ticks": 0, "reaped": 0}
        ticks = p["ticks"] - was["ticks"]
        if not is_boundary(pid, p) and pid not in whole:
            ticks += max(0, p["reaped"] - was["reaped"] - counted.get(pid, 0))
        out[pid] = max(0, ticks) / span
    return out


def jobs(procs, cpu, vram, sessions, user_of, root="/proc", links=None, names=None):
    """The heavy jobs. cpu: pid -> cores over the last interval; vram: pid -> MiB on any GPU; links: from links();
    names: container id -> name.

    A job's origin is the process at the far end of its links, or its root without one: its session and ssh
    connection are the origin's, and origin gives its pid, start and directory. links lists them from the job
    out."""
    links, names = links or {}, names or {}
    groups = {}
    for pid in procs:
        groups.setdefault(job_root(pid, procs), []).append(pid)
    out = []
    for top, members in groups.items():
        use = {pid: (cpu.get(pid, 0.0), procs[pid]["rss"], vram.get(pid, 0)) for pid in members}
        cores = sum(c for c, _, _ in use.values())
        rss = sum(r for _, r, _ in use.values())
        vram_mb = sum(v for _, _, v in use.values())
        if cores < MIN_CPU and rss < MIN_RSS and not vram_mb:
            continue
        heaviest = sorted(members, key=lambda pid: (use[pid][2] > 0, use[pid][0], use[pid][1]), reverse=True)[:TOP]
        command = {pid: " ".join(procs[pid]["argv"])[:MAX_COMMAND] or f"[{procs[pid]['comm']}]" for pid in (top, *heaviest)}
        p = procs[top]
        chain, origin = [], top
        while job_root(origin, procs) in links and len(chain) <= len(links):
            chain.append(links[job_root(origin, procs)])
            origin = chain[-1]["pid"]
        out.append({
            "pid": top, "user": user_of(p["uid"]), "command": command[top], "start": p["start"],
            "cpu": round(cores, 2), "rss": rss, "vram_mb": vram_mb, "procs": len(members), "cwd": _readlink(f"{root}/{top}/cwd") or "",
            "session": session_of(origin, procs, sessions), "ssh": ssh_connection(origin, root),
            "origin": {"pid": origin, "start": procs[origin]["start"], "cwd": _readlink(f"{root}/{origin}/cwd") or ""},
            "links": chain, "container": container_of(top, procs, names),
            "top": [{"pid": pid, "command": command[pid], "cpu": round(use[pid][0], 2),
                     "rss": use[pid][1], "vram_mb": use[pid][2]} for pid in heaviest],
        })
    return sorted(out, key=lambda j: j["start"])


def smi(*query):
    out = subprocess.run(["nvidia-smi", *query, "--format=csv,noheader,nounits"], capture_output=True, text=True,
                         timeout=10, check=True).stdout
    return [[c.strip() for c in line.split(",")] for line in out.splitlines() if line.strip()]


def gpus():
    """Every GPU, the VRAM each compute process uses (pid -> MiB), and why nvidia-smi failed, if it did."""
    try:
        cards = [{"index": int(i), "name": name, "total_mb": int(total), "used_mb": int(used), "util_pct": int(util)}
                 for i, name, total, used, util in smi("--query-gpu=index,name,memory.total,memory.used,utilization.gpu")]
        vram = {}
        for pid, mb in smi("--query-compute-apps=pid,used_memory"):
            vram[int(pid)] = vram.get(int(pid), 0) + int(mb)
    except FileNotFoundError:
        return [], {}, ""  # no NVIDIA driver: a machine without a GPU
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        detail = (getattr(e, "stderr", "") or str(e)).strip()
        return [], {}, detail.splitlines()[0] if detail else type(e).__name__
    return cards, vram, ""


def cpu_times():
    """(busy, total) ticks of all CPUs since boot, as lockview's resources.cpu_times() counts them."""
    with open("/proc/stat") as f:
        user, nice, system, idle, iowait, irq, softirq, steal = (int(v) for v in f.readline().split()[1:9])
    busy = user + nice + system + irq + softirq + steal
    return busy, busy + idle + iowait


def meminfo():
    out = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, value = line.split(":", 1)
            out[key] = int(value.split()[0]) * 1024
    return out


def pressure():
    """System-wide memory pressure: avg10 of `some` and `full` (percent), or None without PSI."""
    try:
        with open("/proc/pressure/memory") as f:
            return {line.split()[0]: float(line.split()[1].split("=")[1]) for line in f if line.strip()}
    except (OSError, IndexError, ValueError):
        return None


def boot_time():
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime "):
                return float(line.split()[1])
    raise RuntimeError("/proc/stat has no btime")


def users():
    names = {}
    try:
        with open("/etc/passwd") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) > 2 and parts[2].isdigit():
                    names[int(parts[2])] = parts[0]
    except OSError:
        pass
    return lambda uid: names.get(uid, str(uid))


def main(interval):
    btime, user_of = boot_time(), users()
    sessions_dir = os.path.expanduser("~/.claude/sessions")
    serving = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "lockview", "serving")
    docker = Docker()
    before, cpu0, t0 = read_procs(), cpu_times(), time.time()
    relayed = 0.0  # when lockview's timeline was last relayed
    while True:
        time.sleep(interval)
        procs, cpu1, t1 = read_procs(), cpu_times(), time.time()
        mark_container_inits(procs)
        docker.update(procs)
        cores = cores_used(before, procs, t1 - t0)
        cards, vram, gpu_error = gpus()
        sessions = load_sessions(procs, sessions_dir)
        mem = meminfo()
        found = jobs(procs, cores, vram, sessions, user_of, links=links(procs, docker), names=docker.names)
        for j in found:
            j["start"] = btime + j["start"] / CLK_TCK
            j["origin"]["start"] = btime + j["origin"]["start"] / CLK_TCK
        busy, total = cpu1[0] - cpu0[0], cpu1[1] - cpu0[1]
        reading = {
            "v": 1, "t": t1, "host": os.uname()[1], "home": os.path.expanduser("~"), "cpus": os.cpu_count(),
            "load": list(os.getloadavg()), "cpu_pct": round(100 * busy / total, 1) if total > 0 else None,
            "ram_total": mem["MemTotal"], "ram_available": mem["MemAvailable"], "pressure": pressure(),
            "gpus": cards, "gpu_error": gpu_error, "docker_error": docker.error, "sessions": sorted(s["name"] for s in sessions.values()), "jobs": found,
        }
        if t1 - relayed >= TIMELINE_EVERY:
            relayed = t1
            reading["timeline"], reading["timeline_error"] = lockview_timeline(procs, serving)
        print(json.dumps(reading, separators=(",", ":")), flush=True)
        before, cpu0, t0 = procs, cpu1, t1


if __name__ == "__main__":
    try:
        main(float(sys.argv[1]) if len(sys.argv) > 1 else 3.0)
    except (BrokenPipeError, KeyboardInterrupt):
        pass  # lockview closed the connection
