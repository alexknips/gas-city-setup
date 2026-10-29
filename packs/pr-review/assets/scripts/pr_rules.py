#!/usr/bin/env python3
"""Zero-token PR rules (README.md, "Rules"): what a PR's paths, added lines, CI and migrations trip.

Reads GitHub through `gh` (GraphQL for the PR, its checks and review requests; REST for the changed
files and their patches). No model, no tokens. Everything it returns is a rule id, a pattern index, a
count, a PR number or a link: never a title, a path, a line or a check name, because those are the
repository's own text and the sweep's records and messages must not carry it (README, "What never
leaves GitHub").
"""
import json
import os
import re
import subprocess
import tomllib

CODE_EXT = re.compile(r'\.(py|ts|tsx|js|jsx|mjs|cjs|go|rb|java|kt|swift|rs|php|cs|scala|ex|exs|vue|svelte|sql)$', re.I)
TEST_PATH = re.compile(r'(?i)(^|/)(tests?|__tests__|spec|e2e|cypress|playwright)/|\.(test|spec)\.|_test\.|(^|/)test_[^/]*$')
MIG_DIR = re.compile(r'(?i)(^|/)(migrations?|migrate|alembic/versions|drizzle)/')
MIG_KEY = re.compile(r'[Vv]?(\d+(?:[._]\d+)*)')
CI_RED = {'FAILURE', 'TIMED_OUT', 'ACTION_REQUIRED', 'STARTUP_FAILURE', 'CANCELLED', 'ERROR'}
CI_WAIT = {'PENDING', 'EXPECTED', 'QUEUED', 'IN_PROGRESS', 'WAITING', 'REQUESTED'}

# Code-defined rules: id -> (area, label). Areas as in rules.toml; "scope" selects without a finding.
BUILTIN = {
    'requested': ('scope', 'the reviewer was asked for a review'),
    'ci-red': ('A7', 'a check failed, errored, timed out or was cancelled on the head commit'),
    'ci-none': ('A7', 'no check or status reported on the head commit'),
    'ci-skipped': ('A7', 'checks reported, none succeeded and none failed (all skipped or neutral)'),
    'migration': ('A4', 'adds, changes or removes a migration'),
    'migration-edit': ('A4', 'changes or removes a migration that is already on the base branch'),
    'migration-order': ('A4', "a new migration does not sort after the base branch's newest, or reuses its number"),
    'conflicts': ('A4', 'GitHub reports a merge conflict with the base branch'),
    'untested': ('A5', 'code changed and no test file changed'),
    'no-description': ('B3', 'the description is empty or a line'),
}

PR_FIELDS = """
  number url title state isDraft createdAt updatedAt mergedAt closedAt
  author { login }
  additions deletions changedFiles body
  headRefOid baseRefName baseRefOid mergeable
  mergeCommit { oid parents(first: 1) { nodes { oid } } }
  reviewRequests(first: 20) { nodes { requestedReviewer { ... on User { login } ... on Team { slug } } } }
  timelineItems(itemTypes: [REVIEW_REQUESTED_EVENT], first: 20) {
    nodes { ... on ReviewRequestedEvent { requestedReviewer { ... on User { login } } } } }
  commits(last: 1) { nodes { commit { oid statusCheckRollup { state
    contexts(first: 100) { totalCount nodes { __typename
      ... on CheckRun { name status conclusion } ... on StatusContext { context state } } } } } } }
"""


class GhError(RuntimeError):
    pass


def gh(args, timeout=120):
    r = subprocess.run(['gh', *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        # gh's stderr can quote the request; keep the first line only and never the response body.
        raise GhError(f"gh {args[0]} {args[1] if len(args) > 1 else ''} failed: {r.stderr.strip().splitlines()[:1]}")
    return r.stdout


def graphql(query, **variables):
    args = ['api', 'graphql', '-f', f'query={query}']
    for k, v in variables.items():
        if v is not None:
            args += ['-F' if isinstance(v, int) else '-f', f'{k}={v}']
    out = json.loads(gh(args))
    if out.get('errors'):
        raise GhError(f"graphql: {[e.get('type') or e.get('message', '')[:80] for e in out['errors']]}")
    return out['data']


def search_prs(query, page=25):
    """Every PR a GitHub search returns, with PR_FIELDS (search caps at 1000 results)."""
    q = ('query($q: String!, $n: Int!, $after: String) { search(query: $q, type: ISSUE, first: $n, after: $after) {'
         ' issueCount pageInfo { hasNextPage endCursor } nodes { ... on PullRequest {' + PR_FIELDS + '} } } }')
    after, prs = None, []
    while True:
        s = graphql(q, q=query, n=page, after=after)['search']
        prs += [n for n in s['nodes'] if n]
        if not s['pageInfo']['hasNextPage']:
            return prs
        after = s['pageInfo']['endCursor']


def open_prs(repo, page=25):
    """Open PRs with PR_FIELDS, and {default branch: head oid}."""
    owner, name = repo.split('/')
    q = ('query($o: String!, $r: String!, $n: Int!, $after: String) { repository(owner: $o, name: $r) {'
         ' defaultBranchRef { name target { oid } }'
         ' pullRequests(states: OPEN, first: $n, after: $after) { pageInfo { hasNextPage endCursor }'
         ' nodes {' + PR_FIELDS + '} } } }')
    after, prs, heads = None, [], {}
    while True:
        r = graphql(q, o=owner, r=name, n=page, after=after)['repository']
        heads[r['defaultBranchRef']['name']] = r['defaultBranchRef']['target']['oid']
        prs += r['pullRequests']['nodes']
        if not r['pullRequests']['pageInfo']['hasNextPage']:
            return prs, heads
        after = r['pullRequests']['pageInfo']['endCursor']


def get_pr(repo, number):
    owner, name = repo.split('/')
    q = ('query($o: String!, $r: String!, $n: Int!) { repository(owner: $o, name: $r) {'
         ' pullRequest(number: $n) {' + PR_FIELDS + '} } }')
    return graphql(q, o=owner, r=name, n=number)['repository']['pullRequest']


def base_for(pr, branch_heads):
    """The commit a PR's new migrations must sort after: main just before the merge for a merged PR,
    the base branch's head now for an open one."""
    if pr['state'] == 'MERGED' and pr.get('mergeCommit'):
        parents = pr['mergeCommit']['parents']['nodes']
        return parents[0]['oid'] if parents else None
    return branch_heads.get(pr['baseRefName']) or pr['baseRefOid']


def branch_oid(repo, branch):
    owner, name = repo.split('/')
    q = 'query($o: String!, $r: String!, $q: String!) { repository(owner: $o, name: $r) { ref(qualifiedName: $q) { target { oid } } } }'
    ref = graphql(q, o=owner, r=name, q=f'refs/heads/{branch}')['repository']['ref']
    return ref and ref['target']['oid']


def pr_files(repo, number):
    """The PR's changed files with their patches (GitHub leaves `patch` out of very large files)."""
    out = gh(['api', '--paginate', f'repos/{repo}/pulls/{number}/files?per_page=100', '--jq',
              '.[] | {filename, status, additions, deletions, patch, previous_filename}'], timeout=300)
    return [json.loads(line) for line in out.splitlines() if line.strip()]


_tree_cache = {}


def tree_names(repo, oid, path):
    """Entry names of directory `path` at commit `oid`, or None when it does not exist there."""
    key = (repo, oid, path)
    if key not in _tree_cache:
        owner, name = repo.split('/')
        q = ('query($o: String!, $r: String!, $e: String!) { repository(owner: $o, name: $r) {'
             ' object(expression: $e) { ... on Tree { entries { name } } } } }')
        obj = graphql(q, o=owner, r=name, e=f'{oid}:{path}')['repository']['object']
        _tree_cache[key] = None if obj is None else [e['name'] for e in obj['entries']]
    return _tree_cache[key]


def load_rules(path):
    with open(path, 'rb') as f:
        cfg = tomllib.load(f)
    rules = []
    for r in cfg.get('rule', []):
        rules.append({
            'id': r['id'], 'area': r['area'], 'label': r.get('label', ''),
            'paths': [re.compile(p) for p in r.get('paths', [])],
            'added': [re.compile(p) for p in r.get('added', [])],
            'in_paths': [re.compile(p) for p in r.get('in_paths', [])],
            'not_paths': [re.compile(p) for p in r.get('not_paths', [])],
            'tests': r.get('tests', 'skip'),
        })
        if rules[-1]['tests'] not in ('skip', 'also', 'only'):
            raise ValueError(f"rule {r['id']}: tests must be skip, also or only")
    ids = [r['id'] for r in rules]
    clash = set(ids) & set(BUILTIN) or {i for i in ids if ids.count(i) > 1}
    if clash:
        raise ValueError(f'rule ids must be unique and not built in: {sorted(clash)}')
    return {'select_areas': set(cfg.get('select_areas', ['A1', 'A2', 'A4', 'A7'])), 'rules': rules,
            'not_paths': [re.compile(p) for p in cfg.get('not_paths', [])],
            'test_paths': [re.compile(p) for p in cfg.get('test_paths', [TEST_PATH.pattern])]}


def areas(ruleset):
    out = {k: v[0] for k, v in BUILTIN.items()}
    out.update({r['id']: r['area'] for r in ruleset['rules']})
    return out


def ci_state(pr):
    """green / red / none / skipped / pending, from the head commit's check rollup."""
    nodes = (pr.get('commits') or {}).get('nodes') or []
    roll = nodes and nodes[0]['commit'].get('statusCheckRollup')
    if not roll:
        return 'none'
    ctx = roll['contexts']['nodes']
    concl = [c.get('conclusion') or c.get('state') for c in ctx]
    status = [c.get('status') for c in ctx if c['__typename'] == 'CheckRun']
    if any(c in CI_RED for c in concl) or roll['state'] in ('FAILURE', 'ERROR'):
        return 'red'
    if roll['state'] in CI_WAIT or any(s and s != 'COMPLETED' for s in status) or any(c in CI_WAIT for c in concl):
        return 'pending'
    if not any(c == 'SUCCESS' for c in concl):
        return 'skipped'
    return 'green'


def requested(pr, reviewer):
    who = reviewer.lower()
    now = [((n.get('requestedReviewer') or {}).get('login') or '').lower() for n in pr['reviewRequests']['nodes']]
    ever = [((n.get('requestedReviewer') or {}).get('login') or '').lower() for n in pr['timelineItems']['nodes']]
    return who in now or who in ever


def added_lines(patch):
    for line in (patch or '').splitlines():
        if line.startswith('+') and not line.startswith('+++'):
            yield line[1:]


def _mig_parts(path):
    """(migration dir, entry name, sequence prefix) for a path inside a migration dir, else None.
    The entry is the path part right below the dir: a file (Rails, Drizzle, Knex) or a folder (Prisma)."""
    m = MIG_DIR.search(path)
    if not m:
        return None
    comp = path[m.end():].split('/', 1)[0]
    k = MIG_KEY.match(comp)
    return path[:m.end() - 1], comp, (k.group(1) if k else None)


def migration_hits(repo, files, base_oid):
    """migration / migration-edit / migration-order hits, comparing new migrations with the base tree.
    Order is the entries' names compared as strings, the order Prisma, Rails, Drizzle, Knex and most
    other runners apply them in (a 12-digit and a 14-digit timestamp compare as text, not as numbers);
    a sequence prefix that an existing entry already has is a duplicate."""
    hits, added = {}, {}
    for f in files:
        names = [f['filename']] + ([f['previous_filename']] if f.get('previous_filename') else [])
        for n in names:
            parts = _mig_parts(n)
            if not parts:
                continue
            hits.setdefault('migration', []).append('m')
            d, comp, seq = parts
            if seq is None:
                continue
            if f['status'] == 'added' or (f['status'] == 'renamed' and n == f['filename']):
                added.setdefault(d, {})[comp] = seq
            elif f['status'] in ('modified', 'removed', 'changed', 'renamed'):
                hits.setdefault('migration-edit', []).append(f['status'][:3])
    if added and base_oid:
        for d, new in added.items():
            base = tree_names(repo, base_oid, d)
            if base is None:
                continue
            old = {c: k.group(1) for c in base if c not in new for k in [MIG_KEY.match(c)] if k}
            newest = max(old, default=None)
            seqs = list(old.values())
            for comp, seq in sorted(new.items()):
                if seq in seqs:
                    hits.setdefault('migration-order', []).append('dup')
                elif newest is not None and comp < newest:
                    hits.setdefault('migration-order', []).append('order')
                seqs.append(seq)
    return hits


def evaluate(pr, files, ruleset, reviewer, repo, base_oid):
    """{rule id: [hit markers]} for one PR. Markers are pattern indexes (p0 = paths[0], a1 = added[1]),
    or a short word for code rules: never the matched text."""
    hits = {}
    if requested(pr, reviewer):
        hits['requested'] = ['r']
    ci = ci_state(pr)
    if ci in ('red', 'none', 'skipped'):
        hits[f'ci-{ci}'] = [ci]
    if pr.get('mergeable') == 'CONFLICTING' and pr['state'] == 'OPEN':
        hits['conflicts'] = ['c']
    if len((pr.get('body') or '').strip()) < 40:
        hits['no-description'] = ['d']
    code = [f for f in files if CODE_EXT.search(f['filename']) and not TEST_PATH.search(f['filename'])
            and not MIG_DIR.search(f['filename'])]
    tests = [f for f in files if TEST_PATH.search(f['filename'])]
    if sum(f['additions'] for f in code) >= 20 and not tests:
        hits['untested'] = ['u']
    hits.update(migration_hits(repo, files, base_oid))
    files = [f for f in files if not any(p.search(f['filename']) for p in ruleset['not_paths'])]
    for r in ruleset['rules']:
        marks = []
        for f in files:
            path = f['filename']
            if any(p.search(path) for p in r['not_paths']):
                continue
            is_test = any(p.search(path) for p in ruleset['test_paths'])
            if (r['tests'] == 'skip' and is_test) or (r['tests'] == 'only' and not is_test):
                continue
            for i, p in enumerate(r['paths']):
                if p.search(path):
                    marks.append(f'p{i}')
            if r['added'] and (not r['in_paths'] or any(p.search(path) for p in r['in_paths'])):
                for line in added_lines(f.get('patch')):
                    for i, p in enumerate(r['added']):
                        if p.search(line):
                            marks.append(f'a{i}')
        if marks:
            hits[r['id']] = marks
    return hits


# Rules about what a PR's content does (not CI, conflicts or who was asked): when one fires, the PR is
# read by the model even if CI alone would escalate it (style guide R11).
NOT_CONTENT = {'requested', 'ci-red', 'ci-none', 'ci-skipped', 'conflicts', 'untested', 'no-description'}


def locate(files, ruleset, hits, limit=12):
    """Where the content rules in `hits` matched: [{rule, label, path, line}] with new-side line numbers
    (a path rule points at the file's first changed line). In memory only: the review prompt uses them
    so a "needs you" names the exact hunks (R12)."""
    by_id = {r['id']: r for r in ruleset['rules']}
    out = []
    for f in files:
        path, patch = f['filename'], f.get('patch') or ''
        if any(p.search(path) for p in ruleset['not_paths']):
            continue
        is_test = any(p.search(path) for p in ruleset['test_paths'])
        added, line, first = [], 0, None
        for raw in patch.splitlines():
            m = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)', raw)
            if m:
                line = int(m.group(1)); continue
            if raw.startswith('+') and not raw.startswith('+++'):
                added.append((line, raw[1:])); first = first or line; line += 1
            elif not raw.startswith('-'):
                line += 1
        for rid in hits:
            r = by_id.get(rid)
            if r is None:
                if rid.startswith('migration') and MIG_DIR.search(path) and first:
                    out.append({'rule': rid, 'label': BUILTIN[rid][1], 'path': path, 'line': first})
                continue
            if any(p.search(path) for p in r['not_paths']) or (r['tests'] == 'skip' and is_test) or (
                    r['tests'] == 'only' and not is_test):
                continue
            if any(p.search(path) for p in r['paths']) and first:
                out.append({'rule': rid, 'label': r['label'], 'path': path, 'line': first})
            if r['added'] and (not r['in_paths'] or any(p.search(path) for p in r['in_paths'])):
                for ln, text in added:
                    if any(p.search(text) for p in r['added']):
                        out.append({'rule': rid, 'label': r['label'], 'path': path, 'line': ln})
                        break
    seen, uniq = set(), []
    for o in out:
        k = (o['rule'], o['path'])
        if k not in seen:
            seen.add(k)
            uniq.append(o)
    return uniq[:limit]


# Rules whose hit is serious on its own: they go before the other select rules when the cap bites.
SERIOUS = {'migration-order', 'migration-edit', 'conflicts', 'irrev-destructive', 'phi-ehr-write', 'phi-logs',
           'phi-fixtures', 'sec-secrets', 'ci-red', 'ci-none', 'ci-skipped'}


def priority(hits, ruleset, area_of):
    """0 = the reviewer was asked, 1 = a serious rule tripped, 2 = another select rule, None = not
    selected. Within a tier the sweep takes more distinct areas first, then the oldest PR."""
    if 'requested' in hits:
        return 0
    sel = [h for h in hits if area_of.get(h) in ruleset['select_areas']]
    if not sel:
        return None
    return 1 if SERIOUS & set(sel) else 2


def selected(hits, ruleset, area_of):
    """Reviewed when the reviewer was asked or a rule in a select area tripped."""
    return 'requested' in hits or any(area_of.get(h) in ruleset['select_areas'] for h in hits)


def load_avg():
    try:
        return os.getloadavg()[0]
    except OSError:
        return 0.0
