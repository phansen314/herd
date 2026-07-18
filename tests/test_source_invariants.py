"""Source-level invariants: statement integrity, the tier boundary, and the
liveness-is-derived rule — asserted against the SQL/hook text itself."""
import os
import re
import sqlite3

import pytest

from helpers import CORE, HERD, WRITES, HOOKS, ROOT, W, cells


# ── statement integrity ──────────────────────────────────────────────────────
@pytest.mark.parametrize("name,sql", list(W.items()))
def test_every_statement_is_complete(name, sql):
    """No ';'-in-comment truncation — both parsers cut at the first ';'."""
    assert sqlite3.complete_statement(sql.strip()), f"{name} is truncated/incomplete"


@pytest.mark.parametrize("dead", ["W3a_discover", "W3b_placement", "W3c_pid"])
def test_dead_kitty_reconcile_statements_gone(dead):
    """The retired kitty-discovery statements must not creep back as dormant SQL."""
    assert dead not in W


def test_pager_actuator_stays_deleted():
    """herd owns no actuator, so attention is binary: armed or acked. The escalation
    surface (W6b_paged, paged_at, paged_level) was schema and SQL with no caller for
    a feature with no owner — it must not creep back. See DECISIONS.md."""
    assert "W6b_paged" not in W
    assert not re.search(r"\bpaged_(at|level)\b", _code(HERD)), "paged_* back in the schema"
    assert not re.search(r"\bpaged_(at|level)\b",
                         "\n".join(_code(s) for s in W.values())), "paged_* back in a statement"


# ── the attention marks are column-safe ──────────────────────────────────────
def test_attention_glyphs_are_two_cells():
    """_line() budgets exactly two cells for the mark. A one-cell glyph shifts that
    row's columns left and a three-cell one shifts them right — ragged for the one
    row you most want to read. Emoji width is not obvious by eye (✓ is one cell,
    ✅ is two), so it is asserted rather than trusted."""
    from herd import cli
    marks = {**cli.ATTENTION_MARKS, "UNKNOWN": cli.MARK_UNKNOWN, "NONE": cli.MARK_NONE}
    bad = {k: (v, cells(v)) for k, v in marks.items() if cells(v) != 2}
    assert not bad, f"marks that would break column alignment: {bad}"


def test_every_attention_status_has_a_reason_and_a_mark():
    """The picker and the preview must agree on which statuses are page-worthy, and
    both must cover every status the daemon can actually arm."""
    from herd import cli
    from herd import daemon
    assert set(cli.ATTENTION_MARKS) == set(cli.ATTENTION_REASONS)
    assert set(cli.ATTENTION_MARKS) == set(daemon.ATTENTION_SECS), \
        "a status the daemon arms has no glyph (or vice versa)"


# ── doc cross-references resolve ─────────────────────────────────────────────
DOCS = ("DESIGN.md", "DECISIONS.md", "README.md")
SCANNED = {".md", ".py", ".sh", ".sql"}


def _anchors(md):
    """The anchors a GitHub-rendered heading exposes: an explicit `{#slug}` when the
    heading carries one, else the auto-slug — lowercased, backticks and punctuation
    dropped, spaces to hyphens."""
    out = set()
    for line in md.splitlines():
        if not line.startswith("#"):
            continue
        head = line.lstrip("#").strip()
        explicit = re.search(r"\{#([\w-]+)\}", head)
        if explicit:
            out.add(explicit.group(1))
            continue
        slug = re.sub(r"[^\w\s-]", "", head.replace("`", "")).strip().lower()
        out.add(re.sub(r"\s", "-", slug))
    return out


def test_doc_cross_references_resolve():
    """A source comment pointing at an anchor that 404s is worse than no pointer: it
    reads as authoritative and silently isn't. Nothing checked these, so a dead
    anchor once shipped in a green commit.

    (Write doc refs in this file's own prose without the '#' — the scan reads its
    own source too, and a literal example would trip it.)"""
    anchors = {d: _anchors((ROOT / d).read_text()) for d in DOCS}
    broken = []
    for p in sorted(ROOT.rglob("*")):
        if not p.is_file() or p.suffix not in SCANNED or ".git" in p.parts:
            continue
        for i, line in enumerate(p.read_text().splitlines(), 1):
            for doc, anchor in re.findall(r"\b(DESIGN|DECISIONS|README)\.md#([\w-]+)", line):
                if anchor not in anchors[f"{doc}.md"]:
                    broken.append(f"{p.relative_to(ROOT)}:{i} -> {doc}.md#{anchor}")
    assert not broken, "dead doc anchors:\n  " + "\n  ".join(broken)


# ── A. tier boundary (text) ──────────────────────────────────────────────────
def _code(text):
    return "\n".join(l.split("--")[0] for l in text.splitlines()).lower()


def test_core_has_no_herd_tables():
    assert "herd_" not in _code(CORE)


def test_core_declares_no_triggers():
    assert "create trigger" not in _code(CORE)


def test_herd_declares_no_trigger():
    assert "create trigger" not in _code(HERD)


def test_no_tier2_ddl_attaches_to_sessions():
    assert "on sessions" not in _code(HERD)


def test_no_live_denormalization_column():
    assert "live" not in _code(HERD)


def test_core_writers_take_no_tier2_value():
    """In every sessions writer, the value region (before the first WHERE) must not
    reference a herd_ table. Routing (WHERE + subqueries) may."""
    core_writers = [n for n, s in W.items()
                    if re.search(r"\b(INSERT\s+INTO|UPDATE)\s+sessions\b",
                                 _code(s), re.I)]
    assert core_writers, "expected some core writers"
    leaks = []
    for n in core_writers:
        values_region = re.split(r"\bWHERE\b", _code(W[n]), maxsplit=1, flags=re.I)[0]
        if re.search(r"\bherd_(sessions|attention)\b", values_region, re.I):
            leaks.append(n)
    assert not leaks, f"tier-2 VALUE leaked into a core column in {leaks}"


# ── 44. liveness is derived, never stored ────────────────────────────────────
def test_every_window_lookup_derives_liveness():
    """Any (socket, window_id) LOOKUP (kitty_socket = :param) must JOIN
    sessions.stopped_at. W2b_placement's `= excluded.` is a WRITE, not a lookup."""
    writes_code = "\n".join(l.split("--")[0] for l in WRITES.splitlines())
    stmts = [s for s in writes_code.split(";") if "window_id" in s and "kitty_socket" in s]
    lookups = [s for s in stmts if re.search(r"kitty_socket\s*=\s*:", s, re.I)]
    offenders = [" ".join(s.split())[:70] for s in lookups
                 if not re.search(r"stopped_at\s+IS\s+NULL", s, re.I)]
    assert not offenders, f"un-joined window lookups: {offenders}"


def test_no_live_column_reference_in_writes():
    writes_code = "\n".join(l.split("--")[0] for l in WRITES.splitlines())
    assert not re.search(r"\blive\s*=\s*1\b", writes_code, re.I)


def test_w5_statusline_never_touches_last_event():
    w5 = "\n".join(l.split("--")[0] for l in W["W5_statusline"].splitlines())
    assert "last_event" not in w5.lower()


# ── 69. source enum: allowed set == written set ──────────────────────────────
def test_herd_source_allowed_equals_written():
    allowed = set(re.search(
        r"source\s+TEXT[^,]*CHECK\s*\(\s*source\s+IN\s*\(([^)]*)\)", HERD, re.I)
        .group(1).replace("'", "").replace(" ", "").split(","))
    src_writers = "\n".join(s for n, s in W.items()
                            if re.search(r"INSERT\s+INTO\s+herd_sessions", s, re.I))
    src_code = "\n".join(l.split("--")[0] for l in src_writers.splitlines())
    written = {v for v in allowed | {"reconcile"} if f"'{v}'" in src_code}
    assert allowed == written, f"allowed={sorted(allowed)} written={sorted(written)}"


# ── 56 / 56b. hooks route all DML through writes.sql, and are executable ──────
def test_no_hook_inlines_dml():
    offenders = []
    for shf in sorted(HOOKS.glob("*.sh")):
        if shf.name == "common.sh":       # the db()/run() wrapper IS the SQL path
            continue
        for i, line in enumerate(shf.read_text().splitlines(), 1):
            if re.search(r"\b(INSERT|UPDATE|DELETE)\s", line.split("#", 1)[0], re.I):
                offenders.append(f"{shf.name}:{i}")
    assert not offenders, f"inlined DML at {offenders}"


@pytest.mark.parametrize("shf", sorted(HOOKS.glob("*.sh")), ids=lambda p: p.name)
def test_every_hook_is_executable(shf):
    """settings.json execs these paths directly; a missing +x is a silent no-op."""
    assert os.access(shf, os.X_OK), f"{shf.name} is not executable"
