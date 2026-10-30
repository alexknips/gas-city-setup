#!/usr/bin/env python3
"""Repository context for a review (gc-sy1hm.2, parity feature a), found by a word search (git grep) in
the worktree at the PR head; no tokens are spent. The block lists names, paths and line numbers only, and
the model opens them before it judges (FINAL step 1):

1. Existing code the PR changes (a function, method or type whose body or definition line changed, or
   whose definition left a file), and where the code outside the diff uses it: a broken contract shows
   at its callers.
2. Existing code the new lines call or render, and where it is defined: new code that misuses an
   existing function shows against its definition. (On the benchmark's escaped bugs, most fixed lines
   sit in code the PR adds, which has no callers outside the diff yet.)
3. Tests: those that use the changed code, the test files named after the changed files, and the
   changed files that have none.

The search is textual: a name that two unrelated things share lists both (the definition search skips a
name with more than two definitions), a call through a variable or a string is not found, and a symbol in
a test file is not listed. PR_REVIEW_CONTEXT=on turns it on (default off).

  pr_context.py --selftest      build a small repository in a scratch dir and check the block
"""
import collections
import os
import re
import subprocess
import sys
import tempfile
import time

MAX_SYMBOLS = 12   # changed symbols listed per review
MAX_CALLEES = 12   # called definitions listed per review
MAX_CALLERS = 5    # uses listed per symbol (distinct files first)
MAX_TESTS = 3      # tests listed per symbol and per changed file
TOO_COMMON = 60    # a name with more uses than this is too common to list (a generic word)
MAX_DEFS = 2       # a called name with more definitions than this is ambiguous: not listed
MAX_CHARS = 6000   # the block's size in the prompt
BUDGET_S = 60      # all searches together
SCAN_UP = 400      # lines scanned above a changed line for the definition around it

CODE_GLOBS = ['*.ts', '*.tsx', '*.mts', '*.cts', '*.js', '*.jsx', '*.mjs', '*.cjs', '*.py', '*.go', '*.rb',
              '*.java', '*.kt', '*.cs', '*.rs', '*.php', '*.swift', '*.scala', '*.vue', '*.svelte']
CODE = re.compile(r'\.(' + '|'.join(g[2:] for g in CODE_GLOBS) + r')$')
EXCLUDE = [':(exclude)**/node_modules/**', ':(exclude)**/vendor/**', ':(exclude)**/dist/**',
           ':(exclude)**/build/**', ':(exclude)**/.next/**', ':(exclude)**/coverage/**', ':(exclude)*.min.js']
TEST_PATH = re.compile(r'(^|/)(__tests__|__mocks__|tests?|specs?|e2e)/|\.(test|spec|e2e)\.[a-z]+$|_test\.(go|py)$'
                       r'|(^|/)test_[^/]*\.py$|Tests?\.(java|kt|cs)$')
GENERIC_STEMS = {'index', 'utils', 'util', 'types', 'type', 'route', 'routes', 'page', 'layout', 'constants',
                 'config', 'helpers', 'helper', 'main', 'mod', 'init', '__init__', 'schema', 'models', 'model'}
KEYWORDS = {'if', 'for', 'while', 'switch', 'catch', 'return', 'function', 'await', 'new', 'typeof', 'super',
            'constructor', '__init__', 'else', 'do', 'try', 'with', 'elif', 'except', 'match', 'case', 'not',
            'and', 'or', 'in', 'is', 'lambda', 'yield', 'assert', 'void', 'delete', 'instanceof', 'sizeof'}
# Calls to the language and its usual libraries: never defined in the repository, or too common to help.
BUILTINS = set('''map filter reduce forEach find findIndex some every includes indexOf push pop shift unshift
slice splice concat join split replace replaceAll trim toString toLowerCase toUpperCase startsWith endsWith keys
values entries assign freeze from isArray parse stringify then catch finally resolve reject all allSettled race
log warn error info debug get set has delete add clear Date now toISOString getTime Number String Boolean Object
Array Promise Math floor ceil round max min abs parseInt parseFloat isNaN setTimeout clearTimeout setInterval
require describe it test expect beforeEach afterEach beforeAll afterAll toBe toEqual len print range str int
dict list append extend format items sort sorted sum Error useState useEffect useMemo useCallback useRef
useContext fetch json send status emit on off call apply bind exec padStart toFixed reverse flat flatMap fill
create update upsert count select where insert execute query length trimEnd trimStart charAt substring
encodeURIComponent decodeURIComponent Symbol Map Set WeakMap RegExp isinstance getattr setattr hasattr super
next done toJSON valueOf'''.split())

N = r'[A-Za-z_$][\w$]*'
MODS = r'(?:(?:export|default|declare|abstract|async|public|private|protected|static|readonly|override|pub)\s+)*'
DEFS = [re.compile(p) for p in (
    rf'^\s*{MODS}function\s*\*?\s*({N})',
    rf'^\s*{MODS}(?:class|interface|enum|struct|trait|namespace)\s+({N})',
    rf'^\s*{MODS}type\s+({N})\s*(?:<.*>)?\s*=',
    rf'^\s*{MODS}(?:const|let|var)\s+({N})\s*(?::[^=]+)?=\s*(?:async\s+)?(?:function\b|\(|{N}\s*=>)',
    rf'^\s*(?:async\s+)?def\s+({N})',
    rf'^\s*func\s+(?:\([^)]*\)\s*)?({N})',
    rf'^\s*{MODS}(?:unsafe\s+)?fn\s+({N})',
    # a method in a class or object: `name(args) {` or `async name(args): T {` (a call never ends in `{`)
    rf'^\s+(?:(?:public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*'
    rf'(?:[\w<>\[\],.?]+\s+)?({N})\s*(?:<[^>]*>)?\s*\(.*\)\s*(?::\s*[^{{;=]+)?\{{\s*$',
)]
CALLS = [re.compile(p) for p in (
    rf'(?<![\w$])\.?({N})\s*(?:<[^<>()]*>)?\s*\(',  # foo(...), obj.foo(...), foo<T>(...)
    r'<([A-Z][\w$]*)[\s/>]',                        # a component a TSX/JSX line renders
)]
IMPORT = re.compile(r'^\s*(import\b|export\s*(\{|\*)|from\s+\S+\s+import\b|}\s*from\b|.*\brequire\()|^\s*[\w$]+,?\s*$')
COMMENT = re.compile(r'^\s*(//|/\*|\*|#)')


def def_name(line):
    """The name a line defines, or None."""
    for p in DEFS:
        m = p.match(line)
        if m and m.group(1) not in KEYWORDS:
            return m.group(1)
    return None


def indent(line):
    return len(line) - len(line.lstrip(' \t'))


def enclosing(lines, n):
    """The definition around line n (1-based) of a file: the nearest line above, or n itself, that defines
    something and sits at a lower indent than every line between. Stops at the top level."""
    if n < 1 or n > len(lines) or not lines[n - 1].strip():
        return None
    name = def_name(lines[n - 1])
    if name:
        return name, n
    cur = indent(lines[n - 1])
    for j in range(n - 1, max(0, n - 1 - SCAN_UP), -1):
        line = lines[j - 1]
        if not line.strip() or COMMENT.match(line) or line.lstrip().startswith('@'):
            continue
        ind = indent(line)
        if ind >= cur:
            continue
        name = def_name(line)
        if name:
            return name, j
        if ind == 0:
            return None  # a top-level statement that defines nothing
        cur = ind
    return None


def changes(idx, path):
    """The PR's changes in one file: added line numbers (new side), the new-side positions where lines were
    removed, and the names defined on removed and on added lines."""
    added, cut, removed_defs, added_defs = set(), set(), set(), set()
    for h in idx[path].get('hunk_text') or []:
        m = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)', h[0])
        n = int(m.group(1)) if m else 1
        for raw in h[1:]:
            if raw.startswith('+'):
                added.add(n)
                name = def_name(raw[1:])
                if name:
                    added_defs.add(name)
                n += 1
            elif raw.startswith('-'):
                cut.add(n)
                name = def_name(raw[1:])
                if name:
                    removed_defs.add(name)
            elif raw.startswith('\\'):
                continue
            else:
                n += 1
    return added, cut, removed_defs, added_defs


def git(tree, *args, timeout=30):
    r = subprocess.run(['git', '-C', tree, *args], capture_output=True, text=True, timeout=timeout)
    return r.stdout if r.returncode in (0, 1) else ''


def grep(tree, names):
    """Every line at the head that holds one of `names` as a word, in code files: [(path, line, text)]."""
    if not names:
        return []
    pats = [x for n in names for x in ('-e', n)]
    out = []
    for raw in git(tree, 'grep', '-n', '-I', '-w', '-F', '--no-color', *pats, '--', *CODE_GLOBS, *EXCLUDE).splitlines():
        parts = raw.split(':', 2)
        if len(parts) == 3 and parts[1].isdigit():
            out.append((parts[0], int(parts[1]), parts[2]))
    return out


def symbols(tree, idx):
    """The existing symbols the PR changes outside test files, most telling first: [(name, path, line, how,
    changed lines)], how = 'signature' (its definition line changed), 'removed' (its definition left this
    file) or 'body' (a line inside it changed). Also the names the PR adds (defined only on added lines)."""
    found, new = {}, set()
    rank = {'signature': 0, 'removed': 1, 'body': 2}
    for path in idx:
        if not CODE.search(path):
            continue
        added, cut, removed_defs, added_defs = changes(idx, path)
        new |= added_defs - removed_defs
        if TEST_PATH.search(path):
            continue
        try:
            with open(os.path.join(tree, path), errors='replace') as f:
                lines = f.read().splitlines()
        except OSError:  # a deleted file or a submodule
            lines = []
        for name in removed_defs - added_defs:
            found.setdefault(name, [name, path, 0, 'removed', 0])
        for n in sorted(added | cut):
            got = enclosing(lines, n)
            if not got:
                continue
            name, line = got
            if line in added and name not in removed_defs:
                continue  # code the PR adds: its uses are in the diff
            how = 'signature' if line in added else 'body'
            old = found.get(name)
            if old is None or rank[how] < rank[old[3]]:
                found[name] = old = [name, path, line, how, old[4] if old else 0]
            old[4] += 1
    new -= set(found)
    return sorted((tuple(s) for s in found.values()), key=lambda s: (rank[s[3]], -s[4], s[1], s[2])), new


def called(idx, new):
    """Names the added lines outside test files call or render, with where: {name: [(path, line)]}, most
    called first. Names the PR itself defines are left out (their definition is in the diff)."""
    out = collections.OrderedDict()
    for path in idx:
        if not CODE.search(path) or TEST_PATH.search(path):
            continue
        added = changes(idx, path)[0]
        for n in sorted(added):
            text = idx[path]['text'].get(n, '')
            if COMMENT.match(text) or IMPORT.match(text):
                continue
            for p in CALLS:
                for m in p.finditer(text):
                    name = m.group(1)
                    if len(name) < 4 or name in KEYWORDS or name in BUILTINS or name in new or def_name(text) == name:
                        continue
                    out.setdefault(name, []).append((path, n))
    return sorted(out.items(), key=lambda kv: -len(kv[1]))


def sibling_tests(files, path):
    """Test files that belong to a changed file by name: foo.ts -> foo.test.ts, __tests__/foo.ts, test_foo.py."""
    base = os.path.basename(path)
    stem = base.split('.')[0]
    if stem in GENERIC_STEMS:
        stem = os.path.basename(os.path.dirname(path)) or stem
    if len(stem) < 3:
        return []
    out = []
    for f in files:
        b = os.path.basename(f)
        if f != path and TEST_PATH.search(f) and (b.split('.')[0] in (stem, f'test_{stem}', f'{stem}_test')):
            out.append(f)
    return sorted(out, key=lambda f: (os.path.dirname(f) != os.path.dirname(path), len(f), f))


def place(path, line):
    return f'{path}:{line}'


def listing(hits, limit):
    """Up to `limit` places, one per file first, then the rest, and a count of what is left."""
    first, rest, seen = [], [], set()
    for p, n, *_ in hits:
        (rest if p in seen else first).append((p, n))
        seen.add(p)
    shown = (first + rest)[:limit]
    more = len(hits) - len(shown)
    text = ', '.join(place(p, n) for p, n in shown)
    if more:
        files = len({h[0] for h in hits}) - len({p for p, _ in shown})
        text += f' (+{more} more' + (f' in {files} more files' if files else '') + ')'
    return text


def build(tree, idx, budget_s=BUDGET_S):
    """The REPOSITORY CONTEXT block for the prompt ('' when there is nothing to say) and its counts."""
    t0 = time.monotonic()
    stats = {'symbols': 0, 'callers': 0, 'tests': 0, 'callees': 0, 'too_common': 0, 'ambiguous': 0, 'cut': 0}
    files = [f for f in git(tree, 'ls-files').splitlines() if CODE.search(f)]
    added = {p: changes(idx, p)[0] for p in idx}  # the model reads these lines in the diff already
    syms, new = symbols(tree, idx)
    stats['new'] = len(new)
    used, unused, tested = [], [], {}
    for name, path, line, how, _ in syms:
        if stats['symbols'] >= MAX_SYMBOLS or time.monotonic() - t0 > budget_s:
            stats['cut'] += 1
            continue
        stats['symbols'] += 1
        hits = [(p, n, t) for p, n, t in grep(tree, [name])
                if n not in added.get(p, ()) and def_name(t) != name and not IMPORT.match(t) and not COMMENT.match(t)]
        where = place(path, line) if line else path
        label = {'signature': 'its definition line changed', 'removed': 'its definition was removed from this file',
                 'body': 'changed inside'}[how]
        if len(hits) > TOO_COMMON:
            stats['too_common'] += 1
            used.append(f'- `{name}` at {where} ({label}): {len(hits)} uses, too common a name to list')
            continue
        tests = [h for h in hits if TEST_PATH.search(h[0])]
        callers = [h for h in hits if not TEST_PATH.search(h[0])]
        stats['callers'] += len(callers)
        stats['tests'] += len(tests)
        if tests:
            tested[name] = tests
        if callers:
            used.append(f'- `{name}` at {where} ({label}): used at {listing(callers, MAX_CALLERS)}')
        else:
            unused.append(f'`{name}`')
    defs_of = []
    listed = {s[0] for s in syms}
    calls = [c for c in called(idx, new) if c[0] not in listed][:4 * MAX_CALLEES]
    if calls and time.monotonic() - t0 <= budget_s:
        names = {name for name, _ in calls}
        defs = collections.defaultdict(list)
        for p, n, t in grep(tree, sorted(names)):
            if n not in added.get(p, ()) and def_name(t) in names:
                defs[def_name(t)].append((p, n))
        for name, sites in calls:
            got = defs.get(name) or []
            if not got or all(TEST_PATH.search(p) for p, _ in got):
                continue  # not defined in the repository (a library) or only in tests
            if len(got) > MAX_DEFS:
                stats['ambiguous'] += 1
                continue
            if stats['callees'] >= MAX_CALLEES:
                stats['cut'] += 1
                continue
            stats['callees'] += 1
            at = ' and '.join(place(p, n) for p, n in got)
            times = f'{len(sites)} times' if len(sites) > 1 else 'once'
            defs_of.append(f'- `{name}`, called {times} (first at {place(*sites[0])}): defined at {at}')
    nearby, untested = [], []
    for path in idx:
        if not CODE.search(path) or TEST_PATH.search(path) or not os.path.exists(os.path.join(tree, path)):
            continue
        t = sibling_tests(files, path)
        if t:
            nearby.append(f"{path}: {', '.join(t[:MAX_TESTS])}" + (f' (+{len(t) - MAX_TESTS} more)' if len(t) > MAX_TESTS else ''))
        else:
            untested.append(path)
    stats['test_files'] = len(nearby)
    parts = []
    if used or unused:
        parts.append('Existing code this PR changes, and where the code outside the diff uses it:\n'
                     + '\n'.join(used + ([f"- No use outside the diff: {', '.join(unused)}"] if unused else [])))
    if defs_of:
        parts.append('Existing code the new lines call or render, and where it is defined:\n' + '\n'.join(defs_of))
    tlines = [f'- `{name}`: {listing(t, MAX_TESTS)}' for name, t in tested.items()]
    if nearby:
        tlines.append('- Test files named after the changed files: ' + '; '.join(nearby))
    if untested:
        tlines.append(f"- No test file named after: {', '.join(untested[:10])}"
                      + (f' (+{len(untested) - 10} more)' if len(untested) > 10 else ''))
    if tlines:
        parts.append('Tests:\n' + '\n'.join(tlines))
    stats['secs'] = round(time.monotonic() - t0, 1)
    if not parts:
        return '', stats
    text = '\n\n'.join(parts)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rsplit('\n', 1)[0] + '\n- (cut: the list is longer)'
    return ('REPOSITORY CONTEXT (a word search at the PR head, not findings; open these in step 1):\n\n' + text), stats


def selftest():
    import pr_format
    d = tempfile.mkdtemp(prefix='pr-context-selftest-')
    run = lambda *a: subprocess.run(['git', '-C', d, *a], capture_output=True, text=True, check=True)
    run('init', '-q')
    run('config', 'user.email', 'test@example.com')
    run('config', 'user.name', 'test')
    os.makedirs(os.path.join(d, 'src', '__tests__'))
    src = {
        'src/fees.ts': ('export function lateFee(days: number): number {\n  if (days <= 0) {\n    return 0;\n  }\n'
                        '  return days * 2;\n}\n\nexport const waive = (fee: number) => {\n  return 0;\n};\n\n'
                        'export function oldRate() {\n  return 1;\n}\n'),
        'src/invoice.ts': ("import { lateFee, waive } from './fees';\n\nexport class Invoice {\n"
                           '  total(days: number): number {\n    return 10 + lateFee(days);\n  }\n'
                           '  free(): number {\n    return waive(oldRate());\n  }\n}\n'),
        'src/__tests__/fees.test.ts': ("import { lateFee } from '../fees';\n\ntest('fee', () => {\n"
                                       '  expect(lateFee(3)).toBe(6);\n});\n'),
        'src/report.py': 'def summary(rows):\n    total = 0\n    for r in rows:\n        total += r\n    return total\n',
        'src/rounding.ts': 'export function roundCents(x: number): number {\n  return Math.round(x * 100) / 100;\n}\n',
    }
    for p, t in src.items():
        with open(os.path.join(d, p), 'w') as f:
            f.write(t)
    run('add', '.')
    run('commit', '-qm', 'base')
    # lateFee's body changes, waive's definition line changes, oldRate goes, and a new function calls roundCents
    fees = (src['src/fees.ts'].replace('return days * 2;', 'return days * 3;')
            .replace('(fee: number) =>', '(fee: number, why: string) =>').split('\nexport function oldRate')[0]
            + '\nexport function feeWithTax(days: number): number {\n  return roundCents(lateFee(days) * 1.2);\n}\n')
    with open(os.path.join(d, 'src/fees.ts'), 'w') as f:
        f.write(fees)
    with open(os.path.join(d, 'src/report.py'), 'w') as f:
        f.write(src['src/report.py'].replace('total += r', 'total += r * 2'))
    diff = run('diff').stdout
    block, stats = build(d, pr_format.diff_index(diff))
    want = ['`waive` at src/fees.ts:8 (its definition line changed): used at src/invoice.ts:8',
            '`oldRate` at src/fees.ts (its definition was removed from this file): used at src/invoice.ts:8',
            '`lateFee` at src/fees.ts:1 (changed inside): used at src/invoice.ts:5',
            'No use outside the diff: `summary`',
            '`roundCents`, called once (first at src/fees.ts:13): defined at src/rounding.ts:1',
            '`lateFee`: src/__tests__/fees.test.ts:4',
            'Test files named after the changed files: src/fees.ts: src/__tests__/fees.test.ts',
            'No test file named after: src/report.py']
    bad = [w for w in want if w not in block]
    order = [block.find(w) for w in want[:3]]
    ok = (not bad and order == sorted(order) and stats['symbols'] == 4 and stats['new'] == 1
          and 'feeWithTax' not in block)
    print('selftest: ok' if ok else 'selftest: FAILED')
    if not ok or os.environ.get('PR_CONTEXT_SHOW'):
        print(block)
        print('missing:', bad, 'stats:', stats)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    if sys.argv[1:] == ['--selftest']:
        sys.exit(selftest())
    print(__doc__)
