"""Validation suite for the herd data model foundation.
Every claim the design rests on is asserted here, not narrated.

Run: python3 validate.py   (no install needed; src/ is put on the path below)
This suite is the ONLY CI gate: import-linter cannot see the tier boundary,
because that boundary is SQL and the hooks are bash. So it lives here."""
import sqlite3, os, pathlib, re, sys, contextlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent / "src"))
from herd.db import (CORE_SCHEMA, HERD_SCHEMA, WRITES as WRITES_PATH,
                     load_statements, apply_schema)

FAILED = []
def check(name, cond, detail=""):
    if cond: print(f"  \033[32mPASS\033[0m {name}" + (f" — {detail}" if detail else ""))
    else:
        print(f"  \033[31mFAIL\033[0m {name} — {detail}"); FAILED.append(name)

@contextlib.contextmanager
def guard(name):
    """Turn an unexpected exception into a FAIL instead of a dead script.

    Without this the suite ABORTS on the first surprising error and every later
    check silently never runs — so it reports fewer failures than exist, which
    is the one thing a validation suite must never do. Found the hard way:
    breaking W2's live=1 guard made a later check raise IntegrityError, killing
    the run at that line and hiding the rest of the section behind a traceback."""
    try:
        yield
    except Exception as e:
        check(name, False, f"raised {type(e).__name__}: {e}")

CORE = CORE_SCHEMA.read_text()
HERD = HERD_SCHEMA.read_text()
WRITES = WRITES_PATH.read_text()
T0 = "2026-07-15T10:00:00.000Z"
T1 = "2026-07-15T10:05:00.000Z"
T2 = "2026-07-15T10:10:00.000Z"

# The loader is production code (herd.db), not a copy living in the suite —
# reconcile will load the same statements the same way, so using it here tests it.
W = load_statements()

# ── STATEMENT INTEGRITY: every named statement must be COMPLETE ────────────
# Both parsers (python load_statements + bash stmt()) terminate a statement at
# the first ';'. A ';' inside an inline comment silently truncates the statement
# to a prefix — sqlite3 then fails with "incomplete input", or worse, executes a
# valid-but-partial statement. sqlite3.complete_statement() catches truncation
# structurally. (This bit W5_statusline twice during the rate-limit extension.)
_incomplete = [n for n, s in W.items() if not sqlite3.complete_statement(s.strip())]
check("every named statement is complete (no ';'-in-comment truncation)",
      not _incomplete,
      f"truncated/incomplete: {_incomplete}" if _incomplete
      else f"all {len(W)} statements terminate cleanly")

# The ps daemon replaced kitty-based reconcile: hooks own identity/placement/pid,
# W3d/W3e do the reaping. W3a_discover (kitty window discovery), W3b_placement
# (per-tick placement refresh) and W3c_pid (fill pid from `kitten @ ls`) are dead —
# nothing calls them. Assert they are GONE so the retired architecture can't creep
# back in as a dormant statement.
_dead_stmts = [n for n in ("W3a_discover", "W3b_placement", "W3c_pid") if n in W]
check("dead kitty-reconcile statements are deleted (W3a/W3b/W3c)",
      not _dead_stmts,
      f"still present: {_dead_stmts}" if _dead_stmts
      else "reconcile is the ps daemon (W3d/W3e); no kitty discovery/placement/pid-fill")

def fresh(tier2=True):
    if os.path.exists("f.db"): os.remove("f.db")
    for s in ("f.db-wal","f.db-shm"):
        if os.path.exists(s): os.remove(s)
    c = sqlite3.connect("f.db")
    # autocommit. Section N runs the real bash hooks against this file from
    # ANOTHER connection, and python's default isolation_level would leave the
    # setup rows in an uncommitted transaction that close() silently rolls back
    # — the hook then sees an empty database. That produced a *false pass*:
    # check 48 lost its reconciled row, W2b inserted a fresh one that landed on
    # the same rowid it was asserting against, and the adopt path was never
    # exercised at all.
    c.isolation_level = None
    apply_schema(c, tier2=tier2)
    c.execute("PRAGMA foreign_keys=ON")
    c.row_factory = sqlite3.Row
    return c

print("\n\033[1m═══ A. TIER BOUNDARY (source-level) ═══\033[0m")
core_l = CORE.lower()
# strip comments before scanning for herd references
core_code = "\n".join(l.split("--")[0] for l in CORE.splitlines()).lower()
check("schema/core.sql contains no herd_ tables",
      "herd_" not in core_code, "scanned non-comment DDL")
check("schema/core.sql declares no triggers",
      "create trigger" not in core_code)
herd_code = "\n".join(l.split("--")[0] for l in HERD.splitlines()).lower()
# The decouple removed the ONLY thing tier 2 attached to tier 1. Assert it stays
# gone: no trigger anywhere, and nothing in herd.sql declares ON sessions. The
# boundary is now strictly one-way (tier2 -> tier1 via FK), with zero tier-2
# machinery reaching back into the core table.
check("schema/herd.sql declares no trigger (decoupled)",
      "create trigger" not in herd_code)
check("no tier-2 DDL attaches to the sessions table",
      "on sessions" not in herd_code)
check("no `live` denormalization column anywhere",
      "live" not in "".join(l.split("--")[0] for l in HERD.splitlines()).lower())

# ── Core columns receive ONLY Claude/process signals, never a tier-2 VALUE ──
# Accepted design: the adopt-path writers (W2/W5b) READ herd_sessions to
# ROUTE their write ("which session row is this arriving Claude session?"). That
# is a routing key, not data. The invariant that must hold is that no tier-2
# VALUE ever lands in a core column. Enforce it structurally: in every statement
# that writes sessions/events, the value-producing region — everything BEFORE
# the first WHERE (the SET list, the INSERT projection, the VALUES tuple) — must
# not reference a herd_ table. The routing region (WHERE and its subqueries) may.
_core_writers = [n for n, s in W.items()
                 if re.search(r"\b(INSERT\s+INTO|UPDATE)\s+(sessions|events)\b",
                              "\n".join(l.split("--")[0] for l in s.splitlines()), re.I)]
_leaks = []
for n in _core_writers:
    code = "\n".join(l.split("--")[0] for l in W[n].splitlines())
    values_region = re.split(r"\bWHERE\b", code, maxsplit=1, flags=re.I)[0]
    if re.search(r"\bherd_(sessions|attention)\b", values_region, re.I):
        _leaks.append(n)
check("core columns take values only from Claude/process signals (routing reads OK)",
      not _leaks,
      f"tier-2 VALUE leaked into a core column in {_leaks}" if _leaks
      else f"all {len(_core_writers)} core writers: herd_ appears only in routing (WHERE), never in values")

# ── 45/46. THE TIER THESIS, EXECUTED ──────────────────────────────────────
# Tier 1 claims to be "facts that would be true if herd didn't exist". Until
# now that was asserted by grepping for the string 'herd_' — but the schema was
# never once applied WITHOUT tier 2. These two run it.
with guard("45 tier 1 applies standalone (herd absent entirely)"):
    c = fresh(tier2=False)
    t = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' "
                                 "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    pk = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO events(session_pk,event_type,source,timestamp) VALUES(?,'start','hook',?)",(pk,T0))
    c.execute("UPDATE sessions SET stopped_at=? WHERE id=?",(T1,pk))   # no trigger to fire
    check("45 tier 1 applies standalone (herd absent entirely)",
          t == ['events','sessions'],
          f"a herd-less install is a working install; tables={t}")

# 46 changed with the decouple. It used to prove the dependency at DDL time —
# CREATE TRIGGER ... ON sessions failed if sessions didn't exist. With the
# trigger gone, herd.sql applies standalone (SQLite does not validate FK parent
# tables at CREATE TABLE time). So the dependency is now purely a RUNTIME fact:
# tier 2 is inert without tier 1. Assert BOTH halves — the DDL change AND that
# tier 2 remains non-functional alone.
with guard("46 tier 2 applies standalone but is inert without tier 1"):
    if os.path.exists("g.db"): os.remove("g.db")
    c2 = sqlite3.connect("g.db"); c2.isolation_level = None
    c2.executescript(HERD)                       # applies standalone now — the new fact
    c2.execute("PRAGMA foreign_keys=ON")
    inert = False
    try:
        # every use of herd_sessions must reach `sessions`; with it absent, even
        # an insert cannot resolve its FK parent — tier 2 does nothing on its own.
        c2.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,source,verified_at)"
                   " VALUES(1,'k','spawn',?)",(T0,))
    except sqlite3.OperationalError:
        inert = True
    check("46 tier 2 applies standalone but is inert without tier 1", inert,
          "herd.sql creates its tables alone, but every row it holds dangles off "
          "sessions — the direction is real, just no longer DDL-enforced")
    c2.close(); os.remove("g.db")

print("\n\033[1m═══ B. SCHEMA APPLIES ═══\033[0m")
c = fresh()
tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
check("four tables", tabs == ['events','herd_attention','herd_sessions','sessions'], str(tabs))
check("WAL mode", c.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
check("auto_vacuum=INCREMENTAL", c.execute("PRAGMA auto_vacuum").fetchone()[0] == 2)
check("idempotent re-apply", (c.executescript(CORE), c.executescript(HERD), True)[-1])
# herd_sessions carries only placement that matters. os_window_id/tab_id/
# window_title were render-only, obtainable ONLY from `kitten @ ls`, and were the
# sole reason the write path would query kitty. Dropped: the jump needs only
# (kitty_socket, window_id), both of which the hook gets from env. The TUI fetches
# grouping/titles on demand. This guard blocks their reintroduction.
_hs_cols = {r[1] for r in c.execute("PRAGMA table_info(herd_sessions)")}
_render_only = {"os_window_id", "tab_id", "window_title"} & _hs_cols
check("herd_sessions has no render-only kitty columns (kitten @ ls off the write path)",
      not _render_only,
      f"still present: {sorted(_render_only)}" if _render_only
      else "kitty_socket + window_id carry the jump; render data is fetched on demand")

print("\n\033[1m═══ C. THE SURROGATE KEY (the spine) ═══\033[0m")
c = fresh()
pk = c.execute("INSERT INTO sessions(cwd,status,status_source,started_at,updated_at) "
               "VALUES('/code/app','unknown','reconcile',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
          "window_id,herd_var,source,verified_at) "
          "VALUES(?,?,?,?,?,?,'spawn',?)",
          (pk,"api-refactor",T0,"unix:/tmp/kitty-1",7,"api-refactor",T0))
c.execute("INSERT INTO herd_attention(session_pk,attention_at) VALUES(?,?)",(pk,T0))
check("tier-2 rows exist with session_id NULL",
      c.execute("SELECT session_id FROM sessions WHERE id=?",(pk,)).fetchone()[0] is None,
      "spawned session has job+placement+attention before Claude reports a UUID")
# adopt — the SHIPPING W2
n = c.execute(W["W2_adopt"], {"session_id":"a3f9-uuid","cwd":"/code/app","model":"opus",
                              "transcript":"/t.jsonl","now":T1,"pid":4242,
                              "socket":"unix:/tmp/kitty-1","win":7}).rowcount
check("W2 adopt via (socket,window_id)", n == 1)
r = c.execute("""SELECT s.session_id,h.job_name,h.window_id,a.attention_at
                 FROM sessions s LEFT JOIN herd_sessions h ON h.session_pk=s.id
                 LEFT JOIN herd_attention a ON a.session_pk=s.id WHERE s.id=?""",(pk,)).fetchone()
check("tier-2 survives adoption", tuple(r) == ("a3f9-uuid","api-refactor",7,T0), str(tuple(r)))
n2 = c.execute(W["W2_adopt"], {"session_id":"a3f9-uuid","cwd":"/code/app","model":"opus",
                               "transcript":"/t.jsonl","now":T1,"pid":4242,
                               "socket":"unix:/tmp/kitty-1","win":7}).rowcount
check("W2 adopt is idempotent", n2 == 0, "re-fire is a no-op")
check("multiple unadopted rows coexist (UNIQUE ignores NULL)",
      (c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/x',?,?)",(T0,T0)),
       c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/y',?,?)",(T0,T0)),
       c.execute("SELECT COUNT(*) FROM sessions WHERE session_id IS NULL").fetchone()[0])[-1] == 2)

print("\n\033[1m═══ D. MUTABILITY CONTRACT (hook re-fire must not clobber) ═══\033[0m")
# W2b_placement is the LIVING writer that upserts herd_sessions on a hook re-fire.
# A resumed SPAWNED session must keep its job identity: job_name/created_at/herd_var
# and source='spawn' are preserved (absent from the SET list), while the placement
# columns it does own (kitty_socket, window_id, verified_at) update. (W3b, the old
# reconcile writer this once tested, is deleted.)
c = fresh()
pk = c.execute("INSERT INTO sessions(session_id,cwd,started_at,updated_at) VALUES('u1','/a',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
          "window_id,herd_var,source,verified_at) VALUES(?,?,?,?,?,?,'spawn',?)",
          (pk,"api-refactor",T0,"unix:/tmp/kitty-1",7,"api-refactor",T0))
c.execute(W["W2b_placement"], {"session_id":"u1","socket":"unix:/tmp/kitty-9",
                               "win":42,"now":T2})
r = c.execute("SELECT job_name,created_at,kitty_socket,window_id,source,herd_var,verified_at "
              "FROM herd_sessions WHERE session_pk=?",(pk,)).fetchone()
check("hook re-fire preserves job_name", r["job_name"] == "api-refactor")
check("hook re-fire preserves created_at", r["created_at"] == T0)
check("hook re-fire preserves herd_var", r["herd_var"] == "api-refactor",
      "the hook can't know the spawn-time var and must not erase it")
check("hook re-fire preserves source='spawn'", r["source"] == "spawn", "provenance doesn't decay to 'hook'")
check("hook re-fire DOES update window_id", r["window_id"] == 42)
check("hook re-fire DOES update kitty_socket", r["kitty_socket"] == "unix:/tmp/kitty-9")
check("hook re-fire DOES update verified_at", r["verified_at"] == T2)

print("\n\033[1m═══ E. JOB NAMES: recyclable handles via R_job_live (no trigger, no UNIQUE) ═══\033[0m")
# Recyclability is now the R_job_live check + sessions.stopped_at, not a partial
# UNIQUE index maintained by a trigger. "Taken" == a LIVE session holds the name.
c = fresh()
def job_holder(conn, job):
    r = conn.execute(W["R_job_live"], {"job": job}).fetchone()
    return r["session_pk"] if r else None

p1 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,?,?,'spawn',?)",(p1,"api-refactor",T0,"unix:/tmp/k1",7,T0))
check("R_job_live reports the live holder", job_holder(c, "api-refactor") == p1,
      "spawn checks this and refuses a name a live session already holds")
# the DB does NOT reject a duplicate insert anymore — the app must consult
# R_job_live first. Prove the constraint is gone (so a stale/dead row can't block):
p2 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/b',?,?)",(T0,T0)).lastrowid
c.execute("UPDATE sessions SET stopped_at=? WHERE id=?",(T2,p1))   # holder dies (just stopped_at)
check("death frees the name with no trigger", job_holder(c, "api-refactor") is None,
      "setting stopped_at makes R_job_live see it dead — no live column to desync")
# reuse: session 2 takes the name. history retained (both rows keep job_name).
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,?,?,'spawn',?)",(p2,"api-refactor",T2,"unix:/tmp/k1",8,T2))
check("name reusable after death",
      c.execute("SELECT COUNT(*) FROM herd_sessions WHERE job_name='api-refactor'").fetchone()[0] == 2,
      "history retained: 2 rows, one live one dead")
check("R_job_live returns exactly the LIVE holder among history",
      job_holder(c, "api-refactor") == p2,
      "dead + live rows share the name; the JOIN picks the live one")
# NULL job_name (reconciled sessions) never collides and is never 'held'
p3 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/c',?,?)",(T0,T0)).lastrowid
p4 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/d',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'hook',?)",(p3,"unix:/tmp/k1",20,T0))
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'hook',?)",(p4,"unix:/tmp/k1",21,T0))
check("many NULL job_names coexist", job_holder(c, None) is None, "reconciled sessions have no job")

print("\n\033[1m═══ F. PID / LIVENESS ═══\033[0m")
c = fresh()
c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at,stopped_at) VALUES(4821,'/a',?,?,?)",(T0,T0,T1))
c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at,stopped_at) VALUES(4821,'/a',?,?,?)",(T0,T0,T1))
c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(4821,'/a',?,?)",(T0,T0))
check("many DEAD sessions may share a pid", True, "pid reuse after death is legal")
try:
    c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(4821,'/b',?,?)",(T0,T0))
    check("only one LIVE session per pid", False, "two live rows allowed!")
except sqlite3.IntegrityError:
    check("only one LIVE session per pid", True)
c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/x',?,?)",(T0,T0))
c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/y',?,?)",(T0,T0))
check("many live sessions with NULL pid coexist", True, "pid unknown before reconcile")

print("\n\033[1m═══ G. THE TWO CLOCKS (the idle-signal thesis) ═══\033[0m")
c = fresh()
c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
          " VALUES('s1','/a','working',?,'tool',?,?)",(T0,T0,T0))
# statusline ticks repeatedly — must move updated_at, must NOT move last_event_at
for t in (T1,T2):
    c.execute(W["W5_statusline"], {"model":None,"sname":None,"ctx":42,"cost":1.5,"branch":None,
                                   "rl5":None,"rl5reset":None,"rl7":None,"rl7reset":None,
                                   "now":t,"session_id":"s1"})
r = c.execute("SELECT last_event_at,updated_at FROM sessions WHERE session_id='s1'").fetchone()
check("statusline moves updated_at", r["updated_at"] == T2)
check("statusline does NOT move last_event_at", r["last_event_at"] == T0,
      "the gap between clocks IS the attention signal")
gap = "10:00 -> 10:10 = 10min of true silence, visible"
check("idle signal is real", r["last_event_at"] != r["updated_at"], gap)

print("\n\033[1m═══ H. STATUSLINE MUST NOT CREATE OR RESURRECT ═══\033[0m")
c = fresh()
n = c.execute(W["W5_statusline"], {"model":None,"sname":None,"ctx":50,"cost":None,"branch":None,
                                   "rl5":None,"rl5reset":None,"rl7":None,"rl7reset":None,
                                   "now":T1,"session_id":"ghost"}).rowcount
check("statusline on unknown session is a no-op", n == 0,
      "UPDATE-only: never invents rows with empty cwd")
c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at,stopped_at)"
          " VALUES('dead','/a','stopped',?,?,?)",(T0,T0,T1))
n = c.execute(W["W5_statusline"], {"model":None,"sname":None,"ctx":50,"cost":None,"branch":None,
                                   "rl5":None,"rl5reset":None,"rl7":None,"rl7reset":None,
                                   "now":T2,"session_id":"dead"}).rowcount
check("statusline cannot resurrect a stopped session", n == 0)

print("\n\033[1m═══ I. PAGER LIFECYCLE ═══\033[0m")
c = fresh()
pk = c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
               " VALUES('s1','/a','waiting',?,'stop',?,?)",(T0,T0,T0)).lastrowid
c.execute(W["W6a_arm"], {"pk":pk,"now":T1})
c.execute(W["W6a_arm"], {"pk":pk,"now":T2})   # tick again — must not move the edge
check("attention_at is an EDGE, not re-stamped",
      c.execute("SELECT attention_at FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()[0] == T1,
      "COALESCE preserves first trip")
c.execute(W["W6b_paged"], {"now":T2,"level":2,"pk":pk})
c.execute(W["W6c_ack"], {"now":T2,"pk":pk,"focus_started_at":T2})
r = c.execute("SELECT attention_at,paged_at,paged_level,ack_at FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()
check("page + ack recorded", tuple(r) == (T1,T2,2,T2), str(tuple(r)))
c.execute(W["W6d_rearm"], {"pk":pk})
check("re-arm clears row", c.execute("SELECT COUNT(*) FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()[0]==0,
      "ack means 'I saw THIS silence', not 'never bother me again'")
c.execute(W["W6a_arm"], {"pk":pk,"now":T2})
check("rule can trip fresh after re-arm",
      c.execute("SELECT attention_at FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()[0]==T2)

print("\n\033[1m═══ J. CASCADE ═══\033[0m")
c = fresh()
pk = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,?,?,'spawn',?)",(pk,"j",T0,"unix:/tmp/k1",7,T0))
c.execute("INSERT INTO herd_attention(session_pk,attention_at) VALUES(?,?)",(pk,T0))
c.execute("INSERT INTO events(session_pk,event_type,source,timestamp) VALUES(?,'start','hook',?)",(pk,T0))
c.execute("DELETE FROM sessions WHERE id=?",(pk,))
orph = {t: c.execute(f"SELECT COUNT(*) FROM {t} WHERE session_pk=?",(pk,)).fetchone()[0]
        for t in ("herd_sessions","herd_attention","events")}
check("ON DELETE CASCADE cleans all tier-2 + events", sum(orph.values())==0, str(orph))

print("\n\033[1m═══ K. CONSTRAINTS ═══\033[0m")
c = fresh()
for col,val,tab in (("status","bogus","sessions"),("status_source","nope","sessions")):
    try:
        c.execute(f"INSERT INTO sessions(cwd,{col},started_at,updated_at) VALUES('/z',?,?,?)",(val,T0,T0))
        check(f"{col} CHECK rejects garbage", False, "accepted!")
    except sqlite3.IntegrityError:
        check(f"{col} CHECK rejects garbage", True)
pk = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
try:
    c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'bogus',?)",(pk,"s",1,T0))
    check("herd source CHECK rejects garbage", False, "accepted!")
except sqlite3.IntegrityError:
    check("herd source CHECK rejects garbage", True)
try:
    c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(999,?,?,'spawn',?)",("s",1,T0))
    check("FK enforced on herd_sessions", False, "orphan accepted!")
except sqlite3.IntegrityError:
    check("FK enforced on herd_sessions", True)
# (socket,window_id) is DELIBERATELY NOT unique anymore. A dead row and a live
# row may share a window — that is what makes window reuse and resume work
# without a `live` denormalization. The liveness JOIN separates them; reconcile's
# ground-truth rebuild keeps at most one LIVE row per window (an app invariant,
# tested in the reconcile fixture, not by a DB constraint).
p2 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/b',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'spawn',?)",(pk,"unix:/tmp/k1",7,T0))
c.execute("UPDATE sessions SET stopped_at=? WHERE id=?",(T1,pk))   # first holder dies
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'spawn',?)",(p2,"unix:/tmp/k1",7,T0))
check("(socket,window_id) may repeat across a dead+live pair", True,
      "no UNIQUE constraint — window is a recyclable handle; JOIN tells them apart")
live_in_win = c.execute(
    "SELECT h.session_pk FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk "
    "WHERE h.kitty_socket='unix:/tmp/k1' AND h.window_id=7 AND s.stopped_at IS NULL").fetchall()
check("exactly one LIVE session resolves for the reused window",
      [r["session_pk"] for r in live_in_win] == [p2],
      "the JOIN returns only the live occupant; the dead row is history")

print("\n\033[1m═══ L. TUI MAIN QUERY ═══\033[0m")
c = fresh()
for i,(cwd,st,ev) in enumerate([("/app","waiting","stop"),("/api","working","tool"),("/web","working","tool")]):
    p = c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
                  " VALUES(?,?,?,?,?,?,?)",(f"s{i}",cwd,st,T0,ev,T0,T2)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,?,?,'spawn',?)",(p,f"job{i}",T0,"unix:/tmp/k1",i+1,T2))
    if i == 0: c.execute("INSERT INTO herd_attention(session_pk,attention_at) VALUES(?,?)",(p,T1))
Q = W["R1_list"]
plan = [r[-1] for r in c.execute("EXPLAIN QUERY PLAN "+Q)]
for p in plan: print("      ", p)
check("main query uses idx_sessions_live", any("idx_sessions_live" in p for p in plan))
check("tier-2 joins hit PK directly", sum("INTEGER PRIMARY KEY" in p for p in plan) == 2)
rows = c.execute(Q).fetchall()
check("attention-first ordering", rows[0]["cwd"] == "/app" and rows[0]["attention_at"] == T1,
      f"waiting session sorts first; got {[r['cwd'] for r in rows]}")

# ═══════════════════════════════════════════════════════════════════════════
# M. WINDOW REUSE + THE WRITE PATHS
#
# Everything below passed the original 40 checks while being broken. §K only
# ever tested two LIVE sessions in one window; it never asked what happens to
# the ordinary case — a user exits claude and starts another in the same
# window. Green is not the same as complete.
# ═══════════════════════════════════════════════════════════════════════════
print("\n\033[1m═══ M. WINDOW REUSE + WRITE PATHS (41-44) ═══\033[0m")

# ── 41. a window is a RECYCLABLE HANDLE — dead + live rows coexist, JOIN splits ──
SOCK = "unix:/tmp/kitty-20035"
def live_in_window(conn, sock, win):
    return [r["session_pk"] for r in conn.execute(
        "SELECT h.session_pk FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk "
        "WHERE h.kitty_socket=? AND h.window_id=? AND s.stopped_at IS NULL", (sock, win))]

c = fresh()
a = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(111,'/code/herd',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,5,'hook',?)",(a,T0,SOCK,T0))
c.execute("UPDATE sessions SET stopped_at=?,status='stopped' WHERE id=?",(T1,a))
b = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(222,'/code/herd',?,?)",(T2,T2)).lastrowid
# no UNIQUE index: the reused-window placement INSERT simply succeeds now.
c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,5,'hook',?)",(b,T2,SOCK,T2))
check("41 window reuse: new session gets placement in a reused window",
      live_in_window(c, SOCK, 5) == [b],
      "dead A + live B share window 5; the JOIN returns only B (A is history)")
# The 'one live per window' invariant is now app-level (reconcile's rebuild), not
# a DB constraint. The DB DELIBERATELY allows two live rows — assert that, so a
# future reader doesn't expect the old UNIQUE to catch a rebuild bug.
c.execute("UPDATE sessions SET stopped_at=NULL WHERE id=?",(a,))   # force both live
check("41b DB no longer rejects two live rows in one window (app-level now)",
      len(live_in_window(c, SOCK, 5)) == 2,
      "reconcile's ground-truth rebuild keeps it to one; the reconcile fixture tests that")

# ── 41c. THE RESUME REGRESSION — the reason this whole pass exists ─────────
# Old model: die -> trigger live=0; resume (W2b sets stopped_at=NULL) never reset
# live, so job/window read as free forever while the session was live. Permanent
# desync. New model: liveness IS stopped_at, so revive is self-consistent with
# no flag to forget. This check FAILS on the old schema and passes on the new.
c = fresh()
pk = c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
               " VALUES('u1','/code/herd','working',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
          "window_id,source,verified_at) VALUES(?,'api',?,?,5,'spawn',?)",(pk,T0,SOCK,T0))
c.execute(W["W4_end"], {"session_id":"u1","now":T1})            # die (SessionEnd)
dead_job = c.execute(W["R_job_live"], {"job":"api"}).fetchone()
dead_win = live_in_window(c, SOCK, 5)
c.execute(W["W2b_insert"], {"session_id":"u1","cwd":"/code/herd","model":"opus",
                            "transcript":"/t.jsonl","now":T2,"pid":None})  # claude --resume
revived_job = c.execute(W["R_job_live"], {"job":"api"}).fetchone()
revived_win = live_in_window(c, SOCK, 5)
check("41c resume revives job+window with no stored flag to desync",
      dead_job is None and dead_win == []
      and revived_job is not None and revived_job["session_pk"] == pk and revived_win == [pk],
      f"dead(job={dead_job and dead_job['session_pk']},win={dead_win}) -> "
      f"revived(job={revived_job and revived_job['session_pk']},win={revived_win}); "
      "old model left both empty forever")

# ── 42. THE THESIS: last_event_at must advance on repeated same-status events ──
# post_tool_use.sh is the hot path and always sends status='working'. A guard of
# `AND status IS NOT :status` freezes last_event_at, and herd then pages you
# about a session that is actively running tools. klawde's bug, mirrored.
c = fresh()
c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
          " VALUES('s1','/a','working',?,'tool',?,?)",(T0,T0,T0))
for t in ("2026-07-15T10:01:00.000Z","2026-07-15T10:02:00.000Z",T1):
    c.execute(W["W4_event"], {"status":"working","now":t,"etype":"tool","session_id":"s1"})
    c.execute(W["W4_event_log"], {"etype":"tool","now":t,"raw":None,"session_id":"s1"})
r = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0]
check("42 last_event_at advances on repeated same-status events", r == T1,
      f"busy session must not read as silent; got {r}")
ev = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
check("42b events and sessions agree", ev == 3 and r == T1,
      "nothing reads events, so a disagreement here is invisible forever")

# ── 43. adoption must target the LIVE row ─────────────────────────────────
def reused_window():
    """dead session A and live session B, both claiming window 5."""
    c = fresh()
    a = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(111,'/code/herd',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,5,'hook',?)",(a,T0,SOCK,T0))
    c.execute("UPDATE sessions SET stopped_at=?,status='stopped' WHERE id=?",(T1,a))
    b = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(222,'/code/herd',?,?)",(T2,T2)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,5,'hook',?)",(b,T2,SOCK,T2))
    return c, a, b

with guard("43 W2 adopts the LIVE row, not the dead predecessor"):
    c, a, b = reused_window()
    c.execute(W["W2_adopt"], {"session_id":"uuid-B","cwd":"/code/herd","model":"opus",
                              "transcript":"/t.jsonl","now":T2,"pid":222,"socket":SOCK,"win":5})
    dead_sid = c.execute("SELECT session_id FROM sessions WHERE id=?",(a,)).fetchone()[0]
    live_sid = c.execute("SELECT session_id FROM sessions WHERE id=?",(b,)).fetchone()[0]
    check("43 W2 adopts the LIVE row, not the dead predecessor",
          live_sid == "uuid-B" and dead_sid is None,
          f"dead={dead_sid} live={live_sid}")

# W5b (Path C) must be equally careful. Built on its OWN fixture: sharing 43's
# would make this assert nothing when 43 fails, and collide on sessions.session_id.
with guard("43b W5b statusline-adopt also targets the LIVE row"):
    c, a, b = reused_window()
    c.execute(W["W5b_adopt"], {"session_id":"uuid-C","now":T2,"socket":SOCK,"win":5})
    dead_sid = c.execute("SELECT session_id FROM sessions WHERE id=?",(a,)).fetchone()[0]
    live_sid = c.execute("SELECT session_id FROM sessions WHERE id=?",(b,)).fetchone()[0]
    check("43b W5b statusline-adopt also targets the LIVE row",
          live_sid == "uuid-C" and dead_sid is None,
          f"dead={dead_sid} live={live_sid}")

# ── 44. source-level: liveness is derived, never stored ───────────────────
# The invariant flipped with the decouple. It USED to be "every (socket,window)
# lookup carries AND live=1". It is now "liveness is read from sessions, never a
# herd_sessions.live column" — so any statement that looks up a session by
# (socket, window_id) must JOIN sessions and filter stopped_at, and the word
# `live` must not appear as a column reference anywhere.
writes_code = "\n".join(l.split("--")[0] for l in WRITES.splitlines())
stmts = [s for s in writes_code.split(";") if "window_id" in s and "kitty_socket" in s]
# A genuine (socket,window) LOOKUP compares kitty_socket to a BOUND param
# (`kitty_socket = :socket`) to FIND a session row — that is what must carry the
# liveness JOIN. W2b_placement also contains `kitty_socket =`, but only in its SET
# list (`= excluded.kitty_socket`, WRITING a value) while it routes by session_id;
# it is not a window lookup and must not be forced to filter stopped_at. Keying on
# `= :` distinguishes the two without a false positive.
lookups = [s for s in stmts if re.search(r"kitty_socket\s*=\s*:", s, re.I)]
offenders = [" ".join(s.split())[:70] for s in lookups
             if not re.search(r"stopped_at\s+IS\s+NULL", s, re.I)]
check("44 every (socket,window_id) lookup derives liveness via stopped_at",
      not offenders,
      f"un-joined: {offenders}" if offenders else "W2, W5b all JOIN sessions.stopped_at")
check("44a no `live` column reference survives in the write paths",
      not re.search(r"\blive\s*=\s*1\b", writes_code, re.I),
      "the denormalization is gone; nothing may filter on it")
# Ask the DB what the index actually IS — it must NOT be unique now.
c = fresh()
idx_sql = c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_herd_window'").fetchone()
check("44b idx_herd_window is a plain (non-unique) lookup index",
      idx_sql is not None and "unique" not in idx_sql[0].lower(),
      f"got: {idx_sql[0] if idx_sql else 'MISSING'}")
check("44b2 herd_sessions has no `live` column",
      not any(r[1] == "live" for r in c.execute("PRAGMA table_info(herd_sessions)")),
      "liveness lives only in sessions.stopped_at")
# and the inverse of 42, at source level: statusline may never carry a clock.
# Strip comments PER LINE (not .split('--')[0], which drops everything after the
# first comment and would miss a last_event_at assignment placed after one).
w5 = "\n".join(l.split("--")[0] for l in W["W5_statusline"].splitlines())
check("44c W5_statusline never touches last_event_*", "last_event" not in w5.lower(),
      "the one invariant no runtime test survives someone 'optimizing' the statusline")

# ═══════════════════════════════════════════════════════════════════════════
# N. HOOKS — end to end, running the real scripts
#
# The hooks are bash and the SQL is a file, so nothing but this exercises them.
# ═══════════════════════════════════════════════════════════════════════════
print("\n\033[1m═══ N. HOOKS (47-54) ═══\033[0m")
import subprocess, json, tempfile, shutil

HOOKS = pathlib.Path(__file__).resolve().parent / "src" / "herd" / "hooks"
RUNTIME = tempfile.mkdtemp(prefix="herd-validate-")
DBPATH = str(pathlib.Path("f.db").resolve())

def hook(script, payload, env=None):
    e = dict(os.environ)
    e.update({"HERD_DB": DBPATH, "HERD_RUNTIME": RUNTIME,
              "HERD_ERRLOG": f"{RUNTIME}/err.log"})
    if env: e.update(env)
    return subprocess.run(["bash", str(HOOKS / script)], input=json.dumps(payload),
                          capture_output=True, text=True, env=e)

def bash_stmt(name):
    r = subprocess.run(["bash", "-c", f'. "{HOOKS}/common.sh"; stmt {name}'],
                       capture_output=True, text=True, env=dict(os.environ, HERD_DB=DBPATH))
    return r.stdout

def norm(s): return " ".join(s.split())

# ── walk-logic: _walk_claude picks the right ancestor (offline, synthetic tree) ──
# The LIVE proof (real SessionStart lands claude's pid) is done by hand against the
# error log — see the plan. This proves the awk walk's LOGIC deterministically by
# injecting a fake `pid ppid comm` ancestry, so a regression in the traversal is
# caught without a live claude.
def walk_claude(start, table, want=None):
    env = dict(os.environ)
    if want is not None: env["HERD_CLAUDE_NAME"] = want
    r = subprocess.run(["bash", "-c", f'. "{HOOKS}/common.sh"; _walk_claude "{start}"'],
                       input="\n".join(table), capture_output=True, text=True, env=env)
    return r.stdout.strip()

# hook(100) -> sh(200) -> claude(300) -> sh(400) -> claude(500) -> kitty(600)
_nested = ["100 200 bash","200 300 sh","300 400 claude","400 500 sh","500 600 claude","600 1 kitty"]
check("walk returns the NEAREST claude ancestor (not a nested outer one)",
      walk_claude("100", _nested) == "300", f"got {walk_claude('100', _nested)!r} (want 300)")
check("walk basenames comm (full path /usr/bin/claude still matches)",
      walk_claude("100", ["100 200 bash","200 300 sh","300 400 /usr/bin/claude"]) == "300")
check("walk returns empty when no claude on the path (no infinite loop at root)",
      walk_claude("100", ["100 200 bash","200 1 sh"]) == "")
check("walk honours HERD_CLAUDE_NAME (node-based install anchor)",
      walk_claude("100", ["100 200 bash","200 300 sh","300 400 node"], want="node") == "300")

# ── 47. bash and python must extract the SAME statement ───────────────────
# This is what makes "writes.sql is canonical" true for BOTH runtimes. Without
# it, herd.db.load_statements() and the hooks' stmt() are two parsers of one
# file that agree only by luck — and every check above tests only the python one.
with guard("47 bash stmt() and python load_statements() agree"):
    mismatch = [n for n in W if norm(bash_stmt(n)) != norm(W[n])]
    check("47 bash stmt() and python load_statements() agree", not mismatch,
          f"drifted: {mismatch}" if mismatch else f"all {len(W)} statements identical in both runtimes")

# ── 48/49. session_start: adopt vs insert ─────────────────────────────────
SOCK = "unix:/tmp/kitty-20035"
with guard("48 session_start adopts the reconciled row via (socket,window_id)"):
    c = fresh()
    pk = c.execute("INSERT INTO sessions(cwd,status,status_source,started_at,updated_at)"
                   " VALUES('/code/herd','unknown','reconcile',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
              "window_id,source,verified_at) VALUES(?,'api',?,?,5,'spawn',?)",(pk,T0,SOCK,T0))
    c.close()
    hook("session_start.sh", {"session_id":"uuid-A","cwd":"/code/herd","model":"claude-opus-4-8",
                              "transcript_path":"/t.jsonl","source":"startup",
                              "hook_event_name":"SessionStart"},
         {"KITTY_WINDOW_ID":"5","KITTY_LISTEN_ON":SOCK})
    c = sqlite3.connect(DBPATH); c.row_factory = sqlite3.Row
    rows = c.execute("SELECT id,session_id,status,model FROM sessions").fetchall()
    # job_name is the discriminator: W2b would create a BRAND NEW session row
    # with no herd_sessions attached. Only a true adopt leaves the spawn-time
    # job still bound to the row Claude's UUID landed on. Asserting on rowid
    # alone silently passes when the fallback fires and happens to reuse id 1.
    job = c.execute("SELECT h.job_name FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk"
                    " WHERE s.session_id='uuid-A'").fetchone()
    check("48 session_start adopts the reconciled row via (socket,window_id)",
          len(rows)==1 and rows[0]["id"]==pk and rows[0]["session_id"]=="uuid-A"
          and rows[0]["status"]=="working" and job is not None and job["job_name"]=="api",
          f"adopted pk={pk}, job still attached={job and job['job_name']}, {len(rows)} row(s) total")
    c.close()

with guard("49 session_start falls back to W2b outside kitty"):
    c = fresh(); c.close()
    hook("session_start.sh", {"session_id":"uuid-B","cwd":"/x","model":"claude-opus-4-8",
                              "transcript_path":"/t.jsonl","source":"startup",
                              "hook_event_name":"SessionStart"},
         {"KITTY_WINDOW_ID":"", "KITTY_LISTEN_ON":""})
    c = sqlite3.connect(DBPATH)
    n = c.execute("SELECT COUNT(*) FROM sessions WHERE session_id='uuid-B' AND status='working'").fetchone()[0]
    check("49 session_start falls back to W2b outside kitty", n==1,
          "hooks wired but herd never saw the window")
    c.close()

# ── 50. THE HOT PATH: last_event_at must advance, and throttle must not lie ──
with guard("50 post_tool_use advances last_event_at"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,"
              "started_at,updated_at) VALUES('s1','/a','working',?,'tool',?,?)",(T0,T0,T0))
    c.close()
    hook("post_tool_use.sh", {"session_id":"s1","tool_name":"Bash","tool_input":{},
                              "tool_response":"ok","hook_event_name":"PostToolUse"},
         {"HERD_TOOL_THROTTLE":"0"})
    c = sqlite3.connect(DBPATH)
    le = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    check("50 post_tool_use advances last_event_at", le != T0,
          f"a busy session must never read as silent; last_event_at={le}")
    c.close()

# NOTE the session_id differs from check 50's. The throttle state is a tmpfs
# file keyed by session_id, so reusing 's1' here would inherit check 50's
# just-written timestamp and suppress all five calls — the check would fail
# while the hook behaved perfectly.
with guard("50b throttle suppresses a burst but keeps the first write"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,"
              "started_at,updated_at) VALUES('s2','/a','working',?,'tool',?,?)",(T0,T0,T0))
    c.close()
    for _ in range(5):
        hook("post_tool_use.sh", {"session_id":"s2","tool_name":"Bash","tool_input":{},
                                  "tool_response":"ok","hook_event_name":"PostToolUse"},
             {"HERD_TOOL_THROTTLE":"60"})
    c = sqlite3.connect(DBPATH)
    ev = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    le = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s2'").fetchone()[0]
    check("50b throttle suppresses a burst but keeps the first write",
          ev == 1 and le != T0,
          f"5 tool calls -> {ev} write(s), last_event_at moved={le != T0}")
    c.close()

# ── 51. Stop — the 'waiting' signal klawde has no hook for ────────────────
with guard("51 stop sets waiting and re-arms attention"):
    c = fresh()
    pk = c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,"
                   "started_at,updated_at) VALUES('s1','/a','working',?,'tool',?,?)",(T0,T0,T0)).lastrowid
    c.execute("INSERT INTO herd_attention(session_pk,attention_at,paged_at,paged_level,ack_at)"
              " VALUES(?,?,?,2,?)",(pk,T0,T0,T0))
    c.close()
    hook("stop.sh", {"session_id":"s1","stop_hook_active":False,
                     "last_assistant_message":"done","hook_event_name":"Stop"})
    c = sqlite3.connect(DBPATH); c.row_factory = sqlite3.Row
    r = c.execute("SELECT status,last_event_type FROM sessions WHERE session_id='s1'").fetchone()
    att = c.execute("SELECT COUNT(*) FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()[0]
    check("51 stop sets waiting and re-arms attention",
          r["status"]=="waiting" and r["last_event_type"]=="stop" and att==0,
          f"status={r['status']} etype={r['last_event_type']} attention_rows={att}")
    c.close()

# ── 52. Notification filters to permission_prompt only ────────────────────
with guard("52 notification ignores idle_prompt, honours permission_prompt"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
              " VALUES('s1','/a','working',?,?)",(T0,T0))
    c.close()
    hook("notification.sh", {"session_id":"s1","notification_type":"idle_prompt",
                             "message":"Claude is waiting","hook_event_name":"Notification"})
    c = sqlite3.connect(DBPATH)
    after_idle = c.execute("SELECT status FROM sessions WHERE session_id='s1'").fetchone()[0]
    c.close()
    hook("notification.sh", {"session_id":"s1","notification_type":"permission_prompt",
                             "message":"allow?","hook_event_name":"Notification"})
    c = sqlite3.connect(DBPATH)
    after_perm = c.execute("SELECT status FROM sessions WHERE session_id='s1'").fetchone()[0]
    check("52 notification ignores idle_prompt, honours permission_prompt",
          after_idle=="working" and after_perm=="needs_approval",
          f"idle->{after_idle}, permission->{after_perm}; Stop owns 'waiting', not idle_prompt")
    c.close()

# ── 53. SessionEnd is the hook-driven death, and frees the handles ────────
# No trigger, no live flag: setting stopped_at is what makes R_job_live and the
# window JOIN see the session as dead. The slot frees automatically.
with guard("53 session_end stops the session and frees job+window"):
    c = fresh()
    pk = c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
                   " VALUES('s1','/a','working',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
              "window_id,source,verified_at) VALUES(?,'api',?,?,5,'spawn',?)",(pk,T0,SOCK,T0))
    c.close()
    hook("session_end.sh", {"session_id":"s1","reason":"prompt_input_exit",
                            "hook_event_name":"SessionEnd"})
    c = sqlite3.connect(DBPATH); c.row_factory = sqlite3.Row
    r = c.execute("SELECT status,status_source,stopped_at FROM sessions WHERE session_id='s1'").fetchone()
    job_free = c.execute(W["R_job_live"], {"job":"api"}).fetchone() is None
    win_free = not c.execute("SELECT 1 FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk "
                             "WHERE h.window_id=5 AND s.stopped_at IS NULL").fetchone()
    check("53 session_end stops the session and frees job+window",
          r["status"]=="stopped" and r["stopped_at"] is not None
          and r["status_source"]=="hook" and job_free and win_free,
          f"status_source={r['status_source']} (hook KNOWS; reconcile infers 'pid'); "
          f"job_free={job_free} win_free={win_free}")
    c.close()

# ── 54. bind() must refuse an unbound param, never bind NULL silently ─────
# sqlite3's own `.param set` fails exactly this way: mis-tokenize the value and
# the parameter is silently NULL. That is how you lose data quietly.
with guard("54 bind() refuses unbound params"):
    r = subprocess.run(["bash","-c",
        f'. "{HOOKS}/common.sh"; bind "UPDATE sessions SET cwd = :cwd WHERE session_id = :session_id;"'],
        capture_output=True, text=True, env=dict(os.environ, HERD_P_cwd="/a"))
    check("54 bind() refuses unbound params", r.returncode != 0 and ":session_id" in r.stderr,
          f"rc={r.returncode}, stderr mentions the missing param")

# ── 54b. the single-pass property, through the real bind() ───────────────
with guard("54b bind() does not rescan substituted values"):
    r = subprocess.run(["bash","-c",
        f'. "{HOOKS}/common.sh"; bind "UPDATE sessions SET cwd = :cwd, updated_at = :now;"'],
        capture_output=True, text=True,
        env=dict(os.environ, HERD_P_cwd="/tmp/:now/x", HERD_P_now="T1"))
    check("54b bind() does not rescan substituted values",
          "'/tmp/:now/x'" in r.stdout, f"a cwd containing ':now' survives: {r.stdout.strip()}")

# ── 55. stop.sh re-arms through writes.sql (W6d_rearm_sid), end to end ────
with guard("55 stop re-arm goes through the canonical statement"):
    c = fresh()
    pk = c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
                   " VALUES('s9','/a','working',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO herd_attention(session_pk,attention_at,paged_level) VALUES(?,?,3)",(pk,T0))
    c.close()
    hook("stop.sh", {"session_id":"s9","stop_hook_active":False,"hook_event_name":"Stop"})
    c = sqlite3.connect(DBPATH)
    att = c.execute("SELECT COUNT(*) FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()[0]
    check("55 stop re-arm goes through the canonical statement", att==0,
          "attention row cleared by W6d_rearm_sid, not by inlined SQL")
    c.close()

# ── 56. NO hook may inline DML. Every write goes through run()/writes.sql, or
# the check-47 drift guard is blind to it and bind()'s quoting is bypassed. The
# one exception is common.sh's db()/run() plumbing, which is the sanctioned path.
with guard("56 no hook script inlines INSERT/UPDATE/DELETE"):
    offenders = []
    for shf in sorted(HOOKS.glob("*.sh")):
        if shf.name == "common.sh":
            continue   # the db()/run() wrapper IS the sanctioned SQL path
        for i, line in enumerate(shf.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if re.search(r"\b(INSERT|UPDATE|DELETE)\s", code, re.I):
                offenders.append(f"{shf.name}:{i}")
    check("56 no hook script inlines INSERT/UPDATE/DELETE", not offenders,
          f"inlined DML at {offenders}" if offenders
          else "every write routes through run() -> writes.sql")

# ── 56b. EVERY hook must be EXECUTABLE ────────────────────────────────────
# settings.json hook commands and the statusline wrapper invoke these scripts
# DIRECTLY (`"/path/hook.sh"`), which needs the exec bit. Every test here runs
# them as `bash <path>`, which does NOT — so a non-executable hook is a silent
# no-op in production while the whole suite stays green. That shipped: the
# statusline was created via Write after the chmod pass, lost the bit, and the
# wrapper died with "Permission denied" (exit 126) leaving a blank statusline.
with guard("56b every hook script is executable"):
    not_exec = [p.name for p in sorted(HOOKS.glob("*.sh")) if not os.access(p, os.X_OK)]
    check("56b every hook script is executable", not not_exec,
          f"NOT executable (would be a silent no-op when invoked directly): {not_exec}"
          if not_exec else f"all {len(list(HOOKS.glob('*.sh')))} hooks have the exec bit")

# ── 57. run_tx is ATOMIC: a mid-transaction failure leaves NOTHING written ──
# This is the property -bail buys. Without it the sqlite3 CLI runs UPDATE, skips
# the failing INSERT, and COMMITs the UPDATE anyway — a half-write. The check
# forces a failure by pointing the second statement's FK at a missing row, and
# asserts the first statement's effect did not survive.
with guard("57 run_tx rolls back the whole transaction on a mid-tx failure"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,started_at,updated_at)"
              " VALUES('s1','/a','working',?,?,?)",(T0,T0,T0))
    c.close()
    # W4_event (updates s1, succeeds) + a second statement that violates the FK.
    r = subprocess.run(["bash","-c",
        '. "$1/common.sh"; '
        'export HERD_P_session_id=s1 HERD_P_now=T9 HERD_P_status=working '
        'HERD_P_etype=tool HERD_P_raw=""; '
        # hand a bad statement to run_tx by shadowing stmt for one name:
        'run_tx W4_event BOGUS_FK', "_", str(HOOKS.parent)],
        capture_output=True, text=True,
        env=dict(os.environ, HERD_DB=DBPATH, HERD_RUNTIME=RUNTIME,
                 HERD_ERRLOG=f"{RUNTIME}/err.log"))
    # BOGUS_FK doesn't exist -> run_tx aborts before executing anything, so
    # last_event_at must still be T0. (Belt: even if it HAD run W4_event alone,
    # the missing-statement guard returns before the db call.)
    c = sqlite3.connect(DBPATH)
    le = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    check("57 run_tx rolls back the whole transaction on a mid-tx failure",
          le == T0, f"an unknown statement must abort with NOTHING written; last_event_at={le}")
    c.close()

# ── 57b. the real atomicity path: a runtime FK error inside the tx ────────
with guard("57b -bail rolls back a committed-prefix on runtime error"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,started_at,updated_at)"
              " VALUES('s1','/a','working',?,?,?)",(T0,T0,T0))
    c.close()
    # Build a two-statement tx by hand through db(): a valid UPDATE then an
    # INSERT with a bad FK. Mirrors exactly what run_tx emits.
    tx = ("BEGIN IMMEDIATE;\n"
          "UPDATE sessions SET last_event_at='T9' WHERE session_id='s1';\n"
          "INSERT INTO events(session_pk,event_type,source,timestamp) "
          "VALUES(99999,'x','hook','T9');\n"
          "COMMIT;\n")
    subprocess.run(["bash","-c", f'. "{HOOKS}/common.sh"; db', "_"],
                   input=tx, capture_output=True, text=True,
                   env=dict(os.environ, HERD_DB=DBPATH, HERD_RUNTIME=RUNTIME,
                            HERD_ERRLOG=f"{RUNTIME}/err.log"))
    c = sqlite3.connect(DBPATH)
    le = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    ev = c.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    check("57b -bail rolls back a committed-prefix on runtime error",
          le == T0 and ev == 0,
          f"the UPDATE must NOT survive the failed INSERT; last_event_at={le}, events={ev}")
    c.close()

# ═══════════════════════════════════════════════════════════════════════════
# O. STATUSLINE — the sink + render + fingerprint cache + Path C, end to end
# ═══════════════════════════════════════════════════════════════════════════
print("\n\033[1m═══ O. STATUSLINE (58-63) ═══\033[0m")

def statusline(payload, env=None):
    return hook("statusline.sh", payload, env)

SL_PAY = {"session_id":"s1","model":{"id":"claude-opus-4-8"},"session_name":"sess",
          "cwd":"/code/herd","context_window":{"used_percentage":42.7},
          "cost":{"total_cost_usd":1.50},
          "rate_limits":{"five_hour":{"used_percentage":73.5,"resets_at":1784172774},
                         "seven_day":{"used_percentage":12,"resets_at":1784259174}}}

# ── 58. W5 captures rate limits; resets_at epoch -> ISO; absent keeps prior ──
with guard("58 W5 captures rate limits with epoch->ISO conversion"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
              " VALUES('s1','/a','working',?,?)",(T0,T0))
    c.execute(W["W5_statusline"], {"model":None,"sname":None,"ctx":None,"cost":None,"branch":None,
                                   "rl5":"73.5","rl5reset":"1784172774","rl7":"12","rl7reset":"1784259174",
                                   "now":T1,"session_id":"s1"})
    r = c.execute("SELECT rate_limit_5h_percent,rate_limit_5h_resets_at FROM sessions WHERE session_id='s1'").fetchone()
    got_iso = r["rate_limit_5h_resets_at"]
    # a later tick with NO rate limits must NOT wipe them (COALESCE keeps prior)
    c.execute(W["W5_statusline"], {"model":None,"sname":None,"ctx":None,"cost":None,"branch":None,
                                   "rl5":None,"rl5reset":None,"rl7":None,"rl7reset":None,
                                   "now":T2,"session_id":"s1"})
    kept = c.execute("SELECT rate_limit_5h_percent FROM sessions WHERE session_id='s1'").fetchone()[0]
    check("58 W5 captures rate limits with epoch->ISO conversion",
          r["rate_limit_5h_percent"]==73.5 and got_iso=="2026-07-16T03:32:54Z" and kept==73.5,
          f"5h%={r['rate_limit_5h_percent']} resets={got_iso} (epoch converted); absent-tick kept={kept}")

# ── 59. statusline.sh sinks metrics into an adopted row ───────────────────
with guard("59 statusline sinks metrics + renders the herd line"):
    c = fresh()
    pk = c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
                   " VALUES('s1','/code/herd','working',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
              "window_id,source,verified_at) VALUES(?,'api-refactor',?,?,5,'spawn',?)",(pk,T0,SOCK,T0))
    c.close()
    r = statusline(SL_PAY)
    c = sqlite3.connect(DBPATH); c.row_factory = sqlite3.Row
    row = c.execute("SELECT context_percent,total_cost_usd,rate_limit_5h_percent,rate_limit_5h_resets_at,model"
                    " FROM sessions WHERE session_id='s1'").fetchone()
    check("59 statusline sinks metrics + renders the herd line",
          row["context_percent"]==42 and isinstance(row["context_percent"],int)
          and row["total_cost_usd"]==1.5
          and row["rate_limit_5h_percent"]==73.5 and row["rate_limit_5h_resets_at"]=="2026-07-16T03:32:54Z"
          and "api-refactor" in r.stdout,
          f"row={dict(row)} render={r.stdout.strip()!r}")
    c.close()

# ── 60. fingerprint HIT: a repeat identical tick does ZERO DB writes ──────
with guard("60 identical tick is a fingerprint hit (no DB write)"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
              " VALUES('s1','/code/herd','working',?,?)",(T0,T0))
    c.close()
    statusline(SL_PAY)                              # tick 1: sink
    c = sqlite3.connect(DBPATH)
    before = c.execute("SELECT updated_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    c.close()
    r2 = statusline(SL_PAY)                          # tick 2: identical -> cache hit
    c = sqlite3.connect(DBPATH)
    after = c.execute("SELECT updated_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    check("60 identical tick is a fingerprint hit (no DB write)",
          before == after and r2.stdout.strip() != "",
          f"updated_at before={before} after={after} (equal => no write); render still emitted")
    c.close()

# ── 61. Path C: statusline adopts a reconciled-but-unadopted row ──────────
with guard("61 Path C: statusline adopts a reconciled session from its env"):
    c = fresh()
    pk = c.execute("INSERT INTO sessions(pid,cwd,status,status_source,started_at,updated_at)"
                   " VALUES(4242,'/code/herd','unknown','reconcile',?,?)",(T0,T0)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,5,'hook',?)",(pk,T0,SOCK,T0))
    c.close()
    statusline({"session_id":"uuid-x","model":{"id":"opus"},"cwd":"/code/herd",
                "context_window":{"used_percentage":30},"cost":{"total_cost_usd":0.10}},
               {"KITTY_WINDOW_ID":"5","KITTY_LISTEN_ON":SOCK})
    c = sqlite3.connect(DBPATH); c.row_factory = sqlite3.Row
    row = c.execute("SELECT session_id,context_percent FROM sessions WHERE id=?",(pk,)).fetchone()
    check("61 Path C: statusline adopts a reconciled session from its env",
          row["session_id"]=="uuid-x" and row["context_percent"]==30,
          f"adopted={row['session_id']} metrics_filled={row['context_percent']}")
    c.close()

# ── 62. statusline never resurrects a stopped session (full-script path) ──
with guard("62 statusline tick on a stopped session is a no-op"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at,stopped_at)"
              " VALUES('dead','/x','stopped',?,?,?)",(T0,T0,T1))
    c.close()
    statusline({"session_id":"dead","model":{"id":"opus"},"cwd":"/x",
                "context_window":{"used_percentage":99},"cost":{"total_cost_usd":5}})
    c = sqlite3.connect(DBPATH); c.row_factory = sqlite3.Row
    row = c.execute("SELECT context_percent,stopped_at FROM sessions WHERE session_id='dead'").fetchone()
    check("62 statusline tick on a stopped session is a no-op",
          row["context_percent"] is None and row["stopped_at"]==T1,
          f"ctx={row['context_percent']} (must be None) stopped_at={row['stopped_at']} (must persist)")
    c.close()

# ── 63. never touches last_event_* (full-script path, not just the SQL) ────
with guard("63 statusline never moves last_event_at"):
    c = fresh()
    c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,"
              "started_at,updated_at) VALUES('s1','/code/herd','working',?,'tool',?,?)",(T0,T0,T0))
    c.close()
    statusline(SL_PAY)
    c = sqlite3.connect(DBPATH)
    le = c.execute("SELECT last_event_at FROM sessions WHERE session_id='s1'").fetchone()[0]
    check("63 statusline never moves last_event_at", le == T0,
          f"last_event_at={le} must stay at the hook-set value; statusline owns only updated_at")
    c.close()

shutil.rmtree(RUNTIME, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════
# P. INSTALLER — klawde -> herd settings.json surgery (pure functions)
# ═══════════════════════════════════════════════════════════════════════════
print("\n\033[1m═══ P. INSTALLER (64-66) ═══\033[0m")
from herd import install as _install

# a synthetic klawde-shaped config: the HTTP PreToolUse hook and the cdh block
# must survive; the klawde commands must be replaced.
KLAWDE_CFG = {"hooks": {
    "PreToolUse":  [{"matcher":".*","hooks":[{"type":"http","url":"http://localhost:8765/x","timeout":5}]}],
    "SessionStart":[{"hooks":[{"type":"command","command":"/h/.klawde/session_start.sh"},
                              {"type":"command","command":"/h/.klawde/kitty_start.sh","async":True}]}],
    "SessionEnd":  [{"hooks":[{"type":"command","command":"/h/.klawde/session_end.sh","async":True}]}],
    "Notification":[{"hooks":[{"type":"command","command":"/h/.klawde/notification.sh","async":True}]}],
    "PostToolUse": [{"hooks":[{"type":"command","command":"/h/.klawde/post_tool_use.sh","async":True}]},
                    {"hooks":[{"type":"command","command":"cdh-claude-hook postToolUse","async":True}]}],
}}

with guard("64 installer preserves cdh + PreToolUse, replaces klawde, fixes async"):
    out = _install.rewire_settings(KLAWDE_CFG)
    def _cmds(d, e): return [h.get("command","") for b in d["hooks"].get(e,[]) for h in b["hooks"]]
    def _async(d, e): return [h.get("async",False) for b in d["hooks"].get(e,[]) for h in b["hooks"]]
    ok = (
        any("cdh-claude-hook" in c for c in _cmds(out,"PostToolUse"))            # cdh preserved
        and any(h.get("type")=="http" for b in out["hooks"]["PreToolUse"] for h in b["hooks"])  # HTTP preserved
        and not any("/.klawde/" in c for e in out["hooks"] for c in _cmds(out,e))  # klawde gone
        and _async(out,"SessionEnd")==[False]                                    # async bug fixed
        and _async(out,"SessionStart")==[False]                                  # blocking
        and any("stop.sh" in c for c in _cmds(out,"Stop"))                       # Stop added
        and _async(out,"Stop")==[True]
        and not any("kitty_start" in c for e in out["hooks"] for c in _cmds(out,e))  # merged away
        and len(_cmds(out,"SessionStart"))==1
    )
    check("64 installer preserves cdh + PreToolUse, replaces klawde, fixes async", ok,
          f"PostToolUse={_cmds(out,'PostToolUse')} SessionEnd_async={_async(out,'SessionEnd')}")

with guard("65 installer is idempotent (re-running changes nothing)"):
    once = _install.rewire_settings(KLAWDE_CFG)
    twice = _install.rewire_settings(once)
    check("65 installer is idempotent (re-running changes nothing)", once == twice,
          "a second rewire must not duplicate herd blocks")

with guard("66 wrapper swap: klawde statusline -> herd, idempotent"):
    w0 = 'CAV=$(bash caveman)\nprintf "%s ┃ " "$CAV"\n"$HOME/.klawde/statusline.sh"\n'
    w1, rep = _install.rewire_wrapper(w0)
    w2, _ = _install.rewire_wrapper(w1)
    check("66 wrapper swap: klawde statusline -> herd, idempotent",
          rep and ".klawde/statusline.sh" not in w1 and _install.STATUSLINE in w1 and w1 == w2,
          "caveman segment kept, klawde statusline replaced, second run is a no-op")

with guard("66b daemon service unit is well-formed and source-based"):
    u = _install.service_unit_text()
    ok = ("-m herd.daemon" in u
          and f"Environment=PYTHONPATH={_install.PKG_SRC}" in u   # runs from the source tree
          and f"Environment=HERD_DB={_install.DB}" in u
          and "Restart=on-failure" in u
          and "WantedBy=default.target" in u                      # starts on login
          and _install.PKG_SRC.name == "src")
    check("66b daemon service unit is well-formed (PYTHONPATH, HERD_DB, ExecStart, autostart)",
          ok, u.replace(chr(10), " ⏎ "))

print("\n\033[1m═══ Q. ID DURABILITY + W2b/RECONCILE SEAM (67-71) ═══\033[0m")

# 67. AUTOINCREMENT: a surrogate id is NEVER reused after a delete. Without it a
# recycled id could reattach to a stale :pk held mid-tick by the TUI/pager once a
# future prune adds a real DELETE FROM sessions. Plain rowid recycles the highest
# id on the next insert; AUTOINCREMENT (sqlite_sequence-backed) never does.
c = fresh()
i1 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
c.execute("DELETE FROM sessions WHERE id=?",(i1,))
i2 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/b',?,?)",(T2,T2)).lastrowid
_seq = c.execute("SELECT seq FROM sqlite_sequence WHERE name='sessions'").fetchone()
check("67 sessions.id is AUTOINCREMENT — a deleted id is never recycled",
      i2 > i1 and _seq is not None,
      f"deleted id={i1}, next id={i2} (must be > {i1}); "
      f"sqlite_sequence(sessions)={_seq['seq'] if _seq else None}")

# 68. Resume sets the FRESH walked pid. The SessionStart hook re-walks to claude on
# every (re)start, so revive overwrites the stale pid with the resumed process's own
# — not NULL (the da36a8f stopgap, now reverted), and not the dead old pid.
c = fresh()
c.execute("INSERT INTO sessions(session_id,pid,cwd,status,started_at,updated_at)"
          " VALUES('u9',111,'/code/herd','working',?,?)",(T0,T0))
c.execute(W["W4_end"], {"session_id":"u9","now":T1})               # die (SessionEnd)
c.execute(W["W2b_insert"], {"session_id":"u9","cwd":"/code/herd","model":"opus",
                            "transcript":"/t.jsonl","now":T2,"pid":999})  # claude --resume
_pid = c.execute("SELECT pid FROM sessions WHERE session_id='u9'").fetchone()["pid"]
check("68 W2b revive sets the fresh walked pid (not the stale one, not NULL)",
      _pid == 999,
      f"pid after revive={_pid} (must be 999 — the resumed proc's own pid, replacing 111)")

# 68b. Both SessionStart writers stamp the walked pid onto the tier-1 row.
c = fresh()
n = c.execute("INSERT INTO sessions(cwd,status,status_source,started_at,updated_at) "
              "VALUES('/code/app','unknown','reconcile',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,"
          "window_id,herd_var,source,verified_at) VALUES(?,?,?,?,?,?,'spawn',?)",
          (n,"api",T0,"unix:/tmp/kitty-1",7,"api",T0))
c.execute(W["W2_adopt"], {"session_id":"a1","cwd":"/code/app","model":"opus",
                          "transcript":"/t.jsonl","now":T1,
                          "socket":"unix:/tmp/kitty-1","win":7,"pid":555})
check("68b W2_adopt stamps the walked pid",
      c.execute("SELECT pid FROM sessions WHERE session_id='a1'").fetchone()["pid"] == 555)
c = fresh()
c.execute(W["W2b_insert"], {"session_id":"b1","cwd":"/x","model":"opus",
                            "transcript":"/t.jsonl","now":T0,"pid":555})
check("68b W2b_insert stamps the walked pid on a fresh insert",
      c.execute("SELECT pid FROM sessions WHERE session_id='b1'").fetchone()["pid"] == 555)

# 68c. W2c_pid_claim reaps a stale live holder of the pid, never the claiming
# session itself. The claim runs at SessionStart before pid is stamped.
c = fresh()
a = c.execute("INSERT INTO sessions(session_id,pid,cwd,status,started_at,updated_at)"
              " VALUES('old',777,'/a','working',?,?)",(T0,T0)).lastrowid
c.execute(W["W2c_pid_claim"], {"pid":777,"session_id":"new","now":T1})
ra = c.execute("SELECT status,status_source,stopped_at FROM sessions WHERE id=?",(a,)).fetchone()
check("68c W2c_pid_claim reaps the stale live holder of the pid",
      ra["stopped_at"] == T1 and ra["status"] == "stopped" and ra["status_source"] == "pid",
      f"stale holder: {dict(ra)}")
# self-exclusion: a live row whose session_id IS the claimant must survive.
c = fresh()
s = c.execute("INSERT INTO sessions(session_id,pid,cwd,status,started_at,updated_at)"
              " VALUES('me',888,'/a','working',?,?)",(T0,T0)).lastrowid
c.execute(W["W2c_pid_claim"], {"pid":888,"session_id":"me","now":T1})
check("68c W2c_pid_claim never reaps the claiming session itself",
      c.execute("SELECT stopped_at FROM sessions WHERE id=?",(s,)).fetchone()["stopped_at"] is None)

# 68d. THE MONEY CHECK: claim + insert is collision-safe. A stale-live row holds
# pid 777; the new session claims it, then W2b_insert stamps 777 — the UNIQUE index
# idx_sessions_pid_live does NOT reject the write, because the stale holder is gone.
c = fresh()
old = c.execute("INSERT INTO sessions(session_id,pid,cwd,status,started_at,updated_at)"
                " VALUES('ghost',777,'/a','working',?,?)",(T0,T0)).lastrowid
with guard("68d claim+insert is collision-safe under idx_sessions_pid_live"):
    c.execute(W["W2c_pid_claim"], {"pid":777,"session_id":"fresh","now":T1})
    c.execute(W["W2b_insert"], {"session_id":"fresh","cwd":"/b","model":"opus",
                                "transcript":"/t.jsonl","now":T1,"pid":777})  # would raise pre-steal
    live = c.execute("SELECT id FROM sessions WHERE pid=777 AND stopped_at IS NULL").fetchall()
    check("68d claim+insert is collision-safe under idx_sessions_pid_live",
          len(live) == 1
          and c.execute("SELECT session_id FROM sessions WHERE id=?",(live[0]['id'],)).fetchone()[0] == "fresh"
          and c.execute("SELECT stopped_at FROM sessions WHERE id=?",(old,)).fetchone()["stopped_at"] == T1,
          "the ghost is reaped and the new session owns pid 777, one live row")

# 68e. claim is a no-op when the walk failed (pid NULL): reaps nothing.
c = fresh()
k = c.execute("INSERT INTO sessions(session_id,pid,cwd,status,started_at,updated_at)"
              " VALUES('keep',321,'/a','working',?,?)",(T0,T0)).lastrowid
c.execute(W["W2c_pid_claim"], {"pid":"","session_id":"whoever","now":T1})   # empty -> NULL
check("68e W2c_pid_claim with unknown pid (NULL) reaps nothing",
      c.execute("SELECT stopped_at FROM sessions WHERE id=?",(k,)).fetchone()["stopped_at"] is None)

# 69. Every value the herd_sessions.source CHECK ALLOWS must be WRITTEN by some
# statement (and every written value must be allowed). 'hook' once sat in the CHECK
# with no writer — a reserved plug; 'reconcile' is the inverse hazard now that
# kitty-discovery is deleted. Derive the allowed set from the schema so the two
# can't drift, and assert it equals the set of source literals the writers use.
_allowed = set(re.search(r"source\s+TEXT[^,]*CHECK\s*\(\s*source\s+IN\s*\(([^)]*)\)",
                         HERD, re.I).group(1).replace("'", "").replace(" ", "").split(","))
_src_writers = "\n".join(s for n, s in W.items()
                         if re.search(r"INSERT\s+INTO\s+herd_sessions", s, re.I))
_src_code = "\n".join(l.split("--")[0] for l in _src_writers.splitlines())
_written = {v for v in _allowed | {"reconcile"} if f"'{v}'" in _src_code}
check("69 herd_sessions.source: allowed set == written set (no orphan either way)",
      _allowed == _written,
      f"allowed={sorted(_allowed)} written={sorted(_written)}")

# 70. W2b_placement records the hook's window as source='hook', with NULL job_name
# (herd didn't name it) — the plug for the seam. pk resolved off session_id, so it
# lands on the row W2b_insert just wrote in the same transaction.
c = fresh()
c.execute(W["W2b_insert"], {"session_id":"u1","cwd":"/code/herd","model":"opus",
                            "transcript":"/t.jsonl","now":T0,"pid":None})
c.execute(W["W2b_placement"], {"session_id":"u1","socket":SOCK,"win":8,"now":T0})
_pl = c.execute("SELECT h.source,h.kitty_socket,h.window_id,h.job_name "
                "FROM herd_sessions h JOIN sessions s ON s.id=h.session_pk "
                "WHERE s.session_id='u1'").fetchone()
check("70 W2b_placement records the hook's window (source=hook, no job)",
      _pl is not None and _pl["source"]=="hook" and _pl["kitty_socket"]==SOCK
      and _pl["window_id"]==8 and _pl["job_name"] is None,
      f"placement={dict(_pl) if _pl else None}")

# (Check 71 removed: it guarded W3a_discover's duplicate-row hazard + W3c_pid fill,
# both deleted with kitty-reconcile. No row-inserter remains to duplicate, and pid
# is hook-written. W2b_placement's surviving value — the jump target — is check 70.)

print("\n\033[1m═══ R. LIVENESS REAPER (daemon.py) ═══\033[0m")
from herd.daemon import reap_once, boot_sweep, _parse_proc_table, _dead, needs_attention, attention_tick

def _live_sess(conn, sid, pid, started=T0, le=T0):
    return conn.execute(
        "INSERT INTO sessions(session_id,pid,cwd,status,status_source,"
        "last_event_at,last_event_type,started_at,updated_at) "
        "VALUES(?,?,'/a','working','hook',?,'tool',?,?)",
        (sid, pid, le, started, started)).lastrowid

# One fixture, four liveness verdicts: absent / zombie / recycled-to-nonclaude / live.
c = fresh()
a = _live_sess(c, "absent", 5000)
z = _live_sess(c, "zombie", 5001)
r = _live_sess(c, "recycled", 5002)
ok = _live_sess(c, "alive", 5003)
procs = {5001: ("Z", "claude"), 5002: ("S", "bash"), 5003: ("S", "claude")}  # 5000 absent
n = reap_once(c, procs, T2)
def _stopped(pk): return c.execute("SELECT stopped_at FROM sessions WHERE id=?",(pk,)).fetchone()[0]
check("reaper reaps absent/zombie/recycled, keeps the live claude",
      n == 3 and _stopped(a) and _stopped(z) and _stopped(r) and _stopped(ok) is None,
      f"reaped={n} absent={_stopped(a)!r} zombie={_stopped(z)!r} recycled={_stopped(r)!r} alive={_stopped(ok)!r}")

# pid-NULL rows are unjudgeable — never reaped (clean death comes via SessionEnd).
c = fresh()
k = c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at) "
              "VALUES('nopid','/a','working',?,?)",(T0,T0)).lastrowid
check("reaper skips pid-NULL rows",
      reap_once(c, {}, T2) == 0 and _stopped(k) is None)

# W3d provenance + the two-clocks invariant: a reap sets stopped/pid, never a clock.
c = fresh()
d = _live_sess(c, "dead", 5010, started=T0, le=T0)
reap_once(c, {}, T2)
row = c.execute("SELECT status,status_source,stopped_at,last_event_at FROM sessions WHERE id=?",(d,)).fetchone()
check("reaper marks status_source='pid' and never moves last_event_at",
      row["status"]=="stopped" and row["status_source"]=="pid"
      and row["stopped_at"]==T2 and row["last_event_at"]==T0,
      f"{dict(row)} (last_event_at must stay {T0})")

# reaping is idempotent — a second tick over the same dead pid reaps nothing.
check("reaper is idempotent (already-stopped rows are not re-reaped)",
      reap_once(c, {}, T2) == 0)

# boot_sweep: reap live rows started BEFORE boot, spare those started after.
c = fresh()
old = _live_sess(c, "preboot", 6000, started=T0)
new = _live_sess(c, "postboot", 6001, started=T2)
boot_sweep(c, T2, T1)   # boot at T1
check("boot_sweep reaps pre-boot rows, spares post-boot",
      _stopped(old) and _stopped(new) is None,
      f"preboot={_stopped(old)!r} postboot={_stopped(new)!r}")
check("boot_sweep with unknown boot_time (None) is a no-op",
      (lambda cc: (boot_sweep(cc, T2, None),
                   cc.execute("SELECT stopped_at FROM sessions WHERE id=?",
                              (_live_sess(cc,'x',6002,started=T0),)).fetchone()[0] is None)[-1])(fresh()))

# _parse_proc_table: pid/state/comm, basenamed comm, junk-tolerant.
pp = _parse_proc_table("  100 Ss /usr/bin/claude\n200 Z claude\nbogus line\n300 R\n400 Sl+ node\n")
check("_parse_proc_table parses state+basename(comm), skips junk/short lines",
      pp == {100:("S","claude"), 200:("Z","claude"), 400:("S","node")},
      str(pp))

# _dead unit: the four verdicts in isolation, incl. HERD_CLAUDE_NAME default.
check("_dead: absent->True, zombie->True, non-claude->True, live claude->False",
      _dead(1,{}) and _dead(1,{1:("Z","claude")}) and _dead(1,{1:("S","bash")})
      and not _dead(1,{1:("S","claude")}))

print("\n\033[1m═══ S. ATTENTION TICK (daemon.py) ═══\033[0m")
# The silence rule, derived from status + (now - last_event_at). Actuator deferred.
T0_20 = "2026-07-15T10:00:20.000Z"   # T0 + 20s
T0_10 = "2026-07-15T10:00:10.000Z"   # T0 + 10s
T0_240 = "2026-07-15T10:04:00.000Z"  # T0 + 240s
check("needs_attention: waiting trips after its grace (>=30s), not before",
      needs_attention("waiting", T0, T1) and not needs_attention("waiting", T0, T0))
check("needs_attention: needs_approval grace is ~15s",
      needs_attention("needs_approval", T0, T0_20) and not needs_attention("needs_approval", T0, T0_10))
check("needs_attention: working is 'stuck' only after ~5min",
      needs_attention("working", T0, T1) and not needs_attention("working", T0, T0_240))
check("needs_attention: stopped/unknown never trip; NULL last_event never trips",
      not needs_attention("stopped", T0, T2) and not needs_attention("unknown", T0, T2)
      and not needs_attention("working", None, T2))

def _att(conn, pk):
    r = conn.execute("SELECT attention_at FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()
    return r["attention_at"] if r else None

# arms a session past threshold; leaves a fresh one alone.
c = fresh()
w = c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
              " VALUES('w','/a','waiting',?,'stop',?,?)",(T0,T0,T0)).lastrowid
fresh_s = c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
                    " VALUES('f','/a','waiting',?,'stop',?,?)",(T1,T1,T1)).lastrowid
armed, dis = attention_tick(c, T1)   # w waited 5min (arm), f just started (no)
check("attention_tick arms a session past its silence threshold, not a fresh one",
      armed==1 and dis==0 and _att(c,w)==T1 and _att(c,fresh_s) is None,
      f"armed={armed} disarmed={dis} w={_att(c,w)!r} fresh={_att(c,fresh_s)!r}")

# the edge is not re-stamped on a later tick while the condition persists.
c.execute("UPDATE sessions SET updated_at=? WHERE id=?",(T2,w))
attention_tick(c, T2)   # w still waiting on the SAME last_event_at=T0
check("attention_tick preserves the edge (W6a COALESCE), doesn't re-stamp",
      _att(c,w)==T1, f"edge moved to {_att(c,w)!r} (must stay {T1})")

# disarms when the condition clears (session resumed working, recent activity).
c = fresh()
d = c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
              " VALUES('d','/a','working',?,'tool',?,?)",(T1,T1,T1)).lastrowid
c.execute("INSERT INTO herd_attention(session_pk,attention_at) VALUES(?,?)",(d,T0))  # was armed
armed, dis = attention_tick(c, T1)   # working, active 0s ago -> not page-worthy
check("attention_tick disarms a session whose silence cleared",
      dis==1 and armed==0 and _att(c,d) is None,
      f"armed={armed} disarmed={dis} row={_att(c,d)!r}")

# stopped sessions are outside the tick entirely (never armed).
c = fresh()
s = c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at,stopped_at)"
              " VALUES('s','/a','waiting',?,'stop',?,?,?)",(T0,T0,T0,T1)).lastrowid
armed, _ = attention_tick(c, T2)
check("attention_tick ignores stopped sessions", armed==0 and _att(c,s) is None)

# ── the CORE/HERD layer gate: HERD_ATTENTION ──────────────────────────────────
from herd.daemon import _attention_enabled, run as _daemon_run
import datetime as _dt
def _env(**kw):
    saved = {k: os.environ.get(k) for k in kw}
    os.environ.update({k: v for k, v in kw.items() if v is not None})
    for k, v in kw.items():
        if v is None: os.environ.pop(k, None)
    return saved
def _restore(saved):
    for k, v in saved.items():
        if v is None: os.environ.pop(k, None)
        else: os.environ[k] = v

_s = _env(HERD_ATTENTION=None); check("HERD_ATTENTION default on (herd's full behavior)", _attention_enabled()); _restore(_s)
_s = _env(HERD_ATTENTION="0"); off0 = _attention_enabled(); _restore(_s)
_s = _env(HERD_ATTENTION="off"); offw = _attention_enabled(); _restore(_s)
_s = _env(HERD_ATTENTION="1"); on1 = _attention_enabled(); _restore(_s)
check("HERD_ATTENTION 0/off -> core-only, 1 -> on",
      not off0 and not offw and on1, f"0={off0} off={offw} 1={on1}")

# run() honors the gate end-to-end: a waiting session is armed only with attention on.
# started_at=now (survives boot_sweep), last_event 5min ago (trips the waiting rule).
_now = _dt.datetime.now(_dt.timezone.utc)
_iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%S.000Z")
c = fresh()
c.execute("INSERT INTO sessions(session_id,cwd,status,last_event_at,last_event_type,started_at,updated_at)"
          " VALUES('wf','/a','waiting',?,'stop',?,?)",
          (_iso(_now - _dt.timedelta(seconds=300)), _iso(_now), _iso(_now - _dt.timedelta(seconds=300))))
DBP = os.path.abspath("f.db")
_daemon_run(db_path=DBP, once=True, attend=False)
_off = c.execute("SELECT COUNT(*) FROM herd_attention").fetchone()[0]
_daemon_run(db_path=DBP, once=True, attend=True)
_on = c.execute("SELECT COUNT(*) FROM herd_attention").fetchone()[0]
check("run() gates attention: core-only writes zero herd_attention, herd mode arms",
      _off == 0 and _on == 1, f"attend=False rows={_off} (want 0); attend=True rows={_on} (want 1)")

print("\n\033[1m═══ T. JUMP / FOCUS (kitty/focus.py + cli.py) ═══\033[0m")
from herd.kitty.focus import window_for_pid, flatten_windows, focus_session
from herd import cli as _cli

# pure resolution: the window whose foreground claude carries the pid.
_wins = [{"id": 1, "foreground_processes": [{"pid": 111, "cmdline": ["bash"]}]},
         {"id": 42, "foreground_processes": [{"pid": 5000, "cmdline": ["/opt/claude"]},
                                             {"pid": 5001, "cmdline": ["node"]}]}]
check("window_for_pid finds the window whose fg claude has the pid",
      window_for_pid(_wins, 5000) == 42)
check("window_for_pid ignores a non-claude proc with a matching pid, and misses cleanly",
      window_for_pid(_wins, 5001) is None and window_for_pid(_wins, 999) is None)
check("flatten_windows parses the ls tree, returns None on garbage",
      flatten_windows('[{"tabs":[{"windows":[{"id":9}]}]}]') == [{"id": 9}]
      and flatten_windows("not json") is None)

def _focus_fixture(win_stored=7, pid=5000, attn=T0):
    c = fresh()
    pk = c.execute("INSERT INTO sessions(session_id,pid,cwd,status,started_at,updated_at)"
                   " VALUES('jz',?,'/code/app','working',?,?)",(pid,T0,T0)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,'hook',?)",(pk,SOCK,win_stored,T0))
    if attn: c.execute("INSERT INTO herd_attention(session_pk,attention_at) VALUES(?,?)",(pk,attn))
    return c, pk

# re-derive (stored 7 -> real 42), focus, ack the attention, self-heal window_id.
c, pk = _focus_fixture(win_stored=7)
calls = []
ok, msg = focus_session(c, pk, T1,
                        list_fn=lambda s: [{"id": 42, "foreground_processes":
                                            [{"pid": 5000, "cmdline": ["claude"]}]}],
                        focus_fn=lambda s, w: (calls.append((s, w)) or True))
check("focus_session re-derives by pid, focuses, acks attention, self-heals window_id",
      ok and calls == [(SOCK, 42)]
      and c.execute("SELECT window_id FROM herd_sessions WHERE session_pk=?",(pk,)).fetchone()[0] == 42
      and c.execute("SELECT ack_at FROM herd_attention WHERE session_pk=?",(pk,)).fetchone()[0] == T1,
      msg)

# pid not visible in kitty -> fall back to the stored window_id.
c, pk = _focus_fixture(win_stored=7, attn=None)
calls = []
ok, _ = focus_session(c, pk, T1, list_fn=lambda s: [], focus_fn=lambda s, w: (calls.append(w) or True))
check("focus_session falls back to the cached window_id when the pid isn't found",
      ok and calls == [7])

# a failed kitty focus surfaces as an error, and no session/placement is an error.
c, pk = _focus_fixture()
okf, _ = focus_session(c, pk, T1, list_fn=lambda s: [], focus_fn=lambda s, w: False)
c2 = fresh()
p2 = c2.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
                " VALUES('np','/a','working',?,?)",(T0,T0)).lastrowid   # no herd_sessions row
okn, _ = focus_session(c2, p2, T1, list_fn=lambda s: [], focus_fn=lambda s, w: True)
check("focus_session errors on kitty failure and on a session with no placement",
      not okf and not okn)

# cli.resolve: herd id, uuid prefix, cwd substring, exact job.
c = fresh()
a = c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
              " VALUES('aaa11111','/x/api','working',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,?,'spawn',?)",(a,"api",SOCK,1,T0))
b = c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at)"
              " VALUES('bbb22222','/y/web','working',?,?)",(T0,T0)).lastrowid
ids = lambda ms: sorted(r["id"] for r in ms)
check("cli.resolve matches by herd id / uuid prefix / cwd / job",
      ids(_cli.resolve(c, str(a))) == [a] and ids(_cli.resolve(c, "aaa1")) == [a]
      and ids(_cli.resolve(c, "web")) == [b] and ids(_cli.resolve(c, "api")) == [a]
      and _cli.resolve(c, "nomatch") == [])
check("cli.resolve refuses an empty query (never matches all)",
      _cli.resolve(c, "") == [] and _cli.resolve(c, "   ") == [])

print("\n" + "═"*72)
if FAILED:
    print(f"\033[31m{len(FAILED)} FAILED:\033[0m " + ", ".join(FAILED)); sys.exit(1)
print("\033[32mALL CHECKS PASS — foundation validated\033[0m")
