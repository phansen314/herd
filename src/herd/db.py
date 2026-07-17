"""Schema locations, statement loading, and connection policy.

This module is deliberately tier-agnostic: it knows where the SQL lives and how
to open a connection correctly, and nothing about what the statements mean.

TIER IS A PROPERTY OF DATA, NOT OF CODE.
  tier 1  = sessions, events        — facts that would be true if herd didn't exist
  tier 2  = herd_sessions, herd_attention — herd's relationship to a session

The boundary is declared and enforced in the SCHEMA files (schema/core.sql
must never mention herd_; validate.py check A proves it, and check 45 proves
core.sql applies standalone). It is NOT a property of the packages here:
herd/hooks is herd's own code, it writes tier-1 facts (allowed: tier2 -> tier1,
the same permission reconcile uses) and reads tier-2 placement when it adopts
(W2/W5b). Trying to read that as a tier violation is what made `core/` look
like a tier when it was really just the hook layer.
"""
import pathlib
import re
import sqlite3

PKG = pathlib.Path(__file__).resolve().parent
SCHEMA_DIR = PKG / "schema"
CORE_SCHEMA = SCHEMA_DIR / "core.sql"   # tier 1
HERD_SCHEMA = SCHEMA_DIR / "herd.sql"   # tier 2
WRITES = SCHEMA_DIR / "writes.sql"      # W1-W6 + R1

_NAME_RE = re.compile(r"^--\s*:name\s+(\S+)\s*$")


def load_statements(src=None):
    """Parse `-- :name X` blocks out of writes.sql -> {name: sql}.

    Every consumer loads the SHIPPING statements through here. Nothing may keep
    its own transcription of a write path: the original validate.py re-typed
    W2b/W2/W5 inline, which let the real write path rot while the suite stayed
    green. Four defects survived 40 checks that way.
    """
    text = WRITES.read_text() if src is None else src
    out, name, buf = {}, None, []
    for line in text.splitlines():
        m = _NAME_RE.match(line)
        if m:
            if name:
                out[name] = "\n".join(buf).strip()
            name, buf = m.group(1), []
        elif name is not None:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf).strip()
    # a block runs to the next :name; keep only its first statement and drop
    # the trailing prose that follows the semicolon.
    for k, v in out.items():
        stmt = v.split(";")[0].strip()
        out[k] = stmt + ";" if stmt else v
    return out


def connect(path, readonly=False):
    """Open a connection with herd's required pragmas.

    busy_timeout is NOT optional and must be set on EVERY connection, including
    the bash hooks' sqlite3 invocations. WAL gives unlimited readers and one
    writer, and writers serialise: without a busy timeout a hook fails outright
    the moment reconcile or the TUI holds the write lock, and the hook is on
    claude's tool loop where the user feels it.
    """
    uri = f"file:{path}?mode=ro" if readonly else f"file:{path}"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def apply_schema(conn, tier2=True):
    """Apply tier 1, then (optionally) tier 2, to an open connection.

    tier2=False is a real supported mode, not a test affordance: tier 1 must
    stand up alone or its claim to be herd-independent is just a comment.
    """
    conn.executescript(CORE_SCHEMA.read_text())
    if tier2:
        conn.executescript(HERD_SCHEMA.read_text())
