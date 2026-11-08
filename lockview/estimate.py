"""How long a job will take, from how long the same job took before, and when each waiter will start.

A command is reduced to two keys: `exact` (its words, with paths cut to basenames and hashes,
numbers and temp paths masked) and `task` (what it runs: a pixi task, a script, a python module, a
pytest target). An estimate is the median of the most specific key's past runs; the p90 says how
wide they spread. With no history, a per-kind default stands in and says so.
"""

from __future__ import annotations

import re
import shlex
import statistics
from dataclasses import dataclass

_HASH = re.compile(r"^[0-9a-f]{7,64}$")
_NUM = re.compile(r"^-?\d[\d.:_-]*$")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_INTERP = {"bash", "sh", "python", "python3", "python3.12", "python3.11", "pytest"}
_GPU_HINT = re.compile(r"(?<!not )\b(gpu|cuda|nvidia-smi|train\.sh)\b")
RECENT = 20  # past runs a median looks at
RUNNERS = {"pytest", "py:pytest", "cargo", "make"}  # say how a job runs, not what it runs
# (median, p90) seconds for a kind of job with no history at all: a gate kind, or a job under a lock.
FALLBACK = {"gpu": (1800.0, 3600.0), "gpu-short": (300.0, 900.0), "cpu": (300.0, 900.0), "cpu-short": (15.0, 70.0),
            "lock": (1800.0, 3600.0)}


def default_kind(kind: str | None) -> str | None:
    """The FALLBACK kind a job's default comes from: a gpu-solo job counts as a gpu job."""
    if kind is None or kind in FALLBACK:
        return kind
    return "gpu" if kind.startswith("gpu") else "cpu"


def _words(command: str | list[str], from_journal: bool) -> list[str]:
    if isinstance(command, list):
        return list(command)
    if not from_journal:
        return command.split()  # argv joined with spaces: its quoting is gone
    try:
        return shlex.split(command)
    except ValueError:
        return command.split()


def _strip_wrappers(w: list[str]) -> list[str]:
    while w:
        head = w[0].rsplit("/", 1)[-1]
        if _ASSIGN.match(w[0]):
            w = w[1:]
        elif head == "env":
            w = w[1:]
            while w and (_ASSIGN.match(w[0]) or w[0].startswith("-")):
                w = w[1:]
        elif head == "nice":
            w = w[3:] if w[1:2] == ["-n"] else w[1:]
        elif head == "timeout":
            w = w[1:]
            while w and w[0].startswith("-"):
                w = w[2:] if w[0] in ("-s", "-k", "--signal", "--kill-after") else w[1:]
            w = w[1:]  # the duration
        elif head in ("nohup", "setsid", "stdbuf", "ionice", "exec"):
            w = w[1:]
            while w and w[0].startswith("-"):
                w = w[1:]
        elif head in ("flock", "systemd-run"):
            # flock [-n|-w N|…] <file|fd> cmd…   systemd-run [--user --scope -q -p K=V …] cmd…
            valued = {"-w", "--timeout", "-E", "-p", "--property", "--unit", "--slice", "--description", "-u", "--setenv"}
            w = w[1:]
            while w and w[0].startswith("-"):
                w = w[2:] if w[0] in valued else w[1:]
            if head == "flock":
                w = w[1:]  # the lock file or fd
        elif head == "bash" and w[1:2] == ["-c"] and w[3:4] == ["agent-gate-job"]:
            w = w[5:]  # the gate's wrapper that records the job's peak: bash -c '…' agent-gate-job <peak file> cmd…
        else:
            return w
    return w


def _script(word: str) -> str:
    """A script by its file and directory: the same file is the same job; two scripts called run.sh are not."""
    parts = word.rstrip("/").split("/")
    return "/".join(parts[-2:]) if len(parts) > 1 and parts[-2] not in ("", ".") else parts[-1]


def _mask(word: str) -> str:
    if "=" in word and word.startswith("--"):
        flag, value = word.split("=", 1)
        return flag if "/" in value else word
    if _ASSIGN.match(word):
        return word.split("=", 1)[0] + "="  # OUT=/some/dir SHA=… : which variables, not their values
    base = word.rstrip("/").rsplit("/", 1)[-1] if "/" in word else word
    if _HASH.match(base) or _NUM.match(base):
        return "#"
    return _script(word) if re.search(r"\.(sh|py)$", base) else base


def _tasks(words: list[str]) -> list[str]:
    """What a command line runs, in order: pixi tasks, scripts, python modules, pytest targets."""
    out = []
    i = 0
    while i < len(words):
        w = words[i]
        b = w.rsplit("/", 1)[-1]
        if b == "pixi" and i + 1 < len(words) and words[i + 1] == "run":
            env, j = "default", i + 2
            while j < len(words) and words[j].startswith("-"):
                if words[j] in ("-e", "--environment") and j + 1 < len(words):
                    env = words[j + 1]
                    j += 1
                elif words[j] == "--manifest-path":
                    j += 1
                j += 1
            if j < len(words):
                out.append(f"pixi:{env}:{words[j]}")
                i = j
        elif b in ("make", "cargo"):
            # make -C dir -j6 check OUT=…  ->  make check;  cargo test -j 2  ->  cargo test
            j = i + 1
            while j < len(words) and (words[j].startswith("-") or "=" in words[j]):
                j += 2 if words[j] in ("-C", "-f", "-j", "--manifest-path", "-p", "--package") else 1
            out.append(f"{b} {words[j]}" if j < len(words) else b)
            i = j
        elif b == "-m" and i + 1 < len(words) and words[i - 1].rsplit("/", 1)[-1].startswith("python"):
            out.append(f"py:{words[i + 1]}")
            i += 1
        elif re.search(r"\.(sh|py)$", b) and not b.startswith("test_"):
            out.append(_script(w))
        elif b == "pytest" or (b.startswith("test_") and b.endswith(".py")):
            out.append(b)
        i += 1
    seen = []
    for t in out:
        if t not in seen:
            seen.append(t)
    return seen[:4]


@dataclass(frozen=True)
class Keys:
    exact: str
    task: str
    gpu_hint: bool


def keys(command: str | list[str], from_journal: bool = False) -> Keys:
    """command: an argv list; a string in the journal's quoting (from_journal); or else a string of
    argv joined with spaces, as /proc's command line and the gate's log give it."""
    words = _strip_wrappers(_words(command, from_journal))
    if words and words[0].rsplit("/", 1)[-1] in ("bash", "sh") and "-c" in words[1:3]:
        i = words.index("-c") + 1
        if isinstance(command, str) and not from_journal:
            script = " ".join(words[i:])  # joined with its arguments: the script runs to the end
        else:
            script = words[i] if i < len(words) else ""
        if from_journal:
            script = script.replace("\\n", "\n")
        words = ["sh", "-c", *re.findall(r"[^\s;|&()\"'`$]+", script)]
    elif words and words[0].rsplit("/", 1)[-1] in _INTERP and len(words) > 1:
        # `bash script.sh args`, `python -s script.py`: the script is the program.
        rest = [w for w in words[1:] if not re.fullmatch(r"-[sIBuxel]+", w)]
        if rest and rest[0] != "-m":
            words = rest
    masked = [_mask(w) for w in words if w]
    text = " ".join(words)
    tasks = _tasks(words)
    return Keys(
        exact=" ".join(masked[:24]),
        task=" + ".join(tasks) if tasks else (masked[0] if masked else ""),
        gpu_hint=bool(_GPU_HINT.search(text)),
    )


@dataclass(frozen=True)
class Estimate:
    median: float
    p90: float
    n: int
    basis: str  # exact | task | default
    key: str

    def remaining(self, elapsed: float) -> tuple[float, bool]:
        """Seconds left and whether the job has run past its usual range (p90)."""
        if elapsed < self.median:
            return self.median - elapsed, False
        if elapsed < self.p90:
            return self.p90 - elapsed, False
        return max(60.0, 0.1 * elapsed), True

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _summary(durations: list[float], basis: str, key: str) -> Estimate:
    recent = durations[-RECENT:]
    ordered = sorted(recent)
    p90 = ordered[min(len(ordered) - 1, round(0.9 * (len(ordered) - 1)))]
    return Estimate(statistics.median(recent), max(p90, statistics.median(recent)), len(recent), basis, key)


class Estimator:
    """Past durations by key. `defaults` stands in, per gate kind, when a job has no history: the
    first runs of new jobs of its kind, since a kind's runs are mostly whatever job repeats most."""

    MIN_RUN = 5.0  # a scope that ends sooner failed to start; it says nothing about the job

    def __init__(self, runs):
        """runs: (Keys, duration_s, kind: a gate kind, lock or None), oldest first."""
        self.exact: dict[str, list[float]] = {}
        self.task: dict[str, list[float]] = {}
        by_kind: dict[str, list[float]] = {kind: [] for kind in FALLBACK}
        for k, duration, kind in runs:
            if duration < self.MIN_RUN:
                continue
            new = k.task not in self.task
            self.exact.setdefault(k.exact, []).append(duration)
            self.task.setdefault(k.task, []).append(duration)
            if new and (d := default_kind(kind)) is not None:
                by_kind[d].append(duration)
        self.defaults = {
            kind: _summary(ds, "default", f"any {kind} job") if len(ds) >= 3
            else Estimate(*FALLBACK[kind], 0, "default", f"any {kind} job (no history: a guess)")
            for kind, ds in by_kind.items()
        }

    def estimate(self, command: str | list[str], kind: str) -> Estimate:
        k = keys(command)
        if len(self.exact.get(k.exact, ())) >= 2:
            return _summary(self.exact[k.exact], "exact", k.exact)
        if k.task in self.task:
            return _summary(self.task[k.task], "task", k.task)
        if k.exact in self.exact:
            return _summary(self.exact[k.exact], "exact", k.exact)
        # A wrapper around known parts (setup.sh + bench.sh) takes at least its longest part.
        parts = [_summary(self.task[p], "part", p) for p in k.task.split(" + ") if p in self.task and p not in RUNNERS]
        if parts:
            return max(parts, key=lambda e: e.median)
        return self.defaults[default_kind(kind)]


@dataclass
class Plan:
    pid: int
    start: float
    end: float
    slots: list[str]


def simulate(slots: dict[str, float], waiters: list[dict], now: float, patience: float,
             short_only: frozenset[str] = frozenset(), held_by_short: frozenset[str] = frozenset()) -> dict[int, Plan]:
    """Predict start and end for each waiter on one lock's slots, by the gate's own rules.

    slots: slot name -> when it frees (now, or a running job's predicted end).
    waiters: oldest first; each {"pid", "needs_idle", "queued_at", "duration"}, "short" for a job the
    gate stops at a limit (gpu-short, cpu-short), and "own_first" for a short job that tries the
    short_only slots first (cpu-short).
    short_only: the slots only short jobs may take (gpu.3, cpu.4); held_by_short: those a short job holds now.
    A job that needs an idle GPU takes every slot at once; younger jobs pass it while it waits,
    until it has waited `patience`, after which only short jobs start, and only while a job that
    isn't short runs, until every slot drains. Otherwise jobs go first come, first served, except
    that a short job passes the others for a short_only slot, and passes them during a drain.
    Resource checks (RAM, VRAM, ungated programs) are assumed to pass: they are not predictable.
    """
    free_at = dict(slots)
    short_in = set(held_by_short)
    pending = list(waiters)
    plans: dict[int, Plan] = {}
    t = now
    while pending:
        free = sorted(s for s, at in free_at.items() if at <= t)
        shared = [s for s in free if s not in short_only]
        own = [s for s in free if s in short_only]
        draining = any(j["needs_idle"] and t - j["queued_at"] >= patience for j in pending)
        long_runs = any(at > t and s not in short_in for s, at in free_at.items())
        started = None
        older = older_short = False  # an older job that isn't short, or is, still waits
        for j in pending:
            if j["needs_idle"]:
                if len(free) == len(free_at):
                    started = (j, list(free_at))
                    break
            elif j.get("short"):
                if (not draining or long_runs) and not older_short:
                    tries = [(shared, draining or not older), (own, True)]
                    if j.get("own_first"):
                        tries.reverse()
                    pick = next((free_slots[0] for free_slots, ok in tries if free_slots and ok), None)
                    if pick:
                        started = (j, [pick])
                        break
                older_short = True
            else:
                if shared and not draining and not older and not older_short:
                    started = (j, [shared[0]])
                    break
                older = True
        if started:
            j, taken = started
            end = t + j["duration"]
            for s in taken:
                free_at[s] = end
                (short_in.add if j.get("short") else short_in.discard)(s)
            plans[j["pid"]] = Plan(j["pid"], t, end, taken)
            pending.remove(j)
            continue
        later = [at for at in free_at.values() if at > t]
        if not later:
            break
        t = min(later)
    return plans
