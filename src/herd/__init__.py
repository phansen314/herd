"""herd — track Claude Code sessions in a local SQLite database.

The version floor lives HERE, not in bin/herd: checking it in the wrapper would cost
a second interpreter start on every `herd ls`. This module is imported before any
other herd code, so a too-old interpreter is caught once, cheaply.

Deliberately pre-3.6 syntax — %-formatting, no f-strings. A version check that
SyntaxErrors on the versions it exists to reject reports nothing.
"""
import sys

MIN_PYTHON = (3, 9)

if sys.version_info < MIN_PYTHON:
    sys.stderr.write(
        "herd: Python %d.%d is too old — herd needs >= %d.%d (running %s)\n"
        % (sys.version_info[0], sys.version_info[1],
           MIN_PYTHON[0], MIN_PYTHON[1], sys.executable))
    sys.stderr.write(
        "  The hooks are bash and keep recording regardless; it is the CLI,\n"
        "  the daemon and the installer that need a newer interpreter.\n")
    raise SystemExit(1)
