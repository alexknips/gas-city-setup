#!/usr/bin/env python3
"""PR review sweep (README.md, "Sweep"): which PRs need a review, from zero-token rules only.

  pr_sweep.py                   one sweep: PRs updated since the last one (open non-draft, or merged
                                since), each new head through the rules, one bead per PR, the review
                                queue in the state file. No model, nothing posted.
  pr_sweep.py --dry-run         the same, printed; writes nothing, creates nothing
  pr_sweep.py --backtest DAYS   every PR opened in the last DAYS days, any state, through the rules:
                                how many each rule flags and what the daily cap would reach
                                (calibration; writes one record file under the state dir)

Config: environment, see pr_state.config() and README.md, "Configuration".
"""
import argparse
import collections
import concurrent.futures as cf
import datetime as dt
import json
import os
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
    partial = False
    for key, (repo, pr, heads) in sorted(found.items()):
        if (pr_state.now() - t0).total_seconds() > cfg['sweep_budget_s']:
            # Out of time: what is recorded stays recorded, and last_sweep does not move, so the next
            # sweep searches the same window again and skips the heads already seen.
            partial = True
            counts['left_for_next'] += 1
            continue
        old = seen.get(key, {})
        if pr['isDraft'] and pr['state'] == 'OPEN':
            counts['draft'] += 1
            continue
        again = pr_rules.asked_again(old, pr['headRefOid'], pr_rules.requested_now(pr, cfg['reviewer']))
        if old.get('head') == pr['headRefOid'] and old.get('status') != 'waiting-ci' and not again:
            counts['seen'] += 1
            continue
        files = pr_rules.pr_files(repo, pr['number'])
        rec = record(cfg, repo, pr, files, ruleset, area_of, heads)
        entry = {'repo': repo, 'number': pr['number'], 'url': pr['url'], 'rig': cfg['repos'][repo],
                 'head': rec['head'], 'state': rec['state'], 'created': rec['created'], 'merged': rec['merged'],
                 'rules': sorted(rec['hits']), 'tier': rec['tier'], 'areas': rec['areas'], 'ci': rec['ci'],
                 'seen_at': old.get('seen_at') or pr_state.iso(t0), 'bead': old.get('bead')}
        ci_wait = pr_state.parse(entry['seen_at']) + dt.timedelta(hours=cfg['ci_wait_h'])
        if rec['ci'] == 'pending' and t0 < ci_wait:
            entry['status'] = 'waiting-ci'
        elif old.get('status') in ('done', 'reviewing') and not again and not (
                pr_rules.SERIOUS & (set(rec['hits']) - set(old.get('rules', [])))):
            # Pushed again after a review: reviewed once unless the reviewer is asked again or the new head
            # trips a serious rule the reviewed head did not.
            entry.update(status=old['status'], outcome=old.get('outcome'), head=old['head'], new_head=rec['head'])
        elif rec['tier'] is None:
            entry.update(status='done', outcome='no-rule')
        else:
            entry.update(status='queued', queued_at=pr_state.iso(t0))
        counts[entry['status'] if entry['status'] != 'done' else entry.get('outcome', 'done')] += 1
        if dry:
            print(json.dumps({'pr': entry['url'], 'status': entry['status'], 'outcome': entry.get('outcome'),
                              'tier': entry['tier'], 'rules': entry['rules'], 'ci': entry['ci']}))
            continue
        record_one(cfg, beads, key, entry, old, counts)
    if dry:
        print(json.dumps({'dry_run': True, 'since': pr_state.iso(since), 'counts': counts}))
        return
    if not partial:
        with pr_state.locked(cfg['state']) as st:
            st['last_sweep'] = pr_state.iso(t0)
    pr_state.log(cfg['state'], {'run': 'sweep', 'since': pr_state.iso(since), 'counts': counts, 'partial': partial,
                                'secs': round((pr_state.now() - t0).total_seconds())})
    print(json.dumps({'sweep': pr_state.iso(t0), 'partial': partial, 'counts': counts}))


def record_one(cfg, beads, key, entry, old, counts):
    """The PR's bead and its state entry, written at once, so a sweep cut short keeps what it did."""
    terminal = entry['status'] == 'done' and entry.get('outcome') in ('no-rule', 'closed')
    outcome = entry.get('outcome') or entry['status']
    try:
        if not entry.get('bead'):
            entry['bead'] = beads.create(entry['rig'], entry['repo'], entry['number'], entry['url'],
                                         entry['head'], outcome)
            if terminal:
                beads.set(entry['rig'], entry['bead'], close=True, outcome=outcome)
        elif entry['head'] != old.get('head') or outcome != old.get('outcome'):
            beads.set(entry['rig'], entry['bead'], head=entry['head'], outcome=outcome)
    except (RuntimeError, OSError) as e:
        counts['bead_error'] += 1
        entry['bead_error'] = str(e)[:200]
    with pr_state.locked(cfg['state']) as st:
        cur = st['prs'].get(key, {})
        if cur.get('status') == 'reviewing':  # the review order took it meanwhile; leave it be
            return
        st['prs'][key] = entry


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--backtest', type=float, metavar='DAYS')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--cache', metavar='DIR', help="backtest: keep GitHub's answers here and reuse them")
    a = ap.parse_args()
    cfg = pr_state.config()
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
