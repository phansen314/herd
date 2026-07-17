#!/bin/bash
# SessionStart. Fires with source = startup|resume|clear|compact.
#
# Two paths:
#   W2  adopt — herd already has a row for this kitty window (spawned by herd,
#       or discovered by reconcile) that is waiting for Claude's UUID. Joins on
#       (socket, window_id) from the ENVIRONMENT: the hook runs inside the
#       window, so $KITTY_WINDOW_ID is free — no pid needed to ROUTE the write.
#   W2b insert — no such row (not in kitty, or herd never saw this window).
#
# pid: this blocking hook ALSO captures claude's pid (claude_pid ppid-walk) and
# stamps it on the row — the reaper's liveness oracle. SPIKE-1 (pid must come from
# kitty) is overturned: the hook is a live descendant of claude, so the walk is
# exact and cleaner than kitten @ ls. Only this blocking hook does it; async hooks
# can be reparented away from claude.
#
# source='compact' and 'resume' are the SAME session continuing: W2b preserves
# started_at and clears stopped_at, so duration reflects total age.
# Resolve our own directory. ${BASH_SOURCE%/*} returns the string UNCHANGED
# when invoked with no directory component (`bash session_start.sh`), which
# yields "session_start.sh/common.sh: Not a directory", leaves every helper
# undefined, and — because hooks exit 0 — makes the hook a SILENT no-op that
# reports success. Fail loudly instead: exit 1 is a non-blocking error whose
# stderr shows in the transcript. Never exit 2; that would block Claude.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

INPUT=$(cat)     # NEVER $(</dev/stdin) — Claude's invocation makes that empty.

{ read -r SID; read -r CWD; read -r MODEL; read -r TRANSCRIPT; } <<JQ
$(printf '%s' "$INPUT" | jq -r '.session_id // "", .cwd // "", .model // "", .transcript_path // ""' 2>/dev/null)
JQ
# .model is a STRING on hook payloads (it is an OBJECT {id,display_name} on the
# statusline payload — same word, different shape).

valid_sid "$SID" || exit 0
now_pair

export HERD_P_session_id="$SID" HERD_P_cwd="$CWD" HERD_P_model="$MODEL" \
       HERD_P_transcript="$TRANSCRIPT" HERD_P_now="$NOW_ISO"

# claude's pid, walked from this (blocking) hook — see claude_pid() in common.sh.
# Empty when the walk fails; bind() then stores NULL and the reaper simply skips the
# row. Claim it FIRST (own txn) so any stale live holder is freed before we stamp it,
# keeping idx_sessions_pid_live satisfiable. Independent of kitty.
export HERD_P_pid="$(claude_pid)"
run W2c_pid_claim >/dev/null 2>&1

ADOPTED=0; IN_KITTY=0
if [ -n "${KITTY_WINDOW_ID:-}" ] && [ -n "${KITTY_LISTEN_ON:-}" ]; then
    IN_KITTY=1
    export HERD_P_socket="$KITTY_LISTEN_ON" HERD_P_win="$KITTY_WINDOW_ID"
    ADOPTED=$(run W2_adopt "SELECT changes();" 2>/dev/null)
fi

# W2 missed: no herd row for this window. Insert the session (tier 1) and — when
# we know the window — its placement (tier 2, source='hook') in ONE transaction,
# so a user-started `claude` is a first-class tracked session (pid-fillable by
# reconcile, not duplicated). Outside kitty there is no window to record; W2b
# stands alone (check 49).
if [ "$ADOPTED" != "1" ]; then
    if [ "$IN_KITTY" = "1" ]; then
        run_tx W2b_insert W2b_placement >/dev/null 2>&1
    else
        run W2b_insert >/dev/null 2>&1
    fi
fi

export HERD_P_etype="start" HERD_P_raw=""
run W4_event_log >/dev/null 2>&1
exit 0
