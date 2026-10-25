#!/usr/bin/env python3
"""Quality metrics for a Gas City, per rig and per local day (the `quality` pack's order).

Sources: each rig's beads database (plain SQL on the city's Dolt server), GitHub (`gh`), the
city event log ($GC_CITY_PATH/.gc/events.jsonl plus its rotated archives) and, optionally, a
scores.json that already prices tokens. Writes OUT_DIR/history/<day>.json (one file per local
day), OUT_DIR/quality.json (7- and 28-day windows and daily series) and OUT_DIR/index.html.
Every setting is an environment variable (the order's [order.env], overridable per city);
README.md defines each metric, its source and its blind spots.

    quality.py                 compute the window and write the outputs
    quality.py --dry-run       compute and print a summary; write nothing (caches included)
    quality.py --bead ID       show how one landed bead was counted (for spot checks)
"""
import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median

try:
    from zoneinfo import ZoneInfo
except ImportError:  # python < 3.9
    ZoneInfo = None

UTC = timezone.utc
CACHE_VERSION = 1
T0 = time.monotonic()


# ---------- config ----------

def env(name, default=""):
    v = os.environ.get(name, "").strip()
    return v if v else default


def env_list(name, default=""):
    return [x.strip() for x in env(name, default).split(",") if x.strip()]


def find_city(start):
    d = os.path.abspath(start)
    while True:
        if os.path.isfile(os.path.join(d, "city.toml")):
            return d
        if os.path.dirname(d) == d:
            return ""
        d = os.path.dirname(d)


CITY = env("GC_CITY_PATH") or env("GC_CITY") or find_city(os.getcwd())
PACK_DIR = env("PACK_DIR", os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OUT_DIR = env("QUALITY_OUT_DIR", os.path.join(CITY, ".gc", "runtime", "quality"))
STATE_DIR = env("QUALITY_STATE_DIR", os.path.join(CITY, ".gc", "runtime", "quality-cache"))
DAYS = int(env("QUALITY_DAYS", "28"))
FOLLOW_DAYS = int(env("QUALITY_FOLLOW_DAYS", "14"))  # horizon for reopens and fix-of-a-fix
ONLY_RIGS = env_list("QUALITY_RIGS")
LANDED_RESULTS = env_list("QUALITY_LANDED_RESULTS", "merged,pull_request")
VERIFY_OK = env_list("QUALITY_VERIFY_OK_LABELS", "verify:live")
VERIFY_FAIL = env_list("QUALITY_VERIFY_FAIL_LABELS", "verify:failed")
FOF_TYPES = env_list("QUALITY_FOF_TYPES", "bug")
# A follow-up that says it predates the landed work is not a fix-of-a-fix.
FOF_EXCLUDE = re.compile(env("QUALITY_FOF_EXCLUDE", r"\bpre-?existing\b|\bolder bug\b|\bnot caused by\b|"
                                                    r"\bold build\b[^.\n]{0,80}\bsame\b"), re.I)
# A follow-up that names a landed bead counts only when the sentence naming it says the bead shipped,
# caused or left the problem (high precision, undercounts; README has the hand-checked sample).
FOF_CUES = re.compile(env("QUALITY_FOF_CUES",
                          r"\b(shipped|merged|landed|since|after|introduced|caused|regress\w*|broke|broken|made|"
                          r"added|rewrote|changed|fixed|new in|follow-?up|filed under|comes with|deploy\w*|"
                          r"roll(ed)?|did not|didn.t|does not|doesn.t|still|not done|missed|reopen\w*|promises)\b"), re.I)
FOF_NOT = re.compile(env("QUALITY_FOF_NOT", r"\b(as in|\w+ family|ruling|out of scope|flagged by|for example|e\.g\.)"), re.I)
# A bounce whose reason matches this was caused by the gate, not by the change (also counted as friction).
GATE_BOUNCE = re.compile(env("QUALITY_GATE_BOUNCE_RE", r"\bpre-?existing\b"), re.I)
HUMAN_PREFIXES = env_list("QUALITY_HUMAN_PREFIXES", "HUMAN:")
HUMAN_DUE_KEY = env("QUALITY_HUMAN_DUE_KEY", "human.due")
FRICTION_EVENTS = env_list("QUALITY_FRICTION_EVENTS",
                           "order.failed,session.reset_stalled,session.cold_start_timeout,"
                           "session.work_query_failed,bead.dead_assignee_reopened,"
                           "supervisor.fs_pressure.skipped_tick")
SCORES_JSON = env("QUALITY_SCORES_JSON")
REPOS = dict(x.split("=", 1) for x in env_list("QUALITY_REPOS") if "=" in x)  # rig=owner/repo|none
USE_GH = env("QUALITY_GH", "auto").lower() not in ("off", "no", "0", "false")
POST_RENDER = env("QUALITY_POST_RENDER")
GC = env("GC_BIN", shutil.which("gc") or "gc")


def log(msg):
    print(f"[quality {time.monotonic() - T0:5.1f}s] {msg}", file=sys.stderr, flush=True)


# ---------- time ----------

def local_zone():
    name = env("QUALITY_TZ") or os.environ.get("TZ", "")
    if not name:
        link = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in link:
            name = link.split("zoneinfo/", 1)[1]
    if name and ZoneInfo:
        try:
            return ZoneInfo(name), name
        except Exception:
            pass
    tz = datetime.now().astimezone().tzinfo
    return tz, str(tz)


LOCAL, LOCAL_NAME = local_zone()


def as_utc(v, naive_tz=UTC, force_tz=False):
    """A DB or JSON timestamp as an aware UTC datetime. Naive values are read in naive_tz;
    force_tz also discards any zone the value claims (Dolt labels local event times as UTC)."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        d = v
    else:
        s = str(v).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if "T" not in s and " " in s:
            s = s.replace(" ", "T", 1)
        s = re.sub(r"(\.\d{6})\d+", r"\1", s)  # Go writes nanoseconds
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            return None
    if force_tz:
        d = d.replace(tzinfo=None)
    if d.tzinfo is None:
        d = d.replace(tzinfo=naive_tz)
    return d.astimezone(UTC)


def day_of(d):
    return d.astimezone(LOCAL).strftime("%Y-%m-%d")


def day_bounds(day):
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=LOCAL)
    end = (start + timedelta(days=1, hours=2)).replace(hour=0)  # DST-safe next midnight
    return start.astimezone(UTC), end.astimezone(UTC)


def hours(a, b):
    if a is None or b is None or b < a:
        return None
    return round((b - a).total_seconds() / 3600, 3)


def iso(d):
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if d else None


def sqlts(d):
    return d.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


# ---------- discovery ----------

class Rig:
    def __init__(self, name, path, prefix, hq, branch):
        self.name, self.path, self.prefix, self.hq, self.branch = name, path, prefix, hq, branch
        self.db = prefix
        try:
            with open(os.path.join(path, ".beads", "metadata.json")) as f:
                self.db = json.load(f).get("dolt_database") or prefix
        except (OSError, ValueError):
            pass
        self.repo = REPOS.get(name) or github_repo(path)
        if self.repo in ("none", "-"):
            self.repo = None


def github_repo(path):
    try:
        url = subprocess.run(["git", "-C", path, "remote", "get-url", "origin"], capture_output=True,
                             text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url)
    return f"{m.group(1)}/{m.group(2)}" if m else None


def discover_rigs():
    cmd = [GC, "rig", "list", "--json"] + (["--city", CITY] if CITY else [])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if out.returncode != 0:
        sys.exit(f"quality: `{' '.join(cmd)}` failed: {out.stderr.strip()[:300]}")
    rigs = []
    for r in json.loads(out.stdout).get("rigs", []):
        if r.get("beads") != "initialized" or (ONLY_RIGS and r["name"] not in ONLY_RIGS):
            continue
        rigs.append(Rig(r["name"], r["path"], r.get("prefix") or r["name"], bool(r.get("hq")),
                        r.get("default_branch") or "main"))
    return rigs


def dolt_port():
    for k in ("QUALITY_DOLT_PORT", "GC_DOLT_PORT", "BEADS_DOLT_SERVER_PORT"):
        if env(k).isdigit():
            return int(env(k))
    try:
        with open(os.path.join(CITY, ".beads", "dolt-server.port")) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


# ---------- beads ----------

class Beads:
    """Read-only SQL on each rig's beads database: pymysql straight to the city's Dolt server
    when it is importable (milliseconds per query), else `bd sql --json` per query."""

    def __init__(self):
        self.conn, self.mode = None, "bd sql"
        port = dolt_port()
        try:
            import pymysql
            import pymysql.cursors
            if port:
                self.conn = pymysql.connect(host=env("QUALITY_DOLT_HOST", "127.0.0.1"), port=port,
                                            user=env("QUALITY_DOLT_USER", "root"),
                                            password=env("QUALITY_DOLT_PASSWORD", ""), autocommit=True,
                                            connect_timeout=10, read_timeout=300,
                                            cursorclass=pymysql.cursors.DictCursor)
                self.mode = f"pymysql 127.0.0.1:{port}"
        except Exception as e:  # missing module or no server: fall back to bd
            log(f"pymysql not used ({e.__class__.__name__}: {e}); falling back to bd sql")

    def rows(self, rig, sql, args=()):
        if self.conn:
            with self.conn.cursor() as cur:
                cur.execute(sql.replace("{T}", f"`{rig.db}`."), args)
                return list(cur.fetchall())
        q = sql.replace("{T}", "") % tuple(sql_literal(a) for a in args)
        out = subprocess.run(["bd", "sql", "--json", q], capture_output=True, text=True, timeout=300,
                             env={**os.environ, "BEADS_DIR": os.path.join(rig.path, ".beads")})
        if out.returncode != 0:
            raise RuntimeError(f"bd sql on {rig.name}: {out.stderr.strip()[:300]}")
        return json.loads(out.stdout or "[]") or []


def sql_literal(v):
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return str(v)
    return "'" + str(v).replace("\\", "\\\\").replace("'", "''") + "'"


def placeholders(n):
    return ",".join(["%s"] * n)


def chunks(xs, n=400):
    xs = list(xs)
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def jload(v):
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v) if v else {}
    except (TypeError, ValueError):
        return {}


def meta_of(value):
    """metadata of an events.old_value/new_value JSON (a full or partial issue)."""
    m = jload(value).get("metadata")
    return jload(m) if not isinstance(m, dict) else m


class EventClock:
    """Instant of each events.created_at. bd up to 1.2 left it to Dolt's CURRENT_TIMESTAMP (the
    server's local time, no zone); bd 1.3 writes UTC. A city that upgraded has both in its window.
    Each 'created' event read against its issue's created_at (true UTC) is an anchor for one zone,
    and consecutive anchors of one zone make a run. An event is read in the zone its run allows;
    where two readings stay possible (between two runs, the repeated DST hour) the bead's own Dolt
    commits (true UTC) pick one, else the local reading is taken and counted as guessed."""

    ANCHOR_S = 120          # a 'created' event within 2 min of issues.created_at
    COMMIT_S = (-1.0, 5.0)  # a bead's commit lands 0-5 s after its event (event times are rounded)
    COMMIT_MARGIN_S = 1.0   # with commits near both readings, the nearer must be this much nearer

    def __init__(self, beads, rigs, since):
        self.beads = beads
        self.zones = {"local": LOCAL, "UTC": UTC}
        self.names = {"local": LOCAL_NAME, "UTC": "UTC"}
        self.runs, self.anchors, self.forced = [], 0, None
        self.counts = defaultdict(int)
        forced = env("QUALITY_EVENTS_TZ")
        if forced and (forced.upper() == "UTC" or ZoneInfo):
            self.zones = {"forced": UTC if forced.upper() == "UTC" else ZoneInfo(forced)}
            self.forced = f"{forced} (QUALITY_EVENTS_TZ)"
            return
        anchors, off = [], []
        for rig in rigs:
            try:
                rows = beads.rows(rig, "SELECT i.created_at AS iss, e.created_at AS ev FROM {T}events e "
                                       "JOIN {T}issues i ON i.id = e.issue_id "
                                       "WHERE e.event_type = 'created' AND i.created_at >= %s", (sqlts(since),))
            except Exception as e:
                log(f"events clock anchors on {rig.name}: {e}")
                continue
            for r in rows:
                iss = as_utc(r["iss"])
                hits = {z for z in self.zones if any(abs((t - iss).total_seconds()) <= self.ANCHOR_S
                                                     for t in self.readings(r["ev"], z))}
                if len(hits) == 1:
                    anchors.append((iss, hits.pop()))
                elif not hits:
                    off.append((as_utc(r["ev"], UTC, True) - iss).total_seconds())
        if not anchors and off:  # the server's zone is neither: one fixed offset, as measured
            o = round(median(off) / 900) * 900
            self.zones = {"fixed": timezone(timedelta(seconds=o))}
            self.forced = f"fixed {o / 3600:+.2f}h (measured on {len(off)} beads)"
            return
        self.anchors = len(anchors)
        for t, z in sorted(anchors):
            if self.runs and self.runs[-1][0] == z:
                self.runs[-1][2] = t
            else:
                self.runs.append([z, t, t])
        if not self.runs:
            self.runs = [["local", None, None]]

    def readings(self, raw, zone):
        """Instants a raw value can mean in one zone: two in a repeated local hour, else one."""
        wall = as_utc(raw, UTC, True)
        if wall is None:
            return []
        wall = wall.replace(tzinfo=None)
        return sorted({wall.replace(tzinfo=self.zones[zone], fold=f).astimezone(UTC) for f in (0, 1)})

    def allowed(self, t):
        """Zones the timeline allows at instant t: a run's own zone, both zones between two runs."""
        runs = self.runs
        if len(runs) == 1 or t < runs[0][1]:
            return {runs[0][0]}
        for i, (z, first, last) in enumerate(runs):
            if t <= last:
                return {z} if t >= first else {z, runs[i - 1][0]}
        return {runs[-1][0]}

    def resolve(self, rig, evs):
        """Set e['at'] (aware UTC, None if unreadable) and e['how'] on events of one rig."""
        if self.forced:
            for e in evs:
                e["at"] = as_utc(e["created_at"], next(iter(self.zones.values())), True)
                e["how"] = "forced"
            return
        pending = []
        for e in evs:
            cands = [(z, t) for z in self.zones for t in self.readings(e["created_at"], z)]
            ok = [(z, t) for z, t in cands if z in self.allowed(t)] or cands
            if len({t for _, t in ok}) <= 1:
                e["at"], e["zone"], e["how"] = (ok[0][1], ok[0][0], "timeline") if ok else (None, None, "unreadable")
                self.counts[e["how"]] += 1
            else:
                e["_cands"] = ok
                pending.append(e)
        if not pending:
            return
        commits = self.commits(rig, pending)
        lo, hi = self.COMMIT_S
        for e in pending:
            cands, dates, hits = e.pop("_cands"), commits.get(e["issue_id"], []), []
            for z, t in cands:
                near = [abs((d - t).total_seconds()) for d in dates[bisect_left(dates, t + timedelta(seconds=lo)):
                                                                   bisect_right(dates, t + timedelta(seconds=hi))]]
                if near:
                    hits.append((min(near), z, t))
            hits.sort()
            if hits:  # several hits: the nearest, a guess unless it is clearly nearer than the next
                _, e["zone"], e["at"] = hits[0]
            else:
                e["zone"], e["at"] = next((c for c in cands if c[0] == "local"), cands[0])
            e["how"] = "commit" if hits and (len(hits) == 1 or hits[1][0] - hits[0][0] >= self.COMMIT_MARGIN_S) \
                else "guessed"
            self.counts[e["how"]] += 1

    def commits(self, rig, pending):
        """Commit dates (UTC) per bead id named in a commit's first line, near the pending events."""
        ts = sorted(t for e in pending for _, t in e["_cands"])
        spans = []
        for t in ts:
            if spans and (t - spans[-1][1]).total_seconds() <= 600:
                spans[-1][1] = t
            else:
                spans.append([t, t])
        ids = {e["issue_id"] for e in pending}
        where = " OR ".join(["(date BETWEEN %s AND %s)"] * len(spans))
        args = [sqlts(x) for a, b in spans for x in (a - timedelta(seconds=10), b + timedelta(seconds=10))]
        out = defaultdict(list)
        try:
            rows = self.beads.rows(rig, f"SELECT date, message FROM {{T}}dolt_log WHERE {where}", tuple(args))
        except Exception as e:
            log(f"events clock: dolt_log on {rig.name}: {e}")
            return out
        for r in rows:
            d = as_utc(r["date"])
            for tok in set(re.findall(r"[\w.-]+", str(r["message"]).split("\n", 1)[0])) & ids:
                out[tok].append(d)
        for v in out.values():
            v.sort()
        return out

    def note(self):
        if self.forced:
            return self.forced
        if self.runs[0][1] is None:
            return f"{LOCAL_NAME} (unchecked: no events)"
        c = self.counts
        tail = f"{c['timeline']} event(s) by timeline, {c['commit']} by Dolt commit, {c['guessed']} guessed"
        if len(self.runs) == 1:
            return f"{self.names[self.runs[0][0]]} (checked on {self.anchors} beads; {tail})"
        parts = []
        for i, (z, first, last) in enumerate(self.runs):
            span = ([f"from {iso(first)}"] if i else []) + ([f"until {iso(last)}"] if i < len(self.runs) - 1 else [])
            parts.append(f"{self.names[z]} {' '.join(span)}")
        return f"{', '.join(parts)} ({self.anchors} anchor beads; {tail})"

    def detail(self):
        return {"runs": [{"zone": self.names.get(z, z), "first": iso(a), "last": iso(b)} for z, a, b in self.runs],
                "anchors": self.anchors, "forced": self.forced, "events": dict(self.counts)}


def is_landed(meta):
    return str(meta.get("merge_result") or "") in LANDED_RESULTS


def collect_rig(beads, rig, start, now, clock):
    """Landed beads of one rig since `start`, with everything the bead-level metrics need."""
    landed = {}
    for r in beads.rows(rig, "SELECT id, title, issue_type, created_at, started_at, closed_at, "
                             "CAST(metadata AS CHAR) AS metadata FROM {T}issues "
                             "WHERE status = 'closed' AND closed_at >= %s", (sqlts(start),)):
        meta = jload(r["metadata"])
        if not is_landed(meta):
            continue
        closed = as_utc(r["closed_at"])
        landed[r["id"]] = {
            "id": r["id"], "rig": rig.name, "title": r["title"], "type": r["issue_type"],
            "created": as_utc(r["created_at"]), "started": as_utc(r["started_at"]), "closed": closed,
            "day": day_of(closed), "merge_result": meta.get("merge_result"),
            "pr_url": meta.get("pr_url") or meta.get("validation_pr") or None,
            "branch": meta.get("branch") or meta.get("gc.work_branch") or None,
            "bounces": [], "closes": [], "reopens": [], "first_claim_event": None,
            "verdict": None, "verified_at": None, "labels": [], "fof": [], "pr": None,
        }
    ids = list(landed)
    rr_like, labels = "%rejection_reason%", VERIFY_OK + VERIFY_FAIL
    evs = []
    for part in chunks(ids):
        evs += beads.rows(rig, "SELECT issue_id, event_type, actor, created_at, comment, "
                               "CASE WHEN new_value LIKE %s THEN new_value END AS nv, "
                               "CASE WHEN event_type = 'reopened' OR old_value LIKE %s THEN old_value END AS ov "
                               f"FROM {{T}}events WHERE issue_id IN ({placeholders(len(part))}) AND "
                               "(event_type IN ('claimed', 'closed', 'reopened', 'label_added') OR new_value LIKE %s)",
                          (rr_like, rr_like, *part, rr_like))
        lab_rows = beads.rows(rig, f"SELECT issue_id, label FROM {{T}}labels WHERE issue_id IN "
                                   f"({placeholders(len(part))}) AND label IN ({placeholders(len(labels))})",
                              (*part, *labels))
        for r in lab_rows:
            landed[r["issue_id"]]["labels"].append(r["label"])
    clock.resolve(rig, evs)
    for e in sorted((e for e in evs if e["at"]), key=lambda e: e["at"]):
        b, at, et = landed[e["issue_id"]], e["at"], e["event_type"]
        new_rr = str(meta_of(e["nv"]).get("rejection_reason") or "").strip() if e["nv"] else ""
        old_rr = str(meta_of(e["ov"]).get("rejection_reason") or "").strip() if e["ov"] else ""
        if new_rr and new_rr != old_rr:
            b["bounces"].append({"at": iso(at), "actor": e["actor"], "reason": new_rr[:160],
                                 "cause": "gate" if GATE_BOUNCE.search(new_rr) else "change"})
        if et == "claimed" and b["first_claim_event"] is None:
            b["first_claim_event"] = at
        elif et == "closed":
            b["closes"].append(at)
        elif et == "reopened":
            prev = b["closes"][-1] if b["closes"] else None
            if is_landed(meta_of(e["ov"])) and prev and at - prev <= timedelta(days=FOLLOW_DAYS):
                b["reopens"].append({"at": iso(at), "actor": e["actor"],
                                     "after_hours": hours(prev, at)})
        elif et == "label_added":
            lab = (e["comment"] or "").replace("Added label:", "").strip()
            if lab in VERIFY_OK and b["verified_at"] is None:
                b["verified_at"] = at
    for b in landed.values():
        if any(l in VERIFY_FAIL for l in b["labels"]):
            b["verdict"] = "failed"
        elif any(l in VERIFY_OK for l in b["labels"]):
            b["verdict"] = "ok"
        b["claimed"] = b["started"] or b["first_claim_event"]
    return landed


def collect_followups(beads, rig, start):
    """Beads of a fix-of-a-fix type created since `start`, with their discovered-from targets."""
    if not FOF_TYPES:
        return []
    rows = beads.rows(rig, "SELECT id, issue_type, title, description, created_at, created_by FROM {T}issues "
                           f"WHERE created_at >= %s AND issue_type IN ({placeholders(len(FOF_TYPES))})",
                      (sqlts(start), *FOF_TYPES))
    deps = defaultdict(set)
    for r in beads.rows(rig, "SELECT issue_id, depends_on_issue_id AS dep, depends_on_external AS ext "
                             "FROM {T}dependencies WHERE type = 'discovered-from'"):
        for t in (r["dep"], r["ext"]):
            if t:
                deps[r["issue_id"]].add(str(t).split(":")[-1])
    return [{"id": r["id"], "rig": rig.name, "title": r["title"], "text": f"{r['title']}\n{r['description']}",
             "created": as_utc(r["created_at"]), "by": r["created_by"], "from": deps.get(r["id"], set())}
            for r in rows]


def collect_human(beads, rig, start, now):
    """Human-gate beads open at some point since `start`."""
    if not HUMAN_PREFIXES and not HUMAN_DUE_KEY:
        return []
    conds, args = [], []
    for p in HUMAN_PREFIXES:
        conds.append("title LIKE %s")
        args.append(p.replace("%", r"\%").replace("_", r"\_") + "%")
    if HUMAN_DUE_KEY:
        conds.append("CAST(metadata AS CHAR) LIKE %s")
        args.append(f'%"{HUMAN_DUE_KEY}"%')
    rows = beads.rows(rig, "SELECT id, title, status, issue_type, created_at, closed_at, updated_at, due_at, "
                           "CAST(metadata AS CHAR) AS metadata FROM {T}issues WHERE created_at <= %s AND "
                           f"(closed_at IS NULL OR closed_at >= %s) AND ({' OR '.join(conds)})",
                      (sqlts(now), sqlts(start), *args))
    gates = []
    for r in rows:
        if r["issue_type"] in ("message", "molecule", "session", "event"):
            continue
        meta = jload(r["metadata"])
        closed = as_utc(r["closed_at"]) or (as_utc(r["updated_at"]) if r["status"] == "closed" else None)
        gates.append({"id": r["id"], "rig": rig.name, "title": r["title"][:120], "created": as_utc(r["created_at"]),
                      "closed": closed, "due": as_utc(meta.get(HUMAN_DUE_KEY)) or as_utc(r["due_at"])})
    return gates


# ---------- GitHub: first-pass CI ----------

class GitHub:
    """Merged PRs per repo and whether each PR's first head passed the branch's required checks.
    A merged PR never changes, so its verdict is cached for good; a daily run only fetches the
    PRs merged since the last one."""

    OK = ("success", "neutral", "skipped")

    def __init__(self, state, dry):
        self.state, self.dry, self.calls = state, dry, 0
        self.cache = state.setdefault("github", {})
        self.error = None

    def api(self, args):
        self.calls += 1
        out = subprocess.run(["gh"] + args, capture_output=True, text=True, timeout=180)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:300] or f"gh exit {out.returncode}")
        return out.stdout

    def required_checks(self, repo, branch):
        try:
            rules = json.loads(self.api(["api", f"repos/{repo}/rules/branches/{branch}"]) or "[]")
            ctx = [c["context"] for r in rules if r.get("type") == "required_status_checks"
                   for c in r.get("parameters", {}).get("required_status_checks", [])]
            if ctx:
                return sorted(set(ctx)), "ruleset"
        except Exception:
            pass
        try:
            prot = json.loads(self.api(["api", f"repos/{repo}/branches/{branch}/protection/required_status_checks"]))
            ctx = list(prot.get("contexts") or []) + [c["context"] for c in prot.get("checks") or []]
            if ctx:
                return sorted(set(ctx)), "branch protection"
        except Exception:
            pass
        return None, "none (all checks on the head count)"

    def first_heads(self, repo, since):
        """head_branch -> [(created_at, sha)] of pull_request workflow runs since `since`."""
        out = self.api(["api", "--paginate",
                        f"repos/{repo}/actions/runs?event=pull_request&created=>={since:%Y-%m-%d}&per_page=100",
                        "--jq", ".workflow_runs[] | [.head_branch, .head_sha, .created_at] | @tsv"])
        heads = defaultdict(list)
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 3:
                heads[parts[0]].append((as_utc(parts[2]), parts[1]))
        for v in heads.values():
            v.sort()
        return heads

    def verdict(self, repo, sha, required):
        runs = json.loads(self.api(["api", f"repos/{repo}/commits/{sha}/check-runs?per_page=100"]))["check_runs"]
        first = {}
        for c in sorted(runs, key=lambda c: c["id"]):  # a re-run of a red check is not a first pass
            first.setdefault(c["name"], c.get("conclusion") or c.get("status"))
        names = required or sorted(first)
        if required and any(n not in first for n in names):  # required contexts can be commit statuses
            st = json.loads(self.api(["api", f"repos/{repo}/commits/{sha}/status"]))
            for s in st.get("statuses", []):
                first.setdefault(s["context"], s["state"])
        if not names:
            return None, [], "no checks on the first head"
        failed = [n for n in names if first.get(n) not in self.OK]
        return not failed, failed, None

    def repo_prs(self, repo, branch, since):
        key = repo.lower()
        c = self.cache.setdefault(key, {"v": CACHE_VERSION, "prs": {}})
        if c.get("v") != CACHE_VERSION:
            c.clear()
            c.update({"v": CACHE_VERSION, "prs": {}})
        prs = json.loads(self.api(["pr", "list", "--repo", repo, "--state", "merged", "--search",
                                   f"merged:>={since:%Y-%m-%d}", "--limit", "1000",
                                   "--json", "number,headRefName,createdAt,mergedAt,url"]) or "[]")
        required, req_src = self.required_checks(repo, branch)
        todo = [p for p in prs if str(p["number"]) not in c["prs"]]
        if todo:
            earliest = min(as_utc(p["createdAt"]) for p in todo) - timedelta(days=1)
            heads = self.first_heads(repo, earliest)
            for p in todo:
                opened, merged = as_utc(p["createdAt"]), as_utc(p["mergedAt"])
                runs = [(at, sha) for at, sha in heads.get(p["headRefName"], [])
                        if opened - timedelta(minutes=10) <= at <= merged]
                rec = {"number": p["number"], "url": p["url"], "head_ref": p["headRefName"],
                       "created": iso(opened), "merged": iso(merged), "first_sha": None,
                       "first_pass": None, "failed": [], "required": required, "note": None}
                if not runs:
                    rec["note"] = "no pull_request workflow run found for the first head"
                else:
                    rec["first_sha"] = runs[0][1]
                    rec["heads"] = len({sha for _, sha in runs})
                    try:
                        rec["first_pass"], rec["failed"], rec["note"] = self.verdict(repo, runs[0][1], required)
                    except Exception as e:
                        rec["note"] = f"check-runs: {e}"
                        c["prs"].pop(str(p["number"]), None)
                        continue  # not cached: try again next run
                c["prs"][str(p["number"])] = rec
        c["required"], c["required_source"] = required, req_src
        return [c["prs"][str(p["number"])] for p in prs if str(p["number"]) in c["prs"]], required, req_src


# ---------- city event log: machine friction ----------

ARCHIVE_RE = re.compile(r"^events\.jsonl\.archive-(\d{8}T\d{6}Z)-seq-\d+-\d+\.gz$")


def friction_rig(e, rig_names, prefixes):
    p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
    for s in (e.get("subject") or "", p.get("session_name") or "", p.get("bead_id") or ""):
        for sep in ("--", "/"):
            if sep in s and s.split(sep, 1)[0] in rig_names:
                return s.split(sep, 1)[0]
        for prefix, name in prefixes:
            if s.startswith(prefix + "-"):
                return name
    return "city"


def scan_events(path, offset, rig_names, prefixes, days):
    """Count friction events in one log file from byte `offset`; returns the end offset."""
    types = [t.encode() for t in FRICTION_EVENTS]
    if not types:
        return offset
    pat = re.compile(rb'"type":"(' + b"|".join(re.escape(t) for t in types) + rb')"')
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rb") as f:
        if offset:
            f.seek(offset)
        for line in f:
            if not line.endswith(b"\n"):
                break  # a line still being written: read it next time
            offset += len(line)
            if not pat.search(line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            t = e.get("type")
            if t == "order.failed":
                key = f"order.failed:{e.get('subject')}"
            else:
                key = t
            at = as_utc(e.get("ts"))
            if at is None:
                continue
            bucket = days.setdefault(day_of(at), {}).setdefault(friction_rig(e, rig_names, prefixes), {})
            bucket[key] = bucket.get(key, 0) + 1
    return offset


def collect_friction(state, rigs, start):
    gcdir = os.path.join(CITY, ".gc")
    cache = state.setdefault("events", {})
    if cache.get("v") != CACHE_VERSION or cache.get("keys") != FRICTION_EVENTS:
        cache.clear()
        cache.update({"v": CACHE_VERSION, "keys": FRICTION_EVENTS, "files": {}})
    files = cache["files"]
    rig_names = {r.name for r in rigs}
    prefixes = sorted(((r.prefix, r.name) for r in rigs), key=lambda x: -len(x[0]))
    try:
        names = os.listdir(gcdir)
    except OSError:
        return None, "no city event log"
    wanted = []
    for n in names:
        m = ARCHIVE_RE.match(n)
        if m and datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) >= start:
            wanted.append(n)
    for n in sorted(wanted):
        if n not in files:
            days = {}
            scan_events(os.path.join(gcdir, n), 0, rig_names, prefixes, days)
            files[n] = {"days": days}
    live = os.path.join(gcdir, "events.jsonl")
    if os.path.exists(live):
        st = os.stat(live)
        rec = files.get("events.jsonl")
        if not rec or rec.get("ino") != st.st_ino or rec.get("offset", 0) > st.st_size:
            rec = {"ino": st.st_ino, "offset": 0, "days": {}}
        rec["offset"] = scan_events(live, rec["offset"], rig_names, prefixes, rec["days"])
        files["events.jsonl"] = rec
    for n in list(files):  # forget archives that fell out of the window
        if n != "events.jsonl" and n not in wanted:
            del files[n]
    merged = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))
    for rec in files.values():
        for day, by_rig in rec["days"].items():
            for rig, counts in by_rig.items():
                for k, v in counts.items():
                    merged[day][rig][k] += v
    covered = min((m.group(1) for n in wanted for m in [ARCHIVE_RE.match(n)]), default=None)
    return merged, f"events.jsonl + {len(wanted)} archive(s)" + (f" (oldest rotated {covered})" if covered else "")


# ---------- cost (reused, not recomputed) ----------

def collect_cost():
    if not SCORES_JSON:
        return None, "QUALITY_SCORES_JSON not set"
    try:
        with open(SCORES_JSON) as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, f"{SCORES_JSON}: {e}"
    out, prev_usd = {}, 0.0
    for d in sorted(data.get("days", []), key=lambda d: d["day"]):
        g = d.get("gc") or {}
        usd, merged = float(g.get("usd") or 0), int(g.get("merged") or 0)
        # a day the usage feed (re)started part-way reads too low: count a day only when the
        # feed also had cost the day before
        ok = usd > 0 and merged > 0 and prev_usd > 0
        out[d["day"]] = {"usd": round(usd, 2), "merged": merged, "feed_restart": usd > 0 and prev_usd <= 0,
                         "per_landed": round(usd / merged, 2) if ok else None}
        prev_usd = usd
    return out, f"{SCORES_JSON} (generated {data.get('generated_at', '?')})"


# ---------- assembly ----------

def link_followups(landed_all, followups, prefixes):
    """Fix-of-a-fix: a follow-up bug names (or is discovered-from) a bead that landed in the
    FOLLOW_DAYS before the bug was filed."""
    # an id inside a path or a longer name (/x/hivemind-d87pp-eval/) is not a reference to the bead
    rx = re.compile(r"(?<![\w/.-])(?:" + "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
                    + r")-[a-z0-9]{3,}(?:\.\d+)*(?![\w/-])") if prefixes else None
    for f in followups:
        text = f["text"] or ""
        if FOF_EXCLUDE.search(text):
            continue
        named = set(f["from"])
        for m in rx.finditer(text) if rx else ():
            if sentence_says_caused(text, m.start(), m.end()):
                named.add(m.group(0))
        named.discard(f["id"])
        for bid in named:
            b = landed_all.get(bid)
            if b and b["closed"] and f["created"] and timedelta(0) <= f["created"] - b["closed"] <= timedelta(days=FOLLOW_DAYS):
                b["fof"].append({"id": f["id"], "title": f["title"][:120], "created": iso(f["created"]),
                                 "via": "discovered-from" if bid in f["from"] else "names it"})


def sentence_says_caused(text, i, j):
    a = max(text.rfind(". ", 0, i), text.rfind("\n", 0, i)) + 1
    ends = [x for x in (text.find(". ", j), text.find("\n", j)) if x != -1]
    s = text[a:min(ends) if ends else len(text)]
    return bool(FOF_CUES.search(s)) and not FOF_NOT.search(s)


def attach_prs(landed_all, prs_by_rig):
    by_url, by_branch = {}, {}
    for b in landed_all.values():
        if b["pr_url"]:
            by_url[b["pr_url"].rstrip("/")] = b
        if b["branch"]:
            by_branch[(b["rig"], b["branch"])] = b
    for rig, prs in prs_by_rig.items():
        for p in prs:
            b = by_url.get(p["url"].rstrip("/")) or by_branch.get((rig, p["head_ref"]))
            if b is None:
                m = re.search(r"([a-z0-9-]+-[a-z0-9]{3,}(?:\.\d+)*)$", p["head_ref"] or "")
                b = landed_all.get(m.group(1)) if m else None
            if b is not None and b["rig"] == rig:
                b.setdefault("prs", []).append(p)
    for b in landed_all.values():  # a reworked bead has several PRs: time to land runs from the first
        if b.get("prs"):
            b["prs"].sort(key=lambda p: p["created"])
            b["pr"] = b["prs"][0]


def bead_record(b):
    pr_opened = as_utc(b["pr"]["created"]) if b["pr"] else None
    return {
        "id": b["id"], "title": b["title"][:120], "type": b["type"], "landed": iso(b["closed"]),
        "bounces": len(b["bounces"]), "bounce_detail": b["bounces"],
        "verdict": b["verdict"], "reopened": b["reopens"],
        "post_merge_failed": b["verdict"] == "failed" or bool(b["reopens"]),
        "fix_of_a_fix": b["fof"],
        "pr": b["pr"]["number"] if b["pr"] else None,
        "prs": [p["number"] for p in b.get("prs", [])],
        "first_pass_ci": b["pr"]["first_pass"] if b["pr"] else None,
        "times": {"created": iso(b["created"]), "claimed": iso(b["claimed"]), "pr_opened": iso(pr_opened),
                  "merged": iso(b["closed"]), "verified": iso(b["verified_at"])},
        "hours": {"created_claimed": hours(b["created"], b["claimed"]),
                  "claimed_pr": hours(b["claimed"], pr_opened),
                  "claimed_merged": hours(b["claimed"], b["closed"]),
                  "merged_verified": hours(b["closed"], b["verified_at"]),
                  "created_merged": hours(b["created"], b["closed"])},
    }


SEGMENTS = ("created_claimed", "claimed_pr", "claimed_merged", "merged_verified", "created_merged")


def empty_rig_day():
    return {"landed": 0, "bounced": 0, "bounces": 0, "bounces_gate": 0, "checked": 0, "verify_ok": 0, "verify_failed": 0,
            "reopened": 0, "post_merge_failed": 0, "fof_beads": 0, "fof_bugs": 0,
            "prs_merged": 0, "prs_known": 0, "prs_first_pass": 0,
            "hours": {s: [] for s in SEGMENTS},
            "human": {"open_eod": 0, "past_due_eod": 0, "answered": 0, "answer_hours": []},
            "friction": {}, "beads": [], "prs": []}


def build_days(day_list, rigs, landed_all, prs_by_rig, gates, friction, cost, now):
    days = {}
    for day in day_list:
        rec = {"day": day, "partial": day == day_of(now), "rigs": {r.name: empty_rig_day() for r in rigs},
               "city": {"cost": (cost or {}).get(day), "friction": {}}}
        days[day] = rec
    for b in landed_all.values():
        if b["day"] not in days:
            continue
        d = days[b["day"]]["rigs"][b["rig"]]
        r = bead_record(b)
        d["beads"].append(r)
        d["landed"] += 1
        d["bounced"] += r["bounces"] > 0
        d["bounces"] += r["bounces"]
        d["bounces_gate"] += sum(x["cause"] == "gate" for x in r["bounce_detail"])
        d["checked"] += r["verdict"] is not None
        d["verify_ok"] += r["verdict"] == "ok"
        d["verify_failed"] += r["verdict"] == "failed"
        d["reopened"] += bool(r["reopened"])
        d["post_merge_failed"] += r["post_merge_failed"]
        d["fof_beads"] += bool(r["fix_of_a_fix"])
        d["fof_bugs"] += len(r["fix_of_a_fix"])
        for s in SEGMENTS:
            if r["hours"][s] is not None:
                d["hours"][s].append(r["hours"][s])
    for b in landed_all.values():  # a gate-caused bounce is machine friction on the day it happened
        for x in b["bounces"]:
            day = day_of(as_utc(x["at"]))
            if x["cause"] == "gate" and day in days:
                f = days[day]["rigs"][b["rig"]]["friction"]
                f["refinery.gate_bounce"] = f.get("refinery.gate_bounce", 0) + 1
    for rig, prs in prs_by_rig.items():
        for p in prs:
            day = day_of(as_utc(p["merged"]))
            if day in days:
                d = days[day]["rigs"][rig]
                d["prs_merged"] += 1
                d["prs_known"] += p["first_pass"] is not None
                d["prs_first_pass"] += p["first_pass"] is True
                d["prs"].append({k: p.get(k) for k in ("number", "head_ref", "first_sha", "first_pass", "failed", "note")})
    for day, rec in days.items():
        start, end = day_bounds(day)
        eod = min(end, now)
        for g in gates:
            h = rec["rigs"][g["rig"]]["human"]
            if g["created"] and g["created"] < eod and (g["closed"] is None or g["closed"] >= eod):
                h["open_eod"] += 1
                h["past_due_eod"] += bool(g["due"] and g["due"] < eod)
            if g["closed"] and start <= g["closed"] < end:
                h["answered"] += 1
                if g["created"]:
                    h["answer_hours"].append(hours(g["created"], g["closed"]))
        for rig, counts in ((friction or {}).get(day) or {}).items():
            target = rec["city"]["friction"] if rig == "city" or rig not in rec["rigs"] else rec["rigs"][rig]["friction"]
            for k, v in counts.items():
                target[k] = target.get(k, 0) + v
    return days


def pct(n, d):
    return round(100 * n / d, 1) if d else None


def quantile(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    i = (len(xs) - 1) * q
    lo = int(i)
    hi = min(lo + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (i - lo), 2)


def summarize(rig_days, city_days, follow_days, today):
    """One window (a list of per-day records of one rig, or 'all' merged) -> metric values."""
    s = defaultdict(int)
    hrs = {k: [] for k in SEGMENTS}
    ans = []
    for d in rig_days:
        for k in ("landed", "bounced", "bounces", "bounces_gate", "checked", "verify_ok", "verify_failed", "reopened",
                  "post_merge_failed", "fof_beads", "fof_bugs", "prs_merged", "prs_known", "prs_first_pass"):
            s[k] += d[k]
        for k in SEGMENTS:
            hrs[k] += d["hours"][k]
        ans += d["human"]["answer_hours"]
    last = rig_days[-1]["human"] if rig_days else {"open_eod": 0, "past_due_eod": 0}
    friction = defaultdict(int)
    for d in rig_days:
        for k, v in d["friction"].items():
            friction[k] += v
    for d in city_days:
        for k, v in d["city"]["friction"].items():
            friction[k] += v
    cost_days = [d["city"]["cost"] for d in city_days if d["city"]["cost"] and d["city"]["cost"]["per_landed"] is not None]
    usd, merged = sum(c["usd"] for c in cost_days), sum(c["merged"] for c in cost_days)
    return {
        "landed": s["landed"],
        "bounce_rate": pct(s["bounced"], s["landed"]), "bounced": s["bounced"], "bounces": s["bounces"],
        "bounces_gate": s["bounces_gate"],
        "post_merge_failure_rate": pct(s["post_merge_failed"], s["landed"]),
        "post_merge_failed": s["post_merge_failed"], "verify_failed": s["verify_failed"],
        "reopened": s["reopened"], "check_coverage": pct(s["checked"], s["landed"]), "checked": s["checked"],
        "fix_of_a_fix_rate": pct(s["fof_beads"], s["landed"]), "fof_beads": s["fof_beads"], "fof_bugs": s["fof_bugs"],
        "first_pass_ci_rate": pct(s["prs_first_pass"], s["prs_known"]), "prs_merged": s["prs_merged"],
        "prs_known": s["prs_known"], "prs_first_pass": s["prs_first_pass"],
        "time_to_land": {k: {"n": len(v), "median": quantile(v, 0.5), "p90": quantile(v, 0.9)} for k, v in hrs.items()},
        "human": {"open_now": last["open_eod"], "past_due_now": last["past_due_eod"], "answered": len(ans),
                  "answer_median_h": quantile(ans, 0.5), "answer_p90_h": quantile(ans, 0.9)},
        "friction_total": sum(friction.values()), "friction": dict(sorted(friction.items(), key=lambda x: -x[1])),
        "cost_per_landed": round(usd / merged, 2) if merged else None, "cost_days": len(cost_days),
        "cost_usd": round(usd, 2), "cost_merged": merged,
    }


def series_value(d, city, metric):
    if metric == "bounce_rate":
        return pct(d["bounced"], d["landed"])
    if metric == "post_merge_failure_rate":
        return pct(d["post_merge_failed"], d["landed"])
    if metric == "fix_of_a_fix_rate":
        return pct(d["fof_beads"], d["landed"])
    if metric == "first_pass_ci_rate":
        return pct(d["prs_first_pass"], d["prs_known"])
    if metric == "claimed_merged_median_h":
        return quantile(d["hours"]["claimed_merged"], 0.5)
    if metric == "created_merged_median_h":
        return quantile(d["hours"]["created_merged"], 0.5)
    if metric == "human_open_eod":
        return d["human"]["open_eod"]
    if metric == "human_past_due_eod":
        return d["human"]["past_due_eod"]
    if metric == "friction":
        return sum(d["friction"].values()) + (sum(city["friction"].values()) if city is not None else 0)
    if metric == "cost_per_landed":
        return (city["cost"] or {}).get("per_landed") if city is not None else None
    if metric == "landed":
        return d["landed"]
    return None


SERIES = ("landed", "bounce_rate", "post_merge_failure_rate", "fix_of_a_fix_rate", "first_pass_ci_rate",
          "claimed_merged_median_h", "created_merged_median_h", "human_open_eod", "human_past_due_eod",
          "friction", "cost_per_landed")


def merge_rig_days(days_list):
    """Sum per-rig day records into one ('all rigs')."""
    out = empty_rig_day()
    for d in days_list:
        for k, v in d.items():
            if isinstance(v, int):
                out[k] += v
        for k in SEGMENTS:
            out["hours"][k] += d["hours"][k]
        for k in ("open_eod", "past_due_eod", "answered"):
            out["human"][k] += d["human"][k]
        out["human"]["answer_hours"] += d["human"]["answer_hours"]
        for k, v in d["friction"].items():
            out["friction"][k] = out["friction"].get(k, 0) + v
    return out


def build_quality(days, day_list, rigs, sources, now):
    today = day_of(now)
    rig_names = [r.name for r in rigs]
    per_rig = {n: [days[d]["rigs"][n] for d in day_list] for n in rig_names}
    per_rig["all"] = [merge_rig_days([days[d]["rigs"][n] for n in rig_names]) for d in day_list]
    city_days = [days[d] for d in day_list]
    windows = {}
    for w in (7, DAYS):
        windows[str(w)] = {}
        for n, rd in per_rig.items():
            # rig windows carry the rig's own friction; the city-level rows (events with no rig,
            # cost) are only folded into 'all'
            windows[str(w)][n] = summarize(rd[-w:], city_days[-w:] if n == "all" else [], FOLLOW_DAYS, today)
    series = {n: {m: [series_value(d, days[day]["city"] if n == "all" else None, m) for d, day in zip(rd, day_list)]
                  for m in SERIES} for n, rd in per_rig.items()}
    active = [n for n in rig_names if windows[str(DAYS)][n]["landed"] or windows[str(DAYS)][n]["prs_merged"]
              or windows[str(DAYS)][n]["human"]["answered"] or windows[str(DAYS)][n]["human"]["open_now"]
              or windows[str(DAYS)][n]["friction_total"]]
    return {
        "generated_at": iso(now), "timezone": LOCAL_NAME, "days": day_list, "today": today,
        "follow_days": FOLLOW_DAYS, "maturing_from": day_of(now - timedelta(days=FOLLOW_DAYS)),
        "rigs": [{"name": r.name, "prefix": r.prefix, "repo": r.repo, "active": r.name in active,
                  "github": sources["github"].get(r.name)} for r in rigs],
        "windows": windows, "series": series, "sources": sources,
        "beads": [dict(b, rig=n, day=day) for day in day_list for n in rig_names for b in days[day]["rigs"][n]["beads"]],
        "config": {"verify_ok_labels": VERIFY_OK, "verify_fail_labels": VERIFY_FAIL, "fof_types": FOF_TYPES,
                   "human_prefixes": HUMAN_PREFIXES, "human_due_key": HUMAN_DUE_KEY,
                   "friction_events": FRICTION_EVENTS,
                   "landed_results": LANDED_RESULTS},
    }


# ---------- output ----------

def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1, sort_keys=False, default=str)
        f.write("\n")
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def render_page():
    src = os.path.join(PACK_DIR, "assets", "page", "index.html")
    dst = os.path.join(OUT_DIR, "index.html")
    tmp = dst + ".tmp"
    shutil.copyfile(src, tmp)
    os.replace(tmp, dst)
    if POST_RENDER:
        cmd = POST_RENDER.replace("{page}", dst).replace("{out}", OUT_DIR)
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            log(f"post-render hook failed ({r.returncode}): {r.stderr.strip()[:300]}")


def fmt(v, unit=""):
    return "no data" if v is None else f"{v}{unit}"


def print_summary(q):
    w7, w28 = q["windows"]["7"], q["windows"][str(DAYS)]
    print(f"quality {q['generated_at']}  days {q['days'][0]}..{q['days'][-1]} ({q['timezone']})")
    print(f"{'rig':22} {'win':>4} {'landed':>6} {'bounce%':>8} {'pmfail%':>8} {'chk%':>6} {'fof%':>6} "
          f"{'1stCI%':>7} {'c->m h':>7} {'p90':>7} {'gates':>5} {'late':>5} {'frict':>6}")
    for r in [x["name"] for x in q["rigs"] if x["active"]] + ["all"]:
        for wname, w in (("7", w7), (str(DAYS), w28)):
            m = w[r]
            print(f"{r:22} {wname:>4} {m['landed']:>6} {fmt(m['bounce_rate']):>8} {fmt(m['post_merge_failure_rate']):>8} "
                  f"{fmt(m['check_coverage']):>6} {fmt(m['fix_of_a_fix_rate']):>6} {fmt(m['first_pass_ci_rate']):>7} "
                  f"{fmt(m['time_to_land']['claimed_merged']['median']):>7} {fmt(m['time_to_land']['claimed_merged']['p90']):>7} "
                  f"{m['human']['open_now']:>5} {m['human']['past_due_now']:>5} {m['friction_total']:>6}")
    c = w28["all"]
    print(f"cost per landed bead ({DAYS}d, city): {fmt(c['cost_per_landed'], ' $')} over {c['cost_days']} day(s) with cost data")
    for k, v in q["sources"].items():
        print(f"source {k}: {json.dumps(v, default=str)[:300]}")


def explain(bead_id, landed_all, beads, clock, rigs):
    """How one bead was counted, with each of its events' resolved instant; events only for a
    bead that did not land in the window."""
    b = landed_all.get(bead_id)
    if b:
        rig = next(r for r in rigs if r.name == b["rig"])
        r = bead_record(b)
        r["labels"] = b["labels"]
        r["pr_detail"] = b.get("prs")
        r["landed_day"] = b["day"]
    else:
        print(f"{bead_id}: not a bead that landed in the window (status closed with merge_result in "
              f"{LANDED_RESULTS}, closed in the last {DAYS} days)", file=sys.stderr)
        rig = max((r for r in rigs if bead_id.startswith(r.prefix + "-")), key=lambda r: len(r.prefix), default=None)
        if not rig:
            return 1
        r = {"id": bead_id, "landed": False}
    evs = beads.rows(rig, "SELECT issue_id, event_type, actor, created_at FROM {T}events WHERE issue_id = %s", (bead_id,))
    clock.resolve(rig, evs)
    r["events"] = [{"type": e["event_type"], "raw": str(e["created_at"]), "at": iso(e["at"]),
                    "zone": clock.names.get(e.get("zone"), e.get("zone")), "how": e["how"], "actor": e["actor"]}
                   for e in sorted(evs, key=lambda e: e["at"] or datetime.min.replace(tzinfo=UTC))]
    print(json.dumps(r, indent=1, default=str))
    return 0 if b else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="compute and print a summary; write nothing")
    ap.add_argument("--bead", help="print how one landed bead was counted and exit")
    ap.add_argument("--days", type=int, default=DAYS, help=f"window in local days (default {DAYS})")
    ap.add_argument("--no-gh", action="store_true", help="skip GitHub (first-pass CI and PR-opened times)")
    args = ap.parse_args()
    if not CITY:
        sys.exit("quality: no city (set GC_CITY_PATH or run inside a city)")
    dry = args.dry_run or bool(args.bead)
    now = datetime.now(UTC)
    day_list = [day_of(now - timedelta(days=i)) for i in range(args.days - 1, -1, -1)]
    start = day_bounds(day_list[0])[0]
    state_path = os.path.join(STATE_DIR, "state.json")
    state = load_json(state_path, {})

    rigs = discover_rigs()
    log(f"{len(rigs)} rig(s): {', '.join(r.name for r in rigs)}")
    beads = Beads()
    clock = EventClock(beads, rigs, start)
    log(f"beads via {beads.mode}; events.created_at timeline: {clock.note()}")

    landed_all, followups, gates, errors = {}, [], [], []
    for rig in rigs:
        try:
            landed_all.update(collect_rig(beads, rig, start, now, clock))
            followups += collect_followups(beads, rig, start)
            gates += collect_human(beads, rig, start, now)
        except Exception as e:
            errors.append(f"{rig.name}: {e.__class__.__name__}: {e}")
            log(f"beads on {rig.name} failed: {e}")
    link_followups(landed_all, followups, [r.prefix for r in rigs])
    log(f"{len(landed_all)} landed bead(s), {len(followups)} follow-up candidate(s), {len(gates)} human gate(s)")

    prs_by_rig, gh_sources = {}, {}
    gh = GitHub(state, dry)
    if USE_GH and not args.no_gh and shutil.which("gh"):
        for rig in rigs:
            if not rig.repo:
                gh_sources[rig.name] = {"repo": None, "note": "no GitHub remote"}
                continue
            try:
                prs, req, req_src = gh.repo_prs(rig.repo, rig.branch, start)
                prs_by_rig[rig.name] = prs
                gh_sources[rig.name] = {"repo": rig.repo, "branch": rig.branch, "required": req,
                                        "required_source": req_src, "prs": len(prs)}
            except Exception as e:
                gh_sources[rig.name] = {"repo": rig.repo, "error": str(e)[:200]}
                log(f"gh on {rig.repo} failed: {e}")
        log(f"github: {gh.calls} gh call(s)")
    else:
        for rig in rigs:
            gh_sources[rig.name] = {"repo": rig.repo, "note": "GitHub disabled (QUALITY_GH=off, --no-gh or no gh)"}
    attach_prs(landed_all, prs_by_rig)

    if args.bead:
        return explain(args.bead, landed_all, beads, clock, rigs)

    friction, friction_src = collect_friction(state, rigs, start)
    cost, cost_src = collect_cost()
    days = build_days(day_list, rigs, landed_all, prs_by_rig, gates, friction, cost, now)
    sources = {"beads": beads.mode, "events_tz": clock.note(), "events_clock": clock.detail(), "github": gh_sources,
               "event_log": friction_src, "cost": cost_src, "errors": errors, "runtime_s": None}
    q = build_quality(days, day_list, rigs, sources, now)
    q["sources"]["runtime_s"] = round(time.monotonic() - T0, 1)
    if dry:
        print_summary(q)
        log("dry run: nothing written")
        return 0
    for day, rec in days.items():
        rec["generated_at"] = q["generated_at"]
        write_json(os.path.join(OUT_DIR, "history", f"{day}.json"), rec)
    write_json(os.path.join(OUT_DIR, "quality.json"), q)
    write_json(state_path, state)
    render_page()
    print_summary(q)
    log(f"wrote {len(days)} day(s) to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
