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

read_input
# payload_read, as every hook does — see common.sh. One field, so a shift is not
# reachable here (valid_sid rejects a mangled id and there is nothing after it to
# displace); it uses the shared reader so that all five parse payloads the same
# way and no one has to remember which two were exceptions.
payload_read '.session_id' SID
valid_sid "$SID" || exit 0

now_pair    # one fork, gives ISO + epoch; the throttle needs epoch

# No ISO stamp, nothing worth writing: W4_event would set last_event_at NULL, and
# the attention rule reads NULL as "no signal" — worse than not writing at all.
# Log it, because a hook that vanishes silently is the bug class this file keeps
# hitting.
if [ -z "$NOW_ISO" ]; then
    herd_log "clock unavailable — skipping the activity write"
    exit 0
fi

TFILE="$HERD_RUNTIME/herd-tool-$SID"
# BOTH sides of the subtraction must be numeric. LAST was guarded; NOW_EPOCH was
# not, and it failed in the UNSAFE direction: a non-numeric NOW_EPOCH (a partial
# `date`, an unexpected format) makes $((NOW_EPOCH - LAST)) nonsense, and the
# throttle swallows the write. The activity clock then never advances and a busy
# session reads silent — the exact failure this hook exists to prevent. A usable
# ISO with an unusable epoch means: skip the throttle, take the write.
case "$NOW_EPOCH" in
    ''|*[!0-9]*) NOW_EPOCH="" ;;
esac
if [ -n "$NOW_EPOCH" ] && [ -f "$TFILE" ]; then
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
# Skipped when the clock is unusable — writing "" would make the next read's
# garbage guard the only thing standing between us and a permanently stuck window.
if [ -n "$NOW_EPOCH" ]; then
    printf '%s\n' "$NOW_EPOCH" > "$TFILE.tmp.$$" 2>/dev/null &&
        mv -f "$TFILE.tmp.$$" "$TFILE" 2>/dev/null
    rm -f "$TFILE.tmp.$$" 2>/dev/null      # the && skipped the mv: leave no debris
fi
exit 0
