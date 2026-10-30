#!/usr/bin/env python3
"""The money lines (style guide R32): one neutral line per changed place in a not-checked area (billing,
plans, credits or pricing) that says what the change does to charges, credits, plans or limits. It
describes and never judges. It reads only those hunks, with no tools, so it holds even when the full
review is held. Capture is off, and nothing it reads or writes is printed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_judge  # noqa: E402

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['lines'], 'properties': {
    'lines': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['n', 'kind', 'what'],
        'properties': {'n': {'type': 'integer', 'minimum': 1},
                       'kind': {'type': 'string', 'enum': ['charges', 'credits', 'plans', 'limits', 'other']},
                       'what': {'type': 'string', 'maxLength': 200}}}}}}
MAX_PLACES = 20  # places per call; a PR with more gets more calls (every place gets its line)

PROMPT = """Below are {count} changed places in a pull request that touch {words}. For each place, write
ONE neutral sentence of at most 20 words saying what the change does to charges, credits, plans or
limits: for example that one is added, removed, recalculated, or now skipped in some case. Describe it;
never judge whether it is right and never recommend anything. Never name files, functions or variables:
use words a person who does not read code understands. If a place changes none of these, say plainly
what it does change. Answer with `n` (the place's number), `kind` (what it is mostly about: charges,
credits, plans, limits, or other) and `what`, for every place.

{places}"""

MAX_HUNK_CHARS = 6000


def describe(cfg, idx, ranges, words):
    """`ranges` [{path, line_start, line_end, hunk}] (pr_format.money_ranges) -> the same with `what` (''
    where the model gave none) and `kind`, and the call's meta (cost)."""
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
    return [dict(r, what=(got.get(n) or {}).get('what', '').strip(), kind=(got.get(n) or {}).get('kind', 'other'))
            for n, r in enumerate(ranges, 1)], meta
