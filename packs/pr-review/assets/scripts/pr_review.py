#!/usr/bin/env python3
"""PR review run (README.md, "Review"): one queued open PR per run, reviewed by a headless model that can
only read; this script renders, gates and delivers the result.

  pr_review.py                  CI-only questions (no model), then the next queued PR within the cap
  pr_review.py --pr REPO#N      review this PR now (queued or not; counts against the cap)
  pr_review.py --dry-run [--pr REPO#N]
                                pick, prepare and build the prompt; run no model, deliver nothing

Delivery (Alex, 2026-10-29 20:45Z): every reviewed open PR gets a SUBMITTED review on GitHub under the
reviewer's login, event COMMENT (APPROVE and REQUEST_CHANGES need the reviewer's word), never a pending
one (R0). Its first line says it is automated and not yet read by the reviewer; the items are inline
comments on their exact lines. Slack gets the outcome word and that review's own URL (R17), once per PR
head (R18). Right before posting the script re-checks that the PR is still open (R15). The model runs in
a git worktree at the PR head with Read, Grep and Glob only, no hooks, no MCP, no settings from the
repository, reads outside the worktree denied, no GitHub or Slack credential, LOOMTRACER_INGEST_DISABLE=1,
and returns decisions for the reviewer (R16) and line-anchored items for the author as JSON. This script
derives the outcome, renders it (assets/style-guide.md) and gates it (pr_format.py): a review that fails
is regenerated once with the gate's reasons, then held. What it prints and keeps in the runtime is ids,
links, outcomes, counts, gate reasons and cost: never the repository's text.

  pr_review.py --redeliver REPO#N   deliver the stored result for the PR's current head again (no model)
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
import pr_format  # noqa: E402
import pr_rules  # noqa: E402
import pr_state  # noqa: E402

HERE = pr_state.HERE
UPSTREAM = os.path.join(HERE, '..', 'upstream', 'security-review.md')
STYLE = os.path.join(HERE, '..', 'style-guide.md')
MAX_DIFF_CHARS = 350_000
# The footer names the person (default: the login); PR_REVIEW_NAMES maps authors' logins to names.
NAME = os.environ.get('PR_REVIEW_REVIEWER_NAME') or os.environ.get('PR_REVIEW_REVIEWER') or 'the reviewer'
NAMES = dict(x.split('=', 1) for x in os.environ.get('PR_REVIEW_NAMES', '').split() if '=' in x)

_TEXT = {'type': 'string'}
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['decisions', 'items', 'safe_to_skip', 'reply_to_author', 'checks'],
    'properties': {
        'decisions': {'type': 'array', 'maxItems': 3, 'description': (
            'Only decisions the reviewer alone can make: a product, risk or compliance trade-off, never a code '
            'fix the author can make. Empty when there is none.'), 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['question', 'options', 'recommendation', 'why', 'area', 'link', 'verified', 'confidence'],
            'properties': {
                'question': {'type': 'string', 'maxLength': 280, 'description': 'One question ending in "?", at most 280 characters.'},
                'options': {'type': 'array', 'minItems': 2, 'maxItems': 4, 'items': _TEXT},
                'recommendation': {'type': 'string', 'maxLength': 280},
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
            'required': ['path', 'side', 'line_start', 'line_end', 'kind', 'severity', 'area', 'what', 'why', 'ask',
                         'verified', 'confidence'],
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
                'what': {'type': 'string', 'maxLength': 280, 'description': 'One sentence, at most 280 characters: what these lines change.'},
                'why': {'type': 'string', 'maxLength': 280, 'description': 'One sentence, at most 280 characters: why it matters, naming the area in words.'},
                'ask': {'type': 'string', 'maxLength': 280, 'description': 'At most 280 characters: the exact change, or the exact question or evidence to ask for.'},
                'verified': {'type': 'string', 'description': 'The files you opened to confirm it.'},
                'confidence': {'type': 'integer', 'minimum': 1, 'maximum': 10}}}},
        'safe_to_skip': {'type': 'string', 'maxLength': 220, 'description': 'One line, at most 220 characters: what the rest of the diff is, e.g. "14 files: tests, UI copy, types".'},
        'reply_to_author': {'type': 'string', 'description': 'A short reply the reviewer could post to the author as is: the changes and questions, plainly. Empty for low risk.'},
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
directory is the repository at the PR head. Do these steps yourself, in order:

1. Open every FLAGGED HUNK listed above in its file, and open its callers and the code it calls (Grep for
   the changed functions and types). Do not write an item about code you have not opened; name what you
   opened in `verified`.
2. Read the rest of the diff the same way. Find the vulnerabilities and every issue the custom scan
   instructions ask about. Apply the FALSE POSITIVE FILTERING rules, give each candidate a confidence from
   1 to 10, and drop anything below 8.
3. Sort what is left into two lists:
   - `decisions`: only what the reviewer alone can decide, a product, risk or compliance trade-off (for
     example whether patient data may go to a vendor, or whether a destructive step may run in production).
     Each is ONE question ending in "?", with 2 to 4 options, your recommendation, why only the reviewer can
     decide it, and a link to where it lives: lines of a file at the head ("code"), the PR description
     ("description"), or a ticket URL that appears in the description ("ticket"). A code fix the author can
     make is never a decision.
   - `items` for the author: a "change" they must make or a "question" they must answer, each anchored to
     lines inside one hunk of the diff (`side` "new" for the PR head, "old" for removed lines).
   A flagged hunk where you find nothing wrong needs no entry: the flags are hints, not findings.
4. Keep at most 3 decisions and 5 items, worst first. Write `reply_to_author`: what the reviewer could
   post to the author as is, plainly, covering the changes and questions. Write `safe_to_skip`.

Your answer is the JSON object of the schema you were given; it replaces the markdown output format
described above. You do not pick the outcome: the script derives it (a decision means the reviewer is
needed; a change means changes are requested; only questions means questions for the author; nothing
means low risk). Follow the STYLE GUIDE above to the letter: one sentence each for `what` and `why`, no
filler, no rule codes, no praise. Never copy secrets, tokens or personal data into your answer; point at
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


def cleanup(cfg, mirror, run_id):
    run = os.path.join(cfg['work'], 'runs', run_id)
    if mirror and os.path.isdir(os.path.join(run, 'tree')):
        git(mirror, 'worktree', 'remove', '--force', os.path.join(run, 'tree'), check=False)
    shutil.rmtree(run, ignore_errors=True)
    if mirror:
        git(mirror, 'worktree', 'prune', check=False)


def build_prompt(cfg, pr, diff, files, commits, fork, ctx, flagged):
    text = open(UPSTREAM).read()
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
    hunks = '\n'.join(f"- {f['path']} near line {f['line']} (new side): {f['label']}" for f in flagged) or '- none'
    return (f'{head}{checklist}\n\nSTYLE GUIDE (how your answer will be rendered and checked):\n\n{style}\n\n'
            f'FLAGGED HUNKS (pattern matches on the diff, not findings; open each one, step 1):\n'
            f'{hunks}\n\nVALID ITEM RANGES (an item\'s line_start and line_end must both fall inside ONE of these '
            f'ranges on its side, or the review is rejected):\n{hunk_ranges(diff)}\n\n{FINAL}')


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
    so['safe_to_skip'] = clean(so['safe_to_skip'])
    so['reply_to_author'] = clean(so['reply_to_author'])
    for it in so['items']:
        for k in ('what', 'why', 'ask', 'verified'):
            it[k] = clean(it[k])
    for d in so['decisions']:
        for k in ('question', 'recommendation', 'why', 'verified'):
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


def post_comment_review(repo, pr, body, comments):
    """A SUBMITTED review with event COMMENT (never pending, R0), its items as inline comments. Returns
    (status, review url, review state, review id)."""
    payload = {'commit_id': pr['headRefOid'], 'body': body, 'event': 'COMMENT'}
    for with_comments in ((True, False) if comments else (False,)):
        p = dict(payload, comments=comments) if with_comments else payload
        r = subprocess.run(['gh', 'api', '-X', 'POST', f"repos/{repo}/pulls/{pr['number']}/reviews", '--input', '-'],
                           input=json.dumps(p), capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            out = json.loads(r.stdout)
            return ('ok' if with_comments or not comments else 'ok-no-inline'), out.get('html_url'), out.get('state'), out.get('id')
    code = re.search(r'HTTP (\d{3})', r.stderr)
    return f"error:{code.group(1) if code else r.returncode}", None, None, None


def deliver(cfg, key, entry, pr, outcome, body, comments, result, extra):
    """R15, R18, R0, R17: re-check that the PR is open and this head was not reviewed yet, post the COMMENT
    review, delete it at once if GitHub made it pending, and post '<word>: <review URL>' to Slack."""
    head = pr['headRefOid']
    fresh = pr_rules.get_pr(entry['repo'], entry['number'])
    if fresh['state'] != 'OPEN':
        finish(cfg, key, dict(entry, head=head), 'closed', {**extra, 'gate': ['R15']})
        return
    if fresh['headRefOid'] != head:
        finish(cfg, key, dict(entry, status='queued'), 'requeued', {**extra, 'note': 'a new head arrived during the review'})
        return
    heads = list(entry.get('review_heads') or [])
    if head in heads:
        finish(cfg, key, dict(entry, head=head), entry.get('outcome') or outcome, {**extra, 'posted': 'skipped-same-head'})
        return
    posted, url, state, rid = post_comment_review(entry['repo'], pr, body, comments)
    if posted.startswith('ok') and state == 'PENDING':  # R0: never leave one in the reviewer's slot
        subprocess.run(['gh', 'api', '-X', 'DELETE', f"repos/{entry['repo']}/pulls/{entry['number']}/reviews/{rid}"],
                       capture_output=True, text=True, timeout=60)
    reasons = pr_format.gate_delivery(fresh['state'], url, state)
    if not posted.startswith('ok') or reasons:
        finish(cfg, key, dict(entry, head=head), 'error', {**extra, 'posted': posted, 'gate': [r.split(':')[0] for r in reasons]})
        return
    heads.append(head)
    pr_state.keep_posted(cfg, entry, head, body, comments, result)
    slack, ts = pr_state.slack_post(cfg, outcome, entry['url'], url)
    extra.update(review_url=url, review_id=rid, review_heads=heads[-20:], body_sha=body_sha(body), posted=posted,
                 delivered_at=pr_state.iso(pr_state.now()), slack=slack)
    if ts:
        extra['slack_ts'] = ts
    finish(cfg, key, dict(entry, head=head), outcome, extra)


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
    stored = json.load(open(p))
    res = stored.get('result') or result_from_body(stored['body'])
    author = author_of(pr)
    ci = pr_rules.ci_state(pr)
    facts = pr_format.facts_line(pr, ci, ci_detail(pr))
    args = (entry['repo'], pr, res['outcome'], res['decisions'], res['items'], res['safe_to_skip'], res['reply'],
            facts, res['rule_ids'], NAME, author)
    body = pr_format.render(*args, single_line=res.get('single_line'))
    comments = pr_format.inline_comments(res['items'], author)
    reasons = pr_format.gate(res['decisions'], res['items'], pr_format.diff_index(pr_diff(entry['repo'], pr['number'])),
                             res['outcome'], body, None, pr.get('body') or '', bool(res.get('single_line')), comments)
    if reasons:
        finish(cfg, key, entry, 'held-gate', {'model': False, 'redeliver': True, 'gate': [r.split(':')[0] for r in reasons]})
        return
    deliver(cfg, key, entry, pr, res['outcome'], body, comments, res, {'model': False, 'redeliver': True, 'gate': []})


def pr_diff(repo, number):
    r = subprocess.run(['gh', 'api', f'repos/{repo}/pulls/{number}', '-H', 'Accept: application/vnd.github.diff'],
                       capture_output=True, text=True, timeout=120)
    return r.stdout if r.returncode == 0 else ''


def result_from_body(body):
    """Decisions and items back from a body this pack rendered (results stored before they kept JSON)."""
    lines = body.splitlines()
    head = next((l for l in lines if l.startswith('**') and ('Changes requested' in l or 'question' in l
                                                             or 'Needs you' in l or 'Low risk' in l)), '')
    outcome = ('escalate' if 'Needs you' in head else 'changes' if 'Changes requested' in head
               else 'questions' if 'question' in head else 'low-risk')
    items, decisions, cur = [], [], None
    for l in lines:
        m = re.match(r'^(?:\d+\.|-) \[`([^`]+)` L(\d+)(?:-L(\d+))?( \(removed lines\))?\]\([^)]*\) · (.+)$', l)
        if m:
            cur = {'path': m.group(1), 'line_start': int(m.group(2)), 'line_end': int(m.group(3) or m.group(2)),
                   'side': 'old' if m.group(4) else 'new', 'area': m.group(5), 'severity': 'medium', 'confidence': 9}
            items.append(cur)
            continue
        if cur is not None:
            for label, field in (('Changed', 'what'), ('Why it matters', 'why')):
                if l.strip().startswith(f'**{label}:**'):
                    cur[field] = l.split(':**', 1)[1].strip()
            m2 = re.match(r'^\s+\*\*(Ask [^:]+|Change):\*\* (.+)$', l)
            if m2:
                cur['kind'] = 'change' if m2.group(1) == 'Change' else 'question'
                cur['ask'] = m2.group(2).strip()
    skip = next((l.split(':**', 1)[1].strip() for l in lines if l.startswith('**Safe to skip:**')), '')
    reply = next((l.split(':**', 1)[1].strip() for l in lines if l.startswith('**Suggested reply')), '')
    rules = re.search(r'<summary>rules</summary>([^<]*)</details>', body)
    return {'outcome': outcome, 'decisions': decisions, 'items': items, 'safe_to_skip': skip, 'reply': reply,
            'rule_ids': [x.strip() for x in rules.group(1).split(',')] if rules else []}


def finish(cfg, key, entry, outcome, extra):
    beads = pr_state.Beads(cfg['city'], enabled=cfg['beads'])
    try:
        beads.set(entry['rig'], entry.get('bead'), head=entry.get('head'), outcome=outcome, close=True)
    except (RuntimeError, OSError):
        extra['bead_error'] = True
    with pr_state.locked(cfg['state']) as st:
        cur = st['prs'].get(key, entry)
        cur.update(entry)
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


def ci_escalation(cfg, key, entry, dry):
    """CI red, missing or skipped with no content rule: one question for the author, no model (R11). With
    a content rule the PR goes to the model and CI is the facts line. Closed PRs are dropped (R15)."""
    ruleset = pr_rules.load_rules(cfg['rules'])
    area_of = pr_rules.areas(ruleset)
    pr = pr_rules.get_pr(entry['repo'], entry['number'])
    if pr['state'] != 'OPEN':
        finish(cfg, key, entry, 'closed', {'model': False})
        return
    if pr['headRefOid'] in (entry.get('review_heads') or []):  # R18: this head was reviewed already
        finish(cfg, key, entry, entry.get('outcome') or 'skipped', {'model': False, 'posted': 'skipped-same-head'})
        return
    ci = pr_rules.ci_state(pr)
    files = pr_rules.pr_files(entry['repo'], pr['number'])
    base = pr_rules.base_for(pr, {pr['baseRefName']: pr_rules.branch_oid(entry['repo'], pr['baseRefName'])})
    hits = pr_rules.evaluate(pr, files, ruleset, cfg['reviewer'], entry['repo'], base)
    if ci not in ('red', 'none', 'skipped') or content_rules(hits, area_of, ruleset):
        return model_review(cfg, key, entry, dry)
    line = {'red': 'CI failed on this commit and nothing else is flagged; can you fix the failing checks?',
            'none': 'No CI ran on this commit and nothing else is flagged; can you run it?',
            'skipped': 'Every CI check on this commit was skipped and nothing else is flagged; can you run them?'}[ci]
    facts = pr_format.facts_line(pr, ci, ci_detail(pr))
    author = author_of(pr)
    args = (entry['repo'], pr, 'questions', [], [], '', '', facts, rule_ids(hits), NAME, author)
    body = pr_format.render(*args, single_line=line)
    reasons = pr_format.gate([], [], {}, 'questions', body, single_line=True)
    if dry:
        print(json.dumps({'pr': entry['url'], 'would': 'questions', 'ci': ci, 'gate': reasons}))
        return
    if reasons:
        finish(cfg, key, dict(entry, head=pr['headRefOid']), 'held-gate', {'model': False, 'gate': reasons})
        return
    result = {'outcome': 'questions', 'decisions': [], 'items': [], 'safe_to_skip': '', 'reply': '',
              'rule_ids': rule_ids(hits), 'single_line': line}
    deliver(cfg, key, entry, pr, 'questions', body, [], result, {'model': False, 'ci': ci, 'gate': []})


def model_review(cfg, key, entry, dry):
    repo = entry['repo']
    ruleset = pr_rules.load_rules(cfg['rules'])
    area_of = pr_rules.areas(ruleset)
    pr = pr_rules.get_pr(repo, entry['number'])
    if pr['state'] != 'OPEN':
        finish(cfg, key, entry, 'closed', {'model': False})
        return
    files_gh = pr_rules.pr_files(repo, pr['number'])
    base = pr_rules.base_for(pr, {pr['baseRefName']: pr_rules.branch_oid(repo, pr['baseRefName'])})
    hits = pr_rules.evaluate(pr, files_gh, ruleset, cfg['reviewer'], repo, base)
    ci = pr_rules.ci_state(pr)
    flagged = pr_rules.locate(files_gh, ruleset, content_rules(hits, area_of, ruleset))
    author = author_of(pr)
    run_id = f"{repo.replace('/', '__')}-{pr['number']}-{pr['headRefOid'][:10]}"
    mirror = None
    try:
        mirror, tree, ctx, diff, files, commits, fork = prepare(cfg, repo, pr, run_id)
        prompt = build_prompt(cfg, pr, diff, files, commits, fork, ctx, flagged)
        if dry:
            print(json.dumps({'pr': entry['url'], 'dry_run': True, 'prompt_chars': len(prompt),
                              'diff_chars': len(diff), 'files': len(files.splitlines()), 'rules': sorted(hits),
                              'flagged_hunks': len(flagged)}))
            return
        idx = pr_format.diff_index(diff)
        facts = pr_format.facts_line(pr, ci, ci_detail(pr))
        extra = {'model': True, 'cost_usd': 0, 'attempts': 0}
        reasons, model = [], None
        for attempt in (1, 2):
            p = prompt if attempt == 1 else (
                prompt + '\n\nYOUR PREVIOUS ANSWER FAILED THE STYLE GUIDE GATE. Fix every reason and answer again:\n'
                + '\n'.join(f'- {r}' for r in reasons) + '\n\nYour previous answer:\n' + json.dumps(model))
            model, meta = run_model(cfg, tree, ctx, p)
            extra['attempts'] = attempt
            extra['cost_usd'] += meta.get('cost_usd') or 0
            extra.update({k: v for k, v in meta.items() if k != 'cost_usd'})
            if model is None:
                finish(cfg, key, dict(entry, head=pr['headRefOid']), 'error', extra)
                return
            decisions = [d for d in model['decisions'] if d['confidence'] >= 8][:pr_format.MAX_DECISIONS]
            items = [i for i in model['items'] if i['confidence'] >= 8][:pr_format.MAX_ITEMS]
            outcome = pr_format.decide(decisions, items, ci)
            reply = model['reply_to_author'] if outcome != 'low-risk' else ''
            args = (repo, pr, outcome, decisions, items, model['safe_to_skip'], reply, facts, rule_ids(hits), NAME, author)
            body = pr_format.render(*args)
            comments = pr_format.inline_comments(items, author)
            links = [(d.get('link') or {}).get('path') for d in decisions if (d.get('link') or {}).get('kind') == 'code']
            reasons = pr_format.gate(decisions, items, idx, outcome, body, tree_lines(tree, links), pr.get('body') or '',
                                     comments=comments)
            if not reasons:
                break
            extra.setdefault('gate_first', [r.split(':')[0] for r in reasons])
        # Enum values, counts and rule ids only, so the log can say why reviews end where they do.
        extra.update(decisions=len(decisions), items=len(items),
                     dropped=len(model['items']) + len(model['decisions']) - len(items) - len(decisions),
                     kinds=sorted(i['kind'] for i in items), severity=sorted(i['severity'] for i in items),
                     checks=model['checks'], flagged_hunks=len(flagged), ci=ci,
                     gate=[r.split(':')[0] for r in reasons])
        if reasons:
            finish(cfg, key, dict(entry, head=pr['headRefOid']), 'held-gate', extra)
            return
        if leaks([body] + [c['body'] for c in comments]):
            finish(cfg, key, dict(entry, head=pr['headRefOid']), 'held-secret', extra)
            return
        result = {'outcome': outcome, 'decisions': decisions, 'items': items, 'safe_to_skip': model['safe_to_skip'],
                  'reply': reply, 'rule_ids': rule_ids(hits)}
        deliver(cfg, key, entry, pr, outcome, body, comments, result, extra)
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
            # CI-only escalations need no model and no cap (R11); a PR with a content rule waits its turn.
            ci_keys = [k for k, e in queued if e.get('ci') in ('red', 'none', 'skipped')
                       and not content_rules(e.get('rules', []), area_of, ruleset)]
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
    a = ap.parse_args()
    cfg = pr_state.config()
    if a.redeliver:
        redeliver(cfg, a.redeliver)
        return
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
    for k in chosen:
        ci_escalation(cfg, k, snap[k], a.dry_run)  # hands the PR to model_review unless only CI is wrong
    if not (chosen or ci_keys or expiring):
        print(json.dumps({'idle': True, 'reviews_today': used, 'cap': cfg['cap']}))


if __name__ == '__main__':
    main()
