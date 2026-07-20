#!/bin/bash
# Stop — the turn ended and Claude wants input. THE 'waiting' SIGNAL.
# MUST EXIT 0: Stop is blocking, exit 2 would prevent Claude finishing its turn.
#
# ${BASH_SOURCE%/*} unchanged with no dir component -> silent no-op; fail loud
# (exit 1, non-blocking). Never exit 2.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

read_input
# payload_read, as every hook does — see common.sh. One field, so a shift is not
# reachable here (valid_sid rejects a mangled id and there is nothing after it to
# displace); it uses the shared reader so that all five parse payloads the same
# way and no one has to remember which two were exceptions.
payload_read '.session_id' SID
valid_sid "$SID" || exit 0
now_pair

# status -> waiting and RE-ARM attention (W6d clears any silence so the rule may
# trip fresh) — both in ONE txn. See DESIGN.md#attention.
export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO" \
       HERD_P_status="waiting" HERD_P_etype="stop"
run_tx W4_event W6d_rearm_sid >/dev/null 2>&1
exit 0
