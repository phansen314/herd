#!/bin/bash
# statusLine command — the ONLY source of per-session metrics (context %, cost,
# rate limits). Fires ~1/sec/session, so fork-light. Two jobs: SINK metrics to
# the DB and RENDER a two-line emoji status. Renders EVERY tick; DB work only on
# a fingerprint change. bash 3.2. Exits 0 always. See DESIGN.md#statuslinesh.
#   L1: ⬢ name | 🧠 ctx | 📁 cwd | 🌿 branch | 🤖 model | 💰 cost | 🔥 burn | ⌛ api
#   L2: ⏱️ 5h N% resets T | 7d N% resets M/D T
# `⬢ name` = Claude's session_name (tier-1 payload fact), NOT herd's job_name.
#
# ${BASH_SOURCE%/*} unchanged with no dir component -> silent no-op; fail loud.
__d="${BASH_SOURCE%/*}"; [ "$__d" = "${BASH_SOURCE}" ] && __d="."
. "$__d/common.sh" || { echo "herd: cannot source $__d/common.sh" >&2; exit 1; }

INPUT=$(cat)

# ── parse: ONE jq into \x1f-separated fields ──────────────────────────────
# \x1f (Unit Separator), NOT tab (tab is IFS whitespace and collapses empties).
# model is an OBJECT here (.model.id). resets_at is a UNIX EPOCH.
IFS=$'\x1f' read -r SID MODEL SNAME CTX COST RL5 RL5RESET RL7 RL7RESET CWD API_MS \
                    CTXSIZE OCWD LADD LDEL TOKIN TOKOUT VER GWT EXC200 OSTYLE <<JQ
$(jq_in -rj '[
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
  .cost.total_api_duration_ms,
  .context_window.context_window_size,
  .worktree.original_cwd,
  .cost.total_lines_added,
  .cost.total_lines_removed,
  .context_window.total_input_tokens,
  .context_window.total_output_tokens,
  .version,
  .workspace.git_worktree,
  (if .exceeds_200k_tokens then 1 else 0 end),
  .output_style.name
] | map(. // "" | tostring) | join("\u001f")')
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
                g="${g#gitdir: }"
                # RELATIVE gitdirs are the norm for submodules and worktrees
                # ("gitdir: ../../.git/modules/foo"). Resolved against the HOOK's
                # cwd instead of $dir they either miss — dropping the branch — or,
                # worse, hit and report a DIFFERENT repo's HEAD into git_branch.
                case "$g" in
                    /*) ;;                                 # absolute: as-is
                    *)  g="$dir/$g" ;;
                esac
                head="$g/HEAD"
            fi
            [ -f "$head" ] || return 1
            IFS= read -r ref < "$head" 2>/dev/null
            case "$ref" in
                "ref: refs/heads/"*) BRANCH="${ref#ref: refs/heads/}" ;;
                ?*)                   BRANCH="${ref:0:7}" ;;   # detached: short sha
            esac
            return 0
        fi
        dir="${dir%/*}"
    done
    return 1
}
[ -n "$CWD" ] && git_branch_of "$CWD"

# ── render: two-line emoji layout. Sets L1S / L2S. Segments hide when empty. ─
L1S=""; L2S=""
render() {
    local name="$1" burn="$2"
    local L1=() L2=() seg t n rest family ver mins hrs rmin apifmt

    [ -n "$name" ] && L1+=("⬢ $name")

    # 🧠 context — always renders, defaults to 0.
    n=0
    [[ "$CTX" =~ ^-?[0-9]+(\.[0-9]+)?$ ]] && printf -v n '%.0f' "$CTX"
    L1+=("🧠 ${n}%")

    [ -n "$CWD" ] && L1+=("📁 ${CWD##*/}")
    [ -n "$BRANCH" ] && L1+=("🌿 $BRANCH")

    # 🤖 model — strip claude- prefix + trailing 8-digit date, title-case family.
    if [ -n "$MODEL" ]; then
        rest="${MODEL#claude-}"
        case "$rest" in
            *-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]) rest="${rest%-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]}" ;;
        esac
        family="${rest%%-*}"
        ver="${rest#*-}"; ver="${ver//-/.}"
        case "$family" in
            opus)   family=Opus ;;
            sonnet) family=Sonnet ;;
            haiku)  family=Haiku ;;
        esac
        L1+=("🤖 $family $ver")
    fi

    # 💰 cost — always 2 dp.
    if [[ "$COST" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        printf -v t '$%.2f' "$COST"
        L1+=("💰 $t")
    fi

    # 🔥 burn — herd's own: $/h since the last prev_cost sample.
    [ -n "$burn" ] && L1+=("🔥 \$$burn/h")

    # ⌛ API duration — ms -> HhMm or Mm. Pure arithmetic. Hidden at 0.
    if [[ "$API_MS" =~ ^[0-9]+$ ]] && [ "$API_MS" -gt 0 ]; then
        mins=$(( API_MS / 60000 )); hrs=$(( mins / 60 )); rmin=$(( mins % 60 ))
        if [ "$hrs" -gt 0 ]; then apifmt="${hrs}h${rmin}m"; else apifmt="${mins}m"; fi
        L1+=("⌛ $apifmt API")
    fi

    # ⏱️ 5h rate limit — reset local TZ, 12h, no date. GNU %-I / BSD padded fallback.
    if [[ "$RL5" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        printf -v n '%.0f' "$RL5"
        seg="⏱️ 5h ${n}%"
        if [ -n "$RL5RESET" ]; then
            t=$(date -d "@$RL5RESET" +'%-I:%M%p' 2>/dev/null \
                || date -r "$RL5RESET" +'%I:%M%p' 2>/dev/null)
            [ -n "$t" ] && seg="$seg resets $t"
        fi
        L2+=("$seg")
    fi

    # 7d rate limit — reset includes M/D since the window spans days.
    if [[ "$RL7" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        printf -v n '%.0f' "$RL7"
        seg="7d ${n}%"
        if [ -n "$RL7RESET" ]; then
            t=$(date -d "@$RL7RESET" +'%-m/%-d %-I:%M%p' 2>/dev/null \
                || date -r "$RL7RESET" +'%m/%d %I:%M%p' 2>/dev/null)
            [ -n "$t" ] && seg="$seg resets $t"
        fi
        L2+=("$seg")
    fi

    L1S=""; for seg in "${L1[@]}"; do L1S="${L1S:+$L1S | }$seg"; done
    L2S=""; for seg in "${L2[@]}"; do L2S="${L2S:+$L2S | }$seg"; done
}

# Emit L1, then L2 on its own line when non-empty. No trailing newline.
emit() {
    if [ -n "$2" ]; then printf '%s\n%s' "$1" "$2"; else printf '%s' "$1"; fi
}

CACHE=""
valid_sid "$SID" && CACHE="$HERD_RUNTIME/herd-stline-$SID"

# ── fingerprint: skip ALL DB work when nothing changed. Covers every rendered
# field so a hit can't show a stale line. Cache is 3 fixed lines: FP, L1, L2.
FP="$MODEL|$SNAME|$CTX|$COST|$RL5|$RL5RESET|$RL7|$RL7RESET|$CWD|$BRANCH|$API_MS"
FP="$FP|$CTXSIZE|$OCWD|$LADD|$LDEL|$TOKIN|$TOKOUT|$VER|$GWT|$EXC200|$OSTYLE"
if [ -n "$CACHE" ] && [ -f "$CACHE" ]; then
    { IFS= read -r PREV_FP; IFS= read -r C1; IFS= read -r C2; } < "$CACHE" 2>/dev/null
    if [ "$FP" = "$PREV_FP" ]; then
        emit "$C1" "$C2"
        exit 0
    fi
fi

# A statusline for a session we cannot key on: render payload-only, no DB.
if [ -z "$CACHE" ]; then
    render "$SNAME" ""; emit "$L1S" "$L2S"
    exit 0
fi

# ── changed: SINK to the DB ───────────────────────────────────────────────
now_pair
export HERD_P_session_id="$SID" HERD_P_model="$MODEL" HERD_P_sname="$SNAME" \
       HERD_P_ctx="$CTX" HERD_P_cost="$COST" HERD_P_branch="$BRANCH" \
       HERD_P_rl5="$RL5" HERD_P_rl5reset="$RL5RESET" \
       HERD_P_rl7="$RL7" HERD_P_rl7reset="$RL7RESET" HERD_P_now="$NOW_ISO" \
       HERD_P_ctxsize="$CTXSIZE" HERD_P_ocwd="$OCWD" \
       HERD_P_ladd="$LADD" HERD_P_ldel="$LDEL" \
       HERD_P_tokin="$TOKIN" HERD_P_tokout="$TOKOUT" \
       HERD_P_ver="$VER" HERD_P_gwt="$GWT" \
       HERD_P_exc200="$EXC200" HERD_P_ostyle="$OSTYLE" HERD_P_apims="$API_MS"

CH=$(run W5_statusline "SELECT changes();" 2>/dev/null); RC=$?

# ── Path C: statusline adopts a reconciled-but-unadopted row in this window,
# when W5 matched nothing AND we are in kitty (env inherited from claude).
#
# GATE ON RC, NOT ON $CH ALONE. run prints "0" when the statement succeeded and
# matched no row, but "" when it FAILED — and a locked DB is the common failure
# (busy_timeout is 3s, statusline fires ~1/sec per session, the fingerprint moves
# every tick so the cache can't absorb it). Treating an error as "not adopted"
# spent the timeout, then an adopt, then a retry: 9s of stall per render, exactly
# when the DB is already contended. An error means we learned nothing about
# adoption, so the only correct move is to skip and render from the payload.
if [ "$RC" -eq 0 ] && [ "$CH" = "0" ] &&
   [ -n "${KITTY_WINDOW_ID:-}" ] && [ -n "${KITTY_LISTEN_ON:-}" ]; then
    export HERD_P_socket="$KITTY_LISTEN_ON" HERD_P_win="$KITTY_WINDOW_ID"
    run W5b_adopt >/dev/null 2>&1
    CH=$(run W5_statusline "SELECT changes();" 2>/dev/null) || CH=""
fi

# ── render input (burn rate) — one read on the miss path. R_statusline feeds
# only the prev_cost pair (pure tier-1); the ⬢ name comes from the payload.
BURN=""
if [ "$CH" = "1" ]; then
    RB=$(run R_statusline 2>/dev/null)          # prev_cost|prev_sampled
    IFS='|' read -r PREV_COST PREV_AT <<< "$RB"
    if [ -n "$COST" ] && [ -n "$PREV_COST" ] && [ -n "$PREV_AT" ]; then
        # mktime() returns -1 on an unparseable stamp; the a<=0/b<=0 guard stops a
        # bogus "$0.00/h". Sub-cent rates are noise, hidden rather than shown as 0.
        BURN=$(awk -v c="$COST" -v p="$PREV_COST" -v t0="$PREV_AT" -v now="$NOW_ISO" '
            function epoch(s){ gsub(/[-T:Z]/," ",s); return mktime(substr(s,1,19)) }
            BEGIN{
                a=epoch(t0); b=epoch(now)
                if (a<=0 || b<=0) exit
                dt=b-a
                if (dt<=0 || c<=p) exit
                r=(c-p)/dt*3600
                if (r>=0.01) printf "%.2f", r
            }' 2>/dev/null)
    fi
fi

render "$SNAME" "$BURN"

# ── cache atomically (tmp + rename — a torn write must not feed a false hit) ─
{ printf '%s\n%s\n%s\n' "$FP" "$L1S" "$L2S"; } > "$CACHE.tmp.$$" 2>/dev/null &&
    mv -f "$CACHE.tmp.$$" "$CACHE" 2>/dev/null

emit "$L1S" "$L2S"
exit 0
