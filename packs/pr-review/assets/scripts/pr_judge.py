#!/usr/bin/env python3
"""Reader test (style guide R26): a second, cheap model reads a review as the reviewer and the author will
and scores it 1-5 on scan, markers, decisions, plain, length and useful. The review run holds a review
below markers 4 or useful 3 (regenerated once with the judge's problems, then held). The reader also
names the cleared entries that describe a possible risk without saying why it is acceptable (R31): those
become questions for the author. The judge sees only the review (body and inline comments), runs with no
tools, no hooks, no MCP and Loomtracer capture off, and what this script prints or keeps in the runtime
is scores and counts, never the review's text.

  pr_judge.py --audit [--limit N]   judge delivered reviews that have no scores yet (pr-review-feedback runs it)
  pr_judge.py --review REPO#N ID    judge one delivered review now; prints its scores

The rubric is the town's first reader test (2026-10-29 21:05Z), which found that the delivered reviews
scanned fast but were rarely useful: fired rules never explained, no inline markers, findings said three
times.
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_rules  # noqa: E402
import pr_state  # noqa: E402

BAR = {'markers': 4, 'useful': 3}
DIMS = ['scan', 'markers', 'decisions', 'plain', 'length', 'useful']
_SCORE = {'type': 'integer', 'minimum': 1, 'maximum': 5}
SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': DIMS + ['problems', 'risky_cleared', 'behaviour_in_skip'],
    'properties': {
        **{d: dict(_SCORE) for d in DIMS},
        'behaviour_in_skip': {'type': 'boolean', 'description': (
            'True when the "Safe to skip" line holds a change in product behaviour (what users, patients or '
            'staff see or get), which needs its own line or a decision.')},
        'risky_cleared': {'type': 'array', 'maxItems': 12, 'description': (
            'The entries under "Checked and cleared" or "What was checked" that describe a behaviour that could be '
            'a risk without saying why it is acceptable or intended.'), 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['n', 'question'], 'properties': {
                'n': {'type': 'integer', 'minimum': 1, 'description': 'The entry number, 1 = the first.'},
                'question': {'type': 'string', 'maxLength': 200, 'description': (
                    'The one concrete check the author can answer about those lines, ending in "?", at most 25 '
                    'words; never a stock question.')}}}},
        'decisions': {'type': 'integer', 'minimum': 0, 'maximum': 5, 'description': '0 = no decision for the reviewer (n/a)'},
        'problems': {'type': 'array', 'maxItems': 3, 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['what', 'fix'],
            'properties': {'what': {'type': 'string', 'maxLength': 240}, 'fix': {'type': 'string', 'maxLength': 240}}}},
    },
}

RUBRIC = """You judge one pull-request review written by an AI review agent for {name}, a busy engineering lead.
It is posted on the PR under {name}'s login; {author}, the PR's author, reads it too. {name} is the
harness engineer and does not know this codebase; {author} wrote the change and reviewed it. A good
review checks the change against the repository's own written goals and rules, citing them, and never
asks {name} to read or review code. {name} has said:
- "it just fluff and really hard to read. there are no markers in the review what I actually need to manually review"
- "that review comment needs to link to the human decisions (either in text or code reference that still need to be answered). otherwise you do not need me!"

Score 1-5 (5 = best):
1. scan: can {name} tell in 10 seconds what the outcome is and whether he must act?
2. markers: does every point {name} or {author} must act on, and every flagged change it calls safe, link to
   the exact code line or text? Inline comments count as markers on their lines.
3. decisions: if {name} must decide something, is it one clear question with options and a recommendation?
   0 when there is no decision for {name}.
4. plain: plain words, no rule codes, jargon or boilerplate?
5. length: no longer than needed; nothing said twice?
6. useful: would this save {name} time compared with reading the PR himself, or is it empty or obvious?
Then give at most 3 problems, each with a concrete fix. Last, list the cleared entries (numbered from 1,
under "Checked and cleared" or "What was checked") that describe a behaviour that could be a risk
without saying why it is acceptable or intended, each with the one concrete question its author should
answer about those lines, and say whether the "Safe to skip" line holds a change
in product behaviour (what users, patients or staff see or get).

Required by the style guide, so never a problem and never counted against a score: line 1, the verdict
with the note that it is automated and {name} hasn't read the PR; line 2, who acts; each item for
{author} as one line in the body that points at its inline comment, which carries the full text on the
exact lines; the "What it does" sentence; the "Safe to skip" line, the rest of the
diff by group with each file linked; the status line (CI, merge conflict, state), shown only when
something did not pass; an approval's short form, two lines and a collapsed list of what was checked;
and, when the PR touches billing, plans, credits or pricing, the question for {author} on who confirms
it, with one line per changed place saying what it changes (not checked by the agent).

Never quote or paraphrase the review's content, file names, code or the PR's subject. Refer only to its
structure: "line 1", "item 2", "the footer", "the inline comment on the second file".

THE REVIEW:

{review}"""


def review_text(body, comments):
    """The review as the judge reads it: the body, then each inline comment with the line it sits on."""
    inline = '\n'.join(f"- inline on {c.get('path')} line {c.get('line')}"
                       + (f" (from line {c['start_line']})" if c.get('start_line') else '')
                       + f":\n{c.get('body')}" for c in comments)
    return f"BODY:\n{body}\n\nINLINE COMMENTS ({len(comments)}):\n{inline or '(none)'}"


def ask(cfg, prompt, schema, model, max_usd, timeout=300):
    """One headless `claude -p` answer to `schema`, with no tools, no hooks, no MCP, no session kept and
    Loomtracer capture off (the prompt holds the repository's text). Returns (structured output or None,
    meta {cost_usd, model[, error]}). The reader test, the money lines and the rules digest use it."""
    keep = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'USER', 'LOGNAME', 'TERM', 'TZ')
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(LOOMTRACER_INGEST_DISABLE='1', HIVEMIND_INGEST_DISABLE='1')
    claude = os.environ.get('PR_REVIEW_CLAUDE') or 'claude'
    cmd = [claude, '-p', '--output-format', 'json', '--json-schema', json.dumps(schema), '--model', model,
           '--no-session-persistence', '--setting-sources', '', '--settings', json.dumps({'disableAllHooks': True}),
           '--strict-mcp-config', '--tools', '', '--max-budget-usd', str(max_usd)]
    os.makedirs(cfg['work'], mode=0o700, exist_ok=True)
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env, cwd=cfg['work'], timeout=timeout)
        out = json.loads(r.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError) as e:
        return None, {'error': type(e).__name__, 'cost_usd': 0, 'model': model}
    meta = {'cost_usd': out.get('total_cost_usd') or 0, 'model': model}
    so = out.get('structured_output')
    if out.get('is_error') or not isinstance(so, dict):
        return None, {**meta, 'error': out.get('subtype') or 'no-structured-output'}
    return so, meta


def judge(cfg, body, comments, name, author):
    """(scores or None, problems, meta). Scores: {scan, markers, decisions, plain, length, useful};
    meta['risky_cleared']: the cleared entries (1 = first) the reader found risky without a reason (R31)."""
    prompt = RUBRIC.format(name=name, author=author, review=review_text(body, comments))
    so, meta = ask(cfg, prompt, SCHEMA, cfg['judge_model'], cfg['judge_max_usd'])
    if so is None:
        return None, [], meta
    risky = {int(x['n']): (x.get('question') or '').strip() for x in so.get('risky_cleared') or []
             if isinstance(x, dict) and isinstance(x.get('n'), int)}
    meta['risky_cleared'], meta['risky_questions'] = sorted(risky), risky
    meta['behaviour_in_skip'] = bool(so.get('behaviour_in_skip'))
    return {d: so[d] for d in DIMS}, so.get('problems') or [], meta


def below_bar(scores):
    """The R26 hold reasons ([] = the review passes the reader test)."""
    if scores is None:
        return ['R26: the reader test did not run']
    return [f'R26: the reader test scored {d} {scores[d]}, at least {v} needed' for d, v in BAR.items() if scores[d] < v]


def delivered(repo, number, review_id):
    """A delivered review's body and inline comments, the comments from the PR's comment list: the
    per-review list does not report `line`, which made the first judge call anchored comments unanchored."""
    rev = json.loads(pr_rules.gh(['api', f'repos/{repo}/pulls/{number}/reviews/{review_id}']))
    author = pr_rules.gh(['api', f'repos/{repo}/pulls/{number}', '--jq', '.user.login']).strip()
    jq = f'.[] | select(.pull_request_review_id == {int(review_id)}) | {{path, line, start_line, body}}'
    out = pr_rules.gh(['api', '--paginate', f'repos/{repo}/pulls/{number}/comments?per_page=100', '--jq', jq])
    mine = [json.loads(l) for l in out.splitlines() if l.strip()]
    return rev.get('body') or '', mine, rev.get('state'), author


def audit(cfg, limit):
    """Judge delivered reviews without scores (each once), newest first; scores go into the state file."""
    names = dict(x.split('=', 1) for x in os.environ.get('PR_REVIEW_NAMES', '').split() if '=' in x)
    name = os.environ.get('PR_REVIEW_REVIEWER_NAME') or cfg['reviewer'] or 'the reviewer'
    with pr_state.locked(cfg['state']) as st:
        todo = sorted(((k, dict(e)) for k, e in st['prs'].items()
                       if e.get('review_id') and e.get('delivered_at') and not e.get('judge')
                       and e.get('outcome') != 'not-reviewed'),  # the R33 note is fixed text
                      key=lambda ke: ke[1]['delivered_at'], reverse=True)[:limit]
    done, cost = {}, 0.0
    for key, e in todo:
        try:
            body, comments, _, login = delivered(e['repo'], e['number'], e['review_id'])
        except (pr_rules.GhError, ValueError, OSError):
            continue
        author = names.get(login, login or 'the author')
        scores, _, meta = judge(cfg, body, comments, name, author)
        cost += meta.get('cost_usd') or 0
        if scores:
            done[key] = {'judge': scores, 'judge_at': pr_state.iso(pr_state.now()), 'judge_review': e['review_id'],
                         'judge_below_bar': bool(below_bar(scores)), 'judge_risky': len(meta.get('risky_cleared') or [])}
    with pr_state.locked(cfg['state']) as st:
        for k, u in done.items():
            if k in st['prs']:
                st['prs'][k].update(u)
    rec = {'run': 'judge-audit', 'judged': len(done), 'tried': len(todo), 'cost_usd': round(cost, 4),
           'below_bar': sum(1 for u in done.values() if u['judge_below_bar'])}
    pr_state.log(cfg['state'], rec)
    print(json.dumps(rec))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--audit', action='store_true')
    ap.add_argument('--limit', type=int, default=5)
    ap.add_argument('--review', nargs=2, metavar=('REPO#N', 'ID'))
    a = ap.parse_args()
    cfg = pr_state.config()
    if a.review:
        repo, n = a.review[0].rsplit('#', 1)
        body, comments, state, login = delivered(repo, int(n), a.review[1])
        names = dict(x.split('=', 1) for x in os.environ.get('PR_REVIEW_NAMES', '').split() if '=' in x)
        scores, problems, meta = judge(cfg, body, comments, os.environ.get('PR_REVIEW_REVIEWER_NAME') or 'the reviewer',
                                       names.get(login, login or 'the author'))
        print(json.dumps({'review': a.review[1], 'state': state, 'inline': len(comments),
                          'anchored': sum(1 for c in comments if c['line']), 'scores': scores,
                          'problems': len(problems), 'hold': [r.split(':')[0] for r in below_bar(scores)], **meta}))
        return
    if a.audit:
        if not cfg['judge']:
            print(json.dumps({'skipped': 'PR_REVIEW_JUDGE is off'}))
            return
        audit(cfg, a.limit)
        return
    print(__doc__)


if __name__ == '__main__':
    main()
