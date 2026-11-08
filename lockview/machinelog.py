"""lockview's record of the machine: ~/.local/state/lockview/machine-<YYYY-MM>.jsonl.

The sampler reads free RAM, memory pressure, the GPU, CPU time and load every few seconds for the
page. Nothing else on this machine keeps them (sysstat is off), so once 20 s have passed since its
last line, this appends one that sums up the readings since then: how low RAM got, how high pressure
and VRAM went, and how busy the GPU and CPUs were (the README has the format). The gate's queue is in
its own event log.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from .resources import Gpu

SCHEMA = 1


@dataclass(frozen=True)
class Reading:
    ram_gb: float  # MemAvailable, GiB
    pressure: dict[str, float]  # resources.pressure() of the user@.service cgroup oomd watches; {} if unreadable
    gpu: Gpu
    load1: float
    cpu: tuple[int, int]  # resources.cpu_times()


def summary(since: float, now: float, cpu0: tuple[int, int], readings: list[Reading]) -> dict:
    """One line for the readings taken after `since` up to `now`; `cpu0` is the CPU time at `since`."""
    gpus = [r.gpu for r in readings if r.gpu.ok]
    psi = [r.pressure for r in readings if r.pressure]
    busy, total = (b - a for a, b in zip(cpu0, readings[-1].cpu, strict=True))
    return {
        "v": SCHEMA,
        "t": round(now),
        "s": round(now - since),
        "n": len(readings),
        "ram_min_gb": round(min(r.ram_gb for r in readings), 1),
        "psi_some_max": max((p["some_avg10"] for p in psi), default=None),
        "psi_full_max": max((p["full_avg10"] for p in psi), default=None),
        "gpu_util_mean": round(sum(g.util_pct for g in gpus) / len(gpus)) if gpus else None,
        "vram_max_mb": max((g.used_mb for g in gpus), default=None),
        "cpu_pct": round(100 * busy / total, 1) if total > 0 else None,
        "load1": round(readings[-1].load1, 2),
    }


class MachineLog:
    def __init__(self, directory: Path, every: float = 20.0):
        self.directory = directory
        self.every = every
        self.error = ""
        self._since: float | None = None
        self._cpu0 = (0, 0)
        self._readings: list[Reading] = []

    def add(self, now: float, r: Reading) -> None:
        """Take one reading; once `every` seconds have passed since the last line, write the next."""
        if self._since is None:
            self._since, self._cpu0 = now, r.cpu
        self._readings.append(r)
        if now - self._since < self.every:
            return
        line = summary(self._since, now, self._cpu0, self._readings)
        self._since, self._cpu0, self._readings = now, r.cpu, []
        path = self.directory / time.strftime("machine-%Y-%m.jsonl", time.localtime(now))
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with open(path, "a") as f:
                f.write(json.dumps(line, separators=(",", ":")) + "\n")
        except OSError as e:
            self.error = f"{path}: {e.strerror or e}"  # this line is lost; the next write tries again
        else:
            self.error = ""
