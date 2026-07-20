"""B / 45 / 46 — the schema applies, and the tier thesis executed: tier 1 stands
alone, tier 2 is inert without it."""
import sqlite3

from helpers import CORE, HERD, T0, T1


def test_four_tables(fresh):
    c = fresh()
    tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    assert tabs == ['herd_attention', 'herd_sessions', 'sessions']


def test_wal_mode(fresh):
    assert fresh().execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_auto_vacuum_incremental(fresh):
    assert fresh().execute("PRAGMA auto_vacuum").fetchone()[0] == 2


def test_idempotent_reapply(fresh):
    c = fresh()
    c.executescript(CORE)
    c.executescript(HERD)   # must not raise (CREATE ... IF NOT EXISTS)


def test_no_render_only_kitty_columns(fresh):
    """os_window_id/tab_id/window_title were render-only (kitten @ ls) — their
    removal is what took kitty off the write path. Block reintroduction."""
    cols = {r[1] for r in fresh().execute("PRAGMA table_info(herd_sessions)")}
    assert not ({"os_window_id", "tab_id", "window_title"} & cols)


def test_tier1_applies_standalone(fresh):
    """45 — a herd-less install is a working install."""
    c = fresh(tier2=False)
    t = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    assert t == ['sessions']
    pk = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",
                   (T0, T0)).lastrowid
    c.execute("UPDATE sessions SET stopped_at=? WHERE id=?", (T1, pk))   # no trigger to fire


def test_tier2_applies_standalone_but_inert(tmp_path):
    """46 — herd.sql creates its tables alone (SQLite doesn't validate FK parents
    at CREATE), but every row dangles off sessions, so tier 2 does nothing alone."""
    c2 = sqlite3.connect(str(tmp_path / "g.db"))
    c2.isolation_level = None
    c2.executescript(HERD)
    c2.execute("PRAGMA foreign_keys=ON")
    with_parent_missing = False
    try:
        c2.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,source,verified_at)"
                   " VALUES(1,'k','spawn',?)", (T0,))
    except sqlite3.OperationalError:
        with_parent_missing = True
    assert with_parent_missing
    c2.close()


def test_a_db_path_with_uri_metacharacters_opens_the_right_file(tmp_path):
    """The path goes into a file: URI, so `?` starts a query and `#` a fragment —
    unescaped, HERD_DB containing either opened the wrong file or failed
    obscurely. Legal in a filename, so reachable via HERD_DB."""
    from herd.db import connect, apply_schema
    weird = tmp_path / "herd?v=2#tmp.db"
    # create=True: connect() no longer conjures a missing database (that made a
    # typo in HERD_DB an empty file and a permanent failure loop), so a test that
    # brings one into existence has to say so, like the installer does.
    c = connect(str(weird), create=True); apply_schema(c)
    c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at) "
              "VALUES('s','/x','working','t','t')")
    c.close()
    assert weird.exists(), "wrote to a different path than requested"
    ro = connect(str(weird), readonly=True)
    assert ro.execute("SELECT session_id FROM sessions").fetchone()[0] == "s"
    ro.close()
