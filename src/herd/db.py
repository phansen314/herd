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
    tier 1 must stand up alone.

    IDEMPOTENT BUT NOT A MIGRATION. Every object here is `IF NOT EXISTS`, so
    re-running this on an existing database creates new TABLES and new INDEXES and
    silently does nothing about new COLUMNS — `CREATE TABLE IF NOT EXISTS` on a
    table that exists is a no-op regardless of how its definition has changed since.
    See migrate(), which closes that gap, and which bootstrap_db calls right after
    this."""
    conn.executescript(CORE_SCHEMA.read_text())
    if tier2:
        conn.executescript(HERD_SCHEMA.read_text())


def _strip_sql_comments(text):
    """Drop `--` line comments, respecting single-quoted string literals.

    Not optional here: core.sql documents nearly every column with a trailing `--`
    comment, and those comments contain commas and parentheses. Splitting the raw
    body treats each comment as its own column definition — the first attempt at
    this produced columns named `--`, `the` and `user-mutable`."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if ch == "'":
                in_str = text[i + 1:i + 2] == "'"    # '' is an escaped quote
                if in_str:
                    out.append("'")
                    i += 1
            i += 1
            continue
        if ch == "'":
            in_str = True
            out.append(ch)
        elif ch == "-" and text[i + 1:i + 2] == "-":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _split_top_level(body):
    """Split a CREATE TABLE body on commas outside parentheses and string literals."""
    parts, depth, buf, in_str = [], 0, [], False
    for ch in body:
        if in_str:
            buf.append(ch)
            if ch == "'":
                in_str = False
            continue
        if ch == "'":
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


# Words that begin a TABLE constraint rather than a column definition.
_TABLE_CONSTRAINT = {"check", "primary", "unique", "foreign", "constraint"}


def schema_columns(tier2=True):
    """{table: [(column, full declaration), ...]} for the schema herd SHIPS.

    Derived by applying the schema to an in-memory database and reading its own
    CREATE TABLE text back, so SQLite does the parsing rather than a regex here.
    The full declaration is kept, not just name and type: a column's CHECK, DEFAULT
    and COLLATE are part of it, and reconstructing from PRAGMA table_info would
    silently drop the CHECK constraints that core.sql leans on."""
    ref = sqlite3.connect(":memory:")
    try:
        apply_schema(ref, tier2=tier2)
        out = {}
        for name, sql in ref.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"):
            body = sql[sql.index("(") + 1:sql.rindex(")")]
            cols = []
            for part in _split_top_level(_strip_sql_comments(body)):
                decl = " ".join(part.split())        # a decl may span lines
                if not decl:
                    continue
                first = decl.split()[0].strip('"[]`')
                if first.lower() in _TABLE_CONSTRAINT:
                    continue
                cols.append((first, decl))
            out[name] = cols
        return out
    finally:
        ref.close()


def missing_columns(conn, tier2=True):
    """{table: [(column, declaration), ...]} present in the shipped schema and NOT
    in this database. Only tables that already exist are considered — a missing
    table is apply_schema's job, not a column problem.

    This is the upgrade gap. Herd has added columns to `sessions` repeatedly (see
    `feat(statusline): persist every field the payload carries`), and until
    migrate() existed, a user who installed before such a change kept the old table
    forever: re-running the installer reported success, every statement naming the
    new column failed with `no such column`, the hooks logged that to
    ~/.herd/hook-errors.log and exited 0 as designed, and their metrics silently
    stopped. Nothing they would look at said so."""
    have = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    out = {}
    for table, cols in schema_columns(tier2=tier2).items():
        if table not in have:
            continue
        present = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        gap = [(c, d) for c, d in cols if c not in present]
        if gap:
            out[table] = gap
    return out


def migrate(conn, tier2=True):
    """Add columns this database is missing. Returns (added, failed) as lists of
    strings, both empty when there was nothing to do.

    ADDITIVE ONLY, and deliberately so. Every schema change herd has shipped is a
    new nullable column, which ALTER TABLE ADD COLUMN handles exactly. Anything
    else — a dropped column, a changed type, a new UNIQUE — is NOT attempted: it is
    reported as a failure with SQLite's own message, so it surfaces loudly at
    install time instead of becoming a silent write failure later.

    No PRAGMA user_version. A version counter would have to be bumped by hand on
    every schema change, and a stale one is worse than none — the same trap as the
    hardcoded test counts this repo has already had to delete twice. This diffs the
    live database against the shipped schema instead, so it cannot go stale."""
    added, failed = [], []
    for table, cols in sorted(missing_columns(conn, tier2=tier2).items()):
        for col, decl in cols:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {decl}")
                added.append(f"{table}.{col}")
            except sqlite3.Error as e:
                failed.append(f"{table}.{col}: {e}")
    return added, failed
