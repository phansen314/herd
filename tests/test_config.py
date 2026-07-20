"""C — ~/.herd/config: the settings file the daemon and the hooks BOTH read.

The file exists to end a DIVERGENCE, not to add a feature: the hooks inherit your
shell and the systemd daemon does not, so a setting exported in .bashrc reached one
and not the other. The parser tests are the cheap half; the two end-to-end tests at
the bottom are the point.
"""
import json
import os
import pathlib
import subprocess
import sys

import pytest

from herd import config as cfg

from helpers import HOOKS

SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
SCHEMA = SRC / "herd" / "schema"


def _write(tmp_path, text):
    p = tmp_path / "config"
    p.write_text(text)
    return p


def test_parse_reads_values_and_ignores_comments_and_blanks(tmp_path):
    vals, probs = cfg.parse("# a comment\n\nHERD_WAIT_SECS=90\n\n#HERD_STUCK_SECS=1\n")
    assert vals == {"HERD_WAIT_SECS": "90"}          # the commented one stays off
    assert probs == []


def test_parse_tolerates_export_and_stray_whitespace(tmp_path):
    """`export FOO=bar` is what muscle memory types into a file like this. Binding a
    key literally named "export FOO" would be a silent no-op — the exact failure the
    config file replaced."""
    vals, probs = cfg.parse("  export HERD_CLAUDE_NAME = myclaude  \n")
    assert vals == {"HERD_CLAUDE_NAME": "myclaude"} and probs == []


def test_parse_reports_a_typo_instead_of_swallowing_it(tmp_path):
    """A misspelled knob that stays quiet is indistinguishable from one being obeyed."""
    vals, probs = cfg.parse("HERD_WAIT_SEC=90\nHERD_STUCK_SECONDS=5\n")
    assert vals == {}
    assert len(probs) == 2 and "HERD_WAIT_SEC" in probs[0]


def test_parse_reports_a_line_with_no_equals(tmp_path):
    vals, probs = cfg.parse("HERD_WAIT_SECS 90\n")
    assert vals == {} and "no '='" in probs[0]


def test_a_repeated_key_is_reported_and_the_later_one_wins(tmp_path):
    vals, probs = cfg.parse("HERD_WAIT_SECS=1\nHERD_WAIT_SECS=2\n")
    assert vals["HERD_WAIT_SECS"] == "2" and "set twice" in probs[0]


def test_a_missing_file_is_silent(tmp_path):
    """The normal case. No file is not a problem worth a line of output."""
    assert cfg.load(tmp_path / "nope") == ({}, [])


def test_an_unreadable_file_complains_but_does_not_raise(tmp_path):
    """This is read on the import path of every herd command, so a mangled or
    permission-denied file must degrade to a complaint, never a traceback that
    takes out `herd ls`."""
    p = _write(tmp_path, "HERD_WAIT_SECS=90\n")
    p.chmod(0o000)
    try:
        vals, probs = cfg.load(p)
    finally:
        p.chmod(0o644)
    assert vals == {} and probs and "cannot read" in probs[0]


def test_apply_fills_only_what_the_environment_lacks(tmp_path):
    p = _write(tmp_path, "HERD_WAIT_SECS=90\nHERD_STUCK_SECS=45\n")
    env = {"HERD_WAIT_SECS": "7"}
    applied, shadowed, _ = cfg.apply(p, env)
    assert applied == {"HERD_STUCK_SECS": "45"}      # the gap it filled
    assert env["HERD_WAIT_SECS"] == "7"              # the environment still wins
    assert shadowed == {"HERD_WAIT_SECS": ("90", "7")}


def test_an_identical_value_in_both_places_is_not_reported_as_shadowed(tmp_path):
    """Only a DISAGREEMENT is worth a line. A test harness exporting the same value
    the file names is the common case, and flagging it would train people to ignore
    the warning that matters."""
    p = _write(tmp_path, "HERD_DB=/x/herd.db\n")
    _, shadowed, _ = cfg.apply(p, {"HERD_DB": "/x/herd.db"})
    assert shadowed == {}


# ── the divergence itself ────────────────────────────────────────────────────
def _fake_claude(tmp_path, name):
    """A live process whose comm is `name` — what a node-based install looks like
    to `ps`, and the reason HERD_CLAUDE_NAME exists."""
    exe = tmp_path / name
    exe.write_bytes(pathlib.Path("/bin/sleep").read_bytes())
    exe.chmod(0o755)
    return subprocess.Popen([str(exe), "600"])


def _db_with_session(tmp_path, pid):
    import sqlite3
    db = tmp_path / "herd.db"
    c = sqlite3.connect(db)
    for f in ("core.sql", "herd.sql"):
        c.executescript((SCHEMA / f).read_text())
    c.execute("INSERT INTO sessions(session_id,cwd,model,pid,status,status_source,"
              "last_event_at,last_event_type,started_at,updated_at) VALUES"
              "('s1','/x','m',?,'working','hook','2026-07-15T10:00:00.000Z','tool',"
              "'2026-07-15T10:00:00.000Z','2026-07-15T10:00:00.000Z')", (pid,))
    c.commit()
    c.close()
    return db


@pytest.mark.shell
def test_the_config_file_stops_the_reaper_killing_a_renamed_claude(tmp_path):
    """THE bug this file exists for, end to end through the real daemon.

    HERD_CLAUDE_NAME exported in a shell reached the hooks (children of that shell)
    and NOT the systemd daemon, which inherits nothing. The hooks stored a valid pid;
    the reaper compared its comm against its own default `claude`, read the mismatch
    as a recycled pid, and stopped EVERY live session on the first tick. Setting it
    in the config file has to reach the daemon, because the daemon reads the file.
    """
    proc = _fake_claude(tmp_path, "myclaude")
    try:
        db = _db_with_session(tmp_path, proc.pid)
        cfgp = _write(tmp_path, "HERD_CLAUDE_NAME=myclaude\n")
        env = dict(os.environ, PYTHONPATH=str(SRC), HERD_DB=str(db),
                   HERD_RUNTIME=str(tmp_path), HERD_ATTENTION="0",
                   HOME=str(tmp_path))
        env.pop("HERD_CLAUDE_NAME", None)            # the shell does NOT have it
        env["HERD_CONFIG"] = str(cfgp)
        subprocess.run([sys.executable, "-m", "herd.daemon", "--once"],
                       env=env, capture_output=True, text=True, timeout=60)
        import sqlite3
        c = sqlite3.connect(db)
        status, stopped = c.execute(
            "SELECT status, stopped_at FROM sessions WHERE session_id='s1'").fetchone()
        c.close()
        assert (status, stopped) == ("working", None), \
            "the reaper stopped a live claude it should have recognised"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.mark.shell
def test_the_hooks_read_the_same_file(tmp_path):
    """The other half. A file only the daemon reads would leave the divergence in
    place — it would just move which side is wrong. Uses HERD_DB because it is the
    one setting whose effect a hook makes directly visible: the row lands in the
    database the FILE names, with nothing in the environment saying so."""
    db = tmp_path / "from-config.db"
    import sqlite3
    c = sqlite3.connect(db)
    for f in ("core.sql", "herd.sql"):
        c.executescript((SCHEMA / f).read_text())
    c.commit()
    c.close()
    cfgp = _write(tmp_path, f"HERD_DB={db}\n")
    # HOME is the SAFETY NET, not the subject. HERD_DB stays unset because the file
    # supplying it is the whole point — but if the config loader ever stops working,
    # common.sh falls back to $HOME/.herd/herd.db, and with the real HOME that is the
    # developer's live database. It reached it: verifying this test "fails without the
    # fix" meant deleting the loader, which is exactly the isolation the test relies
    # on, and session_start.sh wrote a row into the real herd — where the hook's pid
    # ancestry walk then made W2c_pid_claim reap the live session that owned that pid.
    # A test whose sandbox depends on the code under test has no sandbox.
    env = dict(os.environ, HERD_RUNTIME=str(tmp_path), HERD_CONFIG=str(cfgp),
               HERD_ERRLOG=str(tmp_path / "err.log"), HOME=str(tmp_path))
    env.pop("HERD_DB", None)                          # nothing in the environment
    subprocess.run(["bash", str(HOOKS / "session_start.sh")],
                   input=json.dumps({"session_id": "cfg-1", "cwd": "/x",
                                     "model": {"id": "m"}, "transcript_path": "/t"}),
                   env=env, capture_output=True, text=True, timeout=60)
    c = sqlite3.connect(db)
    got = c.execute("SELECT session_id FROM sessions").fetchall()
    c.close()
    assert got == [("cfg-1",)], "the hook did not honour HERD_DB from the config file"


def test_a_leading_tilde_expands(tmp_path):
    """The shipped template shows `#HERD_DB=~/.herd/herd.db`, and nothing else would
    expand it: this file is not read by a shell, and common.sh assigns the value
    quoted. Uncommenting that line would have pointed the database at a directory
    literally named "~" — and since the unit no longer sets HERD_DB, that file is
    now the only thing naming it."""
    vals, _ = cfg.parse("HERD_DB=~/.herd/x.db\nHERD_TEMPLATES=~\n")
    assert vals["HERD_DB"] == os.path.expanduser("~/.herd/x.db")
    assert vals["HERD_TEMPLATES"] == os.path.expanduser("~")


def test_a_tilde_not_at_the_start_is_left_alone(tmp_path):
    """Only a LEADING ~ is a home reference. A path that merely contains one is a
    real path, and rewriting it would be the surprise."""
    vals, _ = cfg.parse("HERD_DB=/srv/~backup/herd.db\n")
    assert vals["HERD_DB"] == "/srv/~backup/herd.db"


@pytest.mark.shell
def test_bash_and_python_expand_the_tilde_the_same_way(tmp_path, monkeypatch):
    """Two parsers, one file: a rule only one side implements is the divergence this
    file exists to end. BOTH sides get the same HOME — pointing only the subprocess
    at tmp_path made them disagree for a reason that had nothing to do with the rule
    under test."""
    p = _write(tmp_path, "HERD_DB=~/.herd/x.db\n")
    monkeypatch.setenv("HOME", str(tmp_path))
    env = dict(os.environ, HERD_CONFIG=str(p), HOME=str(tmp_path))
    env.pop("HERD_DB", None)
    got = subprocess.run(
        ["bash", "-c", f'. {HOOKS / "common.sh"}; printf "%s" "$HERD_DB"'],
        env=env, capture_output=True, text=True, timeout=60).stdout
    expected = cfg.parse("HERD_DB=~/.herd/x.db\n")[0]["HERD_DB"]
    assert got == expected == str(tmp_path / ".herd" / "x.db")


@pytest.mark.shell
def test_the_daemon_uses_the_database_the_config_file_names(tmp_path):
    """The unit sets no HERD_DB any more, so this file is authoritative for it. If
    the daemon ignored it, it would silently open (and CREATE) a different database
    and reap nothing in the one herd actually uses."""
    import sqlite3
    proc = _fake_claude(tmp_path, "claude")
    try:
        db = _db_with_session(tmp_path, proc.pid)
        cfgp = _write(tmp_path, f"HERD_DB={db}\n")
        env = dict(os.environ, PYTHONPATH=str(SRC), HERD_RUNTIME=str(tmp_path),
                   HERD_CONFIG=str(cfgp), HERD_ATTENTION="0", HOME=str(tmp_path))
        env.pop("HERD_DB", None)                      # nothing in the environment
        subprocess.run([sys.executable, "-m", "herd.daemon", "--once"],
                       env=env, capture_output=True, text=True, timeout=60)
        # it opened THIS database: the live session is still live, and no stray
        # herd.db appeared next to it
        c = sqlite3.connect(db)
        status = c.execute("SELECT status FROM sessions WHERE session_id='s1'").fetchone()[0]
        c.close()
        assert status == "working"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
