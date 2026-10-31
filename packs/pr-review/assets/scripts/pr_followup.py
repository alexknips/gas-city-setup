#!/usr/bin/env python3
"""Same-head follow-ups on the author's answers (decision-57b6d9bb; the reviewer, 2026-10-30 22:18Z: "make
sure our system is able to re-review things when [the author] addresses questions or makes changes").

The sweep sees, at zero tokens, that the PR's author answered on a head we reviewed (a reply in one of our
review threads, a PR comment or a review, a thread of ours resolved: pr_rules.answer_activity) and queues a
follow-up. Its model call gets our delivered review's asks for the author with the replies in their
threads and the author's other comments (host-local: never in logs, beads, mail or Slack) and runs in the
PR's worktree at the head, read-only, so it can check what the answers say. It calls each ask:

  answered   cleared: the answer is the reason, linked to the ask's lines
  open       still an ask; one short follow-up goes into its thread (none when nobody answered it)
  change     the answer shows the code must change: the ask becomes a change

The review is then built again from the delivered one (pr_review.answer_review): updated in place or a new
one with the pointer, our change request dismissed when nothing is left, an approval when everything is
cleared and the PR meets the bar (R27). One follow-up per batch of answers (keyed on the newest reply),
at most PR_REVIEW_FOLLOWUP_ROUNDS per head; then one line tells the author and it stops.

  pr_followup.py --selftest   the asks, the threads, the calls and the rebuilt review on synthetic data
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_format  # noqa: E402

STATUSES = ('answered', 'open', 'change')
MAX_WHY, MAX_FOLLOW, MAX_ASK = 25, 25, 20  # words
MAX_ASKS = 12
MAX_TEXT = 1500  # characters of one reply shown to the model
MAX_COMMENTS = 10

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['calls'], 'properties': {
    'calls': {'type': 'array', 'maxItems': MAX_ASKS, 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['id', 'status', 'why', 'follow_up', 'ask'],
        'properties': {'id': {'type': 'string', 'description': 'E1, E2, ... or D1, D2, ...'},
                       'status': {'type': 'string', 'enum': list(STATUSES)},
                       'why': {'type': 'string', 'maxLength': 240},
                       'follow_up': {'type': 'string', 'maxLength': 240},
                       'ask': {'type': 'string', 'maxLength': 200}}}}}}

PROMPT = """You reviewed pull request {url} at head {head}. Its author, {author}, has answered since. The
repository is checked out at that head in your working directory: read the code to check what the answers
say, and never trust an answer the code contradicts. Below are the review's asks for the author (E1, E2,
... on their lines; D1, ... questions with options), each with the replies in its thread, then the
author's other comments on the PR, oldest first.

For every ask give one `calls` entry (`id`, `status`, `why`, `follow_up`, `ask`; leave unused fields empty):
- "answered": the answer settles it: the code does what the answer says, or the answer makes a sound case
  that the behaviour is intended. `why`: that answer as the reason, one fact of at most 25 words.
- "open": it still needs something. `why`: what is missing, at most 20 words. `follow_up`: one short reply
  for its thread, at most 25 words, to {author}, asking only for what is missing; empty when nobody
  answered this ask yet.
- "change": the answer shows the code must change. `ask`: the change to make, at most 20 words.
Plain words a person who does not read code understands: no code, no file or function names, no rule codes,
no links. Never raise anything new here: only the asks below.

THE ASKS:
{asks}

OTHER COMMENTS BY {author}:
{comments}"""

REPLIES_Q = """query($o: String!, $r: String!, $n: Int!) { repository(owner: $o, name: $r) { pullRequest(number: $n) {
  author { login }
  reviews(last: 50) { nodes { databaseId author { login } body submittedAt } }
  comments(last: 50) { nodes { databaseId author { login } body createdAt } }
  reviewThreads(first: 100) { nodes { id isResolved resolvedBy { login }
    comments(first: 50) { nodes { databaseId author { login } body createdAt path originalLine
      pullRequestReview { databaseId } } } } } } } }"""


def asks_of(result):
    """Our delivered review's asks for the author: the items (E1..) and the author's decisions (D1..)."""
    out = [{'id': f'E{k}', 'kind': 'item', 'it': it} for k, it in enumerate(result.get('items') or [], 1)]
    theirs = pr_format.split(result.get('decisions') or [])[1]
    return (out + [{'id': f'D{k}', 'kind': 'decision', 'it': d} for k, d in enumerate(theirs, 1)])[:MAX_ASKS]


def login(n):
    return (((n or {}).get('author') or {}).get('login') or '').lower()


def who(c, us, author, our_posts=()):
    """Who wrote a comment: 'we', 'author' or 'someone'. On a PR whose author is the reviewer (gcs-71c), ours
    are the posts we recorded (`our_posts`) and every other post of that login is the author's."""
    if c.get('databaseId') in our_posts:
        return 'we'
    if login(c) == author:
        return 'author'
    return 'we' if login(c) == us else 'someone'


def threads_of(pr, us, asks, our_reviews=(), our_posts=()):
    """{ask id: {'comment': our first comment's id, 'replies': [(who, text)], 'resolved': bool}}: the thread
    our review opened on each item's lines (the inline comment sits on its last line)."""
    us, out = (us or '').lower(), {}
    for t in (pr.get('reviewThreads') or {}).get('nodes') or []:
        cs = (t.get('comments') or {}).get('nodes') or []
        if not cs or login(cs[0]) != us:
            continue
        rid = (cs[0].get('pullRequestReview') or {}).get('databaseId')
        if our_reviews and rid not in our_reviews:
            continue
        for a in asks:
            it = a['it']
            if a['kind'] == 'item' and a['id'] not in out and cs[0].get('path') == it.get('path') and \
                    cs[0].get('originalLine') == it.get('line_end'):
                out[a['id']] = {'comment': cs[0].get('databaseId'), 'resolved': bool(t.get('isResolved')),
                                'replies': [(who(c, us, login(pr), our_posts), (c.get('body') or '')[:MAX_TEXT])
                                            for c in cs[1:]]}
    return out


def others_of(pr, us, since, our_posts=()):
    """The author's PR comments and review bodies after `since`, oldest first (texts, host-local). On the
    reviewer's own PR (gcs-71c) our recorded posts and our reviews are left out, the rest is the author's."""
    author, out = login(pr), []
    for kind, when in (('comments', 'createdAt'), ('reviews', 'submittedAt')):
        for c in (pr.get(kind) or {}).get('nodes') or []:
            t = c.get(when) or ''
            if login(c) == author and (not since or t > since) and (c.get('body') or '').strip() and (
                    author != (us or '').lower() or (c.get('databaseId') not in our_posts and not pr_format.is_ours(c['body']))):
                out.append((t, c['body'][:MAX_TEXT]))
    return [b for _, b in sorted(out)][-MAX_COMMENTS:]


def prompt(url, head, author, asks, threads, others):
    lines = []
    for a in asks:
        it = a['it']
        if a['kind'] == 'item':
            span = f"lines {it['line_start']}-{it['line_end']}" if it['line_start'] != it['line_end'] else f"line {it['line_start']}"
            lines.append(f"- {a['id']} ({it.get('kind', 'change')}) at {it['path']} {span}: {it.get('change', '')}: {it.get('ask', '')}")
        else:
            opts = '; '.join(f"{pr_format.LETTERS[j]}. {o}" for j, o in enumerate(it.get('options') or []))
            lines.append(f"- {a['id']} (question with options): {it.get('question', '')} Options: {opts}")
        th = threads.get(a['id'])
        for who, text in (th or {}).get('replies') or []:
            lines.append(f"    reply ({'our follow-up' if who == 'we' else who}): {' '.join(text.split())}")
        if th and th.get('resolved'):
            lines.append('    (the thread is resolved)')
    return PROMPT.format(url=url, head=head, author=author, asks='\n'.join(lines) or '(none)',
                         comments='\n'.join(f"- {' '.join(c.split())}" for c in others) or '(none)')


def words_ok(text, most, voice='disclose'):
    """A follow-up's own text passes: plain, short, no codes, filler or links; in the reviewer's voice no
    disclosure phrase (R38)."""
    t = (text or '').strip()
    return bool(t) and len(t.split()) <= most and not pr_format.field_reasons('it', 'text', t) and \
        not pr_format.IDENT_RE.search(t) and 'http' not in t and \
        not (voice == 'reviewer' and pr_format.DISCLOSURE_RE.search(t))


def apply(result, calls, asks, voice='disclose'):
    """The delivered review's parts after the author's answers: (items, cleared, decisions, counts, replies)
    where replies = [(ask id, follow-up text)] for the open asks whose answer is still missing something.
    An ask without a usable call stays open."""
    by = {c.get('id'): c for c in calls or [] if c.get('status') in STATUSES}
    items, cleared, decisions, replies = [], list(result.get('cleared') or []), [], []
    counts = {'answered': 0, 'open': 0, 'change': 0}
    mine = pr_format.split(result.get('decisions') or [])[0]
    for a in asks:
        c, it = by.get(a['id']) or {'status': 'open'}, a['it']
        st = c['status']
        if st == 'answered' and not words_ok(c.get('why'), MAX_WHY):
            st = 'open'
        if st == 'change' and not words_ok(c.get('ask'), MAX_ASK):
            st = 'open'
        counts[st] += 1
        if a['kind'] == 'decision':
            if st != 'answered':
                decisions.append(it)
            continue
        if st == 'answered':
            cleared.append({'path': it['path'], 'side': it.get('side', 'new'), 'line_start': it['line_start'],
                            'line_end': it['line_end'], 'change': it.get('change', ''), 'why': c['why'].strip(),
                            'answered': True, 'rule': it.get('rule') or '', 'money': bool(it.get('money'))})
        elif st == 'change':
            items.append(dict(it, kind='change', severity='medium' if it.get('severity') == 'low' else it.get('severity', 'medium'),
                              ask=c['ask'].strip()))
        else:
            items.append(it)
            if words_ok(c.get('follow_up'), MAX_FOLLOW, voice):
                replies.append((a['id'], c['follow_up'].strip()))
    return items, cleared, mine + decisions, counts, replies


def signed_off(result, decisions, cleared=()):
    """The not-checked area was answered for (R20): its sign-off question is gone, or a fallback's questions
    on its money places are all answered."""
    nc = pr_format.NOT_CHECKED_AREA
    if any(d.get('area') == nc for d in result.get('decisions') or []):
        return not any(d.get('area') == nc for d in decisions)
    money = [it for it in result.get('items') or [] if it.get('money')]
    return bool(money) and sum(1 for c in cleared if c.get('money') and c.get('answered')) == len(money)


def cap_line(author, rounds, voice='disclose', name='the reviewer'):
    """The one line when the rounds on a head are used up (decision-57b6d9bb)."""
    if voice == 'reviewer':
        return f"{author}: I've followed up {rounds} times on this commit, so I'll leave it here; your next push gets a new review."
    return (f"{author}: the automated follow-ups stop after {rounds} rounds on this commit; the next push gets a "
            f"new review. {name} hasn't read this PR.")


def selftest():
    us, author = 'rev', 'auth'
    result = {'items': [
        {'path': 'src/a.ts', 'side': 'new', 'line_start': 3, 'line_end': 4, 'kind': 'question', 'severity': 'low',
         'area': 'other', 'change': 'Reminder sent twice', 'ask': 'Does a retry send the reminder again?', 'why': 'w', 'rule': ''},
        {'path': 'src/b.ts', 'side': 'new', 'line_start': 9, 'line_end': 9, 'kind': 'question', 'severity': 'low',
         'area': 'other', 'change': 'Fee rounding changed', 'ask': 'Should fees round up now?', 'why': 'w', 'rule': ''},
        {'path': 'src/c.ts', 'side': 'new', 'line_start': 1, 'line_end': 2, 'kind': 'question', 'severity': 'low',
         'area': 'other', 'change': 'Clinic name in texts', 'ask': 'May the clinic name appear in texts?', 'why': 'w', 'rule': ''}],
        'decisions': [{'question': 'Who signs off billing?', 'options': ['You', 'The owner'], 'actors': ['author', 'picked'],
                       'area': pr_format.NOT_CHECKED_AREA}], 'cleared': []}
    asks = asks_of(result)

    def node(cid, who, body, path=None, line=None, rid=None, at='2026-10-30T23:00:00Z'):
        return {'databaseId': cid, 'author': {'login': who}, 'body': body, 'createdAt': at, 'path': path,
                'originalLine': line, 'pullRequestReview': {'databaseId': rid}}
    pr = {'author': {'login': author}, 'reviewThreads': {'nodes': [
        {'id': 'T1', 'isResolved': False, 'comments': {'nodes': [node(1, us, 'ours', 'src/a.ts', 4, 77),
                                                                   node(5, author, 'The retry is idempotent by visit id.')]}},
        {'id': 'T2', 'isResolved': True, 'comments': {'nodes': [node(2, us, 'ours', 'src/b.ts', 9, 77)]}},
        {'id': 'T9', 'isResolved': False, 'comments': {'nodes': [node(3, 'other', 'theirs', 'src/c.ts', 2, 5)]}}]},
        'comments': {'nodes': [node(6, author, 'Billing owner signed off in the ticket.'), node(4, us, 'our own')]},
        'reviews': {'nodes': []}}
    th = threads_of(pr, us, asks, [77])
    others = others_of(pr, us, '2026-10-30T22:00:00Z')
    p = prompt('https://github.com/o/r/pull/1', 'a' * 40, author, asks, th, others)
    calls = [{'id': 'E1', 'status': 'answered', 'why': 'A retry reuses the visit id, so the reminder goes out once.'},
             {'id': 'E2', 'status': 'open', 'why': 'No answer yet.', 'follow_up': ''},
             {'id': 'E3', 'status': 'change', 'ask': 'Leave the clinic name out of the text message.'},
             {'id': 'D1', 'status': 'answered', 'why': 'The billing owner signed off in the linked ticket.'}]
    items, cleared, decisions, counts, replies = apply(result, calls, asks, 'reviewer')
    checks = {
        'the asks: items E1-E3, then the author question D1': [a['id'] for a in asks] == ['E1', 'E2', 'E3', 'D1'],
        'each item finds the thread our review opened on its last line, with the replies': th.get('E1', {}).get('comment') == 1
        and th['E1']['replies'] == [('author', 'The retry is idempotent by visit id.')] and th.get('E2', {}).get('resolved')
        and 'E3' not in th,
        "the author's other comments, never ours": others == ['Billing owner signed off in the ticket.'],
        'the prompt holds the asks with their replies, nothing else of ours': 'reply (author): The retry is idempotent' in p
        and 'our own' not in p and 'E3 (question)' in p,
        'answered is cleared with the answer as the reason': cleared and cleared[0]['answered'] and
        cleared[0]['why'].startswith('A retry reuses') and cleared[0]['path'] == 'src/a.ts',
        'open stays an item; no follow-up text when there is none': [i['path'] for i in items] == ['src/b.ts', 'src/c.ts']
        and replies == [],
        'change becomes a change item': items[1]['kind'] == 'change' and items[1]['ask'].startswith('Leave the clinic'),
        'the answered billing question is gone and counts as signed off': decisions == [] and signed_off(result, decisions, cleared),
        'counts': counts == {'answered': 2, 'open': 1, 'change': 1},
    }
    _, _, _, c2, r2 = apply(result, [{'id': 'E1', 'status': 'answered', 'why': 'ok'},
                                     {'id': 'E2', 'status': 'open', 'why': 'x', 'follow_up': 'Does the waiver apply to old invoices too?'},
                                     {'id': 'E3', 'status': 'open', 'why': 'x', 'follow_up': 'Thanks, this automated review will check later.'}],
                            asks, 'reviewer')
    checks['a reason under 4 words stays open; a follow-up with a disclosure phrase is dropped in the voice'] = \
        c2['answered'] == 0 and r2 == [('E2', 'Does the waiver apply to old invoices too?')]
    checks['the cap line names the author and, in the voice, no disclosure'] = \
        cap_line('Author', 3, 'reviewer').startswith('Author: ') and not pr_format.DISCLOSURE_RE.search(cap_line('Author', 3, 'reviewer'))
    for k, v in checks.items():
        print('ok  ' if v else 'FAIL', k)
    return all(checks.values())


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
