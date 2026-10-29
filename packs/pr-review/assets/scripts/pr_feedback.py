#!/usr/bin/env python3
"""Review feedback (README.md, "Quality loop"): what the reviewer did with each posted review, read from
GitHub and Slack, with no extra work for them.

  pr_feedback.py                     one pass over the posted reviews (what the hourly order runs)
  pr_feedback.py --dry-run           the same, printing counts; writes nothing
  pr_feedback.py --record KEY VERDICT [--note TEXT]
                                     record a verdict seen by hand (e.g. reviews deleted before this order ran)

Verdicts:
  accepted-unchanged  the reviewer submitted our pending review as it was (good)
  edited              they changed it before submitting (partial: their version is kept host-local)
  discarded           our pending review is gone and nothing was submitted (bad)
  waiting / editing   still pending, unchanged / changed (not final)
  false alarm         a "needs you" that the reviewer then approved without changes (flag on top)
  Slack: a thumbs-up or thumbs-down on the link message, and replies in its thread (their reason).

Host-local (PR_REVIEW_WORK_DIR/feedback, mode 700): every verdict with its text (their edits, replies).
A bad or partial verdict also drops a candidate regression case in PR_REVIEW_WORK_DIR/regression/inbox
for the weekly triage. The state file and the summary get verdicts and counts only. The summary goes to
PR_REVIEW_PANEL as a Today-page card (quality pack, QUALITY_PANELS).
"""
import argparse
import collections
import datetime as dt
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_rules  # noqa: E402
import pr_state  # noqa: E402
from pr_review import body_sha  # noqa: E402

REVIEWED = {'low-risk', 'questions', 'changes', 'escalate'}
FINAL = {'accepted-unchanged', 'edited', 'discarded'}
GOOD, BAD = {'+1', 'thumbsup', 'white_check_mark', 'heavy_check_mark'}, {'-1', 'thumbsdown', 'x'}


def reviews_by(repo, number, reviewer):
    r = subprocess.run(['gh', 'api', '--paginate', f'repos/{repo}/pulls/{number}/reviews?per_page=100', '--jq',
                        '.[] | {id, state, login: .user.login, body, submitted_at}'],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return None
    rows = [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
    return [x for x in rows if x['login'].lower() == reviewer.lower()]


def github_verdict(entry, mine):
    """(verdict, their body or None, their review state or None)."""
    posted = entry.get('posted_at') or ''
    done = [r for r in mine if r['state'] != 'PENDING' and (r.get('submitted_at') or '') >= posted]
    if done:
        r = done[-1]
        same = entry.get('body_sha') and body_sha(r['body'] or '') == entry['body_sha']
        return ('accepted-unchanged' if same else 'edited'), (None if same else r['body']), r['state']
    pending = [r for r in mine if r['state'] == 'PENDING']
    if pending:
        same = entry.get('body_sha') and body_sha(pending[0]['body'] or '') == entry['body_sha']
        return ('waiting' if same else 'editing'), None, None
    return 'discarded', None, None


def slack_verdict(cfg, token, entry):
    """(thumbs 'up' / 'down' / None, [reply texts]) for our link message."""
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
    thumbs = 'down' if names & BAD else ('up' if names & GOOD else None)
    return thumbs, replies


def keep(cfg, rec):
    """One verdict, with any text, in the host-local feedback log; bad or partial ones also become a
    candidate regression case (inbox) holding the body we posted."""
    d = pr_state.private_dir(cfg, 'feedback')
    with open(os.path.join(d, 'feedback.jsonl'), 'a') as f:
        f.write(json.dumps(rec) + '\n')
    os.chmod(os.path.join(d, 'feedback.jsonl'), 0o600)
    if rec['verdict'] in ('edited', 'discarded') or rec.get('thumbs') == 'down' or rec.get('false_alarm'):
        posted = os.path.join(d, 'posted', f"{rec['key'].replace('/', '__').replace('#', '-')}-{(rec.get('head') or '')[:10]}.json")
        body = json.load(open(posted)).get('body') if os.path.exists(posted) else None
        inbox = pr_state.private_dir(cfg, 'regression', 'inbox')
        p = os.path.join(inbox, f"{rec['key'].replace('/', '__').replace('#', '-')}-{rec['verdict']}.json")
        with open(p, 'w') as f:
            json.dump({'expect': 'fail', 'todo': 'triage: add or change a style-guide rule so the gate fails this',
                       'outcome': rec.get('outcome'), 'n_items': rec.get('items', 0), 'body': body,
                       'feedback': {k: rec.get(k) for k in ('verdict', 'thumbs', 'replies', 'their_body', 'false_alarm')}},
                      f)
        os.chmod(p, 0o600)


def week_of(iso):
    y, w, _ = pr_state.parse(iso).isocalendar()
    return f'{y}-W{w:02d}'


def summarize(st, weeks=2):
    """Counts per ISO week of posting (verdicts, thumbs, false alarms) and gate holds per week of review."""
    rows = collections.defaultdict(collections.Counter)
    for e in st['prs'].values():
        if e.get('posted_at') and e.get('outcome') in REVIEWED:
            c = rows[week_of(e['posted_at'])]
            c['posted'] += 1
            c[e.get('fb') or 'waiting'] += 1
            c['thumbs_' + e['fb_thumbs']] += 1 if e.get('fb_thumbs') else 0
            c['false_alarm'] += 1 if e.get('fb_false_alarm') else 0
        if e.get('outcome') == 'held-gate' and e.get('reviewed_at'):
            rows[week_of(e['reviewed_at'])]['held_gate'] += 1
        if e.get('gate_first') and e.get('reviewed_at'):
            rows[week_of(e['reviewed_at'])]['regenerated'] += 1
    for s in st.get('feedback_seed', []):
        rows[week_of(s['at'])][s['verdict']] += 1
        rows[week_of(s['at'])]['posted'] += 1
    return {w: dict(rows[w]) for w in sorted(rows)[-weeks:]}


def panel(cfg, summary, now):
    week = week_of(pr_state.iso(now))
    c = summary.get(week, {})
    posted = c.get('posted', 0)
    pct = (lambda n: f'{round(100 * n / posted)}%' if posted else 'no data')
    return {'title': f"PR reviews ({cfg['mode']}), week {week}", 'at': pr_state.iso(now),
            'rows': [[str(posted), 'reviews posted'],
                     [pct(c.get('accepted-unchanged', 0)), f"accepted unchanged ({c.get('accepted-unchanged', 0)})"],
                     [str(c.get('edited', 0)), 'edited before submitting'],
                     [pct(c.get('discarded', 0)), f"discarded ({c.get('discarded', 0)})"],
                     [str(c.get('false_alarm', 0)), '"needs you" he approved unchanged (false alarms)'],
                     [str(c.get('held_gate', 0)), f"held by the gate ({c.get('regenerated', 0)} regenerated)"]],
            'note': (f"{c.get('waiting', 0) + c.get('editing', 0)} still pending. Read from GitHub and Slack by the "
                     f"pr-review-feedback order; texts stay on the host.")}


def run(cfg, dry):
    t = pr_state.now()
    token = pr_state.slack_token(cfg['slack_env']) if cfg['slack_channel'] else None
    with pr_state.locked(cfg['state']) as st:
        todo = {k: dict(e) for k, e in st['prs'].items()
                if e.get('posted_at') and e.get('outcome') in REVIEWED and e.get('fb') not in FINAL}
    counts, updates = collections.Counter(), {}
    for key, e in sorted(todo.items()):
        mine = reviews_by(e['repo'], e['number'], cfg['reviewer'])
        if mine is None:
            counts['unreadable'] += 1
            continue
        verdict, theirs, state = github_verdict(e, mine)
        thumbs, replies = slack_verdict(cfg, token, e)
        false_alarm = e['outcome'] == 'escalate' and state == 'APPROVED' and verdict in ('accepted-unchanged', 'edited')
        counts[verdict] += 1
        changed = (verdict != e.get('fb') or thumbs != e.get('fb_thumbs') or len(replies) != e.get('fb_replies', 0))
        if changed and not dry:
            keep(cfg, {'key': key, 'url': e['url'], 'head': e.get('head'), 'outcome': e['outcome'], 'items': e.get('items'),
                       'verdict': verdict, 'thumbs': thumbs, 'replies': replies, 'their_body': theirs,
                       'their_state': state, 'false_alarm': false_alarm, 'at': pr_state.iso(t)})
        updates[key] = {'fb': verdict, 'fb_at': pr_state.iso(t), 'fb_thumbs': thumbs, 'fb_replies': len(replies),
                        'fb_false_alarm': false_alarm}
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


def record(cfg, key, verdict, note):
    """A verdict seen by hand: counted in the summary (feedback_seed) and kept in the host-local log."""
    t = pr_state.iso(pr_state.now())
    with pr_state.locked(cfg['state']) as st:
        st.setdefault('feedback_seed', []).append({'key': key, 'verdict': verdict, 'at': t})
    keep(cfg, {'key': key, 'url': f"https://github.com/{key.replace('#', '/pull/')}", 'verdict': verdict,
               'note': note, 'at': t, 'by_hand': True})
    print(json.dumps({'recorded': key, 'verdict': verdict}))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--record', nargs=2, metavar=('KEY', 'VERDICT'))
    ap.add_argument('--note', default='')
    a = ap.parse_args()
    cfg = pr_state.config()
    if a.record:
        if a.record[1] not in FINAL:
            sys.exit(f'VERDICT must be one of {sorted(FINAL)}')
        record(cfg, a.record[0], a.record[1], a.note)
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
