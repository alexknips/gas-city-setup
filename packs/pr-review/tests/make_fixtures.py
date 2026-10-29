#!/usr/bin/env python3
"""Writes the public regression fixtures (synthetic look-alikes; no real repository's text) into
tests/fixtures/. Run it after changing a case below, then run the regression:

  python3 assets/scripts/pr_format.py --regress tests/fixtures [<host-local fixture dirs>...]
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
    'path': 'src/notify/send.ts', 'side': 'new', 'line_start': 13, 'line_end': 15, 'kind': 'check',
    'severity': 'high', 'area': 'patient data',
    'what': 'The reminder SMS now carries the full name and date of birth, and the same text is written to the info log.',
    'why': 'Patient data goes to the SMS provider and into application logs, which are not meant to hold PHI.',
    'ask': 'Decide whether the SMS may name the patient at all, and ask for the log line to drop the body.',
    'verified': 'read remind() and the logger config', 'confidence': 9}

LEGACY_CI_BODY = """**Automated review** by Alex's review agents (Claude Code). Alex has not read this PR unless he says so.
Shadow mode: this review is pending and only Alex sees it; he submits or discards it.

**Needs you** · head `0123456789`. Reviewed after the merge.

No check or status ran on this head. A7: CI red or skipped means escalate. No model review was run.

Zero-token flags: ci-none (no check or status reported on the head commit), irrev-messages (a message sent to people (SMS, email, push)), sec-auth (sign-in, sessions, roles or permissions). CI on this head: none.
Not checked: billing, plans, credits, pricing."""

CASES = {
    # Case 1 (2026-10-29, the reviewer: "it just fluff and really hard to read. there are no markers in
    # the review what I actually need to manually review"): a CI escalation posted with rule labels only.
    'case01-ci-escalation-without-markers': {
        'expect': 'fail', 'expect_reasons': ['R1', 'R3', 'R7', 'R8', 'R9', 'R10'],
        'body': LEGACY_CI_BODY, 'outcome': 'escalate', 'n_items': 0, 'comments': []},
    'case02-needs-you-with-markers': {
        'expect': 'pass', 'outcome': 'escalate', 'diff': DIFF, 'items': [GOOD_ITEM],
        'safe_to_skip': '1 test file: a happy-path test.', 'flagged_paths': ['src/notify/send.ts'],
        'facts': 'No CI ran · no merge conflict · merged', 'rule_ids': ['irrev-messages', 'phi-logs']},
    'case03-item-outside-the-diff': {
        'expect': 'fail', 'expect_reasons': ['R3'], 'outcome': 'escalate', 'diff': DIFF,
        'items': [dict(GOOD_ITEM, line_start=40, line_end=44)], 'safe_to_skip': '1 test file.'},
    'case04-filler': {
        'expect': 'fail', 'expect_reasons': ['R6'], 'outcome': 'escalate', 'diff': DIFF,
        'items': [dict(GOOD_ITEM, ask='Please review this change and make sure it follows best practices.')],
        'safe_to_skip': '1 test file.'},
    'case05-needs-you-without-items': {
        'expect': 'fail', 'expect_reasons': ['R12'], 'outcome': 'escalate', 'diff': DIFF, 'items': [],
        'safe_to_skip': 'everything else', 'flagged_paths': ['src/notify/send.ts']},
    'case06-question-without-question-mark': {
        'expect': 'fail', 'expect_reasons': ['R13'], 'outcome': 'questions', 'diff': DIFF,
        'items': [dict(GOOD_ITEM, kind='question', severity='medium',
                       ask='Tell us which ticket asked for the name in the reminder text.')],
        'safe_to_skip': '1 test file.'},
    'case07-low-risk-clean': {
        'expect': 'pass', 'outcome': 'low-risk', 'diff': DIFF, 'items': [],
        'safe_to_skip': 'all 2 files: a queue call and a test.'},
    'case08-ci-only-one-line': {
        'expect': 'pass', 'outcome': 'escalate', 'diff': DIFF, 'items': [],
        'single_line': 'No CI ran on this commit; nothing else flagged.', 'facts': 'No CI ran · no merge conflict · open'},
    'case09-rule-code-in-a-field': {
        'expect': 'fail', 'expect_reasons': ['R10'], 'outcome': 'escalate', 'diff': DIFF,
        'items': [dict(GOOD_ITEM, why='This trips phi-logs: patient data goes into application logs.')],
        'safe_to_skip': '1 test file.'},
    'case10-removed-line-on-old-side': {
        'expect': 'pass', 'outcome': 'escalate', 'diff': DIFF,
        'items': [dict(GOOD_ITEM, side='old', line_start=12, line_end=12, area='behaviour patients notice',
                       what='The plain queue call that used to run first is removed and re-added after the send.',
                       why='Reminders now go out before the visit is queued, so a failed send skips the queue.',
                       ask='Ask whether a send failure should still queue the visit?', kind='question')],
        'safe_to_skip': '1 test file.'},
}

if __name__ == '__main__':
    out = os.path.join(HERE, 'fixtures')
    os.makedirs(out, exist_ok=True)
    for name, fx in CASES.items():
        with open(os.path.join(out, f'{name}.json'), 'w') as f:
            json.dump(fx, f, indent=1)
    print(f'{len(CASES)} fixtures in {out}')
