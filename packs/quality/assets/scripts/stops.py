#!/usr/bin/env python3
"""Stop census for a Gas City (the `quality` pack's quality-stops order; README.md, "Why it's waiting").

No model and no tokens. Each run names, for every bead in flight and every seat that should be up,
why it waits and who clears it: one typed stop with a class, an actor and how long it has waited.
Work that is moving is listed too, with who moves it, so "is it stuck?" has an answer on the page
before anyone asks. Sources: the beads stores (plain SQL), `gc rig list`, `gc agent list`,
`gc session list`, `gc config show`, the city event log (read incrementally from a byte offset)
and /proc (load, CPU per process). Writes OUT_DIR/stops.json (now, for the page) and appends one
line per run to OUT_DIR/stops/<day>.jsonl, the record that time per class is summed from.

    stops.py               the census; above QUALITY_MAX_LOAD only its host part (load, top consumers)
    stops.py --dry-run     print the census; write nothing
    stops.py --bead ID     how one bead was classified, with the inputs that decided it
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as q  # noqa: E402  (same directory: one reading of beads, rigs and event times)

try:
    import tomllib
except ImportError:  # python < 3.11: no named sessions, so no seat_down
    tomllib = None

UTC = q.UTC
STOPS_DIR = os.path.join(q.OUT_DIR, "stops")
STATE = os.path.join(q.STATE_DIR, "stops-state.json")
LOG = os.path.join(q.STATE_DIR, "stops.log")
INTERVAL_MIN = float(q.env("QUALITY_STOPS_INTERVAL_MIN", "30"))  # one run stands for this long (the order's interval)
MAX_LOAD = float(q.env("QUALITY_MAX_LOAD", "15"))
LOAD_STOP = float(q.env("QUALITY_STOP_LOAD", q.env("QUALITY_MAX_LOAD", "15")))
GRACE_MIN = float(q.env("QUALITY_STOP_GRACE_MIN", "20"))     # younger than this, routed work or a down seat is not a stop yet
IDLE_MIN = float(q.env("QUALITY_STOP_IDLE_MIN", "60"))       # an assignee whose session did nothing this long is idle
RESETS = int(q.env("QUALITY_STOP_RESETS", "3"))              # stalled-session resets in one local day that make a stop
VERIFY_H = float(q.env("QUALITY_STOP_VERIFY_H", "30"))       # hours after landing a post-merge verdict is due
VERIFY_DAYS = float(q.env("QUALITY_STOP_VERIFY_DAYS", "3"))  # landed beads older than this leave the census
SKIP_TYPES = q.env_list("QUALITY_STOP_SKIP_TYPES", "session,convoy,epic,milestone,molecule,message")
REVIEWERS = q.env_list("QUALITY_STOP_REVIEWERS", "refinery")  # agent base names that review and merge
WATCHERS = q.env_list("QUALITY_STOP_WATCHERS", "witness")     # agent base names that wake or reclaim a rig's seats
ROUTER = q.env("QUALITY_STOP_ROUTER", "mayor")                # routes work, applies config, holds what has no owner
HUMAN = q.env("QUALITY_STOP_HUMAN", "owner")                  # the person behind human gates nobody is assigned to
DEPLOYER = q.env("QUALITY_STOP_DEPLOYER", "post-merge check")  # what labels a landing verified
REJECTION_KEY = "rejection_reason"
TOP = 5

# A bead gets the first class that applies, in this order (the lead-time stages' order: a blocker,
# then a hold, then the phase it is in). Seat and host classes stand beside them.
CLASSES = [
    ("blocked", "bead", "blocked by another bead"),
    ("parked", "bead", "parked on purpose"),
    ("human_gate_due", "bead", "waiting for a person, past due"),
    ("human_gate", "bead", "waiting for a person"),
    ("rig_suspended", "bead", "rig suspended"),
    ("review_down", "bead", "in review, reviewer not running"),
    ("review_idle", "bead", "in review, reviewer idle since it was routed"),
    ("bounced", "bead", "bounced, nobody assigned to rework it"),
    ("assignee_down", "bead", "assignee not running"),
    ("assignee_idle", "bead", "assignee idle"),
    ("pool_suspended", "bead", "routed to a suspended pool"),
    ("pool_cap", "bead", "routed, pool at its cap"),
    ("routed_unclaimed", "bead", "routed, not picked up"),
    ("deploy_failed", "bead", "landed, post-merge check failed"),
    ("landed_unverified", "bead", "landed, no post-merge verdict yet"),
    ("seat_down", "seat", "always-on seat not running"),
    ("seat_resets", "seat", "seat reset as stalled repeatedly today"),
    ("seat_mail", "seat", "mail unread, seat down or idle since it came"),
    ("host_load", "host", "host load over the threshold"),
]
LABEL = {k: label for k, _, label in CLASSES}
KIND = {k: kind for k, kind, _ in CLASSES}
MOVING = {"working": "working", "rework": "reworking a bounce", "review": "in review", "routed": "just routed"}
UP_STATES = ("active", "awake")
# gc's own names for a session: its session name (gastown__polecat-gc-wbnef, rig--gastown__refinery)
# and its id (gc-wbnef, gc-wisp-ovexok: the city's prefix and a short id); a pool slot's session
# name ends in its id
GC_NAME = re.compile(r"__|^[a-z0-9]+-(?:wisp-)?[a-z0-9]{3,}$")


def session_id_of(name):
    """'gastown__polecat-gc-wbnef' -> 'gc-wbnef'; None for a name without an id in it."""
    if "__" not in name:
        return None
    tail = name.rsplit("__", 1)[1]
    return tail.split("-", 1)[1] if "-" in tail else None


def now_utc():
    return datetime.now(UTC)


def hrs(a, b):
    return None if a is None or b is None else round(max((b - a).total_seconds(), 0) / 3600, 2)


def base(name):
    """'hivemind/gastown.witness' -> 'witness': the agent's name without its rig and binding."""
    return str(name or "").rsplit("/", 1)[-1].rsplit(".", 1)[-1]


def rig_of(name):
    return name.split("/", 1)[0] if "/" in str(name or "") else None


def adopt_conventions():
    """Take the metrics order's conventions (human-gate prefixes, verify labels, hold and route
    keys) from its quality.json, so a city sets them once, on quality-metrics; this order's own
    environment wins."""
    cfg = q.load_json(os.path.join(q.OUT_DIR, "quality.json"), {}).get("config") or {}
    for var, attr, key in (("QUALITY_HUMAN_PREFIXES", "HUMAN_PREFIXES", "human_prefixes"),
                           ("QUALITY_HUMAN_DUE_KEY", "HUMAN_DUE_KEY", "human_due_key"),
                           ("QUALITY_VERIFY_OK_LABELS", "VERIFY_OK", "verify_ok_labels"),
                           ("QUALITY_VERIFY_FAIL_LABELS", "VERIFY_FAIL", "verify_fail_labels"),
                           ("QUALITY_LANDED_RESULTS", "LANDED_RESULTS", "landed_results"),
                           ("QUALITY_PARKED_STATUSES", "PARKED_STATUSES", "parked_statuses"),
                           ("QUALITY_HOLD_KEYS", "HOLD_KEYS", "hold_keys"),
                           ("QUALITY_ROUTE_KEYS", "ROUTE_KEYS", "route_keys")):
        if not q.env(var) and cfg.get(key):
            setattr(q, attr, cfg[key])


# ---------- what gc says ----------

def gc_out(args):
    # in the city directory: `--city` costs the CLI about twice the CPU (measured 2026-10-26)
    cmd = [q.GC, *args]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=q.CITY)
    except (OSError, subprocess.SubprocessError) as e:
        return None, f"gc {' '.join(args)}: {e}"
    if out.returncode != 0:
        return None, f"gc {' '.join(args)}: exit {out.returncode}: {out.stderr.strip()[-200:]}"
    return out.stdout, None


def gc_json(args):
    text, err = gc_out(args)
    if err:
        return None, err
    try:
        return json.loads(text), None
    except ValueError:
        return None, f"gc {' '.join(args)}: no JSON on stdout"


# What `gc rig list`, `gc agent list` and `gc config show` answer changes only with these files
# (rig and agent suspension is the runtime file), so their answers are kept until one changes.
CONFIG_FILES = ("city.toml", "pack.toml", "packs.lock", ".gc/site.toml", ".gc/runtime/suspension-state.json")
CONFIG_MAX_H = 6
GC_CACHE_V = 2


def config_key():
    out = []
    for f in CONFIG_FILES:
        try:
            out.append(os.stat(os.path.join(q.CITY, f)).st_mtime_ns)
        except OSError:
            out.append(None)
    return out


def gc_cached(state, args, now, keep):
    """(what `keep` takes from the stdout, error) of a gc call about the city's configuration; kept
    in the state while no file in CONFIG_FILES changed and it is younger than CONFIG_MAX_H."""
    c, k, key = state.setdefault("gc", {}), " ".join(args), config_key()
    rec = c.get(k)
    if rec and rec.get("v") == GC_CACHE_V and rec.get("key") == key and \
            now - (q.as_utc(rec.get("at")) or now) < timedelta(hours=CONFIG_MAX_H):
        return rec["out"], None
    out, err = gc_out(args)
    if err:
        return None, err
    try:
        kept = keep(out)
    except (ValueError, TypeError, AttributeError, KeyError) as e:
        return None, f"gc {' '.join(args)}: unreadable answer ({e.__class__.__name__}: {str(e)[:100]})"
    c[k] = {"v": GC_CACHE_V, "key": key, "at": q.iso(now), "out": kept}
    return kept, None


def keep_agents(text):
    return [{k: a.get(k) for k in ("qualified_name", "pool", "suspended", "scope")}
            for a in json.loads(text).get("agents", []) if a.get("qualified_name")]


def keep_named(text):
    if tomllib is None:
        raise ValueError("no tomllib (python < 3.11)")
    return [{k: n.get(k) for k in ("template", "dir", "mode")} for n in tomllib.loads(text).get("named_session", [])]


def keep_rigs(text):
    return json.loads(text).get("rigs", [])


class City:
    """Agents (pools, caps, suspension), sessions (state, last activity) and always-on seats."""

    def __init__(self, rigs, errors, state, now):
        self.rig_suspended = {r.name for r in rigs if getattr(r, "suspended", False)}
        agents, err = gc_cached(state, ["agent", "list", "--json"], now, keep_agents)
        if err:
            errors.append(err)
        self.agents = {a["qualified_name"]: a for a in agents or []}
        d, err = gc_json(["session", "list", "--json"])
        if err:
            errors.append(err)
        self.sessions = (d or {}).get("sessions", [])
        self.by_name = {}
        for s in self.sessions:
            for k in ("alias", "agent_name", "session_name", "id", "name"):
                if s.get(k):
                    self.by_name.setdefault(s[k], s)
        named, err = gc_cached(state, ["config", "show"], now, keep_named)
        self.named_all = named or []
        self.named = [n for n in self.named_all if n.get("mode") == "always"]
        if err:
            errors.append(f"always-on seats unknown: {err}")

    def session(self, name):
        if not name:
            return None
        s = self.by_name.get(name)
        if s is None and session_id_of(name):  # a session name: look it up by the id it ends in
            s = self.by_name.get(session_id_of(name))
        if s is None:  # 'mayor' for 'gastown.mayor': a unique suffix match
            hits = {id(x): x for k, x in self.by_name.items() if k.endswith(("/" + name, "." + name))}
            s = next(iter(hits.values())) if len(hits) == 1 else None
        return s

    def alias(self, name):
        """The name a seat goes by (its alias) for any name gc gives it (session name, id, short
        name); a name no session has is kept as it is."""
        s = self.session(name) if name else None
        return (s.get("alias") or s.get("agent_name") or name) if s else name

    def is_agent(self, name):
        """An agent (or one of its sessions, live or gone) rather than a person: a configured agent,
        a session gc lists, or a name in an agent's or a session's form (rig/name, binding.name,
        gc session names and ids). Anything else, an email address included, is a person."""
        return name in self.agents or self.session(name) is not None or (
            "@" not in name and ("/" in name or "." in name or bool(GC_NAME.search(name))))

    def slots(self, pool):
        return [s for s in self.sessions if s.get("template") == pool and s.get("state") in UP_STATES]

    def watcher(self, rig):
        """Who wakes or reclaims a rig's seats: its watcher agent (QUALITY_STOP_WATCHERS), else the router."""
        if rig:
            for name in self.agents:
                if rig_of(name) == rig and base(name) in WATCHERS:
                    return name
        return ROUTER

    def is_pool(self, name):
        """An agent whose sessions are slots with their own names (a polecat pool), not a named seat."""
        return name in self.agents and not any(self.agent_of_named(n)[0] == name for n in self.named_all)

    def agent_of_named(self, n):
        for name, a in self.agents.items():
            if (rig_of(name) or "") == (n.get("dir") or "") and base(name) == base(n.get("template")):
                return name, a
        return None, None


def up(s):
    return bool(s) and s.get("state") in UP_STATES


def last_active(s):
    t = q.as_utc((s or {}).get("last_active"))
    return t if t and t.year > 1 else None


def state_of(s):
    if not s:
        return "no session"
    why = s.get("sleep_reason") or s.get("state_reason") or ""
    return f"{s.get('state') or '?'}{': ' + why if why else ''}"


# ---------- the event log: stalls and stops of sessions ----------

SESSION_EVENTS = re.compile(rb'"type":"session\.(reset_stalled|stopped|woke|idle_killed)"')


def scan_session_events(state, now):
    """Per session (by every name an event gives it): stalled resets per local day, last stop and
    last wake. Reads events.jsonl from the last offset; a new inode (rotation) starts it over."""
    ev = state.setdefault("events", {})
    path = os.path.join(q.CITY, ".gc", "events.jsonl")
    try:
        st = os.stat(path)
    except OSError:
        return ev, "no city event log"
    if ev.get("ino") != st.st_ino or ev.get("offset", 0) > st.st_size:
        ev.clear()
        ev.update({"ino": st.st_ino, "offset": 0, "resets": {}, "stopped": {}, "woke": {}})
    with open(path, "rb") as f:
        f.seek(ev["offset"])
        for line in f:
            if not line.endswith(b"\n"):
                break  # still being written
            ev["offset"] += len(line)
            if not SESSION_EVENTS.search(line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            at = q.as_utc(e.get("ts"))
            if at is None:
                continue
            p = e.get("payload") if isinstance(e.get("payload"), dict) else {}
            names = {x for x in (e.get("subject"), p.get("session_id"), p.get("session_name")) if x}
            t = e.get("type")
            for n in names:
                if t == "session.reset_stalled":
                    r = ev["resets"].setdefault(q.day_of(at), {}).setdefault(n, [0, q.iso(at)])
                    r[0] += 1
                elif t == "session.woke":
                    ev["woke"][n] = q.iso(at)
                else:
                    ev["stopped"][n] = q.iso(at)
    keep = {q.day_of(now - timedelta(days=d)) for d in range(2)}
    ev["resets"] = {d: v for d, v in ev["resets"].items() if d in keep}
    old = q.iso(now - timedelta(days=7))
    for k in ("stopped", "woke"):
        ev[k] = {n: t for n, t in ev[k].items() if t >= old}
    return ev, None


def session_names(s):
    return [s[k] for k in ("alias", "agent_name", "session_name", "id", "name") if s and s.get(k)]


# ---------- the host ----------

def procs():
    """Processes running and blocked on I/O now (/proc/stat), the two things the load average counts."""
    out = {}
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith(("procs_running", "procs_blocked")):
                    k, v = line.split()
                    out[k] = int(v)
    except (OSError, ValueError):
        pass
    return out


def user_of(uid):
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def cpu_top(sessions, secs=2.0):
    """CPU cores used over `secs` per seat (processes whose cwd is in its work_dir) or other command,
    from /proc/<pid>/stat read twice: a process's own time, plus the time of its children that
    ended in the sample (bd, gc, git calls: too short to be seen themselves), at most one core's
    worth per seat, since a child's whole life lands when it is reaped. The system-wide counters in
    /proc/stat are not used: on the host this was built on their ticks added up to half the wall
    time (only their ratios hold, which is what top shows); this sample came within 0.1-2 cores of
    top's CPU line over the same windows (README).
    Linux only; elsewhere empty."""
    def snap():
        out = {}
        try:
            pids = [p for p in os.listdir("/proc") if p.isdigit()]
        except OSError:
            return out
        for p in pids:
            try:
                with open(f"/proc/{p}/stat", "rb") as f:
                    s = f.read()
            except OSError:
                continue
            r = s.rfind(b")")
            x = s[r + 2:].split()
            if len(x) > 14:
                out[p] = (int(x[11]) + int(x[12]), int(x[13]) + int(x[14]), s[s.find(b"(") + 1:r].decode(errors="replace"))
        return out
    a, t0 = snap(), time.monotonic()
    if not a:
        return [], None, None
    time.sleep(secs)
    b, dt = snap(), time.monotonic() - t0
    hz, me = os.sysconf("SC_CLK_TCK"), os.getuid()
    dirs = sorted(((s["work_dir"].rstrip("/"), s.get("alias") or s.get("session_name")) for s in sessions
                   if s.get("work_dir")), key=lambda x: -len(x[0]))
    own, kids = Counter(), Counter()
    for p, (ticks, child, comm) in b.items():
        if p not in a:
            continue
        d_own, d_kids = max(ticks - a[p][0], 0), max(child - a[p][1], 0)
        if not d_own and not d_kids:
            continue
        try:
            cwd = os.readlink(f"/proc/{p}/cwd")
        except OSError:
            cwd = ""
        who = next((n for w, n in dirs if cwd == w or cwd.startswith(w + "/")), None)
        if who is None:
            try:
                uid = os.stat(f"/proc/{p}").st_uid
            except OSError:  # it ended
                uid = me
            who = comm if uid == me else f"{user_of(uid)}:{comm}"  # another user's process on the host
        own[who] += d_own / hz / dt
        kids[who] += d_kids / hz / dt
    total = {w: own[w] + min(kids[w], 1.0) for w in set(own) | set(kids)}
    top = sorted(total, key=lambda w: -total[w])[:TOP]
    return ([{"who": w, "cores": round(total[w], 2), "own": round(own[w], 2), "children": round(kids[w], 2)}
             for w in top if total[w] >= 0.05], round(sum(own.values()), 2), round(sum(min(v, 1.0) for v in kids.values()), 2))


def host_stop(sessions, now):
    load1 = os.getloadavg()[0]
    top, own, kids = cpu_top(sessions)
    host = {"load1": round(load1, 2), "cores": os.cpu_count(), "threshold": LOAD_STOP, "top": top,
            "cpu_own": own, "cpu_children": kids, **procs()}
    if load1 <= LOAD_STOP:
        return host, None
    why = f"load {load1:.1f} on {host['cores']} cores; top: " + (", ".join(f"{t['who']} {t['cores']}" for t in top)
                                                                 or "unknown (no /proc)")
    if own is not None:
        used = own + kids
        why += f"; {used:.1f} cores in use in a 2 s sample ({kids:.1f} of them in short-lived children)"
    if host.get("procs_blocked"):
        why += f"; {host['procs_blocked']} process(es) waiting on I/O"
    return host, {"kind": "host", "id": "host", "rig": None, "title": "host", "class": "host_load",
                  "actor": top[0]["who"] if top else ROUTER, "why": why, "since": q.iso(now), "age_h": 0.0}


# ---------- the beads ----------

def read_store(beads, store, now, info):
    """The store's beads in flight with their metadata, blocks dependencies and change events, and
    its beads landed in the last VERIFY_DAYS with their verify labels."""
    skip = tuple(SKIP_TYPES)
    rows = beads.rows(store, "SELECT id, title, status, assignee, issue_type, created_at, created_by, started_at, "
                             "defer_until, CAST(metadata AS CHAR) AS metadata FROM {T}issues "
                             "WHERE status NOT IN ('closed', 'tombstone')"
                      + (f" AND issue_type NOT IN ({q.placeholders(len(skip))})" if skip else ""), skip)
    out = []
    for r in rows:
        out.append({"id": r["id"], "rig": store.name, "title": r["title"] or "", "status": r["status"] or "",
                    "assignee": (r["assignee"] or "").strip(), "type": r["issue_type"],
                    "created": q.as_utc(r["created_at"]), "created_by": r["created_by"] or "",
                    "started": q.as_utc(r["started_at"]), "defer_until": q.as_utc(r["defer_until"]),
                    "meta": q.jload(r["metadata"]), "deps": [], "events": []})
    ids = [b["id"] for b in out]
    by_id = {b["id"]: b for b in out}
    for part in q.chunks(ids):
        for d in beads.rows(store, "SELECT issue_id, depends_on_issue_id AS dep, depends_on_external AS ext, created_at "
                                   f"FROM {{T}}dependencies WHERE type = 'blocks' AND issue_id IN ({q.placeholders(len(part))})",
                            tuple(part)):
            on = d["dep"] or (str(d["ext"]).split(":")[-1] if d["ext"] else None)
            if on:
                by_id[d["issue_id"]]["deps"].append({"on": on, "issue_id": d["issue_id"], "created_at": d["created_at"]})
        for e in beads.rows(store, "SELECT issue_id, event_type, actor, created_at, new_value FROM {T}events "
                                   f"WHERE issue_id IN ({q.placeholders(len(part))})", tuple(part)):
            by_id[e["issue_id"]]["events"].append({"issue_id": e["issue_id"], "type": e["event_type"], "actor": e["actor"] or "",
                                                   "created_at": e["created_at"], "nv": q.jload(e["new_value"])})
    labels = q.VERIFY_OK + q.VERIFY_FAIL
    landed = []
    uses = labels and beads.rows(store, f"SELECT 1 AS x FROM {{T}}labels WHERE label IN ({q.placeholders(len(labels))}) LIMIT 1",
                                 tuple(labels))
    if uses:
        for r in beads.rows(store, "SELECT id, title, assignee, closed_at, CAST(metadata AS CHAR) AS metadata FROM {T}issues "
                                   "WHERE status = 'closed' AND closed_at >= %s",
                            (q.sqlts(now - timedelta(days=VERIFY_DAYS)),)):
            if q.is_landed(q.jload(r["metadata"])):
                landed.append({"id": r["id"], "rig": store.name, "title": r["title"] or "", "closed": q.as_utc(r["closed_at"]),
                               "labels": []})
        lid = {b["id"]: b for b in landed}
        for part in q.chunks(list(lid)):
            for r in beads.rows(store, f"SELECT issue_id, label FROM {{T}}labels WHERE issue_id IN ({q.placeholders(len(part))})",
                                tuple(part)):
                lid[r["issue_id"]]["labels"].append(r["label"])
    info["verify"][store.name] = bool(uses)
    return out, landed


def blocker_info(beads, stores, ids):
    """id -> status, assignee, title of each blocker, from the store whose prefix the id carries."""
    order = sorted(stores, key=lambda r: -len(r.prefix))
    by_store = defaultdict(list)
    for i in ids:
        r = next((s for s in order if i.startswith(s.prefix + "-")), None)
        if r:
            by_store[r.name].append((r, i))
    out = {}
    for pairs in by_store.values():
        rig = pairs[0][0]
        for part in q.chunks([i for _, i in pairs]):
            try:
                rows = beads.rows(rig, f"SELECT id, status, assignee, title FROM {{T}}issues WHERE id IN ({q.placeholders(len(part))})",
                                  tuple(part))
            except Exception as e:
                q.log(f"blockers on {rig.name}: {e}")
                continue
            for r in rows:
                out[r["id"]] = {"status": r["status"], "assignee": (r["assignee"] or "").strip(), "title": r["title"] or ""}
    return out


def value_set(e, key):
    """(True, value) when the event's new value sets `key`, a column or a metadata key."""
    nv = e["nv"]
    if key in nv:
        return True, nv[key]
    m = q.jload(nv.get("metadata"))
    if key in m:
        return True, m[key]
    return False, None


def set_by(b, key, want=None):
    """(instant, actor) of the newest event that set `key` to a non-empty value (to `want` when
    given); the bead's creation when no event did (it was set when the bead was made)."""
    for e in reversed(b["events"]):
        hit, v = value_set(e, key)
        v = "" if v is None else str(v).strip()
        if hit and v and (want is None or v == str(want)) and e.get("at"):
            return e["at"], e["actor"]
    return b["created"], b["created_by"]


def stop(b, cls, actor, why, since, now, **extra):
    r = {"kind": "bead", "id": b["id"], "rig": b["rig"], "title": b["title"][:120], "class": cls,
         "actor": actor or "nobody", "why": str(why)[:300], "since": q.iso(since), "age_h": hrs(since, now)}
    r.update({k: v for k, v in extra.items() if v})
    return r


def root_of(on, work, blockers, hops=6):
    """The open bead at the end of a chain of blocks dependencies that starts at `on` (a blocker
    that is itself blocked by an open bead passes the wait on), and how many hops away it is."""
    seen, cur, n = {on}, on, 0
    while n < hops:
        nxt = [d["on"] for d in (work.get(cur) or {}).get("deps", [])
               if (blockers.get(d["on"]) or {}).get("status") not in ("closed", "tombstone") and d["on"] not in seen]
        if not nxt:
            break
        cur, n = nxt[0], n + 1
        seen.add(cur)
    return cur, n


def classify(b, city, blockers, holders, now, work=None):
    """The bead's one class (a stop, or a moving state with who moves it), or None for backlog
    (open, nobody assigned, not routed, not held)."""
    m, a, status, rig = b["meta"], b["assignee"], b["status"], b["rig"]
    grace = timedelta(minutes=GRACE_MIN)
    also = [f"rig {rig} suspended"] if rig in city.rig_suspended else []
    route_key = next((k for k in q.ROUTE_KEYS if str(m.get(k) or "").strip()), None)
    route = str(m[route_key]).strip() if route_key else ""
    agent = a and city.is_agent(a)
    # 1. blocked by an open bead (a blocker in no store counts as open)
    open_on = [d for d in b["deps"] if (blockers.get(d["on"]) or {}).get("status") not in ("closed", "tombstone")]
    if open_on:
        d = max(open_on, key=lambda d: d.get("at") or b["created"])
        info = blockers.get(d["on"])
        if info is None:
            return stop(b, "blocked", ROUTER, f"blocked by {d['on']}, found in no store", d.get("at") or b["created"], now, also=also)
        more = f" and {len(open_on) - 1} more" if len(open_on) > 1 else ""
        root, hops = root_of(d["on"], work or {}, blockers)
        ri = blockers.get(root) or info
        chain = f"; the chain ends {hops} hop(s) on at {root} ({ri['status']}{', unassigned' if not ri['assignee'] else ''}): " \
                f"{ri['title'][:50]}" if hops else ""
        return stop(b, "blocked", ri["assignee"] or ROUTER,
                    f"blocked by {d['on']} ({info['status']}{', unassigned' if not info['assignee'] else ''}){more}: "
                    f"{info['title'][:60]}{chain}", d.get("at") or b["created"], now, also=also, on=d["on"], root=root)
    # 2. parked on purpose: a hold reason, a parked status, a defer date ahead
    hold = next((k for k in q.HOLD_KEYS if str(m.get(k) or "").strip()), None)
    # who clears a hold: the assignee when there is one, else whoever set it
    if hold:
        at, by = set_by(b, hold)
        return stop(b, "parked", a or by, str(m[hold]).strip() + f" (set by {by or '?'})", at, now, also=also)
    if status in q.PARKED_STATUSES:
        at, by = set_by(b, "status", status)
        return stop(b, "parked", a or by, f"status {status}, set by {by or '?'}", at, now, also=also)
    if b["defer_until"] and b["defer_until"] > now:
        at, by = set_by(b, "defer_until")
        return stop(b, "parked", a or by, f"deferred until {q.iso(b['defer_until'])} by {by or '?'}", at, now, also=also)
    # 3. a person: a gate (title prefix or due key), or assigned to someone who is no agent
    due = q.as_utc(m.get(q.HUMAN_DUE_KEY))
    if b["title"].startswith(tuple(q.HUMAN_PREFIXES)) or q.HUMAN_DUE_KEY in m or (a and not agent):
        who = a if a and not agent else HUMAN
        if due and due < now:
            return stop(b, "human_gate_due", who, f"due {q.iso(due)} ({hrs(due, now)} h ago)", b["created"], now, also=also)
        return stop(b, "human_gate", who, f"due {q.iso(due)}" if due else "no due time", b["created"], now, also=also)
    # 4. the whole rig is off
    if rig in city.rig_suspended:
        return stop(b, "rig_suspended", ROUTER, f"rig {rig} is suspended" + (f"; assigned to {a}" if a else
                    f"; routed to {route}" if route else ""), None, now)
    rejected = str(m.get(REJECTION_KEY) or "").strip()
    # 5. with a reviewer
    if agent and base(a) in REVIEWERS:
        at, _ = set_by(b, "assignee", a)
        s, la = city.session(a), last_active(city.session(a))
        if not up(s):
            return stop(b, "review_down", city.watcher(rig_of(a) or rig), f"{a} not running ({state_of(s)})", at, now)
        if la and la < at and now - at > grace:
            return stop(b, "review_idle", a, f"{a} last active {q.iso(la)}, before the bead reached it", at, now)
        return stop(b, "review", a, f"{a} last active {q.iso(la)}" if la else f"{a} running", at, now)
    # 6. bounced and nobody has it
    if rejected and not a:
        at, by = set_by(b, REJECTION_KEY)
        return stop(b, "bounced", route or ROUTER, f"bounced by {by or '?'}: {rejected}" +
                    (f"; routed to {route}" if route else "; not routed"), at, now)
    # 7. assigned to an agent
    if agent:
        s = city.session(a)
        claimed = q.as_utc(m.get("gc.claimed_at")) or b["started"] or set_by(b, "assignee", a)[0]
        moving = "rework" if rejected else "working"
        if not up(s):
            # waiting since the later of the claim and the session's last stop
            since = max([t for t in (q.as_utc(city.stopped.get(n)) for n in session_names(s)) if t] +
                        ([claimed] if claimed else []), default=None)
            return stop(b, "assignee_down", city.watcher(rig_of(a) or rig), f"{a} not running ({state_of(s)})", since, now)
        la = last_active(s)
        if la and now - la > timedelta(minutes=IDLE_MIN):
            return stop(b, "assignee_idle", city.watcher(rig_of(a) or rig), f"{a} last active {q.iso(la)}", la, now)
        return stop(b, moving, a, (f"{a} last active {q.iso(la)}" if la else f"{a} running") +
                    (f"; bounced: {rejected[:100]}" if rejected else ""), claimed, now)
    # 8. routed, nobody has claimed it
    if route:
        at, by = set_by(b, route_key, route)
        pool = city.agents.get(route)
        # a seat that runs under its own name (the mayor, a crew) takes its own work: not a pool
        seat = city.by_name.get(route)
        if (pool and pool.get("suspended")) or rig_of(route) in city.rig_suspended:
            return stop(b, "pool_suspended", ROUTER, f"routed to {route}, which is suspended", at, now)
        if seat is not None:
            if not up(seat):
                return stop(b, "assignee_down", city.watcher(rig_of(route) or rig), f"routed to {route}, not running "
                            f"({state_of(seat)})", at, now)
        else:
            cap = ((pool or {}).get("pool") or {}).get("max")
            busy = city.slots(route)
            if isinstance(cap, int) and cap > 0 and len(busy) >= cap:
                held = sorted({h for s in busy for n in session_names(s) for h in holders.get(n, [])})
                return stop(b, "pool_cap", route, f"{len(busy)} of {cap} slot(s) busy" +
                            (f", on {', '.join(held[:4])}" if held else ", on no bead in flight"), at, now)
        if now - at < grace:
            return stop(b, "routed", route, f"routed by {by or '?'}", at, now)
        free = "" if pool is None or seat is not None else \
            f"; {len(city.slots(route))} of {((pool.get('pool') or {}).get('max'))} slot(s) busy"
        return stop(b, "routed_unclaimed", city.watcher(rig_of(route) or rig), f"routed to {route} by {by or '?'}{free}", at, now)
    return None


def landed_stop(b, now):
    if any(lab in q.VERIFY_OK for lab in b["labels"]):
        return None
    if any(lab in q.VERIFY_FAIL for lab in b["labels"]):
        return stop(dict(b, created=b["closed"]), "deploy_failed", ROUTER, "post-merge check failed (" +
                    ", ".join(lab for lab in b["labels"] if lab in q.VERIFY_FAIL) + ")", b["closed"], now)
    if b["closed"] and now - b["closed"] > timedelta(hours=VERIFY_H):
        return stop(dict(b, created=b["closed"]), "landed_unverified", DEPLOYER,
                    f"landed {q.iso(b['closed'])}, no verify label after {VERIFY_H:g} h", b["closed"], now)
    return None


# ---------- seats ----------

def read_mail(beads, hq, now):
    """Unread mail per recipient: open message beads in the city store without mail.read."""
    out = defaultdict(list)
    for st in hq:
        for t in ("wisps", "issues"):
            try:
                rows = beads.rows(st, f"SELECT id, assignee, sender, title, created_at, CAST(metadata AS CHAR) AS metadata "
                                      f"FROM {{T}}{t} WHERE issue_type = 'message' AND status = 'open'")
            except Exception:
                continue
            for r in rows:
                m = q.jload(r["metadata"])
                if str(m.get("mail.read") or "").lower() == "true" or not r["assignee"]:
                    continue
                out[r["assignee"]].append({"id": r["id"], "from": r["sender"] or "", "title": r["title"] or "",
                                           "at": q.as_utc(r["created_at"])})
    return out


def seat_overview(city, mail, ev, now):
    """Every session gc knows, with its state, last activity and unread mail; always-on seats first."""
    always = {city.agent_of_named(n)[0] for n in city.named} - {None}
    rows = []
    for s in city.sessions:
        name = s.get("alias") or s.get("agent_name") or s.get("session_name")
        unread = [x for n in session_names(s) for x in mail.get(n, [])]
        la = last_active(s)
        rows.append({"seat": name, "rig": rig_of(name), "state": state_of(s), "up": up(s), "always_on": name in always,
                     "last_active": q.iso(la), "idle_h": hrs(la, now) if la else None, "unread": len(unread),
                     "oldest_unread_h": max((hrs(x["at"], now) for x in unread if x["at"]), default=None),
                     "resets_today": max((v[0] for n in session_names(s) for v in [(ev.get("resets") or {})
                                          .get(q.day_of(now), {}).get(n)] if v), default=0)})
    rows.sort(key=lambda r: (not r["always_on"], r["seat"]))
    return rows


def mail_stops(city, mail, now):
    """Mail that waits: unread longer than the grace time, its seat down, or idle since before it
    came. Mail a seat sent itself (a handoff note for its next session) is not counted."""
    out, grace = [], timedelta(minutes=GRACE_MIN)
    for s in city.sessions:
        names = session_names(s)
        unread = [x for n in names for x in mail.get(n, []) if x["at"] and now - x["at"] > grace and x["from"] not in names]
        if not unread:
            continue
        first = min(unread, key=lambda x: x["at"])
        la = last_active(s)
        if up(s) and (la is None or la > first["at"]):
            continue  # the seat has been active since the mail came: it is busy, not waiting
        name = s.get("alias") or names[0]
        actor = HUMAN if base(name) == base(ROUTER) else (city.watcher(rig_of(name)) if rig_of(name) else ROUTER)
        out.append({"kind": "seat", "id": name, "rig": rig_of(name), "title": name, "class": "seat_mail", "actor": actor,
                    "why": f"{len(unread)} unread, oldest from {first['from'] or '?'}: {first['title'][:80]}; "
                           f"{'not running (' + state_of(s) + ')' if not up(s) else 'last active ' + str(q.iso(la))}",
                    "since": q.iso(first["at"]), "age_h": hrs(first["at"], now)})
    return out


def seat_stops(city, ev, now):
    out = []
    grace = timedelta(minutes=GRACE_MIN)
    for n in city.named:
        name, agent = city.agent_of_named(n)
        if not name or agent.get("suspended") or rig_of(name) in city.rig_suspended:
            continue
        s = city.session(name)
        if up(s):
            continue
        since = max((q.as_utc(ev.get("stopped", {}).get(x)) for x in session_names(s)), default=None,
                    key=lambda t: t or datetime.min.replace(tzinfo=UTC))
        if since and now - since < grace:
            continue
        actor = HUMAN if base(name) == base(ROUTER) else ROUTER
        out.append({"kind": "seat", "id": name, "rig": rig_of(name), "title": name, "class": "seat_down", "actor": actor,
                    "why": f"mode always, {state_of(s)}", "since": q.iso(since), "age_h": hrs(since, now)})
    today = (ev.get("resets") or {}).get(q.day_of(now), {})
    seen = set()
    for s in city.sessions:
        names = session_names(s)
        hits = [today[x] for x in names if x in today]
        if not hits or id(s) in seen:
            continue
        seen.add(id(s))
        n, first = max(hits, key=lambda h: h[0])
        if n >= RESETS:
            name = s.get("alias") or names[0]
            out.append({"kind": "seat", "id": name, "rig": rig_of(name), "title": name, "class": "seat_resets",
                        "actor": ROUTER, "why": f"reset as stalled {n} times today", "since": first,
                        "age_h": hrs(q.as_utc(first), now)})
    return out


# ---------- the census ----------

def census(now, only=None):
    """Everything one run sees: stops, moving work, backlog per rig, host, sources."""
    errors, info = [], {"verify": {}}
    state = q.load_json(STATE, {})
    beads = q.Beads()
    listed, _ = gc_cached(state, ["rig", "list", "--json"], now, keep_rigs)
    rigs, hq = q.discover_rigs(listed)  # asks gc itself (with one retry) when the cached answer is unusable
    stores = rigs + [h for h in hq if h not in rigs]
    q.log(f"{len(stores)} stores")
    city = City(rigs, errors, state, now)
    q.log(f"gc: {len(city.agents)} agents, {len(city.sessions)} sessions, {len(city.named)} always-on seats")
    ev, err = scan_session_events(state, now)
    q.log(f"event log read to byte {ev.get('offset')}")
    if err:
        errors.append(err)
    city.stopped = ev.get("stopped", {})
    clock = q.EventClock.from_detail(beads, (q.load_json(os.path.join(q.OUT_DIR, "quality.json"), {}).get("sources") or {})
                                     .get("events_clock"))
    clock_note = "timeline from quality.json"
    if clock is None:
        clock = q.EventClock(beads, stores, now - timedelta(days=2))
        clock_note = "timeline measured on the last 2 days"
    work, landed = [], []
    for st in stores:
        try:
            w, ld = read_store(beads, st, now, info)
        except Exception as e:  # one store down must not hide the others
            errors.append(f"{st.name}: {e.__class__.__name__}: {str(e)[:200]}")
            continue
        evs = [e for b in w for e in b["events"]]
        clock.resolve(st, evs)
        deps = [d for b in w for d in b["deps"]]
        clock.resolve(st, deps, count=False)
        for b in w:
            b["events"].sort(key=lambda e: e.get("at") or b["created"])
        work += w
        landed += ld
    q.log(f"beads: {len(work)} in flight, {len(landed)} landed recently")
    if only:
        work = [b for b in work if b["id"] == only]
        landed = [b for b in landed if b["id"] == only]
    blockers = blocker_info(beads, stores, {d["on"] for b in work for d in b["deps"]})
    work_by_id = {b["id"]: b for b in work}
    holders = defaultdict(list)  # session name -> beads assigned to it
    for b in work:
        if b["assignee"]:
            for n in session_names(city.session(b["assignee"])) or [b["assignee"]]:
                holders[n].append(b["id"])
    stops, moving, backlog = [], [], Counter()
    for b in work:
        r = classify(b, city, blockers, holders, now, work_by_id)
        if r is None:
            backlog[b["rig"]] += 1
        elif r["class"] in MOVING:
            moving.append(r)
        else:
            stops.append(r)
    stops += [r for r in (landed_stop(b, now) for b in landed) if r]
    mail = read_mail(beads, hq, now)
    seats = seat_overview(city, mail, ev, now)
    if not only:
        stops += seat_stops(city, ev, now) + mail_stops(city, mail, now)
    for r in stops + moving:
        r["actor"] = city.alias(r["actor"])
    q.log(f"{len(stops)} stops, {len(moving)} moving")
    host, hs = host_stop(city.sessions, now)
    if hs and not only:
        stops.append(hs)
    sources = {"beads": beads.mode, "stores": [s.name for s in stores], "events_tz": f"{clock_note}: {clock.note()}",
               "sessions": len(city.sessions), "agents": len(city.agents), "always_on_seats": len(city.named),
               "event_log_offset": ev.get("offset"), "verify_labels_used": info["verify"], "errors": errors}
    return {"stops": stops, "moving": moving, "backlog": dict(backlog), "host": host, "sources": sources, "seats": seats,
            "work": work, "landed": landed, "state": state, "city": city}


# ---------- history: time per class ----------

def day_file(day):
    return os.path.join(STOPS_DIR, f"{day}.jsonl")


def record_line(now, part, host, stops):
    return {"at": q.iso(now), "part": part, "load": host["load1"],
            "n": dict(Counter(s["class"] for s in stops)),
            "s": [[s["id"], s["class"], s["actor"], s["since"]] for s in stops]}


def class_days(now, days=7):
    """Per local day: stop-hours per class (each run stands for the time to the next run, at most
    two intervals) and how much of the day the census covered, beads and seats ('full') and the
    host alone ('host')."""
    names = [q.day_of(now - timedelta(days=d)) for d in range(days - 1, -1, -1)]
    lines = []
    for day in names:
        try:
            with open(day_file(day)) as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    r["_at"] = q.as_utc(r.get("at"))
                    if r["_at"]:
                        lines.append((day, r))
        except OSError:
            continue
    lines.sort(key=lambda t: t[1]["_at"])
    cap = timedelta(minutes=2 * INTERVAL_MIN)
    out = {d: {"day": d, "runs": 0, "full_h": 0.0, "host_h": 0.0, "h": defaultdict(float)} for d in names}
    for i, (day, r) in enumerate(lines):
        nxt = lines[i + 1][1]["_at"] if i + 1 < len(lines) else now
        dt = min(nxt - r["_at"], cap).total_seconds() / 3600
        o = out[day]
        o["runs"] += 1
        o["host_h"] += dt
        if r.get("part") == "full":
            o["full_h"] += dt
        for cls, n in (r.get("n") or {}).items():
            if r.get("part") == "full" or KIND.get(cls) == "host":
                o["h"][cls] += n * dt
    res = []
    for d in names:
        o = out[d]
        start, end = q.day_bounds(d)
        span = (min(end, now) - start).total_seconds() / 3600
        res.append({"day": d, "runs": o["runs"], "coverage_full": round(o["full_h"] / span, 3) if span > 0 else None,
                    "coverage_host": round(o["host_h"] / span, 3) if span > 0 else None,
                    "h": {k: round(v, 2) for k, v in o["h"].items()}})
    return res


def prune(now):
    keep = {q.day_of(now - timedelta(days=d)) for d in range(q.DAYS)}
    try:
        for n in os.listdir(STOPS_DIR):
            if n.endswith(".jsonl") and n[:-6] not in keep:
                os.remove(os.path.join(STOPS_DIR, n))
    except OSError:
        pass


# ---------- output ----------

def class_summary(stops, days):
    by = defaultdict(list)
    for s in stops:
        by[s["class"]].append(s)
    out = []
    for k, kind, label in CLASSES:
        xs = by.get(k, [])
        ages = [s["age_h"] for s in xs if s["age_h"] is not None]
        out.append({"key": k, "kind": kind, "label": label, "n": len(xs), "oldest_h": max(ages) if ages else None,
                    "actors": Counter(s["actor"] for s in xs).most_common(3),
                    "h_7d": round(sum(d["h"].get(k, 0) for d in days), 1)})
    return out


def write_census(now, part, c, previous):
    """stops.json for the page; a host-only run keeps the last full census's bead and seat stops."""
    stops = c["stops"]
    if part == "host":
        kept = [s for s in previous.get("stops", []) if s.get("kind") != "host"]
        page_stops, moving, backlog = kept + stops, previous.get("moving", []), previous.get("backlog", {})
        full_at, seats = previous.get("full_at"), previous.get("seats", [])
    else:
        page_stops, moving, backlog, full_at, seats = stops, c["moving"], c["backlog"], q.iso(now), c["seats"]
    os.makedirs(STOPS_DIR, exist_ok=True)
    with open(day_file(q.day_of(now)), "a") as f:
        f.write(json.dumps(record_line(now, part, c["host"], stops), separators=(",", ":")) + "\n")
    days = class_days(now)
    rank = {k: i for i, (k, _, _) in enumerate(CLASSES)}
    page_stops.sort(key=lambda s: (rank.get(s["class"], 99), -(s.get("age_h") or 0)))
    moving.sort(key=lambda s: -(s.get("age_h") or 0))
    out = {"generated_at": q.iso(now), "timezone": q.LOCAL_NAME, "part": part, "full_at": full_at,
           "interval_min": INTERVAL_MIN, "host": c["host"], "stops": page_stops, "moving": moving, "backlog": backlog, "seats": seats,
           "classes": class_summary(page_stops, days), "moving_labels": MOVING, "days": days, "sources": c["sources"],
           "config": {"max_load": MAX_LOAD, "load_stop": LOAD_STOP, "grace_min": GRACE_MIN, "idle_min": IDLE_MIN,
                      "resets": RESETS, "verify_h": VERIFY_H, "reviewers": REVIEWERS, "watchers": WATCHERS,
                      "router": ROUTER, "human": HUMAN, "human_prefixes": q.HUMAN_PREFIXES}}
    q.write_json(os.path.join(q.OUT_DIR, "stops.json"), out)
    prune(now)
    return out


def log_run(msg):
    try:
        os.makedirs(q.STATE_DIR, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(f"{q.iso(now_utc())} {msg}\n")
    except OSError:
        pass


def print_census(c):
    for s in c["stops"]:
        print(f"{s['class']:18} {s['id'][:34]:34} {(s.get('rig') or ''):16} actor={s['actor'][:34]:34} "
              f"age={s['age_h'] if s['age_h'] is not None else '?':>7} h  {s['why'][:120]}")
    for s in c["moving"]:
        print(f"{'(' + s['class'] + ')':18} {s['id'][:34]:34} {(s.get('rig') or ''):16} actor={s['actor'][:34]:34} "
              f"age={s['age_h'] if s['age_h'] is not None else '?':>7} h  {s['why'][:120]}")
    print(f"backlog (not routed, not held): {sum(c['backlog'].values())} {dict(c['backlog'])}")
    for r in c.get("seats", []):
        print(f"seat {r['seat'][:40]:40} {'always' if r['always_on'] else '':6} {r['state'][:22]:22} last active "
              f"{r['last_active']} unread {r['unread']} resets today {r['resets_today']}")
    h = c["host"]
    print(f"host: load {h['load1']} on {h['cores']} cores (stop above {h['threshold']}); top {h['top']}")
    print("sources:", json.dumps({k: v for k, v in c["sources"].items()}, default=str)[:1500])


def explain(c, bead_id):
    for b in c["work"]:
        print(json.dumps({k: v for k, v in b.items() if k not in ("events",)}, default=str, indent=1))
        print(f"events ({len(b['events'])}):")
        for e in b["events"]:
            print(f"  {q.iso(e.get('at'))} {e['type']:14} {e['actor'][:30]:30} "
                  f"{json.dumps({k: v for k, v in e['nv'].items() if k not in ('description', 'design', 'notes')}, default=str)[:200]}")
    for b in c["landed"]:
        print(json.dumps(b, default=str))
    if not c["work"] and not c["landed"]:
        print(f"{bead_id}: not in flight (closed, a skipped type, or in no store)")
    print_census(c)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--bead")
    args = ap.parse_args()
    if not q.CITY:
        sys.exit("quality-stops: no city (set GC_CITY_PATH or run inside a city)")
    adopt_conventions()
    now = now_utc()
    if args.bead:
        explain(census(now, only=args.bead), args.bead)
        return 0
    load1 = os.getloadavg()[0]
    t0 = time.monotonic()
    if load1 > MAX_LOAD and not args.dry_run:
        # the gate: no beads and no seats. The load is what a census at this moment has to say, so
        # its host part runs: one /proc pass (about a second) and, to name the seats behind the
        # load, one `gc session list`
        sessions = []
        if load1 > LOAD_STOP:
            d, _ = gc_json(["session", "list", "--json"])
            sessions = (d or {}).get("sessions", [])
        host, hs = host_stop(sessions, now)
        previous = q.load_json(os.path.join(q.OUT_DIR, "stops.json"), {})
        c = {"stops": [hs] if hs else [], "host": host,
             "sources": dict(previous.get("sources") or {}, skipped=f"load {load1:.1f} > {MAX_LOAD:g}: host part only")}
        write_census(now, "host", c, previous)
        log_run(f"part=host load={load1:.2f} max={MAX_LOAD:g} secs={time.monotonic() - t0:.1f}")
        print(f"quality-stops: load {load1:.1f} > {MAX_LOAD:g}: host part only")
        return 0
    c = census(now)
    if args.dry_run:
        print_census(c)
        q.log("dry run: nothing written")
        return 0
    previous = q.load_json(os.path.join(q.OUT_DIR, "stops.json"), {})
    write_census(now, "full", c, previous)
    q.write_json(STATE, c["state"], compact=True)
    log_run(f"part=full load={load1:.2f} stops={len(c['stops'])} moving={len(c['moving'])} "
            f"errors={len(c['sources']['errors'])} secs={time.monotonic() - t0:.1f}")
    print(f"quality-stops: {len(c['stops'])} stop(s), {len(c['moving'])} moving, backlog {sum(c['backlog'].values())}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
