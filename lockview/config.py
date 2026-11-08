"""This machine's settings: $XDG_CONFIG_HOME/agent-locks/config.toml (~/.config by default).

The gate's five kinds are fixed, and the file sets their numbers. share/config.example.toml explains
every key. A machine without a GPU leaves out [gpu], and with it the GPU kinds. A key the file must
not have, or a missing one, is an error that names it, so a typo can't silently keep a default.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import MISSING, dataclass, fields
from pathlib import Path

PATH = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "agent-locks" / "config.toml"
EXAMPLE = Path(__file__).resolve().parent.parent / "share" / "config.example.toml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Cpu:
    slots: int
    short_slots: int  # extra slots only the short kind may take
    memory_max_gb: int
    ram_needed_gb: int
    short_limit_min: float


@dataclass(frozen=True)
class Gpu(Cpu):
    short_slot_ram_gb: int
    vram_needed_mb: int
    solo_patience_min: float
    programs: tuple[str, ...] = ()  # executable path patterns of the programs that must run through the gate


@dataclass(frozen=True)
class Remote:
    hosts: tuple[str, ...] = ()  # ssh aliases


@dataclass(frozen=True)
class Settings:
    cpu: Cpu
    gpu: Gpu | None = None
    remote: Remote = Remote()


def tilde(path: Path) -> str:
    return "~/" + str(path.relative_to(Path.home())) if path.is_relative_to(Path.home()) else str(path)


def load(path: Path = PATH) -> Settings:
    try:
        data = tomllib.loads(path.read_text())
    except FileNotFoundError:
        raise ConfigError(f"{tilde(path)} is missing: copy {tilde(EXAMPLE)} there and set this machine's numbers") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{tilde(path)}: {e}") from None
    try:
        unknown = sorted(set(data) - {"cpu", "gpu", "remote"})
        if unknown:
            raise ConfigError(f"it has no section [{unknown[0]}]")
        if "cpu" not in data:
            raise ConfigError("it needs a [cpu] section")
        return Settings(_section(data, "cpu", Cpu), _section(data, "gpu", Gpu) if "gpu" in data else None,
                        _section(data, "remote", Remote) if "remote" in data else Remote())
    except ConfigError as e:
        raise ConfigError(f"{tilde(path)}: {e}") from None


def _section(data: dict, name: str, cls: type):
    table = data[name]
    if not isinstance(table, dict):
        raise ConfigError(f"{name} must be a section, [{name}]")
    wanted = {f.name: f for f in fields(cls)}
    unknown = sorted(set(table) - set(wanted))
    if unknown:
        raise ConfigError(f"[{name}] has no setting {unknown[0]}")
    values = {}
    for key, f in wanted.items():
        if key not in table:
            if f.default is MISSING:
                raise ConfigError(f"[{name}] needs {key}")
            continue
        v = table[key]
        if f.type == "int":
            if type(v) is not int or v < 0:
                raise ConfigError(f"[{name}] {key} must be a whole number, 0 or more, not {v!r}")
        elif f.type == "float":
            if type(v) not in (int, float) or v < 0:
                raise ConfigError(f"[{name}] {key} must be a number, 0 or more, not {v!r}")
        elif not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
            raise ConfigError(f"[{name}] {key} must be a list of strings, not {v!r}")
        else:
            v = tuple(dict.fromkeys(v))
        values[key] = v
    if values.get("slots", 1) < 1:
        raise ConfigError(f"[{name}] slots must be 1 or more")
    return cls(**values)
