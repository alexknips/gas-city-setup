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
not_checked (the words for a not-checked area the PR touches, or ''), model (whether a model read the PR),
names ([reviewer, author], for the check that each option starts with who carries it out).

`pr_format.py --regress DIR...` runs the gate over regression fixtures (README.md, "Quality loop").
`pr_format.py --regate FILE` gates the held attempts pr_review kept (feedback/held.jsonl) again with this
code, at zero tokens; it prints PR numbers, attempts and rule ids only.
"""
import hashlib
import json
import os
import re
import sys

import pr_incremental

AREAS = ['patient data', 'security', "can't be undone", 'migration order', 'behaviour patients notice',
         'product', 'compliance', 'other']
NOT_CHECKED_AREA = 'not checked'  # the area of the decision R20 adds; the model never uses it
KINDS = {'question': 'Ask {author}', 'change': 'Change'}
# Who carries out a decision's option (decision-554d2e7b, the reviewer 2026-10-30: "you wrote a review and the author
# just needs to answer, not me"): the reviewer, the author, or someone the author picks (a teammate, the
# owner of billing). A decision none of whose options is the reviewer's is a question for the author.
ACTORS = ('reviewer', 'author', 'picked')
# R28: line 1 is the verdict, then the honesty note as a suffix (the review is posted under the reviewer's
# login, so the author must see it is automated): "**Approved as low risk** · automated, <reviewer> hasn't read this PR".
SUFFIX = " · automated, {name} hasn't read this PR"
# R33: what a requested PR gets when its review was held: never silence.
NOT_REVIEWED = '**Not reviewed automatically.**'  # how the notes of v4.2-v5 began
NOT_SUMMARIZED = '(not summarized)'  # R32: a money place the line reader gave no line, still linked
# How our reviews begin: earlier bodies with a marker, v5 bodies with the verdict and the suffix.
MARKERS = ('Automated review', 'Automated approval', NOT_REVIEWED)
OURS_RE = r'^\*\*[A-Z][A-Za-z ]+\*\* · automated, '
# decision-ed7320b7 (the reviewer, 2026-10-30 06:05Z: "just act as if you are me", on the repositories the
# town's override names): with voice 'reviewer' the review speaks as the reviewer. Line 1 is the verdict
# word, then an invisible, neutral marker by which we know our own reviews; no disclosure line.
MARK = '<!-- pr-review v5.6 -->'
MARK_PREFIX = '<!-- pr-review '
VOICE_WORDS = {'changes': 'Changes requested', 'questions': 'Questions', 'low-risk': 'Low risk',
               'not-reviewed': 'Questions'}
VOICE_APPROVED = 'Approved'
NOTHING_NEEDED = '{author}: nothing needed.'  # line 2 names the author (the mayor, 2026-10-30 07:21Z)
NOTHING_NEEDED_RE = re.compile(r'^(.+?): nothing needed\.$')
YOU = 'You'  # how an option for the author starts in the reviewer's voice
# R38: what a review in the reviewer's voice never says. Phrases, not words: a PR may add "automated tests"
# or change how a team uses Claude Code, and "AI" is allowed (code and features use the word).
DISCLOSURE_RE = re.compile(r"(?i)\bautomated(,| (reviews?|approvals?|notes?)\b)|\bhasn't read\b|\bhas not read\b|"
                           r"\breview agents?\b|\bclaude code('s)? (reviews?|agents?)\b|\breviewed by claude\b|"
                           r'\bnot reviewed automatically\b|\bnot checked by any agent\b')
# decision-3aeec149 (06:09Z: "needs me should be reserved for escalations that cannot be escalated to [the
# author], such as potential gaps in our review methodology"): every finding goes to the author; a gap in
# our method goes to the reviewer by Slack ("Review gap"), never on the PR. The kinds: a repository rule no
# check can verify, a kind of risk the method does not handle (both from the model), a review that could
# not reach its bar (the R33 floor), a not-checked area (billing; accepted by the reviewer, so said as scope).
GAP_KINDS = ('unverifiable-rule', 'new-risk', 'below-bar', 'not-checked')
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
# R33: a fallback (the money review or the note, made when the full review was held) passes the bar the
# full review failed (the mayor, 2026-10-30 10:25Z): no larger than the largest full reviews that passed
# the reader test (7 links, 17 lines, 2,085 characters shown, 10-29..10-30); its money places beyond the
# question's own, at most FALLBACK_SHOWN, then one link to the files view.
FALLBACK_LINKS, FALLBACK_LINES, FALLBACK_CHARS, FALLBACK_SHOWN = 8, 17, 2100, 2
# R33 (the mayor, 2026-10-30 21:39Z, after 4 of 5 requested reviews fell back: "One specific question per linked
# line with its own reason, no boilerplate sentences, no orphan link, one status line"): the fallback asks about
# at most FALLBACK_QUESTIONS places, one question each (pr_fallback); the places it does not ask about are
# collapsed, each linked to its lines; in the reviewer's voice line 1 says who acts.
FALLBACK_QUESTIONS = 3
VOICE_FALLBACK = ' · for {author}, nothing needed from me'
# A question that fits any PR is boilerplate, not a question about its lines (R33).
STOCK_RE = re.compile(r'(?i)^(is|was|has) (this|that|it)( change| code)?( been)? (intended|tested|safe|correct|right|'
                      r'expected|deliberate|reviewed)( here)?\?$|\b(have|has|did|was|were) (you|this|it|anyone|they|'
                      r'this change|the change|this code)( been)? test(ed)?\b|\bwho (reviews|should review|signs? off|approves)\b|\bwhat sign-?off\b|'
                      r'\bbefore (it|this) merges\b|\bcan you (explain|clarify|describe)\b|'
                      r'\bwhat does this (change|code) do\b|\bany tests?\b')
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
# R22: line 2, who acts: "<reviewer>: 1 decision. <author>: 1 change, get CI running." or "Nothing for <reviewer> or <author>."
WHO_RE = re.compile(r'^(.+?): (nothing|(\d+) decisions?)( \((.+)\))?\. (.+?): (.+)\.$')
NOTHING_RE = re.compile(r'^Nothing for (.+) or (.+)\.$')
# The same in the reviewer's voice (decision-ed7320b7): the verdict word and the marker; what the author must do.
VOICE_LINE1_RE = re.compile(r'^\*\*(Approved|Changes requested|Changes suggested|Questions|Low risk)\*\*'
                            r'( · for (.+), nothing needed from me)? <!-- pr-review v[0-9.]+ -->$')
VOICE_WHO_RE = re.compile(r'^(.+?): (.+)\.$')
# R37: a decision never asks the reviewer to read or review code (the reviewer does not know the codebase).
REVIEW_CODE_RE = re.compile(r'(?i)\byou(rself)? (review|read|check|look at)\b|\b(review|read|check)\b[^.?]{0,40}\byourself\b')
# R29: an entry's change is plain words, never a raw identifier.
IDENT_RE = re.compile(r'`|\b[a-z]+[A-Z][A-Za-z0-9]*\b|\b[A-Za-z]+_[A-Za-z0-9_]+\b|\w\(\)|::|->')
# R36: a link that lands only on comment lines does not show why the code is safe.
COMMENT_RE = re.compile(r'^\s*(//|#|/\*|\*|<!--|--|"""|\'\'\')')
# In a markdown file a heading, a bullet or a rule is the text itself; only an HTML comment is a comment.
MARKDOWN_EXT = ('.md', '.mdx', '.markdown')


def is_comment(path, line):
    """R36: the line is a comment in its file's language (by its first characters)."""
    if (path or '').lower().endswith(MARKDOWN_EXT):
        return line.lstrip().startswith('<!--')
    return bool(COMMENT_RE.match(line))


# R24: a review a newer one replaces becomes a pointer to it, and every pointer of ours on a PR follows the
# newest review (the mayor, 2026-10-30 07:21Z). Both wordings are pointers ("of this commit" until v5.8).
POINTER = 'Replaced by [the newest review]'
POINTER_PREFIX = 'Replaced by [the new'
POINTER_RE = re.compile(r'Replaced by \[the (?:newest review|new review of this commit)\]\(([^)\s]+)\)')


def pointer_body(name, new_url, voice='disclose'):
    """What an earlier review becomes when a newer one replaces it (R24)."""
    if voice == 'reviewer':
        return f"**Replaced** {MARK}\n\n{POINTER}({new_url})."
    return f"**Replaced**{SUFFIX.format(name=name)}\n\n{POINTER}({new_url})."


def repoints(reviews, name, voice='disclose'):
    """R24: [(review id, new body)] for each pointer of ours on a PR that is older than the newest whole review
    and points elsewhere. `reviews`: our reviews on the PR, [{id, body, html_url}]."""
    whole = [r for r in reviews if not POINTER_RE.search(r.get('body') or '')]
    if not whole:
        return []
    newest = max(whole, key=lambda r: int(r['id']))
    out = []
    for r in reviews:
        m = POINTER_RE.search(r.get('body') or '')
        if m and int(r['id']) < int(newest['id']) and m.group(1) != newest['html_url']:
            out.append((r['id'], pointer_body(name, newest['html_url'], voice)))
    return out


def is_ours(body):
    """Our review by its body: the earlier visible markers, or the invisible one on line 1 (v5.6)."""
    b = body or ''
    return b.startswith(MARKERS) or bool(re.match(OURS_RE, b)) or MARK_PREFIX in b.split('\n', 1)[0]


def ours_jq():
    """A jq test for "this review body is ours" (every version)."""
    return ' or '.join([f'(.body | startswith("{m}"))' for m in MARKERS] +
                       [f'(.body | test("{OURS_RE.replace(chr(92), chr(92) * 2)}"))',
                        f'(.body | split("\\n") | .[0] | contains("{MARK_PREFIX}"))'])


def voiced(rv):
    """Whether the review speaks as the reviewer (decision-ed7320b7)."""
    return rv.get('voice') == 'reviewer'


def open_gaps(rv):
    """The review's gaps in our method that the reviewer has not accepted (decision-3aeec149)."""
    return [g for g in rv.get('gaps') or [] if not g.get('accepted')]


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


def for_reviewer(d):
    """Whether the reviewer carries out one of the decision's options (decision-554d2e7b); None for a
    decision without actors (one delivered before v5.5)."""
    a = d.get('actors')
    return None if a is None else 'reviewer' in a


def split(decisions):
    """(the reviewer's decisions, the author's): a decision none of whose options is the reviewer's is a
    question for the author (decision-554d2e7b)."""
    return [d for d in decisions if for_reviewer(d) is not False], [d for d in decisions if for_reviewer(d) is False]


def decide(decisions, items, blockers_=()):
    """R16: Needs you only with a decision the reviewer carries out an option of. Otherwise changes (a medium
    or high change item, or any change), questions (only questions, a decision whose options are all the
    author's among them), or low risk (nothing at all: no item, no blocker; a blocker means CI did not pass
    or the branch conflicts)."""
    mine, theirs = split(decisions)
    if mine:
        return 'escalate'
    asks = list(items) + list(blockers_) + [{'kind': 'question'} for _ in theirs]
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
    d, theirs = split(rv['decisions'])
    n_change = sum(1 for i in rv['items'] if i['kind'] == 'change')
    n_q = sum(1 for i in rv['items'] if i['kind'] == 'question') + len(theirs)
    parts = ([plural(n_change, 'change', 'changes')] if n_change else []) + \
            ([plural(n_q, 'question', 'questions')] if n_q else []) + [b['short'] for b in rv['blockers']] + \
            [f"if {name} picks {LETTERS[x['recommended']]}, {x['author_task']}" for x in d if x.get('author_task')]
    if voiced(rv):  # what the author must do, by name, or nothing (decision-ed7320b7)
        return f"{author}: {', '.join(parts)}." if parts else NOTHING_NEEDED.format(author=author)
    if not d and not parts:
        return f'Nothing for {name} or {author}.'
    you = plural(len(d), 'decision', 'decisions') if d else 'nothing'
    return f"{name}: {you}. {author}: {', '.join(parts) or 'nothing'}."


def word_of(rv):
    """Line 1's outcome word (R1), matching the review's event (R35); in the reviewer's voice the R33 note
    is their question to the author."""
    if rv.get('approve'):
        return VOICE_APPROVED if voiced(rv) else APPROVED
    if rv['outcome'] == 'changes' and not rv.get('request_changes'):
        return SUGGESTED
    return (VOICE_WORDS if voiced(rv) else WORDS)[rv['outcome']]


def event_of(rv):
    """The review's GitHub event: APPROVE under the bar (R27), REQUEST_CHANGES for changes when the reviewer
    allows it (R35), else COMMENT (questions, escalations, low risk without approval, the R33 note)."""
    if rv.get('approve'):
        return 'APPROVE'
    if rv['outcome'] == 'changes' and rv.get('request_changes'):
        return 'REQUEST_CHANGES'
    return 'COMMENT'


def author_asks(rv):
    """What the author has to do: items, blockers and their decisions."""
    return len(rv.get('items') or []) + len(rv.get('blockers') or []) + len(split(rv.get('decisions') or [])[1])


def answers_text(rv, author):
    """decision-57b6d9bb: the follow-up's line on what became of the asks after the author's answers."""
    a = rv['answers']
    parts = ([f"{a['answered']} answered"] if a.get('answered') else []) + \
        ([f"{a['open']} still open" + (' in its thread' if a['open'] == 1 else ' in their threads')] if a.get('open') else []) + \
        ([f"{a['change']} now {'a change' if a['change'] == 1 else 'changes'}"] if a.get('change') else [])
    who = 'your' if voiced(rv) else f"{author}'s"
    return f"**Since {who} answers:** {', '.join(parts) or 'nothing changed'}."


def headline(rv, name, author=''):
    """R28: line 1, the verdict, then the automated note; in the reviewer's voice the verdict and the
    invisible marker (decision-ed7320b7)."""
    if voiced(rv):  # a fallback says who acts (R33): the author answers, the reviewer has nothing to do
        said = VOICE_FALLBACK.format(author=author) if rv.get('fallback') and author_asks(rv) else ''
        return f"**{word_of(rv)}**{said} {MARK}"
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
    for the author, the decisions whose options are all theirs (decision-554d2e7b), the asks with links
    to their lines (each item is also an inline comment) and the blockers; what was checked and cleared (R19, R29); safe to skip with the files view (R30); and the
    status line only when something did not pass (R30)."""
    n = pr['number']
    lines = [headline(rv, name, author), who_acts(rv, name, author)]
    since = pr_incremental.text(repo, rv['since']) if rv.get('since') else ''  # gc-sy1hm.2 c
    if rv.get('approve'):
        if approval_reason(rv):  # the one-line reason, visible, not only inside the collapsed section
            lines += ['', approval_reason(rv)]
        if rv.get('cleared') or since:
            lines += ['', '<details><summary>What was checked</summary>', '']
            lines += ([f'- {since}'] if since else []) + [cleared_line(repo, n, rv, c) for c in rv.get('cleared') or []]
            lines += ['', '</details>']
        return '\n'.join(lines)
    lines.append('')
    if rv.get('held_why'):  # the note's questions in the disclosing voice: why the review was held (R33)
        lines += [rv['held_why'], '']
    if rv.get('summary'):
        lines += [f"**What it does:** {rv['summary']}", '']
    if since:
        lines += [since, '']
    if rv.get('answers'):  # decision-57b6d9bb: a follow-up on the author's answers
        lines += [answers_text(rv, author), '']
    mine, theirs = split(rv['decisions'])
    if mine:
        lines.append(f'**For {name}:**')
        for k, d in enumerate(mine, 1):
            lines += decision_lines(repo, pr, rv, k, d)
        lines.append('')
    if theirs or rv['items'] or rv['blockers']:
        # A decision whose options are all the author's is their question, first (decision-554d2e7b).
        lines.append(f'**For {author}:**')
        for k, d in enumerate(theirs, 1):
            lines += decision_lines(repo, pr, rv, k, d)
        k = len(theirs)
        for k, it in enumerate(rv['items'], k + 1):
            old = ' (removed lines)' if it.get('side') == 'old' else ''
            url = diff_link(repo, n, it['path'], it.get('side', 'new'), it['line_start'], it['line_end'])
            label = 'Question' if voiced(rv) and it['kind'] == 'question' else KINDS[it['kind']].format(author=author)
            # R4: the body item states its ask; its inline comment carries the evidence (the mayor, 06:4xZ).
            lines.append(f"{k}. **{it['change']}** · {label}: {it['ask']} · [`{it['path']}` {rng_text(it)}{old}]({url})"
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
    if rv.get('fallback') and rv.get('others'):
        lines += other_places(repo, pr['headRefOid'], rv['others'], rv.get('not_checked')) + ['']
    if rv['facts'] != GREEN_FACTS and not blockers_say(rv):
        lines.append(rv['facts'])
    return '\n'.join(lines).rstrip()


def other_places(repo, head, places, words=''):
    """R33: the places a fallback does not ask about, collapsed, each linked to its lines (never a link to the
    files view that no line anchors), one line per group: the not-checked area, a rule's label, the rest."""
    return ['<details><summary>Other places</summary>', ''] + grouped_links(repo, head, places, words) + ['', '</details>']


def grouped_links(repo, head, places, words=''):
    """One line per group of places, each place linked to its lines: "- <group>: [`a.ts` L3](…), …"."""
    groups = {}
    for x in places:
        g = (words or NOT_CHECKED_AREA) if x.get('money') else (x.get('what') or 'other changes')
        groups.setdefault(g, []).append(place_link(repo, head, x))
    return [f"- {g[:1].upper()}{g[1:]}: {', '.join(links)}" for g, links in groups.items()]


def blockers_say(rv):
    """R33 (one status line): a fallback whose every part of the status line that did not pass is already a
    blocker, on an open PR, shows no status line; the blocker says it once, linked."""
    if not rv.get('fallback') or not rv.get('blockers'):
        return False
    parts, causes = (rv.get('facts') or '').split(' · '), {b.get('cause') for b in rv['blockers']}
    return len(parts) == 3 and (parts[0] == 'CI passed' or 'ci' in causes) and \
        (parts[1] == 'no merge conflict' or 'conflict' in causes) and parts[2] == 'open'


def approval_reason(rv):
    """R36: an approval's one line on why, above "What was checked": the one cleared entry's reason, else
    what the PR does."""
    cleared = rv.get('cleared') or []
    text = (cleared[0].get('why') if len(cleared) == 1 else '') or rv.get('summary') or ''
    text = ' '.join(text.split())  # one line, whatever the model's text holds
    return text[:1].upper() + text[1:] + ('' if text.endswith(('.', '!', '?')) else '.') if text else ''


def decision_lines(repo, pr, rv, k, d):
    """One decision's block (R16, R32, R37), the decision block first (the mayor, 2026-10-30 03:05Z): for
    money, the plain risk sentence, then the question, the options on lettered lines (each starts with who
    carries it out) and the recommendation; the evidence after them."""
    head, lines = pr['headRefOid'], []
    money = d.get('area') == NOT_CHECKED_AREA
    key = money_key(d) if money else None
    if money and rv.get('fallback'):  # R33: the opening backed by at most FALLBACK_SHOWN places
        d = dict(d, opening_places=(d.get('opening_places') or [])[:FALLBACK_SHOWN])
    if money:  # the question first, linked to the line it depends on; that place is linked there only (R21)
        link = f" · {place_link(repo, head, d['where'][key])}" if key is not None else ''
        lines += [f"{k}. **{d['question']}**{link}", '   ' + opening_text(repo, head, d, skip=key)]
    else:
        opener = '' if d.get('where') else f" · [{link_text(d)}]({decision_link(repo, pr, d)})"
        lines.append(f"{k}. **{d['question']}**{opener}")
        # A model's `why` says why only the reviewer can decide: not shown once the question is the author's.
        if d.get('why') and (d.get('auto') or for_reviewer(d) is not False):
            lines.append(f"   {d['why']}")
    if rule_ref(repo, rv, d.get('rule')):
        lines.append(f"   Rule{rule_ref(repo, rv, d.get('rule'))[len(' · rule'):]}")
    why_opt = d.get('option_reasons') or []
    lines += [f'   {LETTERS[j]}. {o}' + (f': {why_opt[j]}' if j < len(why_opt) and why_opt[j] else '')
              for j, o in enumerate(d['options'][:len(LETTERS)])]
    rec = d.get('recommended', 0)
    lines.append(f"   **Recommended: {LETTERS[rec] if isinstance(rec, int) and 0 <= rec < len(LETTERS) else '?'}.** "
                 f"{d['reason']}")
    others = []
    if d.get('where') and money:
        backed = {j for j in d.get('opening_places') or []} if d.get('opening') else set()
        rest = [x for j, x in enumerate(d['where']) if j not in backed | {key}]
        # R33: a fallback shows the places the question needs; the rest are collapsed, each linked to its lines
        shown, others = money_lines(repo, head, rest, FALLBACK_SHOWN if rv.get('fallback') else MONEY_SHOWN)
        lines += [''] + shown if shown else []
    elif d.get('where'):
        lines.append('   **Where:**')
        shown = d['where'][:FALLBACK_SHOWN + 1] if rv.get('fallback') else d['where']  # R33: the note's places, cut
        for x in shown:
            lines.append(f"   - {place_link(repo, head, x)}: {x.get('what') or ''}")
        others = grouped_links(repo, head, d['where'][len(shown):])  # R33: collapsed, each linked to its lines
    if others:  # R32: every place stays linked, once, out of the visible length
        lines += ['', '<details><summary>Other places</summary>', ''] + others + ['', '</details>', '']
    return lines


def money_key(d):
    """R32: the place a money question depends on: the riskiest one that changes behaviour, else the first."""
    where = d.get('where') or []
    if not where:
        return None
    moving = [j for j, x in enumerate(where) if x.get('behaviour', True)]
    return min(moving or range(len(where)), key=lambda j: -(where[j].get('risk') or 3))



def place_link(repo, head, x):
    return f"[`{short_name(x['path'])}` {rng_text(x)}]({code_link(repo, head, x['path'], x['line_start'], x['line_end'])})"


def money_lines(repo, head, where, most=MONEY_SHOWN):
    """R32: the places that change behaviour first, by risk, at most `most`, each with its line, under
    one header per kind (visible); then the other places for the collapsed "Other places": the lower-risk
    ones with a few words each, the ones that change no behaviour as links. Every place stays linked;
    never a bare count. Returns (visible lines, collapsed lines)."""
    moving = sorted((x for x in where if x.get('behaviour', True)), key=lambda x: -(x.get('risk') or 3))
    shown, more = moving[:most], moving[most:]
    quiet = [x for x in where if not x.get('behaviour', True)]
    out, order = [], {k: j for j, k in enumerate(MONEY_KINDS)}
    for kind in sorted({x.get('kind') or 'other' for x in shown}, key=lambda k: order.get(k, len(order))):
        out.append(f'**{kind.capitalize()}:**')  # not nested under the question: it would read as more asks
        out += [f"- {place_link(repo, head, x)}: {x.get('what') or NOT_SUMMARIZED}"
                for x in shown if (x.get('kind') or 'other') == kind]
    other = [f"- {place_link(repo, head, x)}: {few_words(x.get('what') or NOT_SUMMARIZED)}" for x in more]
    if quiet:
        other.append('- No behaviour change: ' + ', '.join(place_link(repo, head, x) for x in quiet))
    return out, other


def few_words(text, n=8):
    w = text.split()
    return text if len(w) <= n else ' '.join(w[:n]).rstrip('.,;:') + ' …'


def opening_text(repo, head, d, skip=None):
    """R32: the line under the money question: "not checked" said once, then what the change does and its
    default state, followed by the places that back it (a claim without a marker is dropped upstream; the
    place the question links, `skip`, is linked there only)."""
    places = [d['where'][j] for j in d.get('opening_places') or [] if 0 <= j < len(d['where']) and j != skip]
    back = f" ({', '.join(place_link(repo, head, x) for x in places)})" if places and d.get('opening') else ''
    first = d.get('scope') or 'These changes are not checked by any agent.'
    backed = d.get('opening') and (places or skip in (d.get('opening_places') or []))
    return first + (f" {d['opening'].rstrip('.')}{back}." if backed else '')


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


def self_answered(items, ns, flagged=(), idx=None):
    """R31's converse (the mayor, 2026-10-30 06:4xZ): a question whose own explanation answers it is not
    a question. The new-side question items the reader names (`ns`: 1 = the first item) become cleared
    entries with the item's own reason, keeping the flag they covered. Returns (items, cleared entries)."""
    keep, out = [], []
    for k, it in enumerate(items, 1):
        if k in ns and it.get('kind') == 'question' and it.get('side', 'new') == 'new':
            f = next((f for f in flagged if idx is not None and covers(idx, f, [it])), None)
            out.append({'flag': f['id'] if f else None, 'path': it['path'], 'line_start': it['line_start'],
                        'line_end': it['line_end'], 'change': it['change'], 'why': it['why'],
                        'rule': it.get('rule') or '', 'answered': True})
        else:
            keep.append(it)
    return keep, out


# The flow fix (the mayor, 2026-10-30 16:10Z): gate reasons whose right form the script makes from the
# answer's own parts. A review held by these only is repaired and gated again instead of falling back;
# any other reason (coverage, plain words, a comment link, the reader test) still falls back.
REPAIRABLE = {
    'R13': re.compile(r'^R13: item (\d+) is a question but its ask does not end with a question mark$'),
    'R36': re.compile(r'^R36: cleared entry (\d+) repeats cleared entry \d+$'),
    'R37': re.compile(r"^R37: (decision|item|cleared entry) (\d+) cites rule .+, which is not in the repository's digest$"),
}
REPAIR_GROUP = {'decision': 'decisions', 'item': 'items', 'cleared entry': 'cleared'}


def format_only(reasons):
    """There are reasons, and repair() fixes every one of them."""
    return bool(reasons) and all(any(p.match(r) for p in REPAIRABLE.values()) for r in reasons)


def repair(rv, reasons):
    """The review with what format_only() reasons name made right from its own parts: a question's ask ends
    in "?" (R13), a cleared entry that repeats an earlier one is dropped (R36), the citation of a rule the
    digest does not hold is dropped (R37). The caller gates the result again. Returns (rv, rule ids)."""
    rv = dict(rv, **{g: [dict(x) for x in rv.get(g) or []] for g in REPAIR_GROUP.values()})
    drop, fixed = set(), set()
    for r in reasons:
        m13, m36, m37 = (REPAIRABLE[k].match(r) for k in ('R13', 'R36', 'R37'))
        if m13:
            it = rv['items'][int(m13.group(1)) - 1]
            it['ask'] = (it.get('ask') or '').rstrip().rstrip('.!:;') + '?'
            fixed.add('R13')
        elif m36:
            drop.add(int(m36.group(1)) - 1)
            fixed.add('R36')
        elif m37:
            rv[REPAIR_GROUP[m37.group(1)]][int(m37.group(2)) - 1]['rule'] = ''
            fixed.add('R37')
    rv['cleared'] = [c for j, c in enumerate(rv['cleared']) if j not in drop]
    return rv, sorted(fixed)


# The narrow repair turn (the mayor, 2026-10-30 16:36Z): a reason about an entry's own words (R29 lead words,
# length and identifiers; R5, R6, R10, R14, R25 on a field) is fixed by rewriting that field, nothing else.
WORDING_RE = re.compile(r'^R(?:5|6|10|14|19|25|29): (item|cleared entry|decision) (\d+) (?:(change|why|ask|question|'
                        r'reason) (?:is|has|had)\b|does not lead with|leads with a category|names a raw identifier)')
WORDING_FIELDS = ('change', 'why', 'ask', 'question', 'reason')


def fixable(reasons):
    """There are reasons, and each is one repair() fixes or one a rewrite of an entry's own words fixes: a
    review held only by these is never replaced by a fallback before both were tried."""
    return bool(reasons) and all(any(p.match(r) for p in REPAIRABLE.values()) or WORDING_RE.match(r) for r in reasons)


def rewrite_asks(rv, reasons):
    """{entry id ("item 2"): {fields, rules}} for the wording reasons; a decision the script made (auto) is
    not the model's to rewrite."""
    out = {}
    for r in reasons:
        m = WORDING_RE.match(r)
        if not m or (m.group(1) == 'decision' and rv['decisions'][int(m.group(2)) - 1].get('auto')):
            continue
        a = out.setdefault(f'{m.group(1)} {m.group(2)}', {'fields': [], 'rules': []})
        f = m.group(3) or 'change'
        a['fields'] += [] if f in a['fields'] else [f]
        a['rules'].append(r)
    return out


def field_check(kind, f, v, label=None):
    """The gate's wording reasons for one field of an entry ("item", "cleared entry", "decision"), from the
    same functions the gate runs on it."""
    p = f'{kind} 1'
    if f == 'change':
        return change_reasons(p, v, label)
    why = field_reasons(p, f, v)
    if kind == 'item' or (kind == 'cleared entry' and f == 'why'):
        why += words_reasons(p, f, v, 20 if f == 'ask' else 30)
    return why


def field_limit(kind, f, v):
    """The field's budget in words, said plainly for the narrow turn, with its count now."""
    n = len((v or '').split())
    most = 6 if f == 'change' else 20 if (kind, f) == ('item', 'ask') else 30 if f == 'why' else None
    least = 3 if f == 'change' else 4
    return (f'{least} to {most} words' if most else f'at least {least} words, at most {MAX_FIELD} characters') + \
        f', it has {n}' + ('; plain words, no code names' if f == 'change' else '')


def apply_rewrite(rv, asks, answer, clean=lambda t: t):
    """The review with the rewritten fields the answer gives for the asked entries and fields only; anything
    else in the answer is ignored. A field may come as up to three versions, best first: the first that
    passes the field's own checks is taken (the model does not count words reliably), else the first.
    Returns (rv, fields rewritten)."""
    rv = dict(rv, **{g: [dict(x) for x in rv.get(g) or []] for g in REPAIR_GROUP.values()})
    n = 0
    for e in answer or []:
        a = asks.get(e.get('id')) if isinstance(e, dict) else None
        if not a:
            continue
        kind, k = e['id'].rsplit(' ', 1)
        x = rv[REPAIR_GROUP[kind]][int(k) - 1]
        for f in a['fields']:
            got = e.get(f)
            versions = [' '.join(clean(v).split()) for v in (got if isinstance(got, list) else [got]) if isinstance(v, str)]
            versions = [v for v in versions if v]
            if versions:
                x[f] = next((v for v in versions if not field_check(kind, f, v, x.get('label'))), versions[0])
                n += 1
    return rv, n


def first_item(rv):
    """The number of the first item in the body: the author's own decisions come before the items."""
    return len(split(rv.get('decisions') or [])[1]) + 1


def inline_comments(items, author, voice='disclose', first=1):
    """One inline comment per item, on its exact lines (R4), numbered as in the body (`first`: first_item);
    a range becomes a multi-line comment. It carries the evidence; the ask stands in the body (the mayor,
    2026-10-30 06:4xZ). A change that carries a one-click fix ends with it as a GitHub suggestion block
    (gc-sy1hm.2 b)."""
    out = []
    for k, it in enumerate(items, first):
        side = 'RIGHT' if it.get('side', 'new') == 'new' else 'LEFT'
        text = f"**{k}. {it['change']}**: {it['why']}"
        if it.get('suggestion'):
            text += f"\n\n```suggestion\n{it['suggestion'].rstrip(chr(10))}\n```"
        c = {'path': it['path'], 'line': it['line_end'], 'side': side, 'body': text}
        if it['line_start'] != it['line_end']:
            c.update(start_line=it['line_start'], start_side=side)
        out.append(c)
    return out


MAX_SUGGESTION_LINES = 12
FENCE_RE = re.compile(r'`{3,}|~{3,}')


def suggestion_reasons(p, it, idx):
    """Why an item's one-click fix (a GitHub suggestion block, gc-sy1hm.2 b) cannot be shown. It is code, so
    it is not cleaned like the model's prose: it must be a change on the new side, the whole replacement
    of its lines (all in one hunk), at most 12 lines, unable to close its code fence (then nothing in it is
    rendered, so no link, image or mention), and different from the lines it replaces."""
    s = (it.get('suggestion') or '').rstrip('\n')
    if not s:
        return []
    if it.get('kind') != 'change' or it.get('side', 'new') != 'new':
        return [f'R4: {p} carries a one-click fix but is not a change on the new side']
    if FENCE_RE.search(s):
        return [f'R4: {p} one-click fix holds a code fence']
    if s.count('\n') >= MAX_SUGGESTION_LINES or len(s) > 2000:
        return [f'R4: {p} one-click fix is longer than {MAX_SUGGESTION_LINES} lines']
    text = (idx.get(it.get('path')) or {}).get('text') or {}
    old = [text.get(n) for n in range(it['line_start'], it['line_end'] + 1)]
    if not old or any(t is None for t in old):
        return [f'R4: {p} one-click fix replaces lines that are not all in the diff']
    if [x.rstrip() for x in old] == [x.rstrip() for x in s.split('\n')]:
        return [f'R4: {p} one-click fix changes nothing']
    return []


def keep_suggestions(items, idx):
    """Each item with a one-click fix the gate would refuse loses the fix (the item stays). Returns the
    items, how many fixes were kept and how many dropped."""
    out, kept, dropped = [], 0, 0
    for k, it in enumerate(items, 1):
        if not it.get('suggestion'):
            out.append(it)
        elif in_diff(idx, it) and not suggestion_reasons(f'item {k}', it, idx):
            out.append(it)
            kept += 1
        else:
            out.append(dict(it, suggestion=''))
            dropped += 1
    return out, kept, dropped


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


# Why A over B and C, not A's own reason again (the mayor, 2026-10-30 07:21Z).
MONEY_REASON = 'It takes {who} one paragraph, and it gives whoever owns billing something concrete to agree with.'


def not_checked_decision(ranges, words, author, overview='', overview_places=(), voice='disclose'):
    """R20, R32, R37: the PR touches an area no agent checks, so a person must confirm what it changes.
    `ranges`: [{path, line_start, line_end, what, kind, behaviour, risk}], each changed range with one
    neutral line (money_ranges, then pr_money). The question is the one the options answer (the mayor,
    02:35Z). `overview` (what the change does and its default state) is shown only when the places it names
    (`overview_places`, indexes into `ranges`) have their lines: a claim without a marker can be a model
    invention. The options never ask the reviewer to read code (R37) and each has its reason; all are
    the author's or someone's they pick, so it is a question for the author (decision-554d2e7b)."""
    first = ranges[0]
    backing = [j for j in overview_places if 0 <= j < len(ranges)
               and ranges[j].get('what') and ranges[j].get('what') != NOT_SUMMARIZED]
    # Every option is the author's or someone's they pick, so this is a question for the author, not the
    # reviewer's decision (decision-554d2e7b replaces "billing is a decision for the reviewer"). In the
    # reviewer's voice the gap is said as scope (decision-ed7320b7), and the author is "you".
    if voice == 'reviewer':
        return dict(not_checked_decision(ranges, words, author, overview, overview_places),
                    scope=f'{words[0].upper()}{words[1:]} are outside this review.',
                    options=['You state in the PR description what changes for customers',
                             'Whoever owns billing signs off, here or by linking where they already did',
                             'You merge it as is'],
                    option_reasons=['a short written account puts on record what customers will see',
                                    'they know the pricing rules this touches',
                                    'when customers see no change from it yet, for example while it is off'],
                    reason=MONEY_REASON.format(who='you'))
    return {'question': f'What sign-off does this change to {words} need before it merges?',
            'opening': overview if overview and backing else '', 'opening_places': backing if overview else [],
            'options': [f'{author} states in the PR description what changes for customers',
                        'Whoever owns billing signs off, here or by linking where they already did',
                        f'{author} merges it as is'],
            'option_reasons': ['a short written account puts on record what customers will see',
                               'they know the pricing rules this touches',
                               'when customers see no change from it yet, for example while it is off'],
            'actors': ['author', 'picked', 'author'],
            'recommended': 0,
            'reason': MONEY_REASON.format(who=author),
            'why': 'No agent checks money logic, so a person has to confirm what it changes.',
            'where': [{k: x.get(k) for k in ('path', 'line_start', 'line_end', 'what', 'kind', 'behaviour', 'risk')}
                      for x in ranges],
            'area': NOT_CHECKED_AREA, 'rule': '',
            'link': {'kind': 'code', 'path': first['path'], 'line_start': first['line_start'], 'line_end': first['line_end']},
            'verified': 'zero-token rule', 'confidence': 10, 'auto': True}


def floor_decision(places, author, why, voice='disclose'):
    """R33 (the mayor, 2026-10-30 02:11Z: "a hand-off with no options still forces the reviewer to read the PR"):
    the note's decision. `places`: [{path, line_start, line_end, what}] the zero-token rules know (the
    flagged hunks and the money places), each linked; `why`: the structural reason the review was held.
    Rule-level options, all the author's, so the note asks the author (decision-554d2e7b); none asks the
    reviewer to read code (R37)."""
    d = {'question': 'Who reviews this PR before it merges?',
         'options': [f'{author} asks a teammate who knows this code to review it',
                     f'{author} states in the PR description what it changes and why that is safe',
                     f'{author} merges it as is'],
         'option_reasons': ['a person who knows the code checks in one pass what our review could not',
                            'the author knows the change best, and the account stays on record',
                            'when a wrong change here would be cheap to undo'],
         'actors': ['author', 'author', 'author'],
         'recommended': 0, 'reason': 'It costs one message, and it is the only option where someone else checks the code.',
         'why': why, 'area': 'other', 'rule': '', 'verified': 'zero-token rules', 'confidence': 10, 'auto': True}
    if voice == 'reviewer':  # the author's question, with no word on why our review was held (decision-ed7320b7)
        d.update(options=['You ask a teammate who knows this code to review it',
                          'You state in the PR description what it changes and why that is safe',
                          'You merge it as is'],
                 option_reasons=['a teammate who knows the code can check it in one pass',
                                 'you know the change best, and the account stays on record',
                                 'when a wrong change here would be cheap to undo'],
                 reason='It costs you one message, and it is the only option where someone else checks the code.',
                 why='This one needs a second reviewer who knows this code.')
    if places:
        first = places[0]
        d['where'] = [{k: x.get(k) for k in ('path', 'line_start', 'line_end', 'what', 'kind')} for x in places]
        d['link'] = {'kind': 'code', 'path': first['path'], 'line_start': first['line_start'], 'line_end': first['line_end']}
    else:
        d['link'] = {'kind': 'description'}
    return d


def actor_reasons(p, options, actors, names):
    """R22: each option starts with who carries it out, checked against the names (`names`: [reviewer,
    author]; skipped without them): the reviewer's name, the author's, or for someone the author picks
    neither (a teammate, the owner of billing). The body then shows who acts (gate_body reads it)."""
    if not names:
        return []
    reviewer, author = names
    why = []
    for j, (o, a) in enumerate(zip(options, actors)):
        o = (o or '').strip()
        said = reviewer if o.startswith(f'{reviewer} ') else author if o.startswith(f'{author} ') else None
        want = {'reviewer': reviewer, 'author': author, 'picked': None}[a]
        if said != want and want:
            why.append(f'R22: {p} option {LETTERS[j]} is one {want} carries out, so it starts with "{want}"')
        elif said != want:
            why.append(f'R22: {p} option {LETTERS[j]} is carried out by someone {author} picks, so it starts with who '
                       f'that is (a teammate, the owner of billing), not with {said}')
    return why


ECHO_STOP = {'what', 'will', 'with', 'that', 'this', 'when', 'they', 'from', 'have', 'your', 'them', 'then', 'only'}


def echoes(reason, own):
    """R16: the recommendation's reason says again what the recommended option's own reason says."""
    def words(t):
        return set(re.findall(r'[a-z]{4,}', (t or '').lower())) - ECHO_STOP
    own = words(own[0] if own else '')
    return len(own) >= 3 and len(own & words(reason)) >= 0.5 * len(own)


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
        why.append(f'R29: {p} does not lead with 3-6 plain words on what the PR changed (the change is '
                   f'{len(v.split())} words)')
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
    if outcome == 'low-risk' and (rv.get('since') or {}).get('open'):  # gc-sy1hm.2 c
        why.append(f"R2: low risk while {rv['since']['open']} earlier asks are still open in their threads; raise each "
                   f'again as an item on its lines, or say why it is fixed')
    if outcome == 'escalate' and not decisions:
        why.append('R16: needs you without a decision for the reviewer')
    elif outcome == 'escalate' and not any(for_reviewer(d) for d in decisions):
        why.append('R16: needs you, but the reviewer carries out none of the options; a decision whose options are '
                   "all the author's is a question for the author")
    elif outcome == 'escalate':  # decision-3aeec149
        why.append('R16: "Needs you" is not an outcome on the PR: every finding goes to the author, and a gap in '
                   'our method goes to the reviewer by Slack')
    for g in rv.get('gaps') or []:
        if g.get('kind') not in GAP_KINDS:
            why.append(f"R16: a gap of kind {g.get('kind')!r}, not one of {list(GAP_KINDS)}")
    if rv.get('approve') and outcome != 'low-risk':
        why.append(f'R27: an approval for a {outcome} review')
    for k, d in enumerate(decisions, 1):
        p = f'decision {k}'
        q = (d.get('question') or '').strip()
        if not q.endswith('?') or q.count('?') != 1:
            why.append(f'R16: {p} is not one question ending in "?"')
        if len(d.get('options') or []) < 2:
            why.append(f'R16: {p} has fewer than 2 options')
        acts = d.get('actors')
        if not isinstance(acts, list) or len(acts) != len(d.get('options') or []) or any(a not in ACTORS for a in acts):
            why.append(f"R16: {p} does not say who carries out each option ({', '.join(ACTORS)})")
        else:
            why += actor_reasons(p, d['options'], acts, rv.get('names'))
            if 'reviewer' in acts:  # decision-3aeec149: a finding about the PR is the author's to settle
                why.append(f'R16: {p} has an option for the reviewer; every finding goes to the author')
        for f in ('question', 'why', 'reason'):
            why += field_reasons(p, f, d.get(f))
        rec = d.get('recommended')
        if not isinstance(rec, int) or not 0 <= rec < len(d.get('options') or []):
            why.append(f'R16: {p} recommends none of its options')
        elif echoes(d.get('reason'), (d.get('option_reasons') or [None] * (rec + 1))[rec:rec + 1]):
            why.append(f'R16: {p} recommendation repeats option {LETTERS[rec]}\'s own reason; say why it over the others')
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
    # R20: a not-checked area gets the sign-off question, the author's (decision-554d2e7b); never otherwise.
    nc = [d for d in decisions if d.get('area') == NOT_CHECKED_AREA]
    # R33 (the mayor, 21:39Z): a fallback asks its own question on each money place it links instead.
    asks_money = rv.get('fallback') and not decisions and any(it.get('money') for it in items)
    if rv.get('not_checked') and not nc and not asks_money and not rv.get('signed_off'):
        why.append(f"R20: the PR touches {rv['not_checked']}, which is not checked, so it gets the sign-off question")
    if any(for_reviewer(d) is not False for d in nc):
        why.append(f'R20: the sign-off on {NOT_CHECKED_AREA} code is a question for the author, not a decision for '
                   f'the reviewer')
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
        else:
            why += suggestion_reasons(p, it, idx)
        why += change_reasons(p, it.get('change'))
        for f in ('why', 'ask'):  # the ask stands in the body: at most 20 words (R4)
            why += field_reasons(p, f, it.get(f)) + words_reasons(p, f, it.get(f), 20 if f == 'ask' else 30)
        if it.get('area') not in AREAS:
            why.append(f'R5: {p} area is not one of {AREAS}')
        if it.get('kind') == 'question' and not (it.get('ask') or '').rstrip().endswith('?'):
            why.append(f'R13: {p} is a question but its ask does not end with a question mark')
        if rv.get('fallback') and STOCK_RE.search(it.get('ask') or ''):
            why.append(f'R33: {p} asks a question that fits any PR; ask about the behaviour on its lines')
        # R21: each finding once: no two items on overlapping lines of a file, no repeated sentence.
        for j, o in seen:
            if o['path'] == it['path'] and o.get('side', 'new') == it.get('side', 'new') and \
                    o['line_start'] <= it['line_end'] and it['line_start'] <= o['line_end']:
                why.append(f'R21: {p} repeats the lines of item {j}; say each finding once')
            elif (o.get('change') or '').strip().lower() == (it.get('change') or '').strip().lower() or \
                    (it.get('ask') or '').strip() and (o.get('ask') or '').strip().lower() == it['ask'].strip().lower():
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
        if f is None and c.get('answered'):  # a self-answered question: its own lines, no flag (R31)
            f = {'path': c.get('path'), 'line': c.get('line_start'), 'label': 'a question its own text answers'}
        elif f is None:
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
            # A rule that fired on a comment's own words is cleared on that comment: the link holds the
            # flagged line, and that line is a comment (a hunk of comments only has no other line to link).
            on_flag = c['line_start'] <= f['line'] <= c['line_end'] and is_comment(c['path'], text.get(f['line']) or '')
            if shown and all(is_comment(c['path'], t) for t in shown) and not on_flag:
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
    mine, theirs = split(rv.get('decisions') or [])
    n_dec, n_theirs, n_items = len(mine), len(theirs), len(rv.get('items') or [])
    n_block, n_clear = len(rv.get('blockers') or []), len(rv.get('cleared') or [])
    lines = body.splitlines()
    word = word_of(rv)
    l2 = lines[1] if len(lines) > 1 else ''
    asks = n_items + n_block + n_theirs
    if voiced(rv):
        why_v, said_to_author, nothing = voice_reasons(lines, l2, word, rv, asks, n_dec, comments)
        why += why_v
    else:
        why_d, said_to_author, nothing = disclose_reasons(body, lines, l2, word, rv, asks, mine, n_dec)
        why += why_d
    rest = [l for l in lines[2:] if l.strip()]
    if rv.get('approve'):
        # The one-line reason: the line render() writes for these parts; on a body delivered earlier (no
        # parts in rv), one plain line.
        reason = approval_reason(rv)
        plain = rest and (rest[0] == reason if reason else not re.match(r'(<|- |\*\*|\d+\. )', rest[0]))
        folded = rest[1:] if plain else rest
        if folded and not (folded[0] == '<details><summary>What was checked</summary>' and folded[-1] == '</details>'
                           and all(l.startswith('- ') for l in folded[1:-1])):
            why.append('R36: an approval is line 1, line 2, one line on why and the collapsed "What was checked", '
                       'nothing else')
        if not nothing:
            why.append('R36: line 2 of an approval is "' + ('<author>: nothing needed.' if voiced(rv)
                                                               else 'Nothing for <reviewer> or <author>.') + '"')
    elif re.sub(r'<details><summary>Other places</summary>', '', body).count('<details>'):
        why.append('R23: a collapsed section other than the money places in a review that is not an approval')
    return why + gate_body_rest(body, lines, rest, rv, comments, said_to_author, nothing, n_dec, n_theirs, n_items,
                                n_block, n_clear)


def voice_reasons(lines, l2, word, rv, asks, n_dec, comments):
    """Line 1 and line 2 in the reviewer's voice (decision-ed7320b7), and R38: no disclosure phrase in the
    body or an inline comment. Returns (reasons, what line 2 asks of the author or None, nothing needed)."""
    why = []
    m1 = VOICE_LINE1_RE.match(lines[0] if lines else '')
    if not m1:
        why.append(f'R28: line 1 is not the verdict and the marker ("**{word}** {MARK}")')
    elif m1.group(1) != word:
        why.append(f"{'R35' if rv['outcome'] == 'changes' else 'R1'}: line 1 says \"{m1.group(1)}\" for {rv['outcome']}")
    elif bool(m1.group(2)) != bool(rv.get('fallback') and asks):  # the mayor, 21:39Z: say plainly who acts
        said = VOICE_FALLBACK.format(author='<author>')
        why.append(f'R28: a fallback\'s line 1 says who acts ("**{word}**{said}"), a full review\'s does not')
    nothing = bool(NOTHING_NEEDED_RE.match(l2))
    m = None if nothing else VOICE_WHO_RE.match(l2)
    if nothing and asks:
        why.append(f'R22: line 2 says nothing is needed, but the review has {asks} asks')
    elif l2 == 'Nothing needed.':
        why.append('R22: line 2 names the author: "<author>: nothing needed."')
    elif not nothing and not m:
        why.append('R22: line 2 does not say what the author must do ("<author>: ..." or "<author>: nothing needed.")')
    elif m and not asks:
        why.append('R22: line 2 is "<author>: nothing needed." when the author has nothing to do')
    if n_dec:
        why.append(f'R16: {n_dec} decisions for the reviewer on the PR; every finding goes to the author')
    texts = ['\n'.join(lines)] + [c.get('body') or '' for c in comments or []]
    hits = sorted({m.group(0).lower() for t in texts for m in DISCLOSURE_RE.finditer(t)})
    if hits:
        why.append(f"R38: the reviewer's voice never says {', '.join(repr(h) for h in hits)}")
    return why, (m.group(2) if m else None), nothing


def disclose_reasons(body, lines, l2, word, rv, asks, mine, n_dec):
    """Line 1 (the verdict and the automated note) and line 2 (who acts). Returns (reasons, what line 2
    asks of the author or None, whether it says nothing for anyone)."""
    why = []
    m1 = LINE1_RE.match(lines[0] if lines else '')
    if not m1:
        why.append(f'R28: line 1 is not the verdict and the automated note ("**{word}**' + SUFFIX.format(name='<reviewer>') + '")')
    elif m1.group(1) != word:
        rule = 'R35' if rv['outcome'] == 'changes' else 'R1'
        why.append(f'{rule}: line 1 says "{m1.group(1)}" for {rv["outcome"]} ({event_of(rv)} review)')
    m, nothing = WHO_RE.match(l2), NOTHING_RE.match(l2)
    if nothing:
        if n_dec or asks or rv.get('not_checked'):
            why.append('R22: line 2 says nothing for anyone, but the review has decisions or asks')
    elif not m:
        why.append('R22: line 2 does not say who acts ("<reviewer>: ... <author>: ..." or "Nothing for <reviewer> or <author>.")')
    else:
        if int(m.group(3) or 0) != n_dec:
            why.append(f'R22: line 2 counts {m.group(3) or 0} decisions, the review has {n_dec}')
        tasks = [x for x in mine if x.get('author_task')]
        for x in tasks:
            if f"picks {LETTERS[x['recommended']]}, {x['author_task']}" not in m.group(7):
                why.append("R22: line 2 does not name the task the recommended option gives the author")
        asks += len(tasks)
        if mine and not any(x.get('question') for x in mine):  # a body delivered earlier: its decisions are counts
            asks += len(re.findall(rf'\bif {re.escape(m.group(1))} picks [A-D], ', m.group(7)))
        if (m.group(7) == 'nothing') != (asks == 0):
            why.append(f'R22: line 2 says the author has {m.group(7)!r}, the review has {asks} asks')
        if not n_dec and not asks:
            why.append('R22: line 2 is "Nothing for <reviewer> or <author>." when nobody acts')
        why += reviewer_option_reasons(body, rv, m.group(1), int(m.group(3) or 0), m.group(6))
    return why, (m.group(7) if m else None), bool(nothing)


def gate_body_rest(body, lines, rest, rv, comments, said_to_author, nothing, n_dec, n_theirs, n_items, n_block, n_clear):
    """The body rules after line 2, the same in either voice."""
    why = []
    if re.search(r'^   \*\*Other places:\*\*', body, re.M):
        why.append('R32: the other money places are not collapsed (they go in "Other places" after the recommendation)')
    if rv['outcome'] != 'low-risk' or n_clear:
        links = re.findall(r'\]\(https://github\.com/[^)]+/(?:pull/\d+/files#diff-[0-9a-f]{64}[RL]\d+(?:-[RL]\d+)?|'
                           r'blob/[0-9a-f]{40}/[^)#]+#L\d+(?:-L\d+)?|pull/\d+(?:/checks|/conflicts)?)\)', body)
        n_entries = n_items + n_dec + n_theirs + n_block + n_clear
        if not links or len(links) < n_entries:
            why.append(f'R3: {len(links)} links to lines or text for {n_entries} entries')
    summary = re.search(r'^\*\*What it does:\*\* (.*)$', body, re.M)
    if rv.get('model') and not rv.get('approve') and not (summary and summary.group(1).strip()):
        why.append('R7: no "What it does" line')
    asked = bool(rv.get('fallback') and n_items and not n_dec + n_theirs)  # R33: the fallback's own questions
    if rv['outcome'] == 'not-reviewed' and n_dec + n_theirs != 1 and not asked:
        why.append('R33: the note carries neither a question per linked place nor its one question for the author')
    skip = re.search(r'^\*\*Safe to skip:\*\* (.*)$', body, re.M)
    if skip:
        groups = [g for g in skip.group(1).split('; ') if g.strip()]
        if any(not re.search(r'\]\(https://github\.com/[^)]+/pull/\d+/files(#diff-[0-9a-f]{64})?\)', g) for g in groups):
            why.append('R30: a "Safe to skip" group does not link its files')
    fl = next((l for l in lines if FACTS_RE.match(l)), None)
    facts = rv.get('facts')
    if fl == GREEN_FACTS:
        why.append('R30: the status line on a PR where everything passed; show it only when something did not')
    elif fl is None and facts is not None and facts != GREEN_FACTS and not blockers_say(rv):
        why.append('R8: no status line, and something did not pass')
    elif fl and blockers_say(rv):
        why.append('R33: the status line says again what a blocker says; a fallback says it once')
    if fl and fl != GREEN_FACTS:
        if said_to_author is not None or nothing:  # R22: a blocker in the status line is a linked ask in line 2
            missing = [short for cue, short in FACT_ASKS if cue in fl and short not in (said_to_author or '')]
            if missing:
                why.append(f"R22: the status line has a blocker but line 2 does not ask for it: {', '.join(missing)}")
        if rest and rest[-1] != fl:
            why.append('R9: the status line is not the last line')
    block = body.split('   **What changes:**', 1)[1].split('\n   A. ', 1)[0] if '   **What changes:**' in body else ''
    one_by_one = re.findall(r'^   - .*\]\(https://github\.com/[^)]+/blob/[0-9a-f]{40}/[^)]+\): \S', block, re.M)
    if len(one_by_one) > MONEY_SHOWN:
        why.append(f'R32: {len(one_by_one)} money places listed one by one; show at most {MONEY_SHOWN}, the rest in a '
                   f'linked line')
    code = re.sub(r'(?m)^   Rule: .*$|rule: \[[^\]]*\]\([^)]*\)', '', body)  # rule citations may repeat
    urls = re.findall(r'\]\((https://github\.com/[^)]+/blob/[0-9a-f]{40}/[^)#]+#L\d+(?:-L\d+)?)\)', code)
    twice = len(urls) - len(set(urls))
    if twice:
        why.append(f'R21: {twice} code links repeat a place already linked; link each place once')
    phrase = 'outside this review' if voiced(rv) else 'not checked'  # in the reviewer's voice, a gap is scope
    said = len(re.findall(rf'(?i)\b{phrase}\b', body))
    if asked and said:
        why.append(f'R33: the fallback says "{phrase}"; its questions are about the lines, never a scope sentence')
    elif rv.get('not_checked') and said != 1 and not asked and not rv.get('signed_off'):
        why.append(f'R32: "{phrase}" is said {said} times; say it once')
    elif not rv.get('not_checked') and said:
        why.append(f'R23: "{phrase}" on a PR that does not touch that area')
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
    if rv.get('fallback'):  # R33: the fallback passes the bar the full review failed (the mayor, 10:25Z)
        # What a reader sees without opening "Other places", whose places are each linked to their lines.
        seen = re.sub(r'(?s)<details><summary>Other places</summary>.*?</details>', '', body)
        n_links, n_lines = len(re.findall(r'\]\(https?://', seen)), sum(1 for x in seen.splitlines() if x.strip())
        n_chars = len(re.sub(r'\]\([^)]*\)', ']', seen))
        if n_links > FALLBACK_LINKS or n_lines > FALLBACK_LINES or n_chars > FALLBACK_CHARS:
            why.append(f'R33: the fallback is {n_links} links, {n_lines} lines and {n_chars} characters; at most '
                       f'{FALLBACK_LINKS}, {FALLBACK_LINES} and {FALLBACK_CHARS}')
        if re.search(r'\]\(https://github\.com/[^)]+/pull/\d+/files\)', body):  # the mayor, 21:39Z
            why.append('R33: a link to the files view that no line anchors; link each place to its lines')
    return why


def option_lines(body, who):
    """The lettered option lines of each numbered entry under "**For <who>:**": [[option text, ...], ...]."""
    out, inside = [], False
    for line in body.splitlines():
        if line.startswith('**For '):
            inside = line == f'**For {who}:**'
        elif inside and re.match(r'^\d+\. ', line):
            out.append([])
        elif inside and out and re.match(r'^   [A-D]\. ', line):
            out[-1].append(line[6:])
        elif line.startswith('**'):
            inside = False
    return out


def reviewer_option_reasons(body, rv, reviewer, counted, author):
    """R22 (decision-554d2e7b): "Needs you", or line 2's "<reviewer>: N decisions", only when the reviewer
    carries out an option of each: read from the text, so it holds for a body delivered earlier too. Each
    decision under "**For <reviewer>:**" has an option line that starts with the reviewer's name; no option
    of a question for the author does."""
    why = []
    if rv['outcome'] == 'escalate' and not counted:
        why.append(f'R22: needs you, but line 2 gives {reviewer} nothing')
    if counted:
        for k, opts in enumerate(option_lines(body, reviewer) or [[]], 1):
            if not any(o.startswith(f'{reviewer} ') for o in opts):
                why.append(f"R22: decision {k} for {reviewer} has no option {reviewer} carries out; a decision whose "
                           f"options are all the author's is a question for the author")
    if author != reviewer and any(o.startswith(f'{reviewer} ') for opts in option_lines(body, author) for o in opts):
        why.append(f'R22: a question for {author} has an option {reviewer} carries out, so it is a decision for {reviewer}')
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
    """R27, the bar for an APPROVE (the reviewer, 2026-10-29 21:01Z: "actually approve low risk PRs not just say you
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
            'rules_sha': 'c' * 40, 'since': fx.get('since'), 'voice': fx.get('voice', 'disclose'),
            'gaps': fx.get('gaps', []), 'names': ['Reviewer', YOU if fx.get('voice') == 'reviewer' else 'Author'],
            'fallback': fx.get('fallback', False), 'others': fx.get('others', []), 'held_why': fx.get('held_why', '')}


def route(fx):
    """A routing case (R33, R34, the sweep's review requests): the decision function's answer for the
    fixture's inputs must be `want`. Returns the reasons it is not."""
    import pr_rules
    got = getattr(pr_rules, fx['route'])(**fx['args'])
    return [] if got == fx['want'] else [f"{fx.get('rule', 'route')}: {fx['route']} gave {got!r}, want {fx['want']!r}"]


def regress(dirs):
    """Run the gate over every fixture (*.json) in dirs. A fixture holds `expect` ("pass" or "fail"),
    optionally `expect_reasons` (rule ids that must be among the reasons), and either `body` (+ outcome,
    n_items, n_decisions, n_author_decisions) for a delivered body, or the review's parts (outcome,
    decisions, items, blockers, cleared, flagged, diff, ...) to render and gate (`line2`: the rendered line 2;
    `derived`: the parts must give the outcome; `body_re` / `body_not_re`: patterns the rendered body must /
    must not hold, multiline, reason LAYOUT; `repair`: the run's repair flow first (`rewrite`: the narrow
    turn's answer, {entry id: {field: text}}), a list = the rule ids the script must repair, reason REPAIR;
    `fallback`: a fallback body, R33); `delivery` ({pr_state, review_url, review_state, ...})
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
                rv = {'outcome': fx['outcome'], 'decisions': [{}] * fx.get('n_decisions', 0)
                      + [{'actors': ['author']}] * fx.get('n_author_decisions', 0),
                      'items': [{}] * fx.get('n_items', 0), 'not_checked': fx.get('not_checked', ''),
                      'approve': fx.get('approve', False), 'model': fx.get('model', True),
                      'request_changes': fx.get('request_changes', False), 'facts': fx.get('facts'),
                      'voice': fx.get('voice', 'disclose'), 'fallback': fx.get('fallback', False)}
                reasons = gate_body(fx['body'], rv, fx.get('comments_v'))
            else:
                idx = diff_index(fx['diff'])
                if fx.get('judge_answered'):  # the reader found these questions answered by their own text (R31)
                    kept, more = self_answered(fx.get('items', []), set(fx['judge_answered']), fx.get('flagged', []), idx)
                    fx = dict(fx, items=kept, cleared=fx.get('cleared', []) + more)
                pr = {'number': 1, 'state': 'OPEN', 'mergeable': 'MERGEABLE', 'url': 'https://github.com/example/repo/pull/1',
                      'headRefOid': 'a' * 40}
                rv = review_of(fx)
                body = render('example/repo', pr, rv, 'Reviewer', 'Author')
                if fx.get('body_edit'):  # a rendered body broken on purpose
                    body = body.replace(*fx['body_edit'])
                fixed = None
                if 'repair' in fx:  # the run's flow: held by fixable reasons only -> rewritten, repaired, gated again
                    def gated(x):
                        return gate(x, idx, render('example/repo', pr, x, 'Reviewer', 'Author'), fx.get('tree_lines'),
                                    fx.get('pr_text', ''), fx.get('flagged', []),
                                    inline_comments(x['items'], 'Author', x['voice'], first_item(x)))
                    first, fixed = gated(rv), []
                    if fixable(first):  # `rewrite`: the narrow turn's answer, {entry id: {field: text}}
                        answer = [dict(v, id=k) for k, v in (fx.get('rewrite') or {}).items()]
                        rv = apply_rewrite(rv, rewrite_asks(rv, first), answer)[0]
                        rv, fixed = repair(rv, first)
                        again = gated(rv)
                        if format_only(again):
                            rv, more = repair(rv, again)
                            fixed = sorted(set(fixed) | set(more))
                    body = render('example/repo', pr, rv, 'Reviewer', 'Author')
                if 'line2' in fx and body.splitlines()[1] != fx['line2']:
                    reasons_l2 = ["R22: line 2 is not the fixture's line2"]
                else:
                    reasons_l2 = []
                if isinstance(fx.get('repair'), list) and fixed != fx['repair']:
                    reasons_l2.append(f"REPAIR: repaired {fixed}, the fixture wants {fx['repair']}")
                reasons_l2 += [f'LAYOUT: the body does not hold pattern {j + 1}' for j, x in enumerate(fx.get('body_re', []))
                               if not re.search(x, body, re.M)]
                reasons_l2 += [f'LAYOUT: the body holds pattern {j + 1} it must not' for j, x in enumerate(fx.get('body_not_re', []))
                               if re.search(x, body, re.M)]
                if fx.get('derived') and decide(rv['decisions'], rv['items'], rv['blockers']) != rv['outcome']:
                    reasons_l2.append(f"R1: the parts give {decide(rv['decisions'], rv['items'], rv['blockers'])}, "
                                      f"not {rv['outcome']}")
                reasons = reasons_l2 + gate(rv, idx, body, fx.get('tree_lines'), fx.get('pr_text', ''),
                                            fx.get('flagged', []),
                                            inline_comments(rv['items'], 'Author', rv['voice'], first_item(rv)))
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


def regate(path):
    """Each held attempt kept by pr_review.keep_held, rendered and gated again with this code, through the
    run's repair of format-only reasons (no rewrite turn and no reader test: they cost tokens). Prints the PR number, the
    attempt, the rule ids then and now, and what a repair fixed; never the text. Returns the rows read."""
    n = 0
    for line in open(path):
        h = json.loads(line)
        m = re.match(r'https://github\.com/([^/]+/[^/]+)/pull/(\d+)', h['pr'])
        pr = {'number': int(m.group(2)), 'state': 'OPEN', 'mergeable': 'MERGEABLE', 'headRefOid': h['head']}
        rv, idx, author = h['rv'], diff_index(h.get('diff') or ''), h.get('author') or 'the author'

        def gated(rv):
            body = render(m.group(1), pr, rv, rv['names'][0], author)
            comments = inline_comments(rv['items'], author, rv.get('voice'), first_item(rv))
            return gate(rv, idx, body, None, h.get('pr_text', ''), h.get('flagged') or [], comments)
        now, fixed = gated(rv), []
        if format_only(now):
            rv, fixed = repair(rv, now)
            now = gated(rv)
        ids = lambda rs: sorted({r.split(':')[0] for r in rs})  # noqa: E731
        print(f"{m.group(2)} attempt {h['attempt']}: then {ids(h['reasons'])}, now {ids(now)}"
              + (f', repaired {fixed}' if fixed else '') + ('' if h.get('diff') else ' (no diff kept)'))
        n += 1
    return n


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--regress':
        sys.exit(1 if regress(sys.argv[2:]) else 0)
    if len(sys.argv) == 3 and sys.argv[1] == '--regate':
        regate(sys.argv[2])
        sys.exit(0)
    print(__doc__)
