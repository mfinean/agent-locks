"""One read of /proc: every process's parent, command line, start time, CPU time, memory, cgroup and open
lock files.

Everything here only reads. Opening /proc/<pid>/fd/<n> would open the target itself, so lock files
are found by readlink and never opened.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

CLK_TCK = os.sysconf("SC_CLK_TCK")
PAGE = os.sysconf("SC_PAGE_SIZE")


def boot_time() -> float:
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime "):
                return float(line.split()[1])
    raise RuntimeError("/proc/stat has no btime")


@dataclass
class Proc:
    pid: int
    ppid: int
    comm: str
    argv: tuple[str, ...]
    start_ticks: int
    cgroup: str = ""
    lock_fds: dict[str, tuple[int, int]] = field(default_factory=dict)  # path -> (st_dev, st_ino)
    sid: int = 0  # its session's leader
    ticks: int = 0  # CPU time so far, user and system, in clock ticks
    rss: int = 0  # bytes

    def started_at(self, btime: float) -> float:
        return btime + self.start_ticks / CLK_TCK

    @property
    def cmdline(self) -> str:
        return " ".join(self.argv)


def _stat(pid: int | str) -> tuple[str, list[str]]:
    """comm, and the fields after it, from /proc/<pid>/stat: [0] is field 3 (state), so field n is [n - 3]."""
    with open(f"/proc/{pid}/stat") as f:
        data = f.read()
    return data[data.index("(") + 1 : data.rindex(")")], data[data.rindex(")") + 2 :].split()


def read_stat(pid: int | str) -> tuple[int, str, int]:
    """(ppid, comm, start ticks) from /proc/<pid>/stat."""
    comm, rest = _stat(pid)
    return int(rest[1]), comm, int(rest[19])


def _lock_fds(pid: str) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    try:
        fds = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return out
    for fd in fds:
        try:
            target = os.readlink(f"/proc/{pid}/fd/{fd}")
        except OSError:
            continue
        if not target.endswith(".lock") or target in out:
            continue
        try:
            st = os.stat(target)
        except OSError:
            continue
        out[target] = (st.st_dev, st.st_ino)
    return out


class Snapshot:
    def __init__(self, procs: dict[int, Proc], btime: float, taken: float = 0.0):
        self.procs = procs
        self.btime = btime
        self.taken = taken  # when it was read, for CPU use between two snapshots
        self.children: dict[int, list[int]] = {}
        for p in procs.values():
            self.children.setdefault(p.ppid, []).append(p.pid)

    @classmethod
    def take(cls) -> Snapshot:
        procs: dict[int, Proc] = {}
        taken = time.time()
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                comm, rest = _stat(name)
                with open(f"/proc/{name}/cmdline", "rb") as f:
                    argv = tuple(a.decode(errors="replace") for a in f.read().split(b"\0") if a)
                with open(f"/proc/{name}/cgroup") as f:
                    cgroup = f.read().strip().split("::", 1)[-1]
            except (OSError, ValueError, IndexError):
                continue  # exited mid-read, or a kernel thread
            # ppid is field 4, session 6, utime and stime 14 and 15, starttime 22, rss 24 (pages).
            procs[int(name)] = Proc(int(name), int(rest[1]), comm, argv, int(rest[19]), cgroup, _lock_fds(name),
                                    int(rest[3]), int(rest[11]) + int(rest[12]), int(rest[21]) * PAGE)
        return cls(procs, boot_time(), taken)

    def get(self, pid: int) -> Proc | None:
        return self.procs.get(pid)

    def ancestors(self, pid: int):
        seen = set()
        p = self.procs.get(pid)
        while p and p.ppid not in seen and p.ppid > 1:
            seen.add(p.ppid)
            p = self.procs.get(p.ppid)
            if p:
                yield p

    def descendants(self, pid: int):
        stack = list(self.children.get(pid, ()))
        while stack:
            c = stack.pop()
            if c in self.procs:
                yield self.procs[c]
                stack.extend(self.children.get(c, ()))

    def started_at(self, pid: int) -> float | None:
        p = self.procs.get(pid)
        return p.started_at(self.btime) if p else None


def exe(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""


def cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""

