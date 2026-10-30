# Review style guide, v5.1

What a delivered review must look like. The review model gets this file with its instructions. The
gate in `pr_format.py` checks every rule marked **[gate]** before anything is delivered. A review that
fails is regenerated once with the gate's reasons; if it fails again, nothing is delivered and the PR's
bead says `held-gate` (a PR that requests the reviewer gets the short note instead, R33). Each rule
names the complaint it came from. A new complaint becomes a new rule and a regression case (README.md,
"Quality loop").

- v1, 2026-10-29 19:56Z. The reviewer on a shadow review: "it just fluff and really hard to read.
  there are no markers in the review what I actually need to manually review. this is not good enough yet".
- v2, 2026-10-29 20:08-20:21Z. Our pending reviews took his GitHub review slot ("what is also bad: is
  the comment and how difficult my human review now is"). Then: "I need a link to the review comment.
  that review comment needs to link to the human decisions (either in text or code reference that still
  need to be answered). otherwise you do not need me!" And on a merged PR: "was merged. we don't need to
  review it".
- v3, 2026-10-29 20:45Z: "no, don't link to the dashboard but actually leave a review comment every
  single time on the PR and then link to that". Pages are gone; every review is a submitted comment.
- v4, 2026-10-29 21:01-21:05Z: "actually approve low risk PRs not just say you do", and "review our
  reviews again. are they easy to read". A reader test of the 7 delivered reviews: they scan fast, but
  "nothing to check" hid fired rules and a PR that touches billing, findings were said three times, and
  a separate comment came with an approval. R19-R27.
- v4.1, 2026-10-29 21:57Z: the minimal billing review, after a real billing PR was held by the reader
  test three times.
- v4.2, 2026-10-30: R33-R35, after the mayor's test batch of 17 open PRs (2026-10-29 22:30Z): 6 held
  reviews left PRs that requested the reviewer silent, a PR without CI got no model review and no
  question, and a COMMENT review said "Changes requested". The reviewer, 23:09Z: "yes, we should use
  the formal github request changes for sure". The minimal billing review of v4.1 is part of R33.
- v5, 2026-10-30: R28-R32, R36, R37. The mayor's reader tests of the delivered v4 reviews (22:03Z-22:08Z,
  01:40Z): the disclaimer sat above the verdict, entries opened with category labels, identical
  boilerplate, a risky "cleared" entry inside an approval, a billing review that said "go look yourself"
  and named "not checked" three times. And the reviewer on the #1970 shape (22:41Z): "much better.
  fundamentally I think myself as the harness engineer without any knowledge right now of the codebase
  -> Nils reviewed these changes. but we should check for consistency with the goals and high level
  rules of the repo."
- v5.1, 2026-10-30 02:11Z: the fallback ladder never ends below a floor the reviewer can act on. A real
  billing PR's money review was held over 2 of 22 places without a line and fell to a bare note ("a
  hand-off with no options still forces Alex to read the PR"). R32 and R33 changed; body items became one
  line each (their text is in the inline comment); R31's question comes from the reader.

## Who reads it

The reviewer (Alex) is the harness engineer and does not know the codebase. The author (Nils) wrote the
change and has reviewed it. A review checks the PR for consistency with the repository's goals and
high-level rules, as its own docs write them down (R37), and for the issues the scan looks for. It never
asks the reviewer to read or review code.

## The shape

ONE submitted review on the PR (R0, R24): APPROVE for a low-risk PR that meets the bar (R27),
REQUEST_CHANGES for changes (R35), else COMMENT. The items are also inline comments on their lines:

```
**Needs you** · automated, Alex hasn't read this PR
Alex: 1 decision (billing, plans, credits or pricing not checked). Nils: 1 change, get CI running.

**What it does:** Reminders now take an SMS credit and name the patient in the text.

**For Alex:**
1. **Who confirms what this PR changes in billing, plans, credits or pricing before it merges?**
   No agent checks money logic, so a person has to confirm what it changes.
   **What changes:**
   - Credits · [`ledger.ts` L10-L24](…/blob/<head>/src/billing/ledger.ts#L10-L24): each reminder now takes one SMS credit.
   A. Nils states in the PR description what changes for customers
   B. A person who owns billing reviews the changes
   C. Accept as is
   **Recommended: A.** One paragraph from Nils puts on record what customers will see, and nobody has to read code.

**For Nils:**
1. **Reminder text names the patient** · Change · [`src/notify/send.ts` L13-L15](…/files#diff-…R13-R15) · rule: [`AGENTS.md` L3](…/blob/<base>/AGENTS.md#L3)
2. [Get CI running](…/pull/N/checks): no check ran on this commit, so nothing has tested it.

**Checked and cleared:**
- **Audit call records the user** · [`src/audit/log.ts` L40-L44](…/files#diff-…R40-R44): only the user id is written, consistent with the logging rule.

**Safe to skip:** tests: [`notify.test.ts`](…/files#diff-…)

No CI ran · no merge conflict · open
```

A low-risk PR that meets the bar is two lines and what was checked, collapsed (R36):

```
**Approved as low risk** · automated, Alex hasn't read this PR
Nothing for Alex or Nils.

<details><summary>What was checked</summary>

- **Audit call records the user** · [`src/audit/log.ts` L40-L44](…): only the user id is written.

</details>
```

## Rules

| Id | Rule | Gate | From |
| --- | --- | --- | --- |
| R0 | Never a PENDING review: it takes the reviewer's own review slot. Every review is SUBMITTED: event APPROVE only under R27 (the reviewer said so, 21:01Z), REQUEST_CHANGES for changes (R35, the reviewer said so, 23:09Z), else COMMENT. | [gate] (delivery) | v2: our pending reviews blocked his own |
| R1 | The outcome word in line 1 (R28): `Needs you`, `Changes requested` (`Changes suggested` on a COMMENT review, R35), `Questions`, `Low risk`, `Approved as low risk`. | [gate] | v1, v3, v4 |
| R2 | At most 5 items for the author, worst first; low risk has none. | [gate] | v1 |
| R3 | Each item links to the exact changed lines in the PR's Files view (path and line range as link text), and the range is inside one hunk of the diff on the side it names. | [gate] | v1: "no markers" |
| R4 | Each item is also an inline comment on its exact lines, so the markers show in "Files changed". | [gate] | v1: "no markers" |
| R5 | Each item says what changed (R29), why it matters, and the exact change, or the exact question or evidence to ask the author for. Its body line (what changed, change or question, the link) points at its inline comment, which carries the text (R21). | [gate] | v1; the mayor, 02:11Z |
| R6 | No filler: no praise, no "make sure", "consider", "please review", "best practices", "potential issues". | [gate] (deny-list) | v1: "fluff" |
| R7 | Whenever a model read the PR (and it is not an approval): one `What it does:` sentence. The rest of the diff goes in `Safe to skip:` (R30). | [gate] | v1; the mayor, 01:40Z: summary and skip list mixed |
| R8 | The status line (CI passed / red: names / none ran, merge conflict yes or no, open) appears when something did not pass (R30). | [gate] | — |
| R9 | No boilerplate. The status line, when there is one, is last. | [gate] | v1: "fluff" |
| R10 | No rule codes or flag ids anywhere in the review (A1, B3, ci-none, phi-…, F1). A repository rule is cited only by its link (R37). | [gate] | v1 |
| R11 | A review without a model happens only when CI is the only problem, no content rule fired and the PR does not request the reviewer (R34); it is then the CI ask for the author (R22), linked to the checks. | [gate] | pulse#1929 |
| R13 | A question asks one exact thing and ends with a question mark. | [gate] | — |
| R14 | Sizes: each sentence field at most 300 characters, the text at most 6,000. | [gate] | v1 |
| R15 | Never deliver for a merged or closed PR. The sweep takes open PRs only, the queue drops a PR once it merges or closes, and the run re-checks the state right before delivery. | [gate] (delivery) | v2: "was merged. we don't need to review it" |
| R16 | "Needs you" means one or more decisions only the reviewer can make, at the level of goals and rules (R37), or a product, risk or compliance trade-off; never a code fix the author can make. Each is ONE question ending in "?", with 2-4 options on lettered lines and a recommended letter with a one-sentence reason, linked to where it lives: lines of a file at the head commit (the path exists, the lines are within the file) or the PR description or a ticket URL in it. With no decision, the outcome is never "Needs you". Rule flags alone never route to the reviewer. | [gate] | v2: "otherwise you do not need me!"; the mayor, 22:08Z |
| R17 | Every review has its own URL, the submitted review's GitHub URL (…/pull/N#pullrequestreview-ID), and Slack posts exactly that with the outcome word. | [gate] (delivery) | v2, v3: "leave a review comment every single time on the PR and then link to that" |
| R18 | One review and one Slack message per (PR, head commit): the same head is never reviewed twice; a new head gets a new review. A deliberate re-review of the same head (a rule change) updates the earlier review in place, or cuts it to a pointer to the new one, and updates its Slack message in place. | [gate] (delivery) | v2: two runs of the same head both posted |
| R19 | Never "nothing to check" when a content rule fired. Each flagged hunk is cleared in the review (`Checked and cleared:`, R29), or becomes an item or a decision on its lines. | [gate] | v4: "nothing to check" hid fired rules |
| R20 | A PR that touches a NOT-CHECKED area (billing, plans, credits, pricing) says so in line 2 and gets a decision on who confirms what it changes (R32). Never low risk, never an approval. Found by a zero-token rule and by the lines the model reports. | [gate] | v4: billing-touching PRs called low risk, despite the honesty rule |
| R21 | Each finding is said once: its inline comment states it in full on the exact lines, anchored (GitHub reports a line for it), and the body has one line pointing at it. No suggested reply, no two items on the same lines. | [gate] (+ delivery) | v4: one review said its finding three times; the mayor, 02:11Z |
| R22 | Line 2 says who acts: `<reviewer>: N decisions` or `nothing`, then `<author>:` the changes, questions and blockers, e.g. "Alex: nothing. Nils: 1 change, resolve the merge conflict, get CI running."; "Nothing for <reviewer> or <author>." when nobody acts. A blocker in the status line (CI red, missing, skipped or running, a merge conflict) is a linked ask for the author; a merge conflict names and links the conflicting files. | [gate] | v4; the mayor, 01:40Z |
| R23 | No rule-codes section; a collapsed section only in an approval (R36). "Not checked" appears only when the PR touches that area. | [gate] | v4 |
| R24 | ONE review of ours per PR head: an approval carries the whole review in its body, with no separate comment. | [gate] (delivery) | v4: a comment plus an approval on one head |
| R25 | No "[link removed]" fragments: a sentence that needed a link to another site is rewritten without it. | [gate] | v4 |
| R26 | Reader test: after the zero-token checks, a second, cheap model scores the review 1-5 on scan, markers, decisions, plain, length and useful. Below markers 4 or useful 3 it is regenerated once with the reader's problems, then held. The same test runs over every delivered review for the weekly numbers. | [gate] (model) | v4: "are they easy to read" |
| R27 | APPROVE only under the bar, checked right before posting: the PR is open and on the reviewed head; CI is green (at least one check SUCCESS, every check SUCCESS, NEUTRAL or SKIPPED); no decision, item or blocker and no not-checked area; every flagged hunk cleared with a line in its hunk. Anything short of the bar asks the author for what is missing instead. | [gate] (delivery) | v4: "actually approve low risk PRs not just say you do" |
| R28 | The verdict first. Line 1 is the outcome word in bold with the honesty note as a suffix: "**Approved as low risk** · automated, Alex hasn't read this PR". No disclaimer line above it. | [gate] | the mayor, 22:03Z |
| R29 | Each cleared entry and each item leads with 3-6 plain words on what this PR changed there (never a category label, never a raw identifier), then the ONE fact that makes it safe, or the one ask, in at most 25 words. | [gate] | the mayor, 22:03Z and 01:40Z |
| R30 | No identical boilerplate across reviews. The status line shows only when something did not pass. `Safe to skip:` links every file it names, by group ("tests: [`a.test.ts`](…), …"). A change in product behaviour is never in it (R37). | [gate] | the mayor, 22:03Z and 01:40Z |
| R31 | A cleared entry that describes a behaviour that could be a risk without saying why it is acceptable or intended is not cleared: the reader test names it, the review is regenerated, and what the reader still names becomes a question for the author, so there is no approval. The question is the reader's one concrete check on those lines, never a stock prompt. | [gate] (model) | the mayor, 22:03Z: a risky "cleared" entry inside an approval; 02:11Z |
| R32 | The money decision (R20) saves the reviewer time: one neutral line per changed place saying what the change does to charges, credits, plans or limits (it describes, never judges), from a zero-tool reading of the hunk, so it holds even when the full review is held (a place without a line gets one more try, then keeps its link with "(not summarized)": a missing line never holds the money review); EVERY place linked to its changed range at the head, grouped by kind, the short file name as the link text, never a bare count; the options on lettered lines and the recommendation with a reason; "not checked" said ONCE, in line 2. | [gate] | the mayor, 22:08Z and 01:40Z: "go look yourself" |
| R33 | Never silent on a PR that requests the reviewer, and never below a floor the reviewer can act on: the fallback ladder is monotone. When the gate or the reader test holds a review twice, a PR that touches a not-checked area gets the money review (line 1, line 2, the money decision of R32, the status line; nothing the review model wrote). Otherwise, or if that is held too, a PR that requests the reviewer gets the note: "**Not reviewed automatically**" as its verdict, why it was held in plain words (for example "it touches billing in 22 places, which no agent checks"), the places the zero-token rules know, each linked, and one rule-level decision, "Who reviews this PR before it merges?", with lettered options and a recommended letter with its reason (none asks the reviewer to read code, R37). Slack "Not reviewed: <review URL>". The reader test scores the fallbacks for the log; it never holds them. The reader's reasons for each held attempt are kept host-local. | [gate] | the mayor, 21:57Z, 22:30Z and 02:11Z: requested PRs held and silent, then a bare note |
| R34 | Missing, skipped or red CI never skips the model on a PR that requests the reviewer: the model reviews it, and the CI blocker is one linked ask for the author (R22). | [gate] (routing) | the mayor, 22:30Z: a PR without CI got "Questions" with no question |
| R35 | The outcome word matches the event. "Changes requested" only on a REQUEST_CHANGES review (with `PR_REVIEW_REQUEST_CHANGES=on`, the reviewer's word); a COMMENT review says "Changes suggested". Questions and escalations stay COMMENT. Our newest review is our only live verdict: it dismisses our earlier REQUEST_CHANGES and APPROVE reviews on the PR (never one the reviewer wrote). A block that rested only on a merge conflict or CI is lifted by the sweep once both are fixed on a new head; a block on content gets a review of the new head. | [gate] (+ sweep) | the mayor, 22:30Z and 01:40Z; the reviewer, 23:09Z |
| R36 | Approvals stay short: line 1, "Nothing for <reviewer> or <author>.", and the cleared entries in ONE collapsed "What was checked", one plain line each, no duplicates, each link on the code that makes it safe (not a comment line). | [gate] | the mayor, 22:30Z |
| R37 | The review checks the PR for consistency with the repository's goals and high-level rules, as its docs write them down (README, AGENTS.md / CLAUDE.md, CONTRIBUTING, SECURITY, docs/, the PR template, the CI workflows): a rules digest per repository, cached by the docs' blob SHAs, host-local, each rule linked to its doc lines. A finding in an area where the digest has a rule cites it (its doc link); "consistent with rule X" is a valid cleared reason. Decisions are at the level of goals and rules ("Rule X says …; this PR does … → accept as an exception, ask the author to change it, or update the rule?"), never "review the code yourself". Patient data and SOC 2 findings are framed against the repository's own security and compliance rules, and say so where it has none. A change in product behaviour gets its own linked line (intended, default safe) or a decision, never the skip list. | [gate] (+ model) | the reviewer, 22:41Z (#1970); the mayor, 01:40Z |

R12 of v1 (every flagged hunk becomes an item for the reviewer) is withdrawn by R16: flags are hints for
the model, not findings for the reviewer. R19 keeps them visible: each one is cleared or acted on.
