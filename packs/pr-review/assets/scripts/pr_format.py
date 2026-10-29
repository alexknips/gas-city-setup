#!/usr/bin/env python3
"""Review format and gate (assets/style-guide.md). Pure functions, no network.

  diff_index(diff)              which lines of which file an item may point at
  decide(decisions, items, ci)  the outcome
  render(...)                   the review as markdown (what the gate reads) ...
  render_page(...)              ... and as the private HTML page that is delivered (R0, R17)
  gate(...)                     the reasons a review breaks the style guide ([] = it may be delivered)

`pr_format.py --regress DIR...` runs the gate over regression fixtures (README.md, "Quality loop").
"""
import hashlib
import html
import json
import os
import re
import sys

AREAS = ['patient data', 'security', "can't be undone", 'migration order', 'behaviour patients notice',
         'product', 'compliance', 'other']
KINDS = {'question': 'Ask {author}', 'change': 'Change'}
FOOTER = ('Automated review (Claude Code); {name} has not read this PR. '
          'Not checked: billing, plans, credits, pricing.')
MARKER = 'Automated review'
MAX_ITEMS, MAX_DECISIONS, MAX_FIELD, MAX_BODY = 5, 3, 300, 6000

# R6: phrases that carry no information on their own. Matched case-insensitively in every text field.
FILLER = [r'\bmake sure\b', r'\bconsider (reviewing|checking|adding)\b', r'\bplease (review|check|verify)\b',
          r'\bbest practices?\b', r'\bpotential(ly)? (issues?|problems?|risks?)\b', r'\blooks? good\b',
          r'\bgreat (job|work)\b', r'\bit is (important|recommended|advisable) to\b', r'\bin general\b',
          r'\boverall\b', r'\bas needed\b', r'\bdouble[- ]check\b', r'\bkeep in mind\b', r'\bshould be reviewed\b',
          r'\bmay want to\b', r'\bmight want to\b', r'\bverify (that )?everything\b']
FILLER_RE = re.compile('|'.join(FILLER), re.I)
# R10: rule codes and ids that must not appear in the text outside the closing <details>.
CODES_RE = re.compile(r'\b(A[1-7]|B[1-4]|R\d{1,2}|ci-(red|none|skipped)|phi-[a-z-]+|sec-[a-z-]+|irrev-[a-z-]+|'
                      r'migration-(order|edit)|no-description|untested)\b')
LINE1 = {
    'escalate': re.compile(r'^\*\*Needs you: (\d+) decisions?\*\*$'),
    'questions': re.compile(r'^\*\*(\d+) questions? for .+\*\*$|^\*\*1 question for [^*]+:\*\* .+$'),
    'changes': re.compile(r'^\*\*Changes requested: (\d+)\*\*$'),
    'low-risk': re.compile(r'^\*\*Low risk: nothing to check\*\*$'),
}


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


def link_text(d):
    ln = d.get('link') or {}
    if ln.get('kind') == 'code':
        s, e = ln.get('line_start'), ln.get('line_end')
        return f"`{ln.get('path')}` L{s}" + ('' if s == e else f'-L{e}')
    return {'description': 'the PR description', 'ticket': 'the ticket'}.get(ln.get('kind'), 'the source')


def in_diff(idx, item):
    f = idx.get(item.get('path'))
    if not f:
        return False
    k = 0 if item.get('side', 'new') == 'new' else 1
    return any(item['line_start'] in h[k] and item['line_end'] in h[k] for h in f['hunks'])


def decide(decisions, items, ci):
    """R16: Needs you only with a decision only the reviewer can make. Otherwise changes (a medium or
    high change item, or changes mixed with questions), questions (only questions), or low risk
    (nothing, and CI passed; CI red or missing with nothing else becomes a question for the author)."""
    if decisions:
        return 'escalate'
    if any(i['kind'] == 'change' and i['severity'] in ('medium', 'high') for i in items):
        return 'changes'
    if items:
        return 'questions' if all(i['kind'] == 'question' for i in items) else 'changes'
    return 'low-risk' if ci == 'green' else 'questions'


def plural(n, one, many):
    return f'{n} {one if n == 1 else many}'


def facts_line(pr, ci, ci_bad):
    c = {'green': 'CI passed', 'red': f"CI red: {', '.join(ci_bad[:6]) or 'see checks'}",
         'none': 'No CI ran', 'skipped': 'CI skipped (no check passed)', 'pending': 'CI still running'}[ci]
    conflict = 'merge conflict' if pr.get('mergeable') == 'CONFLICTING' else 'no merge conflict'
    state = {'MERGED': 'merged', 'OPEN': 'open', 'CLOSED': 'closed'}.get(pr['state'], pr['state'].lower())
    return f'{c} · {conflict} · {state}'


def headline(outcome, decisions, items, author, single_line=None):
    if single_line:
        return f'**1 question for {author}:** {single_line}'
    return {'escalate': f"**Needs you: {plural(len(decisions), 'decision', 'decisions')}**",
            'questions': f"**{plural(len(items), 'question', 'questions')} for {author}**",
            'changes': f"**Changes requested: {sum(1 for i in items if i['kind'] == 'change')}**",
            'low-risk': '**Low risk: nothing to check**'}[outcome]


def render(repo, pr, outcome, decisions, items, safe_to_skip, reply, facts, rule_ids, name, author,
           single_line=None):
    """The review as markdown: what the gate checks, and the text of the page."""
    lines = [headline(outcome, decisions, items, author, single_line), '']
    for k, d in enumerate(decisions, 1):
        lines += [f"{k}. **{d['question']}** · {d['area']} · [open {link_text(d)}]({decision_link(repo, pr, d)})",
                  f"   **Why you:** {d['why']}",
                  f"   **Options:** {'; '.join(d['options'])}",
                  f"   **Recommended:** {d['recommendation']}"]
    if decisions and items:
        lines += ['', f'**For {author}:**']
    for k, it in enumerate(items, 1):
        rng = f"L{it['line_start']}" if it['line_start'] == it['line_end'] else f"L{it['line_start']}-L{it['line_end']}"
        old = ' (removed lines)' if it.get('side') == 'old' else ''
        url = diff_link(repo, pr['number'], it['path'], it.get('side', 'new'), it['line_start'], it['line_end'])
        label = KINDS[it['kind']].format(author=author)
        lines += [f"{'-' if decisions else str(k) + '.'} [`{it['path']}` {rng}{old}]({url}) · {it['area']}",
                  f"   **Changed:** {it['what']}",
                  f"   **Why it matters:** {it['why']}",
                  f"   **{label}:** {it['ask']}"]
    if decisions or items:
        lines.append('')
    if safe_to_skip:
        lines += [f'**Safe to skip:** {safe_to_skip}', '']
    if reply:
        lines += [f'**Suggested reply to {author}:** {reply}', '']
    lines += [facts, '', f"<sub>{FOOTER.format(name=name)}</sub>"]
    if rule_ids:
        lines += ['', f"<details><summary>rules</summary>{', '.join(rule_ids)}</details>"]
    return '\n'.join(lines)


def render_page(repo, pr, outcome, decisions, items, safe_to_skip, reply, facts, rule_ids, name, author,
                reviewed_at, single_line=None):
    """The private page (R0, R17): the same content as render(), as self-contained HTML (no external
    resources), with the suggested reply in a copyable box."""
    e = html.escape
    out = [f"<h1>{e(headline(outcome, decisions, items, author, single_line).replace('**', ''))}</h1>",
           f"<p class=meta><a href=\"{e(pr['url'])}\">{e(repo)}#{pr['number']}</a> · head "
           f"<code>{e(pr['headRefOid'][:10])}</code> · reviewed {e(reviewed_at)}</p>"]
    if decisions:
        out.append('<h2>Your decisions</h2><ol>')
        for d in decisions:
            out.append(f"<li><p class=q>{e(d['question'])}</p><p class=meta>{e(d['area'])} · "
                       f"<a href=\"{e(decision_link(repo, pr, d))}\">open {e(link_text(d).replace('`', ''))}</a></p>"
                       f"<p><b>Why you:</b> {e(d['why'])}</p><p><b>Options:</b> {e('; '.join(d['options']))}</p>"
                       f"<p><b>Recommended:</b> {e(d['recommendation'])}</p></li>")
        out.append('</ol>')
    if items:
        out.append(f"<h2>{'For ' + e(author) if decisions else 'What to look at'}</h2><ol>")
        for it in items:
            rng = f"L{it['line_start']}" + ('' if it['line_start'] == it['line_end'] else f"-L{it['line_end']}")
            url = diff_link(repo, pr['number'], it['path'], it.get('side', 'new'), it['line_start'], it['line_end'])
            label = KINDS[it['kind']].format(author=author)
            out.append(f"<li><p class=meta><a href=\"{e(url)}\">open line: {e(it['path'])} {rng}</a> · {e(it['area'])}</p>"
                       f"<p><b>Changed:</b> {e(it['what'])}</p><p><b>Why it matters:</b> {e(it['why'])}</p>"
                       f"<p><b>{e(label)}:</b> {e(it['ask'])}</p></li>")
        out.append('</ol>')
    if safe_to_skip:
        out.append(f'<p><b>Safe to skip:</b> {e(safe_to_skip)}</p>')
    if reply:
        out.append(f'<h2>Suggested reply to {e(author)}</h2><textarea readonly rows=6>{e(reply)}</textarea>'
                   '<p class=meta>Copy it into the PR yourself if you want; nothing was posted.</p>')
    out.append(f'<p>{e(facts)}</p><p class=meta>{e(FOOTER.format(name=name))}</p>')
    if rule_ids:
        out.append(f"<details><summary>rules</summary>{e(', '.join(rule_ids))}</details>")
    style = ('body{font:15px/1.45 system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#1d2125;'
             'background:#fff}@media(prefers-color-scheme:dark){body{background:#16181b;color:#e3e5e8}a{color:#8ab4f8}}'
             'h1{font-size:22px}h2{font-size:17px;margin-top:22px}li{margin:0 0 14px}.q{font-weight:600}'
             '.meta{color:#777;font-size:13px}textarea{width:100%;font:inherit}')
    return (f'<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
            f'<meta name=robots content=noindex><title>Review {e(repo)}#{pr["number"]}</title><style>{style}</style>'
            f'<body>{"".join(out)}</body></html>')


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
    return why


def gate_entries(decisions, items, idx, outcome, tree_lines, pr_text, single_line=False):
    """Reasons the model's decisions and items break the style guide. tree_lines: {path: line count} at
    the head (None = not checked); pr_text: the PR description, where a ticket link must appear."""
    why = []
    if single_line:
        return why
    if len(items) > MAX_ITEMS:
        why.append(f'R2: {len(items)} items, at most {MAX_ITEMS}')
    if len(decisions) > MAX_DECISIONS:
        why.append(f'R16: {len(decisions)} decisions, at most {MAX_DECISIONS}')
    if outcome == 'low-risk' and (items or decisions):
        why.append('R2: low risk with items')
    if outcome == 'escalate' and not decisions:
        why.append('R16: needs you without a decision for the reviewer')
    for k, d in enumerate(decisions, 1):
        p = f'decision {k}'
        q = (d.get('question') or '').strip()
        if not q.endswith('?') or q.count('?') != 1:
            why.append(f'R16: {p} is not one question ending in "?"')
        if len(d.get('options') or []) < 2:
            why.append(f'R16: {p} has fewer than 2 options')
        for f in ('question', 'why', 'recommendation'):
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
        if d.get('area') not in AREAS:
            why.append(f'R16: {p} area is not one of {AREAS}')
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
    return why


def gate_body(body, n_decisions, n_items, outcome, single_line=False):
    """Reasons the rendered markdown breaks the style guide (also runs on bodies delivered earlier)."""
    why = []
    main = body.split('<details>')[0]
    lines = main.splitlines()
    first = lines[0] if lines else ''
    pat = LINE1.get(outcome)
    m = pat.match(first) if pat else None
    if not m:
        why.append(f'R1: line 1 is not the outcome and count for {outcome}')
    elif m.groups() and m.group(1) and outcome != 'changes':
        want = n_decisions if outcome == 'escalate' else n_items
        if int(m.group(1)) != want:
            why.append(f'R1: line 1 counts {m.group(1)}, the review has {want}')
    if not single_line and outcome != 'low-risk':
        links = re.findall(r'\]\(https://github\.com/[^)]+/(?:pull/\d+/files#diff-[0-9a-f]{64}[RL]\d+(?:-[RL]\d+)?|'
                           r'blob/[0-9a-f]{40}/[^)#]+#L\d+(?:-L\d+)?|pull/\d+)\)', main)
        if not links or len(links) < n_items + n_decisions:
            why.append(f'R3: {len(links)} links to lines or text for {n_items + n_decisions} entries')
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
    c = CODES_RE.search(prose.replace(tail[-1] if tail else '', ''))
    if c:
        why.append(f'R10: rule code "{c.group(0)}" in the body')
    if len(body) > MAX_BODY:
        why.append(f'R14: body is {len(body)} characters, at most {MAX_BODY}')
    return why


def gate_delivery(pr_state, page_url, post_slack=False, head=None, slack_heads=()):
    """R15: never deliver for a merged or closed PR. R17: a delivered review has its own URL. R0: that
    URL is the private page, never a GitHub review in the reviewer's name. R18: one Slack message per
    (PR, head): a re-review of a head already posted updates the page and posts nothing."""
    why = []
    if post_slack and head and head in slack_heads:
        why.append('R18: a second Slack message for the same head')
    if pr_state != 'OPEN':
        why.append(f'R15: the PR is {pr_state.lower()}')
    if not page_url or not re.match(r'^https?://', page_url):
        why.append('R17: the review has no URL of its own')
    elif re.search(r'github\.com/', page_url):
        why.append("R0: the review's URL is on GitHub, not the private page")
    return why


def gate(decisions, items, idx, outcome, body, tree_lines=None, pr_text='', single_line=False):
    return (gate_entries(decisions, items, idx, outcome, tree_lines, pr_text, single_line)
            + gate_body(body, len(decisions), len(items), outcome, single_line))


def regress(dirs):
    """Run the gate over every fixture (*.json) in dirs. A fixture holds `expect` ("pass" or "fail"),
    optionally `expect_reasons` (rule ids that must be among the reasons), and either `body` (+ outcome,
    n_items, n_decisions) for a delivered body, or `decisions` + `items` + `diff` + `outcome` to render
    and gate; `delivery` ({pr_state, page_url}) adds the delivery checks. Returns the mismatches."""
    bad = total = 0
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith('.json'):
                continue
            fx = json.load(open(os.path.join(d, fn)))
            total += 1
            single = fx.get('single_line')
            if 'body' in fx:
                reasons = gate_body(fx['body'], fx.get('n_decisions', 0), fx.get('n_items', 0), fx['outcome'], bool(single))
            else:
                idx = diff_index(fx['diff'])
                pr = {'number': 1, 'state': 'OPEN', 'mergeable': 'MERGEABLE', 'url': 'https://github.com/example/repo/pull/1',
                      'headRefOid': 'a' * 40}
                body = render('example/repo', pr, fx['outcome'], fx.get('decisions', []), fx['items'],
                              fx.get('safe_to_skip', ''), fx.get('reply', ''),
                              fx.get('facts', 'CI passed · no merge conflict · open'), fx.get('rule_ids', []),
                              'Reviewer', 'the author', single)
                reasons = gate(fx.get('decisions', []), fx['items'], idx, fx['outcome'], body, fx.get('tree_lines'),
                               fx.get('pr_text', ''), bool(single))
            if 'delivery' in fx:
                dl = fx['delivery']
                reasons += gate_delivery(dl.get('pr_state', 'OPEN'), dl.get('page_url'), dl.get('post_slack', False),
                                         dl.get('head'), dl.get('slack_heads', []))
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
