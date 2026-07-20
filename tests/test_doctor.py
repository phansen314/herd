"""herd doctor — the diagnosis layer. Every failure it reports is one the system
is designed to survive SILENTLY (hooks never print, a missing dep exits 0, the
daemon logs to a journal you must know to read), so these tests care about one
thing: does the broken case actually get named."""
import fcntl
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


# ── jq version: PRESENCE IS NOT ENOUGH ──────────────────────────────────────
_HAVE_JQ = lambda b: f"/usr/bin/{b}"             # noqa: E731


@pytest.mark.parametrize("raw", ["jq-1.5", "jq version 1.5", "jq-1.4"])
def test_jq_below_1_6_fails_because_strflocaltime_is_missing(raw):
    """The whole point: jq is installed, `which` is happy, and the statusline still
    records nothing — strflocaltime raises and one raise aborts the entire filter."""
    out = doctor.check_jq_version(which=_HAVE_JQ, run=lambda: raw + "\n")
    assert FAIL in _levels(out)
    assert "too old" in _text(out) and "strflocaltime" in _text(out)


@pytest.mark.parametrize("raw", ["jq-1.6", "jq-1.7.1", "jq-1.7rc1", "jq-2.0"])
def test_jq_at_or_above_1_6_is_ok(raw):
    out = doctor.check_jq_version(which=_HAVE_JQ, run=lambda: raw + "\n")
    assert _levels(out) == [OK]


def test_no_jq_reports_nothing_here_because_check_deps_already_failed_it():
    """Two lines for one cause is noise — check_deps owns the absent case."""
    assert doctor.check_jq_version(which=lambda b: None) == []


def test_an_unreadable_or_unrunnable_jq_version_warns_rather_than_crashing():
    """doctor must be safe on a machine that is already sick, so a jq that errors
    or prints something unrecognised cannot take the whole report down."""
    def boom():
        raise OSError("no such file")
    assert WARN in _levels(doctor.check_jq_version(which=_HAVE_JQ, run=boom))
    out = doctor.check_jq_version(which=_HAVE_JQ, run=lambda: "not a version")
    assert WARN in _levels(out) and FAIL not in _levels(out)


# ── the interpreter doctor is standing inside ───────────────────────────────
def test_a_too_old_python_is_named_with_its_path():
    out = doctor.check_python(version_info=(3, 8, 0), executable="/usr/bin/python3")
    assert FAIL in _levels(out)
    assert "3.8" in _text(out) and "/usr/bin/python3" in _text(out)


def test_a_supported_python_is_ok():
    out = doctor.check_python(version_info=(3, 9, 0), executable="/opt/py/bin/python3")
    assert _levels(out) == [OK]
    assert "/opt/py/bin/python3" in _text(out)     # WHICH python, not just a version


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


def test_a_tier1_only_db_is_not_reported_as_healthy(fresh, tmp_path):
    """core.sql applied, herd.sql missing — and doctor said "herd looks healthy".

    check_db asserted only that `sessions` existed. Such a DB opens, counts sessions
    fine, and fails every W1/W2/W6 statement: spawn, jump, placement and attention
    are all dead. That is exactly the silent, half-broken state doctor exists to
    name, and it was the one shape that sailed through. The missing tables are named
    because "schema incomplete" without them is a mystery."""
    c = fresh(tier2=False, name="tier1.db")
    c.close()
    out = doctor.check_db(str(tmp_path / "tier1.db"))
    assert FAIL in _levels(out)
    assert "herd_sessions" in _text(out) and "herd_attention" in _text(out)


def test_a_db_missing_a_column_is_reported(fresh, tmp_path):
    """Tables are not enough. A database created before a column was added has every
    table doctor checks for, and still fails every statement naming that column —
    which the hooks log and exit 0 on, so the only symptom is metrics quietly going
    stale. Naming the column is what turns that into one command."""
    import sqlite3
    from helpers import CORE, HERD
    core = CORE.replace("    api_duration_ms      INTEGER,\n", "")
    assert core != CORE, "fixture is stale: api_duration_ms not in core.sql"
    p = tmp_path / "old.db"
    c = sqlite3.connect(p)
    c.executescript(core)
    c.executescript(HERD)
    c.commit()
    c.close()

    out = doctor.check_db(str(p))
    assert FAIL in _levels(out)
    assert "api_duration_ms" in _text(out) and "herd.install" in _text(out)


def test_required_tables_matches_the_schema(fresh):
    """REQUIRED_TABLES is hand-written, so pin it against what the schema actually
    creates — a new table must not be able to appear without this list noticing."""
    c = fresh()
    got = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")}
    assert got == set(doctor.REQUIRED_TABLES)


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
    out = doctor.check_wiring(_settings({}), (HOOKS,), (SL,), EVENTS)
    assert _text(out).count("not wired") == 2


def test_a_hook_wired_to_a_missing_file_is_reported(tmp_path):
    """The moved-checkout case: settings.json holds absolute paths into the tree."""
    out = doctor.check_wiring(
        _settings({"SessionStart": "/hooks/session_start.sh"}), (HOOKS,), (SL,), ("SessionStart",))
    assert FAIL in _levels(out) and "missing file" in _text(out)


def test_a_hook_without_the_executable_bit_is_reported(tmp_path):
    h = tmp_path / "session_start.sh"
    h.write_text("#!/bin/bash\n")
    h.chmod(0o644)                                 # the silent-no-op bug
    out = doctor.check_wiring(_settings({"SessionStart": str(h)}), (tmp_path,),
                              (str(tmp_path / "statusline.sh"),), ("SessionStart",))
    assert FAIL in _levels(out) and "not executable" in _text(out)


def test_a_statusline_without_the_executable_bit_is_reported(tmp_path):
    """The hook branch has checked +x for a while ("a lost +x is a silent no-op",
    and a blank statusline once shipped exactly that way). The statusLine branch
    took the path-match shortcut and returned OK without ever looking — for the ONE
    script that writes every metric column, so the symptom is cost, context and
    branch silently never being recorded."""
    sl = tmp_path / "statusline.sh"
    sl.write_text("#!/bin/bash\n")
    sl.chmod(0o644)
    out = doctor.check_wiring(_settings({}, statusline=str(sl)), (tmp_path,),
                              (str(sl),), ())
    assert FAIL in _levels(out) and "not executable" in _text(out)


def test_a_statusline_wired_to_a_missing_file_is_reported(tmp_path):
    """Same shortcut, same blind spot: the path matched, so nothing checked it
    still existed."""
    sl = tmp_path / "statusline.sh"                     # never created
    out = doctor.check_wiring(_settings({}, statusline=str(sl)), (tmp_path,),
                              (str(sl),), ())
    assert FAIL in _levels(out) and "missing file" in _text(out)


def test_an_unset_statusline_is_a_failure(tmp_path):
    out = doctor.check_wiring(json.dumps({"hooks": {}}), (HOOKS,), (SL,), ())
    assert FAIL in _levels(out) and "statusLine not set" in _text(out)


def test_a_statusline_behind_a_wrapper_counts_as_wired(tmp_path):
    wrapper = tmp_path / "custom-status-line.sh"
    wrapper.write_text(f'#!/bin/bash\nexec "{SL}" "$@"\n')
    out = doctor.check_wiring(_settings({}, statusline=str(wrapper)), (HOOKS,), (SL,), ())
    assert FAIL not in _levels(out) and "via wrapper" in _text(out)


def test_a_foreign_statusline_warns_that_no_metrics_are_recorded(tmp_path):
    out = doctor.check_wiring(_settings({}, statusline="/opt/mine.sh"), (HOOKS,), (SL,), ())
    assert WARN in _levels(out) and "records no metrics" in _text(out)


def test_unparseable_settings_is_reported_not_raised():
    out = doctor.check_wiring("{not json", (HOOKS,), (SL,), EVENTS)
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
    """A REAL flock, not a lock file plus alive=lambda: True.

    This used to write a pid and stub liveness, so it asserted nothing about the
    lock — the file's mere existence plus a fake `alive` was the whole test, which
    is exactly the thing that made the pid-reuse bug below invisible."""
    lock = tmp_path / "herd-daemon.lock"
    lock.write_text(f"{os.getpid()}\n")
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)      # a live daemon would
        out = doctor.check_daemon(str(lock), holder=os.getpid())
    assert _levels(out) == [OK]


def test_a_recycled_pid_does_not_read_as_running(tmp_path):
    """The lock FILE outlives its holder; the flock does not.

    check_daemon read the recorded pid and called os.kill(pid, 0). After a daemon
    crash that pid sits in the file, and once the OS recycles the number ANY
    unrelated process makes that probe succeed — so doctor printed "daemon running
    (pid N)" while nothing was reaping and sessions piled up in `herd ls`, the exact
    symptom the check exists to catch. Nobody holds this lock, and this process is
    unquestionably alive, so a pid-based check must say "running" and a lock-based
    one must not."""
    lock = tmp_path / "herd-daemon.lock"
    lock.write_text(f"{os.getpid()}\n")                     # our own pid: alive
    out = doctor.check_daemon(str(lock), holder=os.getpid())
    assert _levels(out) == [FAIL] and "stale lock" in _text(out)


def test_an_unprobeable_lock_falls_back_to_the_pid(tmp_path):
    """When the lock gives no answer, the old pid check is better than reporting
    every daemon dead. held is injected because the only way to reach that branch
    for real is a lock file that exists and cannot be opened, which is not
    arrangeable as root — where CI often runs."""
    lock = tmp_path / "herd-daemon.lock"
    lock.write_text("4242\n")
    assert _levels(doctor.check_daemon(str(lock), holder=4242,
                                       alive=lambda pid: True, held=None)) == [FAIL]
    assert _levels(doctor.check_daemon(str(lock), holder=4242,
                                       alive=lambda pid: True, held=True)) == [OK]


def test_lock_is_held_answers_the_lock_not_the_file(tmp_path):
    """daemon.lock_is_held on its own: held, not held, and no file at all."""
    lock = tmp_path / "herd-daemon.lock"
    assert daemon.lock_is_held(str(lock)) is None           # no file
    lock.write_text("1\n")
    assert daemon.lock_is_held(str(lock)) is False          # file, nobody holding
    with open(lock, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert daemon.lock_is_held(str(lock)) is True
    assert daemon.lock_is_held(str(lock)) is False          # released on close


def test_probing_the_lock_does_not_steal_it(tmp_path):
    """The probe takes the lock when nobody holds it, so it MUST release — a doctor
    run that left the lock held would stop the next daemon from starting."""
    lock = tmp_path / "herd-daemon.lock"
    lock.write_text("1\n")
    daemon.lock_is_held(str(lock))
    assert daemon.acquire_single_instance(str(lock)) is True


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


# ── hook mode: which hooks are actually running ─────────────────────────────
INST = pathlib.Path("/home/u/.herd/hooks")
TREE = pathlib.Path("/home/u/code/herd/src/herd/hooks")


def _mode_settings(root):
    return json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": f"{root}/stop.sh"}]}]}})


def test_copy_mode_is_reported_as_healthy():
    out = doctor.check_hook_mode(_mode_settings(INST), INST, TREE, current=True)
    assert _levels(out) == [OK] and "installed copy" in _text(out)


def test_dev_mode_is_reported_as_a_warning_not_a_failure():
    """--dev is a legitimate choice while editing hooks, but it must never be a
    surprise: a git checkout changes what every running session executes."""
    out = doctor.check_hook_mode(_mode_settings(TREE), INST, TREE)
    assert _levels(out) == [WARN]
    assert "CHECKOUT" in _text(out) and "--dev" in _text(out)


def test_stale_installed_hooks_are_reported():
    """The cost of the copy: edits to the tree do nothing until you re-install."""
    out = doctor.check_hook_mode(_mode_settings(INST), INST, TREE, current=False)
    assert _levels(out) == [WARN] and "STALE" in _text(out)


def test_wiring_to_both_roots_is_a_failure():
    """Every event wired twice means every hook fires twice."""
    both = json.dumps({"hooks": {"Stop": [
        {"hooks": [{"type": "command", "command": f"{INST}/stop.sh"}]},
        {"hooks": [{"type": "command", "command": f"{TREE}/stop.sh"}]}]}})
    out = doctor.check_hook_mode(both, INST, TREE)
    assert _levels(out) == [FAIL] and "BOTH" in _text(out)


def test_no_herd_hooks_at_all_is_a_failure():
    out = doctor.check_hook_mode(json.dumps({"hooks": {}}), INST, TREE)
    assert _levels(out) == [FAIL] and "no herd hooks wired" in _text(out)


def test_a_dev_install_is_not_mistaken_for_broken_wiring():
    """check_wiring accepts either root — otherwise --dev reads as 'not wired'."""
    out = doctor.check_wiring(_mode_settings(TREE), (INST, TREE),
                              (f"{INST}/statusline.sh", f"{TREE}/statusline.sh"), ("Stop",))
    assert not any(l == FAIL and "not wired" in h for l, h, _ in out)


def test_negative_threshold_warns():
    """A negative grace period is a cutoff in the FUTURE, not a shorter grace.
    doctor must not report it OK while the daemon ignores it."""
    out = doctor.check_env({"HERD_STRANDED_SECS": "-60"})
    assert WARN in _levels(out) and "HERD_STRANDED_SECS" in _text(out)


def test_zero_threshold_is_accepted():
    """Zero is a coherent 'no grace' choice, unlike a negative."""
    assert WARN not in _levels(doctor.check_env({"HERD_WAIT_SECS": "0"}))


# ── doctor must never raise: it runs on a machine that is already sick ───────
MALFORMED = [
    ({"hooks": {"SessionStart": [{"matcher": "Bash"}]}}, "block with no hooks key"),
    ({"hooks": {"SessionStart": [{"hooks": [{"type": "command"}]}]}}, "hook with no command"),
    ({"hooks": {"SessionStart": "not-a-list"}}, "event is a string"),
    ({"hooks": {"SessionStart": [None, 3, "x"]}}, "blocks are not dicts"),
    ({"hooks": "not-a-dict"}, "hooks is a string"),
    ({"hooks": {"SessionStart": [{"hooks": "not-a-list"}]}}, "hooks value is a string"),
    ({"statusLine": "a-bare-string"}, "statusLine is not an object"),
    ({"statusLine": []}, "statusLine is a list"),
]


@pytest.mark.parametrize("data,why", MALFORMED)
def test_a_malformed_settings_file_is_reported_not_raised(data, why):
    """`b["hooks"]` raised KeyError on a block carrying only a matcher — from the
    command you run to find out WHY the wiring looks wrong. install._strip_managed
    has always used .get() on this same structure."""
    text = json.dumps(data)
    w = doctor.check_wiring(text, ("/r",), ("/r/statusline.sh",), ("SessionStart",))
    m = doctor.check_hook_mode(text, "/installed", "/checkout")
    assert w and m, why
    assert all(lv in (OK, WARN, FAIL) for lv in _levels(w) + _levels(m)), why


def test_a_statusline_pointing_at_a_directory_is_reported(tmp_path):
    """The one file doctor opens whose contents it does not control. A directory,
    an unreadable file and a non-UTF-8 one all raised straight out of check_wiring."""
    d = tmp_path / "wrapdir"
    d.mkdir()
    r = doctor.check_wiring(json.dumps({"statusLine": {"command": str(d)}}),
                            ("/r",), ("/r/statusline.sh",), ())
    assert _levels(r) == [WARN] and "unreadable" in _text(r)


def test_a_binary_statusline_wrapper_is_reported(tmp_path):
    w = tmp_path / "wrapper.sh"
    w.write_bytes(b"\xff\xfe\x00not utf-8")
    r = doctor.check_wiring(json.dumps({"statusLine": {"command": str(w)}}),
                            ("/r",), ("/r/statusline.sh",), ())
    assert _levels(r) == [WARN] and "unreadable" in _text(r)


def test_a_binary_hook_error_log_is_reported(tmp_path):
    """check_errlog caught OSError but not UnicodeDecodeError — and a statusline
    killed mid-write (which happens as a matter of course) can leave partial bytes."""
    p = tmp_path / "err.log"
    p.write_bytes(b"\xff\xfe\x00binary")
    r = doctor.check_errlog(str(p))
    assert _levels(r) == [WARN] and "unreadable" in _text(r)


def test_an_unreadable_settings_file_is_named_once(tmp_path):
    """collect() read it with no guard at all (PermissionError, verified). And the
    finding must REPLACE the two checks that parse it — both would say 'missing',
    which is a different problem with a different fix."""
    s = tmp_path / "settings.json"
    s.write_text("{}")
    s.chmod(0o000)
    try:
        wiring = dict(doctor.collect(environ={}, settings_path=str(s)))["wiring"]
    finally:
        s.chmod(0o644)
    assert _levels(wiring) == [FAIL]
    assert "unreadable" in _text(wiring) and "missing" not in _text(wiring)


def test_a_check_that_crashes_becomes_a_finding(monkeypatch):
    """The property, independent of having thought of every input: one bad check
    must not cost the user the other five sections."""
    def boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(doctor, "check_db", boom)
    sections = dict(doctor.collect(environ={}))
    assert _levels(sections["database"]) == [FAIL]
    assert "kaboom" in _text(sections["database"])
    assert sections["daemon"] and sections["environment"]     # the rest still ran


# ── argv ────────────────────────────────────────────────────────────────────
def test_doctor_refuses_unknown_argv(capsys):
    """`herd doctor --json` silently ran a full text diagnostic."""
    assert doctor.main(["--json"]) == 2
    assert "usage" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_doctor_help_does_not_diagnose(flag, monkeypatch, capsys):
    monkeypatch.setattr(doctor, "collect",
                        lambda *a, **k: pytest.fail("--help must not run the checks"))
    assert doctor.main([flag]) == 0
    assert "usage" in capsys.readouterr().out


# ── the env knobs doctor claims to report ───────────────────────────────────
def test_the_daemon_log_cap_is_checked_too(monkeypatch):
    """The list had drifted from daemon._int_env's, so a malformed
    HERD_DAEMON_LOG_MAX — which the daemon does reject — was reported by nobody."""
    r = doctor.check_env({"HERD_DAEMON_LOG_MAX": "big", "HERD_ERRLOG_MAX": "-1"})
    assert _levels(r) == [WARN, WARN]
    assert "HERD_DAEMON_LOG_MAX" in _text(r) and "HERD_ERRLOG_MAX" in _text(r)


def test_a_long_claude_name_is_flagged():
    """ps -o comm= is capped at 15 chars, so a longer name cannot match what the
    reaper observes. It truncates to compare now, hence WARN not FAIL."""
    r = doctor.check_env({"HERD_CLAUDE_NAME": "claude-code-node-runner"})
    assert _levels(r) == [WARN] and "claude-code-nod" in _text(r)
    assert _levels(doctor.check_env({"HERD_CLAUDE_NAME": "claude"})) == [OK]


def test_attention_off_is_reported():
    r = doctor.check_env({"HERD_ATTENTION": "0"})
    assert _levels(r) == [WARN] and "core-only" in _text(r)
    assert _levels(doctor.check_env({"HERD_ATTENTION": "1"})) == [OK]   # the default


# ── the config file, and whether the DAEMON actually got it ──────────────────
def test_config_check_is_quiet_without_a_file(tmp_path):
    assert doctor.check_config({}, tmp_path / "none") == []


def test_config_check_reports_a_key_the_environment_overrides(tmp_path):
    """Not an error — a one-off override is a feature, and the systemd unit setting
    HERD_DB is normal. But never silent: the file says one thing and the process is
    doing another, which is the whole class of bug the file replaced."""
    p = tmp_path / "config"
    p.write_text("HERD_WAIT_SECS=90\n")
    out = doctor.check_config({"HERD_WAIT_SECS": "7"}, p, daemon_env={})
    assert any(lvl == doctor.WARN and "overridden" in head for lvl, head, _ in out)


def test_config_check_fails_when_the_running_daemon_lacks_the_key(tmp_path):
    """THE check. The daemon is a different process with a different environment, so
    inference from this one proves nothing — a config file edited but never picked
    up looks identical to one being obeyed until you read /proc."""
    p = tmp_path / "config"
    p.write_text("HERD_CLAUDE_NAME=myclaude\n")
    out = doctor.check_config({}, p, daemon_env={"HERD_DB": "/x"})
    assert any(lvl == doctor.FAIL and "does not have HERD_CLAUDE_NAME" in head
               for lvl, head, _ in out)


def test_config_check_warns_when_the_daemon_disagrees(tmp_path):
    p = tmp_path / "config"
    p.write_text("HERD_CLAUDE_NAME=myclaude\n")
    out = doctor.check_config({}, p, daemon_env={"HERD_CLAUDE_NAME": "claude"})
    assert any(lvl == doctor.WARN and "different HERD_CLAUDE_NAME" in head
               for lvl, head, _ in out)


def test_config_check_passes_when_daemon_and_file_agree(tmp_path):
    p = tmp_path / "config"
    p.write_text("HERD_WAIT_SECS=90\n")
    out = doctor.check_config({}, p, daemon_env={"HERD_WAIT_SECS": "90"})
    assert [lvl for lvl, _, _ in out] == [doctor.OK]


def test_daemon_environ_is_none_when_nothing_holds_the_lock(tmp_path):
    """Not running, not Linux, or another user's daemon: the check stays quiet
    rather than guessing. A doctor that invents findings is worse than one that
    admits it cannot see."""
    assert doctor._daemon_environ(lock_path=tmp_path / "no-lock") is None


def test_daemon_environ_parses_proc_environ(tmp_path):
    lock = tmp_path / "lock"
    lock.write_text("4242\n")
    env = doctor._daemon_environ(lock_path=lock,
                                read=lambda pid: b"HERD_DB=/x\x00HERD_WAIT_SECS=90\x00")
    assert env == {"HERD_DB": "/x", "HERD_WAIT_SECS": "90"}
