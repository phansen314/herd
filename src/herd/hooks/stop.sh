#!/bin/bash
# Stop — the turn ended and Claude wants input. THE 'waiting' SIGNAL.
# MUST EXIT 0: Stop is blocking, exit 2 would prevent Claude finishing its turn.
#
# ${BASH_SOURCE%/*} unchanged with no dir component -> silent no-op; fail loud.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

read_input
# One field, so a shift is unreachable; the shared reader is used anyway so all
# five hooks parse payloads identically. See common.sh.
payload_read '.session_id' SID
valid_sid "$SID" || exit 0
now_pair

# status -> waiting and RE-ARM attention (W6d clears any silence so the rule may
# trip fresh) — both in ONE txn. See DESIGN.md#attention.
export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO" \
       HERD_P_status="waiting" HERD_P_etype="stop"
run_tx W4_event W6d_rearm_sid >/dev/null 2>&1
exit 0
