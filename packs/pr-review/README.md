# pr-review

A Gas City pack that reviews a stream of pull requests for one person and surfaces only what needs
them. It is three orders and a few scripts: no agents, no formulas, nothing in the core.

- **`pr-review-sweep`** (every 5 min, no model): finds each new head of an open PR, puts it through
  zero-token rules, records one bead per PR, and queues the PRs that need a review. Merged and closed
  PRs are never reviewed (R15).
- **`pr-review-run`** (every 5 min): reviews the next queued PR with one headless, read-only `claude -p`
  run, within a daily cap. The script, not the model, renders the review in the style guide's shape,
  gates it, and posts it as a **submitted COMMENT review** under the reviewer's login: an honesty line
  first, the items as inline comments, never a pending review (R0). Slack gets the outcome word and that
  review's URL, once per PR head (R17, R18). A PR whose only problem is CI gets one question for its
  author, without a model.
- **`pr-review-feedback`** (hourly, no model): reads what the reviewer did with each review (submitted
  unchanged, edited, discarded; a thumbs-up or thumbs-down and replies in Slack) and keeps the score.

Together they are a loop ([Quality loop](#quality-loop)): a versioned style guide, a gate that holds
any review breaking it, feedback from the reviewer's own actions, and a regression set every change
must pass.

All three ship **off**. A city turns them on with an override that names its repos, the reviewer
and the Slack channel ([Turning it on](#turning-it-on)).

## Flow

```
GitHub ──sweep (rules, no model)──► bead per PR + queue ──run──► only CI wrong, no content rule? ── one line (no model)
                                                          │
                                                          └──► next in queue within cap
                                                               worktree at PR head (outside the runtime)
                                                               claude -p: Read/Grep/Glob only → line-anchored items (JSON)
                                                               script: outcome, render, GATE (fail: regenerate once, then hold)
                                                               ├─► re-check: still open? (R15)
                                                               ├─► GitHub: a SUBMITTED COMMENT review, inline comments (R0, R4)
                                                               ├─► Slack: "<word>: <review URL>", once per head (R17, R18)
                                                               └─► bead: outcome, closed
feedback (hourly) ◄── the reviewer's actions on GitHub and Slack ──► score, Today card, regression inbox
```

## Configuration

Set in the city's `[[orders.overrides]]` env, on **both** orders (they share the state file).

| Variable | Default | Meaning |
| --- | --- | --- |
| `PR_REVIEW` | `off` | Master switch. `on` runs the orders; `off` makes every fire exit at once. |
| `PR_REVIEW_MODE` | `shadow` | `shadow`: every review is posted with event COMMENT. `live` (only on the reviewer's word) would use APPROVE and REQUEST_CHANGES; not built yet. |
| `PR_REVIEW_EFFORT` | `high` | `claude --effort` for the review run. |
| `PR_REVIEW_REPOS` | (none) | `owner/repo=rig …`. The rig is where the PR's bead goes; without `=rig` it is the repo name. |
| `PR_REVIEW_REVIEWER` | (none) | GitHub login whose review requests select a PR, and whose `gh` login posts the reviews. |
| `PR_REVIEW_REVIEWER_NAME` | the login | The name in every review's footer ("<name> has not read this PR"). |
| `PR_REVIEW_NAMES` | (none) | `login=Name …` for PR authors, as questions address them ("2 questions for Nils"). |
| `PR_REVIEW_PANEL` | `<state dir>/panel.json` | Where the feedback order writes this week's numbers as a Today card; point the quality pack's `QUALITY_PANELS` at it. |
| `PR_REVIEW_SLACK_CHANNEL` | (none) | Channel id for outcome links. Empty: no Slack. |
| `PR_REVIEW_SLACK_ENV` | `~/.config/gc/slack.env ~/.config/gc/slack2.env` | Files searched for the first `xoxb-` bot token. |
| `PR_REVIEW_DAILY_CAP` | `10` | Model reviews per UTC day. CI escalations do not count. |
| `PR_REVIEW_RESERVE` | `3` | Slots of the cap that tier-2 PRs may not use, kept for later asked-for and serious PRs. |
| `PR_REVIEW_MAX_WAIT_H` | `24` | A queued PR not reached in this many hours closes as `over-cap`. |
| `PR_REVIEW_LOOKBACK_H` | `2` | How far back the first sweep looks. Later sweeps start at the previous one. |
| `PR_REVIEW_SWEEP_BUDGET_S` | `420` | A sweep stops taking new heads after this many seconds (the order times out at 10 min). Each PR is recorded as soon as it is looked at; the rest waits for the next sweep. |
| `PR_REVIEW_CI_WAIT_H` | `3` | How long a head with running CI waits before the rules take CI as it is. |
| `PR_REVIEW_RULES` | `assets/rules.toml` | The rule file ([Rules](#rules)). |
| `PR_REVIEW_CHECKLIST` | `assets/checklist-health.md` | Custom scan instructions appended to the review prompt. |
| `PR_REVIEW_MODEL` | `opus` | Model for the review run. |
| `PR_REVIEW_MAX_USD` | `5` | Spend limit per review (`claude --max-budget-usd`). |
| `PR_REVIEW_TIMEOUT_S` | `1200` | Wall-clock limit per review. |
| `PR_REVIEW_STATE_DIR` | `<city>/.gc/runtime/pr-review` | `prs.json` (state) and `pr-review.jsonl` (one line per run or action). |
| `PR_REVIEW_WORK_DIR` | `/var/tmp/gc-pr-review` | Bare blob-less mirrors and per-review worktrees: the repositories' code. Mode 700, outside the town runtime, and not under `/home`, `/data` or `/tmp`, which the review model may not read. |
| `PR_REVIEW_BEADS` | `on` | `off` skips the beads (tests). |
| `PR_REVIEW_MAX_LOAD` | `15` | Above this 1-minute load average a run skips and logs. |

## What never leaves GitHub

Pull requests can carry regulated data (health records, PHI) or code under SOC 2 controls. So the
pack keeps the repository's text on GitHub and in the work dir, and nowhere else:

- **Beads** hold the repo, PR number, link, head SHA and outcome. No title, path, finding or rule id.
- **The state file and the log** hold those, plus rule ids, tier, CI state, times and cost. Rule ids
  are this pack's own words.
- **Slack** gets `<outcome word>: <PR link>`. The script builds the message from a fixed word and the
  link; no model text reaches Slack.
- **The review model** runs with `LOOMTRACER_INGEST_DISABLE=1` and `HIVEMIND_INGEST_DISABLE=1`,
  `disableAllHooks`, `--no-session-persistence` (no transcript on disk) and no `GC_*` variables, so no
  ingest hook ships its session anywhere.
- **What the scripts print** (what an order log or an agent's transcript sees) is ids, links,
  outcomes, counts, sizes and cost. Error messages keep only the scripts' own text.
- **The work dir** holds the code while a review runs. Its context files and worktree are deleted
  afterwards. The bare mirror stays, like any clone. It also keeps, mode 700, what was posted, the
  reviewer's edits and Slack replies (`feedback/`) and the real regression cases (`regression/`).

## Rules

The sweep's rules match what a PR changes, at no token cost. A rule **selects** a PR for review when
its area is in `select_areas` (A1, A2, A4, A7 by default). Other rules only tell the reviewer what
to ask about. Rules skip test files unless they ask for them (`tests = "also"` or `"only"`). Docs
and lockfiles are never matched.

Built into the code:

| Rule | Area | Definition | Source | Blind spots |
| --- | --- | --- | --- | --- |
| `requested` | scope | The reviewer was asked for a review, now or at any earlier time. | GraphQL `reviewRequests` + `REVIEW_REQUESTED_EVENT` | Team requests do not count. |
| `ci-red` | A7 | A check failed, errored, timed out, was cancelled or needs action on the head commit. | `statusCheckRollup` of the head | First 100 contexts only. |
| `ci-none` | A7 | No check or status on the head. | same | A repo without CI trips it on every PR. |
| `ci-skipped` | A7 | Checks ran, none passed and none failed (all skipped or neutral). | same | |
| `migration` | A4 | A file under `migrations/`, `migrate/`, `alembic/versions/` or `drizzle/` changed. | REST PR files | Another folder name misses. A non-DB folder with that name matches. |
| `migration-edit` | A4 | A migration that exists on the base was changed, removed or renamed. | same | |
| `migration-order` | A4 | A new migration's name sorts before the base's newest (string order, as Prisma, Rails, Drizzle and Knex apply them), or reuses a sequence prefix. | the base tree at the merge's parent (merged PR) or the base head (open PR) | Runners that order by a graph (Alembic) are not checked. |
| `conflicts` | A4 | GitHub reports a merge conflict (open PRs). | `mergeable` | `UNKNOWN` right after a push. |
| `untested` | A5 hint | 20+ added code lines and no test file changed. | PR files | Tests elsewhere in the repo do not count. |
| `no-description` | B3 hint | The PR body is under 40 characters. | PR body | |

In `assets/rules.toml` (pattern rules; one term per pattern, so a backtest can say which term
flagged what):

| Rule | Area | Matches |
| --- | --- | --- |
| `phi-schema` | A1 | Schema, model or migration lines naming patient fields (DOB, SSN, MRN, diagnosis, insurance, names, phone, address). |
| `phi-logs` | A1 | A log, print, error or exception call whose arguments pass a patient field (`${patient.email}`, `, dob)`). A message that only says the word does not count. |
| `phi-third-party` | A1 | Analytics, telemetry, export or webhook paths; analytics, error-tracking, CRM or LLM SDKs; CSV or bucket writes. |
| `phi-ehr` | A1 | Paths of an EHR integration (Athena, Epic, MyChart, FHIR, HL7, EHR/EMR, Redox and similar). |
| `phi-ehr-write` | A1 | A write, send or sync call on the same line as an EHR name. |
| `phi-fixtures` | A1 | In tests only: an SSN shape, a phone number outside the fictional 555 exchange, a real-looking email. |
| `sec-auth` | A2 | Paths of sign-in, sessions, OAuth/SSO, passwords/MFA, permissions/roles, policies, and middleware files. |
| `sec-secrets` | A2 | `.env`, key and certificate files, credential paths; added AWS keys, private keys, Slack/GitHub/API tokens, long literal secrets. |
| `sec-infra` | A2 | Terraform, CDK, CloudFormation, Kubernetes, Helm, IAM, WAF, containers, platform config. |
| `sec-ci` | A2 | `.github/workflows`, actions, CODEOWNERS and other CI configs. |
| `sec-audit` | A2 | Audit-log paths and lines. |
| `irrev-destructive` | A4 | DROP, TRUNCATE, DELETE FROM, ALTER … DROP/RENAME, ORM drops and bulk deletes, backfills, data-migration scripts. |
| `irrev-messages` | A4 | SMS, email, push or message sends, and their providers. |

Blind spots of every pattern rule: it is a match, not a judgement, so whether a hit is a real risk is
the review's job. GitHub leaves the `patch` out of very large files, so their lines are not matched
(the backtest counts how often: `patch_missing_prs`). A3 (money) has no rule. It is not checked, and
every review says so.

## Priority and the cap

Every selected PR gets a tier: **0** the reviewer was asked, **1** a serious rule tripped (CI,
migration order or edits, conflicts, destructive statements, EHR writes, PHI in logs or fixtures,
secrets), **2** any other select rule. Each run reviews the first queued PR by tier, then by more
distinct areas, then the oldest. A tier-2 PR runs only while more than `PR_REVIEW_RESERVE` slots of
the day's cap are left. A PR the cap never reaches closes as `over-cap` after
`PR_REVIEW_MAX_WAIT_H`. Its bead says so, and Slack gets nothing.

A new push to a PR already reviewed is not reviewed again, unless the new head trips a serious rule
the reviewed head did not.

## The review prompt

The prompt is Claude Code's `/security-review` command, copied unchanged into `assets/upstream/`
from [anthropics/claude-code-security-review](https://github.com/anthropics/claude-code-security-review)
(MIT, Copyright 2025 Anthropic; see `assets/upstream/SOURCE`). `pr_review.py` adapts it at run time:

- The command's `git status / diff / log` substitutions become the PR's link, title and description,
  its changed files, its commits and its diff (from where the PR forked off its base). A diff over
  350k characters is left in a file the model reads in parts.
- The checklist (`PR_REVIEW_CHECKLIST`), the style guide (`assets/style-guide.md`) and the hunks the
  rules flagged (path and line, found in memory) go in before `START ANALYSIS`.
- The closing steps are rewritten. The upstream command starts sub-tasks; here the model finds the
  issues, filters false positives and drops anything below confidence 8 itself, because the run has
  no Task tool. Each flagged hunk must become an item: a problem, or "nothing wrong here" with what to
  look at (R12).
- The answer is JSON (`--json-schema`): at most 5 items, worst first, each anchored to a line range on
  one side of the diff, with a kind (check for the reviewer, question for the author, change to make),
  severity, area, what changed, why it matters, what to decide or ask, what was verified, and a
  confidence; then a "safe to skip" line and the B3 checks. The model does not pick the outcome.

`assets/checklist-health.md` is the default checklist for health-care code: HIPAA Security Rule
technical safeguards (45 CFR 164.312), SOC 2 Trust Services Criteria CC6, CC7 and CC8.1, actions
that cannot be undone, and migration order. A city points `PR_REVIEW_CHECKLIST` at its own file.

**What the model can do**: read the worktree and its context dir (Read, Grep, Glob). It has no Bash,
no Task, no web and no MCP. It loads no settings from the repository, so a PR cannot add hooks or
tools. Reads under `/home`, `/root`, `/etc`, `/proc`, `/data`, `/tmp` and similar roots are denied. A
PR's text can sway the review; it cannot act.

**What the script checks before posting**:
- It keeps only items with confidence 8 or more, at most 5, derives the outcome, and renders the body
  and one inline comment per item. Then the gate ([Quality loop](#quality-loop)).
- It strips @-mentions, images, HTML and links to hosts other than github.com from model text: an
  image in a review is fetched when someone opens it.
- It withholds the whole review text if it contains a credential from this host (`gh` token, Slack
  tokens, Claude and Loomtracer credentials) or a secret or SSN shape. The PR then escalates with a
  note to review it by hand.

## Outcomes

One per reviewed PR, derived by the script (`pr_format.decide`), not chosen by the model.

| Outcome | When | Line 1 | Slack (shadow) |
| --- | --- | --- | --- |
| `escalate` | one or more decisions only the reviewer can make (R16): a product, risk or compliance trade-off, each one question with options, a recommendation and a link to the code at the head or the PR text | **Needs you: N decisions** | "Needs you" |
| `changes` | a change item of medium or high severity for the author, or changes mixed with questions | **Changes requested: N** | "Would request changes" |
| `questions` | only questions for the author; or CI red, missing or skipped with nothing else | **N questions for <author>** | "Would ask" |
| `low-risk` | nothing for anyone, and CI passed | **Low risk: nothing to check** | "Would approve" |

Rule flags alone never route a PR to the reviewer: they tell the model where to look first. Each
review ends with a suggested reply to the author that the reviewer can reuse. `pr_review.py --redeliver
REPO#N` posts the stored result for the current head again without a model run (once per head, R18).

Other outcomes on a bead: `no-rule` (not selected), `over-cap`, `closed` (merged or closed before its
review was delivered, R15), `held-gate` (the gate failed twice; nothing delivered), `held-secret` (the
text held a credential shape), `error` (the run failed; see the log).

## Quality loop

The reviewer's complaints about the first shadow reviews (2026-10-29: "it just fluff and really hard to
read. there are no markers in the review what I actually need to manually review"; then the pending
reviews in his name blocking his own; then "that review comment needs to link to the human decisions
… otherwise you do not need me!") became a loop, so a complaint is fixed once and stays fixed. The
measure is the reviewer's effort: time from the Slack post to his own decision on the PR, what he
opened, his thumbs on the Slack link. Not review volume.

1. **Style guide** (`assets/style-guide.md`, versioned, v2). Rules R0 to R17, each with the complaint it
   came from: never the reviewer's identity (R0); the outcome and count on line 1; "needs you" only for
   decisions only the reviewer can make, each linked to the code or text (R16); at most 5 items for the
   author, each linked to the exact diff lines, with what changed, why it matters and the change or
   question; a "Safe to skip" line; one facts line; the footer last; no rule codes or boilerplate;
   never for a merged PR (R15); every review at its own URL (R17). The model reads it in its prompt.
2. **Gate** (`pr_format.gate`, zero tokens). Every machine-checkable rule, against the model's items and
   the rendered text: each item's range resolved against the diff's hunks on its side, each decision's
   code link resolved against the head (the file exists, the lines are within it) and asked as one
   question with options, fields of at least four words with no filler (a deny-list) and no rule codes,
   the sizes, line 1, "Safe to skip", the facts line and the footer. Right before delivery: the PR is
   still open (R15) and the review has its own URL that is not on GitHub (R0, R17). On a failure the model answers again
   with the gate's reasons. A second failure posts nothing (`held-gate`). The log keeps the rule ids of
   each failure (`gate_first`, `gate`), so the score can count holds and regenerations.
3. **Feedback** (`pr-review-feedback`, hourly, zero tokens). For each delivered review: the thumbs-up or
   thumbs-down and the replies on its Slack message; the reviewer's own GitHub review after delivery and
   the minutes to it, or the PR merged or closed before they looked; a "needs you" they approved as is
   (false alarm). A verdict is never inferred from someone else's action: `--record KEY VERDICT --by WHO`
   records what was seen by hand and who did it. Blind spot: which items on the page were opened (the
   page server keeps no log the pack reads). The text (replies, notes, what was delivered) stays in
   `PR_REVIEW_WORK_DIR/feedback` (mode 700); the state file holds counts. The week's numbers (delivered,
   thumbs, median minutes to the reviewer's own review, PRs merged before they looked, false alarms, gate
   holds) go to `PR_REVIEW_PANEL`, a card on the quality pack's Today page.
4. **Regression set**. Every thumbs-down or reply drops a candidate case (the delivered text and the
   feedback) into `PR_REVIEW_WORK_DIR/regression/inbox`. The weekly quality review triages each one: a
   new or sharper style-guide rule if the gate could have caught it, then the case moves to
   `regression/` with the verdict it must get. `assets/scripts/pr-review-regress.sh` runs every case
   (the pack's synthetic fixtures in `tests/fixtures`, written by `tests/make_fixtures.py`, plus the
   host-local real ones) through the gate. Before any style-guide, gate, rule or prompt change moves a
   city's pin, it must end "N of N fixtures as expected": old failures still fail, good reviews still
   pass. Real cases never enter the public pack; a look-alike with invented text does.
   Format and gate rules from the reviewer's feedback are applied directly and listed for them. A
   change to what counts as "needs you" is their decision.

Case 1, in both sets: a CI escalation posted with rule labels and no line markers (fails on R1, R3,
R7, R8, R9, R10). Case 2: a review delivered on a merged PR (R15). Case 3: "needs you" without a linked
decision (R16). Case 4: a delivered review without its own URL (R17); case 5: delivered as a GitHub
review (R0).

The regression covers the gate and the renderer, not the model: it cannot show that a prompt change
makes the model write better items. The feedback numbers measure that.

## Calibration

`pr-review-sweep.sh --backtest 7` runs the rules over every PR opened in the last 7 days, in any
state. It prints per repo and per rule how many PRs each flags, which pattern flagged them, and what
the cap would reach. It posts and creates nothing. `--cache DIR` keeps GitHub's answers (the code)
in DIR for re-runs while tuning. Keep DIR outside the town runtime and delete it afterwards.

First calibration (2026-10-29, three repos, 329 PRs, one author):
- The first pass selected 221 PRs. Most DROP, auth-path and inline-token hits sat in test files only
  (29 of 30, 42 of 72, 9 of 11). `truncate` matched a CSS class, `notify(` matched UI toasts, bare EHR
  names flagged 30% of PRs, and UI `charts/` folders matched Helm.
- After the fixes, 158 PRs were selected (48%, about 23 a day, peaks of 56). A cap of 10 a day
  reached 63: all 12 asked for, 32 of 56 serious, 19 of 90 other.
- 73% of PRs merged within 30 minutes of opening (median 16 min). So the sweep also takes PRs merged
  since its last run, and most reviews come after the merge.
- Spot-checked against independent sources: PR totals against `gh pr list` (3 of 3 repos); line
  totals from the file lists against GitHub's own (329 of 329); `requested` against the REST timeline
  (37 of 37); CI state against `gh pr checks` (13 of 13).

## Turning it on

1. Import the pack and pin it (the city's `pack.toml`):
   ```toml
   [imports.pr-review]
     source = "https://github.com/alexknips/gas-city-setup.git//packs/pr-review"
     version = "sha:<commit>"
   ```
   then `gc import install`.
2. Override the three orders' env in `city.toml`. Shadow first:
   ```toml
   [[orders.overrides]]
   name = "pr-review-sweep"
   [orders.overrides.env]
   PR_REVIEW = "on"
   PR_REVIEW_REPOS = "org/repo-a=rig-a org/repo-b=rig-b"
   PR_REVIEW_REVIEWER = "<github login>"
   PR_REVIEW_SLACK_CHANNEL = "<channel id>"
   PR_REVIEW_LOOKBACK_H = "24"

   [[orders.overrides]]
   name = "pr-review-run"
   [orders.overrides.env]
   # the same five lines, plus PR_REVIEW_REVIEWER_NAME and PR_REVIEW_NAMES

   [[orders.overrides]]
   name = "pr-review-feedback"
   [orders.overrides.env]
   # the same five lines, plus PR_REVIEW_PANEL = "<the quality pack's out dir>/pr-review-panel.json"
   ```
   and add that panel path to the quality pack's `quality-stops` override:
   `QUALITY_PANELS = "<out dir>/pr-review-panel.json"` (the Today page shows it as a card).
3. Check: `gc order run pr-review-sweep`, then `tail $PR_REVIEW_STATE_DIR/pr-review.jsonl`, and
   `assets/scripts/pr-review-regress.sh` (every fixture as expected).

Going live is one line: `PR_REVIEW_MODE = "live"` on `pr-review-run`. Leave it at `shadow` until
the reviewer says so.

## Traps

- One pending review per user per PR: if the reviewer already has a draft review on a PR, the post
  fails (`pending-exists` in the log) and the outcome is still recorded.
- `claude -p` resolves from `PR_REVIEW_CLAUDE`, then PATH, then `~/.local/bin/claude`. An order's PATH
  can be short.
- The first review of a large repo fetches its blobs into the mirror (hundreds of MB once).
