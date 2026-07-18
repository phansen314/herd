"""The hook contract: NOTHING MAY BLOCK CLAUDE. Every hook exits 0 under every
degradation we can induce — malformed payload, empty stdin, unreachable DB, broken
jq, broken sqlite3. DESIGN.md#the-hooks-hookssh states this as the loudest invariant in the
project; before this file nothing enforced it.

Also covers the two guards whose documented rationale had no test: bind()'s quote
escaping (DESIGN.md#write-paths-schemawritessql justifies the hand-rolled binder with an o'brien
example) and valid_sid()'s path-escape rejection.
"""
import json
import pathlib
import os
import subprocess

import pytest

from helpers import HOOKS

# statusline.sh is included deliberately: it is not a "hook" in settings.json terms,
# but it runs on the same ~1/sec path and a nonzero exit is just as user-visible.
ALL_HOOKS = ["session_start.sh", "stop.sh", "session_end.sh", "notification.sh",
             "post_tool_use.sh", "statusline.sh"]

GOOD = {"session_id": "abc123", "cwd": "/code/herd", "model": {"id": "opus"},
        "transcript_path": "/t.jsonl", "hook_event_name": "X"}


def _run(hook_env, script, stdin_text, env=None):
    """Raw-stdin runner. hook_env.run json.dumps()es its payload, so it cannot send
    malformed JSON — which is exactly the case under test."""
    e = dict(os.environ, HERD_DB=hook_env.path, HERD_RUNTIME=hook_env.runtime,
             HERD_ERRLOG=f"{hook_env.runtime}/err.log")
    if env:
        e.update(env)
    return subprocess.run(["bash", str(HOOKS / script)], input=stdin_text,
                          capture_output=True, text=True, env=e)


def _stub_bin(tmp_path, name):
    """A PATH dir that shadows `name` with a failing stub — simulates the binary
    being missing/broken without unsetting PATH (which would hide bash itself)."""
    d = tmp_path / f"stub-{name}"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\nexit 127\n")
    p.chmod(0o755)
    return {"PATH": f"{d}{os.pathsep}{os.environ['PATH']}"}


@pytest.mark.parametrize("script", ALL_HOOKS)
@pytest.mark.parametrize("stdin_text,label", [
    ("", "empty stdin"),
    ("not json at all", "malformed"),
    ("{", "truncated json"),
    ("null", "json null"),
    ("[]", "json array not object"),
    (json.dumps({}), "empty object"),
])
def test_hook_exits_zero_on_bad_payload(hook_env, script, stdin_text, label):
    assert _run(hook_env, script, stdin_text).returncode == 0, label


@pytest.mark.parametrize("script", ALL_HOOKS)
def test_hook_exits_zero_when_db_unreachable(hook_env, script):
    r = _run(hook_env, script, json.dumps(GOOD),
             env={"HERD_DB": "/nonexistent/dir/herd.db"})
    assert r.returncode == 0


@pytest.mark.parametrize("script", ALL_HOOKS)
def test_hook_exits_zero_when_jq_broken(hook_env, script, tmp_path):
    r = _run(hook_env, script, json.dumps(GOOD), env=_stub_bin(tmp_path, "jq"))
    assert r.returncode == 0


@pytest.mark.parametrize("script", ALL_HOOKS)
def test_hook_exits_zero_when_sqlite3_broken(hook_env, script, tmp_path):
    r = _run(hook_env, script, json.dumps(GOOD), env=_stub_bin(tmp_path, "sqlite3"))
    assert r.returncode == 0


def test_statusline_still_renders_without_a_db(hook_env):
    """Degrading to payload-only render is the documented behaviour — an empty
    statusline would read as "claude is broken" to the user."""
    r = _run(hook_env, "statusline.sh", json.dumps(
        {**GOOD, "session_name": "sess", "context_window": {"used_percentage": 5}}),
        env={"HERD_DB": "/nonexistent/dir/herd.db"})
    assert r.returncode == 0 and r.stdout.strip() != ""


# ── bind(): the reason the hand-rolled binder exists ─────────────────────────
def _bind(sql, params, extra_env=None):
    e = dict(os.environ, **{f"HERD_P_{k}": v for k, v in params.items()})
    if extra_env:
        e.update(extra_env)
    r = subprocess.run(["bash", "-c", f'. "{HOOKS}/common.sh"; bind {sql!r}'],
                       capture_output=True, text=True, env=e)
    return r.returncode, r.stdout


def test_bind_escapes_embedded_single_quote():
    """DESIGN.md justifies bind() over sqlite3's `.param set` with exactly this
    case. A naive binder emits ...'/tmp/o'brien'... and corrupts the statement."""
    rc, out = _bind("SELECT :cwd;", {"cwd": "/tmp/o'brien"})
    assert rc == 0
    assert "'/tmp/o''brien'" in out


def test_bind_escapes_sql_injection_attempt():
    rc, out = _bind("SELECT :x;", {"x": "'; DROP TABLE sessions; --"})
    assert rc == 0
    # the payload survives as ONE quoted literal — no bare statement separator
    assert "''; DROP TABLE sessions; --'" in out


def test_bind_empty_value_becomes_null_not_empty_string():
    """COALESCE(:x, col) throughout writes.sql depends on this: an absent payload
    field must be NULL (keep prior value), never '' (wipe it)."""
    rc, out = _bind("SELECT :x;", {"x": ""})
    assert rc == 0 and "NULL" in out and "''" not in out


def test_bind_fails_loudly_on_an_unbound_param():
    """Silent NULL-binding is the failure mode that motivated not using .param set."""
    rc, _ = _bind("SELECT :never_set_anywhere;", {})
    assert rc != 0


# ── valid_sid(): the path-escape guard ───────────────────────────────────────
def _valid_sid(sid):
    r = subprocess.run(["bash", "-c", f'. "{HOOKS}/common.sh"; valid_sid {sid!r}'],
                       capture_output=True, text=True, env=dict(os.environ))
    return r.returncode == 0


@pytest.mark.parametrize("sid", [
    "abc123", "07c6e17d-1a44-40d9-9e41-07d896ace781", "A-1",
])
def test_valid_sid_accepts_real_uuids(sid):
    assert _valid_sid(sid)


@pytest.mark.parametrize("sid", [
    "", "../../etc/passwd", "a/b", "a b", "a;rm -rf /", "a$(id)", "a`id`",
    "a'b", 'a"b', "a\nb", ".", "..", "a.b",
])
def test_valid_sid_rejects_path_escapes_and_shell_metacharacters(sid):
    """The SID becomes a cache filename under $HERD_RUNTIME. Anything that can
    traverse or inject must be refused before it reaches the path."""
    assert not _valid_sid(sid)


@pytest.mark.parametrize("script", ALL_HOOKS)
def test_a_broken_jq_is_logged_not_merely_survived(hook_env, script, tmp_path):
    """Exiting 0 is only half the contract. A hook that parses no session_id writes
    nothing and stays quiet — correct, the payload wasn't ours — and a MISSING jq
    produced the identical outcome: exit 0, nothing written, empty HERD_ERRLOG.
    That is the file README's troubleshooting sends you to first, so herd recorded
    nothing forever with no way to find out why."""
    r = _run(hook_env, script, json.dumps(GOOD), env=_stub_bin(tmp_path, "jq"))
    assert r.returncode == 0                          # still never blocks Claude
    log = pathlib.Path(hook_env.runtime, "err.log")
    assert log.exists(), f"{script} left no trace of a missing jq"
    assert "jq NOT FOUND" in log.read_text()


@pytest.mark.parametrize("script", ALL_HOOKS)
def test_a_payload_without_a_session_id_stays_quiet(hook_env, script):
    """The other side: not every payload is ours, and a hook that logged on every
    one would bury the signal it exists to provide."""
    _run(hook_env, script, json.dumps({"cwd": "/x"}))
    log = pathlib.Path(hook_env.runtime, "err.log")
    assert not log.exists() or "jq" not in log.read_text()


# ── the error log must stay bounded ─────────────────────────────────────────
def _log_via(hook_env, script, env=None):
    e = {"HERD_ERRLOG_MAX": "200"}
    e.update(env or {})
    return _run(hook_env, script, json.dumps(GOOD), env=e)


def test_the_errlog_rotates_instead_of_growing_without_bound(hook_env, tmp_path):
    """A persistent fault logs on EVERY hook fire — six scripts per prompt, plus a
    statusline ~1/sec/session. Unbounded, today's error ends up buried under weeks
    of history in the file troubleshooting tells you to read."""
    log = pathlib.Path(hook_env.runtime, "err.log")
    for _ in range(40):
        _log_via(hook_env, "stop.sh", env=_stub_bin(tmp_path, "jq"))
    assert log.exists()
    assert log.stat().st_size <= 400, "errlog grew past the cap"
    assert pathlib.Path(str(log) + ".1").exists(), "the previous window was dropped"


def test_rotation_can_be_disabled(hook_env, tmp_path):
    """0 means keep everything — someone mid-investigation may want the full run."""
    env = {"HERD_ERRLOG_MAX": "0"}
    env.update(_stub_bin(tmp_path, "jq"))
    for _ in range(20):
        _run(hook_env, "stop.sh", json.dumps(GOOD), env=env)
    assert not pathlib.Path(hook_env.runtime, "err.log.1").exists()
