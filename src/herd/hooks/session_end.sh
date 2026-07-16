#!/bin/bash
# SessionEnd — the only hook-driven death (W4b). reason = clear|resume|logout|
# prompt_input_exit|bypass_permissions_disabled|other.
#
# REGISTER THIS BLOCKING, NEVER async: an async hook can be killed when the
# session exits, leaving stopped_at NULL and the row appearing live until
# reconcile notices. On `/clear`, Claude emits SessionEnd then SessionStart for
# the NEW session in the SAME kitty window — if this hasn't landed first, two
# sessions read as live in one window (harmless now — the liveness JOIN and
# reconcile's rebuild sort it out — but the death should still land promptly).
# Resolve our own directory. ${BASH_SOURCE%/*} returns the string UNCHANGED
# when invoked with no directory component (`bash session_start.sh`), which
# yields "session_start.sh/common.sh: Not a directory", leaves every helper
# undefined, and — because hooks exit 0 — makes the hook a SILENT no-op that
# reports success. Fail loudly instead: exit 1 is a non-blocking error whose
# stderr shows in the transcript. Never exit 2; that would block Claude.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

INPUT=$(cat)
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
valid_sid "$SID" || exit 0
now_pair

# Event log + death in ONE transaction: the forensic 'end' event and the
# stopped_at write land together or not at all. Log first so the trail is
# honest if W4_end ever changes to delete the row. Setting stopped_at makes
# every liveness JOIN see this session as dead — its job name and window slot
# are free automatically, no trigger.
export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO" HERD_P_etype="end" HERD_P_raw=""
run_tx W4_event_log W4_end >/dev/null 2>&1

rm -f "$HERD_RUNTIME/herd-tool-$SID" 2>/dev/null
exit 0
