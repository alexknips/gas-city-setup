# Review style guide, v5.8

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
  -> [the author] reviewed these changes. but we should check for consistency with the goals and high level
  rules of the repo."
- v5.1, 2026-10-30 02:11Z: the fallback ladder never ends below a floor the reviewer can act on. A real
  billing PR's money review was held over 2 of 22 places without a line and fell to a bare note ("a
  hand-off with no options still forces the reviewer to read the PR"). R32 and R33 changed; body items became one
  line each (their text is in the inline comment); R31's question comes from the reader.
- v5.2, 2026-10-30 02:24Z: the money review was one point short on length (30 lines for 19 places) and
  line 2 said "Author: nothing" though the recommended option gave the author a task. R22 and R32 changed.
- v5.3, 2026-10-30 02:35Z: the question named an effect no linked place backed, asked "who" where the
  options answered "what sign-off", and the bare links of the other places made it long. R20, R23 and R32
  changed. This is the last round fitted to one PR; quality is measured on the benchmark from here.
- v5.4, 2026-10-30 03:05Z: two general rules from that PR: each place is linked once (R21), and a decision
  shows its block first (risk, question, options, recommendation), then the evidence (R16, R32). The
  examples name nobody: "the reviewer" and "the author" (the town's names live in its overrides).
- v5.5, 2026-10-30 05:31Z: the reviewer on a billing review that said "Needs you" with three options for
  the author: "I don't think that needs me. you wrote a review and [the author] just needs to answer not
  me". A decision whose options are all the author's (or someone's they pick) is a question for the
  author; each option says who carries it out. R16, R20, R22, R32 and R33 changed.
- v5.6, 2026-10-30 06:05-06:09Z, the reviewer: "do not post ever that i did not read the PRs -> the whoole
  point is to have a system act in my place [...]" / "just act as if you are me" (on the repositories the
  town's override names), and "needs me should be reserved for escalations that cannot be escalated to [the
  author], such as potential gaps in our review methodology". The voice setting (R28, R38): the pack's
  default still discloses. Every finding goes to the author; a gap in our method goes to the reviewer by
  Slack, never on the PR (R16). R16, R20, R22, R28, R33 and R37 changed; R38 is new.
- v5.7, 2026-10-30 06:4xZ, the mayor's reader tests of the first reviews in the voice: a question its own
  explanation answered, and a body item that said less than its inline comment. Two general rules: a
  self-answered question is cleared (R31), and the body item states its ask while the inline comment
  carries the evidence (R4).
- v5.8, 2026-10-30 07:21Z, the mayor's reader tests of the reruns: the money question comes first with the
  line it depends on, the evidence after the decision and not under it (R32); options that do not overlap
  and a recommendation that says why that option over the others (R16); line 2 names the author when
  nothing is needed, and an approval shows its reason above what was checked (R28, R36); every pointer
  follows the newest review (R24).
- v5.8.1, 2026-10-30 16:19Z, the mayor's P1: a rule that fired on a comment's own words is cleared on that
  comment, and a markdown heading or bullet is text, not a comment (R36).

## Who reads it

The reviewer is the harness engineer and does not know the codebase. The author wrote the
change and has reviewed it. A review checks the PR for consistency with the repository's goals and
high-level rules, as its own docs write them down (R37), and for the issues the scan looks for. It never
asks the reviewer to read or review code.

## The shape

ONE submitted review on the PR (R0, R24): APPROVE for a low-risk PR that meets the bar (R27),
REQUEST_CHANGES for changes (R35), else COMMENT. The items are also inline comments on their lines:

```
**Questions** · automated, Reviewer hasn't read this PR
Reviewer: nothing. Author: 1 change, 1 question, get CI running.

**What it does:** Overdue invoices get a late fee, and reminders name the patient.

**For Author:**
1. **What sign-off does this change to billing, plans, credits or pricing need before it merges?** · [`fees.ts` L10-L24](…/blob/<head>/src/billing/fees.ts#L10-L24)
   These changes are not checked by any agent. Overdue invoices get a late fee after thirty days, on by default.
   A. Author states in the PR description what changes for customers: a short written account puts on record what customers will see
   B. Whoever owns billing signs off, here or by linking where they already did: they know the pricing rules this touches
   C. Author merges it as is: when customers see no change from it yet, for example while it is off
   **Recommended: A.** It takes Author one paragraph, and it gives whoever owns billing something concrete to agree with.

**Charges:**
- [`invoice.ts` L40-L52](…/blob/<head>/src/billing/invoice.ts#L40-L52): the invoice total now includes the fee.

<details><summary>Other places</summary>

- No behaviour change: [`types.ts` L3](…), [`fees.test.ts` L40-L52](…)

</details>

2. **Reminder text names the patient** · Change: send only the clinic name and time · [`src/notify/send.ts` L13-L15](…/files#diff-…R13-R15) · rule: [`AGENTS.md` L3](…/blob/<base>/AGENTS.md#L3)
3. [Get CI running](…/pull/N/checks): no check ran on this commit, so nothing has tested it.

**Checked and cleared:**
- **Audit call records the user** · [`src/audit/log.ts` L40-L44](…/files#diff-…R40-R44): only the user id is written, consistent with the logging rule.

**Safe to skip:** tests: [`notify.test.ts`](…/files#diff-…)

No CI ran · no merge conflict · open
```

A low-risk PR that meets the bar is two lines and what was checked, collapsed (R36):

```
**Approved as low risk** · automated, Reviewer hasn't read this PR
Nothing for Reviewer or Author.

Only the user id is written.

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
| R4 | Each item is also an inline comment on its exact lines, so the markers show in "Files changed". The body item is self-contained: what changed, its kind and its ask (at most 20 words), then the link; the inline comment, numbered as in the body, carries the evidence (why it matters), not the same ask again. | [gate] | v1: "no markers"; v5.7 |
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
| R16 | Every finding about the PR goes to the author, as a change, a question, or a choice: a rule, product, risk or compliance trade-off they must settle, with the evidence it needs (patient data, security, irreversible steps, migration order and unexplained behaviour included). A choice is ONE question ending in "?", with 2-4 options on lettered lines, each starting with who carries it out (the author, or someone they pick: a teammate, the owner of billing; the model names the actor of each), and a recommended letter with a one-sentence reason: why that option over the others, never the option's own reason again; no two options mean the same (two that both come down to "an owner signs off" are one), linked to where it lives: lines of a file at the head commit (the path exists, the lines are within the file) or the PR description or a ticket URL in it. No option is the reviewer's. "Needs you" is never an outcome on the PR: the reviewer gets only a GAP IN OUR METHOD that the author cannot close (a repository rule no reading of the code can verify, a kind of risk the method does not handle, a review that could not reach its bar), by Slack ("Review gap: <review URL>"), while the PR gets its normal review. A gap kind the reviewer accepted (billing) is said to the author as scope and not sent again; each new kind becomes one rule-level ask to the reviewer, never one per PR. A review with an open gap is never an approval. | [gate] | v2: "otherwise you do not need me!"; the mayor, 22:08Z; v5.5, v5.6: "needs me should be reserved for escalations that cannot be escalated to [the author]" |
| R17 | Every review has its own URL, the submitted review's GitHub URL (…/pull/N#pullrequestreview-ID), and Slack posts exactly that with the outcome word. | [gate] (delivery) | v2, v3: "leave a review comment every single time on the PR and then link to that" |
| R18 | One review and one Slack message per (PR, head commit): the same head is never reviewed twice; a new head gets a new review. A deliberate re-review of the same head (a rule change) updates the earlier review in place, or cuts it to a pointer to the new one, and updates its Slack message in place. | [gate] (delivery) | v2: two runs of the same head both posted |
| R19 | Never "nothing to check" when a content rule fired. Each flagged hunk is cleared in the review (`Checked and cleared:`, R29), or becomes an item or a decision on its lines. | [gate] | v4: "nothing to check" hid fired rules |
| R20 | A PR that touches a NOT-CHECKED area (billing, plans, credits, pricing) gets a question for the author on the sign-off it needs, which says once that no agent checked it (R32). Never low risk, never an approval, never "Needs you" for that alone. Found by a zero-token rule and by the lines the model reports. | [gate] | v4: billing-touching PRs called low risk, despite the honesty rule; v5.5: "you wrote a review and [the author] just needs to answer not me" |
| R21 | Each finding is said once: its inline comment states it in full on the exact lines, anchored (GitHub reports a line for it), and the body has one line pointing at it. Each code place is linked once in the whole review (a repository rule may be cited more than once). No suggested reply, no two items on the same lines. | [gate] (+ delivery) | v4: one review said its finding three times; the mayor, 02:11Z |
| R22 | Line 2 says who acts: `<reviewer>: N decisions` or `nothing`, then `<author>:` the changes, questions (their decisions among them) and blockers, e.g. "Reviewer: nothing. Author: 1 change, resolve the merge conflict, get CI running."; `<reviewer>: N decisions` only when the reviewer carries out an option of each, read from the text (each such decision has an option line that starts with the reviewer's name); "Nothing for <reviewer> or <author>." when nobody acts. When a decision's recommended option gives the author a task, line 2 names it ("if Reviewer picks A, state in the PR description what changes for customers"): the author may stop reading at line 2. A blocker in the status line (CI red, missing, skipped or running, a merge conflict) is a linked ask for the author; a merge conflict names and links the conflicting files. | [gate] | v4; the mayor, 01:40Z |
| R23 | No rule-codes section; a collapsed section only in an approval (R36) or for the other money places (R32). "Not checked" appears only when the PR touches that area. | [gate] | v4 |
| R24 | ONE review of ours per PR head: an approval carries the whole review in its body, with no separate comment. A review a newer one replaces becomes a one-line pointer to it, and every pointer of ours on the PR follows the newest review (never a chain). | [gate] (delivery) | v4: a comment plus an approval on one head |
| R25 | No "[link removed]" fragments: a sentence that needed a link to another site is rewritten without it. | [gate] | v4 |
| R26 | Reader test: after the zero-token checks, a second, cheap model scores the review 1-5 on scan, markers, decisions, plain, length and useful. Below markers 4 or useful 3 it is regenerated once with the reader's problems, then held. The same test runs over every delivered review for the weekly numbers. | [gate] (model) | v4: "are they easy to read" |
| R27 | APPROVE only under the bar, checked right before posting: the PR is open and on the reviewed head; CI is green (at least one check SUCCESS, every check SUCCESS, NEUTRAL or SKIPPED); no decision, item or blocker and no not-checked area; every flagged hunk cleared with a line in its hunk. Anything short of the bar asks the author for what is missing instead. | [gate] (delivery) | v4: "actually approve low risk PRs not just say you do" |
| R28 | The verdict first. Line 1 is the outcome word in bold with the honesty note as a suffix: "**Approved as low risk** · automated, Reviewer hasn't read this PR". No disclaimer line above it. With the voice "reviewer" (a setting; the town sets it for the reviewer's own repositories) the review speaks as the reviewer: line 1 is the verdict word alone ("**Approved**", "**Changes requested**", "**Questions**") followed by an invisible marker (`<!-- pr-review v5.6 -->`) by which our own reviews are known; line 2 says what the author must do, by name ("Author: 1 question.", or "Author: nothing needed."); options for the author start with "You". | [gate] | the mayor, 22:03Z; v5.6: "just act as if you are me" |
| R29 | Each cleared entry and each item leads with 3-6 plain words on what this PR changed there (never a category label, never a raw identifier), then the ONE fact that makes it safe, or the one ask, in at most 25 words. | [gate] | the mayor, 22:03Z and 01:40Z |
| R30 | No identical boilerplate across reviews. The status line shows only when something did not pass. `Safe to skip:` links every file it names, by group ("tests: [`a.test.ts`](…), …"). A change in product behaviour is never in it (R37). | [gate] | the mayor, 22:03Z and 01:40Z |
| R31 | A cleared entry that describes a behaviour that could be a risk without saying why it is acceptable or intended is not cleared: the reader test names it, the review is regenerated, and what the reader still names becomes a question for the author, so there is no approval. The question is the reader's one concrete check on those lines, never a stock prompt. The converse holds too (v5.7): a question whose own explanation answers it is not a question; the reader names it, and it becomes a cleared entry with that explanation as its reason (keeping the flag it covered), so the PR may then be approved under R27. | [gate] (model) | the mayor, 22:03Z: a risky "cleared" entry inside an approval; 02:11Z |
| R32 | The money question (R20, for the author) saves everyone time. Its block comes first and leads with the question its options answer ("What sign-off does this change … need before it merges?"), in bold, with one link to the line it depends on (the riskiest place that changes behaviour, linked there only); then one plain risk sentence (that no agent checked these changes, said once, then what happens on merge with the default state, followed by links to the places that show it); the options, each a real choice with its reason, and the recommendation; the evidence after the decision, NOT nested under the question (a list under it reads as more asks) (an effect no linked place backs is dropped: it could be a model invention). The places that change behaviour come first, by risk, at most 6, under one header per kind; the others go in a collapsed "Other places" after the recommendation (the lower-risk ones with a few words each), every place still linked. Each option has its reason. One neutral line per changed place saying what the change does to charges, credits, plans or limits (it describes, never judges), from a zero-tool reading of the hunk, so it holds even when the full review is held (a place without a line gets one more try, then keeps its link with "(not summarized)": a missing line never holds the money review); EVERY place linked to its changed range at the head, grouped by kind, the short file name as the link text, never a bare count; the options on lettered lines and the recommendation with a reason; "not checked" said ONCE, in line 2. | [gate] | the mayor, 22:08Z and 01:40Z: "go look yourself" |
| R33 | Never silent on a PR that requests the reviewer, and never below a floor the reviewer can act on: the fallback ladder is monotone. When the gate or the reader test holds a review twice, a PR that touches a not-checked area gets the money review (line 1, line 2, the money decision of R32, the status line; nothing the review model wrote). Otherwise, or if that is held too, a PR that requests the reviewer gets the note: "**Not reviewed automatically**" as its verdict, why it was held in plain words (for example "it touches billing in 22 places, which no agent checks"), the places the zero-token rules know, each linked, and one rule-level question for the author, "Who reviews this PR before it merges?", with lettered options (all the author's) and a recommended letter with its reason (none asks the reviewer to read code, R37). In the reviewer's voice the note is their question to the author ("**Questions**"), without why the review was held; that goes to the reviewer as a gap. Slack "Not reviewed: <review URL>". The reader test scores the fallbacks for the log; it never holds them. The reader's reasons for each held attempt are kept host-local. | [gate] | the mayor, 21:57Z, 22:30Z and 02:11Z: requested PRs held and silent, then a bare note |
| R34 | Missing, skipped or red CI never skips the model on a PR that requests the reviewer: the model reviews it, and the CI blocker is one linked ask for the author (R22). | [gate] (routing) | the mayor, 22:30Z: a PR without CI got "Questions" with no question |
| R35 | The outcome word matches the event. "Changes requested" only on a REQUEST_CHANGES review (with `PR_REVIEW_REQUEST_CHANGES=on`, the reviewer's word); a COMMENT review says "Changes suggested". Questions and escalations stay COMMENT. Our newest review is our only live verdict: it dismisses our earlier REQUEST_CHANGES and APPROVE reviews on the PR (never one the reviewer wrote). A block that rested only on a merge conflict or CI is lifted by the sweep once both are fixed on a new head; a block on content gets a review of the new head. | [gate] (+ sweep) | the mayor, 22:30Z and 01:40Z; the reviewer, 23:09Z |
| R36 | Approvals stay short: line 1, "Nothing for <reviewer> or <author>." (in the reviewer's voice "<author>: nothing needed."), one plain line on why it is safe, visible (the one cleared entry's reason, else what the PR does), and the cleared entries in ONE collapsed "What was checked", one plain line each, no duplicates, each link on the code that makes it safe (not a comment line, unless the rule fired on that comment's own words: then the link holds that line). | [gate] | the mayor, 22:30Z |
| R38 | In the reviewer's voice the review never discloses itself: no "automated review" (or "automated, …", "automated approval"), "hasn't read", "has not read", "review agent", "Claude Code review", "reviewed by Claude", "not reviewed automatically" or "not checked by any agent", in the body or an inline comment. Phrases, not words: a PR may add "automated tests" or change how a team uses Claude Code. A gap is said as scope ("Billing, plans, credits or pricing are outside this review."). The word "AI" is allowed (code and features use it). | [gate] | the reviewer, 2026-10-30 06:05Z: "do not post ever that i did not read the PRs" |
| R37 | The review checks the PR for consistency with the repository's goals and high-level rules, as its docs write them down (README, AGENTS.md / CLAUDE.md, CONTRIBUTING, SECURITY, docs/, the PR template, the CI workflows): a rules digest per repository, cached by the docs' blob SHAs, host-local, each rule linked to its doc lines. A finding in an area where the digest has a rule cites it (its doc link); "consistent with rule X" is a valid cleared reason. Choices are at the level of goals and rules ("Rule X says …; this PR does … → change it to follow the rule, or put on record why it is an exception?"), never "review the code yourself". Patient data and SOC 2 findings are framed against the repository's own security and compliance rules, and say so where it has none. A change in product behaviour gets its own linked line (intended, default safe) or a decision, never the skip list. | [gate] (+ model) | the reviewer, 22:41Z (#1970); the mayor, 01:40Z |

R12 of v1 (every flagged hunk becomes an item for the reviewer) is withdrawn by R16: flags are hints for
the model, not findings for the reviewer. R19 keeps them visible: each one is cleared or acted on.
<!-- not in the review prompt -->
## Parts the script writes (not in the review model's prompt)

The review model writes none of these, so they are kept out of its prompt, which stays byte-identical to the
benchmark's pin (the mayor, 2026-10-30 22:22Z). The gate checks them like the rules above.

- v5.9, 2026-10-30 21:39Z, the mayor: 4 of 5 requested reviews fell back, and the reader found a template (the
  same question, scope sentence and options on every PR, a link to the files view no line anchors, the status
  said twice, a line 1 that did not say who acts). R33 now reads, after its stock forms: v5.9: before either, the fallback asks its own questions: one question per linked place (at most 3, the riskiest first: the money places; for the note the flagged hunks, filled up with the largest changed hunks), each a body item on its lines with its reason as the inline comment, and one "What it does" sentence, written by a cheap reader of those hunks and the PR description and checked by the script (a question that fits any PR, a repeated one or a code name fails; one more try for the places that failed). No scope sentence and no options; the places not asked about are collapsed under "Other places", each linked to its lines, never a link to the files view no line anchors; a status line only for what no blocker says; in the reviewer's voice line 1 says who acts ("**Questions** · for <author>, nothing needed from me"). The stock forms above go out only when no question passes.
- v6.0, 2026-10-30 22:22Z, decision-57b6d9bb (the reviewer, 22:18Z: "make sure our system is able to re-review
  things when [the author] addresses questions or makes changes"): follow-ups. A new head on a PR we reviewed
  is reviewed again once its CI settles, the incremental way (earlier asks first, as fixed, open or gone).
  When the author answers on the head we reviewed (a reply in one of our threads, a PR comment or review, a
  thread of ours resolved), a same-head follow-up calls each ask answered (cleared, the answer is the reason,
  linked), still open (one short follow-up in its thread) or now a change; the review is rebuilt from the
  delivered one with a line "**Since your answers:** 2 answered, 1 still open in its thread." (in the
  disclosing voice "Since <author>'s answers"), updated in place or posted new with the pointer; our change
  request is dismissed when nothing is left and the PR is approved when everything is cleared and it meets
  R27. A follow-up on a fallback never approves. One follow-up per batch of answers, at most
  PR_REVIEW_FOLLOWUP_ROUNDS per head, then one line tells the author and it stops. A fallback's line 1 says
  who acts only while the author has something to do.
<!-- /not in the review prompt -->
