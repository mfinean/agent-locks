"""Finished jobs from the user journal: every systemd-run scope's start, end and CPU time.

agent-gate runs each job as `agent-gate-<gate pid>.scope`; `flock … systemd-run` jobs run as
`run-<id>.scope`. systemd logs "Started <unit> - <command>." and, at the end, "<unit>: Consumed
<cpu> CPU time, <mem> memory peak". Matching on SYSLOG_IDENTIFIER=systemd uses the journal's index:
two weeks read in well under a second.

The logged memory peak is not read: it is the value systemd cached the last time something read the
unit's properties over D-Bus, and none at all if nothing did. The gate's watcher reads them as the
job starts, so a gate job's logged "peak" is its start-up footprint (a 16 GB training run logs 0.05G).
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

GATE_UNIT = re.compile(r"agent-gate-(\d+)\.scope")  # a gate job's scope, named for its gate's pid
_UNIT = r"((?:agent-gate-\d+|run-[\w-]+)\.scope)"
_STARTED = re.compile(rf"Started {_UNIT} - (.*)\.$", re.S)
_CONSUMED = re.compile(rf"{_UNIT}: Consumed (.*?) CPU time")
_OOM = re.compile(rf"{_UNIT}: Failed with result 'oom-kill'")
_KILL = re.compile(rf"{_UNIT}: Sending signal SIGKILL to process")
_UNITS = {"d": 86400, "h": 3600, "min": 60, "s": 1, "ms": 1e-3, "us": 1e-6}


def parse_timespan(text: str) -> float:
    """systemd's "1h 2min 3.456s" -> seconds."""
    total = 0.0
    for value, unit in re.findall(r"([\d.]+)\s*(d|h|min|ms|us|s)\b", text):
        total += float(value) * _UNITS[unit]
    return total


@dataclass
class Run:
    unit: str
    command: str
    start: float
    end: float | None = None
    cpu_s: float | None = None
    outcome: str = ""  # "" (ended), oom-kill, killed (SIGKILL on request: the gate's thrash guard, or a person)
    manager: str = ""  # pid of the systemd user manager that started it

    @property
    def gated(self) -> bool:
        return GATE_UNIT.fullmatch(self.unit) is not None

    @property
    def gate_pid(self) -> int | None:
        m = GATE_UNIT.fullmatch(self.unit)
        return int(m[1]) if m else None

    @property
    def duration(self) -> float | None:
        return None if self.end is None else self.end - self.start


class History:
    """Runs keyed by unit; `update` reads only what the journal gained since the last call."""

    def __init__(self, days: int = 14):
        self.days = days
        self.cursor: str | None = None
        self.open: dict[str, Run] = {}  # started, no end logged yet
        self.manager = ""  # the newest systemd user manager seen
        self.done: list[Run] = []
        self.error = ""

    def feed(self, entries) -> None:
        """entries: (time, systemd manager pid, message)."""
        for t, manager, msg in entries:
            self.manager = manager or self.manager
            if m := _STARTED.match(msg):
                self.open[m[1]] = Run(m[1], m[2], t, manager=manager)
            elif m := _CONSUMED.match(msg):
                run = self.open.pop(m[1], None)
                if run:
                    run.end, run.cpu_s = t, parse_timespan(m[2])
                    self.done.append(run)
            elif m := _OOM.match(msg):
                if run := self.open.get(m[1]):
                    run.outcome = "oom-kill"
            elif m := _KILL.match(msg):
                if (run := self.open.get(m[1])) and not run.outcome:
                    run.outcome = "killed"

    def update(self) -> None:
        cmd = ["journalctl", "--user", "--no-pager", "-o", "json", "--output-fields=MESSAGE,_PID", "SYSLOG_IDENTIFIER=systemd"]
        cmd += [f"--after-cursor={self.cursor}"] if self.cursor else [f"--since=-{self.days}d"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=True).stdout
        except (OSError, subprocess.SubprocessError) as e:
            self.error = f"journalctl: {e}"
            return
        self.error = ""
        entries = []
        for line in out.splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            self.cursor = r.get("__CURSOR", self.cursor)
            msg = r.get("MESSAGE")
            if isinstance(msg, str) and ".scope" in msg:
                entries.append((int(r["__REALTIME_TIMESTAMP"]) / 1e6, r.get("_PID", ""), msg))
        self.feed(entries)

    def lost(self) -> list[Run]:
        """Scopes started under an earlier user manager that never logged an end: the session died
        under them (a logout, or systemd-oomd killing user@.service). A short scope under the current
        manager can end without a "Consumed" line; it is not lost, only unrecorded."""
        return [r for r in self.open.values() if r.manager and r.manager != self.manager]
