#!/usr/bin/env python3
"""PR review run (README.md, "Review"): one queued PR per run, reviewed by a headless model that can
only read; this script renders, gates and posts the result.

  pr_review.py                  CI-only escalations (no model), then the next queued PR within the cap
  pr_review.py --pr REPO#N      review this PR now (queued or not; replaces this pack's own pending review)
  pr_review.py --dry-run [--pr REPO#N]
                                pick, prepare and build the prompt; run no model, post nothing

The model runs in a git worktree at the PR head with Read, Grep and Glob only, no hooks, no MCP, no
settings from the repository, reads outside the worktree denied, no GitHub or Slack credential in its
environment, and LOOMTRACER_INGEST_DISABLE=1. It returns line-anchored items as JSON. This script
decides the outcome, renders the review in the style guide's shape (assets/style-guide.md), and runs
the gate (pr_format.py): a review that fails is regenerated once with the gate's reasons, and if it
fails again nothing is posted ("held-gate"). Then it scans for credentials and posts: a PENDING review
in shadow mode (only the reviewer sees it), the outcome's review event in live mode. Slack gets the
outcome word and the link only. What it prints and keeps is ids, links, outcomes, counts, gate reasons
and cost: never the repository's text.
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
EVENTS = {'low-risk': 'APPROVE', 'questions': 'COMMENT', 'changes': 'REQUEST_CHANGES', 'escalate': None}

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['items', 'safe_to_skip', 'checks'],
    'properties': {
        'items': {'type': 'array', 'maxItems': 5, 'description': 'Worst first. Empty only when nothing needs a person.',
                  'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['path', 'side', 'line_start', 'line_end', 'kind', 'severity', 'area', 'what', 'why', 'ask',
                         'verified', 'confidence'],
            'properties': {
                'path': {'type': 'string', 'description': 'File path as in the diff.'},
                'side': {'type': 'string', 'enum': ['new', 'old'],
                         'description': 'new = lines of the PR head; old = removed lines (base side).'},
                'line_start': {'type': 'integer', 'minimum': 1},
                'line_end': {'type': 'integer', 'minimum': 1},
                'kind': {'type': 'string', 'enum': ['check', 'question', 'change'],
                         'description': 'check = the reviewer decides; question = ask the author; change = the author must fix.'},
                'severity': {'type': 'string', 'enum': ['high', 'medium', 'low']},
                'area': {'type': 'string', 'enum': pr_format.AREAS},
                'what': {'type': 'string', 'description': 'One sentence: what these lines change.'},
                'why': {'type': 'string', 'description': 'One sentence: why it matters, naming the area in words.'},
                'ask': {'type': 'string', 'description': 'What the reviewer must decide or check, or the exact question or evidence to ask the author for.'},
                'verified': {'type': 'string', 'description': 'What in the code you read to confirm it.'},
                'confidence': {'type': 'integer', 'minimum': 1, 'maximum': 10}}}},
        'safe_to_skip': {'type': 'string', 'description': 'One line: what the rest of the diff is, e.g. "14 files: tests, UI copy, types".'},
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
directory is the repository at the PR head. Do these steps yourself:

1. Read the diff and the code around it (callers, schemas, configuration, existing migrations). Find the
   vulnerabilities, and every issue the custom scan instructions above ask about.
2. For each candidate, apply the FALSE POSITIVE FILTERING rules and give it a confidence from 1 to 10;
   drop anything below 8.
3. Go through each FLAGGED HUNK listed above. Either it holds a real issue (an item of kind "change" or
   "check"), or you find nothing wrong there: then it is still an item of kind "check" that says what
   the hunk does, why the flag matters, what the reviewer should look at, and that you found no problem.
4. Keep at most 5 items, worst first. Anchor each to the exact lines in the diff: `path` as in the diff,
   `side` "new" for lines of the PR head or "old" for removed lines, and a range inside one hunk.

Your answer is the JSON object of the schema you were given; it replaces the markdown output format
described above. You do not pick the outcome: the script derives it from your items (any "check" or a
high item means the reviewer is needed; "change" items mean changes are requested; only "question" items
mean questions for the author; no items means low risk). Follow the STYLE GUIDE above to the letter:
one sentence each for `what` and `why`, an exact decision or question in `ask`, no filler, no rule codes,
no praise. `safe_to_skip` says in one line what the rest of the diff is. Never copy secrets, tokens or
personal data into your answer; point at them by path and line instead.
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
    status = (f"Pull request {pr['url']}, head {pr['headRefOid']}, forked from the base branch at {fork}. "
              f"{'Already merged.' if pr['state'] == 'MERGED' else 'Open.'}\n"
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
        text = text.replace(old, new.replace('```', "'''"), 1)
    head, sep, _ = text.partition('START ANALYSIS:')
    if not sep:
        raise RuntimeError('upstream prompt changed: START ANALYSIS missing')
    checklist = open(cfg['checklist']).read()
    style = open(STYLE).read()
    hunks = '\n'.join(f"- {f['path']} near line {f['line']} (new side): {f['label']}" for f in flagged) or '- none'
    return (f'{head}{checklist}\n\nSTYLE GUIDE (how your items will be rendered and checked):\n\n{style}\n\n'
            f'FLAGGED HUNKS (pattern matches on the diff, not judgements; each needs an item, see step 3):\n'
            f'{hunks}\n\n{FINAL}')


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
    for it in so['items']:
        for k in ('what', 'why', 'ask', 'verified'):
            it[k] = clean(it[k])
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
    """Hash of a review body as GitHub returns it (line endings and outer space normalized)."""
    return hashlib.sha256(body.replace('\r\n', '\n').strip().encode()).hexdigest()


def own_pending(repo, number, reviewer):
    """(this pack's pending review id or None, why to skip or None). Only one pending review per user is
    allowed: ours is replaced; a review the reviewer submitted or edited is left alone."""
    out = subprocess.run(['gh', 'api', '--paginate', f'repos/{repo}/pulls/{number}/reviews?per_page=100', '--jq',
                          '.[] | {id, state, login: .user.login, body}'], capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        return None, 'reviews-unreadable'
    mine = [json.loads(l) for l in out.stdout.splitlines() if l.strip()]
    mine = [r for r in mine if r['login'].lower() == reviewer.lower()]
    if any(r['state'] != 'PENDING' for r in mine):
        return None, 'submitted'
    for r in mine:
        if pr_format.MARKER not in (r['body'] or ''):
            return None, 'edited'
        return r['id'], None
    return None, None


def post_review(cfg, repo, pr, outcome, body, comments, posted_sha=None):
    """Replace this pack's pending review, then post: a pending review (shadow, or escalate in live) or
    the outcome's event (live). Returns 'ok', 'ok-no-inline', 'skipped-submitted', 'skipped-edited' or an
    error code. On a merged PR a live approve or request-changes is posted as a comment."""
    old, skip = own_pending(repo, pr['number'], cfg['reviewer'])
    if skip:
        return f'skipped-{skip}'
    if old:
        cur = subprocess.run(['gh', 'api', f"repos/{repo}/pulls/{pr['number']}/reviews/{old}", '--jq', '.body'],
                             capture_output=True, text=True, timeout=60).stdout.rstrip('\n')
        if posted_sha and body_sha(cur) != posted_sha:
            return 'skipped-edited'
        d = subprocess.run(['gh', 'api', '-X', 'DELETE', f"repos/{repo}/pulls/{pr['number']}/reviews/{old}"],
                           capture_output=True, text=True, timeout=60)
        if d.returncode != 0:
            return 'error:replace'
    payload = {'commit_id': pr['headRefOid'], 'body': body}
    event = EVENTS[outcome] if cfg['mode'] == 'live' else None
    if event and pr['state'] != 'OPEN':
        event = 'COMMENT'
    if event:
        payload['event'] = event
    for with_comments in ((True, False) if comments else (False,)):
        p = dict(payload, comments=comments) if with_comments else payload
        r = subprocess.run(['gh', 'api', '-X', 'POST', f"repos/{repo}/pulls/{pr['number']}/reviews", '--input', '-'],
                           input=json.dumps(p), capture_output=True, text=True, timeout=120)
        if r.returncode == 0:
            return 'ok' if with_comments or not comments else 'ok-no-inline'
        if 'pending review' in (r.stdout + r.stderr).lower():
            return 'pending-exists'
    code = re.search(r'HTTP (\d{3})', r.stderr)
    return f"error:{code.group(1) if code else r.returncode}"


def finish(cfg, key, entry, outcome, extra):
    beads = pr_state.Beads(cfg['city'], enabled=cfg['beads'])
    try:
        beads.set(entry['rig'], entry.get('bead'), head=entry['head'], outcome=outcome, close=True)
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


def ci_escalation(cfg, key, entry, dry):
    """A7 without a model, only when no content rule fired (R11): one sentence. Otherwise the PR goes to
    a model review, where CI is a fact line."""
    ruleset = pr_rules.load_rules(cfg['rules'])
    area_of = pr_rules.areas(ruleset)
    pr = pr_rules.get_pr(entry['repo'], entry['number'])
    ci = pr_rules.ci_state(pr)
    files = pr_rules.pr_files(entry['repo'], pr['number'])
    base = pr_rules.base_for(pr, {pr['baseRefName']: pr_rules.branch_oid(entry['repo'], pr['baseRefName'])})
    hits = pr_rules.evaluate(pr, files, ruleset, cfg['reviewer'], entry['repo'], base)
    if ci not in ('red', 'none', 'skipped') or content_rules(hits, area_of, ruleset):
        return model_review(cfg, key, entry, dry)
    line = {'red': 'CI failed on this commit; nothing else flagged.',
            'none': 'No CI ran on this commit; nothing else flagged.',
            'skipped': 'Every CI check on this commit was skipped; nothing else flagged.'}[ci]
    facts = pr_format.facts_line(pr, ci, ci_detail(pr))
    body, comments = pr_format.render(entry['repo'], pr, 'escalate', [], '', facts, rule_ids(hits), NAME,
                                      NAMES.get((pr.get('author') or {}).get('login'), 'the author'), single_line=line)
    reasons = pr_format.gate([], {}, [], 'escalate', body, comments, single_line=True)
    if dry:
        print(json.dumps({'pr': entry['url'], 'would': 'escalate', 'ci': ci, 'gate': reasons}))
        return
    if reasons:
        finish(cfg, key, dict(entry, head=pr['headRefOid']), 'held-gate', {'model': False, 'gate': reasons})
        return
    post_and_finish(cfg, key, entry, pr, 'escalate', body, comments, {'model': False, 'ci': ci, 'gate': []})


def post_and_finish(cfg, key, entry, pr, outcome, body, comments, extra):
    posted = post_review(cfg, entry['repo'], pr, outcome, body, comments, entry.get('body_sha'))
    slack, ts = ('skipped', None)
    if posted.startswith('ok'):
        pr_state.keep_posted(cfg, entry, pr['headRefOid'], body, comments)
        slack, ts = pr_state.slack_post(cfg, outcome, entry['url'])
        extra['body_sha'] = body_sha(body)
        extra['posted_at'] = pr_state.iso(pr_state.now())
        if ts:
            extra['slack_ts'] = ts
    finish(cfg, key, dict(entry, head=pr['headRefOid']), outcome if posted.startswith('ok') else
           ('skipped' if posted.startswith('skipped') else 'error'), {**extra, 'posted': posted, 'slack': slack})


def model_review(cfg, key, entry, dry):
    repo = entry['repo']
    ruleset = pr_rules.load_rules(cfg['rules'])
    area_of = pr_rules.areas(ruleset)
    pr = pr_rules.get_pr(repo, entry['number'])
    if pr['state'] == 'CLOSED':
        finish(cfg, key, entry, 'closed', {'model': False})
        return
    files_gh = pr_rules.pr_files(repo, pr['number'])
    base = pr_rules.base_for(pr, {pr['baseRefName']: pr_rules.branch_oid(repo, pr['baseRefName'])})
    hits = pr_rules.evaluate(pr, files_gh, ruleset, cfg['reviewer'], repo, base)
    ci = pr_rules.ci_state(pr)
    flagged = pr_rules.locate(files_gh, ruleset, content_rules(hits, area_of, ruleset))
    author = NAMES.get((pr.get('author') or {}).get('login'), (pr.get('author') or {}).get('login') or 'the author')
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
            items = [i for i in model['items'] if i['confidence'] >= 8][:pr_format.MAX_ITEMS]
            outcome = pr_format.decide(items, ci, bool(flagged))
            body, comments = pr_format.render(repo, pr, outcome, items, model['safe_to_skip'], facts,
                                              rule_ids(hits), NAME, author)
            reasons = pr_format.gate(items, idx, [f['path'] for f in flagged], outcome, body, comments)
            if not reasons:
                break
            extra.setdefault('gate_first', [r.split(':')[0] for r in reasons])
        # Enum values, counts and rule ids only, so the log can say why reviews end where they do.
        extra.update(items=len(items), dropped=len(model['items']) - len(items),
                     kinds=sorted(i['kind'] for i in items), severity=sorted(i['severity'] for i in items),
                     checks=model['checks'], flagged_hunks=len(flagged), ci=ci,
                     gate=[r.split(':')[0] for r in reasons])
        if reasons:
            finish(cfg, key, dict(entry, head=pr['headRefOid']), 'held-gate', extra)
            return
        if leaks([body] + [c['body'] for c in comments]):
            finish(cfg, key, dict(entry, head=pr['headRefOid']), 'held-secret', extra)
            return
        post_and_finish(cfg, key, entry, pr, outcome, body, comments, extra)
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
            rest = sorted(((k, e) for k, e in queued if k not in ci_keys),
                          key=lambda ke: (ke[1]['tier'], -len(ke[1].get('areas') or []), ke[1]['created']))
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
    a = ap.parse_args()
    cfg = pr_state.config()
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
