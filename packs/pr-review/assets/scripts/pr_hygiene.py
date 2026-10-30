#!/usr/bin/env python3
"""Host-local hygiene check (the mayor, 2026-10-30 03:05Z, decision-64e81af8): no public file of this pack
names a person or a repository from the town's overrides (PR_REVIEW_NAMES logins and names,
PR_REVIEW_REVIEWER_NAME, PR_REVIEW_REPOS owners, repositories and rigs). Examples in the pack use an
unrelated domain and the words "the reviewer" and "the author". It prints file paths and counts only,
never the names it looks for, and is skipped where the town's overrides are not in the environment.

The same check covers commit messages and a PR's title and body (decision-67f9a695, hygiene B, forward
only): there the reviewer's own login and name are allowed; the client's people, owners, repositories
and rigs are not. Matching in messages ignores case. The refinery runs it as a gate at the PR head.

  pr_hygiene.py [PACK_DIR]                                    exit 1 when a public file names one
  pr_hygiene.py --messages REV_RANGE [--pr N --repo OWNER/NAME] [--city CITY_TOML]
                                      exit 1 when a commit message in the range, or the PR's title or body,
                                      names one; the words come from the environment, or with --city from
                                      every env table there that carries the name keys
"""
import argparse
import json
import os
import re
import subprocess
import sys

KEYS = ('PR_REVIEW_NAMES', 'PR_REVIEW_REVIEWER_NAME', 'PR_REVIEW_REPOS', 'PR_REVIEW_REVIEWER')


def names(env=None):
    """The words to look for, from the town's overrides (the environment, or one table of them)."""
    env = os.environ if env is None else env
    out = set()
    for pair in env.get('PR_REVIEW_NAMES', '').split():
        out |= {x for x in pair.split('=') if x}
    if env.get('PR_REVIEW_REVIEWER_NAME'):
        out.add(env['PR_REVIEW_REVIEWER_NAME'])
    for pair in env.get('PR_REVIEW_REPOS', '').split():
        repo, _, rig = pair.partition('=')
        out |= {repo, *repo.split('/'), rig} - {''}
    return {x for x in out if len(x) >= 3}


def own(env):
    """The reviewer's own login and name, and the names-map pair that holds either (allowed in messages)."""
    mine = {env[k] for k in ('PR_REVIEW_REVIEWER', 'PR_REVIEW_REVIEWER_NAME') if env.get(k)}
    for pair in env.get('PR_REVIEW_NAMES', '').split():
        parts = {x for x in pair.split('=') if x}
        if parts & mine:
            mine |= parts
    return mine


def tables(node, path=()):
    """The env tables of a city config that carry the town's name keys (orders' overrides)."""
    if isinstance(node, dict):
        if any(k in node for k in KEYS):
            yield path, node
        for k, v in node.items():
            yield from tables(v, path + (str(k),))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from tables(v, path + (f'[{i}]',))


def word_pattern(words, flags=0):
    return re.compile(r'(?<![\w-])(' + '|'.join(re.escape(w) for w in sorted(words, key=len, reverse=True))
                      + r')(?![\w-])', flags)


def messages(pack, rev_range, pr=None, repo=None, city=None):
    """Hygiene B: count the client names in each commit message of the range and the PR's title and body.
    Prints labels and counts only, never the names or the text. Returns the exit code."""
    if city:
        import tomllib
        with open(city, 'rb') as f:
            envs = [{k: str(v) for k, v in t.items() if k in KEYS} for _, t in tables(tomllib.load(f))]
    else:
        envs = [os.environ]
    words = set().union(*[names(e) - own(e) for e in envs]) if envs else set()
    if not words:
        print('message hygiene: skipped (no town overrides found)')
        return 0
    pattern = word_pattern(words, re.I)
    texts = []
    r = subprocess.run(['git', '-C', pack, 'rev-list', rev_range], capture_output=True, text=True)
    if r.returncode != 0:
        print(f'message hygiene: git rev-list failed (rc {r.returncode})')
        return 1
    for c in r.stdout.split():
        msg = subprocess.run(['git', '-C', pack, 'log', '-1', '--format=%B', c], capture_output=True, text=True).stdout
        texts.append((f'commit {c[:7]} message', msg))
    if pr:
        r = subprocess.run(['gh', 'pr', 'view', str(pr), '--json', 'title,body'] + (['--repo', repo] if repo else []),
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            print(f'message hygiene: gh pr view failed (rc {r.returncode})')
            return 1
        p = json.loads(r.stdout)
        texts += [(f'PR #{pr} title', p.get('title') or ''), (f'PR #{pr} body', p.get('body') or '')]
    hits = [(label, len({m.lower() for m in pattern.findall(text)})) for label, text in texts]
    hits = [(label, n) for label, n in hits if n]
    if hits:
        print(f'message hygiene: FAIL, {len(hits)} of {len(texts)} texts name a client person, owner, repository '
              f'or rig: ' + ', '.join(f'{label} ({n})' for label, n in hits))
        return 1
    print(f'message hygiene: clean ({len(texts)} texts, {len(words)} client names checked)')
    return 0


def main():
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
    if '--messages' in sys.argv:
        ap = argparse.ArgumentParser()
        ap.add_argument('--messages', required=True, metavar='REV_RANGE')
        ap.add_argument('--pr')
        ap.add_argument('--repo')
        ap.add_argument('--city')
        a = ap.parse_args()
        return messages(here, a.messages, a.pr, a.repo, a.city)
    pack = sys.argv[1] if len(sys.argv) > 1 else here
    words = names()
    if not words:
        print('hygiene: skipped (the town overrides are not in the environment)')
        return 0
    pattern = re.compile(r'(?<![\w-])(' + '|'.join(re.escape(w) for w in sorted(words, key=len, reverse=True)) + r')(?![\w-])')
    r = subprocess.run(['git', '-C', pack, 'ls-files', '-z', '.'], capture_output=True, text=True)
    files = [f for f in r.stdout.split('\0') if f] if r.returncode == 0 else []
    hits = []
    for f in files:
        try:
            text = open(os.path.join(pack, f), encoding='utf-8').read()
        except (OSError, UnicodeDecodeError):
            continue
        n = len(pattern.findall(text))
        if n:
            hits.append((f, n))
    if hits:
        print(f'hygiene: FAIL, {len(hits)} public files name a town person or repository: '
              + ', '.join(f'{f} ({n})' for f, n in hits))
        return 1
    print(f'hygiene: clean ({len(files)} public files, {len(words)} town names checked)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
