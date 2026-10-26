# Gas City dashboard: design

Status: proposed, waiting for Alex's pick (bead gcs-tk5). Written 2026-10-26 by the quality crew.
Mockup of the first screen: [`dashboard-mock.html`](dashboard-mock.html) (example data, open it in a
browser).

## For Alex: pick one

Three facts decide this:

1. **The fork is a copy of a finished workspace.** gc folded gascity-dashboard into gc itself on
   2026-06-29 (gastownhall/gascity #3727). The dashboard gc 1.5.1 serves on :8372 is that app. It
   lives in `internal/api/dashboardspa` of gastownhall/gascity and gets commits every week there
   (131 commits, the latest on 2026-10-25). The standalone repo we forked stopped on 2026-07-18. A
   PR to it has nobody to merge it.
2. **gc's dashboard is good at operating the town, and it has no extension point.** It is live,
   and it covers agents, beads, runs, mail and health, with buttons to act. Its views are compiled
   into gc, so a pack cannot add one. Its "needs you" list does not know this town's conventions:
   on 2026-10-26 it flagged 53 beads. The stop census put 32 of them as parked on purpose and 13
   as blocked by another bead. One was a past-due gate waiting on you, and one an epic held for
   your judgement (which the census misses, see 1a).
3. **Our pages hold what gc lacks, but nothing puts it first.** Only our pages say why work waits
   and on whom, whether the town gets better, and what a landed bead costs. The front page (Daily)
   answers "how much landed today". "What needs me" sits as one table row halfway down the
   Quality page.

| | What we build | Cost, and who bears it | What you get |
|---|---|---|---|
| **A (default)** | A new first screen, **Today**, in the quality pack, built from the data the pack already writes. Every row links into gc's own dashboard for live detail and actions. Daily and Scoreboard move into the pack too (the open bead gcs-qc8). | About 3 beads of quality-crew work. The mayor applies one re-pin and a nav change. No new service: static files written by the census order that already runs every 30 minutes. The fork stays unused. | One screen that answers your five questions, in any city that imports the pack. Decisions refresh every 30 minutes, not live. Acting takes one click into gc's dashboard. |
| **B** | Our views added to the fork (alexknips/gascity-dashboard), run as a second Node service. | We take over a July snapshot of an app gc now ships. Against gc 1.5.1 it already breaks: its Runs feed times out and shows 0 runs where gc shows 15. Every gc API change is ours to chase, with no upstream to merge into. It adds one more Node process on a host that is over its load gate most of the day. | Live data, actions and our views in one app, for as long as we keep it working. |
| **C** | PRs to gc's own dashboard in gastownhall/gascity: "needs you" that honours a city's hold markers, and a way for a city to add its own pages. | This is gc core code. It needs you to widen the dashboard exception from the standalone repo to gc's source, and a fork of gascity, which you turned down for core work before. Big repo (Go, Bazel, React), review time we do not control, and it only ships when the city upgrades gc. | Every Gas City gets it, in the one dashboard. Can follow A: A shows what is worth upstreaming. |

**Recommendation: A.** The data that makes a dashboard useful to you (the stop census, the quality
trends, one cost figure) is already in the pack and spot-checked. gc's dashboard already does live
detail and actions well, so A links to it instead of rebuilding it. B pays to maintain a copy of
gc's own UI and gets nothing upstream. C is the right home for two small fixes, but only after A
has shown which ones matter.

If you say nothing within 24 hours, A stands. The fork stays as it is: public, untouched, no cost.

## 1. What each surface shows today

All observations are from this city on 2026-10-26 between 16:20 and 17:00 UTC, with host load
between 23 and 35 on 12 cores. Screenshots exist for every view named below. They show bead titles
from private rigs, so they are not in this public repo; the bead gcs-tk5 links the private page
that holds them.

### 1a. gc's own dashboard (`gc dashboard`, served by the supervisor on :8372)

What it is: the React app from gascity-dashboard, built into the gc binary, with a Go backend
(`internal/api/dashboardbff`). Views: Home, Agents, Beads, Runs, Mail, Activity, Health. Live over
server-sent events. Actions: nudge, claim, close, create and sling a bead, send mail. It serves
every city the supervisor knows (this one and herdr-city).

What it showed:

- **Home**: 14 active sessions, 424.6M tokens today, $142.76 estimated today ("cost excludes
  unpriced model calls"), burn $7.43 per hour, 42 runs queued, a row of context gauges per
  session. Its "Needs you" line held one item: "Dolt health advisory [MEDIUM]".
- **Agents**: "Needs you (3)": the deacon and two refineries, rate limited. Then every session with
  state, context use and last activity, each with a Peek button.
- **Beads**: "Needs you (53)", then all open beads. The rule behind the list
  (`attention/beadsNeedingAttention.ts`): open and unclaimed for over 24 hours, escalated, stalled
  for an hour, or held by `gc.checkpoint_hold`, `gc.founder_gate`, `gc.waiting_on` or a `hold:`
  label. This town parks work with `hold_reason`/`waiting_on` metadata and the `deferred` status,
  so gc reads parked beads as unclaimed work.
- **Runs**: 15 active runs, all patrol molecules (witness, refinery, deacon). "PR 0, design 0,
  bugfix 0".
- **Mail**: the nav badge says 98, Home says 174 unread.
- **Under load**: one capture showed Beads stuck on "Loading beads" after 25 seconds, and Health
  said "Supervisor unreachable" for a moment, while the API answered in under 0.3 s from the shell.

The 53 beads in gc's "Needs you", checked against the stop census of 16:38 UTC:

| Stop census says | Beads |
|---|---|
| parked on purpose (30 held by the mayor, 2 by old sessions) | 32 |
| blocked by another bead (10 on the mayor; 3 on Alex, all behind one root) | 13 |
| not a stop: 3 backlog tasks and bugs (open, unrouted, no hold), 4 epics the census skips | 7 |
| waiting for a person, past due | 1 |

One of the 4 skipped epics, hu-65k, is held with `hold_reason=alex_judges_redesign_on_preview_and_demo`:
a real decision for Alex that gc's list catches and the census misses, because the census skips
epics and milestones by design (`QUALITY_STOP_SKIP_TYPES`). The Today page has to close that gap
(section 3).

Extension points: none for a pack. The SPA routes are fixed in `App.tsx`; the Go side builds links
only to its own run views (`dashboardbff/links.go`); the module list is compiled in. What a pack
can use is a deep link: `/city/<city>/beads?bead=<id>` opens one bead in gc's view, checked
against the 1.5.1 bundle.

### 1b. Upstream gascity-dashboard (our fork, at upstream fdd2d63), run locally

How: Node 26 (`engines` asks for 24 or newer), `npm install && npm run build`, then
`PORT=<port> DASHBOARD_READONLY=1 GC_SUPERVISOR_URL=http://127.0.0.1:8372 GC_CITY_NAME=gc node backend/dist/server.js`.
Read-only mode turns every non-GET to the supervisor into a 405. The install took 10 minutes at
load 30.

What it showed:

- **Home**: an "Attention" list of five Dolt backup and escalation mails, then "53 more in beads,
  1 more in runs, 93 more in mail", then "0 in flight, 0 waiting, nothing failing".
- **Runs**: "runs partial". Its formula feed timed out after 2.5 s on every load, so it showed 0
  active runs where gc's own showed 15. The Home line above was built on that empty feed.
- **Agents, Beads, Health**: the same views as gc's own, with small differences. It has a Convoy
  view gc's lacks; gc's has the cockpit Home this one lacks.

In short: an older copy of gc's own dashboard, already drifting from gc 1.5.1's API.

### 1c. Our pages on :8787

- **Daily** (front page; `herdr-town-score` writes `scores.json`): merged today and over 7 days,
  commits, tokens with a dollar estimate, done and code landed per rig per day over 14 days, a day
  table. Its token chart reads 0 before 10-24 (the token source changed then) and its axis labels
  render as "00M". It prices today at $809.18 from `herdr-town.toml`.
- **Quality** (this pack): nine metric tiles with 28-day sparklines, the stop census ("Why it's
  waiting, now"), a per-rig table, time to land by stage, machine friction. Census every 30
  minutes, metrics daily, weekly review files under `reviews/`.
- **Roadmap** (beadline): forecast bars for goals and milestones; no targets are set.
- **Scoreboard**: Gas City against herdr-town per ISO week. herdr-town stopped merging on 09-18,
  so half the page is zeros.
- **stats.html**: a redirect to Daily.

Good: the only surface that says why work waits and who clears it, whether quality moves, and
what a landed bead costs. Static files, cheap to serve, up when the supervisor is down, work at
phone width, and every metric's blind spots are written down. Weak: no first screen for decisions;
four pages that overlap (Daily and Scoreboard both count merges); not live; no actions; three
dollar figures for the same day across the town (gc's $142.76, Daily's $809.18, the pack's $195.87
by 09:31 UTC). Only Quality ships in a pack. Daily and Scoreboard are hand-built for this town
(gcs-qc8 is open for that).

### Side by side, against the operator's jobs

| Job | gc's own (:8372) | Upstream fork | Our pages |
|---|---|---|---|
| 1. What needs me now | Flags 53 beads; the real gate and the held epic are in there, unranked | Same 53, plus mails | The census knows most of it (1 past due, 4 more on Alex; it skips held epics), but it is a row on the Quality page |
| 2. What is stuck, and who clears it | Not covered: blocked and parked look the same as unclaimed | Same | Covered: stop census by class and actor, every stop, replay-tested |
| 3. Is the town healthy | Strong: supervisor, Dolt, sessions, host, live | Same, Runs broken | Partial: seats and host load from the census |
| 4. Is it getting better | Not covered | Not covered | Covered: 28-day trends, weekly review with a proposal |
| 5. What it costs | Today and burn per hour; leaves out unpriced models | Same | Cost per landed bead from transcripts, spot-checked; Daily shows a second figure |
| Act on it | Yes (nudge, claim, close, mail) | Yes, unless read-only | No |

## 2. Who it is for, and their jobs

One person running a town of agents. They do not watch it all day. They open it a few times a
day, on a laptop and sometimes on the phone, to find out whether they are the bottleneck. Their
jobs, in the order they ask:

1. **What needs me now?** The decisions only they can make, oldest first, and what each one
   unblocks.
2. **What is stuck, why, and who clears it?** Mostly the mayor's queue: is it moving or rotting?
3. **Is the town healthy?** Supervisor, seats, host load, friction.
4. **Is it getting better?** Landing speed, bounces, post-merge failures, and what the weekly
   review proposed and whether it worked.
5. **What does it cost?** One figure they can trust, per day and per landed bead.

Acting on an answer (nudge a seat, answer a gate, close a bead) happens in gc's dashboard or in a
session. The first screen needs to get them there in one click, not replace it.

## 3. Recommendation

**Base: our pages, shipped in the quality pack.** gc's dashboard stays the place for live detail
and actions; the pack's first screen links into it. The fork is not needed for this.

Why not the others, in one line each: the fork (B) duplicates gc's own UI with an API that already
drifted, and no upstream will take its PRs. gc's own dashboard (C) is where fixes belong
eventually, but it is core code, and it needs your call plus a gascity fork.

### The first screen: Today

See [`dashboard-mock.html`](dashboard-mock.html). Top to bottom:

- **A one-line verdict** ("2 decisions wait on you. 42 beads wait on the mayor. Host load is over
  the gate. 11.3 beads landed per day this week."), with freshness chips: census time, metrics
  time, latest review, supervisor status.
- **Needs you** (job 1). One card per root decision whose next actor is the configured human. A
  bead blocked behind the same root is listed under it ("Answering it unblocks 3 beads"). On
  2026-10-26 this turns 5 stops into 2 decisions: a past-due gate (a host task only Alex can run), and one deferred
  decision that 3 hivemind beads wait on. Past due is red, parked is amber. Each card has "Open in
  gc", the deep link into gc's bead view. The real page must also list held epics and milestones
  whose next actor is the human (hu-65k would be a third card); the census skips those types
  today, and the mock shows the census as it is.
- **Waiting, and who clears it** (job 2). Every stop grouped by who must move next, as a bar split
  by reason (the mayor: 42, of which 31 parked, 10 blocked, 1 routed). The full list stays on the
  Quality page, one click away.
- **Is the town healthy?** (job 3). Supervisor (from gc's health API), seats, host load against
  the gate with the busiest processes, unread seat mail, friction over 7 days.
- **Is it getting better?** (job 4). Five tiles: beads landed per day, bounce rate, claimed to
  merged, post-merge failures, cost per landed bead. Each shows the 7-day value against the
  28-day value, coloured better or worse, over a 28-day sparkline. Under them, the latest weekly
  review's finding, its proposal and what was decided.
- **What it costs** (job 5). One figure source: transcripts at API list price (the pack's
  `prices.toml`). The other figures the town shows that day are named in a footnote, not averaged
  in.

The design notes in the mock (purple) say where each block's data comes from. They go away in the
real page.

### What goes in the pack, for any city

- `packs/quality`: a Today page and its small adapter, rendered by the existing census order
  (`quality-stops`, every 30 minutes) from `stops.json`, `quality.json` and `reviews/`, plus one
  read of gc's API (`/v0/city/<city>/health`) made by the order, not by the browser. The page then
  needs no cross-origin call and keeps working when the supervisor is down. The prototype adapter
  took 0.7 s of CPU on this city's files (1.4 to 2.3 s wall at load 30), most of it reading the
  490 KB quality.json.
- New config: the gc dashboard's base URL (for the deep links) and the human's display name. The
  output directory already comes from config.
- Daily and Scoreboard as generic pages (gcs-qc8), without the herdr-town parts, so a city that
  imports the pack gets the whole set. Nav: Today, Quality, Daily, Roadmap (Roadmap only where
  beadline runs).
- One cost figure: Daily reads the pack's cost instead of pricing tokens itself.

### What goes upstream as a PR

Nothing is needed for A. If you later pick C, the candidates, smallest first:

1. "Needs you" honours a city's own hold markers (`hold_reason`, `waiting_on`, the `deferred`
   status), and treats a bead behind a held bead as waiting, not unclaimed. On 2026-10-26 that
   would take gc's list from 53 down to 8.
2. A city-config list of extra nav links, so gc's dashboard can point at a pack's pages.
3. The models gc's price table leaves out, so its cost figure stops undercounting.

All three are gc core code, so each needs your yes first. This seat files no beads against gc.

### Build plan under A (after you pick)

1. Today page and adapter in the pack, spot-checked by hand against gc's bead view and the census.
   The census learns to report held epics and milestones whose next actor is the human (and only
   those; the other skipped types stay skipped). About 1 bead.
2. gcs-qc8: generic Daily and Scoreboard in the pack. About 1 to 2 beads.
3. Mail the mayor the city-side diff: the re-pin, the nav order, and retiring the herdr-town
   Scoreboard columns.

Running cost: rendering Today adds about a second of CPU to an order that already runs every 30
minutes. No new process.

## Appendix: how the screenshots were taken

- gc's dashboard and the upstream fork: Playwright (`playwright-core` with the cached headless
  Chromium), a 10 to 25 second settle per view because both pages stream, full-page captures, and a
  request log to confirm that only GET requests went out.
- Our pages: headless Chromium `--screenshot` against the static files.
- The upstream fork ran read-only on a loopback port against this city's supervisor.
