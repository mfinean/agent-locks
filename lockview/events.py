"""The gate's own record of its jobs: ~/.local/state/agent-gate/events-<YYYY-MM>.jsonl.

Each gate appends a queue, a wait each time its reason for waiting changes, a start and an end for
its one job (docs/how-it-works.md has the format). A job is named by its gate's pid and start
ticks, since pids are reused. `update` reads only what the files gained since the last call.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import owners

EVENTS_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "agent-gate"
SCHEMA = 1


@dataclass
class Job:
    pid: int
    tick: int
    kind: str
    cwd: str
    command: str  # argv joined with spaces, cut at 2,000 characters
    gate: str  # the first 8 hex digits of the sha1 of the gate copy that ran it
    queued: float
    waits: list[tuple[float, str, str]] = field(default_factory=list)  # (t, why, detail)
    start: float | None = None
    slots: list[str] = field(default_factory=list)
    unit: str = ""
    end: float | None = None
    code: int | None = None
    peak: int | None = None  # bytes, read inside the job's scope as its last act
    owner: dict | None = None  # who queued it, as agent-gate logs it: project, session, worktree

    def waited_by(self, until: float) -> dict[str, float]:
        """Seconds spent waiting, by the gate's reason, up to the job's start or `until`."""
        out: dict[str, float] = {}
        stop = self.start if self.start is not None else until
        for (t, why, _), (t2, _, _) in zip(self.waits, [*self.waits[1:], (stop, "", "")], strict=False):
            out[why] = out.get(why, 0.0) + max(0.0, min(t2, stop) - t)
        return out


class EventLog:
    def __init__(self, directory: Path = EVENTS_DIR, days: int = 14):
        self.directory = directory
        self.days = days
        self.offsets: dict[Path, int] = {}
        self.jobs: dict[tuple[int, int], Job] = {}
        self.error = ""

    def update(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        oldest = time.strftime("%Y-%m", time.localtime(now - self.days * 86400))
        try:
            paths = sorted(p for p in self.directory.glob("events-*.jsonl") if p.stem[len("events-"):] >= oldest)
            if any(p.stat().st_size < self.offsets.get(p, 0) for p in paths):
                self.offsets, self.jobs = {}, {}  # only appends are expected: a file rewritten in place is read again
            for path in paths:
                self._read(path)
        except OSError as e:
            self.error = f"{self.directory}: {e}"
            return
        self.error = ""
        cutoff = now - self.days * 86400
        self.jobs = {k: j for k, j in self.jobs.items() if (j.end or j.start or j.queued) >= cutoff}

    def _read(self, path: Path) -> None:
        with path.open("rb") as f:
            f.seek(self.offsets.get(path, 0))
            data = f.read()
        complete = data[: data.rfind(b"\n") + 1]  # a line still being written waits for the next read
        self.offsets[path] = self.offsets.get(path, 0) + len(complete)
        self.feed(complete.decode(errors="replace").splitlines())

    def feed(self, lines) -> None:
        for line in lines:
            try:
                e = json.loads(line)
                if e.get("v") != SCHEMA:
                    continue
                self._apply(e)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue  # a torn or foreign line says nothing about any job

    def _apply(self, e: dict) -> None:
        key = (int(e["pid"]), int(e["tick"]))
        if e["ev"] == "queue":
            self.jobs[key] = Job(key[0], key[1], e["kind"], e.get("cwd", ""), e.get("cmd", ""), e.get("gate", ""), float(e["t"]),
                                 owner=owners.checked(e.get("owner")))
            return
        job = self.jobs.get(key)
        if job is None:  # its queue event is older than what was read
            return
        if e["ev"] == "wait":
            job.waits.append((float(e["t"]), e["why"], e.get("detail", "")))
        elif e["ev"] == "start":
            job.start, job.slots, job.unit = float(e["t"]), list(e.get("slots", [])), e.get("unit", "")
        elif e["ev"] == "end":
            job.end, job.code, job.peak = float(e["t"]), e.get("code"), e.get("peak")
