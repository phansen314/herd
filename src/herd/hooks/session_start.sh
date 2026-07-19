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

read_input      # see common.sh: NOT $(</dev/stdin), which Claude leaves empty

{ read -r SID; read -r CWD; read -r MODEL; read -r TRANSCRIPT; } <<JQ
$(jq_in -r '.session_id // "", .cwd // "", .model // "", .transcript_path // ""')
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

# HERD_JOB is set in the ENVIRONMENT of a spawned claude (launch.py --env), so a
# spawned session can state its own job identity. Empty for a user-started claude,
# which binds to SQL NULL and makes every use below a no-op.
export HERD_P_job="${HERD_JOB:-}"

ADOPTED=0; IN_KITTY=0; W2_RC=0
if [ -n "${KITTY_WINDOW_ID:-}" ] && [ -n "${KITTY_LISTEN_ON:-}" ]; then
    IN_KITTY=1
    export HERD_P_socket="$KITTY_LISTEN_ON" HERD_P_win="$KITTY_WINDOW_ID"
    ADOPTED=$(run W2_adopt "SELECT changes();" 2>/dev/null); W2_RC=$?
fi

# Adoption by WINDOW missed, but this session knows which job it is. The window
# stamp is written by W1_spawn_window AFTER kitten @ launch returns, and that write
# can be stuck behind the WAL write lock for up to the 3s busy_timeout while this
# claude is already starting — so "no row for my window" does not mean "no
# reservation for me". Ask by name instead of creating a duplicate.
if [ "$ADOPTED" != "1" ] && [ "$W2_RC" -eq 0 ] && [ -n "${HERD_JOB:-}" ]; then
    ADOPTED=$(run W2_adopt_job "SELECT changes();" 2>/dev/null) || ADOPTED=0
    [ "$ADOPTED" = "1" ] && [ "$IN_KITTY" = "1" ] && run W2b_placement >/dev/null 2>&1
fi

# W2 FAILED (a locked DB is the common case) — "0 rows changed" and "we never
# found out" are different answers, and only the first means "no row to adopt".
# Falling through on a failure inserted a SECOND row for this window while the
# spawn reservation kept the job_name, so the live session was left unnamed and
# `herd jump <job>` could never find it again.
#
# But deferring unconditionally was worse than the bug it fixed. Path C only ever
# UPDATEs an existing reservation, so it can rescue a SPAWNED session and nothing
# else. For a user-started claude there is no row at all: W5_statusline matches
# nothing, W5b_adopt has nothing to adopt, and SessionStart never fires again — so
# one transient SQLITE_BUSY made that session invisible to herd for its entire life.
#
# So ask which situation this is. R_window_unadopted is a READ, and WAL readers do
# not block on a writer, meaning it answers reliably in exactly the case that made
# the write fail. A reservation exists -> defer, Path C has it. Nothing there ->
# insert, because an unnamed-but-visible session beats a lost one.
if [ "$W2_RC" -ne 0 ]; then
    DEFER=1
    if [ "$IN_KITTY" = "1" ]; then
        [ -n "$(run R_window_unadopted 2>/dev/null)" ] || DEFER=0
    else
        DEFER=0            # no window, so no reservation could ever exist
    fi
    if [ "$DEFER" = "1" ]; then
        herd_log "W2_adopt failed (rc=$W2_RC) — reservation present, deferring to statusline"
        exit 0
    fi
    herd_log "W2_adopt failed (rc=$W2_RC) — no reservation to adopt, inserting"
    ADOPTED=0
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
