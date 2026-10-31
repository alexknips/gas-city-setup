#!/usr/bin/env python3
"""Review feedback (README.md, "Quality loop"): what each delivered review cost or saved the reviewer,
read from Slack and GitHub with no extra work for them. The measure is the reviewer's effort, not volume.

  pr_feedback.py                     one pass over the delivered reviews (what the hourly order runs)
  pr_feedback.py --dry-run           the same, printing counts; writes nothing
  pr_feedback.py --record KEY VERDICT [--by WHO] [--note TEXT]
                                     record what was seen by hand, saying who did it

Per delivered review (a private page, R0):
  thumbs     a thumbs-up or thumbs-down on the Slack message (good / bad), replies in its thread (reason)
  decision   the reviewer's own GitHub review submitted after delivery (approve / changes / comment), and
             the minutes from delivery to it; or the PR merged or closed before they reviewed it
  false alarm  "needs you" and the reviewer then approved without changes requested

Host-local (PR_REVIEW_WORK_DIR/feedback, mode 700): replies and notes with their text. A thumbs-down or a
reply also drops a candidate regression case in PR_REVIEW_WORK_DIR/regression/inbox for the weekly
triage. The state file and the Today card (PR_REVIEW_PANEL; quality pack: QUALITY_PANELS) get counts and
medians only. A verdict is never inferred from someone else's action.
"""
import argparse
import collections
import json
import os
import statistics
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_format  # noqa: E402
import pr_judge  # noqa: E402
import pr_rules  # noqa: E402
import pr_state  # noqa: E402

DELIVERED = {'low-risk', 'questions', 'changes', 'escalate', 'not-reviewed'}
OURS_JQ = pr_format.ours_jq()
GOOD, BAD = {'+1', 'thumbsup', 'white_check_mark', 'heavy_check_mark'}, {'-1', 'thumbsdown', 'x'}
VERDICTS = {'good', 'bad', 'rejected'}


def reviews_by(repo, number, reviewer):
    r = subprocess.run(['gh', 'api', '--paginate', f'repos/{repo}/pulls/{number}/reviews?per_page=100', '--jq',
                        f'.[] | {{id, state, login: .user.login, submitted_at, ours: ({OURS_JQ})}}'],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None
    rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    # Our own automated reviews are posted under the same login; they are not the reviewer's decision.
    return [x for x in rows if x['login'].lower() == reviewer.lower() and x['state'] != 'PENDING' and not x['ours']]


def decision(entry, reviewer):
    """(kind, state, minutes after delivery) of the first thing that settled the PR after delivery: the
    reviewer's own review, else a merge or close (not theirs), else (None, None, None)."""
    since = entry['delivered_at']
    mine = reviews_by(entry['repo'], entry['number'], reviewer)
    if mine is None:
        return 'unreadable', None, None
    after = sorted((r for r in mine if (r.get('submitted_at') or '') >= since), key=lambda r: r['submitted_at'])
    minutes = lambda t: round((pr_state.parse(t) - pr_state.parse(since)).total_seconds() / 60)  # noqa: E731
    if after:
        return 'their-review', after[0]['state'], minutes(after[0]['submitted_at'])
    pr = pr_rules.get_pr(entry['repo'], entry['number'])
    if pr and pr['state'] == 'MERGED':
        return 'merged-without-their-review', None, minutes(pr['mergedAt'])
    if pr and pr['state'] == 'CLOSED':
        return 'closed-without-their-review', None, minutes(pr['closedAt'])
    return None, None, None


def slack_verdict(cfg, token, entry):
    """(thumbs 'up' / 'down' / None, [reply texts]) for our Slack message."""
    ts = entry.get('slack_ts')
    if not (token and ts and cfg['slack_channel']):
        return None, []
    try:
        rx = pr_state.slack_get(token, 'reactions.get', {'channel': cfg['slack_channel'], 'timestamp': ts})
        names = {x['name'].split('::')[0] for x in ((rx.get('message') or {}).get('reactions') or [])}
        rp = pr_state.slack_get(token, 'conversations.replies', {'channel': cfg['slack_channel'], 'ts': ts, 'limit': 50})
        replies = [m.get('text', '') for m in (rp.get('messages') or [])[1:] if not m.get('bot_id')]
    except OSError:
        return None, []
    return ('down' if names & BAD else ('up' if names & GOOD else None)), replies


def keep(cfg, rec):
    """A feedback record with any text, host-local; a thumbs-down or a reply also becomes a candidate
    regression case (inbox) holding the review that was delivered."""
    d = pr_state.private_dir(cfg, 'feedback')
    p = os.path.join(d, 'feedback.jsonl')
    with open(p, 'a') as f:
        f.write(json.dumps(rec) + '\n')
    os.chmod(p, 0o600)
    if rec.get('thumbs') == 'down' or rec.get('replies') or rec.get('verdict') in ('bad', 'rejected'):
        stem = rec['key'].replace('/', '__').replace('#', '-')
        posted = os.path.join(d, 'posted', f"{stem}-{(rec.get('head') or '')[:10]}.json")
        body = json.load(open(posted)).get('body') if os.path.exists(posted) else None
        q = os.path.join(pr_state.private_dir(cfg, 'regression', 'inbox'), f'{stem}.json')
        with open(q, 'w') as f:
            json.dump({'expect': 'fail', 'todo': 'triage: add or change a style-guide rule so the gate fails this',
                       'outcome': rec.get('outcome'), 'body': body,
                       'feedback': {k: rec.get(k) for k in ('thumbs', 'replies', 'verdict', 'by', 'note')}}, f)
        os.chmod(q, 0o600)


def week_of(iso):
    y, w, _ = pr_state.parse(iso).isocalendar()
    return f'{y}-W{w:02d}'


def posted_scores(st, state_dir):
    """The reader test's scores of the body each delivered review posted: {state key: (kind, scores or None)}.

    The entry's own `judge` cannot say it: each run's fields merge into the PR's entry, so after a fallback
    it holds the held attempt's scores, and after a run that never reached the reader an earlier run's
    (gcs-6aq: 12 of 31 delivered entries in week 44, all 9 of the card's "below the bar"). So each review is
    matched by its review_id to the last run in the log that posted or updated it. Kind is 'fallback' when
    that run fell back, else 'review' ('unmatched' when no run row names the review). The scores are the
    audit's when it read that review after that run, else the run's own reader test of what it posted (a
    fallback's log-only `fallback_judge`, a review's `judge`), else None: posted unscored."""
    last = {}
    path = os.path.join(state_dir, 'pr-review.jsonl')
    for line in open(path) if os.path.exists(path) else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get('run') == 'review' and r.get('review_id') and r.get('posted') in ('ok', 'updated'):
            last[r['review_id']] = r
    out = {}
    for key, e in st['prs'].items():
        if not (e.get('delivered_at') and e.get('outcome') in DELIVERED):
            continue
        row = last.get(e.get('review_id'))
        if row is None:
            out[key] = ('unmatched', None)
            continue
        kind = 'fallback' if row.get('fallback') else 'review'
        audited = e.get('judge') if (e.get('review_id') and e.get('judge_review') == e['review_id']
                                     and (e.get('judge_at') or '') >= (row.get('at') or '')) else None
        s = audited or row.get('fallback_judge' if kind == 'fallback' else 'judge')
        out[key] = (kind, s if isinstance(s, dict) and all(isinstance(s.get(d), int) for d in pr_judge.BAR) else None)
    return out


def summarize(st, state_dir, weeks=2):
    """Per ISO week of delivery: counts by outcome, thumbs, replies, the reviewer's decisions and the median
    minutes to their own review; gate holds and regenerations per week of review; the reader test of what
    was posted (posted_scores), full reviews and fallbacks apart."""
    rows, mins = collections.defaultdict(collections.Counter), collections.defaultdict(list)
    judged = collections.defaultdict(lambda: collections.defaultdict(list))
    scored = posted_scores(st, state_dir)
    for key, e in st['prs'].items():
        if e.get('delivered_at') and e.get('outcome') in DELIVERED:
            w = week_of(e['delivered_at'])
            c = rows[w]
            c['delivered'] += 1
            c['outcome_' + e['outcome']] += 1
            if e.get('fb_thumbs'):
                c['thumbs_' + e['fb_thumbs']] += 1
            c['replies'] += 1 if e.get('fb_replies') else 0
            if e.get('fb_decision'):
                c[e['fb_decision']] += 1
                if e['fb_decision'] == 'their-review' and e.get('fb_minutes') is not None:
                    mins[w].append(e['fb_minutes'])
            c['false_alarm'] += 1 if e.get('fb_false_alarm') else 0
            c['approved'] += 1 if e.get('approved') else 0
            kind, s = scored.get(key, ('unmatched', None))  # R26: the reader test of the posted body
            if s is None:
                c['reader_unscored'] += 1
            elif kind == 'fallback':
                c['fallback_judged'] += 1
                c['fallback_below_bar'] += 1 if pr_judge.below_bar(s) else 0
                judged[w]['fallback_useful'].append(s['useful'])
            else:
                c['judged'] += 1
                c['judge_below_bar'] += 1 if pr_judge.below_bar(s) else 0
                for d in ('markers', 'useful'):
                    judged[w][d].append(s[d])
        if e.get('outcome') == 'held-gate' and e.get('reviewed_at'):
            rows[week_of(e['reviewed_at'])]['held_gate'] += 1
        if e.get('gate_first') and e.get('reviewed_at'):
            rows[week_of(e['reviewed_at'])]['regenerated'] += 1
    for s in st.get('feedback_seed', []):
        rows[week_of(s['at'])]['seed_' + s['verdict']] += 1
    out = {}
    for w in sorted(rows)[-weeks:]:
        out[w] = dict(rows[w])
        if mins[w]:
            out[w]['median_min_to_their_review'] = round(statistics.median(mins[w]))
        for d, v in judged[w].items():
            out[w][f'median_{d}'] = statistics.median(v)
    return out


def fallback_split(state_dir, weeks=2):
    """Per ISO week, from every model review run in the log (the mayor, 2026-10-30 16:10Z): the runs, those
    that ended in a fallback, split by what held them (wording or format reasons only, or a content rule;
    runs before v5.8.2 did not record it), the runs a repair saved, and the R29 miss rate (first answers
    that missed R29, runs R29 still held at the end)."""
    rows = collections.defaultdict(collections.Counter)
    path = os.path.join(state_dir, 'pr-review.jsonl')
    for line in open(path) if os.path.exists(path) else []:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get('run') != 'review' or not r.get('attempts') or not r.get('at'):
            continue
        c = rows[week_of(r['at'])]
        c['model_runs'] += 1
        c['r29_first'] += 'R29' in (r.get('gate_first') or [])  # the R29 miss rate (the mayor, 16:36Z)
        c['r29_held'] += 'R29' in (r.get('held_gate') or r.get('gate') or [])
        if r.get('fallback'):
            c['fallback'] += 1
            c['fallback_' + (r.get('held_by') or 'unsplit')] += 1
        elif (r.get('repaired') or r.get('rewritten')) and 'repair_held' in r and not r['repair_held']:
            c['repaired'] += 1
    return {w: dict(rows[w]) for w in sorted(rows)[-weeks:]}


def panel(cfg, summary, now):
    week = week_of(pr_state.iso(now))
    c = summary.get(week, {})
    med = c.get('median_min_to_their_review')
    return {'title': f"PR reviews ({cfg['mode']}), week {week}", 'at': pr_state.iso(now),
            'rows': [[str(c.get('delivered', 0)), f"reviews delivered ({c.get('outcome_escalate', 0)} needed you, "
                                                  f"{c.get('approved', 0)} approved)"],
                     [f"{c.get('thumbs_up', 0)} / {c.get('thumbs_down', 0)}", 'thumbs up / down on Slack'],
                     [f'{med} min' if med is not None else 'no data', 'median time to your own review'],
                     [str(c.get('their-review', 0)), 'PRs you reviewed after ours'],
                     [str(c.get('merged-without-their-review', 0)), 'merged before you looked'],
                     [str(c.get('held_gate', 0) + c.get('outcome_not-reviewed', 0)),
                      f"held by the gate ({c.get('regenerated', 0)} regenerated, "
                      f"{c.get('outcome_not-reviewed', 0)} got the short not-reviewed note)"],
                     [f"{c['median_useful']:g} / {c['median_markers']:g}" if c.get('judged') else 'no data',
                      f"reader test of what was posted, median useful / markers of 5 over {c.get('judged', 0)} "
                      f"full reviews ({c.get('judge_below_bar', 0)} below the bar); fallbacks: "
                      f"{c.get('fallback_judged', 0)} read"
                      + (f", median useful {c['median_fallback_useful']:g}" if c.get('fallback_judged') else '')
                      + f", {c.get('fallback_below_bar', 0)} below the bar; "
                        f"{c.get('reader_unscored', 0)} posted unscored"],
                     [f"{c.get('fallback', 0)} / {c.get('model_runs', 0)}",
                      f"review runs that ended in a fallback ({c.get('fallback_format', 0)} held by format reasons only, "
                      f"{c.get('fallback_content', 0)} by a content rule, {c.get('fallback_unsplit', 0)} before the split; "
                      f"{c.get('repaired', 0)} saved by a repair)"],
                     [f"{c.get('r29_first', 0)} / {c.get('model_runs', 0)}",
                      f"first answers that missed R29, plain words ({c.get('r29_held', 0)} still held by it at the end)"]],
            'note': (f"{c.get('replies', 0)} with a reply in Slack, {c.get('false_alarm', 0)} 'needs you' you approved "
                     f"as is. Read hourly from Slack and GitHub; the texts stay on the host.")}


def run(cfg, dry):
    t = pr_state.now()
    token = pr_state.slack_token(cfg['slack_env']) if cfg['slack_channel'] else None
    with pr_state.locked(cfg['state']) as st:
        todo = {k: dict(e) for k, e in st['prs'].items()
                if e.get('delivered_at') and e.get('outcome') in DELIVERED and not e.get('fb_final')}
    counts, updates = collections.Counter(), {}
    for key, e in sorted(todo.items()):
        kind, state, minutes = decision(e, cfg['reviewer'])
        thumbs, replies = slack_verdict(cfg, token, e)
        false_alarm = e['outcome'] == 'escalate' and state == 'APPROVED'
        counts[kind or 'open'] += 1
        if thumbs:
            counts['thumbs_' + thumbs] += 1
        age_d = (t - pr_state.parse(e['delivered_at'])).total_seconds() / 86400
        u = {'fb_at': pr_state.iso(t), 'fb_thumbs': thumbs, 'fb_replies': len(replies), 'fb_decision': kind,
             'fb_state': state, 'fb_minutes': minutes, 'fb_false_alarm': false_alarm,
             'fb_final': bool(kind and kind != 'unreadable') and age_d > 2 or age_d > 14}
        changed = any(u[k] != e.get(k) for k in ('fb_thumbs', 'fb_replies', 'fb_decision'))
        if changed and not dry:
            keep(cfg, {'key': key, 'url': e['url'], 'page': e.get('page'), 'head': e.get('head'),
                       'outcome': e['outcome'], 'thumbs': thumbs, 'replies': replies, 'decision': kind,
                       'their_state': state, 'minutes': minutes, 'false_alarm': false_alarm, 'at': pr_state.iso(t)})
        updates[key] = u
    with pr_state.locked(cfg['state']) as st:
        gaps = gap_asks(cfg, {k: dict(e) for k, e in st['prs'].items()}, dry)
    if dry:
        print(json.dumps({'dry_run': True, 'checked': len(todo), 'counts': counts, **gaps}))
        return
    with pr_state.locked(cfg['state']) as st:
        for k, u in updates.items():
            if k in st['prs']:
                st['prs'][k].update(u)
    if cfg['judge']:  # R26: the reader test of delivered reviews without scores, a few per run
        pr_judge.audit(cfg, int(os.environ.get('PR_REVIEW_JUDGE_PER_RUN', '5')))
    with pr_state.locked(cfg['state']) as st:
        summary = summarize(st, cfg['state'])
        for w, c in fallback_split(cfg['state']).items():
            summary.setdefault(w, {}).update(c)
        st['feedback_summary'] = summary
    os.makedirs(os.path.dirname(cfg['panel']) or '.', exist_ok=True)
    with open(cfg['panel'] + '.tmp', 'w') as f:
        json.dump(panel(cfg, summary, t), f, indent=1)
    os.replace(cfg['panel'] + '.tmp', cfg['panel'])
    pr_state.log(cfg['state'], {'run': 'feedback', 'checked': len(todo), 'counts': counts, **gaps})
    print(json.dumps({'feedback': pr_state.iso(t), 'checked': len(todo), 'counts': counts, **gaps,
                      'week': summary.get(week_of(pr_state.iso(t)), {})}))


GAP_WORDS = {'unverifiable-rule': 'a repository rule that no reading of the code can verify',
             'new-risk': 'a kind of risk the review method does not handle',
             'below-bar': 'reviews that could not reach the quality bar (the note or the minimal money review)',
             'not-checked': 'an area no check covers'}


def gap_asks(cfg, prs, dry):
    """decision-3aeec149: each new kind of gap in our review method becomes ONE rule-level ask to the
    reviewer (accept the gap, add a check, or change the rule), never one per PR and never a request to
    read lines. It is mailed to PR_REVIEW_GAP_ASK_TO (a town address; unset = counted only), with the
    review links. Accepted kinds (gaps/accepted.json) and kinds asked once (gaps/asked.json) are not asked."""
    accepted = pr_state.accepted_gaps(cfg)
    p = os.path.join(pr_state.private_dir(cfg, 'gaps'), 'asked.json')
    asked = set(json.load(open(p))) if os.path.exists(p) else set()
    seen = collections.defaultdict(list)
    for e in prs.values():
        for kind in e.get('gaps_open') or []:
            if e.get('review_url'):
                seen[kind].append(e['review_url'])
    new = sorted(k for k in seen if k not in accepted and k not in asked)
    to, sent = os.environ.get('PR_REVIEW_GAP_ASK_TO', ''), []
    for kind in ([] if dry or not to else new):
        urls = sorted(seen[kind])
        body = (f"Gap kind: {kind} ({GAP_WORDS.get(kind, 'a gap in the review method')}). Seen on {len(urls)} "
                f"reviews: {' '.join(urls[:10])}{' ...' if len(urls) > 10 else ''}\n"
                'One rule-level ask for the reviewer, for this kind (never per PR, never a request to read lines): '
                'A. accept the gap (it is then said to the author as scope, with no more Slack "Review gap"); '
                'B. add a check that covers it; C. change the rule.')
        r = subprocess.run([os.environ.get('GC_BIN') or 'gc', 'mail', 'send', to, '-s',
                            f'pr-review: a new kind of gap in our review method ({kind}): one rule-level ask', '-m', body],
                           cwd=cfg['city'], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            asked.add(kind)
            sent.append(kind)
    if sent:
        with open(p, 'w') as f:
            json.dump(sorted(asked), f)
        os.chmod(p, 0o600)
    return {'gap_kinds_open': {k: len(v) for k, v in sorted(seen.items())}, 'gap_asks_sent': sent,
            'gap_asks_waiting': [k for k in new if k not in sent]}


def record(cfg, key, verdict, by, note):
    """What was seen by hand, with who did it: counted (feedback_seed) and kept host-local."""
    t = pr_state.iso(pr_state.now())
    with pr_state.locked(cfg['state']) as st:
        st.setdefault('feedback_seed', []).append({'key': key, 'verdict': verdict, 'by': by, 'at': t})
    keep(cfg, {'key': key, 'url': f"https://github.com/{key.replace('#', '/pull/')}", 'verdict': verdict, 'by': by,
               'note': note, 'at': t, 'by_hand': True})
    print(json.dumps({'recorded': key, 'verdict': verdict, 'by': by}))


def selftest():
    """Synthetic: the reader card counts the scores of the body each review posted (gcs-6aq). Prints checks."""
    import tempfile
    good = {'scan': 4, 'markers': 4, 'decisions': 3, 'plain': 4, 'length': 4, 'useful': 3}
    low = dict(good, markers=3, useful=2)
    t = '2026-10-28T10:00:00Z'
    log = [
        {'run': 'review', 'at': t, 'review_id': 1, 'posted': 'ok', 'judge': good},  # a full review, read
        # a fallback: the held attempt's scores in judge, the posted fallback's own in fallback_judge
        {'run': 'review', 'at': t, 'review_id': 2, 'posted': 'ok', 'fallback': 'not-checked-minimal',
         'judge': low, 'fallback_judge': good},
        # a fallback after a repair the reader already read: no score of its own
        {'run': 'review', 'at': t, 'review_id': 3, 'posted': 'ok', 'fallback': 'not-reviewed', 'judge': low,
         'repair_judged': True},
        # an earlier head's review, read; then a run that posted a new review without the reader
        {'run': 'review', 'at': '2026-10-28T09:00:00Z', 'review_id': 40, 'posted': 'ok', 'judge': low},
        {'run': 'review', 'at': t, 'review_id': 4, 'posted': 'ok'},
        {'run': 'review', 'at': t, 'review_id': 5, 'posted': 'ok'},  # read by the audit afterwards
        # read by the audit, then updated in place by a re-review that fell back
        {'run': 'review', 'at': '2026-10-28T08:00:00Z', 'review_id': 6, 'posted': 'ok', 'judge': good},
        {'run': 'review', 'at': t, 'review_id': 6, 'posted': 'updated', 'fallback': 'not-reviewed'},
        {'run': 'review', 'at': t, 'outcome': 'held-gate', 'judge': low},  # posted nothing
    ]

    def entry(rid, outcome='questions', **kw):
        return dict({'delivered_at': t, 'outcome': outcome, 'review_id': rid}, **kw)
    st = {'prs': {
        'o/r#1': entry(1, 'low-risk', judge=good),
        'o/r#2': entry(2, judge=low, fallback='not-checked-minimal'),
        'o/r#3': entry(3, 'not-reviewed', judge=low),
        'o/r#4': entry(4, judge=low),  # review 40's scores, carried to the new head's entry
        'o/r#5': entry(5, judge=good, judge_review=5, judge_at='2026-10-28T11:00:00Z'),
        'o/r#6': entry(6, judge=low, judge_review=6, judge_at='2026-10-28T09:00:00Z'),
        'o/r#7': entry(7, judge=good),  # no run row names review 7
        'o/r#8': {'outcome': 'held-gate', 'judge': low},  # never delivered
    }}
    d = tempfile.mkdtemp(prefix='pr-feedback-selftest-')
    with open(os.path.join(d, 'pr-review.jsonl'), 'w') as f:
        f.write(''.join(json.dumps(r) + '\n' for r in log))
    got = posted_scores(st, d)
    c = summarize(st, d)[week_of(t)]
    card = panel({'mode': 'shadow'}, {week_of(t): c}, pr_state.parse(t))['rows'][6]
    checks = {
        'a full review keeps its run\'s scores': got['o/r#1'] == ('review', good),
        'a fallback is scored by its own reader test, not the held attempt\'s': got['o/r#2'] == ('fallback', good),
        'a fallback with no score of its own is unscored': got['o/r#3'] == ('fallback', None),
        'an earlier run\'s scores never stand for a new review': got['o/r#4'] == ('review', None),
        'the audit\'s scores of the posted review count': got['o/r#5'] == ('review', good),
        'an audit before an update in place does not count': got['o/r#6'] == ('fallback', None),
        'a review no run names is unscored': got['o/r#7'] == ('unmatched', None),
        'only delivered reviews are scored': 'o/r#8' not in got,
        'the week: 2 full reviews read, none below the bar': (c.get('judged'), c.get('judge_below_bar')) == (2, 0),
        'the week: 1 fallback read, 4 posted unscored': (c.get('fallback_judged'), c.get('reader_unscored')) == (1, 4),
        'the medians are of the full reviews': (c.get('median_useful'), c.get('median_markers')) == (3, 4),
        'the card names the split and the unscored': card[0] == '3 / 4' and 'over 2 full reviews' in card[1]
        and 'fallbacks: 1 read, median useful 3' in card[1] and '4 posted unscored' in card[1],
    }
    print(json.dumps({'selftest': 'pr_feedback', 'checks': checks}, indent=1))
    return 0 if all(checks.values()) else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--summary', action='store_true',
                    help="print the weeks' numbers and the card from the state and the log; writes nothing, zero tokens")
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--record', nargs=2, metavar=('KEY', 'VERDICT'))
    ap.add_argument('--by', default='')
    ap.add_argument('--note', default='')
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    cfg = pr_state.config()
    if a.summary:  # read without the lock: the lock writes the state back
        path = os.path.join(cfg['state'], 'prs.json')
        st = json.load(open(path)) if os.path.exists(path) else {'prs': {}}
        summary = summarize(st, cfg['state'])
        for w, c in fallback_split(cfg['state']).items():
            summary.setdefault(w, {}).update(c)
        print(json.dumps({'summary': summary, 'card': panel(cfg, summary, pr_state.now())}, indent=1))
        return
    if a.record:
        if a.record[1] not in VERDICTS or not a.by:
            sys.exit(f'VERDICT must be one of {sorted(VERDICTS)}, and --by must say who did it')
        record(cfg, a.record[0], a.record[1], a.by, a.note)
        return
    if not cfg['on'] and not a.dry_run:
        print(json.dumps({'skipped': 'PR_REVIEW is off'}))
        return
    if pr_rules.load_avg() > cfg['max_load']:
        pr_state.log(cfg['state'], {'run': 'feedback', 'skipped': 'load'})
        print(json.dumps({'skipped': 'load'}))
        return
    run(cfg, a.dry_run)


if __name__ == '__main__':
    main()
