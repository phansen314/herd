#!/bin/bash
# SessionEnd — the only hook-driven death (W4_end). MUST be registered BLOCKING:
# an async hook can be killed on exit, leaving stopped_at NULL. See DESIGN.md#per-hook-notes.
#
# ${BASH_SOURCE%/*} unchanged with no dir component -> silent no-op; fail loud
# (exit 1, non-blocking). Never exit 2.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

INPUT=$(cat)
SID=$(jq_in -r '.session_id // empty')
valid_sid "$SID" || exit 0
now_pair

# Mark the death. stopped_at frees the window/job via the liveness JOIN.
export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO"
run W4_end >/dev/null 2>&1

rm -f "$HERD_RUNTIME/herd-tool-$SID" 2>/dev/null
exit 0
