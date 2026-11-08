"""lockview's probe of another machine. lockview runs it there over ssh (`python3 -u - <interval>`, the
source on stdin), and it prints one JSON line of the machine's readings every interval until the
connection closes.

It stands alone: the standard library and Python 3.8. Like lockview, it only reads: /proc,
~/.claude/sessions and nvidia-smi.

A job is a process tree that something started: the topmost process below a boundary, which is an
init, sshd, tmux, screen, a Claude session or an interactive shell, together with its descendants.
`ssh host 'cmd'` gives one job, its `bash -c cmd`, and so does each command a Claude session runs.
Only jobs that use a GPU, half a core or 2 GB are sent.
"""

import json
import os
import subprocess
import sys
import time

MIN_CPU = 0.5  # cores
MIN_RSS = 2 * 2**30
TOP = 5  # a job's heaviest processes sent
MAX_COMMAND = 2000
BOUNDARIES = {"init", "systemd", "tmux: server", "screen", "SCREEN", "claude", "containerd-shim", "containerd-shim-runc-v2"}
SHELLS = {"bash", "sh", "dash", "zsh", "fish"}
CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def read_procs(root="/proc"):
    """pid -> its parent, session, comm, argv, owner's uid, CPU ticks so far, start (ticks after boot) and RSS
    (bytes). Kernel threads, which have no argv, are left out."""
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
        if not raw:
            continue
        rest = stat[stat.rindex(")") + 2:].split()  # rest[0] is field 3 (state)
        out[int(name)] = {
            "ppid": int(rest[1]), "sid": int(rest[3]), "comm": stat[stat.index("(") + 1:stat.rindex(")")],
            "argv": [a.decode(errors="replace") for a in raw.rstrip(b"\0").split(b"\0")], "uid": uid,
            "ticks": int(rest[11]) + int(rest[12]), "start": int(rest[19]), "rss": int(rest[21]) * PAGE,
        }
    return out


def is_boundary(pid, p):
    """A process that starts jobs rather than being part of one."""
    if pid <= 1 or p["comm"] in BOUNDARIES or p["comm"].startswith("sshd"):
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


def jobs(procs, cpu, vram, sessions, user_of, root="/proc"):
    """The heavy jobs. cpu: pid -> cores over the last interval; vram: pid -> MiB on any GPU."""
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
        p = procs[top]
        try:
            cwd = os.readlink(f"{root}/{top}/cwd")
        except OSError:
            cwd = ""
        out.append({
            "pid": top, "user": user_of(p["uid"]), "command": " ".join(p["argv"])[:MAX_COMMAND], "start": p["start"],
            "cpu": round(cores, 2), "rss": rss, "vram_mb": vram_mb, "procs": len(members), "cwd": cwd,
            "session": session_of(top, procs, sessions), "ssh": ssh_connection(top, root),
            "top": [{"pid": pid, "command": " ".join(procs[pid]["argv"])[:MAX_COMMAND], "cpu": round(use[pid][0], 2),
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
    before, cpu0, t0 = read_procs(), cpu_times(), time.time()
    while True:
        time.sleep(interval)
        procs, cpu1, t1 = read_procs(), cpu_times(), time.time()
        span = (t1 - t0) * CLK_TCK
        cores = {pid: max(0, p["ticks"] - before[pid]["ticks"]) / span for pid, p in procs.items()
                 if pid in before and before[pid]["start"] == p["start"]}
        cards, vram, gpu_error = gpus()
        sessions = load_sessions(procs, sessions_dir)
        mem = meminfo()
        found = jobs(procs, cores, vram, sessions, user_of)
        for j in found:
            j["start"] = btime + j["start"] / CLK_TCK
        busy, total = cpu1[0] - cpu0[0], cpu1[1] - cpu0[1]
        print(json.dumps({
            "v": 1, "t": t1, "host": os.uname()[1], "home": os.path.expanduser("~"), "cpus": os.cpu_count(),
            "load": list(os.getloadavg()), "cpu_pct": round(100 * busy / total, 1) if total > 0 else None,
            "ram_total": mem["MemTotal"], "ram_available": mem["MemAvailable"], "pressure": pressure(),
            "gpus": cards, "gpu_error": gpu_error, "sessions": sorted(s["name"] for s in sessions.values()), "jobs": found,
        }, separators=(",", ":")), flush=True)
        before, cpu0, t0 = procs, cpu1, t1


if __name__ == "__main__":
    try:
        main(float(sys.argv[1]) if len(sys.argv) > 1 else 3.0)
    except (BrokenPipeError, KeyboardInterrupt):
        pass  # lockview closed the connection
