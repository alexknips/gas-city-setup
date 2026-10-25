#!/usr/bin/env python3
"""soak-summary.py TSV: measurement 5 numbers from a watchdog.py sample file (herdr procs, zombies, pids, box CPU, host load).
Run: python3 -I -B soak-summary.py data/samples-item6.tsv"""
import csv
import sys
from datetime import datetime, timezone


def pct(vals, p):
    s = sorted(vals)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


def stat(name, vals):
    print(f"{name:<14} min {min(vals):>7.2f}  mean {sum(vals) / len(vals):>7.2f}  p95 {pct(vals, 95):>7.2f}  max {max(vals):>7.2f}")


rows = [r for r in csv.DictReader(open(sys.argv[1]), delimiter="\t") if r["ts"][:2] == "20"]
if not rows:
    sys.exit("no samples")
t = [datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) for r in rows]
hours = (t[-1] - t[0]).total_seconds() / 3600
print(f"{sys.argv[1]}: {len(rows)} samples, {t[0]:%F %T}Z to {t[-1]:%F %T}Z ({hours:.2f} h)")
for col in ("herdr", "zombies", "claude", "pids"):
    stat(col, [float(r[col]) for r in rows])
stat("box_cpu_inst", [float(r["box_cpu_inst"]) for r in rows])
stat("box_cpu_5m", [float(r["box_cpu_5m"]) for r in rows])
stat("host_load1", [float(r["host_load1"]) for r in rows])
near = [r["ts"] for r in rows if r["near"]]
print(f"samples within 20% of a kill limit: {len(near)}" + (f" (first {near[0]}, last {near[-1]})" if near else ""))
gaps = [(t[i + 1] - t[i]).total_seconds() for i in range(len(t) - 1)]
print(f"largest gap between samples: {max(gaps) if gaps else 0:.0f} s (30 s expected)")
