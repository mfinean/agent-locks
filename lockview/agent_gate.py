"""agent-gate: run a heavy job in a machine-wide slot, so the agents sharing a machine take turns.

    agent-gate <kind> [--] <command...>    run the command once a slot of its kind is free
    agent-gate status                       list the queue

The kinds are gpu, gpu-short, gpu-solo, cpu and cpu-short. config.toml sets their numbers and
policy.py has the rules. The job runs in a systemd user scope, agent-gate-<pid>.scope, with
MemoryMax, no swap and a low CPUWeight, and the gate exits with the job's exit code. A short kind's scope stops at
its limit (SIGTERM, then SIGKILL 30 s later: exit 143 or 137), even if the gate dies.

The queue is a directory of tickets, $XDG_RUNTIME_DIR/agent-gate/queue/<pid>-<tick>.json: each gate's
kind, when it queued, and whether its job runs. A ticket whose process has gone, or whose pid now
belongs to another process, is stale, and the next gate that reads it removes it. The slots are flock
locks on <lock>.<n>.lock beside it. The job inherits them, so it keeps its slot if its gate dies.

While it waits, the gate looks again every 15 s ($AGENT_GATE_POLL_S), and rereads config.toml each
time, so a change applies to waiting jobs too. A gpu job that needs more free VRAM than config.toml's
vram_needed_mb sets $AGENT_GATE_VRAM_MB (MiB): it waits for that much, and holds up the jobs behind
it. A job at its MemoryMax doesn't die: the kernel
reclaims its mapped files and it thrashes, and that stall counts in the whole session's memory
pressure, which systemd-oomd kills it for. So the gate kills its own job once the job has stalled
on memory for more than half of each of three seconds in a row (exit 137).

What happens to each job is appended to $XDG_STATE_HOME/agent-gate/events-<YYYY-MM>.jsonl (README:
the gate's event log). Logging is best-effort: a log that can't be written never delays or fails a job.
"""

from __future__ import annotations

import fcntl
import fnmatch
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import config, owners, policy, resources

RUN_DIR = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp") / "agent-gate"
QUEUE = RUN_DIR / "queue"
LOG_DIR = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "agent-gate"
CGROUPS = Path("/sys/fs/cgroup")
IN_SCOPE = re.compile(r"/agent-gate-\d+\.scope(/|$)")  # a cgroup in a gate job's scope
POLL_S = float(os.environ.get("AGENT_GATE_POLL_S") or 15)
SAY_EVERY_S = 300  # how often a waiting gate says it still waits
STOP_GRACE_S = 30  # between a limit's SIGTERM and its SIGKILL
CPU_WEIGHT = 20  # a job's share of a busy CPU against the desktop's apps, which weigh 100; an idle CPU is all its own
THRASH_US = 500_000  # memory stall, in µs per second, that counts as thrashing
THRASH_S = 3
MAX_COMMAND = 2000
# The job's own last act, inside its scope, records its peak: the scope is gone once it ends.
WRAPPER = ('peak=$1; shift; "$@"; code=$?\n'
           'cat "/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/memory.peak" > "$peak" 2>/dev/null\n'
           "exit $code")


def version(directory: Path = Path(__file__).parent) -> str:
    """The first 8 hex digits of the sha1 of the gate's code there: which copy a gate runs."""
    return hashlib.sha1(b"".join((directory / f).read_bytes() for f in ("agent_gate.py", "policy.py"))).hexdigest()[:8]


VERSION = version()


def say(text: str) -> None:
    print(f"agent-gate: {text}", file=sys.stderr, flush=True)


def start_tick(pid: int) -> int | None:
    """When a process started, in clock ticks since boot (/proc/<pid>/stat's 22nd field)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return int(stat[stat.rindex(")") + 2:].split()[19])


@dataclass
class Ticket:
    pid: int
    tick: int
    kind: str
    queued: float
    cwd: str
    command: list[str]
    vram_mb: int | None = None  # $AGENT_GATE_VRAM_MB: the free VRAM it needs, in place of config.toml's
    state: str = "waiting"
    slots: list[str] = field(default_factory=list)
    unit: str = ""
    started: float | None = None
    owner: dict = field(default_factory=dict)  # owners.Owner.as_dict(), when it queued
    version: str = VERSION  # the copy of the gate it runs

    @property
    def path(self) -> Path:
        return QUEUE / f"{self.pid}-{self.tick}.json"

    def job(self, cfg: policy.Config) -> policy.Job:
        return policy.Job(self.pid, self.tick, self.kind, self.kind == "gpu-solo", self.queued, self.state == "running",
                          self.vram_mb or cfg.vram_mb)

    def write(self) -> None:
        QUEUE.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self)))
        tmp.replace(self.path)


def read_queue(remove_stale: bool = True) -> list[Ticket]:
    """Every live gate's ticket. A stale one is removed, unless the reader only watches (lockview)."""
    out = []
    for f in QUEUE.glob("*.json"):
        try:
            t = Ticket(**json.loads(f.read_text()))
        except (OSError, ValueError, TypeError):
            continue
        if start_tick(t.pid) == t.tick:
            out.append(t)
        elif remove_stale:
            f.unlink(missing_ok=True)
    return out


def programs(patterns: tuple[str, ...]) -> tuple[policy.Program, ...]:
    """The processes of config.toml's programs; one is gated when it runs in a gate's scope."""
    if not patterns:
        return ()
    out = []
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{name}/exe")
            if any(fnmatch.fnmatch(exe, p) for p in patterns):
                out.append(policy.Program(int(name), bool(IN_SCOPE.search(Path(f"/proc/{name}/cgroup").read_text()))))
        except OSError:
            continue
    return tuple(out)


def machine(cfg: policy.Config, kind: str) -> policy.Machine:
    ram = resources.ram_available_gb()
    if cfg.kinds[kind].lock != "gpu":
        return policy.Machine(ram, 0)  # a cpu job waits only for RAM
    g = resources.gpu(processes=False)
    return policy.Machine(ram, g.free_mb if g.ok else None, g.error, programs(cfg.programs))


class Log:
    """One gate's lines in this month's event log."""

    def __init__(self, pid: int, tick: int):
        self.pid, self.tick = pid, tick

    def __call__(self, ev: str, **fields) -> None:
        line = json.dumps({"v": 1, "t": round(time.time(), 3), "ev": ev, "pid": self.pid, "tick": self.tick, **fields},
                          ensure_ascii=False, separators=(",", ":"))
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            with open(LOG_DIR / time.strftime("events-%Y-%m.jsonl"), "a", encoding="utf-8") as f:
                end = time.monotonic() + 1  # a writer stuck holding the lock costs at most a second
                while True:
                    try:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > end:
                            return
                        time.sleep(0.02)
                f.write(line + "\n")
        except OSError:
            pass


def _take(lock: str, i: int) -> int | None:
    """Slot <lock>.<i>'s lock, held on the returned fd, or None if another job holds it."""
    fd = os.open(RUN_DIR / f"{lock}.{i}.lock", os.O_WRONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _release(fds: list[int]) -> None:
    for fd in fds:
        os.close(fd)


def _load(kind: str) -> policy.Config:
    cfg = policy.from_settings(config.load())
    if kind not in cfg.kinds:
        raise config.ConfigError(f"{config.tilde(config.PATH)} has no {kind} jobs (no [{kind.split('-')[0]}] section)")
    return cfg


def wait_for_slots(t: Ticket, cfg: policy.Config, log: Log) -> tuple[policy.Config, list[int], list[str]]:
    """Look until the rules let the job start in a free slot; return the config then and the held slots."""
    last_why, said, began, broken = None, -SAY_EVERY_S, time.monotonic(), ""
    while True:
        k = cfg.kinds[t.kind]
        why, detail = "slots", ""  # with no free slot, the rules aren't even asked
        m = None
        tries = [list(range(1, k.slots + 1))] if t.kind == "gpu-solo" else [[i] for i in cfg.order(t.kind)]
        for slots in tries:
            fds = []
            for i in slots:
                fd = _take(k.lock, i)
                if fd is None:
                    break
                fds.append(fd)
            if len(fds) < len(slots):
                _release(fds)
                continue
            queue = [x.job(cfg) for x in read_queue()]
            m = m or machine(cfg, t.kind)
            reasons = policy.blockers(t.job(cfg), 0 if t.kind == "gpu-solo" else slots[0], queue, m, cfg, time.time())
            if not reasons:
                return cfg, fds, [f"{k.lock}.{i}" for i in slots]
            _release(fds)
            why, detail = reasons[0].why, reasons[0].text
        if why != last_why:
            log("wait", why=why, detail=detail)
            last_why = why
        waited = time.monotonic() - began
        if waited - said >= SAY_EVERY_S:
            say(f"waiting for a {t.kind} slot ({waited:.0f}s)")
            said = waited
        time.sleep(POLL_S)
        try:
            cfg, broken = _load(t.kind), ""
        except config.ConfigError as e:
            if str(e) != broken:  # once, not at every look until someone fixes it
                say(f"{e}; keeping the settings it had")
            broken = str(e)


class Thrash:
    """Counts the seconds in a row that a job stalled on memory for more than half of each."""

    def __init__(self) -> None:
        self.before: int | None = None
        self.hot = 0

    def observe(self, stall_us: int) -> bool:
        """Its memory.pressure `some` total now, a second after the last; whether to kill it."""
        if self.before is not None:
            self.hot = self.hot + 1 if stall_us - self.before > THRASH_US else 0
        self.before = stall_us
        return self.hot >= THRASH_S


def _stall_us(cg: Path) -> int | None:
    try:
        for line in (cg / "memory.pressure").read_text().splitlines():
            if line.startswith("some"):
                return int(line.rsplit("total=", 1)[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def iec(n: float) -> str:
    """Bytes as numfmt --to=iec writes them: 512, 46M, 1.5G, 16G."""
    unit = ""
    for u in "KMGTPE":
        if n < 1024:
            break
        n, unit = n / 1024, u
    return f"{math.ceil(n * 10) / 10:.1f}{unit}" if unit and n < 10 else f"{math.ceil(n)}{unit}"


class Watcher(threading.Thread):
    """Watches the job's scope until it ends: keeps its peak memory, and kills it if it thrashes."""

    def __init__(self, pid: int, unit: str, mem: str):
        super().__init__(daemon=True)
        self.pid, self.unit, self.mem = pid, unit, mem
        self.done = threading.Event()
        self.peak: int | None = None

    def run(self) -> None:
        cg = None
        while cg is None and not self.done.wait(0.2):  # systemd-run moves the job into its scope as it starts
            try:
                path = Path(f"/proc/{self.pid}/cgroup").read_text().strip().split("::", 1)[-1]
            except OSError:
                return
            if path.endswith("/" + self.unit):
                cg = CGROUPS / path.lstrip("/")
        thrash = Thrash()
        while cg and not self.done.wait(1.0):
            stall = _stall_us(cg)
            if stall is None:
                return
            try:
                self.peak = int((cg / "memory.peak").read_text())
            except (OSError, ValueError):
                pass
            if thrash.observe(stall):
                say(f"killed the job: it thrashed at its {self.mem} MemoryMax for {THRASH_S} s, which would soon have had "
                    f"systemd-oomd kill the whole session. Its peak was {iec(self.peak or 0)}.")
                subprocess.run(["systemctl", "--user", "kill", "-s", "KILL", self.unit], check=False)
                return


def run(t: Ticket, cfg: policy.Config, fds: list[int], slots: list[str], log: Log, waited: float) -> int:
    k = cfg.kinds[t.kind]
    t.state, t.slots, t.unit, t.started = "running", slots, f"agent-gate-{t.pid}.scope", time.time()
    t.write()
    say(f"{t.kind} after {waited:.0f}s: {' '.join(t.command)}")
    log("start", unit=t.unit, slots=slots)
    peak_file = RUN_DIR / f"peak.{t.pid}"
    props = ["-p", f"MemoryMax={k.mem}", "-p", "MemorySwapMax=0", "-p", f"CPUWeight={CPU_WEIGHT}"]
    if k.limit:
        props += ["-p", f"RuntimeMaxSec={k.limit}", "-p", f"TimeoutStopSec={STOP_GRACE_S}"]
    began = time.monotonic()
    try:
        p = subprocess.Popen(["systemd-run", "--user", "--scope", "-q", "--collect", f"--unit={t.unit}", *props,
                              "bash", "-c", WRAPPER, "agent-gate-job", str(peak_file), *t.command], pass_fds=fds)
    except OSError as e:
        say(f"can't start the job: {e}")
        log("end", code=127, peak=None)
        return 127
    watcher = Watcher(p.pid, t.unit, k.mem)
    watcher.start()
    while True:
        try:
            code = p.wait()
            break
        except KeyboardInterrupt:
            continue  # the terminal sent the job the same Ctrl-C: its exit code says what it did
    watcher.done.set()
    watcher.join()
    code = 128 - code if code < 0 else code
    if k.limit and code in (143, 137) and time.monotonic() - began >= k.limit:
        say(f"stopped the job at the {t.kind} limit of {k.limit / 60:g} min; rerun it as {t.kind.removesuffix('-short')}")
    try:
        peak = int(peak_file.read_text())
    except (OSError, ValueError):
        peak = watcher.peak
    peak_file.unlink(missing_ok=True)
    if peak is not None:
        say(f"the job's peak memory was {iec(peak)} of {k.mem}")
    log("end", code=code, peak=peak)
    return code


def _age(seconds: float) -> str:
    """As ps's etime: [[dd-]hh:]mm:ss."""
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return (f"{d}-" if d else "") + (f"{h:02d}:" if d or h else "") + f"{m:02d}:{s:02d}"


def status() -> int:
    """The queue, waiting gates first, one line each that starts with the gate's pid; then the machine."""
    now = time.time()
    print("pid kind state needs-idle-gpu age command")
    for t in sorted(read_queue(), key=lambda t: (t.state != "waiting", t.pid)):
        print(f"{t.pid} {t.kind} {t.state} {'yes' if t.kind == 'gpu-solo' else 'no'} {_age(now - t.queued)} "
              f"{' '.join(t.command)[:90]}")
    try:
        cfg = policy.from_settings(config.load())
    except config.ConfigError as e:
        print(f"settings: {e}")
        return 0
    line = f"programs running: {len(programs(cfg.programs))}; RAM available: {resources.ram_available_gb()} GB"
    if "gpu" in cfg.kinds:
        g = resources.gpu(processes=False)
        line += f"; GPU free: {g.free_mb if g.ok else 'unknown'} MiB"
    print(line)
    return 0


def main(args: list[str], prog: str = "agent-gate") -> int:
    """args: what follows the name it was run as, `prog`. The launcher passes that name on, so the
    gate's command line reads like the command that started it."""
    if args[:1] == ["status"]:
        return status()
    if not args or args[0].startswith("-"):
        say(f"usage: {prog} gpu|gpu-short|gpu-solo|cpu|cpu-short -- <command...> | {prog} status")
        return 2
    kind, command = args[0], args[1:]
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        say("no command")
        return 2
    if kind not in ("gpu", "gpu-short", "gpu-solo", "cpu", "cpu-short"):
        say("kind must be gpu, gpu-short, gpu-solo, cpu or cpu-short")
        return 2
    try:
        cfg = _load(kind)
    except config.ConfigError as e:
        say(str(e))
        return 2
    vram = os.environ.get("AGENT_GATE_VRAM_MB", "")
    if vram and not vram.isdigit():
        say(f"AGENT_GATE_VRAM_MB must be a whole number of MiB, not {vram!r}")
        return 2
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    me = os.getpid()
    cwd = os.getcwd()
    owner = owners.of(cwd, {}, owners.own_session(me)).as_dict()  # now, while its worktree and session exist
    t = Ticket(me, start_tick(me), kind, time.time(), cwd, command, int(vram) if vram else None, owner=owner)
    log = Log(t.pid, t.tick)
    log("queue", kind=kind, cwd=t.cwd, cmd=" ".join(command)[:MAX_COMMAND], gate=VERSION, owner=owner)
    t.write()
    try:
        began = time.monotonic()
        cfg, fds, slots = wait_for_slots(t, cfg, log)
        return run(t, cfg, fds, slots, log, time.monotonic() - began)
    except KeyboardInterrupt:
        return 130
    finally:
        t.path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
