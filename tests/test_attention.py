"""S — the attention tick (daemon.py): the derived silence rule, arm/disarm edge
handling, and the HERD_ATTENTION core/herd layer gate."""
import datetime as dt
import sqlite3

import pytest

from herd import cli, daemon
from herd.daemon import (needs_attention, attention_tick, _attention_enabled,
                         run as daemon_run)

from helpers import (T0, T1, T2, T0_10, T0_20, T0_240, W, cells, mk_session,
                     mk_attention)


@pytest.mark.parametrize("status,le,now,expect", [
    ("waiting", T0, T1, True),                 # trips after 30s grace
    ("waiting", T0, T0, False),
    ("needs_approval", T0, T0_20, True),        # ~15s grace
    ("needs_approval", T0, T0_10, False),
    ("working", T0, T1, True),                  # 'stuck' after ~5min
    ("working", T0, T0_240, False),
    ("stopped", T0, T2, False),                 # never page-worthy
    ("unknown", T0, T2, False),
    ("working", None, T2, False),               # NULL last_event never trips
])
def test_needs_attention(status, le, now, expect):
    assert needs_attention(status, le, now) is expect


def _att(c, pk):
    r = c.execute("SELECT attention_at FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()
    return r["attention_at"] if r else None


def test_tick_arms_past_threshold_not_fresh(fresh):
    c = fresh()
    w = mk_session(c, session_id="w", status="waiting", last_event_at=T0, last_event_type="stop")
    f = mk_session(c, session_id="f", status="waiting", last_event_at=T1, last_event_type="stop",
                   started_at=T1, updated_at=T1)
    armed, dis = attention_tick(c, T1)
    assert armed == 1 and dis == 0 and _att(c, w) == T1 and _att(c, f) is None


def test_tick_preserves_edge(fresh):
    c = fresh()
    w = mk_session(c, session_id="w", status="waiting", last_event_at=T0, last_event_type="stop")
    attention_tick(c, T1)
    c.execute("UPDATE sessions SET updated_at=? WHERE id=?", (T2, w))
    attention_tick(c, T2)   # still waiting on the same last_event_at
    assert _att(c, w) == T1


def test_tick_disarms_when_silence_clears(fresh):
    c = fresh()
    d = mk_session(c, session_id="d", status="working", last_event_at=T1, last_event_type="tool",
                   started_at=T1, updated_at=T1)
    mk_attention(c, d, attention_at=T0)   # was armed
    armed, dis = attention_tick(c, T1)
    assert dis == 1 and armed == 0 and _att(c, d) is None


def test_tick_ignores_stopped(fresh):
    c = fresh()
    s = mk_session(c, session_id="s", status="waiting", last_event_at=T0, last_event_type="stop",
                   stopped_at=T1)
    armed, _ = attention_tick(c, T2)
    assert armed == 0 and _att(c, s) is None


# ── ack: a jump silences the row; ack_at restarts the same timer ─────────────
# The mark is rendered by cli, the timer is run by the daemon, and the bug this
# guards against was the two disagreeing — so these drive both halves.
T1_10 = "2026-07-15T10:05:10.000Z"   # ack + 10s: inside the 30s waiting threshold
T1_40 = "2026-07-15T10:05:40.000Z"   # ack + 40s: past it


def _waiting(c):
    """A session that has been silent long enough to arm at T1."""
    return mk_session(c, session_id="w", status="waiting",
                      last_event_at=T0, last_event_type="stop")


def _ack(c, pk, now):
    c.execute(W["W6c_ack"], {"pk": pk, "now": now, "focus_started_at": now})


def _row(c, pk):
    return c.execute("SELECT attention_at, ack_at FROM herd_attention WHERE session_pk=?",
                     (pk,)).fetchone()


def _marks(c):
    """The rendered mark column, straight through _line — the real read path. Every
    glyph is a single codepoint (rendering two cells); MARK_NONE is two spaces."""
    out = []
    for r in cli._live(c):
        line = cli._line(r)
        out.append(cli.MARK_NONE if line.startswith(cli.MARK_NONE) else line[0])
    return out


WAITING_MARK = cli.ATTENTION_MARKS["waiting"]


def test_ack_silences_the_render_but_keeps_the_row(fresh):
    c = fresh()
    w = _waiting(c)
    attention_tick(c, T1)
    assert _marks(c) == [WAITING_MARK]
    _ack(c, w, T1)
    assert _marks(c) == [cli.MARK_NONE]           # quiet
    assert _row(c, w)["ack_at"] == T1            # but still armed, still recorded


def test_acked_row_does_not_flap(fresh):
    """The trap: W6d is a whole-row DELETE, so disarming an acked row would drop
    ack_at and the next tick would re-arm from the old last_event_at, every tick."""
    c = fresh()
    w = _waiting(c)
    attention_tick(c, T1)
    _ack(c, w, T1)
    for _ in range(3):
        armed, dis = attention_tick(c, T1_10)
        assert (armed, dis) == (0, 0)
    r = _row(c, w)
    assert (r["attention_at"], r["ack_at"]) == (T1, T1)
    assert _marks(c) == [cli.MARK_NONE]


def test_ack_timer_renotifies(fresh):
    c = fresh()
    w = _waiting(c)
    attention_tick(c, T1)
    _ack(c, w, T1)
    armed, dis = attention_tick(c, T1_40)        # ack's own silence ran out
    assert (armed, dis) == (0, 1) and _row(c, w) is None
    armed, dis = attention_tick(c, T1_40)        # next tick re-arms fresh
    assert (armed, dis) == (1, 0)
    r = _row(c, w)
    assert (r["attention_at"], r["ack_at"]) == (T1_40, None)
    assert _marks(c) == [WAITING_MARK]            # and it speaks up again


def test_activity_clears_an_acked_row(fresh):
    """Real work still wins over the ack timer, via the existing disarm branch."""
    c = fresh()
    w = _waiting(c)
    attention_tick(c, T1)
    _ack(c, w, T1)
    c.execute("UPDATE sessions SET status='working', last_event_at=? WHERE id=?", (T1_40, w))
    armed, dis = attention_tick(c, T1_40)
    assert (armed, dis) == (0, 1) and _row(c, w) is None


# ── the mark says WHICH kind of attention ────────────────────────────────────
# The distinction is the whole point: Claude's bell already covers `waiting` and
# `needs_approval` (it ends a turn / raises a prompt), so the stuck mark is the only
# one that reports something nothing else can tell you. See DECISIONS.md#pager.
@pytest.mark.parametrize("status,expect", [
    ("waiting", "🙋"),
    ("needs_approval", "🔐"),
    ("working", "🥱"),          # silently stuck — no bell, no tab flag, nothing
])
def test_mark_distinguishes_the_page_worthy_statuses(fresh, status, expect):
    c = fresh()
    mk_session(c, session_id="s", status=status, last_event_at=T0, last_event_type="stop")
    attention_tick(c, T1)
    assert _marks(c) == [expect]
    assert cli.ATTENTION_MARKS[status] == expect, "glyph moved without updating this test"


def test_mark_falls_back_on_an_armed_row_with_an_unexpected_status(fresh):
    """status is CHECK-constrained to five values but only three are page-worthy, and
    a reconcile can flip an armed row to 'unknown' before the next tick disarms it.
    The picker must render that, not raise."""
    c = fresh()
    pk = mk_session(c, session_id="s", status="waiting", last_event_at=T0, last_event_type="stop")
    attention_tick(c, T1)
    c.execute("UPDATE sessions SET status='unknown' WHERE id=?", (pk,))
    assert _marks(c) == [cli.MARK_UNKNOWN]


def test_every_row_reserves_the_same_mark_width(fresh):
    """Quiet and marked rows must start their '#id' at the same column."""
    c = fresh()
    mk_session(c, session_id="a", status="waiting", last_event_at=T0, last_event_type="stop")
    mk_session(c, session_id="b", status="working", last_event_at=T1, last_event_type="tool",
               started_at=T1, updated_at=T1)
    attention_tick(c, T1)
    cols = {cells(cli._line(r).split("#", 1)[0]) for r in cli._live(c)}
    assert cols == {3}, f"mark column (glyph + gutter) is not uniform: {cols}"


def test_preview_names_the_reason_not_just_attention(fresh):
    """The pane has room for words; 'stuck' and 'waiting for you' are different facts."""
    c = fresh()
    mk_session(c, session_id="s", status="working", last_event_at=T0, last_event_type="tool")
    attention_tick(c, T1)
    text = cli._preview_text(next(iter(cli._live(c))))
    assert "🥱 stuck — no activity since" in text, text


# ── the statement-level lifecycle: arm (edge-preserving), ack, re-arm ────────
# The tests above drive the daemon's tick; these drive W6a/W6c/W6d directly, so a
# statement can't rot behind a tick that stopped calling it.
def test_statement_lifecycle(fresh):
    c = fresh()
    pk = mk_session(c, session_id="s1", status="waiting", last_event_at=T0, last_event_type="stop")

    c.execute(W["W6a_arm"], {"pk": pk, "now": T1})
    c.execute(W["W6a_arm"], {"pk": pk, "now": T2})   # tick again — must not move the edge
    assert c.execute("SELECT attention_at FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()[0] == T1

    c.execute(W["W6c_ack"], {"now": T2, "pk": pk, "focus_started_at": T2})
    r = c.execute("SELECT attention_at,ack_at FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()
    assert tuple(r) == (T1, T2)

    c.execute(W["W6d_rearm"], {"pk": pk})
    assert c.execute("SELECT COUNT(*) FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()[0] == 0

    c.execute(W["W6a_arm"], {"pk": pk, "now": T2})   # rule can trip fresh after re-arm
    assert c.execute("SELECT attention_at FROM herd_attention WHERE session_pk=?", (pk,)).fetchone()[0] == T2


# ── the attention_at <= focus_started_at race guard ──────────────────────────
# Both production call sites pass focus_started_at == now == the attention time, so
# only the positive case ever ran: flipping `<=` to `>=` left the suite green.
def _ack_returning(c, pk, focus_started_at, now):
    c.execute(W["W6c_ack"], {"pk": pk, "now": now, "focus_started_at": focus_started_at})
    return c.execute("SELECT ack_at FROM herd_attention WHERE session_pk=?",
                     (pk,)).fetchone()[0]


def test_ack_clears_attention_raised_before_the_jump_started(fresh):
    c = fresh()
    pk = mk_session(c, session_id="s1", status="waiting")
    c.execute(W["W6a_arm"], {"pk": pk, "now": T0})       # raised first
    assert _ack_returning(c, pk, focus_started_at=T0_10, now=T0_20) == T0_20


def test_ack_does_not_clear_attention_raised_mid_jump(fresh):
    """The whole point of the guard: a jump takes real time (kitty round-trip). An
    attention raised AFTER the jump began has not been seen by the user, so acking
    it would silently swallow a genuine notification."""
    c = fresh()
    pk = mk_session(c, session_id="s1", status="waiting")
    c.execute(W["W6a_arm"], {"pk": pk, "now": T0_20})    # raised DURING the jump
    assert _ack_returning(c, pk, focus_started_at=T0_10, now=T1) is None


def test_ack_is_idempotent(fresh):
    """ack_at IS NULL keeps a second focus from overwriting the first ack time."""
    c = fresh()
    pk = mk_session(c, session_id="s1", status="waiting")
    c.execute(W["W6a_arm"], {"pk": pk, "now": T0})
    first = _ack_returning(c, pk, focus_started_at=T0_10, now=T0_10)
    assert _ack_returning(c, pk, focus_started_at=T1, now=T2) == first


@pytest.mark.parametrize("val,expect", [(None, True), ("0", False), ("off", False), ("1", True)])
def test_attention_enabled_gate(monkeypatch, val, expect):
    if val is None:
        monkeypatch.delenv("HERD_ATTENTION", raising=False)
    else:
        monkeypatch.setenv("HERD_ATTENTION", val)
    assert _attention_enabled() is expect


def _db_path(c):
    return next(r[2] for r in c.execute("PRAGMA database_list") if r[1] == "main")


def test_run_gates_attention_end_to_end(fresh):
    """core-only writes zero herd_attention; herd mode arms a waiting session.
    started_at=now survives the boot sweep; last_event 5min ago trips the rule."""
    now = dt.datetime.now(dt.timezone.utc)
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    c = fresh()
    mk_session(c, session_id="wf", status="waiting",
               last_event_at=iso(now - dt.timedelta(seconds=300)), last_event_type="stop",
               started_at=iso(now), updated_at=iso(now - dt.timedelta(seconds=300)))
    dbp = _db_path(c)
    daemon_run(db_path=dbp, once=True, attend=False)
    assert c.execute("SELECT COUNT(*) FROM herd_attention").fetchone()[0] == 0
    daemon_run(db_path=dbp, once=True, attend=True)
    assert c.execute("SELECT COUNT(*) FROM herd_attention").fetchone()[0] == 1


# ── orphaned attention rows ─────────────────────────────────────────────────
def test_attention_for_a_dead_session_is_reclaimed(fresh):
    """Death is an UPDATE (W3d_reap, W4_end), never a DELETE, so ON DELETE CASCADE
    never fires — and attention_tick only visits live rows, so it cannot see the
    orphan it would need to clean. One row leaks per session that ever needed you
    and then died: invisible to every read, and unbounded."""
    c = fresh()
    dead = mk_session(c, session_id="dead", status="waiting", last_event_at=T0)
    live = mk_session(c, session_id="live", status="waiting", last_event_at=T0)
    mk_attention(c, dead, attention_at=T0)
    mk_attention(c, live, attention_at=T0)
    c.execute("UPDATE sessions SET stopped_at=? WHERE id=?", (T1, dead))

    assert daemon.attention_tick(c, T2) is not None       # the tick cannot reach it
    assert c.execute("SELECT COUNT(*) FROM herd_attention "
                     "WHERE session_pk=?", (dead,)).fetchone()[0] == 1

    assert daemon.sweep_dead_attention(c) == 1
    assert c.execute("SELECT COUNT(*) FROM herd_attention "
                     "WHERE session_pk=?", (dead,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM herd_attention "
                     "WHERE session_pk=?", (live,)).fetchone()[0] == 1   # untouched


def test_the_sweep_runs_on_the_tick(fresh, tmp_path):
    """It has to be wired in, not merely available."""
    c = fresh(name="att.db")
    dead = mk_session(c, session_id="d", status="waiting", last_event_at=T0)
    mk_attention(c, dead, attention_at=T0)
    c.execute("UPDATE sessions SET stopped_at=? WHERE id=?", (T1, dead))
    c.close()
    daemon.run(interval=0, db_path=str(tmp_path / "att.db"), once=True, attend=True)
    c = sqlite3.connect(str(tmp_path / "att.db"))
    assert c.execute("SELECT COUNT(*) FROM herd_attention").fetchone()[0] == 0
