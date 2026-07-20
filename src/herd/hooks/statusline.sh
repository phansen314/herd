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

# ── parse: ONE jq into \x1f-separated fields (see payload_read in common.sh) ─
# \x1f (Unit Separator), NOT tab (tab is IFS whitespace and collapses empties).
# model is an OBJECT here (.model.id). resets_at is a UNIX EPOCH.
payload_read '
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
  # The rate-limit reset stamps, FORMATTED HERE rather than by two "date -d @epoch"
  # forks per tick. strflocaltime is local-TZ, as "date -d" was.
  #
  # NO APOSTROPHES IN THESE COMMENTS: the whole filter is one single-quoted bash
  # string, so one would end it and hand the rest of the program to the shell.
  #
  # Padded %I/%m/%d with the leading zeros sub()bed off, NOT the GNU-only %-I/%-m:
  # jq calls the system strftime, and BSD has no "-" flag, so asking for it on
  # macOS emits the format literally. sub() behaves the same on both.
  #
  # GUARD ON TYPE, AND STILL WRAP IN try. strflocaltime raises on a non-number and
  # a raise ABORTS THE WHOLE FILTER — one unexpected field type would empty all 23
  # outputs and sink nothing. Formatting in jq couples one optional nested field to
  # every other field, so it must fail per-field the way the forks did.
  (try (if (.rate_limits.five_hour.resets_at | type) == "number"
        then (.rate_limits.five_hour.resets_at | strflocaltime("%I:%M%p")
              | sub("^0"; ""))
        else "" end) catch ""),
  (try (if (.rate_limits.seven_day.resets_at | type) == "number"
        then (.rate_limits.seven_day.resets_at | strflocaltime("%m/%d %I:%M%p")
              | sub("^0"; "") | sub("/0"; "/") | sub(" 0"; " "))
        else "" end) catch "")' \
    SID MODEL SNAME CTX COST RL5 RL5RESET RL7 RL7RESET CWD API_MS \
    CTXSIZE OCWD LADD LDEL TOKIN TOKOUT VER GWT EXC200 OSTYLE RL5FMT RL7FMT
PARSE_OK=$?

# ── the sentinel check, BEFORE anything reads a field: a shifted parse makes every
# value plausible and wrong, and W5_statusline COALESCEs, so a wrong non-NULL value
# is permanent. SID is field 1 and a shift can only start at or after the field that
# carried the separator, so the id survives even when nothing else does (preview.sh
# relies on the same fact for NF != 20).
if [ "$PARSE_OK" -ne 0 ]; then
    # now_pair before herd_log, or the line stamps "?" — nothing has set $NOW_ISO
    # this early.
    now_pair
    if [ "$PARSE_OK" -eq 2 ]; then
        herd_log "statusline: payload parse shifted (sentinel=[$HERD_PARSE_TAIL]) — no DB write"
    else
        # rc 1: jq failed; jq_in already logged why. No field was parsed, so there is
        # nothing to shift and nothing to blame on the payload.
        herd_log "statusline: no fields parsed — no DB write"
    fi
    printf '%s' "⬢ ? | herd: payload parse error"
    exit 0
fi

# ── pure-bash git branch (zero forks): walk CWD -> .git/HEAD ───────────────
BRANCH=""
git_branch_of() {
    local dir="$1" g head ref
    # ABSOLUTE ONLY. ${dir%/*} returns the string UNCHANGED once no slash is left,
    # so a relative cwd never shrinks and the loop below spins forever.
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
                # RELATIVE gitdirs are the norm for submodules and worktrees, and
                # must resolve against $dir, not the HOOK's cwd — otherwise they
                # miss, or worse, hit a DIFFERENT repo's HEAD.
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
        # `${MODEL#claude-}` alone only strips a prefix at position 0, so a Bedrock or
        # Vertex id (us.anthropic.claude-opus-4-20250514-v1:0) kept its whole vendor
        # path and rendered as "🤖 us.anthropic.claude opus.4.20250514.v1:0". Drop
        # anything up to and including the LAST "claude-" wherever it sits.
        rest="${MODEL##*claude-}"
        # Cut at the 8-digit date and drop everything after it. Matching only a
        # TRAILING date left the Bedrock/Vertex suffix behind
        # (claude-opus-4-20250514-v1:0 -> "Opus 4.20250514.v1:0"), since there the
        # date sits mid-string. `%%` cuts at the first date, so both shapes land on
        # the same family+version, and an id with no date is untouched.
        rest="${rest%%-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]*}"
        family="${rest%%-*}"
        # A dashless id has no version part, and `${rest#*-}` returns $rest unchanged
        # when there is no dash — which rendered the family twice ("🤖 Opus opus").
        case "$rest" in
            *-*) ver="${rest#*-}"; ver="${ver//-/.}" ;;
            *)   ver="" ;;
        esac
        case "$family" in
            opus)   family=Opus ;;
            sonnet) family=Sonnet ;;
            haiku)  family=Haiku ;;
        esac
        if [ -n "$ver" ]; then L1+=("🤖 $family $ver"); else L1+=("🤖 $family"); fi
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

    # ⏱️ 5h rate limit — reset local TZ, 12h, no date.
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
# It must cover every field we RENDER *and* every field we SINK, or a tick that
# changes only a sunk field takes a cache hit and never reaches the DB.
#
# This is an IDLE-path optimization, NOT a hot-path one: token counts move on
# essentially every tick of an active session, so an active session almost always
# misses. See DECISIONS.md#statusline-forks.
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
# matched no row, but "" when it FAILED, and a locked DB is the common failure.
# Treating an error as "not adopted" costs three 3s busy_timeouts per render,
# exactly when the DB is already contended. An error means we learned nothing about
# adoption, so skip and render from the payload.
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
        # epoch() is hand-rolled because mktime() is a GAWK EXTENSION and macOS
        # ships one-true-awk, which aborts at parse. POSIX awk only below.
        #
        # Both stamps are ISO-8601 UTC, so the days-from-civil arithmetic is exact
        # (Hinnant's algorithm, era = 400-year cycle of 146097 days; -719468 shifts
        # the 0000-03-01 epoch to 1970-01-01). Staying in UTC also avoids feeding
        # UTC digits to a local-time parser, which only cancels outside a DST shift.
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
rm -f "$CACHE.tmp.$$" 2>/dev/null      # the && skipped the mv: leave no debris

emit "$L1S" "$L2S"
exit 0
