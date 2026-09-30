# gas-city-setup

A generic [Gas City](https://github.com/gastownhall/gascity) on your own server, from "no server" to a
running city in well under an hour. One script installs and wires everything: Gas City (`gc`), beads + Dolt,
the gastown agents (mayor, deacon, witness, refinery, polecats), Claude Code, GitHub CLI, tmux, plus two
safety tools, [DCG](https://github.com/Dicklesworthstone/destructive_command_guard) (blocks destructive
shell commands before they run) and [CAAM](https://github.com/Dicklesworthstone/coding_agent_account_manager)
(switch Claude accounts when a subscription limit hits).

## 1. Buy a VPS

Ubuntu 24.04, x86-64 or ARM, with root or a sudo user over SSH.

|          | vCPU | RAM   | Disk   | Notes |
|----------|------|-------|--------|-------|
| Comfortable | 8 or more | 32 GB or more | 400 GB or more | several polecats plus builds run side by side |
| Minimum  | 4    | 8 GB  | 80 GB  | one polecat; setup adds a 4 GB swap file below 16 GB RAM |

- **Contabo:** Cloud VPS 12 (12 vCPU, 48 GB RAM, 400 GB SSD), image Ubuntu 24.04.
- **Hetzner Cloud:** a General Purpose (dedicated vCPU) plan with 8 vCPU and 32 GB, image Ubuntu 24.04. Attach a volume if the disk is under 400 GB.

## 2. Run the installer

SSH into the new server and paste this one line (as root or as a sudo user):

```bash
curl -fsSL https://raw.githubusercontent.com/alexknips/gas-city-setup/main/setup.sh | bash
```

It takes about 10 minutes and prints its elapsed time at the end. Started as root, it first creates a
sudo user (`gc`; use another name with `GCS_USER=name` in front of `bash`) with your SSH keys and carries on
as that user. Running it again is safe: it only changes what differs, so re-running is also how you apply
config edits (`gcs setup` does the same from the new user's shell).

## 3. Log in

The last lines of the installer list the logins it cannot do for you (each is skipped once done). Log in as
the new user first (`su - gc`, or `ssh gc@<server>`), then: `claude` (your Claude account), `gh auth login`
(GitHub, needed for pull requests), `sudo tailscale up` (only if enabled), `caam backup claude main` (saves
the Claude login so you can switch accounts later) and finally `gcs setup`. The city is created paused, so no
agent sits on Claude's login screen; that last `gcs setup` notices you are logged in and starts it.

## 4. Add a project

```bash
gcs rig-add https://github.com/<you>/<repo>
```

`gcs rig-add` takes a URL, `owner/repo` or a local path. It clones, tells Claude Code to trust the repo (an
untrusted repo makes every worker die on the folder-trust dialog), runs `gc rig add`, works around the
first-add beads migration failure (beads#4566), and gives the rig one polecat on Sonnet. Then create work:
`bd create "..."` in the rig directory, or ask the mayor: `gc session attach mayor`.

Agents open a **pull request** per finished task and stop; you (or a review bot) merge. For agents that merge
straight to the main branch set `MERGE_MODE=direct` (below).

## Dashboard

The built-in dashboard listens on the server's loopback (`gc dashboard --no-open` prints the URL). From your
laptop: `ssh -L 8372:127.0.0.1:8372 <user>@<server>`, then open <http://127.0.0.1:8372>. The Daily and
Scoreboard pages are a component (`dashboard/install.sh`), installed by the same script whenever it is present.

## Configuration

One file, `~/.config/gcs/config.env`, written with its defaults on the first run and documented line by line.
Edit it, then run `gcs setup`. Keys: `GCS_USER`, `GC_CITY_DIR`, `GCS_REPOS_DIR`, `SWAP`, `TAILSCALE`,
`GIT_NAME`, `GIT_EMAIL`, `MODEL_MAYOR`, `MODEL_DEFAULT`, `MODEL_POLECAT` (`opus`, `sonnet` or `haiku`),
`POLECAT_POOL`, `MERGE_MODE` (`pr` or `direct`), plus the dashboard, review bot and backup sections.
Changing the pool, the models or the merge mode also updates the rigs you already added.

## Adding components

A directory `<name>/` in this repo plugs in without editing `setup.sh`: `install.sh` runs after the city is up
(every setup, so keep it idempotent), `restore.sh` runs before the city is created when setup was started with
`--restore`, and `cmd.sh` runs as `gcs <name> ...`. All `config.env` keys are in the environment.

## Uninstall

```bash
gc stop && gc unregister ~/gc      # stop the city (use your GC_CITY_DIR if you changed it)
gc supervisor uninstall             # remove the systemd unit
rm -rf ~/gc ~/.gcs ~/.config/gcs ~/.local/bin/{gc,gcs,bd,dolt,dcg,caam}   # city, this repo, config, tools
```

Rig repositories are left alone; each has a `.beads/` directory you can delete.

## Credits

Built on [Gas City](https://github.com/gastownhall/gascity) and [beads](https://github.com/gastownhall/beads),
in the spirit of [agentic_coding_flywheel_setup](https://github.com/Dicklesworthstone/agentic_coding_flywheel_setup).
DCG and CAAM are by Jeffrey Emanuel (Dicklesworthstone). Maintained by [alexknips](https://github.com/alexknips).
