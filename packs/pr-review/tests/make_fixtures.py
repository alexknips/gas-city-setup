#!/usr/bin/env python3
"""Writes the public regression fixtures (synthetic look-alikes; no real repository's text) into
tests/fixtures/. Run it after changing a case below (and `git rm` a fixture whose case is gone), then
run the regression:

  assets/scripts/pr-review-regress.sh
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'assets', 'scripts'))
import pr_format  # noqa: E402
import pr_restamp  # noqa: E402
import pr_review  # noqa: E402
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
    'change': 'Reminder text names the patient',
    'why': 'Patient data goes to the SMS provider and into application logs, which are not meant to hold PHI.',
    'ask': 'Drop the message body from the info log line and log the visit id instead.', 'rule': '',
    'verified': 'read remind() and the logger config', 'confidence': 9}

# gc-sy1hm.2 b: a change whose fix rewrites its own line carries it as a one-click suggestion.
SUGGEST_ITEM = dict(GOOD_ITEM, line_start=14, line_end=14, change='Log line prints the message',
                    why="The message holds the patient's name and date of birth, and application logs are not meant to hold PHI.",
                    ask='Log the visit id instead of the message text.',
                    suggestion='  logger.info(`sent reminder for visit ${visit.id}`);')

# gc-sy1hm.2 c: what became of the earlier review's asks (pr_incremental.outcome).
SINCE = {'old': 'b' * 40, 'new': 'a' * 40, 'review_id': 1000, 'fixed': 2, 'again': 1, 'open': 1, 'gone': 0,
         'resolve': [['src/notify/send.ts', 14, 'fixed'], ['src/notify/send.ts', 15, 'fixed'],
                     ['src/notify/send.ts', 13, 'asked-again']]}

DECISION = {
    'question': 'May the reminder SMS name the patient and show the date of birth?',
    'options': ['Reviewer allows it, with the SMS vendor under a BAA', 'Author sends only the clinic name and time'],
    'actors': ['reviewer', 'author'], 'recommended': 1, 'reason': 'Nothing in the ticket needs the patient name.', 'rule': '',
    'why': 'Which patient data may go to a third party is a compliance call, not a code fix the author can make.',
    'area': 'compliance', 'link': {'kind': 'code', 'path': 'src/notify/send.ts', 'line_start': 13, 'line_end': 14},
    'verified': 'read remind() and the smsClient wrapper', 'confidence': 9}
AUTHORS_CALL = dict(DECISION, options=['Author sends only the clinic name and time',
                                       'Author asks the SMS vendor for a BAA first'], actors=['author', 'author'])
TREE = {'src/notify/send.ts': 40, 'test/notify.test.ts': 4}
REVIEW = 'https://github.com/example/repo/pull/1#pullrequestreview-1001'
PAGE = 'http://dash.example/pr-review/repo-1-aaaaaaa.html'
QUESTION = dict(GOOD_ITEM, kind='question', ask='Does the SMS text need the full name at all?')
# A question its own explanation answers (the mayor, 2026-10-30 06:4xZ: then it is not a question).
SELF_ANSWERED = dict(QUESTION, why='The template already uses only the first name, so the full name never reaches the SMS.')
FLAG = {'id': 'F1', 'rule': 'irrev-messages', 'label': 'a message sent to people (SMS, email, push)',
        'path': 'src/notify/send.ts', 'line': 14}
CLEARED = {'flag': 'F1', 'path': 'src/notify/send.ts', 'line_start': 14, 'line_end': 14, 'rule': '',
           'change': 'Reminder now sent by SMS',
           'why': 'The send call is the existing reminder path; only the text changes and it carries no new recipient.'}
NO_CI = pr_format.blockers('example/repo', {'number': 1}, 'none')
NOT_CHECKED = 'billing, plans, credits or pricing'
MONEY = pr_format.not_checked_decision(
    [{'path': 'src/notify/send.ts', 'line_start': 13, 'line_end': 16,
      'what': 'Overdue invoices now get a late fee after thirty days.'}], NOT_CHECKED, 'Author',
    'Overdue invoices get a late fee after thirty days, on by default.', [0])
# decision-ed7320b7: the same in the reviewer's voice (the author is "you", the gap is scope).
MONEY_VOICE = pr_format.not_checked_decision(
    [{'path': 'src/notify/send.ts', 'line_start': 13, 'line_end': 16,
      'what': 'Overdue invoices now get a late fee after thirty days.'}], NOT_CHECKED, 'Author',
    'Overdue invoices get a late fee after thirty days, on by default.', [0], voice='reviewer')
# The mayor, 2026-10-30 07:21Z: three money places, so the evidence groups show (after the decision, not under it).
MONEY3_VOICE = pr_format.not_checked_decision(
    [{'path': 'src/notify/send.ts', 'line_start': 13, 'line_end': 16, 'kind': 'charges', 'risk': 3, 'behaviour': True,
      'what': 'Overdue invoices now get a late fee after thirty days.'},
     {'path': 'src/notify/send.ts', 'line_start': 20, 'line_end': 22, 'kind': 'plans', 'risk': 2, 'behaviour': True,
      'what': 'The fee amount is read from the plan settings.'},
     {'path': 'src/notify/send.ts', 'line_start': 30, 'line_end': 30, 'kind': 'other', 'risk': 1, 'behaviour': True,
      'what': 'The invoice shows the fee on its own line.'}], NOT_CHECKED, 'Author',
    'Overdue invoices get a late fee after thirty days, on by default.', [0], voice='reviewer')
AUTHORS_CALL_VOICE = dict(AUTHORS_CALL, options=['You send only the clinic name and time',
                                                 'You ask the SMS vendor for a BAA first'])
# The money decision as v5.4 delivered it: no actors, the recommended task in line 2, under "For Reviewer".
MONEY_V54 = dict({k: v for k, v in MONEY.items() if k != 'actors'},
                 author_task='state in the PR description what changes for customers')
# What the PR does and the rest of the diff, linked (R7, R30).
SKIP = {'summary': 'Reminders now name the patient and log the message text.',
        'skip': [{'group': 'tests', 'paths': ['test/notify.test.ts']}]}
# The note's floor (R33): why it was held, where, and who reviews it.
FLOOR = pr_format.floor_decision(
    [{'path': 'src/notify/send.ts', 'line_start': 13, 'line_end': 15, 'what': 'a message sent to people (SMS, email, push)'}],
    'Author', "Our review of this PR didn't reach the quality bar: a second reader found our review too hard to act on.")
FLOOR_VOICE = pr_format.floor_decision(
    [{'path': 'src/notify/send.ts', 'line_start': 13, 'line_end': 15, 'what': 'a message sent to people (SMS, email, push)'}],
    'Author', "Our review of this PR didn't reach the quality bar: a second reader found our review too hard to act on.",
    voice='reviewer')
# R37: a repository rule the digest found (synthetic).
RULES = {'D1': {'rule': 'Never write patient data to logs or third-party messages.', 'area': 'patient data',
                'path': 'AGENTS.md', 'line_start': 3, 'line_end': 3}}
# Our live formal block on head a…a, resting on a merge conflict (R35, the sweep).
RC_OLD = {'event': 'REQUEST_CHANGES', 'rc_live': True, 'block': 'mechanical', 'review_id': 1001,
          'review_heads': ['a' * 40], 'head': 'a' * 40, 'status': 'done', 'outcome': 'changes'}
# The same diff with the log line turned into a comment (R36: a cleared link on comment lines only).
COMMENT_DIFF = DIFF.replace('+  logger.info(`sent reminder ${body}`);', '+  // log the reminder for support')
# v5.8.1 (R36): rules that fire on comments' own words, one in a code hunk and one in a hunk of comments
# only, and a markdown guide whose heading and bullet are text, not comments (synthetic).
COMMENTS_DIFF = """diff --git a/src/notify/send.ts b/src/notify/send.ts
index 1111111..2222222 100644
--- a/src/notify/send.ts
+++ b/src/notify/send.ts
@@ -10,5 +10,7 @@ export async function remind(visit: Visit) {
   const contact = await loadContact(visit.contactId);
   if (!contact.optIn) return;
+  // Reminders go out by SMS once the contact opts in.
+  const channel = 'sms';
   await queue.add(visit.id);
   return true;
 }
diff --git a/src/notify/channel.ts b/src/notify/channel.ts
index 5555555..6666666 100644
--- a/src/notify/channel.ts
+++ b/src/notify/channel.ts
@@ -1,3 +1,5 @@
 /**
  * Reminder channels.
+ * A reminder is a text message to the contact's phone.
+ * It never names the reason for the visit.
  */
diff --git a/docs/reminders.mdx b/docs/reminders.mdx
index 7777777..8888888 100644
--- a/docs/reminders.mdx
+++ b/docs/reminders.mdx
@@ -1,2 +1,4 @@
 # Reminders
+## Text messages
+* A reminder is sent by SMS to the contact's phone.
 Opt-in is required.
"""
FLAG_C1 = dict(FLAG, id='F1', line=12)
FLAG_C2 = dict(FLAG, id='F2', path='src/notify/channel.ts', line=3)
CLEARED_C1 = dict(CLEARED, flag='F1', line_start=12, line_end=12, change='Comment on the reminder channel',
                  why='Only a comment is added; the send path and its recipients stay as they are.')
CLEARED_C2 = dict(CLEARED, flag='F2', path='src/notify/channel.ts', line_start=3, line_end=4,
                  change='Comment on what reminders say', why='Only the file comment changes; no code runs differently.')
# The #1981 shape (synthetic): a money question over many places, as the fallback after reader holds.
MONEY_MANY_VOICE = pr_format.not_checked_decision(
    [{'path': f'src/billing/part{k}.ts', 'line_start': 10, 'line_end': 12, 'kind': pr_format.MONEY_KINDS[k % 5],
      'risk': 3 - k % 3, 'behaviour': k < 10, 'what': f'Billing step {k} now reads the fee from the plan.'}
     for k in range(14)], NOT_CHECKED, 'Author',
    'Overdue invoices get a late fee after thirty days, on by default.', [0, 1, 2, 3], voice='reviewer')
FLAG_MD = dict(FLAG, id='F1', path='docs/reminders.mdx', line=3)
CLEARED_MD = dict(CLEARED, flag='F1', path='docs/reminders.mdx', line_start=2, line_end=3,
                  change='Guide section on text reminders', why='Only the guide changes; no code reads this page.')
GREEN = {'head': 'a' * 40, 'fresh': {'state': 'OPEN', 'checks': ['SUCCESS', 'SKIPPED']}}
PR = {'number': 1, 'state': 'OPEN', 'mergeable': 'MERGEABLE', 'headRefOid': 'a' * 40}
# R33 (the mayor, 2026-10-30 21:39Z: "One specific question per linked line with its own reason, no boilerplate
# sentences, no orphan link, one status line"): the fallback's own questions (pr_fallback), structure only.
ASKED_DIFF = ''.join(f"""diff --git a/src/billing/{f}.ts b/src/billing/{f}.ts
index 1111111..2222222 100644
--- a/src/billing/{f}.ts
+++ b/src/billing/{f}.ts
@@ -1,2 +1,4 @@
 export function {f}Total(invoice: Invoice) {{
+  const step = invoice.{f} ?? 0;
+  return invoice.amount + step;
 }}
""" for f in ('fee', 'credits', 'plans', 'limits'))
ASKS = [('fee', 'Late fee after thirty days', 'Overdue invoices now get a fee after thirty days on every plan; should the '
         'free plan be exempt?', 'Free plan customers would be charged for the first time, without notice.'),
        ('credits', 'Credits expire six months sooner', 'Unused credits now expire after six months instead of twelve; do '
         'current balances keep the old date?', 'Customers who bought credits last year could lose them without warning.'),
        ('plans', 'Plan price read at renewal', 'A plan change now takes its new price at the next renewal; does a '
         'downgrade refund the difference?', 'Customers who downgrade mid-cycle would pay the old price until renewal.')]
ASKED = [{'path': f'src/billing/{f}.ts', 'side': 'new', 'line_start': 2, 'line_end': 3, 'kind': 'question',
          'severity': 'low', 'area': 'other', 'change': c, 'ask': q, 'why': w, 'rule': '', 'verified': 'fallback question',
          'confidence': 7, 'money': True} for f, c, q, w in ASKS]
ASKED_OTHERS = [{'path': 'src/billing/limits.ts', 'line_start': 2, 'line_end': 3, 'hunk': 0, 'what': 'model line', 'money': True}]
RED = pr_format.blockers('example/repo', {'number': 1}, 'red', ['unit tests'])
GREEN_FACTS_TEXT = pr_format.GREEN_FACTS
ASKED_FX = dict(voice='reviewer', outcome='changes', derived=True, diff=ASKED_DIFF, items=ASKED, blockers=RED,
                facts='CI red: unit tests · no merge conflict · open', model=False, not_checked=NOT_CHECKED, fallback=True,
                others=ASKED_OTHERS, gaps=[{'kind': 'not-checked', 'accepted': True}, {'kind': 'below-bar', 'accepted': True}])
ASKED_ITEM_RE = r'\. \*\*[^*]+\*\* · Question: [^\n]+\? · \[`src/billing/\w+\.ts` L2-L3\]\(https://github\.com/example/repo/pull/1/files#diff-[0-9a-f]{64}R2-R3\)'


def body(**fx):
    """A v4 body rendered from a fixture's parts, for the cases that then break it on purpose."""
    return pr_format.render('example/repo', PR, pr_format.review_of(fx), 'Reviewer', 'Author')


LOW = dict(outcome='low-risk', diff=DIFF, items=[], flagged=[FLAG], cleared=[CLEARED], **SKIP)

LEGACY_CI_BODY = """**Automated review** by Reviewer's review agents (Claude Code). Reviewer has not read this PR unless they say so.
Shadow mode: this review is pending and only Reviewer sees it; they submit or discard it.

**Needs you** · head `0123456789`. Reviewed after the merge.

No check or status ran on this head. A7: CI red or skipped means escalate. No model review was run.

Zero-token flags: ci-none (no check or status reported on the head commit), irrev-messages (a message sent to people (SMS, email, push)), sec-auth (sign-in, sessions, roles or permissions). CI on this head: none.
Not checked: billing, plans, credits, pricing."""

CASES = {
    # Case 1 (2026-10-29 19:56Z, the reviewer: "it just fluff and really hard to read. there are no
    # markers in the review what I actually need to manually review"): rule labels only, no markers.
    'case01-ci-escalation-without-markers': {
        'expect': 'fail', 'expect_reasons': ['R28', 'R3', 'R7', 'R9', 'R10'],
        'body': LEGACY_CI_BODY, 'outcome': 'escalate', 'n_items': 0},
    # Case 2 (20:19Z, on a merged PR: "was merged. we don't need to review it"): R15.
    'case02-delivered-on-a-merged-pr': {
        'expect': 'fail', 'expect_reasons': ['R15'], 'outcome': 'changes', 'diff': DIFF, 'items': [GOOD_ITEM],
        **SKIP, 'delivery': {'pr_state': 'MERGED', 'review_url': REVIEW, 'review_state': 'COMMENTED'}},
    # Case 3 (20:21Z: "that review comment needs to link to the human decisions ... otherwise you do not
    # need me!"): needs you without a linked decision for the reviewer. R16.
    'case03-needs-you-without-decisions': {
        'expect': 'fail', 'expect_reasons': ['R16'], 'outcome': 'escalate', 'diff': DIFF, 'items': [GOOD_ITEM],
        **SKIP},
    # Case 4 (20:21Z, 20:45Z): a delivered review without its own GitHub review URL. R17.
    'case04-delivered-without-own-url': {
        'expect': 'fail', 'expect_reasons': ['R17'], 'outcome': 'questions', 'diff': DIFF, 'items': [QUESTION],
        **SKIP, 'delivery': {'pr_state': 'OPEN', 'review_url': None}},
    # 20:45Z ("don't link to the dashboard but actually leave a review comment every single time on the PR
    # and then link to that"): a page instead of a review comment. R17.
    'case05-delivered-as-a-page': {
        'expect': 'fail', 'expect_reasons': ['R17'], 'outcome': 'questions', 'diff': DIFF, 'items': [QUESTION],
        **SKIP, 'delivery': {'pr_state': 'OPEN', 'review_url': PAGE}},
    # Case 6 (20:08Z: pending reviews took the reviewer's own review slot): a pending review created. R0.
    'case17-pending-review-created': {
        'expect': 'fail', 'expect_reasons': ['R0'], 'outcome': 'questions', 'diff': DIFF, 'items': [QUESTION],
        **SKIP, 'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'PENDING'}},
    # R18 (20:42Z, the mayor: two runs of the same head both posted to Slack): one message per head.
    'case16-second-slack-post-for-the-same-head': {
        'expect': 'fail', 'expect_reasons': ['R18'], 'outcome': 'changes', 'diff': DIFF, 'items': [GOOD_ITEM],
        **SKIP,
        'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'COMMENTED', 'post_slack': True, 'head': 'a' * 40,
                     'slack_heads': ['a' * 40]}},
    # Since 2026-10-30 06:09Z (decision-3aeec149: "needs me should be reserved for escalations that cannot be
    # escalated to [the author], such as potential gaps in our review methodology") this shape fails: a
    # finding about the PR is the author's, and "Needs you" is not an outcome on the PR.
    'case06-needs-you-with-a-linked-decision': {
        'expect': 'fail', 'expect_reasons': ['R16'], 'only_reasons': ['R16'], 'outcome': 'escalate',
        'diff': DIFF, 'decisions': [DECISION], 'tree_lines': TREE,
        'items': [GOOD_ITEM], **SKIP,
        'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'COMMENTED'}},
    'case07-decision-links-a-missing-file': {
        'expect': 'fail', 'expect_reasons': ['R16'], 'outcome': 'escalate', 'diff': DIFF, 'tree_lines': TREE,
        'decisions': [dict(DECISION, link={'kind': 'code', 'path': 'src/notify/gone.ts', 'line_start': 1, 'line_end': 2})],
        'items': [], **SKIP},
    'case08-decision-is-not-a-question': {
        'expect': 'fail', 'expect_reasons': ['R16'], 'outcome': 'escalate', 'diff': DIFF, 'tree_lines': TREE,
        'decisions': [dict(DECISION, question='Decide what the reminder SMS may contain.')],
        'items': [], **SKIP},
    'case09-item-outside-the-diff': {
        'expect': 'fail', 'expect_reasons': ['R3'], 'outcome': 'changes', 'diff': DIFF,
        'items': [dict(GOOD_ITEM, line_start=40, line_end=44)], **SKIP},
    'case10-filler': {
        'expect': 'fail', 'expect_reasons': ['R6'], 'outcome': 'changes', 'diff': DIFF,
        'items': [dict(GOOD_ITEM, ask='Please review this change and make sure it follows best practices.')],
        **SKIP},
    'case11-question-without-question-mark': {
        'expect': 'fail', 'expect_reasons': ['R13'], 'outcome': 'questions', 'diff': DIFF,
        'items': [dict(QUESTION, ask='Tell us which ticket asked for the name in the reminder text.')],
        **SKIP},
    'case12-low-risk-clean': {
        'expect': 'pass', 'outcome': 'low-risk', 'diff': DIFF, 'items': [],
        **SKIP, 'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'COMMENTED'}},
    'case13-ci-only-one-line': {
        'expect': 'pass', 'outcome': 'questions', 'diff': DIFF, 'items': [], 'blockers': NO_CI, 'model': False,
        'facts': 'No CI ran · no merge conflict · open'},
    'case14-rule-code-in-a-field': {
        'expect': 'fail', 'expect_reasons': ['R10'], 'outcome': 'changes', 'diff': DIFF,
        'items': [dict(GOOD_ITEM, why='This trips phi-logs: patient data goes into application logs.')],
        **SKIP},
    'case15-removed-line-on-old-side': {
        'expect': 'pass', 'outcome': 'questions', 'diff': DIFF,
        'items': [dict(QUESTION, side='old', line_start=12, line_end=12, area='behaviour patients notice',
                       change='Queue call moved after send',
                       why='Reminders now go out before the visit is queued, so a failed send skips the queue.',
                       ask='Should a send failure still queue the visit?')],
        **SKIP},
    # 2026-10-29 21:05Z, the reader test of the 7 delivered reviews ("they scan fast, but 'nothing to
    # check' hides real points"): R19-R26, each a case.
    'case18-low-risk-with-an-uncleared-flag': {
        'expect': 'fail', 'expect_reasons': ['R19'], **dict(LOW, cleared=[])},
    'case19-low-risk-with-its-flag-cleared': {'expect': 'pass', **LOW},
    'case20-cleared-outside-its-hunk': {
        'expect': 'fail', 'expect_reasons': ['R19'], **dict(LOW, cleared=[dict(CLEARED, line_start=30, line_end=31)])},
    'case21-billing-called-low-risk': {
        'expect': 'fail', 'expect_reasons': ['R20'], **dict(LOW, not_checked=NOT_CHECKED)},
    # 2026-10-30 05:31Z (decision-554d2e7b, the reviewer on a billing review: "I don't think that needs me. you wrote
    # a review and the author just needs to answer not me"): the sign-off is a question for the author.
    'case22-billing-is-a-question-for-the-author': {
        'expect': 'pass', 'outcome': 'questions', 'derived': True, 'line2': 'Reviewer: nothing. Author: 1 question.',
        'diff': DIFF, 'items': [], 'decisions': [MONEY], 'tree_lines': TREE,
        'flagged': [FLAG], 'cleared': [CLEARED], 'not_checked': NOT_CHECKED, **SKIP},
    'case23-finding-said-twice': {
        'expect': 'fail', 'expect_reasons': ['R21'], 'outcome': 'changes', 'diff': DIFF, **SKIP,
        'items': [GOOD_ITEM, dict(GOOD_ITEM, line_start=14, line_end=15, kind='question',
                                  ask='Why does the info log need the message body?')]},
    'case24-suggested-reply-repeats-the-items': {
        'expect': 'fail', 'expect_reasons': ['R21'], 'outcome': 'changes', 'n_items': 1,
        'body': body(outcome='changes', items=[GOOD_ITEM], **SKIP).replace(
            '\n\n**Safe to skip:**', '\n\n**Suggested reply to Author:** drop the body from the log line.\n\n**Safe to skip:**')},
    'case25-inline-comment-not-anchored': {
        'expect': 'fail', 'expect_reasons': ['R21'], 'outcome': 'changes', 'diff': DIFF, 'items': [GOOD_ITEM],
        **SKIP,
        'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'COMMENTED', 'inline_lines': [None]}},
    'case26-line-1-does-not-say-who-acts': {
        'expect': 'fail', 'expect_reasons': ['R22'], 'outcome': 'low-risk',
        'body': body(**LOW).replace('Nothing for Reviewer or Author.', 'Nothing to check here.')},
    'case27-blocker-only-in-the-facts-line': {
        'expect': 'fail', 'expect_reasons': ['R22'], 'outcome': 'low-risk',
        'body': body(**dict(LOW, facts='No CI ran · merge conflict · open'))},
    'case28-rule-codes-section': {
        'expect': 'fail', 'expect_reasons': ['R23'], 'outcome': 'low-risk',
        'body': body(**LOW) + '\n\n<details><summary>rules</summary>irrev-messages</details>'},
    'case29-not-checked-footer-on-a-pr-without-that-area': {
        'expect': 'fail', 'expect_reasons': ['R23'], 'outcome': 'low-risk',
        'body': body(**LOW) + '\n\n<sub>Not checked: billing, plans, credits, pricing.</sub>'},
    'case30-approval-plus-a-comment-review': {
        'expect': 'fail', 'expect_reasons': ['R24'], **dict(LOW, approve=True), 'approve_check': GREEN,
        'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'APPROVED', 'ours_on_head': 2}},
    'case31-link-removed-fragment': {
        'expect': 'fail', 'expect_reasons': ['R25'], 'outcome': 'changes', 'diff': DIFF, **SKIP,
        'items': [dict(GOOD_ITEM, why='The vendor terms at [link removed] do not cover patient names in SMS text.')]},
    'case32-reader-test-below-the-bar': {
        'expect': 'fail', 'expect_reasons': ['R26'], **LOW,
        'judge': {'scan': 5, 'markers': 3, 'decisions': 0, 'plain': 4, 'length': 4, 'useful': 2}},
    # 2026-10-29 21:57Z (the mayor, R20 vs R26): a billing PR whose review the reader test held twice gets
    # the minimal review: line 2, the money decision, the facts line, nothing the model wrote. R33.
    'case37-billing-minimal-after-reader-holds': {
        'expect': 'pass', 'outcome': 'questions', 'derived': True, 'diff': DIFF, 'items': [], 'model': False, 'tree_lines': TREE,
        'decisions': [MONEY], 'not_checked': NOT_CHECKED},
    # 2026-10-29 21:01Z (the reviewer: "actually approve low risk PRs not just say you do"): the approval bar, R27.
    'case33-approve-with-an-uncleared-flag': {
        'expect': 'fail', 'expect_reasons': ['R19', 'R27'], **dict(LOW, cleared=[], approve=True), 'approve_check': GREEN},
    'case34-approve-with-ci-not-green': {
        'expect': 'fail', 'expect_reasons': ['R27'], **dict(LOW, approve=True),
        'approve_check': {'head': 'a' * 40, 'fresh': {'state': 'OPEN', 'checks': ['SUCCESS', 'FAILURE']}}},
    'case35-approve-on-a-changed-head': {
        'expect': 'fail', 'expect_reasons': ['R27'], **dict(LOW, approve=True),
        'approve_check': {'head': 'a' * 40, 'fresh': {'state': 'OPEN', 'head': 'b' * 40, 'checks': ['SUCCESS']}}},
    'case36-approve-clean': {
        'expect': 'pass', **dict(LOW, approve=True), 'approve_check': GREEN,
        'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'APPROVED', 'ours_on_head': 1},
        'judge': {'scan': 5, 'markers': 5, 'decisions': 0, 'plain': 5, 'length': 5, 'useful': 4}},
    # 2026-10-29 22:03Z-22:41Z (the mayor; the reviewer on the #1970 shape: "much better ... we should check
    # for consistency with the goals and high level rules of the repo"): R28-R32, R36, R37.
    'case49-honesty-line-above-the-verdict': {
        'expect': 'fail', 'expect_reasons': ['R28'], 'outcome': 'low-risk', 'facts': pr_format.GREEN_FACTS,
        'body': "Automated review by Reviewer's review agents (Claude Code); Reviewer hasn't read this PR yet.\n\n" + body(**LOW)},
    'case50-cleared-entry-leads-with-a-category-label': {
        'expect': 'fail', 'expect_reasons': ['R29'], **dict(LOW, cleared=[dict(CLEARED, change='Behaviour patients notice')])},
    'case51-status-line-on-a-green-pr': {
        'expect': 'fail', 'expect_reasons': ['R30'], 'outcome': 'low-risk', 'facts': pr_format.GREEN_FACTS,
        'body': body(**LOW) + '\n\n' + pr_format.GREEN_FACTS},
    'case52-safe-to-skip-without-the-files-link': {
        'expect': 'fail', 'expect_reasons': ['R30'], 'outcome': 'low-risk', 'facts': pr_format.GREEN_FACTS,
        'body': re.sub(r'^\*\*Safe to skip:\*\* .*$', '**Safe to skip:** tests: notify.test.ts', body(**LOW), flags=re.M)},
    'case53-cleared-risk-without-a-reason': {
        'expect': 'fail', 'expect_reasons': ['R31'], **LOW, 'judge_risky': [1],
        'judge': {'scan': 5, 'markers': 5, 'decisions': 0, 'plain': 5, 'length': 5, 'useful': 4}},
    'case54-risky-cleared-entry-as-a-question': {
        'expect': 'pass', 'outcome': 'questions', 'diff': DIFF, 'flagged': [FLAG], 'cleared': [],
        'items': [pr_review.question_from(CLEARED)], **SKIP},
    'case55-money-place-without-its-line': {
        'expect': 'fail', 'expect_reasons': ['R32'], 'outcome': 'questions', 'diff': DIFF, 'items': [], 'model': False,
        'tree_lines': TREE, 'not_checked': NOT_CHECKED,
        'decisions': [dict(MONEY, where=[dict(MONEY['where'][0], what='')])]},
    'case56-not-checked-said-twice': {
        'expect': 'fail', 'expect_reasons': ['R32'], 'outcome': 'questions', 'n_author_decisions': 1,
        'not_checked': NOT_CHECKED, 'facts': pr_format.GREEN_FACTS,
        'body': body(outcome='questions', decisions=[MONEY], not_checked=NOT_CHECKED, model=False)
        + '\n\n<sub>Not checked: billing, plans, credits or pricing.</sub>'},
    'case57-approval-with-more-than-what-was-checked': {
        'expect': 'fail', 'expect_reasons': ['R36'], 'outcome': 'low-risk', 'approve': True, 'facts': pr_format.GREEN_FACTS,
        'body': body(**dict(LOW, approve=True)) + '\n\n**Safe to skip:** 1 test file.'},
    'case58-cleared-entries-repeat': {
        'expect': 'fail', 'expect_reasons': ['R36'], **dict(LOW, cleared=[CLEARED, dict(CLEARED, change='SMS send added here')])},
    'case59-cleared-link-on-a-comment-line': {  # the flag on the code above it (v5.8.1: a flagged comment may link itself)
        'expect': 'fail', 'expect_reasons': ['R36'],
        **dict(LOW, diff=COMMENT_DIFF, flagged=[dict(FLAG, line=13)])},
    'case60-decision-asks-the-reviewer-to-read-code': {
        'expect': 'fail', 'expect_reasons': ['R37'], 'outcome': 'escalate', 'diff': DIFF, 'tree_lines': TREE,
        'items': [], **SKIP,
        'decisions': [dict(DECISION, options=['You review the changed lines yourself', 'Send only the clinic name and time'])]},
    'case61-finding-without-its-rule': {
        'expect': 'fail', 'expect_reasons': ['R37'], 'outcome': 'changes', 'diff': DIFF, 'items': [GOOD_ITEM],
        **SKIP, 'rules': RULES},
    'case62-finding-cites-its-rule': {
        'expect': 'pass', 'outcome': 'changes', 'diff': DIFF, 'items': [dict(GOOD_ITEM, rule='D1')],
        **SKIP, 'rules': RULES},
    # 2026-10-30 01:40Z (the mayor's reader on 9a62c85: a cleared entry opened with a raw identifier; a
    # conflict blocker named no files).
    'case67-cleared-change-names-an-identifier': {
        'expect': 'fail', 'expect_reasons': ['R29'], **dict(LOW, cleared=[dict(CLEARED, change='Calls sendReminder after queueing')])},
    'case68-conflict-names-its-files': {
        'expect': 'pass', 'outcome': 'changes', 'diff': DIFF, 'items': [], 'model': False,
        'facts': 'CI passed · merge conflict · open',
        'blockers': pr_format.blockers('example/repo', {'number': 1, 'mergeable': 'CONFLICTING'}, 'green', (),
                                       ['src/notify/send.ts'])},
    # 2026-10-30 02:24Z (the mayor on the 29a3588 money review: one point short on length; line 2 said
    # "Author: nothing" though the recommended option gave the author a task): R32, R22. Since v5.5 the
    # money sign-off is the author's question, so a decision for the reviewer carries the task.
    'case69-line-2-misses-the-recommended-task': {
        'expect': 'fail', 'expect_reasons': ['R22'], 'outcome': 'escalate', 'diff': DIFF, 'items': [], 'model': False,
        'tree_lines': TREE, 'decisions': [dict(DECISION, author_task='send only the clinic name and time')],
        'body_edit': ['Author: if Reviewer picks B, send only the clinic name and time.', 'Author: nothing.']},
    # 2026-10-30 03:05Z (the mayor: 28 links for 20 places): each code place is linked once. R21.
    'case72-code-place-linked-twice': {
        'expect': 'fail', 'expect_reasons': ['R21'], 'outcome': 'questions', 'diff': DIFF, 'items': [], 'model': False,
        'tree_lines': TREE, 'decisions': [MONEY], 'not_checked': NOT_CHECKED,
        'body_edit': ['   **Recommended: A.**', '   Also: [`send.ts` L13-L16](' + pr_format.code_link(
            'example/repo', 'a' * 40, 'src/notify/send.ts', 13, 16) + ')\n   **Recommended: A.**']},
    # 2026-10-30 02:35Z (the mayor: the question named an effect no linked place backs).
    'case71-money-opening-without-its-places': {
        'expect': 'fail', 'expect_reasons': ['R32'], 'outcome': 'questions', 'diff': DIFF, 'items': [], 'model': False,
        'tree_lines': TREE, 'decisions': [dict(MONEY, opening_places=[])], 'not_checked': NOT_CHECKED},
    'case70-money-places-collapsed': {
        'expect': 'pass', 'outcome': 'questions', 'derived': True, 'diff': DIFF, 'items': [], 'model': False,
        'tree_lines': {**TREE, **{f'src/billing/p{k}.ts': 40 for k in range(9)}},
        'not_checked': NOT_CHECKED, 'decisions': [pr_format.not_checked_decision(
            [{'path': f'src/billing/p{k}.ts', 'line_start': 10, 'line_end': 20, 'kind': ['credits', 'plans'][k % 2],
              'what': f'Place {k} now adds a late fee to overdue invoices.', 'behaviour': k < 7, 'risk': 1 + k % 5}
             for k in range(9)], NOT_CHECKED, 'Author', 'Overdue invoices get a late fee, on by default.', [0, 2])]},
    # 2026-10-30 01:40Z (the mayor: a formal block on a merge conflict outlived the conflict): the sweep lifts
    # a block that rested only on a conflict or CI once both are fixed; a block on content gets a review of
    # the new head, once.
    'case63-mechanical-block-lifted': {
        'expect': 'pass', 'rule': 'R35', 'route': 'formal_block', 'want': 'lift',
        'args': {'old': RC_OLD, 'head': 'b' * 40, 'mergeable': 'MERGEABLE', 'ci': 'green'}},
    'case64-mechanical-block-kept-while-ci-is-red': {
        'expect': 'pass', 'rule': 'R35', 'route': 'formal_block', 'want': None,
        'args': {'old': RC_OLD, 'head': 'b' * 40, 'mergeable': 'MERGEABLE', 'ci': 'red'}},
    'case65-content-block-gets-a-review': {
        'expect': 'pass', 'rule': 'R35', 'route': 'formal_block', 'want': 'review',
        'args': {'old': dict(RC_OLD, block='content'), 'head': 'b' * 40, 'mergeable': 'MERGEABLE', 'ci': 'green'}},
    'case66-content-block-held-head-not-looped': {
        'expect': 'pass', 'rule': 'R35', 'route': 'formal_block', 'want': None,
        'args': {'old': dict(RC_OLD, block='content', head='b' * 40), 'head': 'b' * 40, 'mergeable': 'MERGEABLE', 'ci': 'green'}},
    # 2026-10-29 22:11Z (the mayor's test batch): three PRs requested the reviewer after the first sweep had
    # set them aside (no rule) and were never selected. The sweep queues a requested PR again, but never
    # a head it already reviewed or held (that would loop).
    'case38-request-added-after-the-first-sweep': {
        'expect': 'pass', 'rule': 'sweep', 'route': 'asked_again', 'want': True,
        'args': {'old': {'head': 'a' * 40, 'status': 'done', 'outcome': 'no-rule'}, 'head': 'a' * 40, 'asked': True}},
    'case39-held-head-not-queued-again': {
        'expect': 'pass', 'rule': 'sweep', 'route': 'asked_again', 'want': False,
        'args': {'old': {'head': 'a' * 40, 'status': 'done', 'outcome': 'held-gate'}, 'head': 'a' * 40, 'asked': True}},
    'case40-requested-again-on-a-new-head': {
        'expect': 'pass', 'rule': 'sweep', 'route': 'asked_again', 'want': True,
        'args': {'old': {'head': 'a' * 40, 'status': 'done', 'outcome': 'questions'}, 'head': 'b' * 40, 'asked': True}},
    # 2026-10-29 22:30Z (the batch: a requested PR with no CI came out "questions" with no model and no
    # question, useful 1): missing or red CI never skips the model on a requested PR. R34.
    'case41-requested-pr-without-ci-gets-the-model': {
        'expect': 'pass', 'rule': 'R34', 'route': 'ci_only', 'want': False,
        'args': {'ci': 'none', 'requested': True, 'content': False}},
    'case42-unrequested-ci-only-escalation': {
        'expect': 'pass', 'rule': 'R11', 'route': 'ci_only', 'want': True,
        'args': {'ci': 'red', 'requested': False, 'content': False}},
    # 22:30Z (6 of 17 requested PRs were held and silent, "the worst failure"): never silent. R33.
    'case43-held-requested-pr-gets-the-note': {
        'expect': 'pass', 'rule': 'R33', 'route': 'held_fallback', 'want': 'not-reviewed',
        'args': {'requested': True, 'not_checked': False, 'gate_held': True}},
    'case44-held-billing-pr-gets-the-money-review': {
        'expect': 'pass', 'rule': 'R33', 'route': 'held_fallback', 'want': 'not-checked-minimal',
        'args': {'requested': False, 'not_checked': True, 'gate_held': True}},
    'case45-not-reviewed-note': {
        'expect': 'pass', 'outcome': 'not-reviewed', 'line2': 'Reviewer: nothing. Author: 1 question, get CI running.', 'diff': DIFF, 'items': [], 'model': False, 'tree_lines': TREE,
        'decisions': [FLOOR], 'blockers': NO_CI, 'facts': 'No CI ran · no merge conflict · open'},
    # 2026-10-30 02:11Z (the mayor: a bare note "still forces the reviewer to read the PR"): the note keeps its floor.
    'case46-note-without-its-decision': {
        'expect': 'fail', 'expect_reasons': ['R33'], 'outcome': 'not-reviewed', 'diff': DIFF, 'items': [], 'model': False,
        'decisions': [], 'blockers': NO_CI, 'facts': 'No CI ran · no merge conflict · open'},
    # 22:30Z (a COMMENT review said "Changes requested": the author sees a review that claims to block and
    # does not) and 23:09Z (the reviewer: "yes, we should use the formal github request changes"): the
    # word matches the event. R35.
    'case47-comment-review-says-changes-requested': {
        'expect': 'fail', 'expect_reasons': ['R35'], 'outcome': 'changes', 'n_items': 1,
        'body': body(outcome='changes', items=[GOOD_ITEM], **SKIP, request_changes=True)},
    'case48-formal-request-changes': {
        'expect': 'pass', 'outcome': 'changes', 'diff': DIFF, 'items': [GOOD_ITEM], **SKIP,
        'request_changes': True,
        'delivery': {'pr_state': 'OPEN', 'review_url': REVIEW, 'review_state': 'CHANGES_REQUESTED', 'ours_on_head': 1}},
    # gc-sy1hm.2 b (parity with the paid review tools: one-click fixes the author commits): a change that
    # rewrites its own line carries the fix; a fix that could close its code fence, sits on a question,
    # changes nothing or runs longer than 12 lines is refused (the script drops it and keeps the item).
    'case73-change-with-a-one-click-fix': {
        'expect': 'pass', 'outcome': 'changes', 'diff': DIFF, 'items': [SUGGEST_ITEM], **SKIP},
    'case74-one-click-fix-closes-its-fence': {
        'expect': 'fail', 'expect_reasons': ['R4'], 'only_reasons': ['R4'], 'outcome': 'changes', 'diff': DIFF,
        'items': [dict(SUGGEST_ITEM, suggestion='  logger.info(visit.id);\n```\n![x](https://example.com/a.png)')], **SKIP},
    'case75-one-click-fix-on-a-question': {
        'expect': 'fail', 'expect_reasons': ['R4'], 'only_reasons': ['R4'], 'outcome': 'questions', 'diff': DIFF,
        'items': [dict(SUGGEST_ITEM, kind='question', ask='Does the log line need the message text?')], **SKIP},
    'case76-one-click-fix-changes-nothing': {
        'expect': 'fail', 'expect_reasons': ['R4'], 'only_reasons': ['R4'], 'outcome': 'changes', 'diff': DIFF,
        'items': [dict(SUGGEST_ITEM, suggestion='  logger.info(`sent reminder ${body}`);')], **SKIP},
    'case77-one-click-fix-too-long': {
        'expect': 'fail', 'expect_reasons': ['R4'], 'only_reasons': ['R4'], 'outcome': 'changes', 'diff': DIFF,
        'items': [dict(SUGGEST_ITEM, suggestion='\n'.join(['  logger.info(visit.id);'] * 13))], **SKIP},
    # gc-sy1hm.2 c (incremental reviews): a review of a new head says, in one line linked to the new commits,
    # what became of our earlier asks; in an approval the line sits in "What was checked" (R36).
    'case78-rereview-says-what-happened-since': {
        'expect': 'pass', 'outcome': 'changes', 'diff': DIFF, 'items': [GOOD_ITEM], **SKIP, 'since': SINCE},
    'case79-approval-says-what-happened-since': {
        'expect': 'pass', **dict(LOW, approve=True), 'approve_check': GREEN,
        'since': dict(SINCE, again=0, open=0, resolve=[['src/notify/send.ts', 14, 'fixed']])},
    'case80-low-risk-with-an-earlier-ask-still-open': {
        'expect': 'fail', 'expect_reasons': ['R2'], 'only_reasons': ['R2'], **LOW, 'since': dict(SINCE, again=0)},
    # 2026-10-30 05:31Z (decision-554d2e7b): "Needs you" only with an option the reviewer carries out; a
    # decision whose options are all the author's is a question for the author. Each option starts with
    # who carries it out, checked against the names (R16, R22).
    'case81-all-author-options-are-not-needs-you': {
        'expect': 'fail', 'expect_reasons': ['R16', 'R22'], 'outcome': 'escalate', 'diff': DIFF, 'tree_lines': TREE,
        'items': [], 'decisions': [AUTHORS_CALL], **SKIP},
    'case82-all-author-options-are-a-question-for-the-author': {
        'expect': 'pass', 'outcome': 'questions', 'derived': True, 'line2': 'Reviewer: nothing. Author: 1 question.',
        'diff': DIFF, 'tree_lines': TREE, 'items': [], 'decisions': [AUTHORS_CALL], **SKIP},
    'case83-delivered-billing-decision-for-the-reviewer': {
        'expect': 'fail', 'expect_reasons': ['R22'], 'only_reasons': ['R22'], 'outcome': 'escalate', 'n_decisions': 1,
        'not_checked': NOT_CHECKED, 'facts': pr_format.GREEN_FACTS, 'model': False,
        'body': body(outcome='escalate', decisions=[MONEY_V54], not_checked=NOT_CHECKED, model=False)},
    'case84-question-for-the-author-with-a-reviewer-option': {
        'expect': 'fail', 'expect_reasons': ['R22'], 'outcome': 'questions', 'diff': DIFF, 'tree_lines': TREE,
        'items': [], 'decisions': [dict(DECISION, actors=['author', 'author'])], **SKIP},
    'case85-reviewer-option-without-the-reviewer-name': {
        'expect': 'fail', 'expect_reasons': ['R22'], 'outcome': 'escalate', 'diff': DIFF, 'tree_lines': TREE,
        'items': [], 'decisions': [dict(DECISION, options=['Allow it, with the SMS vendor under a BAA',
                                                            'Author sends only the clinic name and time'])], **SKIP},
    'case86-decision-without-actors': {
        'expect': 'fail', 'expect_reasons': ['R16'], 'outcome': 'escalate', 'diff': DIFF, 'tree_lines': TREE,
        'items': [], 'decisions': [{k: v for k, v in DECISION.items() if k != 'actors'}], **SKIP},
    # decision-3aeec149: a finding with choices goes to the author, as their question.
    'case87-finding-with-choices-goes-to-the-author': {
        'expect': 'pass', 'outcome': 'changes', 'derived': True,
        'line2': 'Reviewer: nothing. Author: 1 change, 1 question.',
        'diff': DIFF, 'tree_lines': TREE, 'items': [GOOD_ITEM], 'decisions': [AUTHORS_CALL], **SKIP},
    'case88-gap-of-an-unknown-kind': {
        'expect': 'fail', 'expect_reasons': ['R16'], 'only_reasons': ['R16'], **LOW, 'gaps': [{'kind': 'hunch'}]},
    # decision-ed7320b7 (the reviewer, 06:05Z: "do not post ever that i did not read the PRs" / "just act as
    # if you are me"): with voice 'reviewer', line 1 is the verdict word and an invisible marker, line 2
    # what the author must do, a gap is said as scope, and no disclosure phrase appears anywhere (R38).
    'case89-voice-changes': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'changes', 'derived': True, 'line2': 'Author: 1 change.',
        'diff': DIFF, 'items': [GOOD_ITEM], **SKIP},
    'case90-voice-billing-is-scope': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'questions', 'derived': True, 'line2': 'Author: 1 question.',
        'diff': DIFF, 'items': [], 'decisions': [MONEY_VOICE], 'tree_lines': TREE, 'flagged': [FLAG],
        'cleared': [CLEARED], 'not_checked': NOT_CHECKED, 'gaps': [{'kind': 'not-checked', 'accepted': True}], **SKIP},
    'case91-voice-approval': {
        'expect': 'pass', 'voice': 'reviewer', **dict(LOW, approve=True), 'approve_check': GREEN,
        'line2': 'Author: nothing needed.',
        # The mayor, 2026-10-30 07:21Z: the one-line reason above the collapsed section, not only inside it.
        'body_re': [r'^Author: nothing needed\.\n\nThe send call is the existing reminder path[^\n]*\n\n'
                    r'<details><summary>What was checked</summary>']},
    'case92-voice-note-is-the-author-question': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'not-reviewed', 'line2': 'Author: 1 question, get CI running.',
        'diff': DIFF, 'items': [], 'model': False, 'tree_lines': TREE, 'decisions': [FLOOR_VOICE], 'blockers': NO_CI,
        'facts': 'No CI ran · no merge conflict · open', 'gaps': [{'kind': 'below-bar'}]},
    'case93-voice-author-choice-starts-with-you': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'questions', 'derived': True, 'line2': 'Author: 1 question.',
        'diff': DIFF, 'tree_lines': TREE, 'items': [], 'decisions': [AUTHORS_CALL_VOICE], **SKIP},
    'case94-voice-author-choice-names-the-author': {
        'expect': 'fail', 'expect_reasons': ['R22'], 'only_reasons': ['R22'], 'voice': 'reviewer', 'outcome': 'questions',
        'diff': DIFF, 'tree_lines': TREE, 'items': [], 'decisions': [AUTHORS_CALL], **SKIP},
    'case95-voice-disclosure-in-the-body': {
        'expect': 'fail', 'expect_reasons': ['R38'], 'only_reasons': ['R38'], 'voice': 'reviewer', 'outcome': 'changes',
        'diff': DIFF, 'items': [GOOD_ITEM], **SKIP,
        'body_edit': ['Reminders now name the patient', 'An automated review found reminders now name the patient']},
    'case96-voice-disclosure-in-an-inline-comment': {
        'expect': 'fail', 'expect_reasons': ['R38'], 'only_reasons': ['R38'], 'voice': 'reviewer', 'outcome': 'changes',
        'diff': DIFF, **SKIP,
        'items': [dict(GOOD_ITEM, why='The Claude Code review found the log line writes the message body with the name.')]},
    'case97-voice-line-1-keeps-the-old-note': {
        'expect': 'fail', 'expect_reasons': ['R28', 'R38'], 'voice': 'reviewer', 'outcome': 'changes', 'diff': DIFF,
        'items': [GOOD_ITEM], **SKIP,
        'body_edit': ['**Changes suggested** <!-- pr-review v5.6 -->', "**Changes suggested** · automated, Reviewer hasn't read this PR"]},
    'case98-voice-delivered-body': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'changes', 'n_items': 1, 'model': True,
        'facts': pr_format.GREEN_FACTS,
        'body': body(outcome='changes', items=[GOOD_ITEM], voice='reviewer', **SKIP)},
    # decision-ed7320b7: pr_restamp.py rewrites the reviews we delivered into the reviewer's voice, from
    # their text. Each shape we delivered, restamped, passes the voice gate (v3 keeps its old faults).
    'case99-restamp-v5-changes': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'changes', 'n_items': 1, 'facts': pr_format.GREEN_FACTS,
        'body': pr_restamp.voice_body(body(outcome='changes', items=[GOOD_ITEM], **SKIP), 'Reviewer', 'Author')},
    'case100-restamp-v55-billing-question': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'questions', 'n_author_decisions': 1, 'model': False,
        'not_checked': NOT_CHECKED, 'facts': pr_format.GREEN_FACTS,
        'body': pr_restamp.voice_body(body(outcome='questions', decisions=[MONEY], not_checked=NOT_CHECKED, model=False),
                                      'Reviewer', 'Author')},
    'case101-restamp-v54-billing-for-the-reviewer': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'questions', 'n_author_decisions': 1, 'model': False,
        'not_checked': NOT_CHECKED, 'facts': pr_format.GREEN_FACTS,
        'body': pr_restamp.voice_body(body(outcome='escalate', decisions=[MONEY_V54], not_checked=NOT_CHECKED, model=False),
                                      'Reviewer', 'Author')},
    'case102-restamp-note': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'not-reviewed', 'n_author_decisions': 1, 'model': False,
        'facts': 'No CI ran · no merge conflict · open',
        'body': pr_restamp.voice_body(body(outcome='not-reviewed', decisions=[FLOOR], blockers=NO_CI, model=False,
                                           facts='No CI ran · no merge conflict · open'), 'Reviewer', 'Author')},
    # 2026-10-30 06:4xZ (the mayor, two general rules): a question whose own explanation answers it is
    # cleared with that reason, and the PR may then be approved (R31, R27); a body item states its ask in
    # at most 20 words next to its link, and its inline comment carries the evidence (R4).
    'case104-self-answered-question-is-cleared': {
        'expect': 'pass', 'outcome': 'low-risk', 'derived': True, 'diff': DIFF, 'items': [SELF_ANSWERED],
        'flagged': [FLAG], 'cleared': [], 'judge_answered': [1], **SKIP},
    'case105-self-answered-question-then-approved': {
        'expect': 'pass', 'outcome': 'low-risk', 'approve': True, 'diff': DIFF, 'items': [SELF_ANSWERED],
        'flagged': [FLAG], 'cleared': [], 'judge_answered': [1], 'approve_check': GREEN, **SKIP},
    'case106-body-item-ask-over-20-words': {
        'expect': 'fail', 'expect_reasons': ['R29'], 'only_reasons': ['R29'], 'outcome': 'changes', 'diff': DIFF, **SKIP,
        'items': [dict(GOOD_ITEM, ask='Drop the message body from the info log line, log the visit id instead, and add '
                                      'a test that the log line never holds the name.')]},
    # v5.8 (the mayor, 2026-10-30 07:21Z).
    'case107-voice-nothing-needed-names-the-author': {
        'expect': 'fail', 'expect_reasons': ['R22', 'R36'], 'only_reasons': ['R22', 'R36'], 'voice': 'reviewer',
        **dict(LOW, approve=True), 'body_edit': ['Author: nothing needed.', 'Nothing needed.']},
    'case108-money-question-first-evidence-not-nested': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'questions', 'derived': True, 'line2': 'Author: 1 question.',
        'diff': DIFF, 'items': [], 'decisions': [MONEY3_VOICE], 'tree_lines': TREE, 'flagged': [FLAG],
        'cleared': [CLEARED], 'not_checked': NOT_CHECKED, 'gaps': [{'kind': 'not-checked', 'accepted': True}], **SKIP,
        'body_re': [r'^1\. \*\*What sign-off [^\n]*\?\*\* · \[`send\.ts` L13-L16\]\(https://github\.com/[^)]+\)\n'
                    r'   Billing, plans, credits or pricing are outside this review\. Overdue invoices [^\n]*\n   A\. You ',
                    r'^\*\*Plans:\*\*\n- \[`send\.ts` L20-L22\]'],
        'body_not_re': [r'^ +\*\*(Charges|Credits|Plans|Limits|Other):\*\*', r'^ +- \[`send\.ts` L13-L16\]']},
    'case109-recommendation-repeats-its-option': {
        'expect': 'fail', 'expect_reasons': ['R16'], 'only_reasons': ['R16'], 'voice': 'reviewer',
        'outcome': 'not-reviewed', 'diff': DIFF, 'items': [], 'model': False, 'tree_lines': TREE, 'blockers': NO_CI,
        'decisions': [dict(FLOOR_VOICE, reason='A teammate who knows the code can check it in one pass.')],
        'facts': 'No CI ran · no merge conflict · open', 'gaps': [{'kind': 'below-bar'}]},
    # v5.8.1, the mayor's P1 (2026-10-30 16:19Z): a model's low-risk answer with several cleared entries,
    # whose flags sit on comments (one in a code hunk, one in a hunk of comments only), approved; the
    # summary's line break does not break the approval's one-line reason.
    'case110-voice-approval-flags-on-comments': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'low-risk', 'derived': True, 'approve': True,
        'approve_check': GREEN, 'line2': 'Author: nothing needed.', 'diff': COMMENTS_DIFF, 'items': [],
        'flagged': [FLAG_C1, FLAG_C2], 'cleared': [CLEARED_C1, CLEARED_C2],
        'summary': 'Comments say how\nreminders are sent.', 'skip': [],
        'body_re': [r'^Author: nothing needed\.\n\nComments say how reminders are sent\.\n\n'
                    r'<details><summary>What was checked</summary>\n\n- \*\*[^\n]*\n- \*\*[^\n]*\n\n</details>$']},
    'case111-markdown-heading-is-not-a-comment': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'low-risk', 'derived': True, 'approve': True,
        'approve_check': GREEN, 'diff': COMMENTS_DIFF, 'items': [], 'flagged': [FLAG_MD], 'cleared': [CLEARED_MD],
        'summary': 'The guide says how reminders are sent.', 'skip': []},
    # The flow fix (the mayor, 2026-10-30 16:10Z): a review held only by reasons the script fixes from its
    # own parts is repaired and gated again, not replaced by a fallback; a content reason still holds it.
    'case112-repair-repeated-cleared-entry-then-approved': {  # the #1982 shape: low risk, R36 only
        'expect': 'pass', 'voice': 'reviewer', **dict(LOW, approve=True), 'approve_check': GREEN,
        'cleared': [CLEARED, dict(CLEARED, change='SMS reminder path')], 'repair': ['R36'],
        'line2': 'Author: nothing needed.'},
    'case113-repair-question-mark': {
        'expect': 'pass', 'outcome': 'questions', 'derived': True, 'diff': DIFF, 'flagged': [FLAG],
        'items': [dict(QUESTION, ask='Does the SMS text need the full name at all.')], 'repair': ['R13'], **SKIP},
    'case114-no-rewrite-answer-stays-held': {  # the turn gave nothing back: R36 repaired, R29 still holds it
        'expect': 'fail', 'expect_reasons': ['R29'], 'only_reasons': ['R29'], 'voice': 'reviewer',
        **dict(LOW, approve=True),
        'cleared': [dict(CLEARED, change='sendReminder now uses SMS'), dict(CLEARED, change='SMS reminder path')],
        'repair': ['R36']},
    # The narrow repair turn (the mayor, 2026-10-30 16:36Z): an otherwise clean review whose entries fail a
    # wording rule is rewritten (`rewrite`: the turn's answer) and posted, never replaced by a fallback.
    'case117-rewrite-r29-then-approved': {  # the #1982 shape: low risk, a cleared entry fails R29 twice
        'expect': 'pass', 'voice': 'reviewer', **dict(LOW, approve=True), 'approve_check': GREEN, 'repair': [],
        'cleared': [dict(CLEARED, change='sendReminder now calls smsClient.send', why='The send call is the existing reminder path that every visit already used before this change, and only the text of the message changes here, so it carries no new recipient and no new provider at all.')],
        'rewrite': {'cleared entry 1': {'change': 'Reminder now sent by SMS',
                                        'why': 'The send call is the existing reminder path; only the text changes.'}},
        'line2': 'Author: nothing needed.'},
    'case118-rewrite-r29-question': {  # the #112 shape: a question whose change is not 3-6 plain words
        'expect': 'pass', 'outcome': 'questions', 'derived': True, 'diff': DIFF, 'flagged': [FLAG], 'repair': [],
        'items': [dict(QUESTION, change='patient data')],
        'rewrite': {'item 1': {'change': 'Reminder text names the patient'}}, **SKIP},
    'case119-no-rewrite-with-a-content-reason': {
        'expect': 'fail', 'expect_reasons': ['R19', 'R29'], 'only_reasons': ['R19', 'R29'], **LOW, 'repair': [],
        'flagged': [FLAG, dict(FLAG, id='F2', path='test/notify.test.ts', line=2)],
        'cleared': [dict(CLEARED, change='sendReminder now uses SMS')],
        'rewrite': {'cleared entry 1': {'change': 'Reminder now sent by SMS'}}},
    'case120-rewrite-answer-beyond-the-ask-is-ignored': {
        'expect': 'pass', 'outcome': 'questions', 'derived': True, 'diff': DIFF, 'flagged': [FLAG], 'repair': [],
        'items': [dict(QUESTION, change='patient data')],
        'rewrite': {'item 1': {'change': 'Reminder text names the patient', 'ask': 'x'},
                    'cleared entry 9': {'change': 'x'}}, **SKIP},
    # The live shapes the first turns missed (the mayor, 2026-10-30 16:54Z): the turn's answer as versions,
    # best first; the script takes the first that passes the field's own checks (the model does not count).
    'case121-rewrite-versions-change-of-seven-words': {  # the #112 shape: a cleared change of 7 words
        'expect': 'pass', **LOW, 'repair': [],
        'cleared': [dict(CLEARED, change='Reminder now goes out by SMS text')],
        'rewrite': {'cleared entry 1': {'change': ['Reminder now goes out by SMS text', 'Reminder now sent by SMS']}}},
    'case122-rewrite-versions-asks-over-twenty-words': {  # the #1982 shape: R31's questions, asks of 27 and 21 words
        'expect': 'pass', 'outcome': 'questions', 'derived': True, 'diff': DIFF, 'flagged': [FLAG], 'repair': [],
        'items': [dict(QUESTION, ask='Does the SMS text need the full name and the date of birth at all, given the provider stores every message body for ninety days by default?'),
                  dict(QUESTION, line_start=16, line_end=16, change='Reminder returns before the queue check',
                       ask='Can you confirm that returning here is intended even when the queue add fails, since the caller then never retries it?')],
        'rewrite': {'item 1': {'ask': ['Does the SMS text need the full name and the date of birth at all, given the provider stores every message body for ninety days by default?', 'Does the SMS text need the full name and date of birth?']},
                    'item 2': {'ask': ['Is returning here intended when adding to the queue fails?']}}, **SKIP},
    'case123-note-over-ten-places-as-a-fallback': {  # the #112 shape: the note itself was held (R33) and nothing posted
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'not-reviewed', 'diff': DIFF, 'items': [], 'model': False,
        'blockers': NO_CI, 'fallback': True, 'facts': 'No CI ran · no merge conflict · open', 'gaps': [{'kind': 'below-bar'}],
        'decisions': [pr_format.floor_decision(
            [{'path': f'src/notify/part{k}.ts', 'line_start': 3, 'line_end': 5, 'what': FLAG['label']} for k in range(10)],
            'Author', 'Held.', voice='reviewer')],
        # R33 (the mayor, 21:39Z: no orphan link): the other places collapsed, each linked to its lines.
        'body_re': [r'^- A message sent to people \(SMS, email, push\): (\[`part\d\.ts` L3-L5\]\(https://github\.com/example/'
                    r'repo/blob/a{40}/src/notify/part\d\.ts#L3-L5\)(, )?){7}$', r'^<details><summary>Other places</summary>$'],
        'body_not_re': [r'/pull/1/files\)']},
    # R33 (the mayor, 2026-10-30 10:25Z): the fallback passes the bar the full review failed; the #1981 shape.
    'case115-fallback-money-places-cut-to-the-question': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'questions', 'derived': True, 'diff': DIFF, 'items': [],
        'model': False, 'decisions': [MONEY_MANY_VOICE], 'not_checked': NOT_CHECKED,
        'fallback': True, 'gaps': [{'kind': 'not-checked', 'accepted': True}, {'kind': 'below-bar', 'accepted': True}],
        # R33 (the mayor, 21:39Z: no orphan link): the other places collapsed, each linked to its lines.
        'body_re': [r'^<details><summary>Other places</summary>$', r'^- No behaviour change: (\[`part1\d\.ts` L10-L12\]\('
                    r'https://github\.com/example/repo/blob/a{40}/src/billing/part1\d\.ts#L10-L12\)(, )?){4}$'],
        'body_not_re': [r'/pull/1/files\)']},
    # A full review's money decision is over the fallback's bar, and its line 1 does not say who acts (R28).
    'case116-fallback-over-the-bar': {
        'expect': 'fail', 'expect_reasons': ['R33', 'R28'], 'only_reasons': ['R33', 'R28'], 'voice': 'reviewer',
        'outcome': 'questions', 'n_author_decisions': 1, 'model': False, 'not_checked': NOT_CHECKED, 'fallback': True,
        'body': body(outcome='questions', decisions=[MONEY_MANY_VOICE], not_checked=NOT_CHECKED, model=False,
                     voice='reviewer')},
    # R33 (the mayor, 2026-10-30 21:39Z; 4 of 5 requested reviews fell back, and the reader found a template): one
    # question per linked place with its own reason (inline), no scope sentence and no options, the other places
    # collapsed and linked to their lines, the CI blocker said once, line 1 says who acts.
    'case124-fallback-asks-one-question-per-line': {
        'expect': 'pass', **ASKED_FX,
        'body_re': [r'^\*\*Changes suggested\*\* · for Author, nothing needed from me <!-- pr-review v[0-9.]+ -->$',
                    r'^Author: 3 questions, fix the failing checks\.$',
                    r'^1' + ASKED_ITEM_RE + r'\n2' + ASKED_ITEM_RE + r'\n3' + ASKED_ITEM_RE + r'\n4\. \[Fix the failing checks\]',
                    r'^- Billing, plans, credits or pricing: \[`limits\.ts` L2-L3\]\(https://github\.com/example/repo/blob/'
                    r'a{40}/src/billing/limits\.ts#L2-L3\)$'],
        'body_not_re': [r'/pull/1/files\)', r'(?i)outside this review', r'^CI red', r'Recommended:', r'^   [A-D]\. ']},
    'case125-fallback-orphan-files-link': {
        'expect': 'fail', 'expect_reasons': ['R33'], 'only_reasons': ['R33'], **ASKED_FX,
        'body_edit': ['</details>', '</details>\n\n[2 other places](https://github.com/example/repo/pull/1/files) in the files view.']},
    'case126-fallback-status-said-twice': {
        'expect': 'fail', 'expect_reasons': ['R33'], 'only_reasons': ['R33'], **ASKED_FX,
        'body_edit': ['</details>', '</details>\n\nCI red: unit tests · no merge conflict · open']},
    'case127-fallback-stock-question': {
        'expect': 'fail', 'expect_reasons': ['R33'], 'only_reasons': ['R33'],
        **dict(ASKED_FX, items=[dict(ASKED[0], ask='Was this change tested before release?')] + ASKED[1:])},
    'case128-voice-fallback-line-1-without-who-acts': {
        'expect': 'fail', 'expect_reasons': ['R28'], 'only_reasons': ['R28'], **ASKED_FX,
        'body_edit': [' · for Author, nothing needed from me', '']},
    # The note (no money decision) asks the same way: in the reviewer's voice "Questions", in the disclosing voice
    # "Not reviewed automatically" with why it was held; green CI, so no status line.
    'case129-note-asks-one-question-per-line': {
        'expect': 'pass', **dict(ASKED_FX, outcome='not-reviewed', derived=False, blockers=[], facts=GREEN_FACTS_TEXT,
                                 not_checked='', others=[]),
        'body_re': [r'^\*\*Questions\*\* · for Author, nothing needed from me <!-- pr-review v[0-9.]+ -->$',
                    r'^Author: 3 questions\.$', r'^3' + ASKED_ITEM_RE + r'$'],
        'body_not_re': [r'<details>', r'^CI ', r'/pull/1/files\)']},
    'case130-note-asks-in-the-disclosing-voice': {
        'expect': 'pass', **dict(ASKED_FX, voice='disclose', outcome='not-reviewed', derived=False, blockers=[],
                                 facts=GREEN_FACTS_TEXT, not_checked='', others=[],
                                 held_why="Our review of this PR didn't reach the quality bar: a second reader found it too "
                                          'hard to act on.'),
        'body_re': [r"^\*\*Not reviewed automatically\*\* · automated, Reviewer hasn't read this PR$",
                    r'^Reviewer: nothing\. Author: 3 questions\.$', r"^Our review of this PR didn't reach the quality bar"],
        'body_not_re': [r'nothing needed from me']},
    # The second answer after a reader hold adds cleared entries that name no flag on a PR with no flagged hunk
    # (2026-10-30: 3 of 3 such holds, all held by R19 alone): dropped by the repair, then gated again. A cleared
    # entry naming a flag that is not one of the PR's is still a content reason.
    'case131-repair-cleared-entries-naming-no-flag': {
        'expect': 'pass', 'outcome': 'questions', 'derived': True, 'diff': DIFF, 'flagged': [], 'items': [QUESTION],
        'cleared': [dict(CLEARED, flag=''), dict(CLEARED, flag='', line_start=16, line_end=16,
                                                 change='Reminder returns before queueing')],
        'repair': ['R19'], **SKIP},
    'case132-cleared-entry-naming-a-wrong-flag-stays-held': {
        'expect': 'fail', 'expect_reasons': ['R19'], 'only_reasons': ['R19'], 'outcome': 'questions', 'derived': True,
        'diff': DIFF, 'flagged': [], 'items': [QUESTION], 'cleared': [dict(CLEARED, flag='F9')], 'repair': [], **SKIP},
    'case103-restamp-v3': {
        'expect': 'fail', 'expect_reasons': ['R3'], 'only_reasons': ['R3', 'R7', 'R10', 'R22', 'R1', 'R8', 'R23', 'R30'],
        'voice': 'reviewer', 'outcome': 'questions', 'n_items': 0,
        'body': pr_restamp.voice_body(LEGACY_CI_BODY.replace('**Automated review**', 'Automated review'), 'Reviewer', 'Author')},
}

# decision-57b6d9bb (the reviewer, 2026-10-30 22:18Z: "make sure our system is able to re-review things when [the
# author] addresses questions or makes changes"): the sweep's follow-up triggers, structure only.
A40, B40 = 'a' * 40, 'b' * 40
REVIEWED = {'review_heads': [A40], 'head': A40, 'status': 'done', 'review_id': 77, 'head_review_ids': [77]}
for name, old in {'approval': dict(outcome='low-risk', approved=True, event='APPROVE'),
                  'comment': dict(outcome='questions', event='COMMENT'),
                  'change-request': dict(outcome='changes', event='REQUEST_CHANGES', rc_live=True),
                  'fallback': dict(outcome='questions', fallback='not-checked-minimal'),
                  'note': dict(outcome='not-reviewed', fallback='not-reviewed')}.items():
    CASES[f'case-fu-push-after-{name}'] = {'expect': 'pass', 'rule': 'followup', 'route': 'followup_push', 'want': True,
                                           'args': {'old': dict(REVIEWED, **old), 'head': B40}}
CASES.update({
    'case-fu-push-held-head-never-loops': {'expect': 'pass', 'rule': 'followup', 'route': 'followup_push', 'want': False,
                                           'args': {'old': dict(REVIEWED, head=B40, outcome='held-gate'), 'head': B40}},
    'case-fu-push-waiting-for-ci-keeps-its-mark': {
        'expect': 'pass', 'rule': 'followup', 'route': 'followup_push', 'want': True,
        'args': {'old': dict(REVIEWED, head=B40, status='waiting-ci', followup='push'), 'head': B40}},
    # A force-push is a new head like any other: the incremental part then says "rewritten" and the review is whole.
    'case-fu-push-force-push': {'expect': 'pass', 'rule': 'followup', 'route': 'followup_push', 'want': True,
                                'args': {'old': dict(REVIEWED, new_head='c' * 40), 'head': 'd' * 40}},
    'case-fu-push-never-a-pr-we-did-not-review': {'expect': 'pass', 'rule': 'followup', 'route': 'followup_push',
                                                  'want': False, 'args': {'old': {'head': A40, 'status': 'done',
                                                                                  'outcome': 'no-rule'}, 'head': B40}},
})


def thread(*comments, resolved_by=None, tid='T1'):
    return {'id': tid, 'isResolved': bool(resolved_by), 'resolvedBy': {'login': resolved_by} if resolved_by else None,
            'comments': {'nodes': list(comments)}}


def post(cid, who, at='2026-10-30T23:00:00Z', rid=None):
    return {'databaseId': cid, 'author': {'login': who}, 'createdAt': at, 'submittedAt': at,
            'pullRequestReview': {'databaseId': rid}}


OURS = post(1, 'reviewer', '2026-10-30T22:00:00Z', 77)
ACT_ARGS = {'us': 'Reviewer', 'since': '2026-10-30T22:00:00Z', 'our_reviews': [77]}
NONE_ACT = {'replies': 0, 'comments': 0, 'reviews': 0, 'resolved': 0, 'newest': None, 'key': ''}
CASES.update({
    'case-fu-answer-in-a-thread': {
        'expect': 'pass', 'rule': 'followup', 'route': 'answer_activity', 'want': dict(NONE_ACT, replies=1, newest=501, key='501'),
        'args': {'pr': {'author': {'login': 'author'}, 'reviewThreads': {'nodes': [thread(OURS, post(501, 'author'))]}},
                 **ACT_ARGS}},
    'case-fu-answer-as-a-pr-comment': {
        'expect': 'pass', 'rule': 'followup', 'route': 'answer_activity', 'want': dict(NONE_ACT, comments=1, newest=502, key='502'),
        'args': {'pr': {'author': {'login': 'author'}, 'comments': {'nodes': [post(502, 'author')]}}, **ACT_ARGS}},
    'case-fu-answer-a-resolved-thread': {
        'expect': 'pass', 'rule': 'followup', 'route': 'answer_activity',
        'want': dict(NONE_ACT, resolved=1, key='0+1:' + hashlib.sha1(b'T1').hexdigest()[:8]),
        'args': {'pr': {'author': {'login': 'author'}, 'reviewThreads': {'nodes': [thread(OURS, resolved_by='author')]}},
                 **ACT_ARGS}},
    # Our own reply in our thread, our own PR comment, and anything from before our review never count.
    'case-fu-answer-our-own-reply-ignored': {
        'expect': 'pass', 'rule': 'followup', 'route': 'answer_activity', 'want': NONE_ACT,
        'args': {'pr': {'author': {'login': 'author'}, 'reviewThreads': {'nodes': [thread(OURS, post(503, 'Reviewer'))]},
                        'comments': {'nodes': [post(504, 'reviewer'), post(400, 'author', '2026-10-30T21:00:00Z')]}},
                 **ACT_ARGS}},
    'case-fu-answer-first-round': {'expect': 'pass', 'rule': 'followup', 'route': 'answer_batch',
                                   'want': {'do': 'answer', 'key': '501'},
                                   'args': {'old': REVIEWED, 'head': A40, 'act': {'key': '501'}, 'cap': 3}},
    'case-fu-answer-the-same-batch-once': {'expect': 'pass', 'rule': 'followup', 'route': 'answer_batch',
                                           'want': {'do': None, 'key': None},
                                           'args': {'old': dict(REVIEWED, answers_key='501', answer_rounds={A40: 1}),
                                                    'head': A40, 'act': {'key': '501'}, 'cap': 3}},
    # A reply to our follow-up has a newer id: the next batch.
    'case-fu-answer-a-second-round': {'expect': 'pass', 'rule': 'followup', 'route': 'answer_batch',
                                      'want': {'do': 'answer', 'key': '777'},
                                      'args': {'old': dict(REVIEWED, answers_key='501', answer_rounds={A40: 1}),
                                               'head': A40, 'act': {'key': '777'}, 'cap': 3}},
    'case-fu-answer-the-round-cap': {'expect': 'pass', 'rule': 'followup', 'route': 'answer_batch',
                                     'want': {'do': 'cap', 'key': '901'},
                                     'args': {'old': dict(REVIEWED, answers_key='801', answer_rounds={A40: 3}),
                                              'head': A40, 'act': {'key': '901'}, 'cap': 3}},
    'case-fu-answer-after-the-cap-it-stops': {'expect': 'pass', 'rule': 'followup', 'route': 'answer_batch',
                                              'want': {'do': None, 'key': '991'},
                                              'args': {'old': dict(REVIEWED, answers_key='901', answer_rounds={A40: 3},
                                                                   answer_capped=A40), 'head': A40, 'act': {'key': '991'},
                                                       'cap': 3}},
    'case-fu-answer-not-while-queued': {'expect': 'pass', 'rule': 'followup', 'route': 'answer_batch',
                                        'want': {'do': None, 'key': None},
                                        'args': {'old': dict(REVIEWED, status='queued'), 'head': A40, 'act': {'key': '501'},
                                                 'cap': 3}},
    # PR_REVIEW_QUEUE=requested: an unrequested new PR never enters the queue; a requested one and a follow-up do.
    'case-fu-queue-unrequested-new-pr-stays-out': {'expect': 'pass', 'rule': 'followup', 'route': 'in_queue', 'want': False,
                                                   'args': {'queue': 'requested', 'requested': False, 'reviewed': False}},
    'case-fu-queue-requested-pr-enters': {'expect': 'pass', 'rule': 'followup', 'route': 'in_queue', 'want': True,
                                          'args': {'queue': 'requested', 'requested': True, 'reviewed': False}},
    'case-fu-queue-followup-enters': {'expect': 'pass', 'rule': 'followup', 'route': 'in_queue', 'want': True,
                                      'args': {'queue': 'requested', 'requested': False, 'reviewed': True}},
    # The mayor, 2026-10-30 22:26Z (the 1987 shape): the review of a new head on a PR we reviewed, which does not
    # request the reviewer, was held and nothing went out, so our change request stood on a head that was gone. A
    # follow-up is never silent: held, it gets the note; and the note says what of our earlier asks is still open.
    'case-fu-held-followup-is-never-silent': {'expect': 'pass', 'rule': 'R33', 'route': 'held_fallback', 'want': 'not-reviewed',
                                              'args': {'requested': False, 'not_checked': False, 'gate_held': True,
                                                       'reviewed': True}},
    'case-fu-held-unreviewed-unrequested-stays-silent': {'expect': 'pass', 'rule': 'R33', 'route': 'held_fallback',
                                                         'want': None, 'args': {'requested': False, 'not_checked': False,
                                                                                'gate_held': True, 'reviewed': False}},
    'case-fu-held-followup-note-says-what-is-open': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'not-reviewed', 'diff': DIFF, 'items': [], 'model': False,
        'tree_lines': TREE, 'decisions': [FLOOR_VOICE], 'fallback': True, 'gaps': [{'kind': 'below-bar'}],
        'since': {'old': A40, 'new': B40, 'review_id': 77, 'fixed': 0, 'again': 0, 'open': 2, 'gone': 0, 'changed': 1,
                  'resolve': []},
        'body_re': [r'^\*\*Since the last review\*\* \(\[new commits\]\(https://github\.com/example/repo/compare/a{40}\.\.\.'
                    r'b{40}\)\): 1 earlier ask on lines the new commits changed, 2 still open in their threads\.$',
                    r'^\*\*Questions\*\* · for Author, nothing needed from me <!-- pr-review v[0-9.]+ -->$']},
})

if __name__ == '__main__':
    out = os.path.join(HERE, 'fixtures')
    os.makedirs(out, exist_ok=True)
    for name, fx in CASES.items():
        with open(os.path.join(out, f'{name}.json'), 'w') as f:
            json.dump(fx, f, indent=1)
    stale = sorted(n for n in os.listdir(out) if n.endswith('.json') and n[:-5] not in CASES)
    print(f'{len(CASES)} fixtures in {out}' + (f'; stale (git rm them): {stale}' if stale else ''))
