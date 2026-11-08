"""The gate's rules: whether a job may start in a slot and, if not, every reason why.

blockers() checks, in order: RAM, first come first served, programs running outside the gate,
gpu-solo's need for a quiet GPU, the drain for a solo job that has waited its patience, and VRAM.
The first reason is the one the gate logs; lockview shows them all. It is pure: the caller reads the machine and the queue, and decides which slots
are free.

The kinds are fixed, and config.toml sets their numbers. A lock's own kind (gpu, cpu) takes its
first `slots`. Its short kind may take those and the short-only slots after them, and is stopped
at its limit. gpu-solo takes every GPU slot at once.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from . import config


@dataclass(frozen=True)
class Kind:
    lock: str
    slots: int  # it may take slots 1 to this
    mem: str
    need_ram: int
    limit: int = 0  # seconds the gate lets a job of this kind run (gpu-short, cpu-short); 0 for no limit


@dataclass(frozen=True)
class Config:
    kinds: dict[str, Kind]
    solo_patience: int  # seconds
    vram_mb: int
    third_ram: int = 0  # GB available for a gpu-short job in a slot gpu jobs can't use
    programs: tuple[str, ...] = ()  # executable path patterns of the programs that must run through the gate

    def limit(self, kind: str | None) -> int:
        """Seconds the gate lets a job of this kind run: 0 for no limit, or for a kind it doesn't have."""
        k = self.kinds.get(kind)
        return k.limit if k else 0

    def order(self, kind: str) -> list[int]:
        """The slots a kind tries, in order. cpu-short tries its short-only slot first, so it takes no
        slot a cpu job could use; gpu-short tries its last, as a third GPU job slows the other two."""
        n = self.kinds[kind].slots
        return list(range(n, 0, -1)) if kind == "cpu-short" else list(range(1, n + 1))


def from_settings(s: config.Settings) -> Config:
    """The kinds config.toml's numbers give."""
    kinds = {}
    for lock, sec in (("gpu", s.gpu), ("cpu", s.cpu)):
        if sec is None:
            continue
        mem, every = f"{sec.memory_max_gb}G", sec.slots + sec.short_slots
        kinds[lock] = Kind(lock, sec.slots, mem, sec.ram_needed_gb)
        kinds[f"{lock}-short"] = Kind(lock, every, mem, sec.ram_needed_gb, round(sec.short_limit_min * 60))
        if lock == "gpu":
            kinds["gpu-solo"] = Kind(lock, every, mem, sec.ram_needed_gb)
    g = s.gpu
    if g is None:
        return Config(kinds, 0, 0)
    return Config(kinds, round(g.solo_patience_min * 60), g.vram_needed_mb, g.short_slot_ram_gb, g.programs)


@dataclass(frozen=True)
class Job:
    """A gate in the queue, waiting or running its job."""

    pid: int
    tick: int  # when its gate started, in clock ticks since boot: its place in the queue
    kind: str
    needs_idle: bool  # it waits for an idle GPU: gpu-solo
    queued_at: float
    running: bool = False
    vram_need_mb: int = 0  # the free VRAM it needs to start

    def older_than(self, other: Job) -> bool:
        return (self.tick, self.pid) < (other.tick, other.pid)  # ties go to the lower pid, so no two wait for each other


@dataclass(frozen=True)
class Program:
    """A running process of one of config.toml's programs."""

    pid: int
    gated: bool  # it runs under a gate
    who: str = ""  # whose it is, for the reason's text


@dataclass(frozen=True)
class Machine:
    ram_available_gb: int
    vram_free_mb: int | None  # None when nvidia-smi fails
    gpu_error: str = ""
    programs: tuple[Program, ...] = ()


@dataclass(frozen=True)
class Reason:
    why: str  # the code the gate logs: ram, fifo, program, solo, drain or vram
    text: str


def _n(count: int, what: str) -> str:
    return f"{count} {what}{'' if count == 1 else 's'}"


def blockers(me: Job, slot: int, queue: Sequence[Job], m: Machine, cfg: Config, now: float) -> list[Reason]:
    """Every check that keeps `me` from starting in its lock's free slot number `slot` (0 for
    gpu-solo, which takes them all), in the order the gate makes them."""
    k = cfg.kinds[me.kind]
    short_slot = k.lock == "gpu" and slot > cfg.kinds[k.lock].slots
    need = cfg.third_ram if short_slot else k.need_ram
    out = []
    if m.ram_available_gb < need:
        out.append(Reason("ram", f"RAM {m.ram_available_gb} GB available, needs {need} GB"))
    if k.lock != "gpu":
        return out

    others = [j for j in queue if j.pid != me.pid]

    def impatient(j: Job) -> bool:
        return now - j.queued_at >= cfg.solo_patience

    long_runs = any(j.running and j.kind == "gpu" for j in others)
    draining = sorted((j for j in others if not j.running and j.needs_idle and impatient(j)), key=lambda j: (j.tick, j.pid))

    def passes(j: Job) -> bool:
        """Whether `me` may start ahead of an older waiting GPU job. A solo job yields only to solo
        jobs, and every gpu job yields to them once they have waited their patience. A short job
        leaves them to the drain check, and passes gpu jobs for a short-only slot, which they can't
        take, and during a drain, when they can't start anyway."""
        if me.kind == "gpu-solo":
            return not j.needs_idle
        if me.kind == "gpu-short":
            return j.needs_idle or ((bool(draining) or short_slot) and j.kind == "gpu")
        return j.needs_idle and not impatient(j)

    ahead = [j for j in others if not j.running and j.kind.startswith("gpu") and j.older_than(me) and not passes(j)]
    if ahead:
        kinds = {j.kind for j in ahead}
        word = kinds.pop() if len(kinds) == 1 else k.lock
        out.append(Reason("fifo", f"behind {_n(len(ahead), f'older {word} job')} (first come, first served)"))
    ungated = [p for p in m.programs if not p.gated]
    if ungated:
        out.append(Reason("program", ", ".join(f"pid {p.pid}" + (f" ({p.who})" if p.who else "") for p in ungated)
                          + (" runs" if len(ungated) == 1 else " run") + " outside the gate"))
    if me.kind == "gpu-solo" and m.programs:
        out.append(Reason("solo", f"needs no [gpu] programs running at all ({len(m.programs)} running)"))
    if draining and me.kind != "gpu-solo" and not (me.kind == "gpu-short" and long_runs):
        out.append(Reason("drain", f"GPU draining for idle-GPU job pid {draining[0].pid} (waited past {cfg.solo_patience / 3600:g} h)"
                          + ("; a short job starts during a drain only while a gpu job runs" if me.kind == "gpu-short" else "")))
    if m.vram_free_mb is None:
        out.append(Reason("vram", f"nvidia-smi fails: {m.gpu_error}"))
    elif m.vram_free_mb < me.vram_need_mb:
        out.append(Reason("vram", f"VRAM {m.vram_free_mb / 1024:.1f} GB free, needs {me.vram_need_mb / 1024:.1f} GB"))
    return out


def why_waiting(me: Job, queue: Sequence[Job], held: Collection[str], m: Machine, cfg: Config, now: float) -> list[str]:
    """Why a waiting job hasn't started, for people: empty when it starts at the gate's next look.

    held: the slots running jobs hold, by name (gpu.1). First the slots it may take that are
    busy, then the checks that hold it back in its lock's own slots (also while they are busy:
    what would still stop it once one frees), then those only a short-only slot has.
    """
    k = cfg.kinds[me.kind]
    name = lambda i: f"{k.lock}.{i}"
    if me.kind == "gpu-solo":
        busy = [i for i in range(1, k.slots + 1) if name(i) in held]
        return ([f"needs every {k.lock} slot free ({len(busy)} of {k.slots} busy)"] if busy else []) + [
            r.text for r in blockers(me, 0, queue, m, cfg, now)]
    order = cfg.order(me.kind)
    per = {i: ["busy"] if name(i) in held else [r.text for r in blockers(me, i, queue, m, cfg, now)] for i in order}
    if any(not texts for texts in per.values()):
        return []
    main = [i for i in order if i <= cfg.kinds[k.lock].slots]
    extra = [i for i in order if i not in main]
    out = []
    busy = sum(name(i) in held for i in main)
    if busy == len(main):
        out.append(f"no free {k.lock} slot ({busy} of {len(main)} busy)")
    out += [f"{name(i)}: busy" for i in extra if name(i) in held]
    base = [r.text for r in blockers(me, main[0], queue, m, cfg, now)]
    out += base
    out += [f"{name(i)}: {t}" for i in extra if name(i) not in held for t in per[i] if t not in base]
    return out
