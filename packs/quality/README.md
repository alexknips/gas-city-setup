# quality: how good a Gas City's work is

A Gas City pack with one exec order, `quality-metrics`, that measures per rig and per local day
whether work lands first time, holds after merge and how long it takes. It writes a per-day JSON
history and a static page you can put next to any dashboard. Configuration only: no agents, no
formulas, no changes to `gc` or `bd`. No API keys: cost comes from the transcripts the agents
already write, priced from a table in the pack (`assets/prices.toml`).

| Metric | One line |
|---|---|
| Bounce rate | landed beads the refinery sent back at least once |
| Post-merge failure rate | landed beads the post-merge check failed, or that were reopened after landing |
| Fix-of-a-fix | landed beads that drew a bug about them within 14 days |
| First-pass CI | merged PRs whose first head passed every required check |
| Time to land | created → claimed → PR opened → merged → verified, median and p90 |
| Human wait | human-gate beads open, past due, and how long answered ones stayed open |
| Cost per landed bead | list-price dollars (and tokens) of the agents' model calls per landed bead, from their transcripts |
| Machine friction | stalls the city already logs (order failures, stalled sessions, ...) |

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
   ```

3. `gc import install` (fetches the pinned pack and records it in `packs.lock`), `gc reload`, then `gc order list` shows `quality-metrics` and `gc order run quality-metrics`
   runs it now. The first run is the backfill of the last 28 days; after that it runs once a day
   (see "When it runs").

To see the numbers without writing anything: `$PACK_DIR/assets/scripts/quality-metrics.sh --dry-run`
(the path is in `gc order show quality-metrics`). To see how one bead was counted:
`... --bead <id>`. To see one day's cost per seat, transcript and model: `... --cost <YYYY-MM-DD>`
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
| `QUALITY_PRICES` | unset | a TOML price table in the shape of `assets/prices.toml`; its models replace the pack's one by one |
| `QUALITY_POST_RENDER` | unset | shell command run after the page is written; `{page}` and `{out}` are substituted |
| `QUALITY_TZ` | host zone | zone of the day buckets |
| `QUALITY_EVENTS_TZ` | read per event from a timeline | one zone for every `events.created_at`, skipping the timeline (see the time trap below) |
| `QUALITY_DOLT_PORT`, `_HOST`, `_USER`, `_PASSWORD` | `GC_DOLT_PORT`, 127.0.0.1, root, empty | the city's Dolt server |

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
(`$QUALITY_STATE_DIR/transcripts.json`); a whole cached run took 14-16 s wall, 6-8 s CPU.

### When it runs

The order fires hourly (`trigger = "cooldown"`, `interval = "1h"`). Only the first successful run
of a local day (`QUALITY_TZ`) does the work: it writes the day to `$QUALITY_STATE_DIR/last-ok-day`,
and later fires that day exit at once without a `runs.log` line. A run skipped for load writes no
stamp, so the next fire, an hour later, tries again. The page is therefore fresh from the first
quiet hour after midnight.

- Run again the same day: `QUALITY_FORCE=1 gc order run quality-metrics`. A plain
  `gc order run quality-metrics` on a day that already ran exits at once.
- `--dry-run` and `--bead` always run and never write the stamp.
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
  was counted, the PRs merged that day, human gates and friction; city-level friction and cost
  (dollars, tokens by kind, unpriced tokens, and the same per model and per seat).
- `quality.json`: 7-day and 28-day windows per rig and for all rigs, daily series, sources.
- `index.html`: the page. It reads `quality.json` and says "no data" (and why) where a source has none.

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
- **Blind spots**: created → claimed includes time a bead was deliberately parked or blocked. A bead
  reopened and landed again is measured to its last close. Merged → verified exists only where the
  check's label event was recorded.

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

### Machine friction

- **Definition**: per day, counts of `order.failed` (per order), `session.reset_stalled`,
  `session.cold_start_timeout`, `session.work_query_failed`, `bead.dead_assignee_reopened`,
  `supervisor.fs_pressure.skipped_tick`, plus bounces caused by a gate.
- **Source**: `$GC_CITY_PATH/.gc/events.jsonl` and its rotated `events.jsonl.archive-*.gz`, read
  incrementally. An event belongs to a rig when its subject or session names it, else to the city.
- **Blind spots**: counts events, not their cost. Wake orders (a nudge to an idle seat) are not
  counted: gc keeps no record of delivered nudges, and `order.fired` only says the order ran.

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
| Human wait | end of 10-23 | open gc-xroei (past due since 10-20) and gc-14ghu (due 10-25); gc-7ltuf answered after 13.7 h | 2 open, 1 past due, 13.7 h |
| Human wait | end of 10-24, end of 10-20 | 1 open / 1 past due, gc-14ghu 27.8 h; 2 open / 1 past due, gc-c0z9g 25.0 h | same |
| Cost, day | 10-03 (CEST) | independent recount (grep, jq, awk with typed-in rates, every line whose own `cwd` is under the city's `.gc/`): 12,414 messages, $695.85; the 2 extra messages straddle midnight (lines 21:59:57Z, final line 22:00:04Z) and count on 10-04 | 12,412 messages, $695.68 |
| Cost, day | 10-25 (25 h, DST ends) | same recount, 22:00Z to 23:00Z next day: 5,522 messages, $352.47; 7 messages ($0.21) are a person's session launched in the city root that later `cd`ed into a worktree | 5,515 messages, $352.26 |
| Cost, session | cv-crew `a3f0bec6`, 10-25 | 5 Opus 5.5 messages (10 lines): input 10, 1-hour cache writes 29,957, cache reads 255,935, output 2,106 = $0.3330 | $0.3330 |
| Cost, session | gas-city-setup polecat `f6e9fe55`, 10-25 | 6 Sonnet 5.5 messages (11 lines): input 12, 1-hour writes 14,808, reads 330,733, output 1,380 = $0.1061 | $0.1061 |
| Cost, tokens | gc's `.gc/usage.jsonl` | its 5,337 records with a transcript message (by `upstream_req_id`) have identical tokens; its 2,363 priced Opus 5.5 records total $155.16, equal to the pack's price with every cache write at the 5-minute rate (gc ignores the 1-hour split) | same tokens; $196.79 with 1-hour writes at $8 |
| Cost, geo | `inference_geo` per day | every message says `not_available` until 10-09 and `global` from then on, from the same sessions; none says `us` | `not_available` priced as global |
| Friction | 10-20, 10-18, 10-22 | grep of the log: 12 mol-dog-compactor failures; 11 skipped ticks; 1 stalled reset in hivemind-ui; 1 dead-session reopen (hivemind-otruj) | 12, 11, 1, 1 (hivemind) |
