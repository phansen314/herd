"""P (64-66d) — installer surgery on settings.json / the statusline wrapper, the
systemd unit, CLI paths, and the terminal-bell opt-in. Pure functions."""
import json
import os
import pathlib
import plistlib
import subprocess

import pytest
from types import SimpleNamespace

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
    timestamped backups. The OLDEST is the one install #1 took, i.e. pre-herd.

    That backup-selection rule is now reached via --restore-original: the default
    uninstall reverses the edits on the live file and never reads a snapshot to
    write. This is the escape hatch, so it is where the rule has to keep working."""
    wired = {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": str(inst.HOOKS_DIR / "stop.sh")}]}]}}
    (home / "settings.json.herd-bak.20250101-000000").write_text(json.dumps(PRISTINE))
    (home / "settings.json.herd-bak.20250202-000000").write_text(json.dumps(wired))
    inst.SETTINGS.write_text(json.dumps(wired))
    inst.uninstall(restore_original=True)
    assert json.loads(inst.SETTINGS.read_text()) == PRISTINE


# ── uninstall reverses the edits; it does not revert the file ────────────────
# The bug: uninstall wrote the pre-herd snapshot over the live settings.json with
# no backup, so a month of accumulated config was reverted AND unrecoverable.

def _month_of_use(path):
    """Everything a user accumulates between install and uninstall."""
    d = json.loads(path.read_text())
    d["permissions"] = {"allow": ["Bash(rg)", "Bash(pytest)"]}
    d["mcpServers"] = {"jetbrains": {"command": "jb-mcp"}}
    d["hooks"].setdefault("SessionStart", []).append(
        {"hooks": [{"type": "command", "command": "/opt/other-tool.sh"}]})
    path.write_text(json.dumps(d, indent=2))


def test_uninstall_keeps_what_was_added_after_the_install(home):
    """The whole point. Herd's five hook entries come out; permission grants, MCP
    servers and another tool's SessionStart hook stay. Restoring the pre-herd
    snapshot wholesale destroyed all three to undo the five."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    inst.install()
    _month_of_use(inst.SETTINGS)
    inst.uninstall()

    d = json.loads(inst.SETTINGS.read_text())
    assert d["permissions"] == {"allow": ["Bash(rg)", "Bash(pytest)"]}
    assert d["mcpServers"] == {"jetbrains": {"command": "jb-mcp"}}
    assert d["model"] == "opus"                                   # from PRISTINE
    assert not _wired(d)                                          # herd is gone
    cmds = [h.get("command") for bs in d["hooks"].values() for b in bs for h in b["hooks"]]
    assert "/opt/other-tool.sh" in cmds and "/opt/audit.sh" in cmds
    assert d != PRISTINE, "a surgical unwire must not reproduce the pre-herd file"


def test_uninstall_backs_up_before_writing(home):
    """Recoverability, independent of which path ran. The old code took no backup at
    all on the one write that could destroy a month of config."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    inst.install()
    _month_of_use(inst.SETTINGS)
    before = inst.SETTINGS.read_text()
    inst.uninstall()

    baks = [p for p in home.glob("settings.json.herd-bak.*")
            if not p.name.endswith(inst.ORIGINAL_SUFFIX)]
    assert any(p.read_text() == before for p in baks), \
        "no backup holds the file as it was immediately before uninstall"


def test_restore_original_still_reverts_wholesale_but_backs_up(home):
    """The escape hatch keeps its old semantics — and gains the backup, so the
    discarded month is recoverable instead of gone."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    inst.install()
    _month_of_use(inst.SETTINGS)
    before = inst.SETTINGS.read_text()
    inst.uninstall(restore_original=True)

    assert json.loads(inst.SETTINGS.read_text()) == PRISTINE      # wholesale revert
    baks = [p for p in home.glob("settings.json.herd-bak.*")
            if not p.name.endswith(inst.ORIGINAL_SUFFIX)]
    assert any(p.read_text() == before for p in baks)


def test_unwire_restores_the_statusline_it_replaced(home):
    """install's 'set' plan claims klawde's statusLine. Uninstall must hand it back,
    not delete the key — and must keep any sibling keys it never owned."""
    start = dict(PRISTINE, statusLine={"type": "command",
                                       "command": "/home/u/.klawde/statusline.sh",
                                       "padding": 1})
    inst.SETTINGS.write_text(json.dumps(start) + "\n")
    inst.install()
    assert inst.statusline_cmd() in inst.SETTINGS.read_text()      # precondition
    inst.uninstall()
    assert json.loads(inst.SETTINGS.read_text())["statusLine"] == start["statusLine"]


def test_unwire_drops_a_statusline_key_herd_added(home):
    """No statusLine before the install -> the key is ours, so it goes."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    inst.install()
    inst.uninstall()
    assert "statusLine" not in json.loads(inst.SETTINGS.read_text())


def test_unwire_leaves_a_foreign_statusline_alone(home):
    """install refuses to claim a statusline it does not own ('foreign'), so
    uninstall must not touch it either."""
    foreign = {"type": "command", "command": "/usr/bin/starship"}
    inst.SETTINGS.write_text(json.dumps(dict(PRISTINE, statusLine=foreign)) + "\n")
    inst.install()
    inst.uninstall()
    assert json.loads(inst.SETTINGS.read_text())["statusLine"] == foreign


def test_uninstall_refuses_an_unparseable_settings_file(home, capsys):
    """settings.json is the file whose truncation stops Claude Code from starting.
    A broken one must produce an instruction, not a traceback, and no write."""
    inst.SETTINGS.write_text("{not json at all")
    rc = inst.uninstall()
    assert rc == 1
    assert inst.SETTINGS.read_text() == "{not json at all"        # untouched
    assert "--restore-original" in capsys.readouterr().out


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


# ── symlinks + service: the side-effecting paths ────────────────────────────
def test_relink_backs_up_a_real_file_instead_of_destroying_it(tmp_path):
    """uninstall_cli only removes symlinks resolving to OUR target, so anything
    _relink deleted was gone for good. Every other installer path backs up first."""
    link = tmp_path / "bin" / "herd"
    link.parent.mkdir()
    link.write_text("#!/bin/sh\n# my own herd script\n")
    inst._relink(link, tmp_path / "real-herd", ts="20260101-000000")
    assert link.is_symlink()
    saved = list(link.parent.glob("herd.herd-bak.*"))
    assert saved, "the pre-existing script was destroyed with no backup"
    assert "my own herd script" in saved[0].read_text()


def test_relink_replaces_its_own_symlink_without_a_backup(tmp_path):
    """A symlink carries nothing to preserve — backing those up would litter."""
    link = tmp_path / "herd"
    link.symlink_to(tmp_path / "old-target")
    inst._relink(link, tmp_path / "new-target")
    assert link.readlink() == tmp_path / "new-target"
    assert not list(tmp_path.glob("herd.herd-bak.*"))


def test_relink_refuses_a_directory(tmp_path):
    d = tmp_path / "herd"
    d.mkdir()
    with pytest.raises(IsADirectoryError):
        inst._relink(d, tmp_path / "target")
    assert d.is_dir()                                   # left intact


def test_relink_is_idempotent(tmp_path):
    link, target = tmp_path / "herd", tmp_path / "target"
    inst._relink(link, target)
    inst._relink(link, target)
    assert link.readlink() == target
    assert not list(tmp_path.glob("herd.herd-bak.*"))


def test_service_install_is_a_graceful_noop_without_any_service_manager(monkeypatch, tmp_path):
    """Headless/containers: the daemon still works, you just run it yourself. This
    must not look like a failure, and must not write a unit nowhere useful.

    Both probes are patched, not just systemd's. Patching only _has_systemd_user
    made this test fall through to the launchd branch on a Mac and bootstrap a REAL
    LaunchAgent into the developer's session — a unit test restarting the live
    daemon — so the absence of the second patch has to be a test failure, not a
    side effect."""
    monkeypatch.setattr(inst, "_has_systemd_user", lambda: False)
    monkeypatch.setattr(inst, "_has_launchd", lambda: False)
    monkeypatch.setattr(inst, "SERVICE", tmp_path / "herd.service")
    monkeypatch.setattr(inst, "PLIST", tmp_path / "herd.plist")
    msg = inst.install_service()
    assert "SKIPPED" in msg and "herd.daemon" in msg     # tells you what to do
    assert not (tmp_path / "herd.service").exists()
    assert not (tmp_path / "herd.plist").exists()
    assert inst.uninstall_service() == "no herd.service to remove"


# ── the macOS half: a launchd LaunchAgent ────────────────────────────────────
def _plist(**patch):
    """plist_text() parsed back into a dict, so assertions read as key lookups
    rather than substring searches on XML."""
    return plistlib.loads(inst.plist_text().encode())


def test_plist_is_well_formed():
    p = _plist()
    assert p["Label"] == inst.LAUNCHD_LABEL
    assert p["ProgramArguments"][1:] == ["-m", "herd.daemon"]
    assert p["EnvironmentVariables"]["PYTHONPATH"] == str(inst.PKG_SRC)
    assert p["EnvironmentVariables"]["HERD_DB"] == str(inst.DB)
    assert p["RunAtLoad"] is True
    assert inst.PKG_SRC.name == "src"


def test_plist_retries_forever():
    """The launchd mirror of test_service_unit_retries_forever, and the same
    reasoning: a stopped daemon is invisible — sessions just never leave `herd ls`.
    KeepAlive={"SuccessfulExit": False} is the idiom to avoid; it is launchd's
    Restart=on-failure and would leave a clean exit un-restarted."""
    p = _plist()
    assert p["KeepAlive"] is True, "a clean exit must restart too"
    assert p["ThrottleInterval"] == 5
    # ProcessType=Background opts a job into CPU/IO throttling. A throttled reaper
    # is a stale `herd ls`, which is the symptom this service exists to prevent.
    assert "ProcessType" not in p


def test_plist_survives_a_path_that_would_break_hand_rolled_xml(monkeypatch, tmp_path):
    """HOME reaches five values in this plist. Built with a format string, a path
    holding & or < emits XML launchd rejects as 'Bootstrap failed: 5', which says
    nothing about the cause."""
    monkeypatch.setattr(inst, "DB", tmp_path / "a&b" / "<h>" / "herd.db")
    p = _plist()
    assert p["EnvironmentVariables"]["HERD_DB"] == str(tmp_path / "a&b" / "<h>" / "herd.db")


def test_launchd_install_reloads_rather_than_leaving_the_old_definition(monkeypatch, tmp_path):
    """bootstrap on an already-loaded label fails with EEXIST and leaves the OLD
    plist running, so a re-install would silently keep stale settings. bootout
    first — and it must come first, which is what the order assertion pins."""
    calls = []
    monkeypatch.setattr(inst, "_has_systemd_user", lambda: False)
    monkeypatch.setattr(inst, "_has_launchd", lambda: True)
    monkeypatch.setattr(inst, "PLIST", tmp_path / "herd.plist")
    monkeypatch.setattr(inst, "HERD_DIR", tmp_path)
    monkeypatch.setattr(inst, "_launchctl",
                        lambda *a: calls.append(a) or SimpleNamespace(returncode=0, stdout="\tpid = 42\n", stderr=""))
    msg = inst.install_service()
    verbs = [c[0] for c in calls]
    assert verbs[:2] == ["bootout", "bootstrap"]
    assert (tmp_path / "herd.plist").exists()
    assert "pid 42" in msg


def test_launchd_install_falls_back_to_the_deprecated_verbs(monkeypatch, tmp_path):
    """bootstrap/bootout are 10.11+. An older macOS must still end up loaded rather
    than fail the whole install."""
    calls = []

    def fake(*a):
        calls.append(a)
        rc = 1 if a[0] == "bootstrap" else 0
        return SimpleNamespace(returncode=rc, stdout="", stderr="unrecognized")

    monkeypatch.setattr(inst, "_has_systemd_user", lambda: False)
    monkeypatch.setattr(inst, "_has_launchd", lambda: True)
    monkeypatch.setattr(inst, "PLIST", tmp_path / "herd.plist")
    monkeypatch.setattr(inst, "HERD_DIR", tmp_path)
    monkeypatch.setattr(inst, "_launchctl", fake)
    msg = inst.install_service()
    assert "load" in [c[0] for c in calls]
    assert "FAILED" not in msg


def test_launchd_install_reports_a_load_failure_instead_of_claiming_success(monkeypatch, tmp_path):
    """Both verbs failing must say so. The plist on disk without a loaded job is
    the silent-no-reaper case, so it needs to be loud and tell you the manual verb."""
    monkeypatch.setattr(inst, "_has_systemd_user", lambda: False)
    monkeypatch.setattr(inst, "_has_launchd", lambda: True)
    monkeypatch.setattr(inst, "PLIST", tmp_path / "herd.plist")
    monkeypatch.setattr(inst, "HERD_DIR", tmp_path)
    monkeypatch.setattr(inst, "_launchctl",
                        lambda *a: SimpleNamespace(returncode=1, stdout="", stderr="nope"))
    msg = inst.install_service()
    assert "FAILED" in msg and "launchctl bootstrap" in msg


def test_uninstall_launchd_removes_and_unloads_the_plist(monkeypatch, tmp_path):
    calls = []
    plist = tmp_path / "herd.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(inst, "_has_systemd_user", lambda: False)
    monkeypatch.setattr(inst, "_has_launchd", lambda: True)
    monkeypatch.setattr(inst, "PLIST", plist)
    monkeypatch.setattr(inst, "_launchctl",
                        lambda *a: calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    msg = inst.uninstall_service()
    assert "bootout" in [c[0] for c in calls], "left loaded after its plist was deleted"
    assert not plist.exists() and "removed" in msg


def test_uninstall_launchd_with_nothing_installed(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "_has_systemd_user", lambda: False)
    monkeypatch.setattr(inst, "_has_launchd", lambda: True)
    monkeypatch.setattr(inst, "PLIST", tmp_path / "absent.plist")
    assert inst.uninstall_service() == "no LaunchAgent to remove"


@pytest.mark.shell
def test_uninstall_service_removes_the_unit(monkeypatch, tmp_path):
    unit = tmp_path / "herd.service"
    unit.write_text("[Unit]\n")
    monkeypatch.setattr(inst, "SERVICE", unit)
    monkeypatch.setattr(inst, "_has_systemd_user", lambda: True)
    monkeypatch.setattr(inst.subprocess, "run",
                        lambda *a, **k: SimpleNamespace(stdout="", returncode=0))
    assert "removed" in inst.uninstall_service()
    assert not unit.exists()


def test_install_cli_links_both_and_warns_when_not_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr(inst, "CLI_LINK", tmp_path / "bin" / "herd")
    monkeypatch.setattr(inst, "COMPLETION_LINK", tmp_path / "comp" / "herd")
    monkeypatch.setenv("PATH", "/usr/bin")               # ~/.local/bin absent
    msg = inst.install_cli()
    assert (tmp_path / "bin" / "herd").is_symlink()
    assert (tmp_path / "comp" / "herd").is_symlink()
    assert "WARN" in msg or "PATH" in msg


def test_uninstall_cli_leaves_a_foreign_link_alone(monkeypatch, tmp_path):
    """It only removes links resolving to our own target — a stranger's `herd`
    on PATH is not ours to delete."""
    foreign = tmp_path / "bin" / "herd"
    foreign.parent.mkdir()
    foreign.symlink_to(tmp_path / "someone-elses-herd")
    monkeypatch.setattr(inst, "CLI_LINK", foreign)
    monkeypatch.setattr(inst, "COMPLETION_LINK", tmp_path / "comp" / "herd")
    inst.uninstall_cli()
    assert foreign.is_symlink()                          # untouched


def test_wrapper_rewrite_keeps_the_rest_of_the_line():
    """The whole line used to be replaced by the path alone, dropping exec, "$@"
    and any redirect — a wrapper written the obvious way lost its arguments."""
    w = 'exec "$HOME/.klawde/statusline.sh" "$@" 2>/dev/null\n'
    out, rep = inst.rewire_wrapper(w)
    assert rep
    assert out.startswith("exec ") and '"$@"' in out and "2>/dev/null" in out
    assert ".klawde" not in out and inst.statusline_cmd() in out


def test_wrapper_rewrite_preserves_a_composed_line():
    w = 'CAV=$(bash caveman); printf "%s ┃ " "$CAV"; "$HOME/.klawde/statusline.sh"\n'
    out, _ = inst.rewire_wrapper(w)
    assert "bash caveman" in out and "printf" in out and inst.statusline_cmd() in out


def test_wrapper_rewrite_is_still_idempotent():
    w = 'exec "$HOME/.klawde/statusline.sh" "$@"\n'
    once, _ = inst.rewire_wrapper(w)
    assert inst.rewire_wrapper(once)[0] == once


@pytest.mark.shell
def test_bin_herd_resolves_symlinks_without_readlink_f(tmp_path):
    """`readlink -f` is GNU-only and herd claims macOS support; there SELF came
    back empty and the wrapper resolved to the wrong directory."""
    direct = tmp_path / "direct"
    direct.symlink_to(inst.CLI_SRC)                      # absolute
    nested = tmp_path / "d"
    nested.mkdir()
    chained = nested / "chained"
    chained.symlink_to(pathlib.Path("..") / "direct")    # relative, to a symlink
    # HERD_DB points at a fresh temp file. `herd ls` opens the DB, so inheriting
    # the ambient one made this pass only on a machine that had already installed
    # herd — it failed on the first runner with no ~/.herd/herd.db. The subject
    # here is the symlink resolution in bin/herd, not the database.
    db = tmp_path / "herd.db"
    from herd.db import connect, apply_schema
    conn = connect(str(db)); apply_schema(conn); conn.close()
    env = dict(os.environ, HERD_DB=str(db))
    for entry in (inst.CLI_SRC, direct, chained):
        r = subprocess.run(["bash", str(entry), "ls"], capture_output=True,
                           text=True, env=env)
        assert r.returncode == 0, f"{entry}: {r.stderr}"
        assert "readlink" not in r.stderr


# ── argv validation: an option we cannot read must change NOTHING ─────────────
def _main_never_installs(monkeypatch, argv):
    """Run main(argv) with install/uninstall replaced by tripwires."""
    called = []
    monkeypatch.setattr(inst, "install", lambda **k: called.append(("install", k)))
    monkeypatch.setattr(inst, "uninstall", lambda: called.append(("uninstall",)))
    rc = inst.main(argv)
    return rc, called


@pytest.mark.parametrize("argv", [
    ["--help"], ["-h"],
    ["--dry-runn"],            # the typo that silently installed
    ["--DEV"],                 # case matters
    ["-dev"],                  # one dash
    ["--uninstal"],            # near-miss on the destructive flag
    ["install"],               # a bare verb, git-style
    ["--dry-run", "--nope"],   # one good flag does not excuse a bad one
])
def test_unreadable_argv_installs_nothing(monkeypatch, argv):
    """`--help` used to perform a FULL INSTALL: main() membership-tested each known
    flag and let everything else fall through to install(). On a command that
    rewrites settings.json, rewires the statusline and restarts a systemd unit, a
    flag it cannot read must mean stop — never 'proceed with the default'."""
    rc, called = _main_never_installs(monkeypatch, argv)
    assert called == [], f"{argv} reached {called[0][0]}()"
    assert rc in (0, 2)


def test_help_is_help_not_an_install(monkeypatch, capsys):
    rc, called = _main_never_installs(monkeypatch, ["--help"])
    assert rc == 0 and called == []
    assert "usage:" in capsys.readouterr().out


def test_unknown_flag_names_itself_and_exits_nonzero(monkeypatch, capsys):
    rc, called = _main_never_installs(monkeypatch, ["--dry-runn"])
    out = capsys.readouterr().out
    assert rc == 2 and called == []
    assert "--dry-runn" in out and "Nothing was changed" in out


@pytest.mark.parametrize("argv,expect", [
    ([], ("install", {"dry": False, "dev": False})),
    (["--dev"], ("install", {"dry": False, "dev": True})),
    (["--dry-run"], ("install", {"dry": True, "dev": False})),
    (["--dry-run", "--dev"], ("install", {"dry": True, "dev": True})),
    (["--uninstall"], ("uninstall",)),
])
def test_known_flags_still_route(monkeypatch, argv, expect):
    """The refusal must not have broken the flags that DO work."""
    rc, called = _main_never_installs(monkeypatch, argv)
    assert called == [expect]


def test_restore_original_routes_to_the_wholesale_path(monkeypatch):
    called = []
    monkeypatch.setattr(inst, "install", lambda **k: called.append(("install", k)))
    monkeypatch.setattr(inst, "uninstall",
                        lambda **k: called.append(("uninstall", k)))
    inst.main(["--uninstall", "--restore-original"])
    assert called == [("uninstall", {"restore_original": True})]


def test_restore_original_alone_does_nothing(monkeypatch, capsys):
    """It modifies --uninstall; on its own it is closer to a typo than a request,
    and this command does not act on argv it cannot read."""
    rc, called = _main_never_installs(monkeypatch, ["--restore-original"])
    assert rc == 2 and called == []
    assert "without --uninstall" in capsys.readouterr().out


# ── the wrapper rewrite must not claim OTHER tools' statuslines ───────────────
# The real composed wrapper, verbatim from a machine where this broke.
_CAVEMAN_WRAPPER = '''#!/usr/bin/env bash
# herd + caveman composed statusline.
# caveman reads a flag file (no stdin); herd inherits parent stdin directly.

CAVEMAN_SL="$HOME/.claude/plugins/marketplaces/caveman/hooks/caveman-statusline.sh"
if [ -f "$CAVEMAN_SL" ]; then
  CAVEMAN_OUT=$(bash "$CAVEMAN_SL")
  if [ -n "$CAVEMAN_OUT" ]; then
    printf '%s ┃ ' "$CAVEMAN_OUT"
  fi
fi

"$HOME/.klawde/statusline.sh"
'''


def test_wrapper_rewrite_leaves_another_tools_statusline_alone():
    """`caveman-statusline.sh` is not herd's invocation. The token regex matched a
    bare SUFFIX, so it claimed the plugin's path too — and because that path sits in
    a `VAR="..."` assignment with no spaces in it, the unbounded `\\S*` alternative
    matched from column 0 and ate the assignment and its opening quote:

        CAVEMAN_SL="$HOME/.../caveman-statusline.sh"   ->   "<herd>""

    That is a bash syntax error. The wrapper printed NOTHING, so the statusline
    disappeared from every session at once, with no error anywhere to find."""
    out, replaced = inst.rewire_wrapper(_CAVEMAN_WRAPPER)
    assert replaced
    assert 'CAVEMAN_SL="$HOME/.claude/plugins/marketplaces/caveman/hooks/caveman-statusline.sh"' in out
    assert ".klawde" not in out and inst.statusline_cmd() in out
    assert '""' not in out


@pytest.mark.shell
def test_rewritten_wrapper_is_valid_bash(tmp_path):
    """The failure mode was a SYNTAX error, which no assertion about substrings
    would have caught. Parse the result."""
    out, _ = inst.rewire_wrapper(_CAVEMAN_WRAPPER)
    p = tmp_path / "w.sh"
    p.write_text(out)
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_wrapper_rewrite_does_not_swallow_an_assignment_of_the_real_statusline():
    """Even when the path IS a statusline.sh, only the path is the token — the
    variable it is being assigned to is not part of it."""
    out, _ = inst.rewire_wrapper('SL=$HOME/.klawde/statusline.sh\n')
    assert out.startswith("SL=")
    assert inst.statusline_cmd() in out


def test_wrapper_rewrite_is_idempotent_on_the_composed_wrapper():
    once, _ = inst.rewire_wrapper(_CAVEMAN_WRAPPER)
    assert inst.rewire_wrapper(once)[0] == once


# ── unwire_wrapper: the mirror ───────────────────────────────────────────────

def test_unwire_wrapper_round_trips_the_composed_wrapper():
    """rewire then unwire must land back on the original text — the composed
    wrapper is the case where a line-level rewrite already destroyed `exec`/`"$@"`
    once, so the reverse has to be equally surgical."""
    wired, _ = inst.rewire_wrapper(_CAVEMAN_WRAPPER)
    back, changed = inst.unwire_wrapper(wired, _CAVEMAN_WRAPPER)
    assert changed
    assert back == _CAVEMAN_WRAPPER.rstrip("\n") + "\n"
    assert inst.statusline_cmd() not in back


def test_unwire_wrapper_without_an_original_changes_nothing():
    """The pre-herd token is the one thing the rewired file no longer records.
    Guessing a path would leave the wrapper calling something that never existed;
    left alone it still calls herd's, which the user can see and edit."""
    wired, _ = inst.rewire_wrapper(_CAVEMAN_WRAPPER)
    assert inst.unwire_wrapper(wired, None) == (wired, False)
    assert inst.unwire_wrapper(wired, "#!/bin/sh\necho hi\n") == (wired, False)


@pytest.mark.shell
def test_unwired_wrapper_is_valid_bash(tmp_path):
    """Same reasoning as the rewrite: the failure mode is a syntax error."""
    wired, _ = inst.rewire_wrapper(_CAVEMAN_WRAPPER)
    back, _ = inst.unwire_wrapper(wired, _CAVEMAN_WRAPPER)
    p = tmp_path / "w.sh"
    p.write_text(back)
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_uninstall_unwires_the_wrapper_and_backs_it_up(home):
    """End to end on the second file uninstall touches — it had the identical
    no-backup problem."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    inst.WRAPPER.write_text(_CAVEMAN_WRAPPER)
    inst.install()
    wired = inst.WRAPPER.read_text()
    assert inst.statusline_cmd() in wired                          # precondition
    inst.uninstall()

    back = inst.WRAPPER.read_text()
    assert inst.statusline_cmd() not in back
    assert ".klawde/statusline.sh" in back                         # the original token
    assert any(p.read_text() == wired
               for p in home.glob("custom-status-line.sh.herd-bak.*")
               if not p.name.endswith(inst.ORIGINAL_SUFFIX))


def test_uninstall_leaves_a_wrapper_it_cannot_unwire(home, capsys):
    """No pre-herd snapshot -> no original token -> refuse and say so, nonzero."""
    inst.SETTINGS.write_text(json.dumps(PRISTINE) + "\n")
    inst.WRAPPER.write_text(f'exec "{inst.statusline_cmd()}" "$@"\n')
    wired = inst.WRAPPER.read_text()
    assert inst.uninstall() == 1
    assert inst.WRAPPER.read_text() == wired
    assert "LEFT AS-IS" in capsys.readouterr().out


# ── the wrapper rewrite must never produce unparseable bash ───────────────────
_WRAPPER_IDIOMS = [
    ('exec "$(dirname "$0")/statusline.sh" "$@"\n', True),
    ("exec $(dirname $0)/statusline.sh \"$@\"\n", True),
    ('exec "$HOME/.klawde/statusline.sh" "$@" 2>/dev/null\n', True),
    ('echo "hi" ; exec "$SL/statusline.sh"\n', True),
    ("SL=$HOME/.klawde/statusline.sh\n", True),
    ('exec statusline.sh "$@"\n', True),          # cwd-relative: must still rewire
    ('# exec "/old/statusline.sh"\n', False),     # a comment is not an invocation
    ('echo hello\n', False),                      # nothing to do
]


@pytest.mark.shell
@pytest.mark.parametrize("src,expect_replaced", _WRAPPER_IDIOMS,
                         ids=[s.strip()[:28] for s, _ in _WRAPPER_IDIOMS])
def test_wrapper_rewrite_never_emits_broken_bash(src, expect_replaced, tmp_path):
    """Two separate regexes have now turned a working wrapper into a SYNTAX ERROR,
    and the blast radius is identical each time: bash prints nothing, so the
    statusline silently disappears from every running session with nothing logged.

    The nested-quote case is the one that survived the first fix:
        exec "$(dirname "$0")/statusline.sh" "$@"
    the quoted alternative anchored on the INNER quotes, matched `")/statusline.sh"`
    and ate the `)` closing the substitution."""
    out, replaced = inst.rewire_wrapper(src)
    assert replaced is expect_replaced
    p = tmp_path / "w.sh"
    p.write_text(out)
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    assert r.returncode == 0, f"{src!r} -> {out!r}\n{r.stderr}"
    if expect_replaced:
        assert inst.statusline_cmd() in out
    else:
        assert out == src


def test_wrapper_rewrite_refuses_rather_than_break_a_working_wrapper(monkeypatch):
    """The backstop, independent of any regex: if substitution would produce
    something that does not parse and the input did, change nothing and report it."""
    monkeypatch.setattr(inst, "_SL_TOKEN", __import__("re").compile(r'statusline\.sh"'))
    src = 'exec "/x/statusline.sh"\n'             # this token eats the closing quote
    out, replaced = inst.rewire_wrapper(src)
    assert out == src and replaced is False


def test_wrapper_rewrite_leaves_a_comment_unwired():
    """Rewriting inside `#` changes nothing executable, but it used to return
    replaced=True, so install() reported a wrapper it had not wired."""
    out, replaced = inst.rewire_wrapper('# "/old/statusline.sh"\nexec "/old/statusline.sh"\n')
    assert replaced
    assert out.splitlines()[0] == '# "/old/statusline.sh"'
    assert inst.statusline_cmd() in out.splitlines()[1]


@pytest.mark.parametrize("argv", [["--dry-run", "--uninstall"],
                                  ["--uninstall", "--dry-run"],
                                  ["--dev", "--uninstall"]])
def test_uninstall_refuses_conflicting_flags(monkeypatch, argv):
    """Per-token validation passed both, then --uninstall won the dispatch — so
    `--dry-run --uninstall` deleted the service and unwired settings.json having
    been told to touch nothing. uninstall() has no dry mode to honour."""
    rc, called = _main_never_installs(monkeypatch, argv)
    assert rc == 2 and called == []


# ── _launchctl must degrade, not raise (DECISIONS.md#launchd-log) ────────────
def _fake_launchctl_bin(tmp_path, body):
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    p = d / "launchctl"
    p.write_text(f"#!/bin/sh\n{body}\n")
    p.chmod(0o755)
    return d


def test_launchctl_never_raises_on_a_wedged_launchd(tmp_path, monkeypatch):
    """`check=False` suppresses CalledProcessError; `timeout=` still RAISES
    TimeoutExpired — in exactly the case LAUNCHCTL_TIMEOUT exists for. install()
    calls install_service() AFTER rewriting settings.json and the wrapper, so an
    escaping exception left the config changed, no daemon, and a traceback."""
    monkeypatch.setenv("PATH", f"{_fake_launchctl_bin(tmp_path, 'sleep 30')}:{os.environ['PATH']}")
    monkeypatch.setattr(inst, "LAUNCHCTL_TIMEOUT", 1)
    r = inst._launchctl("print", "gui/501/x")        # must not raise
    assert r.returncode != 0 and "timed out" in r.stderr


def test_launchctl_never_raises_when_the_binary_is_gone(tmp_path, monkeypatch):
    """_has_launchd() gates the normal path, but the binary can vanish between that
    check and the five calls an install makes."""
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    r = inst._launchctl("print", "gui/501/x")
    assert r.returncode != 0 and "launchctl" in r.stderr


def test_a_wedged_launchd_reports_instead_of_crashing_the_install(tmp_path, monkeypatch):
    """The whole point: the installer finishes and SAYS what went wrong."""
    monkeypatch.setattr(inst, "PLIST", tmp_path / "herd.plist")
    monkeypatch.setattr(inst, "HERD_DIR", tmp_path)
    monkeypatch.setattr(inst, "_launchctl",
                        lambda *a: subprocess.CompletedProcess(a, 124, "", "timed out"))
    msg = inst.install_launchd()
    assert "FAILED" in msg and "launchctl bootstrap" in msg   # names the manual fix
    assert (tmp_path / "herd.plist").exists()                 # plist still written
