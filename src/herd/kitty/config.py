"""kitty.conf: the two options herd needs, and the block that adds them.

Pure text + environment inspection, no IO — `doctor` reads the state to report it
and `install` reads it to decide whether to offer the edit, and neither should be
the one that knows what the block looks like.

**Why the environment and not kitty.conf.** kitty.conf supports `include`,
last-wins overrides and conditional sections, so "is `allow_remote_control` on?"
cannot be answered by reading one file — and a wrong answer here is worse than no
answer, because it would tell someone their working setup is broken. The
environment is the actual capability: kitty exports KITTY_LISTEN_ON only when
remote control is on AND a socket is configured, which is exactly the pair herd
needs. Parsing is used for one narrow question only — has herd's OWN block already
been added — where the markers make it exact.
"""
import pathlib

KITTY_CONF = pathlib.Path.home() / ".config" / "kitty" / "kitty.conf"

# Markers, not a fuzzy search: uninstall has to remove exactly what install added
# and nothing a person wrote. Same shape as kitty's own BEGIN_KITTY_THEME.
BEGIN = "# BEGIN herd — added by `python3 -m herd.install`"
END = "# END herd"

BLOCK = f"""{BEGIN}
# herd records which kitty window each Claude session lives in, and `herd jump`
# focuses it. Both need kitty's remote control socket; without it herd still
# tracks sessions, but every placement is empty and spawn/jump cannot work.
# {{kitty_pid}} keeps the socket unique per kitty instance — a window id means
# nothing without the socket it came from, so a fixed path would make two kitty
# instances hand out colliding ids.
allow_remote_control yes
listen_on unix:/tmp/kitty-{{kitty_pid}}
{END}"""

# What a person has to do after either option changes. Neither is runtime
# reloadable — `kitten @ load-config` will not pick them up, and saying "reload"
# would send someone looking for a bug that isn't there.
RESTART = "restart kitty (neither option is picked up by a config reload)"

READY = "ready"                     # remote control is on and a socket is exported
OFF = "remote-control-off"          # inside kitty, but no socket — the actionable case
NOT_KITTY = "not-kitty"             # not inside kitty at all; nothing to conclude


def state(environ):
    """Which of the three worlds we're in.

    KITTY_WINDOW_ID is what separates the two "no socket" cases: kitty exports it
    inside every window regardless of remote control, so it present + LISTEN_ON
    absent is precisely "you are in kitty and remote control is off" — the only
    state worth nagging about. Without that distinction a plain xterm and a
    misconfigured kitty look identical, and the check would have to either cry
    wolf everywhere or stay silent where it matters.
    """
    if environ.get("KITTY_LISTEN_ON"):
        return READY
    if environ.get("KITTY_WINDOW_ID"):
        return OFF
    return NOT_KITTY


def has_block(text):
    return BEGIN in (text or "")


def add_block(text):
    """Append the block. Idempotent, and appends at EOF on purpose: kitty is
    last-wins, so this beats an `allow_remote_control no` set earlier in the file.

    The one thing this does not preserve is a missing final newline — a file not
    ending in one gets normalized. Every real kitty.conf ends in a newline, and the
    alternative is writing a malformed line into someone's config.
    """
    if has_block(text):
        return text
    if not text:
        return BLOCK + "\n"
    return text + ("" if text.endswith("\n") else "\n") + "\n" + BLOCK + "\n"


def strip_block(text):
    """Remove exactly what add_block inserted, so strip_block(add_block(t)) == t.

    Deliberately surgical rather than "rstrip and rebuild": the first version
    normalized whitespace it did not add, and eating a trailing blank line the user
    had written is a silent edit to a file herd does not own. A file that never had
    a block comes back byte-identical.
    """
    if not has_block(text):
        return text
    i = text.index(BEGIN)
    j = text.index(END, i) + len(END)
    if text[j:j + 1] == "\n":               # the newline closing the block
        j += 1
    k = i
    if text[:k].endswith("\n\n"):           # the ONE blank line add_block inserted,
        k -= 1                              # never any the user put there
    return text[:k] + text[j:]
