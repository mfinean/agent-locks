"""Machine-wide readings the gate decides on (RAM, VRAM, the gated programs) plus per-scope cgroup stats."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

CGROUP_ROOT = Path("/sys/fs/cgroup")


def meminfo_gb(key: str) -> float:
    """One /proc/meminfo value, such as MemAvailable, in GiB."""
    with open("/proc/meminfo") as f:
        for line in f:
            if line.startswith(key + ":"):
                return int(line.split()[1]) / (1024 * 1024)
    raise RuntimeError(f"/proc/meminfo has no {key}")


def ram_available_gb() -> int:
    """MemAvailable in whole GiB, truncated, as `free -g`'s available column (what the gate reads)."""
    return int(meminfo_gb("MemAvailable"))


def ram_total_gb() -> float:
    return meminfo_gb("MemTotal")


def cpu_times(stat: Path = Path("/proc/stat")) -> tuple[int, int]:
    """(busy, total) clock ticks of all CPUs since boot. Idle and iowait aren't busy; guest time is
    already counted in user, so it isn't added again."""
    with open(stat) as f:
        user, nice, system, idle, iowait, irq, softirq, steal = (int(v) for v in f.readline().split()[1:9])
    busy = user + nice + system + irq + softirq + steal
    return busy, busy + idle + iowait


@dataclass
class Gpu:
    ok: bool
    error: str = ""
    total_mb: int = 0
    used_mb: int = 0
    util_pct: int = 0
    proc_mb: dict[int, int] = field(default_factory=dict)  # pid -> MiB of VRAM

    @property
    def free_mb(self) -> int:
        return self.total_mb - self.used_mb


def _smi(*query: str) -> list[list[str]]:
    out = subprocess.run(
        ["nvidia-smi", *query, "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10, check=True,
    ).stdout
    return [[c.strip() for c in line.split(",")] for line in out.splitlines() if line.strip()]


def gpu(processes: bool = True) -> Gpu:
    """One GPU, as on this workstation, with each process's VRAM unless `processes` is false. A
    driver/library mismatch makes nvidia-smi fail; say so."""
    try:
        total, used, util = (int(v) for v in _smi("--query-gpu=memory.total,memory.used,utilization.gpu")[0])
        procs = {int(pid): int(mb) for pid, mb in _smi("--query-compute-apps=pid,used_memory")} if processes else {}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as e:
        detail = getattr(e, "stderr", "") or str(e)
        return Gpu(ok=False, error=detail.strip().splitlines()[0] if detail.strip() else type(e).__name__)
    return Gpu(True, "", total, used, util, procs)


def pressure(path: Path) -> dict[str, float]:
    """A PSI file's avg10 for `some` and `full` (percent), and their totals (seconds)."""
    out: dict[str, float] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        kind, *pairs = line.split()
        values = dict(p.split("=") for p in pairs)
        out[f"{kind}_avg10"] = float(values["avg10"])
        out[f"{kind}_total_s"] = int(values["total"]) / 1e6
    return out


def session_cgroup(any_cgroup: str) -> Path | None:
    """The user@<uid>.service cgroup that systemd-oomd watches, from any cgroup path beneath it."""
    parts = any_cgroup.strip("/").split("/")
    for i, part in enumerate(parts):
        if part.startswith("user@") and part.endswith(".service"):
            return CGROUP_ROOT.joinpath(*parts[: i + 1])
    return None


@dataclass
class ScopeStats:
    unit: str
    memory_current: int | None = None
    memory_peak: int | None = None
    memory_max: int | None = None
    cpu_usec: int | None = None
    stall_some_total_s: float | None = None


def _int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return None if text == "max" else int(text)


def scope_stats(cgroup: str) -> ScopeStats:
    d = CGROUP_ROOT / cgroup.lstrip("/")
    cpu = None
    try:
        for line in (d / "cpu.stat").read_text().splitlines():
            if line.startswith("usage_usec "):
                cpu = int(line.split()[1])
    except OSError:
        pass
    psi = pressure(d / "memory.pressure")
    return ScopeStats(
        unit=d.name,
        memory_current=_int(d / "memory.current"),
        memory_peak=_int(d / "memory.peak"),
        memory_max=_int(d / "memory.max"),
        cpu_usec=cpu,
        stall_some_total_s=psi.get("some_total_s"),
    )


@dataclass(frozen=True)
class OomdPolicy:
    kills: bool  # ManagedOOMMemoryPressure=kill on the session's user@.service
    limit_pct: float
    duration_s: float


def _oomd_conf(key: str) -> str | None:
    """The last uncommented `key=` across oomd.conf and its drop-ins, in systemd's reading order."""
    files = [Path("/etc/systemd/oomd.conf")]
    for d in ("/usr/lib/systemd/oomd.conf.d", "/etc/systemd/oomd.conf.d"):
        files += sorted(Path(d).glob("*.conf"))
    value = None
    for f in files:
        try:
            for line in f.read_text().splitlines():
                if line.strip().startswith(key + "="):
                    value = line.split("=", 1)[1].strip()
        except OSError:
            continue
    return value


def oomd_policy(uid: int) -> OomdPolicy:
    """What systemd-oomd does to this user's session, from the unit and oomd's own config."""
    props = {}
    try:
        out = subprocess.run(
            ["systemctl", "show", f"user@{uid}.service", "-p", "ManagedOOMMemoryPressure", "-p", "ManagedOOMMemoryPressureLimit"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    except (OSError, subprocess.SubprocessError):
        pass
    # The unit stores its limit as a fraction of 2^32; 0 means oomd's default.
    raw = int(props.get("ManagedOOMMemoryPressureLimit", "0") or 0)
    default = (_oomd_conf("DefaultMemoryPressureLimit") or "60%").rstrip("%")
    limit = raw / 2**32 * 100 if raw else float(default)
    duration = (_oomd_conf("DefaultMemoryPressureDurationSec") or "30s").rstrip("s")
    return OomdPolicy(props.get("ManagedOOMMemoryPressure") == "kill", round(limit, 1), float(duration))
