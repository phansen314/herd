"""herd doctor — the diagnosis layer. Every failure it reports is one the system
is designed to survive SILENTLY (hooks never print, a missing dep exits 0, the
daemon logs to a journal you must know to read), so these tests care about one
thing: does the broken case actually get named."""
import json
import os
import pathlib

import pytest

from herd import daemon, doctor
from herd.doctor import OK, WARN, FAIL

from helpers import T0, mk_session


def _levels(results):
    return [r[0] for r in results]


def _text(results):
    return " ".join(f"{h} {d}" for _, h, d in results)


# ── dependencies ────────────────────────────────────────────────────────────
def test_a_missing_required_dep_is_a_failure():
    out = doctor.check_deps(which=lambda b: None)
    assert FAIL in _levels(out)
    assert "jq NOT FOUND" in _text(out)


def test_a_missing_optional_dep_is_only_a_warning():
    out = doctor.check_deps(which=lambda b: None if b in doctor.OPTIONAL else f"/usr/bin/{b}")
    assert FAIL not in _levels(out) and WARN in _levels(out)
    assert "kitten" in _text(out)


# ── database ────────────────────────────────────────────────────────────────
def test_a_missing_db_says_run_the_installer(tmp_path):
    out = doctor.check_db(str(tmp_path / "nope.db"))
    assert _levels(out) == [FAIL] and "herd.install" in _text(out)


def test_a_db_without_the_schema_is_a_failure(tmp_path):
    import sqlite3
    p = tmp_path / "empty.db"
    sqlite3.connect(str(p)).close()
    out = doctor.check_db(str(p))
    assert FAIL in _levels(out) and "schema not applied" in _text(out)


def test_a_corrupt_db_is_reported_not_raised(tmp_path):
    p = tmp_path / "junk.db"
    p.write_bytes(os.urandom(4096))
    out = doctor.check_db(str(p))                 # must not raise
    assert FAIL in _levels(out)


def test_a_healthy_db_reports_live_and_total(fresh, tmp_path):
    c = fresh(name="doc.db")
    mk_session(c, session_id="live1")
    mk_session(c, session_id="dead1", stopped_at=T0)
    c.close()
    out = doctor.check_db(str(tmp_path / "doc.db"))
    assert FAIL not in _levels(out)
    assert "1 live / 2 total" in _text(out)


# ── wiring ──────────────────────────────────────────────────────────────────
HOOKS = pathlib.Path("/hooks")
SL = "/hooks/statusline.sh"
EVENTS = ("SessionStart", "Stop")


def _settings(cmds, statusline=SL):
    return json.dumps({
        "hooks": {e: [{"hooks": [{"type": "command", "command": c}]}] for e, c in cmds.items()},
        "statusLine": {"type": "command", "command": statusline}})


def test_unwired_hooks_are_reported(tmp_path):
    out = doctor.check_wiring(_settings({}), HOOKS, SL, EVENTS)
    assert _text(out).count("not wired") == 2


def test_a_hook_wired_to_a_missing_file_is_reported(tmp_path):
    """The moved-checkout case: settings.json holds absolute paths into the tree."""
    out = doctor.check_wiring(
        _settings({"SessionStart": "/hooks/session_start.sh"}), HOOKS, SL, ("SessionStart",))
    assert FAIL in _levels(out) and "missing file" in _text(out)


def test_a_hook_without_the_executable_bit_is_reported(tmp_path):
    h = tmp_path / "session_start.sh"
    h.write_text("#!/bin/bash\n")
    h.chmod(0o644)                                 # the silent-no-op bug
    out = doctor.check_wiring(_settings({"SessionStart": str(h)}), tmp_path,
                              str(tmp_path / "statusline.sh"), ("SessionStart",))
    assert FAIL in _levels(out) and "not executable" in _text(out)


def test_an_unset_statusline_is_a_failure(tmp_path):
    out = doctor.check_wiring(json.dumps({"hooks": {}}), HOOKS, SL, ())
    assert FAIL in _levels(out) and "statusLine not set" in _text(out)


def test_a_statusline_behind_a_wrapper_counts_as_wired(tmp_path):
    wrapper = tmp_path / "custom-status-line.sh"
    wrapper.write_text(f'#!/bin/bash\nexec "{SL}" "$@"\n')
    out = doctor.check_wiring(_settings({}, statusline=str(wrapper)), HOOKS, SL, ())
    assert FAIL not in _levels(out) and "via wrapper" in _text(out)


def test_a_foreign_statusline_warns_that_no_metrics_are_recorded(tmp_path):
    out = doctor.check_wiring(_settings({}, statusline="/opt/mine.sh"), HOOKS, SL, ())
    assert WARN in _levels(out) and "records no metrics" in _text(out)


def test_unparseable_settings_is_reported_not_raised():
    out = doctor.check_wiring("{not json", HOOKS, SL, EVENTS)
    assert _levels(out) == [FAIL]


# ── daemon ──────────────────────────────────────────────────────────────────
def test_no_lock_means_the_daemon_is_not_running(tmp_path):
    out = doctor.check_daemon(str(tmp_path / "absent.lock"))
    assert _levels(out) == [FAIL] and "never leave" in _text(out)


def test_a_stale_lock_does_not_read_as_running(tmp_path):
    lock = tmp_path / "herd-daemon.lock"
    lock.write_text("999999\n")
    out = doctor.check_daemon(str(lock), holder=999999, alive=lambda pid: False)
    assert _levels(out) == [FAIL] and "stale lock" in _text(out)


def test_a_held_lock_reads_as_running(tmp_path):
    lock = tmp_path / "herd-daemon.lock"
    lock.write_text(f"{os.getpid()}\n")
    out = doctor.check_daemon(str(lock), holder=os.getpid(), alive=lambda pid: True)
    assert _levels(out) == [OK]


# ── env ─────────────────────────────────────────────────────────────────────
def test_a_malformed_threshold_is_named():
    """It no longer tracebacks, so the only way to notice is being told."""
    out = doctor.check_env({"HERD_WAIT_SECS": "fast"})
    assert WARN in _levels(out) and "HERD_WAIT_SECS" in _text(out)


def test_valid_overrides_are_listed():
    out = doctor.check_env({"HERD_WAIT_SECS": "45"})
    assert _levels(out) == [OK]


# ── errlog ──────────────────────────────────────────────────────────────────
def test_no_errlog_is_good_news(tmp_path):
    assert _levels(doctor.check_errlog(str(tmp_path / "none.log"))) == [OK]


def test_a_missing_dependency_in_the_errlog_is_escalated(tmp_path):
    log = tmp_path / "err.log"
    log.write_text("2026-07-18T10:00:00Z\tstop.sh\tjq NOT FOUND (rc=127)\n")
    out = doctor.check_errlog(str(log))
    assert _levels(out) == [FAIL]


def test_ordinary_errors_only_warn(tmp_path):
    log = tmp_path / "err.log"
    log.write_text("2026-07-18T10:00:00Z\tstop.sh\trc=5 database is locked\n")
    assert _levels(doctor.check_errlog(str(log))) == [WARN]


# ── report ──────────────────────────────────────────────────────────────────
def test_report_exits_nonzero_only_on_failure():
    lines = []
    assert doctor.report([("x", [(OK, "fine", "")])], out=lines.append) == 0
    assert doctor.report([("x", [(WARN, "meh", "")])], out=lines.append) == 0
    assert doctor.report([("x", [(FAIL, "broken", "")])], out=lines.append) == 1
    assert any("not healthy" in ln for ln in lines)


def test_doctor_runs_against_the_real_machine_without_raising():
    """It must survive whatever state the box is in — that is the whole point."""
    lines = []
    rc = doctor.report(doctor.collect(), out=lines.append)
    assert rc in (0, 1) and lines


def test_cli_dispatches_doctor_without_opening_the_db(monkeypatch):
    """A missing or corrupt DB is something doctor REPORTS. main()'s shared connect
    would traceback on exactly the machines it exists to diagnose."""
    from herd import cli
    monkeypatch.setattr(cli, "DEFAULT_DB", "/nonexistent/herd.db")
    monkeypatch.setattr(cli, "connect",
                        lambda *a, **k: pytest.fail("doctor must not open the DB"))
    assert cli.main(["doctor"]) in (0, 1)
