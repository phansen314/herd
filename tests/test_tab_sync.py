"""tab_sync.sh + W7_tab_title — the dedicated tier-2 enrichment hook that captures
the live kitty tab title. Two layers: the statement's SQL semantics (pure), and the
real bash hook run against a fake `kitten` (the fidelity the hook suite exists for).
"""
import os
import stat

from helpers import W, mk_session, mk_herd, SOCK, T0

# A realistic `kitten @ ls` tree: list of OS-windows -> tabs -> windows. Window 200
# lives in the "herd: refactor" tab; 101 in "first tab"; 300 in a second OS-window.
LS_FIXTURE = """[
  {"id":1,"tabs":[
    {"id":10,"title":"first tab","windows":[{"id":100},{"id":101}]},
    {"id":11,"title":"herd: refactor","windows":[{"id":200}]}
  ]},
  {"id":2,"tabs":[
    {"id":20,"title":"other os-window","windows":[{"id":300}]}
  ]}
]"""


# ── W7_tab_title (pure SQL) ───────────────────────────────────────────────────
def test_w7_updates_tab_title_by_session_id(fresh):
    c = fresh()
    pk = mk_session(c, session_id="u-x", cwd="/c")
    mk_herd(c, pk, kitty_socket=SOCK, window_id=7)
    c.execute(W["W7_tab_title"], {"tab_title": "herd: refactor", "session_id": "u-x"})
    got = c.execute("SELECT tab_title FROM herd_sessions WHERE session_pk=?", (pk,)).fetchone()[0]
    assert got == "herd: refactor"


def test_w7_noop_when_unchanged(fresh):
    c = fresh()
    pk = mk_session(c, session_id="u-x", cwd="/c")
    mk_herd(c, pk)
    c.execute(W["W7_tab_title"], {"tab_title": "T", "session_id": "u-x"})
    n = c.execute(W["W7_tab_title"], {"tab_title": "T", "session_id": "u-x"}).rowcount
    assert n == 0                                  # IS NOT guard suppresses the re-write


def test_w7_unknown_session_is_a_noop(fresh):
    c = fresh()
    n = c.execute(W["W7_tab_title"], {"tab_title": "T", "session_id": "nope"}).rowcount
    assert n == 0                                  # no herd row -> nothing, no crash


def test_w7_is_tier2_only_never_touches_sessions(fresh):
    """A session with no herd_sessions row (started outside kitty) must not gain one
    or mutate sessions — the write is tier-2 UPDATE only."""
    c = fresh()
    mk_session(c, session_id="u-x", cwd="/c")      # no mk_herd
    c.execute(W["W7_tab_title"], {"tab_title": "T", "session_id": "u-x"})
    assert c.execute("SELECT COUNT(*) FROM herd_sessions").fetchone()[0] == 0


# ── the real hook against a fake kitten (needs bash+jq+sqlite3; auto-skips) ────
def _fake_kitten(tmp_path, output):
    """A `kitten` on PATH that ignores its args and prints `output`. Returns a PATH
    value with its dir prepended (jq/sqlite3/timeout still resolve from the rest)."""
    d = tmp_path / "shim"
    d.mkdir(exist_ok=True)
    k = d / "kitten"
    k.write_text(f"#!/bin/bash\ncat <<'EOF'\n{output}\nEOF\n")
    k.chmod(k.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return f"{d}:{os.environ['PATH']}"


def _payload(sid):
    return {"session_id": sid, "cwd": "/c", "prompt": "hi",
            "hook_event_name": "UserPromptSubmit"}


def test_hook_captures_the_windows_tab_title(hook_env, tmp_path):
    c = hook_env.conn()
    pk = mk_session(c, session_id="s-tab", cwd="/c")
    mk_herd(c, pk, kitty_socket=SOCK, window_id=200)
    hook_env.run("tab_sync.sh", _payload("s-tab"),
                 {"KITTY_LISTEN_ON": SOCK, "KITTY_WINDOW_ID": "200",
                  "PATH": _fake_kitten(tmp_path, LS_FIXTURE)})
    got = c.execute("SELECT tab_title FROM herd_sessions WHERE session_pk=?", (pk,)).fetchone()[0]
    assert got == "herd: refactor"                 # window 200's tab, across the nesting


def test_hook_outside_kitty_writes_nothing(hook_env, tmp_path):
    c = hook_env.conn()
    pk = mk_session(c, session_id="s-tab", cwd="/c")
    mk_herd(c, pk, kitty_socket=SOCK, window_id=200)
    r = hook_env.run("tab_sync.sh", _payload("s-tab"),
                     {"KITTY_LISTEN_ON": "", "KITTY_WINDOW_ID": "",
                      "PATH": _fake_kitten(tmp_path, LS_FIXTURE)})
    assert r.returncode == 0                        # never blocks the prompt
    got = c.execute("SELECT tab_title FROM herd_sessions WHERE session_pk=?", (pk,)).fetchone()[0]
    assert got is None                              # no socket -> no capture


def test_hook_noops_when_window_absent_from_ls(hook_env, tmp_path):
    """A window id kitty doesn't report (racing tab close) yields an empty title, and
    the hook skips the write rather than blanking the stored one."""
    c = hook_env.conn()
    pk = mk_session(c, session_id="s-tab", cwd="/c")
    mk_herd(c, pk, kitty_socket=SOCK, window_id=999)
    c.execute("UPDATE herd_sessions SET tab_title='kept' WHERE session_pk=?", (pk,))
    hook_env.run("tab_sync.sh", _payload("s-tab"),
                 {"KITTY_LISTEN_ON": SOCK, "KITTY_WINDOW_ID": "999",
                  "PATH": _fake_kitten(tmp_path, LS_FIXTURE)})
    got = c.execute("SELECT tab_title FROM herd_sessions WHERE session_pk=?", (pk,)).fetchone()[0]
    assert got == "kept"                            # empty title -> no clobber


def test_hook_rejects_non_numeric_window_id(hook_env, tmp_path):
    """KITTY_WINDOW_ID goes to jq as --argjson (a number); a non-numeric value must
    bail before the fork, not crash jq."""
    c = hook_env.conn()
    pk = mk_session(c, session_id="s-tab", cwd="/c")
    mk_herd(c, pk, kitty_socket=SOCK, window_id=200)
    r = hook_env.run("tab_sync.sh", _payload("s-tab"),
                     {"KITTY_LISTEN_ON": SOCK, "KITTY_WINDOW_ID": "not-a-number",
                      "PATH": _fake_kitten(tmp_path, LS_FIXTURE)})
    assert r.returncode == 0
    got = c.execute("SELECT tab_title FROM herd_sessions WHERE session_pk=?", (pk,)).fetchone()[0]
    assert got is None
