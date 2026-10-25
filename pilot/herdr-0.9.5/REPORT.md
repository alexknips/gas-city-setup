# herdr 0.9.5 pilot on gc 1.5.1 (gcs-78y)

Status: DRAFT, in progress. Measurements 1, 3 (acted-on half), 4 need a Claude login in the box (decision gc-lvkjm, default B = no login). Measurement 5 (24 h) has not run: the box was killed 8 times, the last two (rule f, host load 72 and 83) while idle (see Kill log). Measurement 6 ran without the city (partial). This file is final after the login cutoff.

## Recommendation (draft; final after the login cutoff, Mon 2026-10-26 09:40Z)
**Do not switch the town to herdr on this evidence.** Nothing observed argues against herdr itself: gc 1.5.1 drives herdr 0.9.5, the one stop I checked (kill 8) left no process behind, and no sample ever showed more than 2 herdr processes or any zombie. But the pilot did not test the thing the switch risks (a supervisor driving a pool through herdr for hours, the Sept 2026 crash-loop), so "no failure seen" is weak.

| # | Measurement | Result |
| --- | --- | --- |
| 1 | pool starts and claims with no prompt answered | not measured: no Claude login in the box. Provider start and pre-trust by inheritance did work (first smoke, 4 agents up, no prompt) |
| 2 | parked question/selector shows `blocked` | partial: Bypass Permissions dialog `blocked` in 3.5 s (2 of 2); `/theme` selector stays `idle`; AskUserQuestion not measured; gc's reaction to `blocked` not tested |
| 3 | nudge drives the agent | partial: typed and submitted, not pasted (1 line and 705 bytes); "acted on" needs a login |
| 4 | sling to closed bead, twice | not measured: needs login and a city |
| 5 | herdr procs, zombies, load over 24 h | not measured over 24 h: 8 stops in 87 min; 23 light-box samples only |
| 6 | phone/web over the tailnet only | partial: reachable via the host's tailnet name, token-gated, not public; not tried from a second device or a browser |

What is missing before a switch can be decided on evidence:
1. **A host that can take the pilot.** The scratch city's CPU went to gc/bd/dolt polling, 1.4 to 3.9 cores (no tmux control was run, see below), and the live town took this host past load 60 four times in 77 minutes. Rules e and f then stop any pilot, even an idle one. Run it on a separate small VM, or in a window the mayor names with the town paused.
2. **A Claude login inside that environment** (Alex's call, gc-lvkjm) for 1, 3 and 4.
3. **A 24 h run of the full city under herdr** counting herdr procs and zombies, which is the actual Sept 2026 failure.
4. **Two things to settle on the herdr side:** an unauthenticated Claude shows `idle`/`interactive_ready` (a watchdog needs its own login check), and herdr's detection rules come from a manifest it fetches (`claude.toml 2026.10.11.1`) that can change without a version bump, so pinning the binary does not pin `blocked`/`idle` behaviour.
5. **A decision on Ovis.** It is third-party Bun/Node code that gives a token holder on the tailnet a terminal on agents running `--dangerously-skip-permissions`. It needs a test from a real second device before anyone relies on it, and a decision on who holds the token.

A control the pilot lacked: the same lean city on the tmux provider, to separate the cost of gc 1.5.1 from the cost of herdr. Without it, "a city is expensive beside the live town" must not be read as "herdr is expensive".

## Setup (what was actually built)

| Piece | Value |
| --- | --- |
| Isolation | docker container `gcs78y-pilot`, own pid namespace, `--cpus 4 --memory 12g --pids-limit 1500`, no host mounts, no `--privileged`, no host network. The live town's `~/gc`, `~/.gc`, `~/.claude` and supervisor env are not reachable from it. |
| Versions | gc 1.5.1, bd 1.3.2, dolt 2.3.2, herdr 0.9.5 (sha256 `14d9e8bc...9929d` checked at build), claude 2.1.310 |
| Supervisor | the box's own `gc supervisor run` (pid in the box), `GC_SESSION=herdr` first, then `[session] provider = "herdr"` in the scratch `city.toml` |
| City | `scratch78y` from `gc init --template gastown`, same gastown pack sha as the live town (33d3a43) |
| Rig | `scratch`, prefix `scr`, a clone of this repo whose only remote is a bare repo inside the box (nothing can reach a real remote) |
| Beads | 3 small beads in the scratch ledger. The live ledger had no small open beads (every open one is a large feature), so these are real follow-ups taken from the gcs-s4e notes: `scr-vq2` (apply.sh: stop the Dolt migrate_schema starts), `scr-n08` (RUNBOOK §6 doctor baseline), `scr-qvj` (RUNBOOK §7 live apply record) |
| Sampler / kill switch | `watchdog.py` on the host (reads the box cgroup, not docker), every 30 s into `data/samples.tsv`. Mayor ruling 2026-10-25 09:15Z: kill on a) herdr procs > 50, b) zombies > 20, c) box pids > 1000, d) box CPU >= 3.5 cores over 5 min, e) host load1 > 20 AND box CPU > 1 core, both over 5 min, f) host load1 > 60. The first smoke ran under the literal "host load > 20"; that rule is retired. |

Files: `Dockerfile`, `pilot.sh` (build/up/exec/watch/kill), `probe-status.sh` (status-change probe), `data/` (raw samples and probe logs).

## Findings so far

### Provider mechanics (gc 1.5.1 + herdr 0.9.5): works
First smoke, 2026-10-25 09:01Z. The supervisor started the city under herdr: one herdr server `herdr --session scratch78y server`, workspaces `gastown` (boot, deacon, mayor tabs) and `scratch` (witness tab), four `claude --dangerously-skip-permissions` processes. No prompt was answered by a human. gc 1.4.x could not do this (#5273).

### Pre-trusted folders: inheritance works
`~/.claude.json` trusts only `/home/ubuntu/scratch-city` and `/home/ubuntu/rigs/scratch` (plus `hasCompletedOnboarding` and `bypassPermissionsModeAccepted`). The agents ran in `scratch-city/.gc/agents/{boot,deacon,mayor}` and `scratch-city/.gc/agents/scratch/witness`, trusted only through the parent, and showed no trust or bypass-permissions dialog (the pane footer read `bypass permissions on`). Polecat worktrees are not tested yet.

### herdr says `idle` for a Claude that is not logged in
With no login in the box, herdr reported `agent_status: idle`, `interactive_ready: true` for panes whose footer read `Not logged in · Run /login`. Nothing in `herdr agent list` tells an unauthenticated agent from a healthy idle one. Any watchdog built on that field has to add its own login check.

### Parked dialogs: a startup form is `blocked`, a slash-command selector is `idle` (measurement 2, partial, no login)
Two slash-command selectors, probed with `probe-status.sh` (herdr status polled every second):
- `/theme` parked in the witness pane for about 30 s (09:39:24Z, then dismissed with Esc): status `idle` the whole time. `herdr agent explain` gave rule `osc_title_idle`, evidence the terminal title `✳ Claude Code`, from manifest `claude.toml 2026.10.11.1`.
- `/login` in the boot pane (09:11:05Z, run 2): `idle` -> `working` after 2 s -> `done` after 22 s. Never `blocked`.
- Claude's "Bypass Permissions mode" accept dialog (10:13Z, bare server, started by hand with `herdr agent start probe --kind claude --pane w1:p1 -- --dangerously-skip-permissions`): `blocked`. `herdr agent start` itself failed with `agent_not_ready: agent probe is blocked during startup` after 3.5 s, `herdr agent list` held `blocked` for the 6 s polled, and the Ovis snapshot showed the same status over the tailnet. `herdr agent explain`: rule `live_blocked_form` (region `after_last_horizontal_rule`, priority 980), same manifest `claude.toml 2026.10.11.1`. Raw: `data/probe-bypass-dialog-blocked.txt`. This dialog showed up only because the launch was manual; the city's launches pass `--settings`, and the first smoke had no dialog.

So herdr does classify some parked Claude screens as `blocked` and does it within seconds, but not every one (`/theme` is `idle`), and an AskUserQuestion-style question is still not measured (needs a login). The `/theme` and `/login` runs above never reached `blocked`, so there was nothing for a watcher to notice. "How long until something notices" is therefore only measured for herdr itself (3.5 s); whether gc 1.5.1 reacts to a `blocked` agent at all was not tested (the dialog run had no city), and is the open half of measurement 2. A real question (AskUserQuestion) still needs a login to be raised, so that half of measurement 2 is not measured.
herdr detects agents with a manifest it fetches into `~/.local/state/herdr/agent-detection/remote/`. The rules therefore change on herdr's side without a version bump, which defeats pinning the binary.

### `gc session nudge` is typed and submitted, not pasted (measurement 3, partial, no login)
Both sent with `--delivery immediate` to the witness (09:40Z):
- one line (`Reply with the single word PONG and nothing else.`): shown as a normal user turn, Claude started its turn (UserPromptSubmit hooks ran);
- five lines, 705 bytes: the full text shown as a user turn, no `[Pasted text #N]` placeholder, turn started.
Both ended in `Not logged in · Please run /login`, so whether the agent acts on a nudge is not measured. The #7027 report of a nudge arriving as an unsubmitted pasted block did not reproduce for these two sizes on herdr 0.9.5 / gc 1.5.1.
The `gc session nudge` call itself took 43 s and 37 s at host load about 20, and 81 s for a default `wait-idle` call at load 30+. That is gc CLI cost, not herdr.

### Even the lean city runs at its CPU cap
Lean city (mayor/deacon/boot suspended, 5 orders left of 23, one rig, one Claude, one dog), `--cpus 2`, 09:36Z start: box CPU sat at 1.9 cores from minute 2 on. After about 5 minutes the cgroup held 124 pids (bd, gc, dolt and sh dominating, about 1 herdr), and had been CFS-throttled 3103 times for 667 s in total. herdr is a rounding error in that. The cost is gc/bd/dolt polling of the beads store, as in the default city. Default orders include `dolt-health`, `beads-health` and `gate-sweep` every 30 s, each a full gc CLI start.

### Kill log: eight stops in 87 minutes, no run reached steady state
All times 2026-10-25 UTC. Raw lines: `data/samples*.tsv`, `data/watchdog.log`, `data/keeper.log`, `data/last-kill.txt`.

| # | Time | Rule | Box at the tripping sample | Host | City / cap |
| --- | --- | --- | --- | --- | --- |
| 1 | 09:04:01 | old: host load > 20 | idle: 1 herdr, 4 claude | load1 33.8 (live clippy build, live Dolt 156%) | default, 4 cpus |
| 2 | ~09:14 | by hand | n/a: the first watchdog went through `docker exec`, and the docker daemon stalled at host load ~100 | load ~100 (live town: 12 rustc + 9 rust-lld) | default, 4 cpus |
| 3 | 09:21:29 | e | cpu5m 2.90, 38 pids | load5m 20.06 | default, 4 cpus |
| 4 | 09:30:36 | e | cpu5m 2.80, 48 pids, 5 claude | load5m 21.61 (load1 41.3) | default, 4 cpus |
| 5 | 09:46:03 | f | cpu5m 1.37, 18 pids, 1 herdr, 2 claude | load1 70.25 (live hivemind polecat running `loomtracer-*` tests at 400% + live Dolt 131%) | lean, 2 cpus |
| 6 | 09:58:25 | e | cpu5m 1.70, 19 pids, 1 herdr, 2 claude, 0 zombies | load5m 20.86 (load1 30.4) | lean, 2 cpus |
| 7 | 10:17:10 | f | cpu5m 0.06, 8 pids, 1 herdr, 1 claude, 0 zombies: **idle** (item 6 box, no city) | load1 71.97 (still 33.5 at 10:19Z with the box dead; live-town builds) | none: bare server + Ovis + 1 agent, 2 cpus |
| 8 | 10:31:08 | f | cpu5m 0.09, 8 pids, 1 herdr, 1 claude, 0 zombies: **idle**, 4 min after restart 1 | load1 83.04, 104 at 10:31:33 (top processes: `rust-lld` linkers, live Dolt 124%) | same light box, 2 cpus |

What the six say, and what they do not:
- herdr was never the problem: at every stop the box held 1 herdr, 0 zombies and at most 48 pids. Rules a, b and c never came close; d came within 20% once (kill 3, cpu5m 2.90 of 3.5).
- The box is not a quiet neighbour either. A default city costs 2.8 to 3.9 cores from its first minutes, a lean city (mayor/deacon/boot suspended, 5 of 23 orders, one rig, one Claude) 1.4 to 1.9 cores, and sits at its CFS cap (3103 throttles in 5 min at `--cpus 2`). That CPU is gc, bd and Dolt polling the beads store (default orders `dolt-health`, `beads-health`, `gate-sweep` run every 30 s, each a full gc CLI start). herdr is about 1 process. gc 1.5.1 on tmux would pay the same polling; this pilot has no tmux control, so "herdr is expensive" is NOT shown, only "a gc 1.5.1 city is not cheap beside the live town".
- Host load is dominated by the live town. At 10:06Z, 8 minutes after kill 6, with the box dead, load1 was still 17 to 19 and the top process was a live-town test binary at 5200% CPU.
- Kills 7 and 8 are the cleanest attribution in the set: the box held one herdr, Ovis and one parked Claude at 0.06 and 0.09 cores (5-min), and rule f (host load1 > 60, "whoever causes it") still stopped it, because the live town pushed host load to 72 and 83. Host load1 passed 60 four times in 77 minutes (about 100 at 09:14Z, 70.25 at 09:46Z, 71.97 at 10:17Z, 83.04 at 10:31Z); in the last three the box used 1.37, 0.06 and 0.09 cores (5-min). After kill 8 I stopped restarting: each restart gave 4 to 7 minutes of samples and added a start burst to a host already above 80. Rule f did its job as written; its consequence is that no soak of any configuration survives 24 h on this host while the live town is working, and stopping an idle box does not lower the load (33.5 afterwards).
- Every start was followed within 2 to 5 minutes by a host load rise (6.9 to 30 in the 5 min after run 6). Some of that is the box, some is the live town's own bursts; with one host and no repeat under a quiet town these cannot be separated. I did not push the attribution beyond that.
- Rule e as written cannot hold a lean city on this host for 24 h: it needs the live town quiet for 5 min windows while the box uses more than 1 core, and none of the four windows between 09:04Z and 09:58Z was. I did not loosen e, change the cap to make e inert (`--cpus 1`), or restart in a loop. A box start costs the live town a cold-start burst each time.

## Measurement 5: NOT measured over 24 h; what exists is 11 minutes of a reduced setup
No 24 h run happened (see Kill log). The only steady-ish samples are two segments of the no-city box (bare herdr server + Ovis + one parked Claude), via `soak-summary.py`:

| Segment | Span | Samples | herdr procs (max) | Zombies (max) | Box pids (max) | Box CPU 5-min (max) | Host load1 (mean / max) | Ended by |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `samples-item6.tsv` | 10:10:10-10:17:10Z | 15 | 2 | 0 | 11 | 0.63 | 16.8 / 72.0 | rule f (kill 7) |
| `samples-item6b.tsv` | 10:27:37-10:31:08Z | 8 | 1 | 0 | 8 | 0.33 | 21.4 / 83.0 | rule f (kill 8) |

That is 23 samples. It says a herdr server with one agent and the web UI is small and leaks nothing in 11 minutes (1 to 2 herdr procs, 0 zombies, under 1 core), and nothing more. It says nothing about the failure this pilot was meant to look for (the Sept 2026 crash-loop: a supervisor driving many sessions through herdr for hours), because no gc supervisor and no pool ran in these segments. After kill 8 the host showed 0 zombies and 0 exact-name `herdr` processes, so the box left nothing behind when stopped (not checked after kill 7).

## Decisions
1. Kill-switch attribution: mayor ruling 2026-10-25 09:15Z (rules a-f above). Applied as written.
2. Claude auth inside the box: Alex's call, bead gc-lvkjm, still open. Default B (no login) applies Mon 2026-10-26 09:40Z, 24 h after the decision was posted to Slack. The host credentials were not copied or mounted (a refresh-token rotation could sign out the live town). Until then 1, 3 (acted-on half) and 4 stay "not measured: no login in the box".
3. City runs: mayor ruling 2026-10-25 10:55Z accepts the stop rule as it stands (no city restart, no `--cpus 1`, no quiet window). Items 1, 3, 4 and 5 on a city are therefore reported "not measured", with the kill-log samples as the reason, and the recommendation does not claim that herdr fails them.

## Measurement 6: phone/web access over the tailnet only (partial: reachable and gated, not driven from a device)
Run 2026-10-25 10:10-10:16Z (the box was then stopped by rule f at 10:17:10Z, kill 7), steps in `item6-box.sh`, raw output in `data/ovis-install-3.log`, `data/ovis-ws-probe.txt`, `data/probe-bypass-dialog-blocked.txt`. This ran WITHOUT the city: the box held a bare `herdr server`, Ovis and one Claude pane, so it cost 0.05 to 0.4 cores and rule e could not fire. Nothing here depends on a Claude login.

Choice: Ovis (`devswha/ovis`, MIT, a herdr plugin with a phone PWA), v0.4.6 = commit `7e12a892273b5d7a2f61c181c95a2bb0ad178309` (tag dated 2026-10-10, 15 days old), installed by commit with `herdr plugin install devswha/ovis --ref <sha> --yes`. `dcolinmorgan/herdr-remote` is ruled out: its remote mode is a Cloudflare tunnel (public) and its license is unspecified. Plain alternative with no third-party code: ssh over the tailnet to the host, then `docker exec -it gcs78y-pilot herdr`.

How it was exposed:
- Ovis reads `HOST`, `PORT`, `HERDR_WEB_TOKEN`, `HERDR_WEB_TELEMETRY` (the earlier plan named `OVIS_*`; those do not exist). Inside the box it binds `0.0.0.0:7317`, because a `127.0.0.1` bind is unreachable through docker's port publish. `docker run -p 127.0.0.1:7317:7317` publishes it on the host loopback only.
- The host already had `tailscale serve --https=443 -> 127.0.0.1:7317` (status line: `tailnet only`, no funnel). That is the only way in.
- `HERDR_WEB_TOKEN` is set (48 hex chars, generated in the box, mode 600, never written to the repo or a log): with a token set Ovis demands it from every client, "this computer and your own Tailscale login included".
- The plugin starts with the herdr server, not at install: it was not listening until the server was restarted. Start it through the server's environment, not the plugin's `start` action, which may run without the token.

| Probe | Result |
| --- | --- |
| host `127.0.0.1:7317/` no token | 200, the static app shell |
| `/api/session` no token / wrong token | 401 `token required` / 401 |
| `/api/session` right token | 200, snapshot with workspace `item6` and agent `probe` status `blocked` |
| same three via the host's `https://<host>.<tailnet>.ts.net` name (from the host itself) | 200 / 401 / 200 |
| WebSocket upgrade `/ws` over the tailnet name | no token 401, token 101 Switching Protocols |
| host listeners on 7317 | `127.0.0.1` only |
| host public IP:7317, docker gateway IPs:7317 | connection refused |
| `tailscale funnel status` | off |
| other containers on docker's default bridge | none: the box is alone there, so the `0.0.0.0` bind inside it has no other neighbour |
| `/api/health` no token | 200, discloses herdr version, protocol, plugin commit and boot id (minor) |

NOT tested: a second tailnet device (the MacBook is offline and the Pixel has been offline for 38 days, so the only online node is the host and every probe came from it), a real browser or PWA, typing into a pane through the WebSocket, push alerts, the pairing flow. So this shows "reachable over the tailnet, token-gated, not public", not "a phone can drive the agents".

Risk to carry into the recommendation: a leaked token plus tailnet reach types into any agent in the city, and the agents run `--dangerously-skip-permissions`. The tailnet currently has three nodes under one login. Ovis is third-party Bun/Node code (`bun install` ran its dependency tree at install time) and is untested against a logged-in agent; it is still installed in the box volume (the box itself is stopped), and Alex's login (gc-lvkjm), if given, should only go in after it is uninstalled (`item6-box.sh uninstall`) or with a decision to keep it.
