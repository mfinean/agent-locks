"""A harness that runs agent-gate against stubs for nvidia-smi and systemd-run.

The copy under test has its own config, slot, queue and log directories and never reaches systemd,
the GPU or the real queue.
"""

import fcntl
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

# Runs the job in place: records the -p properties, and stops the job at RuntimeMaxSec with SIGTERM
# as systemd would.
SYSTEMD_RUN = ('limit=""\n'
               "while [[ $1 == -* ]]; do\n"
               '  if [[ $1 == -p ]]; then echo "$2" >> "$STUB/props.$PPID"; [[ $2 == RuntimeMaxSec=* ]] && limit=${2#*=}; shift; fi\n'
               "  shift\n"
               "done\n"
               'touch "$STUB/running.$PPID"\n'
               '[[ -n $limit ]] && exec timeout --preserve-status "$limit" "$@"\n'
               'exec "$@"')


class AgentGate:
    """bin/agent-gate against stubs for nvidia-smi and systemd-run, with its own config, slots, queue and log.

    The tests steer it through config.toml, which a waiting gate rereads at every look: RAM
    thresholds of 0 always pass and of 10**6 never do. A job runs in place, in the test's cgroup.
    """

    BIN = Path(__file__).resolve().parent.parent / "bin" / "agent-gate"
    EXAMPLE = Path(__file__).resolve().parent.parent / "share" / "config.example.toml"
    NEVER = 10**6

    def __init__(self, root: Path):
        self.root = root
        self.stub = root / "stub"
        self.run_dir = root / "run" / "agent-gate"
        self.log_dir = root / "state" / "agent-gate"
        self.config = root / "config" / "agent-locks" / "config.toml"
        self.claude = root / "claude"  # no sessions registered unless a test registers one
        (self.stub / "bin").mkdir(parents=True)
        self.run_dir.mkdir(parents=True)
        self.config.parent.mkdir(parents=True)
        for name, body in {
            "nvidia-smi": '[[ $1 == --query-gpu=* ]] && cat "$STUB/gpu"; exit 0',  # "total, used, util" MiB
            "systemd-run": SYSTEMD_RUN,
        }.items():
            f = self.stub / "bin" / name
            f.write_text("#!/usr/bin/env bash\n" + body + "\n")
            f.chmod(0o755)
        self.set_gpu(used=1000)
        self.settings = {("gpu", "ram_needed_gb"): 0, ("gpu", "short_slot_ram_gb"): 0, ("cpu", "ram_needed_gb"): 0}
        self.configure()
        self.env = {**os.environ, "PATH": f"{self.stub / 'bin'}:{os.environ['PATH']}", "STUB": str(self.stub),
                    "XDG_RUNTIME_DIR": str(root / "run"), "XDG_STATE_HOME": str(root / "state"),
                    "XDG_CONFIG_HOME": str(root / "config"), "CLAUDE_CONFIG_DIR": str(self.claude), "AGENT_GATE_POLL_S": "0.05",
                    "LC_ALL": "C.UTF-8"}

    def set_gpu(self, used: int, total: int = 24564) -> None:
        (self.stub / "gpu").write_text(f"{total}, {used}, 0\n")

    def configure(self, **changes) -> None:
        """Set config.toml keys, as section__key=value, on top of the example's."""
        self.settings.update({tuple(k.split("__")): v for k, v in changes.items()})
        text, section = [], ""
        for line in self.EXAMPLE.read_text().splitlines():
            if line.startswith("["):
                section = line.strip("[]")
            key = line.split("=")[0].strip()
            text.append(f"{key} = {self.settings[section, key]!r}" if (section, key) in self.settings else line)
        tmp = self.config.with_suffix(".tmp")
        tmp.write_text("\n".join(text) + "\n")
        tmp.replace(self.config)

    def start(self, kind: str, *cmd: str, env: dict | None = None, cwd: Path | None = None) -> subprocess.Popen:
        return subprocess.Popen([str(self.BIN), kind, "--", *cmd], cwd=cwd or self.root, env={**self.env, **(env or {})},
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def gate(self, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.BIN), *args], cwd=self.root, env={**self.env, **(env or {})}, capture_output=True, text=True,
                              timeout=30, check=False)

    def status(self) -> set[tuple[int, str, str]]:
        out = self.gate("status").stdout
        return {(int(w[0]), w[1], w[2]) for w in (line.split() for line in out.splitlines()[1:]) if w and w[0].isdigit()}

    def hold(self, *slots: str) -> list:
        """Take slots as another job would; closing the returned files frees them."""
        held = [open(self.run_dir / f"{s}.lock", "w") for s in slots]
        for f in held:
            fcntl.flock(f, fcntl.LOCK_EX)
        return held

    def props(self, p: subprocess.Popen) -> list[str]:
        """The systemd-run properties the gate gave its job."""
        f = self.stub / f"props.{p.pid}"
        return f.read_text().split() if f.exists() else []

    def events(self, strict: bool = True) -> list[dict]:
        out = []
        for f in sorted(self.log_dir.glob("events-*.jsonl")):
            for line in f.read_text().splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    if strict:
                        raise
        return out

    def event(self, p: subprocess.Popen, ev: str) -> dict | None:
        return next((e for e in self.events(strict=False) if e["pid"] == p.pid and e["ev"] == ev), None)

    def wait_for(self, pred, timeout: float = 10) -> None:
        end = time.time() + timeout
        while not pred(self.events(strict=False)):
            assert time.time() < end, f"timed out; events: {self.events(strict=False)}"
            time.sleep(0.05)

    def wait_for_event(self, p: subprocess.Popen, ev: str, timeout: float = 10) -> dict:
        self.wait_for(lambda _: self.event(p, ev) is not None, timeout)
        return self.event(p, ev)


@pytest.fixture
def agate(tmp_path):
    return AgentGate(tmp_path)
