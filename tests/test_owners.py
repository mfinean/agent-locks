"""Owners from git: a job's project is its repository's, a linked worktree's is its main repository's,
and its session comes from its ancestors. Real repositories and worktrees, under a stand-in home."""

import json
import os
import subprocess

import pytest

from lockview import owners, procs

SESSION_ID = "5fe25d7d-4f92-403c-b77d-1253e8b04e2b"


def _git(*args, cwd):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd, check=True, capture_output=True)


def _session(pid=10, project="rover", worktree=""):
    return owners.Session(pid, "rover-30", SESSION_ID, project, worktree, "busy")


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(owners, "HOME", tmp_path / "home")
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


@pytest.fixture
def repo(home):
    r = home / "rover"
    (r / "src").mkdir(parents=True)
    _git("init", "-q", cwd=r)
    _git("commit", "-q", "--allow-empty", "-m", "x", cwd=r)
    return r


def _worktree(repo, path):
    _git("worktree", "add", "-q", "--detach", str(path), cwd=repo)
    return path


def test_a_checkout_is_its_project(repo):
    assert owners.place(str(repo)) == owners.place(str(repo / "src")) == ("rover", "")


def test_a_linked_worktree_is_its_main_repositorys_wherever_it_lies(repo, home):
    for wt in (home / "wt" / "arm" / "pr42", repo / ".claude" / "worktrees" / "review", home / "rover-docs"):
        _worktree(repo, wt)
        (wt / "sub").mkdir()
        assert owners.place(str(wt / "sub")) == ("rover", wt.name)


def test_a_bare_repositorys_worktree_is_named_without_dot_git(repo, home):
    bare = home / "srv" / "drivers.git"
    _git("clone", "-q", "--bare", str(repo), str(bare), cwd=home)
    assert owners.place(str(_worktree(bare, home / "drv-split"))) == ("drivers", "drv-split")


def test_a_removed_directory_is_placed_by_its_nearest_surviving_ancestor(repo):
    wt = _worktree(repo, repo / ".claude" / "worktrees" / "wt-3")
    _git("worktree", "remove", str(wt), cwd=repo)
    assert owners.place(str(wt / "a" / "b")) == ("rover", "")


def test_a_submodule_is_its_own_project(repo):
    lib = repo / "vendor" / "lib"
    lib.mkdir(parents=True)
    (repo / ".git" / "modules" / "lib").mkdir(parents=True)
    (lib / ".git").write_text("gitdir: ../../.git/modules/lib\n")
    assert owners.place(str(lib)) == ("lib", "")


def test_outside_git_the_project_is_the_directory_under_home(home, tmp_path):
    (home / "datasets" / "held-work").mkdir(parents=True)
    (home / ".cache" / "rover").mkdir(parents=True)
    assert owners.place(str(home / "datasets" / "held-work")) == ("datasets", "")
    assert owners.place(str(home / "gone" / "x")) == ("gone", "")
    for nothing in (home, home / ".cache" / "rover", tmp_path, ""):
        assert owners.place(str(nothing) if nothing else "") == ("", "")


def test_a_repository_at_home_owns_nothing_under_it(home):
    _git("init", "-q", cwd=home)
    (home / "notes").mkdir()
    assert owners.place(str(home / "notes")) == ("notes", "")
    assert owners.place(str(home)) == ("", "")


def test_a_jobs_directory_names_its_project_and_its_session_names_it(repo, home):
    wt = _worktree(repo, home / "wt" / "pr42")
    (home / "tools").mkdir()
    _git("init", "-q", cwd=home / "tools")
    (home / "datasets").mkdir()
    s = _session()
    assert owners.of(str(wt), {}, s) == owners.Owner("rover", "rover-30", "pr42")
    assert owners.of(str(home / "tools"), {}, s) == owners.Owner("tools", "rover-30", "")  # another repository's work
    assert owners.of(str(home / "datasets"), {}, _session(worktree="lidar")) == owners.Owner("rover", "rover-30", "lidar")
    assert owners.of(str(wt), {}) == owners.Owner("rover", "", "pr42")


def test_a_scratchpad_is_its_sessions_even_when_it_holds_a_repository(home, tmp_path):
    pad = tmp_path / "tmp" / "claude-1000" / "-home-u-rover" / SESSION_ID / "scratchpad" / "draft"
    pad.mkdir(parents=True)
    _git("init", "-q", cwd=pad)
    assert owners.of(str(pad), {10: _session()}) == owners.Owner("rover", "rover-30", "")
    assert owners.of(str(pad), {}) == owners.Owner("")  # its session has gone: nothing says whose it was


def test_a_process_is_its_nearest_sessions_and_its_directorys(repo, home):
    wt = _worktree(repo, home / "wt" / "pr42")
    with subprocess.Popen(["sleep", "30"], cwd=wt) as p:
        try:
            snap = procs.Snapshot.take()
            sessions = {os.getpid(): _session(os.getpid(), "agent-locks")}  # the test's process is its ancestor
            assert owners.owner_of(p.pid, snap, sessions) == owners.Owner("rover", "rover-30", "pr42")
            assert owners.owner_of(p.pid, snap, {}) == owners.Owner("rover", "", "pr42")
        finally:
            p.kill()


def _register(reg, pid, start, cwd, name="rover-30"):
    (reg / f"{pid}.json").write_text(json.dumps({"pid": pid, "sessionId": SESSION_ID, "cwd": str(cwd), "name": name,
                                                  "procStart": str(start), "status": "busy"}))


def test_a_process_finds_its_own_session_by_walking_up_its_ancestors(repo, tmp_path):
    reg = tmp_path / "sessions"
    reg.mkdir()
    me, parent = procs.Snapshot.take().get(os.getpid()), procs.Snapshot.take().get(os.getppid())
    with subprocess.Popen(["sleep", "30"]) as p:
        try:
            assert owners.own_session(p.pid, reg) is None
            _register(reg, parent.pid, parent.start_ticks, repo, "outer")
            _register(reg, me.pid, me.start_ticks + 1, repo)  # a pid since reused: not this process's
            assert owners.own_session(p.pid, reg).name == "outer"
            _register(reg, me.pid, me.start_ticks, repo)
            assert owners.own_session(p.pid, reg) == owners.Session(me.pid, "rover-30", SESSION_ID, "rover", "", "busy")
        finally:
            p.kill()


def test_a_session_is_placed_by_its_directory(repo, home, tmp_path):
    wt = _worktree(repo, home / "rover-docs")
    me = procs.Snapshot.take().get(os.getpid())
    reg = tmp_path / "sessions"
    reg.mkdir()
    (reg / f"{me.pid}.json").write_text(json.dumps({"pid": me.pid, "sessionId": SESSION_ID, "cwd": str(wt), "name": "rover-30",
                                                    "procStart": str(me.start_ticks), "status": "busy"}))
    (reg / "1.json").write_text(json.dumps({"pid": 1, "sessionId": "x", "cwd": str(wt), "procStart": "0"}))  # pid 1 started at another time
    snap = procs.Snapshot.take()
    assert owners.load_sessions(snap, reg) == {me.pid: owners.Session(me.pid, "rover-30", SESSION_ID, "rover", "rover-docs", "busy")}
