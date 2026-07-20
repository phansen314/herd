#!/bin/bash
# SessionStart (source = startup|resume|clear|compact). Two paths: W2 adopt
# herd's placeholder row for this kitty window (join on socket+window_id from
# env), else W2b insert. Also captures claude's pid (ppid-walk) — only this
# BLOCKING hook can. See DESIGN.md#per-hook-notes.
#
# ${BASH_SOURCE%/*} unchanged with no dir component -> silent no-op; fail loud.
# Every hook exits 1 at worst, never 2 — exit 2 blocks Claude.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

read_input      # see common.sh: NOT $(</dev/stdin), which Claude leaves empty

# payload_read, as every hook does — see common.sh. cwd is an arbitrary path that
# may legally contain a newline or a separator, and this hook fires ONCE per
# session, so a shifted row is never corrected.
payload_read '.session_id, .cwd, .model, .transcript_path' SID CWD MODEL TRANSCRIPT
PARSE_OK=$?
# .model is a STRING on hook payloads (an OBJECT on the statusline payload).

# A shifted parse must NOT cost the session its row: this hook alone creates it and
# captures claude's pid, and it does not fire again, so exiting here loses the
# session for its whole life. SID is field 1 and survives any shift; drop the three
# untrusted values and record the rest.
if [ "$PARSE_OK" -ne 0 ]; then
    herd_log "session_start: payload parse shifted (sentinel=[$HERD_PARSE_TAIL]) — cwd/model/transcript dropped"
    # "?" and not "": bind() maps EMPTY to SQL NULL and sessions.cwd is NOT NULL, so
    # blanking it fails W2b_insert and the session gets no row at all. model and
    # transcript_path are nullable.
    CWD="?"; MODEL=""; TRANSCRIPT=""
fi

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

# Adoption by WINDOW missed, but this session knows which job it is. W1_spawn_window
# writes the window stamp AFTER kitten @ launch returns and can sit behind the WAL
# write lock for the busy_timeout while this claude is already starting — so "no row
# for my window" does not mean "no reservation for me". Ask by name.
if [ "$ADOPTED" != "1" ] && [ "$W2_RC" -eq 0 ] && [ -n "${HERD_JOB:-}" ]; then
    ADOPTED=$(run W2_adopt_job "SELECT changes();" 2>/dev/null) || ADOPTED=0
    [ "$ADOPTED" = "1" ] && [ "$IN_KITTY" = "1" ] && run W2b_placement >/dev/null 2>&1
fi

# W2 FAILED (a locked DB is the common case) — "0 rows changed" and "we never found
# out" are different answers, and only the first means "no row to adopt". Falling
# through blindly inserts a SECOND row while the reservation keeps the job_name,
# leaving the live session unnamed. Deferring blindly is just as bad: the
# statusline's Path C only UPDATEs an existing reservation, so a user-started claude
# (no row anywhere, and SessionStart never fires again) is lost for its whole life.
#
# So ask which situation this is. R_window_unadopted is a READ and WAL readers do
# not block on a writer, so it answers reliably in exactly the case that made the
# write fail. Reservation -> defer to Path C. Nothing -> insert, because an
# unnamed-but-visible session beats a lost one.
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
