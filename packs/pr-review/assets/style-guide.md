# Review style guide, v1

What a posted review must look like. The review model gets this file with its instructions. The gate
in `pr_format.py` checks every rule marked **[gate]** before anything is posted. A review that fails
is regenerated once with the gate's reasons; if it fails again, nothing is posted and the PR's bead
says `held-gate`. Each rule names the complaint it came from. A new complaint becomes a new rule and
a regression case (README.md, "Quality loop").

v1, 2026-10-29. Source: the reviewer on a shadow review, 19:56Z: "it just fluff and really hard to
read. there are no markers in the review what I actually need to manually review. this is not good
enough yet".

## The shape

```
**Needs you: 2 things to check**

1. [`src/billing/export.ts` L40-L52](…/files#diff-…R40-R52) · patient data
   **Changed:** the nightly export now adds date of birth to the CSV sent to the analytics vendor.
   **Why it matters:** patient data leaves the system for a third party.
   **You decide:** is there a BAA with this vendor, and does the export need DOB at all?
2. …

**Safe to skip:** 14 files: tests, UI copy, type definitions.

CI passed · no merge conflict · merged

<sub>Automated review (Claude Code); <name> has not read this PR. Not checked: billing, plans, credits, pricing.</sub>
```

## Rules

| Id | Rule | Gate | From |
| --- | --- | --- | --- |
| R1 | Line 1 is the outcome and the count in plain words: `Needs you: N things to check`, `N questions for <author>`, `Changes requested: N`, `Low risk: nothing to check`. Nothing comes before it. | [gate] | "fluff… hard to read" |
| R2 | At most 5 items, worst first. Low risk has none. Every other outcome has at least one. | [gate] | "hard to read" |
| R3 | Each item links to the exact changed lines in the PR's Files view. The link text is the path and the line range, and the range is inside the PR's diff on the side it names. | [gate] | "no markers… what I actually need to manually review" |
| R4 | Each item is also an inline comment on those lines, so the markers show in "Files changed". | [gate] | same |
| R5 | Each item says what changed (one sentence), why it matters (one sentence, naming the area in words: patient data, security, can't be undone, migration order, behaviour patients notice), and what the reviewer must decide or check, or the exact question or evidence to ask the author for. | [gate] | same |
| R6 | No filler: no praise, no "make sure", "consider", "please review", "best practices", "potential issues", no restating the PR description. | [gate] (deny-list) | "fluff" |
| R7 | One line `Safe to skip:` saying what the rest of the diff is, so the reviewer knows what not to read. | [gate] | "hard to read" (A6: big PRs) |
| R8 | One facts line: CI (passed / red: names / none ran), merge conflict yes or no, open or merged. | [gate] | — |
| R9 | The footer is small and last: automated, not read by the reviewer, not checked: billing, plans, credits, pricing. No shadow-mode line, no boilerplate at the top. | [gate] | "fluff" |
| R10 | No rule codes in the body (A1, B3, ci-none, phi-…, sec-…, irrev-…). Rule ids go only in a collapsed `<details>` at the very end. | [gate] | "fluff" |
| R11 | A review without a model happens only when no content rule fired (for example, CI did not run and nothing else is flagged), and then it is that one sentence. | [gate] | pulse#1929 (CI escalation with only rule labels) |
| R12 | "Needs you" is never bare. If the model found no problem in a hunk the rules flagged, the item is that hunk: what it does, why the flag matters, what to look at, and that no problem was found. | [gate] | same |
| R13 | A question item asks the author one exact question and ends with a question mark. | [gate] | — |
| R14 | Sizes: each sentence field at most 300 characters, the body at most 5,000. | [gate] | "hard to read" |
