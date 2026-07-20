#!/bin/bash
# Notification — only notification_type=permission_prompt means 'needs_approval'.
# idle_prompt is ignored: stop.sh already owns 'waiting'. See DESIGN.md#per-hook-notes.
#
# ${BASH_SOURCE%/*} unchanged with no dir component -> silent no-op; fail loud.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

read_input
# payload_read, like every other hook — see common.sh. The sentinel is not checked:
# NTYPE is compared against a literal below, so a shifted parse fails that test and
# exits 0, which is what a nonzero return would do anyway. valid_sid guards the id.
payload_read '.session_id, .notification_type' SID NTYPE

valid_sid "$SID" || exit 0
[ "$NTYPE" = "permission_prompt" ] || exit 0

now_pair
export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO" \
       HERD_P_status="needs_approval" HERD_P_etype="notify"
run W4_event >/dev/null 2>&1
exit 0
