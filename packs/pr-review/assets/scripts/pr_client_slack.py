#!/usr/bin/env python3
"""The client's own Slack workspace, as the reviewer (decision-14bb1b55; the mayor's design, 2026-10-30
17:23Z). Our bot cannot see that workspace; the reviewer's claude.ai Slack connector can. Every normal
session has the connector's tools removed by a server-level deny in the user settings. Only this runner
uses it:

  claude -p --restricted --tools "" --allowedTools <the one post tool> --settings <a PreToolUse hook>

`--restricted` ignores the user, project and local settings files (so the deny does not apply here, and
neither do the town's hooks), and `--tools ""` leaves no built-in tool. The hook is this script
(`--hook`). It allows exactly one call: the post tool, to the expected conversation, which must be in
PR_REVIEW_CLIENT_SLACK_CONVERSATIONS, with the exact text the script built, and no other field. It
refuses anything else, and a second call. After the run, the transcript's tool calls are checked
against the same rule. The text is a fixed word and a link (the reviews' voice rules); nothing the model
wrote reaches the client. Logs keep ids, counts and outcome words only. Bounded by turns, capture off,
stripped environment, no session kept.

  pr_client_slack.py --check                 isolation (names and counts only): a normal session must have
                                             no tool of the connector, and the runner must have the post tool
  pr_client_slack.py --post-review KEY       the line for the delivered review of state key OWNER/NAME#N
                                             (PR_REVIEW_CLIENT_SLACK=dry-run prints the ids and the word only)
  pr_client_slack.py --queue [--now]         the queue post: once a weekday from PR_REVIEW_CLIENT_SLACK_QUEUE_AT
                                             (--now: whenever run), the open PRs that request the reviewer or
                                             carry our latest review, one line each (the state word and the link)
  pr_client_slack.py --hook                  the runner's PreToolUse hook (the tool call on stdin)
  pr_client_slack.py --selftest              the hook, the allowlist and the lines, the runner stubbed
"""
import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import uuid
import zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pr_rules  # noqa: E402
import pr_state  # noqa: E402

# The tool input's fields: the conversation (any key naming a channel or conversation) and the text.
CHANNEL_HINTS = ('channel', 'conversation')
TEXT_KEYS = ('text', 'message', 'markdown', 'markdown_text', 'content', 'body')
DEFAULT_PREFIX = 'mcp__claude_ai_Slack'  # the claude.ai connector's tools, when no post tool is configured
PROMPT = """Send one Slack message with the tool {tool}, and do nothing else.

Conversation id: {channel}
Text: every line between the two marker lines, exactly, character for character, with its line breaks:
----- BEGIN TEXT -----
{text}
----- END TEXT -----

Make exactly one call to {tool}. Do not read, search or call any other tool. Then reply DONE."""
WAITING = 'Waiting for review'
QUEUE_TRIES = 3  # failed queue posts retried on later runs of the same day, then left for the next day
PROBE_PROMPT = 'Reply with the single word OK. Do not call any tool.'
KEEP_ENV = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'USER', 'LOGNAME', 'TERM', 'TZ')


def client_word(outcome, approved=False, event='COMMENT'):
    """The review's line-1 word in the reviewer's voice (decision-46c435b0), as the client sees it: the
    same words the reviews use, never a word meant for the reviewer only ("Needs you", "Review gap")."""
    if approved:
        return 'Approved'
    if outcome == 'changes':
        return 'Changes requested' if event == 'REQUEST_CHANGES' else 'Changes suggested'
    return {'low-risk': 'Low risk'}.get(outcome, 'Questions')


def review_line(outcome, review_url, approved=False, event='COMMENT'):
    """The line for a delivered review: the word and the review's own link, as our channel gets it (R17)."""
    return f'{client_word(outcome, approved, event)}: {review_url}'


def same_text(a, b):
    """The text as the script built it, up to line endings and trailing spaces."""
    norm = lambda t: '\n'.join(x.rstrip() for x in t.strip().replace('\r\n', '\n').splitlines())  # noqa: E731
    return isinstance(a, str) and isinstance(b, str) and norm(a) == norm(b)


def refusal(tool_name, tool_input, expect):
    """Why the call is refused, or '' when it is the one expected post: the expected tool, the expected
    conversation (in the allowlist), the exact text, and no other field."""
    if not expect or tool_name != expect.get('tool'):
        return 'tool'
    if not isinstance(tool_input, dict):
        return 'input'
    chan = {k: v for k, v in tool_input.items() if any(h in k.lower() for h in CHANNEL_HINTS)}
    text = {k: v for k, v in tool_input.items() if k.lower() in TEXT_KEYS}
    if set(tool_input) - set(chan) - set(text):
        return 'field'
    if expect.get('channel') not in (expect.get('allow') or []) or not chan or \
            any(v != expect['channel'] for v in chan.values()):
        return 'channel'
    if len(text) != 1 or not same_text(next(iter(text.values())), expect.get('text', '')):
        return 'text'
    return ''


def hook(stdin=sys.stdin, environ=os.environ):
    """The runner's PreToolUse hook: exit 0 lets the one expected post through, exit 2 refuses anything
    else and any second call. Logs the tool name and the verdict, never the text."""
    ev = json.load(stdin)
    expect = json.loads(environ.get('PR_CLIENT_SLACK_EXPECT') or '{}')
    why = refusal(ev.get('tool_name'), ev.get('tool_input'), expect)
    if not why and expect.get('once'):
        try:  # the first allowed call claims the slot; a second one is refused
            os.close(os.open(expect['once'], os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            why = 'second-call'
    if expect.get('state'):
        pr_state.log(expect['state'], {'run': 'client-slack-hook', 'tool': ev.get('tool_name'),
                                       'verdict': why or 'allowed'})
    if why:
        print(f'Refused ({why}): this session may make only the one expected post.', file=sys.stderr)
        return 2
    return 0


def stream(stdout):
    """The runner's stream-json output: (init {servers, tools}, tool calls [{id, name, input}], results
    {id: is_error}, result meta {turns, cost_usd, denials, subtype})."""
    init, calls, results, meta = {}, [], {}, {}
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get('type') == 'system' and ev.get('subtype') == 'init':
            init = {'servers': [(s.get('name'), s.get('status')) for s in ev.get('mcp_servers') or []],
                    'tools': ev.get('tools') or []}
        for block in ((ev.get('message') or {}).get('content') or []) if ev.get('type') in ('assistant', 'user') else []:
            if not isinstance(block, dict):
                continue
            if block.get('type') == 'tool_use':
                calls.append({'id': block.get('id'), 'name': block.get('name'), 'input': block.get('input')})
            elif block.get('type') == 'tool_result':
                results[block.get('tool_use_id')] = bool(block.get('is_error'))
        if ev.get('type') == 'result':
            meta = {'turns': ev.get('num_turns'), 'cost_usd': ev.get('total_cost_usd') or 0,
                    'denials': len(ev.get('permission_denials') or []), 'subtype': ev.get('subtype')}
    return init, calls, results, meta


def run_claude(cfg, expect, prompt, restricted=True, allowed=None):
    """One headless session: `--restricted` (the runner) or the user's settings (a normal session, for
    --check), the hook on every tool call, stripped env, capture off, MCP connected before the first turn."""
    settings = {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [
        {'type': 'command', 'command': f'{sys.executable} {os.path.abspath(__file__)} --hook'}]}]}}
    env = {k: v for k, v in os.environ.items() if k in KEEP_ENV}
    # The connectors load (even where the town turns them off) and are connected before the first turn.
    env.update(LOOMTRACER_INGEST_DISABLE='1', HIVEMIND_INGEST_DISABLE='1', MCP_CONNECTION_NONBLOCKING='false',
               ENABLE_CLAUDEAI_MCP_SERVERS='true', PR_CLIENT_SLACK_EXPECT=json.dumps(expect))
    cmd = [os.environ.get('PR_REVIEW_CLAUDE') or 'claude', '-p'] + (['--restricted'] if restricted else []) + \
        ['--tools', '', '--settings', json.dumps(settings), '--no-session-persistence',
         '--model', cfg['client_slack_model'], '--max-turns', str(cfg['client_slack_max_turns']),
         '--output-format', 'stream-json', '--verbose'] + (['--allowedTools', allowed] if allowed else [])
    cwd = pr_state.private_dir(cfg, 'client-slack') if restricted else cfg['city']
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env, cwd=cwd, timeout=180)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {}, [], {}, {'error': type(e).__name__}
    init, calls, results, meta = stream(r.stdout)
    return init, calls, results, dict(meta, rc=r.returncode)


def post(cfg, channel, text, runner=run_claude):
    """One message to an allowlisted conversation through the runner. Returns (status, meta): 'off',
    'dry-run', 'refused-channel', 'no-tool', 'ok', 'not-posted', 'refused-call' (the model tried another
    call, which the hook refused; the post may still have gone out) or 'error:<kind>'."""
    mode = cfg['client_slack']
    if mode == 'off':
        return 'off', {}
    if not channel or channel not in cfg['client_slack_conversations']:
        return 'refused-channel', {}
    if mode == 'dry-run':
        return 'dry-run', {}
    if not cfg['client_slack_tool']:
        return 'no-tool', {}
    once = os.path.join(pr_state.private_dir(cfg, 'client-slack'), f'once-{uuid.uuid4().hex}')
    expect = {'tool': cfg['client_slack_tool'], 'channel': channel, 'text': text,
              'allow': sorted(cfg['client_slack_conversations']), 'state': cfg['state'], 'once': once}
    try:
        init, calls, results, meta = runner(cfg, expect, PROMPT.format(tool=expect['tool'], channel=channel, text=text),
                                            True, expect['tool'])
    finally:
        if os.path.exists(once):
            os.remove(once)
    if meta.get('error'):
        return f"error:{meta['error']}", meta
    good = [c for c in calls if not refusal(c['name'], c['input'], expect)]
    meta = dict(meta, calls=len(calls), refused=len(calls) - len(good))
    if len(calls) > len(good):
        return 'refused-call', meta
    if len(good) == 1 and results.get(good[0]['id']) is False:
        return 'ok', meta
    return 'not-posted', meta


def check(cfg, runner=run_claude):
    """R1, names and counts only: a normal session (the user's settings, from the city dir) loads no tool
    of the connector; the runner loads the post tool. Prints one JSON line; returns whether both hold."""
    prefix = '__'.join(cfg['client_slack_tool'].split('__')[:2]) if cfg['client_slack_tool'] else DEFAULT_PREFIX
    out = {}
    for name, restricted in (('normal', False), ('runner', True)):
        init, calls, _, meta = runner(cfg, {}, PROBE_PROMPT, restricted, None)
        mine = sorted(t for t in init.get('tools') or [] if t.startswith(prefix + '__'))
        out[name] = {'servers': init.get('servers'), 'tools': len(init.get('tools') or []),
                     'connector_tools': mine if restricted else len(mine), 'calls': len(calls),
                     **{k: meta.get(k) for k in ('turns', 'error') if meta.get(k) is not None}}
    runner_tools = out['runner']['connector_tools']
    out['isolated'] = out['normal']['connector_tools'] == 0 and bool(runner_tools)
    out['post_tool_loaded'] = bool(cfg['client_slack_tool']) and cfg['client_slack_tool'] in runner_tools
    print(json.dumps(out))
    return out['isolated'] and out['post_tool_loaded']


def post_review(cfg, key):
    """The line for the delivered review of a state key, posted (or, in dry-run, printed: ids and the word)."""
    with pr_state.locked(cfg['state']) as st:
        e = dict(st['prs'].get(key) or {})
    if not e.get('review_url'):
        print(json.dumps({'pr': key, 'client_slack': 'no-delivered-review'}))
        return 1
    status, meta = deliver_line(cfg, e['repo'], e['review_url'], e.get('outcome'), e.get('approved'), e.get('event'))
    print(json.dumps({'pr': e.get('url'), 'client_slack': status, 'word': client_word(e.get('outcome'), e.get('approved'),
                                                                                       e.get('event')), **meta}))
    return 0 if status in ('ok', 'dry-run', 'off') else 1


def deliver_line(cfg, repo, review_url, outcome, approved=False, event='COMMENT', runner=run_claude):
    """After a delivered review (pr_review.deliver): its line in the client channel, for the configured
    repositories. Returns (status, meta with counts only)."""
    if cfg['client_slack'] == 'off':
        return 'off', {}
    if cfg['client_slack_repos'] and repo not in cfg['client_slack_repos']:
        return 'not-this-repo', {}
    status, meta = post(cfg, cfg['client_slack_channel'], review_line(outcome, review_url, approved, event), runner)
    pr_state.log(cfg['state'], {'run': 'client-slack', 'kind': 'review', 'status': status,
                                'channel': cfg['client_slack_channel'], 'review': review_url,
                                **{k: meta[k] for k in ('turns', 'calls', 'refused', 'denials') if k in meta}})
    return status, meta


def queue_lines(prs, entries, reviewer):
    """The queue post's lines: each open PR (not a draft) that requests the reviewer or carries our latest
    review, by repository and number, as "<word>: <PR link>". The word is our review's on the PR's current
    head, else "Waiting for review". Nothing from the PR's text."""
    out = []
    for p in sorted(prs, key=lambda p: (p['repo'], p['number'])):
        e = entries.get(f"{p['repo']}#{p['number']}") or {}
        asked = reviewer.lower() in [r.lower() for r in p.get('requested') or []]
        if p.get('draft') or not (asked or e.get('review_url')):
            continue
        current = e.get('review_url') and p['head'] in (e.get('review_heads') or [])
        word = client_word(e.get('outcome'), e.get('approved'), e.get('event')) if current else WAITING
        out.append(f"{word}: {p['url']}")
    return out


def due(now_utc, last_day, at='08:00', tz='Europe/Berlin'):
    """(due, local day): once a weekday, from `at` local time."""
    local = now_utc.astimezone(zoneinfo.ZoneInfo(tz))
    h, m = (int(x) for x in at.split(':'))
    day = local.date().isoformat()
    return local.weekday() < 5 and (local.hour, local.minute) >= (h, m) and last_day != day, day


def open_prs(cfg):
    """The configured repositories' open PRs: number, link, head, draft, requested reviewers (logins)."""
    out = []
    for repo in sorted(cfg['repos']):
        if cfg['client_slack_repos'] and repo not in cfg['client_slack_repos']:
            continue
        rows = json.loads(pr_rules.gh(['pr', 'list', '-R', repo, '--state', 'open', '--limit', '200', '--json',
                                       'number,url,headRefOid,isDraft,reviewRequests']))
        out += [{'repo': repo, 'number': r['number'], 'url': r['url'], 'head': r['headRefOid'], 'draft': r['isDraft'],
                 'requested': [x.get('login') for x in r.get('reviewRequests') or [] if x.get('login')]} for r in rows]
    return out


def queue(cfg, now=False, runner=run_claude, prs=None):
    """The queue post (the mayor's v1, 2026-10-30 17:23Z). Once a weekday from the configured local time
    (`now`: whenever run); skipped when the list is empty; a failed post is tried again on later runs of
    the day, QUEUE_TRIES times. Prints and logs ids, words and counts only."""
    if cfg['client_slack'] == 'off':
        print(json.dumps({'client_slack_queue': 'off'}))
        return 0
    with pr_state.locked(cfg['state']) as st:
        q = dict(st.get('client_slack_queue') or {})
        entries = {k: dict(e) for k, e in st['prs'].items()}
    ok, day = due(pr_state.now(), q.get('day'), cfg['client_slack_queue_at'], cfg['client_slack_tz'])
    if not (ok or now) or (q.get('tries_day') == day and q.get('tries', 0) >= QUEUE_TRIES):
        print(json.dumps({'client_slack_queue': 'not-due', 'day': day}))
        return 0
    lines = queue_lines(open_prs(cfg) if prs is None else prs, entries, cfg['reviewer'])
    if cfg['client_slack'] == 'dry-run':
        print(json.dumps({'client_slack_queue': 'dry-run', 'channel': cfg['client_slack_channel'],
                          'lines': [x.split(': ', 1) for x in lines]}))
        return 0
    status, meta = post(cfg, cfg['client_slack_channel'], '\n'.join(lines), runner) if lines else ('empty', {})
    failed = status in ('no-tool', 'not-posted', 'refused-channel') or status.startswith('error')
    with pr_state.locked(cfg['state']) as st:
        q = dict(st.get('client_slack_queue') or {})
        if failed:
            q.update(tries_day=day, tries=(q.get('tries', 0) if q.get('tries_day') == day else 0) + 1)
        else:  # posted, empty, or a refused extra call after which the post may have gone out: once a day
            q.update(day=day, status=status)
        st['client_slack_queue'] = q
    rec = {'run': 'client-slack', 'kind': 'queue', 'status': status, 'lines': len(lines),
           'channel': cfg['client_slack_channel'], **{k: meta[k] for k in ('turns', 'calls', 'refused', 'denials') if k in meta}}
    pr_state.log(cfg['state'], rec)
    print(json.dumps(rec))
    return 1 if failed else 0


def selftest():
    """Synthetic data only; the runner is stubbed and the hook runs as the runner would run it."""
    import tempfile
    tmp = tempfile.mkdtemp()
    cfg = {'client_slack': 'on', 'client_slack_channel': 'C1', 'client_slack_conversations': {'C1', 'D1'},
           'client_slack_tool': 'mcp__x_Slack__send', 'client_slack_model': 'haiku', 'client_slack_max_turns': 3,
           'client_slack_repos': set(), 'state': tmp, 'work': tmp, 'city': tmp}
    url = 'https://github.com/o/r/pull/1#pullrequestreview-5'
    text = review_line('low-risk', url, approved=True, event='APPROVE')
    exp = {'tool': 'mcp__x_Slack__send', 'channel': 'C1', 'text': text, 'allow': ['C1', 'D1']}
    checks = {
        'the line is the word and the link': text == f'Approved: {url}',
        'never a word for the reviewer only': client_word('escalate') == 'Questions' and
        client_word('not-reviewed') == 'Questions' and client_word('changes', event='REQUEST_CHANGES') == 'Changes requested',
        'the expected post passes': refusal('mcp__x_Slack__send', {'channel_id': 'C1', 'text': text}, exp) == '',
        'another tool is refused': refusal('mcp__x_Slack__search', {'query': 'x'}, exp) == 'tool',
        'another channel is refused': refusal('mcp__x_Slack__send', {'channel_id': 'C2', 'text': text}, exp) == 'channel',
        'a channel off the allowlist is refused': refusal('mcp__x_Slack__send', {'channel_id': 'C9', 'text': text},
                                                          dict(exp, channel='C9')) == 'channel',
        'other text is refused': refusal('mcp__x_Slack__send', {'channel_id': 'C1', 'text': text + '!'}, exp) == 'text',
        'an extra field (a thread) is refused': refusal('mcp__x_Slack__send', {'channel_id': 'C1', 'text': text,
                                                                                'thread_ts': '1.2'}, exp) == 'field',
        'no expectation refuses everything': refusal('mcp__x_Slack__send', {'channel_id': 'C1', 'text': text}, {}) == 'tool',
    }
    once = os.path.join(tmp, 'once-test')
    call = json.dumps({'tool_name': 'mcp__x_Slack__send', 'tool_input': {'channel_id': 'C1', 'text': text}})
    env = dict(os.environ, PR_CLIENT_SLACK_EXPECT=json.dumps(dict(exp, once=once, state=tmp)))
    rc = [subprocess.run([sys.executable, os.path.abspath(__file__), '--hook'], input=call, text=True, env=env,
                         capture_output=True).returncode for _ in range(2)]
    checks['the hook lets one post through and refuses the second'] = rc == [0, 2]
    log = open(os.path.join(tmp, 'pr-review.jsonl')).read()
    checks['the hook logs the tool and the verdict, never the text'] = 'allowed' in log and url not in log

    def stub(calls, results, meta=None):
        def run(cfg, expect, prompt, restricted, allowed):
            stub.seen = {'expect': expect, 'prompt': prompt, 'restricted': restricted, 'allowed': allowed}
            return {}, calls, results, meta or {'turns': 2}
        return run
    good = [{'id': 't1', 'name': 'mcp__x_Slack__send', 'input': {'channel': 'C1', 'text': text}}]
    checks['a post that went out is ok'] = post(cfg, 'C1', text, stub(good, {'t1': False}))[0] == 'ok'
    checks['the runner is restricted and allows the one tool'] = stub.seen['restricted'] and \
        stub.seen['allowed'] == 'mcp__x_Slack__send' and f'\n{text}\n----- END TEXT -----' in stub.seen['prompt']
    checks['a failed call is not posted'] = post(cfg, 'C1', text, stub(good, {'t1': True}))[0] == 'not-posted'
    bad = good + [{'id': 't2', 'name': 'mcp__x_Slack__read', 'input': {'channel': 'C1'}}]
    checks['another call is reported'] = post(cfg, 'C1', text, stub(bad, {'t1': False, 't2': True}))[0] == 'refused-call'
    checks['a channel off the allowlist never starts the runner'] = post(cfg, 'C9', text, None)[0] == 'refused-channel'
    checks['dry-run never starts the runner'] = post(dict(cfg, client_slack='dry-run'), 'C1', text, None)[0] == 'dry-run'
    checks['off is off'] = deliver_line(dict(cfg, client_slack='off'), 'o/r', url, 'low-risk', runner=None)[0] == 'off'
    checks['another repository is skipped'] = deliver_line(dict(cfg, client_slack_repos={'o/x'}), 'o/r', url,
                                                           'low-risk', runner=None)[0] == 'not-this-repo'
    init = {'tools': ['mcp__x_Slack__send', 'mcp__x_Slack__read'], 'servers': [('x Slack', 'connected')]}
    probe = lambda cfg, e, p, restricted, a: (init if restricted else {'tools': []}, [], {}, {})  # noqa: E731
    leak = lambda cfg, e, p, restricted, a: (init, [], {}, {})  # noqa: E731
    with contextlib.redirect_stdout(io.StringIO()):
        checks['--check: isolated when only the runner sees the connector'] = check(cfg, probe)
        checks['--check: not isolated when a normal session sees it'] = not check(cfg, leak)
    import datetime as dt
    head = 'a' * 40
    prs = [{'repo': 'o/r', 'number': 3, 'url': 'https://github.com/o/r/pull/3', 'head': head, 'requested': ['Rev']},
           {'repo': 'o/r', 'number': 1, 'url': 'https://github.com/o/r/pull/1', 'head': head, 'requested': []},
           {'repo': 'o/r', 'number': 2, 'url': 'https://github.com/o/r/pull/2', 'head': 'b' * 40, 'requested': []},
           {'repo': 'o/r', 'number': 4, 'url': 'https://github.com/o/r/pull/4', 'head': head, 'requested': ['rev'],
            'draft': True},
           {'repo': 'o/r', 'number': 5, 'url': 'https://github.com/o/r/pull/5', 'head': head, 'requested': ['someone']}]
    entries = {'o/r#1': {'review_url': url, 'review_heads': [head], 'outcome': 'low-risk', 'approved': True},
               'o/r#2': {'review_url': url, 'review_heads': [head], 'outcome': 'questions'}}
    lines = queue_lines(prs, entries, 'rev')
    checks['the queue: our word on the current head, else waiting; by number; no drafts, no others'] = lines == [
        'Approved: https://github.com/o/r/pull/1', 'Waiting for review: https://github.com/o/r/pull/2',
        'Waiting for review: https://github.com/o/r/pull/3']
    berlin = dt.timezone(dt.timedelta(hours=1))
    fri_0805, fri_0755 = dt.datetime(2026, 10, 30, 8, 5, tzinfo=berlin), dt.datetime(2026, 10, 30, 7, 55, tzinfo=berlin)
    checks['due once a weekday from 08:00 local'] = due(fri_0805, None)[0] and not due(fri_0755, None)[0] and \
        not due(fri_0805, '2026-10-30')[0] and not due(fri_0805 + dt.timedelta(days=1), None)[0]
    checks['a multi-line text matches up to line endings'] = same_text('a\r\nb  \n', 'a\nb') and not same_text('a\nb', 'a b')
    qcfg = dict(cfg, reviewer='rev', client_slack_queue_at='00:00', client_slack_tz='UTC')
    with pr_state.locked(tmp) as st:
        st['prs'].update(entries)
    with contextlib.redirect_stdout(io.StringIO()):
        sent = queue(qcfg, now=True, prs=prs, runner=stub([{'id': 't1', 'name': 'mcp__x_Slack__send', 'input': {
            'channel': 'C1', 'text': '\n'.join(lines)}}], {'t1': False}))
        checks['the queue posts its lines through the runner'] = sent == 0 and '----- BEGIN TEXT -----\n' + \
            '\n'.join(lines) + '\n----- END TEXT -----' in stub.seen['prompt']
        queue(qcfg, now=True, prs=[], runner=None)
    checks['an empty queue posts nothing'] = '"status": "empty"' in open(os.path.join(tmp, 'pr-review.jsonl')).read()
    for k, v in checks.items():
        print('ok  ' if v else 'FAIL', k)
    return all(checks.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--check', action='store_true')
    g.add_argument('--post-review', metavar='KEY')
    g.add_argument('--queue', action='store_true')
    g.add_argument('--hook', action='store_true')
    g.add_argument('--selftest', action='store_true')
    ap.add_argument('--now', action='store_true', help='with --queue: post whatever the time')
    a = ap.parse_args()
    if a.hook:
        return hook()
    if a.selftest:
        return 0 if selftest() else 1
    cfg = pr_state.config()
    if a.check:
        return 0 if check(cfg) else 1
    if a.queue:
        return queue(cfg, now=a.now)
    return post_review(cfg, a.post_review)


if __name__ == '__main__':
    sys.exit(main())
