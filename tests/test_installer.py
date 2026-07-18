"""P (64-66d) — installer surgery on settings.json / the statusline wrapper, the
systemd unit, CLI paths, and the terminal-bell opt-in. Pure functions."""
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
    assert "Restart=on-failure" in u and "WantedBy=default.target" in u
    assert inst.PKG_SRC.name == "src"


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
    ok, detail = I.selftest()
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
    monkeypatch.setattr(I, "HOOKS_DIR", fake)
    ok, detail = I.selftest()
    assert not ok
    assert sorted(detail["not_executable"]) == ["session_start.sh", "statusline.sh"]


def test_selftest_leaves_the_real_db_alone(tmp_path, monkeypatch):
    """It must use a throwaway DB — a selftest that wrote to ~/.herd/herd.db would
    inject a fake session into the user's live list on every install."""
    from herd import install as I
    real = tmp_path / "herd.db"
    monkeypatch.setattr(I, "DB", real)
    I.selftest()
    assert not real.exists()
