"""What herd owns inside ~/.claude/settings.json — read by the installer AND by
doctor, so the two cannot disagree about it.

They did. `install._is_managed` matches a hook by path prefix OR by basename on a
herd-shaped path, deliberately: a prefix test alone misses an install made from a
checkout that has since MOVED, the stale entry survives the strip, step 2 adds a
second one, and every hook fires twice. `doctor.check_wiring` used a plain substring
test against the two current roots — so the exact scenario _is_managed exists to
handle was the one where doctor reported `SessionStart not wired` about hooks that
were running fine. One question, two answers, one file apart.

The walkers moved here for a second reason. doctor's tolerated any shape a hand
edit can leave behind; install's assumed dicts of lists of dicts all the way down
and raised AttributeError on `{"hooks": []}` or `{"hooks": {"Stop": ["x"]}}` —
tracebacking on the one file you would run the installer to repair.
"""
import pathlib

HOOKS_DIR = pathlib.Path(__file__).resolve().parent / "hooks"   # in the checkout
HERD_DIR = pathlib.Path.home() / ".herd"
INSTALLED_HOOKS = HERD_DIR / "hooks"                            # what actually runs

# event -> (hook script, async?). Stop is herd's own (klawde had none). SessionStart
# and SessionEnd are BLOCKING; SessionEnd blocking is the fix for klawde's async bug.
HERD_HOOKS = {
    "SessionStart": ("session_start.sh", False),
    "Stop":         ("stop.sh",          True),
    "SessionEnd":   ("session_end.sh",   False),
    "Notification": ("notification.sh",  True),
    "PostToolUse":  ("post_tool_use.sh", True),
}

OUR_SCRIPTS = {s for s, _ in HERD_HOOKS.values()} | {"statusline.sh", "common.sh"}


def hook_cmd(script, hooks_dir=None):
    return str((hooks_dir or INSTALLED_HOOKS) / script)


def statusline_cmd(hooks_dir=None):
    return str((hooks_dir or INSTALLED_HOOKS) / "statusline.sh")


def default_roots():
    """The two acceptable hook locations: the installed copy and the checkout."""
    return (HOOKS_DIR, INSTALLED_HOOKS)


def is_managed(cmd, roots=None):
    """A command herd owns — klawde's, or any prior herd install. Everything else
    (cdh, the PreToolUse HTTP hook, anything unknown) is preserved untouched.

    Recognising our own scripts BY NAME matters as much as by path: a prefix test
    against the current roots misses an install made from a checkout that has since
    moved, so the stale entry survives the strip and step 2 adds a second one —
    every hook then fires twice. The herd-shaped-path condition keeps us from
    claiming an unrelated tool's session_start.sh."""
    if not cmd:
        return False
    if "/.klawde/" in cmd:
        return True
    # roots is a PARAMETER because the caller's copy of these paths is the one that
    # matters: the installer's tests redirect install.INSTALLED_HOOKS at a temp dir,
    # and resolving against this module's own constants instead made ownership
    # detection quietly ignore the redirect — uninstall then stripped nothing.
    for r in (roots if roots is not None else default_roots()):
        if cmd.startswith(str(r)):
            return True
    parts = cmd.split()
    if not parts:                       # whitespace-only: `not cmd` misses it, and
        return False                    # split()[0] on it is an IndexError
    path = parts[0]
    return path.rsplit("/", 1)[-1] in OUR_SCRIPTS and ("/herd/" in path or "/.herd/" in path)


def hook_commands(data):
    """[(event, command), ...] from a settings dict, tolerating any shape.

    Every level is checked because every level can be hand-edited into something
    else, and this runs against a file herd did not write."""
    out = []
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if not isinstance(hooks, dict):
        return out
    for event, blocks in hooks.items():
        if not isinstance(blocks, list):
            continue
        for b in blocks:
            if not isinstance(b, dict):
                continue
            hs = b.get("hooks")
            if not isinstance(hs, list):
                continue
            for h in hs:
                if isinstance(h, dict):
                    out.append((event, h.get("command") or ""))
    return out


def statusline_command(data):
    """settings.statusLine.command, or "" — including when statusLine is a string,
    a list, or anything else a hand edit can leave behind."""
    if not isinstance(data, dict):
        return ""
    sl = data.get("statusLine")
    return (sl.get("command") or "") if isinstance(sl, dict) else ""


def strip_managed(hooks, roots=None):
    """Remove every herd-managed command IN PLACE, dropping blocks and then events
    that become empty. cdh / PreToolUse-HTTP / others are untouched.

    Shared by rewire_settings (which re-adds herd afterwards) and unwire_settings
    (which does not). It lives in one place because the two must agree on what herd
    owns — a strip that drifts from the re-add either doubles every hook or strands
    one behind.

    Shape-tolerant, like hook_commands. It used to assume dicts of lists of dicts
    and raised AttributeError on `{"hooks": []}`, `{"hooks": {"Stop": ["x"]}}` or a
    settings.json that is not an object at all — aborting the installer with a stack
    trace on exactly the malformed file someone would run it to fix. Anything it
    does not recognise is left ALONE rather than dropped: herd does not own a shape
    it cannot read, and deleting a key it failed to parse would be worse than
    refusing to touch it."""
    if not isinstance(hooks, dict):
        return
    for event in list(hooks):
        blocks = hooks[event]
        if not isinstance(blocks, list):
            continue                    # not ours to interpret; leave it untouched
        for block in blocks:
            if not isinstance(block, dict):
                continue
            hs = block.get("hooks")
            if not isinstance(hs, list):
                continue
            block["hooks"] = [h for h in hs
                              if not (isinstance(h, dict)
                                      and is_managed(h.get("command") or "", roots))]
        # A block we could not parse is KEPT: `b.get("hooks")` on a non-dict raises,
        # and treating it as empty would delete a stranger's entry.
        kept = [b for b in blocks
                if not isinstance(b, dict) or b.get("hooks")]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
