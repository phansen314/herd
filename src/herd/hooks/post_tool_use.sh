#!/bin/bash
# PostToolUse — HOT PATH, fires on every tool call.
#
# herd CANNOT use klawde's fast path (stat a flag file, exit without touching
# sqlite3). klawde's PostToolUse only clears an approval flag, so skipping the
# DB is free. herd's must advance last_event_at: if it doesn't, a session
# actively running tools reads as SILENT and the pager fires at you. That is
# the entire two-clocks thesis, and gating the write on a status change is
# precisely the bug (writes.sql W4) that froze the clock.
#
# So: throttle instead of skip. Never write more than once per window; the
# silence rule works in minutes, so 2s of staleness is invisible to it, while
# a burst of tool calls stops taking the write lock over and over.
# Resolve our own directory. ${BASH_SOURCE%/*} returns the string UNCHANGED
# when invoked with no directory component (`bash session_start.sh`), which
# yields "session_start.sh/common.sh: Not a directory", leaves every helper
# undefined, and — because hooks exit 0 — makes the hook a SILENT no-op that
# reports success. Fail loudly instead: exit 1 is a non-blocking error whose
# stderr shows in the transcript. Never exit 2; that would block Claude.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

HERD_TOOL_THROTTLE="${HERD_TOOL_THROTTLE:-2}"

INPUT=$(cat)
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
valid_sid "$SID" || exit 0

now_pair    # one fork, gives ISO + epoch; the throttle needs epoch anyway

TFILE="$HERD_RUNTIME/herd-tool-$SID"
if [ -f "$TFILE" ]; then
    IFS= read -r LAST < "$TFILE" 2>/dev/null
    case "$LAST" in
        ''|*[!0-9]*) ;;   # garbage -> fall through and write
        *) [ $((NOW_EPOCH - LAST)) -lt "$HERD_TOOL_THROTTLE" ] && exit 0 ;;
    esac
fi

export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO" \
       HERD_P_status="working" HERD_P_etype="tool"
run W4_event >/dev/null 2>&1

# raw_json is NULL here BY CONTRACT — this fires per tool call and the events
# table is unbounded.
export HERD_P_raw=""
run W4_event_log >/dev/null 2>&1

# tempfile+rename: a torn write would leave a partial epoch for the next tick
# to read, and a non-numeric one falls through to an unthrottled write.
printf '%s\n' "$NOW_EPOCH" > "$TFILE.tmp.$$" 2>/dev/null && mv -f "$TFILE.tmp.$$" "$TFILE" 2>/dev/null
exit 0
