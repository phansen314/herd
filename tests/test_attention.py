"""S — the attention tick (daemon.py): the derived silence rule, arm/disarm edge
handling, and the HERD_ATTENTION core/herd layer gate."""
import datetime as dt

import pytest

from herd.daemon import (needs_attention, attention_tick, _attention_enabled,
                         run as daemon_run)

from helpers import T0, T1, T2, T0_10, T0_20, T0_240, mk_session, mk_attention


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
