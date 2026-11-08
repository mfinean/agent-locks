"""How well the gate's slots were used over a window: slot use, waiting, jobs by kind, and the
memory GPU jobs used together.

It counts the logged runs (runs.py) and the live gates. A gate run the log lacks has only a guessed
kind and slot, and the guess puts a third of the GPU's run time on the cpu slots; so a window starts
no earlier than the log, nor than the end of the last finished gate run the log lacks (those of gate
copies older than the log, or one whose start the gate failed to log).
"""

from __future__ import annotations

from collections.abc import Iterator

from . import gate
from .events import EventLog, Job
from .runs import Finished


def _segments(series: dict[str, list[tuple[float, float, float]]], t0: float, t1: float) -> Iterator[tuple[float, float, dict[str, float]]]:
    """Step through [t0, t1]: each piece's start and length, and every series' sum of the weights
    of its intervals (start, end, weight) that cover the piece."""
    edges = []
    for name, intervals in series.items():
        for a, b, w in intervals:
            a, b = max(a, t0), min(b, t1)
            if a < b:
                edges += [(a, name, w), (b, name, -w)]
    edges.sort(key=lambda e: e[0])
    now, t = dict.fromkeys(series, 0.0), t0
    for when, name, dw in edges:
        if when > t:
            yield t, when - t, dict(now)
            t = when
        now[name] += dw
    if t < t1:
        yield t, t1 - t, dict(now)


def _q(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, round(p * (len(xs) - 1)))]


def _spread(xs: list[float]) -> dict:
    return {"n": len(xs), "p50": _q(xs, 0.5), "p90": _q(xs, 0.9), "max": max(xs, default=None), "total": sum(xs)}


def report(finished: list[Finished], log: EventLog, gs: gate.GateState, now: float, hours: int) -> dict:
    first = min((j.queued for j in log.jobs.values()), default=now)
    t0 = max([now - hours * 3600, first, *(f.end for f in finished if f.gated and not f.logged)])
    limited_by = None if t0 == now - hours * 3600 else "log" if t0 == first else "unlogged run"
    finished = [f for f in finished if f.logged]
    cfg = gs.config
    extra = gate.short_only(gs.slots)
    live: list[tuple[gate.Gate, Job | None]] = [(g, log.jobs.get((g.pid, g.start_ticks))) for g in gs.gates]

    # Slot use: the slots each run held (a lock's main slots together, gpu.3 apart), and the time jobs waited.
    busy: dict[str, list] = {}
    waiting: dict[str, list] = {}

    def held(slot: str, a: float, b: float) -> None:
        busy.setdefault(slot if slot in extra else slot.split(".")[0], []).append((a, b, 1.0))

    for f in finished:
        for slot in f.slots:
            held(slot, f.start, f.end)
        if f.kind in cfg.kinds:
            waiting.setdefault(cfg.kinds[f.kind].lock, []).append((f.queued_at, f.start, 1.0))
    for g, _ in live:
        for slot in g.slots:
            held(slot, g.started_at, now)
        waiting.setdefault(cfg.kinds[g.kind].lock, []).append((g.queued_at, g.started_at or now, 1.0))

    locks = {}
    for lock in sorted({k.lock for k in cfg.kinds.values()}):
        n = len([s for s in gs.slots if s.lock == lock and s.name not in extra])
        own = sorted(s for s in extra if s.startswith(lock + "."))
        series = {"busy": busy.get(lock, []), "waiting": waiting.get(lock, []), **{s: busy.get(s, []) for s in own}}
        acc = dict.fromkeys([*series, "full", "idle"], 0.0)
        for _, d, v in _segments(series, t0, now):
            acc["busy"] += d * v["busy"]
            for s in own:
                acc[s] += d * v[s]
            if v["waiting"]:
                acc["waiting"] += d
                acc["full"] += d * (v["busy"] >= n)
                acc["idle"] += d * (n - v["busy"])
        locks[lock] = {
            "slots": n, "slot_s": n * (now - t0), "busy_s": acc["busy"], "waiting_s": acc["waiting"],
            "full_while_waiting_s": acc["full"], "idle_while_waiting_s": acc["idle"],
            "extra": [{"slot": s, "busy_s": acc[s]} for s in own],
        }

    # Jobs queued in the window, by kind: the log's, and the live gates'.
    kinds: dict[str, dict] = {}

    def entry(kind: str) -> dict:
        return kinds.setdefault(kind, {"waits": [], "runs": [], "peaks": [], "by_reason": {}, "waiting": 0,
                                       "failed": 0, "cut_short": 0, "limit_stops": 0})

    for f in finished:
        if f.queued_at < t0:
            continue
        k = entry(f.kind)
        k["waits"].append(f.start - f.queued_at)
        k["runs"].append(f.end - f.start)
        for why, s in f.waited.items():
            k["by_reason"][why] = k["by_reason"].get(why, 0.0) + s
        if f.peak is not None:
            k["peaks"].append(f.peak)
        k["cut_short"] += f.cut_short
        k["failed"] += bool(f.code) and not f.cut_short
        k["limit_stops"] += f.stopped_at(cfg.limit(f.kind))
    for g, job in live:
        if g.queued_at < t0:
            continue
        k = entry(g.kind)
        k["waits"].append((g.started_at or now) - g.queued_at)
        k["waiting"] += g.state == "waiting"
        for why, s in (job.waited_by(now).items() if job else ()):
            k["by_reason"][why] = k["by_reason"].get(why, 0.0) + s

    # GPU jobs running together, by each job's own peak (a running job's peak so far).
    gpu_kinds = {name for name, k in cfg.kinds.items() if k.lock == "gpu"}
    together = [(f.start, f.end, float(f.peak)) for f in finished if f.kind in gpu_kinds and f.peak is not None]
    together += [(g.started_at, now, float(g.stats.memory_peak)) for g, _ in live
                 if g.state == "running" and g.kind in gpu_kinds and g.stats and g.stats.memory_peak is not None]
    top = {"bytes": 0.0, "jobs": 0, "at": None}
    for t, _, v in _segments({"bytes": together, "jobs": [(a, b, 1.0) for a, b, _ in together]}, t0, now):
        if v["bytes"] > top["bytes"]:
            top = {"bytes": v["bytes"], "jobs": round(v["jobs"]), "at": t}
    return {
        "hours": hours, "from": t0, "to": now, "limited_by": limited_by,
        "locks": locks,
        "kinds": {name: {"jobs": len(k["waits"]), "waiting": k["waiting"], "wait": _spread(k["waits"]), "run": _spread(k["runs"]),
                         "by_reason": k["by_reason"], "peak": _spread(k["peaks"]), "failed": k["failed"],
                         "cut_short": k["cut_short"], "limit_stops": k["limit_stops"]}
                  for name, k in sorted(kinds.items())},
        "gpu_together": top,
    }
