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
import pr_rules  # noqa: E402
import pr_state  # noqa: E402

DELIVERED = {'low-risk', 'questions', 'changes', 'escalate'}
GOOD, BAD = {'+1', 'thumbsup', 'white_check_mark', 'heavy_check_mark'}, {'-1', 'thumbsdown', 'x'}
VERDICTS = {'good', 'bad', 'rejected'}


def reviews_by(repo, number, reviewer):
    r = subprocess.run(['gh', 'api', '--paginate', f'repos/{repo}/pulls/{number}/reviews?per_page=100', '--jq',
                        '.[] | {id, state, login: .user.login, submitted_at, ours: (.body | startswith("Automated review"))}'],
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


def summarize(st, weeks=2):
    """Per ISO week of delivery: counts by outcome, thumbs, replies, the reviewer's decisions and the median
    minutes to their own review; gate holds and regenerations per week of review."""
    rows, mins = collections.defaultdict(collections.Counter), collections.defaultdict(list)
    for e in st['prs'].values():
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
    return out


def panel(cfg, summary, now):
    week = week_of(pr_state.iso(now))
    c = summary.get(week, {})
    med = c.get('median_min_to_their_review')
    return {'title': f"PR reviews ({cfg['mode']}), week {week}", 'at': pr_state.iso(now),
            'rows': [[str(c.get('delivered', 0)), f"reviews delivered ({c.get('outcome_escalate', 0)} needed you)"],
                     [f"{c.get('thumbs_up', 0)} / {c.get('thumbs_down', 0)}", 'thumbs up / down on Slack'],
                     [f'{med} min' if med is not None else 'no data', 'median time to your own review'],
                     [str(c.get('their-review', 0)), 'PRs you reviewed after ours'],
                     [str(c.get('merged-without-their-review', 0)), 'merged before you looked'],
                     [str(c.get('held_gate', 0)), f"held by the gate ({c.get('regenerated', 0)} regenerated)"]],
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
    if dry:
        print(json.dumps({'dry_run': True, 'checked': len(todo), 'counts': counts}))
        return
    with pr_state.locked(cfg['state']) as st:
        for k, u in updates.items():
            if k in st['prs']:
                st['prs'][k].update(u)
        summary = summarize(st)
        st['feedback_summary'] = summary
    os.makedirs(os.path.dirname(cfg['panel']) or '.', exist_ok=True)
    with open(cfg['panel'] + '.tmp', 'w') as f:
        json.dump(panel(cfg, summary, t), f, indent=1)
    os.replace(cfg['panel'] + '.tmp', cfg['panel'])
    pr_state.log(cfg['state'], {'run': 'feedback', 'checked': len(todo), 'counts': counts})
    print(json.dumps({'feedback': pr_state.iso(t), 'checked': len(todo), 'counts': counts,
                      'week': summary.get(week_of(pr_state.iso(t)), {})}))


def record(cfg, key, verdict, by, note):
    """What was seen by hand, with who did it: counted (feedback_seed) and kept host-local."""
    t = pr_state.iso(pr_state.now())
    with pr_state.locked(cfg['state']) as st:
        st.setdefault('feedback_seed', []).append({'key': key, 'verdict': verdict, 'by': by, 'at': t})
    keep(cfg, {'key': key, 'url': f"https://github.com/{key.replace('#', '/pull/')}", 'verdict': verdict, 'by': by,
               'note': note, 'at': t, 'by_hand': True})
    print(json.dumps({'recorded': key, 'verdict': verdict, 'by': by}))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--record', nargs=2, metavar=('KEY', 'VERDICT'))
    ap.add_argument('--by', default='')
    ap.add_argument('--note', default='')
    a = ap.parse_args()
    cfg = pr_state.config()
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
