#!/bin/bash
# SessionEnd — the only hook-driven death (W4_end). MUST be registered BLOCKING:
# an async hook can be killed on exit, leaving stopped_at NULL. See DESIGN.md#per-hook-notes.
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

# Mark the death. stopped_at frees the window/job via the liveness JOIN.
export HERD_P_session_id="$SID" HERD_P_now="$NOW_ISO"
run W4_end >/dev/null 2>&1

# Both per-session runtime files, or they leak one pair per session forever
# (unbounded under the ~/.herd/run fallback, which is not a tmpfs).
rm -f "$HERD_RUNTIME/herd-tool-$SID" "$HERD_RUNTIME/herd-stline-$SID" 2>/dev/null
exit 0
