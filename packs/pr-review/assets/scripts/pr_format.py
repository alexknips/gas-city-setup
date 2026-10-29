#!/usr/bin/env python3
"""Review format and gate (assets/style-guide.md). Pure functions, no network.

  diff_index(diff)                   which lines of which file an entry may point at
  blockers(repo, pr, ci, ci_bad)     a merge conflict or CI that did not pass, as linked asks for the author (R22)
  decide(decisions, items, blockers) the outcome
  render(repo, pr, rv, name, author) the review body (markdown), honesty line first ...
  inline_comments(items, author)     ... and one inline comment per item, on its lines (R4)
  gate(rv, idx, body, ...)           the reasons a review breaks the style guide ([] = it may be delivered)
  gate_approve(...)                  the reasons a low-risk review may not be an APPROVE (R27)

A review `rv` is a dict: outcome, approve (bool), decisions, items, blockers, cleared, safe_to_skip, facts,
not_checked (the words for a not-checked area the PR touches, or ''), model (whether a model read the PR).

`pr_format.py --regress DIR...` runs the gate over regression fixtures (README.md, "Quality loop").
"""
import hashlib
import json
import os
import re
import sys

AREAS = ['patient data', 'security', "can't be undone", 'migration order', 'behaviour patients notice',
         'product', 'compliance', 'other']
NOT_CHECKED_AREA = 'not checked'  # the area of the decision R20 adds; the model never uses it
KINDS = {'question': 'Ask {author}', 'change': 'Change'}
# Line 1 of every review (it is posted under the reviewer's login, so the author must see it is automated).
HONEST = "Automated {what} by {name}'s review agents (Claude Code); {name} hasn't read this PR yet."
HONEST_RE = re.compile(r"^Automated (review|approval) by .+'s review agents \(Claude Code\); .+ hasn't read this PR yet\.$")
MARKERS = ('Automated review', 'Automated approval')
WORDS = {'escalate': 'Needs you', 'changes': 'Changes requested', 'questions': 'Questions', 'low-risk': 'Low risk'}
APPROVED = 'Approved as low risk'
MAX_ITEMS, MAX_DECISIONS, MAX_FIELD, MAX_BODY = 5, 3, 300, 6000
MAX_WHERE = 5  # files a not-checked decision links to (R20)

# R6: phrases that carry no information on their own. Matched case-insensitively in every text field.
FILLER = [r'\bmake sure\b', r'\bconsider (reviewing|checking|adding)\b', r'\bplease (review|check|verify)\b',
          r'\bbest practices?\b', r'\bpotential(ly)? (issues?|problems?|risks?)\b', r'\blooks? good\b',
          r'\bgreat (job|work)\b', r'\bit is (important|recommended|advisable) to\b', r'\bin general\b',
          r'\boverall\b', r'\bas needed\b', r'\bdouble[- ]check\b', r'\bkeep in mind\b', r'\bshould be reviewed\b',
          r'\bmay want to\b', r'\bmight want to\b', r'\bverify (that )?everything\b']
FILLER_RE = re.compile('|'.join(FILLER), re.I)
# R10, R23: rule codes and ids that must not appear in the text.
CODES_RE = re.compile(r'\b(A[1-7]|B[1-4]|R\d{1,2}|F\d{1,2}|ci-(red|none|skipped)|phi-[a-z-]+|sec-[a-z-]+|irrev-[a-z-]+|'
                      r'migration-(order|edit)|no-description|untested)\b')
# R25: what clean() leaves where it cut a link or an image out of the model's text.
CUT_RE = re.compile(r'\[(link|image) removed\]')
# R1, R22: the outcome word, then who acts: "**Changes requested. Alex: nothing. Nils: 1 change, get CI running.**"
LINE1_RE = re.compile(r'^\*\*(Needs you|Changes requested|Questions|Low risk|Approved as low risk)\. '
                      r'(.+?): (nothing|(\d+) decisions?)( \((.+)\))?\. (.+?): (.+)\.\*\*$')


def diff_index(diff):
    """{path: {'new': set, 'old': set, 'hunks': [(new set, old set)]}} for every file in a unified diff,
    counting context lines on both sides."""
    idx, cur, hunk, new_l, old_l = {}, None, None, 0, 0
    for raw in diff.splitlines():
        if raw.startswith('diff --git '):
            cur, hunk = None, None
            continue
        if raw.startswith('--- '):
            continue
        if raw.startswith('+++ '):
            cur = raw[6:] if raw.startswith('+++ b/') else None
            if cur is not None:
                idx.setdefault(cur, {'new': set(), 'old': set(), 'hunks': []})
            continue
        m = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', raw)
        if m and cur is not None:
            old_l, new_l = int(m.group(1)), int(m.group(2))
            hunk = (set(), set())
            idx[cur]['hunks'].append(hunk)
            continue
        if cur is None or hunk is None:
            continue
        if raw.startswith('+'):
            idx[cur]['new'].add(new_l); hunk[0].add(new_l); new_l += 1
        elif raw.startswith('-'):
            idx[cur]['old'].add(old_l); hunk[1].add(old_l); old_l += 1
        elif raw.startswith(' ') or raw == '':
            idx[cur]['new'].add(new_l); idx[cur]['old'].add(old_l)
            hunk[0].add(new_l); hunk[1].add(old_l)
            new_l += 1; old_l += 1
    return idx


def diff_link(repo, number, path, side, start, end):
    """The Files-view link to lines of a PR's diff: #diff-<sha256(path)>R40-R52 (L for the old side)."""
    s = 'R' if side == 'new' else 'L'
    rng = f'{s}{start}' if start == end else f'{s}{start}-{s}{end}'
    return f'https://github.com/{repo}/pull/{number}/files#diff-{hashlib.sha256(path.encode()).hexdigest()}{rng}'


def code_link(repo, head, path, start, end):
    """A permalink to lines of a file at the PR's head commit (R16)."""
    rng = f'L{start}' if start == end else f'L{start}-L{end}'
    return f'https://github.com/{repo}/blob/{head}/{path}#{rng}'


def decision_link(repo, pr, d):
    ln = d.get('link') or {}
    if ln.get('kind') == 'code':
        return code_link(repo, pr['headRefOid'], ln.get('path', ''), ln.get('line_start', 1), ln.get('line_end', 1))
    if ln.get('kind') == 'description':
        return f"https://github.com/{repo}/pull/{pr['number']}"
    return ln.get('url') or ''


def rng_text(e):
    return f"L{e['line_start']}" if e['line_start'] == e['line_end'] else f"L{e['line_start']}-L{e['line_end']}"


def link_text(d):
    ln = d.get('link') or {}
    if ln.get('kind') == 'code':
        s, e = ln.get('line_start'), ln.get('line_end')
        return f"`{ln.get('path')}` L{s}" + ('' if s == e else f'-L{e}')
    return {'description': 'the PR description', 'ticket': 'the ticket'}.get(ln.get('kind'), 'the source')


def hunk_of(idx, path, line):
    """The index of the hunk whose new side holds `line`, or None."""
    for k, h in enumerate((idx.get(path) or {}).get('hunks', [])):
        if line in h[0]:
            return k
    return None


def in_diff(idx, e, hunk=None):
    """Both ends of the entry's range are inside one hunk (that hunk, when given) on the side it names."""
    f = idx.get(e.get('path'))
    if not f:
        return False
    k = 0 if e.get('side', 'new') == 'new' else 1
    hunks = f['hunks'] if hunk is None else f['hunks'][hunk:hunk + 1]
    return any(e['line_start'] in h[k] and e['line_end'] in h[k] for h in hunks)


def blockers(repo, pr, ci, ci_bad=()):
    """A merge conflict and CI that did not pass, as linked asks for the author (R22). `kind` decides the
    outcome word (a conflict or red CI is a change); `short` goes into line 1."""
    n, out = pr['number'], []
    checks = f'https://github.com/{repo}/pull/{n}/checks'
    if pr.get('mergeable') == 'CONFLICTING':
        out.append({'kind': 'change', 'short': 'resolve the merge conflict', 'label': 'Resolve the merge conflict',
                    'url': f'https://github.com/{repo}/pull/{n}/conflicts', 'detail': 'GitHub reports one with the base branch.'})
    if ci == 'red':
        names = ', '.join(ci_bad[:4]) or 'see the checks'
        out.append({'kind': 'change', 'short': 'fix the failing checks', 'label': 'Fix the failing checks',
                    'url': checks, 'detail': f'{names}.'})
    elif ci == 'none':
        out.append({'kind': 'question', 'short': 'get CI running', 'label': 'Get CI running', 'url': checks,
                    'detail': 'no check ran on this commit, so nothing has tested it.'})
    elif ci == 'skipped':
        out.append({'kind': 'question', 'short': 'get the checks to run', 'label': 'Get the checks to run', 'url': checks,
                    'detail': 'every check on this commit was skipped, so nothing has tested it.'})
    elif ci == 'pending':
        out.append({'kind': 'question', 'short': 'get CI to finish', 'label': 'Get CI to finish', 'url': checks,
                    'detail': 'it was still running when this review ran.'})
    return out


def decide(decisions, items, blockers_=()):
    """R16: Needs you only with a decision only the reviewer can make. Otherwise changes (a medium or high
    change item, or any change), questions (only questions), or low risk (nothing at all: no item, no
    blocker; a blocker means CI did not pass or the branch conflicts)."""
    if decisions:
        return 'escalate'
    asks = list(items) + list(blockers_)
    if not asks:
        return 'low-risk'
    return 'questions' if all(a['kind'] == 'question' for a in asks) else 'changes'


def plural(n, one, many):
    return f'{n} {one if n == 1 else many}'


def facts_line(pr, ci, ci_bad):
    c = {'green': 'CI passed', 'red': f"CI red: {', '.join(ci_bad[:6]) or 'see checks'}",
         'none': 'No CI ran', 'skipped': 'CI skipped (no check passed)', 'pending': 'CI still running'}[ci]
    conflict = 'merge conflict' if pr.get('mergeable') == 'CONFLICTING' else 'no merge conflict'
    state = {'MERGED': 'merged', 'OPEN': 'open', 'CLOSED': 'closed'}.get(pr['state'], pr['state'].lower())
    return f'{c} · {conflict} · {state}'


def who_acts(rv, name, author):
    """R22: who must do what, in line 1."""
    d = rv['decisions']
    you = plural(len(d), 'decision', 'decisions') if d else 'nothing'
    if rv.get('not_checked'):
        you += f" ({rv['not_checked']} not checked)"
    n_change = sum(1 for i in rv['items'] if i['kind'] == 'change')
    n_q = sum(1 for i in rv['items'] if i['kind'] == 'question')
    parts = ([plural(n_change, 'change', 'changes')] if n_change else []) + \
            ([plural(n_q, 'question', 'questions')] if n_q else []) + [b['short'] for b in rv['blockers']]
    return f"{name}: {you}. {author}: {', '.join(parts) or 'nothing'}."


def headline(rv, name, author):
    word = APPROVED if rv.get('approve') else WORDS[rv['outcome']]
    return f'**{word}. {who_acts(rv, name, author)}**'


def render(repo, pr, rv, name, author):
    """The review body (markdown): the honesty line; the outcome and who acts; the decisions for the
    reviewer with their links; the asks for the author with links to their lines (each item is also an
    inline comment) and the blockers with theirs; each fired rule cleared with a link to its lines (R19);
    safe to skip; the facts line; the not-checked footer only when the PR touches that area (R20, R23)."""
    lines = [HONEST.format(what='approval' if rv.get('approve') else 'review', name=name), '',
             headline(rv, name, author), '']
    if rv['decisions']:
        lines.append(f'**For {name}:**')
        for k, d in enumerate(rv['decisions'], 1):
            area = '' if d['area'] == NOT_CHECKED_AREA else f" · {d['area']}"
            opener = '' if d.get('where') else f" · [open {link_text(d)}]({decision_link(repo, pr, d)})"
            lines.append(f"{k}. **{d['question']}**{area}{opener}")
            if d.get('where'):
                w = d['where']
                links = [f"[`{x['path']}` L{x['line']}]({code_link(repo, pr['headRefOid'], x['path'], x['line'], x['line'])})"
                         for x in w[:MAX_WHERE]]
                more = f', and {plural(len(w) - MAX_WHERE, "more file", "more files")}' if len(w) > MAX_WHERE else ''
                lines.append(f"   **Where:** {', '.join(links)}{more}")
            if d.get('what'):
                lines.append(f"   **What changes (not checked):** {d['what']}")
            lines += [f"   **Why you:** {d['why']}",
                      f"   **Options:** {'; '.join(d['options'])}",
                      f"   **Recommended:** {d['recommendation']}"]
        lines.append('')
    if rv['items'] or rv['blockers']:
        lines.append(f'**For {author}:**')
        k = 0
        for k, it in enumerate(rv['items'], 1):
            old = ' (removed lines)' if it.get('side') == 'old' else ''
            url = diff_link(repo, pr['number'], it['path'], it.get('side', 'new'), it['line_start'], it['line_end'])
            label = KINDS[it['kind']].format(author=author)
            lines += [f"{k}. [`{it['path']}` {rng_text(it)}{old}]({url}) · {it['area']}",
                      f"   **Changed:** {it['what']}",
                      f"   **Why it matters:** {it['why']}",
                      f"   **{label}:** {it['ask']}"]
        for j, b in enumerate(rv['blockers'], k + 1):
            lines.append(f"{j}. [{b['label']}]({b['url']}): {b['detail']}")
        lines.append('')
    if rv.get('cleared'):
        lines.append('**Checked and cleared:**')
        for c in rv['cleared']:
            url = diff_link(repo, pr['number'], c['path'], c.get('side', 'new'), c['line_start'], c['line_end'])
            lines.append(f"- {c['label']} at [`{c['path']}` {rng_text(c)}]({url}): {c['why']}")
        lines.append('')
    if rv.get('safe_to_skip'):
        lines += [f"**Safe to skip:** {rv['safe_to_skip']}", '']
    lines.append(rv['facts'])
    if rv.get('not_checked'):
        lines += ['', f"<sub>Not checked: {rv['not_checked']}.</sub>"]
    return '\n'.join(lines)


def inline_comments(items, author):
    """One inline comment per item, on its exact lines (R4); a range becomes a multi-line comment."""
    out = []
    for k, it in enumerate(items, 1):
        side = 'RIGHT' if it.get('side', 'new') == 'new' else 'LEFT'
        label = KINDS[it['kind']].format(author=author)
        c = {'path': it['path'], 'line': it['line_end'], 'side': side,
             'body': f"**{k}. {it['area']}**: {it['what']} {it['why']}\n\n**{label}:** {it['ask']}"}
        if it['line_start'] != it['line_end']:
            c.update(start_line=it['line_start'], start_side=side)
        out.append(c)
    return out


def not_checked_decision(located, words, author, summary=''):
    """R20: the PR touches an area no agent checks, so the reviewer decides whether to look. `located`:
    [{path, line}] where the not-checked rule matched; the decision links the first."""
    first, where = located[0], []
    for x in located:  # the first line of each file, in order
        if x['path'] not in {w['path'] for w in where}:
            where.append({'path': x['path'], 'line': x['line']})
    return {'question': f'Who reviews the {words} changes before this PR merges?',
            'options': ['You review the lines under Where yourself',
                        f'{author} first writes in the PR description what changes for customers, then you decide',
                        'A person who owns billing reviews them'],
            'recommendation': f'You review the lines under Where yourself: {plural(len(where), "file", "files")}.',
            'why': 'No agent checks money logic, so a person has to clear it.',
            'what': summary,
            'where': where,
            'area': NOT_CHECKED_AREA,
            'link': {'kind': 'code', 'path': first['path'], 'line_start': first['line'], 'line_end': first['line']},
            'verified': 'zero-token rule', 'confidence': 10, 'auto': True}


def field_reasons(p, f, v):
    why, v = [], (v or '').strip()
    if len(v.split()) < 4:
        why.append(f'R5: {p} {f} is empty or under 4 words')
    elif len(v) > MAX_FIELD:
        why.append(f'R14: {p} {f} is over {MAX_FIELD} characters')
    m = FILLER_RE.search(v)
    if m:
        why.append(f'R6: {p} {f} has filler ("{m.group(0)}")')
    c = CODES_RE.search(v)
    if c:
        why.append(f'R10: {p} {f} has a rule code ("{c.group(0)}")')
    if CUT_RE.search(v):
        why.append(f'R25: {p} {f} had a link to another host; rewrite the sentence without it')
    return why


def covers(idx, flag, entries):
    """R19: an entry on the flag's file whose range lies in the flag's hunk (a decision's code link may
    reach outside the hunk but must overlap it). A flag outside every hunk takes any entry on its file."""
    h = hunk_of(idx, flag['path'], flag['line'])
    for e in entries:
        if e.get('path') != flag['path'] or not isinstance(e.get('line_start'), int) or not isinstance(e.get('line_end'), int):
            continue
        if h is None:
            return True
        if e.get('_decision'):
            if any(ln in idx[flag['path']]['hunks'][h][0] for ln in range(e['line_start'], e['line_end'] + 1)):
                return True
        elif in_diff(idx, e, h):
            return True
    return False


def gate_entries(rv, idx, tree_lines, pr_text, flagged=()):
    """Reasons the review's entries break the style guide. tree_lines: {path: line count} at the head
    (None = not checked); pr_text: the PR description, where a ticket link must appear; flagged:
    [{id, rule, label, path, line}], the content rules that fired, each of which must be cleared or
    become an item or a decision (R19)."""
    why = []
    decisions, items, outcome = rv['decisions'], rv['items'], rv['outcome']
    if len(items) > MAX_ITEMS:
        why.append(f'R2: {len(items)} items, at most {MAX_ITEMS}')
    if len(decisions) > MAX_DECISIONS:
        why.append(f'R16: {len(decisions)} decisions, at most {MAX_DECISIONS}')
    if outcome == 'low-risk' and (items or decisions or rv['blockers']):
        why.append('R2: low risk with items, decisions or blockers')
    if outcome == 'escalate' and not decisions:
        why.append('R16: needs you without a decision for the reviewer')
    if rv.get('approve') and outcome != 'low-risk':
        why.append(f'R27: an approval for a {outcome} review')
    for k, d in enumerate(decisions, 1):
        p = f'decision {k}'
        q = (d.get('question') or '').strip()
        if not q.endswith('?') or q.count('?') != 1:
            why.append(f'R16: {p} is not one question ending in "?"')
        if len(d.get('options') or []) < 2:
            why.append(f'R16: {p} has fewer than 2 options')
        for f in ('question', 'why', 'recommendation') + (('what',) if d.get('what') else ()):
            why += field_reasons(p, f, d.get(f))
        ln = d.get('link') or {}
        if ln.get('kind') == 'code':
            n = (tree_lines or {}).get(ln.get('path'))
            if tree_lines is not None and n is None:
                why.append(f"R16: {p} links {ln.get('path')}, which is not a file at the head")
            elif not (isinstance(ln.get('line_start'), int) and isinstance(ln.get('line_end'), int)) or \
                    ln['line_start'] < 1 or ln['line_end'] < ln['line_start'] or (n is not None and ln['line_end'] > n):
                why.append(f"R16: {p} lines L{ln.get('line_start')}-L{ln.get('line_end')} are not within the file")
        elif ln.get('kind') == 'ticket':
            if not ln.get('url') or ln['url'] not in (pr_text or ''):
                why.append(f'R16: {p} ticket link is not in the PR description')
        elif ln.get('kind') != 'description':
            why.append(f'R16: {p} has no link to code or text')
        if d.get('area') not in AREAS + [NOT_CHECKED_AREA]:
            why.append(f'R16: {p} area is not one of {AREAS}')
    # R20: a not-checked area is said on line 1 and is a decision for the reviewer; never otherwise.
    nc = [d for d in decisions if d.get('area') == NOT_CHECKED_AREA]
    if rv.get('not_checked') and (outcome != 'escalate' or not nc):
        why.append(f"R20: the PR touches {rv['not_checked']}, which is not checked, so it is a decision for the reviewer")
    if nc and not rv.get('not_checked'):
        why.append('R20: a not-checked decision on a PR that does not touch that area')
    seen = []
    for k, it in enumerate(items, 1):
        p = f'item {k}'
        if not it.get('path') or not isinstance(it.get('line_start'), int) or not isinstance(it.get('line_end'), int):
            why.append(f'R3: {p} has no path or line range'); continue
        if it['line_start'] > it['line_end'] or it['line_end'] - it['line_start'] > 80:
            why.append(f"R3: {p} range L{it['line_start']}-L{it['line_end']} is reversed or longer than 80 lines")
        elif not in_diff(idx, it):
            why.append(f"R3: {p} {it['path']} L{it['line_start']}-L{it['line_end']} ({it.get('side', 'new')} side) "
                       f'is not inside one hunk of the diff')
        for f in ('what', 'why', 'ask'):
            why += field_reasons(p, f, it.get(f))
        if it.get('area') not in AREAS:
            why.append(f'R5: {p} area is not one of {AREAS}')
        if it.get('kind') == 'question' and not (it.get('ask') or '').rstrip().endswith('?'):
            why.append(f'R13: {p} is a question but its ask does not end with a question mark')
        # R21: each finding once: no two items on overlapping lines of a file, no repeated sentence.
        for j, o in seen:
            if o['path'] == it['path'] and o.get('side', 'new') == it.get('side', 'new') and \
                    o['line_start'] <= it['line_end'] and it['line_start'] <= o['line_end']:
                why.append(f'R21: {p} repeats the lines of item {j}; say each finding once')
            elif (o.get('what') or '').strip().lower() == (it.get('what') or '').strip().lower():
                why.append(f'R21: {p} repeats item {j}; say each finding once')
        seen.append((k, it))
    by_id = {f['id']: f for f in flagged}
    for k, c in enumerate(rv.get('cleared') or [], 1):
        p = f'cleared entry {k}'
        f = by_id.get(c.get('flag'))
        if f is None:
            why.append(f"R19: {p} names flag {c.get('flag')!r}, which is not one of the flagged hunks"); continue
        if c.get('path') != f['path'] or not isinstance(c.get('line_start'), int) or not isinstance(c.get('line_end'), int) \
                or c['line_start'] > c['line_end']:
            why.append(f"R19: {p} must name {f['path']} and a line range inside the hunk of line {f['line']}")
        elif hunk_of(idx, f['path'], f['line']) is not None and not in_diff(idx, dict(c, side='new'),
                                                                            hunk_of(idx, f['path'], f['line'])):
            why.append(f"R19: {p} range L{c['line_start']}-L{c['line_end']} is not inside the hunk of line {f['line']} "
                       f"in {f['path']}")
        why += [r.replace('R5:', 'R19:', 1) for r in field_reasons(p, 'why', c.get('why'))]
    entries = [dict(c, side='new') for c in (rv.get('cleared') or []) if c.get('flag') in by_id] + list(items) + \
              [dict(path=(d.get('link') or {}).get('path'), line_start=(d.get('link') or {}).get('line_start'),
                    line_end=(d.get('link') or {}).get('line_end'), _decision=True)
               for d in decisions if (d.get('link') or {}).get('kind') == 'code']
    for f in flagged:
        if not covers(idx, f, entries):
            why.append(f"R19: flag {f['id']} ({f['label']}, {f['path']} line {f['line']}) is neither cleared nor an "
                       f'item or a decision')
    return why


def gate_body(body, rv, comments=None):
    """Reasons the rendered body breaks the style guide (also runs on bodies delivered earlier: rv then
    needs only outcome, the counts and not_checked). With `comments`, each item has its inline comment (R4)."""
    why = []
    n_dec, n_items = len(rv.get('decisions') or []), len(rv.get('items') or [])
    n_block, n_clear = len(rv.get('blockers') or []), len(rv.get('cleared') or [])
    lines = body.splitlines()
    if not lines or not HONEST_RE.match(lines[0]):
        why.append('R1: line 1 is not the honesty line')
    elif ('approval' in lines[0].split(' by ')[0]) != bool(rv.get('approve')):
        why.append('R1: the honesty line says approval for a review that is not one, or the other way round')
    rest = [l for l in lines[1:] if l.strip()]
    m = LINE1_RE.match(rest[0] if rest else '')
    word = APPROVED if rv.get('approve') else WORDS.get(rv['outcome'])
    if not m:
        why.append(f'R22: line 1 does not say the outcome and who acts ("**{word}. <reviewer>: ... <author>: ...**")')
    else:
        if m.group(1) != word:
            why.append(f'R1: line 1 says "{m.group(1)}" for {rv["outcome"]}')
        if int(m.group(4) or 0) != n_dec:
            why.append(f'R22: line 1 counts {m.group(4) or 0} decisions, the review has {n_dec}')
        if bool(m.group(5)) != bool(rv.get('not_checked')):
            why.append('R20: line 1 must say when the PR touches a not-checked area, and only then')
        if (m.group(8) == 'nothing') != (n_items + n_block == 0):
            why.append(f'R22: line 1 says the author has {m.group(8)!r}, the review has {n_items + n_block} asks')
    if rv['outcome'] != 'low-risk' or n_clear:
        links = re.findall(r'\]\(https://github\.com/[^)]+/(?:pull/\d+/files#diff-[0-9a-f]{64}[RL]\d+(?:-[RL]\d+)?|'
                           r'blob/[0-9a-f]{40}/[^)#]+#L\d+(?:-L\d+)?|pull/\d+(?:/checks|/conflicts)?)\)', body)
        if not links or len(links) < n_items + n_dec + n_block + n_clear:
            why.append(f'R3: {len(links)} links to lines or text for {n_items + n_dec + n_block + n_clear} entries')
    if rv.get('model') and not re.search(r'^\*\*Safe to skip:\*\* \S', body, re.M):
        why.append('R7: no "Safe to skip" line')
    skip = re.search(r'^\*\*Safe to skip:\*\* (.*)$', body, re.M)
    if skip and len(skip.group(1)) > 240:
        why.append(f'R7: "Safe to skip" is {len(skip.group(1))} characters, at most 240')
    facts = re.compile(r'^(CI passed|CI red|No CI ran|CI skipped|CI still running).* · (no )?merge conflict · \w+$')
    fl = next((l for l in lines if facts.match(l)), None)
    if fl is None:
        why.append('R8: no facts line')
    elif m:  # R22: a blocker in the facts line is a linked ask for the author in line 1
        need = [short for cue, short in FACT_ASKS if cue in fl]
        missing = [x for x in need if x not in m.group(8)]
        if missing:
            why.append(f"R22: the facts line has a blocker but line 1 does not ask for it: {', '.join(missing)}")
    tail = [l for l in lines if l.strip()]
    last = tail[-1] if tail else ''
    if rv.get('not_checked'):
        if 'Not checked:' not in last:
            why.append('R20: the not-checked footer is not the last line')
    elif 'Not checked' in body:
        why.append('R23: the not-checked footer on a PR that does not touch that area')
    elif not facts.match(last):
        why.append('R9: the facts line is not the last line')
    if re.search(r'(?i)shadow mode|review agents', '\n'.join(lines[1:])):
        why.append('R9: boilerplate in the body')
    if '<details>' in body:
        why.append('R23: a rule-codes section in the body')
    if re.search(r'(?im)^\*\*Suggested reply', body):
        why.append('R21: a suggested reply repeats the findings')
    if CUT_RE.search(body):
        why.append('R25: a "[link removed]" fragment; rewrite the sentence without the link')
    if comments is not None and len(comments) != n_items:
        why.append(f'R4: {len(comments)} inline comments for {n_items} items')
    prose = re.sub(r'\]\([^)]*\)', ']', '\n'.join(lines[1:]))  # link targets carry R40-R52 anchors
    c = CODES_RE.search(prose)
    if c:
        why.append(f'R10: rule code "{c.group(0)}" in the body')
    if len(body) > MAX_BODY:
        why.append(f'R14: body is {len(body)} characters, at most {MAX_BODY}')
    return why


# R22: what the facts line says -> the ask line 1 must carry (the `short` of blockers()).
FACT_ASKS = [(' · merge conflict · ', 'resolve the merge conflict'), ('CI red', 'fix the failing checks'),
             ('No CI ran', 'get CI running'), ('CI skipped', 'get the checks to run'), ('CI still running', 'get CI to finish')]
REVIEW_URL = re.compile(r'^https://github\.com/[\w.-]+/[\w.-]+/pull/\d+#pullrequestreview-\d+$')


def gate_delivery(pr_state, review_url, review_state=None, post_slack=False, head=None, slack_heads=(),
                  inline_lines=None, ours_on_head=None):
    """R15: never deliver for a merged or closed PR. R17: a delivered review has its own GitHub review URL.
    R0: never a pending review (it takes the reviewer's own review slot). R18: one Slack message per
    (PR, head); the same head is never reviewed twice. R21: every inline comment is anchored to a line
    (inline_lines: the `line` GitHub reports for each, None when it did not anchor). R24: one review of
    ours per head (ours_on_head: how many automated reviews of this head are not pointers to another)."""
    why = []
    if ours_on_head is not None and ours_on_head > 1:
        why.append(f'R24: {ours_on_head} automated reviews of this head; one carries the whole review')
    if post_slack and head and head in slack_heads:
        why.append('R18: a second Slack message for the same head')
    if pr_state != 'OPEN':
        why.append(f'R15: the PR is {pr_state.lower()}')
    if not review_url or not REVIEW_URL.match(review_url):
        why.append('R17: the review has no GitHub review URL of its own')
    if review_state == 'PENDING':
        why.append('R0: a pending review was created')
    if inline_lines is not None and any(l is None for l in inline_lines):
        why.append(f'R21: {sum(1 for l in inline_lines if l is None)} inline comments are not anchored to a line')
    return why


CI_OK = {'SUCCESS', 'NEUTRAL', 'SKIPPED'}


def ci_strict(pr):
    """R27: CI is green for an approval: every check SUCCESS, NEUTRAL or SKIPPED, at least one SUCCESS, and
    every check seen (no more than one page of them)."""
    nodes = (pr.get('commits') or {}).get('nodes') or []
    roll = nodes and nodes[0]['commit'].get('statusCheckRollup')
    if not roll:
        return False
    ctx = roll['contexts']
    concl = [c.get('conclusion') or c.get('state') for c in ctx['nodes']]
    complete = ctx.get('totalCount', len(concl)) <= len(concl)
    return complete and bool(concl) and all(c in CI_OK for c in concl) and 'SUCCESS' in concl


def gate_approve(rv, fresh, head, flagged=(), idx=None):
    """R27, the bar for an APPROVE (Alex, 2026-10-29 21:01Z: "actually approve low risk PRs not just say you
    do"): the PR is open and its head is the reviewed head (checked right before posting); CI is green
    (ci_strict); low risk with no decision, item or blocker and no not-checked area; and every flagged
    hunk has its cleared entry (an item or a decision would not be low risk)."""
    why = []
    if rv['outcome'] != 'low-risk' or rv['decisions'] or rv['items'] or rv['blockers']:
        why.append('R27: only a low-risk review with nothing to ask may be an approval')
    if rv.get('not_checked'):
        why.append('R27: the PR touches a not-checked area')
    if fresh.get('state') != 'OPEN':
        why.append(f"R27: the PR is {str(fresh.get('state')).lower()}")
    if fresh.get('headRefOid') != head:
        why.append('R27: the head changed after the review')
    if not ci_strict(fresh):
        why.append('R27: CI is not green (every check success, neutral or skipped, at least one success)')
    cleared = [dict(c, side='new') for c in (rv.get('cleared') or [])]
    for f in flagged:
        if not any(c.get('flag') == f['id'] for c in cleared) or (idx is not None and not covers(idx, f, [
                c for c in cleared if c.get('flag') == f['id']])):
            why.append(f"R27: flag {f['id']} has no cleared entry in its hunk")
    return why


def gate(rv, idx, body, tree_lines=None, pr_text='', flagged=(), comments=None):
    return gate_entries(rv, idx, tree_lines, pr_text, flagged) + gate_body(body, rv, comments)


def review_of(fx):
    """A fixture's review dict (defaults for what it leaves out; cleared entries get their flag's label)."""
    labels = {f['id']: f['label'] for f in fx.get('flagged', [])}
    cleared = [dict(c, label=c.get('label') or labels.get(c.get('flag'), 'a flagged change')) for c in fx.get('cleared', [])]
    return {'outcome': fx['outcome'], 'approve': fx.get('approve', False), 'decisions': fx.get('decisions', []),
            'items': fx.get('items', []), 'blockers': fx.get('blockers', []), 'cleared': cleared,
            'safe_to_skip': fx.get('safe_to_skip', ''), 'facts': fx.get('facts', 'CI passed · no merge conflict · open'),
            'not_checked': fx.get('not_checked', ''), 'model': fx.get('model', True)}


def regress(dirs):
    """Run the gate over every fixture (*.json) in dirs. A fixture holds `expect` ("pass" or "fail"),
    optionally `expect_reasons` (rule ids that must be among the reasons), and either `body` (+ outcome,
    n_items, n_decisions) for a delivered body, or the review's parts (outcome, decisions, items, blockers,
    cleared, flagged, diff, ...) to render and gate; `delivery` ({pr_state, review_url, review_state, ...})
    adds the delivery checks and `approve_check` ({fresh: {state, head, checks}, head}) the approval bar.
    Returns the number of mismatches."""
    bad = total = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith('.json'):
                continue
            fx = json.load(open(os.path.join(d, fn)))
            total += 1
            if 'body' in fx:
                rv = {'outcome': fx['outcome'], 'decisions': [{}] * fx.get('n_decisions', 0),
                      'items': [{}] * fx.get('n_items', 0), 'not_checked': fx.get('not_checked', ''),
                      'approve': fx.get('approve', False), 'model': fx.get('model', True)}
                reasons = gate_body(fx['body'], rv)
            else:
                idx = diff_index(fx['diff'])
                pr = {'number': 1, 'state': 'OPEN', 'mergeable': 'MERGEABLE', 'url': 'https://github.com/example/repo/pull/1',
                      'headRefOid': 'a' * 40}
                rv = review_of(fx)
                body = render('example/repo', pr, rv, 'Reviewer', 'Author')
                reasons = gate(rv, idx, body, fx.get('tree_lines'), fx.get('pr_text', ''), fx.get('flagged', []),
                               inline_comments(rv['items'], 'Author'))
                if 'approve_check' in fx:
                    ac = fx['approve_check']
                    f = ac['fresh']
                    fresh = {'state': f.get('state', 'OPEN'), 'headRefOid': f.get('head', ac['head']),
                             'commits': {'nodes': [{'commit': {'statusCheckRollup': {'state': 'SUCCESS', 'contexts': {
                                 'totalCount': len(f.get('checks', [])),
                                 'nodes': [{'conclusion': c} for c in f.get('checks', [])]}}}}]}}
                    reasons += gate_approve(rv, fresh, ac['head'], fx.get('flagged', []), idx)
            if 'delivery' in fx:
                dl = fx['delivery']
                reasons += gate_delivery(dl.get('pr_state', 'OPEN'), dl.get('review_url'), dl.get('review_state'),
                                         dl.get('post_slack', False), dl.get('head'), dl.get('slack_heads', []),
                                         dl.get('inline_lines'), dl.get('ours_on_head'))
            if 'judge' in fx:  # R26: the reader test's scores for this review
                import pr_judge
                reasons += pr_judge.below_bar(fx['judge'])
            verdict = 'fail' if reasons else 'pass'
            need = set(fx.get('expect_reasons', []))
            got = {r.split(':')[0] for r in reasons}
            ok = verdict == fx['expect'] and need <= got
            bad += not ok
            # Fixture names and rule ids only: a host-local fixture's text never reaches the output.
            print(f"{'ok  ' if ok else 'FAIL'} {os.path.basename(d)}/{fn}: expect {fx['expect']}, got {verdict}"
                  f" {sorted(got) if reasons else ''}")
    print(f'{total - bad} of {total} fixtures as expected')
    return bad


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--regress':
        sys.exit(1 if regress(sys.argv[2:]) else 0)
    print(__doc__)
