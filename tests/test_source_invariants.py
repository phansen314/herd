"""Source-level invariants: statement integrity, the tier boundary, and the
liveness-is-derived rule — asserted against the SQL/hook text itself."""
import os
import re
import sqlite3

import pytest

from helpers import CORE, HERD, WRITES, HOOKS, W


# ── statement integrity ──────────────────────────────────────────────────────
@pytest.mark.parametrize("name,sql", list(W.items()))
def test_every_statement_is_complete(name, sql):
    """No ';'-in-comment truncation — both parsers cut at the first ';'."""
    assert sqlite3.complete_statement(sql.strip()), f"{name} is truncated/incomplete"


@pytest.mark.parametrize("dead", ["W3a_discover", "W3b_placement", "W3c_pid"])
def test_dead_kitty_reconcile_statements_gone(dead):
    """The retired kitty-discovery statements must not creep back as dormant SQL."""
    assert dead not in W


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
