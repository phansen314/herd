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


# ── upgrading an EXISTING database ──────────────────────────────────────────
# apply_schema is idempotent but is NOT a migration: `CREATE TABLE IF NOT EXISTS`
# on a table that already exists is a no-op no matter how its definition changed.
# So a user who installed before a column was added kept the old table forever —
# re-running the installer reported success, every statement naming the new column
# failed with `no such column`, the hooks logged that and exited 0 as designed, and
# their metrics silently stopped with nothing they would look at saying so.
def _db_missing(tmp_path, *drop, name="old.db"):
    """A database built from the CURRENT schema minus some column declarations —
    i.e. what an older herd would have created."""
    core = CORE
    for line in drop:
        assert line in core, f"fixture is stale: {line!r} not in core.sql"
        core = core.replace(line, "")
    p = tmp_path / name
    c = sqlite3.connect(p)
    c.executescript(core)
    c.executescript(HERD)
    c.commit()
    c.close()
    return p


_API_MS = "    api_duration_ms      INTEGER,\n"


def test_schema_columns_parses_exactly_what_sqlite_reports():
    """The parser guard. schema_columns() reads CREATE TABLE text back out of
    sqlite_master and splits it, and core.sql documents nearly every column with a
    trailing `--` comment containing commas and parentheses. A first cut that did
    not strip comments produced columns named `--`, `the` and `user-mutable`, which
    migrate() then tried to ALTER TABLE ADD. Pinned against SQLite's own answer."""
    from herd.db import schema_columns, apply_schema
    ref = sqlite3.connect(":memory:")
    apply_schema(ref)
    for table, cols in schema_columns().items():
        actual = [r[1] for r in ref.execute(f"PRAGMA table_info({table})")]
        assert [c for c, _ in cols] == actual, table
    ref.close()


def test_migrate_adds_a_missing_column_and_keeps_the_data(tmp_path):
    from herd.db import connect, apply_schema, migrate, missing_columns, load_statements
    p = _db_missing(tmp_path, _API_MS)
    c = sqlite3.connect(p)
    c.execute("INSERT INTO sessions(session_id,cwd,model,status,status_source,"
              "last_event_at,last_event_type,started_at,updated_at) VALUES"
              "('s1','/x','m','working','hook',?,'tool',?,?)", (T0, T0, T0))
    c.commit()
    c.close()

    conn = connect(str(p), create=True)
    apply_schema(conn)                               # the no-op half
    assert [c for c, _ in missing_columns(conn)["sessions"]] == ["api_duration_ms"]
    added, failed = migrate(conn)
    assert added == ["sessions.api_duration_ms"] and failed == []
    assert not missing_columns(conn)

    # the actual symptom: the statusline sink works again, and nothing was lost
    conn.execute(load_statements()["W5_statusline"],
                 {k: None for k in ("session_id", "model", "sname", "ctx", "cost",
                                    "branch", "rl5", "rl5reset", "rl7", "rl7reset",
                                    "now", "ctxsize", "ocwd", "ladd", "ldel", "tokin",
                                    "tokout", "ver", "gwt", "exc200", "ostyle", "apims")})
    assert conn.execute("SELECT session_id FROM sessions").fetchone()[0] == "s1"
    conn.close()


def test_migrate_preserves_a_columns_check_constraint(tmp_path):
    """The declaration is carried across verbatim, not rebuilt from table_info —
    which has no CHECK, so rebuilding would silently drop the constraint that makes
    an invalid status unwritable."""
    from herd.db import connect, migrate
    # status_source's declaration spans lines, so drop the whole column block
    start = CORE.index("    status_source       TEXT")
    end = CORE.index("\n", CORE.index("))", start)) + 1
    block = CORE[start:end]
    assert "CHECK" in block, "fixture is stale: status_source lost its CHECK"

    p = _db_missing(tmp_path, block, name="nocheck.db")
    conn = connect(str(p), create=True)
    added, failed = migrate(conn)
    assert added == ["sessions.status_source"] and failed == []
    conn.execute("INSERT INTO sessions(cwd,status_source,started_at,updated_at) "
                 "VALUES('/x','hook',?,?)", (T0, T0))
    try:
        conn.execute("INSERT INTO sessions(cwd,status_source,started_at,updated_at) "
                     "VALUES('/y','nonsense',?,?)", (T0, T0))
        raise AssertionError("the CHECK constraint was lost in the migration")
    except sqlite3.IntegrityError:
        pass
    conn.close()


def test_migrate_is_a_noop_on_a_current_database(fresh):
    """Every install re-runs this. It must not churn a database that is already
    current, or report having done something."""
    from herd.db import migrate, missing_columns
    c = fresh()
    assert missing_columns(c) == {}
    assert migrate(c) == ([], [])


def test_a_non_additive_gap_is_reported_not_raised(tmp_path):
    """ALTER TABLE ADD COLUMN cannot add a UNIQUE column, and sessions.session_id is
    exactly that. Anything migrate cannot do must surface at install time carrying
    SQLite's own message — the alternative is a write that fails on every tick from
    then on, into a log nobody reads.

    (An earlier version of this test pointed migrate at a table outside herd's
    schema, so it exercised nothing at all.)"""
    from herd.db import connect, migrate, missing_columns
    p = _db_missing(
        tmp_path,
        "    session_id          TEXT UNIQUE,   "
        "-- Claude's UUID. NULL until adopted (UNIQUE ignores NULLs).\n",
        # the partial index references the column, so a herd without one had neither
        "CREATE INDEX IF NOT EXISTS idx_sessions_unadopted\n"
        "    ON sessions(pid) WHERE session_id IS NULL AND stopped_at IS NULL;\n",
        name="nouniq.db")
    conn = connect(str(p), create=True)
    assert [c for c, _ in missing_columns(conn)["sessions"]] == ["session_id"]
    added, failed = migrate(conn)                  # must NOT raise
    assert added == []
    assert len(failed) == 1 and "session_id" in failed[0] and "UNIQUE" in failed[0]
    conn.close()
