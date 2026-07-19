"""kitty remote control: the one thing herd needs that it cannot turn on itself.

Without `allow_remote_control` + `listen_on`, KITTY_LISTEN_ON is unset, every
session records an empty placement and spawn/jump cannot work — and nothing about
that failure points at kitty.conf. These cover the three parts that close the gap:
the state read, the block herd writes into a file it does not own, and doctor's
report.
"""
import subprocess

import pytest

from herd import doctor, install
from herd.doctor import OK, WARN, FAIL
from herd.kitty import config


def _levels(results):
    return [r[0] for r in results]


def _text(results):
    return " ".join(f"{h} {d}" for _, h, d in results)


# ── state: which of the three worlds ────────────────────────────────────────
def test_state_reads_the_environment_not_the_config_file():
    """KITTY_WINDOW_ID is what separates 'not in kitty' from 'in kitty, remote
    control off'. kitty exports it in every window regardless of remote control, so
    without it those two look identical and the check has to either cry wolf in
    every xterm or stay silent in the one place it matters."""
    assert config.state({"KITTY_LISTEN_ON": "unix:/tmp/kitty-1"}) == config.READY
    assert config.state({"KITTY_WINDOW_ID": "3"}) == config.OFF
    assert config.state({}) == config.NOT_KITTY
    # LISTEN_ON wins: a socket is proof, whatever else is or isn't set.
    assert config.state({"KITTY_LISTEN_ON": "unix:/x", "KITTY_WINDOW_ID": "3"}) \
        == config.READY
    assert config.state({"KITTY_LISTEN_ON": ""}) == config.NOT_KITTY   # empty != set


# ── the block: herd writes into a file it does not own ──────────────────────
@pytest.mark.parametrize("original", [
    "",                                     # kitty.conf does not exist yet
    "map f1 launch\n",                      # the ordinary case
    "a\n\n\n",                              # trailing blank lines the USER wrote
    "# BEGIN_KITTY_THEME\ninclude x.conf\n# END_KITTY_THEME\n",   # kitty's own markers
])
def test_add_then_strip_restores_the_file_byte_for_byte(original):
    """The round trip is the whole safety argument for touching kitty.conf at all.
    An early version rstrip()'d the head and rebuilt it, which silently ate trailing
    blank lines the user had written — a file herd does not own, reformatted by a
    tool that was only asked to add two options."""
    added = config.add_block(original)
    assert config.has_block(added)
    assert config.strip_block(added) == original


def test_add_block_is_idempotent():
    """Re-running the installer must not stack duplicate blocks."""
    once = config.add_block("map f1 launch\n")
    assert config.add_block(once) == once
    assert once.count(config.BEGIN) == 1


def test_strip_leaves_a_file_herd_never_touched_alone():
    """The uninstall guarantee: someone who declined the offer, or configured kitty
    by hand years ago, gets their file back untouched — not reformatted."""
    hand_written = "allow_remote_control yes\nlisten_on unix:/tmp/kitty\n"
    assert config.strip_block(hand_written) == hand_written


def test_the_block_carries_both_options_and_a_unique_socket():
    """{kitty_pid} is not cosmetic: a window id means nothing without the socket it
    came from, so a fixed socket path lets two kitty instances hand out colliding
    ids (schema/herd.sql — '(socket, window_id) is the whole jump key')."""
    assert "allow_remote_control yes" in config.BLOCK
    assert "listen_on unix:/tmp/kitty-{kitty_pid}" in config.BLOCK
    assert config.BLOCK.startswith(config.BEGIN) and config.BLOCK.endswith(config.END)


def test_add_block_appends_at_the_end():
    """kitty is last-wins, so the block has to sit after any earlier
    `allow_remote_control no` or it would be silently overridden."""
    out = config.add_block("allow_remote_control no\n")
    assert out.index("allow_remote_control no") < out.index(config.BEGIN)


# ── doctor's report ─────────────────────────────────────────────────────────
_HAVE_KITTEN = lambda b: f"/usr/bin/{b}"        # noqa: E731


class _P:
    def __init__(self, returncode=0, stdout="[]", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def test_no_kitten_says_nothing_at_all():
    """check_deps already WARNs a missing kitten; two lines for one cause is noise
    (the rule check_jq_version set)."""
    assert doctor.check_kitty({}, which=lambda b: None) == []


def test_outside_kitty_is_not_a_warning():
    """Unverifiable is not broken. A check that cries wolf where it cannot see
    teaches people to ignore it — and doctor gets run over ssh and in CI."""
    out = doctor.check_kitty({}, which=_HAVE_KITTEN)
    assert _levels(out) == [OK]
    assert "not running inside kitty" in _text(out)


def test_in_kitty_without_a_socket_names_both_options_and_the_restart():
    """THE case this whole change exists for. It must be actionable on its own:
    both option names, the file, and the fact that a config reload will not do."""
    out = doctor.check_kitty({"KITTY_WINDOW_ID": "3"}, which=_HAVE_KITTEN)
    assert _levels(out) == [WARN]
    txt = _text(out)
    assert "allow_remote_control yes" in txt
    assert "listen_on unix:/tmp/kitty-{kitty_pid}" in txt
    assert "restart kitty" in txt


def test_a_working_socket_is_reported_ok():
    out = doctor.check_kitty({"KITTY_LISTEN_ON": "unix:/tmp/kitty-1"},
                             which=_HAVE_KITTEN, run=lambda: _P())
    assert _levels(out) == [OK]


@pytest.mark.parametrize("boom", [
    OSError("no such file"),
    subprocess.TimeoutExpired("kitten", 5),
])
def test_a_socket_that_cannot_be_reached_warns_with_the_reason(boom):
    """focus._ls() collapses timeout, missing binary and dead socket into "" — which
    is right for jumping and useless for diagnosing, so this probes separately."""
    def run():
        raise boom
    out = doctor.check_kitty({"KITTY_LISTEN_ON": "unix:/tmp/kitty-1"},
                             which=_HAVE_KITTEN, run=run)
    assert _levels(out) == [WARN] and "unreachable" in _text(out)


def test_a_refused_socket_reports_kittens_own_words():
    """The stale-socket case: KITTY_LISTEN_ON points at a kitty that is gone, or at
    one with remote control off. kitten's stderr says which; inventing our own
    wording would send someone looking for the wrong thing."""
    out = doctor.check_kitty(
        {"KITTY_LISTEN_ON": "unix:/tmp/kitty-1"}, which=_HAVE_KITTEN,
        run=lambda: _P(returncode=1, stderr="Error: remote control is not enabled"))
    assert _levels(out) == [WARN]
    assert "remote control is not enabled" in _text(out)


def test_kitty_never_fails_only_warns():
    """kitten/fzf are OPTIONAL and herd records sessions fine without kitty — only
    placement, spawn and jump are lost. A FAIL would make `herd doctor` exit 1 on a
    machine that is working as designed."""
    for environ in ({}, {"KITTY_WINDOW_ID": "3"},
                    {"KITTY_LISTEN_ON": "unix:/x"}):
        out = doctor.check_kitty(environ, which=_HAVE_KITTEN,
                                 run=lambda: _P(returncode=1))
        assert FAIL not in _levels(out)


def test_doctor_has_a_kitty_section():
    sections = dict((name, r) for name, r in doctor.collect())
    assert "kitty" in sections
    assert "kitty" in doctor.USAGE


# ── the installer's offer ───────────────────────────────────────────────────
def test_the_installer_only_writes_when_it_KNOWS_the_config_is_missing():
    """Never on a guess. `not-kitty` means we cannot see the config at all, and
    `ready` means the options are already on however they got there — appending in
    either case edits a working file for no reason."""
    assert install._kitty_decision(config.OFF, False, "y") is True
    assert install._kitty_decision(config.OFF, False, "yes") is True
    assert install._kitty_decision(config.OFF, False, "") is False      # bare enter
    assert install._kitty_decision(config.OFF, False, "n") is False
    assert install._kitty_decision(config.OFF, True, "y") is False      # already there
    assert install._kitty_decision(config.NOT_KITTY, False, "y") is False
    assert install._kitty_decision(config.READY, False, "y") is False


def test_an_already_configured_kitty_is_left_alone(monkeypatch, tmp_path):
    """The case on any machine that was set up by hand — including the one herd was
    built on, whose kitty.conf has both options outside any herd block."""
    conf = tmp_path / "kitty.conf"
    conf.write_text("allow_remote_control yes\nlisten_on unix:/tmp/kitty\n")
    monkeypatch.setattr(config, "KITTY_CONF", conf)
    note = install._offer_kitty(environ={"KITTY_LISTEN_ON": "unix:/tmp/kitty-1"})
    assert "already enabled" in note
    assert conf.read_text() == "allow_remote_control yes\nlisten_on unix:/tmp/kitty\n"


def test_accepting_appends_the_block_and_backs_the_file_up(monkeypatch, tmp_path):
    conf = tmp_path / "kitty.conf"
    conf.write_text("map f1 launch\n")
    monkeypatch.setattr(config, "KITTY_CONF", conf)
    note = install._offer_kitty(environ={"KITTY_WINDOW_ID": "3"}, answer="y")
    assert config.has_block(conf.read_text())
    assert "map f1 launch" in conf.read_text()          # kept what was there
    assert list(tmp_path.glob("kitty.conf.herd-bak.*"))  # backed up first
    assert "restart kitty" in note


def test_declining_writes_nothing_but_still_explains(monkeypatch, tmp_path):
    conf = tmp_path / "kitty.conf"
    conf.write_text("map f1 launch\n")
    monkeypatch.setattr(config, "KITTY_CONF", conf)
    note = install._offer_kitty(environ={"KITTY_WINDOW_ID": "3"}, answer="n")
    assert conf.read_text() == "map f1 launch\n"
    assert "allow_remote_control yes" in note           # the tip, so it isn't a dead end


def test_a_non_tty_never_blocks_on_input(monkeypatch, tmp_path):
    """The installer runs in pipes and CI. _offer_bell degrades to a printed tip on
    a non-tty; this must too, or the install hangs forever waiting on stdin."""
    conf = tmp_path / "kitty.conf"
    conf.write_text("map f1 launch\n")
    monkeypatch.setattr(config, "KITTY_CONF", conf)
    monkeypatch.setattr(install.sys.stdin, "isatty", lambda: False)
    note = install._offer_kitty(environ={"KITTY_WINDOW_ID": "3"})
    assert conf.read_text() == "map f1 launch\n"
    assert "allow_remote_control yes" in note


def test_dry_run_touches_nothing(monkeypatch, tmp_path):
    conf = tmp_path / "kitty.conf"
    conf.write_text("map f1 launch\n")
    monkeypatch.setattr(config, "KITTY_CONF", conf)
    note = install._offer_kitty(dry=True, environ={"KITTY_WINDOW_ID": "3"})
    assert conf.read_text() == "map f1 launch\n"
    assert note.startswith("would")


def test_uninstall_removes_only_herds_block(monkeypatch, tmp_path):
    conf = tmp_path / "kitty.conf"
    original = "map f1 launch\n\n# BEGIN_KITTY_THEME\ninclude x.conf\n# END_KITTY_THEME\n"
    conf.write_text(original)
    monkeypatch.setattr(config, "KITTY_CONF", conf)
    install._offer_kitty(environ={"KITTY_WINDOW_ID": "3"}, answer="y")
    assert config.has_block(conf.read_text())
    install._uninstall_kitty(install._ts())
    assert conf.read_text() == original


def test_uninstall_leaves_a_kitty_conf_herd_never_touched_byte_identical(
        monkeypatch, tmp_path):
    """Someone who declined the offer, or who configured kitty by hand, must not
    have their terminal config rewritten by `--uninstall`."""
    conf = tmp_path / "kitty.conf"
    original = "allow_remote_control yes\n\n\n# my notes\n"
    conf.write_text(original)
    monkeypatch.setattr(config, "KITTY_CONF", conf)
    note = install._uninstall_kitty(install._ts())
    assert conf.read_text() == original
    assert "untouched" in note
    assert not list(tmp_path.glob("kitty.conf.herd-bak.*"))   # no pointless backup


def test_uninstall_with_no_kitty_conf_at_all_is_a_no_op(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "KITTY_CONF", tmp_path / "nope.conf")
    assert "no kitty.conf" in install._uninstall_kitty(install._ts())
