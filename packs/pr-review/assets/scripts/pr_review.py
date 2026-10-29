#!/usr/bin/env python3
"""PR review run (README.md, "Review"): one queued PR per run, reviewed by a headless model that can
only read; this script posts the result.

  pr_review.py                  CI escalations (no model), then the next queued PR within the cap
  pr_review.py --pr REPO#N      review this PR now (queued or not; counts against the cap)
  pr_review.py --dry-run [--pr REPO#N]
                                pick, prepare and build the prompt; run no model, post nothing

The model runs in a git worktree at the PR head with Read, Grep and Glob only, no hooks, no MCP, no
settings from the repository, reads outside the worktree denied, no GitHub or Slack credential in its
environment, and LOOMTRACER_INGEST_DISABLE=1. It returns JSON. This script turns that into one
outcome, scans the text for credentials, and posts: a PENDING review in shadow mode (only the reviewer
sees it), the outcome's review event in live mode. Slack gets the outcome word and the link only.
What it prints and keeps is ids, links, outcomes, counts and cost: never the repository's text.
"""
import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_rules  # noqa: E402
import pr_state  # noqa: E402

HERE = pr_state.HERE
UPSTREAM = os.path.join(HERE, '..', 'upstream', 'security-review.md')
MAX_DIFF_CHARS = 350_000
# The disclosure every review opens with; PR_REVIEW_REVIEWER_NAME names the person (default: the login).
NAME = os.environ.get('PR_REVIEW_REVIEWER_NAME') or os.environ.get('PR_REVIEW_REVIEWER') or 'the reviewer'
HONEST = (f"**Automated review** by {NAME}'s review agents (Claude Code). "
          f"{NAME} has not read this PR unless they say so.")
NOT_CHECKED = 'Not checked: billing, plans, credits, pricing.'
EVENTS = {'low-risk': 'APPROVE', 'questions': 'COMMENT', 'changes': 'REQUEST_CHANGES', 'escalate': None}

SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['outcome', 'summary', 'findings', 'questions', 'evidence_requested', 'checks'],
    'properties': {
        'outcome': {'type': 'string', 'enum': ['low-risk', 'questions', 'changes', 'escalate']},
        'summary': {'type': 'string', 'description': 'Two to four sentences: what the PR changes and the verdict.'},
        'findings': {'type': 'array', 'maxItems': 5, 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['file', 'line', 'severity', 'area', 'problem', 'how_it_fails', 'checked', 'confidence'],
            'properties': {
                'file': {'type': 'string', 'description': 'Path relative to the repository root.'},
                'line': {'type': 'integer', 'minimum': 1, 'description': 'Line in the file at the PR head.'},
                'severity': {'type': 'string', 'enum': ['high', 'medium', 'low']},
                'area': {'type': 'string', 'enum': ['PHI', 'security', 'irreversible', 'behaviour', 'description', 'other']},
                'problem': {'type': 'string'},
                'how_it_fails': {'type': 'string'},
                'checked': {'type': 'string', 'description': 'What in the code you read to confirm it.'},
                'confidence': {'type': 'integer', 'minimum': 1, 'maximum': 10}}}},
        'questions': {'type': 'array', 'maxItems': 5, 'items': {'type': 'string'}},
        'evidence_requested': {'type': 'array', 'maxItems': 3, 'items': {'type': 'string'}},
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
directory is the repository at the PR head. Do the three steps yourself:

1. Identify vulnerabilities, and every issue the custom scan instructions above ask about. Use the
   repository to check context: callers, schemas, configuration, existing migrations.
2. For each candidate, apply the FALSE POSITIVE FILTERING rules and give it a confidence from 1 to 10.
3. Drop every candidate below 8. Keep at most 5 findings, worst first. No style nitpicks.

Then pick ONE outcome:
- low-risk: no findings, no open questions, and what the custom scan instructions ask about is absent
  or clearly fine.
- questions: you cannot judge without answers or evidence (the ticket, screenshots, test output). Put
  specific questions in `questions` and the evidence you need in `evidence_requested`.
- changes: findings the author should fix. Each names a file and a line, how it fails, and what you
  read in the code to confirm it.
- escalate: anything serious (a HIGH finding, PHI exposure, a destructive or irreversible step, a broken
  migration order, CI red), or a call only the human reviewer can make.

Your answer is the JSON object of the schema you were given: it replaces the markdown output format
described above, and each vulnerability becomes one entry in `findings`. Fill `checks` for B3. Write
for the PR's author: plain words, no praise, no filler. Do not write what was not checked: the posted
review adds that line itself. Never copy secrets, tokens or personal data into
your answer, even if you saw them in the code; point at them by file and line instead.
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


def build_prompt(cfg, pr, diff, files, commits, fork, ctx, rule_labels):
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
    flags = ('ZERO-TOKEN FLAGS (pattern matches on paths and added lines, not judgements; check each):\n'
             + ('\n'.join(f'- {x}' for x in rule_labels) or '- none'))
    return f'{head}{checklist}\n\n{flags}\n\n{FINAL}'


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
    meta.update(cost_usd=out.get('total_cost_usd'), turns=out.get('num_turns'), subtype=out.get('subtype'),
                denials=len(out.get('permission_denials') or []))
    so = out.get('structured_output')
    if out.get('is_error') or not isinstance(so, dict):
        return None, {**meta, 'error': out.get('subtype') or 'no-structured-output'}
    so['summary'] = clean(so['summary'])
    so['questions'] = [clean(q) for q in so['questions']]
    so['evidence_requested'] = [clean(q) for q in so['evidence_requested']]
    for f in so['findings']:
        for k in ('problem', 'how_it_fails', 'checked'):
            f[k] = clean(f[k])
    return so, meta


def changed_lines(diff):
    """{path: set of new-side line numbers inside the diff's hunks}: where an inline comment may go."""
    out, path, line = {}, None, 0
    for raw in diff.splitlines():
        if raw.startswith('+++ '):
            path = raw[6:] if raw.startswith('+++ b/') else None
            continue
        m = re.match(r'@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', raw)
        if m:
            line = int(m.group(1))
            continue
        if path is None or raw.startswith(('---', 'diff ', 'index ')):
            continue
        if raw.startswith('+') or raw.startswith(' '):
            out.setdefault(path, set()).add(line)
            line += 1
    return out


def decide(model, findings, sensitive, ci):
    """One outcome from the model's verdict and the rules (README.md, "Outcomes")."""
    m = model['outcome']
    if m == 'escalate' or any(f['severity'] == 'high' for f in findings) or ci in ('red', 'none', 'skipped'):
        return 'escalate', None
    if findings:
        return ('changes' if m == 'changes' or any(f['severity'] == 'medium' for f in findings) else 'questions'), None
    if m == 'changes':  # changes without a finding that names a file and line (B1) cannot stand
        return 'questions', None
    if m == 'questions' and (model['questions'] or model['evidence_requested']):
        return 'questions', None
    if sensitive:
        names = {'A1': 'patient data (A1)', 'A2': 'security (A2)', 'A4': 'what cannot be undone (A4)'}
        return 'escalate', ('The model found nothing, but the rules flag ' + ', '.join(names.get(a, a) for a in sensitive)
                            + '. Approval as low risk needs both the rules and the model to agree, so this one is yours.')
    return 'low-risk', None


def body_for(cfg, pr, outcome, model, findings, comments_inline, note, rule_labels, ci, extra=None):
    word = pr_state.OUTCOMES[outcome][1 if cfg['mode'] == 'live' else 0]
    lines = [HONEST]
    if cfg['mode'] == 'shadow':
        lines.append(f'Shadow mode: this review is pending and only {NAME} sees it, to submit or discard.')
    when = ' Reviewed after the merge.' if pr['state'] == 'MERGED' else ''
    lines += ['', f"**{word}** · head `{pr['headRefOid'][:10]}`.{when}"]
    if note:
        lines += ['', note]
    if extra:
        lines += ['', extra]
    if model:
        lines += ['', model['summary'].strip()]
        if findings:
            lines += ['', '**Findings**, worst first:']
            for i, f in enumerate(findings, 1):
                where = ' (inline)' if (f['file'], f['line']) in comments_inline else ''
                lines.append(f"{i}. `{f['file']}:{f['line']}`{where}, {f['severity']}, {f['area']}: {f['problem'].strip()} "
                             f"How it fails: {f['how_it_fails'].strip()} Checked: {f['checked'].strip()}")
        if model['questions']:
            lines += ['', '**Questions**:'] + [f'- {q.strip()}' for q in model['questions']]
        if model['evidence_requested']:
            lines += ['', '**Evidence wanted**:'] + [f'- {q.strip()}' for q in model['evidence_requested']]
        c = model['checks']
        lines += ['', f"Checks (B3): description matches the diff: {c['description_matches_diff']}; behaviour "
                      f"changes have tests: {c['behaviour_changes_have_tests']}; migrations reversible and in "
                      f"order: {c['migrations_reversible_and_ordered']}."]
    lines += ['', f"Zero-token flags: {', '.join(rule_labels) or 'none'}. CI on this head: {ci}.", NOT_CHECKED]
    return '\n'.join(lines)


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


def post_review(cfg, repo, pr, outcome, body, comments):
    """A pending review (shadow, or escalate in live) or the outcome's event (live). Returns 'ok' or an
    error code. On a merged PR a live approve or request-changes is posted as a comment."""
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


def ci_escalation(cfg, key, entry, dry):
    """A7 without a model: CI red, missing or all skipped on the head -> escalate."""
    pr = pr_rules.get_pr(entry['repo'], entry['number'])
    ci = pr_rules.ci_state(pr)
    ruleset = pr_rules.load_rules(cfg['rules'])
    labels = rule_labels_for(entry['rules'], ruleset)
    roll = (pr['commits']['nodes'][0]['commit'].get('statusCheckRollup') or {}) if pr['commits']['nodes'] else {}
    bad = [c.get('name') or c.get('context') for c in (roll.get('contexts') or {}).get('nodes', [])
           if (c.get('conclusion') or c.get('state')) in pr_rules.CI_RED]
    detail = {'red': f"CI failed on this head: {', '.join(bad[:8]) or 'see the checks tab'}.",
              'none': 'No check or status ran on this head.',
              'skipped': 'Every check on this head was skipped or neutral; none passed.'}.get(ci)
    if detail is None:  # CI recovered since the sweep: back in the queue for a model review
        with pr_state.locked(cfg['state']) as st:
            st['prs'][key].update(status='queued', ci=ci)
        return
    body = body_for(cfg, pr, 'escalate', None, [], set(), None, labels, ci,
                    extra=f'{detail} A7: CI red or skipped means escalate. No model review was run.')
    if dry:
        print(json.dumps({'pr': entry['url'], 'would': 'escalate', 'ci': ci, 'body_chars': len(body)}))
        return
    posted = post_review(cfg, entry['repo'], pr, 'escalate', body, [])
    slack = pr_state.slack_post(cfg, 'escalate', entry['url']) if posted.startswith('ok') else 'skipped'
    finish(cfg, key, dict(entry, head=pr['headRefOid']), 'escalate',
           {'model': False, 'posted': posted, 'slack': slack, 'ci': ci})


def rule_labels_for(rule_ids, ruleset):
    labels = {r['id']: r['label'] for r in ruleset['rules']}
    labels.update({k: v[1] for k, v in pr_rules.BUILTIN.items()})
    return [f'{i} ({labels[i]})' for i in rule_ids if i in labels and i not in ('no-description',)]


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
    sensitive = sorted({area_of[h] for h in hits if area_of.get(h) in ruleset['select_areas']})
    labels = rule_labels_for(sorted(hits), ruleset)
    run_id = f"{repo.replace('/', '__')}-{pr['number']}-{pr['headRefOid'][:10]}"
    mirror = None
    try:
        mirror, tree, ctx, diff, files, commits, fork = prepare(cfg, repo, pr, run_id)
        prompt = build_prompt(cfg, pr, diff, files, commits, fork, ctx, labels)
        if dry:
            print(json.dumps({'pr': entry['url'], 'dry_run': True, 'prompt_chars': len(prompt),
                              'diff_chars': len(diff), 'files': len(files.splitlines()), 'rules': sorted(hits)}))
            return
        model, meta = run_model(cfg, tree, ctx, prompt)
        if model is None:
            finish(cfg, key, dict(entry, head=pr['headRefOid']), 'error', {'model': True, **meta})
            return
        in_diff = changed_lines(diff)
        findings = [f for f in model['findings'] if f['confidence'] >= 8 and f['file']
                    and (os.path.isfile(os.path.join(tree, f['file'])) or f['file'] in in_diff)][:5]
        outcome, note = decide(model, findings, [a for a in sensitive if a != 'A7'], ci)
        inline = {(f['file'], f['line']) for f in findings if f['line'] in in_diff.get(f['file'], set())}
        comments = [{'path': f['file'], 'line': f['line'], 'side': 'RIGHT',
                     'body': f"**{f['severity']}**, {f['area']}: {f['problem'].strip()}\n\nHow it fails: "
                             f"{f['how_it_fails'].strip()}"} for f in findings if (f['file'], f['line']) in inline]
        body = body_for(cfg, pr, outcome, model, findings, inline, note, labels, ci)
        extra = {'model': True, 'findings': len(findings), 'dropped': len(model['findings']) - len(findings), **meta}
        if leaks([body] + [c['body'] for c in comments]):
            outcome, comments = 'escalate', []
            body = '\n'.join([HONEST, '', '**Needs you**: the review text was withheld because it matched a '
                              'credential or personal-data pattern. Review this PR by hand.', '', NOT_CHECKED])
            extra['withheld'] = True
        posted = post_review(cfg, repo, pr, outcome, body, comments)
        slack = pr_state.slack_post(cfg, outcome, entry['url']) if posted.startswith('ok') else 'skipped'
        finish(cfg, key, dict(entry, head=pr['headRefOid']), outcome, {**extra, 'posted': posted, 'slack': slack})
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
            ci_keys = [k for k, e in queued if e.get('ci') in ('red', 'none', 'skipped')]
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
        model_review(cfg, k, snap[k], a.dry_run)
    if not (chosen or ci_keys or expiring):
        print(json.dumps({'idle': True, 'reviews_today': used, 'cap': cfg['cap']}))


if __name__ == '__main__':
    main()
