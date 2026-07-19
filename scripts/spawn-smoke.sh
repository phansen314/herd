#!/usr/bin/env bash
# herd spawn smoke test — drives the one path unit tests cannot reach.
#
# Everything it checks was changed on 2026-07-18 and has never run against a real
# kitty: W2_adopt_job, the HERD_JOB env var, /clear job inheritance, and the
# adoption ordering. Unit tests drive the hooks by hand with a fake window id.
# This drives `kitten @ launch` for real.
#
# RUN FROM A KITTY WINDOW (it needs KITTY_LISTEN_ON). Read-only except for the
# spawn itself and `clean`.
#
#   bash scripts/spawn-smoke.sh            # phase 1: spawn + verify adoption
#   bash scripts/spawn-smoke.sh check      # phase 2: re-verify (after /clear)
#   bash scripts/spawn-smoke.sh clean      # drop the test rows when done
#
# Optional second arg overrides the job name (default: smoketest).
set -uo pipefail

DB="${HERD_DB:-$HOME/.herd/herd.db}"
ERRLOG="${HERD_ERRLOG:-$HOME/.herd/hook-errors.log}"
CMD="${1:-spawn}"
JOB="${2:-smoketest}"
HERD="$(command -v herd || echo "$HOME/code/herd/bin/herd")"
# The repo, from THIS script's location — not from $HERD, which is a symlink in
# ~/.local/bin and whose parent has no src/ (that silently broke the resolve check).
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

c()  { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
ok() { c '32' "  ✔ $1"; }
no() { c '31' "  ✘ $1"; }
hm() { c '33' "  ! $1"; }

q()  { sqlite3 -noheader "$DB" "$1" 2>/dev/null; }

rows() {
    sqlite3 -header -column "$DB" "
      SELECT s.id, substr(s.session_id,1,8) AS sess, COALESCE(h.job_name,'—') AS job,
             COALESCE(h.source,'—') AS src, COALESCE(h.window_id,'—') AS win,
             COALESCE(s.pid,'—') AS pid, s.status,
             CASE WHEN s.stopped_at IS NULL THEN 'LIVE' ELSE 'dead' END AS state
      FROM sessions s LEFT JOIN herd_sessions h ON h.session_pk = s.id
      ORDER BY s.id DESC LIMIT 8;" 2>/dev/null
}

verify() {
    echo; c '1' "current rows (newest first)"; rows; echo
    local live_pk job_src job_win job_sid dupes
    live_pk=$(q "SELECT s.id FROM sessions s JOIN herd_sessions h ON h.session_pk=s.id
                 WHERE h.job_name='$JOB' AND s.stopped_at IS NULL ORDER BY s.id DESC LIMIT 1")
    if [ -z "$live_pk" ]; then
        no "no LIVE session holds job '$JOB' — the job name was lost"
        hm "that is the bug class we fixed today; the rows above say which way"
        return 1
    fi
    job_sid=$(q "SELECT COALESCE(session_id,'') FROM sessions WHERE id=$live_pk")
    job_src=$(q "SELECT source FROM herd_sessions WHERE session_pk=$live_pk")
    job_win=$(q "SELECT COALESCE(window_id,'') FROM herd_sessions WHERE session_pk=$live_pk")
    dupes=$(q "SELECT COUNT(*) FROM sessions s JOIN herd_sessions h ON h.session_pk=s.id
               WHERE h.job_name='$JOB' AND s.stopped_at IS NULL")

    ok "job '$JOB' is held by live session #$live_pk"
    [ -n "$job_sid" ] && ok "adopted Claude's uuid: $job_sid" \
                      || no "session_id still NULL — nothing adopted the reservation"
    # source='spawn' means the reservation ITSELF was adopted (W2_adopt or
    # W2_adopt_job). source='hook' means W2b_insert made a new row — which is the
    # EXPECTED outcome after a /clear, and only suspicious otherwise. Telling the
    # two apart needs the predecessor: a /clear keeps the process, so the dead row
    # it replaced shares this row's pid and window.
    local pred
    if [ "$job_src" = "spawn" ]; then
        ok "route: adopted the reservation in place (source=spawn)"
    else
        pred=$(q "SELECT s.id FROM sessions s JOIN herd_sessions h ON h.session_pk=s.id
                  WHERE h.job_name='$JOB' AND s.stopped_at IS NOT NULL
                    AND s.pid = (SELECT pid FROM sessions WHERE id=$live_pk)
                    AND h.window_id = (SELECT window_id FROM herd_sessions WHERE session_pk=$live_pk)
                  ORDER BY s.id DESC LIMIT 1")
        if [ -n "$pred" ]; then
            ok "route: inherited from #$pred across /clear — same pid, same window"
            ok "  (that is the expected path here: W2_adopt cannot match a stopped predecessor)"
        else
            hm "route: a new row carries the name (source=$job_src) with no predecessor"
            hm "  it means W2_adopt/W2_adopt_job missed — check the errlog"
        fi
    fi
    [ -n "$job_win" ] && ok "placement recorded: window $job_win" \
                      || hm "no window_id yet (focus will fall back)"
    [ "$dupes" = "1" ] && ok "exactly one live holder of '$JOB'" \
                       || no "$dupes live holders of '$JOB' — ambiguous handle"

    echo; c '1' "herd's own view"
    "$HERD" ls | sed 's/^/  /'
    # Resolution is checked WITHOUT focusing. `herd jump <job>` focuses on a unique
    # match, which would yank you to the spawned tab and away from these results.
    echo; c '1' "does the handle resolve? (no focus — read-only)"
    local n
    n=$(cd "$REPO" && PYTHONPATH="$REPO/src" python3 -c "
import sys
from herd.cli import resolve
from herd.db import connect
from herd.daemon import DEFAULT_DB
print(len(resolve(connect(DEFAULT_DB, readonly=True), '$JOB')))" 2>/dev/null)
    case "$n" in
        1) ok "'$JOB' resolves to exactly 1 session — herd jump $JOB will go straight there" ;;
        0|"") hm "'$JOB' resolves to nothing (or the check could not run)" ;;
        *) no "'$JOB' resolves to $n sessions — jump will open the picker, not jump" ;;
    esac
}

case "$CMD" in
spawn)
    c '1' "── preflight ───────────────────────────────────────────────"
    [ -n "${KITTY_LISTEN_ON:-}" ] && ok "KITTY_LISTEN_ON=$KITTY_LISTEN_ON" || {
        no "KITTY_LISTEN_ON is empty — run this from a kitty window with"
        no "  allow_remote_control set, or spawn cannot launch anything"; exit 1; }
    [ -x "$HERD" ] && ok "herd: $HERD" || { no "herd not found"; exit 1; }
    [ -f "$DB" ] && ok "db: $DB" || { no "no db at $DB"; exit 1; }
    q "SELECT 1" >/dev/null && ok "db readable" || { no "db unreadable"; exit 1; }
    if [ -n "$(q "SELECT 1 FROM sessions s JOIN herd_sessions h ON h.session_pk=s.id
                  WHERE h.job_name='$JOB' AND s.stopped_at IS NULL")" ]; then
        no "job '$JOB' is already held by a live session"
        no "  finish that one, or: bash $0 clean"; exit 1
    fi
    ok "job '$JOB' is free"

    echo; c '1' "── spawning ────────────────────────────────────────────────"
    echo "  \$ herd spawn $JOB"
    "$HERD" spawn "$JOB" 2>&1 | sed 's/^/  /'

    resv=$(q "SELECT COALESCE(MAX(id),0) FROM sessions")   # the reservation W1 just made

    echo; c '1' "── waiting for the new claude to reach SessionStart ─────────"
    echo "  (a NEW TAB should have opened. If it asks whether you trust the"
    echo "   folder, ANSWER IT — that prompt blocks SessionStart, and stalling"
    echo "   past 120s is one of the cases we fixed.)"
    for i in $(seq 40); do
        sid=$(q "SELECT session_id FROM sessions s JOIN herd_sessions h ON h.session_pk=s.id
                 WHERE h.job_name='$JOB' AND s.stopped_at IS NULL AND s.session_id IS NOT NULL")
        [ -n "$sid" ] && break
        sleep 1; printf '.'
    done; echo
    # A duplicate is a row created AFTER the reservation, which is what W2b_insert
    # does when both adopt routes miss. Comparing against MAX(id) from BEFORE the
    # spawn was wrong and warned on every successful run: the spawn creates the
    # reservation itself, so the max always moves.
    dup=$(q "SELECT COUNT(*) FROM sessions WHERE id > $resv")
    [ "$dup" = "0" ] && ok "no duplicate row: nothing was created after reservation #$resv" \
                     || hm "$dup row(s) created after reservation #$resv — adoption missed"

    c '1' "── verify ──────────────────────────────────────────────────"
    verify
    echo; c '1' "── next, by hand ───────────────────────────────────────────"
    echo "  1. switch to the new tab and type:  /clear"
    echo "  2. come back here and run:          bash $0 check"
    echo "     -> the job name must SURVIVE the /clear (that is fef4e1b)"
    echo "  3. when done:                       bash $0 clean"
    ;;
check)  verify ;;
clean)
    pks=$(q "SELECT s.id FROM sessions s JOIN herd_sessions h ON h.session_pk=s.id
             WHERE h.job_name='$JOB'")
    [ -z "$pks" ] && { ok "nothing to clean for job '$JOB'"; exit 0; }
    echo "  removing sessions rows: $(echo "$pks" | tr '\n' ' ')"
    echo "  (close the spawned tab first if it is still open)"
    for pk in $pks; do q "DELETE FROM sessions WHERE id=$pk"; done
    ok "cleaned"
    ;;
*) echo "usage: bash $0 [spawn|check|clean] [job-name]"; exit 2 ;;
esac

echo; c '1' "── hook errors (last 5) ────────────────────────────────────"
tail -5 "$ERRLOG" 2>/dev/null | sed 's/^/  /' || echo "  (none)"
[ -s "$ERRLOG" ] || echo "  (empty — good)"
