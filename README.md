# herd

Track your Claude Code sessions in a local SQLite database — which are working,
which are waiting on *you*, where they live in [kitty](https://sw.kovidgoyal.net/kitty/),
and what they've cost — so a pile of sessions across a pile of terminal windows
stops being something you have to hold in your head.

herd is **local-only**: no network, no telemetry, no runtime dependencies. Hooks
are bash + `jq` + `sqlite3` on purpose (a Python cold start on every `SessionStart`
would be felt); the background daemon is stdlib Python.

> **Status: early but usable.** The data model, lifecycle hooks, statusline, the
> liveness/attention daemon, and the `herd` CLI (`ls`, `spawn`, fzf-powered `jump`,
> and the `watch` dashboard) are built, tested, and installed. Ambient attention is
> Claude's terminal bell plus kitty's tab flag rather than a herd-owned notifier —
> see [Notifications](#notifications-kitty-tab-bell) and [Roadmap](#roadmap).

## What it does

Every Claude Code session writes into `~/.herd/herd.db` as it lives:

- **Identity & state** — session UUID, cwd, model, and Claude's own status:
  `working`, `waiting` (turn ended, wants input), `needs_approval` (permission
  prompt), `stopped`.
- **Placement** — the kitty socket + window it's running in (the jump target), plus
  the live tab title (captured per prompt, so a dead session can be reopened under its
  real name — see [`herd restart`](#herd-restart--bring-back-dead-sessions)).
- **Metrics** — context %, cost, burn rate, token counts, lines changed, rate-limit
  windows (from the statusline).
- **Liveness** — a background daemon reaps sessions whose process died *silently*
  (kill -9, crash, closed terminal) where no hook could fire.
- **Attention** — the daemon derives which sessions need you (waiting too long,
  a permission prompt sitting, a working session gone silent) and records it.

## Install

Requires: `bash`, `jq`, `sqlite3`, Python ≥ 3.9, and — for `jump`/`watch`/placement —
`fzf` and kitty. **kitty also has to be configured** for remote control, which is not
its default: see [kitty setup](#kitty-setup). The CLI runs from the source tree — no
`pip install` needed — and the installer *copies* the hooks out of it (see
[Hook development](#hook-development---dev)).

An existing `~/.claude/settings.json` is expected; the installer edits it in place.

```bash
git clone <repo> ~/code/herd && cd ~/code/herd
PYTHONPATH=src python3 -m herd.install            # wire everything
PYTHONPATH=src python3 -m herd.install --dry-run  # preview, touch nothing
```

The installer:

1. **bootstraps** `~/.herd/herd.db` and `~/.herd/templates/`;
2. **copies the hooks + their SQL** from the checkout to `~/.herd/hooks` and
   `~/.herd/schema`, and wires `settings.json` *there* — so a `git checkout`, stash
   or rebase in the source tree cannot change what running sessions execute. The
   SQL travels with them on purpose: `common.sh` resolves `writes.sql` relative to
   the hook directory, and hooks installed without it would fail every write while
   still exiting 0. Re-run the installer after editing hooks, or use `--dev`;
3. **self-tests the staged copy, then promotes it** — the hooks from step 2 are
   staged in a temp directory and executed there against a temp DB *before* anything
   is wired; only a PASS promotes them to `~/.herd/hooks`. This is why the step sits
   here and not at the end: self-testing the hooks after wiring them would copy
   broken hooks into every running session and only then report FAIL. A FAIL aborts
   the install with `settings.json` untouched;
4. **wires the hooks + statusline** into `~/.claude/settings.json` — backing up each
   file first (`*.herd-bak.<ts>`, plus a one-time `*.herd-bak.original`) and preserving
   anything it doesn't own (e.g. an existing PreToolUse hook). The `statusLine` key is
   pointed at herd's `statusline.sh`, except where a `~/.claude/custom-status-line.sh`
   wrapper already fronts it — that wrapper is rewired instead, so it keeps whatever
   else it does. A `statusLine` running a script herd doesn't recognise is **left
   alone and reported**: point it at `src/herd/hooks/statusline.sh` yourself, or herd
   records no cost/context/branch (that hook is the only writer of those columns);
5. **installs the daemon** to start on login and restart on exit — a `systemd --user`
   service (`herd.service`) on Linux, a LaunchAgent (`com.codingzen.herd`) on macOS,
   where it also logs to `~/.herd/daemon.{out,err}.log` because launchd keeps no
   journal. Where neither manager exists (headless/containers) this step is a
   graceful no-op — run the daemon yourself.
6. **symlinks the CLI** — `herd` into `~/.local/bin` and bash completion into
   `~/.local/share/bash-completion/completions` (WARNs if `~/.local/bin` isn't on
   your PATH);
7. **offers to enable kitty remote control** — only when it can see that it's off
   (running inside kitty with no socket). Opt-in, backed up, and removed again by
   `--uninstall`; see [kitty setup](#kitty-setup);

If you use [klawde](https://github.com/wolffiex/klawde), note that the installer
**unwires it**: any hook command under `/.klawde/` is dropped from `settings.json`
(the two tools both own the statusline and would fight).

Undo it — hooks, statusline, service, the CLI symlinks, and the installed hook tree
under `~/.herd/hooks` and `~/.herd/schema` — with:

```bash
PYTHONPATH=src python3 -m herd.install --uninstall
```

This **reverses herd's edits on the live files** — it strips the hook entries herd
owns, hands `statusLine` back to whatever held it before, restores the wrapper's
original statusline invocation, and removes herd's block from `kitty.conf` if the
installer added one. Everything else in `settings.json` is left exactly as it is: permission grants, MCP servers, and other tools' hooks all survive, including
ones added long after the install. Each file is backed up (`*.herd-bak.<ts>`) before
it is written.

The hook tree is removed **only if the unwiring succeeded**. If `settings.json` or
the wrapper can't be unwired — a hand edit it can't parse, or a wrapper with no
pre-herd invocation to restore — uninstall says so, exits nonzero, and *keeps*
`~/.herd/hooks`, because those files are still referenced by the wiring it just
declined to touch. Removing them anyway would turn a merely stale config into a
broken one, with every hook and the `statusLine` pointing at paths that no longer
exist. Resolve what it named and re-run.

Only files herd's own extensions match are removed (`*.sh`, `*.sql`), and the
directory is kept if anything else is in it. A `--dev` install is refused outright:
there, `~/.herd/hooks` *is* the checkout.

One key is deliberately left behind. If `preferredNotifChannel` is set, uninstall
names it and moves on: herd's opt-in and your own setting are the same value on disk,
so it can't tell them apart, and deleting a real preference is the worse mistake.

If you'd rather revert wholesale to the **pre-herd `*.herd-bak.original` snapshot** —
taken once on the first install and never overwritten, so re-installing any number of
times doesn't affect it — add `--restore-original`:

```bash
PYTHONPATH=src python3 -m herd.install --uninstall --restore-original
```

That discards anything added to the file since the install, which is why it isn't the
default; the backup taken first is your way back. (Installs predating that snapshot
fall back to the oldest `*.herd-bak.<ts>`.) If no pre-herd copy survives it says so,
exits nonzero, and leaves the file wired for you to unwire by hand.

Your data survives either way: `~/.herd/herd.db` is never deleted.

### Upgrading

```bash
cd ~/code/herd && git pull
PYTHONPATH=src python3 -m herd.install
```

Re-running the installer **is** the upgrade: it refreshes the hook tree and
migrates the database. Schema changes so far have all been new columns, and
`CREATE TABLE IF NOT EXISTS` does nothing about those on a table that already
exists — so the installer diffs your database against the shipped schema and adds
what is missing, reporting each one. Anything it cannot add additively is reported
as a failure at install time rather than becoming a write that fails on every tick
afterwards.

This matters because of how herd fails: a statement naming a column your database
lacks fails, the hook logs it to `~/.herd/hook-errors.log` and exits 0 by design,
and your metrics simply stop. `herd doctor` names the missing columns if you ever
land in that state.

### kitty setup

herd needs kitty's **remote control** to record *where* a session lives and to jump
there. It is off by default, and when it's off the failure is quiet: sessions are
still tracked, still listed, still costed — but every placement is empty, `herd jump`
has nothing to focus, and `herd spawn` refuses outright.

```conf
# ~/.config/kitty/kitty.conf
allow_remote_control yes
listen_on unix:/tmp/kitty-{kitty_pid}
```

Then **restart kitty** — neither option is picked up by a config reload, so
`kitten @ load-config` will not do it.

`{kitty_pid}` is not cosmetic. A window id only means something paired with the
socket it came from, so a fixed socket path lets two kitty instances hand out
colliding ids and jumps land in the wrong window.

The installer offers to add exactly these two lines for you, wrapped in
`# BEGIN herd` / `# END herd` markers. It only asks when it can *see* that they're
missing — i.e. you're inside kitty and there's no socket — because from outside kitty
it cannot tell a missing config from a working one, and it will not edit a file herd
doesn't own on a guess. The file is backed up first, and `--uninstall` removes the
block again, leaving a kitty.conf herd never touched byte-identical.

`herd doctor` reports the state either way, including a live `kitten @ ls` probe when
a socket is configured.

### Hook development (--dev)

The copy in step 2 is what makes hook edits invisible until you re-install. If you're
*working on* the hooks, that round trip gets old:

```bash
PYTHONPATH=src python3 -m herd.install --dev   # wire the CHECKOUT, no copy
```

Every session then executes `src/herd/hooks/*.sh` directly, so edits take effect on
the next hook fire. The tradeoff is the one the copy exists to prevent: **a `git
checkout`, stash or rebase now changes what live sessions run** — mid-turn. `herd
doctor` reports which mode is wired, WARNs while `--dev` is active, and on a copy
install tells you when the copy has drifted from the tree. Re-run the installer with
no flags to go back to the copy.

### Bad flags are refused

An argv token the installer doesn't recognise is treated as a typo, not ignored:
it prints the usage, **changes nothing**, and exits 2. Same for `--uninstall`
combined with `--dry-run` or `--dev` — uninstall has no dry-run and ignores `--dev`,
so accepting either would do something other than what you asked.

## Using it

```bash
herd ls                 # live sessions, attention-first, by name
herd spawn <job>        # launch claude in a new kitty tab, tracked from the start
herd jump               # fuzzy-pick a session (fzf) with a live preview, and focus it
herd jump <query>       # herd id, name (/rename), job, uuid, or cwd; unique match jumps
herd watch              # the picker as a permanent dashboard
herd watch --one-shot   # same picker, exits after one jump (kitty overlay panel)
herd restart            # fzf multi-select of dead sessions -> resume each in a new tab
herd doctor             # why isn't herd recording anything? (see Troubleshooting)
```

Sessions show by their recognizable name — Claude's `/rename` name, else herd's job,
else the uuid. `jump` opens an fzf picker (with a detail preview) unless the query is
a unique match, in which case it focuses immediately.

### `herd spawn` — name a session up front

So it has a handle before Claude has even reported a UUID:

```bash
herd spawn api                              # a new tab, cwd here
herd spawn api --pane                       # a split instead
herd spawn api --cwd ~/code/x --prompt "review the diff" -- --model opus
```

Job names are recyclable: `spawn` refuses a name a *live* session already holds, but
once that session dies the name is free again. Everything after `--` is passed
through to `claude`.

**Templates** — a preset for a spawn you run often. Drop a TOML file in
`~/.herd/templates/` and pass `-t`:

```toml
# ~/.herd/templates/review.toml
job    = "review"          # optional — then `herd spawn -t review` needs no name
cwd    = "~/code/herd"
type   = "pane"            # tab | pane
title  = "code review"
args   = ["--model", "opus"]
prompt = """
Review the working diff.
Focus on correctness; skip style.
"""

[vars]                     # optional — extra kitty user vars on the new window
PROJECT = "herd"           # (herd sets HERD_JOB itself)
```

```bash
herd spawn -t review                 # everything from the template
herd spawn api -t review --tab       # CLI wins: job=api, type=tab, rest from template
```

Precedence is **CLI flag > template > built-in default**, so a template is a set of
defaults you can always override. The one exception is `--`: those args are *appended*
to the template's `args` rather than replacing them, so a template can carry a base
set and you add ad-hoc flags on top. `-t` tab-completes.

Templates need **Python 3.11+** (stdlib `tomllib`); the rest of herd still runs on
3.9. Unknown keys are rejected rather than ignored, so a typo tells you instead of
silently doing nothing. (Why TOML, and how precedence is resolved:
[DESIGN.md#templates](DESIGN.md#templates).)

### `herd watch` — the dashboard

The same picker, looping, refreshing itself as sessions change. The best home for it
is a **kitty overlay** — a panel that stacks on top of whatever window you're in,
costs no tab and no split, and disappears when you pick something:

```conf
# ~/.config/kitty/kitty.conf
map ctrl+space>c launch --type=overlay ~/.local/bin/herd watch --one-shot
```

That's the whole recipe. Two things make it work, both measured rather than assumed:

- **`--type=overlay` inherits `KITTY_LISTEN_ON`.** `--type=background` does *not*, and
  a process without it fails every `kitten @` call silently — so the overlay can focus
  its target with no `--allow-remote-control` on the mapping.
- **Closing the overlay does not steal focus back.** kitty leaves you on the window you
  jumped to, so the panel can tear itself down immediately after the jump.

`--one-shot` is required here, and it is the only difference from plain `watch`: an
overlay dies with its process, so the forever-loop would survive the jump and strand a
live panel on the window you jumped *away* from — then the next keypress, now in a
different window, would open another. In one-shot mode Enter jumps and closes, Esc
dismisses, `ctrl-r` still refreshes, `ctrl-q` / `ctrl-c` still quit.

Plain `herd watch` keeps the original behavior for a dedicated tab or a spare monitor:
it loops, and Esc re-opens the picker rather than exiting — a window you can't
accidentally fall out of. (Why fzf and not a curses TUI:
[DESIGN.md#watch](DESIGN.md#watch).)

### `herd restart` — bring back dead sessions

A reboot (or a crash, or closing kitty) kills every live session at once. Because herd
records a session's death by *marking* the row, not deleting it, the UUID, cwd and tab
title all survive — enough to rebuild the session exactly. `herd restart` is the
recovery:

```bash
herd restart            # inside kitty; needs fzf
```

It opens an fzf **multi-select** of the dead-but-resumable sessions, most-recently-dead
first (a reboot reaps them all near boot, so the ones you just lost sit at the top).
`Tab` marks, `Enter` confirms. Each pick opens a new kitty tab running
`claude --resume <uuid>` in that session's original cwd, **titled with the tab's real
name** — the one you'd captured while it was alive, not a generic fallback. Resuming
reuses the same session, so its herd row is revived in place (no duplicate); pick five
at once and you're back where the reboot left you.

Runs inside kitty (it opens tabs) and requires `fzf` — the multi-select *is* the whole
UI, so there is no non-fzf fallback. (Why it launches directly instead of through
`spawn`, and why the tab title is the one kitty value herd persists:
[DESIGN.md#restart](DESIGN.md#restart).)

### Reading the database directly

Everything the CLI shows, and more. The DB lives at `~/.herd/herd.db` (WAL mode, so
`-wal` / `-shm` siblings exist — copy all three, or use `sqlite3 .backup`, if you
back it up):

```bash
sqlite3 -header -column ~/.herd/herd.db "
SELECT s.id, substr(s.session_id,1,8) uuid, s.pid, s.status,
       h.job_name job, h.window_id win, s.context_percent ctx,
       printf('\$%.2f', s.total_cost_usd) cost,
       (a.attention_at IS NOT NULL) attn, s.cwd
FROM sessions s
LEFT JOIN herd_sessions  h ON h.session_pk = s.id
LEFT JOIN herd_attention a ON a.session_pk = s.id
WHERE s.stopped_at IS NULL
ORDER BY a.attention_at IS NULL, a.attention_at, s.started_at DESC;"
```

The schema is split in two tiers — `sessions` holds facts that would be true
whether or not herd existed; `herd_sessions`/`herd_attention` are herd's own
relationship to a session. Read tier 1 and you can ignore that herd exists at all.
(Rationale: [DESIGN.md](DESIGN.md#tiers).)

If you build a tool on this, treat `sessions.status` as an **open set**: render any
value you don't recognize as `unknown` rather than switching exhaustively. The current
values are `working`, `waiting`, `needs_approval`, `stopped`, `unknown`, and the
`CHECK` constraint that enforces them will gain members as Claude Code adds lifecycle
hooks. Growing that set is additive for readers that degrade gracefully and breaking
for ones that don't. Same rule for `status_source` (also `CHECK`-constrained:
`hook`, `reconcile`, `pid`) and for `last_event_type`, which carries no `CHECK` at
all — today it is `start|tool|stop|notify|end`.

### The daemon

```bash
# Linux (systemd --user)
systemctl --user status herd            # is it running
systemctl --user restart herd           # after editing the source
journalctl --user -u herd -f            # watch it (quiet unless it errors)

# macOS (launchd). $UID, not `sudo` — it is a per-user agent.
launchctl print gui/$UID/com.codingzen.herd     # is it running (look for `state`/`pid`)
launchctl kickstart -k gui/$UID/com.codingzen.herd   # -k = restart if already up
tail -f ~/.herd/daemon.err.log                  # launchd has no journal; this is it

# or run it by hand:
PYTHONPATH=src python3 -m herd.daemon           # reaper + attention
PYTHONPATH=src python3 -m herd.daemon --once    # a single tick
HERD_ATTENTION=0 PYTHONPATH=src python3 -m herd.daemon   # core-only
```

**Exactly one daemon runs at a time**, enforced by an advisory lock on
`$HERD_RUNTIME/herd-daemon.lock`. A second one exits 1 and names the holder rather
than starting — two daemons tick attention against different clocks, so a session
can arm and disarm on alternating ticks and the mark flickers. If you started one
by hand and then installed the service, the loser is whichever came second; stop it
and let the other run. The lock is released by the kernel however the holder dies,
so a `kill -9` never leaves it stuck.

It runs two layers on one loop, mirroring the tier boundary:

| layer | writes | when |
|---|---|---|
| **core** (tier 1) | `sessions.stopped_at` via `ps` liveness — the reaper | always |
| **herd** (tier 2) | `herd_attention` — the silence rule | gated by `HERD_ATTENTION` |

Run herd purely for **core data collection** with `HERD_ATTENTION=0` and build your
own tooling on the `sessions` table; the *daemon* then never touches
`herd_attention`. (`herd jump` still acks — that write comes from the CLI and is
outside the gate.)

**`herd jump` acks the mark.** Jumping to a session clears its mark without touching
Claude's activity clock. If you look and then answer nothing, the same timer runs
again from the moment you jumped and it speaks up once more — so an ack is a snooze,
not a dismissal. See [DESIGN.md#ack](DESIGN.md#ack).

**Tuning the attention rule.** Settings live in `~/.herd/config` (`KEY=value`, `#`
comments), which the installer creates fully commented out. **Put them there, not in
your shell.** The hooks are children of your shell and see what you export; the
daemon is started by `systemctl --user` and inherits *nothing* from it, so a setting
exported in `.bashrc` reaches half of herd. For `HERD_CLAUDE_NAME` that half-reach is
destructive: the hooks record a pid the reaper then reads as recycled, and every live
session is stopped on the daemon's next tick.

An environment variable still wins over the file, for one-off overrides. `herd doctor`
reads the running daemon's own environment and reports any key the file sets that the
daemon did not actually get — restart the daemon after editing:
`systemctl --user restart herd`.

Defaults shown:

| var | default | meaning |
|---|---|---|
| `HERD_ATTENTION` | `1` | `0`/`off` → core-only (reaper only, no `herd_attention`) |
| `HERD_WAIT_SECS` | `30` | grace before a `waiting` session needs you |
| `HERD_APPROVAL_SECS` | `15` | grace before a `needs_approval` prompt does |
| `HERD_STUCK_SECS` | `300` | silence before a `working` session reads as stuck |
| `HERD_STRANDED_SECS` | `120` | grace before a spawn reservation whose session never started is dropped |
| `HERD_DB` | `~/.herd/herd.db` | database path |

Also read from the same file, by the hooks and CLI:

| var | default | meaning |
|---|---|---|
| `HERD_TEMPLATES` | `~/.herd/templates` | where `herd spawn -t` looks for `<name>.toml` |
| `HERD_TOOL_THROTTLE` | `2` | seconds to coalesce `PostToolUse` writes on the hot path |
| `HERD_CLAUDE_NAME` | `claude` | process name the pid ancestry walk looks for (node-based installs) |
| `HERD_ERRLOG` | `~/.herd/hook-errors.log` | where hooks log failures (they never print to Claude) |
| `HERD_ERRLOG_MAX` | `1048576` | bytes before the log rotates to `.1`; `0` keeps everything |
| `HERD_RUNTIME` | `$XDG_RUNTIME_DIR`, else `~/.herd/run` | per-session throttle + statusline cache files, and the daemon's single-instance lock |

Read by the daemon only, and rarely worth setting — listed because `herd doctor`
can name them in a warning, and a key it reports but the README never mentions
reads like a typo:

| var | default | meaning |
|---|---|---|
| `HERD_DAEMON_LOG_MAX` | `1048576` | bytes before the daemon truncates its own stderr log; `0` disables the cap |
| `HERD_BACKOFF_MAX_SECS` | `60` | ceiling on the retry backoff after consecutive failed ticks |
| `HERD_ORPHAN_GRACE_SECS` | `300` | age before a runtime file whose session is gone is swept |
| `HERD_CONFIG` | `~/.herd/config` | the config file itself — env only, since the file cannot name its own path |

## Notifications (kitty tab bell)

herd sends no notifications itself — **Claude Code rings the bell, kitty flags the
tab.** For an ambient "this session wants you" marker, set Claude's notification
channel to the terminal bell in `~/.claude/settings.json`:

```json
"preferredNotifChannel": "terminal_bell"
```

The installer **offers** to set this (interactive, opt-in — it never forces it, and
never overrides a channel you've already chosen).

Claude rings it when a turn ends waiting for input or a permission prompt appears
(it's the only process with the window's tty — herd's hooks run detached, so they
can't). kitty then marks that tab and flags the window via `bell_on_tab` and
`window_alert_on_bell` (both on by default; add `enable_audio_bell no` for a silent,
visual-only bell), clearing when you focus the tab.

This is deliberately outside herd — the daemon stays kitty-free and you keep control
of your own Claude notification preference.

**The bell does not cover everything, and the marks say which.** herd's own signal
shows in `herd ls` and the jump picker as one of three glyphs:

| | | ambient signal? |
|---|---|---|
| 🙋 | `waiting` — turn ended, wants you | yes, Claude bells |
| 🔐 | `needs_approval` — permission prompt sitting | yes, Claude bells |
| 🥱 | `working` past `HERD_STUCK_SECS` — silently stuck | **no. nothing pushes this at you** |

A stuck session never ends its turn, so it never bells and kitty never flags its tab.
That is the one state you can only find by *looking* — which is what
[`herd watch`](#herd-watch--the-dashboard) is for. Bind it to a key as an overlay
panel (recipe above), and the answer to "is anything wedged?" is one keystroke rather
than a notification you'd have to trust herd to send.

herd deliberately sends none. Why a session-invoked pager was considered and
rejected: [DECISIONS.md#pager](DECISIONS.md#pager).

## Troubleshooting

**Start here:**

```bash
herd doctor
```

It checks dependencies, the database, whether the hooks and statusline are really
wired (and still point at files that exist and are executable), whether exactly one
daemon is running, malformed `HERD_*` values, and recent hook errors. Exits nonzero
if anything is broken:

```
  wiring
    ✔ SessionStart  —  …/src/herd/hooks/session_start.sh
    ✔ statusLine (via wrapper)  —  ~/.claude/custom-status-line.sh
  daemon
    ✘ daemon not running  —  sessions will never leave `herd ls`
```

Everything herd does at runtime is designed to fail *silently* — hooks **never**
print to Claude, a missing dependency exits 0, the daemon logs to a journal. That
is deliberate, and it is why the diagnosis lives in one command instead of in your
terminal. Raw sources, if you want them: `~/.herd/hook-errors.log` (`HERD_ERRLOG`)
and `journalctl --user -u herd`.

| symptom | cause |
|---|---|
| `herd: command not found` | `~/.local/bin` not on PATH (the installer WARNs about this), or an open shell that hasn't rehashed — `hash -r` |
| statusline blank or missing | the hook scripts lost `+x` — `python3 -m herd.install` self-tests for exactly this and now aborts rather than wiring broken hooks. If it passes, check `statusLine` in `~/.claude/settings.json`: the installer leaves a statusline it doesn't own alone (and says so at install time) |
| cost / context / branch always empty, but sessions appear | the hooks are wired and `statusLine` isn't. Only `statusline.sh` writes those columns — see the `statusLine` note under Install |
| no sessions ever appear | `~/.claude/settings.json` wasn't rewired — re-run the installer. Rows come from the hooks and `herd spawn`, never from the daemon, so this is not a daemon problem |
| sessions appear but never go away | the daemon is down; only it reaps silent deaths. Live rows are `stopped_at IS NULL` |
| `herd spawn` → "needs to run inside kitty" | `KITTY_LISTEN_ON` is unset. kitty needs `allow_remote_control yes` **and** `listen_on unix:/tmp/kitty-{kitty_pid}` — see [kitty setup](#kitty-setup) |
| sessions listed fine, but `herd jump` says there's no window | same cause, quieter: remote control is off, so placement was never recorded. `herd doctor` names it |
| `herd spawn -t` → "templates need Python 3.11+" | `tomllib` is 3.11+. Only templates need it; the rest of herd runs on 3.9 |
| `herd watch` → "needs fzf and a tty" | `fzf` isn't installed, or you're not on a terminal |
| `herd jump` prints a list instead of jumping | same — no `fzf`, so it degrades to printing rather than failing |
| a key bound to `launch --type=background` does nothing | that process gets no `KITTY_LISTEN_ON`, so every `kitten @` fails silently; add `--allow-remote-control`, or use `--type=overlay`, which inherits it (see [watch](#herd-watch--the-dashboard)) |
| no `systemctl --user` or `launchctl` (headless/containers) | expected — the service step no-ops. Run `python3 -m herd.daemon` yourself |
| macOS: daemon not running after login | `launchctl print gui/$UID/com.codingzen.herd`; if absent, re-run the installer. Its stderr is `~/.herd/daemon.err.log`, not a journal |

Start fresh: stop the daemon, delete `~/.herd/herd.db*` (the DB plus its `-wal` and
`-shm` siblings), and re-run the installer. You lose history; nothing else depends
on it.

## How it works

Two data sources, two directions:

- **Hooks (push).** Claude Code fires lifecycle hooks — `SessionStart`, `Stop`,
  `Notification`, `PostToolUse`, `SessionEnd` — plus the statusline (~1/sec). They
  record what Claude *reports*: identity, status, metrics, and (from the kitty
  environment) placement. `SessionStart` also walks the process tree to capture
  Claude's own pid.
- **Daemon (pull).** A background process reads the **process table** each tick to
  catch what hooks structurally cannot: a session that died without firing
  `SessionEnd`. Liveness comes from `ps`, never from kitty — absence from a kitty
  listing is evidence about *placement*, and reaping on it would nuke every live
  row on a socket blip.

Everything else — the tier boundary, the identity spine, the two clocks behind the
attention signal, and why every write is a named statement in one canonical SQL file
— is in [DESIGN.md](DESIGN.md). What was tried and rejected along the way, and what
would break if you undid it, is in [DECISIONS.md](DECISIONS.md).

## Development

The whole design is asserted, not narrated, by the `pytest` suite:

```bash
python3 -m pytest       # whole suite, no install needed, a few seconds
```

It runs the real bash hooks and the real Python against throwaway databases, and
proves the invariants the design rests on — the tier boundary, the identity model,
the two-clocks attention thesis, the reaper's liveness rules, that every hook exits 0
under every degradation, and that the hooks and daemon load the same canonical SQL.
New behavior is added test-first (red before green). CI runs the suite plus one
other hard gate: a green run must not be a *skipped* run. `conftest` skips the hook
tests when `bash`, `jq` or `sqlite3` is missing — correct on a contributor's
machine, dangerous on a runner, where "0 failures" looks identical to a run that
never executed them. `.github/check-skips.py` fails the build on any skip whose
reason isn't whitelisted. (Fixture layout and why it runs the real hooks:
[DESIGN.md#testing](DESIGN.md#testing).)

How it works lives in [`DESIGN.md`](DESIGN.md) and why it works that way in
[`DECISIONS.md`](DECISIONS.md); source comments carry a one-line summary and point
there. Start with the schema —
[`schema/core.sql`](src/herd/schema/core.sql) (tier 1),
[`schema/herd.sql`](src/herd/schema/herd.sql) (tier 2), and
[`schema/writes.sql`](src/herd/schema/writes.sql) (every write path).

## Layout

```
src/herd/
  schema/        core.sql · herd.sql · writes.sql   — the data model + canonical SQL
  hooks/         *.sh (bash 3.2 + jq + sqlite3)      — lifecycle capture + statusline
  db.py          statement loader + connection policy
  daemon.py      the reaper + attention tick
  install.py     hooks/statusline/service/CLI wiring
  settings.py    THE definition of what herd owns in ~/.claude/settings.json
  doctor.py      `herd doctor` — the diagnosis layer, safe on a sick machine
  config.py      ~/.herd/config — one settings file the daemon and hooks both read
  cli.py         the `herd` CLI (ls, spawn, jump, watch + fzf preview machinery)
  spawn.py       `herd spawn` — SpawnSpec, resolve_spec, reserve-then-launch
  template.py    ~/.herd/templates/*.toml -> SpawnSpec defaults (herd spawn -t)
  kitty/         config.py — the two kitty.conf options herd needs, and the state read
                 focus.py — re-derive a session's window and jump to it
                 launch.py — `kitten @ launch` for `herd spawn`
completions/     bash completion   ·   bin/herd — the CLI wrapper
tests/           pytest suite (helpers.py + conftest.py + test_*.py per concern)
DESIGN.md        how it works — invariants + current state
DECISIONS.md     why it works that way — what was tried, measured, removed
```

## Roadmap

Navigation is the CLI (`herd jump` fuzzy-picks and focuses, `herd watch` keeps that
picker up as a dashboard), and ambient attention is Claude's terminal bell + kitty's
tab flag (see [Notifications](#notifications-kitty-tab-bell)) — so a dedicated TUI and
a herd-owned notifier are **not planned**; each is handled more cheaply outside herd.
fzf *is* the TUI: it already lists, navigates, and previews live. What's left:

- **More CLI verbs** as needed (`herd kill`), each composing with `herd jump`'s fzf
  picker. Not `herd dismiss` — a jump already acks, and an ack you have to type is
  one more thing to forget.
- *(maybe)* a daemon tab-poke for the one case Claude's bell can't cover — a session
  gone **silently stuck** in `working` (it isn't "done", so it never bells). This is
  the only thing that would put kitty back on the daemon's path, so it stays opt-in.

## Prior art

herd started as a rewrite of *klawde* and deliberately diverges from it: liveness is
derived from `stopped_at` rather than a denormalized flag; the idle signal is the gap
between two clocks rather than a single constantly-stamped one; and every write goes
through canonical SQL rather than statements re-typed into each hook.

## License

MIT — see [LICENSE](LICENSE).
