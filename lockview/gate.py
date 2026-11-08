"""The gate's queue from outside: the gates, the slot each holds, and why each waiter still waits.

agent-gate's tickets list its gates, and policy.py's rules with config.toml's numbers say why each
waits. This only reads: it never opens a slot's lock file or removes a ticket, so it can't take a
slot or change the queue.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from pathlib import Path

from . import agent_gate, config, owners, policy, probe, procs, resources
from .locks import Lock
from .policy import Config
from .procs import Snapshot

GPU_IDLE_FLAG_S = 600  # a job holding a gpu slot with no work on the GPU this long is flagged
LOCK_WAIT_FLAG_S = 60  # and a job holding any slot while it waits this long for another lock


@dataclass
class Gate:
    pid: int
    kind: str
    state: str  # running | waiting
    needs_idle: bool
    queued_at: float
    start_ticks: int
    command: list[str]
    cwd: str
    owner: owners.Owner
    old_copy: bool
    vram_need_mb: int
    slots: list[str] = field(default_factory=list)
    started_at: float | None = None
    scope: str = ""
    stats: resources.ScopeStats | None = None
    vram_mb: int = 0
    reasons: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)  # a running job's ways of holding a slot without using it
    position: int | None = None  # 1-based place among waiters for the same lock, oldest first

    def as_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k not in ("owner", "stats")}
        d["owner"] = self.owner.as_dict()
        d["stats"] = self.stats.__dict__ if self.stats else None
        return d


@dataclass
class Slot:
    name: str  # gpu.1
    lock: str
    kinds: list[str]  # the kinds that may take it: a kind with n slots takes <lock>.1 to <lock>.n
    holder: int | None = None  # pid of the running gate


@dataclass
class ProgramProc:
    pid: int
    gated: bool
    owner: owners.Owner
    command: str


@dataclass
class GateState:
    installed: bool
    error: str
    config: Config | None
    gates: list[Gate]
    slots: list[Slot]
    programs: list[ProgramProc]  # the processes of config.toml's [gpu] programs
    ram_available_gb: int
    gpu: resources.Gpu

    def as_dict(self) -> dict:
        cfg = self.config
        return {
            "installed": self.installed,
            "error": self.error,
            "config": None if cfg is None else {
                "kinds": {k: v.__dict__ for k, v in cfg.kinds.items()},
                "solo_patience": cfg.solo_patience,
                "vram_mb": cfg.vram_mb,
                "third_ram": cfg.third_ram,
                "programs": list(cfg.programs),
            },
            "gates": [g.as_dict() for g in self.gates],
            "slots": [s.__dict__ for s in self.slots],
            "programs": [{**p.__dict__, "owner": p.owner.as_dict()} for p in self.programs],
        }


def _gates(snap: Snapshot, cfg: Config, sessions: dict[int, owners.Session], tickets: list[agent_gate.Ticket],
           installed: str) -> list[Gate]:
    """The gates, from their tickets. installed: the version of the installed gate's code."""
    scopes = {c.rsplit("/", 1)[-1]: c for c in {p.cgroup for p in snap.procs.values()}}  # unit -> its cgroup
    gates = []
    for t in tickets:
        if t.kind not in cfg.kinds:
            continue  # a kind config.toml has since lost
        recorded = owners.checked(t.owner)
        scope = scopes.get(t.unit, "") if t.unit else ""
        gates.append(Gate(
            pid=t.pid,
            kind=t.kind,
            state=t.state,
            needs_idle=t.kind == "gpu-solo",
            queued_at=t.queued,
            start_ticks=t.tick,
            command=t.command,
            cwd=t.cwd,
            owner=owners.Owner(**recorded) if recorded else owners.owner_of(t.pid, snap, sessions),
            old_copy=t.version != installed,
            vram_need_mb=t.vram_mb or cfg.vram_mb,
            slots=list(t.slots),
            started_at=t.started,
            scope=scope,
            stats=resources.scope_stats(scope) if scope else None,
        ))
    return gates


def find_gates(snap: Snapshot, cfg: Config, sessions: dict[int, owners.Session]) -> list[Gate]:
    gates = _gates(snap, cfg, sessions, agent_gate.read_queue(remove_stale=False), agent_gate.version())
    return sorted(gates, key=lambda g: (g.state != "running", g.start_ticks, g.pid))


def find_programs(snap: Snapshot, sessions: dict[int, owners.Session], patterns: tuple[str, ...]) -> list[ProgramProc]:
    """The processes of the programs that must run through the gate (config.toml's [gpu] programs)."""
    out = []
    if not patterns:
        return out
    for pid, p in snap.procs.items():
        exe = procs.exe(pid)
        if not any(fnmatch.fnmatch(exe, pattern) for pattern in patterns):
            continue
        gated = bool(agent_gate.IN_SCOPE.search(p.cgroup))  # as the gate counts it: it runs in a gate's scope
        out.append(ProgramProc(pid, gated, owners.owner_of(pid, snap, sessions), p.cmdline[:300]))
    return out


def _probe_view(snap: Snapshot) -> dict[int, dict]:
    """The snapshot's processes outside every gate job's scope, in probe.py's form. Kernel threads have no argv."""
    return {p.pid: {"ppid": p.ppid, "sid": p.sid, "comm": p.comm, "argv": list(p.argv), "uid": None, "ticks": p.ticks,
                    "start": p.start_ticks, "rss": p.rss}
            for p in snap.procs.values() if p.argv and not agent_gate.IN_SCOPE.search(p.cgroup)}


def outside(snap: Snapshot, before: Snapshot | None, vram_mb: dict[int, int], sessions: dict[int, owners.Session]) -> list[dict]:
    """The heavy jobs Claude sessions run outside the gate.

    The test is the one lockview applies to other machines (probe.py): a process tree that something started,
    using a GPU, half a core or 2 GB. Gate jobs' scopes are left out, and so is any job no Claude session
    started, such as the desktop's own apps. before: the previous sample's snapshot, for CPU use; without
    it, only memory and VRAM count.
    """
    view = _probe_view(snap)
    cpu = probe.cores_used(_probe_view(before), view, snap.taken - before.taken) if before else {}
    out = []
    for j in probe.jobs(view, cpu, vram_mb, {}, lambda _uid: ""):
        if probe.is_boundary(j["pid"], view[j["pid"]]):
            continue  # a session, a shell or a terminal itself, rather than a job one started
        owner = owners.owner_of(j["pid"], snap, sessions)
        if owner.session:
            out.append({**j, "start": snap.btime + j["start"] / procs.CLK_TCK, "owner": owner.as_dict(),
                        "via": f"Claude session {owner.session}"})
    return out


def slot_table(cfg: Config, gates: list[Gate]) -> list[Slot]:
    slots = []
    for lock in sorted({k.lock for k in cfg.kinds.values()}):
        for i in range(1, max(k.slots for k in cfg.kinds.values() if k.lock == lock) + 1):
            name = f"{lock}.{i}"
            kinds = sorted(n for n, k in cfg.kinds.items() if k.lock == lock and k.slots >= i)
            holder = next((g.pid for g in gates if g.state == "running" and name in g.slots), None)
            slots.append(Slot(name, lock, kinds, holder))
    return slots


def short_only(slots: list[Slot]) -> set[str]:
    """The slots a lock's own kind can't take, gpu.3 and cpu.4: only its short kind may (and a gpu-solo
    job, which takes every slot)."""
    return {s.name for s in slots if s.lock not in s.kinds}


def explain_waits(state: GateState, now: float) -> None:
    """Fill each waiter's place in its lock's queue, and its `reasons`: every check of the gate's
    rules (policy.py) that fails right now."""
    cfg = state.config
    queue = [policy.Job(g.pid, g.start_ticks, g.kind, g.needs_idle, g.queued_at, g.state == "running", g.vram_need_mb)
             for g in state.gates]
    gpu = state.gpu
    machine = policy.Machine(state.ram_available_gb, gpu.free_mb if gpu.ok else None, gpu.error, tuple(
        policy.Program(p.pid, p.gated, p.owner.session or p.owner.project or "?") for p in state.programs))
    held = {s.name for s in state.slots if s.holder is not None}
    for lock in {k.lock for k in cfg.kinds.values()}:
        same = sorted((g for g in state.gates if g.state == "waiting" and cfg.kinds[g.kind].lock == lock),
                      key=lambda g: (g.start_ticks, g.pid))
        for i, g in enumerate(same, 1):
            g.position = i
    for g, job in zip(state.gates, queue, strict=True):
        if g.state == "waiting":
            g.reasons = policy.why_waiting(job, queue, held, machine, cfg, now)


def _in_scope(cgroup: str, scope: str) -> bool:
    return cgroup == scope or cgroup.startswith(scope + "/")


def flag_running(state: GateState, lks: list[Lock], snap: Snapshot, gpu_idle_since: dict[int, float], now: float) -> None:
    """Fill each running job's `flags` with the ways it holds a slot without using it.

    Work on the GPU is a CUDA process or a gated program in the job's scope: a program that only
    renders has no CUDA context, so nvidia-smi's compute list alone would miss it. gpu_idle_since
    (gate pid -> the first sample with neither) carries over from sample to sample.
    """
    running = {g.pid: g for g in state.gates if g.state == "running"}
    for pid in [p for p in gpu_idle_since if p not in running]:
        del gpu_idle_since[pid]
    if state.gpu.ok:  # without nvidia-smi, a job's CUDA use is unknown
        program_cgroups = [q.cgroup for p in state.programs if (q := snap.get(p.pid))]
        for g in running.values():
            if state.config.kinds[g.kind].lock != "gpu":
                continue
            if g.vram_mb or any(_in_scope(c, g.scope) for c in program_cgroups):
                gpu_idle_since.pop(g.pid, None)
            elif now - gpu_idle_since.setdefault(g.pid, now) >= GPU_IDLE_FLAG_S:
                g.flags.append(f"no work on the GPU for {(now - gpu_idle_since[g.pid]) // 60:.0f} min while holding {'+'.join(g.slots)}")
    for lk in lks:
        for w in lk.waiters:
            holder = next((running[a.pid] for a in snap.ancestors(w.pid) if a.pid in running), None)
            if holder and now - w.since >= LOCK_WAIT_FLAG_S:
                where = lk.path.replace(str(Path.home()), "~")
                holder.flags.append(f"holding {'+'.join(holder.slots)} while waiting {(now - w.since) // 60:.0f} min for {where}")


def read(snap: Snapshot, sessions: dict[int, owners.Session], now: float, settings: Path = config.PATH) -> GateState:
    gpu = resources.gpu()
    ram = resources.ram_available_gb()
    try:
        cfg = policy.from_settings(config.load(settings))
    except config.ConfigError as e:
        return GateState(False, str(e), None, [], [], [], ram, gpu)
    gates = find_gates(snap, cfg, sessions)
    for g in gates:
        if g.scope:
            g.vram_mb = sum(mb for pid, mb in gpu.proc_mb.items() if (q := snap.get(pid)) and _in_scope(q.cgroup, g.scope))
    state = GateState(True, "", cfg, gates, slot_table(cfg, gates), find_programs(snap, sessions, cfg.programs), ram, gpu)
    explain_waits(state, now)
    return state
