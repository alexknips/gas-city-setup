#!/usr/bin/env python3
"""Facts for the weekly review (the `quality` pack's quality-review order; README.md, "Weekly review").

No model and no tokens: this script reads the per-day history the quality-metrics order writes
(OUT_DIR/history/<day>.json), the city's git log and the beads open now, and writes
OUT_DIR/reviews/<week>.facts.json and <week>.facts.md for the review seat (formula
mol-quality-review). The reviewed week is the 7 local days before this week's review day; it is
compared with the 21 days before it, per week. The script, not the seat, checks whether the last
review's change was applied (git apply --check) and reads its metric now.

    quality_review.py --due              print the week a review is due for (exit 0), or why not (exit 1)
    quality_review.py --facts [--week W] write the facts for the due week (or week W, e.g. 2026-W43)
    quality_review.py --dry-run [--week W]  print the facts as Markdown; write nothing
    quality_review.py --index            rebuild OUT_DIR/reviews/index.json for the page
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as q  # noqa: E402  (same directory: one definition of every metric)

UTC = q.UTC
REVIEWS = os.path.join(q.OUT_DIR, "reviews")
WEEKDAY = int(q.env("QUALITY_REVIEW_WEEKDAY", "1"))  # ISO weekday of the review, 1 = Monday
HOUR = int(q.env("QUALITY_REVIEW_HOUR", "7"))        # local hour on that day from which it is due
BEFORE_DAYS = int(q.env("QUALITY_REVIEW_BEFORE_DAYS", "21"))
CONFIG_REPOS = q.env_list("QUALITY_REVIEW_CONFIG_REPOS") or [q.CITY]
STAMP = os.path.join(q.STATE_DIR, "review-last-week")
OPEN_STATUSES = q.env_list("QUALITY_REVIEW_OPEN_STATUSES", "open,in_progress,blocked,deferred")
TOP = 5


# ---------- the week ----------

def week_label(d):
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def review_window(now, week=None):
    """(label, reviewed days, days before, review day). The last reviewed day is the day before the
    review weekday (Sunday for a Monday review); the label is that day's ISO week."""
    if week:
        m = re.fullmatch(r"(\d{4})-W(\d{2})", week)
        if not m:
            sys.exit(f"quality-review: --week wants YYYY-Www, not {week!r}")
        last = date.fromisocalendar(int(m.group(1)), int(m.group(2)), (WEEKDAY - 2) % 7 + 1)
        review_day = last + timedelta(days=1)
    else:
        loc = now.astimezone(q.LOCAL)
        back = (loc.isoweekday() - WEEKDAY) % 7
        review_day = loc.date() - timedelta(days=back)
        if back == 0 and loc.hour < HOUR:
            review_day -= timedelta(days=7)
        last = review_day - timedelta(days=1)
    days = [(last - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    before = [(last - timedelta(days=i)).isoformat() for i in range(6 + BEFORE_DAYS, 6, -1)]
    return week_label(last), days, before, review_day


def read(path, default=None):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


# ---------- facts ----------

def load_days(days):
    recs, missing = {}, []
    for d in days:
        r = q.load_json(os.path.join(q.OUT_DIR, "history", f"{d}.json"), None)
        if r is None:
            missing.append(d)
        else:
            recs[d] = r
    return recs, missing


def windows(recs, days):
    """quality.summarize() over these days: per rig and for all rigs, from the history records."""
    have = [d for d in days if d in recs]
    rigs = sorted({n for d in have for n in recs[d]["rigs"]})
    per = {n: [recs[d]["rigs"][n] for d in have if n in recs[d]["rigs"]] for n in rigs}
    per["all"] = [q.merge_rig_days(list(recs[d]["rigs"].values())) for d in have]
    city = [recs[d] for d in have]
    return {n: q.summarize(rd, city if n == "all" else [], q.FOLLOW_DAYS, None) for n, rd in per.items()}, len(have)


def r2(v):
    return None if v is None else round(v, 2)


def catalog(m, scope, weeks):
    """The metrics a review may name, as '<scope>.<metric>' -> (value, unit, basis). Counts and
    hours are per week; rates and medians are pooled over the days."""
    out = {}

    def put(k, v, unit, basis):
        out[f"{scope}.{k}"] = {"value": r2(v), "unit": unit, "basis": basis}

    per = (lambda v: None if v is None or not weeks else v / weeks)
    put("landed", per(m["landed"]), "beads", "per week")
    for k in ("bounce_rate", "post_merge_failure_rate", "fix_of_a_fix_rate", "first_pass_ci_rate"):
        put(k, m[k], "%", "pooled")
    for k in ("created_claimed", "claimed_pr", "claimed_merged", "created_merged"):
        put(f"{k}.median_h", m["time_to_land"][k]["median"], "h", "pooled")
    for s, x in m["stages"]["stages"].items():
        put(f"stage.{s}.h", per(x["h"]), "h", "per week")
        put(f"stage.{s}.share", x["share"], "%", "pooled")
        put(f"stage.{s}.median_h", x["median_h"], "h", "pooled")
    put("friction", per(m["friction_total"]), "events", "per week")
    put("human.answer_median_h", m["human"]["answer_median_h"], "h", "pooled")
    if scope == "all":
        put("cost_per_landed", m["cost_per_landed"], "$", "pooled")
        put("cost_usd", m["cost_usd"] / (m["cost_days"] / 7) if m["cost_days"] else None, "$", "per week")
    return out


def stage_reasons(beads, stage, top=8):
    """Hours per reason (route target, blocker, hold text) in one stage, with the beads."""
    agg = defaultdict(lambda: {"h": 0.0, "beads": []})
    for b in beads:
        for why, h in ((b.get("stage_by") or {}).get(stage) or {}).items():
            a = agg[why]
            a["h"] += h
            a["beads"].append(b["id"])
    rows = sorted(agg.items(), key=lambda t: -t[1]["h"])[:top]
    return [{"why": w, "h": round(a["h"], 1), "n": len(a["beads"]), "beads": a["beads"][:6]} for w, a in rows]


def batches(beads, stages=("routed", "unrouted", "parked", "blocked"), min_beads=3, top=3):
    """Beads filed within the same clock hour (UTC) that then waited in one stage: a burst of work
    filed together can be most of a stage's hours while its median stays small."""
    out = []
    for st in stages:
        groups = defaultdict(list)
        for b in beads:
            created = (b.get("times") or {}).get("created")
            if b.get("stages") and b["stages"][st] > 0 and created:
                groups[created[:13]].append(b)
        rows = [(hr, bs, sum(x["stages"][st] for x in bs)) for hr, bs in groups.items() if len(bs) >= min_beads]
        for hr, bs, h in sorted(rows, key=lambda t: -t[2])[:top]:
            out.append({"stage": st, "filed": hr + ":00Z", "n": len(bs), "h": round(h, 1),
                        "median_h": q.quantile([x["stages"][st] for x in bs], 0.5),
                        "rigs": sorted({x.get("rig") for x in bs if x.get("rig")}), "beads": [x["id"] for x in bs][:8]})
    return sorted(out, key=lambda r: -r["h"])


def bead_rows(beads):
    """Every landed bead of the week, compact: enough to recompute any stage number by hand."""
    return [{"id": b["id"], "rig": b.get("rig"), "priority": b.get("priority"), "type": b.get("type"),
             "created": (b.get("times") or {}).get("created"), "landed": b.get("landed"), "bounces": b.get("bounces"),
             "stages": {k: round(v, 2) for k, v in (b.get("stages") or {}).items() if v},
             "why": {k: next(iter(v)) for k, v in (b.get("stage_by") or {}).items() if v}}
            for b in sorted(beads, key=lambda b: b.get("landed") or "")]


def by_priority(beads, stage):
    out = {}
    for p in sorted({b.get("priority") for b in beads if b.get("stages")}, key=lambda p: (p is None, p)):
        xs = [b["stages"][stage] for b in beads if b.get("stages") and b.get("priority") == p]
        out[f"P{p}" if p is not None else "none"] = {"beads": len(xs), "h": round(sum(xs), 1),
                                                     "median_h": q.quantile(xs, 0.5)}
    return out


def git_log(repo, since, until):
    try:
        out = subprocess.run(["git", "-C", repo, "log", f"--since={q.iso(since)}", f"--until={q.iso(until)}",
                              "--format=%x1e%h%x09%aI%x09%s", "--name-only"], capture_output=True, text=True,
                             timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        return {"repo": repo, "error": str(e)[:200]}
    if out.returncode != 0:
        return {"repo": repo, "error": out.stderr.strip()[:200] or f"git exit {out.returncode}"}
    commits = []
    for chunk in out.stdout.split("\x1e"):
        lines = [x for x in chunk.strip().split("\n") if x.strip()]
        if lines:
            sha, when, subject = (lines[0].split("\t") + ["", ""])[:3]
            commits.append({"sha": sha, "date": when, "subject": subject[:160], "files": lines[1:12]})
    return {"repo": repo, "commits": commits}


def open_now(types, now):
    """Work beads open now (types that landed in the window), by state, with the oldest: work
    still waiting never shows in the landed numbers."""
    try:
        rigs, _ = q.discover_rigs()
        beads = q.Beads()
    except SystemExit as e:
        return {"error": str(e)[:200]}
    out = {}
    for rig in rigs:
        try:
            rows = beads.rows(rig, "SELECT id, title, status, priority, assignee, created_at, CAST(metadata AS CHAR) AS metadata "
                                   f"FROM {{T}}issues WHERE status IN ({q.placeholders(len(OPEN_STATUSES))}) "
                                   f"AND issue_type IN ({q.placeholders(len(types))})", (*OPEN_STATUSES, *types))
        except Exception as e:
            out[rig.name] = {"error": str(e)[:160]}
            continue
        states = defaultdict(list)
        for r in rows:
            m = q.jload(r["metadata"])
            hold = next((m[k] for k in q.HOLD_KEYS if str(m.get(k) or "").strip()), "")
            routed = next((m[k] for k in q.ROUTE_KEYS if str(m.get(k) or "").strip()), "")
            if r["status"] == "in_progress":
                st = "claimed"
            elif r["status"] in q.PARKED_STATUSES or hold:
                st = "parked"
            elif routed or r["assignee"]:
                st = "routed"
            else:
                st = "unrouted"
            age = q.hours(q.as_utc(r["created_at"]), now) or 0
            states[st].append({"id": r["id"], "priority": r["priority"], "age_h": round(age, 1),
                               "title": r["title"][:80], "why": str(hold or routed or r["assignee"] or "")[:80]})
        if states:
            out[rig.name] = {st: {"n": len(xs), "age_h": round(sum(x["age_h"] for x in xs), 1),
                                  "oldest": sorted(xs, key=lambda x: -x["age_h"])[:3]} for st, xs in states.items()}
    return out


def previous_review(label):
    """The latest review (the seat's <week>.json) before this week, with its facts."""
    labels = sorted(f[:-5] for f in os.listdir(REVIEWS) if re.fullmatch(r"\d{4}-W\d{2}\.json", f)) \
        if os.path.isdir(REVIEWS) else []
    prev = [x for x in labels if x < label]
    if not prev:
        return None, None
    p = prev[-1]
    return q.load_json(os.path.join(REVIEWS, f"{p}.json"), None), q.load_json(os.path.join(REVIEWS, f"{p}.facts.json"), None)


def applied(diff, repo):
    """'applied' when the diff is already in the repo, 'not applied' when it still applies, else
    'unknown' (the files changed in another way since)."""
    if not diff or not diff.strip():
        return "no diff"
    patch = os.path.join(q.STATE_DIR, "review-check.patch")
    os.makedirs(q.STATE_DIR, exist_ok=True)
    with open(patch, "w") as f:
        f.write(diff if diff.endswith("\n") else diff + "\n")
    try:
        for args, verdict in ((["--reverse"], "applied"), ([], "not applied")):
            r = subprocess.run(["git", "-C", repo, "apply", "--check", *args, patch], capture_output=True, text=True,
                               timeout=60)
            if r.returncode == 0:
                return verdict
        return "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    finally:
        try:
            os.remove(patch)
        except OSError:
            pass


def last_review_check(label, metrics):
    rev, facts = previous_review(label)
    if rev is None:
        return None
    ch = rev.get("change") or {}
    key = ch.get("metric")
    then = ((facts or {}).get("metrics") or {}).get(key, {}).get("last") if key else None
    now = (metrics.get(key) or {}).get("last") if key else None
    repo = ch.get("repo") or q.CITY
    out = {"week": rev.get("week"), "change": ch.get("title"), "metric": key, "direction": ch.get("direction"),
           "target": ch.get("target"), "expect_without": ch.get("expect_without"), "then": then, "now": now,
           "moved": r2(now - then) if isinstance(now, (int, float)) and isinstance(then, (int, float)) else None,
           "applied": applied(ch.get("diff"), repo), "repo": repo,
           "decision_default": (rev.get("decision") or {}).get("default")}
    if out["moved"] is not None and out["direction"] in ("down", "up") and out["applied"] == "applied":
        # moved the wanted way; whether the change caused it is the reviewer's call (see expect_without)
        out["as_expected"] = (out["moved"] < 0) if out["direction"] == "down" else (out["moved"] > 0)
    return out


def build_facts(now, week=None):
    label, days, before, review_day = review_window(now, week)
    recs, missing = load_days(days + before)
    last, n_last = windows(recs, days)
    prev, n_prev = windows(recs, before)
    weeks_prev = n_prev / 7
    beads = [b for d in days if d in recs for r in recs[d]["rigs"].values() for b in r["beads"]]
    scopes = ["all"] + sorted(n for n in last if n != "all" and (last[n]["landed"] or (prev.get(n) or {}).get("landed")))
    metrics = {}
    for s in scopes:
        a = catalog(last[s], s, n_last / 7)
        b = catalog(prev[s], s, weeks_prev) if s in prev else {}
        for k, v in a.items():
            bv = (b.get(k) or {}).get("value")
            metrics[k] = {"last": v["value"], "before": bv, "unit": v["unit"], "basis": v["basis"],
                          "change": r2(v["value"] - bv) if v["value"] is not None and bv is not None else None}
    stages = {}
    for s in scopes:
        st = last[s]["stages"]
        pst = (prev.get(s) or {}).get("stages") or {"stages": {}}
        stages[s] = {"landed": st["landed"], "split": st["beads"], "total_h": st["total_h"],
                     "stages": {k: dict(v, before_per_week=r2(pst["stages"].get(k, {}).get("h", 0) / weeks_prev)
                                        if weeks_prev else None, before_share=pst["stages"].get(k, {}).get("share"))
                                for k, v in st["stages"].items()}}
    seats_last = last["all"]["cost_by_seat"]
    seats_prev = (prev.get("all") or {}).get("cost_by_seat") or {}
    cd_prev = ((prev.get("all") or {}).get("cost_days") or 0) / 7
    cost_by_seat = [{"seat": k, "usd": v["usd"], "before_per_week": r2(seats_prev[k]["usd"] / cd_prev)
                     if k in seats_prev and cd_prev else None} for k, v in list(seats_last.items())[:15]]
    start, end = q.day_bounds(days[0])[0], q.day_bounds(days[-1])[1]
    types = sorted({b["type"] for b in beads if b.get("type")}) or ["task", "bug", "feature", "chore"]
    facts = {
        "week": label, "days": [days[0], days[-1]], "before": [before[0], before[-1]] if before else None,
        "days_with_history": n_last, "before_days_with_history": n_prev, "history_missing": missing,
        "review_day": review_day.isoformat(), "timezone": q.LOCAL_NAME, "generated_at": q.iso(now),
        "out_dir": q.OUT_DIR, "city": q.CITY,
        "stages": stages,
        "batches": batches(beads),
        "routed_by_target": stage_reasons(beads, "routed"),
        "routed_by_priority": by_priority(beads, "routed"),
        "parked_by_reason": stage_reasons(beads, "parked"),
        "blocked_by": stage_reasons(beads, "blocked"),
        "rework_beads": [{"id": b["id"], "rig": b.get("rig"), "h": round(b["stages"]["rework"], 1), "bounces": b["bounces"],
                          "reasons": [x["reason"][:100] for x in b["bounce_detail"]][:2]}
                         for b in sorted((b for b in beads if b.get("stages") and b["stages"]["rework"] > 0),
                                         key=lambda b: -b["stages"]["rework"])[:TOP]],
        "metrics": metrics,
        "cost_by_seat": cost_by_seat,
        "config_commits": [dict(git_log(r, start, end), after=git_log(r, end, now).get("commits")) for r in CONFIG_REPOS],
        "open_now": open_now(types, now),
        "last_review": last_review_check(label, metrics),
        "beads": bead_rows(beads),
        "sources": {"history": os.path.join(q.OUT_DIR, "history"), "metrics_last_ok_day": read(os.path.join(q.STATE_DIR, "last-ok-day")),
                    "bead_types": types},
    }
    return facts


# ---------- the Markdown the seat reads ----------

def fh(v):
    return "no data" if v is None else f"{v:,.1f}"


def pri(p):
    return "" if p is None else f" P{p}"


def to_md(f):
    L = [f"# Quality review facts, week {f['week']}",
         "",
         f"Reviewed: {f['days'][0]} to {f['days'][1]} ({f['timezone']}), {f['days_with_history']} day(s) of history. "
         f"Compared with: {f['before'][0] if f['before'] else '-'} to {f['before'][1] if f['before'] else '-'}, "
         f"{f['before_days_with_history']} day(s), shown per week. Generated {f['generated_at']} by quality_review.py "
         "(no model). Every number below is in the .facts.json next to this file under the same name; its `beads` "
         "list has every landed bead of the week with its stage hours, to recompute any of them."]
    if f["history_missing"]:
        L.append(f"History missing for: {', '.join(f['history_missing'])}.")
    a = f["stages"]["all"]
    L += ["", "## Where the lead time went (landed beads, created to merged)", "",
          f"All rigs: {a['landed']} beads landed, {a['split']} with a stage split, {fh(a['total_h'])} bead-hours.", "",
          "| Stage | Hours | Share | Beads | Median h | p90 h | Before, h/week | Before share | Top beads (h; why) |",
          "|---|---|---|---|---|---|---|---|---|"]
    for k, v in a["stages"].items():
        top = "; ".join(f"{t['id']}{pri(t['priority'])} {fh(t['h'])}" + (f" ({', '.join(t['why'])[:70]})" if t["why"] else "")
                        for t in v["top"][:3])
        L.append(f"| {k} | {fh(v['h'])} | {fh(v['share'])}% | {v['n']} | {fh(v['median_h'])} | {fh(v['p90_h'])} | {fh(v['before_per_week'])} | "
                 f"{fh(v['before_share'])}% | {top} |")
    for s, v in f["stages"].items():
        if s == "all" or not v["landed"]:
            continue
        big = sorted(v["stages"].items(), key=lambda t: -t[1]["h"])[:3]
        L.append(f"- {s}: {v['landed']} landed, {fh(v['total_h'])} h; " +
                 ", ".join(f"{k} {fh(x['h'])} h ({fh(x['share'])}% of the rig, {x['n']} beads, median {fh(x['median_h'])} h, "
                           f"before {fh(x['before_per_week'])} h/week)" for k, x in big))
    L += ["", "Stages: blocked (a 'blocks' dependency on an open bead) > parked (blocked/deferred status, a hold reason, "
          "a defer date) > before the first claim: unrouted / routed (waiting for an agent) > after it: working (to the "
          "first PR), review (PR to first bounce or merge), rework (from the first bounce or reopen). See a bead's timeline: "
          "`quality-metrics.sh --bead <id>`."]
    L += ["", "### Filed together (beads created in the same hour that waited in one stage)", ""] + \
         [f"- {x['stage']}: {x['n']} beads filed {x['filed']} ({', '.join(x['rigs'])}): {fh(x['h'])} h, median {fh(x['median_h'])} h "
          f"(e.g. {', '.join(x['beads'][:4])})" for x in f["batches"]] or ["- none of 3 or more beads"]
    L += ["", "### Routed, waiting for an agent: by target", ""] + \
         [f"- {x['why'] or '(no target)'}: {fh(x['h'])} h over {x['n']} beads (e.g. {', '.join(x['beads'][:4])})"
          for x in f["routed_by_target"]]
    L += ["", "### Routed, by bead priority", ""] + \
         [f"- {p}: {x['beads']} beads, {fh(x['h'])} h, median {fh(x['median_h'])} h" for p, x in f["routed_by_priority"].items()]
    L += ["", "### Parked: by reason", ""] + \
         [f"- {fh(x['h'])} h, {x['n']} bead(s) {', '.join(x['beads'][:3])}: {x['why'][:120]}" for x in f["parked_by_reason"]]
    L += ["", "### Blocked: by blocker", ""] + \
         [f"- {x['why']}: {fh(x['h'])} h ({', '.join(x['beads'][:3])})" for x in f["blocked_by"]] or ["- none"]
    L += ["", "### Rework: the beads with the most", ""] + \
         [f"- {x['id']} ({x['rig']}): {fh(x['h'])} h, {x['bounces']} bounce(s): {' | '.join(x['reasons'])}" for x in f["rework_beads"]]
    L += ["", "## The metrics, this week vs the weeks before", "",
          "Names are the keys a proposal must use for the metric it should move. All rigs in full; per rig the main ones "
          "for rigs that landed beads this week. The .facts.json has every key for every rig.", "",
          "| Metric | This week | Before | Change | Unit | Basis |", "|---|---|---|---|---|---|"]
    landed_rigs = {s for s, v in f["stages"].items() if v["landed"]}
    rig_keys = ("landed", "bounce_rate", "first_pass_ci_rate", "post_merge_failure_rate", "created_merged.median_h",
                "created_claimed.median_h") + tuple(f"stage.{x}.h" for x in q.STAGES)
    for k, v in f["metrics"].items():
        scope, _, name = k.partition(".")
        if (v["last"] is None and v["before"] is None) or (scope != "all" and (scope not in landed_rigs or name not in rig_keys)):
            continue
        L.append(f"| {k} | {fh(v['last'])} | {fh(v['before'])} | {fh(v['change'])} | {v['unit']} | {v['basis']} |")
    L += ["", "## Cost by seat (list price, all rigs)", "", "| Seat | This week $ | Before $/week |", "|---|---|---|"]
    L += [f"| {x['seat']} | {fh(x['usd'])} | {fh(x['before_per_week'])} |" for x in f["cost_by_seat"]]
    L += ["", "## City config commits"]
    for c in f["config_commits"]:
        L += ["", f"In {c['repo']} during the week:" if "error" not in c else f"{c['repo']}: no data ({c['error']})"]
        L += [f"- {x['sha']} {x['date'][:16]} {x['subject']} ({', '.join(x['files'][:4])})" for x in c.get("commits") or []] or ["- none"]
        if c.get("after"):
            L += ["", "Since the week ended:"] + [f"- {x['sha']} {x['date'][:16]} {x['subject']}" for x in c["after"]]
    L += ["", "## Work open now (not in the numbers above)", ""]
    if "error" in f["open_now"]:
        L.append(f"No data: {f['open_now']['error']}")
    for rig, v in f["open_now"].items():
        if not isinstance(v, dict):
            continue
        if "error" in v:
            L.append(f"- {rig}: no data ({v['error']})")
            continue
        L.append(f"- {rig}: " + "; ".join(f"{st} {x['n']} (oldest {', '.join(o['id'] + ' P' + str(o['priority']) + ' ' + fh(o['age_h']) + ' h' for o in x['oldest'][:2])})"
                                          for st, x in sorted(v.items())))
    lr = f["last_review"]
    L += ["", "## Last review's change (checked by this script)", ""]
    if not lr:
        L.append("No earlier review in the output directory.")
    else:
        L.append(f"Week {lr['week']}: {lr['change'] or '(no change proposed)'}. Applied: {lr['applied']} (git apply --check in "
                 f"{lr['repo']}). Metric {lr['metric']}: then {fh(lr['then'])}, now {fh(lr['now'])}, moved {fh(lr['moved'])} "
                 f"(wanted {lr['direction'] or '-'} to {lr['target']})" + (f"; as expected: {lr['as_expected']}" if "as_expected" in lr else "") + ".")
    return "\n".join(L) + "\n"


# ---------- index for the page ----------

def build_index():
    if not os.path.isdir(REVIEWS):
        return {"weeks": []}
    labels = sorted({m.group(1) for f in os.listdir(REVIEWS) for m in [re.match(r"(\d{4}-W\d{2})\.(facts\.)?json$", f)] if m})
    weeks = []
    for i, w in enumerate(labels):
        f = q.load_json(os.path.join(REVIEWS, f"{w}.facts.json"), {}) or {}
        r = q.load_json(os.path.join(REVIEWS, f"{w}.json"), None)
        nxt = q.load_json(os.path.join(REVIEWS, f"{labels[i + 1]}.facts.json"), {}) if i + 1 < len(labels) else {}
        st = ((f.get("stages") or {}).get("all") or {}).get("stages") or {}
        biggest = max(st.items(), key=lambda t: t[1]["h"], default=(None, None))
        e = {"week": w, "days": f.get("days"), "facts": bool(f), "review": r is not None,
             "biggest_stage": {"stage": biggest[0], "h": biggest[1]["h"], "share": biggest[1]["share"]} if biggest[0] else None,
             "md": read(os.path.join(REVIEWS, f"{w}.md"))}
        if r:
            sd, ch, dc = r.get("slowdown") or {}, r.get("change") or {}, r.get("decision") or {}
            e.update({"slowdown": {k: sd.get(k) for k in ("stage", "rig", "hours", "share", "summary")},
                      "change": {k: ch.get(k) for k in ("title", "metric", "baseline", "target", "direction", "charter", "files")},
                      "decision_default": dc.get("default")})
        lr = (nxt or {}).get("last_review")
        if lr and lr.get("week") == w:
            e["followup"] = {k: lr.get(k) for k in ("applied", "metric", "then", "now", "moved", "as_expected")}
        weeks.append(e)
    return {"generated_at": q.iso(datetime.now(UTC)), "weeks": weeks}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--due", action="store_true")
    g.add_argument("--facts", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--index", action="store_true")
    ap.add_argument("--week", help="a past week to review, YYYY-Www (a backtest)")
    args = ap.parse_args()
    if not q.CITY:
        sys.exit("quality-review: no city (set GC_CITY_PATH or run inside a city)")
    now = datetime.now(UTC)
    if args.due:
        label, days, _, review_day = review_window(now)
        force = q.env("QUALITY_FORCE") == "1"
        if not force and read(STAMP) == label:
            print(f"quality-review: week {label} already reviewed; next on the first fire after "
                  f"{(review_day + timedelta(days=7)).isoformat()} {HOUR:02d}:00 {q.LOCAL_NAME}")
            return 1
        today = q.day_of(now)
        if not force and read(os.path.join(q.STATE_DIR, "last-ok-day")) != today:
            print(f"quality-review: week {label} is due; waiting for today's ({today}) quality-metrics run")
            return 1
        # right after an upgrade, today's history may come from a pack version without the stages
        if "stages" not in (q.load_json(os.path.join(q.OUT_DIR, "quality.json"), {}).get("sources") or {}):
            print(f"quality-review: week {label} is due; waiting for a quality-metrics run that writes the "
                  "lead-time stages (QUALITY_FORCE=1 gc order run quality-metrics)")
            return 1
        print(label)
        return 0
    if args.index:
        q.write_json(os.path.join(REVIEWS, "index.json"), build_index())
        print(os.path.join(REVIEWS, "index.json"))
        return 0
    facts = build_facts(now, args.week)
    md = to_md(facts)
    if args.dry_run:
        print(md)
        q.log("dry run: nothing written")
        return 0
    base = os.path.join(REVIEWS, f"{facts['week']}.facts")
    q.write_json(base + ".json", facts)
    with open(base + ".md.tmp", "w") as fo:
        fo.write(md)
    os.replace(base + ".md.tmp", base + ".md")
    q.write_json(os.path.join(REVIEWS, "index.json"), build_index())
    print(f"{facts['week']}\t{base}.md\t{base}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
