#!/usr/bin/env python3
"""State, beads and Slack for the PR review orders (README.md).

The state file ($PR_REVIEW_STATE_DIR/prs.json) and the beads hold only a PR's repo, number, link, head
SHA, outcome, rule ids, tier, times and cost: no title, path, line or finding. Slack gets the outcome
word and the link, nothing else.
"""
import contextlib
import datetime as dt
import fcntl
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request

# Outcome -> (Slack word in shadow mode, Slack word in live mode). None: nothing goes to Slack.
OUTCOMES = {
    'low-risk': ('Low risk', 'Low risk'),
    'questions': ('Questions for the author', 'Questions for the author'),
    'changes': ('Changes requested', 'Changes requested'),
    'escalate': ('Needs you', 'Needs you'),
    'not-reviewed': ('Not reviewed', 'Not reviewed'),
    'no-rule': (None, None),
    'over-cap': (None, None),
    'closed': (None, None),
    'error': (None, None),
    'held-gate': (None, None),
    'held-secret': (None, None),
    'held-inline': (None, None),
    'requeued': (None, None),
    'skipped': (None, None),
}


HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORK = '/var/tmp/gc-pr-review'


def config():
    """Both orders' settings, from the environment (README.md, "Configuration")."""
    e = os.environ.get
    city = e('GC_CITY_PATH') or e('GC_CITY') or os.getcwd()
    repos = {}
    for item in e('PR_REVIEW_REPOS', '').replace(',', ' ').split():
        repo, _, rig = item.partition('=')
        repos[repo] = rig or repo.split('/')[1]
    mode = e('PR_REVIEW_MODE', 'shadow')
    if mode not in ('shadow', 'live'):
        raise SystemExit(f'PR_REVIEW_MODE must be shadow or live, not {mode!r}')
    work = e('PR_REVIEW_WORK_DIR') or DEFAULT_WORK
    return {
        # gcs-71c: eval PRs, reviewed by hand in their own work dir and state; 'none' delivers nothing (the
        # mayor, 2026-10-31 00:18Z: until the reviewer picks), 'comment' one COMMENT on the draft.
        'eval_deliver': 'comment' if e('PR_REVIEW_EVAL_DELIVER', 'none') == 'comment' else 'none',
        'eval_work': e('PR_REVIEW_EVAL_WORK_DIR') or os.path.join(work, 'eval'),
        'eval_out': e('PR_REVIEW_EVAL_OUT_DIR') or os.path.join(work, 'redteam', 'eval'),
        'eval_run': e('PR_REVIEW_EVAL_RUN', ''),
        'city': city,
        'on': e('PR_REVIEW', 'off') == 'on',
        'mode': mode,
        'repos': repos,
        'reviewer': e('PR_REVIEW_REVIEWER', ''),
        'rules': e('PR_REVIEW_RULES') or os.path.join(HERE, '..', 'rules.toml'),
        'checklist': e('PR_REVIEW_CHECKLIST') or os.path.join(HERE, '..', 'checklist-health.md'),
        # decision-fff4a691: no daily cap unless one is set ('off' = none).
        'cap': NO_CAP if e('PR_REVIEW_DAILY_CAP', 'off') == 'off' else int(e('PR_REVIEW_DAILY_CAP')),
        'reserve': int(e('PR_REVIEW_RESERVE', '3')),
        'max_wait_h': float(e('PR_REVIEW_MAX_WAIT_H', '24')),
        'lookback_h': float(e('PR_REVIEW_LOOKBACK_H', '2')),
        'ci_wait_h': float(e('PR_REVIEW_CI_WAIT_H', '3')),
        'state': e('PR_REVIEW_STATE_DIR') or os.path.join(city, '.gc', 'runtime', 'pr-review'),
        'work': work,
        'keep_days': int(e('PR_REVIEW_KEEP_DAYS', '30')),  # whole reviews kept host-local (held, bench)
        'max_load': float(e('PR_REVIEW_MAX_LOAD', '15')),
        'beads': e('PR_REVIEW_BEADS', 'on') == 'on',
        'slack_channel': e('PR_REVIEW_SLACK_CHANNEL', ''),
        'slack_env': e('PR_REVIEW_SLACK_ENV', '~/.config/gc/slack.env ~/.config/gc/slack2.env').split(),
        # decision-14bb1b55: the client's own Slack, as the reviewer, only through pr_client_slack.py's runner.
        'client_slack': e('PR_REVIEW_CLIENT_SLACK', 'off') if e('PR_REVIEW_CLIENT_SLACK', 'off') in ('on', 'dry-run') else 'off',
        'client_slack_channel': e('PR_REVIEW_CLIENT_SLACK_CHANNEL', ''),
        'client_slack_conversations': set(e('PR_REVIEW_CLIENT_SLACK_CONVERSATIONS', '').replace(',', ' ').split()),
        'client_slack_tool': e('PR_REVIEW_CLIENT_SLACK_TOOL', ''),
        'client_slack_repos': set(e('PR_REVIEW_CLIENT_SLACK_REPOS', '').replace(',', ' ').split()),
        'client_slack_model': e('PR_REVIEW_CLIENT_SLACK_MODEL', 'haiku'),
        'client_slack_max_turns': int(e('PR_REVIEW_CLIENT_SLACK_MAX_TURNS', '3')),
        'client_slack_queue_at': e('PR_REVIEW_CLIENT_SLACK_QUEUE_AT', '08:00'),
        'client_slack_tz': e('PR_REVIEW_CLIENT_SLACK_TZ', 'Europe/Berlin'),
        'model': e('PR_REVIEW_MODEL', 'opus'),
        'effort': e('PR_REVIEW_EFFORT', 'high'),
        'prompt': e('PR_REVIEW_PROMPT', 'security'),
        'context': e('PR_REVIEW_CONTEXT', 'off') == 'on',
        'suggestions': e('PR_REVIEW_SUGGESTIONS', 'off') == 'on',
        'incremental': e('PR_REVIEW_INCREMENTAL', 'off') == 'on',
        # decision-57b6d9bb: a PR we reviewed is reviewed again on a new head (incremental) and followed up on
        # the author's answers, at most PR_REVIEW_FOLLOWUP_ROUNDS rounds per head. PR_REVIEW_QUEUE=requested:
        # only PRs that request the reviewer, and those follow-ups, enter the queue.
        'followups': e('PR_REVIEW_FOLLOWUPS', 'on') == 'on',
        'followup_rounds': int(e('PR_REVIEW_FOLLOWUP_ROUNDS', '3')),
        'queue': 'requested' if e('PR_REVIEW_QUEUE', 'all') == 'requested' else 'all',
        # decision-ed7320b7: 'reviewer' = the review speaks as the reviewer (no disclosure line); the pack
        # default 'disclose' says on line 1 that it is automated and the reviewer hasn't read the PR.
        'voice': 'reviewer' if e('PR_REVIEW_VOICE', 'disclose') == 'reviewer' else 'disclose',
        # decision-fff4a691: turns bound each call, never dollars. The review used 2-11 turns (10-30), so
        # 33; the tool-less helpers (reader, money lines, digest, bench labels) answer in 1-2.
        'max_turns': int(e('PR_REVIEW_MAX_TURNS', '33')),
        'timeout_s': int(e('PR_REVIEW_TIMEOUT_S', '1200')),
        'sweep_budget_s': int(e('PR_REVIEW_SWEEP_BUDGET_S', '240')),
        'approve': e('PR_REVIEW_APPROVE', 'off') == 'on',
        'request_changes': e('PR_REVIEW_REQUEST_CHANGES', 'off') == 'on',
        'judge': e('PR_REVIEW_JUDGE', 'on') == 'on',
        'judge_model': e('PR_REVIEW_JUDGE_MODEL', 'sonnet'),
        'judge_max_turns': int(e('PR_REVIEW_JUDGE_MAX_TURNS', '6')),
        'digest_model': e('PR_REVIEW_DIGEST_MODEL', 'sonnet'),
        'digest_max_turns': int(e('PR_REVIEW_DIGEST_MAX_TURNS', '6')),
        'panel': e('PR_REVIEW_PANEL') or os.path.join(e('PR_REVIEW_STATE_DIR') or os.path.join(city, '.gc', 'runtime', 'pr-review'), 'panel.json'),
    }


def now():
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def iso(t):
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def parse(s):
    return dt.datetime.fromisoformat(s.replace('Z', '+00:00'))


@contextlib.contextmanager
def locked(state_dir):
    """The state file, read and written under an exclusive lock (the two orders can overlap). Hold it
    briefly: a review takes the lock to pick and again to record, never while the model runs."""
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, 'prs.json')
    with open(os.path.join(state_dir, 'state.lock'), 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = {'prs': {}}
        if os.path.exists(path):
            with open(path) as f:
                st = json.load(f)
        yield st
        with open(path + '.tmp', 'w') as f:
            json.dump(st, f, indent=1, sort_keys=True)
        os.replace(path + '.tmp', path)


def log(state_dir, rec):
    """One JSON line per run or action to $state/pr-review.jsonl (ids, counts, outcomes, cost only)."""
    os.makedirs(state_dir, exist_ok=True)
    rec = {'at': iso(now()), **rec}
    with open(os.path.join(state_dir, 'pr-review.jsonl'), 'a') as f:
        f.write(json.dumps(rec, sort_keys=True) + '\n')


class Beads:
    """One bead per PR in the rig's own store: title "PR review <repo>#<n>", labels pr-review, and
    metadata pr_repo, pr_number, pr_url, pr_head, pr_outcome. Nothing else is written to it."""

    def __init__(self, city, enabled=True):
        self.city = city
        self.enabled = enabled
        self.gc = os.environ.get('GC_BIN') or 'gc'

    def _run(self, rig, *args):
        r = subprocess.run([self.gc, 'bd', '--rig', rig, *args], cwd=self.city, capture_output=True,
                           text=True, timeout=120)
        if r.returncode != 0:
            raise RuntimeError(f'gc bd {args[0]} on rig {rig} failed (rc {r.returncode})')
        return r.stdout.strip()

    def create(self, rig, repo, number, url, head, outcome):
        if not self.enabled:
            return None
        meta = {'pr_repo': repo, 'pr_number': str(number), 'pr_url': url, 'pr_head': head, 'pr_outcome': outcome}
        out = self._run(rig, 'create', '--silent', '--type', 'task', '--labels', 'pr-review', '--priority', '3',
                        '--title', f'PR review {repo}#{number}', '--description', url,
                        '--metadata', json.dumps(meta))
        ids = re.findall(r'\b[a-z0-9]+-[a-z0-9.]+\b', out)
        return ids[-1] if ids else None

    def set(self, rig, bead, head=None, outcome=None, close=False):
        if not self.enabled or not bead:
            return
        args = ['update', bead]
        if head:
            args += ['--set-metadata', f'pr_head={head}']
        if outcome:
            args += ['--set-metadata', f'pr_outcome={outcome}']
        if len(args) > 2:
            self._run(rig, *args)
        if close:
            self.close(rig, bead, outcome or 'done')

    def close(self, rig, bead, reason):
        """Close once: a bead already closed is left as it is."""
        if self.enabled and bead and self.status(rig, bead) != 'closed':
            self._run(rig, 'close', bead, '--reason', reason)

    def status(self, rig, bead):
        out = json.loads(self._run(rig, 'show', bead, '--json'))
        return (out[0] if isinstance(out, list) else out).get('status')


def prune_jsonl(path, days, at_now=None):
    """Drop the rows older than `days` from a host-local JSON-lines file (the mayor, 2026-10-30 16:50Z: the
    held attempts and the benchmark's reviews hold a client's PR descriptions and diffs). A row without
    `at` is stamped now, so it ages out too. Rewritten only when something changes, mode 600. Returns the
    rows dropped."""
    if not os.path.exists(path):
        return 0
    at_now = at_now or now()
    cut, keep, dropped, stamped = at_now - dt.timedelta(days=days), [], 0, False
    for line in open(path):
        try:
            r = json.loads(line)
        except ValueError:
            dropped += 1
            continue
        if not r.get('at'):
            r['at'], stamped = iso(at_now), True
        elif parse(r['at']) < cut:
            dropped += 1
            continue
        keep.append(json.dumps(r))
    if dropped or stamped:
        tmp = path + '.tmp'
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
            f.write(''.join(x + '\n' for x in keep))
        os.replace(tmp, path)
    return dropped


def prune_map(path, days, at_now=None):
    """prune_jsonl for a host-local JSON map {key: {..., 'at'}} (the client entry's words). Returns the rows
    dropped."""
    if not os.path.exists(path):
        return 0
    at_now = at_now or now()
    try:
        rows = json.load(open(path))
    except ValueError:
        rows = {}
    cut, keep, stamped = at_now - dt.timedelta(days=days), {}, False
    for k, r in rows.items():
        if not isinstance(r, dict):
            continue
        if not r.get('at'):
            r['at'], stamped = iso(at_now), True
        if parse(r['at']) >= cut:
            keep[k] = r
    if len(keep) < len(rows) or stamped:
        with os.fdopen(os.open(path + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
            json.dump(keep, f)
        os.replace(path + '.tmp', path)
    return len(rows) - len(keep)


POSTED_NAME = re.compile(r'(.+)-(\d+)-[0-9a-f]{1,40}\.json')


def prune_posted(cfg, closed_at, at_now=None):
    """feedback/posted: a delivered review's result stays while its PR is open, and goes PR_REVIEW_KEEP_DAYS
    after the PR merged or closed (the mayor, 2026-10-30 17:50Z: --redeliver never needs a closed PR's
    result). A PR closes after its review was delivered, so only files older than that are looked up, once
    per PR, through `closed_at(repo, number)`: the merge or close time, or None while open or unknown (then
    the file stays). Returns the files dropped."""
    d = os.path.join(cfg['work'], 'feedback', 'posted')
    if not os.path.isdir(d):
        return 0
    cut, dropped, seen = (at_now or now()) - dt.timedelta(days=cfg['keep_days']), 0, {}
    for name in sorted(os.listdir(d)):
        m, p = POSTED_NAME.fullmatch(name), os.path.join(d, name)
        try:
            at = parse(json.load(open(p)).get('at') or '') if m else None
        except (ValueError, OSError):
            at = dt.datetime.fromtimestamp(os.path.getmtime(p), dt.timezone.utc) if m else None
        if at is None or at >= cut:
            continue
        pr = (m.group(1).replace('__', '/', 1), int(m.group(2)))
        if pr not in seen:
            seen[pr] = closed_at(*pr)
        if seen[pr] is not None and seen[pr] < cut:
            os.remove(p)
            dropped += 1
    return dropped


def prune_private(cfg, closed_at=None):
    """The retention bound on the host-local stores that hold the repositories' text (PR_REVIEW_KEEP_DAYS):
    feedback/held.jsonl, the benchmark's <repo>-reviews-<version>.jsonl, feedback/reader.jsonl and the
    client entry's words by rows; feedback/posted by its PR's merge or close (with `closed_at`, see
    prune_posted). Returns the rows or files dropped per store."""
    bench = os.path.join(cfg['work'], 'bench')
    days = cfg['keep_days']
    out = {'held': prune_jsonl(os.path.join(cfg['work'], 'feedback', 'held.jsonl'), days),
           'bench': sum(prune_jsonl(os.path.join(bench, f), days) for f in sorted(os.listdir(bench))
                        if '-reviews-' in f and f.endswith('.jsonl')) if os.path.isdir(bench) else 0,
           'reader': prune_jsonl(os.path.join(cfg['work'], 'feedback', 'reader.jsonl'), days),
           'words': prune_map(os.path.join(cfg['work'], 'client-slack', 'words.json'), days)}
    if closed_at:
        out['posted'] = prune_posted(cfg, closed_at)
    return out


def private_dir(cfg, *parts):
    """A dir under PR_REVIEW_WORK_DIR (mode 700) for what holds the repositories' text: posted review
    bodies, the reviewer's edits and replies, host-local regression cases. Never the town runtime."""
    d = cfg['work']
    for part in parts:  # each level 700: os.makedirs gives the mode to the last one only
        d = os.path.join(d, part)
        os.makedirs(d, mode=0o700, exist_ok=True)
    return d


def keep_posted(cfg, entry, head, body, comments, result=None):
    """The delivered body, comments and structured result, host-local: feedback and the regression set
    read them, and --redeliver rebuilds a review from them without a new model run."""
    p = os.path.join(private_dir(cfg, 'feedback', 'posted'),
                     f"{entry['repo'].replace('/', '__')}-{entry['number']}-{head[:10]}.json")
    with open(p, 'w') as f:
        json.dump({'url': entry['url'], 'head': head, 'body': body, 'comments': comments, 'result': result,
                   'at': iso(now())}, f)
    os.chmod(p, 0o600)


def slack_get(token, method, params):
    """A read-only Slack call; conversations.* and reactions.* take query parameters, not JSON."""
    req = urllib.request.Request(f'https://slack.com/api/{method}?' + urllib.parse.urlencode(params),
                                 headers={'Authorization': f'Bearer {token}'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def slack_token(env_files):
    """The first xoxb- bot token in the env files (the town's slack.env convention)."""
    for f in env_files:
        try:
            with open(os.path.expanduser(f)) as fh:
                m = re.search(r'xoxb-[A-Za-z0-9-]+', fh.read())
        except OSError:
            continue
        if m:
            return m.group(0)
    return None


def slack_call(token, method, payload):
    req = urllib.request.Request(f'https://slack.com/api/{method}', data=json.dumps(payload).encode(),
                                 headers={'Authorization': f'Bearer {token}',
                                          'Content-Type': 'application/json; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


NO_CAP = 10 ** 6  # PR_REVIEW_DAILY_CAP=off
APPROVED = 'Approved as low risk'
CHANGES_SUGGESTED = 'Changes suggested'
# decision-3aeec149: "needs me should be reserved for escalations that cannot be escalated to [the author],
# such as potential gaps in our review methodology". The PR gets its normal review; Slack says this.
REVIEW_GAP = 'Review gap'
ACCEPTED_GAPS = ['not-checked']  # gap kinds the reviewer accepted (billing: "ignore, but be honest we don't check it")


def accepted_gaps(cfg):
    """The gap kinds the reviewer accepted, host-local (gaps/accepted.json; written with the default once)."""
    p = os.path.join(private_dir(cfg, 'gaps'), 'accepted.json')
    if not os.path.exists(p):
        with open(p, 'w') as f:
            json.dump(ACCEPTED_GAPS, f)
        os.chmod(p, 0o600)
    return set(json.load(open(p)))


def slack_post(cfg, outcome, url, review_url, approved=False, ts=None, event='COMMENT', gap=False):
    """Post '<word>: <review URL>' to the channel (R17): the submitted review's own GitHub URL. The text is
    built here from a fixed word and the link; nothing the model wrote reaches Slack. The word matches the
    review's event (R35): changes are "requested" only on a REQUEST_CHANGES review. With `ts`, the
    message of an earlier review of the same head is updated in place instead (R18). Returns (status,
    message ts): 'ok', 'off' or an error code; the ts lets the feedback order find reactions and replies.
    `gap`: a gap in our method the reviewer has not accepted, so the word is "Review gap" (decision-3aeec149)."""
    word = APPROVED if approved else OUTCOMES[outcome][1 if cfg['mode'] == 'live' else 0]
    if outcome == 'changes' and event != 'REQUEST_CHANGES':
        word = CHANGES_SUGGESTED
    if gap:
        word = REVIEW_GAP
    if not word or not cfg['slack_channel']:
        return 'off', None
    if not re.fullmatch(r'https://github\.com/[\w.-]+/[\w.-]+/pull/\d+#pullrequestreview-\d+', review_url or '') or \
            not (review_url or '').startswith(url + '#'):
        return 'bad-url', None
    token = slack_token(cfg['slack_env'])
    if not token:
        return 'no-token', None
    try:
        msg = {'channel': cfg['slack_channel'], 'text': f'{word}: {review_url}', 'unfurl_links': False,
               'unfurl_media': False}
        r = slack_call(token, 'chat.update', dict(msg, ts=ts)) if ts else slack_call(token, 'chat.postMessage', msg)
    except OSError as e:
        return f'error:{type(e).__name__}', None
    if not r.get('ok'):
        return f"error:{r.get('error')}", None
    return ('updated' if ts else 'ok'), r.get('ts')
