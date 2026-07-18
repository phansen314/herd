#!/bin/bash
# SessionEnd — the only hook-driven death (W4_end). MUST be registered BLOCKING:
# an async hook can be killed on exit, leaving stopped_at NULL. See DESIGN.md#per-hook-notes.
#
# ${BASH_SOURCE%/*} unchanged with no dir component -> silent no-op; fail loud
# (exit 1, non-blocking). Never exit 2.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

INPUT=$(cat)
SID=$(printf '%s' "$INPUT" | jq -r '.session_id // empty' 2>/dev/null)
valid_sid "$SID" || exit 0
now_pair

# Event log + death in ONE txn. Log first so the trail stays honest if W4_end
# ever changes to delete the row. stopped_at frees the window/job via the JOIN.
export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO" HERD_P_etype="end" HERD_P_raw=""
run_tx W4_event_log W4_end >/dev/null 2>&1

rm -f "$HERD_RUNTIME/herd-tool-$SID" 2>/dev/null
exit 0
