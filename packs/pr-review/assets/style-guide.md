# Review style guide, v3

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

## The shape

A submitted COMMENT review on the PR (R0, R17); the items are also inline comments on their lines:

```
Automated review by Alex's review agents (Claude Code); Alex hasn't read this PR yet.

**Needs you: 1 decision**

1. **May the reminder SMS name the patient and show the date of birth?** · compliance · [open `src/notify/send.ts` L13-L14](…/blob/<head>/src/notify/send.ts#L13-L14)
   **Why you:** which patient data may go to a third party is a compliance call, not a code fix.
   **Options:** allow it, with the vendor under a BAA; send only the clinic name and time
   **Recommended:** send only the clinic name and time.

**For Nils:**
- [`src/notify/send.ts` L13-L15](…/files#diff-…R13-R15) · patient data
   **Changed:** the reminder text is also written to the info log.
   **Why it matters:** application logs are not meant to hold patient data.
   **Change:** drop the body from the log line and log the visit id.

**Safe to skip:** 1 test file: a happy-path test.

**Suggested reply to Nils:** …

CI passed · no merge conflict · open

<sub>Not checked: billing, plans, credits, pricing.</sub>
```

## Rules

| Id | Rule | Gate | From |
| --- | --- | --- | --- |
| R0 | Never a PENDING review: it takes the reviewer's own review slot. Every review is SUBMITTED, event COMMENT (APPROVE or REQUEST_CHANGES only on the reviewer's word: SOC 2 reads them as human review). | [gate] (delivery) | v2: our pending reviews blocked his own |
| R1 | Line 1 is one short honesty line ("Automated review by <name>'s review agents (Claude Code); <name> hasn't read this PR yet."), because the author reads the review as the reviewer's. Then the outcome and the count: `Needs you: N decisions`, `N questions for <author>`, `Changes requested: N`, `Low risk: nothing to check`. | [gate] | v1, v3 |
| R2 | At most 5 items for the author, worst first; low risk has none. | [gate] | v1 |
| R3 | Each item links to the exact changed lines in the PR's Files view (path and line range as link text), and the range is inside one hunk of the diff on the side it names. | [gate] | v1: "no markers" |
| R4 | Each item is also an inline comment on its exact lines, so the markers show in "Files changed". | [gate] | v1: "no markers" |
| R5 | Each item says what changed (one sentence), why it matters (one sentence, naming the area in words), and the exact change, or the exact question or evidence to ask the author for. | [gate] | v1 |
| R6 | No filler: no praise, no "make sure", "consider", "please review", "best practices", "potential issues". | [gate] (deny-list) | v1: "fluff" |
| R7 | One line `Safe to skip:` (at most 240 characters) saying what the rest of the diff is. | [gate] | v1 |
| R8 | One facts line: CI (passed / red: names / none ran), merge conflict yes or no, open. | [gate] | — |
| R9 | The footer is small and last: not checked: billing, plans, credits, pricing. No other boilerplate. | [gate] | v1: "fluff" |
| R10 | No rule codes in the text (A1, B3, ci-none, phi-…). Rule ids only in a collapsed `<details>` at the end. | [gate] | v1 |
| R11 | A review without a model happens only when CI is the only problem and no content rule fired; it is then one question for the author ("No CI ran on this commit…; can you run it?"). | [gate] | pulse#1929 |
| R13 | A question asks one exact thing and ends with a question mark. | [gate] | — |
| R14 | Sizes: each sentence field at most 300 characters, the text at most 6,000. | [gate] | v1 |
| R15 | Never deliver for a merged or closed PR. The sweep takes open PRs only, the queue drops a PR once it merges or closes, and the run re-checks the state right before delivery. | [gate] (delivery) | v2: "was merged. we don't need to review it" |
| R16 | "Needs you" means one or more decisions only the reviewer can make: a product, risk or compliance trade-off, never a code fix the author can make. Each is ONE question ending in "?", with 2-4 options and a recommendation, linked to where it lives: lines of a file at the head commit (the path exists, the lines are within the file) or the PR description or a ticket URL in it. With no decision, the outcome is never "Needs you". Rule flags alone never route to the reviewer. | [gate] | v2: "otherwise you do not need me!" |
| R17 | Every review has its own URL, the submitted review's GitHub URL (…/pull/N#pullrequestreview-ID), and Slack posts exactly that with the outcome word. | [gate] (delivery) | v2, v3: "leave a review comment every single time on the PR and then link to that" |
| R18 | One review and one Slack message per (PR, head commit): the same head is never reviewed twice; a new head gets a new review. | [gate] (delivery) | v2: two runs of the same head both posted |

R12 of v1 (every flagged hunk becomes an item for the reviewer) is withdrawn by R16: flags are hints for
the model, not findings for the reviewer.
