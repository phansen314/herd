"""Shared constants + row builders for the herd test suite.

The builders collapse the endless `INSERT INTO sessions(...) VALUES(...)` that the
old validate.py repeated ~90 times. Every column has a sane default; name only
what the test cares about.
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "src"))

from herd.db import (CORE_SCHEMA, HERD_SCHEMA, WRITES as WRITES_PATH,  # noqa: E402
                     load_statements)

# ── fixed clocks (ISO-UTC, millis) ───────────────────────────────────────────
T0 = "2026-07-15T10:00:00.000Z"
T1 = "2026-07-15T10:05:00.000Z"   # T0 + 5min
T2 = "2026-07-15T10:10:00.000Z"   # T0 + 10min
T0_10 = "2026-07-15T10:00:10.000Z"
T0_20 = "2026-07-15T10:00:20.000Z"
T0_240 = "2026-07-15T10:04:00.000Z"

SOCK = "unix:/tmp/kitty-20035"
HOOKS = ROOT / "src" / "herd" / "hooks"


def cells(s):
    """Terminal cells `s` occupies — East Asian Wide/Fullwidth count 2. Column
    assertions must use this, NOT len(): '  ' is two codepoints and two cells, but
    '🙋' is ONE codepoint and two cells, so len() disagrees with the screen."""
    import unicodedata as ud
    return sum(2 if ud.east_asian_width(c) in ("W", "F") else 1 for c in s)

CORE = CORE_SCHEMA.read_text()
HERD = HERD_SCHEMA.read_text()
WRITES = WRITES_PATH.read_text()

# The loader is production code (herd.db); using it here tests it too.
W = load_statements()

# Every W5_statusline param, all NULL. Spread it (`{**SL_PARAMS, "ctx": 42}`) and
# name only what the test asserts on. Canonical here, not copied per test file:
# W5 grows, and two hand-kept copies drift into "did not supply a value" failures.
SL_PARAMS = {k: None for k in (
    "model", "sname", "ctx", "cost", "branch", "rl5", "rl5reset", "rl7", "rl7reset",
    "gwt", "ocwd", "ver", "ostyle", "ctxsize", "exc200", "tokin", "tokout",
    "ladd", "ldel", "apims",
)}


def strip_sql(text):
    """Drop `--` comments per line and collapse whitespace — for source-level
    assertions that must ignore comment prose."""
    import re
    code = "\n".join(l.split("--")[0] for l in text.splitlines())
    return re.sub(r"\s+", " ", code).strip()


# ── row builders ─────────────────────────────────────────────────────────────
def mk_session(c, session_id=None, pid=None, cwd="/a", status="working",
               status_source=None, session_name=None, last_event_at=None,
               last_event_type=None, started_at=T0, updated_at=T0, stopped_at=None):
    return c.execute(
        "INSERT INTO sessions(session_id,pid,cwd,status,status_source,session_name,"
        "last_event_at,last_event_type,started_at,updated_at,stopped_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (session_id, pid, cwd, status, status_source, session_name,
         last_event_at, last_event_type, started_at, updated_at, stopped_at)).lastrowid


def mk_herd(c, pk, job_name=None, created_at=None, kitty_socket=SOCK, window_id=None,
            herd_var=None, source="spawn", verified_at=T0):
    return c.execute(
        "INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
        "window_id,herd_var,source,verified_at) VALUES(?,?,?,?,?,?,?,?)",
        (pk, job_name, created_at, kitty_socket, window_id, herd_var, source, verified_at))


def mk_attention(c, pk, attention_at=None, ack_at=None):
    return c.execute(
        "INSERT INTO herd_attention(session_pk,attention_at,ack_at) "
        "VALUES(?,?,?)", (pk, attention_at, ack_at))


# ── read helpers ─────────────────────────────────────────────────────────────
def live_in_window(c, sock, win):
    """The pks of LIVE sessions in a kitty window — the liveness JOIN that
    replaced the `live` denormalization."""
    return [r["session_pk"] for r in c.execute(
        "SELECT h.session_pk FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk "
        "WHERE h.kitty_socket=? AND h.window_id=? AND s.stopped_at IS NULL", (sock, win))]


def job_holder(c, job):
    """The live holder of a job name via R_job_live, or None."""
    r = c.execute(W["R_job_live"], {"job": job}).fetchone()
    return r["session_pk"] if r else None


def stopped_at(c, pk):
    return c.execute("SELECT stopped_at FROM sessions WHERE id=?", (pk,)).fetchone()[0]
