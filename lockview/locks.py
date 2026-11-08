"""Exclusive flock(1)/flock(2) locks that agents take around jobs, other than the gate's own slots.

/proc/locks says which inodes are locked and which processes are blocked on them; its holder pid is
often a `flock` process that has already exited (`exec 9>f; flock 9`), so holders are found the other
way round: every live process with the file open that isn't itself waiting, nor the parent of a waiter.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import owners, resources
from .procs import Snapshot

HOME = str(Path.home())
IGNORED = re.compile(r"/\.pixi/|/site-packages/|/\.cache/(ov|uv|pixi[^/]*|bazel[^/]*|pip)/|DerivedDataCache|/\.claude/|/agent-gate/")
_LINE = re.compile(r"\d+:\s+(->\s+)?FLOCK\s+\S+\s+(READ|WRITE)\s+(\d+)\s+([0-9a-f]+):([0-9a-f]+):(\d+)")


@dataclass
class Party:
    pid: int
    since: float
    command: str
    owner: owners.Owner
    scope: str = ""
    stats: resources.ScopeStats | None = None

    def as_dict(self) -> dict:
        return {**{k: v for k, v in self.__dict__.items() if k not in ("owner", "stats")},
                "owner": self.owner.as_dict(), "stats": self.stats.__dict__ if self.stats else None}


@dataclass
class Lock:
    path: str
    holders: list[Party] = field(default_factory=list)
    waiters: list[Party] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"path": self.path, "holders": [h.as_dict() for h in self.holders],
                "waiters": [w.as_dict() for w in self.waiters]}


def parse_proc_locks(text: str) -> dict[tuple[int, int], dict]:
    """(st_dev, st_ino) -> {"exclusive": bool, "blocked": [pid, ...]} for every FLOCK entry."""
    out: dict[tuple[int, int], dict] = {}
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if not m:
            continue
        blocked, mode, pid, major, minor, ino = m.groups()
        key = (os.makedev(int(major, 16), int(minor, 16)), int(ino))
        entry = out.setdefault(key, {"exclusive": False, "blocked": []})
        if blocked:
            entry["blocked"].append(int(pid))
        if mode == "WRITE":
            entry["exclusive"] = True
    return out


def _wanted(path: str, contended: bool) -> bool:
    if IGNORED.search(path) or path.startswith("/dev/shm/"):
        return False
    return contended or (path.endswith(".lock") and path.startswith(HOME))


def _job(pid: int, snap: Snapshot) -> tuple[str, float | None]:
    """The job under a lock holder: the first descendant in a cgroup scope of its own, and its start."""
    own = snap.procs[pid].cgroup
    for d in snap.descendants(pid):
        if d.cgroup != own and d.cgroup.endswith(".scope"):
            return d.cgroup, d.started_at(snap.btime)
    return "", None


def _party(shown: int, since: float, snap: Snapshot, sessions) -> Party:
    p = snap.procs[shown]
    scope, _ = _job(shown, snap)
    return Party(shown, since, p.cmdline[:400], owners.owner_of(shown, snap, sessions), scope,
                 resources.scope_stats(scope) if scope else None)


def read(snap: Snapshot, sessions: dict[int, owners.Session], proc_locks: str | None = None) -> list[Lock]:
    table = parse_proc_locks(proc_locks if proc_locks is not None else Path("/proc/locks").read_text())
    openers: dict[tuple[int, int], dict[int, str]] = {}
    for pid, p in snap.procs.items():
        for path, key in p.lock_fds.items():
            if key in table and table[key]["exclusive"]:
                openers.setdefault(key, {})[pid] = path
    locks = []
    for key, pids in openers.items():
        path = next(iter(pids.values()))
        blocked = {b for b in table[key]["blocked"] if b in snap.procs}
        if not _wanted(path, bool(blocked)):
            continue
        waiting = set(blocked)
        for b in blocked:  # the shell that ran `flock 9` waits with it
            waiting.update(a.pid for a in snap.ancestors(b) if a.pid in pids)
        holding = {pid for pid in pids if pid not in waiting}
        # Report each group once, at its outermost opener (children inherit the open file).
        roots = {pid for pid in holding if snap.procs[pid].ppid not in holding}
        lock = Lock(path)
        for pid in sorted(roots):
            # Held since its job started (flock forks the command once it has the lock), else since
            # the holder itself started.
            _, job_start = _job(pid, snap)
            lock.holders.append(_party(pid, job_start or snap.procs[pid].started_at(snap.btime), snap, sessions))
        for b in sorted(blocked, key=lambda b: snap.procs[b].start_ticks):
            # A blocked flock starts waiting as it starts; show the script around a bare `flock 9`.
            chain = [a for a in snap.ancestors(b) if a.pid in waiting]
            shown = chain[-1].pid if chain else b
            lock.waiters.append(_party(shown, snap.procs[b].started_at(snap.btime), snap, sessions))
        if lock.holders or lock.waiters:
            locks.append(lock)
    return sorted(locks, key=lambda lk: (-len(lk.waiters), lk.path))
