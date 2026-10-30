#!/usr/bin/env python3
"""PR review run (README.md, "Review"): one queued open PR per run, reviewed by a headless model that can
only read; this script renders, gates and delivers the result.

  pr_review.py                  CI-only questions (no model), then the next queued PR within the cap
  pr_review.py --pr REPO#N      review this PR now (queued or not; counts against the cap)
  pr_review.py --dry-run [--pr REPO#N]
                                pick, prepare and build the prompt; run no model, deliver nothing

Delivery (Alex, 2026-10-29 20:45Z and 21:01Z): every reviewed open PR gets ONE SUBMITTED review on GitHub
under the reviewer's login (R0, R24), never a pending one: event APPROVE for a low-risk PR that meets the
bar (R27, PR_REVIEW_APPROVE=on), else COMMENT (REQUEST_CHANGES is not authorized). Its first line says it
is automated and not yet read by the reviewer, line 2 says who acts (R22); the items are inline comments
on their exact lines. Slack gets the outcome word and that review's own URL (R17), once per PR head (R18).
Right before posting the script re-checks that the PR is still open and on the reviewed head (R15). A
review passes the zero-token gate, then the reader test (R26, pr_judge.py). The model runs in
a git worktree at the PR head with Read, Grep and Glob only, no hooks, no MCP, no settings from the
repository, reads outside the worktree denied, no GitHub or Slack credential, LOOMTRACER_INGEST_DISABLE=1,
and returns decisions for the reviewer (R16) and line-anchored items for the author as JSON. This script
derives the outcome, renders it (assets/style-guide.md) and gates it (pr_format.py): a review that fails
is regenerated once with the gate's reasons, then held. What it prints and keeps in the runtime is ids,
links, outcomes, counts, gate reasons and cost: never the repository's text.

  pr_review.py --redeliver REPO#N   deliver the stored result for the PR's current head again (no model)
  pr_review.py --no-deliver --pr REPO#N
                                    the whole review (model, gate, reader test) without posting or recording
                                    anything; prints counts, rule ids and scores
  pr_review.py --rereview REPO#N    review the current head again although it was reviewed (a rule change):
                                    the earlier review is updated in place when it can be (same event, no
                                    inline comments), else it is cut to a pointer to the new one; its Slack
                                    message is updated in place
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_digest  # noqa: E402
import pr_format  # noqa: E402
import pr_judge  # noqa: E402
import pr_money  # noqa: E402
import pr_rules  # noqa: E402
import pr_state  # noqa: E402

HERE = pr_state.HERE
UPSTREAM = os.path.join(HERE, '..', 'upstream', 'security-review.md')
# gc-sy1hm.2: the security-only upstream prompt says 'This is not a general code review'; the correctness
# prompt looks for the defects a PR ships. PR_REVIEW_PROMPT picks one; the benchmark compares them.
PROMPTS = {'security': UPSTREAM, 'correctness': os.path.join(HERE, '..', 'correctness-review.md')}
STYLE = os.path.join(HERE, '..', 'style-guide.md')
MAX_DIFF_CHARS = 350_000
# The footer names the person (default: the login); PR_REVIEW_NAMES maps authors' logins to names.
NAME = os.environ.get('PR_REVIEW_REVIEWER_NAME') or os.environ.get('PR_REVIEW_REVIEWER') or 'the reviewer'
NAMES = dict(x.split('=', 1) for x in os.environ.get('PR_REVIEW_NAMES', '').split() if '=' in x)

_TEXT = {'type': 'string'}
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['decisions', 'items', 'cleared', 'not_checked_lines', 'summary', 'skip', 'checks'],
    'properties': {
        'decisions': {'type': 'array', 'maxItems': 3, 'description': (
            'Only decisions the reviewer alone can make: a product, risk or compliance trade-off, never a code '
            'fix the author can make. Empty when there is none.'), 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['question', 'options', 'recommended', 'reason', 'why', 'area', 'rule', 'link', 'verified',
                         'confidence'],
            'properties': {
                'question': {'type': 'string', 'maxLength': 280, 'description': 'One question ending in "?", at most 280 characters.'},
                'options': {'type': 'array', 'minItems': 2, 'maxItems': 4, 'items': _TEXT},
                'recommended': {'type': 'integer', 'minimum': 0, 'maximum': 3,
                                'description': 'The index of the option you recommend (0 = the first).'},
                'reason': {'type': 'string', 'maxLength': 200, 'description': 'One short sentence: why that option.'},
                'rule': {'type': 'string', 'description': 'The id of the repository rule this is about (e.g. "D3"), or "" when none is.'},
                'why': {'type': 'string', 'maxLength': 280, 'description': 'One sentence, at most 280 characters: why only the reviewer can decide this.'},
                'area': {'type': 'string', 'enum': pr_format.AREAS},
                'link': {'type': 'object', 'additionalProperties': False, 'required': ['kind'], 'properties': {
                    'kind': {'type': 'string', 'enum': ['code', 'description', 'ticket'],
                             'description': 'code = lines of a file at the PR head; description = the PR text; '
                                            'ticket = a URL that appears in the PR description.'},
                    'path': _TEXT, 'line_start': {'type': 'integer', 'minimum': 1},
                    'line_end': {'type': 'integer', 'minimum': 1}, 'url': _TEXT}},
                'verified': {'type': 'string', 'description': 'The files you opened to confirm it.'},
                'confidence': {'type': 'integer', 'minimum': 1, 'maximum': 10}}}},
        'items': {'type': 'array', 'maxItems': 5, 'description': 'For the author, worst first.', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['path', 'side', 'line_start', 'line_end', 'kind', 'severity', 'area', 'change', 'why', 'ask',
                         'rule', 'verified', 'confidence'],
            'properties': {
                'path': {'type': 'string', 'description': 'File path as in the diff.'},
                'side': {'type': 'string', 'enum': ['new', 'old'],
                         'description': 'new = lines of the PR head; old = removed lines (base side).'},
                'line_start': {'type': 'integer', 'minimum': 1},
                'line_end': {'type': 'integer', 'minimum': 1},
                'kind': {'type': 'string', 'enum': ['question', 'change'],
                         'description': 'question = ask the author; change = the author must fix.'},
                'severity': {'type': 'string', 'enum': ['high', 'medium', 'low']},
                'area': {'type': 'string', 'enum': pr_format.AREAS},
                'change': {'type': 'string', 'maxLength': 60, 'description': (
                    '3-6 plain words: what this PR changed on these lines, e.g. "Reminder text names the patient". '
                    'Never a category label.')},
                'why': {'type': 'string', 'maxLength': 200, 'description': 'The one fact that makes it matter, at most 25 words.'},
                'ask': {'type': 'string', 'maxLength': 200, 'description': 'The one change, or the one question, at most 25 words.'},
                'rule': {'type': 'string', 'description': 'The id of the repository rule this is about (e.g. "D3"), or "" when none is.'},
                'verified': {'type': 'string', 'description': 'The files you opened to confirm it.'},
                'confidence': {'type': 'integer', 'minimum': 1, 'maximum': 10}}}},
        'cleared': {'type': 'array', 'maxItems': 12, 'description': (
            'One entry per FLAGGED HUNK that needs no decision and no item: why it is safe.'), 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['flag', 'path', 'line_start', 'line_end', 'change', 'why', 'rule'],
            'properties': {
                'flag': {'type': 'string', 'description': 'The flag id from the FLAGGED HUNKS list, e.g. "F1".'},
                'path': {'type': 'string', 'description': "The flag's file."},
                'line_start': {'type': 'integer', 'minimum': 1,
                               'description': "New-side line inside the flag's hunk where the flagged code is."},
                'line_end': {'type': 'integer', 'minimum': 1},
                'change': {'type': 'string', 'maxLength': 60, 'description': (
                    '3-6 plain words: what this PR changed there, e.g. "Log line prints the visit id". Never a '
                    'category label.')},
                'why': {'type': 'string', 'maxLength': 200, 'description': (
                    'The one fact that makes it safe, at most 25 words, e.g. "Only the visit id is logged, no '
                    'patient field." A possible risk needs the reason it is acceptable or intended.')},
                'rule': {'type': 'string', 'description': 'The id of the repository rule this is about (e.g. "D3"), or "" when none is.'}}}},
        'not_checked_lines': {'type': 'array', 'maxItems': 10, 'description': (
            'New-side lines of the diff that change billing, plans, credits or pricing (not checked by this '
            'review). Empty when there are none.'), 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['path', 'line'],
            'properties': {'path': _TEXT, 'line': {'type': 'integer', 'minimum': 1}}}},
        'summary': {'type': 'string', 'maxLength': 140, 'description': (
            'One short, concrete sentence of at most 15 words: what this PR does, for someone who does not know '
            'the code.')},
        'skip': {'type': 'array', 'maxItems': 6, 'description': (
            'The rest of the diff that needs no look, by group. Never a change in product behaviour: that is a '
            'cleared entry (intended, default safe) or a decision.'), 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['group', 'paths'],
            'properties': {
                'group': {'type': 'string', 'maxLength': 30, 'description': '1-3 plain words, e.g. "tests", "types", "UI copy".'},
                'paths': {'type': 'array', 'maxItems': 40, 'items': _TEXT, 'description': 'File paths as in the diff.'}}}},
        'checks': {'type': 'object', 'additionalProperties': False,
                   'required': ['description_matches_diff', 'behaviour_changes_have_tests',
                                'migrations_reversible_and_ordered'],
                   'properties': {
                       'description_matches_diff': {'type': 'string', 'enum': ['yes', 'partly', 'no']},
                       'behaviour_changes_have_tests': {'type': 'string', 'enum': ['yes', 'partly', 'no', 'n/a']},
                       'migrations_reversible_and_ordered': {'type': 'string', 'enum': ['yes', 'no', 'n/a']}}},
    },
}

# Reads the model may not make (deny beats allow): everything that holds credentials or other people's
# data. The worktree and the context dir live under PR_REVIEW_WORK_DIR (default /var/tmp/...).
DENY_ROOTS = ['//home/**', '//root/**', '//etc/**', '//proc/**', '//sys/**', '//run/**', '//data/**', '//tmp/**',
              '//mnt/**', '//srv/**', '//opt/**', '//var/lib/**', '//var/log/**', '//var/backups/**',
              '//var/mail/**', '//var/spool/**', '//usr/local/etc/**']

FINAL = """START ANALYSIS:

This review runs headless with read-only tools (Read, Grep, Glob) and no sub-tasks; your working
directory is the repository at the PR head. Two people read it: {name}, the harness engineer, who does
not know this codebase, and {author}, who wrote the change and has reviewed it. Its job is to check the
PR for CONSISTENCY WITH THE REPOSITORY'S GOALS AND RULES, as THE REPOSITORY'S RULES above write them
down, and for the vulnerabilities and issues the scan instructions ask about. Do these steps yourself,
in order:

1. Open every FLAGGED HUNK listed above (F1, F2, ...) in its file, and open its callers and the code it
   calls (Grep for the changed functions and types). Do not write an entry about code you have not opened;
   name what you opened in `verified`.
2. Read the rest of the diff the same way, with the repository's rules at hand. Find where the PR goes
   against a rule or a goal, and every defect and issue the instructions above and the custom scan
   instructions ask about. Apply their filtering rules, give each candidate a confidence from 1 to 10,
   and drop anything below 8.
3. Sort what is left into these lists:
   - `decisions`: only what {name} alone can decide, at the level of goals and rules, never of code. For
     example: "Rule D3 says X; this PR does Y. Accept it as an exception, ask {author} to change it, or
     update the rule?" A product, risk or compliance trade-off (whether patient data may go to a vendor,
     whether a destructive step may run in production) is one too. Each is ONE question ending in "?",
     with 2 to 4 options, `recommended` (the index of the option you recommend) and `reason` (one short
     sentence on why), `why` (why only {name} can decide it), `rule`, and a link to where it lives: lines
     of a file at the head ("code"), the PR description ("description"), or a ticket URL that appears in
     the description ("ticket"). Never ask {name} to read, review or check code. A code fix the author can
     make is never a decision.
   - `items` for {author}: a "change" they must make or a "question" they must answer, each anchored to
     lines inside one hunk of the diff (`side` "new" for the PR head, "old" for removed lines). `change`
     is 3-6 plain words on what the PR changed there; `why` is the one fact that makes it matter and `ask`
     the one thing to do, at most 25 words each. Say each finding once: never two items on the same lines.
   - `cleared`: EVERY flagged hunk that is not covered by a decision or an item gets one entry: its flag
     id, its file, a new-side line range inside that hunk on the CODE that makes it safe (not a comment),
     `change` (3-6 plain words on what the PR changed, never a category label) and `why` (the one fact
     that makes it safe, at most 25 words). "Consistent with rule D2" is such a fact when the rule covers
     it. A behaviour that could be a risk is cleared only when `why` says why it is acceptable or
     intended; otherwise it is a question item for {author}. A flagged hunk with no entry anywhere fails
     the review.
   - `rule` on every decision, item and cleared entry: the id of the repository rule it is about, or "".
     A finding in an area where the repository has a written rule must cite it. Judge patient data,
     security and compliance against the repository's own rules; where it has none, say so in `why`.
   - `not_checked_lines`: new-side lines that change {not_checked}. This review does NOT check that area:
     never write decisions, items or cleared entries about it; only list where it is.
4. Keep at most 3 decisions and 5 items, worst first. Write `summary`, one sentence on what the PR does,
   and `skip`, the files that need no look, by group. A change in product behaviour never goes in `skip`:
   it is a cleared entry that says it is intended and its default is safe, or a decision. `change` is
   plain words about the line, never an identifier (no function, variable or type names).

Your answer is the JSON object of the schema you were given; it replaces the markdown output format
described above. You do not pick the outcome: the script derives it (a decision means {name} is needed;
a change means changes are requested; only questions means questions for {author}; nothing means low
risk). Follow the STYLE GUIDE above to the letter: plain words, no filler, no rule codes or flag ids in
any text (cite a repository rule only through `rule`), no praise, and no link to any site but github.com
(write the sentence without it). Never copy secrets, tokens or personal data into your answer; point at
them by path and line instead.
"""


def git(mirror, *args, timeout=600, check=True):
    env = dict(os.environ, GIT_LFS_SKIP_SMUDGE='1', GIT_TERMINAL_PROMPT='0')
    r = subprocess.run(['git', '-C', mirror, *args], capture_output=True, text=True, timeout=timeout, env=env)
    if check and r.returncode != 0:
        raise RuntimeError(f'git {args[0]} failed (rc {r.returncode})')
    return r.stdout


def prepare(cfg, repo, pr, run_id):
    """A worktree at the PR head and the diff against where the PR forked from its base. Returns
    (tree, ctx, diff, files_list, commits, base_sha)."""
    owner, name = repo.split('/')
    os.makedirs(cfg['work'], mode=0o700, exist_ok=True)
    os.chmod(cfg['work'], 0o700)  # it holds the repositories' code
    mirror = os.path.join(cfg['work'], 'mirrors', f'{owner}__{name}.git')
    if not os.path.isdir(mirror):
        os.makedirs(os.path.dirname(mirror), exist_ok=True)
        r = subprocess.run(['git', 'clone', '--quiet', '--bare', '--filter=blob:none',
                            f'https://github.com/{repo}.git', mirror], capture_output=True, text=True, timeout=1800)
        if r.returncode != 0:
            raise RuntimeError(f'git clone failed (rc {r.returncode})')
    n, base_ref, head = pr['number'], pr['baseRefName'], pr['headRefOid']
    git(mirror, 'fetch', '--quiet', 'origin', f'+refs/heads/{base_ref}:refs/heads/{base_ref}',
        f'+refs/pull/{n}/head:refs/pr/{n}')
    base = pr_rules.base_for(pr, {base_ref: git(mirror, 'rev-parse', f'refs/heads/{base_ref}').strip()})
    fork = git(mirror, 'merge-base', base, head).strip()
    run = os.path.join(cfg['work'], 'runs', run_id)
    tree, ctx = os.path.join(run, 'tree'), os.path.join(run, 'ctx')
    if os.path.exists(run):  # left by a run that died
        cleanup(cfg, mirror, run_id)
    os.makedirs(ctx, mode=0o700, exist_ok=True)
    git(mirror, 'worktree', 'add', '--detach', '--force', tree, head, timeout=1800)
    diff = git(mirror, 'diff', '--no-ext-diff', '--no-textconv', fork, head)
    with open(os.path.join(ctx, 'pr.diff'), 'w') as f:
        f.write(diff)
    files = git(mirror, 'diff', '--no-ext-diff', '--name-status', fork, head)
    commits = git(mirror, 'log', '--no-decorate', '--format=%h %s', f'{fork}..{head}')
    return mirror, tree, ctx, diff, files, commits, fork


def conflict_files(mirror, pr):
    """The files that conflict with the base branch (git merge-tree in the mirror, no tokens), so the
    conflict blocker names and links them. [] when GitHub reports no conflict or git cannot tell."""
    if pr.get('mergeable') != 'CONFLICTING':
        return []
    out = git(mirror, 'merge-tree', '--write-tree', '--name-only', '--no-messages',
              f"refs/heads/{pr['baseRefName']}", pr['headRefOid'], check=False)
    names = []
    for line in out.splitlines()[1:]:
        if not line.strip():
            break
        names.append(line.strip())
    return sorted(set(names))


def cleanup(cfg, mirror, run_id):
    run = os.path.join(cfg['work'], 'runs', run_id)
    if mirror and os.path.isdir(os.path.join(run, 'tree')):
        git(mirror, 'worktree', 'remove', '--force', os.path.join(run, 'tree'), check=False)
    shutil.rmtree(run, ignore_errors=True)
    if mirror:
        git(mirror, 'worktree', 'prune', check=False)


def build_prompt(cfg, pr, diff, files, commits, fork, ctx, flagged, not_checked, digest_block='', author='the author'):
    text = open(PROMPTS.get(cfg.get('prompt'), UPSTREAM)).read()
    text = re.sub(r'\A---\n.*?\n---\n', '', text, flags=re.S)  # the slash command's frontmatter
    status = (f"Pull request {pr['url']}, head {pr['headRefOid']}, forked from the base branch at {fork}. Open.\n"
              f"Title: {pr.get('title') or ''}\n\nDescription:\n{(pr.get('body') or '').strip() or '(empty)'}")
    if len(diff) > MAX_DIFF_CHARS:
        diff_block = (f"(The diff is {len(diff)} characters, too long to inline. Read it from {ctx}/pr.diff "
                      f"in parts; review all of it.)")
    else:
        diff_block = diff
    swaps = [('!`git status`', status), ('!`git diff --name-only origin/HEAD...`', files),
             ('!`git log --no-decorate origin/HEAD...`', commits), ('!`git diff --merge-base origin/HEAD`', diff_block)]
    for old, new in swaps:
        if old not in text:
            raise RuntimeError(f'upstream prompt changed: {old!r} missing')
        text = text.replace(old, new.replace('```', "\'\'\'"), 1)
    head, sep, _ = text.partition('START ANALYSIS:')
    if not sep:
        raise RuntimeError('upstream prompt changed: START ANALYSIS missing')
    checklist = open(cfg['checklist']).read()
    style = open(STYLE).read()
    idx = pr_format.diff_index(diff)
    hunks = '\n'.join(f"- {f['id']}: {f['path']} line {f['line']} (new side), hunk {hunk_span(idx, f)}: {f['label']}"
                      for f in flagged) or '- none'
    final = FINAL.replace('{not_checked}', not_checked or 'billing, plans, credits or pricing')
    final = final.replace('{name}', NAME).replace('{author}', author)
    return (f'{head}{checklist}\n\nSTYLE GUIDE (how your answer will be rendered and checked):\n\n{style}\n\n'
            f'{digest_block}\n\n'
            f'FLAGGED HUNKS (pattern matches on the diff, not findings; open each one, step 1; each needs a '
            f'decision, an item or a cleared entry, step 3):\n'
            f'{hunks}\n\nVALID ITEM RANGES (an item\'s line_start and line_end must both fall inside ONE of these '
            f'ranges on its side, or the review is rejected):\n{hunk_ranges(diff)}\n\n'
            f"{final}")


def hunk_span(idx, flag):
    """The new-side line range of the hunk a flag sits in, for the model's cleared entry."""
    k = pr_format.hunk_of(idx, flag['path'], flag['line'])
    if k is None:
        return 'unknown'
    h = idx[flag['path']]['hunks'][k][0]
    return f'{min(h)}-{max(h)}'


def hunk_ranges(diff, max_files=80):
    """Each file's hunks as line ranges per side, so the model anchors items inside the diff (R3)."""
    out = []
    for path, f in list(pr_format.diff_index(diff).items())[:max_files]:
        new = [f'{min(h[0])}-{max(h[0])}' for h in f['hunks'] if h[0]]
        old = [f'{min(h[1])}-{max(h[1])}' for h in f['hunks'] if h[1]]
        out.append(f"- {path}: new {', '.join(new) or 'none'}; old {', '.join(old) or 'none'}")
    return '\n'.join(out) or '- none'


def claude_bin():
    """PR_REVIEW_CLAUDE, else claude on PATH, else ~/.local/bin/claude (an order's PATH can be short)."""
    return (os.environ.get('PR_REVIEW_CLAUDE') or shutil.which('claude')
            or os.path.expanduser('~/.local/bin/claude'))


def clean(text):
    """Model text bound for GitHub: no @-mentions (they notify people), no images, no HTML and no links
    to other hosts (an image or link in a review is fetched when someone opens it, which could carry
    the repository's code elsewhere)."""
    text = re.sub(r'!\[[^\]]*\]\([^)]*\)', '[image removed]', text)
    text = re.sub(r'<[^>]{1,200}>', '', text)
    text = re.sub(r'https?://(?!github\.com/)[^\s)\]]+', '[link removed]', text)
    return re.sub(r'@(?=[A-Za-z0-9])', '@\u200b', text)


def run_model(cfg, tree, ctx, prompt):
    """The headless review. Returns (structured output or None, meta with cost and counts)."""
    settings = {'disableAllHooks': True,
                'permissions': {'deny': [f'{t}({p})' for p in DENY_ROOTS for t in ('Read', 'Grep', 'Glob')]}}
    keep = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'USER', 'LOGNAME', 'TERM', 'TZ')
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(LOOMTRACER_INGEST_DISABLE='1', HIVEMIND_INGEST_DISABLE='1')
    cmd = [claude_bin(), '-p', '--output-format', 'json', '--json-schema', json.dumps(SCHEMA), '--model', cfg['model'],
           '--no-session-persistence', '--setting-sources', '', '--settings', json.dumps(settings),
           '--strict-mcp-config', '--tools', 'Read,Grep,Glob', '--add-dir', ctx,
           '--max-budget-usd', str(cfg['max_usd'])]
    if cfg.get('effort'):
        cmd += ['--effort', cfg['effort']]
    t0 = pr_state.now()
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=tree, env=env,
                           timeout=cfg['timeout_s'])
    except subprocess.TimeoutExpired:
        return None, {'error': 'timeout', 'secs': cfg['timeout_s']}
    meta = {'secs': round((pr_state.now() - t0).total_seconds()), 'rc': r.returncode}
    try:
        out = json.loads(r.stdout)
    except ValueError:
        return None, {**meta, 'error': 'no-json'}
    meta.update(cost_usd=out.get('total_cost_usd') or 0, turns=out.get('num_turns'), subtype=out.get('subtype'),
                denials=len(out.get('permission_denials') or []))
    so = out.get('structured_output')
    if out.get('is_error') or not isinstance(so, dict):
        return None, {**meta, 'error': out.get('subtype') or 'no-structured-output'}
    so['summary'] = clean(so['summary'])
    for g in so['skip']:
        g['group'] = clean(g['group'])
    for c in so['cleared']:
        for k in ('change', 'why'):
            c[k] = clean(c[k])
    for it in so['items']:
        for k in ('change', 'why', 'ask', 'verified'):
            it[k] = clean(it[k])
    for d in so['decisions']:
        for k in ('question', 'reason', 'why', 'verified'):
            d[k] = clean(d[k])
        d['options'] = [clean(o) for o in d['options']]
    return so, meta


def known_secrets():
    """Credentials on this host, to make sure none ends up in a review (exact match)."""
    vals = set()
    r = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        vals.add(r.stdout.strip())
    paths = [os.path.expanduser(p) for p in ('~/.config/gc/slack.env', '~/.config/gc/slack2.env',
                                             '~/.claude/.credentials.json')]
    tokdir = os.path.expanduser('~/.config/loomtracer/tokens')
    if os.path.isdir(tokdir):
        paths += [os.path.join(tokdir, f) for f in os.listdir(tokdir)]
    for p in paths:
        try:
            vals.update(t for t in re.findall(r'[A-Za-z0-9_\-./+=]{20,}', open(p).read()))
        except OSError:
            pass
    return {v for v in vals if len(v) >= 20}


SECRET_SHAPES = [re.compile(p) for p in (r'AKIA[0-9A-Z]{16}', r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
                                         r'\bxox[abposr]-[A-Za-z0-9-]{10,}', r'\bgh[pousr]_[A-Za-z0-9]{20,}',
                                         r'\bsk-(ant-)?[A-Za-z0-9_-]{20,}', r'\b\d{3}-\d{2}-\d{4}\b')]


def leaks(texts):
    known = known_secrets()
    blob = '\n'.join(texts)
    return any(k in blob for k in known) or any(p.search(blob) for p in SECRET_SHAPES)


def body_sha(body):
    """Hash of a review body (line endings and outer space normalized)."""
    return hashlib.sha256(body.replace('\r\n', '\n').strip().encode()).hexdigest()


def tree_lines(tree, paths):
    """{path: line count} at the head for the files decisions link to (a missing file is left out)."""
    out = {}
    for p in paths:
        f = os.path.normpath(os.path.join(tree, p))
        if f.startswith(os.path.normpath(tree) + os.sep) and os.path.isfile(f):
            with open(f, 'rb') as fh:
                out[p] = fh.read().count(b'\n') + 1
    return out


def post_review(repo, pr, body, comments, event='COMMENT'):
    """ONE SUBMITTED review (never pending, R0; one per head, R24), its items as inline comments. There is
    no fallback without the inline comments: a review without its markers is not delivered (R4, R21).
    Returns (status, review url, review state, review id)."""
    payload = {'commit_id': pr['headRefOid'], 'body': body, 'event': event}
    if comments:
        payload['comments'] = comments
    r = subprocess.run(['gh', 'api', '-X', 'POST', f"repos/{repo}/pulls/{pr['number']}/reviews", '--input', '-'],
                       input=json.dumps(payload), capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        out = json.loads(r.stdout)
        return 'ok', out.get('html_url'), out.get('state'), out.get('id')
    code = re.search(r'HTTP (\d{3})', r.stderr)
    return f"error:{code.group(1) if code else r.returncode}", None, None, None


def update_review(repo, number, review_id, body):
    """Replace the body of a submitted review (its event and inline comments stay). Same return shape."""
    r = subprocess.run(['gh', 'api', '-X', 'PUT', f'repos/{repo}/pulls/{number}/reviews/{review_id}', '--input', '-'],
                       input=json.dumps({'body': body}), capture_output=True, text=True, timeout=120)
    if r.returncode == 0:
        out = json.loads(r.stdout)
        return 'updated', out.get('html_url'), out.get('state'), out.get('id')
    code = re.search(r'HTTP (\d{3})', r.stderr)
    return f"error:{code.group(1) if code else r.returncode}", None, None, None


def anchored_lines(repo, number, review_id):
    """The `line` GitHub reports for each inline comment of a review (None = not anchored), read from the
    PR's comment list (the per-review list never reports `line`)."""
    r = subprocess.run(['gh', 'api', '--paginate', f'repos/{repo}/pulls/{number}/comments?per_page=100', '--jq',
                        f'.[] | select(.pull_request_review_id == {int(review_id)}) | .line'],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None
    return [None if l.strip() in ('', 'null') else int(l) for l in r.stdout.splitlines()]


def ours_on_head(repo, number, head):
    """R24: how many automated reviews of this head are whole reviews (not pointers to a newer one)."""
    jq = f'.[] | select(.commit_id == "{head}") | [({OURS_JQ}), (.body | contains("{POINTER}"))]'
    r = subprocess.run(['gh', 'api', '--paginate', f'repos/{repo}/pulls/{number}/reviews?per_page=100', '--jq', jq],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None
    rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    return sum(1 for ours, pointer in rows if ours and not pointer)


POINTER = 'Replaced by [the new review of this commit]'
OURS_JQ = pr_format.ours_jq()
DISMISSAL = 'Replaced by a newer automated review.'


def dismiss_stale(repo, number, keep_id, reviewer):
    """R35, R31: our newest review is our only live verdict on a PR. A later COMMENT review would leave an
    earlier REQUEST_CHANGES blocking, or an earlier APPROVE standing after the review found a question, so
    every newer review dismisses our earlier ones in those states. Only automated reviews (their body
    begins the way ours do); never one the reviewer wrote. Returns (dismissed, failed)."""
    jq = (f'.[] | select((.state == "CHANGES_REQUESTED" or .state == "APPROVED") and .id != {int(keep_id)} and '
          f'(.user.login | ascii_downcase) == "{reviewer.lower()}" and ({OURS_JQ})) | .id')
    r = subprocess.run(['gh', 'api', '--paginate', f'repos/{repo}/pulls/{number}/reviews?per_page=100', '--jq', jq],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return 0, 1
    ok = bad = 0
    for rid in (l.strip() for l in r.stdout.splitlines() if l.strip()):
        d = subprocess.run(['gh', 'api', '-X', 'PUT', f'repos/{repo}/pulls/{number}/reviews/{rid}/dismissals',
                            '-f', f'message={DISMISSAL}', '-f', 'event=DISMISS'],
                           capture_output=True, text=True, timeout=60)
        ok, bad = ok + (d.returncode == 0), bad + (d.returncode != 0)
    return ok, bad


def pointer_body(name, new_url):
    """What an earlier review of the same head becomes when a new one replaces it (R24)."""
    return f"**Replaced**{pr_format.SUFFIX.format(name=name)}\n\n{POINTER}({new_url})."


def block_kind(rv):
    """What a formal block rests on: 'mechanical' (only a merge conflict or CI) the sweep lifts once the
    cause is gone; 'content' (an item or a decision) needs a review of the new head."""
    if not rv['items'] and not rv['decisions'] and rv['blockers'] and \
            all(b.get('cause') in ('conflict', 'ci') for b in rv['blockers']):
        return 'mechanical'
    return 'content'


def deliver(cfg, key, entry, pr, rv, body, comments, result, extra, flagged=(), idx=None, rereview=False):
    """R15, R18, R27, R0, R17, R21: re-check that the PR is open and on the reviewed head, and that this
    head was not reviewed yet (unless `rereview`); re-check the approval bar; post ONE review (APPROVE or
    COMMENT), delete it at once if GitHub made it pending, check its inline comments are anchored, and post
    '<word>: <review URL>' to Slack. A re-review updates the earlier review in place when it can (same
    event, no inline comments), else replaces it with a pointer; its Slack message is updated in place."""
    repo, n, head = entry['repo'], entry['number'], pr['headRefOid']
    event = pr_format.event_of(rv)
    if cfg.get('no_deliver'):
        finish(cfg, key, entry, rv['outcome'], {**extra, 'event': event, 'inline': len(comments), 'body_chars': len(body)})
        return
    fresh = pr_rules.get_pr(repo, n)
    if fresh['state'] != 'OPEN':
        finish(cfg, key, dict(entry, head=head), 'closed', {**extra, 'gate': ['R15']})
        return
    if fresh['headRefOid'] != head:
        finish(cfg, key, entry, 'requeued', {**extra, 'note': 'a new head arrived during the review'})
        return
    heads = list(entry.get('review_heads') or [])
    if head in heads and not rereview:
        finish(cfg, key, dict(entry, head=head), entry.get('outcome') or rv['outcome'], {**extra, 'posted': 'skipped-same-head'})
        return
    if event == 'APPROVE':
        why = pr_format.gate_approve(rv, fresh, head, flagged, idx)
        if why:  # CI or the PR changed between the review and now: review it again later
            finish(cfg, key, entry, 'requeued', {**extra, 'note': 'the approval bar failed at posting',
                                                 'gate': sorted({r.split(':')[0] for r in why})})
            return
    old_id = entry.get('review_id') if rereview and head in heads else None
    old_event = entry.get('event') or ('APPROVE' if entry.get('approved') else 'COMMENT')
    if old_id and event == old_event and not comments and not entry.get('inline', entry.get('items', 0)):
        posted, url, state, rid = update_review(repo, n, old_id, body)
    else:
        posted, url, state, rid = post_review(repo, pr, body, comments, event)
        if old_id and posted == 'ok':
            p2 = update_review(repo, n, old_id, pointer_body(NAME, url))[0]
            extra.update(replaced=old_id, replaced_status=p2)
    if posted == 'ok' and state == 'PENDING':  # R0: never leave one in the reviewer's slot
        subprocess.run(['gh', 'api', '-X', 'DELETE', f'repos/{repo}/pulls/{n}/reviews/{rid}'],
                       capture_output=True, text=True, timeout=60)
    elif posted == 'ok':
        extra['dismissed'], extra['dismiss_failed'] = dismiss_stale(repo, n, rid, cfg['reviewer'])
    if not url:
        finish(cfg, key, dict(entry, head=head), 'held-inline' if comments else 'error', {**extra, 'posted': posted})
        return
    inline = anchored_lines(repo, n, rid) if comments else None
    if comments and inline is not None and len(inline) < len(comments):
        inline += [None] * (len(comments) - len(inline))
    reasons = pr_format.gate_delivery(fresh['state'], url, state, inline_lines=inline,
                                      ours_on_head=ours_on_head(repo, n, head))
    if head not in heads:
        heads.append(head)
    pr_state.keep_posted(cfg, entry, head, body, comments, result)
    extra.update(review_url=url, review_id=rid, review_heads=heads[-20:], body_sha=body_sha(body), posted=posted,
                 delivered_at=pr_state.iso(pr_state.now()), approved=event == 'APPROVE', event=event,
                 inline=len(comments), rc_live=event == 'REQUEST_CHANGES',
                 block=block_kind(rv) if event == 'REQUEST_CHANGES' else None)
    if reasons:  # posted, but it broke a delivery rule: no Slack link, and the log says which
        finish(cfg, key, dict(entry, head=head), 'error', {**extra, 'gate': [r.split(':')[0] for r in reasons]})
        return
    ts = entry.get('slack_ts') if rereview and head in (entry.get('review_heads') or []) else None
    slack, new_ts = pr_state.slack_post(cfg, rv['outcome'], entry['url'], url, approved=event == 'APPROVE', ts=ts,
                                        event=event)
    extra['slack'] = slack
    if new_ts:
        extra['slack_ts'] = new_ts
    finish(cfg, key, dict(entry, head=head), rv['outcome'], extra)


def redeliver(cfg, key):
    """Deliver the stored result for the PR's current head again, without a model run."""
    with pr_state.locked(cfg['state']) as st:
        entry = dict(st['prs'][key])
    pr = pr_rules.get_pr(entry['repo'], entry['number'])
    p = os.path.join(cfg['work'], 'feedback', 'posted',
                     f"{entry['repo'].replace('/', '__')}-{entry['number']}-{pr['headRefOid'][:10]}.json")
    if not os.path.exists(p):
        print(json.dumps({'pr': entry['url'], 'redeliver': 'no stored result for this head'}))
        return
    res = json.load(open(p)).get('result') or {}
    if 'blockers' not in res or res.get('outcome') == 'not-reviewed' or \
            any('change' not in x for x in res.get('items', []) + res.get('cleared', [])):  # pre-v5, or the R33 note
        print(json.dumps({'pr': entry['url'], 'redeliver': 'no stored review for this head; use --rereview'}))
        return
    author = author_of(pr)
    ci = pr_rules.ci_state(pr)
    rv = dict(res, blockers=pr_format.blockers(entry['repo'], pr, ci, ci_detail(pr)),
              facts=pr_format.facts_line(pr, ci, ci_detail(pr)), approve=False, request_changes=cfg['request_changes'])
    rv['outcome'] = pr_format.decide(rv['decisions'], rv['items'], rv['blockers'])
    body = pr_format.render(entry['repo'], pr, rv, NAME, author)
    comments = pr_format.inline_comments(rv['items'], author)
    idx = pr_format.diff_index(pr_diff(entry['repo'], pr['number']))
    reasons = pr_format.gate(rv, idx, body, None, pr.get('body') or '', res.get('flagged') or [], comments)
    if reasons:
        finish(cfg, key, entry, 'held-gate', {'model': False, 'redeliver': True, 'gate': [r.split(':')[0] for r in reasons]})
        return
    deliver(cfg, key, entry, pr, rv, body, comments, res, {'model': False, 'redeliver': True, 'gate': []})


def pr_diff(repo, number):
    r = subprocess.run(['gh', 'api', f'repos/{repo}/pulls/{number}', '-H', 'Accept: application/vnd.github.diff'],
                       capture_output=True, text=True, timeout=120)
    return r.stdout if r.returncode == 0 else ''


def finish(cfg, key, entry, outcome, extra):
    """Record the run's end. 'requeued' puts the PR back in the queue and keeps its bead open."""
    if cfg.get('no_deliver'):
        print(json.dumps({'pr': entry['url'], 'outcome': outcome, 'no_deliver': True, **extra}))
        return
    requeue = outcome == 'requeued'
    if not requeue:
        beads = pr_state.Beads(cfg['city'], enabled=cfg['beads'])
        try:
            beads.set(entry['rig'], entry.get('bead'), head=entry.get('head'), outcome=outcome, close=True)
        except (RuntimeError, OSError):
            extra['bead_error'] = True
    with pr_state.locked(cfg['state']) as st:
        cur = st['prs'].get(key, entry)
        cur.update(entry)
        if requeue:
            cur.update(status='queued', requeued_at=pr_state.iso(pr_state.now()), note=extra.get('note'))
        else:
            cur.update(status='done', outcome=outcome, reviewed_at=pr_state.iso(pr_state.now()), **extra)
        st['prs'][key] = cur
    pr_state.log(cfg['state'], {'run': 'review', 'pr': entry['url'], 'outcome': outcome, **extra})
    print(json.dumps({'pr': entry['url'], 'outcome': outcome, **extra}))


def rule_ids(hits):
    return sorted(h for h in hits if h != 'no-description')


def ci_detail(pr):
    roll = (pr['commits']['nodes'][0]['commit'].get('statusCheckRollup') or {}) if pr['commits']['nodes'] else {}
    return [c.get('name') or c.get('context') for c in (roll.get('contexts') or {}).get('nodes', [])
            if (c.get('conclusion') or c.get('state')) in pr_rules.CI_RED]


def content_rules(hits, area_of, ruleset):
    return sorted(h for h in hits if area_of.get(h) in ruleset['select_areas'] and h not in pr_rules.NOT_CONTENT)


def author_of(pr):
    login = (pr.get('author') or {}).get('login')
    return NAMES.get(login, login or 'the author')


MAX_FLAGS = 12


def flags_for(files, ruleset, hits, area_of):
    """The content rules' hunks (R19) with ids F1.., at most MAX_FLAGS; and the not-checked hunks (R20)."""
    located = pr_rules.locate(files, ruleset, content_rules(hits, area_of, ruleset), limit=1000)
    flagged = [dict(f, id=f'F{k}') for k, f in enumerate(located[:MAX_FLAGS], 1)]
    nc = pr_rules.locate(files, ruleset, pr_rules.not_checked(hits, ruleset, area_of), limit=1000)
    return flagged, max(0, len(located) - MAX_FLAGS), nc


def assemble(cfg, repo, pr, ci, decisions, items, cleared, skim, flagged, flags_cut, money, ruleset, author, model,
             idx=None, digest=None, conflicts=(), money_info=None):
    """The review dict (pr_format): the not-checked decision first when the PR touches that area (R20,
    R32; `money` holds its changed ranges with their lines); blockers for a conflict or CI that did not
    pass (R22); the repository's rules for the links (R37); an approval only when the bar holds (R27), and
    a low-risk review that misses the bar asks the author for what is missing instead."""
    words = ruleset.get('not_checked') or 'billing, plans, credits or pricing'
    if money:
        mi = money_info or {}
        decisions = [pr_format.not_checked_decision(money, words, author, mi.get('overview', ''), mi.get('overview_places', ()))] + \
            decisions[:pr_format.MAX_DECISIONS - 1]
    labels = {f['id']: f['label'] for f in flagged}
    cleared = [dict(c, label=labels.get(c.get('flag'), 'a flagged change')) for c in cleared]
    skip = [dict(g, paths=[x for x in g['paths'] if idx is None or x in idx]) for g in (skim or {}).get('skip') or []]
    rv = {'decisions': decisions, 'items': items, 'cleared': cleared, 'summary': (skim or {}).get('summary') or '',
          'skip': [g for g in skip if g['paths']],
          'blockers': pr_format.blockers(repo, pr, ci, ci_detail(pr), conflicts),
          'facts': pr_format.facts_line(pr, ci, ci_detail(pr)),
          'not_checked': words if money else '', 'model': model, 'flags_cut': flags_cut, 'approve': False,
          'request_changes': cfg['request_changes'], 'rules': (digest or {}).get('rules') or {},
          'rules_sha': (digest or {}).get('sha') or ''}
    rv['outcome'] = pr_format.decide(rv['decisions'], rv['items'], rv['blockers'])
    if rv['outcome'] == 'low-risk' and cfg['approve']:
        if not pr_format.ci_strict(pr):
            rv['blockers'].append({'kind': 'question', 'short': 'get every check to pass', 'label': 'Get every check to pass',
                                   'url': f"https://github.com/{repo}/pull/{pr['number']}/checks",
                                   'detail': 'an approval needs every check passed, neutral or skipped.'})
        if flags_cut:
            rv['blockers'].append({'kind': 'question', 'short': 'split the PR', 'label': 'Split the PR',
                                   'url': f"https://github.com/{repo}/pull/{pr['number']}/files",
                                   'detail': f'{len(flagged) + flags_cut} flagged changes are more than one review checks.'})
        rv['outcome'] = pr_format.decide(rv['decisions'], rv['items'], rv['blockers'])
        rv['approve'] = rv['outcome'] == 'low-risk' and not pr_format.gate_approve(rv, pr, pr['headRefOid'], flagged, idx)
    return rv


def minimal_not_checked(cfg, repo, pr, ci, money, ruleset, author, money_info=None):
    """R33 (the mayor, 2026-10-29 21:57Z): the review of a PR that touches a not-checked area, when the
    reader test held the full one twice: line 2, the money decision (each place linked to its changed range
    with its one line, R32) and the status line, nothing the review model wrote."""
    rv = assemble(cfg, repo, pr, ci, [], [], [], '', [], 0, money, ruleset, author, model=False, money_info=money_info)
    return rv, pr_format.render(repo, pr, rv, NAME, author), []


def describe_money(cfg, idx, located, ruleset):
    """R32: the not-checked places as changed ranges, each with one neutral line and its kind from pr_money
    (every place is linked; the mayor, 2026-10-30 01:40Z: never a bare count; 02:11Z: a missing line gets
    one retry, then "(not summarized)"). Returns (ranges, meta)."""
    ranges = pr_format.money_ranges(idx, located)
    words, out, cost, info = ruleset.get('not_checked') or 'billing, plans, credits or pricing', [], 0, {}
    for k in range(0, len(ranges), pr_money.MAX_PLACES):
        part, m = pr_money.describe(cfg, idx, ranges[k:k + pr_money.MAX_PLACES], words)
        out, cost = out + part, cost + (m.get('cost_usd') or 0)
        info = info or {'overview': clean(m.get('overview') or ''), 'overview_places': m.get('overview_places') or []}
    miss = [j for j, r in enumerate(out) if not r['what'].strip()]
    if miss:  # one more try for just the places that came back without a line (the mayor, 02:11Z)
        again, m = pr_money.describe(cfg, idx, [out[j] for j in miss], words)
        cost += m.get('cost_usd') or 0
        for j, r in zip(miss, again):
            out[j].update({k: r[k] for k in ('what', 'kind', 'behaviour', 'risk') if k in r})
    # A place still without a line keeps its link and says so: it never holds the money review.
    return [dict(r, what=clip(clean(r['what']), pr_format.MONEY_WORDS) or pr_format.NOT_SUMMARIZED) for r in out], \
        {'cost_usd': cost, **info}


def clip(text, words):
    """At most `words` words, so a long money line never holds the money review (R32)."""
    w = text.split()
    return text if len(w) <= words else ' '.join(w[:words - 1]) + ' …'


def question_from(c, question=''):
    """R31: a cleared entry the reader still finds risky without a reason becomes a question for the author:
    the reader's one concrete check on those lines (the mayor, 02:11Z: never a stock prompt)."""
    q = (question or '').strip() or f"What makes the {c['change'][0].lower() + c['change'][1:]} safe on these lines?"
    return {'path': c['path'], 'side': 'new', 'line_start': c['line_start'], 'line_end': c['line_end'],
            'kind': 'question', 'severity': 'low', 'area': 'other', 'change': c['change'], 'why': c['why'],
            'ask': q if q.endswith('?') else q.rstrip('.') + '?', 'rule': c.get('rule') or '', 'verified': '',
            'confidence': 8}


def not_reviewed(repo, pr, ci, idx, flagged, money, author, held, codes, words):
    """R33: a PR that requests the reviewer is never silent, and the note keeps a floor the reviewer can act
    on (the mayor, 2026-10-30 02:11Z): why the review was held, in plain words; the places the zero-token
    rules know, each linked (the money places with their lines, the flagged hunks with what the rule saw);
    and one rule-level decision with lettered options and a recommendation."""
    places, seen = [], set()
    for x in list(money) + [dict(pr_format.changed_range(idx, f['path'], f['line']), what=f['label']) for f in flagged]:
        k = (x['path'], x.get('hunk') if x.get('hunk') is not None else x['line_start'])
        if k not in seen:
            seen.add(k)
            places.append(x)
    parts = []
    if money:
        parts.append(f"it touches {words} in {pr_format.plural(len(money), 'place', 'places')}, which no agent checks")
    if held == 'held-secret':
        parts.append('its text matched the shape of a secret, so it was withheld')
    elif 'R26' in codes or 'R31' in codes:
        parts.append('a second reader found our review too hard to act on')
    elif codes:
        parts.append('our review broke the review format twice')
    why = "Our review of this PR didn't reach the quality bar: " + '; '.join(parts or ['it was held']) + '.'
    rv = {'outcome': 'not-reviewed', 'decisions': [pr_format.floor_decision(places, author, why)], 'items': [],
          'cleared': [], 'blockers': pr_format.blockers(repo, pr, ci, ci_detail(pr)), 'summary': '', 'skip': [],
          'facts': pr_format.facts_line(pr, ci, ci_detail(pr)), 'not_checked': '', 'approve': False, 'model': False,
          'request_changes': False, 'rules': {}, 'rules_sha': ''}
    return rv, pr_format.render(repo, pr, rv, NAME, author), []


def keep_reader(cfg, entry, head, attempt, scores, problems):
    """The reader test's reasons for a held attempt, host-local (they can describe the repository's
    code), to tune the review and the reader."""
    p = os.path.join(pr_state.private_dir(cfg, 'feedback'), 'reader.jsonl')
    with open(p, 'a') as f:
        f.write(json.dumps({'pr': entry['url'], 'head': head, 'attempt': attempt, 'scores': scores,
                            'problems': problems, 'at': pr_state.iso(pr_state.now())}) + '\n')
    os.chmod(p, 0o600)


def ci_escalation(cfg, key, entry, dry, rereview=False):
    """CI red, missing or skipped with no content rule: the blockers for the author, no model (R11). With
    a content rule, or when the PR requests the reviewer (R34), the PR goes to the model. Closed PRs are
    dropped (R15)."""
    ruleset = pr_rules.load_rules(cfg['rules'])
    area_of = pr_rules.areas(ruleset)
    pr = pr_rules.get_pr(entry['repo'], entry['number'])
    if pr['state'] != 'OPEN':
        finish(cfg, key, entry, 'closed', {'model': False})
        return
    if pr['headRefOid'] in (entry.get('review_heads') or []) and not rereview:  # R18: reviewed already
        finish(cfg, key, entry, entry.get('outcome') or 'skipped', {'model': False, 'posted': 'skipped-same-head'})
        return
    ci = pr_rules.ci_state(pr)
    files = pr_rules.pr_files(entry['repo'], pr['number'])
    base = pr_rules.base_for(pr, {pr['baseRefName']: pr_rules.branch_oid(entry['repo'], pr['baseRefName'])})
    hits = pr_rules.evaluate(pr, files, ruleset, cfg['reviewer'], entry['repo'], base)
    if not pr_rules.ci_only(ci, 'requested' in hits, bool(content_rules(hits, area_of, ruleset))):
        return model_review(cfg, key, entry, dry, rereview)
    _, _, nc = flags_for(files, ruleset, hits, area_of)
    author = author_of(pr)
    money, mmeta = describe_money(cfg, pr_format.diff_index(pr_diff(entry['repo'], pr['number'])), nc, ruleset) \
        if nc else ([], {})
    rv = assemble(cfg, entry['repo'], pr, ci, [], [], [], '', [], 0, money, ruleset, author, model=False, money_info=mmeta)
    body = pr_format.render(entry['repo'], pr, rv, NAME, author)
    reasons = pr_format.gate(rv, {}, body, None, pr.get('body') or '', [], [])
    if dry:
        print(json.dumps({'pr': entry['url'], 'would': rv['outcome'], 'ci': ci, 'gate': [r.split(':')[0] for r in reasons]}))
        return
    if reasons:
        finish(cfg, key, dict(entry, head=pr['headRefOid']), 'held-gate', {'model': False, 'gate': [r.split(':')[0] for r in reasons]})
        return
    result = {k: rv[k] for k in ('outcome', 'decisions', 'items', 'cleared', 'blockers', 'summary', 'skip', 'not_checked')}
    result.update(rule_ids=rule_ids(hits), flagged=[])
    deliver(cfg, key, entry, pr, rv, body, [], result, {'model': False, 'ci': ci, 'gate': [],
                                                        'money_cost_usd': mmeta.get('cost_usd', 0)}, rereview=rereview)


def model_review(cfg, key, entry, dry, rereview=False):
    repo = entry['repo']
    ruleset = pr_rules.load_rules(cfg['rules'])
    area_of = pr_rules.areas(ruleset)
    pr = pr_rules.get_pr(repo, entry['number'])
    if pr['state'] != 'OPEN' and not cfg.get('bench'):  # the benchmark reviews merged PRs at their head, offline
        finish(cfg, key, entry, 'closed', {'model': False})
        return
    files_gh = pr_rules.pr_files(repo, pr['number'])
    base = pr_rules.base_for(pr, {pr['baseRefName']: pr_rules.branch_oid(repo, pr['baseRefName'])})
    hits = pr_rules.evaluate(pr, files_gh, ruleset, cfg['reviewer'], repo, base)
    ci = pr_rules.ci_state(pr)
    flagged, flags_cut, nc_rule = flags_for(files_gh, ruleset, hits, area_of)
    author = author_of(pr)
    run_id = f"{repo.replace('/', '__')}-{pr['number']}-{pr['headRefOid'][:10]}"
    mirror = None
    try:
        mirror, tree, ctx, diff, files, commits, fork = prepare(cfg, repo, pr, run_id)
        # R37: the repository's own rules, as they stand on the base branch the PR merges into
        digest, dmeta = pr_digest.get(cfg, repo, mirror, git(mirror, 'rev-parse', f"refs/heads/{pr['baseRefName']}").strip())
        conflicts = conflict_files(mirror, pr)
        prompt = build_prompt(cfg, pr, diff, files, commits, fork, ctx, flagged, ruleset.get('not_checked'),
                              pr_digest.prompt_block(digest), author)
        if dry:
            print(json.dumps({'pr': entry['url'], 'dry_run': True, 'prompt_chars': len(prompt),
                              'diff_chars': len(diff), 'files': len(files.splitlines()), 'rules': sorted(hits),
                              'flagged_hunks': len(flagged), 'flags_cut': flags_cut, 'not_checked_hunks': len(nc_rule)}))
            return
        idx = pr_format.diff_index(diff)
        extra = {'model': True, 'cost_usd': 0, 'attempts': 0, 'judge_cost_usd': 0, 'money_cost_usd': 0,
                 'digest': dmeta.get('digest'), 'digest_rules': dmeta.get('rules', 0),
                 'digest_cost_usd': dmeta.get('cost_usd', 0)}
        reasons, problems, model, risky, risky_q, money_memo = [], [], None, [], {}, {}
        for attempt in (1, 2):
            p = prompt if attempt == 1 else (
                prompt + '\n\nYOUR PREVIOUS ANSWER FAILED THE STYLE GUIDE GATE. Fix every reason and answer again:\n'
                + '\n'.join(f'- {r}' for r in reasons)
                + ''.join(f"\n- a reader found: {x['what']} Fix: {x['fix']}" for x in problems)
                + '\n\nYour previous answer:\n' + json.dumps(model))
            model, meta = run_model(cfg, tree, ctx, p)
            extra['attempts'] = attempt
            extra['cost_usd'] += meta.get('cost_usd') or 0
            extra.update({k: v for k, v in meta.items() if k != 'cost_usd'})
            if model is None:
                finish(cfg, key, dict(entry, head=pr['headRefOid']), 'error', extra)
                return
            decisions = [d for d in model['decisions'] if d['confidence'] >= 8]
            items = [i for i in model['items'] if i['confidence'] >= 8][:pr_format.MAX_ITEMS]
            # R20: the rule's hunks, plus the money lines the model saw that are in the diff.
            nc = nc_rule + [dict(x, rule='model') for x in model['not_checked_lines']
                            if x['line'] in (idx.get(x['path']) or {}).get('new', set())]
            nc_key = tuple(sorted({(x['path'], x['line']) for x in nc}))
            if nc_key not in money_memo:  # R32: the money lines, once per set of places
                money_memo[nc_key] = describe_money(cfg, idx, nc, ruleset) if nc else ([], {})
                extra['money_cost_usd'] += money_memo[nc_key][1].get('cost_usd') or 0
            money, money_info = money_memo[nc_key]
            rv = assemble(cfg, repo, pr, ci, decisions, items, model['cleared'], model, flagged,
                          flags_cut, money, ruleset, author, model=True, idx=idx, digest=digest, money_info=money_info, conflicts=conflicts)
            body = pr_format.render(repo, pr, rv, NAME, author)
            comments = pr_format.inline_comments(rv['items'], author)
            links = [(d.get('link') or {}).get('path') for d in rv['decisions'] if (d.get('link') or {}).get('kind') == 'code']
            reasons = pr_format.gate(rv, idx, body, tree_lines(tree, links), pr.get('body') or '', flagged, comments)
            problems = []
            if not reasons and cfg['judge']:
                scores, problems, jm = pr_judge.judge(cfg, body, comments, NAME, author)
                extra['judge_cost_usd'] += jm.get('cost_usd') or 0
                extra['judge'] = scores
                risky = [j for j in jm.get('risky_cleared') or [] if j <= len(rv['cleared'])]
                risky_q = jm.get('risky_questions') or {}
                extra['risky_cleared'] = len(risky)
                reasons = pr_judge.below_bar(scores) + ([
                    f'R31: cleared entries {risky} describe a possible risk without saying why it is acceptable or '
                    f'intended; give the fact that makes each one safe, or make it a question for the author']
                    if risky and scores is not None else []) + ([
                    'R37: a change in product behaviour sits in "Safe to skip"; give it its own linked line saying '
                    'it is intended and its default is safe, or make it a decision'] if jm.get('behaviour_in_skip') else [])
                extra['behaviour_in_skip'] = bool(jm.get('behaviour_in_skip'))
                if reasons and scores is not None:
                    keep_reader(cfg, entry, pr['headRefOid'], attempt, scores, problems)
                if scores is None:  # the reader test did not run: hold without a second model run
                    break
            if not reasons:
                break
            extra.setdefault('gate_first', sorted({r.split(':')[0] for r in reasons}))
        in_skip = [r for r in reasons if r.startswith('R37: a change in product behaviour')]
        if reasons and all(r.startswith('R31') or r in in_skip for r in reasons):
            # R31: what the reader still finds risky after the second answer is not cleared; it becomes a
            # question for the author. A behaviour change the reader still finds in the skip list means no
            # approval (R37). Either way the PR is not approved.
            asked = [question_from(c, risky_q.get(j)) for j, c in enumerate(rv['cleared'], 1) if j in risky]
            rv = assemble(cfg, repo, pr, ci, decisions, (items + asked)[:pr_format.MAX_ITEMS],
                          [c for j, c in enumerate(model['cleared'], 1) if j not in risky], model,
                          flagged, flags_cut, money, ruleset, author, model=True, idx=idx, digest=digest, money_info=money_info,
                          conflicts=conflicts)
            if in_skip:
                rv['approve'] = False
            body = pr_format.render(repo, pr, rv, NAME, author)
            comments = pr_format.inline_comments(rv['items'], author)
            links = [(d.get('link') or {}).get('path') for d in rv['decisions'] if (d.get('link') or {}).get('kind') == 'code']
            reasons = pr_format.gate(rv, idx, body, tree_lines(tree, links), pr.get('body') or '', flagged, comments)
            extra['r31_questions'] = len(asked)
        # Enum values, counts and rule ids only, so the log can say why reviews end where they do.
        extra.update(decisions=len(rv['decisions']), items=len(rv['items']), cleared=len(rv['cleared']),
                     blockers=len(rv['blockers']), not_checked=bool(rv['not_checked']), approve=rv['approve'],
                     dropped=len(model['items']) + len(model['decisions']) - len(items) - len(decisions),
                     kinds=sorted(i['kind'] for i in items), severity=sorted(i['severity'] for i in items),
                     checks=model['checks'], flagged_hunks=len(flagged), flags_cut=flags_cut, ci=ci,
                     money_places=len(money), money_unlined=sum(1 for x in money if not x.get('what')),
                     gate=sorted({r.split(':')[0] for r in reasons}))
        if cfg.get('collect') is not None:  # the benchmark (pr_bench.py): the review as it stands, nothing delivered
            cfg['collect'].append({'rv': rv, 'held': sorted({r.split(':')[0] for r in reasons}), 'extra': dict(extra)})
            return
        held = 'held-gate' if reasons else ('held-secret' if leaks([body] + [c['body'] for c in comments]) else None)
        fallback = held and pr_rules.held_fallback(
            'requested' in hits, bool(rv['not_checked']),
            held == 'held-gate')
        if fallback == 'not-checked-minimal':
            # The gate or the reader test held it twice, but a PR that touches a not-checked area is never silent:
            # deliver only line 2, the money decision and the facts line, with no model findings (R33).
            rv, body, comments = minimal_not_checked(cfg, repo, pr, ci, money, ruleset, author, money_info)
            reasons = pr_format.gate(rv, idx, body, tree_lines(tree, [w['path'] for w in rv['decisions'][0]['where']]),
                                     pr.get('body') or '', [], comments)
            extra.update(fallback=fallback, held=held, reader_held=extra.get('judge'), decisions=1, items=0,
                         cleared=0, held_gate=extra['gate'], gate=sorted({r.split(':')[0] for r in reasons}))
            if cfg['judge'] and not reasons:  # for the log only: the money review is never held by the reader
                fs, _, fm = pr_judge.judge(cfg, body, comments, NAME, author)
                extra.update(fallback_judge=fs, judge_cost_usd=extra['judge_cost_usd'] + (fm.get('cost_usd') or 0))
            held = 'held-gate' if reasons else None
            fallback = held and pr_rules.held_fallback('requested' in hits, False, False)
        if fallback == 'not-reviewed':
            # A PR that requests the reviewer is never silent: the note, with its floor (R33).
            codes = extra.get('held_gate') or extra['gate']
            rv, body, comments = not_reviewed(repo, pr, ci, idx, flagged, money, author, held, codes,
                                              ruleset.get('not_checked') or 'billing, plans, credits or pricing')
            reasons = pr_format.gate(rv, idx, body, tree_lines(tree, [w['path'] for w in rv['decisions'][0].get('where') or []]),
                                     pr.get('body') or '', [], comments)
            extra.update(fallback=fallback, held=held, held_gate=codes, decisions=1, items=0, cleared=0,
                         blockers=len(rv['blockers']), gate=sorted({r.split(':')[0] for r in reasons}))
            if cfg['judge'] and not reasons:  # the reader test, for the log: the floor is never held by it
                fs, _, fm = pr_judge.judge(cfg, body, comments, NAME, author)
                extra.update(fallback_judge=fs, judge_cost_usd=extra['judge_cost_usd'] + (fm.get('cost_usd') or 0))
            held = 'held-gate' if reasons else None
        if held:
            finish(cfg, key, dict(entry, head=pr['headRefOid']), held, extra)
            return
        result = {k: rv[k] for k in ('outcome', 'decisions', 'items', 'cleared', 'blockers', 'summary', 'skip', 'not_checked')}
        result.update(rule_ids=rule_ids(hits), flagged=flagged if rv['outcome'] != 'not-reviewed' else [],
                      rules=rv.get('rules') or {}, rules_sha=rv.get('rules_sha') or '')
        deliver(cfg, key, entry, pr, rv, body, comments, result, extra, flagged, idx, rereview)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        # Only our own RuntimeError texts are kept; an OSError can name a file of the repository.
        detail = str(e)[:120] if isinstance(e, RuntimeError) else getattr(e, 'errno', None)
        finish(cfg, key, dict(entry, head=pr['headRefOid']), 'error', {'model': False, 'error': type(e).__name__,
                                                                        'detail': detail})
    finally:
        cleanup(cfg, mirror, run_id)


def pick(cfg, only):
    """Under the lock: expire what waited too long, reset stale picks, and choose this run's work."""
    t = pr_state.now()
    today = t.strftime('%Y-%m-%d')
    with pr_state.locked(cfg['state']) as st:
        prs = st['prs']
        for e in prs.values():
            if e.get('status') == 'reviewing' and pr_state.parse(e['picked_at']) < t - dt.timedelta(hours=1):
                e['status'] = 'queued'
        used = sum(1 for e in prs.values() if e.get('model') and (e.get('reviewed_at') or '').startswith(today))
        used += sum(1 for e in prs.values() if e.get('status') == 'reviewing')
        if only:
            chosen = [only] if only in prs else []
            ci_keys = []
        else:
            expired = [k for k, e in prs.items() if e.get('status') == 'queued'
                       and pr_state.parse(e['queued_at']) < t - dt.timedelta(hours=cfg['max_wait_h'])]
            for k in expired:
                prs[k].update(status='expiring')
            queued = [(k, e) for k, e in prs.items() if e.get('status') == 'queued']
            ruleset = pr_rules.load_rules(cfg['rules'])
            area_of = pr_rules.areas(ruleset)
            # CI-only escalations need no model and no cap (R11); a PR with a content rule, or one that
            # requests the reviewer (R34), waits its turn for the model.
            ci_keys = [k for k, e in queued if pr_rules.ci_only(e.get('ci'), 'requested' in e.get('rules', []),
                                                                bool(content_rules(e.get('rules', []), area_of, ruleset)))]
            # Tier, then more areas, then the newest PR first (most merge within half an hour).
            rest = sorted(((k, e) for k, e in queued if k not in ci_keys), key=lambda ke: ke[1]['created'], reverse=True)
            rest.sort(key=lambda ke: (ke[1]['tier'], -len(ke[1].get('areas') or [])))
            chosen = []
            for k, e in rest:
                room = cfg['cap'] - used - (cfg['reserve'] if e['tier'] == 2 else 0)
                if room > 0:
                    chosen = [k]
                break
        for k in chosen + ci_keys:
            prs[k].update(status='reviewing', picked_at=pr_state.iso(t))
        expiring = [k for k, e in prs.items() if e.get('status') == 'expiring']
        snapshot = {k: dict(prs[k]) for k in chosen + ci_keys + expiring}
    return chosen, ci_keys, expiring, snapshot, used


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pr', metavar='REPO#N')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--redeliver', metavar='REPO#N')
    ap.add_argument('--rereview', metavar='REPO#N')
    ap.add_argument('--no-deliver', action='store_true')
    a = ap.parse_args()
    cfg = pr_state.config()
    if a.no_deliver and (a.pr or a.rereview):
        cfg['no_deliver'] = True
        key = a.pr or a.rereview
        repo, n = key.rsplit('#', 1)
        with pr_state.locked(cfg['state']) as st:
            entry = dict(st['prs'].get(key) or {'repo': repo, 'number': int(n), 'url': f'https://github.com/{repo}/pull/{n}',
                                                 'rig': cfg['repos'].get(repo, repo.split('/')[1]), 'rules': []})
        ci_escalation(cfg, key, entry, False, rereview=True)
        return
    if a.redeliver:
        redeliver(cfg, a.redeliver)
        return
    if a.rereview:
        a.pr = a.rereview
    if not cfg['on'] and not a.dry_run and not a.pr:
        print(json.dumps({'skipped': 'PR_REVIEW is off'}))
        return
    load = pr_rules.load_avg()
    if load > cfg['max_load']:
        pr_state.log(cfg['state'], {'run': 'review', 'skipped': 'load', 'load': load})
        print(json.dumps({'skipped': 'load', 'load': load}))
        return
    if a.pr and a.dry_run:
        repo, n = a.pr.rsplit('#', 1)
        entry = {'repo': repo, 'number': int(n), 'url': f'https://github.com/{repo}/pull/{n}',
                 'rig': cfg['repos'].get(repo, repo.split('/')[1]), 'rules': []}
        model_review(cfg, a.pr, entry, dry=True)
        return
    if a.pr:
        with pr_state.locked(cfg['state']) as st:
            if a.pr not in st['prs']:
                repo, n = a.pr.rsplit('#', 1)
                st['prs'][a.pr] = {'repo': repo, 'number': int(n), 'url': f'https://github.com/{repo}/pull/{n}',
                                   'rig': cfg['repos'].get(repo, repo.split('/')[1]), 'status': 'queued',
                                   'tier': 0, 'rules': [], 'created': pr_state.iso(pr_state.now()),
                                   'queued_at': pr_state.iso(pr_state.now())}
            st['prs'][a.pr]['status'] = 'queued'
    chosen, ci_keys, expiring, snap, used = pick(cfg, a.pr)
    for k in expiring:
        finish(cfg, k, snap[k], 'over-cap', {'model': False})
    for k in ci_keys:
        ci_escalation(cfg, k, snap[k], a.dry_run)
    for k in chosen:  # hands the PR to model_review unless only CI is wrong
        ci_escalation(cfg, k, snap[k], a.dry_run, rereview=bool(a.rereview))
    if not (chosen or ci_keys or expiring):
        print(json.dumps({'idle': True, 'reviews_today': used, 'cap': cfg['cap']}))


if __name__ == '__main__':
    main()
