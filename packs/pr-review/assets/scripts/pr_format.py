#!/usr/bin/env python3
"""Review format and gate (assets/style-guide.md). Pure functions, no network.

  diff_index(diff)                   which lines of which file an entry may point at
  blockers(repo, pr, ci, ci_bad)     a merge conflict or CI that did not pass, as linked asks for the author (R22)
  decide(decisions, items, blockers) the outcome
  render(repo, pr, rv, name, author) the review body (markdown), the verdict first (R28) ...
  inline_comments(items, author)     ... and one inline comment per item, on its lines (R4)
  gate(rv, idx, body, ...)           the reasons a review breaks the style guide ([] = it may be delivered)
  gate_approve(...)                  the reasons a low-risk review may not be an APPROVE (R27)

A review `rv` is a dict: outcome, approve (bool), decisions, items, blockers, cleared, summary, skip, facts,
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
# R28: line 1 is the verdict, then the honesty note as a suffix (the review is posted under the reviewer's
# login, so the author must see it is automated): "**Approved as low risk** · automated, Alex hasn't read this PR".
SUFFIX = " · automated, {name} hasn't read this PR"
# R33: what a requested PR gets when its review was held: never silence.
NOT_REVIEWED = '**Not reviewed automatically.**'  # how the notes of v4.2-v5 began
NOT_SUMMARIZED = '(not summarized)'  # R32: a money place the line reader gave no line, still linked
# How our reviews begin: earlier bodies with a marker, v5 bodies with the verdict and the suffix.
MARKERS = ('Automated review', 'Automated approval', NOT_REVIEWED)
OURS_RE = r'^\*\*[A-Z][A-Za-z ]+\*\* · automated, '
LETTERS = 'ABCD'
WORDS = {'escalate': 'Needs you', 'changes': 'Changes requested', 'questions': 'Questions', 'low-risk': 'Low risk',
         'not-reviewed': 'Not reviewed automatically'}
APPROVED = 'Approved as low risk'
# R35: the word matches the event. Changes are "requested" only on a REQUEST_CHANGES review.
SUGGESTED = 'Changes suggested'
MAX_ITEMS, MAX_DECISIONS, MAX_FIELD, MAX_BODY = 5, 3, 300, 6000
MAX_SKIP_LINKS = 12  # files the skip list links one by one; then one link to the files view (R30)
MONEY_KINDS = ['charges', 'credits', 'plans', 'limits', 'other']  # R32: the money places, grouped
MONEY_SHOWN = 6  # R32: the money places shown one by one (behaviour first, by risk); the rest in two linked lines
MONEY_WORDS = 30  # R32: one line per place ("about 25 words"; pr_review clips longer ones)
GREEN_FACTS = 'CI passed · no merge conflict · open'  # R30: the status line shows only when it is not this

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
# R28, R1: line 1, the verdict and the automated note.
LINE1_RE = re.compile(r'^\*\*(Needs you|Changes requested|Changes suggested|Questions|Low risk|Approved as low risk|'
                      r'Not reviewed automatically)\*\*'
                      r" · automated, (.+) hasn't read this PR$")
# R22: line 2, who acts: "Alex: 1 decision. Nils: 1 change, get CI running." or "Nothing for Alex or Nils."
WHO_RE = re.compile(r'^(.+?): (nothing|(\d+) decisions?)( \((.+)\))?\. (.+?): (.+)\.$')
NOTHING_RE = re.compile(r'^Nothing for (.+) or (.+)\.$')
# R37: a decision never asks the reviewer to read or review code (the reviewer does not know the codebase).
REVIEW_CODE_RE = re.compile(r'(?i)\byou(rself)? (review|read|check|look at)\b|\b(review|read|check)\b[^.?]{0,40}\byourself\b')
# R29: an entry's change is plain words, never a raw identifier.
IDENT_RE = re.compile(r'`|\b[a-z]+[A-Z][A-Za-z0-9]*\b|\b[A-Za-z]+_[A-Za-z0-9_]+\b|\w\(\)|::|->')
# R36: a link that lands only on comment lines does not show why the code is safe.
COMMENT_RE = re.compile(r'^\s*(//|#|/\*|\*|<!--|--|"""|\'\'\')')


def is_ours(body):
    return (body or '').startswith(MARKERS) or bool(re.match(OURS_RE, body or ''))


def ours_jq():
    """A jq test for "this review body is ours" (every version)."""
    return ' or '.join([f'(.body | startswith("{m}"))' for m in MARKERS] +
                       [f'(.body | test("{OURS_RE.replace(chr(92), chr(92) * 2)}"))'])


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
                idx.setdefault(cur, {'new': set(), 'old': set(), 'hunks': [], 'text': {}, 'hunk_text': []})
            continue
        m = re.match(r'@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', raw)
        if m and cur is not None:
            old_l, new_l = int(m.group(1)), int(m.group(2))
            hunk = (set(), set(), set())  # new side, old side, added lines
            idx[cur]['hunks'].append(hunk)
            idx[cur]['hunk_text'].append([raw])
            continue
        if cur is None or hunk is None:
            continue
        idx[cur]['hunk_text'][-1].append(raw)
        if raw.startswith('+'):
            idx[cur]['text'][new_l] = raw[1:]
            idx[cur]['new'].add(new_l); hunk[0].add(new_l); hunk[2].add(new_l); new_l += 1
        elif raw.startswith('-'):
            idx[cur]['old'].add(old_l); hunk[1].add(old_l); old_l += 1
        elif raw.startswith(' ') or raw == '':
            idx[cur]['text'][new_l] = raw[1:]
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


def files_link(repo, number, path):
    """The Files-view link to one file of a PR's diff."""
    return f'https://github.com/{repo}/pull/{number}/files#diff-{hashlib.sha256(path.encode()).hexdigest()}'


def short_name(path):
    return path.rsplit('/', 1)[-1]


def blockers(repo, pr, ci, ci_bad=(), conflicts=()):
    """A merge conflict and CI that did not pass, as linked asks for the author (R22). `kind` decides the
    outcome word (a conflict or red CI is a change); `short` goes into line 2; `cause` lets the sweep lift
    a formal block once the cause is gone. `conflicts`: the conflicting files (git merge-tree), named and
    linked."""
    n, out = pr['number'], []
    checks = f'https://github.com/{repo}/pull/{n}/checks'
    if pr.get('mergeable') == 'CONFLICTING':
        named = ', '.join(f'[`{short_name(c)}`]({files_link(repo, n, c)})' for c in conflicts[:8])
        more = f' and {plural(len(conflicts) - 8, "more file", "more files")}' if len(conflicts) > 8 else ''
        out.append({'kind': 'change', 'cause': 'conflict', 'short': 'resolve the merge conflict',
                    'label': 'Resolve the merge conflict', 'url': f'https://github.com/{repo}/pull/{n}/conflicts',
                    'detail': f'it is in {named}{more}.' if named else 'GitHub reports one with the base branch.'})
    if ci == 'red':
        names = ', '.join(ci_bad[:4]) or 'see the checks'
        out.append({'kind': 'change', 'cause': 'ci', 'short': 'fix the failing checks', 'label': 'Fix the failing checks',
                    'url': checks, 'detail': f'{names}.'})
    elif ci == 'none':
        out.append({'kind': 'question', 'cause': 'ci', 'short': 'get CI running', 'label': 'Get CI running', 'url': checks,
                    'detail': 'no check ran on this commit, so nothing has tested it.'})
    elif ci == 'skipped':
        out.append({'kind': 'question', 'cause': 'ci', 'short': 'get the checks to run', 'label': 'Get the checks to run', 'url': checks,
                    'detail': 'every check on this commit was skipped, so nothing has tested it.'})
    elif ci == 'pending':
        out.append({'kind': 'question', 'cause': 'ci', 'short': 'get CI to finish', 'label': 'Get CI to finish', 'url': checks,
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
    """R22: line 2, who must do what; "Nothing for <reviewer> or <author>." when nobody acts."""
    d = rv['decisions']
    n_change = sum(1 for i in rv['items'] if i['kind'] == 'change')
    n_q = sum(1 for i in rv['items'] if i['kind'] == 'question')
    parts = ([plural(n_change, 'change', 'changes')] if n_change else []) + \
            ([plural(n_q, 'question', 'questions')] if n_q else []) + [b['short'] for b in rv['blockers']] + \
            [f"if {name} picks {LETTERS[x['recommended']]}, {x['author_task']}" for x in d if x.get('author_task')]
    if not d and not parts:
        return f'Nothing for {name} or {author}.'
    you = plural(len(d), 'decision', 'decisions') if d else 'nothing'
    return f"{name}: {you}. {author}: {', '.join(parts) or 'nothing'}."


def word_of(rv):
    """Line 1's outcome word (R1), matching the review's event (R35)."""
    if rv.get('approve'):
        return APPROVED
    if rv['outcome'] == 'changes' and not rv.get('request_changes'):
        return SUGGESTED
    return WORDS[rv['outcome']]


def event_of(rv):
    """The review's GitHub event: APPROVE under the bar (R27), REQUEST_CHANGES for changes when the reviewer
    allows it (R35), else COMMENT (questions, escalations, low risk without approval, the R33 note)."""
    if rv.get('approve'):
        return 'APPROVE'
    if rv['outcome'] == 'changes' and rv.get('request_changes'):
        return 'REQUEST_CHANGES'
    return 'COMMENT'


def headline(rv, name):
    """R28: line 1, the verdict, then the automated note."""
    return f'**{word_of(rv)}**' + SUFFIX.format(name=name)


def rule_ref(repo, rv, rid):
    """R37: ' · rule: <the doc lines>' for an entry that cites a rule of the repo's digest."""
    r = (rv.get('rules') or {}).get(rid) if rid else None
    if not r:
        return ''
    url = code_link(repo, rv['rules_sha'], r['path'], r['line_start'], r['line_end'])
    return f" · rule: [`{r['path']}` {rng_text(r)}]({url})"


def cleared_line(repo, number, rv, c):
    """R29: what the PR changed in 3-6 plain words, its lines, and the one fact that makes it safe."""
    url = diff_link(repo, number, c['path'], c.get('side', 'new'), c['line_start'], c['line_end'])
    return f"- **{c['change']}** · [`{c['path']}` {rng_text(c)}]({url}): {c['why']}{rule_ref(repo, rv, c.get('rule'))}"


def render(repo, pr, rv, name, author):
    """The review body (markdown). Line 1, the verdict and the automated note (R28); line 2, who acts (R22).
    An approval stops there, with what was checked collapsed (R36). Otherwise: the decisions for the
    reviewer, each with its options on lettered lines and the recommendation's reason (R16, R32, R37);
    the asks for the author with links to their lines (each item is also an inline comment) and the
    blockers; what was checked and cleared (R19, R29); safe to skip with the files view (R30); and the
    status line only when something did not pass (R30)."""
    n, head = pr['number'], pr['headRefOid']
    lines = [headline(rv, name), who_acts(rv, name, author)]
    if rv.get('approve'):
        if rv.get('cleared'):
            lines += ['', '<details><summary>What was checked</summary>', '']
            lines += [cleared_line(repo, n, rv, c) for c in rv['cleared']]
            lines += ['', '</details>']
        return '\n'.join(lines)
    lines.append('')
    if rv.get('summary'):
        lines += [f"**What it does:** {rv['summary']}", '']
    if rv['decisions']:
        lines.append(f'**For {name}:**')
        for k, d in enumerate(rv['decisions'], 1):
            money = d.get('area') == NOT_CHECKED_AREA
            opener = '' if d.get('where') else f" · [{link_text(d)}]({decision_link(repo, pr, d)})"
            lines += [f"{k}. **{d['question']}**{opener}"]
            if money:
                lines.append('   ' + opening_text(repo, head, d))
            elif d.get('why'):
                lines.append(f"   {d['why']}")
            if rule_ref(repo, rv, d.get('rule')):
                lines.append(f"   Rule{rule_ref(repo, rv, d.get('rule'))[len(' · rule'):]}")
            others = []
            if d.get('where') and money:
                shown, others = money_lines(repo, head, d['where'])
                lines += shown
            elif d.get('where'):
                lines.append('   **Where:**')
                for x in d['where']:
                    url = code_link(repo, head, x['path'], x['line_start'], x['line_end'])
                    lines.append(f"   - [`{short_name(x['path'])}` {rng_text(x)}]({url}): {x.get('what') or ''}")
            why_opt = d.get('option_reasons') or []
            lines += [f'   {LETTERS[j]}. {o}' + (f': {why_opt[j]}' if j < len(why_opt) and why_opt[j] else '')
                      for j, o in enumerate(d['options'][:len(LETTERS)])]
            rec = d.get('recommended', 0)
            lines.append(f"   **Recommended: {LETTERS[rec] if isinstance(rec, int) and 0 <= rec < len(LETTERS) else '?'}.** "
                         f"{d['reason']}")
            if others:  # R32: every place stays linked, out of the visible length (the mayor, 02:35Z)
                lines += ['', '<details><summary>Other places</summary>', ''] + others + ['', '</details>', '']
        lines.append('')
    if rv['items'] or rv['blockers']:
        lines.append(f'**For {author}:**')
        k = 0
        for k, it in enumerate(rv['items'], 1):
            old = ' (removed lines)' if it.get('side') == 'old' else ''
            url = diff_link(repo, n, it['path'], it.get('side', 'new'), it['line_start'], it['line_end'])
            label = KINDS[it['kind']].format(author=author)
            lines.append(f"{k}. **{it['change']}** · {label} · [`{it['path']}` {rng_text(it)}{old}]({url})"
                         f"{rule_ref(repo, rv, it.get('rule'))}")
        for j, b in enumerate(rv['blockers'], k + 1):
            lines.append(f"{j}. [{b['label']}]({b['url']}): {b['detail']}")
        lines.append('')
    if rv.get('cleared'):
        lines.append('**Checked and cleared:**')
        lines += [cleared_line(repo, n, rv, c) for c in rv['cleared']]
        lines.append('')
    if rv.get('skip'):
        lines += [f'**Safe to skip:** {skip_text(repo, n, rv["skip"])}', '']
    if rv['facts'] != GREEN_FACTS:
        lines.append(rv['facts'])
    return '\n'.join(lines).rstrip()


def place_link(repo, head, x):
    return f"[`{short_name(x['path'])}` {rng_text(x)}]({code_link(repo, head, x['path'], x['line_start'], x['line_end'])})"


def money_lines(repo, head, where):
    """R32: the places that change behaviour first, by risk, at most MONEY_SHOWN, each with its line, under
    one header per kind (visible); then the other places for the collapsed "Other places": the lower-risk
    ones with a few words each, the ones that change no behaviour as links. Every place stays linked;
    never a bare count. Returns (visible lines, collapsed lines)."""
    moving = sorted((x for x in where if x.get('behaviour', True)), key=lambda x: -(x.get('risk') or 3))
    shown, more = moving[:MONEY_SHOWN], moving[MONEY_SHOWN:]
    quiet = [x for x in where if not x.get('behaviour', True)]
    out, order = [], {k: j for j, k in enumerate(MONEY_KINDS)}
    for kind in sorted({x.get('kind') or 'other' for x in shown}, key=lambda k: order.get(k, len(order))):
        out.append(f'   **{kind.capitalize()}:**')
        out += [f"   - {place_link(repo, head, x)}: {x.get('what') or NOT_SUMMARIZED}"
                for x in shown if (x.get('kind') or 'other') == kind]
    other = [f"- {place_link(repo, head, x)}: {few_words(x.get('what') or NOT_SUMMARIZED)}" for x in more]
    if quiet:
        other.append('- No behaviour change: ' + ', '.join(place_link(repo, head, x) for x in quiet))
    return out, other


def few_words(text, n=8):
    w = text.split()
    return text if len(w) <= n else ' '.join(w[:n]).rstrip('.,;:') + ' …'


def opening_text(repo, head, d):
    """R32: the money decision's first line: "not checked" said once, then what the change does and its
    default state, followed by the places that back it (a claim without a marker is dropped upstream)."""
    places = [d['where'][j] for j in d.get('opening_places') or [] if 0 <= j < len(d['where'])]
    back = f" ({', '.join(place_link(repo, head, x) for x in places)})" if places and d.get('opening') else ''
    return 'These changes are not checked by any agent.' + (f" {d['opening'].rstrip('.')}{back}." if back else '')


def skip_text(repo, number, groups):
    """R30: the rest of the diff, each file linked, by group: "tests: [`a.ts`](…), [`b.ts`](…); types: …"."""
    parts, left = [], MAX_SKIP_LINKS
    for g in groups:
        shown = g['paths'][:max(left, 0)]
        left -= len(shown)
        links = ', '.join(f'[`{short_name(x)}`]({files_link(repo, number, x)})' for x in shown)
        rest = len(g['paths']) - len(shown)
        more = f"{', ' if links else ''}[{plural(rest, 'more file', 'more files')}](https://github.com/{repo}/pull/{number}/files)" if rest else ''
        parts.append(f"{g['group']}: {links}{more}")
    return '; '.join(parts)


def inline_comments(items, author):
    """One inline comment per item, on its exact lines (R4); a range becomes a multi-line comment."""
    out = []
    for k, it in enumerate(items, 1):
        side = 'RIGHT' if it.get('side', 'new') == 'new' else 'LEFT'
        label = KINDS[it['kind']].format(author=author)
        c = {'path': it['path'], 'line': it['line_end'], 'side': side,
             'body': f"**{k}. {it['change']}**: {it['why']}\n\n**{label}:** {it['ask']}"}
        if it['line_start'] != it['line_end']:
            c.update(start_line=it['line_start'], start_side=side)
        out.append(c)
    return out


def changed_range(idx, path, line):
    """R32: the changed range a not-checked line sits in: the added lines of its hunk (or the line alone
    when it is not in the diff), as {path, line_start, line_end, hunk}."""
    k = hunk_of(idx, path, line)
    if k is None:
        return {'path': path, 'line_start': line, 'line_end': line, 'hunk': None}
    h = idx[path]['hunks'][k]
    span = h[2] or h[0]
    return {'path': path, 'line_start': min(span), 'line_end': max(span), 'hunk': k}


def money_ranges(idx, located):
    """R32: each not-checked location once: one changed range per hunk, in diff order."""
    out, seen = [], set()
    for x in located:
        r = changed_range(idx, x['path'], x['line'])
        key = (r['path'], r['hunk'] if r['hunk'] is not None else r['line_start'])
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def not_checked_decision(ranges, words, author, overview='', overview_places=()):
    """R20, R32, R37: the PR touches an area no agent checks, so a person must confirm what it changes.
    `ranges`: [{path, line_start, line_end, what, kind, behaviour, risk}], each changed range with one
    neutral line (money_ranges, then pr_money). The question is the one the options answer (the mayor,
    02:35Z). `overview` (what the change does and its default state) is shown only when the places it names
    (`overview_places`, indexes into `ranges`) have their lines: a claim without a marker can be a model
    invention. The options never ask the reviewer to read code (R37); each has its reason, and the
    recommended one's task for the author goes into line 2."""
    first = ranges[0]
    backing = [j for j in overview_places if 0 <= j < len(ranges)
               and ranges[j].get('what') and ranges[j].get('what') != NOT_SUMMARIZED]
    return {'question': f'What sign-off does this change to {words} need before it merges?',
            'opening': overview if overview and backing else '', 'opening_places': backing if overview else [],
            'options': [f'{author} states in the PR description what changes for customers',
                        'A person who owns billing reviews the changes',
                        'Accept as is'],
            'option_reasons': ['a short written account puts on record what customers will see',
                               'they know the pricing rules this touches',
                               'only if whoever owns pricing has already agreed to it'],
            'recommended': 0,
            'reason': f'One paragraph from {author} puts on record what customers will see, and nobody has to read code.',
            'author_task': 'state in the PR description what changes for customers',
            'why': 'No agent checks money logic, so a person has to confirm what it changes.',
            'where': [{k: x.get(k) for k in ('path', 'line_start', 'line_end', 'what', 'kind', 'behaviour', 'risk')}
                      for x in ranges],
            'area': NOT_CHECKED_AREA, 'rule': '',
            'link': {'kind': 'code', 'path': first['path'], 'line_start': first['line_start'], 'line_end': first['line_end']},
            'verified': 'zero-token rule', 'confidence': 10, 'auto': True}


def floor_decision(places, author, why):
    """R33 (the mayor, 2026-10-30 02:11Z: "a hand-off with no options still forces Alex to read the PR"):
    the note's decision. `places`: [{path, line_start, line_end, what}] the zero-token rules know (the
    flagged hunks and the money places), each linked; `why`: the structural reason the review was held.
    Rule-level options; none asks the reviewer to read code (R37)."""
    d = {'question': 'Who reviews this PR before it merges?',
         'options': [f'{author} asks a teammate who knows this code to review it',
                     f'{author} states in the PR description what it changes and why that is safe, and you then decide',
                     'Accept it as is'],
         'option_reasons': ['a person who knows the code checks in one pass what our review could not',
                            'the author knows the change; a written account lets you decide without reading code',
                            'only if a wrong change here would be cheap to undo'],
         'recommended': 0, 'reason': 'A person who knows the code checks in one pass what our review could not.',
         'author_task': 'ask a teammate who knows this code to review it',
         'why': why, 'area': 'other', 'rule': '', 'verified': 'zero-token rules', 'confidence': 10, 'auto': True}
    if places:
        first = places[0]
        d['where'] = [{k: x.get(k) for k in ('path', 'line_start', 'line_end', 'what', 'kind')} for x in places]
        d['link'] = {'kind': 'code', 'path': first['path'], 'line_start': first['line_start'], 'line_end': first['line_end']}
    else:
        d['link'] = {'kind': 'description'}
    return d


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


def change_reasons(p, v, label=None):
    """R29: an entry leads with 3-6 plain words on what the PR changed, never a category label."""
    v = (v or '').strip()
    why = []
    if not 3 <= len(v.split()) <= 6:
        why.append(f'R29: {p} does not lead with 3-6 plain words on what the PR changed')
    elif v.lower() in AREAS or (label and v.lower() == label.lower()):
        why.append(f'R29: {p} leads with a category label, not with what the PR changed')
    elif IDENT_RE.search(v):
        why.append(f'R29: {p} names a raw identifier; say what changed in plain words')
    c = CODES_RE.search(v)
    if c:
        why.append(f'R10: {p} change has a rule code ("{c.group(0)}")')
    return why


def words_reasons(p, f, v, most=30):
    """R29: the one fact (or the one ask) is short."""
    n = len((v or '').split())
    return [f'R29: {p} {f} is {n} words, at most {most}'] if n > most else []


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
        for f in ('question', 'why', 'reason'):
            why += field_reasons(p, f, d.get(f))
        rec = d.get('recommended')
        if not isinstance(rec, int) or not 0 <= rec < len(d.get('options') or []):
            why.append(f'R16: {p} recommends none of its options')
        if any(REVIEW_CODE_RE.search(t or '') for t in list(d.get('options') or []) + list(d.get('option_reasons') or [])
               + [d.get('reason'), d.get('question')]):
            why.append(f'R37: {p} asks the reviewer to read or review code; decide at the level of rules and goals')
        if d.get('opening') and not d.get('opening_places'):
            why.append(f'R32: {p} opening names an effect no linked place backs')
        for j, x in enumerate(d.get('where') or [], 1):  # R32: every place, linked, with its line
            if not isinstance(x.get('line_start'), int) or not isinstance(x.get('line_end'), int):
                why.append(f'R32: {p} location {j} is not linked to its changed range')
            if d.get('area') != NOT_CHECKED_AREA or x.get('what') == NOT_SUMMARIZED:
                continue
            if len((x.get('what') or '').split()) < 3:
                why.append(f'R32: {p} location {j} does not say in one line what it changes')
            why += [r.replace('R29', 'R32') for r in words_reasons(f'{p} location {j}', 'line', x.get('what'), MONEY_WORDS)]
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
        why += change_reasons(p, it.get('change'))
        for f in ('why', 'ask'):
            why += field_reasons(p, f, it.get(f)) + words_reasons(p, f, it.get(f))
        if it.get('area') not in AREAS:
            why.append(f'R5: {p} area is not one of {AREAS}')
        if it.get('kind') == 'question' and not (it.get('ask') or '').rstrip().endswith('?'):
            why.append(f'R13: {p} is a question but its ask does not end with a question mark')
        # R21: each finding once: no two items on overlapping lines of a file, no repeated sentence.
        for j, o in seen:
            if o['path'] == it['path'] and o.get('side', 'new') == it.get('side', 'new') and \
                    o['line_start'] <= it['line_end'] and it['line_start'] <= o['line_end']:
                why.append(f'R21: {p} repeats the lines of item {j}; say each finding once')
            elif (o.get('change') or '').strip().lower() == (it.get('change') or '').strip().lower():
                why.append(f'R21: {p} repeats item {j}; say each finding once')
        seen.append((k, it))
    # R37: a finding in an area where the repository has a written rule cites it (rule link).
    rules = rv.get('rules') or {}
    ruled = {r['area'] for r in rules.values()} - {'other'}
    for p, e in [(f'decision {k}', d) for k, d in enumerate(decisions, 1) if not d.get('auto')] + \
            [(f'item {k}', it) for k, it in enumerate(items, 1)]:
        rid = e.get('rule') or ''
        if rid and rid not in rules:
            why.append(f"R37: {p} cites rule {rid!r}, which is not in the repository's digest")
        elif not rid and e.get('area') in ruled:
            why.append(f"R37: {p} is about {e.get('area')}, where the repository has a written rule, but cites none")
    by_id = {f['id']: f for f in flagged}
    seen_c = []
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
        else:
            for j, o in seen_c:
                if (o['path'], o['line_start'], o['line_end']) == (c['path'], c['line_start'], c['line_end']) or \
                        (o.get('change') or '').strip().lower() == (c.get('change') or '').strip().lower():
                    why.append(f'R36: {p} repeats cleared entry {j}')
            text = (idx.get(c['path']) or {}).get('text') or {}
            shown = [text[ln] for ln in range(c['line_start'], c['line_end'] + 1) if (text.get(ln) or '').strip()]
            if shown and all(COMMENT_RE.match(t) for t in shown):
                why.append(f'R36: {p} links only comment lines; link the code that makes it safe')
            seen_c.append((k, c))
        why += [r.replace('R5:', 'R19:', 1) for r in field_reasons(p, 'why', c.get('why'))]
        why += change_reasons(p, c.get('change'), f['label']) + words_reasons(p, 'why', c.get('why'))
        if c.get('rule') and c['rule'] not in (rv.get('rules') or {}):
            why.append(f"R37: {p} cites rule {c['rule']!r}, which is not in the repository's digest")
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
    needs only outcome, the counts and not_checked; `facts` None = not known). With `comments`, each item
    has its inline comment (R4)."""
    why = []
    n_dec, n_items = len(rv.get('decisions') or []), len(rv.get('items') or [])
    n_block, n_clear = len(rv.get('blockers') or []), len(rv.get('cleared') or [])
    lines = body.splitlines()
    word = word_of(rv)
    m1 = LINE1_RE.match(lines[0] if lines else '')
    if not m1:
        why.append(f'R28: line 1 is not the verdict and the automated note ("**{word}**' + SUFFIX.format(name='<reviewer>') + '")')
    elif m1.group(1) != word:
        rule = 'R35' if rv['outcome'] == 'changes' else 'R1'
        why.append(f'{rule}: line 1 says "{m1.group(1)}" for {rv["outcome"]} ({event_of(rv)} review)')
    l2 = lines[1] if len(lines) > 1 else ''
    m, nothing = WHO_RE.match(l2), NOTHING_RE.match(l2)
    asks = n_items + n_block
    if nothing:
        if n_dec or asks or rv.get('not_checked'):
            why.append('R22: line 2 says nothing for anyone, but the review has decisions or asks')
    elif not m:
        why.append('R22: line 2 does not say who acts ("<reviewer>: ... <author>: ..." or "Nothing for <reviewer> or <author>.")')
    else:
        if int(m.group(3) or 0) != n_dec:
            why.append(f'R22: line 2 counts {m.group(3) or 0} decisions, the review has {n_dec}')
        tasks = [x for x in rv.get('decisions') or [] if x.get('author_task')]
        for x in tasks:
            if f"picks {LETTERS[x['recommended']]}, {x['author_task']}" not in m.group(7):
                why.append("R22: line 2 does not name the task the recommended option gives the author")
        asks += len(tasks)
        if (m.group(7) == 'nothing') != (asks == 0):
            why.append(f'R22: line 2 says the author has {m.group(7)!r}, the review has {asks} asks')
        if not n_dec and not asks:
            why.append('R22: line 2 is "Nothing for <reviewer> or <author>." when nobody acts')
    rest = [l for l in lines[2:] if l.strip()]
    if rv.get('approve'):
        if rest and not (rest[0] == '<details><summary>What was checked</summary>' and rest[-1] == '</details>'
                         and all(l.startswith('- ') for l in rest[1:-1])):
            why.append('R36: an approval is line 1, line 2 and the collapsed "What was checked", nothing else')
        if not nothing:
            why.append('R36: line 2 of an approval is "Nothing for <reviewer> or <author>."')
    elif re.sub(r'<details><summary>Other places</summary>', '', body).count('<details>'):
        why.append('R23: a collapsed section other than the money places in a review that is not an approval')
    if re.search(r'^   \*\*Other places:\*\*', body, re.M):
        why.append('R32: the other money places are not collapsed (they go in "Other places" after the recommendation)')
    if rv['outcome'] != 'low-risk' or n_clear:
        links = re.findall(r'\]\(https://github\.com/[^)]+/(?:pull/\d+/files#diff-[0-9a-f]{64}[RL]\d+(?:-[RL]\d+)?|'
                           r'blob/[0-9a-f]{40}/[^)#]+#L\d+(?:-L\d+)?|pull/\d+(?:/checks|/conflicts)?)\)', body)
        if not links or len(links) < n_items + n_dec + n_block + n_clear:
            why.append(f'R3: {len(links)} links to lines or text for {n_items + n_dec + n_block + n_clear} entries')
    summary = re.search(r'^\*\*What it does:\*\* (.*)$', body, re.M)
    if rv.get('model') and not rv.get('approve') and not (summary and summary.group(1).strip()):
        why.append('R7: no "What it does" line')
    if rv['outcome'] == 'not-reviewed' and n_dec != 1:
        why.append('R33: the note does not carry its one decision for the reviewer (who reviews it, with options)')
    skip = re.search(r'^\*\*Safe to skip:\*\* (.*)$', body, re.M)
    if skip:
        groups = [g for g in skip.group(1).split('; ') if g.strip()]
        if any(not re.search(r'\]\(https://github\.com/[^)]+/pull/\d+/files(#diff-[0-9a-f]{64})?\)', g) for g in groups):
            why.append('R30: a "Safe to skip" group does not link its files')
    fl = next((l for l in lines if FACTS_RE.match(l)), None)
    facts = rv.get('facts')
    if fl == GREEN_FACTS:
        why.append('R30: the status line on a PR where everything passed; show it only when something did not')
    elif fl is None and facts is not None and facts != GREEN_FACTS:
        why.append('R8: no status line, and something did not pass')
    if fl and fl != GREEN_FACTS:
        if m or nothing:  # R22: a blocker in the status line is a linked ask for the author in line 2
            missing = [short for cue, short in FACT_ASKS if cue in fl and short not in (m.group(7) if m else '')]
            if missing:
                why.append(f"R22: the status line has a blocker but line 2 does not ask for it: {', '.join(missing)}")
        if rest and rest[-1] != fl:
            why.append('R9: the status line is not the last line')
    block = body.split('   **What changes:**', 1)[1].split('\n   A. ', 1)[0] if '   **What changes:**' in body else ''
    one_by_one = re.findall(r'^   - .*\]\(https://github\.com/[^)]+/blob/[0-9a-f]{40}/[^)]+\): \S', block, re.M)
    if len(one_by_one) > MONEY_SHOWN:
        why.append(f'R32: {len(one_by_one)} money places listed one by one; show at most {MONEY_SHOWN}, the rest in a '
                   f'linked line')
    said = len(re.findall(r'(?i)\bnot checked\b', body))
    if rv.get('not_checked') and said != 1:
        why.append(f'R32: "not checked" is said {said} times; say it once, in line 2')
    elif not rv.get('not_checked') and said:
        why.append('R23: "not checked" on a PR that does not touch that area')
    if re.search(r'(?i)shadow mode|review agents', '\n'.join(lines[1:])):
        why.append('R9: boilerplate in the body')
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
    shown = len(re.sub(r'\]\([^)]*\)', ']', body))  # what a reader reads: link targets do not count
    if shown > MAX_BODY:
        why.append(f'R14: the text is {shown} characters, at most {MAX_BODY}')
    return why


# R22: what the facts line says -> the ask line 1 must carry (the `short` of blockers()).
FACT_ASKS = [(' · merge conflict · ', 'resolve the merge conflict'), ('CI red', 'fix the failing checks'),
             ('No CI ran', 'get CI running'), ('CI skipped', 'get the checks to run'), ('CI still running', 'get CI to finish')]
FACTS_RE = re.compile(r'^(CI passed|CI red|No CI ran|CI skipped|CI still running).* · (no )?merge conflict · \w+$')
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
            'summary': fx.get('summary', ''), 'skip': fx.get('skip', []),
            'facts': fx.get('facts', 'CI passed · no merge conflict · open'),
            'not_checked': fx.get('not_checked', ''), 'model': fx.get('model', True),
            'request_changes': fx.get('request_changes', False), 'rules': fx.get('rules', {}),
            'rules_sha': 'c' * 40}


def route(fx):
    """A routing case (R33, R34, the sweep's review requests): the decision function's answer for the
    fixture's inputs must be `want`. Returns the reasons it is not."""
    import pr_rules
    got = getattr(pr_rules, fx['route'])(**fx['args'])
    return [] if got == fx['want'] else [f"{fx.get('rule', 'route')}: {fx['route']} gave {got!r}, want {fx['want']!r}"]


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
            if 'route' in fx:
                reasons = route(fx)
            elif 'body' in fx:
                rv = {'outcome': fx['outcome'], 'decisions': [{}] * fx.get('n_decisions', 0),
                      'items': [{}] * fx.get('n_items', 0), 'not_checked': fx.get('not_checked', ''),
                      'approve': fx.get('approve', False), 'model': fx.get('model', True),
                      'request_changes': fx.get('request_changes', False), 'facts': fx.get('facts')}
                reasons = gate_body(fx['body'], rv)
            else:
                idx = diff_index(fx['diff'])
                pr = {'number': 1, 'state': 'OPEN', 'mergeable': 'MERGEABLE', 'url': 'https://github.com/example/repo/pull/1',
                      'headRefOid': 'a' * 40}
                rv = review_of(fx)
                body = render('example/repo', pr, rv, 'Reviewer', 'Author')
                if fx.get('body_edit'):  # a rendered body broken on purpose
                    body = body.replace(*fx['body_edit'])
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
            if fx.get('judge_risky'):  # R31: cleared entries the reader found risky without a reason
                reasons.append(f"R31: cleared entries {fx['judge_risky']} describe a possible risk without a reason")
            verdict = 'fail' if reasons else 'pass'
            need = set(fx.get('expect_reasons', []))
            got = {r.split(':')[0] for r in reasons}
            ok = verdict == fx['expect'] and need <= got and ('only_reasons' not in fx or got <= set(fx['only_reasons']))
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
