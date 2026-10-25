# Rehearsal log: gc 1.4.1 -> 1.5.1 (bead gcs-j47)

Everything here ran in the throwaway container `gcu-rehearsal` (`rehearsal/rehearse.sh`), never on the live town. The box holds
a copy of `/home/ubuntu/gc` (city.toml, packs, orders, formulas, commands, assets), the Dolt data (all nine databases, 7.3 GB,
copied 2026-10-25 02:05 CEST from the running town: crash-consistent), `~/.gc`, the live-identical supervisor unit and wrappers,
gc 1.4.1 / bd 1.2.2 / dolt 2.3.2 laid out at the brew paths, and a stub `claude` that idles. Times are UTC; the host's own rustc
builds pushed load to 15-69 several times, so a watchdog (`docker pause` above load 20, resume below 15) froze the box. Pauses
stop the box but not wall-clock timeouts; read the pause list before blaming a step for its duration.

## What was verified

| Check | Result |
|---|---|
| Town starts on 1.4.1 in the box | yes, ~19 tmux sessions; doctor baseline 138 passed / 6 warned / 0 failed |
| bd 1.3.2 migrates the Dolt schema | v53 -> v66 in every scope, **not automatically** (#5920): explicit `gc bd migrate schema` per scope, supervisor stopped. ~8 min on a quiet host (hivemind ~5 min, beadline 49 s, city 5 s, others 17-46 s) |
| City starts on 1.5.1 | yes: "City started under supervisor" in 4 min 55 s; `bd recompute-blocked` ran per scope (hivemind corrected 3 rows; neurofilament skipped: `needs a clean working set`, repaired by `apply.sh` with `gc bd dolt commit` + `bd recompute-blocked`) |
| `gc doctor --fix` | 3 min 41 s, non-interactive; re-pins `[imports.bd]`/`[imports.core]`, renames `[imports.gascity]` -> `[imports.gc]`, refetches `packs.lock`; `city.toml` untouched. After: 158 passed / 11 warned / 1 failed (advisory timeout), `blocking_failed` 0 |
| Overrides | all nine `[[orders.overrides]]` resolve; `orphan-sweep` live-tmux guard still resolves the core script and still intercepts (kept); mayor-mail-wake / deacon-patrol-wake / alex-daily `--dry-run` pass; model pins and patches identical (see RUNBOOK section 5) |
| `gc gc capture-decision` after the import rename | still resolves to the city pack's command: no collision |
| Rollback from a half-upgraded box (apply failed, town down) | **passed in one clean run**: `rollback.sh` rc 0, `POSTCHECK OK` (fails 0, warns 4), 14 min (03:51:32-04:05:35Z): stop 2 s, restore Dolt 2 min 41 s, offline heads equal MANIFEST (9/9), start on 1.4.1 2 min 22 s, verify 7 min (bd read in every scope 3 min, doctor 3 min) |
| Rollback from a running 1.5.1 city | steps 1-4 passed (stop 11 s, upgraded Dolt moved aside, backup restored 77 s, heads equal, 2 min 8 s); step 5 started 1.4.1 ("City started under supervisor"); the readiness test of that day was wrong (see failures) and was replaced. Re-run as a whole: **see "Final passes" below** |

## Failures met, and what they changed

| # | What happened | Fix |
|---|---|---|
| 1 | `gc start` on 1.5.1 failed after 78 s: `column "storage_class"`; bd logs "refusing to auto-apply 13 pending schema migrations to a shared server database (v53 -> v66) (#5920)" | `apply.sh` step 6 runs `gc bd migrate schema` in the city and every rig scope first |
| 2 | First manual migration ran while the supervisor retried the city init every 10 s: load 60-69, `0062_events_dolt_ignore` died `context canceled` | migrate with the supervisor stopped; the migration is resumable (hq resumed 61 -> 66) |
| 3 | After `gc stop`, any `gc`/`bd` call (even `gc status`) restarts the city's managed Dolt, so the cold backup refused | `stop_city_dolt` SIGTERMs the pid in `dolt.pid` (clean in <2 s); no gc/bd call between stop and backup |
| 4 | `wait_city_ready` demanded a `gc status` without `partial status`; 1.4.1 and 1.5.1 both print `unknown (partial status)` for every agent on a healthy city | ready = controller supervisor-managed + at least 3 tmux sessions; named seats come and go while starts retry, so they are not gated |
| 5 | Pass #1 of the full detached `apply.sh` died 13 s in: `install.sh:47` `bd version \| head -1` under `set -eo pipefail`: head closes the pipe, bd gets SIGPIPE, rc 141 | capture into a variable; every `cmd \| grep -q` / `head` guard under pipefail replaced by `city_dolt_running` / `units_running` (a `! cmd \| grep -q` guard could also read "dolt not running" while it was) |
| 6 | Pass #2 aborted in precheck after 25 s: it counted the apply's own `gcu-apply-*` unit as "another apply running" (only visible in a real detached run) | `units_running` ignores `$GCU_UNIT` |
| 7 | Pass #2 died at step 6 in 2 s: `gc bd: not in a city directory` for a rig path. `gc stop` unregisters the city and 1.5.1 then cannot map a rig path to its city; the earlier piecewise migrations worked only because the city was still registered | rig-scope `gc bd` calls run with `GC_CITY=$CITY` (tested: beadline, 13 migrations applied, v66) |
| 8 | The 1.4.1 rollback `start` printed "City started under supervisor" but the readiness test failed (failure 4) | same fix as 4 |
| 9 | Pass #3's postcheck failed: `gc doctor --json 2>&1` mixed the 10 stderr config warnings of 1.5.1 into the JSON | stdout only (`postcheck.sh`); `gc order list` likewise. No other town script parses merged gc stderr (grep in RUNBOOK section 5) |

Failure 7 is also the case the rollback was rehearsed on: pass #2 left the box with the backup taken, the wrappers and unit
on 1.5.1, the data still v53 and the town down; `rollback.sh` brought it back to a healthy 1.4.1 in one run.

## Step times (one clean run each; load noted)

| Step | Measured |
|---|---|
| `gc stop` + supervisor stop + city Dolt stop | 26-30 s |
| Cold backup (7.3 GB cp + 6 tars + manifest) | 7 min 20 s cold cache (load 5-18); 2 min 45 s warm (pass #2) |
| Repoint wrappers and unit | 2 s |
| `gc bd migrate schema`, 8 scopes | ~8 min, quiet host |
| Start on 1.5.1 | 4 min 55 s |
| `gc doctor --fix` | 3 min 41 s |
| Before-snapshot (`capture.sh --quick`, town up) | ~15 min unpaused: the full `gc doctor --json` plus ~40 `gc order show`; 32 min wall in pass #2 with ~17 min of pauses. It runs before the stop, so it is not downtime |

Downtime on a quiet host: stop 0.5 + backup 3-7 + migrate 8 + start 5 + doctor 4 = about 20-25 min until ready; allow 30.

## Final passes

### Full `apply.sh` pass #3 (04:06-04:44Z): a real `systemd-run --user` unit in the box, via `run-detached.sh --foreground`

The box was paused by the watchdog 04:32-04:35 and 04:42-04:43 (host load 20+). Every step ran:

| Step | Time (UTC) |
|---|---|
| 0-1 install (idempotent) + precheck: fails 0, warns 0 | 04:06:06-04:06:22 |
| 2 before-snapshot, town up | 04:06:22-04:13:27 (7 min) |
| 3 stop the town | 19 s |
| 4 cold backup, 7.4 GB | 04:13:46-04:16:20 (2 min 34 s) |
| 5 repoint wrappers + unit | 1 s |
| 6 migrate 8 scopes (all "Schema already at v66"), start, `is_blocked` repair in 1 scope | 04:16:21-04:24:20 (8 min) |
| 7 `gc doctor --fix`, wait for ready | 04:24:20-04:27:55 (3.5 min) |
| 8 postcheck | 04:27:55-04:44:07 (16 min with the two pauses; doctor ~5 min) |

**Town down from the stop to ready: 04:13:27-04:27:55, about 15 minutes** on a quiet host (allow 30 under load).

The unit ended `POSTCHECK FAILED` on one check only: **failure 9**, `postcheck.sh` read `gc doctor --json 2>&1`, and the
10 config warnings 1.5.1 prints on stderr broke the JSON. Fixed (stdout only, 70b0b16). The fix was validated by running
`postcheck.sh` alone on the resulting 1.5.1 city (04:49-04:55Z): **`POSTCHECK OK`, fails 0, warns 5**; doctor **157 passed,
12 warned, 1 failed, `blocking_failed` 0, `ok=true`**; the one failure is the advisory 1-minute timeout of
`pool-idle-routed-work`. Every order the town depends on is listed, mayor-mail-wake and deacon-patrol-wake `--dry-run` pass,
`bd list` works in all 8 scopes. The 5 warns: `alex-daily --dry-run` exit 124 (needs the cell and network), 2 supervisor.log
lines matching `panic|fatal|cannot|refus` (**not examined**: grep them on the live town), the doctor timeout, and the
known box artefacts of RUNBOOK section 6.

**Honest limit:** the apply unit itself therefore never printed `apply complete` with a green postcheck in ONE run; the
postcheck fix was verified in a separate run of the same script on the same city. Re-run `apply.sh` end to end on a
fresh box only if you want that one-run proof (about 45 min).

### Rollback pass #2, from a running 1.5.1 city

ROLLBACK2_PLACEHOLDER

## Not covered

See RUNBOOK "Gaps": stub `claude`, no live agent conversations, no polecat mid-work at apply time, `.gc` runtime state not
copied into the box, Dolt 2.3.2 against the release notes' 2.2.0 pin.
