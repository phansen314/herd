#!/bin/bash
# PostToolUse — HOT PATH, fires per tool call. Cannot skip the DB (must advance
# last_event_at or a busy session reads silent and pages you), so it THROTTLES:
# one write per HERD_TOOL_THROTTLE window. See DESIGN.md#per-hook-notes.
#
# ${BASH_SOURCE%/*} unchanged with no dir component -> silent no-op; fail loud
# (exit 1, non-blocking). Never exit 2.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

HERD_TOOL_THROTTLE="${HERD_TOOL_THROTTLE:-2}"

INPUT=$(cat)
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
valid_sid "$SID" || exit 0

now_pair    # one fork, gives ISO + epoch; the throttle needs epoch

TFILE="$HERD_RUNTIME/herd-tool-$SID"
if [ -f "$TFILE" ]; then
    IFS= read -r LAST < "$TFILE" 2>/dev/null
    case "$LAST" in
        ''|*[!0-9]*) ;;   # garbage -> fall through and write
        *) [ $((NOW_EPOCH - LAST)) -lt "$HERD_TOOL_THROTTLE" ] && exit 0 ;;
    esac
fi

# advance the activity clock — the single lifecycle write (no events log).
export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO" \
       HERD_P_status="working" HERD_P_etype="tool"
run W4_event >/dev/null 2>&1

# tempfile+rename: a torn write must not leave a partial epoch for the next tick.
printf '%s\n' "$NOW_EPOCH" > "$TFILE.tmp.$$" 2>/dev/null && mv -f "$TFILE.tmp.$$" "$TFILE" 2>/dev/null
exit 0
