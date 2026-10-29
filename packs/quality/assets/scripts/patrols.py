#!/usr/bin/env python3
"""Patrol census for a Gas City (the `quality` pack; README.md, "What patrols cost and find").

No model and no tokens. Reads the Claude Code transcripts of the city's patrol seats (witnesses,
deacon, boot, dogs by default) and splits each session into wakes: a wake starts with a turn gc or
an order put into the session (a patrol prompt, a nudge, a fresh start) and runs to the next one.
For each wake: when, which seat, the model, the tokens and list-price dollars of every assistant
message in it (subagents included), and what it did besides reading: nudges, mail, bead changes,
recoveries. A wake that did none of those is "all clear".

    patrols.py                     the last 7 local days, a table per seat
    patrols.py --days 14           a longer window
    patrols.py --since 2026-10-22 --until 2026-10-29    local days, end exclusive
    patrols.py --json FILE         also write every wake (with its actions) to FILE
    patrols.py --seat SEAT         list that seat's wakes one per line (SEAT as in the table)
"""
import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as q  # noqa: E402  (same directory: session dirs, transcript and price readers)

UTC = q.UTC
ROLES = q.env_list("QUALITY_PATROL_ROLES", "witness,deacon,boot,dog")
# A typed turn that continues the wake before it instead of starting one.
CONTINUE = re.compile(q.env("QUALITY_PATROL_CONTINUE_RE",
                            r"This session is being continued from a previous conversation|"
                            r"usage limit has reset|^Continue\b"), re.I)
PATROL_WAKE = re.compile(q.env("QUALITY_PATROL_WAKE_RE", r"patrol|gc prime"), re.I)
START = re.compile(r"^\[[^\]\n]{1,80}\] \S+ • \d{4}-\d\d-\d\d")
SKIP_TURN = re.compile(r"^(<command-(name|message|args)>|<local-command-std(out|err)>|Caveat: |\[Request interrupted)")

# What a wake did besides reading, by the commands it ran. Order matters: a command is counted
# once per kind it matches.
ACTIONS = [
    ("nudge", re.compile(r"\bgc\s+(?:session\s+)?nudge\s+(?:--\S+\s+)*['\"]?([\w./@-]+)|\btmux\b[^|;&\n]*\bsend-keys\b[^|;&\n]*-t\s+(\S+)")),
    ("mail", re.compile(r"\bgc\s+mail\s+(?:send|reply)\s+(?:--?\S+\s+)*['\"]?([\w./@-]+)")),
    ("recover", re.compile(r"\bgc\s+session\s+(?:kill|restart|reset|close|wake|stop|suspend|resume)\s+['\"]?([\w./@-]+)|"
                           r"\bgc\s+(?:agent|rig)\s+(?:suspend|resume|drain)\s+['\"]?([\w./@-]+)|"
                           r"\bgit\b[^|;&\n]*\bpush\b[^|;&\n]*?(\S+)?$")),
    ("bead", re.compile(r"\b(?:gc\s+)?bd\s+(?:update|close|reopen|assign)\s+['\"]?([\w.-]+)|"
                        r"\b(?:gc\s+)?bd\s+create\b()|\bgc\s+sling\s+(?:--?\S+\s+)*['\"]?([\w./@-]+)")),
    ("order", re.compile(r"\bgc\s+order\s+run\s+([\w.-]+)")),
]
# A bead change that only maintains the patrol itself: its own wisp, a mail it read or archived.
# A command that appends to a notes file (a handoff, a memory) only records what the wake did.
NOTES_WRITE = re.compile(r">>\s*['\"]?\S+\.md\b|\btee\s+-a\s+\S+\.md\b")
SELF_BEAD = re.compile(r"^\$|^\"?\$|^-|wisp", re.I)
NOTES_ONLY = re.compile(r"--(?:append-)?notes\b")
STATE_CHANGE = re.compile(r"--(?:status|assignee|set-metadata|unset-metadata|claim|priority|parent)\b|\bclose\b|\breopen\b")
PATROL_TITLE = re.compile(r"\b([a-z][a-z0-9]*-wisp-[a-z0-9]+)\b[^\n]{0,8}(?:—|-)\s*mol-[a-z-]*patrol")


def role_of(seat):
    base = re.split(r"[/.]", seat)[-1]
    base = re.sub(r"-\d+$", "", base)
    return base if base in ROLES else None


def encoded(path):
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def turn_text(e):
    """The text of a typed user turn (one gc or a person put into the session), else None."""
    if e.get("type") != "user" or e.get("isSidechain") or e.get("isMeta"):
        return None
    c = (e.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        c = "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str) or not c.strip() or SKIP_TURN.match(c):
        return None
    return c


def trigger_kind(text):
    if CONTINUE.search(text[:400]):
        return "continue"
    if START.match(text):
        return "start"
    if PATROL_WAKE.search(text[:600]):
        return "patrol"
    return "nudge"


def new_wake(seat, path, ts, text, kind):
    return {"seat": seat, "file": path, "start": ts, "end": ts, "kind": kind, "trigger": text[:300],
            "msgs": {}, "models": Counter(), "commands": [], "actions": [], "wisps": set(), "said": ""}


def add_usage(w, e):
    m = e.get("message") or {}
    u = m.get("usage")
    if not u or not m.get("id") or m.get("model") == "<synthetic>":
        return
    at = q.as_utc(e.get("timestamp"))
    if at is None:
        return
    cw = int(u.get("cache_creation_input_tokens") or 0)
    cw1 = int((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
    out = int(u.get("output_tokens") or 0)
    prev = w["msgs"].get(m["id"])
    if prev and prev[q.M_OUT] > out:
        return
    w["msgs"][m["id"]] = [int(at.timestamp()), m.get("model") or "", u.get("speed"), u.get("inference_geo"),
                          u.get("service_tier"), int(u.get("input_tokens") or 0), cw - cw1, cw1,
                          int(u.get("cache_read_input_tokens") or 0), out,
                          int((u.get("server_tool_use") or {}).get("web_search_requests") or 0)]


def read_main(path, seat, since, until):
    """The wakes of one main transcript (not a subagent's) that start inside the window."""
    wakes, cur = [], None
    with open(path, "rb") as f:
        for line in f:
            if b'"type":"user"' not in line and b'"type":"assistant"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts = q.as_utc(e.get("timestamp"))
            if ts is None:
                continue
            text = turn_text(e)
            if text is not None:
                kind = trigger_kind(text)
                if kind == "continue" and cur is not None:
                    continue
                cur = new_wake(seat, path, ts, text, kind)
                wakes.append(cur)
                continue
            if cur is None:  # lines before the first typed turn (a resumed file's replay)
                continue
            cur["end"] = max(cur["end"], ts)
            if e.get("type") == "assistant":
                add_usage(cur, e)
                for b in (e.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        cmd = (b.get("input") or {}).get("command")
                        if cmd:
                            cur["commands"].append(cmd)
                    elif isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip():
                        cur["said"] = b["text"]
            else:
                for b in (e.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        r = b.get("content")
                        if isinstance(r, list):
                            r = " ".join(x.get("text", "") for x in r if isinstance(x, dict))
                        for wid in PATROL_TITLE.findall(str(r or "")):
                            cur["wisps"].add(wid)
    return [w for w in wakes if since <= w["start"] < until]


def read_sub(path, wakes):
    """Add a subagent transcript's messages to the wake of its parent session they fall in."""
    starts = [w["start"] for w in wakes]
    with open(path, "rb") as f:
        for line in f:
            if b'"usage"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts = q.as_utc(e.get("timestamp"))
            if ts is None or e.get("type") != "assistant":
                continue
            i = max((k for k, s in enumerate(starts) if s <= ts), default=None)
            if i is not None:
                add_usage(wakes[i], e)


def classify(w):
    """The actions a wake took (kind, target, command excerpt), patrol upkeep left out."""
    out = []
    for cmd in w["commands"]:
        if NOTES_WRITE.search(cmd):
            continue
        for part in re.split(r"\n|;|&&|\|\|", cmd):
            part = part.strip()
            if not part or part.startswith("#") or "--help" in part:
                continue
            for kind, rx in ACTIONS:
                m = rx.search(part)
                if not m:
                    continue
                target = next((g for g in m.groups() if g), "")
                if kind == "bead":
                    if re.search(r"\bbd\s+(?:update|close)\b", part):
                        if target in w["wisps"] or SELF_BEAD.search(target):
                            continue  # its own patrol wisp, or a variable holding it
                        if NOTES_ONLY.search(part) and not STATE_CHANGE.search(part.split(target, 1)[-1]):
                            kind = "note"
                    if "mol " in part:
                        continue
                if kind == "recover" and "push" in part and "--dry-run" in part:
                    continue
                out.append((kind, target.strip("'\""), part[:160]))
    return out


def price(w, prices, memo):
    usd, unpriced, toks = 0.0, 0, Counter()
    for m in w["msgs"].values():
        t = {"input": m[q.M_IN], "cache_write": m[q.M_CW5] + m[q.M_CW1], "cache_read": m[q.M_CR], "output": m[q.M_OUT]}
        toks.update(t)
        w["models"][m[q.M_MODEL]] += 1
        p, _ = q.price_of(m, prices, memo)
        if p is None:
            unpriced += sum(t.values())
        else:
            usd += p
    return usd, unpriced, toks


def patrol_dirs(state):
    """work_dir -> seat of every session dir whose seat is a patrol role (from the metrics
    order's state; the census never queries the beads store)."""
    dirs = (state.get("session_dirs") or {})
    return {d: v["seat"] for d, v in dirs.items() if role_of(v["seat"])}


def transcript_files(dirs):
    """(path, seat, is_subagent) of every transcript launched in a patrol seat's dir."""
    want = {encoded(d): d for d in dirs}
    out = []
    for base in q.transcript_dirs():
        if not os.path.isdir(base):
            continue
        for proj in os.listdir(base):
            d = next((want[k] for k in want if proj == k or proj.startswith(k + "-")), None)
            if d is None:
                continue
            for dp, _, names in os.walk(os.path.join(base, proj)):
                for n in names:
                    if n.endswith(".jsonl"):
                        out.append((os.path.join(dp, n), dirs[d], os.path.basename(dp) == "subagents"))
    return out


def census(since, until):
    state = q.load_json(os.path.join(q.STATE_DIR, "state.json"), {})
    dirs = patrol_dirs(state)
    if not dirs:
        sys.exit("patrols: no patrol seat dirs in the metrics state (run the quality-metrics order first)")
    prices, src = q.load_prices()
    if prices is None:
        sys.exit(f"patrols: {src}")
    files = transcript_files(dirs)
    cut = since.timestamp() - 86400
    by_session = defaultdict(list)
    subs = []
    for path, seat, is_sub in files:
        try:
            if os.stat(path).st_mtime < cut:
                continue
        except OSError:
            continue
        if is_sub:
            subs.append(path)
            continue
        wakes = read_main(path, seat, since, until)
        by_session[q.session_of(path)] += wakes
    for path in subs:
        wakes = sorted(by_session.get(q.session_of(path), []), key=lambda w: w["start"])
        if wakes:
            read_sub(path, wakes)
    memo, all_wakes = {}, []
    for wakes in by_session.values():
        for w in wakes:
            w["usd"], w["unpriced"], w["tokens"] = price(w, prices, memo)
            w["actions"] = classify(w)
            all_wakes.append(w)
    return sorted(all_wakes, key=lambda w: w["start"]), src


ACTED = ("nudge", "mail", "recover", "bead", "order")


def acted(w):
    return any(a[0] in ACTED for a in w["actions"])


def summary(wakes):
    seats = defaultdict(list)
    for w in wakes:
        seats[w["seat"]].append(w)
    rows = []
    for seat, ws in seats.items():
        toks = [sum(w["tokens"].values()) for w in ws if w["msgs"]]
        usd = [w["usd"] for w in ws if w["msgs"]]
        kinds = Counter(a[0] for w in ws for a in w["actions"] if a[0] in ACTED)
        models = Counter()
        for w in ws:
            models.update(w["models"])
        rows.append({
            "seat": seat, "role": role_of(seat), "wakes": len(ws), "billed_wakes": len(toks),
            "by_trigger": dict(Counter(w["kind"] for w in ws)),
            "acted": sum(1 for w in ws if acted(w)), "all_clear": sum(1 for w in ws if w["msgs"] and not acted(w)),
            "tokens": sum(toks), "usd": round(sum(usd), 2),
            "tokens_per_wake_median": int(q.quantile(toks, 0.5) or 0) if toks else 0,
            "usd_per_wake_median": round(q.quantile(usd, 0.5) or 0, 2) if usd else 0,
            "usd_per_wake_mean": round(sum(usd) / len(usd), 2) if usd else 0,
            "minutes_median": round(q.quantile([(w["end"] - w["start"]).total_seconds() / 60 for w in ws], 0.5) or 0, 1),
            "actions": dict(kinds), "models": dict(models),
            "unpriced_tokens": sum(w["unpriced"] for w in ws),
        })
    return sorted(rows, key=lambda r: -r["usd"])


def local_day_start(day):
    return q.day_bounds(day)[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--since", help="first local day (YYYY-MM-DD)")
    ap.add_argument("--until", help="local day after the last one (YYYY-MM-DD)")
    ap.add_argument("--json", help="write every wake to this file")
    ap.add_argument("--seat", help="list one seat's wakes")
    args = ap.parse_args()
    today = q.day_of(datetime.now(UTC))
    until = local_day_start(args.until) if args.until else local_day_start(today)
    since = local_day_start(args.since) if args.since else until - timedelta(days=args.days)
    wakes, src = census(since, until)
    rows = summary(wakes)
    tot = sum(r["usd"] for r in rows)
    print(f"patrol seats {q.iso(since)} .. {q.iso(until)}: {len(wakes)} wakes, ${tot:.0f} at list price ({src})")
    print(f"{'seat':40s} {'wakes':>5s} {'acted':>5s} {'clear':>5s} {'$':>7s} {'$/wake':>7s} {'Mtok/wake':>9s} {'min':>5s}  actions / models")
    for r in rows:
        print(f"{r['seat']:40s} {r['wakes']:5d} {r['acted']:5d} {r['all_clear']:5d} {r['usd']:7.0f} {r['usd_per_wake_median']:7.2f} "
              f"{r['tokens_per_wake_median'] / 1e6:9.2f} {r['minutes_median']:5.0f}  {r['actions']} {dict(r['models'])} {r['by_trigger']}")
    if args.seat:
        for w in wakes:
            if w["seat"] != args.seat:
                continue
            acts = [f"{a[0]}:{a[1]}" for a in w["actions"] if a[0] in ACTED]
            print(f"{q.iso(w['start'])} {w['kind']:8s} ${w['usd']:6.2f} {sum(w['tokens'].values()) / 1e6:6.2f}M "
                  f"{(w['end'] - w['start']).total_seconds() / 60:5.1f}m {' '.join(acts) or 'ALL CLEAR'}  | {w['trigger'][:90]!r}")
    if args.json:
        out = [{**{k: w[k] for k in ("seat", "file", "kind", "trigger", "actions", "said")}, "start": q.iso(w["start"]),
                "end": q.iso(w["end"]), "usd": round(w["usd"], 4), "tokens": dict(w["tokens"]),
                "models": dict(w["models"]), "wisps": sorted(w["wisps"]), "messages": len(w["msgs"]),
                "commands": w["commands"]} for w in wakes]
        with open(args.json, "w") as f:
            json.dump({"since": q.iso(since), "until": q.iso(until), "seats": rows, "wakes": out}, f, indent=1)


if __name__ == "__main__":
    main()
