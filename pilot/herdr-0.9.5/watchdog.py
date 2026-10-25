#!/usr/bin/env python3
"""Kill switch and sampler for the gcs-78y herdr pilot box. Runs on the HOST.

Mayor ruling 2026-10-25 (mail gc-wisp-ap22v): kill the box (docker kill, and pkill its herdr) when ANY holds:
  a  herdr processes in the box > 50
  b  zombie processes in the box > 20
  c  box pids > 1000                      (the box cap is 1500)
  d  box CPU >= 3.5 cores, 5 min average  (the box cap is 4)
  e  host load1 > 20 AND box CPU > 1 core, both 5 min averages (the pilot is contributing)
  f  host load1 > 60, whoever causes it   (host emergency backstop)
Every sample goes to the TSV; the `near` column lists each rule within 20% of its limit. A kill writes a KILL
line with the sampler line that tripped it.

Sampling reads the container's cgroup (cgroup v2, systemd driver) and /proc, never docker: the first pilot run
showed a docker daemon starved at host load 100, which would have disabled a docker-based watchdog. Only the
kill itself tries docker (with a timeout), then falls back to SIGKILL on every pid of the cgroup.

usage: watchdog.py TSV [--name gcs78y-pilot] [--every 30] [--dry-run]
"""
import argparse
import os
import signal
import subprocess
import sys
import time
from collections import deque

LIMITS = {"a": 50, "b": 20, "c": 1000, "d": 3.5, "e_load": 20.0, "e_cpu": 1.0, "f": 60.0}
WINDOW = 300          # seconds the CPU and load averages cover
MIN_SPAN = 90         # do not judge d/e on a window shorter than this (first samples after a start)
HEADER = ["ts", "herdr", "zombies", "claude", "pids", "box_cpu_inst", "box_cpu_5m", "host_load1",
          "host_load1_5m", "near"]


def docker(*args, timeout=15):
    try:
        return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        return ""


def cgroup_dir(name):
    cid = docker("inspect", "-f", "{{.Id}}", name)
    if not cid:
        sys.exit(f"watchdog: cannot resolve container {name} (docker unresponsive or box not created)")
    path = f"/sys/fs/cgroup/system.slice/docker-{cid}.scope"
    if not os.path.isdir(path):
        sys.exit(f"watchdog: no cgroup at {path}")
    return path


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def box_pids(cg):
    return [int(p) for p in read(f"{cg}/cgroup.procs").split()]


def proc_state(pid):
    """(comm, state) of a pid from /proc; ('', '') if it is gone."""
    stat = read(f"/proc/{pid}/stat")
    if not stat:
        return "", ""
    # comm sits in parentheses and may hold spaces: split on the last ')'
    head, _, tail = stat.rpartition(")")
    return head.partition("(")[2], tail.split()[0] if tail.split() else ""


def cpu_usec(cg):
    for line in read(f"{cg}/cpu.stat").splitlines():
        if line.startswith("usage_usec"):
            return int(line.split()[1])
    return 0


def kill_box(name, cg):
    """docker kill with a timeout (it may hang on a starved daemon), then SIGKILL what is still in the cgroup."""
    subprocess.Popen(["docker", "kill", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    for pid in box_pids(cg):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass   # not ours (the container's root-owned init) or already gone; docker kill takes those


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tsv")
    ap.add_argument("--name", default="gcs78y-pilot")
    ap.add_argument("--every", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true", help="evaluate and log, never kill")
    args = ap.parse_args()

    cg = cgroup_dir(args.name)
    if not os.path.exists(args.tsv) or os.path.getsize(args.tsv) == 0:
        with open(args.tsv, "w") as f:
            f.write("\t".join(HEADER) + "\n")
    hist = deque()   # (t, cpu_usec, load1)
    print(f"watchdog: {args.name} cgroup {cg}, every {args.every}s -> {args.tsv}", flush=True)

    while os.path.isdir(cg) and box_pids(cg):
        t = time.time()
        pids = box_pids(cg)
        comms = [proc_state(p) for p in pids]
        herdr = sum(1 for c, _ in comms if c == "herdr")
        zomb = sum(1 for _, s in comms if s == "Z")
        claude = sum(1 for c, _ in comms if c == "claude")
        load1 = float(read("/proc/loadavg").split()[0])
        usec = cpu_usec(cg)
        hist.append((t, usec, load1))
        while len(hist) > 2 and t - hist[1][0] >= WINDOW:
            hist.popleft()
        t0, u0, _ = hist[0]
        span = t - t0
        inst = (usec - hist[-2][1]) / 1e6 / max(t - hist[-2][0], 1e-9) if len(hist) > 1 else 0.0
        cpu5 = (usec - u0) / 1e6 / span if span > 0 else 0.0
        load5 = sum(h[2] for h in hist) / len(hist)
        judged = span >= MIN_SPAN

        trips = []
        if herdr > LIMITS["a"]: trips.append("a:herdr>50")
        if zomb > LIMITS["b"]: trips.append("b:zombies>20")
        if len(pids) > LIMITS["c"]: trips.append("c:pids>1000")
        if judged and cpu5 >= LIMITS["d"]: trips.append("d:cpu5m>=3.5")
        if judged and load5 > LIMITS["e_load"] and cpu5 > LIMITS["e_cpu"]: trips.append("e:load5m>20&cpu5m>1")
        if load1 > LIMITS["f"]: trips.append("f:load1>60")

        near = []
        if herdr >= 0.8 * LIMITS["a"]: near.append("a")
        if zomb >= 0.8 * LIMITS["b"]: near.append("b")
        if len(pids) >= 0.8 * LIMITS["c"]: near.append("c")
        if cpu5 >= 0.8 * LIMITS["d"]: near.append("d")
        if load5 >= 0.8 * LIMITS["e_load"] and cpu5 >= 0.8 * LIMITS["e_cpu"]: near.append("e")
        if load1 >= 0.8 * LIMITS["f"]: near.append("f")

        row = [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)), herdr, zomb, claude, len(pids),
               f"{inst:.2f}", f"{cpu5:.2f}", f"{load1:.2f}", f"{load5:.2f}", ",".join(near)]
        line = "\t".join(str(x) for x in row)
        with open(args.tsv, "a") as f:
            f.write(line + "\n")
            if trips:
                f.write(f"KILL\t{','.join(trips)}\ttripped by: {line}\n")
        if trips:
            print(f"watchdog: KILL {','.join(trips)} <- {line}", flush=True)
            if not args.dry_run:
                # keeper.sh reads this to decide whether a restart is allowed (load-class kills only)
                with open(os.path.join(os.path.dirname(os.path.abspath(args.tsv)), "last-kill.txt"), "w") as f:
                    f.write(f"{int(t)}\t{','.join(trips)}\t{line}\n")
                kill_box(args.name, cg)
                sys.exit(3)
        time.sleep(args.every)
    print("watchdog: box is gone; ends", flush=True)


if __name__ == "__main__":
    main()
