#!/usr/bin/env python3
"""Restamp our delivered reviews into the reviewer's voice (decision-ed7320b7), with zero tokens: each
review body of ours, and our inline comments, rewritten from the text GitHub holds. Line 1 becomes the
verdict word and the invisible marker, line 2 what the author must do; the disclosure lines go, "not
checked by any agent" becomes scope, a section for the reviewer becomes "Open questions". The findings,
links and options stay as they were. A review whose text would still hold a disclosure phrase (R38) is
left alone and counted.

  pr_restamp.py                       dry run over the PRs the reviewer reviewed on the configured repositories
                                      (GitHub's search, and the state's PRs)
  pr_restamp.py --apply               rewrite the reviews on open PRs
  pr_restamp.py --apply --merged      ... and on merged and closed PRs (GitHub keeps each review's edit history)

Prints counts only. A dismissed review keeps its dismissal message (GitHub has no edit for it).
  pr_restamp.py --ours ID,ID ...      also treat these review ids as ours (bodies whose marker was removed by hand)

Either way it counts the reviewer's reviews that have a body but no marker (`unmarked_with_body`, with their
ids): a lost marker, or the reviewer's own words. They are never touched unless named with --ours.
  pr_restamp.py --selftest            the shapes and the PR list on synthetic data
"""
import argparse
import collections
import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_format  # noqa: E402
import pr_state  # noqa: E402

VERDICT_RE = re.compile(r'^\*\*(Approved as low risk|Approved|Needs you|Changes requested|Changes suggested|Questions|'
                        r'Low risk|Not reviewed automatically|Replaced)\.?\*\*')
# v4 (until 10-30 ~01:40Z): the verdict and who acts in one bold run, under the honesty line.
V4_RE = re.compile(r'^\*\*(Approved as low risk|Needs you|Changes requested|Changes suggested|Questions|Low risk)\. (.+)\*\*$')
# v3 (10-29 evening): "**Low risk: nothing to check**", "**Changes requested: 2**", "**Needs you: 1 decision**",
# "**2 questions for <author>**", "**1 question for <author>:** <the question>".
V3_RE = re.compile(r'^\*\*(?:(Needs you|Changes requested|Low risk): [^*]*|(\d+) questions? for [^*]*?:?)\*\*(.*)$')
WORD = {'Approved as low risk': 'Approved', 'Needs you': 'Questions', 'Not reviewed automatically': 'Questions'}
BY_STATE = {'APPROVED': 'Approved', 'CHANGES_REQUESTED': 'Changes requested'}  # a body with no verdict line
DROP_RE = re.compile(r"(?i)^(\*\*)?automated (review|approval)\b|^shadow mode\b|^\s*our review of this pr didn't reach")


def voice_body(body, reviewer, author, state=None):
    """See voice_body_unchecked; None when a disclosure phrase remains (R38)."""
    text = voice_body_unchecked(body, reviewer, author, state)
    return None if text is None or pr_format.DISCLOSURE_RE.search(text) else text


def voice_body_unchecked(body, reviewer, author, state=None):
    """The body in the reviewer's voice (or None when no verdict is found). Works on every shape we
    delivered: v3 (a disclosure header, then the verdict line), v4 (the honesty line, then the verdict and
    who acts in one bold run), v5-v5.5 (the verdict with the automated note on line 1), the notes and the
    pointers; a body with no verdict line takes it from the review's state (`state`)."""
    lines = (body or '').replace('\r\n', '\n').split('\n')
    k = next((j for j, line in enumerate(lines) if VERDICT_RE.match(line) or V4_RE.match(line) or V3_RE.match(line)), None)
    who = None
    if k is not None and V4_RE.match(lines[k]):
        word, who = V4_RE.match(lines[k]).groups()
    elif k is not None and V3_RE.match(lines[k]):
        m = V3_RE.match(lines[k])
        word, who = (m.group(1) or 'Questions'), (m.group(3).strip() or None)
    elif k is not None:
        word = VERDICT_RE.match(lines[k]).group(1)
    elif any(line.startswith('Replaced by [') for line in lines):
        word = 'Replaced'
    elif state in BY_STATE:
        word = BY_STATE[state]
    else:
        return None
    out = [f'**{WORD.get(word, word)}** {pr_format.MARK}']
    rest = [line for j, line in enumerate(lines) if j != k and not DROP_RE.search(line)]
    while rest and not rest[0].strip():  # a v3 or v4 header leaves blank lines above the content
        rest.pop(0)
    if who:
        rest = [who] + rest
    if rest and (m := pr_format.WHO_RE.match(rest[0])):  # line 2: what the author must do
        part = re.sub(rf'\bif {re.escape(m.group(1))} picks [A-D], ', '', m.group(7))
        rest[0] = pr_format.NOTHING_NEEDED.format(author=author) if part == 'nothing' else f'{m.group(6)}: {part}.'
    elif rest and pr_format.NOTHING_RE.match(rest[0]):
        rest[0] = pr_format.NOTHING_NEEDED.format(author=author)
    text = '\n'.join(out + rest)
    text = text.replace('These changes are not checked by any agent.', 'These changes are outside this review.')
    text = text.replace(f'**For {reviewer}:**', '**Open questions:**')
    return re.sub(rf' · Ask {re.escape(author)} · ', ' · Question · ', text)


CLEARED_RE = re.compile(r'^- \*\*.+?\*\* · \[[^\]]*\]\([^)]*\): (.+?)(?: · rule: .*)?$')


def current(body, author):
    """A body in the voice brought to v5.8 (the mayor, 2026-10-30 07:21Z): line 2 names the author when nothing
    is needed; an approval with one cleared entry shows its reason above "What was checked". Returns (text,
    the kinds of change made)."""
    lines, kinds = body.replace('\r\n', '\n').split('\n'), []
    if len(lines) > 1 and lines[1] == 'Nothing needed.':
        lines[1] = pr_format.NOTHING_NEEDED.format(author=author)
        kinds.append('v58_line2_names_author')
    rest = [line for line in lines[2:] if line.strip()]
    if lines[0].startswith(f'**{pr_format.VOICE_APPROVED}**') and len(rest) == 3 and \
            rest[0] == '<details><summary>What was checked</summary>' and rest[2] == '</details>':
        m = CLEARED_RE.match(rest[1])
        if m:
            lines = lines[:2] + ['', pr_format.approval_reason({'cleared': [{'why': m.group(1)}]})] + lines[2:]
            kinds.append('v58_approval_reason_line')
    return '\n'.join(lines), kinds


def voice_comment(body, author):
    """An inline comment of ours in the reviewer's voice, or None when a disclosure phrase would remain."""
    text = (body or '').replace(f'**Ask {author}:**', '**Question:**')
    return None if pr_format.DISCLOSURE_RE.search(text) else text


def body_sha(body):
    """As pr_review.body_sha (the state's hash of the delivered body)."""
    return hashlib.sha256(body.replace('\r\n', '\n').strip().encode()).hexdigest()


def gh_lines(path, jq):
    r = subprocess.run(['gh', 'api', '--paginate', path, '--jq', jq], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f'gh api failed (rc {r.returncode})')
    return [json.loads(x) for x in r.stdout.splitlines() if x.strip()]


def gh_write(method, path, body):
    r = subprocess.run(['gh', 'api', '-X', method, path, '--input', '-'], input=json.dumps({'body': body}),
                       capture_output=True, text=True, timeout=120)
    return r.returncode == 0


def targets(entries, searched):
    """[(repo, number, state entry or {})]: the state's PRs, and the PRs GitHub finds reviewed by the
    reviewer (a review can predate the state, or its entry be gone: the mayor found 3 of 21, 2026-10-30)."""
    out = {(e['repo'], e['number']): e for e in entries.values()}
    for repo, num in searched:
        out.setdefault((repo, num), {})
    return [(repo, num, e) for (repo, num), e in sorted(out.items())]


def searched_prs(cfg):
    """(repo, number) of every PR on the configured repositories that the reviewer reviewed."""
    out = []
    for repo in cfg['repos']:
        r = subprocess.run(['gh', 'search', 'prs', '--repo', repo, '--reviewed-by', cfg['reviewer'], '--limit', '1000',
                            '--json', 'number', '--jq', '.[].number'], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f'gh search failed (rc {r.returncode})')
        out += [(repo, int(x)) for x in r.stdout.split() if x.strip().isdigit()]
    return out


def restamp(cfg, apply, merged, ours=()):
    reviewer = cfg['reviewer'].lower()
    name = os.environ.get('PR_REVIEW_REVIEWER_NAME') or cfg['reviewer'] or 'the reviewer'
    names = dict(x.split('=', 1) for x in os.environ.get('PR_REVIEW_NAMES', '').split() if '=' in x)
    with pr_state.locked(cfg['state']) as st:
        entries = {k: dict(e) for k, e in st['prs'].items() if e.get('repo') in cfg['repos'] and e.get('review_id')}
    found = searched_prs(cfg)
    todo = targets(entries, found)
    n = collections.Counter(prs=len(todo), prs_in_state=len(entries), prs_not_in_state=len(todo) - len(entries))
    new_sha, lost_ids = {}, []
    for repo, num, e in todo:
        key = f'{repo}#{num}'
        try:
            pr = gh_lines(f'repos/{repo}/pulls/{num}', '{state, merged, author: .user.login}')[0]
            reviews = gh_lines(f'repos/{repo}/pulls/{num}/reviews?per_page=100',
                               f'.[] | select((.user.login | ascii_downcase) == "{reviewer}") | '
                               '{id, state, body, html_url}')
        except (RuntimeError, IndexError, ValueError):
            n['gh_failed'] += 1
            continue
        if pr['state'] != 'open' and not merged:
            n['prs_skipped_not_open'] += 1
            continue
        author = names.get(pr['author'], pr['author'])
        lost = unmarked(reviews, ours)
        n['unmarked_with_body'] += len(lost)
        lost_ids += lost
        mine = [rv for rv in reviews if pr_format.is_ours(rv['body']) or int(rv['id']) in ours]
        retarget = dict(pr_format.repoints(mine, name, 'reviewer'))  # pointers follow the newest review
        for rv in mine:
            n['reviews_ours'] += 1
            first = (rv['body'] or '').split('\n', 1)[0]
            voiced = pr_format.MARK_PREFIX in first
            n['already_in_voice'] += voiced
            body = rv['body'] if voiced else voice_body(rv['body'], name, author, rv['state'])
            if body is not None:
                body, kinds = (retarget[rv['id']], ['repointed']) if rv['id'] in retarget else current(body, author)
                for kind in kinds:
                    n[kind] += 1
                if body == rv['body']:
                    n['unchanged'] += 1
                    continue
            if body is None:
                n['left_disclosure_remains'] += 1
                if not apply:  # which of our own template phrases stays (counts, never the review's text)
                    probe = voice_body_unchecked(rv['body'], name, author, rv['state'])
                    for hit in {m.group(0).lower() for m in pr_format.DISCLOSURE_RE.finditer(probe or '')}:
                        n[f'left_phrase:{hit}'] += 1
                    n['left_no_verdict'] += probe is None
                continue
            n['dismissal_message_kept'] += rv['state'] == 'DISMISSED'
            comments = [] if voiced else gh_lines(f'repos/{repo}/pulls/{num}/comments?per_page=100',
                                                  f'.[] | select(.pull_request_review_id == {int(rv["id"])}) | {{id, body}}')
            new_c = [(c['id'], voice_comment(c['body'], author)) for c in comments]
            n['comments_left_disclosure_remains'] += sum(1 for _, b in new_c if b is None)
            if not apply:
                n['reviews_would_change'] += 1
                n['comments_would_change'] += sum(1 for (cid, b), c in zip(new_c, comments) if b and b != c['body'])
                continue
            if gh_write('PUT', f'repos/{repo}/pulls/{num}/reviews/{rv["id"]}', body):
                n['reviews_changed'] += 1
                if rv['id'] == e.get('review_id'):
                    new_sha[key] = body_sha(body)
            else:
                n['reviews_put_failed'] += 1
                continue
            for (cid, b), c in zip(new_c, comments):
                if b and b != c['body']:
                    ok = gh_write('PATCH', f'repos/{repo}/pulls/comments/{cid}', b)
                    n['comments_changed' if ok else 'comments_patch_failed'] += 1
    if new_sha:  # the state keeps the hash of the body that is now on GitHub
        with pr_state.locked(cfg['state']) as st:
            for key, sha in new_sha.items():
                if key in st['prs']:
                    st['prs'][key]['body_sha'] = sha
    print(json.dumps({'apply': apply, 'merged': merged, **dict(sorted(n.items())), 'unmarked_with_body_ids': lost_ids}))


def unmarked(reviews, ours=()):
    """The ids of the reviewer's reviews with a body but no marker and not named as ours: a marker lost to a
    hand edit, or the reviewer's own words (the mayor, 2026-10-30 15:45Z). Counted, never changed."""
    return [int(r['id']) for r in reviews if (r.get('body') or '').strip() and not pr_format.is_ours(r['body'])
            and int(r['id']) not in ours]


def selftest():
    """Each shape we delivered, restamped, has no disclosure phrase; a PR found only by GitHub's search is
    restamped too. Synthetic text only."""
    honest = "Automated review by A's review agents (Claude Code); A hasn't read this PR yet."
    shapes = {'v5': "**Changes requested** · automated, A hasn't read this PR\nA: nothing. Author: 1 change.\n\nx",
              'v4': honest + '\n\n**Approved as low risk. Nothing for A or Author.**\n\nx',
              'v3': honest + '\n\n**Low risk: nothing to check**\n\nx',
              'pointer': honest + '\n\nReplaced by [the new review of this commit](https://github.com/o/r/pull/1).',
              'note': "**Not reviewed automatically**\n   Our review of this PR didn't reach the quality bar: it was held."}
    got = {k: voice_body(b, 'A', 'Author', 'COMMENTED') for k, b in shapes.items()}
    entries = {'o/r#1': {'repo': 'o/r', 'number': 1, 'review_id': 5}}
    checks = {f'{k}: in the voice, no disclosure': bool(v) and v.startswith('**') and pr_format.MARK in v.split('\n')[0]
              for k, v in got.items()}
    checks['a PR found only by the search is restamped'] = [t[:2] for t in targets(entries, [('o/r', 1), ('o/r', 2)])] == \
        [('o/r', 1), ('o/r', 2)]
    link = f'[`a.py` L1](https://github.com/o/r/blob/{"0" * 40}/a.py#L1)'
    approval = f'**Approved** {pr_format.MARK}\nNothing needed.\n\n<details><summary>What was checked</summary>\n\n' \
               f'- **Adds a field** · {link}: nothing reads it yet\n\n</details>'
    up = current(approval, 'Author')[0].split('\n')
    checks['v5.8: line 2 names the author, the approval shows its reason'] = \
        up[1] == 'Author: nothing needed.' and up[3] == 'Nothing reads it yet.' and up[5].startswith('<details>')
    u = 'https://github.com/o/r/pull/1#pullrequestreview-'
    chain = [{'id': 1, 'body': pr_format.pointer_body('A', u + '2', 'reviewer'), 'html_url': u + '1'},
             {'id': 2, 'body': pr_format.pointer_body('A', u + '3', 'reviewer'), 'html_url': u + '2'},
             {'id': 3, 'body': approval, 'html_url': u + '3'}]
    moved = dict(pr_format.repoints(chain, 'A', 'reviewer'))
    checks['v5.8: every pointer follows the newest review'] = list(moved) == [1] and moved[1].endswith(f'({u}3).')
    hand = [{'id': 7, 'body': 'Looks good to me.'}, {'id': 8, 'body': ''}, {'id': 9, 'body': approval}]
    checks['v5.8.2: a body without the marker is counted, an empty approval and ours are not'] = \
        unmarked(hand) == [7] and unmarked(hand, ours=(7,)) == []
    for k, v in checks.items():
        print('ok  ' if v else 'FAIL', k)
    return all(checks.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--merged', action='store_true')
    ap.add_argument('--ours', default='', help='review ids, comma-separated')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        sys.exit(0 if selftest() else 1)
    restamp(pr_state.config(), a.apply, a.merged, {int(x) for x in a.ours.split(',') if x.strip().isdigit()})


if __name__ == '__main__':
    main()
