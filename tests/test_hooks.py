"""N (47-57b) — the hooks end to end: the real bash scripts run against a temp DB.
Nothing but this exercises the bash + writes.sql seam."""
import os
import subprocess

import pytest

from helpers import W, T0, SOCK, HOOKS, mk_session, mk_herd, mk_attention


# ── _walk_claude: the ppid-walk logic, against synthetic ancestries ──────────
def _walk(start, table, want=None):
    env = dict(os.environ)
    if want is not None:
        env["HERD_CLAUDE_NAME"] = want
    return subprocess.run(["bash", "-c", f'. "{HOOKS}/common.sh"; _walk_claude "{start}"'],
                          input="\n".join(table), capture_output=True, text=True, env=env).stdout.strip()


_NESTED = ["100 200 bash", "200 300 sh", "300 400 claude", "400 500 sh", "500 600 claude", "600 1 kitty"]


@pytest.mark.parametrize("start,table,want,expect", [
    ("100", _NESTED, None, "300"),                                              # nearest ancestor
    ("100", ["100 200 bash", "200 300 sh", "300 400 /usr/bin/claude"], None, "300"),  # basenamed
    ("100", ["100 200 bash", "200 1 sh"], None, ""),                            # none on path
    ("100", ["100 200 bash", "200 300 sh", "300 400 node"], "node", "300"),     # HERD_CLAUDE_NAME
])
def test_walk_claude(start, table, want, expect):
    assert _walk(start, table, want) == expect


# ── 47. bash stmt() and python load_statements() agree ───────────────────────
def test_bash_and_python_extract_same(bash_stmt):
    norm = lambda s: " ".join(s.split())
    mismatch = [n for n in W if norm(bash_stmt(n)) != norm(W[n])]
    assert not mismatch, f"drifted: {mismatch}"


# ── 48/49. session_start: adopt vs insert ────────────────────────────────────
def test_session_start_adopts_reconciled_row(hook_env):
    c = hook_env.conn()
    pk = mk_session(c, cwd="/code/herd", status="unknown", status_source="reconcile")
    mk_herd(c, pk, job_name="api", created_at=T0, window_id=5)
    hook_env.run("session_start.sh",
                 {"session_id": "uuid-A", "cwd": "/code/herd", "model": "claude-opus-4-8",
                  "transcript_path": "/t.jsonl", "source": "startup", "hook_event_name": "SessionStart"},
                 {"KITTY_WINDOW_ID": "5", "KITTY_LISTEN_ON": SOCK})
    rows = c.execute("SELECT id,session_id,status FROM sessions").fetchall()
    job = c.execute("SELECT h.job_name FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk"
                    " WHERE s.session_id='uuid-A'").fetchone()
    # job still attached is the discriminator: W2b fallback would create a NEW row.
    assert len(rows) == 1 and rows[0]["id"] == pk and rows[0]["session_id"] == "uuid-A"
    assert rows[0]["status"] == "working" and job is not None and job["job_name"] == "api"


def test_session_start_falls_back_to_w2b_outside_kitty(hook_env):
    hook_env.run("session_start.sh",
                 {"session_id": "uuid-B", "cwd": "/x", "model": "claude-opus-4-8",
                  "transcript_path": "/t.jsonl", "source": "startup", "hook_event_name": "SessionStart"},
                 {"KITTY_WINDOW_ID": "", "KITTY_LISTEN_ON": ""})
    n = hook_env.conn().execute(
        "SELECT COUNT(*) FROM sessions WHERE session_id='uuid-B' AND status='working'").fetchone()[0]
    assert n == 1


# ── 50. the hot path: last_event_at advances; throttle suppresses a burst ────
def test_post_tool_use_advances_last_event(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="s1", last_event_at=T0, last_event_type="tool")
    hook_env.run("post_tool_use.sh",
                 {"session_id": "s1", "tool_name": "Bash", "tool_input": {}, "tool_response": "ok",
                  "hook_event_name": "PostToolUse"}, {"HERD_TOOL_THROTTLE": "0"})
    assert c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0] != T0


def test_post_tool_use_throttle_keeps_first_write(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="s2", last_event_at=T0, last_event_type="tool")
    fire = lambda: hook_env.run(
        "post_tool_use.sh",
        {"session_id": "s2", "tool_name": "Bash", "tool_input": {}, "tool_response": "ok",
         "hook_event_name": "PostToolUse"}, {"HERD_TOOL_THROTTLE": "60"})
    fire()
    first = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s2'").fetchone()[0]
    assert first != T0                       # the first fire wrote
    for _ in range(4):
        fire()
    # the next 4 fires are inside the throttle window -> they must NOT write, so
    # last_event_at never advances past the first fire's timestamp.
    assert c.execute("SELECT last_event_at FROM sessions WHERE session_id='s2'").fetchone()[0] == first


# ── 51/52/53. stop / notification / session_end ──────────────────────────────
def test_stop_sets_waiting_and_rearms(hook_env):
    c = hook_env.conn()
    pk = mk_session(c, session_id="s1", last_event_at=T0, last_event_type="tool")
    mk_attention(c, pk, attention_at=T0, ack_at=T0)
    hook_env.run("stop.sh", {"session_id": "s1", "stop_hook_active": False,
                             "last_assistant_message": "done", "hook_event_name": "Stop"})
    r = c.execute("SELECT status,last_event_type FROM sessions WHERE session_id='s1'").fetchone()
    att = c.execute("SELECT COUNT(*) FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()[0]
    assert r["status"] == "waiting" and r["last_event_type"] == "stop" and att == 0


def test_notification_filters_to_permission_prompt(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="s1")
    hook_env.run("notification.sh", {"session_id": "s1", "notification_type": "idle_prompt",
                                     "message": "waiting", "hook_event_name": "Notification"})
    after_idle = c.execute("SELECT status FROM sessions WHERE session_id='s1'").fetchone()[0]
    hook_env.run("notification.sh", {"session_id": "s1", "notification_type": "permission_prompt",
                                     "message": "allow?", "hook_event_name": "Notification"})
    after_perm = c.execute("SELECT status FROM sessions WHERE session_id='s1'").fetchone()[0]
    assert after_idle == "working" and after_perm == "needs_approval"


def test_session_end_stops_and_frees_handles(hook_env):
    c = hook_env.conn()
    pk = mk_session(c, session_id="s1")
    mk_herd(c, pk, job_name="api", created_at=T0, window_id=5)
    hook_env.run("session_end.sh", {"session_id": "s1", "reason": "prompt_input_exit",
                                    "hook_event_name": "SessionEnd"})
    r = c.execute("SELECT status,status_source,stopped_at FROM sessions WHERE session_id='s1'").fetchone()
    job_free = c.execute(W["R_job_live"], {"job": "api"}).fetchone() is None
    win_free = not c.execute("SELECT 1 FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk "
                             "WHERE h.window_id=5 AND s.stopped_at IS NULL").fetchone()
    assert r["status"] == "stopped" and r["stopped_at"] is not None
    assert r["status_source"] == "hook" and job_free and win_free


# ── 54. bind(): refuse unbound, never rescan ─────────────────────────────────
def test_bind_refuses_unbound_param():
    r = subprocess.run(
        ["bash", "-c", f'. "{HOOKS}/common.sh"; bind "UPDATE sessions SET cwd = :cwd WHERE session_id = :session_id;"'],
        capture_output=True, text=True, env=dict(os.environ, HERD_P_cwd="/a"))
    assert r.returncode != 0 and ":session_id" in r.stderr


def test_bind_does_not_rescan_substituted_values():
    r = subprocess.run(
        ["bash", "-c", f'. "{HOOKS}/common.sh"; bind "UPDATE sessions SET cwd = :cwd, updated_at = :now;"'],
        capture_output=True, text=True,
        env=dict(os.environ, HERD_P_cwd="/tmp/:now/x", HERD_P_now="T1"))
    assert "'/tmp/:now/x'" in r.stdout


# ── 55. stop re-arm routes through W6d_rearm_sid ──────────────────────────────
def test_stop_rearm_uses_canonical_statement(hook_env):
    c = hook_env.conn()
    pk = mk_session(c, session_id="s9")
    mk_attention(c, pk, attention_at=T0)
    hook_env.run("stop.sh", {"session_id": "s9", "stop_hook_active": False, "hook_event_name": "Stop"})
    assert c.execute("SELECT COUNT(*) FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()[0] == 0


# ── 57. run_tx / -bail atomicity ─────────────────────────────────────────────
def test_run_tx_aborts_on_unknown_statement(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="s1", last_event_at=T0)
    subprocess.run(
        ["bash", "-c",
         '. "$1/common.sh"; export HERD_P_session_id=s1 HERD_P_now=T9 HERD_P_status=working '
         'HERD_P_etype=tool; run_tx W4_event BOGUS_FK', "_", str(HOOKS.parent)],
        capture_output=True, text=True,
        env=dict(os.environ, HERD_DB=hook_env.path, HERD_RUNTIME=hook_env.runtime,
                 HERD_ERRLOG=f"{hook_env.runtime}/err.log"))
    assert c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0] == T0


def test_bail_rolls_back_committed_prefix(hook_env):
    c = hook_env.conn()
    mk_session(c, session_id="s1", last_event_at=T0)
    # a valid UPDATE then a statement that fails (FK: no session 99999). -bail must
    # stop before COMMIT so the UPDATE rolls back too.
    tx = ("BEGIN IMMEDIATE;\n"
          "UPDATE sessions SET last_event_at='T9' WHERE session_id='s1';\n"
          "INSERT INTO herd_attention(session_pk,attention_at) VALUES(99999,'T9');\n"
          "COMMIT;\n")
    subprocess.run(["bash", "-c", f'. "{HOOKS}/common.sh"; db', "_"], input=tx,
                   capture_output=True, text=True,
                   env=dict(os.environ, HERD_DB=hook_env.path, HERD_RUNTIME=hook_env.runtime,
                            HERD_ERRLOG=f"{hook_env.runtime}/err.log"))
    assert c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0] == T0
    assert c.execute("SELECT COUNT(*) FROM herd_attention").fetchone()[0] == 0


# ── session_start.sh: the third branch, and the pid capture ──────────────────
def test_session_start_in_kitty_without_a_placeholder_records_placement(hook_env):
    """W2 misses (no herd-spawned row for this window) but we ARE in kitty, so the
    W2b_insert + W2b_placement pair must run in one txn — a user-started `claude`
    becomes a first-class tracked session. This branch (session_start.sh:41) was
    never driven through bash; only the adopt and non-kitty paths were."""
    c = hook_env.conn()
    hook_env.run("session_start.sh",
                 {"session_id": "fresh-uuid", "cwd": "/code/herd", "model": "opus",
                  "transcript_path": "/t.jsonl"},
                 {"KITTY_WINDOW_ID": "77", "KITTY_LISTEN_ON": SOCK})
    r = c.execute("SELECT s.id, s.cwd, h.kitty_socket, h.window_id, h.source "
                  "FROM sessions s JOIN herd_sessions h ON h.session_pk = s.id "
                  "WHERE s.session_id='fresh-uuid'").fetchone()
    assert r is not None, "no placement row — the W2b pair did not run"
    assert (r["kitty_socket"], r["window_id"]) == (SOCK, 77)
    assert r["source"] == "hook"          # 'hook', not 'spawn' — herd didn't launch it


def test_session_start_captures_the_claude_pid(hook_env):
    """claude_pid() walks ancestors for a process named `claude`. In the suite the
    ancestor is pytest/bash, so HERD_P_pid was always empty and the pid-writing
    branch of the real bash never executed. HERD_CLAUDE_NAME retargets the walk."""
    c = hook_env.conn()
    hook_env.run("session_start.sh",
                 {"session_id": "pid-uuid", "cwd": "/code/herd", "model": "opus",
                  "transcript_path": "/t.jsonl"},
                 {"HERD_CLAUDE_NAME": "bash"})
    pid = c.execute("SELECT pid FROM sessions WHERE session_id='pid-uuid'").fetchone()[0]
    assert pid is not None and pid > 0


def test_session_start_leaves_pid_null_when_no_claude_ancestor(hook_env):
    """No match -> NULL, not 0 or a bogus pid: the reaper treats NULL as 'skip',
    and a wrong pid would let it reap a live session."""
    c = hook_env.conn()
    hook_env.run("session_start.sh",
                 {"session_id": "nopid-uuid", "cwd": "/code/herd", "model": "opus",
                  "transcript_path": "/t.jsonl"},
                 {"HERD_CLAUDE_NAME": "definitely-not-a-real-process-name"})
    pid = c.execute("SELECT pid FROM sessions WHERE session_id='nopid-uuid'").fetchone()[0]
    assert pid is None


# ── fork-reduction refactor: the merged paths must equal the paths they replaced ──
def _sh(script, env=None, stdin=""):
    e = dict(os.environ)
    if env:
        e.update(env)
    return subprocess.run(["bash", "-c", f'. "{HOOKS}/common.sh"; {script}'],
                          input=stdin, capture_output=True, text=True, env=e)


@pytest.mark.parametrize("name", sorted(W))
def test_stmt_bind_equals_stmt_then_bind(name):
    """run()/run_tx() extract-and-bind in ONE awk fork now. stmt() and bind() still
    exist as the separately-tested halves, so the merged path is a SECOND copy of
    the substitution rule unless the two are pinned together — which is exactly the
    drift this repo guards everywhere else.

    Bound with a full param environment so the comparison exercises real
    substitution rather than two identical 'unbound' outputs."""
    env = {f"HERD_P_{p}": f"v-{p}" for p in (
        "session_id", "now", "status", "etype", "pk", "cutoff", "boot_time", "job",
        "socket", "win", "cwd", "model", "sname", "ctx", "cost", "branch", "pid",
        "rl5", "rl5reset", "rl7", "rl7reset", "ctxsize", "ocwd", "ladd", "ldel",
        "tokin", "tokout", "ver", "gwt", "exc200", "ostyle", "apims", "id",
        "transcript", "source", "created_at", "verified_at", "herd_var")}
    merged = _sh(f'stmt_bind {name}', env)
    composed = _sh(f'bind "$(stmt {name})"', env)
    assert merged.stdout == composed.stdout, f"{name}: merged != composed"
    assert (merged.returncode == 0) == (composed.returncode == 0)


def test_stmt_bind_reports_unbound_like_bind_does():
    """An unbound param must still fail the statement rather than sending `:name`
    to sqlite as a literal."""
    r = _sh('stmt_bind W4_event', {"HERD_P_session_id": "s"})   # :now/:status unbound
    assert r.returncode != 0
    assert "unbound param" in r.stderr


def test_stmt_bind_cuts_on_the_raw_semicolon_not_a_bound_value():
    """The `;` that ends a statement is found on the RAW line. A bound VALUE may
    contain one — a cwd or a /rename can — and cutting there would ship a truncated
    statement to sqlite."""
    r = _sh('stmt_bind W4_event', {"HERD_P_session_id": "a;DROP TABLE sessions;--",
                                   "HERD_P_now": "n", "HERD_P_status": "working",
                                   "HERD_P_etype": "tool"})
    assert r.returncode == 0
    assert r.stdout.rstrip().endswith(";")
    assert "'a;DROP TABLE sessions;--'" in r.stdout    # quoted, inert, intact


def test_unknown_statement_is_empty_not_an_unbound_error():
    """run() distinguishes these by emptiness, so stmt_bind must return nothing
    (not an error line) for a name that does not exist."""
    r = _sh('stmt_bind W_NOPE_NOT_A_STATEMENT')
    assert r.stdout == ""


def test_read_input_slurps_stdin_without_a_fork():
    """Replaced `INPUT=$(cat)`. NOT $(</dev/stdin) — Claude's invocation leaves that
    empty, which is why the comment in session_start.sh warns about it."""
    payload = '{"a": 1, "b": "two words", "c": null}'
    r = _sh('read_input; printf "%s" "$INPUT"', stdin=payload)
    assert r.stdout == payload


def test_read_input_keeps_interior_whitespace_and_blank_lines():
    """IFS= matters: without it read strips leading/trailing whitespace."""
    payload = '  {"a":\n\n  1}  '
    r = _sh('read_input; printf "[%s]" "$INPUT"', stdin=payload)
    assert r.stdout == f"[{payload}]"


def test_now_pair_falls_back_when_date_has_no_percent_3n(tmp_path):
    """macOS/BSD date leaves %3N unexpanded. The old code paid a `date -u +%3N`
    probe at SOURCE time — every hook fire, ~1/sec/session — to learn this; now the
    real call detects it and latches. A regression here writes a last_event_at of
    '...:00.3NZ', which no consumer can parse."""
    fake = tmp_path / "date"
    fake.write_text('#!/bin/bash\nargs=(); for a in "$@"; do args+=("${a//%3N/3N}"); done\n'
                    'exec /usr/bin/date "${args[@]}"\n')
    fake.chmod(0o755)
    env = {"PATH": f"{tmp_path}:{os.environ['PATH']}"}
    r = _sh('now_pair; echo "$NOW_ISO|$NOW_EPOCH"', env)
    iso, _, epoch = r.stdout.strip().partition("|")
    assert iso.endswith(".000Z"), iso
    assert "3N" not in iso
    assert epoch.isdigit()


def test_now_pair_gives_millis_on_gnu_date():
    r = _sh('now_pair; echo "$NOW_ISO"')
    iso = r.stdout.strip()
    assert iso.endswith("Z") and iso[-5:-1].lstrip(".").isdigit(), iso


def test_db_leaves_no_errfile_behind(hook_env):
    """db() truncates one per-process errfile and an EXIT trap reaps it, instead of
    forking `rm` per call. The trap is the only thing deleting it now."""
    import pathlib
    r = hook_env.run("post_tool_use.sh", {"session_id": "s1"})
    assert r.returncode == 0
    debris = list(pathlib.Path(hook_env.runtime).glob("herd-db-err.*"))
    assert debris == [], f"errfile debris: {debris}"
