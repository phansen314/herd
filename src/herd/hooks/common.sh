# herd/hooks/common.sh — shared hook library. SOURCE THIS, don't run it.
# bash 3.2 compatible (macOS ships 3.2). Rationale: DESIGN.md#the-hooks-hookssh.
#
# NOTHING HERE MAY BLOCK CLAUDE — every hook exits 0. The SQL lives in
# schema/writes.sql and is NOT copied here (test_source_invariants.py forbids inline
# DML; test_hooks.py guards bash/python drift).
#
# FORK COST IS THE RECURRING CONSTRAINT in this file: the statusline sources it and
# fires ~1/sec/session, so builtins are preferred over $(...) throughout, and where
# the code looks odd it is usually for that reason.
#
# ── the config file (~/.herd/config), read BEFORE the defaults below ──────
# The daemon (systemd, empty env) and the hooks (children of your shell) share no
# environment, so both read this file by the same rules. herd/config.py is the
# python half; test_source_invariants pins the two key lists together.
# A key that reaches only one half is silent and costs live sessions:
# DECISIONS.md#env-divergence.
#
# The environment WINS over the file — same precedence as config.py. `${!k+x}` is
# not used to test that: indirect expansion with a modifier is not bash 3.2. eval is
# safe here only because $k has already been matched against the literal list below,
# never taken from the file as-is.
herd_load_config() {
    local f="${HERD_CONFIG:-$HOME/.herd/config}" line k v cur
    local tab=$'\t'
    [ -r "$f" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ""|"#"*) continue ;;
            *"="*) ;;
            *) continue ;;                  # no '=': config.py reports it, we skip
        esac
        k="${line%%=*}"; v="${line#*=}"
        # trim surrounding whitespace, and the `export ` muscle memory invites.
        # SPACE AND TAB, both ends: python trims with .strip()/.rstrip(), which eat
        # both. A space-only class here let `HERD_DB=/a/b.db<TAB># c` bind a path
        # with a trailing tab on the bash side while python bound the clean one —
        # sqlite creates that file, so the hooks recorded into it and the daemon
        # read the real DB, with nothing erroring on either side.
        k="${k#"${k%%[! $tab]*}"}"; k="${k%"${k##*[! $tab]}"}"; k="${k#export }"
        k="${k#"${k%%[! $tab]*}"}"
        v="${v#"${v%%[! $tab]*}"}"
        # INLINE COMMENT, cut before the trailing trim. A '#' opens one only at the
        # start of the value or after whitespace — `/srv/repo#2/herd.db` keeps its
        # '#'. The rule is config._strip_inline_comment's, character for character.
        case "$v" in
            "#"*) v="" ;;
            *[" $tab"]"#"*) v="${v%%[ $tab]#*}" ;;
        esac
        v="${v%"${v##*[! $tab]}"}"
        case "$k" in
            HERD_ATTENTION|HERD_WAIT_SECS|HERD_APPROVAL_SECS|HERD_STUCK_SECS|\
            HERD_STRANDED_SECS|HERD_DAEMON_LOG_MAX|HERD_CLAUDE_NAME|HERD_RUNTIME|\
            HERD_DB|HERD_TOOL_THROTTLE|HERD_ERRLOG|HERD_ERRLOG_MAX|HERD_TEMPLATES) ;;
            *) continue ;;                  # unknown key: config.py names it
        esac
        # leading ~ only, matching config.py. eval assigns $v QUOTED below, so bash
        # never expands it — and the shipped template shows ~/.herd/herd.db.
        case "$v" in
            "~") v="$HOME" ;;
            "~/"*) v="$HOME/${v#\~/}" ;;
        esac
        eval "cur=\${$k+set}"
        [ -n "$cur" ] && continue           # already in the environment: it wins
        eval "$k=\$v"
    done < "$f"
}
herd_load_config

# Config is default-expansion (${X:-...}) ONLY, never unconditional assignment,
# so tests can redirect state.
HERD_DB="${HERD_DB:-$HOME/.herd/herd.db}"
# HERD_RUNTIME, else XDG_RUNTIME_DIR, else ~/.herd/run — NEVER /tmp. Our names here
# are predictable and db() creates its error file with `: >`, a redirect that
# FOLLOWS SYMLINKS: on a shared box another user could pre-create that path as a
# link to a file of ours and have the next hook truncate it. Only a 0700 dir we own
# is safe. config.runtime_dir() must agree — the daemon takes its single-instance
# lock here, so two answers means two daemons.
HERD_RUNTIME="${HERD_RUNTIME:-${XDG_RUNTIME_DIR:-}}"
if [ -z "$HERD_RUNTIME" ]; then
    HERD_RUNTIME="$HOME/.herd/run"
    # `[ -d ]` is a builtin, mkdir is a fork: only pay it when actually missing.
    [ -d "$HERD_RUNTIME" ] || { mkdir -p "$HERD_RUNTIME" 2>/dev/null && \
                                chmod 700 "$HERD_RUNTIME" 2>/dev/null; }
fi
HERD_ERRLOG="${HERD_ERRLOG:-$HOME/.herd/hook-errors.log}"

# writes.sql sits at hooks/../schema/. ${BASH_SOURCE%/*} not $(dirname) — no fork.
__herd_dir="${BASH_SOURCE%/*}"
[ "$__herd_dir" = "${BASH_SOURCE}" ] && __herd_dir="."
HERD_WRITES="${HERD_WRITES:-$__herd_dir/../schema/writes.sql}"

# ── payload ───────────────────────────────────────────────────────────────
# Slurp stdin into INPUT with NO FORK ($(cat) is a subshell plus an exec).
#
# NEVER `$(</dev/stdin)`: Claude's invocation makes that empty. `read -d ''` is a
# different mechanism — it reads fd 0 directly, to EOF.
#
# A JSON payload contains no NUL, so read never finds its delimiter and returns
# nonzero at EOF with INPUT fully populated. `|| :` keeps that expected rc off
# `set -e` and out of the caller's $?. IFS= stops whitespace being stripped.
read_input() { IFS= read -r -d '' INPUT || :; }

# ── time ──────────────────────────────────────────────────────────────────
# now_pair emits ISO + epoch from a SINGLE fork (throttle needs epoch, write
# needs ISO).
#
# GNU-vs-BSD is detected from the REAL call, not a separate probe (that would be a
# fork per hook fire to answer a per-machine constant). The first call asks for %3N
# and checks whether digits came back; BSD costs one retry and latches $__HERD_FMT
# for the rest of the process.
__HERD_FMT='+%Y-%m-%dT%H:%M:%S.%3NZ %s'

NOW_ISO=""; NOW_EPOCH=""
now_pair() {
    local __o __ms
    __o=$(date -u "$__HERD_FMT")
    # millis field of "<iso>.<ms>Z <epoch>". Non-digits mean %3N went unexpanded —
    # BSD renders it literally, e.g. "...:00.3NZ". Latch ONLY on that positive
    # detection: an EMPTY $__o means `date` failed and says nothing about the
    # format, so latching on it would downgrade the whole process to whole seconds
    # after one transient failure.
    if [ -n "$__o" ]; then
        __ms="${__o%Z *}"; __ms="${__ms##*.}"
        case "$__ms" in
            ''|*[!0-9]*)
                __HERD_FMT='+%Y-%m-%dT%H:%M:%S.000Z %s'
                __o=$(date -u "$__HERD_FMT") ;;
        esac
    fi
    NOW_ISO="${__o% *}"
    NOW_EPOCH="${__o##* }"
}

# ── logging ───────────────────────────────────────────────────────────────
# Cap the log: a persistent fault logs on EVERY hook fire, so uncapped it buries
# today's error. One rotation, not a numbered series — .1 keeps the previous window.
HERD_ERRLOG_MAX="${HERD_ERRLOG_MAX:-1048576}"       # bytes; 0 disables rotation

herd_log_rotate() {
    local size
    [ "$HERD_ERRLOG_MAX" -gt 0 ] 2>/dev/null || return 0
    size=$(wc -c < "$HERD_ERRLOG" 2>/dev/null) || return 0
    size="${size//[^0-9]/}"                          # wc pads on some platforms
    [ -n "$size" ] && [ "$size" -gt "$HERD_ERRLOG_MAX" ] || return 0
    mv -f "$HERD_ERRLOG" "$HERD_ERRLOG.1" 2>/dev/null
}

herd_log() {
    printf '%s\t%s\t%s\n' "${NOW_ISO:-?}" "${0##*/}" "$*" >> "$HERD_ERRLOG" 2>/dev/null
    herd_log_rotate
}

# ── payload parsing: ONE reader for all five hooks + the statusline ────────
# payload_read '<jq exprs>' VAR... — extract fields into named variables:
#
#     payload_read '.session_id, .cwd' SID CWD || <the parse shifted>
#
# Field values may legally contain newlines (a cwd) or the separator (a /rename'd
# session_name), and either one shifts every later value down a slot — silently and
# permanently, since these get persisted. So the separator and both newline forms
# are stripped from every field, and a sentinel is appended AFTER the join where no
# value can reach it. A nonzero return means a shifted parse and the caller decides:
# statusline.sh refuses to write (a wrong row is permanent, a wrong render lasts one
# tick), session_start.sh writes anyway minus the untrusted fields (it is the only
# thing that ever creates the row).
#
# HERD_PARSE_TAIL is a documented output, not an internal: on a nonzero return it
# holds whatever arrived where the sentinel should have been, and every caller logs
# it as the only clue to what the payload did.
#
# NO APOSTROPHES IN A CALLER EXPRESSION: it arrives here as a single-quoted bash
# string at the call site, so one would end it and hand the rest to the shell.
payload_read() {
    local __expr="$1"; shift
    local __out
    # The expression is concatenated into a SINGLE-quoted filter rather than
    # interpolated into a double-quoted one: jq filters are full of double quotes
    # ("number", "%I:%M%p"), and escaping them at every call site is how the
    # apostrophe rule above gets broken by accident.
    __out=$(jq_in -rj '['"$__expr"'] | map(. // "" | tostring | gsub("[\\n\\r\u001f]"; " ")) | join("\u001f") | . + "\u001fEOR"') || return 1
    IFS=$'\x1f' read -r "$@" HERD_PARSE_TAIL <<JQEOF
$__out
JQEOF
    [ "$HERD_PARSE_TAIL" = "EOR" ]
}

# jq_in <jq args...> — filter $INPUT, and LOG when jq itself fails. Without the
# log, a MISSING jq is indistinguishable from "the payload wasn't for us": exit 0,
# nothing written, empty errlog. rc=127 is the missing-binary case.
#
# stderr stays discarded rather than captured — merging it risks a jq warning
# landing in the parsed output.
jq_in() {
    local out rc
    out=$(printf '%s' "$INPUT" | jq "$@" 2>/dev/null); rc=$?
    if [ "$rc" -ne 0 ]; then
        [ -n "$NOW_ISO" ] || now_pair          # failure path only — no extra fork
        if [ "$rc" -eq 127 ]; then
            herd_log "jq NOT FOUND (rc=127) — herd cannot parse any payload"
        else
            herd_log "jq failed (rc=$rc)"
        fi
        return "$rc"
    fi
    printf '%s' "$out"
}

# ── identity guard ────────────────────────────────────────────────────────
# A session_id becomes a filename (throttle/cache); reject / and .. so a payload
# can't escape $HERD_RUNTIME.
valid_sid() { case "$1" in ''|*[!a-zA-Z0-9-]*) return 1 ;; *) return 0 ;; esac; }

# ── claude pid (process-ancestry walk) ──────────────────────────────────────
# Walk UP from this hook to the first ancestor with comm==claude. MEANINGFUL ONLY
# FROM A BLOCKING HOOK (an async hook can be reparented to init). Exactly one
# claude is an ancestor, so first-match-up wins with no ppid cross-check. Overrides
# via HERD_CLAUDE_NAME. Split for testability. See DESIGN.md#pid.
_walk_claude() {
    awk -v start="$1" -v want="${HERD_CLAUDE_NAME:-claude}" '
        { nm=$3; sub(/.*\//,"",nm); pp[$1]=$2; comm[$1]=nm }
        END {
            p=start
            while (p != "" && p != "1" && (p in pp)) {
                if (comm[p]==want) { print p; exit }
                p=pp[p]
            }
        }'
}
claude_pid() { ps -eo pid=,ppid=,comm= 2>/dev/null | _walk_claude "$$"; }

# ── sqlite3 ───────────────────────────────────────────────────────────────
# -bail is LOAD-BEARING for run_tx: without it the CLI skips a failed statement
# and COMMITs anyway, half-committing. busy_timeout is not optional (WAL
# serialises writers). See DESIGN.md#commonsh-internals.
# ONE errfile per process, reaped by a trap, instead of an `rm` fork per db() call
# (the statusline makes 2-3 a tick). `>` is a builtin truncate, so reuse is free.
# No hook sets its own EXIT trap; if one ever does, it must call herd_db_cleanup.
#
# Trapped on the SIGNALS too, not just EXIT: the statusline is killed on timeout as
# a matter of course and an EXIT-only trap does not run for SIGTERM, which would
# leak one file per killed hook forever with no sweeper anywhere. SIGKILL still
# leaks; those are zero-byte and rare enough to live with.
__HERD_ERRFILE="$HERD_RUNTIME/herd-db-err.$$"
herd_db_cleanup() { rm -f "$__HERD_ERRFILE" 2>/dev/null; }
trap herd_db_cleanup EXIT
trap 'herd_db_cleanup; exit 143' TERM
trap 'herd_db_cleanup; exit 130' INT
trap 'herd_db_cleanup; exit 129' HUP

# mode=rw, NEVER rwc, and never a bare path (sqlite3's default CREATES). A typo'd
# HERD_DB must fail, not silently produce an empty schema-less file that the daemon
# then reports as "no such table: sessions" forever. db.py:connect() says
# create=False for the same reason; only the installer may create a database.
#
# The path goes into a URI, so '?' would start a query string and '#' a fragment,
# and '%' is how a URI escapes anything — percent-encode all three, '%' FIRST or
# the escapes we add get re-escaped. Mirrors urllib.parse.quote in db.py.
#
# CACHED via pure parameter expansion, deliberately not $(...): that is a fork and
# db() runs 2-3 times a tick. Recomputed only when HERD_DB actually changes.
__HERD_DB_URI=""; __HERD_DB_URI_FOR=""
herd_db_uri() {
    local u="${HERD_DB//%/%25}"
    u="${u//\?/%3F}"; u="${u//#/%23}"
    __HERD_DB_URI="file:$u?mode=rw"
    __HERD_DB_URI_FOR="$HERD_DB"
}

db() {
    local err="$__HERD_ERRFILE" rc
    : > "$err" 2>/dev/null              # builtin truncate: no stale stderr leaks in
    [ "$__HERD_DB_URI_FOR" = "$HERD_DB" ] || herd_db_uri
    sqlite3 \
        -bail \
        -cmd ".timeout 3000" \
        -cmd "PRAGMA foreign_keys=ON" \
        -cmd "PRAGMA synchronous=NORMAL" \
        "$__HERD_DB_URI" "$@" 2>"$err"
    rc=$?
    if [ $rc -ne 0 ] && [ -s "$err" ]; then
        { printf '%s\t%s\trc=%d\t' "${NOW_ISO:-?}" "${0##*/}" "$rc"
          tr '\n' ' ' < "$err"; printf '\n'; } >> "$HERD_ERRLOG" 2>/dev/null
        herd_log_rotate            # db() appends directly, so cap it here too
    fi
    return $rc
}

# ── statement extraction ──────────────────────────────────────────────────
# Pull one `-- :name X` block out of writes.sql, stopping at the first `;`
# (mirrors herd.db.load_statements(); test_hooks.py asserts they agree). Stopping
# at `;` also keeps bind() from substituting `:name` mentions in trailing prose.
# awk despite the fork: it beats pure-bash here.
stmt() {
    awk -v want="$1" '
        index($0, "-- :name ") == 1 { f = ($0 == "-- :name " want); next }
        !f { next }
        { print; if (index($0, ";")) exit }
    ' "$HERD_WRITES"
}

# The :param substitution, as an awk FUNCTION so extract-and-bind is one fork, not
# two. Composed into both bind() and stmt_bind() — one copy of the rule, two entry
# points. Do not inline it into either.
__HERD_BINDFN='
function bindline(s,   out, name, key, val, q) {
    q = sprintf("%c", 39)
    out = ""
    while (match(s, /:[a-zA-Z_][a-zA-Z_0-9]*/)) {
        name = substr(s, RSTART + 1, RLENGTH - 1)
        out  = out substr(s, 1, RSTART - 1)
        key  = "HERD_P_" name
        if (key in ENVIRON) {
            val = ENVIRON[key]
            if (val == "") { out = out "NULL" }
            else { gsub(q, q q, val); out = out q val q }
        } else {
            missing++
            printf("herd: unbound param :%s\n", name) > "/dev/stderr"
            out = out ":" name
        }
        s = substr(s, RSTART + RLENGTH)
    }
    return out s
}'

# ── parameter binding ─────────────────────────────────────────────────────
# Expand :name params from HERD_P_<name> env vars, SINGLE PASS. Not sqlite3's
# .param set (its shell-quoting mis-tokenizes SQL escapes and binds NULL
# silently). Single-pass so a value containing ":now" isn't rescanned. Empty ->
# NULL; unknown param -> hard failure. See DESIGN.md#commonsh-internals.
bind() {
    printf '%s' "$1" | awk "$__HERD_BINDFN"'
        BEGIN { missing = 0 }
        { print bindline($0) }
        END { exit (missing > 0) }
    '
}

# stmt + bind in ONE awk fork. run()/run_tx() use this; stmt() and bind() stay as
# the separately-testable halves. The `;` cut tests $0 — the RAW line — not the
# bound text, because a bound VALUE may itself contain a semicolon (a cwd or a
# /rename can), and cutting on that would truncate the statement mid-flight.
stmt_bind() {
    awk -v want="$1" "$__HERD_BINDFN"'
        BEGIN { missing = 0 }
        index($0, "-- :name ") == 1 { f = ($0 == "-- :name " want); next }
        !f { next }
        { print bindline($0); if (index($0, ";")) exit }
        END { exit (missing > 0) }
    ' "$HERD_WRITES"
}

# run <statement_name> [<extra_sql>] — extract, bind, execute. Extra SQL may be
# appended (e.g. "SELECT changes();") to read a result on the SAME connection.
run() {
    local bound rc
    bound=$(stmt_bind "$1"); rc=$?
    # Emptiness FIRST: an unbound param still emits the statement and only sets rc,
    # so checking rc first would report a typo'd name as an unbound param.
    if [ -z "$bound" ]; then herd_log "no such statement: $1"; return 1; fi
    if [ "$rc" -ne 0 ]; then herd_log "unbound params in $1"; return 1; fi
    if [ -n "$2" ]; then
        printf '%s\n%s\n' "$bound" "$2" | db
    else
        printf '%s\n' "$bound" | db
    fi
}

# run_tx <name> [<name> ...] — bind each, wrap in ONE BEGIN IMMEDIATE..COMMIT,
# one fork. IMMEDIATE (not plain BEGIN) takes the write lock up front, avoiding
# SQLITE_BUSY_SNAPSHOT the busy timeout can't retry. All binding happens before
# any SQL runs, so an unbound param aborts with nothing executed.
run_tx() {
    local name bound rc body=""
    for name in "$@"; do
        bound=$(stmt_bind "$name"); rc=$?
        if [ -z "$bound" ]; then herd_log "no such statement: $name"; return 1; fi
        if [ "$rc" -ne 0 ]; then herd_log "unbound params in $name"; return 1; fi
        body="$body$bound
"
    done
    printf 'BEGIN IMMEDIATE;\n%sCOMMIT;\n' "$body" | db
}
