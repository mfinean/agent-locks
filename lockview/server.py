"""The sampler (every few seconds: gate, locks, journal) and a localhost HTTP server.

GET /            the page (web/index.html)
GET /api/state   the latest sample as JSON

History comes from the gate's event log, joined with the journal (runs.py). Neither knows which
session started a job, nor which lock a `flock … systemd-run` scope ran under; the sampler sees
those live and keeps them in ~/.local/state/lockview/observed.json. Beside it, machinelog.py keeps a
history of the machine's RAM, memory pressure, GPU and CPU use.
remote.py watches the other machines listed in config.toml (config.py) over ssh.
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import capacity, config, estimate, events, gate, journal, locks, machinelog, owners, procs, remote, resources, runs

WEB = Path(__file__).resolve().parent / "web"
STATE_DIR = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "lockview"
WINDOW = 24 * 3600  # history sent to the page
KEEP = 14 * 86400  # observations kept
CAPACITY_HOURS = (24, 72, 168)  # the windows the capacity view offers


class Observed:
    """unit -> what was seen live: its owner, and the lock a lock-held run ran under."""

    def __init__(self, path: Path):
        self.path = path
        try:
            self.units: dict[str, dict] = json.loads(path.read_text())
        except (OSError, ValueError):
            self.units = {}
        self.dirty = False

    def note(self, unit: str, **fields) -> None:
        old = self.units.get(unit, {})
        new = {**old, **fields}
        if new != old:
            self.units[unit] = new
            self.dirty = True

    def save(self, now: float) -> None:
        if not self.dirty:
            return
        self.units = {u: d for u, d in self.units.items() if now - d.get("seen", now) < KEEP}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.units))
        tmp.replace(self.path)
        self.dirty = False


def _unit(scope: str) -> str:
    return scope.rsplit("/", 1)[-1]


def estimator(finished: list[runs.Finished], observed: dict[str, dict]) -> estimate.Estimator:
    """Estimates from every run that ended by itself: one cut short says only that it takes at least that long."""
    return estimate.Estimator(
        (f.keys, f.end - f.start, f.kind or ("lock" if "lock" in observed.get(f.unit, {}) else None))
        for f in finished if not f.cut_short
    )


def history_rows(finished: list[runs.Finished], lost: list[journal.Run], observed: dict[str, dict],
                 gs: gate.GateState, now: float) -> list[dict]:
    """Finished gated and lock-held runs in the window, each in a lane.

    A logged run sits in the slots the gate's log names. A gate run the log lacks has only a
    guessed kind, so it goes in the first slot free for its whole run, among its guessed kind's
    slots and then the other kind's: the gate never ran more jobs than it has slots, so a guess
    that would overfill a lane is wrong. That shows occupancy, not which numbered slot a run
    really had; a run that fits nowhere stays out of the timeline.
    """
    busy: dict[str, list[tuple[float, float]]] = {s.name: [] for s in gs.slots}
    short_only = gate.short_only(gs.slots)  # a run with a guessed kind is never a short one
    for g in gs.gates:
        for slot in g.slots:
            if slot in busy and g.started_at:
                busy[slot].append((g.started_at, float("inf")))
    recent = [f for f in finished if f.end >= now - WINDOW]
    for f in recent:  # logged placements first: they are facts
        for x in f.slots:
            if x in busy:
                busy[x].append((f.start, f.end))

    def fits(slot: str, start: float, end: float) -> bool:
        return all(end <= a + 1 or start >= b - 1 for a, b in busy[slot])

    rows = []
    for f in recent:
        seen = observed.get(f.unit, {})
        kind = f.kind
        if kind is None:
            if "lock" not in seen:
                continue
            kind, lanes = "lock", ["lock:" + seen["lock"].replace(str(Path.home()), "~")]
        elif f.logged:
            lanes = [x for x in f.slots if x in busy]
        else:
            order = [x for x in busy if x.startswith(kind + ".")] + [x for x in busy if not x.startswith(kind + ".")]
            lane = next((x for x in order if x not in short_only and fits(x, f.start, f.end)), None)
            if lane:
                busy[lane].append((f.start, f.end))
                kind = lane.split(".")[0]
            lanes = [lane] if lane else []
        rows.append({
            "unit": f.unit, "lane": lanes[0] if lanes else None, "lanes": lanes,
            "placed": f.logged or kind == "lock", "logged": f.logged, "kind": kind,
            "owner": f.owner or seen.get("owner") or owners.of(f.cwd, {}).as_dict(),
            "command": f.command[:400], "label": f.keys.task, "cwd": f.cwd,
            "start": f.start, "end": f.end, "cpu_s": f.cpu_s, "outcome": f.outcome, "cut_short": f.cut_short,
            "queued_at": f.queued_at, "waited": f.waited, "code": f.code, "peak": f.peak,
            "mem": gs.config.kinds[kind].mem if gs.config and kind in gs.config.kinds else None,
            "limit_stop": bool(gs.config) and f.stopped_at(gs.config.limit(f.kind)),
        })
    for r in lost:
        if r.gated and r.start > now - WINDOW:
            rows.append({"unit": r.unit, "lane": None, "lanes": [], "kind": None, "owner": owners.Owner("").as_dict(),
                         "command": r.command[:400], "label": estimate.keys(r.command, from_journal=True).task,
                         "start": r.start, "end": None, "outcome": "lost"})
    return rows


def gate_jobs(gs: gate.GateState, est: estimate.Estimator | None, now: float) -> list[dict]:
    """The gate's jobs for the page: each with its estimate, and a waiter's predicted start by the gate's rules."""
    if not gs.installed:
        return []
    cfg = gs.config
    rows = []
    for g in gs.gates:
        d = g.as_dict()
        d["label"] = estimate.keys(g.command).task
        e = est.estimate(g.command, g.kind) if est else None
        d["estimate"] = e.as_dict() if e else None
        if g.state == "running" and e:
            left, over = e.remaining(now - g.started_at)
            if limit := cfg.limit(g.kind):  # the gate stops it there
                left = max(0.0, min(left, g.started_at + limit - now))
            d["remaining"], d["overrun"], d["predicted_end"] = left, over, now + left
        rows.append(d)
    by_pid = {d["pid"]: d for d in rows}
    short_only = frozenset(gate.short_only(gs.slots))
    held_by_short = frozenset(s for g in gs.gates if g.state == "running" and cfg.limit(g.kind) for s in g.slots)
    for lock in {k.lock for k in cfg.kinds.values()}:
        free_at = {}
        for s in gs.slots:
            if s.lock == lock:
                holder = by_pid.get(s.holder) if s.holder else None
                free_at[s.name] = holder.get("predicted_end", now) if holder else now
        waiters = []
        for d in rows:
            if d["state"] != "waiting" or cfg.kinds[d["kind"]].lock != lock:
                continue
            limit = cfg.limit(d["kind"])
            duration = d["estimate"]["median"] if d["estimate"] else 1800
            waiters.append({"pid": d["pid"], "needs_idle": d["needs_idle"], "queued_at": d["queued_at"], "short": bool(limit),
                            "own_first": d["kind"] == "cpu-short", "duration": min(duration, limit) if limit else duration})
        waiters.sort(key=lambda w: (by_pid[w["pid"]]["start_ticks"], w["pid"]))
        plans = estimate.simulate(free_at, waiters, now, cfg.solo_patience, short_only, held_by_short)
        for pid, plan in plans.items():
            by_pid[pid]["predicted"] = plan.__dict__
    return rows


def lock_rows(lks: list[locks.Lock], est: estimate.Estimator | None, now: float) -> list[dict]:
    """The other locks for the page: each holder with its estimate, and each waiter's predicted start."""
    out = []
    for lk in lks:
        d = lk.as_dict()
        free_at = {"1": now}
        for party in d["holders"] + d["waiters"]:
            party["label"] = estimate.keys(party["command"]).task
        for h in d["holders"]:
            e = est.estimate(h["command"], "lock") if est else None
            h["estimate"] = e.as_dict() if e else None
            if e:
                left, over = e.remaining(now - h["since"])
                h["remaining"], h["overrun"], h["predicted_end"] = left, over, now + left
                free_at["1"] = max(free_at["1"], now + left)
        waiters = []
        for w in d["waiters"]:
            e = est.estimate(w["command"], "lock") if est else None
            w["estimate"] = e.as_dict() if e else None
            waiters.append({"pid": w["pid"], "needs_idle": False, "queued_at": w["since"],
                            "duration": e.median if e else 1800})
        plans = estimate.simulate(free_at, waiters, now, 0)
        for w in d["waiters"]:
            if w["pid"] in plans:
                w["predicted"] = plans[w["pid"]].__dict__
        d["label"] = lk.path.replace(str(Path.home()), "~")
        out.append(d)
    return out


class Sampler:
    def __init__(self, interval: float = 3.0, journal_every: float = 20.0):
        self.interval = interval
        self.journal_every = journal_every
        self.history = journal.History()
        self.events = events.EventLog()
        self.finished: list[runs.Finished] = []
        self.observed = Observed(STATE_DIR / "observed.json")
        self.machine_log = machinelog.MachineLog(STATE_DIR)
        try:
            hosts = config.load().remote.hosts
        except config.ConfigError:
            hosts = ()  # the gate's card says what is wrong with the file
        self.remotes = remote.Remotes(hosts, interval)
        self.estimator: estimate.Estimator | None = None
        self.capacity: dict[int, dict] = {}
        self._journal_at = 0.0
        self.body = b"{}"
        self.lock = threading.Lock()
        self.oomd = resources.oomd_policy(os.getuid())
        self.gpu_idle_since: dict[int, float] = {}  # gate pid -> since when its job has had no work on the GPU
        self.last_snap: procs.Snapshot | None = None  # the previous sample's, for each process's CPU use since

    def _refresh_history(self, now: float) -> bool:
        if now - self._journal_at < self.journal_every:
            return False
        self._journal_at = now
        self.history.update()
        self.events.update(now)
        self.finished = runs.merge(self.history, self.events)
        self.estimator = estimator(self.finished, self.observed.units)
        return True

    def sample(self) -> dict:
        t0 = time.time()
        refreshed = self._refresh_history(t0)
        snap = procs.Snapshot.take()
        sessions = owners.load_sessions(snap)
        now = time.time()
        gs = gate.read(snap, sessions, now)
        lks = locks.read(snap, sessions)
        gate.flag_running(gs, lks, snap, self.gpu_idle_since, now)
        outside = gate.outside(snap, self.last_snap, gs.gpu.proc_mb, sessions)
        self.last_snap = snap
        if refreshed and gs.installed:
            self.capacity = {h: capacity.report(self.finished, self.events, gs, now, h) for h in CAPACITY_HOURS}
        est = self.estimator

        for g in gs.gates:
            if g.state == "running" and g.scope:
                self.observed.note(_unit(g.scope), owner=g.owner.as_dict(), seen=now)
        for lk in lks:
            for h in lk.holders:
                if h.scope:
                    self.observed.note(_unit(h.scope), lock=lk.path, owner=h.owner.as_dict(), seen=now)
        self.observed.save(now)

        jobs = gate_jobs(gs, est, now)
        session_cg = resources.session_cgroup(snap.procs[os.getpid()].cgroup) if os.getpid() in snap.procs else None
        psi = resources.pressure(session_cg / "memory.pressure") if session_cg else {}
        load = os.getloadavg()
        self.machine_log.add(now, machinelog.Reading(resources.meminfo_gb("MemAvailable"), psi, gs.gpu, load[0], resources.cpu_times()))
        return {
            "now": now,
            "sample_ms": round(1000 * (time.time() - t0)),
            "machine": {
                "ram_available_gb": gs.ram_available_gb,
                "ram_total_gb": round(resources.ram_total_gb(), 1),
                "gpu": {**gs.gpu.__dict__, "proc_mb": None, "free_mb": gs.gpu.free_mb},
                "pressure": psi,
                "oomd": self.oomd.__dict__,
                "load": load,
                "cpus": os.cpu_count(),
            },
            "sessions": [s.__dict__ for s in sessions.values()],
            "gate": {**gs.as_dict(), "gates": jobs},
            "outside": outside,
            "locks": lock_rows(lks, est, now),
            "history": history_rows(self.finished, self.history.lost(), self.observed.units, gs, now),
            "journal_error": self.history.error,
            "events_error": self.events.error,
            "machine_log_error": self.machine_log.error,
            "capacity": self.capacity,
            "remotes": self.remotes.state(snap, sessions, now),
        }

    def run(self) -> None:
        while True:
            try:
                body = json.dumps(self.sample(), default=str).encode()
            except Exception:  # keep the page alive and say what broke, rather than freeze on a stale sample
                body = json.dumps({"error": traceback.format_exc(), "now": time.time()}).encode()
            with self.lock:
                self.body = body
            time.sleep(self.interval)


def make_handler(sampler: Sampler):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, "text/html; charset=utf-8", (WEB / "index.html").read_bytes())
            elif self.path == "/api/state":
                with sampler.lock:
                    body = sampler.body
                self._send(200, "application/json", body)
            else:
                self._send(404, "text/plain", b"not found")

        def _send(self, code: int, ctype: str, body: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    return Handler


def start(port: int = 0, interval: float = 3.0) -> tuple[ThreadingHTTPServer, Sampler]:
    sampler = Sampler(interval)
    sampler.remotes.start()
    with sampler.lock:
        sampler.body = json.dumps(sampler.sample(), default=str).encode()
    threading.Thread(target=sampler.run, daemon=True, name="sampler").start()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(sampler))  # port 0: any free one
    threading.Thread(target=httpd.serve_forever, daemon=True, name="http").start()
    return httpd, sampler
