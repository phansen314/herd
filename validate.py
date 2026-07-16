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

def fresh(tier2=True):
    if os.path.exists("f.db"): os.remove("f.db")
    for s in ("f.db-wal","f.db-shm"):
        if os.path.exists(s): os.remove(s)
    c = sqlite3.connect("f.db")
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
check("schema/herd.sql owns the trigger on sessions",
      "create trigger" in herd_code and "on sessions" in herd_code)

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

with guard("46 tier 2 cannot stand without tier 1"):
    if os.path.exists("g.db"): os.remove("g.db")
    c2 = sqlite3.connect("g.db")
    try:
        c2.executescript(HERD)
        check("46 tier 2 cannot stand without tier 1", False,
              "herd.sql applied with no sessions table — the dependency is not real")
    except sqlite3.OperationalError as e:
        check("46 tier 2 cannot stand without tier 1", True,
              f"one-way dependency proven: {e}")
    c2.close(); os.remove("g.db")

print("\n\033[1m═══ B. SCHEMA APPLIES ═══\033[0m")
c = fresh()
tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
check("four tables", tabs == ['events','herd_attention','herd_sessions','sessions'], str(tabs))
check("WAL mode", c.execute("PRAGMA journal_mode").fetchone()[0] == "wal")
check("auto_vacuum=INCREMENTAL", c.execute("PRAGMA auto_vacuum").fetchone()[0] == 2)
check("idempotent re-apply", (c.executescript(CORE), c.executescript(HERD), True)[-1])

print("\n\033[1m═══ C. THE SURROGATE KEY (the spine) ═══\033[0m")
c = fresh()
pk = c.execute("INSERT INTO sessions(cwd,status,status_source,started_at,updated_at) "
               "VALUES('/code/app','unknown','reconcile',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,live,kitty_socket,"
          "os_window_id,tab_id,window_id,herd_var,source,verified_at) "
          "VALUES(?,?,?,1,?,?,?,?,?,'spawn',?)",
          (pk,"api-refactor",T0,"unix:/tmp/kitty-1",1,3,7,"api-refactor",T0))
c.execute("INSERT INTO herd_attention(session_pk,attention_at) VALUES(?,?)",(pk,T0))
check("tier-2 rows exist with session_id NULL",
      c.execute("SELECT session_id FROM sessions WHERE id=?",(pk,)).fetchone()[0] is None,
      "spawned session has job+placement+attention before Claude reports a UUID")
# adopt — the SHIPPING W2
n = c.execute(W["W2_adopt"], {"session_id":"a3f9-uuid","cwd":"/code/app","model":"opus",
                              "transcript":"/t.jsonl","now":T1,
                              "socket":"unix:/tmp/kitty-1","win":7}).rowcount
check("W2 adopt via (socket,window_id)", n == 1)
r = c.execute("""SELECT s.session_id,h.job_name,h.window_id,a.attention_at
                 FROM sessions s LEFT JOIN herd_sessions h ON h.session_pk=s.id
                 LEFT JOIN herd_attention a ON a.session_pk=s.id WHERE s.id=?""",(pk,)).fetchone()
check("tier-2 survives adoption", tuple(r) == ("a3f9-uuid","api-refactor",7,T0), str(tuple(r)))
n2 = c.execute(W["W2_adopt"], {"session_id":"a3f9-uuid","cwd":"/code/app","model":"opus",
                               "transcript":"/t.jsonl","now":T1,
                               "socket":"unix:/tmp/kitty-1","win":7}).rowcount
check("W2 adopt is idempotent", n2 == 0, "re-fire is a no-op")
check("multiple unadopted rows coexist (UNIQUE ignores NULL)",
      (c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/x',?,?)",(T0,T0)),
       c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/y',?,?)",(T0,T0)),
       c.execute("SELECT COUNT(*) FROM sessions WHERE session_id IS NULL").fetchone()[0])[-1] == 2)

print("\n\033[1m═══ D. MUTABILITY CONTRACT (reconcile must not clobber) ═══\033[0m")
c = fresh()
pk = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,live,kitty_socket,"
          "window_id,herd_var,source,verified_at) VALUES(?,?,?,1,?,?,?,'spawn',?)",
          (pk,"api-refactor",T0,"unix:/tmp/kitty-1",7,"api-refactor",T0))
c.execute(W["W3b_placement"], {"pk":pk,"socket":"unix:/tmp/kitty-9","oswin":2,"tab":5,
                               "win":42,"title":"new title","var":None,"now":T2})
r = c.execute("SELECT job_name,created_at,live,window_id,tab_id,source,herd_var,verified_at "
              "FROM herd_sessions WHERE session_pk=?",(pk,)).fetchone()
check("reconcile preserves job_name", r["job_name"] == "api-refactor")
check("reconcile preserves created_at", r["created_at"] == T0)
check("reconcile preserves live", r["live"] == 1)
check("reconcile preserves herd_var (COALESCE)", r["herd_var"] == "api-refactor",
      "NULL from reconcile must not erase the spawn-time var")
check("reconcile preserves source='spawn'", r["source"] == "spawn", "provenance doesn't decay")
check("reconcile DOES update window_id", r["window_id"] == 42)
check("reconcile DOES update tab_id", r["tab_id"] == 5)
check("reconcile DOES update verified_at", r["verified_at"] == T2)

print("\n\033[1m═══ E. JOB NAMES: recyclable handles + history ═══\033[0m")
c = fresh()
p1 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/a',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,?,?,'spawn',?)",(p1,"api-refactor",T0,"unix:/tmp/k1",7,T0))
p2 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/b',?,?)",(T0,T0)).lastrowid
try:
    c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,?,?,'spawn',?)",(p2,"api-refactor",T0,"unix:/tmp/k1",8,T0))
    check("duplicate live job_name rejected", False, "it was ALLOWED")
except sqlite3.IntegrityError:
    check("duplicate live job_name rejected", True)
# kill session 1 -> trigger must free the name
c.execute("UPDATE sessions SET stopped_at=? WHERE id=?",(T2,p1))
check("trg_herd_job_death fired",
      c.execute("SELECT live FROM herd_sessions WHERE session_pk=?",(p1,)).fetchone()[0] == 0)
c.execute("INSERT INTO herd_sessions(session_pk,job_name,created_at,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,?,?,'spawn',?)",(p2,"api-refactor",T2,"unix:/tmp/k1",8,T2))
check("name reusable after death",
      c.execute("SELECT COUNT(*) FROM herd_sessions WHERE job_name='api-refactor'").fetchone()[0] == 2,
      "history retained: 2 rows, one live one dead")
# trigger must NOT fire on unrelated updates
c.execute("UPDATE sessions SET updated_at=? WHERE id=?",(T2,p2))
check("trigger ignores non-death updates",
      c.execute("SELECT live FROM herd_sessions WHERE session_pk=?",(p2,)).fetchone()[0] == 1)
# NULL job_name (reconciled sessions) must not collide
p3 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/c',?,?)",(T0,T0)).lastrowid
p4 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/d',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'reconcile',?)",(p3,"unix:/tmp/k1",20,T0))
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'reconcile',?)",(p4,"unix:/tmp/k1",21,T0))
check("many NULL job_names coexist", True, "reconciled sessions have no job")

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
    c.execute(W["W5_statusline"], {"model":None,"sname":None,"ctx":42,"cost":1.5,
                                   "branch":None,"now":t,"session_id":"s1"})
r = c.execute("SELECT last_event_at,updated_at FROM sessions WHERE session_id='s1'").fetchone()
check("statusline moves updated_at", r["updated_at"] == T2)
check("statusline does NOT move last_event_at", r["last_event_at"] == T0,
      "the gap between clocks IS the attention signal")
gap = "10:00 -> 10:10 = 10min of true silence, visible"
check("idle signal is real", r["last_event_at"] != r["updated_at"], gap)

print("\n\033[1m═══ H. STATUSLINE MUST NOT CREATE OR RESURRECT ═══\033[0m")
c = fresh()
n = c.execute(W["W5_statusline"], {"model":None,"sname":None,"ctx":50,"cost":None,
                                   "branch":None,"now":T1,"session_id":"ghost"}).rowcount
check("statusline on unknown session is a no-op", n == 0,
      "UPDATE-only: never invents rows with empty cwd")
c.execute("INSERT INTO sessions(session_id,cwd,status,started_at,updated_at,stopped_at)"
          " VALUES('dead','/a','stopped',?,?,?)",(T0,T0,T1))
n = c.execute(W["W5_statusline"], {"model":None,"sname":None,"ctx":50,"cost":None,
                                   "branch":None,"now":T2,"session_id":"dead"}).rowcount
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
# composite window uniqueness
p2 = c.execute("INSERT INTO sessions(cwd,started_at,updated_at) VALUES('/b',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'spawn',?)",(pk,"unix:/tmp/k1",7,T0))
try:
    c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'spawn',?)",(p2,"unix:/tmp/k1",7,T0))
    check("(socket,window_id) unique", False, "duplicate window accepted!")
except sqlite3.IntegrityError:
    check("(socket,window_id) unique", True, "one claude per kitty window")
c.execute("INSERT INTO herd_sessions(session_pk,kitty_socket,window_id,source,verified_at) VALUES(?,?,?,'spawn',?)",(p2,"unix:/tmp/kitty-OTHER",7,T0))
check("same window_id on DIFFERENT socket is legal", True,
      "listen_on unix:/tmp/kitty-{kitty_pid} => per-instance id space")

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

# ── 41. a window is a RECYCLABLE HANDLE, exactly like a job name ───────────
c = fresh()
SOCK = "unix:/tmp/kitty-20035"
a = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(111,'/code/herd',?,?)",(T0,T0)).lastrowid
c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
          " VALUES(?,?,?,5,'reconcile',?)",(a,T0,SOCK,T0))
c.execute("UPDATE sessions SET stopped_at=?,status='stopped' WHERE id=?",(T1,a))
b = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(222,'/code/herd',?,?)",(T2,T2)).lastrowid
try:
    c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,5,'reconcile',?)",(b,T2,SOCK,T2))
    check("41 window reuse: new session gets placement in a reused window", True,
          "dead row keeps window_id as history")
except sqlite3.IntegrityError as e:
    check("41 window reuse: new session gets placement in a reused window", False,
          f"dead session owns the window forever: {e}")
# ...and the original invariant must survive the fix
c3 = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(333,'/x',?,?)",(T2,T2)).lastrowid
try:
    c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,5,'reconcile',?)",(c3,T2,SOCK,T2))
    check("41b two LIVE sessions in one window still rejected", False, "REGRESSION: allowed!")
except sqlite3.IntegrityError:
    check("41b two LIVE sessions in one window still rejected", True, "invariant preserved")

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
              " VALUES(?,?,?,5,'reconcile',?)",(a,T0,SOCK,T0))
    c.execute("UPDATE sessions SET stopped_at=?,status='stopped' WHERE id=?",(T1,a))
    b = c.execute("INSERT INTO sessions(pid,cwd,started_at,updated_at) VALUES(222,'/code/herd',?,?)",(T2,T2)).lastrowid
    c.execute("INSERT INTO herd_sessions(session_pk,created_at,kitty_socket,window_id,source,verified_at)"
              " VALUES(?,?,?,5,'reconcile',?)",(b,T2,SOCK,T2))
    return c, a, b

with guard("43 W2 adopts the LIVE row, not the dead predecessor"):
    c, a, b = reused_window()
    c.execute(W["W2_adopt"], {"session_id":"uuid-B","cwd":"/code/herd","model":"opus",
                              "transcript":"/t.jsonl","now":T2,"socket":SOCK,"win":5})
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

# ── 44. source-level: the (socket, window_id) contract ────────────────────
# The pair is unique only among LIVE rows, so a scalar subquery on it can match
# a dead row AND a live row — SQLite then picks one arbitrarily. Every query on
# the pair must carry `AND live = 1`. No runtime test catches a NEW query that
# forgets; this does.
writes_code = "\n".join(l.split("--")[0] for l in WRITES.splitlines())
stmts = [s for s in writes_code.split(";") if "window_id" in s and "kitty_socket" in s]
offenders = [" ".join(s.split())[:60] for s in stmts
             if re.search(r"WHERE[\s\S]*kitty_socket\s*=", s, re.I) and not re.search(r"live\s*=\s*1", s, re.I)]
check("44 every (socket,window_id) lookup filters live=1", not offenders,
      f"unguarded: {offenders}" if offenders else "W2, W3a, W5b all guarded")
# Ask the DB what the index actually IS. Regexing the source text here reads the
# PROSE in the neighbouring idx_herd_job_live comment (which legitimately says
# "live = 1") and passes even when the index predicate is gone — a check that
# cannot fail is worse than no check, because it manufactures confidence.
c = fresh()
idx_sql = c.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_herd_window'").fetchone()
check("44b idx_herd_window is partial on live=1",
      idx_sql is not None and re.search(r"live\s*=\s*1", idx_sql[0], re.I) is not None,
      "a dead session must not own a window forever; "
      + (f"predicate: {idx_sql[0].split('WHERE')[-1].strip()}" if idx_sql else "index MISSING"))
# and the inverse of 42, at source level: statusline may never carry a clock
w5 = W["W5_statusline"].split("--")[0]
check("44c W5_statusline never touches last_event_*", "last_event" not in w5.lower(),
      "the one invariant no runtime test survives someone 'optimizing' the statusline")

print("\n" + "═"*72)
if FAILED:
    print(f"\033[31m{len(FAILED)} FAILED:\033[0m " + ", ".join(FAILED)); sys.exit(1)
print("\033[32mALL CHECKS PASS — foundation validated\033[0m")
