# quality: how good a Gas City's work is

A Gas City pack with four exec orders and one formula. `quality-metrics` measures per rig and per
local day whether work lands first time, holds after merge, how long it takes and where that time
went. It writes a per-day JSON history and a static page you can put next to any dashboard.
`quality-stops` is a census every 30 minutes: why each bead in flight and each seat is waiting, who
clears it, and for how long (see "Why it's waiting"). After each census it also writes the pack's
first screen, **Today**: what waits on the city's human, who clears every other wait, health,
trends and one cost figure, every row linking into gc's own dashboard (see "Today").
`quality-review` turns the history into a
weekly review: a script writes the week's facts, and one agent session (formula
`mol-quality-review`) proposes one config change by mail, as a decision with a default (see
"Weekly review"). `quality-patrol-gate`, off until a city turns it on, wakes a patrol seat only
when there is something for it to find (see "Patrol gate"). Configuration only: no changes to `gc` or `bd`. No API keys: cost comes from the
transcripts the agents already write, priced from a table in the pack (`assets/prices.toml`).

| Metric | One line |
|---|---|
| Bounce rate | landed beads the refinery sent back at least once |
| Post-merge failure rate | landed beads the post-merge check failed, or that were reopened after landing |
| Fix-of-a-fix | landed beads that drew a bug about them within 14 days |
| First-pass CI | merged PRs whose first head passed every required check |
| Time to land | created → claimed → PR opened → merged → verified, median and p90 |
| Lead-time stages | created → merged of each landed bead cut into blocked, parked, waiting to be routed, routed and waiting for an agent, working, in review, rework |
| Human wait | human-gate beads open, past due, and how long answered ones stayed open |
| Cost per landed bead | list-price dollars (and tokens) of the agents' model calls per landed bead, from their transcripts |
| Owner turns | turns a person typed into the city (the mayor's seat, hand sessions in the city), listed for labelling |
| Machine friction | stalls the city already logs (order failures, stalled sessions, ...) |
| Why it's waiting | every 30 minutes: one typed stop per waiting bead and seat, with who clears it and for how long; stop-hours per class |
| Weekly review | once a week: the biggest slowdown, what last week's change did, one proposed config change |
| Today | after every census, one page: the decisions waiting on the human with what each unblocks, who clears the other waits, health, 7-day trends, cost |
| Patrol census | on demand (`patrols.py`): per patrol seat, how many wakes ran, what each cost, and how many did something besides reading |
| Patrol gate | every 10 minutes, off until a city turns it on: wakes a witness only for a stop it clears (or a 6 h heartbeat), submits gc's own stuck prompts, tells the deacon about dialogs and the disk |

## Use it in a city

1. Import the pack, pinned, in the city's `pack.toml`:

   ```toml
   [imports.quality]
   source = "https://github.com/alexknips/gas-city-setup.git//packs/quality"
   version = "sha:<commit>"
   ```

2. Optionally set the output and the city's conventions in `city.toml` (every key below is
   optional; the order runs with the defaults):

   ```toml
   [[orders.overrides]]
   name = "quality-metrics"
   [orders.overrides.env]
   QUALITY_OUT_DIR = "/srv/dashboard/quality"     # where the page and the JSON go
   QUALITY_HUMAN_PREFIXES = "HUMAN:,ALEX:"        # title prefixes of human-gate beads
   QUALITY_PRICES = "/srv/city/prices.toml"        # your own token prices, merged over the pack's

   [[orders.overrides]]
   name = "quality-review"
   [orders.overrides.env]
   QUALITY_REVIEW_AGENT = "dog"                    # who writes the review; empty = facts only, no tokens
   QUALITY_REVIEW_MAIL_TO = "mayor"                # who gets it, with a ready-to-file decision
   ```

   The review and census orders find the output directory the metrics order wrote last
   (`$QUALITY_STATE_DIR/out-dir`), so `QUALITY_OUT_DIR` need not be set twice. The census also
   takes the metrics order's conventions (human-gate prefixes and due key, verify labels, hold and
   route keys, parked statuses) from its `quality.json`, so they are set once, on `quality-metrics`.
   The census's own settings are optional; a city whose human gates name a person sets
   `QUALITY_STOP_HUMAN` on a `quality-stops` override. The Today page needs nothing more. When a
   browser reaches gc's dashboard at another address than the supervisor's own (a tailnet name, a
   proxy), set `QUALITY_GC_DASHBOARD_URL` on that override so the page's links open; a city that
   adds its own nav with `QUALITY_POST_RENDER` sets it there too (`{name}` is `today` there and
   `quality` on the metrics order).

3. `gc import install` (fetches the pinned pack and records it in `packs.lock`), `gc reload`, then `gc order list` shows `quality-metrics` and `gc order run quality-metrics`
   runs it now. The first run is the backfill of the last 28 days; after that it runs once a day
   (see "When it runs").

To see the numbers without writing anything: `$PACK_DIR/assets/scripts/quality-metrics.sh --dry-run`
(the path is in `gc order show quality-metrics`). To see how one bead was counted, with its
events and its stage timeline: `... --bead <id>` (reads only that bead's rig, 10-60 s). To see one day's cost per seat, transcript and model: `... --cost <YYYY-MM-DD>`
(reads the transcript cache the order keeps; without it, all transcripts in the window).

## Settings

All environment variables of the order: the pack sets the first three in `[order.env]`; a city
sets or changes any of them with `[[orders.overrides]]` + `[orders.overrides.env]`.

| Variable | Default | Meaning |
|---|---|---|
| `QUALITY_DAYS` | `28` | days each run recomputes and the page shows |
| `QUALITY_FOLLOW_DAYS` | `14` | window after landing for reopens and fix-of-a-fix |
| `QUALITY_MAX_LOAD` | `15` | skip the run (and log it) when the 1-minute load is above this |
| `QUALITY_OUT_DIR` | `$GC_CITY_PATH/.gc/runtime/quality` | `index.html`, `quality.json`, `history/<day>.json` |
| `QUALITY_STATE_DIR` | `$GC_CITY_PATH/.gc/runtime/quality-cache` | caches and `runs.log` |
| `QUALITY_RIGS` | all rigs with beads | comma list of rig names to measure |
| `QUALITY_REPOS` | `git remote origin` of each rig | `rig=owner/repo` pairs; `rig=none` turns GitHub off for a rig |
| `QUALITY_GH` | `auto` | `off` skips GitHub (first-pass CI, PR-opened times) |
| `QUALITY_LANDED_RESULTS` | `merged,pull_request` | `metadata.merge_result` values that mean landed |
| `QUALITY_VERIFY_OK_LABELS` | `verify:live` | labels a post-merge check adds when the work holds |
| `QUALITY_VERIFY_FAIL_LABELS` | `verify:failed` | labels it adds when it does not |
| `QUALITY_FOF_TYPES` | `bug` | bead types that can be a fix-of-a-fix |
| `QUALITY_FOF_EXCLUDE` | pre-existing / older bug / not caused by / old build ... same | a follow-up whose text matches is not counted |
| `QUALITY_FOF_CUES`, `QUALITY_FOF_NOT` | see `quality.py` | the causal words a mention needs, and phrases that cancel it |
| `QUALITY_GATE_BOUNCE_RE` | `pre-?existing` | a bounce reason that matches was caused by the gate, not the change |
| `QUALITY_HUMAN_PREFIXES` | `HUMAN:` | title prefixes of human-gate beads |
| `QUALITY_HUMAN_DUE_KEY` | `human.due` | metadata key with a gate's due time (also marks a gate) |
| `QUALITY_FRICTION_EVENTS` | six event types, see below | city event types counted as friction |
| `QUALITY_TRANSCRIPTS` | `$CLAUDE_CONFIG_DIR/projects`, else `~/.claude/projects` | comma list of Claude Code transcript directories the agents write to |
| `QUALITY_OWNER_SEATS` | `mayor` | comma list of seats a person types into; a seat matches with or without its rig or binding prefix (`mayor` matches `gastown.mayor`) |
| `QUALITY_INJECTED_RE` | see `quality.py` | a user turn whose text matches was put there by gc or Claude Code, not typed |
| `QUALITY_PRICES` | unset | a TOML price table in the shape of `assets/prices.toml`; its models replace the pack's one by one |
| `GC_BIN` | the gc that started the order, else `gc` on PATH | the gc binary every order calls (see "Traps") |
| `QUALITY_POST_RENDER` | unset | shell command run after the page is written; `{page}`, `{out}` and `{name}` (`quality`) are substituted |
| `QUALITY_TZ` | host zone | zone of the day buckets |
| `QUALITY_EVENTS_TZ` | read per event from a timeline | one zone for every `events.created_at`, skipping the timeline (see the time trap below) |
| `QUALITY_PARKED_STATUSES` | `blocked,deferred` | bead statuses that count as parked in the lead-time stages |
| `QUALITY_HOLD_KEYS` | `hold_reason` | metadata keys whose non-empty value parks a bead (the text is the reason shown) |
| `QUALITY_ROUTE_KEYS` | `gc.routed_to` | metadata keys whose non-empty value means the bead is routed (an assignee counts too) |
| `QUALITY_DOLT_PORT`, `_HOST`, `_USER`, `_PASSWORD` | `GC_DOLT_PORT`, 127.0.0.1, root, empty | the city's Dolt server |

Settings of the `quality-review` order (the pack sets the first five in its `[order.env]`):

| Variable | Default | Meaning |
|---|---|---|
| `QUALITY_REVIEW_WEEKDAY` | `1` | ISO weekday of the review (1 = Monday); the reviewed week is the 7 local days before it |
| `QUALITY_REVIEW_HOUR` | `7` | local hour on that day from which the review is due |
| `QUALITY_REVIEW_AGENT` | empty | agent the formula is slung to (`gc sling <agent> mol-quality-review --formula`); empty = facts only |
| `QUALITY_REVIEW_MAIL_TO` | `mayor` | who gets the review and the ready-to-file decision |
| `QUALITY_MAX_LOAD` | `15` | skip (and log) when the 1-minute load is above this |
| `QUALITY_REVIEW_WORDS` | `300` | word limit of the review |
| `QUALITY_REVIEW_BEFORE_DAYS` | `21` | days before the reviewed week it is compared with (per week) |
| `QUALITY_REVIEW_CONFIG_REPOS` | the city dir | comma list of git repos whose commits the facts list |
| `QUALITY_REVIEW_OPEN_STATUSES` | `open,in_progress,blocked,deferred` | statuses of the "work open now" section |
| `QUALITY_OUT_DIR`, `QUALITY_STATE_DIR`, `QUALITY_TZ` | as for `quality-metrics` | the same history, caches and day buckets |

Settings of the `quality-stops` order (the pack sets the first two in its `[order.env]`):

| Variable | Default | Meaning |
|---|---|---|
| `QUALITY_STOPS_INTERVAL_MIN` | `30` | the order's `interval` in minutes: each census stands for this long in the stop-hours |
| `QUALITY_MAX_LOAD` | `15` | above this 1-minute load only the census's host part runs (and is logged) |
| `QUALITY_STOP_LOAD` | `QUALITY_MAX_LOAD` | the load above which the host is a stop |
| `QUALITY_STOP_GRACE_MIN` | `20` | routed work younger than this, a seat down for less, and mail unread for less are not stops |
| `QUALITY_STOP_IDLE_MIN` | `60` | an assignee whose session has done nothing for this long is idle |
| `QUALITY_STOP_RESETS` | `3` | stalled-session resets in one local day that make a seat a stop |
| `QUALITY_STOP_VERIFY_H`, `_DAYS` | `30`, `3` | a landing without a verify label is a stop after these hours, until it is these days old |
| `QUALITY_STOP_SKIP_TYPES` | `session,convoy,molecule,message` | bead types that are not work in flight (a skipped type can still block) |
| `QUALITY_STOP_HUMAN_ONLY_TYPES` | `epic,milestone` | containers: read, but a stop only when a person acts next (see "Classes") |
| `QUALITY_STOP_REVIEWERS` | `refinery` | agent names (without rig and binding) that review and merge |
| `QUALITY_STOP_WATCHERS` | `witness` | agent names that wake or reclaim a rig's seats; who clears a down or idle seat |
| `QUALITY_STOP_ROUTER` | `mayor` | who routes, applies config and holds what nobody owns |
| `QUALITY_STOP_HUMAN` | `owner` | the person behind a human gate nobody is assigned to |
| `QUALITY_STOP_DEPLOYER` | `post-merge check` | what labels a landing verified |
| `QUALITY_GC_API` | what `gc dashboard --no-open` prints | gc's API base URL, read once per run for the city's health (Today); `off` = none |
| `QUALITY_GC_CITY_NAME` | the city `/v0/cities` lists at this city's path | the city's name in gc's API and dashboard links |
| `QUALITY_GC_DASHBOARD_URL` | the API URL | gc's dashboard as a browser reaches it; Today links to `<it>/city/<city>/beads?bead=<id>` |
| `QUALITY_HUMAN_NAME` | `QUALITY_STOP_HUMAN` | how Today names the human |
| `QUALITY_POST_RENDER` | unset | as for `quality-metrics`, run on `today.html` with `{name}` = `today` |
| `QUALITY_HUMAN_PREFIXES`, `_DUE_KEY`, `QUALITY_VERIFY_*`, `QUALITY_HOLD_KEYS`, `QUALITY_ROUTE_KEYS`, `QUALITY_PARKED_STATUSES`, `QUALITY_LANDED_RESULTS` | the metrics order's, from its `quality.json` | set here only to differ |
| `QUALITY_OUT_DIR`, `QUALITY_STATE_DIR`, `QUALITY_TZ`, `QUALITY_DAYS` | as for `quality-metrics` | the same page, caches, day buckets; `stops/<day>.jsonl` is kept `QUALITY_DAYS` days |

## What a run does and costs

`quality-metrics.sh` checks the load gate, then runs `quality.py` under `nice -n 10` and `ionice -c 3`.
`quality.py` makes one `gc rig list --json` call, then reads each rig's beads with plain SQL on the
city's Dolt server (`pymysql`; without it, `bd sql --json` per query), plus `dolt_log` around the
few events whose zone the timeline leaves open (see the time trap). It reads GitHub with `gh`, and
the city event log and the agents' transcripts incrementally from a byte offset. Every run
recomputes the whole window, so late signals (a reopen, a bug filed a week later) land on the right day.

Measured in the city it was built in (8 rigs, 263 landed beads and 229 merged PRs in 28 days): the first
run took 146 s, almost all of it 252 `gh` calls to backfill the PRs (442 s for the live first
run at load 11.6); a daily run takes about 13-20 s wall and 4 s CPU, 17 `gh` calls. A merged PR's
verdict never changes, so it is cached for good. The transcripts are the other cold cost: the
first run reads every transcript of the window, here 3.7 GB in 4,516 files, in 80-98 s wall and
51-54 s CPU at load 13-19. After that a run reads only what was appended and the cost part takes
4-7 s wall, about 3 s CPU, mostly loading and writing its 17 MB cache
(`$QUALITY_STATE_DIR/transcripts.json`); a whole cached run took 14-16 s wall, 6-8 s CPU. Owner
turns come from the same pass and add 0.4 MB to that cache (2,589 typed turns of every seat in 28
days, 400 characters each at most). The cache's format changed with them, so the first run after
that upgrade reads every transcript again: 654 s wall in a test run at load 40-100 under `nice`,
inside the order's 15-minute timeout; the load gate keeps a live run from starting that high.

The lead-time stages read each landed bead's whole event history. An update's `old_value` is the
whole row, so the first read is large (here 26 MB for the busiest rig's 221 landed beads in 28
days); events are only ever added, so the order caches each bead's extracted state changes in
`state.json`, keyed by its event count and newest event, and a later run reads only the beads
that changed. Measured on 2026-10-26 (259 landed beads, 30 `blocks` dependencies): the cold read
took 9.0 s wall and 1.9 s CPU at load 13, a cached one 2.6 s wall and 0.7 s CPU at load 8 (one
count query per rig), the split itself 0.6-1.9 s; the cache is 0.9 MB.

The weekly review's facts step reads the history files, one `gc rig list`, the open beads and
`git log`: 4.4 s wall at load 6-14, 8.8 s for the whole order run at load 11, 17 s wall (3 s CPU)
at load 41.

### When it runs

The order fires hourly (`trigger = "cooldown"`, `interval = "1h"`). Only the first successful run
of a local day (`QUALITY_TZ`) does the work: it writes the day to `$QUALITY_STATE_DIR/last-ok-day`,
and later fires that day exit at once without a `runs.log` line. A run skipped for load writes no
stamp, so the next fire, an hour later, tries again. The page is therefore fresh from the first
quiet hour after midnight.

- Run again the same day: `QUALITY_FORCE=1 gc order run quality-metrics`. A plain
  `gc order run quality-metrics` on a day that already ran exits at once.
- `--dry-run` and `--bead` always run and never write the stamp. `--bead` and `--cost` also skip
  the load gate (one bead or one day: the weekly reviewer's hand checks must not silently skip).
- Retry less often: `interval = "3h"` in the city's `[[orders.overrides]]`.

Why a cooldown and not a cron like `15 6 * * *`: on 2026-10-25 the first 06:15 slot never fired
here (no `order.fired`), although the controller dispatched other orders in that minute. The
likely cause, from the gc 1.4.1 source: the controller reads an order's last run once, when it
first evaluates it, and later only its own dispatches move it (a manual `gc order run` does not).
With no last run a cron order is due only in its exact minute, with no catch-up, and a dispatch
tick stops after it has fired 4 orders, so one order can go a whole minute unevaluated. A
cooldown is due at the first evaluation after the interval, whatever minute that is. gc 1.4.1
cron also has no ranges: `6-23` never matches (1.5.1 has them).

Output (`QUALITY_OUT_DIR`):

- `history/<day>.json`: one file per local day: per rig the counts, every landed bead with how it
  was counted (incl. its priority, its stage hours and why), the PRs merged that day, human gates
  and friction; city-level friction and cost (dollars, tokens by kind, unpriced tokens, and the same
  per model and per seat).
- `quality.json`: 7-day and 28-day windows per rig and for all rigs (incl. the stage split), daily
  series, sources.
- `reviews/`: the weekly review's `<week>.facts.json` and `.facts.md` (the script), `<week>.md`
  and `<week>.json` (the reviewer), and `index.json` (the page's Weekly review card).
- `index.html`: the page. It reads `quality.json` and `reviews/index.json` and says "no data" (and
  why) where a source has none.

## The metrics

"Landed" means status closed with `metadata.merge_result` in `QUALITY_LANDED_RESULTS`; a landed bead
belongs to the local day of its `closed_at`. Rates are shares of the beads that landed in the window.

### Bounce rate

- **Definition**: landed beads with at least one bounce / landed beads. Also shown: the number of
  bounces and how many were caused by a gate.
- **Source**: the beads `events` table. A bounce is an event whose new value sets
  `metadata.rejection_reason` to a non-empty value different from the old one. The gastown refinery
  sets it when it sends work back and unsets it on merge, so the current metadata never shows it;
  only the event history does.
- **Blind spots**: a refinery that rejects without `rejection_reason` is not seen. Which bounces were
  caused by the gate rather than the change comes from the reason's text (`QUALITY_GATE_BOUNCE_RE`).

### Post-merge failure rate

- **Definition**: landed beads that carry a fail label, or were reopened while landed within
  `QUALITY_FOLLOW_DAYS` of a close / landed beads. Check coverage = landed beads with any verdict
  label / landed beads.
- **Source**: `labels`, and `reopened` events whose old value had a landed `merge_result`.
- **Blind spots**: only as good as the post-merge check; with low coverage the rate is mostly reopens.
  A check that later replaces `verify:failed` with `verify:live` hides the failure from the labels,
  which is why reopens count too. Landings less than 14 days old can still fail.

### Fix-of-a-fix

- **Definition**: landed beads that drew at least one bug within `QUALITY_FOLLOW_DAYS` / landed beads.
  A bug counts against a landed bead when it is `discovered-from` it, or when its text names the bead's
  id in a sentence that also says it shipped, merged, caused, broke or left the problem (the cue words).
- **Source**: beads of `QUALITY_FOF_TYPES` created after the landing, their title and description,
  and `discovered-from` dependencies.
- **Blind spots**: a text heuristic, tuned for precision. A bug that says the problem is pre-existing,
  an older bug or "not caused by" the bead is left out; an id inside a path is not a mention. A mention
  without a cue word is not counted, so the rate undercounts. Recent landings are still maturing.

### First-pass CI

- **Definition**: merged PRs whose first head passed every required check on its first try / merged
  PRs whose first head is known. Per rig = per repository, by the local day the PR merged.
- **Source**: `gh`: merged PRs; the branch's required checks from rulesets, else branch protection,
  else every check on the head; the first `pull_request` workflow run per PR gives the first head; that
  head's check runs (earliest run per check name, so a re-run of a red check is not a first pass) and
  commit statuses.
- **Blind spots**: uses today's required checks for older PRs. A PR whose first head ran no
  `pull_request` workflow is unknown. A red required check that was already red on the base branch
  still counts against the PR. Rigs without a GitHub remote have no data.

### Time to land

- **Definition**: hours per stage, median and p90 over the window: created → claimed, claimed → PR
  opened, claimed → merged, merged → verified, created → merged.
- **Source**: `issues.created_at`, `issues.started_at` (else the first `claimed` event),
  `issues.closed_at`; the bead's first PR's `createdAt` (matched by `metadata.pr_url`, `branch`, or a
  head branch ending in the bead id); the `label_added` event of a verify-OK label.
- **Blind spots**: created → claimed includes time a bead was deliberately parked or blocked (the
  lead-time stages below say which). A bead reopened and landed again is measured to its last close.
  Merged → verified exists only where the check's label event was recorded.

### Lead-time stages

- **Definition**: each landed bead's created → merged (its last close) cut into seven stages that are
  exclusive and add up to it. At each moment the first that holds wins:
  1. **blocked**: a `blocks` dependency on a bead that is still open;
  2. **parked**: status in `QUALITY_PARKED_STATUSES`, a non-empty `QUALITY_HOLD_KEYS` value, a
     `defer_until` still ahead, or status closed before the first claim;
  3. before the first claim: **routed** (waiting for an agent) when a `QUALITY_ROUTE_KEYS` value or
     an assignee is set, else **unrouted** (waiting to be routed);
  4. after the first claim: **working** until the bead's first PR opens, **review** from then until
     the first bounce or the merge, **rework** from the first bounce or the first reopen after the
     claim until the merge.

  Per window, rig and stage: total hours, share of all created → merged hours, how many beads spent
  time in it, the median and p90 over those beads, and the five beads with the most, each with why
  (the blocker, the hold reason, the route target). `--bead <id>` prints the bead's timeline. Every
  run checks that every bead's stages add up to its created → merged (within 0.01 h) and names any
  that do not in `quality.json` → `sources.stages.not_adding_up`.
- **Source**: the beads `events` table. An event's `old_value` is the whole row before it (an empty
  field is left out), so the state between two events is the next event's `old_value`; `new_value`
  holds only what changed, and its metadata only the changed keys. `dependencies` of type `blocks`
  (incl. `depends_on_external` ids of other rigs; `created_at` is in the same zone as the events and
  read through the same timeline) and the blocker's `closed_at` from the store its prefix names. The
  first PR's `createdAt` from GitHub, bounces from `rejection_reason`, the claim from `started_at`.
- **Blind spots**: a dependency deleted before the run is not seen (bd keeps no event for it). A
  blocker still open (or not found) when the bead was claimed counts only up to the claim, since the
  work went ahead. An assignee before the claim counts as routed even when it is a person. A claim
  given back to the pool is still working, not waiting. Without a known PR there is no review stage:
  working runs to the first bounce or the merge. Events in the same second are ordered creation
  first, close last, else as stored. Only landed beads are split, so work still waiting is not in it;
  the weekly review lists the open work separately.

### Human wait

- **Definition**: per rig and day, human-gate beads open at the end of the day, how many of those are
  past their due time, and how long the gates answered that day stayed open.
- **Source**: beads whose title starts with a `QUALITY_HUMAN_PREFIXES` prefix or whose metadata has
  `QUALITY_HUMAN_DUE_KEY`; due time from that key, else `due_at`.
- **Blind spots**: work waiting behind a gate is not linked to it in the bead graph, so the time other
  beads spend blocked on a person is not measured, only the gate's own open time.

### Cost per landed bead

- **Definition**: per local day, the list-price dollars of every model call the city's agents
  made / the beads this pack counts as landed that day (all rigs). The window value divides the
  dollars of every covered day by the beads landed on those days. Tokens per landed bead (all four
  kinds, and output alone) are shown next to it: on a subscription the dollars are what the same
  calls would cost on the Claude API, not what the city pays.
- **Source**: the agents' Claude Code transcripts (`QUALITY_TRANSCRIPTS`). Each assistant line
  carries its message's `usage`: uncached input, cache writes split into 5-minute and 1-hour,
  cache reads, output, `speed`, `inference_geo`, `service_tier` and web searches.
  - **Which transcripts**: those whose launch directory (the first `cwd` in the file) is the
    `work_dir` of a gc session bead, or a directory below one (a worktree an agent made). Session
    beads are read from `issues` and, for gc 1.5 and later, `wisps`; every dir seen is kept in
    the state, because wisps can be purged. A seat is the session bead's `template`. Subagent
    transcripts (`<session>/subagents/*.jsonl`) count with their session.
  - **Each message once**: Claude Code writes one line per content block, so an id repeats; older
    versions wrote a partial output count on the earlier lines, so the line with the largest
    output count is kept. An id seen in two files counts once. A message belongs to the local day
    of that line's timestamp.
  - **Prices** (`assets/prices.toml`, from the Claude API pricing page, read 2026-10-26): per
    model, uncached input, 5-minute and 1-hour cache writes, cache reads and output, with the fast
    mode and long-prompt rates where a model has them, US-only inference at 1.1x and web search at
    $10 per 1,000. The model id must match exactly (a trailing date is dropped first): there is
    no prefix matching, which once priced claude-opus-5-5 as claude-opus-5's $15/$75 elsewhere in
    this town. A model, speed, tier or geo without a price is counted as unpriced tokens and shown
    by name; it is never priced at $0.
  - **Days covered**: a day counts when the transcripts reach back past it. If the oldest message
    the pack has seen falls inside the window, that day and the days before it show no data. The
    cache keeps every message until it leaves the window, so transcripts Claude Code deletes later
    (`cleanupPeriodDays`, 30 by default) still count.
- **Blind spots**:
  - A lower bound. Claude Code's side calls (WebFetch summaries, compaction, session titles)
    write no usage to the transcript: all 8,883 requests the transcripts logged from 10-24 on were
    main-thread calls.
  - Only Claude Code is read; sessions of another provider (gc's `provider`) are not counted.
  - A `claude` an agent starts outside its work_dir (in a `/tmp` scratch dir) is not counted, and
    nor is a session a person starts by hand, even in the city root to repair the city. Here 3,321
    messages in 104 transcripts of the 28 days were left out: other projects on the host (a
    second city, the dashboard repo, tools) 2,671; a person's sessions in the city root and a
    worktree made from one 530; runs in `/tmp`, 101 of them in agents' scratch dirs, 120 (0.1% of
    the city's 126,777).
  - City-level only: the dogs, the deacon, the mayor and the witnesses work for every rig, so cost
    is not split per rig. The page shows it per seat instead.
  - With `QUALITY_RIGS` set, the dollars are still the whole city's, but only those rigs' beads
    are counted as landed.
  - List prices, not a bill: batch discounts, credits and negotiated rates are not known.
  - A history day older than the window that a pack version before this one wrote holds another
    source's dollars (here the dashboard feed's, $2.22 per landed bead on 09-28 against $45.39
    from transcripts on 09-29). Such a day counts as a day without cost, and the weekly review
    says how many days of each window were priced.

### Owner turns

- **Definition**: per local day, the user turns a person typed into the city: in the seats named by
  `QUALITY_OWNER_SEATS` (default the mayor, the seat the owner talks to) and in Claude Code
  sessions started by hand in the city directory or a rig's directory (not any gc session's
  work_dir). McEvoy's first check for a Gas City ("count how often you ask your own system
  whether it is stuck", *Building a Gas City maturity model*, 2026-10-22) starts from these turns.
- **Source**: the same transcripts as cost, read in the same pass. A user line counts when it is
  not a tool result, a meta line, a compaction summary, a subagent's prompt (sidechain) or a
  headless run, and its text does not match `QUALITY_INJECTED_RE`: system reminders (gc delivers
  queued nudges and mail notices this way, also as one paste), the session's start prompt
  (`[<city>] <seat> • <date>`), task notifications, slash-command and shell output, interruptions.
  A turn is counted once by its uuid (a resumed session copies earlier lines into a new file).
  The first 400 characters of each turn are kept in the cache for `quality.py --owner DAY|all`,
  which lists them with where they were typed; `quality.json` holds only counts.
- **Stuck-asks are counted by hand, not by a rule.** The pack has no rule that tells a stuck-ask
  ("is it stuck?", "how is this going?", "did the deploy work?") from other turns well enough to
  ship. In the city it was built in, phrase rules written from 30 days of the owner's turns found
  all 17 stuck-asks and 10 unjam orders in them, but on a holdout labelled first (181 turns the
  same person typed into another agent setup, 15 stuck-asks and 9 unjam turns) they found 4 and 1.
  So the number is a hand count: `quality.py --owner all`, then label each turn:
  - **stuck-ask**: a question whether work, an agent or the system is moving, done or alive
    (status, progress, liveness: "how far is it", "did you finish", "is the bot still up").
  - **unjam**: an order to fix, restart or nudge something that stopped, or a report that it did
    (a pasted error, "the system is struggling again").
  - **other**: everything else (new work, decisions, questions about content).
- **Blind spots**:
  - Counts turns, not what they ask (above). A person who asks once and waits looks the same as
    one who asks five times.
  - Only Claude Code transcripts on this host: turns in Slack, on the dashboard, in a phone app or
    in another agent's CLI are not read. A seat listed in `QUALITY_OWNER_SEATS` that agents also
    nudge with plain text (not a system reminder) counts those nudges too; the default mayor seat
    gets its nudges as reminders.
  - Claude Code deletes transcripts older than `cleanupPeriodDays` (30 by default). The cache keeps
    turns it has read until they leave the window, so a city that runs the pack daily keeps the
    whole window; a first run sees at most the last 30 days, and the first day it reaches is
    shown as no data, as for cost.
  - A shell command typed with `!` and its output are not counted as turns.
  - City-level only.

### Machine friction

- **Definition**: per day, counts of `order.failed` (per order), `session.reset_stalled`,
  `session.cold_start_timeout`, `session.work_query_failed`, `bead.dead_assignee_reopened`,
  `supervisor.fs_pressure.skipped_tick`, plus bounces caused by a gate.
- **Source**: `$GC_CITY_PATH/.gc/events.jsonl` and its rotated `events.jsonl.archive-*.gz`, read
  incrementally. An event belongs to a rig when its subject or session names it, else to the city.
- **Blind spots**: counts events, not their cost. Wake orders (a nudge to an idle seat) are not
  counted: gc keeps no record of delivered nudges, and `order.fired` only says the order ran.

## Why it's waiting (stop census)

The `quality-stops` order answers "is it stuck, and on whom?" before anyone has to ask. Every 30
minutes it gives every bead in flight one class: why it waits, or that it is moving. Each class
comes with the seat or person who has to act next and how long the bead has waited. It also names
always-on seats that are down, keep being reset or leave mail unread, and the host's load with the
seats behind it. It uses no model and no tokens. It is the measure rung of the pack's next loop
(the town's maturity grid, 2026-10-26: Dispatch and Recover lacked a typed reason and an actor for
work that waits).

### Classes

A bead in flight (not closed, not a `QUALITY_STOP_SKIP_TYPES` type) gets the first class that
applies, in this order (the lead-time stages' order: a blocker, then a hold, then the phase):

| Class | When | Who clears it |
|---|---|---|
| blocked | a `blocks` dependency on a bead still open (a blocker in no store counts as open) | the assignee of the bead at the end of the chain (a blocker that is itself blocked passes the wait on, up to 6 hops), else the router |
| parked | a non-empty `QUALITY_HOLD_KEYS` value, a status in `QUALITY_PARKED_STATUSES`, or a `defer_until` ahead | the assignee, else whoever set the hold (the actor of the event that set it) |
| human_gate_due, human_gate | a title with a `QUALITY_HUMAN_PREFIXES` prefix or a `QUALITY_HUMAN_DUE_KEY`, or assigned to someone who is not an agent; past its due time or not | the assignee, else `QUALITY_STOP_HUMAN` |
| rig_suspended | the bead's rig is suspended | the router |
| review_down, review_idle | assigned to a reviewer (`QUALITY_STOP_REVIEWERS`) whose session is not running, or whose last activity is older than the moment the bead reached it (after the grace time) | the rig's watcher; the reviewer |
| bounced | `rejection_reason` set and nobody assigned | the route target, else the router |
| assignee_down, assignee_idle | assigned to an agent whose session is not running, or has done nothing for `QUALITY_STOP_IDLE_MIN` | the rig's watcher (`QUALITY_STOP_WATCHERS`), else the router |
| pool_suspended, pool_cap, routed_unclaimed | routed (`QUALITY_ROUTE_KEYS`), nobody assigned: the pool or its rig is suspended; every slot is busy (the stop names the beads that hold them); a slot is free but nobody took it within the grace time | the router; the pool; the rig's watcher |
| deploy_failed, landed_unverified | landed in the last `QUALITY_STOP_VERIFY_DAYS` in a rig that uses verify labels: a fail label, or no verdict after `QUALITY_STOP_VERIFY_H` | the router; `QUALITY_STOP_DEPLOYER` |
| seat_down | a `mode = "always"` named session not running for longer than the grace time (its agent and rig not suspended) | the router (the human, when the router is the seat) |
| seat_resets | reset as stalled `QUALITY_STOP_RESETS` or more times this local day | the router |
| seat_mail | mail unread for longer than the grace time while the seat is down, or idle since before it came (mail a seat sent itself, a handoff note, is left out) | the rig's watcher, else the router |
| host_load | the 1-minute load above `QUALITY_STOP_LOAD` | the seat or command using the most CPU |

Moving, listed with who moves it: **working** and **rework** (an agent assigned, its session
active), **review** (with a reviewer that has been active since), **routed** (younger than the
grace time). Routed to a seat that runs under its own name (the mayor, a crew) is that seat's
work, not a pool's: not running is `assignee_down`. **Backlog** (open, not routed, not held, nobody
assigned) is counted per rig and not listed. A name is an agent when gc lists it as an agent or a
session, or it has an agent's or a session's form (`rig/name`, `binding.name`, gc session names
and ids); anything else, an email address included, is a person.

**Containers** (`QUALITY_STOP_HUMAN_ONLY_TYPES`: epics and milestones) are classified like any
bead but kept only when a person acts next. Nobody works on a container, so it is never moving or
backlog, and a hold on it that an agent set is that agent's plan, not a wait. A container's hold
waits on `QUALITY_STOP_HUMAN` when its text has that name as a word
(`alex_judges_redesign_on_preview_and_demo`) and names no other bead still in flight: a milestone
held "until its exit-gate child hivemind-zdsh.21 closes" waits on that child first. A container
never counts as the bead that keeps a pool slot busy.

### Output and when it runs

- `stops.json`: the census now (stops, moving, backlog, every session with its state, last
  activity, unread mail and resets today, the host), the classes with their 7-day stop-hours, and
  the census's coverage. The page's "Why it's waiting" card reads it and follows the rig filter.
- `stops/<day>.jsonl`: one line per run, `[id, class, actor, since]` per stop, kept `QUALITY_DAYS`
  days. Stop-hours come from it: each run stands for the time to the next one, at most two
  intervals. Coverage is the share of the day the runs stood for: "full" (beads and seats) and
  "host".
- `today.json` and `today.html`, the Today page (see "Today"), after every run, full or host-only.
  A Today failure is noted in `stops.log` (`today_error=`); the census is written either way.
- `$QUALITY_STATE_DIR/stops-state.json` (the event-log offset, stalled resets and session stops of
  the last 7 days, the cached gc answers), `today-state.json` (gc's API address and the city's
  name there) and `stops.log` (one line per run).
- The order fires every 30 minutes (cooldown). Above `QUALITY_MAX_LOAD` only its host part runs,
  because a busy host is what a census then has to report: the load, CPU per seat over 2 s, one
  `gc session list` to name the seats. Beads and seats wait for the next fire, and the page shows
  them as of the last full census.
- Cost, measured on 2026-10-26 in the city it was built in (8 rigs, 57 beads in flight, 19
  sessions): a first run 25-29 s wall and 11-13 s CPU at load 19-20, most of it reading the 80 MB
  event log once and three gc calls; later runs 9-14 s wall and 5-6 s CPU at load 17-24, most of
  it the one `gc session list` (6-7 s wall at that load); a host-only run 3.5-4.5 s wall, 2.2-2.5 s
  CPU. 48 runs a day are about 4 CPU minutes. `gc rig list`, `gc agent list` and `gc config show`
  are kept until `city.toml`, `pack.toml`, `packs.lock`, `.gc/site.toml` or
  `.gc/runtime/suspension-state.json` changes (at most 6 h).
- One bead by hand: `quality-stops.sh --bead <id>` prints its row, dependencies, every event with
  its resolved time, and its class. `--dry-run` prints the census and writes nothing.

### Sources

The beads stores (`issues`: status, assignee, `defer_until`, metadata; `dependencies` of type
`blocks`; `events`, read through the pack's event timeline, for when and by whom a hold, route,
bounce or assignee was set), mail beads (open, without `mail.read`), `gc rig list` (stores,
suspension), `gc agent list` (pools and their caps, suspension), `gc config show` (named sessions
and their mode), `gc session list` (state, last activity, work_dir), the city event log
(`session.reset_stalled`, `session.stopped`, `session.woke`, `session.idle_killed`, read from a
byte offset) and `/proc` (load average; every process's CPU read twice 2 s apart and grouped by the
seat whose work_dir holds its cwd, else by command, another user's as `user:command`).

### Blind spots

- A snapshot every 30 minutes: a wait shorter than that can be missed, and stop-hours are an
  estimate from the snapshots. Above the load gate the beads and seats are not read; the coverage
  says how much of a day was.
- A city that is down writes no census: the page then shows the last one as stale. A census that
  should report the city down has to run outside it.
- Who clears a stop is a rule (the table), not a check that they know or can.
- Anything not in beads or sessions: Slack threads, deploys outside beads (a preview build, a
  domain), a seat that is running but hard to find, work that already finished.
- CPU of a process too short-lived for a 2 s sample is counted when its parent reaps it, to the
  parent's seat (at most one core per seat, since a long child's whole life lands at once). The
  system-wide ticks in `/proc/stat` are not used: on the host this was built on they added up to
  about half the wall time (only their ratios hold, which is what `top` shows). Over the same 4 s
  windows (2026-10-26) the census counted 5.8, 8.8 and 8.2 cores in use where `top`'s CPU line said
  6.1, 8.9 and 6.3: a long child that ends inside the sample can still push it up.
- `last_active` is gc's, so idle classes are only as good as its activity tracking.
- A metadata key the city uses for a hold other than `QUALITY_HOLD_KEYS` (here hc-fav.2's
  `waiting_on`) leaves the bead in the backlog.

### Backtest (rung 2)

`stops_replay.py` rebuilds the census at a past instant, with the census's own rules, from what the
city keeps anyway:
- each bead's state from its events (an event's `old_value` is the whole row before it);
- which sessions were up, from the event log and its archives;
- each pool's cap, from the git history of `city.toml`;
- the load, from sysstat when the host keeps it.

It cannot see:
- last activity, so no idle classes;
- rig and agent suspension (runtime state with no history);
- unread mail;
- post-merge verdicts;
- which seats were always-on then;
- the processes behind a past load.

`--at ISO [--bead ID]` prints one census and `--from DAY --to DAY` the stop-hours per day. The 7
days of W43 (336 censuses over 699 beads) took 325 s wall, 166 s CPU and 89 MB.

The test, set before it ran (bead gcs-lqd): of the 27 turns in which the owner asked whether
something was stuck or ordered an unjam in the 30 days to 10-26 (hand-labelled, see "Owner turns"),
15 are about things a census could see; the other 12 are the city being down (8) and Slack threads
(4). The census passes if, at the census slot before each ask, it names at least 12 of the 15
typed and with an actor. The per-turn reading of the 15 differs slightly from gcs-ybt's totals:
CPU 2, mayor liveness 2 and the nudge 1 here, where gcs-ybt counted CPU 4 and mayor 1, and the
nudge under progress.

| Ask (UTC) | About | Census before it said | Named |
|---|---|---|---|
| 09-28 02:40, check on the mayor | the mayor after a reset | 02:30: `seat_down` gastown.mayor, asleep 1.3 h (every seat down after the reset) | yes |
| 09-28 03:11, is the public website fixed | the site's beads | 03:00: hc-8h3 parked, part (a) shipped, /demo waits on hivemind-2cde; hc-fav.11 open with nobody on it (backlog, not listed) | partly |
| 09-29 05:25, the host is struggling (CPU) | host load | no load history (sysstat keeps 8 days); the mayor measured 48.4, 15-min 34.8, a minute later | not checkable |
| 09-29 05:28, how many polecats per rig, does it work | pools and their work | 05:00: hivemind pool at its cap on hivemind-ydmp (rework) with 2 waiting, hivemind-ui's at its cap on hu-myj5, hc-fav.6.1 working | yes |
| 10-02 23:41, how is it going | the queue just routed | 23:30: hivemind pool (cap 1) on hivemind-md0y, 12 beads waiting 15-38 h; m306.4.3 bounced; hivemind-8606 human gate 16.5 h past due | yes |
| 10-03 16:09, is the work almost done | open work | 16:00: 14 at a pool cap, 2 working, 12 blocked, 16 parked, 10 backlog | yes |
| 10-03 16:20, check the CPU usage | host load | no load history; the mayor measured 29.5, 15-min 25.1, a minute later | not checkable |
| 10-03 19:20, progress of the benchmark polecats | hmd-9dd, hmd-o17 | 19:00: both working 1.8 h on their slots, hmd-0ao waiting at the cap of 2 | yes |
| 10-08 03:15, a pasted preview-build failure: solved yet? | a preview build of branch screenshots/hc-fav.6.9 | 03:00: hc-fav.6.9 working; the build is not in beads | no |
| 10-08 15:35, is the new domain live yet | a domain waiting for Vercel's check | nothing: not in beads | no |
| 10-14 00:32, cannot see the new session | a new session he could not find in tmux | 00:30: the session running | no |
| 10-15 15:51, nudge the crew | the hivemind crew and its mail | 15:30: the crew running; no unread mail to it 12:00-16:00Z in the store | no |
| 10-20 16:15, how is the quality work going | quality-bar beads | none in flight | no |
| 10-23 01:38, status of the repo merge | hivemind-21yg5, -89vgq, -4ruxj | all closed by 10-22 | no |
| 10-23 02:02, is the mayor stuck | the mayor, 24 min into a long turn | 02:00: the mayor running; load 8.5 (sysstat) | no |

**Result: fail.** Named 5 of 15, partly 1, not checkable 2. Even with the partial and both CPU
asks counted, that is 8, under the 12 set. Where it names, it is the dispatch side: pools at their
cap and what holds them, queues, working slots, seats down. Where it does not, the owner asked
about something no bead or seat was waiting on: a deploy outside beads, finished work, a seat that
was busy. The seat list now on the page shows a running seat with its last activity. That
answers two of the misses (is the mayor stuck, cannot see the session), but it was added after these asks
were read, so it is not counted. Whether such a list would have spared an ask is a shadow-rung
question, not a replay question. So the loop does not climb on this evidence: no shadow ask seat
is proposed. The census stays a measure: the Dispatch and Recover stops typed, with an actor and a
time.

The same replay over W43 (10-19 to 10-25), stop-hours per class:

| Class | Hours | Mostly |
|---|---|---|
| parked | 5,330 | 32 beads on hold at any time |
| blocked | 2,224 | 13 beads at any time (on 10-26: 9 in the hc launch chain behind hc-fav.2, 3 behind hivemind-m306.6) |
| pool_cap | 760 | 680 h on 10-19: the 10-18 scan batch behind the hivemind pool's cap of 1 |
| routed_unclaimed | 574 | gcs-qc8, -f70, -jc0, -vlb, routed to a rig that was suspended (the replay cannot see suspension; live they are `pool_suspended`), parked on 10-24 |
| human_gate_due | 276 | gc-xroei, past due since 10-20 |
| host_load | 34 | load over 15, from sysstat |

## Weekly review

Once a week the city gets a short review: where the last week's lead time went (hours, with named
beads), what the previous review's change did, and one proposed config change as an exact diff,
mailed to `QUALITY_REVIEW_MAIL_TO` with a ready-to-file decision (options, default, why, cost if
wrong). The coordinator files the decision for the city's human and applies the diff the city's
usual way. The pack never applies anything.

It has two halves and a fence between them:

- **The facts are a script** (`quality_review.py`, no model, no tokens). It reads the per-day
  history the metrics order wrote and compares the reviewed week (the 7 local days before the
  review day) with the `QUALITY_REVIEW_BEFORE_DAYS` before it, per week. It writes
  `reviews/<week>.facts.json` and a Markdown rendering `reviews/<week>.facts.md` with the same
  numbers under the same names: the stage split (all rigs and per rig, median and p90, top beads with
  why, routed wait by target and by bead priority, parked by reason, blocked by blocker, the beads
  with the most rework and their bounce reasons), "filed together" (beads created in the same clock
  hour that waited in one stage: a burst can be most of a sum while the median stays small), every
  landed bead of the week with its stage hours (`beads`, JSON only), every metric of the page as a named key (`all.stage.routed.h`,
  `hivemind.bounce_rate`, ...) with this week, before and the change, cost by seat, the config
  repos' commits in the week and since, the work open now (not in the landed numbers), and the last
  review's change.
- **The review is one agent session** (`QUALITY_REVIEW_AGENT`, formula `mol-quality-review`, a
  root-only wisp so a pool can take it). It picks the biggest slowdown a config change can reduce
  and says why bigger ones were passed over, checks two or more of its beads by hand
  (`quality-metrics.sh --bead`), finds the cause in the config (`gc config explain`, the git log of
  the file), and proposes one change: a diff that `git apply --check` accepts in the city, the
  metric key it should move, from what to what, cost and undo. It writes `reviews/<week>.md`
  (at most `QUALITY_REVIEW_WORDS` words) and `reviews/<week>.json`, rebuilds `reviews/index.json`,
  and mails the coordinator.
- **The fence**: the review's output is a proposal, never an action; it may not touch anything but
  its own wisp. It may not propose a change to the review itself (this pack's orders, formula,
  scripts or settings). A change touching money or spend, scope, releases, merges, credentials or
  architecture is charter-level and its decision defaults to "No change". And the loop is closed by
  the script, not by the reviewer: next week's facts check with git whether the previous diff is in
  the city (`git apply --check --reverse`: applied; `--check`: not applied; neither: unknown) and
  read the named metric from both weeks' facts files. It calls the move "as expected" only when the
  change was applied and the metric moved the wanted way; the review also records what it expected
  without the change (`expect_without`), because work comes in bursts and a metric can fall on its
  own.

When it runs: hourly on a cooldown, like the metrics order; it does the work on the first fire
after `QUALITY_REVIEW_WEEKDAY` at `QUALITY_REVIEW_HOUR` (local) once that day's quality-metrics run
succeeded (`last-ok-day`), and records the week in `$QUALITY_STATE_DIR/review-last-week`. A load
skip or a failed sling is retried an hour later. `QUALITY_FORCE=1 gc order run quality-review` runs
it again. `quality-review.sh --dry-run` prints the due week's facts and writes nothing;
`quality-review.sh --week 2026-W42` writes a past week's facts without slinging (to try a reviewer
on evidence cut before the outcomes were known).

The page's Weekly review card shows the latest review and, per week, the biggest stage from the
facts, the slowdown and change the review named, the decision's default, and next week's check:
applied or not, and the metric then and now.

Cost: the review is one agent session. A backtest of the formula on week 2026-W43's facts (same
text, a scratch output dir, the mail written to a file) took 43 Opus 5.5 calls, 3.7M cache-read and
38k output tokens: $2.13 at list price, priced from its transcript with this pack's table; it ran
11 min, mostly waiting for the host load to fall. The facts step costs no tokens.

Blind spots: "filed together" groups by clock hour, so a burst that straddles an hour shows as two.
"Applied" sees only the exact diff in the first config repo; a change made another way
shows as unknown. One week is a small sample, so a metric's move after a change can be noise or
another cause; the review is asked to say so. The facts cover landed beads plus a snapshot of open
work, not the history of work still open. The review is as good as the agent that writes it: its
numbers can be traced (every one must come from the facts or a command it names), its judgement
cannot.

## Today (the first screen)

`today.html` answers what the person running the city asks when they open it, in this order
(`docs/dashboard-design.md` in this repo): what needs me, what is stuck and who clears it, is the
town healthy, is it getting better, what does it cost. It is a static page. The census order writes
`today.json` next to it after every run (`today.py`), from the files the pack already writes and
one read of gc's API. The browser makes no call to gc, so the page still loads when the supervisor
is down. Acting on a row is one click into gc's own dashboard, which has the claim, close, nudge
and mail actions.

| Block | What it shows | Source |
|---|---|---|
| Summary | decisions waiting on the human, the stops of the actor with the most, host load against the gate, beads landed per day; when each source was read | the blocks below |
| Needs you | every stop whose actor is `QUALITY_STOP_HUMAN`, one card per root: beads blocked behind the same root are listed under it ("Answering it unblocks 3 beads"). Past due first, then gates, holds and blocked work, oldest first. "Open in gc" opens the bead in gc's bead view, a seat in its Agents view | `stops.json` |
| Waiting, and who clears it | every bead and seat stop per actor, split into 7 fixed reason groups (the exact classes on hover); moving and backlog counts | `stops.json` |
| Is the town healthy? | supervisor status, version, uptime; seats running, always-on seats down; host load and its busiest seats or commands; unread seat mail; friction over 7 days | gc's `/v0/city/<city>/health`; `stops.json`; `quality.json` |
| Is it getting better? | landed per day, bounce rate, claimed → merged median, post-merge failure rate, cost per landed bead: the 7-day value against the 28-day one, over the daily series (the last 7 days in the accent colour); the latest weekly review's slowdown, proposal and decision, and the check of the change before it | `quality.json`, `reviews/` |
| What it costs | cost per landed bead over 7 and 28 days, 7-day cost, today's cost as of the metrics run | `quality.json` (transcripts at list price) |

gc's API address is `QUALITY_GC_API`, else what `gc dashboard --no-open` prints. It is kept 6
hours, and looked up again after a failed read. The city's name is the one `/v0/cities` lists at
this city's path. Links go to `QUALITY_GC_DASHBOARD_URL`, else to the API address, which is right
for a browser on the same host.

Cost, measured on 2026-10-26 in the city it was built in (load 23-48): `today.py` 2.8 s wall and
1.0 s CPU with the address kept, most of it Python reading the 490 KB `quality.json`; a run that
looks the address up adds one `gc dashboard --no-open` (6.9 s wall and 1.7 s CPU in all, at load
26).

Blind spots:
- As fresh as its sources: the census (30 minutes, or the last full census while the host is over
  the load gate, which the page says), the metrics (daily), the review (weekly). The supervisor
  line is as of the census run, not live.
- "Needs you" is the census's rule for who acts next, so it misses what the census misses: a
  decision asked only in mail or chat, a hold under a key that is not in `QUALITY_HOLD_KEYS`, a
  container whose hold names the human in other words.
- One cost source. Other figures the town shows for the same day (gc's own estimate, a page that
  prices tokens itself) are not reconciled here.
- The 7-day window counts today so far, as the Quality page does, so landed per day reads low
  early in the day.

## What patrols cost and find (patrol census)

`assets/scripts/patrols.py` answers "what do the patrol seats cost, and how often do they find
something?" with no model call. Run it by hand in the city (it is not an order):

    python3 <pack>/assets/scripts/patrols.py                      # the last 7 local days
    python3 <pack>/assets/scripts/patrols.py --since 2026-10-22 --until 2026-10-29 --json wakes.json
    python3 <pack>/assets/scripts/patrols.py --seat hivemind/gastown.witness   # one line per wake

It reads the metrics order's state (`QUALITY_STATE_DIR`), transcripts (`QUALITY_TRANSCRIPTS`) and
prices (`QUALITY_PRICES`), plus `QUALITY_PATROL_ROLES`, `QUALITY_PATROL_WAKE_RE` and
`QUALITY_PATROL_CONTINUE_RE` below. A 7-day run read ~150 MB of transcripts in 6.6 s wall and
6.1 s CPU (load 8). It writes nothing unless `--json` is given.

- **Patrol seats** are the session dirs in the metrics state (`session_dirs`, written by
  `quality-metrics`) whose seat's base name is in `QUALITY_PATROL_ROLES` (default
  `witness,deacon,boot,dog`).
- **A wake** starts at a turn gc, an order or another agent put into the session (a patrol prompt,
  a nudge, a fresh start) and runs to the next one. A compaction summary or "usage limit has reset"
  continues the wake before it. The trigger is `patrol` (text matching `QUALITY_PATROL_WAKE_RE`),
  `start` (gc's `[gc] <seat> • <time>` start prompt) or `nudge` (anything else).
- **Cost** is every assistant message in the wake, subagents included, priced like "Cost per
  landed bead" (list price, `assets/prices.toml`).
- **Acted** means the wake ran at least one command that changes something: a nudge
  (`gc session nudge`, `tmux send-keys`), mail (`gc mail send|reply`), a recovery (`gc session
  kill|restart|reset|wake|...`, `git push`), a bead change (`bd update|close|reopen|create`,
  `gc sling`) or `gc order run`. Patrol upkeep does not count: its own patrol wisp (an id whose
  title is `mol-*patrol`, or a shell variable), notes-only updates and appends to `.md` notes.
  **All clear** is a billed wake that did none of these.

Blind spots:
- An action a seat takes inside its own script is invisible (the deacon's `stagger-nudge.sh`
  nudges witnesses from a `systemd-run` unit, so those nudges are not counted as the deacon's).
- "Acted" says that something changed, not that it was a catch: a status report mailed to the
  mayor counts the same as an escalation. Read the `--seat` list or the JSON's `said` (the wake's
  last message) before calling a wake a catch.
- A seat's session dirs come from session beads; a seat whose beads were purged before the
  metrics order first saw them is missing.

Spot checks, 2026-10-29, local days 10-22..10-28 in the city it was built in:
- Dollars per seat against the metrics page's `by_seat` for the same days: within $3.5 for each
  of the 8 seats ($1098 in all).
- All 9 witness wakes counted as acted, read by hand: 9 real actions (2 escalations, 4 FYIs, 2
  relays, 1 refinery nudge). A random sample of 5 all-clear witness wakes: only reads, wisp upkeep
  and notes.
- The 22 deacon wakes first counted as acted, read by hand: 21 real; 1 was a `gc session nudge`
  quoted in a note appended to its handoff file (now skipped). Of the 21, one is the deacon
  resetting its own session at 81% context, not a catch.

## Patrol gate

`quality-patrol-gate` (every 10 minutes, no model, no tokens) wakes a patrol seat only when there
is something for it to find. It exists because a patrol costs the same whether it finds something
or not: in the city it was built in, 578 witness wakes in a week cost $517 at list price and 9 of
them acted (see "What patrols cost and find"). The order ships **off** (`QUALITY_GATE = "off"`).

| Part | Looks for | Does (when live) |
|---|---|---|
| witness | a census stop on a bead in an awake rig whose class is in `QUALITY_GATE_WITNESS_CLASSES` (an assignee down or idle, a reviewer down or idle, a bounce nobody reworks, a route nobody claimed) | nudges that rig's watcher seat once per stop with gc's wake prompt and the stops; otherwise nudges each watcher every `QUALITY_GATE_HEARTBEAT_H` hours, the longest idle first, one per run |
| enter | gc's own wake or start prompt (`QUALITY_GATE_PROMPT_RE`) left unsent in the box of a seat in `QUALITY_GATE_ENTER_ROLES`, idle and the same as on the previous run | presses Enter, looks again 3 s later and presses once more if it is still there; never more than twice per prompt |
| deacon | a dialog open in one of those panes, unsent text that is not gc's, a prompt two Enters did not clear, free disk under `QUALITY_GATE_DISK_MIN_GB` or falling `QUALITY_GATE_DISK_DROP_GBH` or faster over 30+ minutes | nudges the deacon with the findings: when a new one appears, else at most every `QUALITY_GATE_DEACON_EVERY_MIN` |

Each part is `off`, `shadow` (log what it would do; the default) or `live`:
`QUALITY_GATE_WITNESS`, `QUALITY_GATE_ENTER`, `QUALITY_GATE_DEACON`. A trial turns the order on
with every part in shadow next to the city's usual patrols, then compares its log with what the
patrols caught (`patrols.py`). Each run appends one JSON line to `$QUALITY_STATE_DIR/patrol-gate.jsonl`
(what each part found and did); `patrol-gate-state.json` holds what was already woken or pressed.

Safety rules:
- Enter goes only to a pane whose box text matches gc's own prompts, of a seat whose role is in
  `QUALITY_GATE_ENTER_ROLES` (patrol and merge seats, never a crew seat or the mayor), only while
  the pane is idle, only when the same text was there one run earlier.
- Any other text in a box is never touched; it goes to the deacon as a finding.
- When gc lists no session as up (it cannot reach tmux), the witness part wakes nobody, since every
  assignee would read as down and every witness as due.
- The census part waits above `QUALITY_GATE_MAX_LOAD` (40). The quality-stops order skips its bead
  part above 15, and on 2026-10-26 it did so for nearly four hours, so the gate runs its own census
  pass (reading, never writing, that order's state).

| Variable | Default | Meaning |
|---|---|---|
| `QUALITY_GATE` | `off` | `on` runs the order; `off` makes each fire exit at once |
| `QUALITY_GATE_WITNESS`, `_ENTER`, `_DEACON` | `shadow` | per part: `off`, `shadow` or `live` |
| `QUALITY_GATE_WITNESS_CLASSES` | `assignee_down,assignee_idle,review_idle,review_down,bounced,routed_unclaimed` | census classes that wake a rig's watcher |
| `QUALITY_GATE_HEARTBEAT_H` | `6` | a watcher idle this long is woken anyway |
| `QUALITY_GATE_HEARTBEATS_PER_RUN` | `1` | heartbeat wakes per run (patrols that start together load the host) |
| `QUALITY_GATE_WAKE_TEXT` | gc's witness wake prompt | the nudge text; the stops found are appended |
| `QUALITY_GATE_ENTER_ROLES` | `witness,refinery,deacon,dog,boot` | seats whose stuck gc prompt the gate submits |
| `QUALITY_GATE_PROMPT_RE` | `^Run 'gc prime' to check `, gc's start prompt, `[Pasted text #N` | what counts as gc's own prompt |
| `QUALITY_GATE_DIALOG_RE`, `_BUSY_RE`, `_BOX_CHAR` | Claude Code's | how a pane shows a dialog, work in progress and its input box |
| `QUALITY_GATE_DEACON_SEAT` | `deacon` | base name of the seat that takes the findings |
| `QUALITY_GATE_DISK_PATH`, `_DISK_MIN_GB`, `_DISK_DROP_GBH` | `/`, `40`, `15` | the disk rule |
| `QUALITY_GATE_MAX_LOAD` | `40` | above this the census part waits; panes and disk still run |
| `QUALITY_TMUX`, `QUALITY_TMUX_SOCKET` | tmux on PATH or next to gc, `gc` | how the panes are read |

Cost, measured on 2026-10-29 in the city it was built in (load 10): one run 5.4-6.1 s wall and
2.8 s CPU, most of it the census pass and its `gc session list`.

Backtest on the week of 10-22..10-28 in the city it was built in (every acting patrol wake read by
hand; times from bead events, the census log and the agents' own notes):
- gcs-tk5 sat routed to an idle refinery from 17:12Z; the witness's hourly patrol nudged the
  refinery at 17:26Z. The gate's idle-reviewer stop would have woken the witness at 17:22-17:32Z.
  The census order alone would not: it ran host-only from 16:39Z to 20:22Z (load 23-55).
- hivemind-1sanf (bounced twice) was caught by a witness woken by a nudge, which the gate does
  not change.
- gc's prompt stuck unsent in a refinery's or witness's box: the deacon found it 12 times, up to
  77 minutes after it appeared. The gate's rule is the deacon's own (its pane scan), so it
  submits within 10-20 minutes; this cannot be replayed from records (no pane history) and is what
  a shadow trial checks.
- Free disk falling 15-21 G/h on 10-23, 10-25 and 10-26: by the agents' own numbers the disk rule
  fires 30-90 minutes before the deacon's patrol found it.
- Not caught by any rule: one witness judgment escalation (a re-sling watcher about to re-sling a
  bead the owner was driving by hand) and three deacon judgment notes (the mayor stuck on a wait
  loop that matched itself, a deploy unit that read FAILED but had worked, why the town went down).
  These wait for a heartbeat.

Blind spots:
- It wakes for what the census types and the rules above match. What only a reading seat notices
  waits for the heartbeat.
- In a shadow trial, heartbeats rarely show while the city's usual patrols keep the watchers busy.
  At 6 hours they come to 4 a day per awake rig.
- A pane is read as Claude Code draws it today (the box character, the dim placeholder, "esc to
  interrupt"); another provider or a new Claude Code layout needs the `_RE` and `_BOX_CHAR` settings.

## Traps this pack handles

- `events.created_at` has no zone, and which zone it is in depends on the bd that wrote it. bd 1.2
  and older left it to Dolt's `CURRENT_TIMESTAMP`, the server's local time; bd 1.3 writes UTC.
  `issues.*_at` and Dolt commit dates are UTC throughout. So a city that upgraded bd has both zones
  in its window, and a local-time city has one hour a year (the repeated DST hour) whose local
  times mean two instants. The pack reads each event's zone from a timeline:
  1. Anchors: every `created` event of an issue created in the window, read against that issue's
     `created_at`, matches the local reading or the UTC reading (within 2 min). Consecutive anchors
     of one zone make a run. The whole city shares one timeline, since every rig is written by the
     same bd. One run means one zone, as before the upgrade.
  2. An event's candidates are its local reading (both folds in the DST hour) and its UTC reading,
     kept where the timeline allows that zone: inside a run its own zone, between two runs both.
  3. Where more than one candidate is left (the gap around a switch, the DST hour), the bead's own
     Dolt commits (`dolt_log`, a first line naming the bead id) pick the reading that has a commit
     from 1 s before to 5 s after it. With commits near both, the nearer wins if it is at least 1 s
     nearer. Otherwise the local reading is taken and the event counts as guessed.
  4. Events are ordered by the resolved instant, not by the raw value.

  `quality.json` → `sources.events_tz` names the timeline (for example `Europe/Berlin until
  2026-10-25T07:26:04Z, UTC from 2026-10-25T08:34:27Z`) and how many events were read by timeline,
  by Dolt commit, or guessed. `sources.events_clock` has the same as data. `quality.py --bead ID`
  lists each of the bead's events with its raw value, its instant and how it was read, also for a
  bead that has not landed.

  Blind spots: a city whose rigs run different bd versions at the same time is misread, and so is
  an event inside a run that a bd of the other zone wrote. A switch and a switch back between two
  anchors of one zone go unseen. Where Dolt history was squashed, the gap around a switch has no
  commits to match, and its events are guessed.
- `metadata` is a JSON column: read it with `CAST(metadata AS CHAR)`; `LEFT()` on it fails in Dolt.
- gc's own usage log (`.gc/usage.jsonl`) is not a cost source: it covers a gc session completely or
  not at all (measured on 10-25: 2,985 of the 7,787 transcript messages since 10-24 00:00Z, missing
  most dog sessions and every crew session), it was empty from 10-04 to 10-18 under gc 1.4.1, and gc 1.5.1 prices only
  claude-opus-5-5, with every cache write at the 5-minute rate.
- Every `gc` CLI call costs seconds of CPU, so the pack calls `gc` once and reads Dolt directly.
  The census calls `gc session list` every run and keeps the other answers until a config or
  suspension file changes. It runs `gc` in the city directory: `--city` cost the CLI about twice
  the CPU (1.5+1.0 s against 0.7+0.5 s for `gc agent list`, 2026-10-26).
- Graphviz installs its own `gc` at `/usr/bin/gc`. It prints nothing for `gc rig list --json` and
  exits 0. A forced run started from systemd (2026-10-26 07:32Z and 09:23Z) called Gas City's gc by
  its full path, but the script it ran had systemd's PATH, which leaves out `~/.local/bin`, so
  `gc` meant Graphviz's. Every entry point sources `gc-bin.sh`, which sets `GC_BIN` to the gc
  that started the order (the nearest ancestor process whose binary is named `gc`; Linux `/proc`)
  and falls back to PATH only without one. A run with no JSON from `gc` fails with one line that
  names the binary.
- Dolt closes a connection that has been idle longer than its `read_timeout_millis` (60 s in the
  city this was built in), and a run that reads files between two queries can be idle that long
  under load: the replay lost its connection twice on 2026-10-26 at load 35-48 while it read the
  event-log archives. A query that loses its connection (MySQL errors 2006, 2013) reconnects and
  runs once more.
- A bead's assignee can be a session name (`gastown__polecat-gc-wbnef`) and not an alias; the
  session id is its tail. gc names some of a pool slot's `session.woke`/`stopped` events after the
  pool (`hivemind/gastown.polecat`) and others after the slot (`hivemind/gastown.furiosa`), so the
  replay tells slots apart by session id.

## Spot checks

Each metric was checked by hand against the raw beads, events, GitHub and log data on 2026-10-25 in
the city it was built in. The cases:

| Metric | Case | By hand | Counted |
|---|---|---|---|
| Bounce | hivemind-14zub | 1 bounce (10-24 20:14Z, CHANGELOG line under the wrong release) | 1 |
| Bounce | hivemind-v79o2 | 2 bounces (UBS +7, then UBS +3, 10-20) | 2 |
| Bounce | hivemind-pen9l | 1 bounce (acceptance item DONE (5) not addressed) | 1 |
| Bounce | hivemind-n33h7 | merged without a bounce | 0 |
| Bounce | hivemind-sn5ge | 1 bounce, caused by a pre-existing red required check | 1, gate |
| Post-merge | hivemind-14zub | verify:live 21:14Z, not reopened | held |
| Post-merge | hivemind-hqbd9 | checker PARTIAL → verify:failed + reopen 1.1 h after merge; re-landed, now verify:live | failed (reopen) |
| Post-merge | hivemind-vxepq | verify:failed, reopened 0.8 h after merge | failed |
| Post-merge | hivemind-f3b4y | verify:failed, reopened 7.5 h after a close | failed |
| Fix-of-a-fix | hivemind-x4xl7 | 3kwfd, 5n6gc, n33h7: filed by the after-deploy check of the build that shipped it ("Comes with hivemind-x4xl7") | 3 bugs |
| Fix-of-a-fix | hivemind-n33h7 | r9cs1: found checking n33h7's docs on the release | 1 bug |
| Fix-of-a-fix | hivemind-4p0xd, hivemind-pf6xi | csgor (wrong MCP schema shipped in d51a5078); mrk8r (pf6xi made "changed since" 40-100x slower) | 1 bug each |
| Fix-of-a-fix | hivemind-pf6xi → svqxz, hivemind-d87pp → sn5ge | "Older bug: old build does the same"; id only inside a file path | not counted |
| Fix-of-a-fix | random sample of 12 counted links | 10 defects or incomplete fixes of the landed bead, 1 borderline, 1 reference only | |
| First-pass CI | loomtracer #367 (14zub), #368 (n33h7) | first heads 8edb3452, 52c121e9: all 10 required checks green (the red Plugins job is not required) | yes, yes |
| First-pass CI | loomtracer #366 (5n6gc), #362 (erlbl) | first heads 0093f687 (4 required Rust checks red), 6d4bfeee (Dependency audit red) | no, no |
| Time to land | hivemind-14zub | created 10-23 16:25:04Z, claimed 16:27:53Z, PR 16:43:18Z, merged 10-24 20:32:13Z, verified 21:14:44Z | 28.07 h claimed → merged |
| Time to land | hivemind-cyccr, hivemind-r9cs1 | claimed → merged 48 min and 63 min; PRs #369, #370 opened 18:51:45Z, 20:45:30Z | same |
| Time to land | hivemind-hqbd9 (reworked) | first PR #336 opened 03:36:59Z, second #340 | runs from #336 |
| Event time | timeline, 10-25 | `created` events vs `issues.created_at` in all 8 rigs: +2 h before 01:00Z, +1 h until gcs-d9w 07:26:04Z, 0 h from hivemind-rzs8x 08:34:27Z; every event matched to its commit is local up to 07:42:17Z and UTC from 08:16:11Z | local until 07:26:04Z, UTC from 08:34:27Z (1706 anchors) |
| Event time | gcs-d9w (both sides of the switch) | raw 08:16:36 and 08:25:58 = UTC (commits 08:16:36.7, 08:25:58.8); raw 08:41:46 = local 07:41:46Z (commit 07:41:46.4); `closed` raw 08:26:06 = UTC (`bd: close` 08:26:07.2, `closed_at` 08:26:07) | same, all by commit, in that order |
| Event time | hivemind-0921o, hivemind-nf97m | `label_added` raw 08:32:20 = local 07:32:20Z (commit 07:32:20.6); `claimed` raw 08:39:31 = UTC (`started_at` 08:39:32) | same |
| Event time | hivemind-j6tgw, hivemind-plehj (DST hour) | j6tgw `claimed` raw 02:54:19 = first fold 00:54:19Z, `closed` raw 02:33:55 = second fold 01:33:55Z (`closed_at`); plehj `claimed` raw 02:22:29 = second fold 01:22:29Z (`started_at`) | same, by commit |
| Event time | hivemind-14zub (10-24, CEST) | events +2 h: bounce 20:14:50Z, merged 20:32:13Z, verified 21:14:44Z | same, by timeline |
| Stages | hivemind-p7da (607.6 h) | status blocked 09-26 16:26:14Z, hold_reason from 09-28 04:24:02Z (raw 06:24:02 CEST), `blocks` on hc-3nq added 10-21 05:07:54Z, hc-3nq closed 21:18:14Z, routed 21:13:27Z, claimed 23:42:54Z, PR #351 23:46:14Z, merged 10-22 00:01:21Z | unrouted 0.011, parked 588.694, blocked 16.172, routed 2.411, working 0.056, review 0.252 h; sum 607.596 |
| Stages | hivemind-qqou8 (42.9 h) | created 10-18 07:29:34Z, routed 07:52:42Z, claimed 10-20 01:56:48Z, PR #320 02:01:04Z, merged 02:22:16Z | unrouted 0.386, routed 42.068, working 0.071, review 0.353 h |
| Stages | hivemind-9gatq (24.9 h) | `blocks` on hc-24n and hc-0k1 from 10-19 17:56:30Z / 17:56:47Z, hc-24n closed 10-20 18:24:35Z (the later one), hold cleared 18:25:02Z, routed 18:25:09Z, claimed 18:26:19Z, PR #335 18:36:42Z, merged 18:51:05Z | unrouted 0.007, blocked 24.468, parked 0.007, routed 0.019, working 0.173, review 0.24 h |
| Stages | hivemind-v79o2 (10.8 h, 2 bounces) | created 10-20 00:59:23Z, routed 05:00:05Z, claimed 08:36:51Z, PR #329 10:14:12Z, first bounce 10:29:50Z, merged 11:48:05Z | unrouted 4.012, routed 3.613, working 1.623, review 0.261, rework 1.304 h |
| Stages | every landed bead, 28 days to 10-26 | stages add up to created → merged | 259 of 259 within 0.01 h |
| Human wait | end of 10-23 | open gc-xroei (past due since 10-20) and gc-14ghu (due 10-25); gc-7ltuf answered after 13.7 h | 2 open, 1 past due, 13.7 h |
| Human wait | end of 10-24, end of 10-20 | 1 open / 1 past due, gc-14ghu 27.8 h; 2 open / 1 past due, gc-c0z9g 25.0 h | same |
| Cost, day | 10-03 (CEST) | independent recount (grep, jq, awk with typed-in rates, every line whose own `cwd` is under the city's `.gc/`): 12,414 messages, $695.85; the 2 extra messages straddle midnight (lines 21:59:57Z, final line 22:00:04Z) and count on 10-04 | 12,412 messages, $695.68 |
| Cost, day | 10-25 (25 h, DST ends) | same recount, 22:00Z to 23:00Z next day: 5,522 messages, $352.47; 7 messages ($0.21) are a person's session launched in the city root that later `cd`ed into a worktree | 5,515 messages, $352.26 |
| Cost, session | cv-crew `a3f0bec6`, 10-25 | 5 Opus 5.5 messages (10 lines): input 10, 1-hour cache writes 29,957, cache reads 255,935, output 2,106 = $0.3330 | $0.3330 |
| Cost, session | gas-city-setup polecat `f6e9fe55`, 10-25 | 6 Sonnet 5.5 messages (11 lines): input 12, 1-hour writes 14,808, reads 330,733, output 1,380 = $0.1061 | $0.1061 |
| Cost, tokens | gc's `.gc/usage.jsonl` | its 5,337 records with a transcript message (by `upstream_req_id`) have identical tokens; its 2,363 priced Opus 5.5 records total $155.16, equal to the pack's price with every cache write at the 5-minute rate (gc ignores the 1-hour split) | same tokens; $196.79 with 1-hour writes at $8 |
| Cost, geo | `inference_geo` per day | every message says `not_available` until 10-09 and `global` from then on, from the same sessions; none says `us` | `not_available` priced as global |
| Owner turns | 28 days to 10-26 | independent extraction (every user line of the mayor's transcripts and of the sessions started in the city root, injected text dropped by the same kinds of rule, each turn read by hand): 135 turns since 09-25 20:15Z, 123 inside the window (the other 12 are before its first day) | 123, the same uuids |
| Owner turns | 10-25 (25 h, DST ends) | 4 in the mayor's seat (10-24 22:00Z-23:49Z) and 12 in hand sessions in the city root (18:35Z-22:33Z) | 16 (mayor 4, hand 12) |
| Owner turns | other seats | the owner also typed into a polecat, a crew, a refinery and cv-crew (15 turns); none is an owner seat here, so none is counted. Agents' nudges into those seats ("MAYOR, interrupt: ...") are plain typed text, which is why the default is the mayor only | 0 |
| Stuck-asks (hand) | 30 days to 10-26 | 17 stuck-asks and 10 unjam turns among the 135; phrase rules found 17/17 and 10/10 there (written from them) and 4/15 and 1/9 on a holdout labelled first | not shipped, see "Owner turns" |
| Friction | 10-20, 10-18, 10-22 | grep of the log: 12 mol-dog-compactor failures; 11 skipped ticks; 1 stalled reset in hivemind-ui; 1 dead-session reopen (hivemind-otruj) | 12, 11, 1, 1 (hivemind) |
| Stops | hivemind-zdsh.23, .25 (10-26 08:03Z) | routed to hivemind/gastown.polecat at 05:19Z and 07:20Z, nobody assigned; the pool's cap is 1 and its one slot (furiosa) is active on hivemind-zdsh.22 | pool_cap 2.7 h and 0.7 h, "1 of 1 slot(s) busy, on hivemind-zdsh.22" |
| Stops | the same at 08:23Z | zdsh.22 handed to the refinery, zdsh.23 claimed by furiosa | zdsh.22 in review, zdsh.23 working, zdsh.25 pool_cap on zdsh.23 |
| Stops | gc-xroei | ALEX: gate, `human.due` 10-20 07:16:36Z | human_gate_due, 144.8 h past due |
| Stops | gcs-78y | `hold_reason` set by gastown.mayor 10-25 18:50Z, nobody assigned | parked 13.2 h, actor gastown.mayor |
| Stops | hmd-4tf | status blocked set by a polecat 09-19 00:15:41Z, which assigned it to gastown.mayor with `awaiting_signoff` | parked 896 h, actor gastown.mayor (its rig suspended too) |
| Stops | hc-e21.5 | `blocks` on hc-mnn (open, unassigned), which `blocks` on hc-fav.2 (open, unassigned) | blocked; the chain ends 1 hop on at hc-fav.2; actor the router |
| Stops | hivemind-f3b4y | closed 10-23 08:23Z with `verify:failed` | deploy_failed, 72 h |
| Stops | stalled resets | the event log: hivemind--gastown__refinery reset 3 times on local 10-25 (first 02:00:42Z), quality-crew once on 10-26 | 3 and 1, in the right local days across the DST change |
| Stops | seats, 10-26 08:23Z | 19 sessions, 15 awake; the 2 always-on witnesses asleep are in suspended rigs; the mayor's 6 unread mails came while it was active | no seat_down, no seat_mail |
| Stops | host, 10-26 | `top` over the same 4 s windows: 6.1, 8.9, 6.3 cores busy | 5.8, 8.8, 8.2 (own + reaped children) |
| Stops (replay) | live census 10-26 08:03Z vs `stops_replay.py --at` the same instant | 13 blocked, 33 parked, 1 human_gate_due, 2 pool_cap, 2 working, deploy_failed 1 | the same, without deploy_failed (not replayed), plus 2 seat_down in suspended rigs (no suspension history) |
| Stops (replay) | hivemind-d4fbt (10-18 scan batch) | the stage split: routed 26.69 h | pool_cap 14.0 + 12.5 = 26.5 h, "1 of 1 slot(s) busy, on hivemind-u46nq" then hivemind-7v4dg |
| Stops, containers | the 5 epics and milestones in flight, 10-26 18:03Z | hu-65k (epic): `hold_reason` alex_judges_redesign_on_preview_and_demo, no assignee, waits on Alex; hivemind-zdsh (milestone): held until its exit-gate child hivemind-zdsh.21 closes, and .21 is in flight (parked by the mayor); hc-e21, hc-fav, hc-j4b: open goals, no hold | hu-65k parked, actor alex; the other four not in the census (before the in-flight rule, hivemind-zdsh came out as alex's because its text names him) |
| Stops (replay), containers | `stops_replay.py --at 2026-10-26T16:38:00Z` for the same 5 and gc-xroei | as above | hu-65k parked on alex, gc-xroei past due, the other containers not stops |
| Today, needs you | census of 10-26 18:03Z, checked in `bd show` | gc-xroei: a decision bead, `human.due` 10-20 07:16:36Z, unassigned; hivemind-m306.6: deferred, assigned to alex, and hivemind-fabq.4, -5r6q, -o6iy each `blocks` on it; hu-65k as above | 3 cards: gc-xroei past due 154.8 h; m306.6 parked, "Answering it unblocks 3 beads" (those three); hu-65k parked |
| Today, who clears it | the same census | stops.json: 54 stops, one of them the host | 53 stops over 5 actors (gastown.mayor 43, alex 6, the hivemind polecat pool 2, two old sessions 1 each) |
| Today, trends and cost | quality.json of 10-26 09:31Z | landed per day over the last 7 days: 23+12+8+16+1+15+4 = 79, 11.3 a day; 7-day cost $2,008.87 / 79 = $25.43 | 11.3 per day; $25.43 per landed bead |
| Today, links | "Open in gc" on hu-65k | gc 1.5.1's dashboard at `/city/gc/beads?bead=hu-65k` opens the bead's detail (title, epic, open, description); the page sent only GET requests | the same URL |
