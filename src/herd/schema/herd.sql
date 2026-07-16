-- ═══════════════════════════════════════════════════════════════════════════
-- herd/schema.sql — TIER 2: herd's relationship to a session
--
-- Every row here answers a question that exists only because herd exists:
-- where is it, what did I name it, have I bothered you about it.
--
-- All FKs point at sessions(id) — the surrogate — never at session_id, which
-- is NULL for spawned/reconciled sessions until a hook adopts them.
--
-- Applied AFTER herd/core/schema.sql against the same DB file. Table prefix
-- `herd_` keeps the seam legible in the DB; the source seam is the real one.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── HERD_SESSIONS ──────────────────────────────────────────────────────────
-- Merged placement + job. One row per session herd knows about.
--
-- MUTABILITY CONTRACT — reconcile rewrites this row on every tick, so:
--   IMMUTABLE (set once at spawn/discovery, reconcile MUST NOT touch):
--       job_name, created_at, live
--   MUTABLE (reconcile overwrites freely):
--       kitty_socket, os_window_id, tab_id, window_id, window_title,
--       herd_var, source, verified_at
-- This is enforced by DISCIPLINE, not structure: name your columns in the
-- UPDATE, never blanket-overwrite. Precedent: klawde's session_start.sh
-- ON CONFLICT DO UPDATE names each column and deliberately preserves
-- started_at. See UPSERT_RECONCILE in herd/kitty/reconcile.py.
--
-- PLACEMENT IS A CACHE, NOT A FACT. Never trusted on the focus path: herd
-- re-derives from `kitten @ ls` (~20-23ms over a unix socket — MEASURED; the
-- cost is kitten's python process spawn, not the JSON, so --match filtering
-- does not make it cheaper) before every jump and rewrites the row. Local-only
-- means there is no excuse for cache-and-pray. A kitty restart invalidates
-- every window_id here; re-derivation makes that invisible rather than fatal.
-- klawde fires its match from a possibly-hours-old row and surfaces the error
-- on miss.
CREATE TABLE IF NOT EXISTS herd_sessions (
    session_pk   INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,

    -- ── immutable: herd's job identity ───────────────────────────────────
    job_name     TEXT,        -- NULL for sessions herd didn't spawn.
                              -- NOT sessions.session_name — that one is
                              -- user-mutable and would break our handle.
    created_at   TEXT,
    live         INTEGER NOT NULL DEFAULT 1,
                              -- Denormalized from sessions.stopped_at.
                              -- UNAVOIDABLE: SQLite prohibits subqueries in
                              -- partial index WHERE clauses, so
                              --   WHERE (SELECT stopped_at FROM sessions ...)
                              -- is rejected outright. Merging placement+jobs
                              -- does NOT remove this need. Maintained by
                              -- trg_herd_job_death below.

    -- ── mutable: kitty placement, rewritten each reconcile ───────────────
    kitty_socket TEXT NOT NULL,   -- $KITTY_LISTEN_ON. window_id is MEANINGLESS
                                  -- without it: listen_on unix:/tmp/kitty-
                                  -- {kitty_pid} gives each kitty instance its
                                  -- own socket and its own id space. Never
                                  -- match on window_id alone.
    os_window_id INTEGER,
    tab_id       INTEGER,         -- MEASURED: focus does NOT need a separate
                                  -- focus-tab call — `focus-window --match
                                  -- id:N` on a window in a background tab
                                  -- activates that tab and returns 0. Kept for
                                  -- rendering/grouping, not for the jump.
    window_id    INTEGER,         -- kitty's leaf id. MUTABLE across restarts.
    window_title TEXT,
    herd_var     TEXT,            -- HERD_JOB user var, stamped at spawn via
                                  -- `kitten @ launch --var HERD_JOB=<name>`.
                                  -- The durable handle: `--match
                                  -- var:HERD_JOB=x` is immune to kitty
                                  -- renumbering. NULL when herd didn't spawn.
                                  --
                                  -- MEASURED CAVEAT: user vars are WINDOW-
                                  -- scoped and STICKY — they survive claude
                                  -- exiting. So this identifies a WINDOW, never
                                  -- a session. Always AND it with pid liveness
                                  -- before believing a match. (Same is true of
                                  -- `env:`.) Match values are unanchored regex:
                                  -- anchor them (^...$) or `job` matches
                                  -- `job-2`.
    source       TEXT NOT NULL CHECK (source IN ('spawn','hook','reconcile')),
    verified_at  TEXT NOT NULL    -- staleness made explicit. TUI renders a
                                  -- placement as degraded when older than the
                                  -- last reconcile tick.
);

-- Reconcile's join: kitty hands us (socket, window_id); find the session.
-- Composite because window_id alone is not unique across kitty instances.
--
-- `live = 1` IS LOAD-BEARING. Without it a DEAD session owns (socket,
-- window_id) forever, and the most ordinary thing a user can do — exit claude
-- and start another in the same window — makes the new session's placement
-- INSERT fail on UNIQUE. The session lands in tier 1 with no placement: listed
-- but unjumpable, and nothing surfaces an error. Same argument as
-- idx_herd_job_live below: a kitty window is a RECYCLABLE HANDLE, exactly like
-- a job name. Dead rows keep window_id as history.
--
-- THE CONTRACT THIS CREATES — read before writing any query on this key:
-- (kitty_socket, window_id) is now unique only among LIVE rows. A scalar
-- subquery keyed on the pair can therefore match a dead row and a live row and
-- SQLite will pick one ARBITRARILY. Every read, guard, or join on this pair
-- MUST carry `AND live = 1`. See writes.sql W2/W3a.
CREATE UNIQUE INDEX IF NOT EXISTS idx_herd_window
    ON herd_sessions(kitty_socket, window_id)
    WHERE window_id IS NOT NULL AND live = 1;

-- Job names are RECYCLABLE HANDLES, not permanent identifiers: unique among
-- live jobs only, so `herd new api-refactor` works again tomorrow after
-- today's died, while history is retained.
CREATE UNIQUE INDEX IF NOT EXISTS idx_herd_job_live
    ON herd_sessions(job_name) WHERE live = 1 AND job_name IS NOT NULL;

-- Maintains the `live` denormalization. DECLARED HERE (tier 2 owns it) but
-- ATTACHED to sessions (tier 1). That keeps core/schema.sql free of any
-- mention of herd — the boundary is a SOURCE-level property. Same allowed
-- direction as reconcile writing tier-1 rows: tier2 -> tier1 is fine.
CREATE TRIGGER IF NOT EXISTS trg_herd_job_death
AFTER UPDATE OF stopped_at ON sessions
WHEN NEW.stopped_at IS NOT NULL AND OLD.stopped_at IS NULL
BEGIN
    UPDATE herd_sessions SET live = 0 WHERE session_pk = NEW.id;
END;


-- ── HERD_ATTENTION ─────────────────────────────────────────────────────────
-- Records what herd DID, not what herd THINKS.
--
-- "Needs attention" is DERIVED every tick from sessions.last_event_at +
-- last_event_type and is deliberately NOT stored. What must persist is the
-- ACTION: paging is a side effect in the world, and without memory a 1s poll
-- loop would page you 60x/min about one stuck session.
--
-- Kept OUT of herd_sessions for two reasons:
--   1. It is the only table written on EVERY tick; everything else is
--      read-mostly. Isolating the write path keeps contention off the rest.
--   2. `DELETE FROM herd_attention` is a meaningful "shut up and re-evaluate
--      everything" operation. As columns on a wide row that becomes a 4-col
--      UPDATE indistinguishable from data loss.
--
-- WRITER: the TUI's tick. NOT hooks — hooks cannot detect silence, since by
-- definition nothing is firing. This makes herd's TUI a WRITER, breaking
-- klawde's read-only-TUI invariant (it opens file:...?mode=ro and only cracks
-- this for a short-lived rw connection in reset_needs_approval). herd holds
-- one RW connection; WAL keeps it out of the hooks' way.
CREATE TABLE IF NOT EXISTS herd_attention (
    session_pk   INTEGER PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,

    attention_at TEXT,    -- the EDGE: when the silence rule first tripped.
                          -- NOT the same as last_event_at's age. A session
                          -- silent 5m after a tool call has last_event_at 5m
                          -- old but attention_at seconds old — it only just
                          -- BECAME suspicious at the threshold.
    paged_at     TEXT,    -- when we last actually notified you
    paged_level  INTEGER NOT NULL DEFAULT 0,   -- escalation rung
    ack_at       TEXT     -- implicit (you jumped to it — focus sets this) or
                          -- explicit (dismissed in the TUI).
);

-- RE-ARM RULE: an ack suppresses paging until the NEXT semantic event, at
-- which point the row is cleared and the rule may trip fresh. So ack means
-- "I have seen THIS silence", not "never bother me about this session".
CREATE INDEX IF NOT EXISTS idx_herd_attention_active
    ON herd_attention(attention_at) WHERE attention_at IS NOT NULL;
