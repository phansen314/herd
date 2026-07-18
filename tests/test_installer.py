"""P (64-66d) — installer surgery on settings.json / the statusline wrapper, the
systemd unit, CLI paths, and the terminal-bell opt-in. Pure functions."""
import json
import os
import pathlib

import pytest

from herd import install as inst

KLAWDE_CFG = {"hooks": {
    "PreToolUse":  [{"matcher": ".*", "hooks": [{"type": "http", "url": "http://localhost:8765/x", "timeout": 5}]}],
    "SessionStart": [{"hooks": [{"type": "command", "command": "/h/.klawde/session_start.sh"},
                                {"type": "command", "command": "/h/.klawde/kitty_start.sh", "async": True}]}],
    "SessionEnd":  [{"hooks": [{"type": "command", "command": "/h/.klawde/session_end.sh", "async": True}]}],
    "Notification": [{"hooks": [{"type": "command", "command": "/h/.klawde/notification.sh", "async": True}]}],
    "PostToolUse": [{"hooks": [{"type": "command", "command": "/h/.klawde/post_tool_use.sh", "async": True}]},
                    {"hooks": [{"type": "command", "command": "cdh-claude-hook postToolUse", "async": True}]}],
}}


def _cmds(d, e):
    return [h.get("command", "") for b in d["hooks"].get(e, []) for h in b["hooks"]]


def _async(d, e):
    return [h.get("async", False) for b in d["hooks"].get(e, []) for h in b["hooks"]]


def test_rewire_preserves_foreign_replaces_klawde_fixes_async():
    out = inst.rewire_settings(KLAWDE_CFG)
    assert any("cdh-claude-hook" in c for c in _cmds(out, "PostToolUse"))          # cdh preserved
    assert any(h.get("type") == "http" for b in out["hooks"]["PreToolUse"] for h in b["hooks"])  # HTTP preserved
    assert not any("/.klawde/" in c for e in out["hooks"] for c in _cmds(out, e))  # klawde gone
    assert _async(out, "SessionEnd") == [False]                                    # async bug fixed
    assert _async(out, "SessionStart") == [False]                                  # blocking
    assert any("stop.sh" in c for c in _cmds(out, "Stop")) and _async(out, "Stop") == [True]
    assert not any("kitty_start" in c for e in out["hooks"] for c in _cmds(out, e))
    assert len(_cmds(out, "SessionStart")) == 1


def test_rewire_is_idempotent():
    once = inst.rewire_settings(KLAWDE_CFG)
    assert once == inst.rewire_settings(once)


def test_wrapper_swap_is_idempotent():
    w0 = 'CAV=$(bash caveman)\nprintf "%s ┃ " "$CAV"\n"$HOME/.klawde/statusline.sh"\n'
    w1, rep = inst.rewire_wrapper(w0)
    w2, _ = inst.rewire_wrapper(w1)
    assert rep and ".klawde/statusline.sh" not in w1 and inst.STATUSLINE in w1 and w1 == w2


def test_service_unit_is_well_formed():
    u = inst.service_unit_text()
    assert "-m herd.daemon" in u
    assert f"Environment=PYTHONPATH={inst.PKG_SRC}" in u
    assert f"Environment=HERD_DB={inst.DB}" in u
    assert "WantedBy=default.target" in u
    assert inst.PKG_SRC.name == "src"


def _section(unit, name):
    """The keys under [name]. Directives are section-scoped and systemd IGNORES a
    misplaced one with only a log line, so asserting on the whole file is not
    enough — StartLimitIntervalSec in [Service] shipped exactly that way."""
    body = unit.split(f"[{name}]", 1)[1]
    body = body.split("\n[", 1)[0]
    return dict(ln.split("=", 1) for ln in body.strip().splitlines() if "=" in ln)


def test_service_unit_retries_forever():
    """The daemon is the only reaper of silent deaths, so a stopped unit is
    invisible: sessions just never leave `herd ls`. on-failure ignores a clean
    exit, and the default start limit latches a persistently-failing unit into
    `failed` — both leave nothing reaping with nothing to notice."""
    u = inst.service_unit_text()
    assert _section(u, "Service")["Restart"] == "always"
    # StartLimitIntervalSec is a [Unit] directive. In [Service] systemd logs
    # "Unknown key name ... ignoring" and keeps the 10s default — the fix looks
    # applied and does nothing.
    assert _section(u, "Unit")["StartLimitIntervalSec"] == "0"
    assert "StartLimitIntervalSec" not in _section(u, "Service")


def test_cli_paths_resolve_and_completion_ships():
    assert inst.CLI_SRC.name == "herd" and inst.CLI_SRC.parent.name == "bin" and inst.CLI_SRC.exists()
    assert inst.CLI_LINK == pathlib.Path.home() / ".local" / "bin" / "herd"
    assert inst.COMPLETION_SRC.exists()
    assert "readlink" in inst.CLI_SRC.read_text()   # wrapper dereferences the PATH symlink


@pytest.mark.parametrize("current,answer,expect", [
    ("desktop", "y", None),            # existing choice untouched
    (None, "y", "terminal_bell"),
    (None, "yes", "terminal_bell"),
    (None, "n", None),
    (None, "", None),
])
def test_bell_decision(current, answer, expect):
    assert inst._bell_decision(current, answer) == expect


# ── selftest(): the installer's own proof that the wiring works ──────────────
def test_selftest_passes_against_the_real_hooks():
    """It direct-execs the shipped scripts (not `bash <path>`), so this also covers
    the +x bit the way production hits it."""
    from herd import install as I
    ok, detail = I.selftest(I.HOOKS_DIR)
    assert ok, detail
    assert detail["status"] == "working" and detail["context_percent"] == 10


def test_selftest_reports_a_missing_executable_bit(monkeypatch, tmp_path):
    """The failure it exists to catch: a hook without +x is a silent no-op under
    settings.json, which is how a blank statusline once shipped."""
    from herd import install as I
    fake = tmp_path / "hooks"
    fake.mkdir()
    for name in ("session_start.sh", "statusline.sh"):
        p = fake / name
        p.write_text("#!/usr/bin/env bash\nexit 0\n")
        p.chmod(0o644)                      # readable, NOT executable
    ok, detail = I.selftest(fake)
    assert not ok
    assert sorted(detail["not_executable"]) == ["session_start.sh", "statusline.sh"]


def test_selftest_leaves_the_real_db_alone(tmp_path, monkeypatch):
    """It must use a throwaway DB — a selftest that wrote to ~/.herd/herd.db would
    inject a fake session into the user's live list on every install."""
    from herd import install as I
    real = tmp_path / "herd.db"
    monkeypatch.setattr(I, "DB", real)
    I.selftest(I.HOOKS_DIR)
    assert not real.exists()


# ── the side-effecting half: install()/uninstall() against a temp HOME ──────
# These were 0%-covered, which is how the double-install uninstall trap survived.
@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point every path install() writes at a temp dir, and stub the side effects
    that reach outside it (systemd, PATH symlinks, the interactive bell prompt)."""
    monkeypatch.setattr(inst, "SETTINGS", tmp_path / "settings.json")
    monkeypatch.setattr(inst, "WRAPPER", tmp_path / "custom-status-line.sh")
    monkeypatch.setattr(inst, "HERD_DIR", tmp_path)
    monkeypatch.setattr(inst, "DB", tmp_path / "herd.db")
    # sync_hooks() COPIES into these — unpatched, the suite writes to the real ~/.herd
    monkeypatch.setattr(inst, "INSTALLED_HOOKS", tmp_path / "hooks")
    monkeypatch.setattr(inst, "INSTALLED_SCHEMA", tmp_path / "schema")
    monkeypatch.setattr(inst, "install_service", lambda dry=False: "service stubbed")
    monkeypatch.setattr(inst, "uninstall_service", lambda: "service stubbed")
    monkeypatch.setattr(inst, "install_cli", lambda dry=False: "cli stubbed")
    monkeypatch.setattr(inst, "uninstall_cli", lambda: "cli stubbed")
    monkeypatch.setattr(inst, "_offer_bell", lambda s: "bell stubbed")
    ts = ["20260101-000000", "20260202-000000", "20260303-000000"]
    monkeypatch.setattr(inst, "_ts", lambda: ts.pop(0))
    return tmp_path


PRISTINE = {"model": "opus", "hooks": {"PreToolUse": [
    {"matcher": "Bash", "hooks": [{"type": "command", "command": "/opt/audit.sh"}]}]}}


def _wired(d, hooks_dir=None):
    """Wired to the dir install() targets — the installed COPY by default."""
    root = str(hooks_dir or inst.INSTALLED_HOOKS)
    return any(root in h.get("command", "")
               for bs in d["hooks"].values() for b in bs for h in b["hooks"])


def test_uninstall_reverses_a_repeated_install(home, capsys):
    """The trap: install #2 backs up the ALREADY-WIRED file, so restoring the newest
    backup reinstates herd and reports success. README calls re-installing the fix
    for two troubleshooting entries, so the second install is the common path."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    inst.install(); inst.install()
    assert _wired(json.loads(inst.SETTINGS.read_text()))     # precondition
    inst.uninstall()
    assert json.loads(inst.SETTINGS.read_text()) == PRISTINE
    assert not _wired(json.loads(inst.SETTINGS.read_text()))


def test_the_original_backup_is_never_overwritten(home):
    """Every later install must leave the pre-herd snapshot alone — it is the only
    copy that predates herd, and uninstall restores from it."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    inst.install()
    orig = inst.SETTINGS.with_name(inst.SETTINGS.name + inst.ORIGINAL_SUFFIX)
    assert json.loads(orig.read_text()) == PRISTINE
    inst.install(); inst.install()
    assert json.loads(orig.read_text()) == PRISTINE


def test_uninstall_migrates_a_legacy_double_install(home):
    """An install predating ORIGINAL_SUFFIX has no pristine snapshot — only
    timestamped backups. The OLDEST is the one install #1 took, i.e. pre-herd."""
    wired = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": str(inst.HOOKS_DIR / "stop.sh")}]}]}}
    (home / "settings.json.herd-bak.20250101-000000").write_text(json.dumps(PRISTINE))
    (home / "settings.json.herd-bak.20250202-000000").write_text(json.dumps(wired))
    inst.SETTINGS.write_text(json.dumps(wired))
    inst.uninstall()
    assert json.loads(inst.SETTINGS.read_text()) == PRISTINE


def test_a_failed_selftest_aborts_before_touching_settings(home, monkeypatch):
    """The self-test exists to catch hooks that are wired but silently no-op. Running
    it after the write meant reporting FAIL on a config already pointed at herd."""
    monkeypatch.setattr(inst, "selftest", lambda *a, **k: (False, {"not_executable": ["stop.sh"]}))
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    assert inst.install() == 1
    assert json.loads(inst.SETTINGS.read_text()) == PRISTINE
    assert not list(home.glob("settings.json.herd-bak.*"))
    assert inst.main([]) == 1                      # and the exit status carries it


def test_install_absorbs_a_write_that_lands_during_the_bell_prompt(home, monkeypatch):
    """_offer_bell blocks on input(); Claude Code writes settings.json (permission
    grants) meanwhile. A stale in-memory copy would clobber them."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")

    def racing_prompt(settings):
        d = json.loads(inst.SETTINGS.read_text())
        d["permissions"] = {"allow": ["Bash(ls:*)"]}       # granted during the prompt
        inst.SETTINGS.write_text(json.dumps(d) + "\n")
        return "bell stubbed"

    monkeypatch.setattr(inst, "_offer_bell", racing_prompt)
    inst.install()
    out = json.loads(inst.SETTINGS.read_text())
    assert out["permissions"] == {"allow": ["Bash(ls:*)"]}  # survived
    assert _wired(out)                                      # and herd still wired


def test_settings_are_written_atomically(home, monkeypatch):
    """A torn settings.json stops Claude Code from starting at all."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    real = inst.os.replace
    monkeypatch.setattr(inst.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("ENOSPC")))
    with pytest.raises(OSError):
        inst.install()
    assert json.loads(inst.SETTINGS.read_text()) == PRISTINE   # intact, not truncated
    monkeypatch.setattr(inst.os, "replace", real)
    assert not list(home.glob("*.herd-tmp.*"))                 # and no debris


# ── statusLine wiring ───────────────────────────────────────────────────────
# statusline.sh is the ONLY writer of every metric column, and it used to be wired
# solely by rewriting an existing custom-status-line.sh — so a machine without one
# got no statusline at all, while install still printed PASS.
def _sl(d):
    return d.get("statusLine", {}).get("command")


def test_fresh_machine_gets_the_statusline_wired_directly():
    out = inst.rewire_settings({}, wrapper_exists=False)
    assert _sl(out) == inst.STATUSLINE


def test_an_existing_wrapper_stays_in_front():
    """The wrapper is rewired to call herd, so the key should keep pointing at it
    rather than reaching around it."""
    cfg = {"statusLine": {"type": "command", "command": str(inst.WRAPPER)}}
    out = inst.rewire_settings(cfg, wrapper_exists=True)
    assert _sl(out) == str(inst.WRAPPER)


def test_a_dangling_wrapper_pointer_is_repointed_at_herd():
    cfg = {"statusLine": {"type": "command", "command": str(inst.WRAPPER)}}
    out = inst.rewire_settings(cfg, wrapper_exists=False)
    assert _sl(out) == inst.STATUSLINE


def test_klawdes_statusline_is_replaced_but_a_foreign_one_is_not():
    klawde = {"statusLine": {"type": "command", "command": "/h/.klawde/statusline.sh"}}
    assert _sl(inst.rewire_settings(klawde, wrapper_exists=False)) == inst.STATUSLINE
    mine = {"statusLine": {"type": "command", "command": "/opt/my-statusline.sh"}}
    assert _sl(inst.rewire_settings(mine, wrapper_exists=False)) == "/opt/my-statusline.sh"


def test_statusline_wiring_preserves_sibling_keys_and_is_idempotent():
    cfg = {"statusLine": {"type": "command", "command": "/h/.klawde/statusline.sh",
                          "padding": 0}}
    once = inst.rewire_settings(cfg, wrapper_exists=False)
    assert once["statusLine"]["padding"] == 0
    assert inst.rewire_settings(once, wrapper_exists=False) == once


def test_install_warns_instead_of_clobbering_a_foreign_statusline(home, capsys):
    cfg = dict(PRISTINE, statusLine={"type": "command", "command": "/opt/mine.sh"})
    inst.SETTINGS.write_text(json.dumps(cfg) + "\n")
    inst.install()
    out = json.loads(inst.SETTINGS.read_text())
    assert _sl(out) == "/opt/mine.sh"                     # untouched
    assert "LEFT ALONE" in capsys.readouterr().out        # and it said so


def test_install_wires_the_statusline_on_a_machine_with_no_wrapper(home, capsys):
    """The end-to-end bug: no custom-status-line.sh anywhere."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    assert not inst.WRAPPER.exists()
    inst.install()
    assert _sl(json.loads(inst.SETTINGS.read_text())) == inst.statusline_cmd()


# ── hook installation: copy by default, --dev for the checkout ──────────────
def test_sync_copies_hooks_and_the_sql_they_read(home):
    """common.sh resolves HERD_WRITES as <hooks>/../schema/writes.sql. Hooks copied
    WITHOUT their schema find no statements, so every write path fails with "no
    such statement" while each hook still exits 0 — a herd that records nothing."""
    hooks_dir = inst.sync_hooks()
    assert hooks_dir == inst.INSTALLED_HOOKS
    names = {p.name for p in hooks_dir.glob("*.sh")}
    assert {p.name for p in inst.HOOKS_DIR.glob("*.sh")} == names
    assert (inst.INSTALLED_SCHEMA / "writes.sql").exists()
    assert (inst.INSTALLED_SCHEMA / "writes.sql").read_text() == \
        (inst.SCHEMA_DIR / "writes.sql").read_text()


def test_copied_hooks_keep_the_executable_bit(home):
    """settings.json exec's these paths directly; a lost +x is a silent no-op."""
    for p in inst.sync_hooks().glob("*.sh"):
        assert os.access(p, os.X_OK), f"{p.name} lost +x in the copy"


def test_the_copied_hooks_actually_work(home):
    """The copy is only worth anything if the SELF-TEST passes against it — that is
    what proves the schema travelled and the paths still resolve."""
    ok, detail = inst.selftest(inst.sync_hooks())
    assert ok, detail


def test_dev_wires_the_checkout_instead(home):
    assert inst.sync_hooks(dev=True) == inst.HOOKS_DIR
    assert not inst.INSTALLED_HOOKS.exists()     # nothing copied


def test_install_wires_the_copy_and_dev_wires_the_tree(home):
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    inst.install()
    assert _wired(json.loads(inst.SETTINGS.read_text()))               # the copy
    inst.install(dev=True)
    assert _wired(json.loads(inst.SETTINGS.read_text()), inst.HOOKS_DIR)


def test_reinstalling_in_the_other_mode_leaves_one_entry_per_event(home):
    """Switching modes must REPLACE the wiring, not stack a second copy — two
    entries per event means every hook fires twice."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    inst.install()
    inst.install(dev=True)
    hooks = json.loads(inst.SETTINGS.read_text())["hooks"]
    for event in inst.HERD_HOOKS:
        ours = [h["command"] for bs in [hooks[event]] for b in bs for h in b["hooks"]
                if inst._is_managed(h.get("command", ""))]
        assert len(ours) == 1, f"{event} wired {len(ours)}x: {ours}"


def test_a_moved_checkout_is_still_recognised_as_ours():
    """_is_managed matched only the CURRENT roots, so an install made from a
    checkout that has since moved survived the strip and got a second entry."""
    stale = "/old/place/herd/src/herd/hooks/session_start.sh"
    assert inst._is_managed(stale)
    assert not inst._is_managed("/opt/other-tool/session_start.sh")   # not ours
    assert not inst._is_managed("cdh-claude-hook postToolUse")


def test_hooks_are_current_detects_drift(home):
    inst.sync_hooks()
    assert inst.hooks_are_current()
    (inst.INSTALLED_HOOKS / "stop.sh").write_text("#!/bin/bash\n# edited\nexit 0\n")
    assert not inst.hooks_are_current()
