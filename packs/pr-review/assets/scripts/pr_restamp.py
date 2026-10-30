#!/usr/bin/env python3
"""Restamp our delivered reviews into the reviewer's voice (decision-ed7320b7), with zero tokens: each
review body of ours, and our inline comments, rewritten from the text GitHub holds. Line 1 becomes the
verdict word and the invisible marker, line 2 what the author must do; the disclosure lines go, "not
checked by any agent" becomes scope, a section for the reviewer becomes "Open questions". The findings,
links and options stay as they were. A review whose text would still hold a disclosure phrase (R38) is
left alone and counted.

  pr_restamp.py                       dry run over the state's PRs of the configured repositories
  pr_restamp.py --apply               rewrite the reviews on open PRs
  pr_restamp.py --apply --merged      ... and on merged and closed PRs (GitHub keeps each review's edit history)

Prints counts only. A dismissed review keeps its dismissal message (GitHub has no edit for it).
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
        rest[0] = pr_format.NOTHING_NEEDED if part == 'nothing' else f'{m.group(6)}: {part}.'
    elif rest and pr_format.NOTHING_RE.match(rest[0]):
        rest[0] = pr_format.NOTHING_NEEDED
    text = '\n'.join(out + rest)
    text = text.replace('These changes are not checked by any agent.', 'These changes are outside this review.')
    text = text.replace(f'**For {reviewer}:**', '**Open questions:**')
    return re.sub(rf' · Ask {re.escape(author)} · ', ' · Question · ', text)


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


def restamp(cfg, apply, merged):
    reviewer = cfg['reviewer'].lower()
    name = os.environ.get('PR_REVIEW_REVIEWER_NAME') or cfg['reviewer'] or 'the reviewer'
    names = dict(x.split('=', 1) for x in os.environ.get('PR_REVIEW_NAMES', '').split() if '=' in x)
    with pr_state.locked(cfg['state']) as st:
        entries = {k: dict(e) for k, e in st['prs'].items() if e.get('repo') in cfg['repos'] and e.get('review_id')}
    n = collections.Counter(prs=len(entries))
    new_sha = {}
    for key, e in sorted(entries.items()):
        repo, num = e['repo'], e['number']
        try:
            pr = gh_lines(f'repos/{repo}/pulls/{num}', '{state, merged, author: .user.login}')[0]
            reviews = gh_lines(f'repos/{repo}/pulls/{num}/reviews?per_page=100',
                               f'.[] | select((.user.login | ascii_downcase) == "{reviewer}") | {{id, state, body}}')
        except (RuntimeError, IndexError, ValueError):
            n['gh_failed'] += 1
            continue
        if pr['state'] != 'open' and not merged:
            n['prs_skipped_not_open'] += 1
            continue
        author = names.get(pr['author'], pr['author'])
        for rv in reviews:
            if not pr_format.is_ours(rv['body']):
                continue
            n['reviews_ours'] += 1
            first = (rv['body'] or '').split('\n', 1)[0]
            if pr_format.MARK_PREFIX in first:
                n['already_in_voice'] += 1
                continue
            body = voice_body(rv['body'], name, author, rv['state'])
            if body is None:
                n['left_disclosure_remains'] += 1
                if not apply:  # which of our own template phrases stays (counts, never the review's text)
                    probe = voice_body_unchecked(rv['body'], name, author, rv['state'])
                    for hit in {m.group(0).lower() for m in pr_format.DISCLOSURE_RE.finditer(probe or '')}:
                        n[f'left_phrase:{hit}'] += 1
                    n['left_no_verdict'] += probe is None
                continue
            n['dismissal_message_kept'] += rv['state'] == 'DISMISSED'
            comments = gh_lines(f'repos/{repo}/pulls/{num}/comments?per_page=100',
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
    print(json.dumps({'apply': apply, 'merged': merged, **dict(sorted(n.items()))}))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--merged', action='store_true')
    a = ap.parse_args()
    restamp(pr_state.config(), a.apply, a.merged)


if __name__ == '__main__':
    main()
