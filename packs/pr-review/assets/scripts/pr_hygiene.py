#!/usr/bin/env python3
"""Host-local hygiene check (the mayor, 2026-10-30 03:05Z, decision-64e81af8): no public file of this pack
names a person or a repository from the town's overrides (PR_REVIEW_NAMES logins and names,
PR_REVIEW_REVIEWER_NAME, PR_REVIEW_REPOS owners, repositories and rigs). Examples in the pack use an
unrelated domain and the words "the reviewer" and "the author". It prints file paths and counts only,
never the names it looks for, and is skipped where the town's overrides are not in the environment.

  pr_hygiene.py [PACK_DIR]    exit 1 when a public file names one
"""
import os
import re
import subprocess
import sys


def names():
    """The words to look for, from the town's overrides in the environment."""
    out = set()
    for pair in os.environ.get('PR_REVIEW_NAMES', '').split():
        out |= {x for x in pair.split('=') if x}
    if os.environ.get('PR_REVIEW_REVIEWER_NAME'):
        out.add(os.environ['PR_REVIEW_REVIEWER_NAME'])
    for pair in os.environ.get('PR_REVIEW_REPOS', '').split():
        repo, _, rig = pair.partition('=')
        out |= {repo, *repo.split('/'), rig} - {''}
    return {x for x in out if len(x) >= 3}


def main():
    pack = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..')
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
