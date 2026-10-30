#!/usr/bin/env python3
"""The escaped-bug benchmark (gc-sy1hm.2): how many real problems our review would have caught, measured
on the repository's own history instead of claimed.

  pr_bench.py --mine REPO [--days 60] [--within 14]   cases and controls from git history, zero tokens
  pr_bench.py --run REPO [--cases 5] [--controls 5] [--max-usd 10]
                                                      review a sample at their heads (nothing delivered), then score
  pr_bench.py --label REPO                            whether each case's later PR fixes a defect (capture off)
  pr_bench.py --score REPO                            recall, a precision proxy, false alarms, cost per PR

A CASE is a merged PR P whose added lines a later PR Q changed within `--within` days, where Q reads as a
fix (its title says fix, bug, hotfix, revert, regression, ...; or it names P or P's ticket). Its labels
are the lines of P that Q changed, found with `git blame` at Q's parent in the mirror; the repository
squash-merges, so a blamed commit is a PR's merge commit. A CONTROL is a merged PR with at least 10
added lines outside tests whose lines nobody changed within the same time. The benchmark is the
repository's text, so it stays host-local (PR_REVIEW_WORK_DIR/bench, mode 600) and this script prints
counts only.
"""
import argparse
import collections
import datetime as dt
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_judge  # noqa: E402
import pr_rules  # noqa: E402
import pr_state  # noqa: E402

FIX_RE = re.compile(r'(?i)\b(fix(es|ed|ing)?|bug(s|fix)?|hotfix|revert(s|ed)?|regression|broken|crash(es)?|'
                    r'patch(es)?|correct(s|ed)?|wrong|incorrect|typo)\b')
TICKET_RE = re.compile(r'\b[A-Z][A-Z0-9]{1,9}-\d+\b')
TEST_RE = re.compile(r'(?i)(^|/)(tests?|__tests__|spec|e2e|fixtures?)(/|$)|\.(test|spec)\.[a-z]+$|_test\.[a-z]+$')
MIN_CONTROL_LINES = 10


def git(mirror, *args, check=True):
    r = subprocess.run(['git', '-C', mirror, *args], capture_output=True, text=True, timeout=900,
                       env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
    if check and r.returncode != 0:
        raise RuntimeError(f'git {args[0]} failed (rc {r.returncode})')
    return r.stdout


def merged_prs(repo, since, until):
    """Merged PRs in [since, until), in 10-day windows (a search returns at most 1000)."""
    out, t = {}, since
    while t < until:
        u = min(t + dt.timedelta(days=10), until)
        for pr in pr_rules.search_prs(f'repo:{repo} is:pr is:merged merged:{pr_state.iso(t)}..{pr_state.iso(u)}'):
            if pr.get('mergeCommit') and pr.get('mergedAt'):
                out[pr['number']] = pr
        t = u
    return out


def changed_old_lines(mirror, parent, commit):
    """{path: [old-side line numbers]} that `commit` removed or rewrote, against `parent`."""
    out, path = collections.defaultdict(list), None
    for line in git(mirror, 'diff', '-U0', '--no-ext-diff', '--no-renames', parent, commit).splitlines():
        if line.startswith('--- '):
            path = line[6:] if line.startswith('--- a/') else None
        m = re.match(r'@@ -(\d+)(?:,(\d+))? \+', line)
        if m and path:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            out[path] += range(start, start + count)
    return out


def added_lines(mirror, parent, commit):
    """{path: [new-side line numbers]} that `commit` added, against `parent`."""
    out, path = collections.defaultdict(list), None
    for line in git(mirror, 'diff', '-U0', '--no-ext-diff', '--no-renames', parent, commit).splitlines():
        if line.startswith('+++ '):
            path = line[6:] if line.startswith('+++ b/') else None
        m = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@', line)
        if m and path:
            start, count = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
            out[path] += range(start, start + count)
    return out


def blame(mirror, commit, path, lines):
    """{line at `commit`: (origin commit, line number in the origin)} for the given lines."""
    out = {}
    for a, b in spans(lines):
        r = git(mirror, 'blame', '--porcelain', '-l', '-L', f'{a},{b}', commit, '--', path, check=False)
        for m in re.finditer(r'^([0-9a-f]{40}) (\d+) (\d+)', r, re.M):
            out[int(m.group(3))] = (m.group(1), int(m.group(2)))
    return out


def still_there(mirror, commit, later, path, lines):
    """Every given line of `commit` still exists unchanged at `later` (git blame --reverse names, for each
    line, the last revision in which it existed: `later` itself when nobody changed it)."""
    if later == commit:
        return True
    for a, b in spans(lines):
        r = git(mirror, 'blame', '--porcelain', '-l', '--reverse', f'{commit}..{later}', '-L', f'{a},{b}', '--', path,
                check=False)
        seen = re.findall(r'^([0-9a-f]{40}) \d+ \d+', r, re.M)
        if not seen or any(s != later for s in seen):
            return False
    return True


def spans(lines):
    s = sorted(set(lines))
    out = []
    for n in s:
        if out and n == out[-1][1] + 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return out


def mine(cfg, repo, days, within):
    owner, name = repo.split('/')
    # Its own full clone: blame on the reviews' blob-less mirror would fetch blob after blob, in a mirror
    # the review runs use at the same time.
    mirror = os.path.join(pr_state.private_dir(cfg, 'bench', 'mirrors'), f'{owner}__{name}.git')
    if not os.path.isdir(mirror):
        r = subprocess.run(['git', 'clone', '--quiet', '--bare', f'https://github.com/{repo}.git', mirror],
                           capture_output=True, text=True, timeout=3600, env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
        if r.returncode != 0:
            raise SystemExit(json.dumps({'repo': repo, 'error': f'git clone failed (rc {r.returncode})'}))
    git(mirror, 'fetch', '--quiet', 'origin', '+refs/heads/main:refs/heads/main')
    now = pr_state.now()
    prs = merged_prs(repo, now - dt.timedelta(days=days), now)
    by_merge = {pr['mergeCommit']['oid']: pr for pr in prs.values()}
    when = {n: pr_state.parse(pr['mergedAt']) for n, pr in prs.items()}
    counts = collections.Counter(merged=len(prs))
    cases = {}
    for q in sorted(prs.values(), key=lambda p: p['mergedAt']):
        text = f"{q.get('title') or ''}\n{q.get('body') or ''}"
        refs = {int(x) for x in re.findall(r'#(\d+)', text)}
        tickets = set(TICKET_RE.findall(q.get('title') or ''))
        fixlike = bool(FIX_RE.search(q.get('title') or ''))
        if not (fixlike or refs):
            continue
        counts['fix_candidates'] += 1
        qc = q['mergeCommit']['oid']
        try:
            parent = git(mirror, 'rev-parse', f'{qc}^').strip()
            old = changed_old_lines(mirror, parent, qc)
        except RuntimeError:
            counts['no_commit'] += 1
            continue
        hit = collections.defaultdict(lambda: collections.defaultdict(set))
        for path, lines in old.items():
            if TEST_RE.search(path):
                continue
            for _, (origin, oline) in blame(mirror, parent, path, lines).items():
                p = by_merge.get(origin)
                if not p or p['number'] == q['number']:
                    continue
                gap = when[q['number']] - when[p['number']]
                if dt.timedelta(0) < gap <= dt.timedelta(days=within):
                    hit[p['number']][path].add(oline)
        for pn, paths in hit.items():
            p = prs[pn]
            why = 'ref' if pn in refs else 'ticket' if tickets & set(TICKET_RE.findall(p.get('title') or '')) else \
                'title' if fixlike else None
            if not why:
                continue
            c = cases.setdefault(pn, {'pr': pn, 'head': p['headRefOid'], 'merge': p['mergeCommit']['oid'],
                                      'merged_at': p['mergedAt'], 'fixes': [], 'fixed': {}})
            c['fixes'].append({'pr': q['number'], 'merged_at': q['mergedAt'], 'why': why})
            for path, lines in paths.items():
                c['fixed'][path] = sorted(set(c['fixed'].get(path, [])) | lines)
    counts['cases'] = len(cases)
    # Controls: merged early enough to have `within` days after them, no case, nothing of theirs changed.
    controls = []
    cutoff = now - dt.timedelta(days=within)
    for p in sorted(prs.values(), key=lambda p: p['mergedAt'], reverse=True):
        if p['number'] in cases or when[p['number']] > cutoff or len(controls) >= 60:
            continue
        pc = p['mergeCommit']['oid']
        try:
            added = {k: v for k, v in added_lines(mirror, f'{pc}^', pc).items() if not TEST_RE.search(k)}
        except RuntimeError:
            counts['control_no_commit'] += 1
            continue
        n_added = sum(len(v) for v in added.values())
        counts['control_candidates'] += 1
        if n_added < MIN_CONTROL_LINES:
            counts['control_small'] += 1
            continue
        later = git(mirror, 'rev-list', '-1', f"--before={pr_state.iso(when[p['number']] + dt.timedelta(days=within))}",
                    'main').strip()
        if not all(still_there(mirror, pc, later, path, lines) for path, lines in added.items()):
            counts['control_changed'] += 1
        else:
            controls.append({'pr': p['number'], 'head': p['headRefOid'], 'merge': pc, 'merged_at': p['mergedAt'],
                             'added': n_added})
    counts['controls'] = len(controls)
    bench = {'repo': repo, 'built_at': pr_state.iso(now), 'days': days, 'within': within,
             'cases': sorted(cases.values(), key=lambda c: c['merged_at']), 'controls': controls,
             'counts': dict(counts)}
    p = os.path.join(pr_state.private_dir(cfg, 'bench'), f'{owner}__{name}.json')
    with open(p, 'w') as f:
        json.dump(bench, f)
    os.chmod(p, 0o600)
    whys = collections.Counter(f['why'] for c in cases.values() for f in c['fixes'])
    print(json.dumps({'repo': repo, **counts, 'case_reasons': dict(whys),
                      'fixed_lines': sum(len(v) for c in cases.values() for v in c['fixed'].values())}))


LABEL_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['defect', 'confidence'], 'properties': {
    'defect': {'type': 'string', 'enum': ['yes', 'no', 'unclear']},
    'confidence': {'type': 'integer', 'minimum': 1, 'maximum': 5}}}
LABEL_PROMPT = """Two pull requests of one repository: an EARLIER one, and a LATER one that changed some of the
earlier one's lines within two weeks. Decide whether the LATER one fixes a DEFECT the earlier one
introduced: a bug, a crash, a wrong value or wrong behaviour, a security, privacy or data problem.
Answer "no" for a follow-up feature, a refactor, a rename, a copy change or a planned next step, and
"unclear" when you cannot tell. Give a confidence from 1 to 5.

EARLIER PR title: {p_title}

LATER PR title: {q_title}
LATER PR description (start):
{q_body}

What the LATER PR changed in the earlier PR's files:
{diff}"""


def label(cfg, repo, limit=None):
    """For each case, whether its later PR fixes a defect of the earlier one (a capture-off reader with no
    tools); stored in the benchmark, printed as counts."""
    bench_p, mirror, _ = paths(cfg, repo)
    bench = json.load(open(bench_p))
    got, cost = collections.Counter(), 0.0
    for c in (sample(bench['cases'], limit) if limit else bench['cases']):
        if c.get('label'):
            got[c['label']['defect']] += 1
            continue
        p = pr_rules.get_pr(repo, c['pr'])
        best = None
        for fx in c['fixes']:
            q = pr_rules.get_pr(repo, fx['pr'])
            qc = q['mergeCommit']['oid']
            diff = git(mirror, 'diff', '-U3', '--no-ext-diff', f'{qc}^', qc, '--', *c['fixed'], check=False)[:5000]
            so, meta = pr_judge.ask(cfg, LABEL_PROMPT.format(p_title=p.get('title') or '', q_title=q.get('title') or '',
                                                             q_body=(q.get('body') or '')[:1500], diff=diff),
                                    LABEL_SCHEMA, cfg['judge_model'], cfg['judge_max_usd'])
            cost += meta.get('cost_usd') or 0
            if so and (best is None or (so['defect'] == 'yes' and best['defect'] != 'yes')):
                best = dict(so, fix_pr=fx['pr'])
            if best and best['defect'] == 'yes':
                break
        c['label'] = best or {'defect': 'unclear', 'confidence': 1}
        got[c['label']['defect']] += 1
        with open(bench_p, 'w') as f:
            json.dump(bench, f)
    print(json.dumps({'repo': repo, 'labels': dict(got), 'cost_usd': round(cost, 4)}))


HERE = os.path.dirname(os.path.abspath(__file__))
VERSIONED = ['pr_review.py', 'pr_format.py', 'pr_judge.py', 'pr_money.py', 'pr_digest.py', 'pr_context.py',
             '../style-guide.md']
TOLERANCE = 3  # lines: an entry this close to a fixed line counts as pointing at it


def version(cfg):
    """The reviewer's version for the report: a hash of the files that shape a review, and the prompt it uses."""
    flags = ('+context' if cfg.get('context') else '') + ('+suggestions' if cfg.get('suggestions') else '')
    h = hashlib.sha256(((cfg.get('prompt') or 'security') + flags).encode())
    for f in VERSIONED + ['../correctness-review.md', '../upstream/security-review.md']:
        h.update(open(os.path.join(HERE, f), 'rb').read())
    return h.hexdigest()[:10]


def paths(cfg, repo, ver=None):
    owner, name = repo.split('/')
    d = pr_state.private_dir(cfg, 'bench')
    return (os.path.join(d, f'{owner}__{name}.json'), os.path.join(d, 'mirrors', f'{owner}__{name}.git'),
            os.path.join(d, f'{owner}__{name}-results-{ver}.json') if ver else None)


def sample(items, n):
    """n items spread evenly over the list (deterministic)."""
    if n >= len(items):
        return list(items)
    return [items[round(k * (len(items) - 1) / max(n - 1, 1))] for k in range(n)]


def anchors(rv):
    """Where the review points: items on the new side and decisions linked to code, as (path, start, end)."""
    out = [(i['path'], i['line_start'], i['line_end'], i['kind']) for i in rv.get('items') or [] if i.get('side', 'new') == 'new']
    for d in rv.get('decisions') or []:
        ln = d.get('link') or {}
        if ln.get('kind') == 'code' and not d.get('auto'):
            out.append((ln['path'], ln['line_start'], ln['line_end'], 'decision'))
    return out


def raw_anchors(model):
    """The model's own entries before the script's confidence bar, with their confidence, so one run scores
    precision at stricter bars too: (path, start, end, kind, confidence)."""
    if not model:
        return None
    out = [(i['path'], i['line_start'], i['line_end'], i['kind'], i['confidence'])
           for i in model.get('items') or [] if i.get('side', 'new') == 'new']
    for d in model.get('decisions') or []:
        ln = d.get('link') or {}
        if ln.get('kind') == 'code' and ln.get('path') and ln.get('line_start'):
            out.append((ln['path'], ln['line_start'], ln.get('line_end') or ln['line_start'], 'decision', d['confidence']))
    return out


def run(cfg, repo, n_cases, n_controls, max_usd):
    """Review the sampled cases and controls at their heads with this version (nothing delivered); the
    results are saved after each PR, so a run resumes where it stopped."""
    import pr_review  # the review pipeline (lazy: it is heavy)
    bench_p, _, _ = paths(cfg, repo)
    bench = json.load(open(bench_p))
    ver = version(cfg)
    _, _, res_p = paths(cfg, repo, ver)
    res = json.load(open(res_p)) if os.path.exists(res_p) else {'version': ver, 'repo': repo, 'prs': {}}
    defects = [c for c in bench['cases'] if (c.get('label') or {}).get('defect') == 'yes']
    cases = defects if any(c.get('label') for c in bench['cases']) else bench['cases']  # labelled: real defects only
    picks = [('case', c) for c in sample(cases, n_cases)] + [('control', c) for c in sample(bench['controls'], n_controls)]
    spent = sum(r.get('cost_usd', 0) for r in res['prs'].values())
    for kind, c in picks:
        key = str(c['pr'])
        if key in res['prs']:
            continue
        if spent >= max_usd:
            break
        run_cfg = dict(cfg, bench=True, no_deliver=True, collect=[])
        entry = {'repo': repo, 'number': c['pr'], 'url': f"https://github.com/{repo}/pull/{c['pr']}",
                 'rig': cfg['repos'].get(repo, repo.split('/')[1]), 'rules': []}
        try:
            pr_review.model_review(run_cfg, f"{repo}#{c['pr']}", entry, dry=False)
        except Exception as e:  # one PR's failure must not stop the run
            res['prs'][key] = {'kind': kind, 'error': type(e).__name__, 'cost_usd': 0}
            continue
        got = run_cfg['collect'][0] if run_cfg['collect'] else None
        x = (got or {}).get('extra') or {}
        cost = sum(x.get(k) or 0 for k in ('cost_usd', 'judge_cost_usd', 'money_cost_usd', 'digest_cost_usd'))
        spent += cost
        res['prs'][key] = {'kind': kind, 'head': c['head'], 'cost_usd': round(cost, 4), 'held': (got or {}).get('held'),
                           'anchors': anchors(got['rv']) if got else None,
                           'raw': raw_anchors(got.get('model')) if got else None,
                           'context': {k[8:]: v for k, v in x.items() if k.startswith('context_')}, 'cleared': len((got or {}).get('rv', {}).get('cleared') or []),
                           'outcome': ((got or {}).get('rv') or {}).get('outcome'), 'turns': x.get('turns'),
                           'judge': x.get('judge'),
                           'error': None if got else 'no review'}
        with open(res_p, 'w') as f:
            json.dump(res, f)
        os.chmod(res_p, 0o600)
    score(cfg, repo, ver)


def head_lines(mirror, merge, head, path, lines):
    """The fixed lines (numbered in the merge commit) as numbered in the PR head, through the diff between
    the two; a line inside a changed hunk maps to the hunk's start."""
    out, shifts = set(), []
    r = git(mirror, 'diff', '-U0', '--no-ext-diff', merge, head, '--', path, check=False)
    for m in re.finditer(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@', r, re.M):
        a, b = int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)
        c, d = int(m.group(3)), int(m.group(4) if m.group(4) is not None else 1)
        shifts.append((a, b, c, d))
    for n in lines:
        off, mapped = 0, None
        for a, b, c, d in shifts:
            if n < a or (b == 0 and n <= a):
                break
            if a <= n < a + b:
                mapped = c
                break
            off = (c + (d or 1)) - (a + (b or 1))  # a zero-length side sits after the line it names
        out.add(mapped if mapped is not None else n + off)
    return out


def reader_spread(scores):
    """The reader test's scores over the reviewed PRs (the mayor, 02:35Z: a distribution per version, not
    one PR): median and 10th percentile of useful and markers, and how many reached the bar."""
    got = [x for x in scores if x]
    out = {'reader_n': len(got)}
    for dim in ('useful', 'markers'):
        v = sorted(x[dim] for x in got)
        out[f'{dim}_median'] = statistics.median(v) if v else None
        out[f'{dim}_p10'] = v[int(0.1 * (len(v) - 1))] if v else None
    out['reader_at_bar'] = sum(1 for x in got if not pr_judge.below_bar(x))
    return out


BARS = (8, 9, 10)  # the script posts entries at confidence 8 and up; the benchmark also scores 9 and 10


def on_fixed(a, fixed):
    return any(a[0] == p and any(a[1] - TOLERANCE <= n <= a[2] + TOLERANCE for n in ls) for p, ls in fixed.items())


def by_bar(rows):
    """rows: (kind, raw anchors, fixed lines or None) per reviewed PR. Per confidence bar: cases caught,
    entries, entries on fixed lines, controls with a change ask; only PRs whose run kept the raw entries."""
    out = {}
    for bar in BARS:
        caught = cases = entries = good = alarms = controls = 0
        for kind, raw, fixed in rows:
            kept = [a for a in raw if a[4] >= bar]
            if kind == 'control':
                controls += 1
                alarms += any(a[3] == 'change' for a in kept)
                continue
            cases += 1
            g = [a for a in kept if on_fixed(a, fixed)]
            entries += len(kept)
            good += len(g)
            caught += bool(g)
        out[str(bar)] = {'cases': cases, 'caught': caught, 'entries': entries, 'on_fixed_lines': good,
                         'precision_proxy': round(good / entries, 3) if entries else None,
                         'controls': controls, 'controls_with_a_change_ask': alarms}
    return out


def score(cfg, repo, ver):
    """Recall on the cases, a precision proxy and false alarms on the controls, and cost per PR."""
    bench_p, mirror, res_p = paths(cfg, repo, ver)
    if not os.path.exists(res_p):
        print(json.dumps({'repo': repo, 'version': ver, 'results': 'none for this version; --run first'}))
        return None
    bench, res = json.load(open(bench_p)), json.load(open(res_p))
    cases = {str(c['pr']): c for c in bench['cases']}
    hit = reviewed = entries = on_target = alarms = controls = defects = defects_hit = 0
    bars = []
    for key, r in res['prs'].items():
        if r.get('anchors') is None:
            continue
        if r['kind'] == 'control':
            controls += 1
            alarms += any(k == 'change' for *_, k in r['anchors'])
            if r.get('raw') is not None:
                bars.append(('control', r['raw'], None))
            continue
        reviewed += 1
        c = cases[key]
        is_defect = (c.get('label') or {}).get('defect') == 'yes'
        defects += is_defect
        fixed = {p: head_lines(mirror, c['merge'], c['head'], p, ls) for p, ls in c['fixed'].items()}
        good = [a for a in r['anchors'] if on_fixed(a, fixed)]
        if r.get('raw') is not None and is_defect:
            bars.append(('case', r['raw'], fixed))
        entries += len(r['anchors'])
        on_target += len(good)
        hit += bool(good)
        defects_hit += bool(good) and is_defect
    done = [r for r in res['prs'].values() if r.get('anchors') is not None]
    out = {'repo': repo, 'version': ver, 'cases_reviewed': reviewed, 'cases_caught': hit,
           'recall': round(hit / reviewed, 3) if reviewed else None,
           'defects_reviewed': defects, 'defects_caught': defects_hit,
           'recall_on_defects': round(defects_hit / defects, 3) if defects else None,
           'entries_on_cases': entries, 'entries_on_fixed_lines': on_target,
           'precision_proxy': round(on_target / entries, 3) if entries else None,
           'controls_reviewed': controls, 'controls_with_a_change_ask': alarms,
           'errors': sum(1 for r in res['prs'].values() if r.get('anchors') is None),
           **reader_spread([r.get('judge') for r in done]),
           'by_confidence': by_bar(bars) if bars else None,
           'cost_per_pr_usd': round(sum(r['cost_usd'] for r in done) / len(done), 3) if done else None}
    print(json.dumps(out))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--mine', metavar='REPO')
    ap.add_argument('--run', metavar='REPO')
    ap.add_argument('--score', metavar='REPO')
    ap.add_argument('--label', metavar='REPO')
    ap.add_argument('--days', type=int, default=60)
    ap.add_argument('--within', type=int, default=14)
    ap.add_argument('--cases', type=int, default=5)
    ap.add_argument('--controls', type=int, default=5)
    ap.add_argument('--max-usd', type=float, default=10)
    ap.add_argument('--limit', type=int, help='--label: an even sample of this many cases')
    a = ap.parse_args()
    cfg = pr_state.config()
    if a.mine:
        mine(cfg, a.mine, a.days, a.within)
    elif a.run:
        run(cfg, a.run, a.cases, a.controls, a.max_usd)
    elif a.label:
        label(cfg, a.label, a.limit)
    elif a.score:
        score(cfg, a.score, version(cfg))
    else:
        print(__doc__)


if __name__ == '__main__':
    main()
