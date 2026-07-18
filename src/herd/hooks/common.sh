# herd/hooks/common.sh — shared hook library. SOURCE THIS, don't run it.
# bash 3.2 compatible. Rationale + gotchas: DESIGN.md#the-hooks-hookssh.
#
# NOTHING HERE MAY BLOCK CLAUDE — every hook exits 0. The SQL lives in
# schema/writes.sql and is NOT copied here (test_source_invariants.py forbids inline
# DML; test_hooks.py guards bash/python drift).
#
# Config is default-expansion (${X:-...}) ONLY, never unconditional assignment,
# so tests can redirect state (HERD_RUNTIME earned this — see DESIGN.md).
HERD_DB="${HERD_DB:-$HOME/.herd/herd.db}"
HERD_RUNTIME="${HERD_RUNTIME:-${XDG_RUNTIME_DIR:-/tmp}}"
HERD_ERRLOG="${HERD_ERRLOG:-$HOME/.herd/hook-errors.log}"

# writes.sql sits at hooks/../schema/. ${BASH_SOURCE%/*} not $(dirname) — no fork.
__herd_dir="${BASH_SOURCE%/*}"
[ "$__herd_dir" = "${BASH_SOURCE}" ] && __herd_dir="."
HERD_WRITES="${HERD_WRITES:-$__herd_dir/../schema/writes.sql}"

# ── time ──────────────────────────────────────────────────────────────────
# Probe GNU-vs-BSD date ONCE at source time. now_pair emits ISO + epoch from a
# SINGLE fork (throttle needs epoch, write needs ISO).
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
db() {
    local err="$HERD_RUNTIME/herd-db-err.$$" rc
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

# ── parameter binding ─────────────────────────────────────────────────────
# Expand :name params from HERD_P_<name> env vars, SINGLE PASS. Not sqlite3's
# .param set (its shell-quoting mis-tokenizes SQL escapes and binds NULL
# silently). Single-pass so a value containing ":now" isn't rescanned. Empty ->
# NULL; unknown param -> hard failure. See DESIGN.md#commonsh-internals.
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

# run <statement_name> [<extra_sql>] — extract, bind, execute. Extra SQL may be
# appended (e.g. "SELECT changes();") to read a result on the SAME connection.
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

# run_tx <name> [<name> ...] — bind each, wrap in ONE BEGIN IMMEDIATE..COMMIT,
# one fork. IMMEDIATE (not plain BEGIN) takes the write lock up front, avoiding
# SQLITE_BUSY_SNAPSHOT the busy timeout can't retry. All binding happens before
# any SQL runs, so an unbound param aborts with nothing executed.
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
