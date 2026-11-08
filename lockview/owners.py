"""Who a job belongs to: the live Claude session that started it, and the git repository it runs in.

A session registers itself in ~/.claude/sessions/<pid>.json ($CLAUDE_CONFIG_DIR/sessions if that
is set); subagents run inside their parent's process, so a job's nearest registered ancestor is its
session. A session's scratchpad,
<tmp>/claude-<uid>/<project>/<session id>/, is that session's too.

A job's project is the git repository of its working directory: the main repository for a linked
worktree, whose directory name is then the job's worktree. Outside git it is its session's project,
or else the directory under home it runs in. A directory that has since been removed, such as a
worktree, is placed by its nearest surviving ancestor.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from . import procs
from .procs import Snapshot

SESSIONS_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "sessions"
HOME = Path.home()

_SCRATCH = re.compile(r"/claude-\d+/-[^/]*/(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?:/|$)")


@dataclass(frozen=True)
class Session:
    pid: int
    name: str
    session_id: str
    project: str
    worktree: str
    status: str


@dataclass(frozen=True)
class Owner:
    project: str  # "" when nothing names one
    session: str = ""  # live session name, e.g. rover-30
    worktree: str = ""  # the linked git worktree it runs in, by directory name

    def as_dict(self) -> dict:
        return {"project": self.project, "session": self.session, "worktree": self.worktree}


def checked(value) -> dict | None:
    """An owner recorded by the gate (Owner.as_dict()), if it has that shape: project, session and
    worktree, as text. Extra keys are dropped."""
    keys = ("project", "session", "worktree")
    if not isinstance(value, dict) or not all(isinstance(value.get(k), str) for k in keys):
        return None
    return {k: value[k] for k in keys}


def repo(directory: str) -> tuple[str, str] | None:
    """(project, worktree) of the git checkout a directory is in; None outside git. Home itself is
    never the checkout, so a dotfiles repository there doesn't own everything under it."""
    if not directory:
        return None
    d = Path(directory)
    while not d.is_dir() and d != d.parent:
        d = d.parent
    for top in (d, *d.parents):
        if top == HOME:
            return None
        git = top / ".git"
        if git.is_dir():
            return top.name, ""
        if git.is_file():
            return _linked(top, git)
    return None


def _linked(top: Path, git: Path) -> tuple[str, str]:
    """A .git file. A linked worktree's points to its main repository's .git/worktrees/<id>, whose
    commondir file leads back to that .git (or to a bare repository); a submodule's has no commondir."""
    try:
        gitdir = top / git.read_text().removeprefix("gitdir:").strip()
        common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
    except OSError:
        return top.name, ""
    return common.parent.name if common.name == ".git" else common.name.removesuffix(".git"), top.name


def under_home(directory: str) -> str:
    """The directory under home that a path is in, unless it is hidden; "" for anything else."""
    parts = Path(directory).parts[len(HOME.parts):] if directory and Path(directory).is_relative_to(HOME) else ()
    return parts[0] if parts and not parts[0].startswith(".") else ""


def place(directory: str) -> tuple[str, str]:
    """(project, worktree) for a directory alone."""
    return repo(directory) or (under_home(directory), "")


def _registration(f: Path) -> dict | None:
    try:
        d = json.loads(f.read_text())
        d["pid"] = int(d["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return d


def _session(d: dict, start_ticks: int) -> Session | None:
    """The session a registration describes, if its pid is still the process that registered (pids get reused)."""
    if str(start_ticks) != str(d.get("procStart", start_ticks)):
        return None
    project, worktree = place(d.get("cwd", ""))
    return Session(d["pid"], d.get("name") or d.get("sessionId", "")[:8], d.get("sessionId", ""), project, worktree,
                   d.get("status", ""))


def load_sessions(snap: Snapshot, directory: Path = SESSIONS_DIR) -> dict[int, Session]:
    """Every live registered session."""
    out: dict[int, Session] = {}
    for f in directory.glob("*.json"):
        d = _registration(f)
        p = snap.get(d["pid"]) if d else None
        if p and (s := _session(d, p.start_ticks)):
            out[s.pid] = s
    return out


def own_session(pid: int, directory: Path = SESSIONS_DIR) -> Session | None:
    """The nearest registered session of a process and its ancestors, read from /proc one ancestor
    at a time: for a process asking about itself, without a snapshot of every process."""
    while pid > 1:
        try:
            ppid, _, start = procs.read_stat(pid)
        except (OSError, ValueError, IndexError):
            return None
        d = _registration(directory / f"{pid}.json")
        if d and d["pid"] == pid and (s := _session(d, start)):
            return s
        pid = ppid
    return None


def of(directory: str, sessions: dict[int, Session], session: Session | None = None) -> Owner:
    """The owner of work in a directory, done by `session` if that is known."""
    m = _SCRATCH.search(directory)
    if m:  # whatever a scratchpad holds is its session's
        session = session or next((s for s in sessions.values() if s.session_id == m["id"]), None)
    found = None if m else repo(directory)
    if session is None:
        return Owner(found[0], worktree=found[1]) if found else Owner(under_home(directory))
    project, worktree = found or (session.project, session.worktree)
    return Owner(project, session.name, worktree)


def owner_of(pid: int, snap: Snapshot, sessions: dict[int, Session]) -> Owner:
    """A process's owner: its nearest registered ancestor's session, and its working directory."""
    session = next((sessions[p] for p in (pid, *(a.pid for a in snap.ancestors(pid))) if p in sessions), None)
    return of(procs.cwd(pid), sessions, session)
