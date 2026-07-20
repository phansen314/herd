"""kitty.conf: the two options herd needs, and the block that adds them.

Pure text + environment inspection, no IO; shared by doctor and install.

State is read from the ENVIRONMENT, not by parsing kitty.conf: `include`, last-wins
overrides and conditional sections mean no single file answers "is
allow_remote_control on?". kitty exports KITTY_LISTEN_ON only when remote control is
on AND a socket is configured — exactly the pair herd needs.
"""
import pathlib

KITTY_CONF = pathlib.Path.home() / ".config" / "kitty" / "kitty.conf"

# Markers, not a fuzzy search: uninstall must remove exactly what install added and
# nothing a person wrote. Same shape as kitty's own BEGIN_KITTY_THEME.
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

# Neither option is runtime reloadable — `kitten @ load-config` will not pick them up.
RESTART = "restart kitty (neither option is picked up by a config reload)"

READY = "ready"                     # remote control is on and a socket is exported
OFF = "remote-control-off"          # inside kitty, but no socket — the actionable case
NOT_KITTY = "not-kitty"             # not inside kitty at all; nothing to conclude


def state(environ):
    """Which of the three worlds we're in.

    KITTY_WINDOW_ID separates the two "no socket" cases: kitty exports it in every
    window regardless of remote control, so present + LISTEN_ON absent means exactly
    "in kitty, remote control off".
    """
    if environ.get("KITTY_LISTEN_ON"):
        return READY
    if environ.get("KITTY_WINDOW_ID"):
        return OFF
    return NOT_KITTY


def has_block(text):
    return BEGIN in (text or "")


def add_block(text):
    """Append the block. Idempotent, and at EOF on purpose: kitty is last-wins, so
    this beats an `allow_remote_control no` set earlier in the file. A file with no
    trailing newline gets one added."""
    if has_block(text):
        return text
    if not text:
        return BLOCK + "\n"
    return text + ("" if text.endswith("\n") else "\n") + "\n" + BLOCK + "\n"


def strip_block(text):
    """Remove exactly what add_block inserted, so strip_block(add_block(t)) == t.

    Surgical rather than "rstrip and rebuild": whitespace herd did not add must
    survive, and a file that never had a block comes back byte-identical.
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
