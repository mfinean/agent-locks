"""Finished runs: the gate's event log joined with the journal.

A gate job's kind, slots, queue time, exit code and peak come from the gate's log. The journal adds
its CPU time and whether systemd OOM-killed or SIGKILLed it, and it has the runs the log lacks:
those of gate copies older than the log, and every `flock … systemd-run` scope under a lock. A gate
killed while its job ran logs no end, so the journal's end stands in for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from . import estimate
from .events import EventLog, Job
from .journal import GATE_UNIT, History, Run

MATCH_S = 10.0  # a log start and a journal start this close are one run: pids are reused, but not this fast
_keys = lru_cache(maxsize=16384)(estimate.keys)  # a merge runs every refresh, over two weeks of runs


@dataclass
class Finished:
    unit: str
    command: str
    keys: estimate.Keys
    start: float
    end: float
    # The gate kind; for a gate scope the log lacks, a guess (gpu or cpu); None for any other scope.
    kind: str | None
    logged: bool  # from the gate's log: kind, slots, queue time, code and peak are facts
    slots: list[str] = field(default_factory=list)
    queued_at: float | None = None
    waited: dict[str, float] = field(default_factory=dict)  # seconds, by the gate's reason
    code: int | None = None
    peak: int | None = None
    cwd: str = ""
    cpu_s: float | None = None
    outcome: str = ""  # oom-kill | killed, from the journal
    owner: dict | None = None  # who queued it, from the gate's log

    @property
    def gated(self) -> bool:
        return GATE_UNIT.fullmatch(self.unit) is not None

    @property
    def cut_short(self) -> bool:
        """Ended by a signal (an OOM kill, SIGKILL, the gate's limit, a crash): the job takes at least this long."""
        return bool(self.outcome) or (self.code is not None and self.code > 128)

    def stopped_at(self, limit: int) -> bool:
        """Stopped by the gate at its kind's time limit (0 for none), by its own test: TERM, or KILL after it, at the limit."""
        return limit > 0 and self.code in (137, 143) and self.end - self.start >= limit


def _logged(job: Job, run: Run | None) -> Finished | None:
    end = job.end if job.end is not None else run.end if run else None
    if job.start is None or end is None:
        return None
    return Finished(
        job.unit, job.command, _keys(job.command), job.start, end, job.kind, True, list(job.slots), job.queued,
        job.waited_by(job.start), job.code, job.peak, job.cwd,
        run.cpu_s if run else None, run.outcome if run else "", job.owner,
    )


def _unlogged(run: Run) -> Finished:
    k = _keys(run.command, from_journal=True)
    kind = ("gpu" if k.gpu_hint else "cpu") if run.gated else None
    return Finished(run.unit, run.command, k, run.start, run.end, kind, False, cpu_s=run.cpu_s, outcome=run.outcome)


def merge(history: History, log: EventLog) -> list[Finished]:
    """Every finished run, oldest start first."""
    by_unit: dict[str, list[Run]] = {}
    for r in [*history.done, *history.open.values()]:
        by_unit.setdefault(r.unit, []).append(r)
    matched: set[int] = set()
    out = []
    for job in log.jobs.values():
        run = next((r for r in by_unit.get(job.unit, ()) if job.start is not None and abs(r.start - job.start) <= MATCH_S), None)
        if run:
            matched.add(id(run))
        if f := _logged(job, run):
            out.append(f)
    out += [_unlogged(r) for r in history.done if id(r) not in matched]
    return sorted(out, key=lambda f: f.start)
