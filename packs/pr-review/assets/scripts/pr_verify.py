#!/usr/bin/env python3
"""Verify before posting (gcs-z3j; the mayor, decision-27473469: reviews that point out real issues, not imaginary
ones). Each point a review would post is checked against the repository at the reviewed head by a separate model
with read-only tools, capture off, on the rubric of the precision audit: REAL, PLAUSIBLE, NOT_AN_ISSUE or GENERIC.
A REAL point posts; a PLAUSIBLE one only at high severity, as a question; the rest are dropped and logged by rule id.

This module is the zero-token part: the points, the prompt, the answer's schema and what the verdicts do to a
review. pr_review.verify_review() runs the model (in run_model's sandbox) and decides what is left to post.

  pr_verify.py --selftest
"""
import json
import sys

VERDICTS = ('REAL', 'PLAUSIBLE', 'NOT_AN_ISSUE', 'GENERIC')
SEVERITIES = ('high', 'medium', 'low', 'none')
KEEP = 'REAL'
RC_SEVERITIES = ('high', 'medium')  # R35 (gcs-z3j): a formal change request rests on a REAL change of these only

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['points'], 'properties': {
    'points': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['n', 'verdict', 'severity'],
        'properties': {'n': {'type': 'integer'}, 'verdict': {'type': 'string', 'enum': list(VERDICTS)},
                       'severity': {'type': 'string', 'enum': list(SEVERITIES)}}}}}}
MISSED_MAX = 3
MISSED_FIELDS = ('path', 'line_start', 'line_end', 'kind', 'severity', 'area', 'change', 'why', 'ask')


def schema(item_schema):
    """The verifier's answer: a verdict per point and the high or medium issues no point raises (`missed`), each in
    the review's own item fields and limits (`item_schema`: the review answer's item, so a missed issue that posts
    meets the same style guide)."""
    props = {k: v for k, v in item_schema['properties'].items() if k in MISSED_FIELDS}
    props['severity'] = {'type': 'string', 'enum': list(RC_SEVERITIES)}
    missed = {'type': 'array', 'maxItems': MISSED_MAX, 'items': {
        'type': 'object', 'additionalProperties': False, 'required': list(MISSED_FIELDS), 'properties': props}}
    return {'type': 'object', 'additionalProperties': False, 'required': ['points', 'missed'],
            'properties': {**SCHEMA['properties'], 'missed': missed}}

PROMPT = """You are checking a code review before it is posted. A reviewer looked at a pull request and wrote the
numbered points below. Each point will be posted only if it points at a REAL issue in the code, so the author is
never asked about something the code already handles.

The repository is checked out in the current directory at the exact commit under review. The pull request's changes
at that commit are in {side}/diff.patch. The numbered points, each a requested change or a question with the file and
lines it is about, are between the two marker lines:
----- BEGIN POINTS -----
{points}----- END POINTS -----

For each point, check the code: diff.patch and the repository (use Grep and Read to find the callers, the tests,
the config and the repository's own docs and rules: README, AGENTS.md, CLAUDE.md, CONTRIBUTING, docs). Decide:
- REAL: the code at this commit really has this problem or gap, or really breaks a rule the repository writes down.
  For a question: the answer really is a problem, or the code does not answer it and it matters.
- PLAUSIBLE: a fair concern a careful reviewer would raise, but the code you can see neither confirms nor rules it out.
- NOT_AN_ISSUE: the code already handles it, the cited code does not do what the point says, the point misreads the
  change, or it asks about something that is not there.
- GENERIC: boilerplate that would fit almost any pull request and says nothing specific about this one.
Severity of the issue the point is about: high (bug, data loss, security or privacy hole, compliance break), medium
(wrong behaviour in some cases, missing test for risky logic), low (minor), none (no issue).

Be strict in both directions: do not excuse a wrong point, and do not invent problems. Answer every point, by its
number, in `points` (there may be none: the reviewer raised nothing and would approve).

Then look for REAL issues of high or medium severity in diff.patch that no point raises. List at most three in
`missed`, worst first, only ones you confirmed in the code, each on the new-side lines of its file and in the fields'
own words: `change` is 3-6 plain words on what the pull request changed there, `why` the one fact that makes it
matter, `ask` the one change or question for the author (a question ends with "?"). An empty list when there are
none: never pad it."""


def points(rv):
    """The review's points to check, in order: each item, then each decision the model made (the script's own
    decisions, `auto`, are rule-based and kept as they are). [{n, ref: (part, index), kind, text}]."""
    out = []
    for j, i in enumerate(rv.get('items') or []):
        where = f"{i.get('path')} L{i.get('line_start')}-L{i.get('line_end') or i.get('line_start')}"
        text = ' '.join(x for x in (i.get('change'), i.get('why'), i.get('ask')) if x)
        out.append({'ref': ('items', j), 'kind': 'change' if i.get('kind') == 'change' else 'question',
                    'text': f'{where}: {text}'})
    for j, d in enumerate(rv.get('decisions') or []):
        if d.get('auto'):
            continue
        ln = d.get('link') or {}
        where = f"{ln.get('path')} L{ln.get('line_start')}-L{ln.get('line_end') or ln.get('line_start')}" \
            if ln.get('kind') == 'code' else 'the pull request'
        opts = '; '.join(d.get('options') or [])
        out.append({'ref': ('decisions', j), 'kind': 'question',
                    'text': f"{where}: {d.get('question') or ''} {d.get('why') or ''} Options: {opts}".strip()})
    for n, p in enumerate(out, 1):
        p['n'] = n
    return out


def points_md(pts):
    """points.md for the verifier (host-local, in the run's own directory; never printed or logged)."""
    return ''.join(f"{p['n']}. [{p['kind']}] {p['text']}\n" for p in pts)


def verdicts_of(answer, pts):
    """{n: (verdict, severity)} from the model's answer; a point it did not answer, or answered outside the enums,
    has none (and is not kept)."""
    out = {}
    for x in (answer or {}).get('points') or []:
        if isinstance(x, dict) and x.get('verdict') in VERDICTS and x.get('severity') in SEVERITIES and \
                isinstance(x.get('n'), int) and 1 <= x['n'] <= len(pts):
            out.setdefault(x['n'], (x['verdict'], x['severity']))
    return out


SEV_ORDER = {s: k for k, s in enumerate(SEVERITIES)}  # high first


def lower(a, b):
    return a if SEV_ORDER[a] >= SEV_ORDER[b] else b


def combine(a, b, pts):
    """Two readings (decision-b84b12fb: one reading flips verdicts): a point is kept only when both keep it. REAL in
    both: REAL at the lower severity; REAL or PLAUSIBLE-high in both: PLAUSIBLE high (a question); else dropped as
    the harsher reading says (NOT_AN_ISSUE before GENERIC before PLAUSIBLE). A point either reading did not answer
    has no verdict. Returns ({n: (verdict, severity)}, the number of points the readings disagree on)."""
    va, vb, out, disagree = verdicts_of(a, pts), verdicts_of(b, pts), {}, 0
    for n in range(1, len(pts) + 1):
        x, y = va.get(n), vb.get(n)
        if not x or not y:
            continue
        disagree += x[0] != y[0]
        ask = lambda v: v[0] == 'REAL' or v == ('PLAUSIBLE', 'high')  # noqa: E731
        if x[0] == y[0] == 'REAL':
            out[n] = ('REAL', lower(x[1], y[1]))
        elif ask(x) and ask(y):
            out[n] = ('PLAUSIBLE', 'high')
        else:
            out[n] = (next(v for v in ('NOT_AN_ISSUE', 'GENERIC', 'PLAUSIBLE', 'REAL') if v in (x[0], y[0])), 'none')
    return out, disagree


def confirmed_missed(a, b):
    """1b (the mayor, gc-wisp-rdwakv): the first reading's high or medium missed issues that the second reading
    also names in the same file, at most MISSED_MAX, as review items marked checked (REAL)."""
    other = {m.get('path') for m in (b or {}).get('missed') or [] if isinstance(m, dict) and m.get('severity') in RC_SEVERITIES}
    out = []
    for m in (a or {}).get('missed') or []:
        if isinstance(m, dict) and m.get('severity') in RC_SEVERITIES and m.get('path') in other and \
                all(m.get(k) not in (None, '') for k in MISSED_FIELDS):
            out.append({**{k: m[k] for k in MISSED_FIELDS}, 'side': 'new', 'rule': '', 'confidence': 9,
                        'verified': 'two readings of the verifier', 'checked': KEEP, 'checked_severity': m['severity']})
    return out[:MISSED_MAX]


def as_question(item):
    """A PLAUSIBLE high point posts as a question for the author, never as a change request."""
    ask = (item.get('ask') or '').strip() or 'Is this safe here?'
    return dict(item, kind='question', ask=ask if ask.endswith('?') else ask.rstrip('.') + '?')


def apply(rv, pts, verdicts):
    """What posts: the REAL points, the PLAUSIBLE high ones as questions, each marked `checked` (and
    `checked_severity`); the script's own decisions stay. Returns (rv kept, log of counts and rule ids)."""
    keep = {'items': {}, 'decisions': {}}
    log = {'points': len(pts), 'kept': 0, 'asked': 0, 'dropped': {}, 'dropped_rules': [], 'unanswered': 0}
    for p in pts:
        part, j = p['ref']
        v = verdicts.get(p['n'])
        if v is None:
            log['unanswered'] += 1
            continue
        verdict, sev = v
        src = rv[part][j]
        if verdict == KEEP:
            keep[part][j] = dict(src, checked=verdict, checked_severity=sev)
            log['kept'] += 1
        elif verdict == 'PLAUSIBLE' and sev == 'high':
            x = as_question(src) if part == 'items' else src
            keep[part][j] = dict(x, checked=verdict, checked_severity=sev)
            log['asked'] += 1
        else:
            log['dropped'][verdict] = log['dropped'].get(verdict, 0) + 1
            log['dropped_rules'].append(src.get('rule') or '')
    items = [keep['items'][j] for j in range(len(rv.get('items') or [])) if j in keep['items']]
    decisions = [d if d.get('auto') else keep['decisions'][j] for j, d in enumerate(rv.get('decisions') or [])
                 if d.get('auto') or j in keep['decisions']]
    log['dropped_rules'] = sorted(set(log['dropped_rules']) - {''})
    return dict(rv, items=items, decisions=decisions, verified=True), log


def survivors(rv):
    """The checked points left in a review (the script's own decisions are not points)."""
    return [x for x in (rv.get('items') or []) + (rv.get('decisions') or []) if x.get('checked')]


def rc_ok(rv):
    """R35 after verification: a formal change request rests on a REAL change of medium or high severity. A
    review that was never verified (PR_REVIEW_VERIFY off) is not held to it."""
    if not rv.get('verified'):
        return True
    return any(i.get('kind') == 'change' and i.get('checked') == KEEP and i.get('checked_severity') in RC_SEVERITIES
               for i in rv.get('items') or [])


def selftest():
    item = lambda kind, rule, **k: dict({'path': 'a.ts', 'line_start': 3, 'line_end': 4, 'kind': kind,  # noqa: E731
                                          'change': 'Sends the name', 'why': 'w', 'ask': 'Who reads it', 'rule': rule}, **k)
    rv = {'outcome': 'changes', 'items': [item('change', 'r1'), item('question', 'r2'), item('change', 'r3'),
                                          item('question', '')],
          'decisions': [{'auto': True, 'question': 'Sign-off?'}, {'question': 'Which one?', 'options': ['a', 'b'],
                                                                  'link': {'kind': 'code', 'path': 'b.ts', 'line_start': 1}}]}
    pts = points(rv)
    md = points_md(pts)
    ans = {'points': [{'n': 1, 'verdict': 'REAL', 'severity': 'medium'}, {'n': 2, 'verdict': 'NOT_AN_ISSUE', 'severity': 'none'},
                      {'n': 3, 'verdict': 'PLAUSIBLE', 'severity': 'high'}, {'n': 4, 'verdict': 'GENERIC', 'severity': 'none'},
                      {'n': 5, 'verdict': 'PLAUSIBLE', 'severity': 'low'}, {'n': 9, 'verdict': 'REAL', 'severity': 'high'},
                      {'n': 1, 'verdict': 'GENERIC', 'severity': 'none'}]}
    v = verdicts_of(ans, pts)
    kept, log = apply(rv, pts, v)
    low_change = apply(rv, pts, {1: ('REAL', 'low')})[0]
    checks = {
        'the points are the items and the model\'s decisions, numbered, the script\'s decision left out':
            [p['ref'] for p in pts] == [('items', 0), ('items', 1), ('items', 2), ('items', 3), ('decisions', 1)] and
            md.startswith('1. [change] a.ts L3-L4: Sends the name w Who reads it\n') and '5. [question] b.ts L1-L1: Which one?' in md,
        'an answer out of range or given twice keeps the first, in range only': v.get(1) == ('REAL', 'medium') and 9 not in v,
        'REAL posts, PLAUSIBLE high posts as a question, the rest are dropped with their rule ids':
            [(i['rule'], i['kind'], i['checked']) for i in kept['items']] == [('r1', 'change', 'REAL'), ('r3', 'question', 'PLAUSIBLE')]
            and kept['items'][1]['ask'] == 'Who reads it?' and log['dropped'] == {'NOT_AN_ISSUE': 1, 'GENERIC': 1, 'PLAUSIBLE': 1}
            and log['dropped_rules'] == ['r2'] and log['kept'] == 1 and log['asked'] == 1,
        'the script\'s own decision stays; a dropped model decision goes':
            [d.get('question') for d in kept['decisions']] == ['Sign-off?'] and kept['verified'] is True,
        'a point the verifier did not answer is not kept':
            apply(rv, pts, {})[1]['unanswered'] == 5 and survivors(apply(rv, pts, {})[0]) == [],
        'R35: a formal change request needs a REAL change of medium or high severity once verified':
            rc_ok(kept) and not rc_ok(low_change) and not rc_ok(dict(kept, items=[kept['items'][1]])) and rc_ok({'items': []}),
    }
    two = lambda *vs: {'points': [{'n': n, 'verdict': v, 'severity': s} for n, (v, s) in enumerate(vs, 1)]}  # noqa: E731
    a = two(('REAL', 'high'), ('REAL', 'medium'), ('PLAUSIBLE', 'high'), ('REAL', 'low'), ('GENERIC', 'none'))
    b = two(('REAL', 'medium'), ('NOT_AN_ISSUE', 'none'), ('REAL', 'high'), ('PLAUSIBLE', 'low'), ('PLAUSIBLE', 'low'))
    both, disagree = combine(a, b, pts)
    half, _ = combine(a, {'points': []}, pts)
    m = lambda path, sev='high', **k: dict({'path': path, 'line_start': 2, 'line_end': 3, 'kind': 'change',  # noqa: E731
                                            'severity': sev, 'area': 'security', 'change': 'Logs the token',
                                            'why': 'w', 'ask': 'Drop it'}, **k)
    got = confirmed_missed({'missed': [m('a.ts'), m('b.ts'), m('c.ts', why='')]}, {'missed': [m('a.ts', 'medium'), m('c.ts')]})
    item_schema = {'properties': {k: {'type': 'string'} for k in MISSED_FIELDS + ('rule', 'verified', 'side')}}
    sch = schema(item_schema)
    checks.update({
        'two readings: REAL in both is REAL at the lower severity; a point one reading drops is dropped; REAL or '
        'PLAUSIBLE-high in both is a question; a point one reading did not answer has no verdict':
            both == {1: ('REAL', 'medium'), 2: ('NOT_AN_ISSUE', 'none'), 3: ('PLAUSIBLE', 'high'), 4: ('PLAUSIBLE', 'none'),
                     5: ('GENERIC', 'none')} and disagree == 4 and half == {},
        '1b: a missed issue posts only when the second reading names one in the same file, complete, marked REAL':
            [x['path'] for x in got] == ['a.ts'] and got[0]['checked'] == 'REAL' and got[0]['side'] == 'new' and
            got[0]['checked_severity'] == 'high' and confirmed_missed({'missed': [m('a.ts')]}, {'missed': [m('a.ts', 'low')]}) == [],
        'the answer\'s schema asks for points and up to three missed issues in the review\'s item fields, high or medium':
            sch['required'] == ['points', 'missed'] and sch['properties']['missed']['maxItems'] == MISSED_MAX and
            sch['properties']['missed']['items']['properties']['severity']['enum'] == ['high', 'medium'] and
            sorted(sch['properties']['missed']['items']['properties']) == sorted(MISSED_FIELDS),
    })
    for k, ok in checks.items():
        print('ok  ' if ok else 'FAIL', k)
    return all(checks.values())


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
    print(json.dumps({'verdicts': VERDICTS}))
