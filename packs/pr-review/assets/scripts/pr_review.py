#!/usr/bin/env python3
"""PR review run (README.md, "Review"): one queued open PR per run, reviewed by a headless model that can
only read; this script renders, gates and delivers the result.

  pr_review.py                  CI-only questions (no model), then the next queued PR within the cap
  pr_review.py --pr REPO#N      review this PR now (queued or not; counts against the cap)
  pr_review.py --dry-run [--pr REPO#N]
                                pick, prepare and build the prompt; run no model, deliver nothing

Delivery (the reviewer, 2026-10-29 20:45Z and 21:01Z): every reviewed open PR gets ONE SUBMITTED review on GitHub
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
  pr_review.py --followup REPO#N [--dry-run | --no-deliver]
                                    the same-head follow-up on the author's answers (decision-57b6d9bb;
                                    the sweep queues it by itself): --dry-run prints the counts, no model
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
import pr_client_slack  # noqa: E402
import pr_context  # noqa: E402
import pr_digest  # noqa: E402
import pr_fallback  # noqa: E402
import pr_followup  # noqa: E402
import pr_format  # noqa: E402
import pr_incremental  # noqa: E402
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
# The style guide's parts the review model never writes (the mayor, 2026-10-30 22:22Z: keep the first-review
# prompt byte-identical to the benchmark's pin): marked in the guide, cut from the prompt.
SCRIPT_ONLY_RE = re.compile(r'<!-- not in the review prompt -->\n.*?<!-- /not in the review prompt -->\n', re.S)
MAX_DIFF_CHARS = 350_000
# The footer names the person (default: the login); PR_REVIEW_NAMES maps authors' logins to names.
NAME = os.environ.get('PR_REVIEW_REVIEWER_NAME') or os.environ.get('PR_REVIEW_REVIEWER') or 'the reviewer'
NAMES = dict(x.split('=', 1) for x in os.environ.get('PR_REVIEW_NAMES', '').split() if '=' in x)

_TEXT = {'type': 'string'}
SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['decisions', 'items', 'cleared', 'not_checked_lines', 'summary', 'skip', 'checks', 'gaps'],
    'properties': {
        'decisions': {'type': 'array', 'maxItems': 3, 'description': (
            'Choices for the author: a rule, product, risk or compliance trade-off they must settle, with '
            'options. Empty when there is none.'), 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['question', 'options', 'actors', 'recommended', 'reason', 'why', 'area', 'rule', 'link',
                         'verified', 'confidence'],
            'properties': {
                'question': {'type': 'string', 'maxLength': 280, 'description': 'One question ending in "?", at most 280 characters.'},
                'options': {'type': 'array', 'minItems': 2, 'maxItems': 4, 'items': _TEXT,
                            'description': 'Each one sentence that starts with who carries it out.'},
                'actors': {'type': 'array', 'minItems': 2, 'maxItems': 4,
                           'items': {'type': 'string', 'enum': ['author', 'picked']},
                           'description': 'For each option, in order, who carries it out: author, or picked '
                                          '(someone the author picks: a teammate, the owner of billing).'},
                'recommended': {'type': 'integer', 'minimum': 0, 'maximum': 3,
                                'description': 'The index of the option you recommend (0 = the first).'},
                'reason': {'type': 'string', 'maxLength': 200, 'description': 'One short sentence: why that option.'},
                'rule': {'type': 'string', 'description': 'The id of the repository rule this is about (e.g. "D3"), or "" when none is.'},
                'why': {'type': 'string', 'maxLength': 280, 'description': 'One sentence, at most 280 characters: why the author must settle this.'},
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
        'gaps': {'type': 'array', 'maxItems': 2, 'description': (
            'Gaps in this review\'s method that the author cannot close: never a finding about the PR. Empty '
            'when there is none.'), 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['kind', 'rule', 'why'],
            'properties': {
                'kind': {'type': 'string', 'enum': ['unverifiable-rule', 'new-risk'],
                         'description': 'unverifiable-rule = a repository rule this PR touches that no reading of '
                                        'the code can verify; new-risk = a kind of risk these instructions do not cover.'},
                'rule': {'type': 'string', 'description': 'The rule id for unverifiable-rule, else "".'},
                'why': {'type': 'string', 'maxLength': 200, 'description': 'One sentence: what the method cannot check.'}}}},
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

{since_step}1. Open every FLAGGED HUNK listed above (F1, F2, ...) in its file, and open its callers and the code it
   calls (Grep for the changed functions and types). Do not write an entry about code you have not opened;
   name what you opened in `verified`.{context_step}
2. Read the rest of the diff the same way, with the repository's rules at hand. Find where the PR goes
   against a rule or a goal, and every defect and issue the instructions above and the custom scan
   instructions ask about. Apply their filtering rules, give each candidate a confidence from 1 to 10,
   and drop anything below 8.
3. Sort what is left into these lists:
   - `decisions`: choices for {author}, at the level of goals and rules, never of code: a trade-off they
     must settle, with options. For example: "Rule D3 says X; this PR does Y. Change it to follow the rule,
     or put on record why it is an exception?" A product, risk or compliance trade-off (whether patient
     data may go to a vendor, whether a destructive step may run in production) is one too: ask for the
     evidence (the vendor terms, the migration plan). Every finding about the PR goes to {author}, never
     to {name}. Each is ONE question ending in "?", with 2 to 4 options, `recommended` (the index of the
     option you recommend) and `reason` (one short sentence on why), `why` (why {author} must settle it),
     `rule`, and a link to where it lives: lines of a file at the head ("code"), the PR description
     ("description"), or a ticket URL that appears in the description ("ticket"). A plain code fix is an
     item, not a decision. Each option is one sentence that starts with who carries it out, and `actors`
     says who that is, in order: "author" or "picked" (someone {author} picks: a teammate, the owner of
     billing). For example {author_option} (author), "A teammate who owns billing confirms it"
     (picked).
   - `items` for {author}: a "change" they must make or a "question" they must answer, each anchored to
     lines inside one hunk of the diff (`side` "new" for the PR head, "old" for removed lines). `change`
     is 3-6 plain words on what the PR changed there; `why` is the one fact that makes it matter (at most
     25 words; it goes in the inline comment) and `ask` the one thing to do (at most 20 words; it stands
     in the body). A question whose own `why` already answers it is not a question: make it a cleared
     entry when its lines are a flagged hunk, else leave it out. Say each finding once: never two items on the same lines.{suggest_step}
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
   - `gaps`: only a gap in this review's method that {author} cannot close: a repository rule this PR
     touches that no reading of the code can verify ("unverifiable-rule", with its id), or a kind of risk
     these instructions do not cover ("new-risk"). Never a finding about the PR: that is an item or a
     decision for {author}. Usually empty.
4. Keep at most 3 decisions and 5 items, worst first. Write `summary`, one sentence on what the PR does,
   and `skip`, the files that need no look, by group. A change in product behaviour never goes in `skip`:
   it is a cleared entry that says it is intended and its default is safe, or a decision. `change` is
   plain words about the line, never an identifier (no function, variable or type names).

Your answer is the JSON object of the schema you were given; it replaces the markdown output format
described above. You do not pick the outcome: the script derives it (a change means changes are
requested; only questions and decisions means questions for {author}; nothing means low risk). Follow the STYLE GUIDE above to the letter: plain
words, no filler, no rule codes or flag ids in any text (cite a repository rule only through `rule`),
no praise, and no link to any site but github.com (write the sentence without it). Never copy secrets,
tokens or personal data into your answer; point at them by path and line instead.
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


def build_prompt(cfg, pr, diff, files, commits, fork, ctx, flagged, not_checked, digest_block='', author='the author',
                 context_block='', since_block=''):
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
    style = SCRIPT_ONLY_RE.sub('', open(STYLE).read())  # the parts the script writes stay out (byte-identical prompt)
    idx = pr_format.diff_index(diff)
    hunks = '\n'.join(f"- {f['id']}: {f['path']} line {f['line']} (new side), hunk {hunk_span(idx, f)}: {f['label']}"
                      for f in flagged) or '- none'
    final = FINAL.replace('{not_checked}', not_checked or 'billing, plans, credits or pricing')
    final = final.replace('{name}', NAME).replace('{author}', author)
    final = final.replace('{author_option}', '"You change it to follow the rule"' if cfg.get('voice') == 'reviewer'
                          else f'"{author} changes it to follow the rule"')
    final = final.replace('{context_step}', CONTEXT_STEP if context_block else '')
    final = final.replace('{since_step}', pr_incremental.STEP if since_block else '')
    final = final.replace('{suggest_step}', SUGGEST_STEP.replace('{author}', author) if cfg.get('suggestions') else '')
    return (f'{head}{checklist}\n\nSTYLE GUIDE (how your answer will be rendered and checked):\n\n{style}\n\n'
            f'{digest_block}\n\n'
            + (f'{context_block}\n\n' if context_block else '') + (f'{since_block}\n\n' if since_block else '') +
            f'FLAGGED HUNKS (pattern matches on the diff, not findings; open each one, step 1; each needs a '
            f'decision, an item or a cleared entry, step 3):\n'
            f'{hunks}\n\nVALID ITEM RANGES (an item\'s line_start and line_end must both fall inside ONE of these '
            f'ranges on its side, or the review is rejected):\n{hunk_ranges(diff)}\n\n'
            f"{final}")


# Step 1's second half when the prompt carries REPOSITORY CONTEXT (PR_REVIEW_CONTEXT=on, gc-sy1hm.2 a).
CONTEXT_STEP = """ Then open the places REPOSITORY CONTEXT lists for the
   changed code: check that each use still gets what it expects from it (arguments, return values,
   errors, empty or missing values) and that each listed test still describes the new behaviour."""


# The items' one-click fix (PR_REVIEW_SUGGESTIONS=on, gc-sy1hm.2 b): a field of the schema and this sentence.
SUGGEST_STEP = """
     When a "change" is fixed by rewriting exactly its own lines, put the rewritten lines in `suggestion`:
     the whole replacement for line_start-line_end at the PR head, with their indentation, at most 12
     lines, only code you have checked is correct; {author} applies it with one click. Otherwise ""."""
SUGGESTION = {'type': 'string', 'maxLength': 2000, 'description': (
    'Only for a "change" on the new side whose fix rewrites exactly lines line_start-line_end: the whole '
    'replacement for those lines at the PR head, with their indentation, at most 12 lines. "" otherwise.')}


def schema(cfg):
    """The answer's JSON schema; with suggestions on, each item has a one-click fix field; on an incremental
    review (gc-sy1hm.2 c), the answer says what became of each earlier ask."""
    if not cfg.get('suggestions') and not cfg.get('_since'):
        return SCHEMA
    s = json.loads(json.dumps(SCHEMA))
    if cfg.get('suggestions'):
        it = s['properties']['items']['items']
        it['properties']['suggestion'] = SUGGESTION
        it['required'] = it['required'] + ['suggestion']
    if cfg.get('_since'):
        s['properties']['earlier'] = pr_incremental.SCHEMA
        s['required'] = s['required'] + ['earlier']
    return s


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


def run_model(cfg, tree, ctx, prompt, answer_schema=None, max_turns=None):
    """The headless review (`answer_schema`: another answer in the same sandbox, the follow-up's). Returns
    (structured output or None, meta with cost and counts)."""
    settings = {'disableAllHooks': True,
                'permissions': {'deny': [f'{t}({p})' for p in DENY_ROOTS for t in ('Read', 'Grep', 'Glob')]}}
    keep = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'USER', 'LOGNAME', 'TERM', 'TZ')
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update(LOOMTRACER_INGEST_DISABLE='1', HIVEMIND_INGEST_DISABLE='1')
    cmd = [claude_bin(), '-p', '--output-format', 'json', '--json-schema', json.dumps(answer_schema or schema(cfg)),
           '--model', cfg['model'], '--no-session-persistence', '--setting-sources', '', '--settings', json.dumps(settings),
           '--strict-mcp-config', '--tools', 'Read,Grep,Glob', '--add-dir', ctx,
           '--max-turns', str(max_turns or cfg['max_turns'])]
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
    if answer_schema:
        return so, meta
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
    jq = f'.[] | select(.commit_id == "{head}") | [({OURS_JQ}), (.body | contains("{pr_format.POINTER_PREFIX}"))]'
    r = subprocess.run(['gh', 'api', '--paginate', f'repos/{repo}/pulls/{number}/reviews?per_page=100', '--jq', jq],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None
    rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    return sum(1 for ours, pointer in rows if ours and not pointer)


OURS_JQ = pr_format.ours_jq()
DISMISSAL = 'Replaced by a newer automated review.'
DISMISSAL_VOICE = 'Replaced by a newer review.'
STALE = {'disclose': 'This automated review was of an earlier commit.', 'reviewer': 'This review was of an earlier commit.'}  # decision-ed7320b7


def dismiss_stale(repo, number, keep_id, reviewer, voice='disclose', message=None):
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
                            '-f', f"message={(message or {}).get(voice) or (DISMISSAL_VOICE if voice == 'reviewer' else DISMISSAL)}",
                            '-f', 'event=DISMISS'],
                           capture_output=True, text=True, timeout=60)
        ok, bad = ok + (d.returncode == 0), bad + (d.returncode != 0)
    return ok, bad


def repoint(repo, number, reviewer, voice='disclose'):
    """R24: every pointer of ours on the PR follows the newest review (the mayor, 2026-10-30 07:21Z: a chain
    of pointers left the oldest review pointing at a replaced one). Returns (updated, failed)."""
    jq = f'.[] | select((.user.login | ascii_downcase) == "{reviewer.lower()}" and ({OURS_JQ})) | {{id, body, html_url}}'
    r = subprocess.run(['gh', 'api', '--paginate', f'repos/{repo}/pulls/{number}/reviews?per_page=100', '--jq', jq],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return 0, 1
    ok = bad = 0
    for rid, body in pr_format.repoints([json.loads(x) for x in r.stdout.splitlines() if x.strip()], NAME, voice):
        good = update_review(repo, number, rid, body)[0] == 'updated'
        ok, bad = ok + good, bad + (not good)
    return ok, bad


def block_kind(rv):
    """What a formal block rests on: 'mechanical' (only a merge conflict or CI) the sweep lifts once the
    cause is gone; 'content' (an item or a decision) needs a review of the new head."""
    if not rv['items'] and not rv['decisions'] and rv['blockers'] and \
            all(b.get('cause') in ('conflict', 'ci') for b in rv['blockers']):
        return 'mechanical'
    return 'content'


def deliver(cfg, key, entry, pr, rv, body, comments, result, extra, flagged=(), idx=None, rereview=False, summary=''):
    """R15, R18, R27, R0, R17, R21: re-check that the PR is open and on the reviewed head, and that this
    head was not reviewed yet (unless `rereview`); re-check the approval bar; post ONE review (APPROVE or
    COMMENT), delete it at once if GitHub made it pending, check its inline comments are anchored, and post
    '<word>: <review URL>' to Slack. A re-review updates the earlier review in place when it can (same
    event, no inline comments), else replaces it with a pointer; its Slack message is updated in place.
    The client's channel gets the PR's entry once per head; `summary`: the model's summary of the PR, for
    the entry's 3-5 words."""
    repo, n, head = entry['repo'], entry['number'], pr['headRefOid']
    event = pr_format.event_of(rv)
    # The gap kinds, host-local and counts only (decision-3aeec149); the feedback order asks once per kind.
    extra.update(gaps=sorted({g['kind'] for g in rv.get('gaps') or []}),
                 gaps_open=sorted({g['kind'] for g in pr_format.open_gaps(rv)}), voice=rv.get('voice'))
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
        if not url and comments and rv.get('fallback'):  # R33: never silent; the questions stand in the body
            posted, url, state, rid = post_review(repo, pr, body, [], event)
            extra['inline_dropped'], comments = len(comments), []
        if old_id and posted == 'ok':
            p2 = update_review(repo, n, old_id, pr_format.pointer_body(NAME, url, cfg.get('voice')))[0]
            extra.update(replaced=old_id, replaced_status=p2)
    if posted == 'ok' and state == 'PENDING':  # R0: never leave one in the reviewer's slot
        subprocess.run(['gh', 'api', '-X', 'DELETE', f'repos/{repo}/pulls/{n}/reviews/{rid}'],
                       capture_output=True, text=True, timeout=60)
    elif posted == 'ok':
        extra['dismissed'], extra['dismiss_failed'] = dismiss_stale(repo, n, rid, cfg['reviewer'], cfg.get('voice'))
        extra['repointed'], extra['repoint_failed'] = repoint(repo, n, cfg['reviewer'], cfg.get('voice'))
        if (cfg.get('incremental') or entry.get('followup') == 'push') and rv.get('since'):  # gc-sy1hm.2 c, once it is up
            extra['threads_resolved'], extra['threads_resolve_failed'] = pr_incremental.resolve_threads(repo, n, rv['since'])
    if not url:
        finish(cfg, key, dict(entry, head=head), 'held-inline' if comments else 'error', {**extra, 'posted': posted})
        return
    inline = anchored_lines(repo, n, rid) if comments else None
    if comments and inline is not None and len(inline) < len(comments):
        inline += [None] * (len(comments) - len(inline))
    reasons = pr_format.gate_delivery(fresh['state'], url, state, inline_lines=inline,
                                      ours_on_head=ours_on_head(repo, n, head))
    if head not in heads:  # the answer trigger counts the author's posts from our first review of the head
        heads.append(head)
        extra.update(head_reviewed_at=pr_state.iso(pr_state.now()), head_review_ids=[rid])
    else:
        extra['head_review_ids'] = ([x for x in entry.get('head_review_ids') or [entry.get('review_id')] if x] + [rid])[-10:]
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
                                        event=event, gap=bool(pr_format.open_gaps(rv)))
    extra['slack'] = slack
    if new_ts:
        extra['slack_ts'] = new_ts
    # The client's entry (decision-14bb1b55; the mayor, 2026-10-30 19:40Z and 19:41Z): the review in two lines. Its
    # confidence and flag counts stay on the PR's entry for the queue. Once per head, by the client post's own
    # record (the mayor, 19:12Z: our channel is off, so its ts says nothing about the client's).
    review = pr_client_slack.review_of(rv, event, extra.get('ci') or pr_rules.ci_state(fresh),
                                       extra.get('flagged_hunks', len(result.get('flagged') or [])),
                                       extra.get('flags_cut', rv.get('flags_cut') or 0), extra.get('gaps_open') or ())
    extra.update(confidence=review['confidence'], flagged_total=review['flagged'], open_changes=review['changes'],
                 open_questions=review['questions'])
    # Once per head and status (decision-57b6d9bb): a follow-up posts a new entry only when readiness moved.
    ready = pr_client_slack.readiness(fresh.get('mergeable'), extra.get('ci') or pr_rules.ci_state(fresh),
                                      pr_client_slack.ours_of(review), fresh.get('state', 'OPEN'))
    if cfg['client_slack'] != 'off' and not pr_client_slack.posted_on(entry, head, pr_client_slack.sent_reviews(cfg['state']),
                                                                      ready):
        try:
            status, meta = pr_client_slack.deliver_entry(cfg, key, dict(entry, review_url=url), review, fresh, summary,
                                                         lambda s: pr_client_slack.words_turn(cfg, s))
        except Exception as e:  # noqa: BLE001 - the review is out: a failed entry never keeps the run from its record
            status, meta = f'error:{type(e).__name__}', {}
        extra.update(client_slack=status, **{k: meta[k] for k in ('words_from', 'words_cost_usd') if k in meta},
                     **({'client_ts': meta['ts']} if meta.get('ts') else {}))
        if status in pr_client_slack.POSTED:
            extra['client_posts'] = pr_client_slack.record_post(entry, head, status, meta.get('ts'), ready)
    finish(cfg, key, dict(entry, head=head), rv['outcome'], extra)
    return url


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
              facts=pr_format.facts_line(pr, ci, ci_detail(pr)), approve=False, request_changes=cfg['request_changes'],
              voice=cfg.get('voice'), names=[NAME, pr_format.YOU if cfg.get('voice') == 'reviewer' else author])
    rv['outcome'] = pr_format.decide(rv['decisions'], rv['items'], rv['blockers'])
    body = pr_format.render(entry['repo'], pr, rv, NAME, author)
    comments = pr_format.inline_comments(rv['items'], author, cfg.get('voice'), pr_format.first_item(rv))
    idx = pr_format.diff_index(pr_diff(entry['repo'], pr['number']))
    # A fallback is gated as it was in the run: it covers no flagged hunk (R33).
    reasons = pr_format.gate(rv, idx, body, None, pr.get('body') or '', [] if res.get('fallback') else res.get('flagged') or [],
                             comments)
    if reasons:
        finish(cfg, key, entry, 'held-gate', {'model': False, 'redeliver': True, 'gate': [r.split(':')[0] for r in reasons]})
        return
    deliver(cfg, key, entry, pr, rv, body, comments, res, {'model': False, 'redeliver': True, 'gate': []},
            summary=res.get('summary') or '')


FOLLOWUP_TURNS = 16  # the follow-up reads the asks' lines and what the answers point at; the review used 2-11


def post_reply(repo, number, comment_id, body):
    """A reply in the review thread of our inline comment `comment_id`."""
    r = subprocess.run(['gh', 'api', '-X', 'POST', f'repos/{repo}/pulls/{number}/comments/{int(comment_id)}/replies',
                        '-f', f'body={body}'], capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def post_comment(repo, number, body):
    r = subprocess.run(['gh', 'api', '-X', 'POST', f'repos/{repo}/issues/{number}/comments', '-f', f'body={body}'],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def rebuild(cfg, repo, pr, ci, res, calls, asks, author):
    """decision-57b6d9bb: the delivered review's parts after the author's answers (pr_followup.apply): answered
    asks cleared with the answer as the reason, open ones kept, changes as changes; the blockers and status
    as they stand now. A follow-up on a fallback stays one: it never approves (the full review is still
    owed). Returns (rv, the follow-up replies for open asks)."""
    voice = cfg.get('voice') or 'disclose'
    items, cleared, decisions, counts, replies = pr_followup.apply(res, calls, asks, voice)
    rv = {'decisions': decisions, 'items': items, 'cleared': cleared, 'summary': res.get('summary') or '',
          'skip': res.get('skip') or [], 'since': None, 'blockers': pr_format.blockers(repo, pr, ci, ci_detail(pr)),
          'facts': pr_format.facts_line(pr, ci, ci_detail(pr)), 'not_checked': res.get('not_checked') or '',
          'model': bool(res.get('summary')), 'flags_cut': 0, 'approve': False, 'request_changes': cfg['request_changes'],
          'rules': res.get('rules') or {}, 'rules_sha': res.get('rules_sha') or '', 'voice': voice, 'gaps': [],
          'names': [NAME, pr_format.YOU if voice == 'reviewer' else author], 'fallback': bool(res.get('fallback')),
          'others': res.get('others') or [], 'answers': counts,
          'signed_off': pr_followup.signed_off(res, decisions, cleared)}
    rv['outcome'] = pr_format.decide(rv['decisions'], rv['items'], rv['blockers'])
    return rv, replies


def answer_review(cfg, key, entry, dry=False, answer=None):
    """decision-57b6d9bb: the same-head follow-up on the author's answers (pr_followup), or the one line when
    the rounds on the head are used up. The answers are read from GitHub into the prompt only (host-local,
    capture off); what is printed and kept is counts and ids. `answer(prompt)` stands in for the model."""
    repo, n = entry['repo'], entry['number']
    pr = pr_rules.get_pr(repo, n)
    if pr['state'] != 'OPEN':
        finish(cfg, key, entry, 'closed', {'model': False})
        return
    head = pr['headRefOid']
    if head not in (entry.get('review_heads') or []):  # pushed meanwhile: the new head gets its incremental review
        return ci_escalation(cfg, key, dict(entry, followup='push'), dry)
    rounds = dict(entry.get('answer_rounds') or {})
    done = {'answers_key': entry.get('answers_pending') or entry.get('answers_key'), 'followup': None,
            'answers_pending': None}
    author, voice = author_of(pr), cfg.get('voice') or 'disclose'
    if entry.get('followup') == 'cap':
        ok = dry or cfg.get('no_deliver') or post_comment(repo, n, pr_followup.cap_line(author, rounds.get(head, 0), voice, NAME))
        finish(cfg, key, entry, entry.get('outcome') or 'done',
               {**done, 'model': False, 'answer_capped': head if ok else None, 'followup_cap': 'posted' if ok else 'failed'})
        return
    p = os.path.join(pr_state.private_dir(cfg, 'feedback', 'posted'), f"{repo.replace('/', '__')}-{n}-{head[:10]}.json")
    try:
        res = json.load(open(p)).get('result') or {}
    except (OSError, ValueError):
        res = {}
    asks = pr_followup.asks_of(res)
    if not asks:  # nothing of ours to follow up on this head (an approval, or no stored review)
        finish(cfg, key, entry, entry.get('outcome') or 'done', {**done, 'model': False,
                                                                 'followup_skipped': 'no asks' if res else 'no stored review'})
        return
    owner, name = repo.split('/')
    act = pr_rules.graphql(pr_followup.REPLIES_Q, o=owner, r=name, n=n)['repository']['pullRequest']
    ours = [x for x in entry.get('head_review_ids') or [entry.get('review_id')] if x]
    threads = pr_followup.threads_of(act, cfg['reviewer'], asks, ours)
    others = pr_followup.others_of(act, cfg['reviewer'], entry.get('head_reviewed_at') or entry.get('delivered_at'))
    prompt = pr_followup.prompt(pr['url'], head, author, asks, threads, others)
    if dry:
        print(json.dumps({'pr': entry['url'], 'would': 'follow-up', 'asks': len(asks), 'threads': len(threads),
                          'replies': sum(len(t['replies']) for t in threads.values()), 'comments': len(others),
                          'prompt_chars': len(prompt)}))
        return
    run_id = f"{repo.replace('/', '__')}-{n}-{head[:10]}-followup"
    mirror = None
    try:
        if answer:
            so, meta = answer(prompt)
        else:
            mirror, tree, ctx, *_ = prepare(cfg, repo, pr, run_id)
            so, meta = run_model(cfg, tree, ctx, prompt, pr_followup.SCHEMA, FOLLOWUP_TURNS)
        rounds[head] = rounds.get(head, 0) + 1
        extra = {**done, 'model': True, 'followup_kind': 'answer', 'answer_rounds': dict(list(rounds.items())[-10:]),
                 'cost_usd': meta.get('cost_usd') or 0, 'turns': meta.get('turns'), 'asks': len(asks),
                 'threads': len(threads), 'ci': pr_rules.ci_state(pr)}
        if not so:
            finish(cfg, key, entry, 'error', {**extra, 'error': meta.get('error') or 'no-answer'})
            return
        ci = pr_rules.ci_state(pr)
        rv, replies = rebuild(cfg, repo, pr, ci, res, so.get('calls') or [], asks, author)
        idx = pr_format.diff_index(pr_diff(repo, n))
        flagged = [] if res.get('fallback') else res.get('flagged') or []
        if rv['outcome'] == 'low-risk' and cfg['approve'] and not rv['fallback']:
            rv['approve'] = not pr_format.gate_approve(rv, pr, head, flagged, idx)
        body = pr_format.render(repo, pr, rv, NAME, author)
        reasons = pr_format.gate(rv, idx, body, None, pr.get('body') or '', flagged, None)
        if not reasons and leaks([body] + [t for _, t in replies]):
            reasons = ['held-secret']
        extra.update(followup_calls=rv['answers'], items=len(rv['items']), cleared=len(rv['cleared']),
                     decisions=len(rv['decisions']), gate=sorted({r.split(':')[0] for r in reasons}))
        if reasons:
            finish(cfg, key, entry, 'held-gate', extra)
            return
        result = {k: rv[k] for k in ('outcome', 'decisions', 'items', 'cleared', 'blockers', 'summary', 'skip', 'not_checked')}
        result.update(rule_ids=res.get('rule_ids') or [], flagged=res.get('flagged') or [], rules=rv['rules'],
                      rules_sha=rv['rules_sha'], **({'fallback': True, 'others': rv['others']} if rv['fallback'] else {}))
        url = deliver(cfg, key, entry, pr, rv, body, [], result, extra, flagged, idx, rereview=True,
                      summary=res.get('summary') or '')
        if url:  # the follow-ups go into the open asks' threads once the review is up
            sent = sum(post_reply(repo, n, threads[a]['comment'], t) for a, t in replies if a in threads)
            with pr_state.locked(cfg['state']) as st:
                st['prs'][key].update(followup_replies=sent)
            pr_state.log(cfg['state'], {'run': 'followup-replies', 'pr': entry['url'], 'sent': sent, 'open': len(replies)})
    except (RuntimeError, OSError, subprocess.SubprocessError, pr_rules.GhError) as e:
        finish(cfg, key, entry, 'error', {**done, 'model': False, 'error': type(e).__name__})
    finally:
        if mirror:
            cleanup(cfg, mirror, run_id)


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
        if not requeue:  # a follow-up's mark ends with its run (decision-57b6d9bb)
            cur.pop('followup', None)
            cur.pop('answers_pending', None)
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
    voice = cfg.get('voice') or 'disclose'
    accepted = pr_state.accepted_gaps(cfg)
    gaps = [dict(kind=g['kind'], rule=g.get('rule') or '', accepted=g['kind'] in accepted)
            for g in (skim or {}).get('gaps') or [] if isinstance(g, dict) and g.get('kind')]
    if money:
        mi = money_info or {}
        decisions = [pr_format.not_checked_decision(money, words, author, mi.get('overview', ''), mi.get('overview_places', ()),
                                                    voice)] + decisions[:pr_format.MAX_DECISIONS - 1]
        gaps.append({'kind': 'not-checked', 'rule': '', 'accepted': 'not-checked' in accepted})
    labels = {f['id']: f['label'] for f in flagged}
    cleared = [dict(c, label=labels.get(c.get('flag'), 'a flagged change')) for c in cleared]
    skip = [dict(g, paths=[x for x in g['paths'] if idx is None or x in idx]) for g in (skim or {}).get('skip') or []]
    rv = {'decisions': decisions, 'items': items, 'cleared': cleared, 'summary': (skim or {}).get('summary') or '',
          'skip': [g for g in skip if g['paths']],
          'blockers': pr_format.blockers(repo, pr, ci, ci_detail(pr), conflicts),
          'facts': pr_format.facts_line(pr, ci, ci_detail(pr)),
          'not_checked': words if money else '', 'model': model, 'flags_cut': flags_cut, 'approve': False,
          'request_changes': cfg['request_changes'], 'rules': (digest or {}).get('rules') or {},
          'rules_sha': (digest or {}).get('sha') or '', 'voice': voice, 'gaps': gaps,
          'names': [NAME, pr_format.YOU if voice == 'reviewer' else author]}
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
        rv['approve'] = rv['outcome'] == 'low-risk' and not pr_format.open_gaps(rv) and \
            not pr_format.gate_approve(rv, pr, pr['headRefOid'], flagged, idx)
    return rv


def minimal_not_checked(cfg, repo, pr, ci, money, ruleset, author, money_info=None, since=None):
    """R33 (the mayor, 2026-10-29 21:57Z): the review of a PR that touches a not-checked area, when the
    reader test held the full one twice: line 2, the money decision (each place linked to its changed range
    with its one line, R32) and the status line, nothing the review model wrote."""
    rv = assemble(cfg, repo, pr, ci, [], [], [], '', [], 0, money, ruleset, author, model=False, money_info=money_info)
    rv['gaps'].append({'kind': 'below-bar', 'rule': '', 'accepted': 'below-bar' in pr_state.accepted_gaps(cfg)})
    rv['fallback'] = True  # R33: held to the bar the full review failed, its places cut to the question's
    rv['since'] = since
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


def not_reviewed(repo, pr, ci, idx, flagged, money, author, held, codes, words, cfg=None, floor=False, since=None):
    """R33: a PR that requests the reviewer is never silent, and the note keeps a floor the reviewer can act
    on (the mayor, 2026-10-30 02:11Z): why the review was held, in plain words; the places the zero-token
    rules know, each linked (the money places with their lines, the flagged hunks with what the rule saw);
    and one rule-level decision with lettered options and a recommendation. `floor`: the minimal note
    when the note itself was held (never silent): one place, described by the script's own words."""
    places, seen = [], set()
    for x in list(money) + [dict(pr_format.changed_range(idx, f['path'], f['line']), what=f['label']) for f in flagged]:
        k = (x['path'], x.get('hunk') if x.get('hunk') is not None else x['line_start'])
        if k not in seen:
            seen.add(k)
            places.append(x)
    if floor:  # one place in the script's words: a flag's rule label already, the area's name for a money place
        places = [dict(places[0], what=f'a change to {words}') if money else places[0]] if places else []
    why = held_why(money, words, held, codes)
    voice = (cfg or {}).get('voice') or 'disclose'
    rv = {'outcome': 'not-reviewed', 'decisions': [pr_format.floor_decision(places, author, why, voice)], 'items': [],
          'cleared': [], 'blockers': pr_format.blockers(repo, pr, ci, ci_detail(pr)), 'summary': '', 'skip': [],
          'facts': pr_format.facts_line(pr, ci, ci_detail(pr)), 'not_checked': '', 'approve': False, 'model': False,
          'request_changes': False, 'rules': {}, 'rules_sha': '', 'voice': voice,
          'gaps': [{'kind': 'below-bar', 'rule': '', 'accepted': 'below-bar' in pr_state.accepted_gaps(cfg)}] if cfg else [],
          'names': [NAME, pr_format.YOU if voice == 'reviewer' else author], 'fallback': True, 'since': since}
    return rv, pr_format.render(repo, pr, rv, NAME, author), []


def held_why(money, words, held, codes):
    """R33: why the review was held, in plain words (the note says it in the disclosing voice only)."""
    parts = []
    if money:
        parts.append(f"it touches {words} in {pr_format.plural(len(money), 'place', 'places')}, which no agent checks")
    if held == 'held-secret':
        parts.append('its text matched the shape of a secret, so it was withheld')
    elif 'R26' in codes or 'R31' in codes:
        parts.append('a second reader found our review too hard to act on')
    elif codes:
        parts.append('our review broke the review format twice')
    return "Our review of this PR didn't reach the quality bar: " + '; '.join(parts or ['it was held']) + '.'


def asked_fallback(cfg, repo, pr, ci, idx, flagged, money, author, ruleset, note=False, held=None, codes=(), answer=None,
                   since=None):
    """R33 (the mayor, 2026-10-30 21:39Z: "One specific question per linked line with its own reason, no
    boilerplate sentences, no orphan link, one status line"): the fallback as one question per place, the
    riskiest first, each on its lines with its reason as the inline comment (pr_fallback); the places it does
    not ask about collapsed, each linked to its lines. `note`: the note's form (a PR that requests the
    reviewer, the flagged hunks among its places), else the money review's (its money places only).
    Returns (rv, body, comments, meta: counts, rule ids and cost); rv None when no place got a question
    that passes, or the questions match a secret's shape: the stock fallback then."""
    words = ruleset.get('not_checked') or 'billing, plans, credits or pricing'
    places = pr_fallback.places(idx, money, flagged if note else [], fill=note)
    if not places or not (answer or cfg.get('judge_model')):
        return None, None, None, {'asked': 0}
    items, others, summary, meta = pr_fallback.ask(cfg, idx, places, answer, title=pr.get('title') or '',
                                                   description=pr.get('body') or '')
    if not items:
        return None, None, None, meta
    voice = cfg.get('voice') or 'disclose'
    accepted = pr_state.accepted_gaps(cfg)
    rv = {'decisions': [], 'items': items, 'cleared': [], 'summary': summary, 'skip': [], 'since': since,
          'blockers': pr_format.blockers(repo, pr, ci, ci_detail(pr)), 'facts': pr_format.facts_line(pr, ci, ci_detail(pr)),
          'not_checked': words if money and not note else '', 'model': False, 'flags_cut': 0, 'approve': False,
          'request_changes': cfg.get('request_changes', False) and not note, 'rules': {}, 'rules_sha': '', 'voice': voice,
          'gaps': [{'kind': k, 'rule': '', 'accepted': k in accepted} for k in (['not-checked'] if money else []) + ['below-bar']],
          'names': [NAME, pr_format.YOU if voice == 'reviewer' else author], 'fallback': True, 'others': others,
          'held_why': held_why(money, words, held, codes) if note and voice != 'reviewer' else ''}
    rv['outcome'] = 'not-reviewed' if note else pr_format.decide([], items, rv['blockers'])
    body = pr_format.render(repo, pr, rv, NAME, author)
    comments = pr_format.inline_comments(items, author, voice, pr_format.first_item(rv))
    if leaks([body] + [c['body'] for c in comments]):
        return None, None, None, dict(meta, secret=True)
    return rv, body, comments, meta


def asked_log(meta, form):
    """The fallback's log fields: which form went out (asked, stock, floor) and the questions' counts."""
    return {'fallback_form': form, 'fallback_asked': meta.get('asked', 0), 'fallback_kept': meta.get('kept', 0),
            'fallback_calls': meta.get('calls', 0), 'fallback_failed': meta.get('failed', []),
            'fallback_cost_usd': meta.get('cost_usd', 0), **({'fallback_gate': meta['gate']} if meta.get('gate') else {})}


def linked_paths(rv):
    """The files a review's decisions and items link, for their line counts at the head (R16)."""
    return [w['path'] for d in rv['decisions'] for w in d.get('where') or []] + [i['path'] for i in rv['items']]


def reader_reasons(scores, jm, risky):
    """The reader test's hold reasons (R26 below the bar, R31 risky cleared entries, R37 a behaviour change
    in the skip list): the same bar for an attempt and for a repaired review."""
    return pr_judge.below_bar(scores) + ([
        f'R31: cleared entries {risky} describe a possible risk without saying why it is acceptable or '
        f'intended; give the fact that makes each one safe, or make it a question for the author']
        if risky and scores is not None else []) + ([
        'R37: a change in product behaviour sits in "Safe to skip"; give it its own linked line saying '
        'it is intended and its default is safe, or make it a decision'] if jm.get('behaviour_in_skip') else [])


# The narrow repair turn (the mayor, 2026-10-30 16:36Z): a finished review is never turned into a fallback
# by a wording rule. Only the failing entries, their rules and their own lines go to the model, bounded by
# turns; it rewrites the named fields and nothing else.
REWRITE_TURNS = 2
REWRITE_PROMPT = """Some entries of a code review failed the wording rules of its style guide. Rewrite ONLY the
fields named in each entry's "rewrite" list so that they pass, in plain words. Keep the meaning and every
fact; add nothing, and do not add, drop or merge entries. "limits" gives each field's budget and its count
now: count the words of what you write. For each field give up to three versions, best first, each within
its budget, the last one clearly shorter. "lines" are the entry's own lines of code, for context only.

The reasons they failed:
{rules}

The style guide's rows for these rules:
{rows}

The entries (JSON):
{entries}

Answer with each entry's id and its rewritten fields."""
REWRITE_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['entries'], 'properties': {
    'entries': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False, 'required': ['id'],
        'properties': {'id': {'type': 'string'}, **{f: {'type': 'array', 'maxItems': 3, 'items': {'type': 'string'}}
                                                    for f in pr_format.WORDING_FIELDS}}}}}}


def style_rows(ids):
    """The style guide's table rows for these rule ids, as the model's own prompt has them."""
    return '\n'.join(line for line in open(STYLE).read().splitlines() if any(line.startswith(f'| {r} |') for r in ids))


def rewrite(cfg, rv, reasons, idx):
    """The narrow repair turn: the entries a wording rule failed, with the rules, their rows and the
    entries' own lines, to the judge model for at most REWRITE_TURNS turns; the named fields come back
    rewritten (cleaned like any model text) and nothing else changes. Returns (rv, meta)."""
    asks = pr_format.rewrite_asks(rv, reasons)
    if not asks:
        return rv, {'cost_usd': 0, 'rewritten': 0, 'asked': 0}
    entries = []
    for eid, a in asks.items():
        kind, k = eid.rsplit(' ', 1)
        x = rv[pr_format.REPAIR_GROUP[kind]][int(k) - 1]
        text = (idx.get(x.get('path')) or {}).get('text') or {}
        lines = [text.get(ln, '') for ln in range(x.get('line_start') or 1, (x.get('line_end') or 0) + 1)][:30]
        entries.append({'id': eid, 'rewrite': a['fields'], **{f: x[f] for f in pr_format.WORDING_FIELDS if x.get(f)},
                        'limits': {f: pr_format.field_limit(kind, f, x.get(f)) for f in a['fields']},
                        'lines': '\n'.join(lines)})
    rules = [r for a in asks.values() for r in a['rules']]
    prompt = REWRITE_PROMPT.format(rules='\n'.join(f'- {r}' for r in rules),
                                   rows=style_rows({r.split(':')[0] for r in rules}),
                                   entries=json.dumps(entries, indent=1))
    so, meta = pr_judge.ask(cfg, prompt, REWRITE_SCHEMA, cfg['judge_model'], REWRITE_TURNS)
    rv, n = pr_format.apply_rewrite(rv, asks, (so or {}).get('entries'), clean)
    return rv, dict(meta, rewritten=n, asked=sum(len(a['fields']) for a in asks.values()))


HELD_DIFF_MAX = 400_000  # characters of diff kept with a held attempt; a larger diff is left out


def keep_held(cfg, entry, pr, attempt, rv, body, reasons, flagged, diff, author):
    """A held attempt's structure, host-local (it holds the repository's text): the review dict, its body,
    the gate's full reasons, the flags, the PR description and the diff, so `pr_format.py --regate` can
    gate it again with later code at zero tokens. Not for the benchmark (it keeps its own)."""
    if cfg.get('collect') is not None:
        return
    p = os.path.join(pr_state.private_dir(cfg, 'feedback'), 'held.jsonl')
    pr_state.prune_jsonl(p, cfg.get('keep_days', 30))  # PR_REVIEW_KEEP_DAYS: older rows go on the next write
    with open(p, 'a') as f:
        f.write(json.dumps({'pr': entry['url'], 'head': pr['headRefOid'], 'attempt': attempt, 'author': author, 'reasons': reasons,
                            'rv': rv, 'body': body, 'flagged': flagged, 'pr_text': pr.get('body') or '',
                            'diff': diff if len(diff) <= HELD_DIFF_MAX else None,
                            'at': pr_state.iso(pr_state.now())}) + '\n')
    os.chmod(p, 0o600)


def held_note(cfg, repo, pr, ci, idx, flagged, money, author, held, codes, ruleset, gate, answer=None, since=None):
    """R33: the note for a held review of a PR that requests the reviewer, gated by `gate(rv, body,
    comments)`: first its own question per place (asked_fallback); without one that passes, the stock note
    (one question, lettered options). When the stock note itself is held, the floor instead (never silent,
    the mayor 2026-10-30 16:54Z): one place in the script's own words. The floor holds no model text, so it
    goes out whatever its gate says, unless it matches a secret's shape. Returns (rv, body, comments,
    reasons, log fields)."""
    words = ruleset.get('not_checked') or 'billing, plans, credits or pricing'
    rv, body, comments, meta = asked_fallback(cfg, repo, pr, ci, idx, flagged, money, author, ruleset, True, held, codes,
                                              answer, since)
    if rv:
        reasons = gate(rv, body, comments)
        if not reasons:
            return rv, body, comments, [], asked_log(meta, 'asked')
        meta['gate'] = sorted({r.split(':')[0] for r in reasons})
    rv, body, comments = not_reviewed(repo, pr, ci, idx, flagged, money, author, held, codes, words, cfg, since=since)
    reasons = gate(rv, body, comments)
    if not reasons:
        return rv, body, comments, [], asked_log(meta, 'stock')
    note = {'floor': True, 'note_gate': sorted({r.split(':')[0] for r in reasons}), **asked_log(meta, 'floor')}
    rv, body, comments = not_reviewed(repo, pr, ci, idx, flagged, money, author, held, codes, words, cfg, floor=True,
                                      since=since)
    note['floor_gate'] = sorted({r.split(':')[0] for r in gate(rv, body, comments)})
    return rv, body, comments, (reasons if leaks([body]) else []), note


def keep_reader(cfg, entry, head, attempt, scores, problems):
    """The reader test's reasons for a held attempt, host-local (they can describe the repository's
    code), to tune the review and the reader."""
    p = os.path.join(pr_state.private_dir(cfg, 'feedback'), 'reader.jsonl')
    pr_state.prune_jsonl(p, cfg.get('keep_days', 30))  # PR_REVIEW_KEEP_DAYS, as the held attempts
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
    # A follow-up (decision-57b6d9bb) is reviewed by the model like a requested PR (R34).
    if entry.get('followup') or not pr_rules.ci_only(ci, 'requested' in hits, bool(content_rules(hits, area_of, ruleset))):
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
        # gc-sy1hm.2 a: where the changed code is used and tested, found by a word search (zero tokens)
        context_block, cstats = pr_context.build(tree, pr_format.diff_index(diff)) if cfg.get('context') else ('', {})
        # gc-sy1hm.2 c: our earlier asks on this PR and the new commits since the head we reviewed
        prev, since_rows, since_block, inc = None, [], '', {}
        if cfg.get('incremental') or entry.get('followup') == 'push':  # decision-57b6d9bb: a push follow-up
            prev = pr_incremental.earlier(cfg, entry, pr['headRefOid'], pr_state.private_dir(cfg, 'feedback', 'posted'))
            if prev:
                sdiff, su0, why = pr_incremental.since(mirror, prev['head'], pr['headRefOid'])
                inc = {'incremental': why, 'earlier_asks': len(prev['items'])}
                if why == 'ok':
                    since_rows = pr_incremental.rows(prev, su0)
                    since_block = pr_incremental.prompt_block(prev, pr['headRefOid'], since_rows, sdiff, ctx)
        mcfg = dict(cfg, _since=bool(since_block))
        prompt = build_prompt(cfg, pr, diff, files, commits, fork, ctx, flagged, ruleset.get('not_checked'),
                              pr_digest.prompt_block(digest), author, context_block, since_block)
        if dry:
            print(json.dumps({'pr': entry['url'], 'dry_run': True, 'prompt_chars': len(prompt),
                              'context_chars': len(context_block), **{f'context_{k}': v for k, v in cstats.items()},
                              'diff_chars': len(diff), 'files': len(files.splitlines()), 'rules': sorted(hits),
                              'flagged_hunks': len(flagged), 'flags_cut': flags_cut, 'not_checked_hunks': len(nc_rule)}))
            return
        idx = pr_format.diff_index(diff)
        extra = {'model': True, 'cost_usd': 0, 'attempts': 0, 'judge_cost_usd': 0, 'money_cost_usd': 0,
                 'digest': dmeta.get('digest'), 'digest_rules': dmeta.get('rules', 0),
                 'digest_cost_usd': dmeta.get('cost_usd', 0),
                 **{f'context_{k}': v for k, v in cstats.items()}, **inc}
        reasons, problems, model, risky, risky_q, money_memo, answered = [], [], None, [], {}, {}, []
        for attempt in (1, 2):
            p = prompt if attempt == 1 else (
                prompt + '\n\nYOUR PREVIOUS ANSWER FAILED THE STYLE GUIDE GATE. Fix every reason and answer again:\n'
                + '\n'.join(f'- {r}' for r in reasons)
                + ''.join(f"\n- a reader found: {x['what']} Fix: {x['fix']}" for x in problems)
                + '\n\nYour previous answer:\n' + json.dumps(model))
            model, meta = run_model(mcfg, tree, ctx, p)
            extra['attempts'] = attempt
            extra['cost_usd'] += meta.get('cost_usd') or 0
            extra.update({k: v for k, v in meta.items() if k != 'cost_usd'})
            if model is None:
                finish(cfg, key, dict(entry, head=pr['headRefOid']), 'error', extra)
                return
            decisions = [d for d in model['decisions'] if d['confidence'] >= 8]
            items = [i for i in model['items'] if i['confidence'] >= 8][:pr_format.MAX_ITEMS]
            if cfg.get('suggestions'):  # gc-sy1hm.2 b: a fix the gate would refuse is dropped, its item stays
                items, extra['suggestions'], extra['suggestions_dropped'] = pr_format.keep_suggestions(items, idx)
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
            if since_block:
                rv['since'] = pr_incremental.outcome(since_rows, model.get('earlier'), rv['items'], prev, pr['headRefOid'])
            body = pr_format.render(repo, pr, rv, NAME, author)
            comments = pr_format.inline_comments(rv['items'], author, cfg.get('voice'), pr_format.first_item(rv))
            links = [(d.get('link') or {}).get('path') for d in rv['decisions'] if (d.get('link') or {}).get('kind') == 'code']
            reasons = pr_format.gate(rv, idx, body, tree_lines(tree, links), pr.get('body') or '', flagged, comments)
            problems = []
            if not reasons and cfg['judge']:
                scores, problems, jm = pr_judge.judge(cfg, body, comments, NAME, author)
                extra['judge_cost_usd'] += jm.get('cost_usd') or 0
                extra['judge'] = scores
                risky = [j for j in jm.get('risky_cleared') or [] if j <= len(rv['cleared'])]
                risky_q = jm.get('risky_questions') or {}
                answered = [n - pr_format.first_item(rv) + 1 for n in jm.get('answered') or []]  # body no. -> item no.
                extra['risky_cleared'] = len(risky)
                reasons = reader_reasons(scores, jm, risky)
                extra['behaviour_in_skip'] = bool(jm.get('behaviour_in_skip'))
                if reasons and scores is not None:
                    keep_reader(cfg, entry, pr['headRefOid'], attempt, scores, problems)
                if scores is None:  # the reader test did not run: hold without a second model run
                    break
            if not reasons:
                break
            keep_held(cfg, entry, pr, attempt, rv, body, reasons, flagged, diff, author)
            extra.setdefault('gate_first', sorted({r.split(':')[0] for r in reasons}))
        if not reasons and answered:  # R31's converse: a question its own text answers is checked, not asked
            kept, more = pr_format.self_answered(rv['items'], set(answered), flagged, idx)
            if more:
                rv2 = assemble(cfg, repo, pr, ci, decisions, kept, model['cleared'] + more, model, flagged, flags_cut, money,
                               ruleset, author, model=True, idx=idx, digest=digest, money_info=money_info,
                               conflicts=conflicts)
                if since_block:
                    rv2['since'] = pr_incremental.outcome(since_rows, model.get('earlier'), rv2['items'], prev, pr['headRefOid'])
                body2 = pr_format.render(repo, pr, rv2, NAME, author)
                comments2 = pr_format.inline_comments(rv2['items'], author, cfg.get('voice'), pr_format.first_item(rv2))
                links2 = [(d.get('link') or {}).get('path') for d in rv2['decisions'] if (d.get('link') or {}).get('kind') == 'code']
                if not pr_format.gate(rv2, idx, body2, tree_lines(tree, links2), pr.get('body') or '', flagged, comments2):
                    rv, body, comments = rv2, body2, comments2
                    extra['self_answered'] = len(more)
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
            if since_block:
                rv['since'] = pr_incremental.outcome(since_rows, model.get('earlier'), rv['items'], prev, pr['headRefOid'])
            body = pr_format.render(repo, pr, rv, NAME, author)
            comments = pr_format.inline_comments(rv['items'], author, cfg.get('voice'), pr_format.first_item(rv))
            links = [(d.get('link') or {}).get('path') for d in rv['decisions'] if (d.get('link') or {}).get('kind') == 'code']
            reasons = pr_format.gate(rv, idx, body, tree_lines(tree, links), pr.get('body') or '', flagged, comments)
            extra['r31_questions'] = len(asked)
        if pr_format.fixable(reasons):
            # The flow fix (the mayor, 2026-10-30 16:10Z and 16:36Z): the posted review is never worse than the
            # one it replaces, so a wording or format rule never turns a finished review into a fallback.
            # (1) One narrow turn rewrites the entries a wording rule failed; (2) the script makes right
            # what it can from the parts; (3) the gate and the reader test again. Only a content rule
            # then sends it to the fallback.
            def gated(x):
                b = pr_format.render(repo, pr, x, NAME, author)
                c = pr_format.inline_comments(x['items'], author, cfg.get('voice'), pr_format.first_item(x))
                ln = [(d.get('link') or {}).get('path') for d in x['decisions'] if (d.get('link') or {}).get('kind') == 'code']
                return b, c, pr_format.gate(x, idx, b, tree_lines(tree, ln), pr.get('body') or '', flagged, c)
            rv_r = rv
            if any(pr_format.WORDING_RE.match(r) for r in reasons):
                rv_r, wm = rewrite(cfg, rv, reasons, idx)
                extra.update(rewrite_cost_usd=wm.get('cost_usd') or 0, rewrite_turns=wm.get('turns'),
                             rewrite_asked=wm.get('asked', 0), rewritten=wm.get('rewritten', 0),
                             **({'rewrite_error': wm['error']} if wm.get('error') else {}))
            rv_r, fixed = pr_format.repair(rv_r, reasons)
            body_r, comments_r, again = gated(rv_r)
            if pr_format.format_only(again):  # a rewrite can leave what the script fixes (a missing "?")
                rv_r, more = pr_format.repair(rv_r, again)
                fixed = sorted(set(fixed) | set(more))
                body_r, comments_r, again = gated(rv_r)
            extra['repaired'] = fixed
            if not again and cfg['judge']:
                scores, _, jm = pr_judge.judge(cfg, body_r, comments_r, NAME, author)
                extra['judge_cost_usd'] += jm.get('cost_usd') or 0
                extra.update(judge=scores, repair_judged=True)
                again = reader_reasons(scores, jm, [j for j in jm.get('risky_cleared') or [] if j <= len(rv_r['cleared'])])
            extra['repair_held'] = sorted({r.split(':')[0] for r in again})
            if again:  # still held: the reasons that hold it now (the fallback split below)
                keep_held(cfg, entry, pr, 'repaired', rv_r, body_r, again, flagged, diff, author)
            else:
                rv, body, comments = rv_r, body_r, comments_r
            reasons = again
        # Enum values, counts and rule ids only, so the log can say why reviews end where they do.
        extra.update(decisions=len(rv['decisions']), author_decisions=len(pr_format.split(rv['decisions'])[1]),
                     items=len(rv['items']), cleared=len(rv['cleared']),
                     blockers=len(rv['blockers']), not_checked=bool(rv['not_checked']), approve=rv['approve'],
                     dropped=len(model['items']) + len(model['decisions']) - len(items) - len(decisions),
                     kinds=sorted(i['kind'] for i in items), severity=sorted(i['severity'] for i in items),
                     checks=model['checks'], flagged_hunks=len(flagged), flags_cut=flags_cut, ci=ci,
                     money_places=len(money), money_unlined=sum(1 for x in money if not x.get('what')),
                     **({f'since_{k}': rv['since'][k] for k in ('fixed', 'again', 'open', 'gone')}
                        | {'since_to_resolve': len(rv['since']['resolve'])} if rv.get('since') else {}),
                     gate=sorted({r.split(':')[0] for r in reasons}))
        if cfg.get('collect') is not None:  # the benchmark (pr_bench.py): the review as it stands, nothing delivered
            cfg['collect'].append({'rv': rv, 'held': sorted({r.split(':')[0] for r in reasons}), 'extra': dict(extra),
                                   'model': model, 'body': body, 'reasons': reasons, 'flagged': flagged, 'author': author,
                                   'pr_text': pr.get('body') or '', 'head': pr['headRefOid'], 'diff': diff})
            return
        if reasons:  # the weekly split: held by wording or format reasons only, or by a content rule
            extra['held_by'] = 'format' if pr_format.fixable(reasons) else 'content'
        held = 'held-gate' if reasons else ('held-secret' if leaks([body] + [c['body'] for c in comments]) else None)
        # The mayor, 22:26Z: a review of a PR we reviewed before is never silent, and its fallback says what of
        # our earlier asks is still open (zero tokens: where their lines are now).
        reviewed = bool(entry.get('review_heads'))
        fb_since = pr_incremental.held(since_rows, prev, pr['headRefOid']) if prev and since_rows else None
        fallback = held and pr_rules.held_fallback(
            'requested' in hits, bool(rv['not_checked']),
            held == 'held-gate', reviewed)
        if fallback == 'not-checked-minimal':
            # The gate or the reader test held it twice, but a PR that touches a not-checked area is never silent:
            # first one question per money place (R33, the mayor 21:39Z); without one that passes, line 2, the
            # stock money question and the facts line, with no model findings.
            def gated(x, b, c):
                return pr_format.gate(x, idx, b, tree_lines(tree, linked_paths(x)), pr.get('body') or '', [], c)
            rv, body, comments, ameta = asked_fallback(cfg, repo, pr, ci, idx, [], money, author, ruleset, since=fb_since)
            reasons, form = (gated(rv, body, comments), 'asked') if rv else (['R33: no question'], 'asked')
            if reasons:
                if rv:
                    ameta['gate'] = sorted({r.split(':')[0] for r in reasons})
                rv, body, comments = minimal_not_checked(cfg, repo, pr, ci, money, ruleset, author, money_info, fb_since)
                reasons, form = gated(rv, body, comments), 'stock'
            extra.update(fallback=fallback, held=held, reader_held=extra.get('judge'), decisions=len(rv['decisions']),
                         author_decisions=len(rv['decisions']), items=len(rv['items']), cleared=0,
                         blockers=len(rv['blockers']), held_gate=extra['gate'],
                         gate=sorted({r.split(':')[0] for r in reasons}), **asked_log(ameta, form))
            if cfg['judge'] and not reasons and not extra.get('repair_judged'):  # for the log only: never held by it
                fs, _, fm = pr_judge.judge(cfg, body, comments, NAME, author)
                extra.update(fallback_judge=fs, judge_cost_usd=extra['judge_cost_usd'] + (fm.get('cost_usd') or 0))
            held = 'held-gate' if reasons else None
            fallback = held and pr_rules.held_fallback('requested' in hits, False, False, reviewed)
        if fallback == 'not-reviewed':
            # A PR that requests the reviewer, or one we reviewed before, is never silent: the note, with its floor (R33).
            codes = extra.get('held_gate') or extra['gate']
            rv, body, comments, reasons, note = held_note(
                cfg, repo, pr, ci, idx, flagged, money, author, held, codes, ruleset,
                lambda x, b, c: pr_format.gate(x, idx, b, tree_lines(tree, linked_paths(x)), pr.get('body') or '', [], c),
                since=fb_since)
            extra.update(fallback=fallback, held=held, held_gate=codes, decisions=len(rv['decisions']),
                         author_decisions=len(rv['decisions']), items=len(rv['items']), cleared=0,
                         blockers=len(rv['blockers']), gate=sorted({r.split(':')[0] for r in reasons}), **note)
            if cfg['judge'] and not reasons and not extra.get('repair_judged'):  # for the log: never held by it
                fs, _, fm = pr_judge.judge(cfg, body, comments, NAME, author)
                extra.update(fallback_judge=fs, judge_cost_usd=extra['judge_cost_usd'] + (fm.get('cost_usd') or 0))
            held = 'held-gate' if reasons else None
        if held:
            # Nothing could go out on this head (a floor matching a secret's shape): our change request never
            # stands on a head we did not review (the mayor, 22:26Z).
            if entry.get('rc_live') and entry.get('event') == 'REQUEST_CHANGES' and not cfg.get('no_deliver'):
                extra['rc_lifted'], extra['rc_lift_failed'] = dismiss_stale(repo, pr['number'], 0, cfg['reviewer'],
                                                                            cfg.get('voice'), STALE)
                extra['rc_live'] = bool(extra['rc_lift_failed'])
            finish(cfg, key, dict(entry, head=pr['headRefOid']), held, extra)
            return
        result = {k: rv[k] for k in ('outcome', 'decisions', 'items', 'cleared', 'blockers', 'summary', 'skip', 'not_checked')}
        result.update(rule_ids=rule_ids(hits), flagged=flagged if rv['outcome'] != 'not-reviewed' else [],
                      rules=rv.get('rules') or {}, rules_sha=rv.get('rules_sha') or '')
        if rv.get('fallback'):  # --redeliver renders a fallback as one (R33)
            result.update(fallback=True, others=rv.get('others') or [])
        deliver(cfg, key, entry, pr, rv, body, comments, result, extra, flagged, idx, rereview, model.get('summary') or '')
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
            # PR_REVIEW_QUEUE=requested (decision-57b6d9bb): PRs that request the reviewer and follow-ups only
            queued = [(k, e) for k, e in prs.items() if e.get('status') == 'queued' and pr_rules.in_queue(
                cfg['queue'], 'requested' in e.get('rules', []), e.get('followup') or e.get('review_heads'))]
            ruleset = pr_rules.load_rules(cfg['rules'])
            area_of = pr_rules.areas(ruleset)
            # CI-only escalations need no model and no cap (R11); a PR with a content rule, or one that
            # requests the reviewer (R34), waits its turn for the model.
            ci_keys = [k for k, e in queued if not e.get('followup') and pr_rules.ci_only(e.get('ci'), 'requested' in e.get('rules', []),
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


def selftest():
    """The note and its floor (R33), synthetic data only, no model and no network: a note over many places
    passes its gate (the places cut to the question's, then one link); when the note is held, the floor goes
    out: one place in the script's own words."""
    import tempfile
    tmp = tempfile.mkdtemp()
    cfg = {'voice': 'reviewer', 'work': tmp}
    diff = ''.join(f'diff --git a/src/a{k}.ts b/src/a{k}.ts\n--- a/src/a{k}.ts\n+++ b/src/a{k}.ts\n'
                   f'@@ -1,2 +1,3 @@\n x\n+send(contact.phone)\n y\n' for k in range(10))
    idx = pr_format.diff_index(diff)
    label = 'a message sent to people (SMS, email, push)'
    flagged = [{'id': f'F{k}', 'rule': 'r', 'label': label, 'path': f'src/a{k}.ts', 'line': 2} for k in range(10)]
    pr = {'number': 1, 'headRefOid': 'a' * 40, 'state': 'OPEN', 'mergeable': 'MERGEABLE', 'commits': {'nodes': []}}
    ruleset = {'not_checked': 'billing, plans, credits or pricing'}
    real = lambda x, b, c: pr_format.gate(x, idx, b, None, '', [], c)  # noqa: E731
    rv, body, _, reasons, note = held_note(cfg, 'o/r', pr, 'green', idx, flagged, [], 'Author', 'held-gate', ['R29'],
                                           ruleset, real)
    checks = {'a stock note over ten places passes its gate': reasons == [] and note.get('fallback_form') == 'stock',
              'the stock note shows three places, the other seven collapsed and linked to their lines (no orphan link)':
              body.count('](https://github.com/o/r/blob/') == 10 and '<details><summary>Other places</summary>' in body
              and '/pull/1/files)' not in body}
    checks.update(selftest_asked(cfg, idx, flagged, pr, ruleset, real))
    checks.update(selftest_followup(idx, pr))
    calls = []

    def hold_first(x, b, c):
        calls.append(b)
        return ['R33: held'] if len(calls) == 1 else real(x, b, c)
    rv, body, _, reasons, note = held_note(cfg, 'o/r', pr, 'green', idx, flagged, [], 'Author', 'held-gate', ['R29'],
                                           ruleset, hold_first)
    checks['a held note falls to the floor, which goes out'] = reasons == [] and note.get('floor') and \
        note.get('note_gate') == ['R33'] and note.get('floor_gate') == []
    checks['the floor has one place, in the rule label'] = len(rv['decisions'][0]['where']) == 1 and \
        rv['decisions'][0]['where'][0]['what'] == label
    money = [{'path': 'src/a0.ts', 'line_start': 2, 'line_end': 2, 'what': 'model text', 'hunk': 0}]
    calls.clear()
    rv, body, _, reasons, note = held_note(cfg, 'o/r', pr, 'green', idx, flagged, money, 'Author', 'held-gate', ['R29'],
                                           ruleset, hold_first)
    checks['a money place in the floor is named by the area, not by model text'] = \
        rv['decisions'][0]['where'][0]['what'] == 'a change to billing, plans, credits or pricing' and 'model text' not in body
    import datetime as dt
    held = os.path.join(tmp, 'held.jsonl')
    t = pr_state.now()
    with open(held, 'w') as f:
        for r in [{'pr': 'old', 'at': pr_state.iso(t - dt.timedelta(days=31))}, {'pr': 'new', 'at': pr_state.iso(t)},
                  {'pr': 'unstamped'}]:
            f.write(json.dumps(r) + '\n')
    dropped = pr_state.prune_jsonl(held, 30, t)
    rows = [json.loads(x) for x in open(held)]
    checks['retention: rows older than the bound go, an unstamped row is stamped, mode 600'] = dropped == 1 and \
        [r['pr'] for r in rows] == ['new', 'unstamped'] and rows[1].get('at') == pr_state.iso(t) and \
        os.stat(held).st_mode & 0o777 == 0o600
    checks['retention: nothing to drop leaves the file as it is'] = pr_state.prune_jsonl(held, 30, t) == 0
    checks.update(selftest_retention(tempfile.mkdtemp(), t))
    checks.update(selftest_client_once(cfg, tmp))
    for k, v in checks.items():
        print('ok  ' if v else 'FAIL', k)
    return all(checks.values())


def selftest_asked(cfg, idx, flagged, pr, ruleset, real):
    """R33 (the mayor, 21:39Z): the fallback's own questions, with a stub for the model."""
    asks = [('Shift reminders sent by text', 'Shift reminders now go by text to the phone on file; should staff who '
             'opted out still get them?', 'Staff who opted out of texts would get messages they declined.'),
            ('Cancellation text to the clinic', 'Cancelled visits now send a text to the clinic; who at the clinic '
             'receives it after hours?', 'A cancellation sent to an unattended phone leaves the slot unfilled.'),
            ('Text when lab results ready', 'Lab results now send a text when ready; does the message name the test '
             'or only say results are in?', 'A text that names the test discloses health details on a lock screen.')]

    def stub(prompt):
        return {'summary': '', 'places': [{'n': n, 'changes': [c], 'questions': [q], 'reason': r}
                                          for n, (c, q, r) in enumerate(asks, 1)]}, \
            {'cost_usd': 0.01}
    rv, body, comments, reasons, note = held_note(cfg, 'o/r', pr, 'green', idx, flagged, [], 'Author', 'held-gate',
                                                  ['R29'], ruleset, real, answer=stub)
    checks = {'the note asks one question per place; the other seven are collapsed, each linked to its lines':
              reasons == [] and note.get('fallback_form') == 'asked' and len(rv['items']) == 3 and len(comments) == 3
              and not rv['decisions'] and body.count('](https://github.com/o/r/blob/') == 7 and '/pull/1/files)' not in body,
              "in the reviewer's voice line 1 says who acts": body.splitlines()[0].startswith(
                  '**Questions** · for Author, nothing needed from me <!--'),
              'each question carries its own reason as its inline comment':
              [c['body'] for c in comments] == [f'**{k}. {c}**: {r}' for k, (c, _, r) in enumerate(asks, 1)],
              'the log has counts and the form only': note.get('fallback_kept') == 3 and note.get('fallback_calls') == 1
              and not any(isinstance(v, str) and len(v) > 12 for v in note.values())}
    _, _, _, reasons, note = held_note(cfg, 'o/r', pr, 'green', idx, flagged, [], 'Author', 'held-gate', ['R29'], ruleset,
                                       lambda x, b, c: ['R33: held'] if x['items'] else real(x, b, c), answer=stub)
    checks['asked questions the gate holds fall to the stock note'] = reasons == [] and \
        note.get('fallback_form') == 'stock' and note.get('fallback_gate') == ['R33']
    rv, _, _, reasons, note = held_note(cfg, 'o/r', pr, 'green', idx, flagged, [], 'Author', 'held-gate', ['R29'],
                                        ruleset, real, answer=lambda p: (None, {}))
    checks['no question passes in two turns: the stock note'] = reasons == [] and rv['decisions'] and \
        note.get('fallback_form') == 'stock' and note.get('fallback_calls') == 2
    money = [{'path': f'src/a{k}.ts', 'line_start': 2, 'line_end': 2, 'hunk': 0, 'what': 'model line', 'kind': 'charges',
              'behaviour': True, 'risk': 4 - k} for k in range(4)]
    red = dict(pr, commits={'nodes': [{'commit': {'statusCheckRollup': {'contexts': {'nodes': [
        {'name': 'unit', 'conclusion': 'FAILURE'}]}}}}]})
    rv, body, comments, meta = asked_fallback(dict(cfg, request_changes=True), 'o/r', red, 'red', idx, flagged, money,
                                              'Author', ruleset, answer=stub)
    checks['the money review asks about its money places only, the CI blocker said once, no scope sentence'] = \
        rv['outcome'] == 'changes' and pr_format.event_of(rv) == 'REQUEST_CHANGES' and \
        pr_format.gate(rv, idx, body, None, '', [], comments) == [] and [i['path'] for i in rv['items']] == \
        ['src/a0.ts', 'src/a1.ts', 'src/a2.ts'] and 'CI red' not in body and 'outside this review' not in body and \
        '- Billing, plans, credits or pricing: [`a3.ts` L2]' in body and meta['kept'] == 3
    return checks


def selftest_followup(idx, pr):
    """decision-57b6d9bb: the review rebuilt from the author's answers, gated as delivered."""
    q = {'side': 'new', 'line_start': 2, 'line_end': 2, 'kind': 'question', 'severity': 'low', 'area': 'other',
         'why': 'Texts can reach a shared phone.', 'rule': '', 'verified': 'read it', 'confidence': 8}
    res = {'items': [dict(q, path='src/a0.ts', change='Reminder sent by text', ask='Does a reminder go to the home phone?'),
                     dict(q, path='src/a1.ts', change='Cancellation sent by text', ask='Does a cancellation name the visit?')],
           'decisions': [], 'cleared': [], 'blockers': [], 'summary': 'Reminders and cancellations now go out by text.',
           'skip': [], 'not_checked': '', 'flagged': [], 'rules': {}}
    asks = pr_followup.asks_of(res)
    cfg = {'voice': 'reviewer', 'request_changes': False, 'approve': True}
    green = dict(pr, commits={'nodes': [{'commit': {'statusCheckRollup': {'state': 'SUCCESS', 'contexts': {
        'totalCount': 1, 'nodes': [{'conclusion': 'SUCCESS'}]}}}}]})
    half = [{'id': 'E1', 'status': 'answered', 'why': 'Reminders go only to the mobile number the patient confirmed.'},
            {'id': 'E2', 'status': 'open', 'why': 'Not answered yet.', 'follow_up': 'Does the text say what the visit was for?'}]
    rv, replies = rebuild(cfg, 'o/r', green, 'green', res, half, asks, 'Author')
    body = pr_format.render('o/r', green, rv, NAME, 'Author')
    checks = {'a follow-up: the answered ask is cleared with the answer, the open one stays, the line says so':
              rv['outcome'] == 'questions' and len(rv['cleared']) == 1 and rv['cleared'][0]['answered'] and
              '**Since your answers:** 1 answered, 1 still open in its thread.' in body and
              pr_format.gate(rv, idx, body, None, '', [], None) == [] and
              replies == [('E2', 'Does the text say what the visit was for?')]}
    every = [dict(half[0]), {'id': 'E2', 'status': 'answered', 'why': 'The text says only the date and the clinic name.'}]
    rv, _ = rebuild(cfg, 'o/r', green, 'green', res, every, asks, 'Author')
    rv['approve'] = not pr_format.gate_approve(rv, green, green['headRefOid'], [], idx)
    body = pr_format.render('o/r', green, rv, NAME, 'Author')
    checks['everything answered on a green PR: an approval under the bar (R27)'] = rv['outcome'] == 'low-risk' and \
        rv['approve'] and pr_format.event_of(rv) == 'APPROVE' and pr_format.gate(rv, idx, body, None, '', [], None) == []
    money = dict(res, fallback=True, not_checked='billing, plans, credits or pricing', summary='',
                 items=[dict(i, money=True) for i in res['items']], others=[])
    rv, _ = rebuild(cfg, 'o/r', green, 'green', money, every, pr_followup.asks_of(money), 'Author')
    body = pr_format.render('o/r', green, rv, NAME, 'Author')
    checks['a fallback whose money questions are all answered: low risk, never an approval, no "for" on line 1'] = \
        rv['outcome'] == 'low-risk' and rv['fallback'] and rv['signed_off'] and not rv['approve'] and \
        ' · for ' not in body.splitlines()[0] and pr_format.gate(rv, idx, body, None, '', [], None) == []
    return checks


def selftest_retention(work, t):
    """The sweep's retention pass (the mayor, 2026-10-30 17:50Z): reader.jsonl and the client words by age;
    a posted result stays while its PR is open and goes PR_REVIEW_KEEP_DAYS after the PR merged or closed."""
    old, new = pr_state.iso(t - dt.timedelta(days=31)), pr_state.iso(t - dt.timedelta(days=2))
    cfg = {'work': work, 'keep_days': 30}
    reader = os.path.join(pr_state.private_dir(cfg, 'feedback'), 'reader.jsonl')
    with open(reader, 'w') as f:
        f.write(json.dumps({'pr': 'old', 'at': old}) + '\n' + json.dumps({'pr': 'new', 'at': new}) + '\n')
    with open(os.path.join(pr_state.private_dir(cfg, 'client-slack'), 'words.json'), 'w') as f:
        json.dump({'o/r#1': {'words': 'a b c', 'at': old}, 'o/r#2': {'words': 'a b c', 'at': new}}, f)
    posted = pr_state.private_dir(cfg, 'feedback', 'posted')
    files = {'o-x__r-y-1-aaaaaaaaaa.json': old, 'o-x__r-y-2-bbbbbbbbbb.json': old, 'o-x__r-y-3-cccccccccc.json': old,
             'o-x__r-y-4-dddddddddd.json': new, 'o-x__r-y-5-eeeeeeeeee.json': old}
    for name, at in files.items():
        with open(os.path.join(posted, name), 'w') as f:
            json.dump({'at': at}, f)
    closed = {1: None, 2: t - dt.timedelta(days=31), 3: t - dt.timedelta(days=3), 4: t - dt.timedelta(days=40)}
    asked = []

    def closed_at(repo, n):  # 1 open, 2 merged 31 days ago, 3 closed 3 days ago, 4 never asked, 5 unknown
        asked.append((repo, n))
        return closed.get(n)
    got = pr_state.prune_private(cfg, closed_at)
    left = sorted(os.listdir(posted))
    return {'retention: reader.jsonl and the client words by age; a posted result goes 30 days after its PR closed':
            got == {'held': 0, 'bench': 0, 'reader': 1, 'words': 1, 'posted': 1} and
            [json.loads(x)['pr'] for x in open(reader)] == ['new'] and
            list(json.load(open(os.path.join(work, 'client-slack', 'words.json')))) == ['o/r#2'] and
            left == sorted(set(files) - {'o-x__r-y-2-bbbbbbbbbb.json'}),
            'retention: only results older than the bound are looked up, by repository and number':
            sorted(asked) == [('o-x/r-y', 1), ('o-x/r-y', 2), ('o-x/r-y', 3), ('o-x/r-y', 5)]}


def selftest_client_once(cfg, tmp):
    """deliver() with GitHub, our Slack and the client runner stubbed (the mayor, 2026-10-30 19:12Z): with our
    channel empty, a same-head re-review sends no second client line and makes no Slack call; a new head
    sends its line once; the entry keeps the head's post, its ts and the review's confidence for the queue;
    a failed client entry never keeps the delivered review from being recorded."""
    import contextlib
    import io
    url, head = 'https://github.com/o/r/pull/1', 'c' * 40
    fresh = {'number': 1, 'url': url, 'headRefOid': head, 'state': 'OPEN', 'mergeable': 'MERGEABLE', 'commits': {'nodes': []}}
    ids, sent, slack = iter(range(5, 99)), [], []
    g = globals()
    saved = {k: g[k] for k in ('post_review', 'update_review', 'dismiss_stale', 'repoint', 'ours_on_head')}
    mods = [(pr_rules, 'get_pr'), (pr_state, 'slack_token'), (pr_state, 'slack_call'), (pr_client_slack, 'deliver_entry')]
    kept = [getattr(m, n) for m, n in mods]

    def posted(*a, **k):
        n = next(ids)
        return 'ok', f'{url}#pullrequestreview-{n}', 'COMMENTED', n
    g.update(post_review=posted, update_review=lambda repo, n, rid, body: ('ok', f'{url}#pullrequestreview-{rid}', 'COMMENTED', rid),
             dismiss_stale=lambda *a: (0, 0), repoint=lambda *a: (0, 0), ours_on_head=lambda *a: 1)
    pr_rules.get_pr = lambda repo, n: fresh
    pr_state.slack_token = lambda *a: slack.append('token')
    pr_state.slack_call = lambda *a: slack.append('call') or {}
    pr_client_slack.deliver_entry = lambda c, key, e, review, pr=None, summary='', turn=None: \
        sent.append((e['review_url'], review['confidence'])) or ('ok', {'ts': '1793385205.766639', 'words_from': 'summary'})
    dcfg = dict(cfg, state=tmp, city=tmp, beads=False, mode='live', slack_channel='', slack_env=[], client_slack='on',
                reviewer='rev', request_changes=False)
    rv = {'outcome': 'questions', 'approve': False, 'request_changes': False, 'decisions': [], 'blockers': [], 'gaps': [],
          'cleared': [], 'items': [{'kind': 'question'}]}
    entry = {'repo': 'o/r', 'number': 1, 'url': url, 'rig': 'r'}
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            deliver(dcfg, 'o/r#1', entry, fresh, rv, 'body', [], {'flagged': []}, {'ci': 'green'}, summary='Adds a thing')
            with pr_state.locked(tmp) as st:
                e1 = dict(st['prs']['o/r#1'])
            deliver(dcfg, 'o/r#1', e1, fresh, rv, 'body', [], {'flagged': []}, {'ci': 'green'}, rereview=True)
            with pr_state.locked(tmp) as st:
                e2 = dict(st['prs']['o/r#1'])
            fresh['headRefOid'] = 'd' * 40
            deliver(dcfg, 'o/r#1', e2, fresh, rv, 'body', [], {'flagged': []}, {'ci': 'green'})
            with pr_state.locked(tmp) as st:
                e3 = dict(st['prs']['o/r#1'])
            pr_client_slack.deliver_entry = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('x'))
            fresh['headRefOid'] = 'e' * 40
            deliver(dcfg, 'o/r#1', e3, fresh, rv, 'body', [], {'flagged': []}, {'ci': 'green'})
            with pr_state.locked(tmp) as st:
                e4 = dict(st['prs']['o/r#1'])
            tries = []

            def picky(repo, pr, body, comments, event='COMMENT'):  # GitHub refuses the inline comments
                tries.append(len(comments))
                return ('error', None, None, None) if comments else posted()
            g['post_review'] = picky
            fresh['headRefOid'] = 'f' * 40
            deliver(dcfg, 'o/r#1', e4, fresh, dict(rv, fallback=True), 'body',
                    [{'path': 'a', 'line': 1, 'side': 'RIGHT', 'body': 'x'}], {'flagged': []}, {'ci': 'green'})
            with pr_state.locked(tmp) as st:
                e5 = dict(st['prs']['o/r#1'])
            # decision-57b6d9bb: a same-head follow-up posts a new client line only when the status moved
            pr_client_slack.deliver_entry = lambda c, key, e, review, pr=None, summary='', turn=None: \
                sent.append((e['review_url'], review['confidence'])) or ('ok', {'ts': '1793385999.000001'})
            e6, posts = e5, []
            for x in (rv, rv, dict(rv, outcome='changes', items=[{'kind': 'change'}])):
                deliver(dcfg, 'o/r#1', e6, fresh, x, 'body', [], {'flagged': []}, {'ci': 'green'}, rereview=True)
                with pr_state.locked(tmp) as st:
                    e6 = dict(st['prs']['o/r#1'])
                posts.append(len(sent))
    finally:
        g.update(saved)
        for (m, n), f in zip(mods, kept):
            setattr(m, n, f)
    return {
        'our Slack empty + a same-head re-review: no second client line, no Slack call; a new head posts once':
            [s[1] for s in sent[:2]] == ['medium', 'medium'] and sent[0][0] != sent[1][0] and not slack and
            e2.get('review_url') == e1.get('review_url'),
        'the entry keeps the client post per head, its ts and the confidence for the queue':
            e1.get('client_posts') == {head: {'status': 'ok', 'ts': '1793385205.766639', 'ready': 'Needs answers'}} and
            e1.get('client_ts') == '1793385205.766639' and e1.get('confidence') == 'medium' and
            list(e3.get('client_posts') or {}) == [head, 'd' * 40],
        'a failed client entry still records the delivered review':
            'e' * 40 in (e4.get('review_heads') or []) and e4.get('client_slack') == 'error:RuntimeError',
        'same head: a new client line once per status (Needs answers, then nothing, then Needs changes)':
            posts == [3, 3, 4] and (e6.get('client_posts') or {}).get('f' * 40, {}).get('ready') == 'Needs changes',
        'a fallback whose inline comments GitHub refuses goes out without them, never silent (R33)':
            tries == [1, 0] and 'f' * 40 in (e5.get('review_heads') or []) and e5.get('inline_dropped') == 1 and
            e5.get('inline') == 0,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--pr', metavar='REPO#N')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--redeliver', metavar='REPO#N')
    ap.add_argument('--rereview', metavar='REPO#N')
    ap.add_argument('--no-deliver', action='store_true')
    ap.add_argument('--followup', metavar='REPO#N', help="the same-head follow-up on the author's answers")
    ap.add_argument('--selftest', action='store_true', help='the note and its floor on synthetic data')
    a = ap.parse_args()
    if a.selftest:
        return 0 if selftest() else 1
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
    if a.followup:  # decision-57b6d9bb: the same-head follow-up on the author's answers, by hand
        cfg['no_deliver'] = a.no_deliver
        with pr_state.locked(cfg['state']) as st:
            entry = dict(st['prs'].get(a.followup) or {})
        if not entry.get('review_heads'):
            print(json.dumps({'pr': a.followup, 'followup': 'no review of ours on this PR'}))
            return
        answer_review(cfg, a.followup, dict(entry, followup='answer'), a.dry_run)
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
    for k in chosen:  # hands the PR to model_review unless only CI is wrong; answers go to their follow-up
        if snap[k].get('followup') in ('answer', 'cap'):
            answer_review(cfg, k, snap[k], a.dry_run)
        else:
            ci_escalation(cfg, k, snap[k], a.dry_run, rereview=bool(a.rereview))
    if not (chosen or ci_keys or expiring):
        print(json.dumps({'idle': True, 'reviews_today': used, 'cap': cfg['cap']}))


if __name__ == '__main__':
    sys.exit(main())
