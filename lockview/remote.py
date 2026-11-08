"""Other machines, watched over ssh: config.toml's [remote] hosts lists their ssh aliases.

For each host a thread keeps one ssh connection open (BatchMode, so it never prompts). It sends
probe.py as python3's program on stdin, and the probe prints a JSON line of the host's readings
every interval: RAM, memory pressure, GPUs, CPU and its heavy jobs. A connection that fails is
retried, 5 s later at first and then up to every minute.

Who started a remote job: the Claude session there that it runs under, or else the ssh client here
whose connection started it. sshd puts the client's address and port in SSH_CONNECTION, and the
local ssh process with a socket on that address and port is the job's client. Clients are
remembered for a day after they exit, so a job detached with `setsid nohup … &` keeps its owner,
if lockview saw its client live: a job matches a remembered client only if it started while that
client ran, since the client's port may since have been reused.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import os
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import config, owners
from .procs import Snapshot

PROBE = Path(__file__).resolve().with_name("probe.py")
RETRY = (5.0, 60.0)  # the first retry's delay, and the longest
KEEP = 86400  # how long an ssh client is remembered after it was last seen
SKEW = 30.0  # seconds of clock difference, and of sampling, allowed in matching a job to a client


def ssh_command(alias: str, interval: float) -> list[str]:
    return ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=10",
            "-o", "ServerAliveCountMax=3", alias, "python3", "-u", "-", str(interval)]


class Host:
    """One machine's latest reading, kept current by a thread that runs the probe there."""

    def __init__(self, alias: str, interval: float, command: list[str] | None = None, retry: tuple[float, float] = RETRY):
        self.alias = alias
        self.command = command or ssh_command(alias, interval)
        self.retry = retry
        self.reading: dict | None = None
        self.received = 0.0  # when the reading arrived
        self.connected = False
        self.error = ""

    def start(self) -> None:
        threading.Thread(target=self.run, daemon=True, name=f"remote:{self.alias}").start()

    def run(self) -> None:
        delay = self.retry[0]
        while True:
            if self.connect():
                delay = self.retry[0]
            time.sleep(delay)
            delay = min(2 * delay, self.retry[1])

    def connect(self) -> bool:
        """One connection, until it closes. Whether any reading came over it."""
        try:
            p = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as e:
            self.error = str(e)
            return False
        with contextlib.suppress(OSError):  # ssh exited before it took the probe: its stderr says why
            p.stdin.write(PROBE.read_text())
        with contextlib.suppress(OSError):
            p.stdin.close()  # python3 - runs its program once stdin ends
        got = False
        for line in p.stdout:
            try:
                reading = json.loads(line)
            except ValueError:
                continue
            self.reading, self.received, self.connected, self.error, got = reading, time.time(), True, "", True
        self.connected = False
        code = p.wait()
        lines = [x.strip() for x in p.stderr.read().splitlines() if x.strip()]
        self.error = lines[-1] if lines else self.error or f"the connection closed (exit {code})"
        return got

    def as_dict(self, now: float, clients: dict[tuple, Client]) -> dict:
        out = {"alias": self.alias, "connected": self.connected, "error": self.error,
               "age": now - self.received if self.received else None, "reading": None}
        if self.reading:
            r = dict(self.reading)
            r["jobs"] = [_job(j, r.get("home", ""), self.alias, clients, now) for j in r.get("jobs", [])]
            out["reading"] = r
        return out


def _job(j: dict, home: str, alias: str, clients: dict[tuple, Client], now: float) -> dict:
    owner, via = owner_of(j, home, alias, clients, now)
    return {**j, "command": unwrap(j["command"]), "owner": owner.as_dict(), "via": via}


def unwrap(command: str) -> str:
    """The command a Claude session's Bash tool ran, from inside the shell wrapper it runs it in
    (`… && eval '<command>' && pwd -P >| …`). Any other command comes back as it is."""
    marker = " && eval "
    if "shell-snapshots/snapshot-" not in command or marker not in command:
        return command
    lex = shlex.shlex(command[command.index(marker) + len(marker):], posix=True)
    lex.whitespace_split = True
    try:
        return lex.get_token() or command  # only the first word: a cut-off tail after it doesn't matter
    except ValueError:
        return command


@dataclass(frozen=True)
class Client:
    """An ssh client process seen here."""

    pid: int
    started: float
    last_seen: float
    owner: owners.Owner


def owner_of(j: dict, home: str, alias: str, clients: dict[tuple, Client], now: float) -> tuple[owners.Owner, str]:
    """Who started a job on another machine, and how that is known. clients: (address, port) -> the
    ssh client here that connected from there, live (last seen `now`) or remembered."""
    if s := j.get("session"):
        return owners.Owner(_project(s["cwd"], home), s["name"]), f"Claude session {s['name']} on {alias}"
    if j.get("ssh"):
        ip, port = _ip(j["ssh"][0]), j["ssh"][1]
        c = clients.get((ip, port))
        if c and c.started - SKEW <= j["start"] <= c.last_seen + SKEW:
            return c.owner, f"ssh from here, pid {c.pid}" + ("" if c.last_seen == now else ", which has since exited")
        if any(addr == ip for addr, _ in clients):
            return owners.Owner(""), "ssh from here, by a client that exited before lockview saw it"
        return owners.Owner(""), f"ssh from {ip}"
    return owners.Owner(_project(j.get("cwd", ""), home)), ""


def _project(cwd: str, home: str) -> str:
    """A directory there, by name: this machine can't ask its git."""
    cwd = cwd.rstrip("/")
    return Path(cwd).name if cwd and cwd != home.rstrip("/") else ""


def _ip(text: str):
    a = ipaddress.ip_address(text)
    return a.ipv4_mapped or a if a.version == 6 else a


def _hex_addr(field: str):
    """An address:port from /proc/net/tcp{,6}, where each 32-bit word is in host (little-endian) order."""
    addr, port = field.split(":")
    raw = bytes.fromhex(addr)
    raw = b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4))
    return _ip(str(ipaddress.ip_address(raw))), int(port, 16)


def ssh_clients(snap: Snapshot, proc: Path = Path("/proc")) -> dict[tuple, int]:
    """(local address, port) -> the ssh client process here that holds that TCP socket."""
    by_inode = {}
    for p in snap.procs.values():
        if p.comm != "ssh":
            continue
        try:
            fds = os.listdir(proc / str(p.pid) / "fd")
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(proc / str(p.pid) / "fd" / fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                by_inode[target[8:-1]] = p.pid
    out = {}
    if not by_inode:
        return out
    for name in ("tcp", "tcp6"):
        try:
            lines = (proc / "net" / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) > 9 and f[9] in by_inode:
                out[_hex_addr(f[1])] = by_inode[f[9]]
    return out


class Remotes:
    def __init__(self, aliases: tuple[str, ...], interval: float):
        self.hosts = [Host(a, interval) for a in aliases]
        self.clients: dict[tuple, Client] = {}

    def start(self) -> None:
        for h in self.hosts:
            h.start()

    def state(self, snap: Snapshot, sessions: dict[int, owners.Session], now: float) -> dict:
        if self.hosts:
            self.note_clients(snap, sessions, now)
        return {"config": config.tilde(config.PATH),
                "hosts": [h.as_dict(now, self.clients) for h in self.hosts]}

    def note_clients(self, snap: Snapshot, sessions: dict[int, owners.Session], now: float) -> None:
        for key, pid in ssh_clients(snap).items():
            p = snap.procs[pid]
            self.clients[key] = Client(pid, p.started_at(snap.btime), now, owners.owner_of(pid, snap, sessions))
        self.clients = {k: c for k, c in self.clients.items() if now - c.last_seen < KEEP}
