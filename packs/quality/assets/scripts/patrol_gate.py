#!/usr/bin/env python3
"""Patrol gate for a Gas City (the `quality` pack's quality-patrol-gate order; README.md, "Patrol gate").

No model and no tokens. A patrol seat costs the same whether it finds something or not, so this
order looks for what the seats are woken to find and wakes one only when there is something:

- witness: a census stop on a bead in its rig that a watcher clears (QUALITY_GATE_WITNESS_CLASSES),
  once per stop; else once every QUALITY_GATE_HEARTBEAT_H hours as a backstop.
- stuck prompt: a pane of a seat in QUALITY_GATE_ENTER_ROLES whose input box holds gc's own wake
  prompt (QUALITY_GATE_PROMPT_RE), idle and unchanged since the previous run: press Enter, at most
  twice per prompt. Any other text in a box is never touched.
- deacon: a dialog open in a seat's pane, unsent text that is not gc's, a prompt two Enters did not
  clear, free disk under QUALITY_GATE_DISK_MIN_GB or falling faster than QUALITY_GATE_DISK_DROP_GBH.

Each part is off, shadow (log what it would do, the default) or live: QUALITY_GATE_WITNESS,
QUALITY_GATE_ENTER, QUALITY_GATE_DEACON. QUALITY_GATE=off (the default) makes the order do nothing.
Every run appends one JSON line to $QUALITY_STATE_DIR/patrol-gate.jsonl.

    patrol_gate.py               one run, as the order runs it
    patrol_gate.py --dry-run     print what a run would do; act on nothing, write nothing
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import quality as q  # noqa: E402
import stops as s  # noqa: E402  (the census: one reading of beads, sessions and who clears each stop)

GATE = q.env("QUALITY_GATE", "off").lower()
MODES = {part: q.env(f"QUALITY_GATE_{part.upper()}", "shadow").lower() for part in ("witness", "enter", "deacon")}
STATE = os.path.join(q.STATE_DIR, "patrol-gate-state.json")
LOG = os.path.join(q.STATE_DIR, "patrol-gate.jsonl")
MAX_LOAD = float(q.env("QUALITY_GATE_MAX_LOAD", "40"))  # above it the census part waits; panes and disk still run
WITNESS_CLASSES = q.env_list("QUALITY_GATE_WITNESS_CLASSES",
                             "assignee_down,assignee_idle,review_idle,review_down,bounced,routed_unclaimed")
HEARTBEAT_H = float(q.env("QUALITY_GATE_HEARTBEAT_H", "6"))
HEARTBEATS_PER_RUN = int(q.env("QUALITY_GATE_HEARTBEATS_PER_RUN", "1"))  # staggered: patrols starting together load the host
WAKE_TEXT = q.env("QUALITY_GATE_WAKE_TEXT", "Run 'gc prime' to check worker status and begin patrol cycle.")
DEACON = q.env("QUALITY_GATE_DEACON_SEAT", "deacon")  # base name of the seat that takes what no rule clears
DEACON_EVERY_MIN = float(q.env("QUALITY_GATE_DEACON_EVERY_MIN", "30"))
ENTER_ROLES = q.env_list("QUALITY_GATE_ENTER_ROLES", "witness,refinery,deacon,dog,boot")
# gc's wake prompts ("Run 'gc prime' to check worker status ..." for a witness, "... check merge
# queue ..." for a refinery), its start prompt, and a start prompt Claude Code folded into a paste
PROMPT_RE = re.compile(q.env("QUALITY_GATE_PROMPT_RE",
                             r"^Run 'gc prime' to check |^\[gc\] \S+ • \d{4}-|^\[Pasted text #\d+"))
BOX_CHAR = q.env("QUALITY_GATE_BOX_CHAR", "❯")
ENTER_PAUSE_S = float(q.env("QUALITY_GATE_ENTER_PAUSE_S", "3"))
BUSY_RE = re.compile(q.env("QUALITY_GATE_BUSY_RE", r"esc to interrupt"))
DIALOG_RE = re.compile(q.env("QUALITY_GATE_DIALOG_RE", r"0: Dismiss|Do you want|Yes, and|Enter to select|Esc to cancel"))
TMUX = q.env("QUALITY_TMUX") or shutil.which("tmux") or ""
TMUX_SOCKET = q.env("QUALITY_TMUX_SOCKET", "gc")
DISK_PATH = q.env("QUALITY_GATE_DISK_PATH", "/")
DISK_MIN_GB = float(q.env("QUALITY_GATE_DISK_MIN_GB", "40"))
DISK_DROP_GBH = float(q.env("QUALITY_GATE_DISK_DROP_GBH", "15"))
ESC = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def role(name):
    return s.base(name or "")


def nudge(seat, text, mode, errors):
    """Nudge a seat when the part is live; in shadow only say that it would."""
    if mode != "live":
        return "would nudge"
    out = subprocess.run([q.GC, "session", "nudge", seat, text], capture_output=True, text=True, timeout=180,
                         cwd=q.CITY or None)
    if out.returncode != 0:
        errors.append(f"nudge {seat}: {out.stderr.strip()[-200:]}")
        return "nudge failed"
    return "nudged"


# ---------- witnesses: wake on a stop they clear, else on the heartbeat ----------

def witness_part(c, st, now, errors):
    city = c["city"]
    if not any(s.up(x) for x in city.sessions):
        # gc lists every session asleep when it cannot reach tmux: that would read as every
        # assignee down and every witness due, so wake nobody
        errors.append(f"witness: none of {len(city.sessions)} sessions is up; skipped")
        return []
    woken = st.setdefault("woken", {})
    last = st.setdefault("last_wake", {})
    found = {}
    for r in c["stops"]:
        if r.get("kind") != "bead" or r["class"] not in WITNESS_CLASSES or r["rig"] in city.rig_suspended:
            continue
        w = city.watcher(r["rig"])
        if role(w) not in s.WATCHERS:
            continue  # no watcher seat on that rig: the router has it, as the census says
        key = f"{r['id']}|{r['class']}|{r['since']}"
        if key in woken:
            continue
        found.setdefault(w, []).append((key, r))
    out = []
    for w, rs in sorted(found.items()):
        lines = [f"{r['id']} {s.LABEL.get(r['class'], r['class'])} since {r['since'][11:16]}Z ({r['why'][:120]})"
                 for _, r in rs]
        text = WAKE_TEXT + " Patrol gate found: " + "; ".join(lines)
        did = nudge(w, text, MODES["witness"], errors)
        out.append({"seat": w, "why": "stops", "stops": [k for k, _ in rs], "did": did})
        if did != "nudge failed":
            for k, _ in rs:
                woken[k] = q.iso(now)
            last[w] = q.iso(now)
    # the heartbeat: every witness seat of an awake rig, longest idle first
    due = []
    for n in city.named:
        if role(n.get("template")) not in s.WATCHERS or (n.get("dir") or "") in city.rig_suspended:
            continue
        w, _ = city.agent_of_named(n)
        if not w or w in found:
            continue
        seen = [t for t in (s.last_active(city.session(w)), q.as_utc(last.get(w))) if t]
        idle_h = s.hrs(max(seen), now) if seen else None
        if idle_h is None or idle_h >= HEARTBEAT_H:
            due.append((-(idle_h or 1e9), w, idle_h))
    for _, w, idle_h in sorted(due)[:HEARTBEATS_PER_RUN]:
        did = nudge(w, WAKE_TEXT + f" (Patrol gate: {HEARTBEAT_H:g} h heartbeat, nothing found.)", MODES["witness"], errors)
        out.append({"seat": w, "why": "heartbeat", "idle_h": idle_h, "did": did})
        if did != "nudge failed":
            last[w] = q.iso(now)
    cut = now - timedelta(days=3)
    st["woken"] = {k: v for k, v in woken.items() if (q.as_utc(v) or now) >= cut}
    return out


# ---------- panes: gc's own prompt left unsent, dialogs, other unsent text ----------

def tmux(*args):
    return subprocess.run([TMUX, "-u", "-L", TMUX_SOCKET, *args], capture_output=True, text=True, timeout=20)


def read_pane(target):
    """(text in the input box, busy, dialog open) of one pane, or None when it cannot be read.
    Claude Code draws the box's placeholder dim; that part is not text anyone typed."""
    p = tmux("capture-pane", "-e", "-p", "-t", target)
    return parse_pane(p.stdout) if p.returncode == 0 else None


def parse_pane(text):
    box = next((ln for ln in reversed(text.splitlines()) if BOX_CHAR in ln), "")
    box = ESC.sub("", box.split("\x1b[2m", 1)[0]).replace(BOX_CHAR, "", 1)
    box = box.replace("\u00a0", " ").strip()  # an empty box is padded with no-break spaces
    plain = ESC.sub("", text)
    return box, bool(BUSY_RE.search(plain)), bool(DIALOG_RE.search("\n".join(plain.splitlines()[-15:])))


def pane_part(city, st, now, errors, findings):
    if not TMUX:
        errors.append("panes: no tmux (set QUALITY_TMUX)")
        return []
    panes = st.setdefault("panes", {})
    out, seen = [], set()
    for x in city.sessions:
        target, seat = x.get("session_name"), x.get("alias") or x.get("agent_name") or ""
        if not target or not s.up(x) or role(seat) not in ENTER_ROLES:
            continue
        r = read_pane(target)
        if r is None:
            continue
        seen.add(target)
        box, busy, dialog = r
        if dialog:
            findings.append(f"{seat}: a dialog is open in its pane")
        if not box or busy:
            panes.pop(target, None)
            continue
        key = hashlib.sha1(box.encode()).hexdigest()[:12]
        prev = panes.get(target)
        if not prev or prev.get("key") != key:
            panes[target] = {"key": key, "since": q.iso(now), "enters": 0}
            continue  # first sight: the prompt may still be on its way in
        if not PROMPT_RE.search(box):
            if not prev.get("told"):
                findings.append(f"{seat}: unsent text in its box since {prev['since'][11:16]}Z: {box[:80]!r}")
                prev["told"] = True
            continue
        if prev["enters"] >= 2:
            if not prev.get("told"):
                findings.append(f"{seat}: gc's prompt still unsent after 2 Enters: {box[:80]!r}")
                prev["told"] = True
            continue
        if MODES["enter"] != "live":
            if not prev.get("logged"):  # shadow: say it once per prompt; no Enter, so nothing to escalate
                out.append({"seat": seat, "pane": target, "box": box[:80], "since": prev["since"], "did": "would press Enter"})
                prev["logged"] = True
            continue
        # a long-idle box often swallows the first Enter: look again a moment later and press once more
        did = "pressed Enter, still unsent"
        while prev["enters"] < 2:
            e = tmux("send-keys", "-t", target, "Enter")
            prev["enters"] += 1
            if e.returncode != 0:
                did = f"Enter failed: {e.stderr.strip()[:100]}"
                break
            time.sleep(ENTER_PAUSE_S)
            again = read_pane(target)
            if not again or again[0] != box:
                did = f"pressed Enter x{prev['enters']}, sent"
                break
        out.append({"seat": seat, "pane": target, "box": box[:80], "since": prev["since"], "did": did})
    for t in list(panes):
        if t not in seen:
            del panes[t]
    return out


# ---------- the host's disk ----------

def disk_part(st, now, findings):
    try:
        free = shutil.disk_usage(DISK_PATH).free / 1e9
    except OSError as e:
        return {"error": str(e)}
    hist = [h for h in st.get("disk", []) if now.timestamp() - h[0] <= 3 * 3600] + [[int(now.timestamp()), round(free, 1)]]
    st["disk"] = hist
    old = [h for h in hist if now.timestamp() - h[0] >= 1800]
    rate = None
    if old:
        t0, f0 = old[-1]
        rate = round((f0 - free) / ((now.timestamp() - t0) / 3600), 1)
    if free < DISK_MIN_GB:
        findings.append(f"disk: {free:.0f}G free on {DISK_PATH} (under {DISK_MIN_GB:g}G)")
    elif rate is not None and rate >= DISK_DROP_GBH:
        findings.append(f"disk: {free:.0f}G free on {DISK_PATH}, falling {rate:g} G/h")
    return {"free_gb": round(free, 1), "drop_gbh": rate}


def deacon_part(city, st, now, findings, errors):
    if not findings:
        return None
    seat = next((a for a in city.agents if role(a) == DEACON and "/" not in a), None)
    if not seat:
        errors.append(f"deacon: no agent named {DEACON}")
        return None
    last = q.as_utc(st.get("deacon_last"))
    known = set(st.get("deacon_told", []))
    new = [f for f in findings if f not in known]
    if not new and last and s.hrs(last, now) * 60 < DEACON_EVERY_MIN:
        return {"seat": seat, "findings": findings, "did": "told already"}
    did = nudge(seat, "PATROL GATE: " + "; ".join(findings)[:900] + " Look at these, then end the turn.", MODES["deacon"], errors)
    if did != "nudge failed":
        st["deacon_last"] = q.iso(now)
        st["deacon_told"] = findings
    return {"seat": seat, "findings": findings, "did": did}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if GATE in ("off", "no", "0", "false") and not args.dry_run:
        return 0
    if not q.CITY:
        sys.exit("patrol-gate: no city (set GC_CITY_PATH or run inside a city)")
    if args.dry_run:
        for part in MODES:
            MODES[part] = "shadow"
    s.adopt_conventions()
    now, t0, load1 = s.now_utc(), time.monotonic(), os.getloadavg()[0]
    st = q.load_json(STATE, {})
    errors, findings, rec = [], [], {"at": q.iso(now), "load": round(load1, 2), "modes": dict(MODES)}
    if load1 <= MAX_LOAD:
        # the census as the quality-stops order runs it, but its state is only read: that order owns
        # its event-log offset, and a run here must not move it
        c = s.census(now)
        errors += c["sources"]["errors"]
        city = c["city"]
        rec["witness"] = witness_part(c, st, now, errors)
    else:
        rec["witness"] = f"skipped: load {load1:.1f} > {MAX_LOAD:g}"
        city = s.City([], errors, q.load_json(s.STATE, {}), now)
    rec["enter"] = pane_part(city, st, now, errors, findings)
    rec["disk"] = disk_part(st, now, findings)
    rec["deacon"] = deacon_part(city, st, now, findings, errors)
    rec["errors"] = errors
    rec["secs"] = round(time.monotonic() - t0, 1)
    if args.dry_run:
        print(json.dumps(rec, indent=1, default=str))
        return 0
    q.write_json(STATE, st, compact=True)
    with open(LOG, "a") as f:
        f.write(json.dumps(rec, default=str) + "\n")
    acts = (len(rec["witness"]) if isinstance(rec["witness"], list) else 0) + len(rec["enter"]) + bool(rec["deacon"])
    print(f"patrol-gate: {acts} wake(s) or Enter(s), modes {MODES}, {rec['secs']} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
