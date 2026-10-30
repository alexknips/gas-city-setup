#!/usr/bin/env python3
"""Incremental reviews (gc-sy1hm.2, parity feature c; PR_REVIEW_INCREMENTAL=on, default off).

When a PR we reviewed gets a review of a new head (the sweep re-queues one when the reviewer is asked
again, a serious rule fires, or our formal block rested on content), the review of the new head stays a
whole review (R18: one per head), and three things are added:

1. The prompt lists our earlier asks for the author (from the delivered review, kept host-local) with
   where their lines sit at the new head and whether the new commits changed them, plus the new commits'
   diff (old head..new head). The model checks each earlier ask first ('fixed', 'open', 'gone') and
   reviews the new commits first.
2. After the new review is posted, our earlier threads are resolved where that is safe: an ask the model
   calls fixed or gone, but only when the new commits changed its lines (zero-token check), and an ask
   the new review raises again on overlapping lines (its new thread replaces the old one). Every other
   thread stays open.
3. The body says what happened since the last review, in one line with a link to the new commits.

A force-push that rewrote history (the old head is not an ancestor of the new one) or an old head GitHub
no longer serves gives no incremental part: the review is the whole review it always was.

  pr_incremental.py --selftest    build a small repository in a scratch dir and check the parts
"""
import json
import os
import re
import subprocess
import sys
import tempfile

MAX_SINCE_CHARS = 60_000   # the new commits' diff inline; longer goes to a file the model reads
MAX_EARLIER = 10
STATUSES = ['fixed', 'open', 'gone']


def git(mirror, *args, check=False):
    r = subprocess.run(['git', '-C', mirror, *args], capture_output=True, text=True, timeout=600)
    if check and r.returncode:
        raise RuntimeError(f'git {args[0]} failed (rc {r.returncode})')
    return r


def earlier(cfg, entry, head, private_dir):
    """Our last delivered review of an earlier head of this PR, from the host-local copy kept at delivery:
    {head, review_id, items} or None (no earlier review, the same head, or no copy)."""
    heads = [h for h in entry.get('review_heads') or [] if h != head]
    if not heads or not entry.get('review_id') or head in (entry.get('review_heads') or []):
        return None
    old = heads[-1]
    p = os.path.join(private_dir, f"{entry['repo'].replace('/', '__')}-{entry['number']}-{old[:10]}.json")
    try:
        d = json.load(open(p))
    except (OSError, ValueError):
        return None
    items = [it for it in ((d.get('result') or {}).get('items') or []) if it.get('path')][:MAX_EARLIER]
    return {'head': old, 'review_id': entry['review_id'], 'items': items} if items else None


def since(mirror, old, new):
    """The new commits' diff (old head..new head) and its zero-context form, or (None, None, reason)."""
    if git(mirror, 'cat-file', '-e', f'{old}^{{commit}}').returncode:
        git(mirror, 'fetch', '--quiet', 'origin', old)  # GitHub serves a pushed-over commit for a while
        if git(mirror, 'cat-file', '-e', f'{old}^{{commit}}').returncode:
            return None, None, 'old-head-gone'
    if git(mirror, 'merge-base', '--is-ancestor', old, new).returncode:
        return None, None, 'rewritten'
    diff = git(mirror, 'diff', '--no-ext-diff', '--no-textconv', old, new, check=True).stdout
    u0 = git(mirror, 'diff', '-U0', '--no-ext-diff', '--no-textconv', old, new, check=True).stdout
    return diff, u0, 'ok'


def hunks_u0(u0):
    """{old path: {'hunks': [(a, b, c, d)], 'deleted': bool, 'new_path': path}} from a -U0 diff."""
    out, cur = {}, None
    for line in u0.splitlines():
        if line.startswith('--- '):
            cur = line[6:] if line.startswith('--- a/') else None
            if cur is not None:
                out.setdefault(cur, {'hunks': [], 'deleted': False, 'new_path': cur})
        elif line.startswith('+++ ') and cur is not None:
            if line.startswith('+++ /dev/null'):
                out[cur]['deleted'] = True
            elif line.startswith('+++ b/'):
                out[cur]['new_path'] = line[6:]
        else:
            m = re.match(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', line)
            if m and cur is not None:
                out[cur]['hunks'].append((int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1),
                                          int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)))
    return out


def where_now(changes, path, start, end):
    """An earlier ask's new-side lines (numbered at the old head) at the new head: (path, start, end,
    touched, gone). touched: the new commits changed a line of the range or inserted inside it."""
    f = changes.get(path)
    if f is None:
        return path, start, end, False, False
    if f['deleted']:
        return path, start, end, True, True
    touched, off_start, off_end = False, 0, 0
    for a, b, c, d in f['hunks']:
        lo, hi = (a, a + b - 1) if b else (a + 1, a)  # b == 0: an insertion after line a
        if b and lo <= end and start <= hi:
            touched = True
        if not b and start <= a < end:
            touched = True
        shift = (c + d) - (a + b) if b else (c + d) - (a + 1)
        if (a + b - 1 if b else a) < start:
            off_start = shift
        if (a + b - 1 if b else a) < end:
            off_end = shift
    s, e = start + off_start, max(start + off_start, end + off_end)
    return f['new_path'], s, e, touched, False


def rows(prev, u0):
    """One row per earlier ask: id E1..En, the ask, and where it is now."""
    changes = hunks_u0(u0)
    out = []
    for k, it in enumerate(prev['items'], 1):
        if it.get('side', 'new') != 'new':
            out.append({'id': f'E{k}', 'item': it, 'path': it['path'], 'start': None, 'end': None,
                        'touched': False, 'gone': False})
            continue
        p, s, e, touched, gone = where_now(changes, it['path'], it['line_start'], it['line_end'])
        out.append({'id': f'E{k}', 'item': it, 'path': p, 'start': s, 'end': e, 'touched': touched, 'gone': gone})
    return out


def prompt_block(prev, new_head, rs, diff, ctx):
    """SINCE OUR LAST REVIEW, for the prompt."""
    lines = []
    for r in rs:
        it = r['item']
        if r['start'] is None:
            at = f"removed lines of {it['path']} (not mapped to the new head)"
        elif r['gone']:
            at = f"{it['path']}, a file the new commits deleted"
        else:
            span = f"line {r['start']}" if r['start'] == r['end'] else f"lines {r['start']}-{r['end']}"
            at = (f"{r['path']} {span} at the new head; the new commits "
                  + ('changed these lines' if r['touched'] else 'did not change these lines'))
        lines.append(f"- {r['id']} ({it.get('kind', 'change')}): {it.get('change', '')}: {it.get('ask', '')} At {at}.")
    if len(diff) > MAX_SINCE_CHARS:
        with open(os.path.join(ctx, 'since.diff'), 'w') as f:
            f.write(diff)
        dblock = f'(The diff is {len(diff)} characters: read it from {ctx}/since.diff.)'
    else:
        dblock = diff.replace('```', "'''") or '(no change to the files)'
    return (f"SINCE OUR LAST REVIEW (it reviewed head {prev['head']}; the new commits are "
            f"{prev['head'][:7]}..{new_head[:7]}). Its asks for the author, to check first (step 0):\n"
            + '\n'.join(lines) + f"\n\nThe new commits' diff:\n{dblock}")


STEP = """0. Check each earlier ask (E1, E2, ...) in SINCE OUR LAST REVIEW against the new head, and give it
   one `earlier` entry: "fixed" (the new head does what it asked, or answers its question in the code or
   the description), "open" (it still applies: then it is an item again, on its lines at the new head)
   or "gone" (the code it was about is no longer in the PR), with `why`, one fact of at most 20 words.
   Then review the whole PR, the new commits first. Never raise again an ask the new head fixed.
"""

SCHEMA = {'type': 'array', 'maxItems': MAX_EARLIER, 'description': 'One entry per earlier ask E1, E2, ...',
          'items': {'type': 'object', 'additionalProperties': False, 'required': ['id', 'status', 'why'],
                    'properties': {'id': {'type': 'string', 'description': 'E1, E2, ...'},
                                   'status': {'type': 'string', 'enum': STATUSES},
                                   'why': {'type': 'string', 'maxLength': 160}}}}


def overlaps(r, it):
    return (it.get('side', 'new') == 'new' and r['start'] is not None and it.get('path') == r['path']
            and it['line_start'] <= r['end'] and r['start'] <= it['line_end'])


def outcome(rs, answers, items, prev, new_head):
    """What happened to each earlier ask, and which threads to resolve: {old, new, review_id, fixed,
    again, open, gone, resolve: [(path, line_end at the old head, reason)]}. A thread is resolved only
    when the zero-token check backs the model: fixed or gone with its lines changed by the new commits,
    or raised again by the new review on overlapping lines. A missing or unknown answer is 'open'."""
    said = {a.get('id'): a.get('status') for a in answers or [] if a.get('status') in STATUSES}
    out = {'old': prev['head'], 'new': new_head, 'review_id': prev['review_id'],
           'fixed': 0, 'again': 0, 'open': 0, 'gone': 0, 'resolve': []}
    for r in rs:
        it, st = r['item'], said.get(r['id'], 'open')
        key = (it['path'], it['line_end'])
        if any(overlaps(r, x) for x in items):
            out['again'] += 1
            out['resolve'].append((*key, 'asked-again'))
        elif st in ('fixed', 'gone') and r['touched']:
            out['gone' if st == 'gone' or r['gone'] else 'fixed'] += 1
            out['resolve'].append((*key, st))
        else:
            out['open'] += 1
    return out


def text(repo, s):
    """The body's line on what happened since the last review ('' when there were no earlier asks)."""
    parts = []
    plural = lambda n, one, many: one if n == 1 else many  # noqa: E731
    if s['fixed']:
        parts.append(f"{s['fixed']} earlier {plural(s['fixed'], 'ask', 'asks')} fixed")
    if s['gone']:
        parts.append(f"{s['gone']} about code no longer in the PR")
    if s['again']:
        parts.append(f"{s['again']} asked again below")
    if s['open']:
        parts.append(f"{s['open']} still open in {plural(s['open'], 'its thread', 'their threads')}")
    if not parts:
        return ''
    url = f"https://github.com/{repo}/compare/{s['old']}...{s['new']}"
    return f"**Since the last review** ([new commits]({url})): {', '.join(parts)}."


THREADS_Q = """query($owner: String!, $name: String!, $number: Int!) { repository(owner: $owner, name: $name) {
  pullRequest(number: $number) { reviewThreads(first: 100) { nodes { id isResolved
    comments(first: 1) { nodes { path originalLine pullRequestReview { databaseId } } } } } } } }"""
RESOLVE_M = 'mutation($id: ID!) { resolveReviewThread(input: {threadId: $id}) { thread { isResolved } } }'


def pick_threads(nodes, review_id, resolve):
    """The unresolved threads our earlier review opened on the places to resolve: [thread id]."""
    want = {(p, ln) for p, ln, _ in resolve}
    out = []
    for t in nodes or []:
        first = ((t.get('comments') or {}).get('nodes') or [{}])[0]
        if t.get('isResolved') or ((first.get('pullRequestReview') or {}).get('databaseId')) != review_id:
            continue
        if (first.get('path'), first.get('originalLine')) in want:
            out.append(t['id'])
    return out


def resolve_threads(repo, number, s):
    """Resolve the threads `s` names (after the new review is posted). Returns (resolved, failed)."""
    if not s or not s.get('resolve'):
        return 0, 0
    owner, name = repo.split('/')
    r = subprocess.run(['gh', 'api', 'graphql', '-f', f'query={THREADS_Q}', '-f', f'owner={owner}', '-f', f'name={name}',
                        '-F', f'number={number}'], capture_output=True, text=True, timeout=60)
    try:
        nodes = json.loads(r.stdout)['data']['repository']['pullRequest']['reviewThreads']['nodes']
    except (ValueError, KeyError, TypeError):
        return 0, len(s['resolve'])
    ok = bad = 0
    for tid in pick_threads(nodes, s['review_id'], s['resolve']):
        m = subprocess.run(['gh', 'api', 'graphql', '-f', f'query={RESOLVE_M}', '-F', f'id={tid}'],
                           capture_output=True, text=True, timeout=60)
        if m.returncode == 0 and '"isResolved":true' in m.stdout.replace(' ', ''):
            ok += 1
        else:
            bad += 1
    return ok, bad


def selftest():
    d = tempfile.mkdtemp(prefix='pr-incremental-selftest-')
    run = lambda *a: subprocess.run(['git', '-C', d, *a], capture_output=True, text=True, check=True)  # noqa: E731
    run('init', '-q')
    run('config', 'user.email', 'test@example.com')
    run('config', 'user.name', 'test')
    base = [f'line {n}' for n in range(1, 41)]
    with open(os.path.join(d, 'fees.ts'), 'w') as f:
        f.write('\n'.join(base) + '\n')
    with open(os.path.join(d, 'gone.ts'), 'w') as f:
        f.write('a\nb\nc\n')
    run('add', '.')
    run('commit', '-qm', 'old head')
    old = run('rev-parse', 'HEAD').stdout.strip()
    new_lines = base[:4] + ['inserted 1', 'inserted 2'] + base[4:9] + ['line 10 fixed'] + base[10:]
    with open(os.path.join(d, 'fees.ts'), 'w') as f:  # two lines in above E1, E1's line 10 rewritten
        f.write('\n'.join(new_lines) + '\n')
    run('rm', '-q', 'gone.ts')
    run('commit', '-qam', 'new head')
    new = run('rev-parse', 'HEAD').stdout.strip()
    prev = {'head': old, 'review_id': 77, 'items': [
        {'path': 'fees.ts', 'side': 'new', 'line_start': 10, 'line_end': 10, 'kind': 'change', 'change': 'Fee rounds down', 'ask': 'Round up.'},
        {'path': 'fees.ts', 'side': 'new', 'line_start': 20, 'line_end': 22, 'kind': 'question', 'change': 'Waiver skips audit', 'ask': 'Is that intended?'},
        {'path': 'fees.ts', 'side': 'new', 'line_start': 30, 'line_end': 30, 'kind': 'change', 'change': 'Late fee doubles', 'ask': 'Cap it.'},
        {'path': 'gone.ts', 'side': 'new', 'line_start': 2, 'line_end': 2, 'kind': 'change', 'change': 'Old rate', 'ask': 'Drop it.'},
        {'path': 'fees.ts', 'side': 'new', 'line_start': 35, 'line_end': 35, 'kind': 'change', 'change': 'Typo', 'ask': 'Fix it.'}]}
    diff, u0, why = since(d, old, new)
    rs = rows(prev, u0)
    got_rows = [(r['id'], r['start'], r['end'], r['touched'], r['gone']) for r in rs]
    want_rows = [('E1', 12, 12, True, False), ('E2', 22, 24, False, False), ('E3', 32, 32, False, False),
                 ('E4', 2, 2, True, True), ('E5', 37, 37, False, False)]
    answers = [{'id': 'E1', 'status': 'fixed', 'why': 'rounds up now'}, {'id': 'E2', 'status': 'fixed', 'why': 'x'},
               {'id': 'E3', 'status': 'open', 'why': 'still doubles'}, {'id': 'E4', 'status': 'gone', 'why': 'file deleted'}]
    new_items = [{'path': 'fees.ts', 'side': 'new', 'line_start': 32, 'line_end': 32}]  # E3 raised again
    s = outcome(rs, answers, new_items, prev, new)
    want_s = {'fixed': 1, 'again': 1, 'open': 2, 'gone': 1}  # E2 'fixed' but untouched stays open; E5 unanswered
    resolve = sorted(s['resolve'])
    want_resolve = [('fees.ts', 10, 'fixed'), ('fees.ts', 30, 'asked-again'), ('gone.ts', 2, 'gone')]
    nodes = [{'id': 'T1', 'isResolved': False, 'comments': {'nodes': [{'path': 'fees.ts', 'originalLine': 10, 'pullRequestReview': {'databaseId': 77}}]}},
             {'id': 'T2', 'isResolved': False, 'comments': {'nodes': [{'path': 'fees.ts', 'originalLine': 10, 'pullRequestReview': {'databaseId': 5}}]}},
             {'id': 'T3', 'isResolved': True, 'comments': {'nodes': [{'path': 'fees.ts', 'originalLine': 30, 'pullRequestReview': {'databaseId': 77}}]}},
             {'id': 'T4', 'isResolved': False, 'comments': {'nodes': [{'path': 'fees.ts', 'originalLine': 20, 'pullRequestReview': {'databaseId': 77}}]}},
             {'id': 'T5', 'isResolved': False, 'comments': {'nodes': [{'path': 'gone.ts', 'originalLine': 2, 'pullRequestReview': {'databaseId': 77}}]}}]
    threads = pick_threads(nodes, 77, s['resolve'])
    line = text('example/repo', s)
    block = prompt_block(prev, new, rs, diff, d)
    rewritten = since(d, new, old)[2]
    checks = {'since': why == 'ok', 'rows': got_rows == want_rows,
              'counts': {k: s[k] for k in want_s} == want_s, 'resolve': resolve == want_resolve,
              'threads': threads == ['T1', 'T5'],
              'text': line == (f'**Since the last review** ([new commits](https://github.com/example/repo/compare/{old}...{new})): '
                               '1 earlier ask fixed, 1 about code no longer in the PR, 1 asked again below, '
                               '2 still open in their threads.'),
              'block': 'E2 (question): Waiver skips audit: Is that intended? At fees.ts lines 22-24 at the new head; '
                       'the new commits did not change these lines.' in block,
              'rewritten': rewritten == 'rewritten'}
    ok = all(checks.values())
    print('selftest: ok' if ok else 'selftest: FAILED ' + ', '.join(k for k, v in checks.items() if not v))
    if not ok or os.environ.get('PR_INCREMENTAL_SHOW'):
        print(got_rows, s, threads, line, block[:600], sep='\n')
    return 0 if ok else 1


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        sys.exit(selftest())
    print(__doc__)
