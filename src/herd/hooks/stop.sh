#!/bin/bash
# Stop — the turn ended and Claude wants input. THE 'waiting' SIGNAL.
#
# klawde wires no Stop hook at all, which is exactly why it has no idle state
# and has to strain notification_type='permission_prompt' to keep idle_prompt
# from wedging sessions in a false needs_approval. This hook is the reason herd
# doesn't have to.
#
# MUST EXIT 0. Stop is a BLOCKING event: exit 2 prevents Claude from finishing
# its turn. A bug in herd must never be able to do that.
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

export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO" \
       HERD_P_status="waiting" HERD_P_etype="stop"
run W4_event >/dev/null 2>&1
export HERD_P_raw=""
run W4_event_log >/dev/null 2>&1

# The turn ended, so any silence herd already paged about is over: clear the
# attention row and let the rule trip fresh. W6d_rearm_sid is the RE-ARM — ack
# means "I've seen THIS silence", not "never bother me about this session
# again". Goes through run()/writes.sql like every other write: no inlined SQL.
run W6d_rearm_sid >/dev/null 2>&1
exit 0
