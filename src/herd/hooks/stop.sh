#!/bin/bash
# Stop — the turn ended and Claude wants input. THE 'waiting' SIGNAL.
# MUST EXIT 0: Stop is blocking, exit 2 would prevent Claude finishing its turn.
#
# ${BASH_SOURCE%/*} unchanged with no dir component -> silent no-op; fail loud
# (exit 1, non-blocking). Never exit 2.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

INPUT=$(cat)
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
valid_sid "$SID" || exit 0
now_pair

# status -> waiting, log the event, and RE-ARM attention (W6d clears any silence
# so the rule may trip fresh) — all in ONE txn. See DESIGN.md#attention.
export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO" \
       HERD_P_status="waiting" HERD_P_etype="stop" HERD_P_raw=""
run_tx W4_event W4_event_log W6d_rearm_sid >/dev/null 2>&1
exit 0
