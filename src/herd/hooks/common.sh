# herd/hooks/common.sh — shared hook library. SOURCE THIS, don't run it.
# bash 3.2 compatible. Rationale + gotchas: DESIGN.md#the-hooks-hookssh.
#
# NOTHING HERE MAY BLOCK CLAUDE — every hook exits 0. The SQL lives in
# schema/writes.sql and is NOT copied here (test_source_invariants.py forbids inline
# DML; test_hooks.py guards bash/python drift).
#
# ── the config file (~/.herd/config), read BEFORE the defaults below ──────
# The daemon and the hooks do not share an environment: these scripts are children
# of your shell, the daemon is started by systemd and inherits nothing from it. A
# setting that only reaches one of them is not a preference, it is a divergence —
# HERD_CLAUDE_NAME exported in .bashrc made the hooks store a pid the reaper then
# read as a recycled one, stopping every live session on its first tick. So both
# sides read this file, by the same rules. herd/config.py is the python half;
# test_source_invariants pins the two key lists together.
#
# NO FORKS. `read` is a builtin and the redirect costs no process, which matters on
# the statusline path (~1/sec/session). Skipped entirely when there is no file.
#
# The environment WINS over the file — same precedence as config.py — so a test
# that exports HERD_RUNTIME still redirects state, and a one-off override works.
# `${!k+x}` is not used to test that: indirect expansion with a modifier is not
# bash 3.2, and macOS ships 3.2. eval is safe here only because $k has already been
# matched against the literal list below, never taken from the file as-is.
herd_load_config() {
    local f="${HERD_CONFIG:-$HOME/.herd/config}" line k v cur
    [ -r "$f" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case "$line" in
            ""|"#"*) continue ;;
            *"="*) ;;
            *) continue ;;                  # no '=': config.py reports it, we skip
        esac
        k="${line%%=*}"; v="${line#*=}"
        # trim surrounding whitespace, and the `export ` muscle memory invites
        k="${k#"${k%%[! ]*}"}"; k="${k%"${k##*[! ]}"}"; k="${k#export }"
        k="${k#"${k%%[! ]*}"}"
        v="${v#"${v%%[! ]*}"}"; v="${v%"${v##*[! ]}"}"
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
# so tests can redirect state (HERD_RUNTIME earned this — see DESIGN.md).
HERD_DB="${HERD_DB:-$HOME/.herd/herd.db}"
# HERD_RUNTIME, else XDG_RUNTIME_DIR, else ~/.herd/run — NEVER /tmp, which is what
# this fell back to. Every name we put here is predictable (herd-db-err.$$,
# herd-stline-<uuid>, herd-daemon.lock) and db() creates its error file with `: >`,
# a redirect that FOLLOWS SYMLINKS: on a shared box another user could pre-create
# that path as a link to a file of ours and have the next hook fire truncate it.
# /run/user/<uid> is 0700 and ours, and so is ~/.herd/run; only /tmp was open.
#
# config.runtime_dir() is the python half of this and must agree — the daemon takes
# its single-instance lock in this directory, so two answers means two daemons.
HERD_RUNTIME="${HERD_RUNTIME:-${XDG_RUNTIME_DIR:-}}"
if [ -z "$HERD_RUNTIME" ]; then
    HERD_RUNTIME="$HOME/.herd/run"
    # `[ -d ]` is a builtin and mkdir is a FORK, on a path that runs about once a
    # second per session. Only pay it when the directory is actually missing.
    [ -d "$HERD_RUNTIME" ] || { mkdir -p "$HERD_RUNTIME" 2>/dev/null && \
                                chmod 700 "$HERD_RUNTIME" 2>/dev/null; }
fi
HERD_ERRLOG="${HERD_ERRLOG:-$HOME/.herd/hook-errors.log}"

# writes.sql sits at hooks/../schema/. ${BASH_SOURCE%/*} not $(dirname) — no fork.
__herd_dir="${BASH_SOURCE%/*}"
[ "$__herd_dir" = "${BASH_SOURCE}" ] && __herd_dir="."
HERD_WRITES="${HERD_WRITES:-$__herd_dir/../schema/writes.sql}"

# ── payload ───────────────────────────────────────────────────────────────
# Slurp stdin into INPUT with NO FORK. `$(cat)` cost a subshell plus an exec —
# measured 2.9ms, on the statusline's ~1/sec/session path and on PostToolUse's
# per-tool-call path — to move a few KB that bash can read itself.
#
# NEVER `$(</dev/stdin)`: Claude's invocation makes that empty (learned the hard
# way; the comment it replaces in session_start.sh said so). read -d '' is a
# different mechanism — it reads fd 0 directly, to EOF.
#
# A JSON payload contains no NUL, so read never finds its delimiter and returns
# nonzero at EOF with INPUT fully populated. `|| :` keeps that expected rc off
# `set -e` and out of the caller's $?. IFS= stops leading/trailing whitespace
# being stripped from the payload.
read_input() { IFS= read -r -d '' INPUT || :; }

# ── time ──────────────────────────────────────────────────────────────────
# now_pair emits ISO + epoch from a SINGLE fork (throttle needs epoch, write
# needs ISO).
#
# GNU-vs-BSD is detected from the REAL call, not a separate `date -u +%3N`
# probe. That probe ran at SOURCE time — so every hook fire, including the
# statusline's ~1/sec/session, paid a fork (0.6ms) to answer a question that is
# constant per machine. Now the first call optimistically asks for %3N and
# checks whether it came back as digits; a date without it costs one retry and
# latches $__HERD_FMT for the rest of the process, so BSD pays the same two
# forks it always did and GNU pays one instead of two.
__HERD_FMT='+%Y-%m-%dT%H:%M:%S.%3NZ %s'

NOW_ISO=""; NOW_EPOCH=""
now_pair() {
    local __o __ms
    __o=$(date -u "$__HERD_FMT")
    # millis field of "<iso>.<ms>Z <epoch>". Non-digits (or empty) mean this date
    # left %3N unexpanded — BSD renders it literally, e.g. "...:00.3NZ".
    # Latch ONLY on a positive BSD detection — output that arrived and left %3N
    # unexpanded. An EMPTY $__o means `date` failed, which says nothing about the
    # format, and latching on it downgraded every later stamp in the process to
    # whole seconds after a single transient failure. No output, no conclusion.
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
# Cap the log. A persistent fault logs on EVERY hook fire — a missing jq means six
# scripts complaining per prompt, and the statusline alone runs ~1/sec/session — so
# without a cap this grows without bound and buries today's error under weeks of
# history. One rotation, not a numbered series: the point is a bounded file you can
# actually read, and .1 keeps the previous window for anything mid-investigation.
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

# ── payload parsing ───────────────────────────────────────────────────────
# jq_in <jq args...> — filter $INPUT, and LOG when jq itself fails.
#
# A hook that parses no session_id exits 0 and records nothing, which is correct:
# the payload wasn't for us. A MISSING jq produced the identical outcome — exit 0,
# nothing written, and an empty HERD_ERRLOG, the file troubleshooting tells you to
# check first — so herd silently recorded nothing forever with no way to tell why.
# rc=127 is the missing-binary case; anything else is a real jq/payload error.
#
# stderr stays discarded rather than captured: this runs on the statusline's
# ~1/sec path, and merging it risks a jq warning landing in the parsed output.
# ── payload parsing: ONE reader for all five hooks + the statusline ────────
# Extract fields into named variables. Callers pass a jq expression list and the
# variable names to fill:
#
#     payload_read '.session_id, .cwd' SID CWD || <the parse shifted>
#
# THE POINT IS THAT THERE IS ONE OF THESE. Every hook used to hand-roll its own,
# and the same bug shipped twice in two different shapes:
#
#   * `{ read -r A; read -r B; }` over newline-delimited jq output splits on the
#     first newline IN ANY FIELD. A cwd may legally contain one, so the values
#     after it each moved down a slot and were persisted that way.
#   * joining on \x1f without stripping \x1f from the values is the same shift with
#     a different trigger. The statusline shipped exactly that: a session_name
#     carrying one wrote the context percentage into the cost column.
#
# So the separator and both newline forms are stripped from every field, and a
# sentinel field is appended AFTER the join where no value can reach it. A caller
# that gets a nonzero return has a shifted parse and must decide what to do:
# statusline.sh refuses to write (a wrong row is permanent, a wrong render lasts one
# tick), session_start.sh writes anyway minus the untrusted fields (it is the only
# thing that ever creates the row, so refusing loses the session for its whole life).
#
# On a nonzero return HERD_PARSE_TAIL holds whatever arrived where the sentinel
# should have been. It is deliberately a documented output rather than an internal:
# every caller logs it, and that string is the only clue to what the payload did.
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
# ONE errfile per process, reaped by an EXIT trap, instead of an `rm` fork per
# call. The statusline makes 2-3 db() calls a tick at ~1/sec/session, so that
# `rm` was ~1.8ms/tick spent deleting a file we immediately recreate. `>` is a
# builtin truncate, so reuse costs nothing. No hook sets its own EXIT trap
# (checked); if one ever does, it must call herd_db_cleanup itself.
# Trapped on the SIGNALS too, not just EXIT: the statusline is killed on timeout
# as a matter of course, and an EXIT-only trap does not run for SIGTERM — so the
# old per-call `rm` left nothing behind while this leaked one file per killed
# hook, forever, with no sweeper anywhere. SIGKILL still leaks (nothing can trap
# it); those are zero-byte and rare enough to live with.
__HERD_ERRFILE="$HERD_RUNTIME/herd-db-err.$$"
herd_db_cleanup() { rm -f "$__HERD_ERRFILE" 2>/dev/null; }
trap herd_db_cleanup EXIT
trap 'herd_db_cleanup; exit 143' TERM
trap 'herd_db_cleanup; exit 130' INT
trap 'herd_db_cleanup; exit 129' HUP

db() {
    local err="$__HERD_ERRFILE" rc
    : > "$err" 2>/dev/null              # builtin truncate: no stale stderr leaks in
    sqlite3 \
        -bail \
        -cmd ".timeout 3000" \
        -cmd "PRAGMA foreign_keys=ON" \
        -cmd "PRAGMA synchronous=NORMAL" \
        "$HERD_DB" "$@" 2>"$err"
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
# awk (0.7ms) beats pure-bash (1.6ms) — measured.
stmt() {
    awk -v want="$1" '
        index($0, "-- :name ") == 1 { f = ($0 == "-- :name " want); next }
        !f { next }
        { print; if (index($0, ";")) exit }
    ' "$HERD_WRITES"
}

# The :param substitution, as an awk FUNCTION so extract-and-bind can be one fork
# instead of two. Composed into both bind() and stmt_bind() below — ONE copy of
# the rule, two entry points. Do not inline it into either.
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
    # Empty output means the NAME did not match (an unbound param still emits the
    # statement, with `:name` left in place, and only sets rc) — so the emptiness
    # check has to come first or a typo'd name reports as an unbound param.
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
