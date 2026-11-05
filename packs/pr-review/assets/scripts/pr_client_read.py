#!/usr/bin/env python3
"""Read the client's review channel, as the reviewer (decision-041014d6: "please make it possible to read the
channel"; gcs-y48, the review audit). The same isolation as the post runner (pr_client_slack.py):

  claude -p --restricted --tools "" --allowedTools <the two read tools> --settings <a PreToolUse hook>
         + every other connector tool denied by name

The hook is this script (`--hook`). It allows only the channel and thread read tools
(PR_REVIEW_CLIENT_SLACK_READ_TOOLS), only on a conversation in PR_REVIEW_CLIENT_SLACK_READ_CONVERSATIONS, and at
most PR_REVIEW_CLIENT_SLACK_READ_CALLS calls; it refuses any other tool (a post, a search across the
workspace). After the run the transcript's calls are checked against the same rule, and the answer is used only
when every call passed.

  pr_client_read.py --messages [--since DATE]   the channel's messages about the configured repositories' PRs
                                                -> PR_REVIEW_WORK_DIR/audit/slack.json (mode 600); prints counts
  pr_client_read.py --selftest                  the hook's rule, synthetic

What it prints and keeps in the town runtime is counts. The messages are the client's own text: they stay in
PR_REVIEW_WORK_DIR (mode 700) and are never logged, mailed, captured or posted to our Slack.
"""
import argparse
import collections
import json
import os
import re
import subprocess
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_client_slack  # noqa: E402
import pr_state  # noqa: E402

READ_TOOLS = [t for t in os.environ.get(
    'PR_REVIEW_CLIENT_SLACK_READ_TOOLS',
    'mcp__claude_ai_Slack__slack_read_channel mcp__claude_ai_Slack__slack_read_thread').split() if t]
MAX_CALLS = int(os.environ.get('PR_REVIEW_CLIENT_SLACK_READ_CALLS', '80'))
CHANNEL_KEY = re.compile(r'channel', re.I)
ANSWER_TOOL = 'StructuredOutput'

SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['messages', 'complete'],
    'properties': {
        'complete': {'type': 'boolean'},
        'messages': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['pr', 'ts', 'thread_ts', 'who', 'text'],
            'properties': {'pr': {'type': 'string'}, 'ts': {'type': 'string'}, 'thread_ts': {'type': 'string'},
                           'who': {'type': 'string', 'enum': ['reviewer', 'author', 'other']},
                           'text': {'type': 'string'}}}}}}

PROMPT = """Read the Slack channel {channel} (only this channel) from {since} until now. Use {read_channel} and
page with its cursor until you reach {since}; open every thread in that time with {read_thread}. Do not call
any other tool and do not read any other channel.

Then answer with the messages and thread replies that are about a pull request (PR) of these repositories:
{repos}
A message is about a PR when it links it (https://github.com/<owner>/<name>/pull/<n>) or names its number with
the repository; a thread reply is about the PR its thread is about. For each: pr as "<owner>/<name>#<n>" (the
repository as the link names it), ts, thread_ts ("" for a top-level message), who ("reviewer" when {reviewer}
wrote it, "author" when {author} wrote it, else "other"), and text: the message itself, verbatim, at most 1500
characters. Leave out {reviewer}'s own two-line PR entries (a link, a few words, a confidence and a status) when
nobody replied to them; keep every reply. complete: true when you reached {since}."""


def refusal(tool_name, tool_input, allow):
    """'' when the call is a read of an allowed conversation (or the answer itself), else why it is refused."""
    if tool_name == ANSWER_TOOL:  # --json-schema returns the answer through this tool; it reaches no workspace
        return ''
    if tool_name not in READ_TOOLS:
        return 'tool'
    if not isinstance(tool_input, dict):
        return 'input'
    chans = [v for k, v in tool_input.items() if CHANNEL_KEY.search(k)]
    if not chans or any(v not in allow for v in chans):
        return 'channel'
    return ''


def hook(stdin=sys.stdin, environ=os.environ):
    """PreToolUse: exit 0 lets an allowed read through, exit 2 refuses; at most MAX_CALLS reads per run."""
    ev = json.load(stdin)
    expect = json.loads(environ.get('PR_CLIENT_READ_EXPECT') or '{}')
    why = refusal(ev.get('tool_name'), ev.get('tool_input'), expect.get('allow') or [])
    if not why and expect.get('counter'):
        with open(expect['counter'], 'a+') as f:
            f.seek(0)
            n = len(f.read())
            if n >= expect.get('max', MAX_CALLS):
                why = 'too-many-calls'
            else:
                f.write('.')
    if expect.get('state'):
        pr_state.log(expect['state'], {'run': 'client-read-hook', 'tool': ev.get('tool_name'), 'verdict': why or 'allowed'})
    if why:
        print(f'Refused ({why}): this session may only read the allowed channel.', file=sys.stderr)
        return 2
    return 0


def run_read(cfg, channel, prompt, deny):
    counter = os.path.join(pr_state.private_dir(cfg, 'client-slack'), f'reads-{uuid.uuid4().hex}')
    expect = {'allow': [channel], 'state': cfg['state'], 'counter': counter, 'max': MAX_CALLS}
    settings = {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [
        {'type': 'command', 'command': f'{sys.executable} {os.path.abspath(__file__)} --hook'}]}]},
        'permissions': {'deny': list(deny)}}
    env = {k: v for k, v in os.environ.items() if k in pr_client_slack.KEEP_ENV}
    env.update(LOOMTRACER_INGEST_DISABLE='1', HIVEMIND_INGEST_DISABLE='1', MCP_CONNECTION_NONBLOCKING='false',
               ENABLE_CLAUDEAI_MCP_SERVERS='true', PR_CLIENT_READ_EXPECT=json.dumps(expect))
    cmd = [os.environ.get('PR_REVIEW_CLAUDE') or 'claude', '-p', '--restricted', '--tools', '', '--settings',
           json.dumps(settings), '--no-session-persistence', '--model', os.environ.get('PR_REVIEW_CLIENT_SLACK_READ_MODEL', 'sonnet'),
           '--max-turns', str(MAX_CALLS + 10), '--output-format', 'stream-json', '--verbose',
           '--json-schema', json.dumps(SCHEMA), '--allowedTools', ','.join(READ_TOOLS)]
    try:
        r = subprocess.run(cmd, input=prompt, capture_output=True, text=True, env=env,
                           cwd=pr_state.private_dir(cfg, 'client-slack'), timeout=1800)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, {'error': type(e).__name__}
    finally:
        if os.path.exists(counter):
            os.remove(counter)
    init, calls, _, meta = pr_client_slack.stream(r.stdout)
    answer = None
    for line in r.stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get('type') == 'result':
            answer = ev.get('structured_output')
            meta['cost_usd'] = ev.get('total_cost_usd')
    bad = [c['name'] for c in calls if refusal(c['name'], c['input'], [channel])]
    return answer, dict(meta, rc=r.returncode, calls=len(calls), refused=len(bad),
                        visible=len(init.get('tools') or []))


def messages(cfg, since):
    channel = (cfg.get('client_slack_read_conversations') or [None])[0]
    if not channel:
        print(json.dumps({'read': 'no conversation (PR_REVIEW_CLIENT_SLACK_READ_CONVERSATIONS)'}))
        return None
    # the session sees the two read tools only: a probe lists what it loads, and every other tool is denied by name
    seen = pr_client_slack.run_claude(cfg, {}, pr_client_slack.PROBE_PROMPT, True, None, ())[0].get('tools') or []
    if not all(t in seen for t in READ_TOOLS):
        print(json.dumps({'read': 'no read tool', 'visible': len(seen)}))
        return None
    names = dict(x.split('=', 1) for x in os.environ.get('PR_REVIEW_NAMES', '').split() if '=' in x)
    prompt = PROMPT.format(channel=channel, since=since, read_channel=READ_TOOLS[0], read_thread=READ_TOOLS[-1],
                           repos='\n'.join(f'- {r}' for r in cfg['repos']),
                           reviewer=os.environ.get('PR_REVIEW_REVIEWER_NAME') or cfg['reviewer'],
                           author=' or '.join(sorted(set(names.values()))) or 'the PR author')
    answer, meta = run_read(cfg, channel, prompt, sorted(t for t in seen if t not in READ_TOOLS))
    if meta.get('error') or meta.get('refused') or not isinstance(answer, dict):
        print(json.dumps({'read': 'failed', **{k: meta.get(k) for k in ('error', 'calls', 'refused', 'rc')}}))
        return None
    path = os.path.join(pr_state.private_dir(cfg, 'audit'), 'slack.json')
    fd = os.open(path + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'channel': channel, 'since': since, 'at': pr_state.iso(pr_state.now()), **answer}, f, indent=1)
    os.replace(path + '.tmp', path)
    msgs = answer.get('messages') or []
    print(json.dumps({'read': 'ok', 'complete': answer.get('complete'), 'messages': len(msgs),
                      'prs': len({m['pr'] for m in msgs}), 'who': dict(collections.Counter(m['who'] for m in msgs)),
                      'calls': meta.get('calls'), 'cost_usd': meta.get('cost_usd')}))
    return answer


def selftest():
    allow = ['C1']
    cases = {
        'a channel read of the allowed conversation passes': refusal(READ_TOOLS[0], {'channel_id': 'C1', 'limit': 100}, allow) == '',
        'a thread read of the allowed conversation passes': refusal(READ_TOOLS[-1], {'channel_id': 'C1', 'message_ts': '1.2'}, allow) == '',
        'another conversation is refused': refusal(READ_TOOLS[0], {'channel_id': 'C2'}, allow) == 'channel',
        'a read without a conversation is refused': refusal(READ_TOOLS[0], {'limit': 5}, allow) == 'channel',
        'a post is refused': refusal('mcp__claude_ai_Slack__slack_send_message', {'channel_id': 'C1', 'message': 'x'}, allow) == 'tool',
        'a workspace search is refused': refusal('mcp__claude_ai_Slack__slack_search_public', {'query': 'x'}, allow) == 'tool',
        'the structured answer passes': refusal(ANSWER_TOOL, {'messages': []}, allow) == '',
    }
    for k, v in cases.items():
        print('ok  ' if v else 'FAIL', k)
    return all(cases.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--messages', action='store_true')
    g.add_argument('--hook', action='store_true')
    g.add_argument('--selftest', action='store_true')
    ap.add_argument('--since', default=os.environ.get('PR_REVIEW_AUDIT_SINCE', '2026-10-29'))
    a = ap.parse_args()
    if a.hook:
        return hook()
    if a.selftest:
        return 0 if selftest() else 1
    cfg = pr_state.config()
    cfg['client_slack_read_conversations'] = [c for c in os.environ.get('PR_REVIEW_CLIENT_SLACK_READ_CONVERSATIONS', '').split() if c]
    messages(cfg, a.since)
    return 0


if __name__ == '__main__':
    sys.exit(main())
