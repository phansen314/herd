"""Schema locations, statement loading, and connection policy.

Deliberately tier-agnostic: it knows where the SQL lives and how to open a
connection correctly, nothing about what the statements mean. See DESIGN.md#tiers.
"""
import pathlib
import re
import sqlite3
import urllib.parse

PKG = pathlib.Path(__file__).resolve().parent
SCHEMA_DIR = PKG / "schema"
CORE_SCHEMA = SCHEMA_DIR / "core.sql"   # tier 1
HERD_SCHEMA = SCHEMA_DIR / "herd.sql"   # tier 2
WRITES = SCHEMA_DIR / "writes.sql"      # W1-W6 + R1

_NAME_RE = re.compile(r"^--\s*:name\s+(\S+)\s*$")


def load_statements():
    """Parse `-- :name X` blocks out of writes.sql -> {name: sql}. Every consumer
    loads statements through here rather than keeping its own transcription.
    Mirrors common.sh stmt() — both cut at the first ';', and
    test_hooks.py::test_bash_and_python_extract_same asserts they agree.
    See DESIGN.md#write-paths-schemawritessql."""
    text = WRITES.read_text()
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
    # a block runs to the next :name; keep only its first statement, drop trailing prose.
    for k, v in out.items():
        stmt = v.split(";")[0].strip()
        out[k] = stmt + ";" if stmt else v
    return out


def connect(path, readonly=False, create=False):
    """Open a connection with herd's required pragmas. busy_timeout is NOT optional
    on ANY connection (incl. the bash hooks): WAL serialises writers, so without it
    a hook fails the moment the daemon/TUI holds the write lock.

    create=False IS THE POINT. A plain `file:` URI defaults to rwc, so a missing
    path is silently created — and nothing here applies the schema, so a typo in
    HERD_DB yields an empty database and `no such table: sessions` on every tick
    forever. Only the installer may bring a database into being."""
    # The path goes into a URI, so `?` starts a query and `#` a fragment: an
    # unescaped HERD_DB containing either opens the WRONG file. quote() leaves
    # ordinary paths untouched.
    safe = urllib.parse.quote(str(path))
    mode = "ro" if readonly else ("rwc" if create else "rw")
    uri = f"file:{safe}?mode={mode}"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=3000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def apply_schema(conn, tier2=True):
    """Apply tier 1, then (optionally) tier 2. tier2=False is a supported mode:
    tier 1 must stand up alone."""
    conn.executescript(CORE_SCHEMA.read_text())
    if tier2:
        conn.executescript(HERD_SCHEMA.read_text())
