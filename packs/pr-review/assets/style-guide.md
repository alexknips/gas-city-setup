# Review style guide, v4

What a delivered review must look like. The review model gets this file with its instructions. The
gate in `pr_format.py` checks every rule marked **[gate]** before anything is delivered. A review that
fails is regenerated once with the gate's reasons; if it fails again, nothing is delivered and the PR's
bead says `held-gate`. Each rule names the complaint it came from. A new complaint becomes a new rule
and a regression case (README.md, "Quality loop").

- v1, 2026-10-29 19:56Z. The reviewer on a shadow review: "it just fluff and really hard to read.
  there are no markers in the review what I actually need to manually review. this is not good enough yet".
- v2, 2026-10-29 20:08-20:21Z. Our pending reviews took his GitHub review slot ("what is also bad: is
  the comment and how difficult my human review now is"). Then: "I need a link to the review comment.
  that review comment needs to link to the human decisions (either in text or code reference that still
  need to be answered). otherwise you do not need me!" And on a merged PR: "was merged. we don't need to
  review it".
- v3, 2026-10-29 20:45Z: "no, don't link to the dashboard but actually leave a review comment every
  single time on the PR and then link to that". Pages are gone; every review is a submitted comment.
- v4.1, 2026-10-29 21:57Z: R28, after a real billing PR was held by the reader test three times.
- v4, 2026-10-29 21:01-21:05Z: "actually approve low risk PRs not just say you do", and "review our
  reviews again. are they easy to read". A reader test of the 7 delivered reviews: they scan fast, but
  "nothing to check" hid fired rules and a PR that touches billing, findings were said three times, and
  a separate comment came with an approval. R19-R27.

## The shape

ONE submitted review on the PR (R0, R24): APPROVE for a low-risk PR that meets the bar (R27), else
COMMENT. The items are also inline comments on their lines:

```
Automated review by Alex's review agents (Claude Code); Alex hasn't read this PR yet.

**Needs you. Alex: 1 decision. Nils: 1 change, get CI running.**

**For Alex:**
1. **May the reminder SMS name the patient and show the date of birth?** · compliance · [open `src/notify/send.ts` L13-L14](…/blob/<head>/src/notify/send.ts#L13-L14)
   **Why you:** which patient data may go to a third party is a compliance call, not a code fix.
   **Options:** allow it, with the vendor under a BAA; send only the clinic name and time
   **Recommended:** send only the clinic name and time.

**For Nils:**
1. [`src/notify/send.ts` L13-L15](…/files#diff-…R13-R15) · patient data
   **Changed:** the reminder text is also written to the info log.
   **Why it matters:** application logs are not meant to hold patient data.
   **Change:** drop the body from the log line and log the visit id.
2. [Get CI running](…/pull/N/checks): no check ran on this commit, so nothing has tested it.

**Checked and cleared:**
- audit logging at [`src/audit/log.ts` L40-L44](…/files#diff-…R40-R44): the new audit call records only the user id.

**Safe to skip:** 1 test file: a happy-path test.

No CI ran · no merge conflict · open
```

A low-risk PR that meets the bar: `Automated approval by …` on line 1, `**Approved as low risk. Alex:
nothing. Nils: nothing.**`, the cleared list, safe to skip, the facts line. A PR that touches billing,
plans, credits or pricing: line 2 says so, a decision for Alex asks him to look, and the footer
`<sub>Not checked: billing, plans, credits or pricing.</sub>` closes the review (R20).

## Rules

| Id | Rule | Gate | From |
| --- | --- | --- | --- |
| R0 | Never a PENDING review: it takes the reviewer's own review slot. Every review is SUBMITTED: event APPROVE only under R27 (the reviewer said so, 21:01Z), else COMMENT. REQUEST_CHANGES is not authorized (SOC 2 reads it as human review). | [gate] (delivery) | v2: our pending reviews blocked his own |
| R1 | Line 1 is one short honesty line ("Automated review by <name>'s review agents (Claude Code); <name> hasn't read this PR yet.", "Automated approval by …" for an approval), because the author reads the review as the reviewer's. The next line starts with the outcome word: `Needs you`, `Changes requested`, `Questions`, `Low risk`, `Approved as low risk`. | [gate] | v1, v3, v4 |
| R2 | At most 5 items for the author, worst first; low risk has none. | [gate] | v1 |
| R3 | Each item links to the exact changed lines in the PR's Files view (path and line range as link text), and the range is inside one hunk of the diff on the side it names. | [gate] | v1: "no markers" |
| R4 | Each item is also an inline comment on its exact lines, so the markers show in "Files changed". | [gate] | v1: "no markers" |
| R5 | Each item says what changed (one sentence), why it matters (one sentence, naming the area in words), and the exact change, or the exact question or evidence to ask the author for. | [gate] | v1 |
| R6 | No filler: no praise, no "make sure", "consider", "please review", "best practices", "potential issues". | [gate] (deny-list) | v1: "fluff" |
| R7 | One line `Safe to skip:` (at most 240 characters) saying what the rest of the diff is, whenever a model read the PR. | [gate] | v1 |
| R8 | One facts line: CI (passed / red: names / none ran), merge conflict yes or no, open. | [gate] | — |
| R9 | No boilerplate. The facts line is last, unless the not-checked footer follows it (R20). | [gate] | v1: "fluff" |
| R10 | No rule codes or flag ids anywhere in the review (A1, B3, ci-none, phi-…, F1). | [gate] | v1 |
| R11 | A review without a model happens only when CI is the only problem and no content rule fired; it is then the CI ask for the author (R22), linked to the checks. | [gate] | pulse#1929 |
| R13 | A question asks one exact thing and ends with a question mark. | [gate] | — |
| R14 | Sizes: each sentence field at most 300 characters, the text at most 6,000. | [gate] | v1 |
| R15 | Never deliver for a merged or closed PR. The sweep takes open PRs only, the queue drops a PR once it merges or closes, and the run re-checks the state right before delivery. | [gate] (delivery) | v2: "was merged. we don't need to review it" |
| R16 | "Needs you" means one or more decisions only the reviewer can make: a product, risk or compliance trade-off, never a code fix the author can make. Each is ONE question ending in "?", with 2-4 options and a recommendation, linked to where it lives: lines of a file at the head commit (the path exists, the lines are within the file) or the PR description or a ticket URL in it. With no decision, the outcome is never "Needs you". Rule flags alone never route to the reviewer. | [gate] | v2: "otherwise you do not need me!" |
| R17 | Every review has its own URL, the submitted review's GitHub URL (…/pull/N#pullrequestreview-ID), and Slack posts exactly that with the outcome word. | [gate] (delivery) | v2, v3: "leave a review comment every single time on the PR and then link to that" |
| R18 | One review and one Slack message per (PR, head commit): the same head is never reviewed twice; a new head gets a new review. A deliberate re-review of the same head (a rule change) updates the earlier review in place, or cuts it to a pointer to the new one, and updates its Slack message in place. | [gate] (delivery) | v2: two runs of the same head both posted |
| R19 | Never "nothing to check" when a content rule fired. Each flagged hunk is cleared in the review (`Checked and cleared:`, a link to the flagged lines and one plain sentence on why it is safe), or becomes an item or a decision on its lines. | [gate] | v4: "nothing to check" hid fired rules |
| R20 | A PR that touches a NOT-CHECKED area (billing, plans, credits, pricing) says so on line 2, gets a decision for the reviewer to look at it himself, linked to where it starts, and ends with the not-checked footer. Never low risk, never an approval. Found by a zero-token rule and by the lines the model reports. | [gate] | v4: billing-touching PRs called low risk, despite the honesty rule |
| R21 | Each finding is said once: the item states it in full and its inline comment carries it on the exact line, anchored (GitHub reports a line for it). No suggested reply, no two items on the same lines. | [gate] (+ delivery) | v4: one review said its finding three times |
| R22 | Line 2 says who acts: `<reviewer>: N decisions` or `nothing`, then `<author>:` the changes, questions and blockers, e.g. "Alex: nothing. Nils: 1 change, resolve the merge conflict, get CI running." A blocker in the facts line (CI red, missing, skipped or running, a merge conflict) is a linked ask for the author. | [gate] | v4 |
| R23 | No rule-codes section. The not-checked footer appears only when the PR touches that area. | [gate] | v4 |
| R24 | ONE review of ours per PR head: an approval carries the whole review in its body, with no separate comment. | [gate] (delivery) | v4: a comment plus an approval on one head |
| R25 | No "[link removed]" fragments: a sentence that needed a link to another site is rewritten without it. | [gate] | v4 |
| R26 | Reader test: after the zero-token checks, a second, cheap model scores the review 1-5 on scan, markers, decisions, plain, length and useful. Below markers 4 or useful 3 it is regenerated once with the reader's problems, then held. The same test runs over every delivered review for the weekly numbers. | [gate] (model) | v4: "are they easy to read" |
| R28 | A PR that touches a not-checked area is never silent: when the reader test holds its review twice, it gets the minimal review instead: line 2, the money decision (each file linked at the head, no model text) and the facts line, with the not-checked footer. | [gate] | the mayor, 21:57Z: a held billing review left the PR silent |
| R27 | APPROVE only under the bar, checked right before posting: the PR is open and on the reviewed head; CI is green (at least one check SUCCESS, every check SUCCESS, NEUTRAL or SKIPPED); no decision, item or blocker and no not-checked area; every flagged hunk cleared with a line in its hunk. Anything short of the bar asks the author for what is missing instead. | [gate] (delivery) | v4: "actually approve low risk PRs not just say you do" |

R12 of v1 (every flagged hunk becomes an item for the reviewer) is withdrawn by R16: flags are hints for
the model, not findings for the reviewer. R19 keeps them visible: each one is cleared or acted on.
