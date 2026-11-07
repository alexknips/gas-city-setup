#!/usr/bin/env python3
"""PR review sweep (README.md, "Sweep"): which PRs need a review, from zero-token rules only.

  pr_sweep.py                   one sweep: PRs updated since the last one (open, a draft only if it
                                requests the reviewer, or merged
                                since), each new head through the rules, one bead per PR, the review
                                queue in the state file. No model, nothing posted.
  pr_sweep.py --dry-run         the same, printed; writes nothing, creates nothing
  pr_sweep.py --backtest DAYS   every PR opened in the last DAYS days, any state, through the rules:
                                how many each rule flags and what the daily cap would reach
                                (calibration; writes one record file under the state dir)
  pr_sweep.py --close-terminal [--apply]
                                one-off: close the beads the sweep left open for PRs whose outcome is
                                terminal (before 2026-10-30 an existing bead was never closed); dry run
                                without --apply; counts only
  pr_sweep.py --selftest        the bead closing, on a fake store

Config: environment, see pr_state.config() and README.md, "Configuration".
"""
import argparse
import collections
import concurrent.futures as cf
import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pr_rules  # noqa: E402
import pr_state  # noqa: E402


def cached(cache, name, fetch):
    """fetch(), kept as JSON at cache/name when a cache dir is given (a re-run reads it back)."""
    if not cache:
        return fetch()
    path = os.path.join(cache, name)
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    val = fetch()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.tmp', 'w') as f:
        json.dump(val, f)
    os.replace(path + '.tmp', path)
    return val


def record(cfg, repo, pr, files, ruleset, area_of, heads):
    """The rule verdict on one PR head: ids, counts and links only."""
    hits = pr_rules.evaluate(pr, files, ruleset, cfg['reviewer'], repo, pr_rules.base_for(pr, heads))
    return {
        'repo': repo, 'number': pr['number'], 'url': pr['url'], 'state': pr['state'],
        'draft': pr['isDraft'], 'created': pr['createdAt'], 'merged': pr['mergedAt'],
        'author': (pr.get('author') or {}).get('login'), 'head': pr['headRefOid'],
        'lines': pr['additions'] + pr['deletions'], 'files': len(files), 'files_gh': pr['changedFiles'],
        'patch_missing': sum(1 for f in files if f.get('patch') is None and f['status'] != 'removed'),
        'ci': pr_rules.ci_state(pr), 'hits': hits,
        'selected': pr_rules.selected(hits, ruleset, area_of),
        'tier': pr_rules.priority(hits, ruleset, area_of),
        'areas': sorted({area_of[h] for h in hits if area_of.get(h) in ruleset['select_areas']}),
    }


def backtest(cfg, days, workers, cache=None):
    """Every PR opened in the window, any state, through the rules. With `cache`, GitHub's answers are
    kept there (they hold the repositories' code: keep that dir out of the town's runtime and delete it
    after calibrating) and a re-run with the same window start reads them back."""
    ruleset = pr_rules.load_rules(cfg['rules'])
    area_of = pr_rules.areas(ruleset)
    now = pr_state.now()
    since = now - dt.timedelta(days=days)
    if cache:
        stamp = os.path.join(cache, 'since')
        if os.path.exists(stamp):
            since = dt.datetime.fromisoformat(open(stamp).read().strip())
        else:
            os.makedirs(cache, exist_ok=True)
            open(stamp, 'w').write(since.isoformat())
    records = []
    for repo in cfg['repos']:
        key = repo.replace('/', '__')
        prs = cached(cache, f'{key}/search.json', lambda: pr_rules.search_prs(
            f'repo:{repo} is:pr created:>={pr_state.iso(since)}'))
        heads = {'main': cached(cache, f'{key}/main.json', lambda: pr_rules.branch_oid(repo, 'main'))}

        def one(pr):
            files = cached(cache, f'{key}/files-{pr["number"]}-{pr["headRefOid"]}.json',
                           lambda: pr_rules.pr_files(repo, pr['number']))
            return record(cfg, repo, pr, files, ruleset, area_of, heads)

        with cf.ThreadPoolExecutor(workers) as ex:
            records += list(ex.map(one, prs))
    return ruleset, area_of, since, now, records


def within_cap(sel, cap):
    """Per tier, how many selected PRs the daily cap would reach, filling each day in priority order
    (tier, then more areas, then oldest), as the review order does."""
    got = collections.Counter()
    by_day = collections.defaultdict(list)
    for r in sel:
        by_day[r['created'][:10]].append(r)
    for day in by_day.values():
        day.sort(key=lambda r: (r['tier'], -len(r['areas']), r['created']))
        for r in day[:cap]:
            got[f"t{r['tier']}"] += 1
    return dict(sorted(got.items()))


def summarize(cfg, ruleset, area_of, since, now, records):
    out = {'window': [since.isoformat(), now.isoformat()], 'cap_per_day': cfg['cap'], 'repos': {}}
    rule_ids = list(pr_rules.BUILTIN) + [r['id'] for r in ruleset['rules']]
    for repo in list(cfg['repos']) + ['ALL']:
        rs = [r for r in records if repo in ('ALL', r['repo'])]
        if not rs:
            out['repos'][repo] = {'prs': 0}
            continue
        sel = [r for r in rs if r['selected']]
        per_day = collections.Counter(r['created'][:10] for r in sel)
        lines = sorted(r['lines'] for r in rs)
        only = collections.Counter()
        for r in sel:
            sel_rules = [h for h in r['hits'] if h == 'requested' or area_of.get(h) in ruleset['select_areas']]
            if len(sel_rules) == 1:
                only[sel_rules[0]] += 1
        rules = {}
        for rid in rule_ids:
            flagged = [r for r in rs if rid in r['hits']]
            if not flagged:
                rules[rid] = {'prs': 0}
                continue
            pats = collections.Counter(m for r in flagged for m in set(r['hits'][rid]))
            rules[rid] = {
                'area': area_of[rid], 'prs': len(flagged), 'pct': round(100 * len(flagged) / len(rs)),
                'only_reason': only.get(rid, 0), 'markers': dict(pats.most_common()),
                'examples': [r['url'] for r in flagged[:3]],
            }
        merged = sorted((pr_state.parse(r['merged']) - pr_state.parse(r['created'])).total_seconds() / 60
                        for r in rs if r['merged'])
        out['repos'][repo] = {
            'prs': len(rs),
            'states': dict(collections.Counter(r['state'] + ('/draft' if r['draft'] else '') for r in rs)),
            'authors': len({r['author'] for r in rs}),
            'lines_median': lines[len(lines) // 2], 'lines_p90': lines[int(len(lines) * 0.9)],
            'merge_minutes_median': round(merged[len(merged) // 2]) if merged else None,
            'merged_within_30m': sum(1 for m in merged if m <= 30),
            'ci': dict(collections.Counter(r['ci'] for r in rs)),
            'requested': sum(1 for r in rs if 'requested' in r['hits']),
            'selected': len(sel),
            'no_rule': len(rs) - len(sel),
            'selected_per_day': dict(sorted(per_day.items())),
            'over_cap': sum(max(0, n - cfg['cap']) for n in per_day.values()),
            'tiers': dict(collections.Counter(f"t{r['tier']}" for r in sel)),
            'reviewed_within_cap_by_tier': within_cap(sel, cfg['cap']),
            'patch_missing_prs': sum(1 for r in rs if r['patch_missing']),
            'file_count_mismatch': sum(1 for r in rs if r['files'] != r['files_gh']),
            'rules': rules,
        }
    return out


def sweep(cfg, dry):
    """One sweep. Each PR is keyed '<repo>#<n>' in the state file; a head is looked at once."""
    ruleset = pr_rules.load_rules(cfg['rules'])
    area_of = pr_rules.areas(ruleset)
    beads = pr_state.Beads(cfg['city'], enabled=cfg['beads'] and not dry)
    t0 = pr_state.now()
    with pr_state.locked(cfg['state']) as st:
        last = pr_state.parse(st['last_sweep']) if st.get('last_sweep') else t0 - dt.timedelta(hours=cfg['lookback_h'])
        waiting = [k for k, v in st['prs'].items() if v.get('status') == 'waiting-ci']
        known = set(st['prs'])
    since = last - dt.timedelta(minutes=15)  # search lags; heads already seen are skipped
    found, counts = {}, collections.Counter()
    open_keys = set()
    for repo in cfg['repos']:
        prs, heads = pr_rules.open_prs(repo)
        open_keys |= {f"{repo}#{pr['number']}" for pr in prs}
        # R15: open PRs only. A merged or closed PR is never reviewed.
        for pr in prs:
            key = f"{repo}#{pr['number']}"
            # An open PR nobody touched since the last sweep, and never seen, is not news: the first
            # sweep starts PR_REVIEW_LOOKBACK_H back instead of at every PR ever opened. A PR that requests
            # the reviewer is always news.
            if pr['state'] == 'OPEN' and key not in known and pr_state.parse(pr['updatedAt']) < since \
                    and not pr_rules.requested_now(pr, cfg['reviewer']):
                counts['untouched'] += 1
                continue
            found[key] = (repo, pr, heads)
    for key in waiting:
        if key not in found:
            repo, n = key.rsplit('#', 1)
            pr = pr_rules.get_pr(repo, int(n))
            if pr:
                found[key] = (repo, pr, {pr['baseRefName']: pr_rules.branch_oid(repo, pr['baseRefName'])})
    with pr_state.locked(cfg['state']) as st:
        seen = {k: dict(v) for k, v in st['prs'].items()}
    # R15: a queued or waiting PR that is no longer open leaves the queue ('closed', no Slack).
    gone = [k for k, v in seen.items() if v.get('status') in ('queued', 'waiting-ci') and k not in open_keys]
    for k in gone:
        counts['closed'] += 1
        if not dry:
            record_one(cfg, beads, k, dict(seen[k], status='done', outcome='closed'), seen[k], counts)
    refresh_ended(cfg, beads, seen, open_keys, counts, dry, t0)
    partial = False
    for key, (repo, pr, heads) in sorted(found.items()):
        if (pr_state.now() - t0).total_seconds() > cfg['sweep_budget_s']:
            # Out of time: what is recorded stays recorded, and last_sweep does not move, so the next
            # sweep searches the same window again and skips the heads already seen.
            partial = True
            counts['left_for_next'] += 1
            continue
        old = seen.get(key, {})
        asked = pr_rules.requested_now(pr, cfg['reviewer'])
        if pr['isDraft'] and pr['state'] == 'OPEN':
            counts['draft'] += 1
            if pr_rules.draft_skipped(True, asked):  # gcs-g9e: only a draft that requests the reviewer is reviewed
                continue
            counts['draft_requested'] += 1
        if pr_rules.approved_by(pr, cfg['reviewer']):  # decision-527cf053: approved stays approved, nothing queued
            counts['approved'] += 1
            if old.get('status') in ('queued', 'waiting-ci') and not dry:
                e = dict(old, status='done', outcome=old.get('outcome') or 'approved')
                for k in ('followup', 'answers_pending'):
                    e.pop(k, None)
                record_one(cfg, beads, key, e, old, counts)
            continue
        if pr_rules.eval_case(pr):  # gcs-71c: a red team's test PR is reviewed by hand only, in draft or not
            counts['eval'] += 1
            continue
        block = pr_rules.formal_block(old, pr['headRefOid'], pr.get('mergeable'), pr_rules.ci_state(pr))
        again = pr_rules.asked_again(old, pr['headRefOid'], asked) or \
            block == 'review'  # our block rested on content: the new head gets a review
        # gcs-b3f: asked again after our review of this very head (a submitted review clears the request)
        asked_at = pr_rules.last_asked(pr, cfg['reviewer']) if asked else None
        reask = cfg['followups'] and pr_rules.asked_after_review(old, pr['headRefOid'], asked_at, cfg['followup_rounds'])
        if cfg['followups'] and not reask and pr_rules.asked_after_review(old, pr['headRefOid'], asked_at, float('inf')):
            counts['asked_capped'] += 1  # asked again, and this head's rounds are used up
        if old.get('head') == pr['headRefOid'] and old.get('status') != 'waiting-ci' and not (again or reask):
            ans = answers(cfg, repo, pr, old, since) if cfg['followups'] else None
            if not ans:
                counts['seen'] += 1
                continue
            # decision-57b6d9bb: the author answered on the head we reviewed: a same-head follow-up (or the
            # one line that the rounds are used up), first in the queue.
            entry = dict(old, status='queued', queued_at=pr_state.iso(t0), followup=ans['do'], answers_pending=ans['key'],
                         tier=0)
            counts[f"followup_{ans['do']}"] += 1
            if dry:
                print(json.dumps({'pr': entry['url'], 'status': 'queued', 'followup': ans['do'],
                                  **{k: ans['act'][k] for k in ('replies', 'comments', 'reviews', 'resolved')}}))
                continue
            record_one(cfg, beads, key, entry, old, counts)
            continue
        files = pr_rules.pr_files(repo, pr['number'])
        rec = record(cfg, repo, pr, files, ruleset, area_of, heads)
        entry = {**{k: old[k] for k in CARRY if k in old}, **{k: v for k, v in old.items() if k.startswith('fb_')},
                 'repo': repo, 'number': pr['number'], 'url': pr['url'], 'rig': cfg['repos'][repo],
                 'head': rec['head'], 'state': rec['state'], 'created': rec['created'], 'merged': rec['merged'],
                 'rules': sorted(rec['hits']), 'tier': rec['tier'], 'areas': rec['areas'], 'ci': rec['ci'],
                 'seen_at': old.get('seen_at') or pr_state.iso(t0), 'bead': old.get('bead'),
                 # CI is waited for on each head from when the sweep first saw that head
                 'head_seen_at': (old.get('head_seen_at') or old.get('seen_at')) if old.get('head') == rec['head']
                 else pr_state.iso(t0)}
        ci_wait = pr_state.parse(entry['head_seen_at'] or entry['seen_at']) + dt.timedelta(hours=cfg['ci_wait_h'])
        # decision-57b6d9bb: a new head on a PR we reviewed is reviewed again once its CI settles (incremental);
        # PR_REVIEW_QUEUE=requested keeps an unrequested new PR out of the queue.
        push = cfg['followups'] and pr_rules.followup_push(old, rec['head'])
        wanted = push or reask or again or \
            pr_rules.in_queue(cfg['queue'], 'requested' in rec['hits'], bool(old.get('review_heads')))
        if reask and old.get('status') == 'done':  # a new request: counted once, when it is first queued
            rounds = dict(old.get('asked_rounds') or {})
            rounds[rec['head']] = rounds.get(rec['head'], 0) + 1
            entry.update(asked_at=asked_at, asked_rounds=dict(list(rounds.items())[-10:]))
            counts['asked_again'] += 1
        mark = {'followup': 'push'} if push else {'followup': 'asked'} if reask else {}
        if not wanted and rec['tier'] is not None:
            entry.update(status='done', outcome='unrequested')
        elif rec['ci'] == 'pending' and t0 < ci_wait:
            entry.update(status='waiting-ci', **mark)
        elif push:
            entry.update(status='queued', queued_at=pr_state.iso(t0), followup='push',
                         tier=rec['tier'] if rec['tier'] is not None else 2)
        elif reask:  # gcs-b3f: the re-review a person asked for, first in line like an answer follow-up
            entry.update(status='queued', queued_at=pr_state.iso(t0), followup='asked', tier=0)
        elif old.get('status') in ('done', 'reviewing') and not again and not (
                pr_rules.SERIOUS & (set(rec['hits']) - set(old.get('rules', [])))):
            # Pushed again after a review: reviewed once unless the reviewer is asked again or the new head
            # trips a serious rule the reviewed head did not.
            entry.update(status=old['status'], outcome=old.get('outcome'), head=old['head'], new_head=rec['head'])
        elif rec['tier'] is None:
            entry.update(status='done', outcome='no-rule')
        else:
            entry.update(status='queued', queued_at=pr_state.iso(t0))
        if block == 'lift' and not dry and old.get('review_id'):
            ok = lift_block(repo, pr, old['review_id'])
            counts['block_lifted' if ok else 'block_lift_failed'] += 1
            entry.update(rc_live=not ok and old.get('rc_live'), lifted_at=pr_state.iso(t0) if ok else None)
        counts[entry['status'] if entry['status'] != 'done' else entry.get('outcome', 'done')] += 1
        if dry:
            print(json.dumps({'pr': entry['url'], 'status': entry['status'], 'outcome': entry.get('outcome'),
                              'tier': entry['tier'], 'rules': entry['rules'], 'ci': entry['ci'],
                              **({'followup': entry['followup']} if entry.get('followup') else {})}))
            continue
        record_one(cfg, beads, key, entry, old, counts)
    if dry:
        print(json.dumps({'dry_run': True, 'since': pr_state.iso(since), 'counts': counts}))
        return
    if not partial:
        with pr_state.locked(cfg['state']) as st:
            st['last_sweep'] = pr_state.iso(t0)
    pruned = pr_state.prune_private(cfg, closed_at)  # PR_REVIEW_KEEP_DAYS on the stores of the repositories' text
    pr_state.log(cfg['state'], {'run': 'sweep', 'since': pr_state.iso(since), 'counts': counts, 'partial': partial,
                                'secs': round((pr_state.now() - t0).total_seconds()), 'pruned': pruned})
    print(json.dumps({'sweep': pr_state.iso(t0), 'partial': partial, 'counts': counts}))


def refresh_ended(cfg, beads, seen, open_keys, counts, dry, t0):
    """gcs-pw4: a done PR that left the open list is read again until GitHub says merged or closed; then its
    state is recorded and its bead closes. Before, only a queued or waiting PR was looked at again, so a PR
    with a review outcome kept 'OPEN' and an open bead after its merge. Zero tokens: one GraphQL call per 50
    PRs; half the sweep's budget at most (a bead close is two `gc bd` calls), the rest waits for the next."""
    ended = collections.defaultdict(list)
    for k, v in seen.items():
        repo, n = k.rsplit('#', 1)
        if k not in open_keys and repo in cfg['repos'] and v.get('status') == 'done' and \
                v.get('state', 'OPEN') == 'OPEN' and n.isdigit():
            ended[repo].append(int(n))
    for repo, numbers in sorted(ended.items()):
        try:
            states = pr_states(repo, numbers)
        except pr_rules.GhError:
            counts['refresh_error'] += 1
            continue
        for n, s in sorted(states.items()):
            if not s or s['state'] == 'OPEN':  # GitHub's open list lagged, or the PR is gone: next sweep
                counts['refresh_still_open' if s else 'refresh_missing'] += 1
                continue
            if (pr_state.now() - t0).total_seconds() > cfg['sweep_budget_s'] / 2:  # half for the open PRs
                counts['refresh_left_for_next'] += 1
                continue
            key = f'{repo}#{n}'
            counts[f"refreshed_{s['state'].lower()}"] += 1
            if dry:
                continue
            old, new = seen[key], {'state': s['state'], 'merged': s.get('mergedAt'), 'closed_at': s.get('closedAt')}
            if beads.enabled and old.get('bead') and not terminal(old):  # no bead is made for an entry without one
                try:  # gcs-ilx: count the closes made, apart from the beads a review's delivery had closed already
                    rig = cfg['repos'][repo]
                    if beads.status(rig, old['bead']) == 'closed':
                        counts['bead_already_closed'] += 1
                    else:
                        beads._run(rig, 'close', old['bead'], '--reason', old.get('outcome') or 'done')
                        counts['bead_closed_ended'] += 1
                except (RuntimeError, OSError, ValueError):
                    counts['bead_error'] += 1
                    continue  # the state stays OPEN, so the next sweep tries again
            with pr_state.locked(cfg['state']) as st:  # only these fields: other orders write the entry too
                cur = st['prs'].get(key)
                if cur is not None and cur.get('status') == 'done':
                    cur.update(new)


def pr_states(repo, numbers, batch=50):
    """{number: {state, mergedAt, closedAt} or None} for many PRs, one GraphQL query per `batch` PRs."""
    owner, name = repo.split('/')
    out = {}
    for i in range(0, len(numbers), batch):
        part = numbers[i:i + batch]
        q = ('query($o: String!, $r: String!) { repository(owner: $o, name: $r) {'
             + ' '.join(f'p{n}: pullRequest(number: {int(n)}) {{ state mergedAt closedAt }}' for n in part) + ' } }')
        r = pr_rules.graphql(q, o=owner, r=name)['repository']
        out.update({n: r.get(f'p{n}') for n in part})
    return out


def answers(cfg, repo, pr, old, since):
    """The answer trigger (decision-57b6d9bb), zero tokens: on a head we reviewed and a PR updated since the
    last sweep, what its author did after our first review of the head. {do, key, act} or None."""
    head = pr['headRefOid']
    if head not in (old.get('review_heads') or []) or pr_state.parse(pr['updatedAt']) < since:
        return None
    try:
        act = pr_rules.answer_activity(pr_rules.activity(repo, pr['number']), cfg['reviewer'],
                                       old.get('head_reviewed_at') or old.get('delivered_at'),
                                       old.get('head_review_ids') or [old.get('review_id')])
    except pr_rules.GhError:
        return None
    b = pr_rules.answer_batch(old, head, act, cfg['followup_rounds'])
    return dict(b, act=act) if b['do'] else None


def closed_at(repo, number):
    """The PR's merge or close time, for prune_posted; None while it is open or when GitHub does not answer."""
    try:
        pr = pr_rules.get_pr(repo, number)
    except pr_rules.GhError:
        return None
    t = pr and pr['state'] != 'OPEN' and (pr.get('mergedAt') or pr.get('closedAt'))
    return pr_state.parse(t) if t else None


# What a PR's entry keeps across new heads: the delivered review, for re-reviews, the feedback order and
# the formal block (before 2026-10-30 a new head dropped them); the client channel's posts per head, so a
# head's line goes out once.
CARRY = ('review_id', 'review_url', 'review_heads', 'event', 'approved', 'block', 'rc_live', 'slack', 'slack_ts',
         'delivered_at', 'inline', 'body_sha', 'judge', 'judge_at', 'judge_review', 'judge_below_bar', 'judge_risky',
         'client_posts', 'confidence', 'flagged_total', 'open_changes', 'open_questions',
         'head_reviewed_at', 'head_review_ids', 'answers_key', 'answer_rounds', 'answer_capped', 'asked_at', 'asked_rounds')


def lift_block(repo, pr, review_id):
    """Dismiss our formal block once the merge conflict and the failing checks it rested on are fixed."""
    msg = f"Lifted: the merge conflict and failing checks are fixed on {pr['headRefOid'][:7]}."
    r = subprocess.run(['gh', 'api', '-X', 'PUT', f"repos/{pr_rules.rest(repo)}/pulls/{pr['number']}/reviews/{int(review_id)}/dismissals",
                        '-f', f'message={msg}', '-f', 'event=DISMISS'], capture_output=True, text=True, timeout=60)
    return r.returncode == 0


TERMINAL = ('no-rule', 'closed', 'unrequested')  # sweep outcomes that end a PR's bead; a review's outcome closes it at delivery
ENDED = ('MERGED', 'CLOSED')  # gcs-pw4: and any done PR's bead ends with its PR


def terminal(entry):
    return entry.get('status') == 'done' and (entry.get('outcome') in TERMINAL or entry.get('state') in ENDED)


def record_one(cfg, beads, key, entry, old, counts):
    """The PR's bead and its state entry, written at once, so a sweep cut short keeps what it did."""
    outcome = entry.get('outcome') or entry['status']
    try:
        if not entry.get('bead'):
            entry['bead'] = beads.create(entry['rig'], entry['repo'], entry['number'], entry['url'],
                                         entry['head'], outcome)
            if terminal(entry):
                beads.set(entry['rig'], entry['bead'], close=True, outcome=outcome)
        elif entry['head'] != old.get('head') or outcome != old.get('outcome') or terminal(entry) and not terminal(old):
            # An existing bead closes too when its outcome turns terminal (most PRs merge before a review), or
            # when its PR merges or closes (gcs-pw4).
            beads.set(entry['rig'], entry['bead'], head=entry['head'], outcome=outcome, close=terminal(entry))
    except (RuntimeError, OSError) as e:
        counts['bead_error'] += 1
        entry['bead_error'] = str(e)[:200]
    with pr_state.locked(cfg['state']) as st:
        cur = st['prs'].get(key, {})
        if cur.get('status') == 'reviewing':  # the review order took it meanwhile; leave it be
            return
        st['prs'][key] = entry


def close_terminal(cfg, apply, beads=None):
    """One-off (2026-10-30): close the open pr-review beads of terminal PRs (before the fix the sweep never
    closed an existing bead), with the outcome as the reason, and a second bead of one PR (the state names
    the other one). From each rig's store, so a bead the state lost is seen too. Zero tokens; counts only."""
    beads = beads or pr_state.Beads(cfg['city'], enabled=cfg['beads'])
    with pr_state.locked(cfg['state']) as st:
        entries = {k: dict(v) for k, v in st['prs'].items()}
    counts = collections.Counter()
    for rig in sorted(set(cfg['repos'].values())):
        try:
            found = json.loads(beads._run(rig, 'list', '--label', 'pr-review', '--status', 'open', '--json',
                                          '--limit', '0') or '[]')
        except (RuntimeError, OSError, ValueError):
            counts['list_error'] += 1
            continue
        for b in found:
            m = b.get('metadata') or {}
            e = entries.get(f"{m.get('pr_repo')}#{m.get('pr_number')}") or {}
            if e.get('bead') and e['bead'] != b['id']:
                kind, reason = 'duplicate', f"duplicate of {e['bead']}"
            elif terminal(e):
                kind, reason = 'terminal', e['outcome']
            else:
                counts[f"left_open_{e.get('status') or 'not_in_state'}"] += 1
                continue
            counts[kind] += 1
            if apply:
                try:
                    beads.close(rig, b['id'], reason)
                    counts['closed_now'] += 1
                except (RuntimeError, OSError, ValueError):
                    counts['bead_error'] += 1
    print(json.dumps({'close_terminal': 'applied' if apply else 'dry run', 'counts': counts}))
    return 1 if counts['bead_error'] or counts['list_error'] else 0


class FakeBeads(pr_state.Beads):
    """A store in memory: the real Beads logic over a fake `gc bd`."""

    def __init__(self):
        super().__init__('.')
        self.store, self.calls = {}, []

    def _run(self, rig, *args):
        self.calls.append(args[:2])
        if args[0] == 'create':
            bead = f'fk-{len(self.store) + 1}'
            self.store[bead] = {'status': 'open', 'metadata': json.loads(args[args.index('--metadata') + 1])}
            return bead
        if args[0] == 'show':
            return json.dumps([{'id': args[1], 'status': self.store[args[1]]['status']}])
        if args[0] == 'list':
            return json.dumps([{'id': k, **v} for k, v in self.store.items() if v['status'] == 'open'])
        if args[0] == 'close':
            self.store[args[1]]['status'] = 'closed'
        return ''


def selftest():
    """The bead closes when the outcome is terminal, for a new bead and an existing one, and only once; the
    one-off closes terminal and second beads and leaves the rest open."""
    import tempfile
    fails = []

    def status(b, bead):
        return b.store.get(bead, {}).get('status')
    with tempfile.TemporaryDirectory() as d:
        cfg = {'state': d}
        base = {'repo': 'o/r', 'number': 1, 'url': 'u', 'rig': 'rig', 'head': 'h1'}
        b = FakeBeads()
        new = dict(base, status='done', outcome='no-rule')
        record_one(cfg, b, 'o/r#1', new, {}, collections.Counter())
        if status(b, new['bead']) != 'closed':
            fails.append('a new bead with a terminal outcome is closed')
        b = FakeBeads()
        queued = dict(base, status='queued')
        record_one(cfg, b, 'o/r#2', queued, {}, collections.Counter())
        if status(b, queued['bead']) != 'open':
            fails.append('a queued PR keeps its bead open')
        record_one(cfg, b, 'o/r#2', dict(queued, head='h2'), queued, collections.Counter())
        if status(b, queued['bead']) != 'open':
            fails.append('a new head on a queued PR keeps its bead open')
        gone = dict(queued, status='done', outcome='closed')
        record_one(cfg, b, 'o/r#2', gone, queued, collections.Counter())
        if status(b, queued['bead']) != 'closed':
            fails.append('an existing bead closes when its outcome turns terminal')
        record_one(cfg, b, 'o/r#2', dict(gone, head='h3'), gone, collections.Counter())
        if sum(1 for c in b.calls if c[0] == 'close') != 1:
            fails.append('a closed bead is not closed again')
    with tempfile.TemporaryDirectory() as d:
        cfg = {'state': d, 'repos': {'o/r': 'rig'}}
        b = FakeBeads()

        def meta(n):
            return {'pr_repo': 'o/r', 'pr_number': str(n)}
        b.store = {'fk-1': {'status': 'open', 'metadata': meta(1)}, 'fk-2': {'status': 'open', 'metadata': meta(2)},
                   'fk-3': {'status': 'closed', 'metadata': meta(2)}, 'fk-4': {'status': 'open', 'metadata': meta(3)}}
        with pr_state.locked(d) as st:
            st['prs'] = {'o/r#1': {'status': 'done', 'outcome': 'closed', 'bead': 'fk-1'},
                         'o/r#2': {'status': 'done', 'outcome': 'no-rule', 'bead': 'fk-3'},
                         'o/r#3': {'status': 'queued', 'bead': 'fk-4'}}
        with contextlib.redirect_stdout(io.StringIO()):
            close_terminal(cfg, False, b)
            if [k for k, v in b.store.items() if v['status'] == 'open'] != ['fk-1', 'fk-2', 'fk-4']:
                fails.append('the one-off writes nothing without --apply')
            close_terminal(cfg, True, b)
        if [k for k, v in b.store.items() if v['status'] == 'open'] != ['fk-4']:
            fails.append('the one-off closes the terminal bead and the second bead, and leaves a queued one open')
    fails += selftest_followups()
    fails += selftest_refresh()
    for f in fails:
        print(f'pr_sweep selftest: FAIL: {f}')
    print(f'pr_sweep selftest: {30 - len(fails)} of 30')
    return 1 if fails else 0


def selftest_refresh():
    """gcs-pw4: a done PR that left the open list gets its merged or closed state and its bead closes; a PR
    still open, a PR under review, an open one and a bead already closed are left be; a dry run writes
    nothing; a bead error leaves the state for the next sweep; the one-off closes a merged PR's bead."""
    import tempfile
    fails, g = [], globals()
    real_states = g['pr_states']
    gh = {1: 'MERGED', 2: 'OPEN', 3: 'MERGED', 4: 'CLOSED', 6: 'MERGED', 7: None, 8: 'MERGED'}
    asked = []

    def fake_states(repo, numbers):
        asked.extend(numbers)
        return {n: gh[n] and {'state': gh[n], 'mergedAt': '2026-11-06T10:00:00Z' if gh[n] == 'MERGED' else None,
                              'closedAt': '2026-11-06T10:00:00Z'} for n in numbers}

    class BrokenBeads(FakeBeads):
        def _run(self, rig, *args):
            if args[0] == 'close':
                raise RuntimeError('close failed')
            return super()._run(rig, *args)

    def setup(d, b):
        b.store = {f'fk-{n}': {'status': 'closed' if n in (6, 8) else 'open', 'metadata': {}} for n in (1, 2, 3, 5, 6, 7, 8)}
        done = {'status': 'done', 'outcome': 'low-risk', 'state': 'OPEN', 'head': 'h', 'rig': 'rig', 'repo': 'o/r'}
        with pr_state.locked(d) as st:
            st['prs'] = {'o/r#1': dict(done, bead='fk-1'), 'o/r#2': dict(done, bead='fk-2', outcome='approved'),
                         'o/r#3': dict(done, bead='fk-3', status='reviewing'), 'o/r#4': dict(done, outcome='questions'),
                         'o/r#5': dict(done, bead='fk-5'), 'o/r#6': dict(done, bead='fk-6', outcome='closed'),
                         'o/r#7': dict(done, bead='fk-7'), 'o/r#8': dict(done, bead='fk-8')}
            seen = {k: dict(v) for k, v in st['prs'].items()}
            st['prs']['o/r#1']['fb_written'] = 1  # another order wrote the entry after the sweep read it
        return seen

    def opened(b):
        return sorted(k for k, v in b.store.items() if v['status'] == 'open')
    try:
        g['pr_states'] = fake_states
        with tempfile.TemporaryDirectory() as d:
            cfg = {'state': d, 'repos': {'o/r': 'rig'}, 'sweep_budget_s': 60}
            b = FakeBeads()
            seen = setup(d, b)
            refresh_ended(cfg, b, seen, {'o/r#5'}, collections.Counter(), True, pr_state.now())
            with pr_state.locked(d) as st:
                dry = {k: v.get('state') for k, v in st['prs'].items()}
            if set(dry.values()) != {'OPEN'} or opened(b) != ['fk-1', 'fk-2', 'fk-3', 'fk-5', 'fk-7']:
                fails.append('gcs-pw4: a dry run closes no bead and writes no state')
            asked.clear()
            counts = collections.Counter()
            refresh_ended(cfg, b, seen, {'o/r#5'}, counts, False, pr_state.now())
            with pr_state.locked(d) as st:
                after = {k: dict(v) for k, v in st['prs'].items()}
            p1 = after['o/r#1']
            if not (p1['state'] == 'MERGED' and p1.get('merged') and p1.get('fb_written') == 1 and
                    b.store['fk-1']['status'] == 'closed' and p1['outcome'] == 'low-risk'):
                fails.append('gcs-pw4: a reviewed PR that merged gets MERGED, its bead closes, its outcome and '
                             "another order's fields stay")
            if not (after['o/r#4']['state'] == 'CLOSED' and len(b.store) == 7 and after['o/r#6']['state'] == 'MERGED'
                    and sum(1 for c in b.calls if c[0] == 'close') == 1):
                fails.append('gcs-pw4: an entry with no bead gets no bead, a bead already closed is not closed again')
            if not (opened(b) == ['fk-2', 'fk-3', 'fk-5', 'fk-7'] and after['o/r#2']['state'] == 'OPEN' and
                    after['o/r#3']['state'] == 'OPEN' and after['o/r#7']['state'] == 'OPEN' and sorted(asked) == [1, 2, 4, 6, 7, 8]
                    and counts['refresh_still_open'] == 1 and counts['refresh_missing'] == 1 and counts['bead_closed_ended'] == 1
                    and counts['bead_already_closed'] == 1):
                fails.append('gcs-pw4: a PR GitHub still has open, a PR under review, an open PR and a PR GitHub '
                             'does not return are left be')
            b2 = BrokenBeads()
            seen = setup(d, b2)
            counts = collections.Counter()
            refresh_ended(cfg, b2, seen, {'o/r#5'}, counts, False, pr_state.now())
            with pr_state.locked(d) as st:
                if st['prs']['o/r#1']['state'] != 'OPEN' or not counts['bead_error']:
                    fails.append('gcs-pw4: a bead that fails to close keeps the state OPEN for the next sweep')
            b3 = FakeBeads()
            seen = setup(d, b3)
            counts = collections.Counter()
            refresh_ended(cfg, b3, seen, {'o/r#5'}, counts, False, pr_state.now() - dt.timedelta(seconds=31))
            if counts['refresh_left_for_next'] != 4:
                fails.append('gcs-pw4: past half the budget the refresh leaves the rest for the next sweep')
        with tempfile.TemporaryDirectory() as d:
            cfg = {'state': d, 'repos': {'o/r': 'rig'}}
            b = FakeBeads()
            b.store = {'fk-1': {'status': 'open', 'metadata': {'pr_repo': 'o/r', 'pr_number': '1'}},
                       'fk-2': {'status': 'open', 'metadata': {'pr_repo': 'o/r', 'pr_number': '2'}}}
            with pr_state.locked(d) as st:
                st['prs'] = {'o/r#1': {'status': 'done', 'outcome': 'low-risk', 'state': 'MERGED', 'bead': 'fk-1'},
                             'o/r#2': {'status': 'done', 'outcome': 'low-risk', 'state': 'OPEN', 'bead': 'fk-2'}}
            with contextlib.redirect_stdout(io.StringIO()):
                close_terminal(cfg, True, b)
            if opened(b) != ['fk-2']:
                fails.append("gcs-pw4: the one-off closes a merged PR's bead and leaves an open PR's")
        b = FakeBeads()
        with tempfile.TemporaryDirectory() as d:
            e = {'repo': 'o/r', 'number': 9, 'url': 'u', 'rig': 'rig', 'head': 'h', 'status': 'done',
                 'outcome': 'questions', 'state': 'OPEN'}
            record_one({'state': d}, b, 'o/r#9', e, {}, collections.Counter())
            record_one({'state': d}, b, 'o/r#9', dict(e, state='MERGED'), e, collections.Counter())
            if b.store[e['bead']]['status'] != 'closed':
                fails.append('gcs-pw4: an existing bead closes when the sweep records its PR merged')
    finally:
        g['pr_states'] = real_states
    return fails


def selftest_followups():
    """decision-57b6d9bb, the whole sweep with GitHub stubbed: a push on a reviewed PR waits for its CI, then
    is queued as a push follow-up; the author's reply on a reviewed head queues a same-head follow-up first
    in line; with PR_REVIEW_QUEUE=requested an unrequested new PR stays out and a requested one enters."""
    import tempfile
    a40, b40, t = 'a' * 40, 'b' * 40, pr_state.now()
    now = pr_state.iso(t)
    ago = lambda h: pr_state.iso(t - dt.timedelta(hours=h))  # noqa: E731
    prs = {1: {'head': b40, 'ci': 'pending'}, 2: {'head': a40, 'ci': 'green'}, 3: {'head': a40, 'ci': 'green'},
           4: {'head': a40, 'ci': 'green', 'requested': True},
           5: {'head': a40, 'ci': 'green', 'requested': True, 'branch': 'review-test/c1'},  # gcs-71c: out of draft
           6: {'head': a40, 'ci': 'green', 'requested': True, 'draft': True},
           # gcs-b3f: asked again after our review of the same head (7), after a held attempt (8), before our
           # last review (9: a hand re-review answered it), with the head's rounds used up (10), while CI runs (11)
           7: {'head': a40, 'ci': 'green', 'requested': True, 'asked': ago(14)},
           8: {'head': a40, 'ci': 'green', 'requested': True, 'asked': ago(14)},
           9: {'head': a40, 'ci': 'green', 'requested': True, 'asked': ago(14)},
           10: {'head': a40, 'ci': 'green', 'requested': True, 'asked': ago(14)},
           11: {'head': a40, 'ci': 'pending', 'requested': True, 'asked': ago(1)},  # its CI re-runs
           # gcs-g9e: a draft that does not request the reviewer (12), a red team's test draft that does (13)
           12: {'head': a40, 'ci': 'green', 'draft': True},
           13: {'head': a40, 'ci': 'green', 'requested': True, 'draft': True, 'branch': 'review-test/c2'},
           # decision-527cf053: the reviewer approved (14: a push follow-up was queued; 15: a PR we never saw)
           14: {'head': b40, 'ci': 'green', 'requested': True, 'approved': True},
           15: {'head': a40, 'ci': 'green', 'requested': True, 'approved': True}}

    def fake(n):
        p = prs[n]
        return {'number': n, 'url': f'https://github.com/o/r/pull/{n}', 'state': 'OPEN', 'isDraft': bool(p.get('draft')),
                'headRefOid': p['head'], 'updatedAt': now, 'mergeable': 'MERGEABLE', 'createdAt': now, 'mergedAt': None,
                'baseRefName': 'main', 'headRefName': p.get('branch') or f'topic-{n}',
                'timelineItems': {'nodes': [{'createdAt': p['asked'], 'requestedReviewer': {'login': 'Rev'}}]
                                  if p.get('asked') else []},
                'reviews': {'nodes': [{'author': {'login': 'rev'}, 'state': 'CHANGES_REQUESTED'},
                                      {'author': {'login': 'Rev'}, 'state': 'APPROVED'},
                                      {'author': {'login': 'rev'}, 'state': 'COMMENTED'}] if p.get('approved') else []}}

    def fake_record(cfg, repo, pr, files, ruleset, area_of, heads):
        p = prs[pr['number']]
        hits = ['requested', 'r1'] if p.get('requested') else ['r1']
        return {'head': p['head'], 'state': 'OPEN', 'created': now, 'merged': None, 'hits': hits,
                'tier': 0 if p.get('requested') else 2, 'areas': ['x'], 'ci': p['ci']}
    act = {'author': {'login': 'auth'}, 'reviewThreads': {'nodes': [{'id': 'T1', 'isResolved': False, 'comments': {
        'nodes': [{'databaseId': 1, 'author': {'login': 'rev'}, 'createdAt': pr_state.iso(t - dt.timedelta(hours=1)),
                   'pullRequestReview': {'databaseId': 77}},
                  {'databaseId': 501, 'author': {'login': 'auth'}, 'createdAt': now}]}}]}}
    stubs = {(pr_rules, 'load_rules'): lambda p: {}, (pr_rules, 'areas'): lambda r: {},
             (pr_rules, 'open_prs'): lambda repo: ([fake(n) for n in prs], {'main': 'c' * 40}),
             (pr_rules, 'pr_files'): lambda repo, n: [], (pr_rules, 'ci_state'): lambda pr: prs[pr['number']]['ci'],
             (pr_rules, 'requested_now'): lambda pr, who: bool(prs[pr['number']].get('requested')),
             (pr_rules, 'get_pr'): lambda repo, n: fake(n), (pr_rules, 'activity'): lambda repo, n: act if n == 2 else {'author': {'login': 'auth'}},
             (pr_state, 'Beads'): lambda *a, **k: FakeBeads(), (pr_state, 'prune_private'): lambda *a: {}}
    kept = {k: getattr(*k) for k in stubs}
    g = globals()
    real_record, real_states, fails = g['record'], g['pr_states'], []
    reviewed = {'repo': 'o/r', 'rig': 'rig', 'status': 'done', 'outcome': 'questions', 'review_heads': [a40], 'head': a40,
                'review_id': 77, 'head_review_ids': [77], 'head_reviewed_at': pr_state.iso(t - dt.timedelta(hours=1)),
                'seen_at': pr_state.iso(t - dt.timedelta(days=1)), 'created': now, 'tier': 2, 'rules': ['r1']}
    try:
        for (m, n), f in stubs.items():
            setattr(m, n, f)
        g['record'] = fake_record
        g['pr_states'] = lambda repo, numbers: {n: {'state': 'MERGED', 'mergedAt': now, 'closedAt': now} for n in numbers}
        with tempfile.TemporaryDirectory() as d:
            cfg = {'state': d, 'city': d, 'beads': False, 'reviewer': 'rev', 'repos': {'o/r': 'rig'}, 'rules': '',
                   'lookback_h': 2, 'ci_wait_h': 3, 'sweep_budget_s': 60, 'followups': True, 'followup_rounds': 3,
                   'queue': 'requested', 'work': d, 'keep_days': 30}
            delivered = dict(reviewed, delivered_at=ago(70), head_reviewed_at=ago(70), rules=['requested', 'r1'])
            with pr_state.locked(d) as st:
                st['last_sweep'] = pr_state.iso(t - dt.timedelta(minutes=30))
                st['prs'] = {'o/r#1': dict(reviewed, number=1, url='https://github.com/o/r/pull/1'),
                             'o/r#2': dict(reviewed, number=2, url='https://github.com/o/r/pull/2'),
                             'o/r#7': dict(delivered, number=7, url='https://github.com/o/r/pull/7'),
                             'o/r#8': dict(delivered, number=8, url='https://github.com/o/r/pull/8', outcome='held-gate'),
                             'o/r#9': dict(delivered, number=9, url='https://github.com/o/r/pull/9', delivered_at=ago(1)),
                             'o/r#10': dict(delivered, number=10, url='https://github.com/o/r/pull/10',
                                            asked_rounds={a40: 3}),
                             'o/r#11': dict(delivered, number=11, url='https://github.com/o/r/pull/11', head_seen_at=ago(2),
                                             delivered_at=ago(1.5), head_reviewed_at=ago(1.5)),
                             'o/r#14': dict(reviewed, number=14, url='https://github.com/o/r/pull/14', status='queued',
                                            queued_at=ago(0.2), followup='push'),
                             # gcs-pw4: reviewed, then merged: it is not in the open list any more
                             'o/r#20': dict(reviewed, number=20, url='https://github.com/o/r/pull/20', state='OPEN')}
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                sweep(cfg, False)
                with pr_state.locked(d) as st:
                    first = {k: dict(v) for k, v in st['prs'].items()}
                prs[1]['ci'] = prs[11]['ci'] = 'green'
                sweep(cfg, False)
                with pr_state.locked(d) as st:
                    second = {k: dict(v) for k, v in st['prs'].items()}
                    # the re-review ran and posted (7), or ran and was held (8 asked again, the same request)
                    st['prs']['o/r#7'].update(status='done', outcome='questions', delivered_at=ago(0.5))
                    st['prs']['o/r#7'].pop('followup', None)
                    st['prs']['o/r#8'] = dict(second['o/r#7'], number=8, url='https://github.com/o/r/pull/8',
                                              status='done', outcome='held-gate')
                    st['prs']['o/r#8'].pop('followup', None)
                sweep(cfg, False)
                with pr_state.locked(d) as st:
                    third = {k: dict(v) for k, v in st['prs'].items()}
            counts = [json.loads(x)['counts'] for x in out.getvalue().splitlines() if x.startswith('{"sweep"')]
    finally:
        for (m, n), f in kept.items():
            setattr(m, n, f)
        g['record'] = real_record
        g['pr_states'] = real_states
    if first['o/r#20'].get('state') != 'MERGED' or first['o/r#20'].get('outcome') != 'questions' or \
            not counts or counts[0].get('refreshed_merged') != 1:
        fails.append('gcs-pw4: the sweep records a reviewed PR that left the open list as merged, outcome kept')
    p1, p1b, p2 = first['o/r#1'], second['o/r#1'], first['o/r#2']
    if not (p1.get('status') == 'waiting-ci' and p1.get('followup') == 'push' and p1b.get('status') == 'queued'
            and p1b.get('followup') == 'push'):
        fails.append('a push on a reviewed PR waits for its CI, then is queued as a push follow-up')
    if not (p2.get('status') == 'queued' and p2.get('followup') == 'answer' and p2.get('answers_pending') == '501'
            and p2.get('tier') == 0):
        fails.append("the author's reply queues a same-head follow-up, first in line")
    if (first.get('o/r#3') or {}).get('outcome') != 'unrequested' or (first.get('o/r#3') or {}).get('status') != 'done':
        fails.append('PR_REVIEW_QUEUE=requested keeps an unrequested new PR out of the queue')
    if (first.get('o/r#4') or {}).get('status') != 'queued':
        fails.append('a PR that requests the reviewer enters the queue')
    if second['o/r#2'].get('followup') != 'answer' or second['o/r#2'].get('status') != 'queued':
        fails.append('a queued follow-up is not queued twice or lost by the next sweep')
    if any(k in second for k in ('o/r#5', 'o/r#12', 'o/r#13')):
        fails.append('an unrequested draft, and an eval PR in draft or not, stay out of the queue and get no bead (gcs-71c)')
    if (first.get('o/r#6') or {}).get('status') != 'queued' or (first.get('o/r#6') or {}).get('tier') != 0:
        fails.append('gcs-g9e: a draft that requests the reviewer enters the queue like any requested PR')
    p7, p7b = first['o/r#7'], second['o/r#7']
    if not (p7.get('status') == 'queued' and p7.get('followup') == 'asked' and p7.get('tier') == 0 and
            p7.get('asked_at') == prs[7]['asked'] and p7.get('asked_rounds') == {a40: 1}):
        fails.append('gcs-b3f: a request made again after our review of the same head queues a re-review, first in line')
    if not (p7b.get('status') == 'queued' and p7b.get('followup') == 'asked' and p7b.get('asked_rounds') == {a40: 1}):
        fails.append('gcs-b3f: a queued re-review keeps its mark and is counted once')
    if any(first[k].get('status') != 'done' or first[k].get('followup') for k in ('o/r#8', 'o/r#9', 'o/r#10')):
        fails.append('gcs-b3f: no re-review after a held attempt, for a request our last review answered, or past the rounds')
    if any(third[k].get('status') != 'done' or third[k].get('followup') for k in ('o/r#7', 'o/r#8')):
        fails.append('gcs-b3f: one request is one re-review, whether it posted or was held')
    p11, p11b = first['o/r#11'], second['o/r#11']
    if not (p11.get('status') == 'waiting-ci' and p11.get('followup') == 'asked' and p11b.get('status') == 'queued'
            and p11b.get('followup') == 'asked' and p11b.get('asked_rounds') == {a40: 1}):
        fails.append('gcs-b3f: a re-review waits for CI with its mark, then is queued as one')
    if (first.get('o/r#14') or {}).get('status') != 'done' or (first.get('o/r#14') or {}).get('followup') or \
            'o/r#15' in third or not counts or counts[0].get('approved') != 2:
        fails.append('decision-527cf053: an approved PR leaves the queue and is never queued (a comment after it changes nothing)')
    if not counts or counts[0].get('asked_again') != 2 or counts[0].get('asked_capped') != 1 or \
            counts[0].get('draft_requested') != 2 or counts[0].get('draft') != 3:
        fails.append('gcs-b3f: the sweep counts re-reviews, used-up rounds and drafts that request the reviewer')
    return fails


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--backtest', type=float, metavar='DAYS')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--cache', metavar='DIR', help="backtest: keep GitHub's answers here and reuse them")
    ap.add_argument('--close-terminal', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        sys.exit(selftest())
    cfg = pr_state.config()
    if a.close_terminal:
        sys.exit(close_terminal(cfg, a.apply))
    if not cfg['repos'] or not cfg['reviewer']:
        sys.exit('PR_REVIEW_REPOS and PR_REVIEW_REVIEWER must be set')
    if a.backtest:
        ruleset, area_of, since, now, records = backtest(cfg, a.backtest, a.workers, a.cache)
        os.makedirs(cfg['state'], exist_ok=True)
        path = os.path.join(cfg['state'], f'backtest-{now.strftime("%Y%m%dT%H%M%SZ")}.json')
        with open(path, 'w') as f:
            json.dump({'generated': now.isoformat(), 'records': records}, f, indent=1)
        summary = summarize(cfg, ruleset, area_of, since, now, records)
        summary['records_file'] = path
        print(json.dumps(summary, indent=1))
        return
    if not cfg['on'] and not a.dry_run:
        print(json.dumps({'skipped': 'PR_REVIEW is off'}))
        return
    load = pr_rules.load_avg()
    if load > cfg['max_load']:
        pr_state.log(cfg['state'], {'run': 'sweep', 'skipped': 'load', 'load': load})
        print(json.dumps({'skipped': 'load', 'load': load}))
        return
    sweep(cfg, a.dry_run)


if __name__ == '__main__':
    main()
