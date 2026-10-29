#!/usr/bin/env python3
"""Review format and gate (assets/style-guide.md). Pure functions, no network.

  diff_index(diff)            which lines of which file a review may point at
  decide(items, ci, flagged)  the outcome from the items the model returned
  render(...)                 the review body and its inline comments, in the style guide's shape
  gate(...)                   the reasons a review breaks the style guide ([] = it may be posted)

`pr_format.py --regress DIR...` runs the gate over regression fixtures (README.md, "Quality loop").
"""
import hashlib
import json
import os
import re
import sys

AREAS = ['patient data', 'security', "can't be undone", 'migration order', 'behaviour patients notice', 'other']
KINDS = {'check': 'You decide', 'question': 'Ask {author}', 'change': 'Change'}
FOOTER = ('Automated review (Claude Code); {name} has not read this PR. '
          'Not checked: billing, plans, credits, pricing.')
MARKER = 'Automated review'  # in every body this pack posts, old format and new
MAX_ITEMS, MAX_FIELD, MAX_BODY = 5, 300, 5000

# R6: phrases that carry no information on their own. Matched case-insensitively in item fields.
FILLER = [r'\bmake sure\b', r'\bconsider (reviewing|checking|adding)\b', r'\bplease (review|check|verify)\b',
          r'\bbest practices?\b', r'\bpotential(ly)? (issues?|problems?|risks?)\b', r'\blooks? good\b',
          r'\bgreat (job|work)\b', r'\bnice\b', r'\bit is (important|recommended|advisable) to\b',
          r'\bin general\b', r'\boverall\b', r'\bas needed\b', r'\bdouble[- ]check\b', r'\bkeep in mind\b',
          r'\bshould be reviewed\b', r'\bmay want to\b', r'\bmight want to\b', r'\bverify (that )?everything\b']
FILLER_RE = re.compile('|'.join(FILLER), re.I)
# R10: rule codes and ids that must not appear in the body outside the closing <details>.
CODES_RE = re.compile(r'\b(A[1-7]|B[1-4]|R\d{1,2}|ci-(red|none|skipped)|phi-[a-z-]+|sec-[a-z-]+|irrev-[a-z-]+|'
                      r'migration-(order|edit)|no-description|untested)\b')
LINE1 = {
    'escalate': re.compile(r'^\*\*Needs you: (\d+) things? to check\*\*$|^\*\*Needs you:\*\* .+$'),
    'questions': re.compile(r'^\*\*(\d+) questions? for .+\*\*$'),
    'changes': re.compile(r'^\*\*Changes requested: (\d+)\*\*$'),
    'low-risk': re.compile(r'^\*\*Low risk: nothing to check\*\*$'),
}


def diff_index(diff):
    """{path: {'new': set of new-side lines, 'old': set of old-side lines, 'hunks': [(new set, old set)]}}
    for every file in a unified diff (`git diff`), counting context lines on both sides."""
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


def deleted_paths(diff):
    """Paths a diff removes (their lines exist on the old side only)."""
    out, old = set(), None
    for raw in diff.splitlines():
        if raw.startswith('--- a/'):
            old = raw[6:]
        elif raw.startswith('+++ /dev/null') and old:
            out.add(old)
    return out


def anchor(repo, number, path, side, start, end):
    """The Files-view link to lines of a PR's diff: #diff-<sha256(path)>R40-R52 (L for the old side)."""
    s = 'R' if side == 'new' else 'L'
    rng = f'{s}{start}' if start == end else f'{s}{start}-{s}{end}'
    return f'https://github.com/{repo}/pull/{number}/files#diff-{hashlib.sha256(path.encode()).hexdigest()}{rng}'


def in_diff(idx, item):
    """Both ends of the item's range are lines of one hunk of its file, on its side."""
    f = idx.get(item.get('path'))
    if not f:
        return False
    k = 0 if item.get('side', 'new') == 'new' else 1
    return any(item['line_start'] in h[k] and item['line_end'] in h[k] for h in f['hunks'])


def decide(items, ci, flagged):
    """Outcome from the items (the script decides, not the model): a high item or a check for the
    reviewer = escalate; a medium or high change = changes; only questions = questions; none = low risk
    when CI passed and no rule flagged the PR, else escalate (R12: the gate then wants the flagged hunks)."""
    if any(i['kind'] == 'check' or i['severity'] == 'high' for i in items) or ci in ('red', 'none', 'skipped'):
        return 'escalate'
    if any(i['kind'] == 'change' and i['severity'] in ('medium', 'high') for i in items):
        return 'changes'
    if items:
        return 'questions' if all(i['kind'] == 'question' for i in items) else 'changes'
    return 'escalate' if flagged else 'low-risk'


def plural(n, one, many):
    return f'{n} {one if n == 1 else many}'


def facts_line(pr, ci, ci_bad):
    c = {'green': 'CI passed', 'red': f"CI red: {', '.join(ci_bad[:6]) or 'see checks'}",
         'none': 'No CI ran', 'skipped': 'CI skipped (no check passed)', 'pending': 'CI still running'}[ci]
    conflict = 'merge conflict' if pr.get('mergeable') == 'CONFLICTING' else 'no merge conflict'
    state = {'MERGED': 'merged', 'OPEN': 'open', 'CLOSED': 'closed'}.get(pr['state'], pr['state'].lower())
    return f'{c} · {conflict} · {state}'


def render(repo, pr, outcome, items, safe_to_skip, facts, rule_ids, name, author, single_line=None):
    """(body, inline comments) in the style guide's shape."""
    n = len(items)
    if single_line:
        head = f'**Needs you:** {single_line}'
    else:
        head = {'escalate': f"**Needs you: {plural(n, 'thing', 'things')} to check**",
                'questions': f"**{plural(n, 'question', 'questions')} for {author}**",
                'changes': f'**Changes requested: {n}**',
                'low-risk': '**Low risk: nothing to check**'}[outcome]
    lines, comments = [head, ''], []
    for k, it in enumerate(items, 1):
        rng = f"L{it['line_start']}" if it['line_start'] == it['line_end'] else f"L{it['line_start']}-L{it['line_end']}"
        old = ' (removed lines)' if it.get('side') == 'old' else ''
        url = anchor(repo, pr['number'], it['path'], it.get('side', 'new'), it['line_start'], it['line_end'])
        label = KINDS[it['kind']].format(author=author)
        lines += [f"{k}. [`{it['path']}` {rng}{old}]({url}) · {it['area']}",
                  f"   **Changed:** {it['what']}",
                  f"   **Why it matters:** {it['why']}",
                  f"   **{label}:** {it['ask']}"]
        c = {'path': it['path'], 'line': it['line_end'], 'side': 'RIGHT' if it.get('side', 'new') == 'new' else 'LEFT',
             'body': f"**{k}. {it['area']}**: {it['what']} {it['why']}\n\n**{label}:** {it['ask']}"}
        if it['line_start'] != it['line_end']:
            c.update(start_line=it['line_start'], start_side=c['side'])
        comments.append(c)
    if items:
        lines.append('')
    if safe_to_skip:
        lines += [f'**Safe to skip:** {safe_to_skip}', '']
    lines += [facts, '', f"<sub>{FOOTER.format(name=name)}</sub>"]
    if rule_ids:
        lines += ['', f"<details><summary>rules</summary>{', '.join(rule_ids)}</details>"]
    return '\n'.join(lines), comments


def gate_items(items, idx, flagged_paths, outcome, single_line=False):
    """Reasons the model's items break the style guide."""
    why = []
    if single_line:
        return why
    if len(items) > MAX_ITEMS:
        why.append(f'R2: {len(items)} items, at most {MAX_ITEMS}')
    if outcome == 'low-risk' and items:
        why.append('R2: low risk with items')
    if outcome != 'low-risk' and not items:
        why.append('R12: no items; name the exact hunks to check' + (
            f" (the rules flagged {', '.join(sorted(flagged_paths)[:5])})" if flagged_paths else ''))
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
            v = (it.get(f) or '').strip()
            if len(v.split()) < 4:
                why.append(f'R5: {p} {f} is empty or under 4 words')
            elif len(v) > MAX_FIELD:
                why.append(f'R14: {p} {f} is over {MAX_FIELD} characters')
            m = FILLER_RE.search(v)
            if m:
                why.append(f'R6: {p} {f} has filler ("{m.group(0)}")')
            if CODES_RE.search(v):
                why.append(f'R10: {p} {f} has a rule code ("{CODES_RE.search(v).group(0)}")')
        if it.get('area') not in AREAS:
            why.append(f'R5: {p} area is not one of {AREAS}')
        if it.get('kind') == 'question' and not (it.get('ask') or '').rstrip().endswith('?'):
            why.append(f'R13: {p} is a question but its ask does not end with a question mark')
    return why


def gate_body(body, comments, n_items, outcome, single_line=False):
    """Reasons the rendered body breaks the style guide (also runs on bodies posted earlier)."""
    why = []
    main = body.split('<details>')[0]
    lines = [l for l in main.splitlines()]
    first = lines[0] if lines else ''
    pat = LINE1.get(outcome)
    if not pat or not pat.match(first):
        why.append(f'R1: line 1 is not the outcome and count for {outcome}')
    m = pat.match(first) if pat else None
    if m and m.groups() and m.group(1) and int(m.group(1)) != n_items:
        why.append(f'R1: line 1 counts {m.group(1)}, the review has {n_items}')
    if not single_line and outcome != 'low-risk':
        links = re.findall(r'^\d+\. \[`[^`]+` L\d+(?:-L\d+)?(?: \(removed lines\))?\]\(https://github\.com/[^)]+/files#diff-[0-9a-f]{64}[RL]\d+(?:-[RL]\d+)?\)',
                           main, re.M)
        if len(links) != n_items or n_items == 0:
            why.append(f'R3: {len(links)} item links to diff lines for {n_items} items')
        if len(comments) != n_items:
            why.append(f'R4: {len(comments)} inline comments for {n_items} items')
        if not re.search(r'^\*\*Safe to skip:\*\* \S', main, re.M):
            why.append('R7: no "Safe to skip" line')
    skip = re.search(r'^\*\*Safe to skip:\*\* (.*)$', main, re.M)
    if skip and len(skip.group(1)) > 240:
        why.append(f'R7: "Safe to skip" is {len(skip.group(1))} characters, at most 240')
    if not re.search(r'^(CI passed|CI red|No CI ran|CI skipped|CI still running).* · (no )?merge conflict · \w+$', main, re.M):
        why.append('R8: no facts line')
    tail = [l for l in lines if l.strip()]
    if not tail or not tail[-1].startswith('<sub>' + MARKER) or 'Not checked: billing, plans, credits, pricing.' not in tail[-1]:
        why.append('R9: the footer is not the last line')
    if re.search(r'(?i)shadow mode|review agents', main):
        why.append('R9: boilerplate in the body')
    prose = re.sub(r'\]\([^)]*\)', ']', main)  # link targets carry R40-R52 anchors, not rule codes
    m = CODES_RE.search(prose.replace(tail[-1] if tail else '', ''))
    if m:
        why.append(f'R10: rule code "{m.group(0)}" in the body')
    if len(body) > MAX_BODY:
        why.append(f'R14: body is {len(body)} characters, at most {MAX_BODY}')
    return why


def gate(items, idx, flagged_paths, outcome, body, comments, single_line=False):
    return gate_items(items, idx, flagged_paths, outcome, single_line) + gate_body(
        body, comments, len(items), outcome, single_line)


def regress(dirs):
    """Run the gate over every fixture (*.json) in dirs. A fixture holds `expect` ("pass" or "fail"),
    optionally `expect_reasons` (rule ids that must be among the reasons), and either `body` (+ `outcome`,
    `n_items`, `comments`) for a posted body, or `items` + `diff` + `outcome` (+ repo, pr) to render and
    gate. Returns the number of fixtures whose verdict differs from `expect`."""
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
                reasons = gate_body(fx['body'], fx.get('comments', []), fx.get('n_items', 0), fx['outcome'],
                                    fx.get('single_line', False))
            else:
                idx = diff_index(fx['diff'])
                pr = fx.get('pr', {'number': 1, 'state': 'MERGED', 'mergeable': 'MERGEABLE'})
                body, comments = render(fx.get('repo', 'example/repo'), pr, fx['outcome'], fx['items'],
                                        fx.get('safe_to_skip', ''), fx.get('facts', 'CI passed · no merge conflict · merged'),
                                        fx.get('rule_ids', []), 'Reviewer', 'the author', fx.get('single_line'))
                reasons = gate(fx['items'], idx, fx.get('flagged_paths', []), fx['outcome'], body, comments,
                               bool(fx.get('single_line')))
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
