#!/usr/bin/env python3
"""The fallback's questions (style guide R33; the mayor, 2026-10-30 21:39Z: "One specific question per
linked line with its own reason, no boilerplate sentences, no orphan link, one status line"). When the
full review was held, the fallback asks the author one question about what the code at each of its
places now does, with the reason the answer matters, instead of one stock question with stock options.

A cheap model reads only those hunks (no tools, no MCP, capture off, bounded by turns) and the script
checks every answer with the gate's own field rules before it goes out. A place whose answer fails gets
one more turn with the reasons; a place still without a question goes to the collapsed "Other places",
linked to its lines. When no place has a question, the fallback is the stock one (R33's ladder). Nothing
it reads or writes is printed.

  places(idx, money, flagged)       the places, the riskiest first: money places, the flagged hunks, else the largest hunks
  ask(cfg, idx, places[, answer])   (items, the places not asked about, "What it does", meta): the questions as items

The same turn reads the PR's title and description and writes one "What it does" sentence, checked like the
questions (plain words, no code names) and dropped when it fails.

`pr_fallback.py --selftest` checks the answers' checks and the turns on synthetic data, with no model.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_format  # noqa: E402
import pr_judge  # noqa: E402

TURNS = 2  # the first answer, then one more for the places whose answer failed
MAX_HUNK_CHARS = 6000
MAX_ASK, MAX_REASON = 20, 25  # words: the ask stands in the body (R4), the reason is the inline comment
RANGE_MOST = 40  # lines one question's inline comment spans at most (R3 allows 80)
TEST_RE = re.compile(r'(^|/)(tests?|__tests__|spec|e2e)/|\.(test|spec)\.[a-z]+$|_test\.[a-z]+$', re.I)

VERSIONS = 3  # wordings of each lead and question, best first: the model does not count words reliably (v5.8.3)
MAX_SUMMARY = 30  # words of the one "What it does" sentence
MAX_PR_TEXT = 1500  # characters of the PR description the turn reads
_WORDINGS = {'type': 'array', 'minItems': 1, 'maxItems': VERSIONS, 'items': {'type': 'string', 'maxLength': 200}}
SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['summary', 'places'], 'properties': {
    'summary': {'type': 'string', 'maxLength': 240},
    'places': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['n', 'changes', 'questions', 'reason'],
        'properties': {'n': {'type': 'integer', 'minimum': 1}, 'changes': _WORDINGS, 'questions': _WORDINGS,
                       'reason': {'type': 'string', 'maxLength': 240}}}}}}

PROMPT = """Below are a pull request's title and description and {count} of its changed places. Its author will
get one question about each place, so each question must be worth their time. Write:

- `summary`: one sentence of at most 25 words on what the pull request does, for someone who will not read
  it: what changes for users, patients, customers or staff.
- For each place:
  - `changes`: 3 to 6 plain words naming what the pull request changed there, in up to 3 wordings, the best
    first.
  - `questions`: ONE question, in up to 3 wordings, the best first and each shorter than the one before.
    Each wording is at most 16 words (count them; the limit is 20), ends in "?", and the author can answer
    it from what they know. Name the behaviour the new code has at this place (what users, patients,
    customers or staff now see, get, pay or can do, or what happens in a case the change does not show)
    and ask what happens in that case, or which of two behaviours they intend. It is about this place only.
  - `reason`: one sentence of at most 25 words: what goes wrong, and for whom, if the answer is not what
    the code assumes.

Never a stock question that fits any pull request: never ask whether it was tested, who reviews it,
whether it is safe or correct in general, or for a sign-off. Use plain words a person who does not read
code understands: no file, function or variable names, no code, no links. Give `n`, the place's number.
{again}
TITLE: {title}

DESCRIPTION:
{description}

{places}"""

AGAIN = """
Your earlier answers for these places could not be used: {why}. Write them again.
"""
COUNTS = 'for place {n} your shortest question was {k} words and the limit is 20'

# Our own words for why an answer failed: they go into the second turn's prompt (never the model's text).
WHY = {'R29': 'the change must be 3 to 6 plain words with no code names', 'R13': 'the question must end in "?"',
       'R4': 'the question must be at most 20 words', 'R5': 'every field needs at least 4 words',
       'R6': 'no filler phrases', 'R10': 'no rule codes', 'R25': 'no links', 'plain': 'no code or names from the code',
       'stock': 'the question fits any pull request; ask about the behaviour at this place',
       'same': 'two questions ask the same thing', 'reason': 'the reason must be at most 25 words',
       'none': 'no answer came back'}


def is_test(path):
    return bool(TEST_RE.search(path or ''))


def places(idx, money, flagged, fill=False):
    """The places to ask about, each once (one per hunk), the riskiest first: the money places that change
    behaviour by their risk, then the rest of them, then the flagged hunks with their rule's label; when
    the rules know no place, or with `fill` (the note) fewer than FALLBACK_QUESTIONS, the largest changed
    hunks outside tests after them. Each place: {path, line_start, line_end, hunk, what, money}."""
    out, seen = [], set()

    def add(p):
        key = (p['path'], p.get('hunk') if p.get('hunk') is not None else p['line_start'])
        if key not in seen:
            seen.add(key)
            out.append(p)
    order = sorted(range(len(money)), key=lambda j: (not money[j].get('behaviour', True), -(money[j].get('risk') or 3), j))
    for j in order:
        add(dict(money[j], money=True))
    for f in flagged:
        add(dict(pr_format.changed_range(idx, f['path'], f['line']), what=f.get('label') or '', money=False))
    if not out or (fill and len(out) < pr_format.FALLBACK_QUESTIONS):
        sized = []
        for path, f in idx.items():
            if is_test(path):
                continue
            for k, h in enumerate(f.get('hunks') or []):
                if h[2] and (path, k) not in seen:
                    sized.append((-len(h[2]), path, k, min(h[2]), max(h[2])))
        for _, path, k, a, b in sorted(sized)[:pr_format.FALLBACK_QUESTIONS - len(out)]:
            add({'path': path, 'line_start': a, 'line_end': b, 'hunk': k, 'what': '', 'money': False})
    return out


def place_text(idx, n, p):
    hunks = (idx.get(p['path']) or {}).get('hunk_text') or []
    k = p.get('hunk')
    text = '\n'.join(hunks[k]) if k is not None and k < len(hunks) else '(the change is outside the diff)'
    seen = f" (the rules noted: {p['what']})" if p.get('what') and p['what'] != pr_format.NOT_SUMMARIZED else ''
    return f"PLACE {n} (new lines {p['line_start']}-{p['line_end']}){seen}:\n{text[:MAX_HUNK_CHARS]}"


def words(t):
    return set(re.findall(r'[a-z]{4,}', (t or '').lower())) - pr_format.ECHO_STOP


def check(a, asked=()):
    """The rule ids an answer breaks ([] = it may go out): the gate's own field rules for an item (R4, R5,
    R6, R10, R13, R25, R29), plain words, not a stock question, and not the same as a question already
    kept (`asked`)."""
    if not isinstance(a, dict):
        return ['none']
    ch, q, r = ((a.get(k) or '').strip() for k in ('change', 'question', 'reason'))
    why = [x.split(':')[0] for x in pr_format.change_reasons('it', ch)]
    why += [x.split(':')[0] for f, v in (('ask', q), ('why', r)) for x in pr_format.field_reasons('it', f, v)]
    if not q.endswith('?') or q.count('?') != 1:
        why.append('R13')
    if len(q.split()) > MAX_ASK:
        why.append('R4')
    if len(r.split()) > MAX_REASON:
        why.append('reason')
    if pr_format.IDENT_RE.search(q) or pr_format.IDENT_RE.search(r) or '/' in q:
        why.append('plain')
    if pr_format.STOCK_RE.search(q):
        why.append('stock')
    w = words(q)
    if any(len(w & words(o)) >= 0.6 * max(1, min(len(w), len(words(o)))) for o in asked):
        why.append('same')
    return sorted(set(why))


def summary_ok(s):
    """The "What it does" sentence passes: the gate's field rules, plain words, one sentence of at most
    MAX_SUMMARY words that asks nothing."""
    s = (s or '').strip()
    return bool(s) and not pr_format.field_reasons('it', 'summary', s) and len(s.split()) <= MAX_SUMMARY and \
        '?' not in s and not pr_format.IDENT_RE.search(s) and '/' not in s


def item(p, a):
    """A question as a review item on the place's lines: the body states the ask, the inline comment its
    reason (R4)."""
    end = min(p['line_end'], p['line_start'] + RANGE_MOST - 1)
    return {'path': p['path'], 'side': 'new', 'line_start': p['line_start'], 'line_end': end, 'kind': 'question',
            'severity': 'low', 'area': 'other', 'change': a['change'].strip().rstrip('.'), 'why': a['reason'].strip(),
            'ask': a['question'].strip(), 'rule': '', 'verified': 'fallback question', 'confidence': 7,
            'money': bool(p.get('money'))}


def ask(cfg, idx, todo_places, answer=None, most=None, title='', description=''):
    """Questions for the first `most` places (FALLBACK_QUESTIONS), at most TURNS turns: the second asks
    again only for the places whose answer failed, with why. `answer(prompt)` -> (structured output or
    None, meta); the judge model by default. Returns (items, the other places, the "What it does" sentence
    or '', meta: counts, rule ids and cost only, never the model's text)."""
    most = most or pr_format.FALLBACK_QUESTIONS
    todo = todo_places[:most]
    answer = answer or (lambda prompt: pr_judge.ask(cfg, prompt, SCHEMA, cfg['judge_model'], cfg['judge_max_turns']))
    got, failed, short, meta, summary = {}, {}, {}, {'asked': len(todo), 'calls': 0, 'cost_usd': 0}, ''
    for _ in range(TURNS):
        need = [j for j in range(len(todo)) if j not in got]
        if not need:
            break
        why = [WHY.get(c, c) for c in sorted({c for j in need for c in failed.get(j, [])})]
        why += [COUNTS.format(n=n, k=short[j]) for n, j in enumerate(need, 1) if 'R4' in failed.get(j, [])]
        prompt = PROMPT.format(count=len(need), again=AGAIN.format(why='; '.join(why)) if why else '',
                               title=title or '(none)', description=(description or '(none)')[:MAX_PR_TEXT],
                               places='\n\n'.join(place_text(idx, n, todo[j]) for n, j in enumerate(need, 1)))
        so, m = answer(prompt)
        meta['calls'] += 1
        meta['cost_usd'] += (m or {}).get('cost_usd') or 0
        if not summary and summary_ok((so or {}).get('summary')):
            summary = so['summary'].strip()
        by_n = {x.get('n'): x for x in (so or {}).get('places') or [] if isinstance(x, dict)}
        for n, j in enumerate(need, 1):
            a, bad, short[j] = pick(by_n.get(n), [got[k]['ask'] for k in sorted(got)])
            if bad:
                failed[j] = bad
            else:
                got[j] = item(todo[j], a)
                failed.pop(j, None)
    items = [got[j] for j in sorted(got)]
    others = [p for j, p in enumerate(todo) if j not in got] + list(todo_places[most:])
    meta.update(kept=len(items), failed=sorted({c for w in failed.values() for c in w}),
                ask_words=[len(i['ask'].split()) for i in items], summary=bool(summary))
    return items, others, summary, meta


def pick(a, asked=()):
    """The first of an answer's lead wordings that passes R29/R10, then the first question wording that
    passes check() with it (best first): (the answer with those, [], the question's word count); else
    (None, the first failing rule ids, the shortest question wording's word count)."""
    if not isinstance(a, dict):
        return None, ['none'], 0
    def wordings(k):
        return [v.strip() for v in a.get(k) or [] if isinstance(v, str) and v.strip()][:VERSIONS]
    leads, versions = wordings('changes'), wordings('questions')
    lead = next((c for c in leads if not pr_format.change_reasons('it', c)), leads[0] if leads else '')
    first = None
    for q in versions:
        bad = check(dict(a, change=lead, question=q), asked)
        if not bad:
            return dict(a, change=lead, question=q), [], len(q.split())
        first = first or bad
    return None, first or ['none'], min((len(q.split()) for q in versions), default=0)


def selftest():
    """The answers' checks and the turns on synthetic data, with a stub for the model."""
    diff = ''.join(f'diff --git a/src/b{k}.ts b/src/b{k}.ts\n--- a/src/b{k}.ts\n+++ b/src/b{k}.ts\n'
                   f'@@ -1,2 +1,{2 + k} @@\n x\n' + ''.join(f'+fee({j})\n' for j in range(k + 1)) + ' y\n' for k in range(5))
    diff += 'diff --git a/test/b.test.ts b/test/b.test.ts\n--- a/test/b.test.ts\n+++ b/test/b.test.ts\n' \
            '@@ -1,1 +1,30 @@\n x\n' + '+it()\n' * 29
    idx = pr_format.diff_index(diff)
    good = [{'n': 1, 'change': 'Late fee after thirty days', 'question': 'Overdue invoices now get a fee after thirty '
             'days on every plan; should the free plan be exempt?', 'reason': 'Free plan customers would be charged for '
             'the first time, without notice.'},
            {'n': 2, 'change': 'Credit expiry moved earlier', 'question': 'Unused credits now expire after six months '
             'instead of twelve; do current balances keep the old date?', 'reason': 'Customers who bought credits last '
             'year could lose them without warning.'}]
    stock = {'n': 3, 'change': 'Discount rounding changed', 'question': 'Was this change tested?',
             'reason': 'A wrong rounding rule would overcharge some customers on every invoice.'}
    money = [{'path': f'src/b{k}.ts', 'line_start': 2, 'line_end': 2 + k, 'hunk': 0, 'what': 'model line', 'risk': k + 1,
              'behaviour': k != 1} for k in range(5)]
    ps = places(idx, money, [])
    checks = {'money places come riskiest first, a place that changes no behaviour last':
              [p['path'] for p in ps] == ['src/b4.ts', 'src/b3.ts', 'src/b2.ts', 'src/b0.ts', 'src/b1.ts'],
              'without money or flags, the largest hunks outside tests':
              [p['path'] for p in places(idx, [], [])] == ['src/b4.ts', 'src/b3.ts', 'src/b2.ts'],
              'the note fills one flagged place up with the largest other hunks':
              [p['path'] for p in places(idx, [], [{'path': 'src/b0.ts', 'line': 2, 'label': 'x'}], fill=True)] ==
              ['src/b0.ts', 'src/b4.ts', 'src/b3.ts'],
              'the money review does not fill': len(places(idx, money[:1], [])) == 1}
    checks['a good answer passes'] = check(good[0]) == []
    checks['a stock question fails'] = 'stock' in check(stock)
    checks['the same question twice fails'] = 'same' in check(good[0], [good[0]['question']])
    checks['a code name in the change fails'] = 'R29' in check(dict(good[0], change='Calls applyLateFee now'))
    checks['an ask over 20 words fails'] = 'R4' in check(dict(good[0], question=' '.join(['word'] * 21) + '?'))
    checks['a question without "?" fails'] = 'R13' in check(dict(good[0], question='Tell me about the fee.'))
    prompts, long = [], ' '.join(['word'] * 24) + '?'

    def wordings(a, *qs, leads=None):
        return dict({k: v for k, v in a.items() if k not in ('question', 'change')}, questions=list(qs),
                    changes=leads or [a['change']])
    what = 'Overdue invoices get a late fee, and unused credits expire sooner.'

    def stub(prompt):
        prompts.append(prompt)
        if len(prompts) == 1:  # place 1: the first wordings fail, the second pass; places 2 and 3 fail
            return {'summary': 'It calls applyLateFee() now.',
                    'places': [wordings(good[0], long, good[0]['question'], leads=['Calls applyLateFee now', good[0]['change']]),
                               wordings(good[1], long, long + ' x'), wordings(stock, stock['question'])]}, {'cost_usd': 0.01}
        return {'summary': what, 'places': [wordings(dict(good[1], n=1), good[1]['question']),
                                            wordings(dict(stock, n=2), 'Does rounding now go up for every invoice line, '
                                                     'so totals rise by a cent?')]}, {'cost_usd': 0.01}
    items, others, summary, meta = ask({}, idx, ps, stub, title='Billing changes', description='Fees and credits.')
    checks['two turns: the second only for the places that failed, with why and its word count'] = \
        len(prompts) == 2 and prompts[1].count('PLACE ') == 2 and 'fits any pull request' in prompts[1] and \
        'for place 1 your shortest question was 24 words' in prompts[1] and meta['calls'] == 2
    checks['the first lead and question wordings that pass are taken'] = items[0]['ask'] == good[0]['question'] and \
        items[0]['change'] == good[0]['change'] and meta['ask_words'][0] == len(good[0]['question'].split())
    checks['the turn reads the title and description; a "What it does" that names code is dropped, the next taken'] = \
        'TITLE: Billing changes' in prompts[0] and 'Fees and credits.' in prompts[0] and summary == what and \
        meta['summary'] is True
    checks['three questions, the rest are other places'] = len(items) == 3 and [p['path'] for p in others] == \
        ['src/b0.ts', 'src/b1.ts'] and meta['kept'] == 3 and meta['failed'] == []
    checks['each question is an item on its place, in the order of risk'] = [i['path'] for i in items] == \
        ['src/b4.ts', 'src/b3.ts', 'src/b2.ts'] and all(i['kind'] == 'question' and i['money'] for i in items)
    checks['an item never spans more than 40 lines'] = all(i['line_end'] - i['line_start'] < RANGE_MOST for i in items)
    none, rest, _, m2 = ask({}, idx, ps, lambda p: (None, {'cost_usd': 0}))
    checks['no answer: no items, every place is another place, two calls'] = none == [] and len(rest) == 5 and \
        m2['calls'] == 2 and m2['failed'] == ['none']
    for k, v in checks.items():
        print('ok  ' if v else 'FAIL', k)
    return all(checks.values())


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        sys.exit(0 if selftest() else 1)
    print(__doc__)
