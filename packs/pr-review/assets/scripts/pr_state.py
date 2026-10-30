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
    return {
        'city': city,
        'on': e('PR_REVIEW', 'off') == 'on',
        'mode': mode,
        'repos': repos,
        'reviewer': e('PR_REVIEW_REVIEWER', ''),
        'rules': e('PR_REVIEW_RULES') or os.path.join(HERE, '..', 'rules.toml'),
        'checklist': e('PR_REVIEW_CHECKLIST') or os.path.join(HERE, '..', 'checklist-health.md'),
        'cap': int(e('PR_REVIEW_DAILY_CAP', '10')),
        'reserve': int(e('PR_REVIEW_RESERVE', '3')),
        'max_wait_h': float(e('PR_REVIEW_MAX_WAIT_H', '24')),
        'lookback_h': float(e('PR_REVIEW_LOOKBACK_H', '2')),
        'ci_wait_h': float(e('PR_REVIEW_CI_WAIT_H', '3')),
        'state': e('PR_REVIEW_STATE_DIR') or os.path.join(city, '.gc', 'runtime', 'pr-review'),
        'work': e('PR_REVIEW_WORK_DIR') or '/var/tmp/gc-pr-review',
        'max_load': float(e('PR_REVIEW_MAX_LOAD', '15')),
        'beads': e('PR_REVIEW_BEADS', 'on') == 'on',
        'slack_channel': e('PR_REVIEW_SLACK_CHANNEL', ''),
        'slack_env': e('PR_REVIEW_SLACK_ENV', '~/.config/gc/slack.env ~/.config/gc/slack2.env').split(),
        'model': e('PR_REVIEW_MODEL', 'opus'),
        'effort': e('PR_REVIEW_EFFORT', 'high'),
        'prompt': e('PR_REVIEW_PROMPT', 'security'),
        'context': e('PR_REVIEW_CONTEXT', 'off') == 'on',
        'suggestions': e('PR_REVIEW_SUGGESTIONS', 'off') == 'on',
        'incremental': e('PR_REVIEW_INCREMENTAL', 'off') == 'on',
        'max_usd': e('PR_REVIEW_MAX_USD', '5'),
        'timeout_s': int(e('PR_REVIEW_TIMEOUT_S', '1200')),
        'sweep_budget_s': int(e('PR_REVIEW_SWEEP_BUDGET_S', '240')),
        'approve': e('PR_REVIEW_APPROVE', 'off') == 'on',
        'request_changes': e('PR_REVIEW_REQUEST_CHANGES', 'off') == 'on',
        'judge': e('PR_REVIEW_JUDGE', 'on') == 'on',
        'judge_model': e('PR_REVIEW_JUDGE_MODEL', 'sonnet'),
        'judge_max_usd': e('PR_REVIEW_JUDGE_MAX_USD', '0.25'),
        'digest_model': e('PR_REVIEW_DIGEST_MODEL', 'sonnet'),
        'digest_max_usd': e('PR_REVIEW_DIGEST_MAX_USD', '1'),
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
            self._run(rig, 'close', bead, '--reason', outcome or 'done')


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


APPROVED = 'Approved as low risk'
CHANGES_SUGGESTED = 'Changes suggested'


def slack_post(cfg, outcome, url, review_url, approved=False, ts=None, event='COMMENT'):
    """Post '<word>: <review URL>' to the channel (R17): the submitted review's own GitHub URL. The text is
    built here from a fixed word and the link; nothing the model wrote reaches Slack. The word matches the
    review's event (R35): changes are "requested" only on a REQUEST_CHANGES review. With `ts`, the
    message of an earlier review of the same head is updated in place instead (R18). Returns (status,
    message ts): 'ok', 'off' or an error code; the ts lets the feedback order find reactions and replies."""
    word = APPROVED if approved else OUTCOMES[outcome][1 if cfg['mode'] == 'live' else 0]
    if outcome == 'changes' and event != 'REQUEST_CHANGES':
        word = CHANGES_SUGGESTED
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
