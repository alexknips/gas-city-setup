# pr-review

A Gas City pack that reviews a stream of pull requests for one person and surfaces only what needs
them. It is three orders and a few scripts: no agents, no formulas, nothing in the core.

- **`pr-review-sweep`** (every 5 min, no model): finds each new head of an open PR, puts it through
  zero-token rules, records one bead per PR, and queues the PRs that need a review. Merged and closed
  PRs are never reviewed (R15).
- **`pr-review-run`** (every 5 min): reviews the next queued PR with one headless, read-only `claude -p`
  run (a daily cap only when PR_REVIEW_DAILY_CAP sets one; each call is bounded by turns). The script, not the model, renders the review in the style guide's shape,
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
| `PR_REVIEW_MODE` | `shadow` | `shadow`: the Slack words say what the review would do. `live` (only on the reviewer's word, with a bot identity) is not built yet. |
| `PR_REVIEW_REQUEST_CHANGES` | `off` | `on`: a `changes` outcome is a SUBMITTED REQUEST_CHANGES review that says "Changes requested" (R35), and a newer review of ours dismisses our earlier ones so a later COMMENT does not leave them blocking; `off`: a COMMENT review that says "Changes suggested". Only on the reviewer's word. |
| `PR_REVIEW_APPROVE` | `off` | `on`: a low-risk PR that meets the bar (R27) gets a SUBMITTED APPROVE review; `off`: a COMMENT review that says "Low risk". Only on the reviewer's word: SOC 2 reads an approval as human review. |
| `PR_REVIEW_JUDGE` | `on` | The reader test (R26): a second model scores each review before delivery and holds it below markers 4 or useful 3; the feedback order scores delivered reviews that have no scores yet. |
| `PR_REVIEW_JUDGE_MODEL` | `sonnet` | Model for the reader test (about $0.01-0.025 a review). Calibration 2026-10-29 on the 7 delivered v3 reviews (all poor by the first, Opus reader test) and 5 synthetic v4 reviews: Sonnet held 5 of the 7 and passed 3 of the 5 (the 2 it held are thin by construction: CI only, billing only); Haiku ($0.001) held all 12 with the plain rubric and 1 of 12 once told which parts the style guide requires, so it does not separate. One run each: scores vary between runs near the bar. |
| `PR_REVIEW_JUDGE_MAX_TURNS` | `6` | Turn limit (`claude --max-turns`) per reader test, money-lines call (R32, the same model) and benchmark label. No dollar limits anywhere (decision-fff4a691); cost and turns are logged. |
| `PR_REVIEW_PROMPT` | `security` | The review prompt base: `security`, Claude Code's /security-review ("not a general code review": security only), or `correctness` (`assets/correctness-review.md`: logic, broken contracts, edge cases, error handling, data integrity, security). The benchmark compares them (gc-sy1hm.2); the default changes only on the reviewer's word. |
| `PR_REVIEW_SUGGESTIONS` | `off` | `on` lets each "change" item carry a one-click fix: the schema gets a `suggestion` field (the whole replacement for the item's lines) and the inline comment ends with it as a GitHub suggestion block (R4). The script drops a fix the gate would refuse (on a question or the old side, more than 12 lines, a code fence inside, or no change) and keeps the item. With `off` the schema and prompt are unchanged. Part of the benchmark's version. |
| `PR_REVIEW_GAP_ASK_TO` | (unset) | Where `pr-review-feedback` mails the one rule-level ask per new kind of gap in our method (`gc mail send <address>`; decision-3aeec149). Unset: the kinds are counted in its output only. |
| `PR_REVIEW_VOICE` | `disclose` | `reviewer` makes the review speak as the reviewer (decision-ed7320b7): line 1 is the verdict word and an invisible marker, line 2 what the author must do, a not-checked area is said as scope, and the gate refuses any disclosure phrase (R28, R38). `disclose` keeps the honesty note on line 1. Set it only for repositories whose reviewer asked for it. |
| `PR_REVIEW_INCREMENTAL` | `off` | `on` makes the review of a new head of a PR we reviewed incremental ([The review prompt](#the-review-prompt)): the prompt gets our earlier asks and the new commits' diff, the body says what became of the asks, and our earlier threads are resolved where the new commits back it. With `off`, or when there is no earlier review, the prompt is unchanged. Part of the benchmark's version. |
| `PR_REVIEW_CONTEXT` | `off` | `on` adds REPOSITORY CONTEXT to the prompt ([The review prompt](#the-review-prompt)): where the code outside the diff uses what the PR changes, where the code the new lines call is defined, and the tests. Zero tokens to build; part of the benchmark's version, so on and off score apart. With `off` the prompt is unchanged. |
| `PR_REVIEW_DIGEST_MODEL` | `sonnet` | Model for the repository's rules digest (R37): one pass per change of the repository's docs, cached by their blob SHAs. |
| `PR_REVIEW_DIGEST_MAX_TURNS` | `6` | Turn limit per digest pass. |
| `PR_REVIEW_JUDGE_PER_RUN` | `5` | Delivered reviews the feedback order scores per run. |
| `PR_REVIEW_EFFORT` | `high` | `claude --effort` for the review run. |
| `PR_REVIEW_REPOS` | (none) | `owner/repo=rig …`. The rig is where the PR's bead goes; without `=rig` it is the repo name. |
| `PR_REVIEW_REVIEWER` | (none) | GitHub login whose review requests select a PR, and whose `gh` login posts the reviews. |
| `PR_REVIEW_REVIEWER_NAME` | the login | The name in every review's footer ("<name> has not read this PR"). |
| `PR_REVIEW_NAMES` | (none) | `login=Name …` for PR authors, as questions address them ("2 questions for the author"). |
| `PR_REVIEW_PANEL` | `<state dir>/panel.json` | Where the feedback order writes this week's numbers as a Today card; point the quality pack's `QUALITY_PANELS` at it. |
| `PR_REVIEW_KEEP_DAYS` | `30` | How long what is kept host-local stays. The held attempts, the benchmark's reviews, the reader's reasons and the client entry's words: older rows are dropped on the next write and by every sweep. A delivered review's result (`feedback/posted`): kept while its PR is open, dropped this many days after the PR merged or closed (by the sweep). |
| `PR_REVIEW_CLIENT_SLACK` | `off` | `on` posts each new review's entry (two lines, see [The client's Slack](#the-clients-slack)) in the client's own Slack workspace, as the reviewer, through the runner there; `dry-run` prints the entry's enums and counts instead. |
| `PR_REVIEW_CLIENT_SLACK_CHANNEL` | (none) | The conversation id the entries go to. It must also be in the allowlist. |
| `PR_REVIEW_CLIENT_SLACK_CONVERSATIONS` | (none) | The allowlist: the only conversation ids the runner may post to, enforced in code around the call. |
| `PR_REVIEW_CLIENT_SLACK_TOOL` | (none) | The connector's post tool, by its full name (`--check` lists the connector's tools). |
| `PR_REVIEW_CLIENT_SLACK_REPOS` | (all) | The repositories whose reviews get an entry there. |
| `PR_REVIEW_CLIENT_SLACK_MODEL`, `_MAX_TURNS` | `haiku`, `3` | The runner's model and turn limit. |
| `PR_REVIEW_CLIENT_SLACK_QUEUE_AT`, `_TZ` | `08:00`, `Europe/Berlin` | When the queue post is due: once a weekday from this local time (order `pr-review-client-queue`). |
| `PR_REVIEW_SLACK_CHANNEL` | (none) | Channel id for outcome links. Empty: no Slack. |
| `PR_REVIEW_SLACK_ENV` | `~/.config/gc/slack.env ~/.config/gc/slack2.env` | Files searched for the first `xoxb-` bot token. |
| `PR_REVIEW_DAILY_CAP` | `off` | Model reviews per UTC day (a number), or `off` for no cap. CI escalations do not count. |
| `PR_REVIEW_RESERVE` | `3` | Slots of the cap that tier-2 PRs may not use, kept for later asked-for and serious PRs. |
| `PR_REVIEW_MAX_WAIT_H` | `24` | A queued PR not reached in this many hours closes as `over-cap`. |
| `PR_REVIEW_LOOKBACK_H` | `2` | How far back the first sweep looks. Later sweeps start at the previous one. |
| `PR_REVIEW_SWEEP_BUDGET_S` | `420` | A sweep stops taking new heads after this many seconds (the order times out at 10 min). Each PR is recorded as soon as it is looked at; the rest waits for the next sweep. |
| `PR_REVIEW_CI_WAIT_H` | `3` | How long a head with running CI waits before the rules take CI as it is. |
| `PR_REVIEW_RULES` | `assets/rules.toml` | The rule file ([Rules](#rules)). |
| `PR_REVIEW_CHECKLIST` | `assets/checklist-health.md` | Custom scan instructions appended to the review prompt. |
| `PR_REVIEW_MODEL` | `opus` | Model for the review run. |
| `PR_REVIEW_MAX_TURNS` | `33` | Turn limit per review (`claude --max-turns`; reviews used 2-11 turns). |
| `PR_REVIEW_TIMEOUT_S` | `1200` | Wall-clock limit per review. |
| `PR_REVIEW_STATE_DIR` | `<city>/.gc/runtime/pr-review` | `prs.json` (state) and `pr-review.jsonl` (one line per run or action). |
| `PR_REVIEW_WORK_DIR` | `/var/tmp/gc-pr-review` | Bare blob-less mirrors and per-review worktrees: the repositories' code. Mode 700, outside the town runtime, and not under `/home`, `/data` or `/tmp`, which the review model may not read. |
| `PR_REVIEW_BEADS` | `on` | `off` skips the beads (tests). |
| `PR_REVIEW_MAX_LOAD` | `15` | Above this 1-minute load average a run skips and logs. |

## The client's Slack

decision-14bb1b55: the client's own Slack workspace, where our bot cannot post, is reached as the reviewer
through their claude.ai Slack connector, in exactly one place: `pr_client_slack.py`'s runner.

- The fence: a server-level deny on the connector in the user settings
  (`permissions.deny: ["mcp__claude_ai_Slack"]`) removes its tools from every normal session. The pack's
  own model calls use `--strict-mcp-config`, which loads no claude.ai connector.
- The runner: `claude -p --restricted --tools "" --allowedTools <the post tool>`. `--restricted` ignores
  the settings files, so the deny and the town's hooks do not apply. It also gets a PreToolUse hook
  (`pr_client_slack.py --hook`) through `--settings`, the stripped environment of the review runs,
  capture off, no session kept, MCP connected before the first turn, and a small model bounded by turns.
- The hook lets exactly one call through: the post tool, to the expected conversation (in
  `PR_REVIEW_CLIENT_SLACK_CONVERSATIONS`), with the exact text the script built and no other field (a
  thread, say). It refuses any other call and a second post. After the run, the transcript's calls are
  checked against the same rule. A post counts as landed (`ok`) only when the send tool's result carries
  the message's ts (or a permalink holding it); a successful call without one is `sent-unconfirmed`. The
  run record keeps the ts and the permalink.
- The text is the PR's entry (below): fixed words, counts and links built by the script, never a word
  meant for the reviewer only ("Needs you", "Review gap"). Its 3-5 words on the PR are the only words a
  model wrote. Logs keep ids, enums and counts, never those words.

Before each post a probe lists what the runner's session loads, and the post run denies every other tool
by name (tool-level denies hide them from the model), so it sees exactly the post tool; no post when the
connector does not offer it. `pr_client_slack.py --check` lists, by name and count, what a normal session
(the user's settings, from the city directory) and the runner load, and the runner's view with the
denies; it passes when only the runner has the connector's tools and, with the denies, exactly the post
tool. A stronger fence than the user settings' deny: `ENABLE_CLAUDEAI_MCP_SERVERS=false` in the town
sessions' environment turns every claude.ai connector off whatever settings a session loads (the runner
sets it to `true` for itself); it also takes the other connectors from every session, so the town
decides. `--post-review OWNER/NAME#N` posts a PR's entry as it stands now by hand, for example the
last reviews again in a new format (with `PR_REVIEW_CLIENT_SLACK=dry-run`, its enums and counts only).
The first real post is a test with the town's owner.

The entry, two lines per PR (the mayor, 2026-10-30 19:40Z and 19:41Z):

    <PR link> · <3-5 words on what the PR does> · <high|medium|low> confidence · <status>
    <readiness> · CI <...> · +<added>/-<removed> in <n> files · <tests> · <c> changes, <q> questions open · <n> flagged

- **The words**: the review's summary when it is 3-5 plain words; else one narrow turn (the judge model,
  2 turns at most) gets the summary and its word count and gives up to three versions, and the script
  takes the first that passes; else the PR title's first plain words. A plain word is letters (or an
  acronym, or a number of up to 4 digits), at most 19 characters, so no identifier, path, date, long
  number or secret fits. The words are kept host-local (`client-slack/words.json` in the work dir, mode
  600, `PR_REVIEW_KEEP_DAYS`) for the queue, which makes none (zero tokens).
- **Confidence**, a fixed rule, never the model's say: high for a full review with CI green, nothing in
  a not-checked area, no open question (an item, decision or blocker asking), every flagged hunk in the
  review and no open gap in our method; medium for any other full review; low for a fallback (the note,
  its floor, the money review) or a held run on the head. Left out when nothing of ours is on the head.
- **Status**: our review's word when the latest run delivered a full review on the PR's current head
  ("Approved", "Changes requested", "Changes suggested", "Questions", "Low risk"); else "Waiting for
  review".
- **Readiness**, the first that holds: Conflict, CI failing, Needs changes, Needs answers, Waiting for
  review (no full review of ours on the head, or one without our approval), CI running (approved, checks
  pending), Ready to merge. A PR posted by hand after it closed: Merged or Closed.
- **CI**: failing, pending, skipped or none; green with whether the PR's first push passed the required
  checks (the quality pack's first-pass rule: the branch's rulesets, else its protection; a re-run of a
  red check is not a first pass), "green first push" or "green after N pushes".
- **Tests**: "tests changed" (next to other changes), "tests only" or "no test changes", from the paths
  (the rules' test paths and test or spec names); left out past 100 files.
- **Open asks**: the changes and questions the delivered review leaves open, counted as the outcome
  counts them (its items and blockers by kind, each decision a question), flagged by the rules or not;
  "nothing open" when there are none. So Needs changes always has a change open, Needs answers a
  question, and Ready to merge nothing (a self-test checks this over every outcome). Then "<n> flagged"
  only when the rules flagged a hunk. For a delivered review only. Line 2 over 120 characters drops the
  files count first.

A head's entry goes out once: the PR's state entry keeps the client posts per head (status and ts), so a
same-head re-review sends no second one, whatever our own channel does.

The queue post (order `pr-review-client-queue`, hourly; `pr_client_slack.py --queue [--now]`): once a
weekday from `PR_REVIEW_CLIENT_SLACK_QUEUE_AT`, the open PRs (not drafts) that request the reviewer or
carry a review of ours, one entry each, a blank line between them. The status is our review's when the
PR's latest run delivered a full review on its current head; else "Waiting for review": no review yet,
new commits since, or the latest run was held or a fallback, so the full review is still owed. The
words are those kept at delivery, else the title's. No post when the list is empty. A failed post is
tried again on later runs of the day, 3 times at most. The same runner and hook carry it; its text goes
to the model between two marker lines, and the hook compares it up to line endings and blank lines.

## What never leaves GitHub

Pull requests can carry regulated data (health records, PHI) or code under SOC 2 controls. So the
pack keeps the repository's text on GitHub and in the work dir, and nowhere else:

- **Beads** hold the repo, PR number, link, head SHA and outcome. No title, path, finding or rule id.
- **The state file and the log** hold those, plus rule ids, tier, CI state, times and cost. Rule ids
  are this pack's own words.
- **Slack** gets `<outcome word>: <PR link>`. The script builds the message from a fixed word and the
  link; no model text reaches Slack. The client's own Slack gets the PR's entry: the only model text in
  it is 3-5 plain words on what their PR does, checked by the script.
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
| `money` | A3 | NOT CHECKED (`not_checked_areas`): billing, invoice, pricing, subscription, payment, checkout, Stripe, credit and plan paths; added lines with credits or plan ids. It never selects a PR; on a reviewed PR it makes the review say the area is not checked and ask the reviewer to look (R20). |

Blind spots of every pattern rule: it is a match, not a judgement, so whether a hit is a real risk is
the review's job. GitHub leaves the `patch` out of very large files, so their lines are not matched
(the backtest counts how often: `patch_missing_prs`). A3 (money) is not checked: the `money` rule only
finds where a reviewed PR touches it. Backtest (2026-10-29, 7 days, 336 PRs of the first city, each
flagged PR's matched lines judged by a model with capture off): it flagged 24 PRs, 20 of which touch
money (83%), and found 20 of the 22 that do. Wider terms (stripe, billing, invoice, price, subscription
or refund in added lines) flagged 21 more PRs that do not, so they are left out; the review model lists
the money lines it sees as well, which covers part of the gap.

## Priority and the cap

Every selected PR gets a tier: **0** the reviewer was asked, **1** a serious rule tripped (CI,
migration order or edits, conflicts, destructive statements, EHR writes, PHI in logs or fixtures,
secrets), **2** any other select rule. Each run reviews the first queued PR by tier, then by more
distinct areas, then the oldest. A tier-2 PR runs only while more than `PR_REVIEW_RESERVE` slots of
the day's cap are left. A PR the cap never reaches closes as `over-cap` after
`PR_REVIEW_MAX_WAIT_H`. Its bead says so, and Slack gets nothing.

A new push to a PR already reviewed is not reviewed again, unless the new head trips a serious rule
the reviewed head did not, or the reviewer's review is requested again. A PR that requests the reviewer
is always selected: a request added after the sweep set the PR aside (`no-rule`) queues it, and so does
a request on a new head after a review (a submitted review clears the request, so a pending one is
new). A head already reviewed or held is never queued again. A PR that requests the reviewer always
gets the model, whatever CI says (R34).

The sweep also tends our formal blocks (R35). On a new head of a PR whose live REQUEST_CHANGES of ours
rested only on a merge conflict or CI, it dismisses the block once the PR is mergeable and CI is green
("Lifted: …", zero tokens). A block that rested on content queues the new head for a review instead,
once. A PR's entry keeps its delivered review (id, heads, event, Slack ts, feedback) across new heads.

## The review prompt

The prompt is Claude Code's `/security-review` command, copied unchanged into `assets/upstream/`
from [anthropics/claude-code-security-review](https://github.com/anthropics/claude-code-security-review)
(MIT, Copyright 2025 Anthropic; see `assets/upstream/SOURCE`). `pr_review.py` adapts it at run time:

- The command's `git status / diff / log` substitutions become the PR's link, title and description,
  its changed files, its commits and its diff (from where the PR forked off its base). A diff over
  350k characters is left in a file the model reads in parts.
- The checklist (`PR_REVIEW_CHECKLIST`), the style guide (`assets/style-guide.md`), the repository's
  rules digest (R37) and the hunks the rules flagged (path and line, found in memory) go in before
  `START ANALYSIS`.
- The closing steps are rewritten. Who reads the review (the reviewer, who does not know the codebase,
  and the author, who wrote and reviewed the change) and its job: consistency with the repository's
  goals and rules, plus the scan's issues. The upstream command starts sub-tasks; here the model finds
  the issues, filters false positives and drops anything below confidence 8 itself, because the run
  has no Task tool. Each flagged hunk becomes a decision, an item or a cleared entry (R19).
- The answer is JSON (`--json-schema`): decisions at the level of goals and rules (options, the
  recommended one and a reason); at most 5 items, worst first, each anchored to a line range on one
  side of the diff, with a kind, severity, area, what changed in 3-6 plain words, why it matters and
  the ask (at most 25 words each), the repository rule it is about, what was verified, and a
  confidence; the cleared entries; a one-sentence summary and the skip list by group; the B3 checks.
  The model does not pick the outcome.

**The rules digest** (`pr_digest.py`, R37). The repository's goals and high-level rules, as its own
docs write them down (README, AGENTS.md and CLAUDE.md, CONTRIBUTING, SECURITY, docs/, the PR template,
the CI workflows; at most 160k characters, policy docs first), taken by one model pass with no tools
and capture off, at most 40 rules, each with its doc lines. It is cached by the docs' blob SHAs at the
tip of the base branch the PR merges into, so it is rebuilt only when the docs change, and kept host-local
(`PR_REVIEW_WORK_DIR/digest`, mode 600): it is the repository's text. The review links each cited rule
to its doc lines at that commit. `pr_digest.py --build REPO` builds it ahead of time (counts only).

**Incremental reviews** (`pr_incremental.py`, gc-sy1hm.2 c; `PR_REVIEW_INCREMENTAL=on`). The sweep queues a new
head of a reviewed PR as before (the reviewer asked again, a new serious rule, or our formal block rested
on content), and the review of that head is still a whole review, one per head (R18). Three things are added:
- **The prompt.** It lists our earlier asks for the author (from the delivered review's host-local copy):
  where their lines sit at the new head and whether the new commits changed them. It also carries the new
  commits' diff (old head..new head). Step 0 has the model call each earlier ask fixed, open (then it is
  an item again) or gone, and review the new commits first.
- **The threads.** After the new review is posted, our earlier threads are resolved only where the zero-token
  check backs the model: an ask called fixed or gone whose lines the new commits changed, or one the new
  review raises again on overlapping lines. Every other thread stays open.
- **The body.** One line, linked to the new commits, says what became of the earlier asks; in an approval
  it is the first line of "What was checked". A low-risk review, and so an approval, is refused while an
  earlier ask is still open (R2).

If the history was rewritten (the old head is not an ancestor of the new one) or GitHub no longer serves the
old head, the review is the whole review it always was. Blind spots: an ask the model calls fixed by a change
elsewhere (a caller, say) keeps its thread open, and an ask anchored on removed lines is not mapped to the
new head. Traffic on 10-30: of 17 reviewed PRs, none had a review of a second head yet (most merge within
half an hour). Checked by `pr_incremental.py --selftest` (line mapping, buckets, thread choice, the line)
and fixtures case78-case80. The thread resolution itself has not run against GitHub yet.

**One-click fixes** (gc-sy1hm.2 b; `PR_REVIEW_SUGGESTIONS=on`). A fix is code, so it is not cleaned like the
model's prose (that would break generics and JSX). It is shown only inside a suggestion block it cannot
close, and nothing inside a code block is rendered (no link, image or mention). It still goes through the
credential check with the rest of the review. What it cannot check: whether the fix compiles or is right.
The prompt asks for code the model has checked, and the author applies it or not.

**Repository context** (`pr_context.py`, gc-sy1hm.2 a; `PR_REVIEW_CONTEXT=on`). A word search (git
grep) in the worktree at the PR head, no tokens, at most 6000 characters of names, paths and line numbers
before the flagged hunks; step 1 then asks the model to open them. Three lists: (1) existing code the PR
changes (a definition line that changed, a definition removed from a file, or a body with a changed
line; at most 12, in that order and then by changed lines), with its uses outside the diff (at most 5
each; a name with more than 60 uses is called too common); (2) existing code the new lines call or render
(JSX components too), with its definition, most called first, at most 12; a name with more than 2
definitions is left out as ambiguous; (3) the tests that use the changed code, the test files named after
the changed files, and the changed files without one. Code the PR adds is not in list 1, since its uses
are in the diff. Blind spots: the search is textual, so two unrelated things with one name are listed
together, and a call through a variable, a string or dependency injection is not found. Symbols in
test files are not listed. Definitions are found by line patterns for TypeScript/JavaScript, Python, Go and
Rust; other languages get the tests only. Checked at zero tokens on the benchmark's labelled defects
(10-30): in 28 of 37 cases (21 of 29 and 7 of 8) a line a later PR fixed sits inside a listed changed
symbol or calls a listed definition. A listed place sits in a file that imports the defining module in
74% and 85% of uses and 81% and 73% of definitions, a lower bound since a method called on an object needs
no import. Built in 1-27 s per PR. `pr_context.py --selftest` checks it on a small synthetic repository.

**The money lines** (`pr_money.py`, R32). For a PR that touches billing, plans, credits or pricing, one
call with no tools reads only those hunks and writes one neutral line per changed place (what it does to
charges, credits, plans or limits; clipped at 30 words), its kind, whether it changes behaviour and its
risk, plus one sentence on what the whole change does and its default state, and the question on its
concrete effect after merge; in calls of 20 places. The money question (the author's, R20) shows its block first (risk, question,
options, recommendation), then the evidence; each place is linked once. A place
that comes back without a line gets one more try, then keeps its link with "(not summarized)". It runs apart from the review, so the money question
keeps its lines even when the full review is held.

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

One per reviewed PR, derived by the script (`pr_format.decide`), not chosen by the model. Line 1 of
every review is the outcome word with the automated note (R28); line 2 says who acts (R22).

| Outcome | When | Line 2 | Event | Slack |
| --- | --- | --- | --- | --- |
| `escalate` | no longer an outcome on the PR (decision-3aeec149): every finding goes to the author. A gap in our method (R16) keeps the PR's normal review and changes only the Slack word | (older reviews: `**Needs you** · …`) | COMMENT | "Review gap" for an open gap, whatever the outcome |
| `changes` | a change for the author, a merge conflict or failing checks | `**Changes requested** · …` / `Reviewer: nothing. Author: 1 change, …` (`Changes suggested` with `PR_REVIEW_REQUEST_CHANGES=off`) | REQUEST_CHANGES with `PR_REVIEW_REQUEST_CHANGES=on` (R35), else COMMENT | "Changes requested" / "Changes suggested" |
| `questions` | only questions for the author: a decision whose options are all the author's (R16), the sign-off on a not-checked area (R20, R32), or CI that did not run, was skipped or is still running | `**Questions** · …` / `Reviewer: nothing. Author: 2 questions.` | COMMENT | "Questions for the author" |
| `not-reviewed` | the PR requests the reviewer and its review was held twice (R33) | `**Not reviewed automatically** · …` / `Reviewer: nothing. Author: 1 question, …`, then why it was held, the linked places the rules know, and "Who reviews this PR before it merges?" for the author with lettered options | COMMENT | "Not reviewed" |
| `low-risk` | nothing for anyone: every flagged hunk cleared, CI passed, no conflict | `**Approved as low risk** · …` / `Nothing for Reviewer or Author.` and "What was checked" collapsed (R36) (or `**Low risk**` with `PR_REVIEW_APPROVE=off` or short of the bar) | APPROVE under the bar (R27), else COMMENT | "Approved as low risk" / "Low risk" |

Rule flags alone never route a PR to the reviewer: they tell the model where to look first, and the
review shows each one cleared (with a link) or acted on (R19). `pr_review.py --redeliver REPO#N` posts
the stored result for the current head again without a model run (once per head, R18).
`pr_review.py --rereview REPO#N` reviews a head again after a rule change: the earlier review is
updated in place when it can be (same event, no inline comments), else cut to a pointer to the new one
(R24), and its Slack message is updated in place. After every post, each pointer of ours on the PR follows
the newest review (v5.8: a chain of pointers left the oldest one on a replaced review).
`pr_restamp.py` (decision-ed7320b7; zero tokens) rewrites the reviews we delivered, and our inline comments,
into the reviewer's voice from the text GitHub holds: line 1 the verdict word and the marker, line 2 what
the author must do, the disclosure lines dropped, "not checked by any agent" said as scope, a section for
the reviewer renamed "Open questions"; findings, links and options stay. It knows every shape we delivered
(v3, v4, v5-v5.5, the notes and pointers) and leaves alone, counted, a review whose text would still hold a
disclosure phrase (R38). It finds the PRs by GitHub's search (reviewed by the reviewer on the configured
repositories) as well as the state, so a review older than the state is found too; `--ours ID,...` also
treats the named review ids as ours (a body whose marker was removed by hand). Either way it counts the
reviewer's reviews that have a body but no marker (`unmarked_with_body`, with their ids): a lost marker, or
the reviewer's own words, never touched unless named with `--ours`. A review already in the
voice gets the v5.8 changes: line 2 names the author when nothing is needed, an approval with one cleared
entry shows its reason above "What was checked", and every pointer follows the newest review. A dry run by default
(counts only); `--apply` rewrites the reviews on open PRs, `--apply --merged` those on merged and closed PRs too. GitHub keeps each edited review's history, and a
dismissal message cannot be edited.

Other outcomes on a bead: `no-rule` (not selected), `over-cap`, `closed` (merged or closed before its
review was delivered, R15; the sweep closes the bead on `no-rule` and `closed`, and before v5.8 it never
closed an existing one: `pr_sweep.py --close-terminal [--apply]` closes those, and a second bead of one PR,
counts only), `held-gate` (the gate or the reader test failed twice; nothing delivered),
`held-secret` (the text held a credential shape). Neither is silent on a PR that requests the
reviewer: it gets the short note (`not-reviewed`, logged `fallback: not-reviewed`, R33). A PR that
touches a not-checked area whose review the reader test held twice gets the minimal review first,
logged `fallback: not-checked-minimal`. The posted review is never worse than the one it replaces (the
mayor, 2026-10-30 16:10Z and 16:36Z): a review held only by wording or format reasons is repaired, gated
again and read by the reader test instead of falling back. First one narrow turn (the judge model, at most
2 turns) rewrites the entries a wording rule failed (R29's lead words, length and identifiers; R5, R6, R10,
R14 and R25 on a field): it gets only those entries, the rule messages, their style-guide rows, each
field's budget with its count now ("3 to 6 words, it has 7") and the entries' own lines, and only the named
fields are taken from its answer. It gives up to three versions of each field, best first, and the script
takes the first that passes the field's own checks: the model does not count words reliably (v5.8.3: the
first live turns came back at 7 words for a 3-6 word field and 21 and 27 words for a 20-word ask). Then the script makes right what
it can from the parts: a question's ask without its question mark (R13), a cleared entry that repeats an
earlier one (R36), the citation of a rule the digest does not hold (R37). The log has `rewritten`,
`repaired` and `repair_held` (what still held it). A content reason (R19 coverage, the reader test) still
falls back, and a held run logs `held_by: format` or `content` (the Today card's fallback split, with the
R29 miss rate). A fallback passes the bar the full review
failed (R33). Since v5.9 (the mayor, 2026-10-30 21:39Z: 4 of 5 requested reviews fell back, and the reader
found a template) it first asks its own questions (`pr_fallback.py`): one question per linked place, at most
3, the riskiest first (the money places by their risk; for the note, the flagged hunks, filled up to 3 with
the largest changed hunks outside tests). A cheap reader of only those hunks and the PR's title and
description (the judge model, no tools) writes each question in up to 3 wordings with its 3-6 word lead and
its reason, and one "What it does" sentence; the script takes the first wording that passes the gate's own
field rules, fails a question that fits any PR (tested? who reviews? sign-off?), a repeated one or a code
name, drops a "What it does" that names code, and gives the places that failed one more turn with why and
their word counts. Each question is a body item on its lines, its
reason the inline comment. No scope sentence and no options; the places not asked about are collapsed under
"Other places", each linked to its lines (never a link to the files view no line anchors); a blocker says CI
or a conflict once, with no status line repeating it; in the reviewer's voice line 1 says who acts
("**Questions** · for <author>, nothing needed from me"). Only when no question passes, or the gate holds them,
does the stock form go out: the money question with its places cut to the question's own and at most 2
more, or the note with 3 places, the rest collapsed and linked likewise. The log says which form went out
(`fallback_form`: asked, stock or floor) with `fallback_asked`, `fallback_kept`, `fallback_calls`,
`fallback_failed` (rule ids), `fallback_gate` and `fallback_cost_usd`. Measured on the four fallbacks of
2026-10-30 21:29Z, rebuilt from their held attempts with nothing delivered: the reader test passed all four on
each of three readings (useful 3, markers 4-5, plain 4; the stock forms had scored useful 2, 3, 3, 4 and
plain 3), at about $0.01-0.04 per fallback for the questions. When GitHub refuses a fallback's inline comments,
the same body goes out without them (logged `inline_dropped`): a fallback is never silent. The gate holds a fallback over 8 links,
17 lines or 2,100 characters shown outside "Other places" (the largest full reviews that passed the reader
test). The reader's reasons for each held attempt go to
`PR_REVIEW_WORK_DIR/feedback/reader.jsonl`. Never silent (the mayor, 2026-10-30 16:54Z): when the note
itself is held, the floor goes out instead, the note with one place in the script's own words (a flag's
rule label, or the area's name for a money place). It holds no model text, so it is posted whatever its
gate says (logged `floor`, `note_gate`, `floor_gate`), unless it matches a secret's shape; `pr_review.py
--selftest` covers it, and the fallback's own questions with a stub for the model (asked, held by the gate,
none passing, the money form with red CI); `pr_fallback.py --selftest` the places' order, the answers'
checks and the second turn. The held attempt itself (the review dict, its body, the
gate's reasons, the flags and the diff) to `feedback/held.jsonl`, so `pr_format.py --regate FILE` gates it
again with later code at zero tokens (PR numbers and rule ids only). Both hold a client's PR descriptions and diffs, so rows older than `PR_REVIEW_KEEP_DAYS` (30) are
dropped on the next write and by every sweep. The sweep also drops a delivered review's result
(`feedback/posted`) 30 days after its PR merged or closed (`--redeliver` never needs a closed PR's), and
logs the rows or files dropped per store (`pruned`: held, bench, reader, words, posted). Also `held-inline` (GitHub refused an inline comment;
nothing delivered, since a review without its markers breaks R4), `error` (the run failed, or a
delivered review broke a delivery rule; see the log). A PR whose head or CI changed between the review
and the post goes back to the queue (`requeued`).

## Quality loop

The reviewer's complaints about the first shadow reviews (2026-10-29: "it just fluff and really hard to
read. there are no markers in the review what I actually need to manually review"; then the pending
reviews in his name blocking his own; then "that review comment needs to link to the human decisions
… otherwise you do not need me!") became a loop, so a complaint is fixed once and stays fixed. The
measure is the reviewer's effort: time from the Slack post to his own decision on the PR, what he
opened, his thumbs on the Slack link. Not review volume.

1. **Style guide** (`assets/style-guide.md`, versioned, v5). Rules R0 to R37 (no R12), each with the
   complaint it came from: never a pending review (R0); the verdict first with the automated note, then
   who acts (R28, R22); "needs you" only for decisions only the reviewer can make, with an option they
   carry out, at the level of goals and rules, each linked (R16, R37); a decision whose options are all
   the author's is a question for the author; at most 5 items for the author, each linked to the exact diff
   lines, leading with what changed in plain words, said once (R21, R29); every flagged hunk cleared
   with a link or acted on (R19), a risky "cleared" turned into a question (R31); a not-checked area
   handed to a person with one line per changed place (R20, R32); what the PR does and a linked skip
   list (R7, R30); the status line only when something did not pass (R30); no rule codes or boilerplate
   (R10, R23); never for a merged PR (R15); every review at its own URL (R17), one per head (R18, R24);
   an approval only under the bar, and short (R27, R36); never silent on a requested PR (R33); the word
   matches the event (R35). The model reads it in its prompt.
2. **Gate** (`pr_format.gate`, zero tokens). Every machine-checkable rule, against the model's items and
   the rendered text: each item's range resolved against the diff's hunks on its side, each decision's
   code link resolved against the head (the file exists, the lines are within it) and asked as one
   question with options, fields of at least four words with no filler (a deny-list) and no rule codes,
   the sizes, line 1 and 2, every flagged hunk covered (R19), the not-checked area (R20), repeats (R21),
   cut links (R25), the leading words of each entry (R29), the status line and the skip list (R30), the
   money places (R32), the approval's short form and its links on code, not comments (R36), the rule
   links and decisions that ask the reviewer to read code (R37). Then the **reader test** (R26,
   `pr_judge.py`): a second, cheap model with no tools and capture off scores the review 1-5 on scan,
   markers, decisions, plain, length and useful, the rubric of the town's first reader test; below
   markers 4 or useful 3 the review is held. The reader also names cleared entries that describe a
   possible risk without a reason (R31; still named after the second answer, they become questions for
   the author) and a behaviour change in the skip list (R37; still there after the second answer, the
   review is not an approval). Right before delivery: the PR is still open and on the
   reviewed head (R15), the approval bar if it is one (R27), and after posting: its own review URL, not
   pending (R0, R17), every inline comment anchored to a line (R21), one review of ours on the head
   (R24). On a gate or reader failure the model answers again with the reasons (and the reader's
   problems). A second failure posts nothing (`held-gate`). The log keeps the rule ids of each failure
   (`gate_first`, `gate`) and the reader's scores, so the score can count holds and regenerations.
3. **Feedback** (`pr-review-feedback`, hourly, zero tokens). For each delivered review: the thumbs-up or
   thumbs-down and the replies on its Slack message; the reviewer's own GitHub review after delivery and
   the minutes to it, or the PR merged or closed before they looked; a "needs you" they approved as is
   (false alarm). A verdict is never inferred from someone else's action: `--record KEY VERDICT --by WHO`
   records what was seen by hand and who did it. Blind spot: which items on the page were opened (the
   page server keeps no log the pack reads). The text (replies, notes, what was delivered) stays in
   `PR_REVIEW_WORK_DIR/feedback` (mode 700); the state file holds counts. The week's numbers (delivered,
   thumbs, median minutes to the reviewer's own review, PRs merged before they looked, false alarms, gate
   holds, approvals, and the reader test's median useful and markers over the delivered reviews) go to
   `PR_REVIEW_PANEL`, a card on the quality pack's Today page. The same run gives the reader test to up
   to `PR_REVIEW_JUDGE_PER_RUN` delivered reviews without scores (each is read once; about $0.02 each).
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
decision (R16). Case 4: a delivered review without its own URL (R17); case 5: delivered as a page
(R17). Cases 18-32 (the reader test of 2026-10-29, 21:05Z): an uncleared flag, a cleared entry outside
its hunk (R19), billing called low risk (R20), a finding said twice, a suggested reply, an unanchored
inline comment (R21), line 2 without who acts or a blocker only in the facts line (R22), a rule-codes
section or a stray not-checked footer (R23), a comment plus an approval (R24), a "[link removed]"
fragment (R25), a reader score below the bar (R26). Case 37: the minimal billing review (R33).
Cases 49-62 (v5): the honesty line above the verdict (R28), a cleared entry that leads with a
category label (R29), the status line on a green PR and a skip list without links (R30), a risky
cleared entry and the question it becomes (R31), a money place without its line and "not checked" said
twice (R32), an approval with more than "What was checked", repeated cleared entries and a cleared link
on a comment line when the flag is on the code (R36), a decision that asks the reviewer to read code, a finding without its rule and
one that cites it (R37). Case 67: a cleared entry that names a raw identifier (R29); case 68: a
conflict blocker that names and links its files (R22). Cases 63-66: the sweep lifts a block that rested on a conflict once it is
fixed, keeps it while CI is red, sends a block on content to a review of the new head, and never loops
on a held head (R35). Host-local cases 09-11: the mayor's 9a62c85 runs on three real PRs (two expected
to fail; one passes with only the v5 format rules firing). Host-local cases 12-13: the db9026d reruns (the
bare note on a billing PR fails, R33; a questions review passes). Cases 45-46: the note with its floor
passes, a note without its decision fails (R33). Case 69: line 2 that misses the recommended option's task
(R22); case 70: a money review with its places collapsed (R32). Host-local case 14: the 29a3588 money review
(19 places one by one) fails (R32). Case 71: a money opening
without the places that back it (R32); host-local case 15: the dfc5ea2 money review (other places not
collapsed) fails (R32). Case 72:
a code place linked twice (R21). Cases 87-98 (v5.6, decision-3aeec149 and decision-ed7320b7): a finding with
choices goes to the author (passes), a gap of an unknown kind fails (R16), and the reviewer's voice: a
changes review, billing as scope, an approval, the R33 note as the author's question, choices that start
with "You" (pass), choices that name the author, a disclosure phrase in the body or an inline comment, the
old note on line 1 (fail, R22, R38, R28), and a delivered body in the voice (passes). Case 6 ("Needs you"
with a decision for the reviewer) fails since v5.6. Cases 104-106 (v5.7): a question its own explanation
answers is cleared (`judge_answered`: the reader's answer), and the PR then passes as low risk and as an
approval; a body item whose ask runs over 20 words fails (R29). Cases 107-109 (v5.8, the mayor, 2026-10-30
07:21Z): line 2 "Nothing needed." without the author's name fails (R22, R36); the money question first with
the line it depends on, then the scope line, the evidence after the decision and not nested under it
(passes; `body_re` and `body_not_re` pin the layout of a rendered case); a recommendation that repeats its
option's own reason fails (R16). Case 91 also pins the approval's reason line. Cases 110-111 (v5.8.1,
the mayor's P1, 2026-10-30 16:19Z): an approval whose flags sit on comments, one in a code hunk and one in a
hunk of comments only, cleared on those comments, with a line break in the summary (passes); a markdown
heading and bullet are text, not comments (passes). Cases 112-120 (v5.8.2, the mayor, 2026-10-30 10:25Z,
16:10Z and 16:36Z; `repair` runs the run's repair flow first, `rewrite` stands for the narrow turn's
answer): a low-risk approval held only by a repeated cleared entry is repaired and approved; a question
without its question mark is repaired; with no answer from the turn, R29 still holds it; the #1981 fallback
shape, 14 money places, renders the question's place and 2 more, the rest collapsed and linked to their
lines (passes), and the same places in the full layout as a fallback fail (R33, and R28: its line 1 does not
say who acts); entries that fail R29 are rewritten and the
review posted (the #1982 approval and #112 question shapes); R19 next to R29 is not rewritten (content);
an answer beyond the ask (another field, another entry) is ignored. Cases 121-123 (v5.8.3, the first live
turns): the turn's versions for a 7-word change and for asks of 27 and 21 words, the first version that
passes taken (with only the first version, both still fail R29); a note over ten places as a fallback
passes (on v5.8.2 it failed R33, and #112 got nothing). Cases 124-130 (v5.9, the mayor, 2026-10-30 21:39Z;
structure only): the fallback's own questions pass, one per linked place, the CI blocker said once, the other
place collapsed and linked, line 1 saying who acts; the same with a link to the files view no line anchors,
with the status line said again, with a question that fits any PR, or with line 1 without who acts fails
(R33, R28); the note asks the same way in the reviewer's voice and, with why it was held, in the disclosing
voice. Cases 81-86 (decision-554d2e7b, the reviewer on 2026-10-30 05:31Z: "you
wrote a review and [the author] just needs to answer not me"): a decision whose options are all the
author's passes as a question for the author and fails as "Needs you" (R16, R22); a billing decision for
the reviewer as v5.4 delivered it fails (R22); options that do not start with who carries them out fail
(R22); a decision without actors fails (R16). A rendered case can pin its line 2 (`line2`) and require that
its parts give its outcome (`derived`). `pr-review-regress.sh` also runs the selftests of `pr_restamp.py` and `pr_sweep.py` (the bead closing), and `pr_hygiene.py`: no public file of the pack names a
person or repository from the town's overrides (host-local; skipped without them). The same check covers
commit messages and a PR's title and body (decision-67f9a695, forward only), as a gate the refinery runs
at the PR head: `pr_hygiene.py --messages <base>..<head> --pr N --repo OWNER/NAME [--city city.toml]`.
There the reviewer's own login and name are allowed, matching ignores case, and it prints labels and
counts only.
Cases 38-48 (the test batch of 2026-10-29, 22:30Z): a review request added after the first sweep, a
held head not queued again and a request on a new head (the sweep's `asked_again`); a requested PR
without CI gets the model, an unrequested one does not (R34, R11); a held requested PR gets the note, a
held billing PR the minimal review, the note with its floor and a note without its decision (R33); a COMMENT
review that says "Changes requested" (fails) and a formal REQUEST_CHANGES review (passes) (R35). Cases 33-36: an approval with an uncleared flag,
with CI not green, on a changed head (all fail, R27), and a clean one (passes).

The regression covers the gate and the renderer, not the model: it cannot show that a prompt change
makes the model write better items. The feedback numbers measure that.

## Benchmark

`pr_bench.py` (gc-sy1hm.2) measures the reviewer on the repository's own history instead of claiming
quality. `--mine REPO` finds escaped bugs at zero tokens: merged PRs whose added lines a later fix changed
within 14 days (git blame in a separate full clone; the repositories squash-merge, so a blamed commit is
a PR), and controls (merged PRs whose added lines nobody changed in that time). `--label REPO` has a
capture-off reader judge whether each later PR fixes a defect of the earlier one. `--run REPO` reviews a
sample of the defects and the controls at their heads with the shipped pipeline (nothing delivered,
resumable, a spend cap) and `--score REPO` reports recall, recall on labelled defects, a precision proxy
(entries on the fixed lines over all entries), controls with a change ask, and cost per PR, per
version. The run also keeps the model's own entries with their confidence, so `by_confidence` scores the
same run at the bars 8 (what the script posts), 9 and 10. The version hashes the files that shape a review
and the settings `PR_REVIEW_PROMPT`, `PR_REVIEW_CONTEXT`, `PR_REVIEW_SUGGESTIONS` and `PR_REVIEW_INCREMENTAL`. Everything it reads and writes is the repository's text: `PR_REVIEW_WORK_DIR/bench`, mode 700.
Each review a run makes is kept whole next to its results (`<repo>-reviews-<version>.jsonl`, the same rows
as `feedback/held.jsonl`), so `pr_format.py --regate` gates a run again with later code at zero tokens.

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
