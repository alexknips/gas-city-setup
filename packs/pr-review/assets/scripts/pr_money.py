#!/usr/bin/env python3
"""The money lines (style guide R32): one neutral line per changed place in a not-checked area (billing,
plans, credits or pricing) that says what the change does to charges, credits, plans or limits, with its
kind, whether it changes behaviour and how risky it is; plus one sentence on what the whole change does
and its default state, and the question on its concrete effect after merge. It describes and never
judges. It reads only those hunks, with no tools, so it holds even when the full review is held. Capture
is off, and nothing it reads or writes is printed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_judge  # noqa: E402

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['overview', 'question', 'lines'], 'properties': {
    'overview': {'type': 'string', 'maxLength': 240, 'description': (
        'One sentence of at most 30 words: what the change does to money overall, and its default state after '
        'merge (on or off by default, for which accounts).')},
    'question': {'type': 'string', 'maxLength': 240, 'description': (
        'ONE question ending in "?", at most 30 words: the concrete effect after merge with the default '
        'setting, then who confirms it, e.g. "After merge every clinic pays one SMS credit per reminder by '
        'default; who confirms that before it ships?"')},
    'lines': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['n', 'kind', 'what', 'behaviour', 'risk'],
        'properties': {'n': {'type': 'integer', 'minimum': 1},
                       'kind': {'type': 'string', 'enum': ['charges', 'credits', 'plans', 'limits', 'other']},
                       'what': {'type': 'string', 'maxLength': 200},
                       'behaviour': {'type': 'boolean', 'description': (
                           'False when the place changes no behaviour: a comment, a rename, a type, a test '
                           'helper, a no-op.')},
                       'risk': {'type': 'integer', 'minimum': 1, 'maximum': 5, 'description': (
                           'How much a mistake here could cost customers or the company, 5 = most.')}}}}}}
MAX_PLACES = 20  # places per call; a PR with more gets more calls (every place gets its line)

PROMPT = """Below are {count} changed places in a pull request that touch {words}. For each place, write
ONE neutral sentence of at most 20 words saying what the change does to charges, credits, plans or
limits: for example that one is added, removed, recalculated, or now skipped in some case. Describe it;
never judge whether it is right and never recommend anything. Never name files, functions or variables:
use words a person who does not read code understands. If a place changes none of these, say plainly
what it does change. For every place give `n` (its number), `kind` (what it is mostly about: charges,
credits, plans, limits, or other), `what`, `behaviour` (false for a comment, a rename, a type or a no-op)
and `risk` (1-5).

Then `overview`: one sentence on what the whole change does to money and its default state after merge
(is it on by default, for which accounts). And `question`: the one question on the concrete effect after
merge with the default setting, and who confirms it.

{places}"""

MAX_HUNK_CHARS = 6000


def describe(cfg, idx, ranges, words):
    """`ranges` [{path, line_start, line_end, hunk}] (pr_format.money_ranges) -> the same with `what` (''
    where the model gave none), `kind`, `behaviour` and `risk`; and the call's meta (cost, overview,
    question)."""
    if not ranges:
        return [], {'cost_usd': 0}
    places = []
    for n, r in enumerate(ranges, 1):
        hunks = (idx.get(r['path']) or {}).get('hunk_text') or []
        k = r.get('hunk')
        text = '\n'.join(hunks[k]) if k is not None and k < len(hunks) else '(the change is outside the diff)'
        places.append(f"PLACE {n} (new lines {r['line_start']}-{r['line_end']}):\n{text[:MAX_HUNK_CHARS]}")
    prompt = PROMPT.format(count=len(ranges), words=words, places='\n\n'.join(places))
    so, meta = pr_judge.ask(cfg, prompt, SCHEMA, cfg['judge_model'], cfg['judge_max_usd'])
    got = {x['n']: x for x in (so or {}).get('lines', []) if isinstance(x.get('n'), int)}
    meta.update(overview=((so or {}).get('overview') or '').strip(), question=((so or {}).get('question') or '').strip())
    out = []
    for n, r in enumerate(ranges, 1):
        g = got.get(n) or {}
        out.append(dict(r, what=(g.get('what') or '').strip(), kind=g.get('kind', 'other'),
                        behaviour=g.get('behaviour', True), risk=g.get('risk', 3)))
    return out, meta
