"""S — herd/settings.py: the one definition of what herd owns in settings.json.

install and doctor both read this. They used to answer the question separately, and
the scenario one was written for was the scenario the other got wrong.
"""
import json

import pytest

from herd import settings as st
from herd import doctor, install as inst


MOVED = "/home/u/old-checkout/herd/src/herd/hooks/session_start.sh"


def test_a_hook_from_a_moved_checkout_is_still_ours():
    """THE disagreement. install._is_managed matches by basename on a herd-shaped
    path precisely so an install made from a checkout that has since moved is still
    recognised — miss it and the strip leaves the stale entry, step 2 adds a second,
    and every hook fires twice. doctor tested `root in cmd` against the CURRENT
    roots, so it called the same command foreign and reported `SessionStart not
    wired` about a hook that was running fine."""
    assert st.is_managed(MOVED, roots=(st.HOOKS_DIR, st.INSTALLED_HOOKS))


def test_doctor_and_install_agree_on_every_shape():
    """One predicate, so the answers cannot drift. Parametrising this over both
    callers is the point — it is not testing is_managed twice."""
    cases = [MOVED, "/h/.klawde/session_start.sh", str(st.INSTALLED_HOOKS / "stop.sh"),
             "cdh-claude-hook postToolUse", "/opt/other/session_start.sh", "", "   "]
    roots = (st.HOOKS_DIR, st.INSTALLED_HOOKS)
    for cmd in cases:
        assert inst._is_managed(cmd) == st.is_managed(cmd, roots), cmd


def test_an_unrelated_tools_session_start_is_not_ours():
    """The herd-shaped-path condition. Matching on basename alone would claim
    somebody else's session_start.sh and strip it out of their settings.json."""
    assert not st.is_managed("/opt/other/session_start.sh")
    assert not st.is_managed("cdh-claude-hook postToolUse")


@pytest.mark.parametrize("data", [
    {"hooks": []},
    {"hooks": "x"},
    {"hooks": {"Stop": ["x"]}},
    {"hooks": {"Stop": [{"hooks": "x"}]}},
    {"hooks": {"Stop": [{"hooks": ["x"]}]}},
    [],
    "a string",
    {},
])
def test_the_walkers_survive_any_hand_edited_shape(data):
    """install's copies assumed dicts of lists of dicts all the way down and raised
    AttributeError — a stack trace from the command you run BECAUSE the file is
    broken. doctor's tolerated it; the tolerant ones are now the only ones."""
    assert isinstance(st.hook_commands(data), list)
    assert isinstance(st.statusline_command(data), str)
    if isinstance(data, dict) and isinstance(data.get("hooks"), dict):
        st.strip_managed(data["hooks"])          # must not raise


def test_strip_keeps_a_block_it_cannot_parse():
    """Anything unrecognised is left ALONE, not dropped. `b.get("hooks")` on a
    non-dict raises, and treating it as empty would delete a stranger's entry."""
    hooks = {"Stop": ["not-a-block", {"hooks": [{"command": str(st.INSTALLED_HOOKS / "stop.sh")}]}]}
    st.strip_managed(hooks)
    assert hooks["Stop"] == ["not-a-block"]      # ours went, theirs stayed


def test_strip_removes_ours_and_spares_foreign():
    hooks = {"PostToolUse": [{"hooks": [
        {"command": str(st.INSTALLED_HOOKS / "post_tool_use.sh")},
        {"command": "cdh-claude-hook postToolUse"}]}]}
    st.strip_managed(hooks)
    assert [h["command"] for h in hooks["PostToolUse"][0]["hooks"]] == \
        ["cdh-claude-hook postToolUse"]


def test_doctor_reports_a_moved_checkout_install_as_wired():
    """The user-visible half of the disagreement, through doctor's own check."""
    data = {"hooks": {ev: [{"hooks": [{"command": MOVED.replace("session_start.sh", scr)}]}]
                      for ev, (scr, _) in st.HERD_HOOKS.items()},
            "statusLine": {"command": MOVED.replace("session_start.sh", "statusline.sh")}}
    out = doctor.check_wiring(json.dumps(data), (st.INSTALLED_HOOKS, st.HOOKS_DIR),
                              (st.statusline_cmd(),), tuple(st.HERD_HOOKS))
    not_wired = [h for lvl, h, _ in out if "not wired" in h]
    assert not_wired == [], f"doctor called a live install unwired: {not_wired}"
