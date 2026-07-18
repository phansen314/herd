#!/bin/bash
# SessionStart (source = startup|resume|clear|compact). Two paths: W2 adopt
# herd's placeholder row for this kitty window (join on socket+window_id from
# env), else W2b insert. Also captures claude's pid (ppid-walk) — only this
# BLOCKING hook can. See DESIGN.md#per-hook-notes.
#
# ${BASH_SOURCE%/*} returns the string UNCHANGED with no dir component, which
# would leave helpers undefined and make the hook a silent no-op. Fail loud
# (exit 1, non-blocking); never exit 2 (blocks Claude).
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

INPUT=$(cat)     # NEVER $(</dev/stdin) — Claude's invocation makes that empty.

{ read -r SID; read -r CWD; read -r MODEL; read -r TRANSCRIPT; } <<JQ
$(printf '%s' "$INPUT" | jq -r '.session_id // "", .cwd // "", .model // "", .transcript_path // ""' 2>/dev/null)
JQ
# .model is a STRING on hook payloads (an OBJECT on the statusline payload).

valid_sid "$SID" || exit 0
now_pair

export HERD_P_session_id="$SID" HERD_P_cwd="$CWD" HERD_P_model="$MODEL" \
       HERD_P_transcript="$TRANSCRIPT" HERD_P_now="$NOW_ISO"

# Claim the pid FIRST (own txn) so any stale live holder is freed before we stamp
# it, keeping idx_sessions_pid_live satisfiable. Empty -> NULL -> reaper skips.
export HERD_P_pid="$(claude_pid)"
run W2c_pid_claim >/dev/null 2>&1

ADOPTED=0; IN_KITTY=0; W2_RC=0
if [ -n "${KITTY_WINDOW_ID:-}" ] && [ -n "${KITTY_LISTEN_ON:-}" ]; then
    IN_KITTY=1
    export HERD_P_socket="$KITTY_LISTEN_ON" HERD_P_win="$KITTY_WINDOW_ID"
    ADOPTED=$(run W2_adopt "SELECT changes();" 2>/dev/null); W2_RC=$?
fi

# W2 FAILED (a locked DB is the common case) — "0 rows changed" and "we never
# found out" are different answers, and only the first means "no row to adopt".
# Falling through on a failure inserted a SECOND row for this window while the
# spawn reservation kept the job_name, so the live session was left unnamed and
# `herd jump <job>` could never find it again.
#
# Deferring is safe: statusline Path C (W5b_adopt) retries adoption on the same
# (socket, window_id) about once a second, and claims the reservation as soon as
# the lock clears — long before W3f's stranded sweep would reclaim it.
if [ "$W2_RC" -ne 0 ]; then
    herd_log "W2_adopt failed (rc=$W2_RC) — deferring to statusline adoption"
    exit 0
fi

# W2 missed: insert the session (+ its placement when in kitty) in ONE txn.
if [ "$ADOPTED" != "1" ]; then
    if [ "$IN_KITTY" = "1" ]; then
        run_tx W2b_insert W2b_placement >/dev/null 2>&1
    else
        run W2b_insert >/dev/null 2>&1
    fi
fi
# W2/W2b already set status='working' and last_event_type='start' — nothing else
# to record (the events log is gone).
exit 0
