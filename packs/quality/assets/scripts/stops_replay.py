#!/usr/bin/env python3
"""Replay the stop census over the past (README.md, "Why it's waiting", backtest).

No model and no tokens. Rebuilds what the census would have said at a past instant from history
the city keeps anyway, and classifies it with the census's own rules (stops.classify):

- each bead's state from its events (an event's old_value is the whole row before it; after the
  last event the row as it is now), its blocks dependencies by creation time;
- which sessions were up from the city event log and its rotated archives (session.woke,
  session.stopped, session.idle_killed), and stalled resets per local day;
- each pool's cap from the city.toml in git at that time (a cap set elsewhere: today's);
- the host's 1-minute load from sysstat (`sar -q`), when the host keeps it.

    stops_replay.py --at ISO [--bead ID ...]   the census at that instant (only those beads with --bead)
    stops_replay.py --from DAY [--to DAY]      stop-hours per class per local day, a census every
                                               QUALITY_STOPS_INTERVAL_MIN minutes

What it cannot see (the census can): when a session was last active (so no assignee_idle or
review_idle), rig and agent suspension (runtime state, no history), unread mail, a landing's
post-merge verdict, which seats were always-on then (today's list is used), the top consumers of
a past load, and dependencies deleted since.
"""
import argparse
import glob
import gzip
import json
import os
import re
import subprocess
import sys
from bisect import bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as q  # noqa: E402
import stops as S  # noqa: E402

try:
    import tomllib
except ImportError:
    tomllib = None

UTC = q.UTC
KEEP_META = set(q.ROUTE_KEYS) | set(q.HOLD_KEYS) | {S.REJECTION_KEY, q.HUMAN_DUE_KEY, "gc.claimed_at"}


def trim(row):
    """The fields a census reads from a full row (an events old_value, or the row now)."""
    if not isinstance(row, dict) or "status" not in row:
        return None
    m = q.jload(row.get("metadata"))
    return {"status": row.get("status") or "", "assignee": (row.get("assignee") or "").strip(), "title": row.get("title"),
            "defer_until": row.get("defer_until"), "meta": {k: v for k, v in m.items() if k in KEEP_META}}


# ---------- beads ----------

def load_beads(beads, stores, t0, t1, clock):
    """Every bead alive at some point in [t0, t1] with its trimmed rows, events and dependencies."""
    skip = tuple(S.SKIP_TYPES)
    out = {}
    for st in stores:
        rows = beads.rows(st, "SELECT id, title, status, assignee, issue_type, created_at, created_by, closed_at, started_at, "
                              "defer_until, CAST(metadata AS CHAR) AS metadata FROM {T}issues WHERE created_at <= %s AND "
                              "(closed_at IS NULL OR closed_at >= %s)" +
                          (f" AND issue_type NOT IN ({q.placeholders(len(skip))})" if skip else ""),
                          (q.sqlts(t1), q.sqlts(t0)) + skip)
        mine = {}
        for r in rows:
            mine[r["id"]] = {"id": r["id"], "rig": st.name, "title": r["title"] or "", "type": r["issue_type"],
                             "created": q.as_utc(r["created_at"]), "created_by": r["created_by"] or "",
                             "closed": q.as_utc(r["closed_at"]), "started": q.as_utc(r["started_at"]),
                             "now": trim(dict(r, metadata=r["metadata"])), "events": [], "deps": []}
        ids = list(mine)
        evs = []
        for part in q.chunks(ids, 40):
            for e in beads.rows(st, "SELECT issue_id, event_type, actor, created_at, old_value, new_value FROM {T}events "
                                    f"WHERE issue_id IN ({q.placeholders(len(part))})", tuple(part)):
                evs.append({"issue_id": e["issue_id"], "type": e["event_type"], "actor": e["actor"] or "",
                            "created_at": e["created_at"], "old": trim(q.jload(e["old_value"])), "nv": q.jload(e["new_value"])})
        clock.resolve(st, evs)
        for e in evs:
            if e.get("at"):
                mine[e["issue_id"]]["events"].append(e)
        deps = []
        for part in q.chunks(ids):
            for d in beads.rows(st, "SELECT issue_id, depends_on_issue_id AS dep, depends_on_external AS ext, created_at "
                                    f"FROM {{T}}dependencies WHERE type = 'blocks' AND issue_id IN ({q.placeholders(len(part))})",
                                tuple(part)):
                on = d["dep"] or (str(d["ext"]).split(":")[-1] if d["ext"] else None)
                if on:
                    deps.append({"issue_id": d["issue_id"], "on": on, "created_at": d["created_at"]})
        clock.resolve(st, deps, count=False)
        for d in deps:
            mine[d["issue_id"]]["deps"].append(d)
        for b in mine.values():
            b["events"].sort(key=lambda e: e["at"])
            b["_times"] = [e["at"] for e in b["events"]]
        out.update(mine)
        q.log(f"{st.name}: {len(mine)} beads, {len(evs)} events")
    return out


def row_at(b, t):
    """The bead's trimmed row at instant t (None when it did not exist or was closed then)."""
    if b["created"] and t < b["created"]:
        return None
    i = bisect_right(b["_times"], t)
    row = None
    for e in b["events"][i:]:  # the first later event that carries the whole row before it
        if e["old"]:
            row = e["old"]
            break
    row = row or b["now"]
    if not row or row["status"] in ("closed", "tombstone"):
        return None
    return row


def bead_at(b, row, t):
    return {"id": b["id"], "rig": b["rig"], "title": row.get("title") or b["title"], "status": row["status"],
            "assignee": row["assignee"], "type": b["type"], "created": b["created"], "created_by": b["created_by"],
            "started": b["started"] if b["started"] and b["started"] <= t else None,
            "defer_until": q.as_utc(row.get("defer_until")), "meta": row["meta"],
            "deps": [d for d in b["deps"] if d.get("at") and d["at"] <= t],
            "events": [e for e in b["events"] if e["at"] <= t]}


# ---------- sessions, caps, load ----------

def session_events(t0, t1):
    """Per session name (alias and session id), its woke/stopped changes in [t0 - 3 days, t1], and
    stalled resets with their times, from events.jsonl and the rotated archives."""
    gcdir = os.path.join(q.CITY, ".gc")
    files = sorted(glob.glob(os.path.join(gcdir, "events.jsonl.archive-*.gz")) +
                   glob.glob(os.path.join(gcdir, "events-archive-old", "events.jsonl.archive-*.gz")))
    want = []
    for f in files:
        m = re.search(r"archive-(\d{8}T\d{6}Z)", f)
        if m and datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) >= t0 - timedelta(days=3):
            want.append(f)
    want.append(os.path.join(gcdir, "events.jsonl"))
    pat = re.compile(rb'"type":"session\.(woke|stopped|idle_killed|reset_stalled)"')
    changes, resets = defaultdict(list), defaultdict(list)
    lo, hi = t0 - timedelta(days=3), t1
    for f in want:
        opener = gzip.open if f.endswith(".gz") else open
        try:
            with opener(f, "rb") as fh:
                for line in fh:
                    if not pat.search(line):
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    at = q.as_utc(e.get("ts"))
                    if at is None or at < lo or at > hi:
                        continue
                    p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
                    names = {x for x in (e.get("subject"), e.get("session_id"), p.get("session_id")) if x}
                    t = e["type"]
                    for n in names:
                        if t == "session.reset_stalled":
                            resets[n].append(at)
                        else:
                            changes[n].append((at, t == "session.woke", e.get("subject") or n, p.get("template")))
        except OSError:
            continue
    for v in changes.values():
        v.sort()
    return changes, resets


def caps_by_commit(t0, t1):
    """[(commit time, {(rig, agent base): max_active_sessions})] of city.toml in git, oldest first."""
    if tomllib is None:
        return []
    r = subprocess.run(["git", "-C", q.CITY, "log", "--format=%H %cI", f"--until={q.iso(t1)}", "--", "city.toml"],
                       capture_output=True, text=True, timeout=60)
    commits = [(q.as_utc(line.split()[1]), line.split()[0]) for line in r.stdout.splitlines() if line.strip()]
    commits.sort()
    keep = [c for c in commits if c[0] >= t0] + [c for c in commits if c[0] < t0][-1:]
    out = []
    for at, sha in sorted(keep):
        try:
            text = subprocess.run(["git", "-C", q.CITY, "show", f"{sha}:city.toml"], capture_output=True, text=True,
                                  timeout=60).stdout
            d = tomllib.loads(text)
        except (ValueError, subprocess.SubprocessError):
            continue
        caps = {}
        for rig in d.get("rigs", []):
            for p in rig.get("patches", []) or []:
                if "max_active_sessions" in p and p.get("agent"):
                    caps[(rig.get("name"), p["agent"])] = p["max_active_sessions"]
        for a in d.get("agent", []):
            if "max_active_sessions" in a and a.get("name"):
                caps[(a.get("dir") or "", a["name"])] = a["max_active_sessions"]
        out.append((at, caps))
    return out


def sar_loads(t0, t1):
    """[(instant, load1)] from sysstat files that cover [t0, t1] ('sar -q', local times)."""
    out = []
    d = t0.astimezone(q.LOCAL).date()
    while d <= t1.astimezone(q.LOCAL).date():
        f = f"/var/log/sysstat/sa{d.day:02d}"
        if os.path.exists(f) and datetime.fromtimestamp(os.path.getmtime(f)).date() >= d:
            r = subprocess.run(["sar", "-q", "-f", f], capture_output=True, text=True, timeout=60,
                               env={**os.environ, "LC_ALL": "C", "S_TIME_FORMAT": "ISO"})
            for line in r.stdout.splitlines():
                x = line.split()
                if len(x) >= 5 and re.match(r"\d\d:\d\d:\d\d$", x[0]) and re.match(r"[\d.]+$", x[3]):
                    at = datetime.combine(d, datetime.strptime(x[0], "%H:%M:%S").time()).replace(tzinfo=q.LOCAL)
                    out.append((at.astimezone(UTC), float(x[3])))
        d += timedelta(days=1)
    return sorted(out)


class Past:
    """The view of the city the census would have had at instant t (a stops.City look-alike)."""

    def __init__(self, now_city, changes, resets, caps, loads, t):
        self.t = t
        self.rig_suspended = set()
        self.named, self.named_all = now_city.named, now_city.named_all
        cap_now = next((c for at, c in reversed(caps) if at <= t), {})
        self.agents = {}
        for name, a in now_city.agents.items():
            cap = cap_now.get((S.rig_of(name) or "", S.base(name)), ((a.get("pool") or {}).get("max")))
            self.agents[name] = dict(a, pool=dict(a.get("pool") or {}, max=cap), suspended=False)
        pools = {S.rig_of(n): n for n in self.agents if self.is_pool(n) and S.rig_of(n)}
        self.sessions, self.by_name, self.stopped = [], {}, {}
        for n, ch in changes.items():
            i = bisect_right([c[0] for c in ch], t)
            if i == 0:
                continue
            at, woke, alias, template = ch[i - 1]
            if self.is_pool(alias):
                # gc once named a pool slot's events after the pool: tell the slots apart by session id
                if n == alias:
                    continue
                alias, tmpl = f"{alias}#{n}", alias
            elif n != alias:
                continue  # keyed by the session id: the alias's entry carries the same change
            else:
                tmpl = template or (alias if alias in self.agents else pools.get(S.rig_of(alias)) if S.rig_of(alias) else alias)
            s = {"alias": alias, "agent_name": alias, "state": "active" if woke else "asleep", "template": tmpl,
                 "last_active": None}
            self.sessions.append(s)
            self.by_name.setdefault(alias, s)
            if "#" in alias:
                self.by_name.setdefault(n, s)
            if not woke:
                self.stopped[alias] = q.iso(at)
        for n, ch in changes.items():  # session ids point at their alias's session
            i = bisect_right([c[0] for c in ch], t)
            if i and n != ch[i - 1][2] and ch[i - 1][2] in self.by_name:
                self.by_name.setdefault(n, self.by_name[ch[i - 1][2]])
        day0 = q.day_bounds(q.day_of(t))[0]
        self.resets = {n: [x for x in v if day0 <= x <= t] for n, v in resets.items()}
        lt = [(a, v) for a, v in loads if a <= t and t - a <= timedelta(minutes=20)]
        self.load = lt[-1][1] if lt else None

    is_pool = S.City.is_pool
    session = S.City.session
    alias = S.City.alias
    is_agent = S.City.is_agent
    slots = S.City.slots
    watcher = S.City.watcher
    agent_of_named = S.City.agent_of_named


def other_blockers(beads, stores, beads_all):
    """Blockers the window does not hold (closed before it, or a skipped type such as a milestone):
    id -> status, assignee, title, created and closed now."""
    ids = {d["on"] for b in beads_all.values() for d in b["deps"]} - set(beads_all)
    order = sorted(stores, key=lambda r: -len(r.prefix))
    by_store = defaultdict(list)
    for i in ids:
        r = next((st for st in order if i.startswith(st.prefix + "-")), None)
        if r:
            by_store[r.name].append((r, i))
    out = {}
    for pairs in by_store.values():
        st = pairs[0][0]
        for part in q.chunks([i for _, i in pairs]):
            for r in beads.rows(st, "SELECT id, status, assignee, title, created_at, closed_at FROM {T}issues "
                                    f"WHERE id IN ({q.placeholders(len(part))})", tuple(part)):
                out[r["id"]] = {"status": r["status"], "assignee": (r["assignee"] or "").strip(), "title": r["title"] or "",
                                "created": q.as_utc(r["created_at"]), "closed": q.as_utc(r["closed_at"])}
    return out


def census_at(t, beads_all, now_city, changes, resets, caps, loads, others, only=None):
    city = Past(now_city, changes, resets, caps, loads, t)
    work = []
    for b in beads_all.values():
        row = row_at(b, t)
        if row:
            work.append(bead_at(b, row, t))
    work_by_id = {b["id"]: b for b in work}
    blockers = {}
    for b in work:
        for d in b["deps"]:
            x = beads_all.get(d["on"])
            if x is None:
                o = others.get(d["on"])  # not in the window: open at t unless it was closed by then
                if o and o["created"] and o["created"] <= t and not (o["closed"] and o["closed"] <= t):
                    st = o["status"] if o["status"] not in ("closed", "tombstone") else "open"  # closed after t
                    blockers[d["on"]] = {"status": st, "assignee": o["assignee"], "title": o["title"]}
                else:
                    blockers[d["on"]] = {"status": "closed", "assignee": "", "title": o["title"] if o else ""}
                continue
            r = row_at(x, t)
            blockers[d["on"]] = ({"status": r["status"], "assignee": r["assignee"], "title": x["title"]} if r else
                                 {"status": "closed", "assignee": "", "title": x["title"]})
    holders = defaultdict(list)
    for b in work:
        if b["assignee"] and b.get("type") not in S.HUMAN_ONLY_TYPES:
            for n in S.session_names(city.session(b["assignee"])) or [b["assignee"]]:
                holders[n].append(b["id"])
    stops, moving, backlog = [], [], Counter()
    for b in work:
        if only and b["id"] not in only:
            continue
        r = S.classify(b, city, blockers, holders, t, work_by_id)
        if not S.counts(b, r, city):
            continue
        if r is None:
            backlog[b["rig"]] += 1
        else:
            r["actor"] = city.alias(r["actor"])
            (moving if r["class"] in S.MOVING else stops).append(r)
    if not only:
        for s in S.seat_stops(city, {"resets": {}, "stopped": city.stopped}, t):
            if s["id"] in city.stopped:  # seen to stop; a seat with no event may not have existed then
                stops.append(s)
        for n, v in city.resets.items():
            if len(v) >= S.RESETS and ("/" in n or "." in n):
                stops.append({"kind": "seat", "id": n, "rig": S.rig_of(n), "title": n, "class": "seat_resets",
                              "actor": city.alias(S.ROUTER), "why": f"reset as stalled {len(v)} times that day",
                              "since": q.iso(v[0]), "age_h": S.hrs(v[0], t)})
        if city.load is not None and city.load > S.LOAD_STOP:
            stops.append({"kind": "host", "id": "host", "rig": None, "title": "host", "class": "host_load",
                          "actor": "unknown (no process history)", "why": f"load {city.load} (sysstat)",
                          "since": q.iso(t), "age_h": 0.0})
    return {"at": q.iso(t), "stops": stops, "moving": moving, "backlog": dict(backlog), "load": city.load,
            "seats": [{"seat": s["alias"], "state": s["state"]} for s in city.sessions]}


def setup(t0, t1):
    S.adopt_conventions()
    beads = q.Beads()
    rigs, hq = q.discover_rigs()
    stores = rigs + [h for h in hq if h not in rigs]
    errors = []
    now_city = S.City(rigs, errors, {}, datetime.now(UTC))
    clock = q.EventClock.from_detail(beads, (q.load_json(os.path.join(q.OUT_DIR, "quality.json"), {}).get("sources") or {})
                                     .get("events_clock")) or q.EventClock(beads, stores, t0 - timedelta(days=30))
    beads_all = load_beads(beads, stores, t0, t1, clock)
    others = other_blockers(beads, stores, beads_all)
    changes, resets = session_events(t0, t1)
    caps = caps_by_commit(t0, t1)
    loads = sar_loads(t0, t1)
    q.log(f"{len(beads_all)} beads, {len(changes)} session names, {len(caps)} city.toml versions, {len(loads)} load samples"
          + (f"; errors: {errors}" if errors else ""))
    return beads_all, now_city, changes, resets, caps, loads, others


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--at", action="append", default=[])
    ap.add_argument("--bead", action="append", default=[])
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--seat", action="append", default=[], help="also print these seats' state (with --at)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.at:
        ts = sorted(q.as_utc(a) for a in args.at)
        data = setup(ts[0], ts[-1])
        for t in ts:
            c = census_at(t, *data, only=set(args.bead) or None)
            if args.json:
                print(json.dumps(c, default=str))
                continue
            print(f"===== census at {c['at']}: load {c['load']}, {len(c['stops'])} stops, {len(c['moving'])} moving, "
                  f"backlog {sum(c['backlog'].values())}")
            for x in c["seats"]:
                if any(w in x["seat"] for w in args.seat):
                    print(f"  seat {x['seat']}: {x['state']}")
            for s in c["stops"] + c["moving"]:
                tag = s["class"] if s["class"] not in S.MOVING else f"({s['class']})"
                print(f"  {tag:18} {s['id'][:30]:30} {(s.get('rig') or ''):18} actor={s['actor'][:32]:32} "
                      f"age={s['age_h'] if s['age_h'] is not None else '?':>7} h  {s['why'][:110]}")
        return 0
    if args.frm:
        d0 = q.day_bounds(args.frm)[0]
        d1 = q.day_bounds(args.to or args.frm)[1]
        d1 = min(d1, datetime.now(UTC))
        data = setup(d0, d1)
        step = timedelta(minutes=S.INTERVAL_MIN)
        days = defaultdict(lambda: defaultdict(float))
        t = d0
        while t < d1:
            c = census_at(t, *data, only=set(args.bead) or None)
            for s in c["stops"]:
                days[q.day_of(t)][s["class"]] += step.total_seconds() / 3600
            t += step
        keys = [k for k, _, _ in S.CLASSES if any(v.get(k) for v in days.values())]
        out = {d: {k: round(v.get(k, 0), 1) for k in keys} for d, v in sorted(days.items())}
        if args.json:
            print(json.dumps(out))
        else:
            print("day        " + " ".join(f"{k[:12]:>12}" for k in keys))
            for d, v in out.items():
                print(f"{d} " + " ".join(f"{v[k]:>12}" for k in keys))
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
