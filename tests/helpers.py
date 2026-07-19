"""Shared constants + row builders for the herd test suite.

The builders collapse the endless `INSERT INTO sessions(...) VALUES(...)` that the
old validate.py repeated ~90 times. Every column has a sane default; name only
what the test cares about.
"""
import contextlib
import pathlib
import shutil
import subprocess
import sys
import tempfile

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

# `date` by ABSOLUTE path, resolved before any test shadows PATH. The fake-date
# fixtures shadow PATH to intercept `date`, so their final `exec` must name the
# real binary directly — and it is /bin/date on macOS, /usr/bin/date on most Linux.
# Hardcoding either one makes the BSD-portability tests fail on the very platform
# they exist to protect (see CONTRIBUTING.md, "write oracles that can fail").
REAL_DATE = shutil.which("date") or "/bin/date"


def sqlite3_cli_emits_raw_control_chars():
    """Does the sqlite3 CLI pass a control character in a VALUE through untouched
    under `.mode list`?

    Newer builds (Apple's 3.51 among them) render it in caret notation instead —
    char(31) comes out as the two bytes "^_", not 0x1f. That decides whether
    preview.sh's separators can ever be forged by row data: where the CLI escapes
    them, a value can no longer split a record, the NF != 20 guard cannot trip,
    and the row renders normally. Probed rather than version-gated — the behavior
    is what matters and it is cheap to just ask.
    """
    out = subprocess.run(
        ["sqlite3", ":memory:"],
        input='.mode list\n.separator "\x1f" "\x1e"\nselect char(31);\n',
        capture_output=True, text=True,
    ).stdout
    return "\x1f" in out


@contextlib.contextmanager
def short_tmp_dir(prefix="herd-"):
    """A temp dir with a SHORT absolute path, for binding AF_UNIX sockets.

    sun_path is 104 bytes on macOS (108 on Linux), and pytest's `tmp_path` under
    macOS's $TMPDIR (/private/var/folders/<...>/pytest-of-<user>/<test-name-N>/)
    blows past it — bind() then fails with "AF_UNIX path too long" for reasons
    that have nothing to do with what the test is asserting. /tmp keeps it short
    on both platforms.
    """
    d = tempfile.mkdtemp(prefix=prefix, dir="/tmp")
    try:
        yield pathlib.Path(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


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
