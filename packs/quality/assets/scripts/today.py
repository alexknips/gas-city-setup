#!/usr/bin/env python3
"""Today: the pack's first screen (README.md, "Today").

No model and no tokens. Reads what the pack's orders already wrote (stops.json from the census,
quality.json from the metrics, reviews/ from the weekly review) and makes one read of gc's own
API (the city's health). Writes OUT_DIR/today.json and copies assets/page/today.html next to it.
The quality-stops order runs it after every census (stops.py calls publish()); run by hand it
reads the stops.json on disk.

    today.py             write today.json and today.html
    today.py --dry-run   print today.json; write nothing
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as q  # noqa: E402  (same directory: output and state dirs, the city, JSON helpers)

STATE = os.path.join(q.STATE_DIR, "today-state.json")
GC_API = q.env("QUALITY_GC_API")                      # gc's API base URL; unset = ask `gc dashboard --no-open`; off = none
GC_CITY_NAME = q.env("QUALITY_GC_CITY_NAME")          # the city's name in gc's API; unset = the one at this city's path
GC_DASHBOARD = q.env("QUALITY_GC_DASHBOARD_URL")      # where a browser reaches gc's dashboard; unset = the API URL
HUMAN_NAME = q.env("QUALITY_HUMAN_NAME")              # the census's human as the page names them; unset = the same
API_KEEP_H = 6                                        # a discovered API URL and city name are kept this long
API_TIMEOUT_S = 5
# The order a person's stops come in: past due first, then open gates, holds, blocked work.
NEED_RANK = {"human_gate_due": 0, "human_gate": 1, "parked": 2, "blocked": 3}
TRENDS = (  # quality.json key, label, unit, which way is better
    ("landed", "Beads landed", "per day", "up"),
    ("bounce_rate", "Bounce rate", "%", "down"),
    ("claimed_merged_median_h", "Claimed to merged", "h, median", "down"),
    ("post_merge_failure_rate", "Post-merge failures", "%", "down"),
    ("cost_per_landed", "Cost per landed bead", "$", "down"),
)


def now_utc():
    return datetime.now(q.UTC)


# ---------- gc's API: one read of the city's health ----------

def get_json(url):
    try:
        with urllib.request.urlopen(url, timeout=API_TIMEOUT_S) as r:
            return json.loads(r.read()), None
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"{url}: {e.__class__.__name__}: {str(e)[:120]}"


def discover_api():
    """The supervisor URL `gc dashboard --no-open` prints ("... served by the gc supervisor at <url>")."""
    try:
        out = subprocess.run([q.GC, "dashboard", "--no-open"], capture_output=True, text=True, timeout=60, cwd=q.CITY)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"gc dashboard --no-open: {e}"
    m = re.search(r"https?://[^\s\"'<>]+", out.stdout + "\n" + out.stderr)
    if not m:
        return None, f"gc dashboard --no-open: no URL in its answer (exit {out.returncode})"
    return m.group(0).rstrip(".,;)/"), None


def city_name(api):
    """The city's name in gc's API: the city whose path is this one in /v0/cities."""
    d, err = get_json(f"{api}/v0/cities")
    if err:
        return None, err
    here = os.path.realpath(q.CITY)
    for c in (d or {}).get("items") or []:
        if c.get("path") and os.path.realpath(c["path"]) == here:
            return c.get("name"), None
    return None, f"{api}/v0/cities: no city at {here}"


def gc_health(state, now):
    """(api base URL, city name, health JSON or None, error or None). A discovered URL and name
    are kept in the state for API_KEEP_H hours, and dropped when a read fails; the health is read
    every run."""
    if GC_API.lower() == "off":
        return None, None, None, "QUALITY_GC_API=off"
    c = state.get("api") or {}
    if now - (q.as_utc(c.get("at")) or now) > timedelta(hours=API_KEEP_H):
        c = {}
    api = (GC_API or c.get("url") or "").rstrip("/")
    if not api:
        api, err = discover_api()
        if err:
            return None, None, None, err
    name = GC_CITY_NAME or (c.get("city") if c.get("url") == api else None)
    if not name:
        name, err = city_name(api)
        if err:
            return api, None, None, err
    if (c.get("url"), c.get("city")) != (api, name):
        state["api"] = {"url": api, "city": name, "at": q.iso(now)}
    health, err = get_json(f"{api}/v0/city/{name}/health")
    if err:
        state.pop("api", None)  # look again next run: the supervisor may have moved
    return api, name, health, err


# ---------- the model ----------

def display(actor, human, human_name):
    """How the page names an actor: the human by their display name, a pool session
    ('gastown__dog-gc-up9w8') as 'dog session gc-up9w8', anything else as the census has it."""
    if actor == human:
        return human_name
    if "__" in actor:
        tail = actor.rsplit("__", 1)[1]
        if "-" in tail:
            agent, sid = tail.split("-", 1)
            return f"{agent} session {sid}"
    return actor


def needs_you(stops, human, labels):
    """The human's stops, one card per root decision: beads blocked behind the same root are
    listed under it, since one answer clears them all."""
    groups = {}
    for s in stops:
        if s.get("actor") != human or s.get("kind") == "host":
            continue
        r = {k: s.get(k) for k in ("kind", "id", "rig", "title", "class", "why", "age_h", "since", "root", "on", "also")}
        r["label"] = labels.get(s["class"], s["class"])
        root = s.get("root") or s["id"]
        g = groups.setdefault(root, {"root": root, "head": None, "behind": []})
        if s["id"] == root:
            g["head"] = r
        else:
            g["behind"].append(r)
    out = []
    for g in groups.values():
        g["behind"].sort(key=lambda r: -(r["age_h"] or 0))
        if g["head"] is None:  # the root is no stop of the human's (it is moving, or someone else's)
            g["head"] = g["behind"].pop(0)
        out.append(g)
    return sorted(out, key=lambda g: (NEED_RANK.get(g["head"]["class"], 9), -(g["head"]["age_h"] or 0)))


def who_clears(stops, human, human_name, labels):
    """Every bead and seat stop grouped by the actor who has to move next (the host is in health)."""
    by = defaultdict(lambda: {"n": 0, "classes": defaultdict(int), "oldest_h": 0.0})
    for s in stops:
        if s.get("kind") == "host":
            continue
        g = by[s.get("actor") or "nobody"]
        g["n"] += 1
        g["classes"][s["class"]] += 1
        g["oldest_h"] = max(g["oldest_h"], s.get("age_h") or 0)
    return sorted(({"actor": a, "name": display(a, human, human_name), "human": a == human, "n": g["n"],
                    "oldest_h": round(g["oldest_h"], 1),
                    "classes": [[k, labels.get(k, k), n] for k, n in sorted(g["classes"].items(), key=lambda kv: -kv[1])]}
                   for a, g in by.items()), key=lambda r: (-r["n"], r["actor"]))


def trends(qual):
    """The five tiles: last 7 days against the longest window, with that window's daily series."""
    days = qual.get("days") or []
    windows = qual.get("windows") or {}
    n_long = max((int(k) for k in windows if str(k).isdigit()), default=0)
    w7 = (windows.get("7") or {}).get("all") or {}
    wl = (windows.get(str(n_long)) or {}).get("all") or {}
    series = (qual.get("series") or {}).get("all") or {}
    out = []
    for key, label, unit, better in TRENDS:
        if key == "landed":
            now = round(w7["landed"] / 7, 1) if w7.get("landed") is not None else None
            base_v = round(wl["landed"] / n_long, 1) if wl.get("landed") is not None and n_long else None
        elif key == "claimed_merged_median_h":
            now = ((w7.get("time_to_land") or {}).get("claimed_merged") or {}).get("median")
            base_v = ((wl.get("time_to_land") or {}).get("claimed_merged") or {}).get("median")
        else:
            now, base_v = w7.get(key), wl.get(key)
        spark = series.get(key) or []
        out.append({"key": key, "label": label, "unit": unit, "better": better, "now": now, "base": base_v,
                    "spark": spark if len(spark) == len(days) else []})
    return out, n_long


def latest_review(out_dir):
    index = q.load_json(os.path.join(out_dir, "reviews", "index.json"), {})
    weeks = index.get("weeks") or []
    if not weeks:
        return None
    w = weeks[-1]
    r = q.load_json(os.path.join(out_dir, "reviews", f"{w['week']}.json"), {}) if w.get("review") else {}
    dec = r.get("decision") or {}
    slow = r.get("slowdown") or w.get("slowdown") or {}
    change = r.get("change") or w.get("change")
    return {"week": w["week"], "days": w.get("days"), "written": bool(w.get("review")),
            "biggest_stage": w.get("biggest_stage"),
            "slowdown": {k: slow.get(k) for k in ("stage", "rig", "hours", "share", "median_h", "summary")} if slow else None,
            "change": change.get("title") if isinstance(change, dict) else change,
            "question": dec.get("question"), "default": dec.get("default") or w.get("decision_default"),
            "options": [o.get("label") for o in dec.get("options") or [] if o.get("label")],
            "last_week": r.get("last_week")}


def build(stops, now, state):
    out_dir = q.OUT_DIR
    qual = q.load_json(os.path.join(out_dir, "quality.json"), {})
    cfg = stops.get("config") or {}
    human = cfg.get("human") or "owner"
    human_name = HUMAN_NAME or human
    labels = {c["key"]: c["label"] for c in stops.get("classes") or []}
    all_stops = stops.get("stops") or []
    errors = []

    api, name, health, err = gc_health(state, now)
    if err:
        errors.append(err)
    link_base = (GC_DASHBOARD or api or "").rstrip("/")
    gc_city = f"{link_base}/city/{name}" if link_base and name else None

    tiles, n_long = trends(qual)
    days = qual.get("days") or []
    windows = qual.get("windows") or {}
    w7 = (windows.get("7") or {}).get("all") or {}
    wl = (windows.get(str(n_long)) or {}).get("all") or {}
    cost_series = ((qual.get("series") or {}).get("all") or {}).get("cost_usd") or []
    today = qual.get("today")
    seats = stops.get("seats") or []
    cls_n = {c["key"]: c["n"] for c in stops.get("classes") or []}
    host_stop = next((s for s in all_stops if s.get("kind") == "host"), None)
    friction = w7.get("friction") or {}

    return {
        "generated_at": q.iso(now),
        "city": name or os.path.basename(os.path.realpath(q.CITY)),
        "human": human, "human_name": human_name, "router": cfg.get("router"),
        "links": {"gc": gc_city, "beads": f"{gc_city}/beads" if gc_city else None,
                  "agents": f"{gc_city}/agents" if gc_city else None, "quality": "index.html"},
        "census": {"at": stops.get("generated_at"), "full_at": stops.get("full_at"), "part": stops.get("part"),
                   "every_min": stops.get("interval_min"), "timezone": stops.get("timezone")},
        "metrics": {"at": qual.get("generated_at"), "today": today, "timezone": qual.get("timezone"),
                    "days": days, "window_days": n_long},
        "needs_you": needs_you(all_stops, human, labels),
        "who": who_clears(all_stops, human, human_name, labels),
        "moving": len(stops.get("moving") or []),
        "backlog": sum((stops.get("backlog") or {}).values()),
        "health": {
            "supervisor": health, "supervisor_error": err,
            "seats": {"total": len(seats), "up": sum(1 for s in seats if s.get("up")),
                      "always_on": sum(1 for s in seats if s.get("always_on")),
                      "always_on_down": cls_n.get("seat_down", 0) if stops.get("full_at") else None,
                      "resets": cls_n.get("seat_resets", 0), "mail_waiting": cls_n.get("seat_mail", 0),
                      "unread": sum(s.get("unread") or 0 for s in seats)},
            "host": dict(stops.get("host") or {}, over=bool(host_stop), busiest=(host_stop or {}).get("actor")),
            "friction_7d": w7.get("friction_total"),
            "friction_top": sorted(friction.items(), key=lambda kv: -kv[1])[:3],
        },
        "trends": tiles,
        "review": latest_review(out_dir),
        "cost": {"usd_7d": w7.get("cost_usd"), "landed_7d": w7.get("cost_landed"),
                 "per_landed_7d": w7.get("cost_per_landed"), "per_landed_long": wl.get("cost_per_landed"),
                 "days_7d": w7.get("cost_days"),
                 "today_usd": cost_series[days.index(today)] if today in days and len(cost_series) == len(days) else None,
                 "unpriced": w7.get("cost_unpriced") or {}, "unpriced_tokens": w7.get("cost_unpriced_tokens")},
        "errors": errors,
    }


# ---------- output ----------

def render_page():
    src = os.path.join(q.PACK_DIR, "assets", "page", "today.html")
    dst = os.path.join(q.OUT_DIR, "today.html")
    tmp = dst + ".tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    if q.POST_RENDER:
        cmd = q.POST_RENDER.replace("{page}", dst).replace("{out}", q.OUT_DIR).replace("{name}", "today")
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return f"post-render hook failed ({r.returncode}): {r.stderr.strip()[:300]}"
    return None


def publish(stops=None, now=None):
    """Write today.json and today.html from a census (stops.json's content; read from disk when
    not given). Returns an error string or None; never raises for a missing source."""
    now = now or now_utc()
    if stops is None:
        stops = q.load_json(os.path.join(q.OUT_DIR, "stops.json"), {})
    state = q.load_json(STATE, {})
    model = build(stops, now, state)
    q.write_json(os.path.join(q.OUT_DIR, "today.json"), model)
    err = render_page()
    q.write_json(STATE, state, compact=True)
    return err


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not q.CITY:
        sys.exit("today: no city (set GC_CITY_PATH or run inside a city)")
    if args.dry_run:
        now = now_utc()
        stops = q.load_json(os.path.join(q.OUT_DIR, "stops.json"), {})
        print(json.dumps(build(stops, now, q.load_json(STATE, {})), indent=1, default=str))
        return 0
    err = publish()
    if err:
        print(f"today: {err}", file=sys.stderr)
    print(f"today: wrote {os.path.join(q.OUT_DIR, 'today.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
