#!/usr/bin/env python3
"""PR review sweep (README.md): which PRs need a review, from zero-token rules only.

  pr_sweep.py --backtest DAYS   apply the rules to every PR opened in the last DAYS days, in any state,
                                and print how many each rule flags (calibration; writes one record
                                file under the state dir, posts nothing, creates nothing)

Config (env): PR_REVIEW_REPOS ("owner/repo[=rig] ..."), PR_REVIEW_REVIEWER (GitHub login),
PR_REVIEW_RULES (rules file; default the pack's assets/rules.toml), PR_REVIEW_DAILY_CAP (10),
PR_REVIEW_STATE_DIR, PR_REVIEW_MAX_LOAD (15).
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

HERE = os.path.dirname(os.path.abspath(__file__))


def config():
    city = os.environ.get('GC_CITY_PATH') or os.environ.get('GC_CITY') or os.getcwd()
    repos = {}
    for item in os.environ.get('PR_REVIEW_REPOS', '').replace(',', ' ').split():
        repo, _, rig = item.partition('=')
        repos[repo] = rig or repo.split('/')[1]
    return {
        'repos': repos,
        'reviewer': os.environ.get('PR_REVIEW_REVIEWER', ''),
        'rules': os.environ.get('PR_REVIEW_RULES') or os.path.join(HERE, '..', 'rules.toml'),
        'cap': int(os.environ.get('PR_REVIEW_DAILY_CAP', '10')),
        'state': os.environ.get('PR_REVIEW_STATE_DIR') or os.path.join(city, '.gc', 'runtime', 'pr-review'),
        'max_load': float(os.environ.get('PR_REVIEW_MAX_LOAD', '15')),
    }


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


def backtest(cfg, days, workers, cache=None):
    """Every PR opened in the window, any state, through the rules. With `cache`, GitHub's answers are
    kept there (they hold the repositories' code: keep that dir out of the town's runtime and delete it
    after calibrating) and a re-run with the same window start reads them back."""
    ruleset = pr_rules.load_rules(cfg['rules'])
    area_of = pr_rules.areas(ruleset)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
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
            f'repo:{repo} is:pr created:>={since.strftime("%Y-%m-%dT%H:%M:%SZ")}'))
        main = cached(cache, f'{key}/main.json', lambda: pr_rules.branch_oid(repo, 'main'))

        def one(pr):
            files = cached(cache, f'{key}/files-{pr["number"]}-{pr["headRefOid"]}.json',
                           lambda: pr_rules.pr_files(repo, pr['number']))
            if pr['state'] == 'MERGED' and pr.get('mergeCommit'):
                parents = pr['mergeCommit']['parents']['nodes']
                base = parents[0]['oid'] if parents else None
            elif pr['state'] == 'OPEN':
                base = main if pr['baseRefName'] == 'main' else pr['baseRefOid']
            else:
                base = pr['baseRefOid']
            hits = pr_rules.evaluate(pr, files, ruleset, cfg['reviewer'], repo, base)
            return {
                'repo': repo, 'number': pr['number'], 'url': pr['url'], 'state': pr['state'],
                'draft': pr['isDraft'], 'created': pr['createdAt'], 'merged': pr['mergedAt'],
                'author': (pr.get('author') or {}).get('login'), 'head': pr['headRefOid'],
                'lines': pr['additions'] + pr['deletions'], 'files': len(files),
                'files_gh': pr['changedFiles'],
                'patch_missing': sum(1 for f in files if f.get('patch') is None and f['status'] != 'removed'),
                'ci': pr_rules.ci_state(pr), 'hits': hits,
                'selected': pr_rules.selected(hits, ruleset, area_of),
                'tier': pr_rules.priority(hits, ruleset, area_of),
                'areas': sorted({area_of[h] for h in hits if area_of.get(h) in ruleset['select_areas']}),
            }

        with cf.ThreadPoolExecutor(workers) as ex:
            records += list(ex.map(one, prs))
    return ruleset, area_of, since, now, records


def within_cap(sel, cap):
    """Per tier, how many selected PRs the daily cap would reach, filling each day in priority order
    (tier, then more areas, then oldest), as the sweep does."""
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
        out['repos'][repo] = {
            'prs': len(rs),
            'states': dict(collections.Counter(r['state'] + ('/draft' if r['draft'] else '') for r in rs)),
            'authors': len({r['author'] for r in rs}),
            'lines_median': lines[len(lines) // 2], 'lines_p90': lines[int(len(lines) * 0.9)],
            'ci': dict(collections.Counter(r['ci'] for r in rs)),
            'requested': sum(1 for r in rs if 'requested' in r['hits']),
            'selected': len(sel),
            'selected_by_ci_alone': sum(1 for r in sel if all(area_of.get(h) == 'A7' for h in r['hits']
                                                               if h == 'requested' or area_of.get(h) in ruleset['select_areas'])),
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


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--backtest', type=float, metavar='DAYS')
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--cache', metavar='DIR', help="backtest: keep GitHub's answers here and reuse them")
    a = ap.parse_args()
    cfg = config()
    if not cfg['repos'] or not cfg['reviewer']:
        sys.exit('PR_REVIEW_REPOS and PR_REVIEW_REVIEWER must be set')
    if pr_rules.load_avg() > cfg['max_load']:
        print(json.dumps({'skipped': 'load', 'load': pr_rules.load_avg()}))
        return
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
    ap.print_help()


if __name__ == '__main__':
    main()
