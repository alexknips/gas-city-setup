# Runbook: upgrade the town from gc 1.4.1 to gc 1.5.1

Decision: Alex, 2026-10-24 (cell decision-2097582d-dc55-4597-8b86-b0f97bdfda46): upgrade the live town to 1.5.1 on tmux,
after a rehearsal. The rehearsal is in `rehearsal/REHEARSAL-LOG.md`. This runbook is applied by the mayor.

What changes: **gc 1.4.1 -> 1.5.1 and bd 1.2.2 -> 1.3.2.** Dolt (2.3.2) stays. The city stays on its gc-managed Dolt
(1.5.0 changed Dolt ownership for new cities only: do NOT migrate it).

**The town is down for about 15 minutes on a quiet host, allow 30** (full apply pass in the rehearsal box, stop to ready:
stop 19 s, cold backup 2.5-7 min, bd schema migration + start 8 min, `gc doctor --fix` 3.5 min). The whole apply takes about
40 minutes because the before-snapshot (7-15 min, town still up) and the postcheck (doctor, 5+ min) bracket the downtime.
Everything is stopped meanwhile, so nothing is dispatched. Under host load 20+ expect double.

## Why it is shaped this way

| Fact (rehearsal) | Consequence |
|---|---|
| bd 1.3.x migrates the Dolt schema v53 -> v66 and gc 1.4.1 cannot read v66 | Rollback needs a **cold data backup**, not a repoint. `backup.sh` takes it. |
| bd 1.3.2 does **not** auto-migrate a shared Dolt database (#5920): `gc start` fails with `column "storage_class"` | `apply.sh` runs `gc bd migrate schema` in the city and every rig scope with the supervisor stopped. |
| `gc stop` stops every agent session, including the mayor's pane that would run the upgrade | `run-detached.sh` runs the apply as a transient `systemd-run --user` unit with its own cgroup. |
| Sessions carry `GC_BIN` and a PATH entry naming the gc that started them | The apply does a full stop and start; it never relies on preserve-sessions. |
| `brew upgrade` would move Dolt to 2.6.0, delete the 1.4.1 Cellar dir the unit names, and leave no old binary | gc 1.5.1 and bd 1.3.2 are installed from the verified release tarballs under `~/.local/opt`, next to brew's. Rollback is a repoint plus a data restore. **Never run `brew upgrade` on this host.** |
| `gc supervisor install --force` regenerates the unit from the caller's environment (shorter PATH, drops LANG/USER lines) | `apply.sh` edits only the unit's `ExecStart` line (diff vs the live unit: that line only). |
| After `gc stop`, any gc/bd call (even `gc status`) restarts the city's managed Dolt | `apply.sh` SIGTERMs the pid in `.gc/runtime/packs/dolt/dolt.pid` and makes no gc/bd call before the backup. |

## 0. Before the window (the town keeps running; every step is read-only or harmless)

Run from a copy outside the git checkout: bash reads a script while it runs, so the scripts must not change under
a 30-minute apply (a `git pull` in the checkout would do that).

```bash
# <rig> = the gas-city-setup checkout after the refinery merged this branch
cp -a <rig>/upgrade/1.4.1-to-1.5.1 /home/ubuntu/gc-town-upgrade
/home/ubuntu/gc-town-upgrade/install.sh      # downloads, sha256-verifies, unpacks gc 1.5.1 + bd 1.3.2 into ~/.local/opt; switches nothing
/home/ubuntu/gc-town-upgrade/precheck.sh     # read only; exit 0 = go
```

`precheck.sh` checks: load <= 20, both old and new binaries present, wrappers and unit have the shape `apply.sh`
edits, `gc status`, **no refinery bead in_progress** (FAIL: it would be mid-merge), polecat beads in_progress (WARN: they
resume from their hook and worktree; not rehearsed, see Gaps), disk >= 2.5x the Dolt dir, GitHub reachable, pack cache.
Re-run it right before the window. For a stricter gate use `precheck.sh --strict` (any in-flight bead FAILs).

Ask yourself before starting:

- No polecat mid-merge: `gc bd list --status in_progress` in the city and each rig. Stop slinging, let the pool drain if you can.
- Host load below 20 for a few minutes (`cat /proc/loadavg`). The live town's own rustc builds push it to 15-40; the
  schema migration and the first start are the slow, load-sensitive parts. Do not start while a build wave is on.
- Leave a note for the mayor session that will exist after the restart (the current one dies at `gc stop`):
  `gc mail send mayor -s "UPGRADE IN FLIGHT" -m "Read /home/ubuntu/gc-upgrade-backups/gcu-apply-*.log. If it ends POSTCHECK OK do RUNBOOK section 3; if it says APPLY FAILED read section 4."`

## 1. Apply

```bash
/home/ubuntu/gc-town-upgrade/run-detached.sh apply.sh
tail -f /home/ubuntu/gc-upgrade-backups/gcu-apply-*.log      # optional; the unit does not depend on you
```

`run-detached.sh` is `systemd-run --user --unit=gcu-apply-<utc> --collect --working-directory=/home/ubuntu/gc` with
absolute paths and a log in `/home/ubuntu/gc-upgrade-backups/`. It returns at once. `apply.sh` steps:

| # | What | Town |
|---|---|---|
| 1 | `install.sh` (idempotent) and `precheck.sh` (aborts, changing nothing, if it fails) | up |
| 2 | before-snapshot (`capture.sh --quick`) for the diff | up |
| 3 | `gc stop`, stop the supervisor unit, stop the city's Dolt, verify no tmux/dolt/unit left | DOWN |
| 4 | `backup.sh`: cold copy of Dolt data, config, `~/.gc`, unit, wrappers; writes `~/gc-upgrade-backups/latest` | down |
| 5 | repoint `~/.local/bin/{gc,bd}` and the unit's `ExecStart` to the 1.5.1 / 1.3.2 installs, `daemon-reload` | down |
| 6 | `gc bd migrate schema` in every scope, start the unit, `gc start` on 1.5.1, repair `is_blocked` where the first start skipped it | starting |
| 7 | `gc doctor --fix` once (non-interactive), wait until the controller is supervisor-managed and 3+ tmux sessions are up | UP |
| 8 | after-snapshot and `postcheck.sh` | up |

Anything that fails after step 3 stops with the town down and prints how to back out (section 4). The log's last line
is `apply complete` on success.

"Ready" in step 7 is deliberately weak: the controller is supervisor-managed and at least 3 tmux sessions exist
(`GCU_READY_SESSIONS`, wait limit `GCU_READY_TIMEOUT`, default 900 s). Do not wait for a `gc status` without
`partial status`: it marks every agent `unknown (partial status)` when its runtime probe times out, which happened on a healthy
city in the rehearsal (40 such lines on the 1.4.1 baseline too). The seats start in waves over the next minutes, some
retrying after `resuming session: context deadline exceeded`; `postcheck.sh` and doctor judge the result.
If the log ends `city not ready`, the city may only be slow: look at `gc status`, `tmux -L gc ls` and
`~/.gc/supervisor.log` and run `postcheck.sh` before you decide to roll back.

## 2. What the apply changes on disk (live files)

- `~/.local/bin/gc`, `~/.local/bin/bd`: the `exec` line points at `~/.local/opt/{gascity/1.5.1/gc,beads/1.3.2/bd}`.
- `~/.local/share/systemd/user/gascity-supervisor.service`: `ExecStart` only.
- `/home/ubuntu/gc/pack.toml` and `packs.lock` (by `gc doctor --fix`): `[imports.bd]` and `[imports.core]` re-pinned
  `sha:f895c0ff` -> `sha:2e643dc2eb5f`, `[imports.gascity]` renamed `[imports.gc]` (same source, version `^0.3`),
  `packs.lock` refetched. **`city.toml` is not touched.** Commit `pack.toml` and `packs.lock` afterwards.
- The Dolt data: every database in `/home/ubuntu/gc/.beads/dolt` goes v53 -> v66 (the database `mp`, bound to no rig, is not
  migrated and stays v53: 1.5.x no longer maintains it).

## 3. After the apply: checks and the decisions that are the mayor's

`postcheck.sh` ran at the end of the apply (supervisor and `gc`/`bd` on PATH are 1.5.1/1.3.2, city dolt up, controller
supervisor-managed, tmux sessions, `gc config show`, every order the town depends on listed, `--dry-run` of mayor-mail-wake /
deacon-patrol-wake / alex-daily, `bd list` readable in every scope, doctor with no blocking failures). Re-run any time:
`/home/ubuntu/gc-town-upgrade/postcheck.sh` (`--no-doctor` skips the 4-7 minute doctor).

Then decide, in this order (none is urgent; the town runs without them):

1. **`[session]` in `/home/ubuntu/gc/city.toml`** (live file: yours to edit). Every gc 1.5.1 command now prints on stderr
   `[session] setup_timeout (1m0s) >= startup_timeout (1m0s): setup_timeout can never fire`. The town sets
   `setup_timeout = "60s"` for host-load headroom (see the comment above it); `startup_timeout` defaults to 60s and bounds
   the whole start first. Add `startup_timeout = "120s"` under `[session]`. (1.5.1 also redefines `setup_timeout` as an
   idle/silence bound between output instead of total setup time, which only makes 60s more generous.)
2. **Nine `named_session ... wake_mode "fresh"` warnings** on every command (mayor, the crew seats, oracle, the three
   witnesses). Effective behaviour is unchanged: the resolved `wake_mode` counts are identical before and after (27 fresh,
   8 resume). Leave them, or set `wake_mode = "resume"` on a seat that should keep its conversation across a drain
   (that is a behaviour change, so do it separately from the upgrade).
3. Commit `pack.toml` + `packs.lock` in `/home/ubuntu/gc` (section 2).
4. `hq` Dolt noms is 6.99 GB and stays that way: 1.5.x never flattens a database that has a remote and all ours do.
   The manual quarantine (gc-l5dqh) still stands.
5. Delete `~/gc-upgrade-backups/*` and the `.beads/dolt.*` dirs only once the town has run a few days on 1.5.1. The
   backup is the only way back to 1.4.1 data.

## 4. Rollback (tested in the rehearsal box)

When: the apply stopped with the town down and you cannot finish by hand, or the town is up but wrong and you want 1.4.1 back.

```bash
/home/ubuntu/gc-town-upgrade/run-detached.sh rollback.sh        # restores the newest backup: ~/gc-upgrade-backups/latest
tail -f /home/ubuntu/gc-upgrade-backups/gcu-rollback-*.log
```

It stops everything on whichever gc is running, **moves** the upgraded Dolt dir aside to `.beads/dolt.1.5.1-<utc>` (never
deleted) and copies the backup in (the backup stays intact, so a failed rollback can be re-run), restores config, packs,
`.gc`, `~/.gc`, wrappers and the unit (so `ExecStart` is the brew 1.4.1 gc again), checks the restored Dolt heads equal the
backup's `MANIFEST.txt` (offline, per database), starts on 1.4.1, waits for the city, and runs `postcheck.sh --expect-old`.

**Beads written between the apply and the rollback are not carried back**: they exist only in the moved-aside dir.
`rollback.sh <backup-dir>` restores a specific backup. The Dolt binary is untouched by both directions.

## 5. Behaviour changes in 1.4.2 -> 1.5.1 that touch this town

"Verified" means seen in the rehearsal; the others come from the release notes plus a grep of the town's own files.

| Change | Touches the town? | What to do |
|---|---|---|
| First start runs `bd recompute-blocked` per scope (the store crossed bd 0059) | Yes. Verified: hivemind corrected 3 rows; neurofilament (a legacy rig) skipped with `needs a clean working set` | Automatic: `apply.sh` runs `gc bd dolt commit` + `bd recompute-blocked` in each scope the supervisor log names. Beads that were wrongly blocked become claimable. |
| `gc start` fails on a v53 store (`column "storage_class"`) | Yes. Verified | Handled by the migrate step; do not skip it. |
| `GC_AGENT`/`BEADS_ACTOR` is the session bead id for unaliased pool sessions (polecats) | Yes, but compatible: `formulas/mol-polecat-base.toml:67` queries `--assignee=$GC_AGENT`, the claim uses the same value; hivemind hooks use `${GC_TEMPLATE:-$GC_AGENT}`; named seats are unaffected | A polecat bead held under the old spelling is re-stamped by `gc hook --claim`. Not rehearsed with live polecats. |
| New stderr lines on every gc command (9 `wake_mode`, 1 `setup_timeout`) | Yes, cosmetic | Section 3.1 / 3.2. Grepped orders, formulas, commands, assets, doctor, agents, packs and `setup.sh`: no script parses `gc ... 2>&1` (all 9 hits redirect to /dev/null), so nothing breaks. |
| `gc doctor --fix` renames `[imports.gascity]` to `[imports.gc]` | Yes. Verified: **no collision** with the city pack's own name `gc`: `gc gc capture-decision --help` resolves to the city's command before and after; `gc gascity ...` no longer exists (nothing in the town calls it) | Let doctor do it (the apply does). The 16 `gc gc capture-decision` call sites stay valid. |
| Core pack is served from the binary (`~/.gc/cache/repos/<hash>/internal/bootstrap/packs/core`), `orphan-sweep.sh` rewritten (`gc agent is-foreign`) | Yes: `orders/orphan-sweep.toml` + `orphan-sweep-live-guard.sh` | **Keep the override.** Verified: `gc order show nudge-on-route` `Source:` resolves to a real `orphan-sweep.sh`, it still calls `gc bd release-if-current "$bead_id" "$expected_assignee"` (line 345), and the shim intercept returns `skipped` for a live tmux assignee. `is-foreign` is about foreign claims, not the stale-roster race the guard covers. |
| New orders `notify-on-human-gate-creation` (event `bead.created`) and `renudge-stale-human-gates` (every 5m) | Inert here: the town creates no human-gate beads (grepped). The first one runs a script on every `bead.created` and exits | Nothing. If it ever shows as load, disable it like `gate-sweep` already is: `[[orders.overrides]] name = "<order>"` with `enabled = false`. |
| `bead.closed` is now emitted for `bd close` | Yes: event-triggered orders (autoclose) fire on closes that used to be silent | Watch the order history in the first hour. |
| `nudge-on-route` no longer retries on an identical `bead.updated` | Yes, fewer duplicate nudges | None. |
| Reaper age gates use UTC | Yes (Berlin is east of UTC): closes and prunes wait the full age | None. |
| Reaper, jsonl-export, mol-dog-backup go through `gc bd` and the bd export format; old snapshots move to `<db>/legacy/` (git mv) | Yes: expect new commits in every database on the first runs | Watch `jsonl-archive` in doctor (local-only is the existing state). |
| Databases bound to no rig are no longer maintained (`mp` here) | Yes: `mp` stays at schema v53 | None unless something starts using `mp`; then `gc bd migrate schema` there first. |
| Compactor never flattens a database that has a remote | Yes: all nine have a `*-backup` remote, so `hq` (6.99 GB) stays manual | Section 3.4. |
| Closed-wisp purge runs in bounded batches | Yes: the first reaper run on `hq` may be heavy | Do not schedule anything heavy right after the upgrade. |
| `pre_start`/setup is cancelled with SIGTERM, not SIGINT | Low: `worktree-setup.sh` already traps TERM; `hivemind-worktree-setup-clean.sh` has no trap, so a cancelled setup can leave a half-made worktree | None needed; if a polecat start is cancelled, the witness sees it. |
| `gc stop` can exit 1 (`stop not confirmed`) | Yes | `apply.sh`/`rollback.sh` tolerate it and verify the state themselves. |
| `gc bd new --metadata @file.json` exits 1; `BD_BIN` is empty for children without a pin; secrets.env keys must be shell identifiers | No: grepped orders, assets, formulas, commands, agents; the town has no secrets.env | None. |
| Formula steps and agent-script notes append (`--append-notes`) | Probably benign | None. |
| `gc doctor --json` has `ok=false` on a blocking failure; `gc doctor --fix` is non-interactive | `postcheck.sh` reads `blocking_failed` | None. |
| Claude permission-prompt security fix; `gc import add --version` for registry packs | No | None. |

### The town's overrides after the upgrade

All still load and behave; none becomes redundant.

| Override | Result |
|---|---|
| `[[orders.overrides]]` x9: beads-health, gate-sweep, order-tracking-sweep, dolt-health, cross-rig-deps, nudge-mail-sweep, spawn-storm-detect, orphan-sweep, mol-dog-doctor | all nine resolve in `gc config show`; `mol-dog-doctor` is still a real order (30 min), so that override is not stale |
| `orders/orphan-sweep.toml` + live-tmux guard | keep (table above) |
| town orders alex-daily, mayor-mail-wake, deacon-patrol-wake, hivemind-deploy-nightly, quality-metrics | listed; the three `--dry-run`s pass in `postcheck.sh` |
| `[imports]` | `gascity` -> `gc`, `bd` and `core` re-pinned; `gastown` unchanged |
| model pins, patches, `option_defaults` | `gc config show` before vs after, normalised for cache hashes: the only differences are the import changes, the core pool-worker prompt file rename (`pool-worker.md` -> `pool-worker.template.md`, the core pack's own file, no town override) and one new default (`[dolt] wait_timeout_seconds = 0`) |

## 6. Doctor after the upgrade

Latest full pass (postcheck on the finished 1.5.1 city): **157 passed, 12 warned, 1 failed (advisory timeout), `blocking_failed` 0,
`ok=true`**. Baseline on 1.4.1 in the box: 138 passed, 6 warned, 0 failed. On 1.5.1 right after `--fix`: 158 passed, 11 warned,
1 failed (an advisory timeout), `blocking_failed` 0; a later capture on the busy box: 155 passed, 11 warned, 4 failed (all
four are 1-minute advisory timeouts), `blocking_failed` 0. The remaining findings, and which are box artefacts:

| Finding | Real or artefact |
|---|---|
| `dolt-noms-size` hq 6.99 GB, `jsonl-archive` local-only, `rig-pack-coverage` (1 named_session), `session-model` (14), `formula-requirements` (1 formula still `contract=graph.v2`) | Already present on 1.4.1 (baseline), same on 1.5.1. Advisory. |
| `pool-idle-routed-work`, `builtin-pack-imports`, `custom-types:hivemind`, `v2-routed-to-namespace`: `timed out after 1m0s` | Load: each is a 1-minute advisory check and the box was busy. Not re-run alone, so unconfirmed: `gc doctor --check <name> --check-timeout 5m` on the live town. |
| `order-outcome-healthy` (decisions-for-humans failing), `nudge-unconfirmed` (11 sessions) | Box artefacts: no Loomtracer cell, and the stub `claude` cannot confirm a nudge. Expect them clean on the live town; if not, they are real. |
| `agent-sessions` (40 agents without sessions), `orphan-sessions` (1; fix skipped while the controller runs) | On-demand pool seats have no session until work arrives. Look at the orphan on the live town. |
| `bd-backup-freshness` (registered but never synced), `order-firing-current` | The box had only just started and has no backup remotes. Re-check on the live town after a day. |

## Gaps: what the rehearsal did not cover

- It ran in a container with a **stub `claude`** (idles) and a copy of the Dolt data from 2026-10-25 02:05 CEST: no live
  agent conversations, no real polecat mid-work, no real pool load. A polecat bead in progress at apply time (resume from
  hook and worktree after the restart) is untested.
- The live `.gc` runtime state (46 GB worktrees, 4.9 GB runtime) is not copied into the box; the backup tars the small parts
  only (see `backup.sh`). Worktrees and agent homes are not touched by the upgrade and are not part of the rollback.
- `alex-daily --dry-run` needs the cell and the network; it passed inside `postcheck.sh` in the box but timed out at 120 s
  in a manual run on a loaded host.
- Dolt 2.3.2 with bd 1.3.2 works in the box. gc's release notes pin Dolt 2.2.0 and warn about 2.3.0/2.3.1; 2.3.2 was not
  measured against that warning beyond the rehearsal's migration and normal operation.
