#!/usr/bin/env python3
"""Writes the public regression fixtures (synthetic look-alikes; no real repository's text) into
tests/fixtures/. Run it after changing a case below (and `git rm` a fixture whose case is gone), then
run the regression:

  assets/scripts/pr-review-regress.sh
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DIFF = """diff --git a/src/notify/send.ts b/src/notify/send.ts
index 1111111..2222222 100644
--- a/src/notify/send.ts
+++ b/src/notify/send.ts
@@ -10,6 +10,9 @@ export async function remind(visit: Visit) {
   const contact = await loadContact(visit.contactId);
   if (!contact.optIn) return;
-  await queue.add(visit.id);
+  const body = `Reminder for ${contact.fullName}, born ${contact.dob}`;
+  await smsClient.send(contact.phone, body);
+  logger.info(`sent reminder ${body}`);
+  await queue.add(visit.id);
   return true;
 }
diff --git a/test/notify.test.ts b/test/notify.test.ts
index 3333333..4444444 100644
--- a/test/notify.test.ts
+++ b/test/notify.test.ts
@@ -1,3 +1,4 @@
 import { remind } from '../src/notify/send';
+it('sends', async () => expect(await remind(fake)).toBe(true));

"""

GOOD_ITEM = {
    'path': 'src/notify/send.ts', 'side': 'new', 'line_start': 13, 'line_end': 15, 'kind': 'change',
    'severity': 'medium', 'area': 'patient data',
    'what': 'The reminder SMS now carries the full name and date of birth, and the same text is written to the info log.',
    'why': 'Patient data goes to the SMS provider and into application logs, which are not meant to hold PHI.',
    'ask': 'Drop the message body from the info log line and log the visit id instead.',
    'verified': 'read remind() and the logger config', 'confidence': 9}

DECISION = {
    'question': 'May the reminder SMS name the patient and show the date of birth?',
    'options': ['Allow it, with the SMS vendor under a BAA', 'Send only the clinic name and time'],
    'recommendation': 'Send only the clinic name and time; nothing in the ticket needs the patient name.',
    'why': 'Which patient data may go to a third party is a compliance call, not a code fix the author can make.',
    'area': 'compliance', 'link': {'kind': 'code', 'path': 'src/notify/send.ts', 'line_start': 13, 'line_end': 14},
    'verified': 'read remind() and the smsClient wrapper', 'confidence': 9}
TREE = {'src/notify/send.ts': 40, 'test/notify.test.ts': 4}
REVIEW = 'https://github.com/example/repo/pull/1#pullrequestreview-1001'
PAGE = 'http://dash.example/pr-review/repo-1-aaaaaaa.html'
QUESTION = dict(GOOD_ITEM, kind='question', ask='Does the SMS text need the full name at all?')

LEGACY_CI_BODY = """**Automated review** by Alex's review agents (Claude Code). Alex has not read this PR unless he says so.
Shadow mode: this review is pending and only Alex sees it; he submits or discards it.

**Needs you** · head `0123456789`. Reviewed after the merge.

No check or status ran on this head. A7: CI red or skipped means escalate. No model review was run.

Zero-token flags: ci-none (no check or status reported on the head commit), irrev-messages (a message sent to people (SMS, email, push)), sec-auth (sign-in, sessions, roles or permissions). CI on this head: none.
Not checked: billing, plans, credits, pricing."""

CASES = {
    # Case 1 (2026-10-29 19:56Z, the reviewer: "it just fluff and really hard to read. there are no
    # markers in the review what I actually need to manually review"): rule labels only, no markers.
    'case01-ci-escalation-without-markers': {
        'expect': 'fail', 'expect_reasons': ['R1', 'R3', 'R7', 'R8', 'R9', 'R10'],
        'body': LEGACY_CI_BODY, 'outcome': 'escalate', 'n_items': 0},
    # Case 2 (20:19Z, on a merged PR: "was merged. we don't need to review it"): R15.
    'case02-delivered-on-a-merged-pr': {
        'expect': 'fail', 'expect_reasons': ['R15'], 'outcome': 'changes', 'diff': DIFF, 'items': [GOOD_ITEM],
        'safe_to_skip': '1 test file.', 'delivery': {'pr_state': 'MERGED', 'review_url': REVIEW, 'review_state': 'COMMENTED'}},
    # Case 3 (20:21Z: "that review comment needs to link to the human decisions ... otherwise you do not
    # need me!"): needs you without a linked decision for the reviewer. R16.
    'case03-needs-you-without-decisions': {
        'expect': 'fail', 'expect_reasons': ['R16'], 'outcome': 'escalate', 'diff': DIFF, 'items': [GOOD_ITEM],
        'safe_to_skip': '1 test file.'},
    # Case 4 (20:21Z, 20:45Z): a delivered review without its own GitHub review URL. R17.
    'case04-delivered-without-own-url': {
        'expect': 'fail', 'expect_reasons': ['R17'], 'outcome': 'questions', 'diff': DIFF, 'items': [QUESTION],
        'safe_to_skip': '1 test file.', 'delivery': {'pr_state': 'OPEN', 'review_url': None}},
    # 20:45Z ("don't link to the dashboard but actually leave a review comment every single time on the PR
    # and then link to that"): a page instead of a review comment. R17.
    'case05-delivered-as-a-page': {
        'expect': 'fail', 'expect_reasons': ['R17'], 'outcome': 'questions', 'diff': DIFF, 'items': [QUESTION],
        'safe_to_skip': '1 test file.', 'delivery': {'pr_state': 'OPEN', 'review_url': PAGE}},
    # Case 6 (20:08Z: pending reviews took the reviewer's own review slot): a pending review created. R0.
    'case17-pending-review-created': {
        'expect': 'fail', 'expect_reasons': ['R0'], 'outcome': 'questions', 'diff': DIFF, 'items': [QUESTION],
        'safe_to_skip': '1 test file.', 'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'PENDING'}},
    # R18 (20:42Z, the mayor: two runs of the same head both posted to Slack): one message per head.
    'case16-second-slack-post-for-the-same-head': {
        'expect': 'fail', 'expect_reasons': ['R18'], 'outcome': 'changes', 'diff': DIFF, 'items': [GOOD_ITEM],
        'safe_to_skip': '1 test file.',
        'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'COMMENTED', 'post_slack': True, 'head': 'a' * 40,
                     'slack_heads': ['a' * 40]}},
    'case06-needs-you-with-a-linked-decision': {
        'expect': 'pass', 'outcome': 'escalate', 'diff': DIFF, 'decisions': [DECISION], 'tree_lines': TREE,
        'items': [GOOD_ITEM], 'safe_to_skip': '1 test file: a happy-path test.',
        'reply': 'Thanks. Please drop the body from the log line; I will decide what the SMS may say.',
        'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'COMMENTED'}},
    'case07-decision-links-a-missing-file': {
        'expect': 'fail', 'expect_reasons': ['R16'], 'outcome': 'escalate', 'diff': DIFF, 'tree_lines': TREE,
        'decisions': [dict(DECISION, link={'kind': 'code', 'path': 'src/notify/gone.ts', 'line_start': 1, 'line_end': 2})],
        'items': [], 'safe_to_skip': 'everything: 2 files.'},
    'case08-decision-is-not-a-question': {
        'expect': 'fail', 'expect_reasons': ['R16'], 'outcome': 'escalate', 'diff': DIFF, 'tree_lines': TREE,
        'decisions': [dict(DECISION, question='Decide what the reminder SMS may contain.')],
        'items': [], 'safe_to_skip': 'everything: 2 files.'},
    'case09-item-outside-the-diff': {
        'expect': 'fail', 'expect_reasons': ['R3'], 'outcome': 'changes', 'diff': DIFF,
        'items': [dict(GOOD_ITEM, line_start=40, line_end=44)], 'safe_to_skip': '1 test file.'},
    'case10-filler': {
        'expect': 'fail', 'expect_reasons': ['R6'], 'outcome': 'changes', 'diff': DIFF,
        'items': [dict(GOOD_ITEM, ask='Please review this change and make sure it follows best practices.')],
        'safe_to_skip': '1 test file.'},
    'case11-question-without-question-mark': {
        'expect': 'fail', 'expect_reasons': ['R13'], 'outcome': 'questions', 'diff': DIFF,
        'items': [dict(QUESTION, ask='Tell us which ticket asked for the name in the reminder text.')],
        'safe_to_skip': '1 test file.'},
    'case12-low-risk-clean': {
        'expect': 'pass', 'outcome': 'low-risk', 'diff': DIFF, 'items': [],
        'safe_to_skip': 'all 2 files: a queue call and a test.', 'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'COMMENTED'}},
    'case13-ci-only-one-line': {
        'expect': 'pass', 'outcome': 'questions', 'diff': DIFF, 'items': [],
        'single_line': 'No CI ran on this commit and nothing else is flagged; can you run it?',
        'facts': 'No CI ran · no merge conflict · open'},
    'case14-rule-code-in-a-field': {
        'expect': 'fail', 'expect_reasons': ['R10'], 'outcome': 'changes', 'diff': DIFF,
        'items': [dict(GOOD_ITEM, why='This trips phi-logs: patient data goes into application logs.')],
        'safe_to_skip': '1 test file.'},
    'case15-removed-line-on-old-side': {
        'expect': 'pass', 'outcome': 'questions', 'diff': DIFF,
        'items': [dict(QUESTION, side='old', line_start=12, line_end=12, area='behaviour patients notice',
                       what='The plain queue call that used to run first is removed and re-added after the send.',
                       why='Reminders now go out before the visit is queued, so a failed send skips the queue.',
                       ask='Should a send failure still queue the visit?')],
        'safe_to_skip': '1 test file.'},
}

if __name__ == '__main__':
    out = os.path.join(HERE, 'fixtures')
    os.makedirs(out, exist_ok=True)
    for name, fx in CASES.items():
        with open(os.path.join(out, f'{name}.json'), 'w') as f:
            json.dump(fx, f, indent=1)
    stale = sorted(n for n in os.listdir(out) if n.endswith('.json') and n[:-5] not in CASES)
    print(f'{len(CASES)} fixtures in {out}' + (f'; stale (git rm them): {stale}' if stale else ''))
