#!/usr/bin/env python3
"""Writes the public regression fixtures (synthetic look-alikes; no real repository's text) into
tests/fixtures/. Run it after changing a case below (and `git rm` a fixture whose case is gone), then
run the regression:

  assets/scripts/pr-review-regress.sh
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', 'assets', 'scripts'))
import pr_format  # noqa: E402
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
GREEN = {'head': 'a' * 40, 'fresh': {'state': 'OPEN', 'checks': ['SUCCESS', 'SKIPPED']}}
PR = {'number': 1, 'state': 'OPEN', 'mergeable': 'MERGEABLE', 'headRefOid': 'a' * 40}


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
    'case59-cleared-link-on-a-comment-line': {
        'expect': 'fail', 'expect_reasons': ['R36'],
        **dict(LOW, diff=COMMENT_DIFF)},
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
        'line2': 'Nothing needed.'},
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
        'body_edit': ['Reminders now name the patient', 'An automated check found reminders now name the patient']},
    'case96-voice-disclosure-in-an-inline-comment': {
        'expect': 'fail', 'expect_reasons': ['R38'], 'only_reasons': ['R38'], 'voice': 'reviewer', 'outcome': 'changes',
        'diff': DIFF, **SKIP,
        'items': [dict(GOOD_ITEM, why='Claude Code found that the log line writes the message body with the name.')]},
    'case97-voice-line-1-keeps-the-old-note': {
        'expect': 'fail', 'expect_reasons': ['R28', 'R38'], 'voice': 'reviewer', 'outcome': 'changes', 'diff': DIFF,
        'items': [GOOD_ITEM], **SKIP,
        'body_edit': ['**Changes suggested** <!-- pr-review v5.6 -->', "**Changes suggested** · automated, Reviewer hasn't read this PR"]},
    'case98-voice-delivered-body': {
        'expect': 'pass', 'voice': 'reviewer', 'outcome': 'changes', 'n_items': 1, 'model': True,
        'facts': pr_format.GREEN_FACTS,
        'body': body(outcome='changes', items=[GOOD_ITEM], voice='reviewer', **SKIP)},
}

if __name__ == '__main__':
    out = os.path.join(HERE, 'fixtures')
    os.makedirs(out, exist_ok=True)
    for name, fx in CASES.items():
        with open(os.path.join(out, f'{name}.json'), 'w') as f:
            json.dump(fx, f, indent=1)
    stale = sorted(n for n in os.listdir(out) if n.endswith('.json') and n[:-5] not in CASES)
    print(f'{len(CASES)} fixtures in {out}' + (f'; stale (git rm them): {stale}' if stale else ''))
