#!/bin/bash
# statusLine.command — the ONLY source of per-session metrics (context %, cost,
# rate limits): lifecycle hooks never see them. Fires ~1/sec/session, so it must
# be fork-light. Two jobs: SINK metrics into herd's DB, and RENDER a compact
# herd line (this replaces klawde's slot in a chained statusline wrapper).
#
# bash 3.2. INPUT=$(cat) (never </dev/stdin). Exits 0 always. Renders EVERY tick
# (a statusLine command must print); DB work happens only on a fingerprint change.
#
# Resolve our own dir — ${BASH_SOURCE%/*} returns the string unchanged when
# invoked with no directory component, which would leave every helper undefined
# and make this a silent no-op. Fail loudly (exit 1, non-blocking) instead.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

INPUT=$(cat)

# ── parse: ONE jq into \x1f-separated fields ──────────────────────────────
# \x1f (Unit Separator), NOT tab: tab is IFS whitespace and collapses empty
# fields, shifting every later field. `model` is an OBJECT here (.model.id),
# unlike hook payloads where it is a string. resets_at is a UNIX EPOCH.
IFS=$'\x1f' read -r SID MODEL SNAME CTX COST RL5 RL5RESET RL7 RL7RESET CWD GWT <<JQ
$(printf '%s' "$INPUT" | jq -rj '[
  .session_id,
  .model.id,
  .session_name,
  .context_window.used_percentage,
  .cost.total_cost_usd,
  .rate_limits.five_hour.used_percentage,
  .rate_limits.five_hour.resets_at,
  .rate_limits.seven_day.used_percentage,
  .rate_limits.seven_day.resets_at,
  .cwd,
  .workspace.git_worktree
] | map(. // "" | tostring) | join("")' 2>/dev/null)
JQ

# ── pure-bash git branch (zero forks): walk CWD -> .git/HEAD ───────────────
BRANCH=""
git_branch_of() {
    local dir="$1" g head ref
    while [ -n "$dir" ] && [ "$dir" != "/" ]; do
        if [ -e "$dir/.git" ]; then
            if [ -d "$dir/.git" ]; then
                head="$dir/.git/HEAD"
            else
                IFS= read -r g < "$dir/.git" 2>/dev/null   # "gitdir: <path>"
                g="${g#gitdir: }"; head="$g/HEAD"
            fi
            [ -f "$head" ] || return 1
            IFS= read -r ref < "$head" 2>/dev/null
            case "$ref" in
                "ref: refs/heads/"*) BRANCH="${ref#ref: refs/heads/}" ;;
                ?*)                   BRANCH="${ref:0:12}" ;;   # detached: short sha
            esac
            return 0
        fi
        dir="${dir%/*}"
    done
    return 1
}
[ -n "$CWD" ] && git_branch_of "$CWD"

# ── render helper (composes the visible line from what we have) ────────────
# job/branch/burn are optional; ctx/cost come straight from the payload.
render_line() {
    local job="$1" burn="$2" out=""
    [ -n "$job" ]    && out="⬢ $job"
    [ -n "$BRANCH" ] && out="${out:+$out }⎇ $BRANCH"
    [ -n "$CTX" ]    && out="${out:+$out · }${CTX%.*}%"
    [ -n "$COST" ]   && out="${out:+$out · }\$$COST"
    [ -n "$burn" ]   && out="${out:+$out · }\$$burn/h"
    printf '%s' "$out"
}

CACHE=""
valid_sid "$SID" && CACHE="$HERD_RUNTIME/herd-stline-$SID"

# ── fingerprint: skip ALL DB work when nothing changed ────────────────────
FP="$MODEL|$SNAME|$CTX|$COST|$RL5|$RL7|$BRANCH"
if [ -n "$CACHE" ] && [ -f "$CACHE" ]; then
    IFS= read -r PREV_FP < "$CACHE" 2>/dev/null
    if [ "$FP" = "$PREV_FP" ]; then
        # unchanged — print the cached rendered line (line 2) and leave.
        { read -r _; IFS= read -r LINE; } < "$CACHE" 2>/dev/null
        printf '%s\n' "$LINE"
        exit 0
    fi
fi

# A statusline for a session we cannot key on: render payload-only, no DB.
if [ -z "$CACHE" ]; then
    render_line "" ""; printf '\n'
    exit 0
fi

# ── changed: SINK to the DB ───────────────────────────────────────────────
now_pair
export HERD_P_session_id="$SID" HERD_P_model="$MODEL" HERD_P_sname="$SNAME" \
       HERD_P_ctx="$CTX" HERD_P_cost="$COST" HERD_P_branch="$BRANCH" \
       HERD_P_rl5="$RL5" HERD_P_rl5reset="$RL5RESET" \
       HERD_P_rl7="$RL7" HERD_P_rl7reset="$RL7RESET" HERD_P_now="$NOW_ISO"

CH=$(run W5_statusline "SELECT changes();" 2>/dev/null)

# ── Path C: statusline adopts a reconciled-but-unadopted row in this window ─
# Only when W5 matched nothing AND we are in kitty (env inherited from claude).
if [ "$CH" != "1" ] && [ -n "${KITTY_WINDOW_ID:-}" ] && [ -n "${KITTY_LISTEN_ON:-}" ]; then
    export HERD_P_socket="$KITTY_LISTEN_ON" HERD_P_win="$KITTY_WINDOW_ID"
    run W5b_adopt >/dev/null 2>&1
    CH=$(run W5_statusline "SELECT changes();" 2>/dev/null)
fi

# ── render inputs (job name + burn) — one read on the miss path ───────────
JOB=""; BURN=""
if [ "$CH" = "1" ]; then
    RB=$(run R_statusline 2>/dev/null)          # job|prev_cost|prev_sampled
    IFS='|' read -r JOB PREV_COST PREV_AT <<< "$RB"
    # burn = (cost - prev_cost) / hours since prev sample. awk: one fork, miss-only.
    if [ -n "$COST" ] && [ -n "$PREV_COST" ] && [ -n "$PREV_AT" ]; then
        BURN=$(awk -v c="$COST" -v p="$PREV_COST" -v t0="$PREV_AT" -v now="$NOW_ISO" '
            function epoch(s,   cmd){ gsub(/[-T:Z]/," ",s); return mktime(substr(s,1,19)) }
            BEGIN{ dt=epoch(now)-epoch(t0); if(dt>0 && c>=p) printf "%.2f",(c-p)/dt*3600 }' 2>/dev/null)
    fi
fi

LINE=$(render_line "$JOB" "$BURN")

# ── cache atomically (tmp + rename — a torn write must not feed a false hit) ─
{ printf '%s\n%s\n' "$FP" "$LINE"; } > "$CACHE.tmp.$$" 2>/dev/null &&
    mv -f "$CACHE.tmp.$$" "$CACHE" 2>/dev/null

printf '%s\n' "$LINE"
exit 0
