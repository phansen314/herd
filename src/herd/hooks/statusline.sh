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

read_input

# ── parse: ONE jq into \x1f-separated fields ──────────────────────────────
# \x1f (Unit Separator), NOT tab (tab is IFS whitespace and collapses empties).
# model is an OBJECT here (.model.id). resets_at is a UNIX EPOCH.
IFS=$'\x1f' read -r SID MODEL SNAME CTX COST RL5 RL5RESET RL7 RL7RESET CWD API_MS \
                    CTXSIZE OCWD LADD LDEL TOKIN TOKOUT VER GWT EXC200 OSTYLE \
                    RL5FMT RL7FMT <<JQ
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
  .output_style.name,
  # The rate-limit reset stamps, FORMATTED HERE. These were two "date -d @epoch"
  # forks per tick (1.25ms measured) on the ~1/sec/session path, doing what the jq
  # we already fork does for free. strflocaltime is local-TZ, as "date -d" was.
  #
  # NO APOSTROPHES IN THESE COMMENTS: the whole filter is one single-quoted bash
  # string, so one would end it and hand the rest of the program to the shell.
  #
  # Padded %I/%m/%d with the leading zeros sub()bed off, NOT the GNU-only %-I/%-m:
  # jq calls the system strftime, and BSD has no "-" flag, so asking for it on
  # macOS emits the format literally. sub() belongs to jq and behaves the same on
  # both — which is also why the old BSD "date -r" fallback disappears with it.
  #
  # GUARD ON TYPE, AND STILL WRAP IN try. strflocaltime raises on a non-number,
  # and a raise ABORTS THE WHOLE FILTER — so one unexpected field type emptied
  # all 23 outputs, rendered a bare context percentage and sank NOTHING to the
  # DB. The `date -d` forks this replaced degraded far better: they lost only
  # their own segment. Moving the formatting into jq coupled one optional nested
  # field to every other field, so it has to fail like the forks did, per-field.
  (try (if (.rate_limits.five_hour.resets_at | type) == "number"
        then (.rate_limits.five_hour.resets_at | strflocaltime("%I:%M%p")
              | sub("^0"; ""))
        else "" end) catch ""),
  (try (if (.rate_limits.seven_day.resets_at | type) == "number"
        then (.rate_limits.seven_day.resets_at | strflocaltime("%m/%d %I:%M%p")
              | sub("^0"; "") | sub("/0"; "/") | sub(" 0"; " "))
        else "" end) catch "")
]
# STRIP NEWLINES, in every field. The 23 values are joined with \u001f and read by
# ONE `read`, which stops at the first newline — so a single one anywhere emptied
# every field AFTER it. A /rename can carry a newline, and a directory name may
# legally contain one.
#
# The damage is a frozen statusline, NOT a corrupted row: W5_statusline COALESCEs
# every column against its current value, so the NULLs those empty fields bind to
# are ignored and the old values SURVIVE — verified. What breaks is that they
# never move again. Cost, context, branch and both rate limits stop updating for
# the life of the session, while the rendered line drops every segment after the
# name and shows a permanent 0%. Nothing is logged, because nothing failed.
| map(. // "" | tostring | gsub("[\\n\\r]"; " ")) | join("\u001f")')
JQ

# ── pure-bash git branch (zero forks): walk CWD -> .git/HEAD ───────────────
BRANCH=""
git_branch_of() {
    local dir="$1" g head ref
    # ABSOLUTE ONLY. ${dir%/*} returns the string UNCHANGED when there is no slash
    # left, so a relative cwd ("src", or anything with no leading /) never shrank
    # and this spun forever — verified. In a hook that reruns about once a second
    # and must never block, that is a core at 100% until Claude's timeout kills it.
    case "$dir" in
        /*) ;;
        *)  return 1 ;;
    esac
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
        [ -n "$RL5FMT" ] && seg="$seg resets $RL5FMT"
        L2+=("$seg")
    fi

    # 7d rate limit — reset includes M/D since the window spans days.
    if [[ "$RL7" =~ ^-?[0-9]+(\.[0-9]+)?$ ]]; then
        printf -v n '%.0f' "$RL7"
        seg="7d ${n}%"
        [ -n "$RL7FMT" ] && seg="$seg resets $RL7FMT"
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

# ── fingerprint: skip ALL DB work when nothing changed. Cache is 3 fixed lines:
# FP, L1, L2.
#
# It covers every field we RENDER *and* every field we SINK — the second set is
# the bigger one (tokens, line counts, version, output style render nothing), and
# it has to be there or a tick that changes only a sunk field would take a cache
# hit and never reach the DB.
#
# The consequence is worth stating plainly, because it is easy to read this block
# as a hot-path optimization and it is not: token counts move on essentially every
# tick of an ACTIVE session, so an active session misses the cache basically
# always. This is an IDLE-path optimization. What an active herd pays is the fork
# count on the miss path below — see DECISIONS.md#statusline-forks.
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
        # epoch() is hand-rolled because mktime() is a GAWK EXTENSION. macOS ships
        # one-true-awk, which has no time functions and aborts at parse — and with
        # stderr dropped that surfaced as BURN="" and a 🔥 segment that silently
        # never rendered on a Mac. Only POSIX awk is used below.
        #
        # Both stamps are ISO-8601 UTC, so the days-from-civil arithmetic is exact
        # (Hinnant's algorithm, era = 400-year cycle of 146097 days; -719468 shifts
        # the 0000-03-01 epoch to 1970-01-01). Doing it in UTC also drops a bug the
        # mktime version had: it fed UTC digits to a LOCAL-time parser, and the
        # offset only cancelled in b-a outside a DST transition.
        #
        # epoch() returns -1 on an unparseable stamp; the a<=0/b<=0 guard stops a
        # bogus "$0.00/h". Sub-cent rates are noise, hidden rather than shown as 0.
        BURN=$(awk -v c="$COST" -v p="$PREV_COST" -v t0="$PREV_AT" -v now="$NOW_ISO" '
            function epoch(s,   y,mo,d,h,mi,se,era,yoe,doy,doe,days) {
                if (s !~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9][T ][0-9][0-9]:[0-9][0-9]:[0-9][0-9]/)
                    return -1
                y=substr(s,1,4)+0; mo=substr(s,6,2)+0; d=substr(s,9,2)+0
                h=substr(s,12,2)+0; mi=substr(s,15,2)+0; se=substr(s,18,2)+0
                if (mo<1 || mo>12 || d<1 || d>31) return -1
                y -= (mo<=2)                       # March-based year
                era = int(y/400); yoe = y - era*400
                doy = int((153*(mo + (mo>2 ? -3 : 9)) + 2)/5) + d-1
                doe = yoe*365 + int(yoe/4) - int(yoe/100) + doy
                days = era*146097 + doe - 719468
                return days*86400 + h*3600 + mi*60 + se
            }
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
rm -f "$CACHE.tmp.$$" 2>/dev/null      # the && skipped the mv: leave no debris,
                                       # as post_tool_use.sh already does

emit "$L1S" "$L2S"
exit 0
