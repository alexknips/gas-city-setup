#!/usr/bin/env python3
"""The repository's rules digest (style guide R37). It holds the repository's goals and high-level rules
as its own docs write them down: README, AGENTS.md and CLAUDE.md, CONTRIBUTING, SECURITY, docs/, the PR
template and the CI workflows. It takes one model pass per change of those docs, cached by the docs'
blob SHAs. The digest is the repository's text, so it stays host-local (PR_REVIEW_WORK_DIR/digest, mode
600), and this script prints counts only. Every review gets the digest. Findings and decisions cite its
rules by id, and the review links each cited rule to its doc lines.

  pr_digest.py --build REPO    build or reuse the digest for the default branch; prints counts
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_format  # noqa: E402
import pr_judge  # noqa: E402
import pr_state  # noqa: E402

VERSION = 1
MAX_DOC_CHARS, MAX_TOTAL_CHARS, MAX_RULES, MAX_RULE_LINES, MAX_DOCS = 20_000, 160_000, 40, 40, 200
SKIP = re.compile(r'(?i)(^|/)(node_modules|vendor|third_party|dist|build|\.venv|changelog)(/|\.|$)')
TOP = re.compile(r'(?i)^(readme|agents|claude|contributing|security|governance)(\.(md|mdx|txt|rst))?$')
NESTED = re.compile(r'(?i)(^|/)(agents|claude)\.md$')
TEMPLATE = re.compile(r'(?i)^\.github/pull_request_template\.md$')
DOCS = re.compile(r'(?i)^(docs?|adrs?|documentation)/.+\.(md|mdx|txt|rst)$')
KEY = re.compile(r'(?i)architect|adr|decision|complian|security|hipaa|soc|polic|privacy|guide|convention|'
                 r'standard|rule|contribut|testing|migration|data')
WORKFLOW = re.compile(r'(?i)^\.github/workflows/[^/]+\.ya?ml$')
# Areas the review frames patient data and SOC 2 findings against (R37: say so when the repo has none).
FRAMED = ('patient data', 'security', 'compliance')

SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['goals', 'rules'], 'properties': {
    'goals': {'type': 'string', 'maxLength': 400, 'description': 'What the repository is for, in one or two plain sentences.'},
    'rules': {'type': 'array', 'maxItems': MAX_RULES, 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['rule', 'area', 'path', 'line_start', 'line_end'],
        'properties': {
            'rule': {'type': 'string', 'maxLength': 200, 'description': 'The rule in one plain sentence.'},
            'area': {'type': 'string', 'enum': pr_format.AREAS},
            'path': {'type': 'string', 'description': 'The doc that states it, as given.'},
            'line_start': {'type': 'integer', 'minimum': 1},
            'line_end': {'type': 'integer', 'minimum': 1}}}}}}

PROMPT = """Below are the docs of one software repository, each line numbered. Extract the repository's goals
and the high-level rules a code reviewer should check a pull request against: architecture constraints,
conventions the docs say are required, security, privacy and compliance rules (patient data, HIPAA,
SOC 2), testing and CI requirements, data and migration rules, and what must never happen. Take only
rules the docs state; never invent one. Write each rule as one plain sentence of at most 200 characters,
give it one area, and give the doc and the lines that state it. At most {max_rules} rules, the most
consequential first. Skip setup instructions, how-tos and anything that does not constrain a change.

{docs}"""


def rank(path):
    """Which docs come first when the digest has to leave some out (lower is earlier; None = not a doc)."""
    if SKIP.search(path):
        return None
    if TOP.match(path):
        return 0
    if NESTED.search(path):
        return 1
    if TEMPLATE.match(path):
        return 2
    if DOCS.match(path):
        return 3 if KEY.search(path) else 4
    if WORKFLOW.match(path):
        return 5
    return None


def git(mirror, *args):
    r = subprocess.run(['git', '-C', mirror, *args], capture_output=True, text=True, timeout=600,
                       env=dict(os.environ, GIT_TERMINAL_PROMPT='0'))
    if r.returncode != 0:
        raise RuntimeError(f'git {args[0]} failed (rc {r.returncode})')
    return r.stdout


def select(mirror, commit):
    """[(path, blob sha)] of the docs the digest may read, in rank order (no sizes: `ls-tree -l` would
    fetch every blob of a partial clone)."""
    entries = []
    for line in git(mirror, 'ls-tree', '-r', commit).splitlines():
        meta, _, path = line.partition('\t')
        parts = meta.split()
        if len(parts) == 3 and parts[1] == 'blob' and rank(path) is not None:
            entries.append((rank(path), path, parts[2]))
    return [(path, blob) for _, path, blob in sorted(entries)][:MAX_DOCS]


def cache_path(cfg, repo):
    return os.path.join(pr_state.private_dir(cfg, 'digest'), repo.replace('/', '__') + '.json')


def get(cfg, repo, mirror, commit):
    """The digest for the docs at `commit` (built when they changed): {sha, goals, rules: {D1: {rule, area,
    path, line_start, line_end}}, missing: [areas of FRAMED with no rule]} or None; and meta for the log."""
    try:
        docs = select(mirror, commit)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        return None, {'digest': 'error', 'error': type(e).__name__}
    if not docs:
        return None, {'digest': 'none', 'docs': 0}
    key = hashlib.sha256((f'v{VERSION}\n' + '\n'.join(f'{p} {b}' for p, b in docs)).encode()).hexdigest()
    p = cache_path(cfg, repo)
    try:
        old = json.load(open(p))
        if old.get('key') == key:
            return old, {'digest': 'cached', 'docs': len(docs), 'rules': len(old['rules'])}
    except (OSError, ValueError, KeyError):
        pass
    texts, lines_of, total = [], {}, 0
    for path, blob in docs:
        try:
            body = git(mirror, 'cat-file', 'blob', blob)[:MAX_DOC_CHARS]
        except (RuntimeError, subprocess.SubprocessError):
            continue
        if total + len(body) > MAX_TOTAL_CHARS:
            continue  # policy docs come first; a later, smaller doc may still fit
        total += len(body)
        rows = body.splitlines()
        lines_of[path] = len(rows)
        texts.append(f'=== {path}\n' + '\n'.join(f'{n}: {t}' for n, t in enumerate(rows, 1)))
    so, meta = pr_judge.ask(cfg, PROMPT.format(max_rules=MAX_RULES, docs='\n\n'.join(texts)), SCHEMA,
                            cfg['digest_model'], cfg['digest_max_turns'], timeout=900)
    if so is None:
        return None, {'digest': 'error', 'error': meta.get('error'), 'cost_usd': meta.get('cost_usd', 0)}
    rules = {}
    for r in so.get('rules') or []:
        n = lines_of.get(r.get('path'))
        if not n or not 1 <= r['line_start'] <= r['line_end'] <= n or r['line_end'] - r['line_start'] > MAX_RULE_LINES:
            continue  # a rule must point at lines of a doc it was given
        rules[f'D{len(rules) + 1}'] = {k: r[k] for k in ('rule', 'area', 'path', 'line_start', 'line_end')}
    areas = {r['area'] for r in rules.values()}
    dg = {'version': VERSION, 'key': key, 'repo': repo, 'sha': commit, 'built_at': pr_state.iso(pr_state.now()),
          'docs': len(texts), 'goals': so.get('goals') or '', 'rules': rules,
          'missing': [a for a in FRAMED if a not in areas], 'cost_usd': meta.get('cost_usd', 0)}
    with open(p, 'w') as f:
        json.dump(dg, f)
    os.chmod(p, 0o600)
    return dg, {'digest': 'built', 'docs': len(texts), 'rules': len(rules), 'cost_usd': meta.get('cost_usd', 0)}


def prompt_block(dg):
    """The digest as the review prompt reads it."""
    if not dg or not dg['rules']:
        return ("THE REPOSITORY'S RULES: its docs state none. A finding about patient data, security or compliance "
                "says that the repository has no written rule for it.")
    rules = '\n'.join(f"- {i} [{r['area']}] {r['rule']} ({r['path']} L{r['line_start']}-L{r['line_end']})"
                      for i, r in dg['rules'].items())
    missing = (f"\nAreas with no written rule: {', '.join(dg['missing'])}. A finding in one of them says that the "
               "repository has no written rule for it.") if dg.get('missing') else ''
    return (f"THE REPOSITORY'S RULES (from its own docs at the base; cite a rule by its id in `rule`):\n"
            f"Goals: {dg.get('goals') or '(not stated)'}\n{rules}{missing}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--build', metavar='REPO')
    a = ap.parse_args()
    if not a.build:
        print(__doc__)
        return
    import pr_review  # the mirror (lazy: pr_review imports this module)
    cfg = pr_state.config()
    owner, name = a.build.split('/')
    mirror = os.path.join(cfg['work'], 'mirrors', f'{owner}__{name}.git')
    if not os.path.isdir(mirror):
        print(json.dumps({'repo': a.build, 'digest': 'no mirror yet; the first review makes it'}))
        return
    branch = pr_review.git(mirror, 'symbolic-ref', '--short', 'HEAD').strip()
    pr_review.git(mirror, 'fetch', '--quiet', 'origin', f'+refs/heads/{branch}:refs/heads/{branch}')
    dg, meta = get(cfg, a.build, mirror, pr_review.git(mirror, 'rev-parse', f'refs/heads/{branch}').strip())
    print(json.dumps({'repo': a.build, **meta, 'missing': (dg or {}).get('missing')}))


if __name__ == '__main__':
    main()
