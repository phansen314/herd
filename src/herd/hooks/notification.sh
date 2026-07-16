#!/bin/bash
# Notification. Only permission_prompt means "needs approval".
#
# notification_type is an enum: permission_prompt, idle_prompt, auth_success,
# elicitation_*, agent_needs_input, agent_completed. klawde must also treat
# idle_prompt carefully because it has no Stop hook; herd does, so idle_prompt
# is redundant here and is ignored outright — Stop already owns 'waiting'.
# Resolve our own directory. ${BASH_SOURCE%/*} returns the string UNCHANGED
# when invoked with no directory component (`bash session_start.sh`), which
# yields "session_start.sh/common.sh: Not a directory", leaves every helper
# undefined, and — because hooks exit 0 — makes the hook a SILENT no-op that
# reports success. Fail loudly instead: exit 1 is a non-blocking error whose
# stderr shows in the transcript. Never exit 2; that would block Claude.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

INPUT=$(cat)
{ read -r SID; read -r NTYPE; } <<JQ
$(printf '%s' "$INPUT" | jq -r '.session_id // "", .notification_type // ""' 2>/dev/null)
JQ

valid_sid "$SID" || exit 0
[ "$NTYPE" = "permission_prompt" ] || exit 0

now_pair
export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO" \
       HERD_P_status="needs_approval" HERD_P_etype="notify" HERD_P_raw=""
run_tx W4_event W4_event_log >/dev/null 2>&1
exit 0
