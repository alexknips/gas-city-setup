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
against the same rule, and the post counts as landed ('ok') only when the send tool's result carries
the message's ts. Bounded by turns, capture off, stripped environment, no session kept.

Each PR's entry is two lines (the mayor, 2026-10-30 19:40Z and 19:41Z), built by the script:

  <PR link> · <3-5 words on what the PR does> · <high|medium|low> confidence · <status>
  <readiness> · CI <...> · +<added>/-<removed> in <n> files · <tests> · <c> changes, <q> questions open · <n> flagged

The 3-5 words are the only words a model wrote: the review's summary when it is 3-5 plain words, else
one narrow turn's versions (the script picks), else the PR title's first plain words. Everything else
is a fixed word or a count, from fixed rules over the review's result and GitHub (zero tokens). Logs
keep ids, counts and enums only, never the words; the words are kept host-local for the queue.

  pr_client_slack.py --check                 isolation (names and counts only): a normal session must have
                                             no tool of the connector, and the runner must have the post tool
  pr_client_slack.py --post-review KEY       the entry of state key OWNER/NAME#N as the PR stands now, posted
                                             (PR_REVIEW_CLIENT_SLACK=dry-run prints the enums and counts only)
  pr_client_slack.py --queue [--now]         the queue post: once a weekday from PR_REVIEW_CLIENT_SLACK_QUEUE_AT
                                             (--now: whenever run), the open PRs that request the reviewer or
                                             carry our latest review, one entry each
  pr_client_slack.py --hook                  the runner's PreToolUse hook (the tool call on stdin)
  pr_client_slack.py --selftest              the hook, the allowlist and the entries, the runner stubbed
"""
import argparse
import contextlib
import datetime as dt
import io
import json
import os
import re
import subprocess
import sys
import uuid
import zoneinfo

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pr_judge  # noqa: E402
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
POSTED = ('ok', 'sent-unconfirmed', 'refused-call')  # the line is or may be in the channel: never again for that head

SEP = ' · '
LINE_MAX = 120  # characters of line 2; the files count goes first when it is longer
WORDS_MIN, WORDS_MAX = 3, 5
# A plain word: letters (one capital first, or an acronym of up to 5), a hyphen or apostrophe between parts,
# or a number of up to 4 digits; at most 19 characters, so no identifier, path, date, address, long number
# or secret (the known ones are 20 characters or more) fits.
PLAIN = re.compile(r"(?:[A-Za-z][a-z]*(?:['-][A-Za-z][a-z]*)*|[A-Z]{2,5}s?|\d{1,4})")
TRAILING = {'a', 'an', 'the', 'to', 'in', 'into', 'for', 'of', 'on', 'and', 'or', 'with', 'from', 'by', 'at', 'as'}
WORDS_TURNS = 2
WORDS_PROMPT = """Shorten this one-sentence summary of a pull request to {lo} to {hi} plain words that say what the
pull request does, for the team's Slack channel. It has {n} words now: count the words of what you write.
Plain words only: no file, function, variable or package names, no code, no numbers, no names of people,
no links and no punctuation. Give up to three versions, best first, each {lo} to {hi} words.

Summary: {summary}"""
WORDS_SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['versions'], 'properties': {
    'versions': {'type': 'array', 'maxItems': 3, 'items': {'type': 'string', 'maxLength': 80}}}}
# A test file: the repository's test paths, or a test or spec name (foo.test.ts, foo_test.go, test_foo.py,
# FooTest.java, FooSpec.scala, foo-spec.js).
TEST_NAME = re.compile(r'(?i:(?:^|[._-])(?:tests?|specs?)(?:[._-]|$))|^Test[A-Z_]|[a-z0-9](?:Tests?|Spec)\.\w+$')
FACTS_Q = ('query($o: String!, $r: String!, $n: Int!) { repository(owner: $o, name: $r) { pullRequest(number: $n) {'
           ' headRefName files(first: 100) { totalCount nodes { path } } } } }')


def client_word(outcome, approved=False, event='COMMENT'):
    """The review's status word in the reviewer's voice (decision-46c435b0), as the client sees it: the
    same words the reviews use, never a word meant for the reviewer only ("Needs you", "Review gap")."""
    if approved:
        return 'Approved'
    if outcome == 'changes':
        return 'Changes requested' if event == 'REQUEST_CHANGES' else 'Changes suggested'
    return {'low-risk': 'Low risk'}.get(outcome, 'Questions')


# ---------- the entry ----------

def plain_words(text):
    """`text` as WORDS_MIN to WORDS_MAX plain words (end punctuation dropped), or '' when it is not."""
    toks = [t.rstrip('.,;:!?') for t in (text or '').split()]
    ok = WORDS_MIN <= len(toks) <= WORDS_MAX and all(len(t) <= 19 and PLAIN.fullmatch(t) for t in toks)
    return ' '.join(toks) if ok else ''


def title_words(title):
    """The PR title's first plain words, cut to WORDS_MAX: no tag, ticket or conventional-commit prefix, the
    run ending at the first word that is not plain, no trailing article or preposition."""
    t = re.sub(r'^(?:\s*\[[^\]]*\])+', '', title or '')
    t = re.sub(r'^\s*[A-Z][A-Z0-9]+-\d+\b[\s:-]*', '', t)
    t = re.sub(r'^\s*[A-Za-z]+(?:\([^)]*\))?!?:\s*', '', t)
    out = []
    for tok in t.split():
        w = tok.rstrip('.,;:!?')
        if not w or len(w) > 19 or not PLAIN.fullmatch(w):
            break
        out.append(w)
        if len(out) == WORDS_MAX or w != tok:
            break
    while out and out[-1].lower() in TRAILING:
        out.pop()
    return ' '.join(out)[:1].upper() + ' '.join(out)[1:]


def words_turn(cfg, summary):
    """The narrow wording turn (like R29's): the summary, its word count and the budget to the judge model,
    at most WORDS_TURNS turns. Returns (versions, meta)."""
    prompt = WORDS_PROMPT.format(lo=WORDS_MIN, hi=WORDS_MAX, n=len(summary.split()), summary=summary)
    so, meta = pr_judge.ask(cfg, prompt, WORDS_SCHEMA, cfg['judge_model'], WORDS_TURNS)
    return [v for v in (so or {}).get('versions') or [] if isinstance(v, str)], meta


def words_for(summary, title, turn=None):
    """(words, where they came from, meta): the summary when it passes; else the first of the turn's
    versions that passes; else the title's first plain words. `turn(summary)` -> (versions, meta)."""
    w = plain_words(summary)
    if w:
        return w, 'summary', {}
    meta = {}
    if (summary or '').strip() and turn:
        versions, meta = turn(summary.strip())
        for v in versions:
            if plain_words(v):
                return plain_words(v), 'turn', meta
    w = title_words(title)
    return w, ('title' if w else 'none'), meta


def confidence(rv, ci, flags_cut=0, gaps_open=()):
    """A fixed rule over the delivered review, never the model's say (the mayor, 19:40Z). low: a fallback
    (the note, its floor, the money review) or no full review; high: a full review with CI green, nothing
    in a not-checked area, no open question (an item, a decision or a blocker asking), every flagged hunk
    in the review and no open gap in our method; medium: any other full review."""
    if rv.get('fallback') or rv.get('outcome') not in FULL:
        return 'low'
    asks = bool(rv.get('decisions')) or any(x.get('kind') == 'question'
                                            for x in (rv.get('items') or []) + (rv.get('blockers') or []))
    high = ci == 'green' and not rv.get('not_checked') and not asks and not flags_cut and not gaps_open
    return 'high' if high else 'medium'


def open_asks(rv):
    """(changes, questions) a delivered review leaves open, counted as pr_format.decide counts its asks: its
    items and blockers by kind, each decision a question; flagged by the rules or not (the mayor, 2026-10-30
    20:13Z). So Needs changes always has a change open, Needs answers a question, and an approval nothing."""
    asks = list(rv.get('items') or []) + list(rv.get('blockers') or [])
    changes = sum(1 for a in asks if a.get('kind') != 'question')
    return changes, len(asks) - changes + len(rv.get('decisions') or [])


def review_of(rv, event, ci, flagged=0, flags_cut=0, gaps_open=()):
    """What the entry says about a delivered review: kind, outcome, approved, event, confidence, the asks it
    leaves open (changes, questions) and the hunks the rules flagged."""
    changes, questions = open_asks(rv)
    return {'kind': 'fallback' if rv.get('fallback') else 'full', 'outcome': rv.get('outcome'),
            'approved': event == 'APPROVE', 'event': event, 'confidence': confidence(rv, ci, flags_cut, gaps_open),
            'flagged': (flagged or 0) + (flags_cut or 0), 'changes': changes, 'questions': questions}


def ours_of(review):
    """Our latest review on the head, for readiness: approved, changes, questions or None."""
    if review.get('kind') != 'full':
        return None
    if review.get('approved'):
        return 'approved'
    return {'changes': 'changes', 'questions': 'questions', 'escalate': 'questions'}.get(review.get('outcome'))


def readiness(mergeable, ci, ours, state='OPEN'):
    """One word or two, the first that holds (the mayor, 19:41Z): Conflict, CI failing, Needs changes, Needs
    answers, Waiting for review (no full review of ours on the head, or one without our approval), then
    our approval on the head: CI running while checks are pending, else Ready to merge. A PR posted again
    by hand after it merged or closed: Merged or Closed."""
    if state in ('MERGED', 'CLOSED'):
        return state.capitalize()
    if mergeable == 'CONFLICTING':
        return 'Conflict'
    if ci == 'red':
        return 'CI failing'
    if ours == 'changes':
        return 'Needs changes'
    if ours == 'questions':
        return 'Needs answers'
    if ours != 'approved':
        return WAITING
    return 'CI running' if ci == 'pending' else 'Ready to merge'


def ci_part(ci, first=None, pushes=0):
    """CI now, and when green whether the first push passed the required checks (the quality pack's rule)."""
    if ci != 'green':
        return {'red': 'CI failing', 'pending': 'CI pending', 'none': 'no CI', 'skipped': 'CI skipped'}.get(ci, 'CI unknown')
    if first is True:
        return 'CI green first push'
    return f'CI green after {pushes} pushes' if first is False and pushes > 1 else 'CI green'


def is_test(path, test_paths=()):
    return bool(pr_rules.TEST_PATH.search(path) or TEST_NAME.search(path.rsplit('/', 1)[-1])
                or any(p.search(path) for p in test_paths))


def tests_part(paths, test_paths=()):
    """tests changed (next to other changes), tests only, or no test changes; None when the paths are unknown."""
    if not paths:
        return None
    t = sum(1 for p in paths if is_test(p, test_paths))
    return 'no test changes' if not t else ('tests only' if t == len(paths) else 'tests changed')


def open_part(review):
    """'1 change, 2 questions open' or 'nothing open', then '<n> flagged' only when the rules flagged a hunk."""
    if review.get('changes') is None:
        return []
    n = lambda k, one: f"{k} {one}{'' if k == 1 else 's'}"  # noqa: E731
    asks = ([n(review['changes'], 'change')] if review['changes'] else []) + \
        ([n(review['questions'], 'question')] if review.get('questions') else [])
    return [(', '.join(asks) + ' open') if asks else 'nothing open'] + \
        ([f"{review['flagged']} flagged"] if review.get('flagged') else [])


def entry_text(url, words, review, facts):
    """The PR's two lines. `review`: kind full / fallback / held / None (nothing of ours on the head), with
    outcome, approved, event, confidence and the flag counts; `facts`: pr_facts. Confidence shows for what
    we delivered or held on the head (a fallback or a held run is low) and is left out when nothing of ours
    is on it; the open asks and the flag count show for a delivered review only."""
    kind = review.get('kind')
    status = client_word(review.get('outcome'), review.get('approved'), review.get('event')) if kind == 'full' else WAITING
    conf = review.get('confidence') if kind == 'full' else ('low' if kind else '')
    line1 = SEP.join(x for x in (url, words, conf and f'{conf} confidence', status) if x)
    size = f"+{facts.get('added', 0)}/-{facts.get('removed', 0)}"
    files = f" in {facts.get('files', 0)} file{'' if facts.get('files') == 1 else 's'}"
    parts = [readiness(facts.get('mergeable'), facts.get('ci'), ours_of(review), facts.get('state', 'OPEN')),
             ci_part(facts.get('ci'), facts.get('first_pass'), facts.get('pushes', 0)), size + files,
             facts.get('tests'), *(open_part(review) if kind in ('full', 'fallback') else [])]
    line2 = SEP.join(x for x in parts if x)
    if len(line2) > LINE_MAX:
        parts[2] = size
        line2 = SEP.join(x for x in parts if x)
    return line1 + '\n' + line2


def test_paths(cfg):
    try:
        return pr_rules.load_rules(cfg['rules'])['test_paths']
    except (OSError, ValueError, KeyError):
        return []


def pr_facts(cfg, repo, number, pr=None):
    """Line 2's facts from GitHub, zero tokens: mergeable, CI now and its first push, size, test paths."""
    pr = pr or pr_rules.get_pr(repo, number)
    ci = pr_rules.ci_state(pr)
    facts = {'url': pr['url'], 'head': pr['headRefOid'], 'title': pr.get('title') or '', 'state': pr.get('state'),
             'mergeable': pr.get('mergeable'),
             'ci': ci, 'first_pass': None, 'pushes': 0, 'added': pr.get('additions') or 0,
             'removed': pr.get('deletions') or 0, 'files': pr.get('changedFiles') or 0, 'tests': None}
    owner, name = repo.split('/')
    try:
        x = pr_rules.graphql(FACTS_Q, o=owner, r=name, n=number)['repository']['pullRequest']
    except (pr_rules.GhError, KeyError, TypeError):
        return facts
    if x['files']['totalCount'] <= len(x['files']['nodes']):  # tests: only when every path is known
        facts['tests'] = tests_part([f['path'] for f in x['files']['nodes']], test_paths(cfg))
    if ci == 'green':
        facts['first_pass'], facts['pushes'] = pr_rules.ci_history(repo, pr, x['headRefName'])
    return facts


def facts_log(facts, review, words, how):
    """The entry as the log and the dry run keep it: enums and counts only, never the words."""
    return {'pr': facts.get('url'), 'status': client_word(review.get('outcome'), review.get('approved'), review.get('event'))
            if review.get('kind') == 'full' else WAITING, 'confidence': review.get('confidence') if review.get('kind') == 'full'
            else ('low' if review.get('kind') else None),
            'readiness': readiness(facts.get('mergeable'), facts.get('ci'), ours_of(review), facts.get('state', 'OPEN')),
            'ci': ci_part(facts.get('ci'), facts.get('first_pass'), facts.get('pushes', 0)), 'tests': facts.get('tests'),
            'changes': review.get('changes'), 'questions': review.get('questions'), 'flagged': review.get('flagged'),
            'words_from': how, 'words': len(words.split())}


# ---------- the words, host-local ----------

def words_path(cfg):
    return os.path.join(pr_state.private_dir(cfg, 'client-slack'), 'words.json')


def stored_words(cfg):
    p = words_path(cfg)
    try:
        return json.load(open(p)) if os.path.exists(p) else {}
    except ValueError:
        return {}


def keep_words(cfg, key, head, words, how):
    """The PR's words for the queue, in the 700 work dir (the client's PR content), mode 600. Rows older than
    PR_REVIEW_KEEP_DAYS go on every write."""
    cut = pr_state.now() - dt.timedelta(days=cfg.get('keep_days', 30))
    rows = {k: v for k, v in stored_words(cfg).items() if pr_state.parse(v.get('at') or '1970-01-01T00:00:00Z') >= cut}
    if words:
        rows[key] = {'head': head, 'words': words, 'from': how, 'at': pr_state.iso(pr_state.now())}
    p = words_path(cfg)
    with os.fdopen(os.open(p + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
        json.dump(rows, f)
    os.replace(p + '.tmp', p)


def posted_result(cfg, e, head):
    """The structured result we delivered on that head (feedback/posted), or None."""
    p = os.path.join(cfg['work'], 'feedback', 'posted', f"{e['repo'].replace('/', '__')}-{e['number']}-{head[:10]}.json")
    try:
        return json.load(open(p)).get('result') if os.path.exists(p) else None
    except ValueError:
        return None


# ---------- the runner ----------

def same_text(a, b):
    """The text as the script built it, up to line endings, trailing spaces and blank lines."""
    norm = lambda t: '\n'.join(x.rstrip() for x in t.strip().replace('\r\n', '\n').splitlines() if x.strip())  # noqa: E731
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


TS_KEY = re.compile(r'"(?:message_)?ts"\s*:\s*"(\d{9,11}\.\d{6})"')
TS_ANY = re.compile(r'(?<![\d.])(\d{10}\.\d{6})(?![\d])')
PERMALINK = re.compile(r'https://[\w.-]+\.slack\.com/archives/[A-Z0-9]+/p(\d{10})(\d{6})\b[^\s"\'<>)]*')


def landed(content):
    """(ts, permalink, shape) from the send tool's result (the mayor, 18:56Z): the posted message's ts, from a
    ts field, any Slack ts, or the permalink. The text is never kept: `shape` is 'json:<its keys>' or 'text'."""
    if isinstance(content, list):
        content = '\n'.join(b.get('text', '') if isinstance(b, dict) else str(b) for b in content)
    text = content if isinstance(content, str) else json.dumps(content)
    link = PERMALINK.search(text)
    ts = TS_KEY.search(text) or TS_ANY.search(text)
    try:
        obj = json.loads(text)
        shape = 'json:' + ','.join(sorted(obj)[:8]) if isinstance(obj, dict) else 'json'
    except ValueError:
        shape = 'text'
    return (ts.group(1) if ts else (f'{link.group(1)}.{link.group(2)}' if link else None)), \
        (link.group(0) if link else None), shape


def stream(stdout):
    """The runner's stream-json output: (init {servers, tools}, tool calls [{id, name, input}], results
    {id: {error, ts, permalink, shape}}, result meta {turns, cost_usd, denials, subtype})."""
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
                ts, link, shape = landed(block.get('content'))
                results[block.get('tool_use_id')] = {'error': bool(block.get('is_error')), 'ts': ts, 'permalink': link,
                                                     'shape': shape}
        if ev.get('type') == 'result':
            meta = {'turns': ev.get('num_turns'), 'cost_usd': ev.get('total_cost_usd') or 0,
                    'denials': len(ev.get('permission_denials') or []), 'subtype': ev.get('subtype')}
    return init, calls, results, meta


def run_claude(cfg, expect, prompt, restricted=True, allowed=None, deny=()):
    """One headless session: `--restricted` (the runner) or the user's settings (a normal session, for
    --check), the hook on every tool call, stripped env, capture off, MCP connected before the first turn;
    `deny`: tools removed from the session (tool-level denies hide them from the model)."""
    settings = {'hooks': {'PreToolUse': [{'matcher': '*', 'hooks': [
        {'type': 'command', 'command': f'{sys.executable} {os.path.abspath(__file__)} --hook'}]}]},
                **({'permissions': {'deny': list(deny)}} if deny else {})}
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
    'dry-run', 'refused-channel', 'no-tool', 'ok' (the send result carried the message's ts),
    'sent-unconfirmed' (the call succeeded, no ts came back), 'not-posted', 'refused-call' (the model tried
    another call, which the hook refused; the post may still have gone out) or 'error:<kind>'."""
    mode = cfg['client_slack']
    if mode == 'off':
        return 'off', {}
    if not channel or channel not in cfg['client_slack_conversations']:
        return 'refused-channel', {}
    if mode == 'dry-run':
        return 'dry-run', {}
    if not cfg['client_slack_tool']:
        return 'no-tool', {}
    tool = cfg['client_slack_tool']
    # The runner sees exactly the post tool (the mayor, 17:38Z): a probe lists what the session loads, and
    # every other tool is denied by name in the post run, regenerated each run.
    seen = runner(cfg, {}, PROBE_PROMPT, True, None, ())[0].get('tools') or []
    if tool not in seen:
        return 'no-tool', {'visible': len(seen)}
    once = os.path.join(pr_state.private_dir(cfg, 'client-slack'), f'once-{uuid.uuid4().hex}')
    expect = {'tool': tool, 'channel': channel, 'text': text,
              'allow': sorted(cfg['client_slack_conversations']), 'state': cfg['state'], 'once': once}
    try:
        init, calls, results, meta = runner(cfg, expect, PROMPT.format(tool=tool, channel=channel, text=text),
                                            True, tool, sorted(t for t in seen if t != tool))
    finally:
        if os.path.exists(once):
            os.remove(once)
    if meta.get('error'):
        return f"error:{meta['error']}", meta
    good = [c for c in calls if not refusal(c['name'], c['input'], expect)]
    meta = dict(meta, calls=len(calls), refused=len(calls) - len(good),
                visible_other=sum(1 for t in init.get('tools') or [] if t != tool))
    sent = [results[c['id']] for c in good if c['id'] in results and not results[c['id']]['error']]
    if sent:
        meta.update(ts=sent[0]['ts'], permalink=sent[0]['permalink'], **({} if sent[0]['ts'] else {'shape': sent[0]['shape']}))
    if len(calls) > len(good):
        return 'refused-call', meta
    if len(good) == 1 and sent:
        return ('ok' if sent[0]['ts'] else 'sent-unconfirmed'), meta
    return 'not-posted', meta


def check(cfg, runner=run_claude):
    """R1, names and counts only: a normal session (the user's settings, from the city dir) loads no tool
    of the connector; the runner loads the post tool. Prints one JSON line; returns whether both hold."""
    prefix = '__'.join(cfg['client_slack_tool'].split('__')[:2]) if cfg['client_slack_tool'] else DEFAULT_PREFIX
    out, seen = {}, []
    for name, restricted in (('normal', False), ('runner', True)):
        init, calls, _, meta = runner(cfg, {}, PROBE_PROMPT, restricted, None, ())
        seen = init.get('tools') or []
        mine = sorted(t for t in seen if t.startswith(prefix + '__'))
        out[name] = {'servers': init.get('servers'), 'tools': len(seen),
                     'connector_tools': mine if restricted else len(mine), 'calls': len(calls),
                     **{k: meta.get(k) for k in ('turns', 'error') if meta.get(k) is not None}}
    tool = cfg['client_slack_tool']
    out['isolated'] = out['normal']['connector_tools'] == 0 and bool(out['runner']['connector_tools'])
    out['post_tool_loaded'] = bool(tool) and tool in out['runner']['connector_tools']
    if out['post_tool_loaded']:  # the post run's view: every other tool denied by name, as post() does it
        init = runner(cfg, {}, PROBE_PROMPT, True, None, sorted(t for t in seen if t != tool))[0]
        out['runner_with_denies'] = sorted(init.get('tools') or [])
        out['post_tool_loaded'] = out['runner_with_denies'] == [tool]
    print(json.dumps(out))
    return out['isolated'] and out['post_tool_loaded']


# ---------- per review ----------

def sent_reviews(state_dir):
    """The review links whose line the run log says landed, from before the per-head record existed."""
    out, path = set(), os.path.join(state_dir, 'pr-review.jsonl')
    for line in open(path) if os.path.exists(path) else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get('run') == 'client-slack' and r.get('kind') == 'review' and r.get('status') in POSTED and r.get('review'):
            out.add(r['review'])
    return out


def posted_on(e, head, sent=()):
    """Whether the client channel has (or may have) the line for this head: the entry's own record of client
    posts per head, never our channel's ts (the mayor, 19:12Z: our Slack is off). An entry from before the
    record: the run log's landed line for its review on this head."""
    if head in (e.get('client_posts') or {}):
        return True
    return not e.get('client_posts') and head in (e.get('review_heads') or []) and e.get('review_url') in sent


def record_post(e, head, status, ts):
    """The entry's client posts with this head's, the last 20 heads (ids and the status only)."""
    posts = {h: p for h, p in (e.get('client_posts') or {}).items() if h != head}
    posts[head] = {'status': status, 'ts': ts}
    return dict(list(posts.items())[-20:])


def send_entry(cfg, key, e, review, facts, words, how, kind='review', runner=run_claude):
    """Post one entry and log it: enums, counts and the message's ts, never the words. Returns (status, meta)."""
    status, meta = post(cfg, cfg['client_slack_channel'], entry_text(facts['url'], words, review, facts), runner)
    rec = {'run': 'client-slack', 'kind': kind, 'status': status, 'channel': cfg['client_slack_channel'],
           **({'review': e['review_url']} if kind == 'review' and e.get('review_url') else {}),
           'head': (facts.get('head') or '')[:10], **facts_log(facts, review, words, how),
           **{k: meta[k] for k in ('turns', 'calls', 'refused', 'denials', 'ts', 'permalink', 'shape') if meta.get(k)}}
    pr_state.log(cfg['state'], rec)
    return status, dict(meta, log=rec)


def deliver_entry(cfg, key, e, review, pr=None, summary='', turn=None, runner=run_claude, facts_of=pr_facts):
    """After a delivered review (pr_review.deliver), for the configured repositories: its entry in the client
    channel. `e`: the PR's entry with this run's review link; `turn`: the wording turn (None: no tokens).
    Returns (status, meta with counts only)."""
    if cfg['client_slack'] == 'off':
        return 'off', {}
    if cfg['client_slack_repos'] and e['repo'] not in cfg['client_slack_repos']:
        return 'not-this-repo', {}
    facts = facts_of(cfg, e['repo'], e['number'], pr)
    words, how, wmeta = words_for(summary, facts['title'], turn)
    keep_words(cfg, key, facts['head'], words, how)
    status, meta = send_entry(cfg, key, e, review, facts, words, how, 'review', runner)
    return status, dict(meta, words_from=how, words_cost_usd=wmeta.get('cost_usd') or 0)


FULL = ('low-risk', 'questions', 'changes', 'escalate')  # outcomes of a full review (not a note or a hold)


def last_delivered(state_dir):
    """{PR link: whether the last review we delivered on it was a fallback}, from the run log (a state
    entry keeps an older run's fallback key after a later full review)."""
    out, path = {}, os.path.join(state_dir, 'pr-review.jsonl')
    for line in open(path) if os.path.exists(path) else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get('run') == 'review' and r.get('review_url') and r.get('pr'):
            out[r['pr']] = bool(r.get('fallback'))
    return out


def review_now(cfg, e, head, fallback=False):
    """What we have on the PR's head now, from its state entry: our full review on the head (the latest run
    delivered it), a fallback delivered on it, a held latest run on it, or nothing (kind None: no review,
    or new commits since). Counts and confidence as stored at delivery, else from the delivered result."""
    on_head = bool(e.get('review_url')) and head in (e.get('review_heads') or [])
    if on_head and e.get('outcome') in FULL and not fallback:
        kind = 'full'
    elif on_head and (fallback or e.get('outcome') == 'not-reviewed'):
        kind = 'fallback'
    elif e.get('head') == head and str(e.get('outcome') or '').startswith('held'):
        return {'kind': 'held'}
    else:
        return {'kind': None}
    approved = bool(e.get('approved'))
    event = e.get('event') or ('APPROVE' if approved else 'COMMENT')
    r = {'kind': kind, 'outcome': e.get('outcome'), 'approved': approved, 'event': event,
         'confidence': 'low' if kind == 'fallback' else e.get('confidence'), 'flagged': e.get('flagged_total'),
         'changes': e.get('open_changes'), 'questions': e.get('open_questions')}
    if kind == 'fallback' and r['flagged'] is None and e.get('flagged_hunks') is not None:
        r['flagged'] = e['flagged_hunks'] + (e.get('flags_cut') or 0)
    if None in (r['confidence'], r['flagged'], r['changes']):  # delivered before the entry kept them
        res = posted_result(cfg, e, head)
        if res:
            old = review_of(dict(res, fallback=kind == 'fallback'), event, e.get('ci'), len(res.get('flagged') or []), 0,
                            e.get('gaps_open') or ())
            r.update({k: r[k] if r[k] is not None else old[k] for k in ('confidence', 'flagged', 'changes', 'questions')})
    return r


def post_review(cfg, key, runner=run_claude, facts_of=pr_facts, turn=None):
    """The PR's entry as it stands now (as the queue builds it), posted by hand: e.g. the last reviews again
    in a new format. The words: those kept at delivery, else the delivered summary (with the wording turn),
    else the title. Records the head's post, so the review flow never sends that head's line again."""
    with pr_state.locked(cfg['state']) as st:
        e = dict(st['prs'].get(key) or {})
    if not e.get('review_url'):
        print(json.dumps({'pr': key, 'client_slack': 'no-delivered-review'}))
        return 1
    facts = facts_of(cfg, e['repo'], e['number'], None)
    review = review_now(cfg, e, facts['head'], last_delivered(cfg['state']).get(e['url']))
    kept = stored_words(cfg).get(key) or {}
    if kept.get('words'):
        words, how = kept['words'], 'kept'
    else:
        res = posted_result(cfg, e, (e.get('review_heads') or [facts['head']])[-1]) or {}
        words, how, _ = words_for(res.get('summary') or '', facts['title'],
                                  turn or (lambda s: words_turn(cfg, s)))
        keep_words(cfg, key, facts['head'], words, how)
    if cfg['client_slack'] == 'dry-run':
        print(json.dumps({'client_slack': 'dry-run', **facts_log(facts, review, words, how)}))
        return 0
    status, meta = send_entry(cfg, key, e, review, facts, words, how, 'repost', runner)
    if status in POSTED:
        with pr_state.locked(cfg['state']) as st:
            if key in st['prs']:
                st['prs'][key]['client_posts'] = record_post(st['prs'][key], facts['head'], status, meta.get('ts'))
    print(json.dumps({'client_slack': status, **{k: v for k, v in meta['log'].items() if k not in ('run', 'permalink')},
                      'permalink': bool(meta.get('permalink'))}))
    return 0 if status in ('ok', 'off') else 1


# ---------- the queue ----------

def queue_entries(cfg, prs, entries, reviewer, fallbacks=None, facts_of=pr_facts, words=None):
    """The queue post's entries: each open PR (not a draft) that requests the reviewer or carries a review of
    ours, by repository and number, as (text, log). Status is our review's word when its latest run
    delivered a full review on the PR's current head; else "Waiting for review": no review yet, new commits
    since, or the latest was held or a fallback (the full review is still owed; the mayor, 17:38Z). The
    words: those kept at delivery, else the title's first plain words (zero tokens)."""
    out, words = [], stored_words(cfg) if words is None else words
    for p in sorted(prs, key=lambda p: (p['repo'], p['number'])):
        key = f"{p['repo']}#{p['number']}"
        e = entries.get(key) or {}
        asked = reviewer.lower() in [r.lower() for r in p.get('requested') or []]
        if p.get('draft') or not (asked or e.get('review_url')):
            continue
        facts = facts_of(cfg, p['repo'], p['number'], None)
        review = review_now(cfg, dict(e, repo=p['repo'], number=p['number']), facts['head'],
                            (fallbacks or {}).get(p['url']))
        w, how = ((words.get(key) or {}).get('words'), 'kept') if (words.get(key) or {}).get('words') else \
            (title_words(facts['title']), 'title')
        out.append((entry_text(p['url'], w or '', review, facts), facts_log(facts, review, w or '', how if w else 'none')))
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


def queue(cfg, now=False, runner=run_claude, prs=None, facts_of=pr_facts):
    """The queue post (the mayor's v1, 2026-10-30 17:23Z). Once a weekday from the configured local time
    (`now`: whenever run); skipped when the list is empty; a failed post is tried again on later runs of
    the day, QUEUE_TRIES times. Prints and logs ids, enums and counts only."""
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
    got = queue_entries(cfg, open_prs(cfg) if prs is None else prs, entries, cfg['reviewer'],
                        last_delivered(cfg['state']), facts_of)
    if cfg['client_slack'] == 'dry-run':
        print(json.dumps({'client_slack_queue': 'dry-run', 'channel': cfg['client_slack_channel'],
                          'entries': [log for _, log in got]}))
        return 0
    text = '\n\n'.join(t for t, _ in got)
    status, meta = post(cfg, cfg['client_slack_channel'], text, runner) if got else ('empty', {})
    failed = status in ('no-tool', 'not-posted', 'refused-channel') or status.startswith('error')
    with pr_state.locked(cfg['state']) as st:
        q = dict(st.get('client_slack_queue') or {})
        if failed:
            q.update(tries_day=day, tries=(q.get('tries', 0) if q.get('tries_day') == day else 0) + 1)
        else:  # posted, empty, or a refused extra call after which the post may have gone out: once a day
            q.update(day=day, status=status, ts=meta.get('ts'))
        st['client_slack_queue'] = q
    rec = {'run': 'client-slack', 'kind': 'queue', 'status': status, 'entries': len(got),
           'channel': cfg['client_slack_channel'],
           **{k: meta[k] for k in ('turns', 'calls', 'refused', 'denials', 'ts', 'permalink', 'shape') if meta.get(k)}}
    pr_state.log(cfg['state'], rec)
    print(json.dumps(dict(rec, permalink=bool(rec.get('permalink')))))
    return 1 if failed else 0


# ---------- self-test ----------

def selftest():
    """Synthetic data only; the runner and GitHub are stubbed and the hook runs as the runner would run it."""
    import tempfile
    tmp = tempfile.mkdtemp()
    cfg = {'client_slack': 'on', 'client_slack_channel': 'C1', 'client_slack_conversations': {'C1', 'D1'},
           'client_slack_tool': 'mcp__x_Slack__send', 'client_slack_model': 'haiku', 'client_slack_max_turns': 3,
           'client_slack_repos': set(), 'state': tmp, 'work': tmp, 'city': tmp, 'keep_days': 30, 'rules': '/nonexistent'}
    pr_url = 'https://github.com/o/r/pull/1'
    facts = {'url': pr_url, 'head': 'a' * 40, 'title': 'feat(api): Add retry to upload job', 'mergeable': 'MERGEABLE',
             'ci': 'green', 'first_pass': True, 'pushes': 1, 'added': 120, 'removed': 8, 'files': 4, 'tests': 'tests changed'}
    full = {'kind': 'full', 'outcome': 'low-risk', 'approved': True, 'event': 'APPROVE', 'confidence': 'high',
            'flagged': 2, 'changes': 0, 'questions': 0}
    text = entry_text(pr_url, 'Adds retry to uploads', full, facts)
    checks = {
        'the entry is two lines in the shape': text == (
            f'{pr_url} · Adds retry to uploads · high confidence · Approved\n'
            'Ready to merge · CI green first push · +120/-8 in 4 files · tests changed · nothing open · 2 flagged'),
        'never a word for the reviewer only': client_word('escalate') == 'Questions' and
        client_word('not-reviewed') == 'Questions' and client_word('changes', event='REQUEST_CHANGES') == 'Changes requested',
    }
    # The words: 3-5 plain words from the summary, else the turn's first passing version, else the title.
    turned = []
    turn = lambda s: (turned.append(s) or ['Adds a retry to the upload job now', 'Retries the `upload` job',  # noqa: E731
                                            'Retries failed upload jobs'], {'cost_usd': 0.01})
    checks['words: a summary of 3-5 plain words is used as it is'] = \
        words_for('Retries failed uploads.', 'x', turn)[:2] == ('Retries failed uploads', 'summary') and not turned
    checks['words: a summary over 5 words is repaired by the turn, the script picks the first that passes'] = \
        words_for('Adds a retry to the upload job when the network fails', 'x', turn)[:2] == \
        ('Retries failed upload jobs', 'turn') and len(turned) == 1
    checks['words: a summary with an identifier is repaired or falls to the title'] = \
        words_for('Calls retryUpload on fail', 'fix: Retry uploads in parseInvoice', turn)[:2] == \
        ('Retries failed upload jobs', 'turn') and \
        words_for('Calls retryUpload on fail', 'fix: Retry uploads in parseInvoice', lambda s: ([], {}))[:2] == \
        ('Retry uploads', 'title')
    checks['words: no summary and no turn is the title, zero tokens'] = words_for('', '[WIP] ABC-12: add audit log to the',
                                                                                  None)[:2] == ('Add audit log', 'title')
    checks['words: no identifier, path, number over 4 digits, date or secret is plain'] = not any(plain_words(x) for x in (
        'Edits src/app.ts now', 'Adds user_id to logs', 'Logs 123456 visits', 'Moves date 2026-10-30 out',
        'Adds abcdefghijklmnopqrstuvwxyz key', 'Adds AKIAABCDEFGHIJKLMNOP key', 'Adds one', 'One two three four five six'))
    # Confidence: a fixed rule over the review.
    rv = {'outcome': 'low-risk', 'items': [], 'decisions': [], 'blockers': [], 'not_checked': '', 'cleared': [{'flag': 'F1'}]}
    checks['confidence: high, medium and low'] = confidence(rv, 'green') == 'high' and \
        confidence(dict(rv, outcome='changes', items=[{'kind': 'change'}]), 'green') == 'high' and \
        confidence(dict(rv, outcome='questions', items=[{'kind': 'question'}]), 'green') == 'medium' and \
        confidence(dict(rv, not_checked='billing'), 'green') == 'medium' and confidence(rv, 'red') == 'medium' and \
        confidence(rv, 'green', flags_cut=2) == 'medium' and confidence(rv, 'green', gaps_open=['new-risk']) == 'medium' and \
        confidence(dict(rv, outcome='not-reviewed', fallback=True), 'green') == 'low' and \
        confidence(dict(rv, outcome='questions', fallback=True), 'green') == 'low'
    # The open asks (the mayor, 20:13Z): a change item the rules never flagged is still a change open.
    rc = review_of(dict(rv, outcome='changes', items=[{'kind': 'change'}], cleared=[]), 'REQUEST_CHANGES', 'green')
    checks['open asks: a change item no rule flagged reads "1 change open", and no "flagged" without a flag'] = \
        entry_text(pr_url, 'Adds retry to uploads', rc, facts).splitlines()[1] == \
        'Needs changes · CI green first push · +120/-8 in 4 files · tests changed · 1 change open' and \
        open_part(dict(rc, changes=2, questions=1, flagged=3)) == ['2 changes, 1 question open', '3 flagged']
    import itertools
    import pr_format
    kinds = [[], [{'kind': 'change'}], [{'kind': 'question'}], [{'kind': 'change'}, {'kind': 'question'}]]
    seen, bad = set(), []
    for items, blockers, decisions in itertools.product(kinds, kinds[:3], [[], [{'actors': ['author']}],
                                                                            [{'actors': ['reviewer']}]]):
        out = pr_format.decide(decisions, items, blockers)
        r = review_of({'outcome': out, 'items': items, 'blockers': blockers, 'decisions': decisions},
                      'APPROVE' if out == 'low-risk' else 'COMMENT', 'green')
        ready = readiness('MERGEABLE', 'green', ours_of(r))
        seen.add(ready)
        if (ready == 'Needs changes' and r['changes'] < 1) or (ready == 'Needs answers' and r['questions'] < 1) or \
                (ready == 'Ready to merge' and r['changes'] + r['questions']):
            bad.append((out, len(items), len(blockers), len(decisions)))
    checks['invariant over decide(): Needs changes has a change open, Needs answers a question, Ready to merge nothing'] = \
        not bad and seen == {'Needs changes', 'Needs answers', 'Ready to merge'}
    # Readiness, one per value, in its order.
    ready = [readiness('CONFLICTING', 'red', 'approved'), readiness('MERGEABLE', 'red', 'approved'),
             readiness('MERGEABLE', 'green', 'changes'), readiness('MERGEABLE', 'green', 'questions'),
             readiness('MERGEABLE', 'green', None), readiness('MERGEABLE', 'green', 'approved'),
             readiness('MERGEABLE', 'pending', 'approved'), readiness('CONFLICTING', 'red', None, 'MERGED'),
             readiness('UNKNOWN', 'green', 'approved', 'CLOSED')]
    checks['readiness: each value, in the order of the rule'] = ready == [
        'Conflict', 'CI failing', 'Needs changes', 'Needs answers', WAITING, 'Ready to merge', 'CI running', 'Merged',
        'Closed']
    checks['CI: green first push, green after 2 pushes, failing, pending'] = [
        ci_part('green', True, 1), ci_part('green', False, 2), ci_part('red'), ci_part('pending'), ci_part('green')] == [
        'CI green first push', 'CI green after 2 pushes', 'CI failing', 'CI pending', 'CI green']
    checks['tests: changed, only, none'] = [
        tests_part(['src/a.ts', 'src/a.test.ts']), tests_part(['tests/a.py', 'src/FooTest.java', 'b_test.go']),
        tests_part(['src/latest.ts', 'src/contest.py']), tests_part([])] == ['tests changed', 'tests only', 'no test changes', None]
    long = dict(facts, added=12345678, removed=87654321, files=123456, first_pass=False, pushes=12, tests='no test changes')
    l2 = entry_text(pr_url, 'Adds retry to uploads', dict(full, flagged=1234), long).splitlines()[1]
    checks['a long line 2 drops the files count first'] = len(l2) <= LINE_MAX and ' in 123456 files' not in l2 and \
        '+12345678/-87654321' in l2 and len(entry_text(pr_url, '', dict(full, flagged=1234), long).splitlines()[1]) > 100
    waiting = entry_text(pr_url, 'Adds retry to uploads', {'kind': None}, facts)
    held = entry_text(pr_url, 'Adds retry to uploads', {'kind': 'held'}, facts)
    note = entry_text(pr_url, '', {'kind': 'fallback', 'outcome': 'not-reviewed', 'confidence': 'low', 'flagged': 3,
                                   'changes': 0, 'questions': 1}, dict(facts, tests='tests only'))
    checks['waiting: no confidence with nothing of ours on the head, low for a held run or a fallback'] = \
        waiting.splitlines()[0] == f'{pr_url} · Adds retry to uploads · {WAITING}' and \
        held.splitlines()[0] == f'{pr_url} · Adds retry to uploads · low confidence · {WAITING}' and \
        note == f'{pr_url} · low confidence · {WAITING}\n{WAITING} · CI green first push · +120/-8 in 4 files · tests only · ' \
        '1 question open · 3 flagged'
    # The hook and the allowlist, on the entry's two lines.
    exp = {'tool': 'mcp__x_Slack__send', 'channel': 'C1', 'text': text, 'allow': ['C1', 'D1']}
    checks.update({
        'the expected post passes': refusal('mcp__x_Slack__send', {'channel_id': 'C1', 'text': text}, exp) == '',
        'another tool is refused': refusal('mcp__x_Slack__search', {'query': 'x'}, exp) == 'tool',
        'another channel is refused': refusal('mcp__x_Slack__send', {'channel_id': 'C2', 'text': text}, exp) == 'channel',
        'a channel off the allowlist is refused': refusal('mcp__x_Slack__send', {'channel_id': 'C9', 'text': text},
                                                          dict(exp, channel='C9')) == 'channel',
        'other text is refused': refusal('mcp__x_Slack__send', {'channel_id': 'C1', 'text': text + '!'}, exp) == 'text',
        'an extra field (a thread) is refused': refusal('mcp__x_Slack__send', {'channel_id': 'C1', 'text': text,
                                                                                'thread_ts': '1.2'}, exp) == 'field',
        'no expectation refuses everything': refusal('mcp__x_Slack__send', {'channel_id': 'C1', 'text': text}, {}) == 'tool',
    })
    once = os.path.join(tmp, 'once-test')
    call = json.dumps({'tool_name': 'mcp__x_Slack__send', 'tool_input': {'channel_id': 'C1', 'text': text}})
    env = dict(os.environ, PR_CLIENT_SLACK_EXPECT=json.dumps(dict(exp, once=once, state=tmp)))
    rc = [subprocess.run([sys.executable, os.path.abspath(__file__), '--hook'], input=call, text=True, env=env,
                         capture_output=True).returncode for _ in range(2)]
    checks['the hook lets one post through and refuses the second'] = rc == [0, 2]
    log = open(os.path.join(tmp, 'pr-review.jsonl')).read()
    checks['the hook logs the tool and the verdict, never the text'] = 'allowed' in log and 'retry' not in log

    def stub(calls, results, meta=None, tools=('mcp__x_Slack__send', 'mcp__x_Slack__read', 'mcp__y__z')):
        def run(cfg, expect, prompt, restricted, allowed, deny=()):
            if not expect:  # the probe
                return {'tools': list(tools)}, [], {}, {}
            stub.seen = {'expect': expect, 'prompt': prompt, 'restricted': restricted, 'allowed': allowed,
                         'deny': list(deny)}
            return {'tools': [t for t in tools if t not in deny]}, calls, results, meta or {'turns': 2}
        return run
    good = [{'id': 't1', 'name': 'mcp__x_Slack__send', 'input': {'channel': 'C1', 'text': text}}]

    def echo(results):  # the model sends exactly the text it was given
        def run(cfg, expect, prompt, restricted, allowed, deny=()):
            if not expect:
                return {'tools': ['mcp__x_Slack__send']}, [], {}, {}
            stub.seen = {'prompt': prompt}
            return {'tools': [expect['tool']]}, [{'id': 't1', 'name': expect['tool'], 'input': {
                'channel': expect['channel'], 'text': expect['text']}}], results, {'turns': 2}
        return run
    res = lambda content, err=False: {'t1': dict(zip(('ts', 'permalink', 'shape'), landed(content)), error=err)}  # noqa: E731
    link = 'https://ws.slack.com/archives/C1/p1793385205766639'
    st, meta = post(cfg, 'C1', text, stub(good, res([{'type': 'text', 'text': json.dumps({'ok': True, 'ts': '1793385205.766639'})}])))
    checks['landed: ok only with the ts from the send result'] = st == 'ok' and meta.get('ts') == '1793385205.766639'
    checks['landed: the ts from a permalink alone'] = landed(f'Message sent: {link}')[:2] == ('1793385205.766639', link)
    st, meta = post(cfg, 'C1', text, stub(good, res('Message sent.')))
    checks['landed: a result without a ts is sent-unconfirmed, its shape kept, not its text'] = \
        st == 'sent-unconfirmed' and meta.get('shape') == 'text' and not meta.get('ts')
    checks['the runner is restricted and allows the one tool'] = stub.seen['restricted'] and \
        stub.seen['allowed'] == 'mcp__x_Slack__send' and f'\n{text}\n----- END TEXT -----' in stub.seen['prompt']
    checks['the post run denies every other tool the probe saw'] = stub.seen['deny'] == ['mcp__x_Slack__read', 'mcp__y__z']
    checks['no post when the connector does not offer the tool'] = post(cfg, 'C1', text, stub(good, {}, tools=()))[0] == 'no-tool'
    checks['a failed call is not posted'] = post(cfg, 'C1', text, stub(good, res('error', True)))[0] == 'not-posted'
    bad = good + [{'id': 't2', 'name': 'mcp__x_Slack__read', 'input': {'channel': 'C1'}}]
    checks['another call is reported'] = post(cfg, 'C1', text, stub(bad, res('{"ts": "1793385205.766639"}')))[0] == 'refused-call'
    checks['a channel off the allowlist never starts the runner'] = post(cfg, 'C9', text, None)[0] == 'refused-channel'
    checks['dry-run never starts the runner'] = post(dict(cfg, client_slack='dry-run'), 'C1', text, None)[0] == 'dry-run'
    e = {'repo': 'o/r', 'number': 1, 'url': pr_url, 'review_url': pr_url + '#pullrequestreview-5', 'review_heads': ['a' * 40]}
    checks['off is off'] = deliver_entry(dict(cfg, client_slack='off'), 'o/r#1', e, full, runner=None)[0] == 'off'
    checks['another repository is skipped'] = deliver_entry(dict(cfg, client_slack_repos={'o/x'}), 'o/r#1', e, full,
                                                            runner=None)[0] == 'not-this-repo'
    # The mayor, 19:12Z: our channel is off, so the client line keys on its own record per head.
    head = 'a' * 40
    checks['our Slack off + a same-head rereview: no second client line'] = \
        posted_on(dict(e, client_posts=record_post(e, head, 'ok', '1.2')), head) and \
        posted_on(e, head, {e['review_url']}) and not posted_on(e, head) and \
        not posted_on(dict(e, client_posts={head: {'status': 'ok'}}), 'b' * 40)
    fx = lambda cfg, repo, n, pr=None: dict(facts, url=f'https://github.com/{repo}/pull/{n}')  # noqa: E731
    sent = deliver_entry(cfg, 'o/r#1', e, full, None, 'Adds a retry to the upload job when it fails', turn,
                         echo(res('{"ts": "1793385205.766639"}')), fx)
    logged = open(os.path.join(tmp, 'pr-review.jsonl')).read().splitlines()[-1]
    checks['a delivered review posts its entry; the log keeps enums, counts and the ts, never the words'] = \
        sent[0] == 'ok' and 'Retries failed upload jobs' in stub.seen['prompt'] and 'upload' not in logged and \
        '"ts": "1793385205.766639"' in logged and '"confidence": "high"' in logged and '"words": 4' in logged
    checks['the words are kept host-local for the queue, mode 600'] = \
        stored_words(cfg)['o/r#1']['words'] == 'Retries failed upload jobs' and os.stat(words_path(cfg)).st_mode & 0o777 == 0o600
    init = {'tools': ['mcp__x_Slack__send', 'mcp__x_Slack__read'], 'servers': [('x Slack', 'connected')]}
    probe = lambda cfg, e, p, restricted, a, d=(): (  # noqa: E731
        {'tools': [t for t in init['tools'] if t not in d]} if restricted else {'tools': []}, [], {}, {})
    leak = lambda cfg, e, p, restricted, a, d=(): (init, [], {}, {})  # noqa: E731
    with contextlib.redirect_stdout(io.StringIO()):
        checks['--check: isolated when only the runner sees the connector, and the denies leave the post tool'] = \
            check(cfg, probe)
        checks['--check: not isolated when a normal session sees it'] = not check(cfg, leak)
    prs = [{'repo': 'o/r', 'number': 3, 'url': 'https://github.com/o/r/pull/3', 'head': head, 'requested': ['Rev']},
           {'repo': 'o/r', 'number': 1, 'url': 'https://github.com/o/r/pull/1', 'head': head, 'requested': []},
           {'repo': 'o/r', 'number': 2, 'url': 'https://github.com/o/r/pull/2', 'head': 'b' * 40, 'requested': []},
           {'repo': 'o/r', 'number': 4, 'url': 'https://github.com/o/r/pull/4', 'head': head, 'requested': ['rev'],
            'draft': True},
           {'repo': 'o/r', 'number': 5, 'url': 'https://github.com/o/r/pull/5', 'head': head, 'requested': ['someone']},
           {'repo': 'o/r', 'number': 6, 'url': 'https://github.com/o/r/pull/6', 'head': head, 'requested': []},
           {'repo': 'o/r', 'number': 7, 'url': 'https://github.com/o/r/pull/7', 'head': head, 'requested': []},
           {'repo': 'o/r', 'number': 8, 'url': 'https://github.com/o/r/pull/8', 'head': head, 'requested': []}]
    url = pr_url + '#pullrequestreview-5'
    entries = {'o/r#1': {'review_url': url, 'review_heads': [head], 'outcome': 'low-risk', 'approved': True,
                         'confidence': 'high', 'flagged_total': 2, 'open_changes': 0, 'open_questions': 0},
               'o/r#2': {'review_url': url, 'review_heads': [head], 'outcome': 'questions'},
               'o/r#6': {'review_url': url, 'review_heads': [head], 'outcome': 'not-reviewed', 'fallback': 'not-reviewed',
                         'flagged_total': 3, 'open_changes': 0, 'open_questions': 1},
               'o/r#7': {'review_url': url, 'review_heads': [head], 'outcome': 'held-gate', 'head': head},
               'o/r#8': {'review_url': url, 'review_heads': [head], 'outcome': 'questions', 'confidence': 'medium'}}
    pfx = lambda cfg, repo, n, pr=None: dict(facts, url=f'https://github.com/{repo}/pull/{n}',  # noqa: E731
                                              head='b' * 40 if n == 2 else head, title='Add audit log')
    got = queue_entries(cfg, prs, entries, 'rev', {'https://github.com/o/r/pull/8': True}, pfx)
    first = [t.splitlines()[0].split(' · ', 1)[1] for t, _ in got]
    checks['the queue: our word for a full review on the current head, else waiting; no drafts, no others'] = first == [
        'Retries failed upload jobs · high confidence · Approved', f'Add audit log · {WAITING}', f'Add audit log · {WAITING}',
        f'Add audit log · low confidence · {WAITING}', f'Add audit log · low confidence · {WAITING}',
        f'Add audit log · low confidence · {WAITING}']
    checks['the queue: line 2 per entry, the flag counts only for a delivered review'] = \
        got[0][0].splitlines()[1].endswith('nothing open · 2 flagged') and \
        got[3][0].splitlines()[1].endswith('1 question open · 3 flagged') and 'flagged' not in got[1][0] and \
        ' open' not in got[1][0] and all(log['words_from'] in ('kept', 'title') for _, log in got)
    berlin = dt.timezone(dt.timedelta(hours=1))
    fri_0805, fri_0755 = dt.datetime(2026, 10, 30, 8, 5, tzinfo=berlin), dt.datetime(2026, 10, 30, 7, 55, tzinfo=berlin)
    checks['due once a weekday from 08:00 local'] = due(fri_0805, None)[0] and not due(fri_0755, None)[0] and \
        not due(fri_0805, '2026-10-30')[0] and not due(fri_0805 + dt.timedelta(days=1), None)[0]
    checks['a multi-line text matches up to line endings and blank lines'] = same_text('a\r\nb  \n\nc', 'a\nb\nc') and \
        not same_text('a\nb', 'a b')
    qcfg = dict(cfg, reviewer='rev', client_slack_queue_at='00:00', client_slack_tz='UTC')
    with pr_state.locked(tmp) as st:
        st['prs'].update(entries)
    qtext = '\n\n'.join(t for t, _ in queue_entries(qcfg, prs, entries, 'rev', last_delivered(tmp), pfx))
    with contextlib.redirect_stdout(io.StringIO()):
        sent = queue(qcfg, now=True, prs=prs, facts_of=pfx, runner=echo(res('{"ts": "1793385205.766639"}')))
        checks['the queue posts its entries through the runner, a blank line between them'] = sent == 0 and \
            '----- BEGIN TEXT -----\n' + qtext + '\n----- END TEXT -----' in stub.seen['prompt'] and '\n\n' in qtext
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
