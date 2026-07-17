# herd/hooks/common.sh — shared hook library. SOURCE THIS, don't run it.
#
# bash 3.2 COMPATIBLE. macOS froze /bin/bash at 3.2 for licensing, and these
# run there. No associative arrays, no ${var^^}, no mapfile/readarray, no
# printf '%(%s)T'. Indexed arrays and printf -v only.
#
# NOTHING HERE MAY BLOCK CLAUDE. Every hook exits 0 unconditionally — Stop's
# exit 2 would literally prevent Claude from finishing its turn, and a herd bug
# must never be able to do that. Failures go to the error log and are silent.
#
# THE SQL LIVES IN schema/writes.sql AND IS NOT COPIED HERE.
# klawde inlines every statement into its hooks; herd deliberately does not.
# validate.py proves W2/W4/W5b behave — if the hooks ran transcriptions of those
# statements instead of the statements themselves, the suite would be verifying
# fiction and a fixed bug could quietly return in the copy.

# EVERY path here is default-expansion (${X:-...}), never an unconditional
# assignment. klawde hardcodes KLAWDE_DB in its common.sh, so
# `KLAWDE_DB=/tmp/x ./hook.sh` silently writes to the REAL database and its
# tests have to fake $HOME to work around it.
#
# HERD_RUNTIME earned this comment the hard way: it was written as
# `${XDG_RUNTIME_DIR:-/tmp}` — klawde's exact bug, one line below the note
# criticising it. The hooks then ignored the test's HERD_RUNTIME and wrote
# throttle state into the real /run/user/$UID, so check 50b passed on the first
# run (creating the file) and failed on every run after (reading it back). A
# suite that cannot redirect a program's state does not test that program; it
# tests the machine it runs on.
HERD_DB="${HERD_DB:-$HOME/.herd/herd.db}"
HERD_RUNTIME="${HERD_RUNTIME:-${XDG_RUNTIME_DIR:-/tmp}}"
HERD_ERRLOG="${HERD_ERRLOG:-$HOME/.herd/hook-errors.log}"

# writes.sql sits next to us in the package: hooks/../schema/writes.sql.
# ${BASH_SOURCE%/*} instead of $(dirname) — dirname is a fork, this is not.
__herd_dir="${BASH_SOURCE%/*}"
[ "$__herd_dir" = "${BASH_SOURCE}" ] && __herd_dir="."
HERD_WRITES="${HERD_WRITES:-$__herd_dir/../schema/writes.sql}"

# ── time ──────────────────────────────────────────────────────────────────
# Probe GNU-vs-BSD date ONCE at source time, then bake the format string.
# now_pair emits ISO + epoch from a SINGLE fork: the throttle needs epoch and
# the write needs ISO, and two date calls would be two forks on the hot path.
__herd_probe=$(date -u +%3N 2>/dev/null)
case "$__herd_probe" in
    ''|*[!0-9]*) __HERD_FMT='+%Y-%m-%dT%H:%M:%S.000Z %s' ;;
    *)           __HERD_FMT='+%Y-%m-%dT%H:%M:%S.%3NZ %s' ;;
esac
unset __herd_probe

NOW_ISO=""; NOW_EPOCH=""
now_pair() {
    local __o
    __o=$(date -u "$__HERD_FMT")
    NOW_ISO="${__o% *}"
    NOW_EPOCH="${__o##* }"
}

# ── logging ───────────────────────────────────────────────────────────────
herd_log() {
    printf '%s\t%s\t%s\n' "${NOW_ISO:-?}" "${0##*/}" "$*" >> "$HERD_ERRLOG" 2>/dev/null
}

# ── identity guard ────────────────────────────────────────────────────────
# A session_id becomes a filename (throttle/cache). A payload with `/` or `..`
# in it would otherwise escape $HERD_RUNTIME.
valid_sid() { case "$1" in ''|*[!a-zA-Z0-9-]*) return 1 ;; *) return 0 ;; esac; }

# ── claude pid (process-ancestry walk) ──────────────────────────────────────
# Find claude's pid by walking the process tree UP from this hook to the first
# ancestor named `claude`. SPIKE-1 concluded pid must come from `kitten @ ls`;
# this is the walk that may overturn that — see the spike.
#
# MEANINGFUL ONLY FROM A BLOCKING HOOK (SessionStart). An async hook can be
# reparented away from claude (ppid -> 1), breaking the chain. SessionStart runs
# while claude WAITS, so its chain is intact.
#
# From a hook's vantage EXACTLY ONE claude is an ancestor — its own session.
# Other sessions' claudes and claude's own MCP children are SIBLINGS, never on the
# upward path, so first-match-walking-up wins with no ppid cross-check (the
# `kitten @ ls` route needed one because foreground_processes is a flat list).
#
# Identity is basename(comm) == claude, overridable via HERD_CLAUDE_NAME. The
# intended robustness anchor for a differently-named install (e.g. node-based) is
# basename($CLAUDE_CODE_EXECPATH); wire that through HERD_CLAUDE_NAME if needed.
#
# Split for testability: _walk_claude reads `pid ppid comm` lines on stdin so the
# suite can inject a synthetic ancestry; claude_pid pipes the real ps in. ONE ps
# fork, portable to Linux and macOS (no /proc dependency).
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
# busy_timeout is NOT optional: WAL serialises writers, and without it a hook
# fails outright the moment reconcile or the TUI holds the write lock.
# The errfile lives in $HERD_RUNTIME, not /tmp — klawde uses a fixed
# /tmp/klawde-db-err.$$ path in a world-writable directory.
db() {
    local err="$HERD_RUNTIME/herd-db-err.$$" rc
    # -bail IS LOAD-BEARING for run_tx. Without it the sqlite3 CLI does not stop
    # on a statement error: it prints the error, SKIPS to the next statement,
    # and runs COMMIT anyway — committing everything before the failure while
    # still exiting nonzero. So `BEGIN; a; b(fails); COMMIT` half-commits `a`.
    # With -bail it stops at the error, never reaches COMMIT, and the open
    # transaction rolls back when sqlite3 exits. Verified both ways.
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
    fi
    rm -f "$err"
    return $rc
}

# ── statement extraction ──────────────────────────────────────────────────
# Pull one `-- :name X` block out of writes.sql, stopping at the first `;`.
# Mirrors herd.db.load_statements(); validate.py check 47 asserts the two
# agree character-for-character, so bash and python cannot drift.
#
# Stopping at the first `;` is load-bearing for a second reason: the prose
# after a statement contains things like ":pid MUST be claude's pid", and bind()
# would happily try to substitute that `:pid`.
#
# An awk fork (0.7ms) beats the pure-bash equivalent (1.6ms) — a `while read`
# over 400 lines costs more than spawning a process. Measured, not assumed.
stmt() {
    awk -v want="$1" '
        index($0, "-- :name ") == 1 { f = ($0 == "-- :name " want); next }
        !f { next }
        { print; if (index($0, ";")) exit }
    ' "$HERD_WRITES"
}

# ── parameter binding ─────────────────────────────────────────────────────
# Expand :name params from HERD_P_<name> environment variables, SINGLE PASS.
#
# Why not sqlite3's own `.param set`? Its dot-command parser uses shell-like
# quoting, not SQL quoting: `.param set :v 'o''brien'` — the CORRECT SQL escape
# — mis-tokenizes, the parameter is left UNBOUND, and sqlite3 then silently
# binds NULL. Silent data loss beats a loud error only in the worst way.
#
# Why single-pass? Sequential ${sql//:name/value} rescans its own output: a cwd
# containing the literal text ":now" gets mangled by the next substitution.
# Emitting each value into `out` and continuing on the REMAINDER means a value
# is never scanned for params. Values travel via the environment, so no shell
# quoting is involved anywhere.
#
# An unknown param is a hard failure (nonzero exit), never a silent NULL.
bind() {
    printf '%s' "$1" | awk '
        BEGIN { q = sprintf("%c", 39); missing = 0 }
        {
            out = ""; s = $0
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
            print out s
        }
        END { exit (missing > 0) }
    '
}

# run <statement_name> — extract, bind, execute. Extra SQL may be appended on
# stdin-style via $2 (e.g. "SELECT changes();") to read a result back on the
# SAME connection, since changes() is per-connection.
run() {
    local sql bound
    sql=$(stmt "$1")
    if [ -z "$sql" ]; then herd_log "no such statement: $1"; return 1; fi
    bound=$(bind "$sql") || { herd_log "unbound params in $1"; return 1; }
    if [ -n "$2" ]; then
        printf '%s\n%s\n' "$bound" "$2" | db
    else
        printf '%s\n' "$bound" | db
    fi
}

# run_tx <name> [<name> ...] — extract+bind each statement, wrap them in ONE
# BEGIN IMMEDIATE ... COMMIT, execute in a SINGLE sqlite3 fork.
#
# Two wins, both real:
#   - one fork and one WAL commit instead of N. On the hot path (post_tool_use,
#     fires per tool call) that halves the sqlite3 spawns.
#   - ATOMICITY. An event and its status change land together or not at all;
#     -bail + the transaction guarantee no half-write survives a mid-tx error.
#
# BEGIN IMMEDIATE, never plain BEGIN: a deferred transaction upgrades to a write
# lock lazily on the first write, and that upgrade can throw SQLITE_BUSY_SNAPSHOT
# which the busy timeout cannot retry away. IMMEDIATE takes the write lock up
# front, so the only wait is the ordinary one the busy timeout handles.
#
# Binding happens for ALL statements BEFORE any SQL runs, so an unbound param
# aborts the whole thing with nothing executed — never a partial transaction.
run_tx() {
    local name sql bound body=""
    for name in "$@"; do
        sql=$(stmt "$name")
        if [ -z "$sql" ]; then herd_log "no such statement: $name"; return 1; fi
        bound=$(bind "$sql") || { herd_log "unbound params in $name"; return 1; }
        body="$body$bound
"
    done
    printf 'BEGIN IMMEDIATE;\n%sCOMMIT;\n' "$body" | db
}
