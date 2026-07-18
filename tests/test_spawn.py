"""herd spawn — build the launch argv, and record the W1 placeholder (guards run
before any launch). IO injected exactly like test_focus_cli.py."""
import sqlite3

import pytest

from herd import cli
from herd.kitty.launch import build_launch_argv
from herd.spawn import SpawnSpec, spawn, valid_job

from helpers import T0, SOCK, mk_session, mk_herd, job_holder


def _spec(job="api", cwd="/code/app", launch_type="tab", prompt=None, claude_args=None):
    return SpawnSpec(job=job, cwd=cwd, launch_type=launch_type, prompt=prompt,
                     claude_args=claude_args or [])


# ── build_launch_argv (pure) ─────────────────────────────────────────────────
def test_argv_tab_shape():
    argv = build_launch_argv(_spec(), SOCK)
    assert argv[:5] == ["kitten", "@", "--to", SOCK, "launch"]
    assert argv[argv.index("--type") + 1] == "tab"
    assert argv[argv.index("--cwd") + 1] == "/code/app"
    assert argv[argv.index("--tab-title") + 1] == "api"
    assert "HERD_JOB=api" in argv
    assert "claude" in argv


def test_argv_pane_maps_to_kitty_window():
    argv = build_launch_argv(_spec(launch_type="pane"), SOCK)
    assert argv[argv.index("--type") + 1] == "window"


def test_argv_threads_claude_args_then_prompt_last():
    argv = build_launch_argv(_spec(prompt="fix parser", claude_args=["--model", "opus"]), SOCK)
    i = argv.index("claude")
    assert argv[i + 1:] == ["--model", "opus", "fix parser"]   # args, then prompt last


# ── spawn (executor) ─────────────────────────────────────────────────────────
def test_spawn_records_placeholder(fresh):
    c = fresh()
    calls = []
    ok, msg, pk = spawn(c, _spec(job="api", cwd="/code/app"), SOCK, T0,
                        launch_fn=lambda spec, sock: (calls.append(sock) or 42))
    assert ok and pk is not None
    row = c.execute(
        "SELECT s.cwd, s.status, s.status_source, h.job_name, h.herd_var, h.source, "
        "h.kitty_socket, h.window_id FROM sessions s "
        "JOIN herd_sessions h ON h.session_pk=s.id WHERE s.id=?", (pk,)).fetchone()
    assert row["cwd"] == "/code/app" and row["status"] == "unknown"
    assert (row["job_name"], row["herd_var"], row["source"]) == ("api", "api", "spawn")
    assert row["kitty_socket"] == SOCK and row["window_id"] == 42
    assert job_holder(c, "api") == pk          # R_job_live now sees the live holder
    assert calls == [SOCK]                      # launched exactly once


def test_spawn_refuses_taken_job_without_launching(fresh):
    c = fresh()
    pk = mk_session(c, cwd="/x")
    mk_herd(c, pk, job_name="api", window_id=7)
    launched = []
    ok, msg, _ = spawn(c, _spec(job="api"), SOCK, T0,
                       launch_fn=lambda s, k: (launched.append(1) or 99))
    assert not ok and "already holds" in msg
    assert launched == []                       # refused before any launch


def test_spawn_refuses_outside_kitty(fresh):
    c = fresh()
    launched = []
    ok, msg, _ = spawn(c, _spec(), None, T0, launch_fn=lambda s, k: (launched.append(1) or 1))
    assert not ok and "kitty" in msg.lower() and launched == []


def test_spawn_launch_failure_writes_nothing(fresh):
    c = fresh()
    ok, _, _ = spawn(c, _spec(), SOCK, T0, launch_fn=lambda s, k: None)
    assert not ok and c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_spawn_rejects_bad_job_before_launch(fresh):
    c = fresh()
    launched = []
    ok, _, _ = spawn(c, _spec(job="bad name"), SOCK, T0,
                     launch_fn=lambda s, k: (launched.append(1) or 1))
    assert not ok and launched == []


def test_spawn_degrades_when_the_write_lock_is_held(tmp_path, fresh):
    """BEGIN IMMEDIATE is the statement most likely to fail — a concurrent writer
    holds the lock past busy_timeout. The handler must not ROLLBACK a transaction
    that never opened: that raises out of spawn() and crashes the CLI it protects."""
    c = fresh(name="lock.db")
    c.execute("PRAGMA busy_timeout=50")          # fail fast instead of waiting 3s
    other = sqlite3.connect(str(tmp_path / "lock.db"), isolation_level=None)
    other.execute("BEGIN IMMEDIATE")             # hold the write lock
    launched = []
    try:
        ok, msg, pk = spawn(c, _spec(job="api"), SOCK, T0,
                            launch_fn=lambda s, k: (launched.append(1) or 99))
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert (ok, pk) == (False, None) and "could not reserve" in msg
    assert launched == []                        # never launched on the reserve path


def test_spawn_still_works_after_a_contended_failure(fresh):
    """The degraded path must leave the connection usable, not wedged mid-txn."""
    c = fresh(name="lock2.db")
    ok, _, pk = spawn(c, _spec(job="api"), SOCK, T0, launch_fn=lambda s, k: 99)
    assert ok and pk is not None and not c.in_transaction


@pytest.mark.parametrize("job,ok", [
    ("api", True), ("api-refactor_2.0", True),
    ("", False), ("bad name", False), ("a/b", False), ("x;y", False),
])
def test_valid_job(job, ok):
    assert valid_job(job) is ok


# ── cli arg splitting ────────────────────────────────────────────────────────
def test_split_dashdash():
    assert cli._split_dashdash(["api", "--type", "pane", "--", "--model", "opus"]) == \
        (["api", "--type", "pane"], ["--model", "opus"])
    assert cli._split_dashdash(["api"]) == (["api"], [])


# ── cmd_spawn: --type / --tab / --pane shorthand ─────────────────────────────
def _capture_spawn(monkeypatch):
    seen = {}

    def fake_spawn(conn, spec, socket, now, **kw):
        seen["spec"], seen["socket"] = spec, socket
        return True, "ok", 1

    monkeypatch.setattr(cli, "spawn", fake_spawn)
    monkeypatch.setenv("KITTY_LISTEN_ON", SOCK)
    return seen


@pytest.mark.parametrize("argv,want", [
    (["api"], "tab"),                  # default
    (["api", "--tab"], "tab"),
    (["api", "--pane"], "pane"),
    (["api", "--type", "pane"], "pane"),
])
def test_cmd_spawn_launch_type(monkeypatch, fresh, argv, want):
    seen = _capture_spawn(monkeypatch)
    assert cli.cmd_spawn(fresh(), argv) == 0
    assert seen["spec"].launch_type == want and seen["socket"] == SOCK


def test_cmd_spawn_tab_and_pane_conflict(monkeypatch, fresh):
    _capture_spawn(monkeypatch)
    assert cli.cmd_spawn(fresh(), ["api", "--tab", "--pane"]) == 2   # usage error


def test_a_raising_launcher_frees_the_job_name(fresh):
    """subprocess.run raises FileNotFoundError when `kitten` is not on PATH. Letting
    that propagate skipped the abort and left a pid-NULL reservation the reaper's
    pid predicate never revisits — R_job_live counted it live, so the name was
    burned until the next boot sweep."""
    c = fresh()

    def boom(spec, sock):
        raise FileNotFoundError(2, "No such file or directory", "kitten")

    ok, msg, pk = spawn(c, _spec(job="api"), SOCK, T0, launch_fn=boom)
    assert not ok and pk is None
    assert "kitten" in msg                       # the real cause reaches the user
    assert c.execute("SELECT COUNT(*) n FROM sessions").fetchone()["n"] == 0
    assert job_holder(c, "api") is None
    ok2, _, _ = spawn(c, _spec(job="api"), SOCK, T0, launch_fn=lambda s, k: 42)
    assert ok2                                   # the name is immediately reusable
