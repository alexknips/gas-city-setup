#!/usr/bin/env python3
"""The review audit (gcs-y48; the reviewer, 2026-11-05: "full analysis: what questions were spot on and which were
off. and why."): every review of ours on the configured repositories, against what the PR's author did with each
ask. Zero tokens to collect; one model answer per PR to call each ask.

  pr_audit.py collect [--since DATE] [--pr REPO#N]
      GitHub, read-only (our reviews, their threads, the author's replies, PR comments and later commits), and
      the host-local posted copies (each inline ask's kind, severity, area, repository rule, money line, and the
      static flags on its lines) -> PR_REVIEW_WORK_DIR/audit/prs/
  pr_audit.py classify [--pr REPO#N] [--limit N] [--again]
      one headless model answer per PR (no tools, no hooks, capture off, pr_judge.ask): each ask spot on, off or
      unclear, what the author did, and for an off one why -> PR_REVIEW_WORK_DIR/audit/verdicts/
  pr_audit.py totals [--tag T --second T2]
      counts per verdict, repository, provenance (what raised the ask), area, reason and evidence, the verdicts
      checked against the zero-token evidence, and how often two readers (two models) agree; an ask they disagree
      on counts as unclear -> PR_REVIEW_WORK_DIR/audit/totals.json
  pr_audit.py propose --tag T --second T2
      3-5 reviewer changes that would remove the most off asks and keep the spot-on ones and every security catch
      -> .../audit/changes.json; prints the changes with every word outside the pack's own docs masked
  pr_audit.py compose --tag T --second T2     the analysis for the client's channel, 2-3 messages in the
      reviewer's voice, held unless each is short enough, links only audited PRs and names no internal system
  pr_audit.py post                            the composed messages through the post runner (pr_client_slack)

It prints counts, rule ids and enums only. The PRs' text (our asks, the author's replies and commits, the code
around each ask) stays under PR_REVIEW_WORK_DIR/audit (mode 700, files 600): it is the repositories' own text
(README, "What never leaves GitHub").
"""
import argparse
import base64
import collections
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_format  # noqa: E402
import pr_judge  # noqa: E402
import pr_rules  # noqa: E402
import pr_state  # noqa: E402

MARK_RE = re.compile(r'<!-- pr-review v[0-9.]+ -->')
CONTEXT_LINES = 12  # lines of code shown above and below each ask
SINCE = os.environ.get('PR_REVIEW_AUDIT_SINCE', '2026-10-29')

AUDIT_Q = '''query($o: String!, $r: String!, $n: Int!) { repository(owner: $o, name: $r) { pullRequest(number: $n) {
  number url state createdAt mergedAt closedAt author { login } headRefOid title body
  reviews(first: 100) { nodes { databaseId author { login } body submittedAt state commit { oid } } }
  reviewThreads(first: 100) { nodes { isResolved isOutdated resolvedBy { login } path line originalLine
    comments(first: 50) { nodes { databaseId author { login } body createdAt pullRequestReview { databaseId } } } } }
  comments(first: 100) { nodes { author { login } body createdAt } }
  commits(last: 100) { nodes { commit { oid committedDate messageHeadline } } } } } }'''


def audit_dir(cfg, *parts):
    return pr_state.private_dir(cfg, 'audit', *parts)


def write_private(path, data):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f, indent=1, sort_keys=True)
    os.replace(path + '.tmp', path)


def pr_file(cfg, sub, repo, number):
    return os.path.join(audit_dir(cfg, sub), f"{repo.replace('/', '__')}-{number}.json")


def login(n):
    return (((n or {}).get('author') or {}).get('login') or '').lower()


def reviewed_numbers(cfg, repo, since):
    """The PRs of `repo` that the reviewer reviewed, newest first (search follows the repository's current name)."""
    name = canonical(repo)
    out = pr_rules.gh(['search', 'prs', '--repo', name, '--reviewed-by', cfg['reviewer'], '--limit', '1000',
                       '--updated', f'>={since}', '--json', 'number', '--jq', '.[].number'])
    return sorted({int(x) for x in out.split() if x.strip().isdigit()}, reverse=True)


def canonical(repo):
    """The repository's current owner/name (a GET follows a rename), remembered for REST paths (gcs-kwj)."""
    try:
        name = pr_rules.gh(['api', f'repos/{repo}', '--jq', '.full_name']).strip() or repo
    except pr_rules.GhError:
        return repo
    pr_rules.learn(repo, {'url': f'https://github.com/{name}/pull/0'})
    return name


def posted_copy(cfg, repo, number, head):
    p = os.path.join(pr_state.private_dir(cfg, 'feedback', 'posted'), f"{repo.replace('/', '__')}-{number}-{head[:10]}.json")
    try:
        return json.load(open(p))
    except (OSError, ValueError):
        return None


def item_meta(copy, path, line, body):
    """The posted item behind one inline comment of ours: matched by the comment's text, else by its place."""
    if not copy:
        return None
    res = copy.get('result') or {}
    items, comments = res.get('items') or [], copy.get('comments') or []
    k = next((i for i, c in enumerate(comments) if (c.get('body') or '').strip() == (body or '').strip()), None)
    if k is None:
        k = next((i for i, c in enumerate(comments) if c.get('path') == path and c.get('line') == line), None)
    if k is None or k >= len(items):
        return None
    it = items[k]
    lo, hi = it.get('line_start') or 0, it.get('line_end') or 0
    flags = sorted({f.get('rule') for f in res.get('flagged') or [] if f.get('path') == it.get('path')
                    and lo <= (f.get('line') or -1) <= hi and f.get('rule')})
    return {k2: it.get(k2) for k2 in ('kind', 'severity', 'area', 'rule', 'confidence', 'verified', 'money')} | {
        'flags': flags, 'fallback': bool(res.get('fallback')), 'outcome': res.get('outcome'),
        'line_start': lo, 'line_end': hi}


def is_note(body):
    """Whether one of our review bodies is the note (R33's fallback): the old notes' first words, or line 1 in the
    reviewer's voice saying who acts, which only a fallback's does (pr_format.voice_reasons)."""
    body = body or ''
    m = pr_format.VOICE_LINE1_RE.match(body.splitlines()[0] if body else '')
    return pr_format.NOT_REVIEWED in body or bool(m and m.group(2))


def provenance(meta, note=False):
    """What raised an ask, from our own records (zero tokens): the note's floor, a static flag on its lines, the
    money step, a repository rule, or the review model's own reading. Without a posted copy (a later review of
    the same head replaced it), the review's own body tells a note from a full review ('unknown')."""
    if meta is None:
        return 'note' if note else 'unknown'
    if meta.get('fallback'):
        return 'note'
    if meta.get('flags'):
        return 'static:' + meta['flags'][0]
    if meta.get('money'):
        return 'money'
    if meta.get('rule'):
        return 'repo-rule'
    return 'model'


def code_context(repo, head, path, line, cache):
    """The file at our review's head around `line` (read-only REST), numbered; '' when GitHub has no copy."""
    key = (head, path)
    if key not in cache:
        try:
            raw = pr_rules.gh(['api', f'repos/{pr_rules.rest(repo)}/contents/{path}?ref={head}', '--jq', '.content'])
            cache[key] = base64.b64decode(raw).decode('utf-8', 'replace').splitlines()
        except (pr_rules.GhError, ValueError):
            cache[key] = []
    rows = cache[key]
    if not rows or not line:
        return ''
    lo, hi = max(1, line - CONTEXT_LINES), min(len(rows), line + CONTEXT_LINES)
    return '\n'.join(f'{n}: {rows[n - 1]}' for n in range(lo, hi + 1))


def collect_pr(cfg, repo, number):
    owner, name = canonical(repo).split('/')
    pr = pr_rules.graphql(AUDIT_Q, o=owner, r=name, n=number)['repository']['pullRequest']
    if not pr:
        return None
    who, author = cfg['reviewer'].lower(), login(pr)
    ours = [r for r in pr['reviews']['nodes'] if login(r) == who and MARK_RE.search(r.get('body') or '')]
    if not ours:
        return None
    by_id = {r['databaseId']: r for r in ours}
    first = min(r['submittedAt'] for r in ours)
    cache, asks = {}, []
    for t in pr['reviewThreads']['nodes']:
        cs = t['comments']['nodes']
        if not cs or login(cs[0]) != who or (cs[0].get('pullRequestReview') or {}).get('databaseId') not in by_id:
            continue
        rv = by_id[cs[0]['pullRequestReview']['databaseId']]
        head = (rv.get('commit') or {}).get('oid') or ''
        line = t.get('originalLine') or t.get('line')
        meta = item_meta(posted_copy(cfg, repo, number, head), t['path'], line, cs[0]['body'])
        asks.append({
            'ref': f"c{cs[0]['databaseId']}", 'review_id': rv['databaseId'], 'review_at': rv['submittedAt'], 'head': head,
            'path': t['path'], 'line': line, 'ask': cs[0]['body'], 'meta': meta,
            'provenance': provenance(meta, is_note(rv.get('body'))),
            'context': code_context(repo, head, t['path'], line, cache),
            'thread': {'resolved': t['isResolved'], 'outdated': t['isOutdated'],
                       'resolved_by': 'author' if login({'author': t.get('resolvedBy')}) == author else
                       ('reviewer' if login({'author': t.get('resolvedBy')}) == who else ('other' if t.get('resolvedBy') else None)),
                       'replies': [{'by': 'author' if login(c) == author else 'reviewer' if login(c) == who else 'other',
                                    'at': c['createdAt'], 'body': c['body']} for c in cs[1:]]}})
    reviews = [{'id': r['databaseId'], 'at': r['submittedAt'], 'head': (r.get('commit') or {}).get('oid') or '',
                'event': r['state'], 'pointer': bool(pr_format.POINTER_RE.search(r.get('body') or '')),
                'note': is_note(r.get('body')),
                'outcome': ((posted_copy(cfg, repo, number, (r.get('commit') or {}).get('oid') or '') or {})
                            .get('result') or {}).get('outcome'), 'body': r['body']} for r in sorted(ours, key=lambda r: r['submittedAt'])]
    return {
        'repo': repo, 'number': number, 'url': pr['url'], 'state': pr['state'], 'merged_at': pr.get('mergedAt'),
        'closed_at': pr.get('closedAt'), 'created': pr['createdAt'], 'author_is_reviewer': author == who,
        'title': pr.get('title') or '', 'description': (pr.get('body') or '')[:4000],
        'reviews': reviews, 'asks': asks,
        'author_comments': [{'at': c['createdAt'], 'body': c['body']} for c in pr['comments']['nodes']
                            if login(c) == author and c['createdAt'] >= first],
        'other_comments': sum(1 for c in pr['comments']['nodes'] if login(c) not in (author, who) and c['createdAt'] >= first),
        'commits_after': [{'at': n['commit']['committedDate'], 'oid': n['commit']['oid'], 'headline': n['commit']['messageHeadline']}
                          for n in pr['commits']['nodes'] if n['commit']['committedDate'] > first],
        'collected_at': pr_state.iso(pr_state.now())}


def collect(cfg, since, only=None):
    counts = collections.Counter()
    targets = [tuple(only.rsplit('#', 1))] if only else \
        [(repo, n) for repo in cfg['repos'] for n in reviewed_numbers(cfg, repo, since)]
    for repo, n in targets:
        try:
            rec = collect_pr(cfg, repo, int(n))
        except pr_rules.GhError:
            counts['gh_error'] += 1
            continue
        if not rec:
            counts['no_review_of_ours'] += 1
            continue
        write_private(pr_file(cfg, 'prs', repo, n), rec)
        counts['prs'] += 1
        counts[f"repo:{repo.split('/')[1]}"] += 1
        counts['reviews'] += len(rec['reviews'])
        counts['pointers'] += sum(r['pointer'] for r in rec['reviews'])
        counts['inline_asks'] += len(rec['asks'])
        counts['asks_with_meta'] += sum(1 for a in rec['asks'] if a['meta'])
        counts['asks_with_context'] += sum(1 for a in rec['asks'] if a['context'])
        counts['asks_author_replied'] += sum(1 for a in rec['asks'] if any(r['by'] == 'author' for r in a['thread']['replies']))
        counts['asks_outdated'] += sum(1 for a in rec['asks'] if a['thread']['outdated'])
        counts['asks_resolved'] += sum(1 for a in rec['asks'] if a['thread']['resolved'])
        counts['prs_merged'] += bool(rec['merged_at'])
        counts['prs_with_author_comments'] += bool(rec['author_comments'])
        counts['prs_with_commits_after'] += bool(rec['commits_after'])
        for a in rec['asks']:
            counts[f"prov:{a['provenance'].split(':')[0]}"] += 1
    print(json.dumps({'collect': dict(sorted(counts.items())), 'dir': audit_dir(cfg, 'prs')}))
    return counts


VERDICTS = ['spot_on', 'off', 'unclear']
EVIDENCE = ['fixed', 'agreed', 'answered', 'pushed_back', 'resolved_silently', 'ignored_merged', 'no_response_open',
            'not_needed']
WHYS = ['product_call', 'missing_repo_context', 'misread_code', 'diff_already_answered', 'ordinary_slip', 'style_or_taste',
        'rule_over_fires', 'other', 'none']
SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['asks'],
    'properties': {'asks': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False,
        'required': ['ref', 'verdict', 'evidence', 'why', 'product_call', 'misleading', 'security_relevant', 'dup_of',
                     'reason'],
        'properties': {
            'ref': {'type': 'string'}, 'verdict': {'type': 'string', 'enum': VERDICTS},
            'evidence': {'type': 'string', 'enum': EVIDENCE}, 'why': {'type': 'string', 'enum': WHYS},
            'product_call': {'type': 'boolean'}, 'misleading': {'type': 'boolean'},
            'security_relevant': {'type': 'boolean'},
            'dup_of': {'type': 'string'}, 'reason': {'type': 'string'}}}}}}

PROMPT = """You audit an automated code reviewer. It posted reviews on one pull request (PR), in the voice of the human
reviewer, with asks for the PR's author: inline comments on lines, and questions or changes in the review bodies.
For each ask, decide from the evidence below whether it was spot on or off (a false positive), and why. The goal:
find the asks that should never have been raised, so the reviewer stops raising them, while every ask that found a
real problem keeps being raised. Judge the ask itself, not only what the author did with it.

verdict:
- spot_on: it found something real: the author fixed it (a later commit or the thread's outdated flag shows the
  lines changed, or a reply says so), agreed, or the problem is plainly real in the code shown.
- off: a false positive: the code was right; the PR's diff, description or commits already answered it; the
  repository's own rules or practice make it moot; it was an ordinary slip not worth a review comment; it was style
  or taste; or it misread the code. An author's reasoned push-back that holds makes it off.
- unclear: you cannot tell from what is here (for example no answer, and the code alone does not settle it).
evidence (what the author did): fixed | agreed | answered | pushed_back | resolved_silently | ignored_merged |
  no_response_open | not_needed (an ask that needed no action, such as a note).
product_call: true when the ask states or recommends how the product should behave (what users, patients,
  customers or staff should see, get, pay or be able to do; which option or default to pick; a price, rule, wording
  or flow) instead of asking the author what was intended. The reviewer must never make product decisions: an
  ask with product_call true is off, with why product_call, whatever else it found.
why (only when off; else none): product_call | missing_repo_context (the repository's own practice or rules make it moot, for
  example its test strategy) | misread_code | diff_already_answered | ordinary_slip | style_or_taste |
  rule_over_fires (a mechanical rule raised it where it does not apply: see each ask's provenance) | other.
misleading: true when an off ask could send the author the wrong way; false when it was only noise.
security_relevant: true when the ask is about access control, authentication, tenant scoping, patient data, audit
  logging, secrets, or a change that cannot be undone, whatever the verdict.
reason: one plain sentence of at most 30 words saying why.
dup_of: the ref of the first ask when this one repeats an earlier ask; else "".

Give one entry for every inline ask, with its ref as given. Also give every question or change that a review BODY
asks of the author its own entry, with ref "body:<review id>:<k>" (k = 1, 2, ... in the body's order): a review
that asks something and has no inline ask holds all its asks in its body. Skip only a body that points to a newer
review. When a body ask and an inline ask are the same question (the note form puts the question in the body and
the place inline), give the inline one dup_of the body entry, so each question counts once.

HUMAN JUDGMENTS (the human reviewer's own words about these reviews; they settle the verdict of the asks they name, and
only those; judge every other ask on its own evidence):
{notes}

THE CLIENT CHANNEL'S MESSAGES ABOUT THIS PR (the review channel, oldest first):
{slack}

THE REPOSITORY'S OWN RULES (from its docs):
{rules}

THE PR:
{pr}
"""


def repo_rules(cfg, repo):
    """The repository's goals and rules as its docs write them (the rules digest's host-local cache), or ''."""
    try:
        d = json.load(open(os.path.join(cfg['work'], 'digest', f"{repo.replace('/', '__')}.json")))
    except (OSError, ValueError):
        return '(none cached)'
    goals = d.get('goals') or ''
    rows = [f"{k}: {v.get('rule')}" for k, v in sorted((d.get('rules') or {}).items())]
    return (f'Goals: {goals}\n' if goals else '') + '\n'.join(rows)


def pr_text(rec):
    out = [f"{rec['url']} state {rec['state']}{' merged ' + rec['merged_at'] if rec['merged_at'] else ''}",
           f"Title: {rec['title']}", f"Description:\n{rec['description']}", '', 'OUR REVIEWS (oldest first):']
    for r in rec['reviews']:
        out.append(f"--- review {r['id']} at {r['at']} on commit {r['head'][:10]} ({r['event']}, outcome "
                   f"{r['outcome'] or '?'}{', replaced by a newer one' if r['pointer'] else ''}):\n{r['body']}")
    out.append('\nINLINE ASKS:')
    for a in rec['asks']:
        m = a['meta'] or {}
        out.append(f"--- ask {a['ref']} (review {a['review_id']}) on {a['path']} line {a['line']}; provenance "
                   f"{a['provenance']}; kind {m.get('kind')}, severity {m.get('severity')}, area {m.get('area')}, "
                   f"repository rule {m.get('rule') or '-'}, static flags {', '.join(m.get('flags') or []) or '-'}\n"
                   f"{a['ask']}\nthread: resolved {a['thread']['resolved']} (by {a['thread']['resolved_by']}), "
                   f"lines changed since (outdated) {a['thread']['outdated']}")
        for x in a['thread']['replies']:
            out.append(f"  reply by {x['by']} at {x['at']}: {x['body']}")
        if a['context']:
            out.append(f"  code at the reviewed commit:\n{a['context']}")
    out.append('\nTHE AUTHOR\'S PR COMMENTS AFTER OUR FIRST REVIEW:')
    out += [f"- {c['at']}: {c['body']}" for c in rec['author_comments']] or ['(none)']
    out.append('\nCOMMITS AFTER OUR FIRST REVIEW:')
    out += [f"- {c['at']} {c['oid'][:10]}: {c['headline']}" for c in rec['commits_after']] or ['(none)']
    return '\n'.join(out)


ASKING = ('questions', 'changes', 'not-reviewed', 'escalate')


def check_verdicts(rec, so):
    """Reasons a model answer is not usable: a missing or unknown inline ref, a body ref of another PR's review, or
    a review that asked something with no inline ask and no body entry."""
    refs = [a['ref'] for a in rec['asks']]
    got = [x['ref'] for x in so.get('asks') or []]
    review_ids = {str(r['id']) for r in rec['reviews']}
    bad = [f'missing:{r}' for r in refs if got.count(r) != 1]
    bad += [f'unknown:{g}' for g in got if g not in refs and not (g.startswith('body:') and g.split(':')[1] in review_ids)]
    inline = {a['review_id'] for a in rec['asks']}
    body = {g.split(':')[1] for g in got if g.startswith('body:')}
    bad += [f"no-body-asks:{r['id']}" for r in rec['reviews'] if not r['pointer'] and r['id'] not in inline
            and str(r['id']) not in body and (r['outcome'] in ASKING or r['outcome'] is None)]
    return bad


def side_inputs(cfg):
    """The human notes (audit/notes.json: {"owner/name#n": ["..."]}) and the channel's messages (audit/slack.json,
    pr_client_read.py), keyed by the configured repository's PR key."""
    def load(name):
        try:
            return json.load(open(os.path.join(audit_dir(cfg), name)))
        except (OSError, ValueError):
            return {}
    names = {canonical(r).lower(): r for r in cfg['repos']} | {r.lower(): r for r in cfg['repos']}
    slack = collections.defaultdict(list)
    for m in load('slack.json').get('messages') or []:
        repo, _, n = (m.get('pr') or '').partition('#')
        if repo.lower() in names and n.isdigit():
            slack[f'{names[repo.lower()]}#{int(n)}'].append(m)
    return load('notes.json'), slack


def classify(cfg, only=None, limit=None, again=False, model=None, tag=''):
    files = sorted(glob.glob(os.path.join(audit_dir(cfg, 'prs'), '*.json')))
    if only:
        files = [pr_file(cfg, 'prs', *only.rsplit('#', 1))]
    counts, cost = collections.Counter(), 0.0
    notes, slack = side_inputs(cfg)
    for p in files:
        rec = json.load(open(p))
        out = pr_file(cfg, 'verdicts' + (f'-{tag}' if tag else ''), rec['repo'], rec['number'])
        if os.path.exists(out) and not again:
            counts['cached'] += 1
            continue
        if limit is not None and counts['asked'] >= limit:
            break
        load = pr_rules.load_avg()
        if load > cfg['max_load']:
            counts['skipped_load'] += 1
            print(json.dumps({'classify': 'stopped: load', 'load': load}))
            break
        key = f"{rec['repo']}#{rec['number']}"
        msgs = sorted(slack.get(key, []), key=lambda m: m.get('ts') or '')
        prompt = PROMPT.format(rules=repo_rules(cfg, rec['repo']), pr=pr_text(rec),
                               notes='\n'.join(f'- {x}' for x in notes.get(key, [])) or '(none)',
                               slack='\n'.join(f"- {m['who']} ({'reply' if m.get('thread_ts') else 'message'}): {m['text']}"
                                                for m in msgs) or '(none)')
        counts['with_slack'] += bool(msgs)
        counts['with_notes'] += bool(notes.get(key))
        so, meta = None, {}
        for attempt in (1, 2):
            so, meta = pr_judge.ask(cfg, prompt, SCHEMA, model or cfg['model'], 4, timeout=900)
            cost += meta.get('cost_usd') or 0
            bad = check_verdicts(rec, so) if so else ['no-answer']
            if not bad:
                break
            prompt += '\n\nYOUR ANSWER WAS NOT USABLE: ' + ', '.join(bad[:20]) + '. Answer again, one entry per ref.'
        counts['asked'] += 1
        if bad:
            counts['unusable'] += 1
            continue
        write_private(out, {'url': rec['url'], 'asks': so['asks'], 'model': meta.get('model'),
                            'cost_usd': meta.get('cost_usd'), 'at': pr_state.iso(pr_state.now())})
        counts['classified'] += 1
    print(json.dumps({'classify': dict(counts), 'cost_usd': round(cost, 2)}))
    return counts


def origins(rec, asks):
    """{ref: (provenance, area)} for every verdict entry. An inline ask has its own; a body ask takes those of the
    inline asks that repeat it (the note puts the question in the body and the place inline), else 'note' when its
    review was the note and 'model' otherwise."""
    by_ref = {a['ref']: a for a in rec['asks']}
    outcome = {str(r['id']): r.get('outcome') for r in rec['reviews']}
    out = {}
    for x in asks:
        a = by_ref.get(x['ref'])
        if a:
            out[x['ref']] = (a['provenance'], (a.get('meta') or {}).get('area') or 'unknown')
    for x in asks:
        if x['ref'] in out:
            continue
        dups = [by_ref[y['ref']] for y in asks if y.get('dup_of') == x['ref'] and y['ref'] in by_ref]
        known = [d for d in dups if d['provenance'] != 'unknown'] or dups
        if known:
            out[x['ref']] = (known[0]['provenance'], (known[0].get('meta') or {}).get('area') or 'unknown')
        else:
            rid = x['ref'].split(':')[1] if x['ref'].startswith('body:') else ''
            note = outcome.get(rid) == 'not-reviewed' or any(str(r['id']) == rid and r.get('note') for r in rec['reviews'])
            out[x['ref']] = ('note' if note else 'model', 'body')
    return out


def totals(cfg, tag='', second=''):
    """Counts over the verdicts (each repeated ask counted once), the checks against zero-token evidence, and with
    `second` (another reader's verdicts) how often the two readers agree; where they disagree the ask counts as
    unclear."""
    t = collections.defaultdict(collections.Counter)
    checks = collections.Counter()
    sub = 'verdicts' + (f'-{tag}' if tag else '')
    for p in sorted(glob.glob(os.path.join(audit_dir(cfg, sub), '*.json'))):
        v = json.load(open(p))
        rec = json.load(open(os.path.join(audit_dir(cfg, 'prs'), os.path.basename(p))))
        other = {}
        if second:
            try:
                other = {x['ref']: x for x in json.load(open(os.path.join(audit_dir(cfg, 'verdicts-' + second),
                                                                          os.path.basename(p))))['asks']}
            except (OSError, ValueError):
                t['agreement']['second_missing_pr'] += 1
        asks = []
        for x in v['asks']:
            if second and not x['dup_of'] and x['ref'] in other:
                y = other[x['ref']]
                t['agreement']['same' if y['verdict'] == x['verdict'] else f"{x['verdict']}->{y['verdict']}"] += 1
                if y['verdict'] != x['verdict']:
                    x = dict(x, verdict='unclear', why='none')
            elif second and not x['dup_of']:
                t['agreement']['ref_only_in_first'] += 1
            asks.append(x)
        by_ref = {a['ref']: a for a in rec['asks']}
        where = origins(rec, v['asks'])
        repo = rec['repo'].split('/')[1]
        t['prs']['all'] += 1
        for x in asks:
            if x['dup_of']:
                t['repeats'][x['verdict']] += 1
                continue
            a = by_ref.get(x['ref'])
            prov, area = where[x['ref']]
            t['verdict'][x['verdict']] += 1
            t['repo'][f"{repo}:{x['verdict']}"] += 1
            t['provenance'][f"{prov}:{x['verdict']}"] += 1
            t['area'][f"{area}:{x['verdict']}"] += 1
            t['evidence'][f"{x['evidence']}:{x['verdict']}"] += 1
            if x['verdict'] == 'off':
                t['why'][x['why']] += 1
                t['why_by_provenance'][f"{x['why']}:{prov}"] += 1
                t['misleading'][str(x['misleading'])] += 1
                if x['security_relevant']:
                    t['off_security']['count'] += 1
            if x['security_relevant']:
                t['security'][x['verdict']] += 1
            if x.get('product_call'):
                t['product_call'][x['verdict']] += 1
            # the verdicts against what GitHub shows, zero tokens
            if a and x['evidence'] == 'fixed' and not a['thread']['outdated'] and not rec['commits_after']:
                checks['fixed_without_any_later_commit'] += 1
            if a and x['verdict'] == 'off' and a['thread']['outdated']:
                checks['off_but_lines_changed_since'] += 1
            if a and x['evidence'] in ('agreed', 'answered', 'pushed_back') and \
                    not any(r['by'] == 'author' for r in a['thread']['replies']) and not rec['author_comments']:
                checks['reply_evidence_without_a_reply'] += 1
            if x['verdict'] == 'off' and x['why'] == 'none':
                checks['off_without_why'] += 1
    out = {k: dict(sorted(c.items())) for k, c in sorted(t.items())}
    out['checks'] = dict(checks)
    write_private(os.path.join(audit_dir(cfg), 'totals.json'), out)
    print(json.dumps(out, indent=1))
    return out


# ---------- what to change, the analysis for the channel, and its post ----------

def own_vocabulary():
    """Every word of the pack's own docs (README, style guide, checklists, rules): the words a proposal may use."""
    here = os.path.dirname(os.path.abspath(__file__))
    words = set()
    for p in glob.glob(os.path.join(here, '..', '*.md')) + glob.glob(os.path.join(here, '..', '..', 'README.md')) + \
            glob.glob(os.path.join(here, '..', 'rules.toml')):
        words |= {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z'-]+", open(p).read())}
    return words


def masked(text, vocab):
    """`text` with every word outside the pack's own vocabulary replaced by [x]: what this session may print."""
    return re.sub(r"[A-Za-z][A-Za-z'-]+", lambda m: m.group(0) if m.group(0).lower() in vocab else '[x]', text)


def off_rows(cfg, tag, second):
    """Every unique ask both readers call off, with what raised it: (PR url, provenance, area, why, misleading,
    security_relevant, reason). The reason is the PRs' text: host-local only."""
    rows, spot = [], collections.Counter()
    for p in sorted(glob.glob(os.path.join(audit_dir(cfg, 'verdicts-' + tag), '*.json'))):
        v = json.load(open(p))
        rec = json.load(open(os.path.join(audit_dir(cfg, 'prs'), os.path.basename(p))))
        try:
            other = {x['ref']: x for x in json.load(open(os.path.join(audit_dir(cfg, 'verdicts-' + second),
                                                                      os.path.basename(p))))['asks']}
        except (OSError, ValueError):
            other = {}
        where = origins(rec, v['asks'])
        for x in v['asks']:
            if x['dup_of'] or (second and other.get(x['ref'], {}).get('verdict', x['verdict']) != x['verdict']):
                continue
            prov, area = where[x['ref']]
            if x['verdict'] == 'spot_on':
                spot[(prov.split(':')[0], area, x['security_relevant'])] += 1
            if x['verdict'] == 'off':
                rows.append({'url': rec['url'], 'provenance': prov, 'area': area, 'why': x['why'],
                             'misleading': x['misleading'], 'security_relevant': x['security_relevant'],
                             'reason': x['reason']})
    return rows, spot


CHANGE_TARGETS = ['note-floor', 'static-rule', 'repo-rule', 'money', 'model-asks', 'repeated-asks',
                  'answered-by-the-pr', 'repo-practice', 'severity', 'other']
PROPOSE_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['changes'],
    'properties': {'changes': {'type': 'array', 'minItems': 3, 'maxItems': 5, 'items': {
        'type': 'object', 'additionalProperties': False,
        'required': ['target', 'rule', 'change', 'off_removed', 'spot_on_at_risk', 'keeps_security'],
        'properties': {'target': {'type': 'string', 'enum': CHANGE_TARGETS}, 'rule': {'type': 'string'},
                       'change': {'type': 'string'}, 'off_removed': {'type': 'integer'},
                       'spot_on_at_risk': {'type': 'integer'}, 'keeps_security': {'type': 'boolean'}}}}}}
PROPOSE = """An automated code reviewer posted asks (questions and change requests) on pull requests. An audit called
each ask spot on or off (a false positive). Below are all the off asks, each with what raised it (provenance: the
note's floor (a fallback when the full review was held), a static rule flag, a repository rule, the money step,
or the review model itself), its review area, the reason category and a one-line reason, and the counts of the
spot-on asks by the same fields.

Propose the 3 to 5 changes to the reviewer that would remove the most off asks while keeping every spot-on ask,
and never stop it from catching security problems (access control, authentication, tenant scoping, patient data,
audit logging, secrets, changes that cannot be undone). For each: target, rule (the static rule id or reviewer
rule it changes, or ""), change (at most 40 words, in general terms: no repository, product, file, function or
code names), off_removed (how many of the off asks below it removes), spot_on_at_risk (how many spot-on asks it
could also remove), keeps_security.

SPOT-ON COUNTS (provenance, area, security relevant: count):
{spot}

OFF ASKS:
{off}
"""


def propose(cfg, tag, second):
    rows, spot = off_rows(cfg, tag, second)
    prompt = PROPOSE.format(spot='\n'.join(f'- {k}: {n}' for k, n in sorted(spot.items())),
                            off='\n'.join(f"- {r['provenance']} | {r['area']} | {r['why']} | security "
                                          f"{r['security_relevant']}: {r['reason']}" for r in rows))
    so, meta = pr_judge.ask(cfg, prompt, PROPOSE_SCHEMA, 'claude-opus-5-5', 4, timeout=900)
    if not so:
        print(json.dumps({'propose': 'no answer', **meta}))
        return None
    write_private(os.path.join(audit_dir(cfg), 'changes.json'), so)
    vocab = own_vocabulary()
    for c in so['changes']:
        print(json.dumps({'target': c['target'], 'rule': masked(c['rule'], vocab), 'off_removed': c['off_removed'],
                          'spot_on_at_risk': c['spot_on_at_risk'], 'keeps_security': c['keeps_security'],
                          'change': masked(c['change'], vocab)}))
    print(json.dumps({'propose': len(so['changes']), 'off_asks': len(rows), 'cost_usd': round(meta.get('cost_usd') or 0, 2)}))
    return so


MSG_MAX = 3400
COMPOSE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['messages'],
                  'properties': {'messages': {'type': 'array', 'minItems': 1, 'maxItems': 3,
                                              'items': {'type': 'string'}}}}
COMPOSE = """Write a Slack post for the client's code review channel, as {reviewer}, the human reviewer whose name
is on the reviews, to {author}, who wrote the PRs. {reviewer} asked for an honest analysis of {reviewer}'s own
review comments on {author}'s PRs since {since}: which questions were spot on, which were off (false positives),
and why, and what changes next.

Write 2 or 3 messages, each under {limit} characters, in plain, short sentences, first person ("my review
comments"). Slack markdown: *bold*, bullet lines, links as <url|#number>. No tables, no headings with #.
Message 1: the totals and the main reasons asks were off. Message 2: the off ones by PR (link each PR, one short
line on why each was off; group repeats; at most 25 PRs, the most misleading first). Message 3 (if room is
needed): what was spot on (a few examples with links) and the changes I am making so this happens less, while
security, patient data, SOC 2 and changes that cannot be undone are still always raised. Thank {author} for the
replies. Never name tools, models, agents or internal systems; never paste code.

TOTALS (unique asks; where two readings disagreed the ask counts as unclear):
{totals}

OFF ASKS (PR, what raised it, area, reason category, misleading, reason):
{off}

SPOT ON (PR, area, reason):
{spot}

THE CHANGES:
{changes}
"""
INTERNAL = re.compile(r'(?i)\b(mayor|polecat|gas ?city|loomtracer|hivemind|bead|crew|claude|anthropic|llm|'
                      r'(?:the |a )?model|agent|automated|pipeline|refinery|town)\b')


def message_problems(msgs, urls):
    """Why the composed messages may not go out: too long, a link that is not one of the audited PRs, an internal
    word."""
    bad = []
    for k, m in enumerate(msgs, 1):
        if len(m) > MSG_MAX:
            bad.append(f'message {k}: {len(m)} characters')
        for u in re.findall(r'https?://[^\s|>]+', m):
            if u.rstrip('/.') not in urls:
                bad.append(f'message {k}: a link that is not an audited PR')
        if INTERNAL.search(m):
            bad.append(f'message {k}: an internal word ({INTERNAL.search(m).group(0).lower()})')
    return bad


def compose(cfg, tag, second, since):
    rows, _ = off_rows(cfg, tag, second)
    tot = json.load(open(os.path.join(audit_dir(cfg), 'totals.json')))
    changes = json.load(open(os.path.join(audit_dir(cfg), 'changes.json')))['changes']
    spots, urls = [], set()
    for p in sorted(glob.glob(os.path.join(audit_dir(cfg, 'verdicts-' + tag), '*.json'))):
        v = json.load(open(p))
        urls.add(v['url'])
        spots += [f"{v['url']} | {x['reason']}" for x in v['asks'] if x['verdict'] == 'spot_on' and not x['dup_of']][:2]
    names = dict(x.split('=', 1) for x in os.environ.get('PR_REVIEW_NAMES', '').split() if '=' in x)
    prompt = COMPOSE.format(
        reviewer=os.environ.get('PR_REVIEW_REVIEWER_NAME') or cfg['reviewer'],
        author=' or '.join(sorted(set(names.values()))) or 'the author', since=since, limit=MSG_MAX - 200,
        totals=json.dumps({k: tot.get(k) for k in ('verdict', 'repo', 'why', 'misleading', 'security', 'agreement')}),
        off='\n'.join(f"- {r['url']} | {r['provenance']} | {r['area']} | {r['why']} | misleading {r['misleading']}: "
                      f"{r['reason']}" for r in rows),
        spot='\n'.join(f'- {s}' for s in spots[:40]),
        changes='\n'.join(f"- {c['change']}" for c in changes))
    for attempt in (1, 2, 3):
        so, meta = pr_judge.ask(cfg, prompt, COMPOSE_SCHEMA, 'claude-opus-5-5', 4, timeout=900)
        bad = message_problems(so['messages'], urls) if so else ['no answer']
        if not bad:
            break
        prompt += '\n\nYOUR MESSAGES CANNOT GO OUT: ' + '; '.join(bad[:10]) + '. Write them again.'
    if bad:
        print(json.dumps({'compose': 'held', 'problems': [b.split(':')[0] + ':' + b.split(':')[1].split('(')[0] for b in bad]}))
        return None
    write_private(os.path.join(audit_dir(cfg), 'messages.json'), so)
    print(json.dumps({'compose': 'ok', 'messages': len(so['messages']), 'chars': [len(m) for m in so['messages']],
                      'links': sum(len(re.findall(r'https?://', m)) for m in so['messages'])}))
    return so


def post(cfg):
    """The composed messages to the client's review channel, in order, through the post runner (pr_client_slack:
    one exact post per run, allowlisted channel). Prints statuses only; stops at the first that did not land."""
    import pr_client_slack
    msgs = json.load(open(os.path.join(audit_dir(cfg), 'messages.json')))['messages']
    channel = cfg.get('client_slack_channel')
    done = []
    for k, m in enumerate(msgs, 1):
        status, meta = pr_client_slack.post(cfg, channel, m)
        done.append(status)
        pr_state.log(cfg['state'], {'run': 'audit-post', 'message': k, 'status': status, 'chars': len(m)})
        if status not in ('ok', 'sent-unconfirmed'):
            break
    print(json.dumps({'post': done}))
    return done


def selftest():
    """The audit's zero-token parts on synthetic data: what raised an ask, the posted item behind a comment, the
    model answer's checks, the mask, the channel messages' checks, and the totals with two readers."""
    import tempfile
    checks = {}
    meta = {'fallback': False, 'flags': [], 'money': None, 'rule': ''}
    checks['provenance: note, static flag, money, repository rule, model, unknown'] = [
        provenance(dict(meta, fallback=True)), provenance(dict(meta, flags=['sec-auth'])),
        provenance(dict(meta, money=[{'x': 1}])), provenance(dict(meta, rule='D3')), provenance(meta),
        provenance(None), provenance(None, True)] == ['note', 'static:sec-auth', 'money', 'repo-rule', 'model', 'unknown', 'note']
    checks['is_note: the old notes and a fallback line 1 in the reviewer voice; a full review is not'] = \
        is_note(pr_format.NOT_REVIEWED + ' x') and not is_note('**Questions** ' + pr_format.MARK + '\n\nx') and not is_note('') \
        and is_note('**Questions**' + pr_format.VOICE_FALLBACK.format(author='Author') + ' ' + pr_format.MARK + '\n\nx')
    copy = {'comments': [{'path': 'a.ts', 'line': 4, 'body': '**1. X**: y'}, {'path': 'b.ts', 'line': 9, 'body': 'z'}],
            'result': {'outcome': 'questions', 'flagged': [{'rule': 'phi-logs', 'path': 'b.ts', 'line': 8}],
                       'items': [{'kind': 'question', 'area': 'other', 'path': 'a.ts', 'line_start': 4, 'line_end': 4},
                                 {'kind': 'change', 'area': 'patient data', 'path': 'b.ts', 'line_start': 7, 'line_end': 9}]}}
    m1, m2 = item_meta(copy, 'a.ts', 4, '**1. X**: y'), item_meta(copy, 'b.ts', 9, 'edited later')
    checks['item_meta: by text, else by place; the static flags on its lines'] = \
        m1['kind'] == 'question' and m1['flags'] == [] and m2['kind'] == 'change' and m2['flags'] == ['phi-logs'] \
        and item_meta(copy, 'c.ts', 1, 'q') is None and item_meta(None, 'a.ts', 4, 'x') is None
    rec = {'asks': [{'ref': 'c1', 'review_id': 10}], 'reviews': [
        {'id': 10, 'pointer': False, 'outcome': 'questions'}, {'id': 11, 'pointer': False, 'outcome': 'not-reviewed'},
        {'id': 12, 'pointer': True, 'outcome': 'questions'}, {'id': 13, 'pointer': False, 'outcome': 'low-risk'}]}
    ok = {'asks': [{'ref': 'c1'}, {'ref': 'body:11:1'}]}
    checks['check_verdicts: every inline ref once, a body entry for an asking review without inline asks'] = \
        check_verdicts(rec, ok) == [] and check_verdicts(rec, {'asks': [{'ref': 'body:11:1'}]}) == ['missing:c1'] and \
        check_verdicts(rec, {'asks': [{'ref': 'c1'}]}) == ['no-body-asks:11'] and \
        check_verdicts(rec, {'asks': [{'ref': 'c1'}, {'ref': 'body:11:1'}, {'ref': 'body:99:1'}, {'ref': 'c7'}]}) == \
        ['unknown:body:99:1', 'unknown:c7']
    vocab = {'the', 'note', 'asks', 'only', 'on', 'flagged', 'lines'}
    checks['masked: a word outside the pack vocabulary is hidden'] = \
        masked('The note asks only on flagged lines of FooBar', vocab) == 'The note asks only on flagged lines [x] [x]'
    urls = {'https://github.com/o/r/pull/1'}
    checks['message_problems: length, an unaudited link, an internal word'] = \
        message_problems(['ok <https://github.com/o/r/pull/1|#1>'], urls) == [] and \
        len(message_problems(['x' * (MSG_MAX + 1)], urls)) == 1 and \
        message_problems(['see https://github.com/o/r/pull/2'], urls) == ['message 1: a link that is not an audited PR'] and \
        message_problems(['the mayor said'], urls) == ['message 1: an internal word (mayor)']
    tmp = tempfile.mkdtemp()
    cfg = {'work': tmp}
    base = {'repo': 'o/r', 'number': 1, 'url': 'https://github.com/o/r/pull/1', 'commits_after': [], 'author_comments': [],
            'asks': [{'ref': 'c1', 'provenance': 'note', 'meta': {'area': 'security'},
                      'thread': {'outdated': True, 'replies': []}},
                     {'ref': 'c2', 'provenance': 'model', 'meta': {'area': 'other'}, 'thread': {'outdated': False, 'replies': []}}],
            'reviews': [{'id': 10}]}
    write_private(pr_file(cfg, 'prs', 'o/r', 1), base)
    a1 = {'verdict': 'off', 'evidence': 'ignored_merged', 'why': 'rule_over_fires', 'misleading': False,
          'security_relevant': True, 'dup_of': '', 'reason': 'r'}
    write_private(pr_file(cfg, 'verdicts-a', 'o/r', 1), {'url': base['url'], 'asks': [
        dict(a1, ref='c1'), dict(a1, ref='c2', verdict='spot_on', why='none', security_relevant=False),
        dict(a1, ref='body:10:1', dup_of='c1')]})
    write_private(pr_file(cfg, 'verdicts-b', 'o/r', 1), {'url': base['url'], 'asks': [
        dict(a1, ref='c1'), dict(a1, ref='c2', verdict='off', why='style_or_taste')]})
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        t1, t2 = totals(cfg, 'a'), totals(cfg, 'a', 'b')
    checks['totals: repeats apart; off with its why; off but its lines changed is a check'] = \
        t1['verdict'] == {'off': 1, 'spot_on': 1} and t1['repeats'] == {'off': 1} and t1['why'] == {'rule_over_fires': 1} \
        and t1['checks'].get('off_but_lines_changed_since') == 1 and t1['off_security'] == {'count': 1}
    checks['totals with two readers: agreement counted, a disagreement counts as unclear'] = \
        t2['agreement'] == {'same': 1, 'spot_on->off': 1} and t2['verdict'] == {'off': 1, 'unclear': 1}
    for k, v in checks.items():
        print('ok  ' if v else 'FAIL', k)
    return all(checks.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('step', choices=['collect', 'classify', 'totals', 'propose', 'compose', 'post', 'selftest'])
    ap.add_argument('--since', default=SINCE)
    ap.add_argument('--pr', metavar='REPO#N')
    ap.add_argument('--limit', type=int)
    ap.add_argument('--again', action='store_true')
    ap.add_argument('--model', help='classify: the model (default PR_REVIEW_MODEL)')
    ap.add_argument('--tag', default='', help='classify/totals: the verdicts dir suffix (one per reader)')
    ap.add_argument('--second', default='', help='totals: the other reader\'s tag, for agreement')
    a = ap.parse_args()
    if a.step == 'selftest':
        return 0 if selftest() else 1
    cfg = pr_state.config()
    if not cfg['repos'] or not cfg['reviewer']:
        sys.exit('PR_REVIEW_REPOS and PR_REVIEW_REVIEWER must be set')
    if a.step == 'collect':
        collect(cfg, a.since, a.pr)
    elif a.step == 'classify':
        classify(cfg, a.pr, a.limit, a.again, a.model, a.tag)
    elif a.step == 'totals':
        totals(cfg, a.tag, a.second)
    elif a.step == 'propose':
        propose(cfg, a.tag, a.second)
    elif a.step == 'compose':
        compose(cfg, a.tag, a.second, a.since)
    else:
        post(cfg)


if __name__ == '__main__':
    sys.exit(main())
