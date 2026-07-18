"""herd spawn — build the launch argv, and record the W1 placeholder (guards run
before any launch). IO injected exactly like test_focus_cli.py."""
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
