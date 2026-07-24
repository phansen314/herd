"""herd restart — the dead-resumable read, the direct launch (NOT spawn()), and the
revival semantics restart relies on.

The load-bearing decision restart makes is to launch `claude --resume <uuid>` DIRECTLY
and let the resumed session's own SessionStart hook revive the dead row — going through
spawn() would collide on the UNIQUE session_id. These tests pin both halves: that the
read offers the right rows, and that a resume upsert revives in place without an orphan.
"""
import pytest

from helpers import W, mk_session, mk_herd, SOCK, T0, T1, T2
from herd import cli
from herd.kitty.launch import build_launch_argv
from herd.spawn import SpawnSpec


# ── R_dead_resumable: R1_list inverted ───────────────────────────────────────
def test_dead_read_offers_only_resumable_dead_rows(fresh):
    c = fresh()
    live = mk_session(c, session_id="u-live", cwd="/a", stopped_at=None)      # live
    dead_no_uuid = mk_session(c, session_id=None, cwd="/b", stopped_at=T0)    # never adopted
    dead = mk_session(c, session_id="u-dead", cwd="/c", stopped_at=T1)        # THE candidate
    got = [r["id"] for r in c.execute(W["R_dead_resumable"])]
    assert got == [dead]
    assert live not in got and dead_no_uuid not in got


def test_dead_read_newest_death_first(fresh):
    c = fresh()
    older = mk_session(c, session_id="u-old", cwd="/a", stopped_at=T0)
    newer = mk_session(c, session_id="u-new", cwd="/b", stopped_at=T2)
    got = [r["id"] for r in c.execute(W["R_dead_resumable"])]
    assert got == [newer, older]        # stopped_at DESC — reboot's just-reaped float up


def test_dead_read_carries_resume_fields(fresh):
    c = fresh()
    pk = mk_session(c, session_id="u-x", cwd="/code/app", session_name="parser",
                    stopped_at=T1)
    mk_herd(c, pk, job_name="api")
    r = c.execute(W["R_dead_resumable"]).fetchone()
    assert r["session_id"] == "u-x" and r["cwd"] == "/code/app"
    assert r["session_name"] == "parser" and r["job_name"] == "api"


# ── the launch.py change: an empty job omits HERD_JOB entirely ───────────────
def test_argv_omits_herd_job_when_job_empty():
    spec = SpawnSpec(job="", cwd="/c", claude_args=["--resume", "u-x"])
    argv = build_launch_argv(spec, SOCK)
    assert "HERD_JOB=" not in " ".join(argv)          # no --var/--env HERD_JOB
    assert argv[-2:] == ["--resume", "u-x"]           # resume threaded verbatim


def test_argv_keeps_herd_job_when_job_present():
    spec = SpawnSpec(job="api", cwd="/c", claude_args=["--resume", "u-x"])
    argv = build_launch_argv(spec, SOCK)
    assert argv.count("HERD_JOB=api") == 2            # both --var and --env, as spawn()


# ── cmd_restart: guards + the spec it builds per pick ─────────────────────────
def _patch_pick(monkeypatch, rows):
    monkeypatch.setattr(cli, "_has_fzf", lambda: True)
    monkeypatch.setattr(cli, "_fzf_pick_multi", lambda rws, q: rows)
    launched = []
    monkeypatch.setattr(cli, "launch", lambda spec, sock: (launched.append((spec, sock)) or 7))
    monkeypatch.setenv("KITTY_LISTEN_ON", SOCK)
    return launched


def test_cmd_restart_needs_kitty(monkeypatch, fresh, capsys):
    monkeypatch.delenv("KITTY_LISTEN_ON", raising=False)
    assert cli.cmd_restart(fresh(), []) == 1
    assert "kitty" in capsys.readouterr().out.lower()


def test_cmd_restart_needs_fzf(monkeypatch, fresh):
    monkeypatch.setenv("KITTY_LISTEN_ON", SOCK)
    monkeypatch.setattr(cli, "_has_fzf", lambda: False)
    assert cli.cmd_restart(fresh(), []) == 2


def test_cmd_restart_no_dead_sessions(monkeypatch, fresh, capsys):
    _patch_pick(monkeypatch, [])
    c = fresh()
    mk_session(c, session_id="u-live", cwd="/a", stopped_at=None)   # only a live one
    assert cli.cmd_restart(c, []) == 0
    assert "no resumable" in capsys.readouterr().out.lower()


def test_cmd_restart_launches_resume_per_pick(monkeypatch, fresh):
    c = fresh()
    p1 = mk_session(c, session_id="u-1", cwd="/code/a", session_name="alpha", stopped_at=T1)
    p2 = mk_session(c, session_id="u-2", cwd="/code/b", stopped_at=T2)
    mk_herd(c, p1, job_name="alpha")            # p1 had a job; p2 did not
    rows = cli._dead(c)
    launched = _patch_pick(monkeypatch, rows)
    assert cli.cmd_restart(c, []) == 0
    specs = [s for s, _ in launched]
    assert {s.cwd for s in specs} == {"/code/a", "/code/b"}
    for s in specs:
        assert s.claude_args[0] == "--resume"
        assert s.claude_args[1] in ("u-1", "u-2")
    bycwd = {s.cwd: s for s in specs}
    assert bycwd["/code/a"].job == "alpha"      # job restamped via --env
    assert bycwd["/code/b"].job == ""           # no job -> HERD_JOB omitted


def test_cmd_restart_cancel_launches_nothing(monkeypatch, fresh):
    c = fresh()
    mk_session(c, session_id="u-1", cwd="/a", stopped_at=T1)
    launched = _patch_pick(monkeypatch, [])     # Esc -> empty pick list
    assert cli.cmd_restart(c, []) == 0
    assert launched == []


def test_cmd_restart_restores_stored_tab_title(monkeypatch, fresh):
    """The resumed tab is re-titled from the captured tab_title when present, and
    falls back to _name(r) (here the session_name) when it is NULL."""
    c = fresh()
    p1 = mk_session(c, session_id="u-1", cwd="/a", session_name="alpha", stopped_at=T1)
    p2 = mk_session(c, session_id="u-2", cwd="/b", session_name="beta", stopped_at=T2)
    mk_herd(c, p1)
    mk_herd(c, p2)
    c.execute("UPDATE herd_sessions SET tab_title='herd: refactor' WHERE session_pk=?", (p1,))
    # p2 has no tab_title (NULL) -> falls back to _name
    launched = _patch_pick(monkeypatch, cli._dead(c))
    assert cli.cmd_restart(c, []) == 0
    bycwd = {s.cwd: s for s, _ in launched}
    assert bycwd["/a"].title == "herd: refactor"   # stored title wins
    assert bycwd["/b"].title == "beta"             # NULL -> _name fallback


# ── the revival restart relies on: W2b upsert flips stopped_at, no orphan ─────
def test_resume_upsert_revives_in_place(fresh):
    """The reason restart launches direct instead of via spawn(): a resumed session's
    W2b_insert upserts on its UNIQUE session_id, reviving the SAME dead row (stopped_at
    -> NULL, fresh pid) rather than making a second one."""
    c = fresh()
    dead = mk_session(c, session_id="u-x", cwd="/code/app", stopped_at=T1)
    assert c.execute("SELECT stopped_at FROM sessions WHERE id=?", (dead,)).fetchone()[0]

    c.execute(W["W2b_insert"], {"session_id": "u-x", "cwd": "/code/app", "model": None,
                                "transcript": None, "pid": 4321, "now": T2})
    rows = c.execute("SELECT id, stopped_at, pid FROM sessions WHERE session_id='u-x'").fetchall()
    assert len(rows) == 1                        # ONE row — no orphan
    assert rows[0]["id"] == dead                 # the SAME row
    assert rows[0]["stopped_at"] is None         # revived
    assert rows[0]["pid"] == 4321                # with the resumed process's pid
