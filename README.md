# tailmon

Agentless CPU / GPU / RAM / IO / disk monitor for the machines on your [Tailscale](https://tailscale.com) tailnet,
plus tools to find, grep and open files on all of them at once.

```
tailmon  2026-10-19 19:52:13

server-1         64 cores  load 0.10 0.07 0.07
  CPU  ··········   0.1%
  RAM  █·········   8.0%  10.0G / 125G  swap 27.4M/32.0G
  IO   disk r 0B/s      w 40.0K/s    net rx 37.6K/s   tx 6.2K/s
  HDD  █████·····  50.2%  /                        55.8G  free of 125G    inodes  15.2%
  HDD  ███████···  74.3%  /home                    2.2T   free of 10.5T   inodes   5.9%
  HDD  ███████···  74.1%  total                    2.2T   free of 10.6T   inodes   6.1%
  TEMP CPU0 35°C  CPU1 31°C  pch_wellsburg 59°C
```

- **Zero install on the monitored hosts.** Hosts are discovered with `tailscale status --json`; each online
  Linux peer is probed over SSH by piping a small, standard-library-only Python script to `python3 -`.
  Nothing is copied or left behind. The local machine is probed directly, and may also be a Mac.
- **Single file, no dependencies.** Just Python 3 and the `tailscale` and `ssh` CLIs.
- **Live view.** `-w 1` keeps one SSH session per host open and streams a sample every second. Hosts that
  join or leave the tailnet are picked up automatically, and dropped connections are retried.
- **NVIDIA GPUs** are reported (utilization, memory, temperature) when `nvidia-smi` is available.
- **Temperatures.** CPU, GPU, NVMe / SATA disk and board sensors, each checked against its own limit.
  Overheating hosts are listed at the top in red, and `--alert CMD` runs a command (a desktop notification,
  an e-mail, …) when one gets too hot.
- **Tailnet file tools.** `search` finds files by name and `grep` searches their contents across all
  online Linux and macOS peers, without installing anything on them.
- **Hosts approve what runs.** `tailmon gate install` gives tailmon its own SSH key, which each host only
  accepts for the exact tailmon scripts it approved (by SHA-256): no shell, no forwarding, no other code.
- **Tailnet mounts.** `mount` puts every online Linux and macOS peer's filesystem under `/media/tailscale/<host>` with
  `sshfs`, so ordinary programs (image viewers, scripts, …) can open files from all machines at once,
  with the same path layout on every host.

## Requirements

On the machine running tailmon:

- Python ≥ 3.6
- `tailscale` CLI, logged in to your tailnet
- `ssh` with key-based (non-interactive) access to the hosts you want to monitor
- for `mount` only: `sshfs` (FUSE; e.g. `sudo apt install sshfs`)

On each monitored host:

- Linux with `python3` ≥ 3.6 on the `PATH` (the probe reads `/proc` and `/sys`)
- optionally `nvidia-smi` for GPU stats
- for `mount`: SFTP enabled in `sshd` (the OpenSSH default)

Non-Linux and offline peers are listed as "not probed". macOS hosts aren't monitored (no `/proc`), but
`search`, `grep`, `mount`, `volume` and the gate work on them: turn on *Remote Login* (System Settings →
General → Sharing) and use the `python3` of the Command Line Tools (`xcode-select --install`) or Homebrew.

## Installation

tailmon is a single script. To run it as `tailmon` from anywhere, link it into a directory on your `PATH`:

```sh
mkdir -p ~/.local/bin && ln -s "$PWD/tailmon.py" ~/.local/bin/tailmon
```

The examples below use `tailmon`; without the link, run `./tailmon.py` from the repository instead.

### Tab completion

`tailmon` then Tab lists the commands, then each command's options and arguments (and the Tailscale hostnames
after `--hosts`). Enable it in your shell's startup file:

```sh
# ~/.zshrc (after compinit; add `autoload -U compinit && compinit` if your .zshrc has none)
eval "$(tailmon completion zsh)"
# ~/.bashrc
eval "$(tailmon completion bash)"
```

The macOS installer already puts the zsh completion in `/usr/local/share/zsh/site-functions`, so zsh only needs
`compinit`.

### macOS installer

`macos/build-pkg.sh` (run on a Mac) builds `macos/build/tailmon-<version>.pkg`, a graphical installer that puts
`tailmon` in `/usr/local/bin` with its man page and zsh completion, offers to install the Command Line Tools if Python 3 is
missing, and adds a `tailscale` command when the Tailscale app is installed without one. Without a Mac, run the
*macOS installer* workflow from the GitHub Actions tab and download the `.pkg` from the run. Set `SIGN_ID` (and
`NOTARY_PROFILE`) to sign and notarize it; an unsigned package opens with right-click → *Open*, or *Open Anyway*
in System Settings → Privacy & Security. To uninstall: `sudo /usr/local/share/tailmon/uninstall.sh`.

## Manual page

A man page is included. Read it in place with `man -l tailmon.1`, or install it:

```sh
mkdir -p ~/.local/share/man/man1 && cp tailmon.1 ~/.local/share/man/man1/   # just for you
sudo install -m644 tailmon.1 /usr/local/share/man/man1/ && sudo mandb        # system-wide
man tailmon
```

## Usage

### Monitoring

```sh
tailmon                              # one snapshot of every online Linux node
tailmon -w 1                         # live view, refreshed every second
tailmon -w 5                         # ... every 5 seconds
tailmon --hosts web-1,db-1           # only these Tailscale hostnames
tailmon --json                       # machine-readable snapshot
tailmon -w 60 --log m.jsonl          # keep a history: one JSON object per host per sample
tailmon -w 60 --log m.jsonl --quiet  # same, without drawing (for cron / systemd)
```

Colors are green / yellow / red at < 70 % / ≥ 70 % / ≥ 90 %. Set `NO_COLOR=1` to disable them.

### Temperatures

```sh
tailmon -w 5 --alert 'notify-send -u critical "$TAILMON_HOST is hot" "$TAILMON_SENSOR $TAILMON_TEMP°C"'
tailmon -w 60 --quiet --alert 'echo "$TAILMON_HOST $TAILMON_SENSOR $TAILMON_TEMP°C" | mail -s hot me@example.org'
tailmon --quiet || echo "something is hot"    # snapshot: exit status 3 when a sensor is at its limit
```

The `TEMP` row shows, per sensor chip, its hottest reading: `CPU` (one per socket: `CPU0`, `CPU1`), `GPU0` …,
disks by name (`nvme0`, `sda`), and board chips by driver name (`acpitz`, `nct6776`, `pch_*`, …). Each
reading is compared to the limit the sensor itself reports (`temp*_max`, the throttling point; `crit` − 10 if it
only has a critical one), or else to 85 °C for CPUs and GPUs, 65 °C for disks and 80 °C for the rest. It turns
yellow ("warm") within 10 °C of that limit and red ("hot") at it; any warm or hot sensor is also listed in a
`HOT` / `WARM` line at the top of the screen, worst first, as `host sensor temp/limit`.

`--alert CMD` runs `CMD` with `sh` each time a sensor becomes hot (once, until it cools below its limit and
heats up again), with `TAILMON_HOST`, `TAILMON_SENSOR`, `TAILMON_CHIP`, `TAILMON_KIND` (`cpu`, `gpu`,
`disk`, `other`), `TAILMON_TEMP`, `TAILMON_LIMIT` and `TAILMON_LEVEL` in its environment. A snapshot runs it
for every hot sensor and exits with status 3; with `--quiet` or `--json` each hot sensor is also printed on stderr.

Readings come from `/sys/class/hwmon` (or `/sys/class/thermal` where there is none) and `nvidia-smi`, as
the SSH user, no root needed. NVMe drives have a sensor out of the box; SATA disks need the kernel's
`drivetemp` module on that host (`sudo modprobe drivetemp`, and `echo drivetemp | sudo tee
/etc/modules-load.d/drivetemp.conf` to keep it after a reboot). Limits can be set by hand in the config
(`temp_high`), e.g. for a disk that the vendor rates lower.

### Sound volume

```sh
tailmon volume                       # volume of every host
tailmon volume 40                    # set every host to 40 %
tailmon volume +5 --hosts asus       # step up (or -5 down) on one host
tailmon volume mute --exclude luzicka   # also unmute, toggle
tailmon serve                        # web page for the whole network: http://<this-pc>:8765/
tailmon serve --bind 100.x.y.z --token s3cret   # tailnet only, and http://...:8765/?token=s3cret
```

Each host's default output is driven with `wpctl` (PipeWire), `pactl` (PulseAudio), `amixer` (ALSA) or,
on macOS, `osascript`, whichever answers first, as the SSH user; nothing is installed remotely. The sound server must be running,
i.e. someone logged into a desktop session as that user. `serve` shows a slider and a mute button per host
and exposes a JSON API: `GET /api/volume` lists the hosts, `POST /api/volume/<host>` with
`{"action": "set", "value": 40}` (or `up`, `down`, `mute`, `unmute`, `toggle`, `get`) changes one.
macOS, Windows and Android hosts are included too (monitoring stays Linux-only):

- **Windows**: enable the OpenSSH server (Settings → System → Optional features → *OpenSSH Server*, then
  `Start-Service sshd; Set-Service sshd -StartupType Automatic` in an admin PowerShell). PowerShell drives
  the default playback device through the Core Audio API; nothing else to install. Set the Windows login
  in `users` in the config if it differs from yours.
- **Android**: install [Termux](https://f-droid.org/packages/com.termux/) and
  [Termux:API](https://f-droid.org/packages/com.termux.api/) from F-Droid (the same source for both), then
  `pkg install openssh python termux-api && passwd && sshd` in Termux (port 8022; add your key to
  `~/.ssh/authorized_keys`) and a `Host <phone> / Port 8022` entry in `~/.ssh/config`. This sets the media
  volume, in Android's 15 steps; Android has no mute for it, so `mute` remembers the level and sets 0.
- **iPhone**: not possible, iOS lets no app set the system volume from outside.

It listens on every interface by default with no password: anyone who can reach the port can change the
volume. Use `--token`, or `--bind` with this PC's Tailscale IP so only the tailnet can reach it.

### Files across the tailnet

```sh
tailmon search /var/log --name '*.log'            # find matching files on every peer
tailmon grep 'ERROR|WARN' /var/log -i             # grep file contents across the tailnet
tailmon search /media --name '*.pdf' -vv --timeout 0  # live progress, no time limit
tailmon search ~/Documents --name '*invoice*' -i     # case-insensitive: also Invoice.PDF
tailmon grep --fixed-strings 'needle' /home --hosts server-1 --json
tailmon mount                                     # every peer's / at /media/tailscale/<host>
tailmon mount --path /data --hosts gpu-box,server-1
tailmon umount                                    # unmount everything under /media/tailscale
```

`search` and `grep` use the same SSH configuration as monitoring. They skip hidden files, `/proc` and
sshfs mounts below the root (peers under `/media/tailscale` can mount each other in a loop; `--boom`
lifts this guard) by default, limit output to 1,000 results, and only search online Linux and macOS peers. Use
`--hidden`, `--proc`, `--boom`, `--max-results N`, `--hosts host1,host2`, `--exclude host3`, and `--json`
as needed (`--exclude biatri-1` leaves out a host whose huge disks would make everyone wait; the `exclude`
config key does it for good). `grep`
uses Python regular expressions; use `--fixed-strings` for literal text.

Each host gets 300 seconds (`--timeout SEC`, `0` for no limit); a host that runs out of time keeps what
it found so far, and the error says which path it was reading. `-v` prints a per-host summary (matches,
time, walk rate, directories and files walked, depth reached below the root) on stderr. `-vv` also shows a
live progress bar per host with the walk rate, the current depth / deepest so far and the path being
searched, redrawn in place without scrolling. It costs a little extra traffic: each host sends its
position twice a second, only when it changed, which also shows where a slow disk is stuck:

```
asus             ████████████████████ 100%    699 found    1.5s   22.8k/s  done: 5366 dirs, 28871 files, depth 9
luzicka          ██████··············  31%  49954 found   56.7s    3.1k/s  depth  4/11  …/Bibliography/EPJ Applied Physics/1998
```

The walk rate (directories plus files per second) shows where searching is fast. Walking a tree is
bound by latency, not bandwidth: each directory listing is a round trip, so a host reads its own disks
far faster than another host can read them through sshfs, even on a fast LAN. Walking the same tree,
with warm caches on the host holding it:

| Tree on (link to this PC)    | walked by that host | walked here through sshfs |
|------------------------------|---------------------|---------------------------|
| asus (internet, ~20 ms)      | 165k/s              | 0.3k/s (~550× slower)     |
| biatri-1 (LAN, 0.3 ms)       | 165k/s              | 2.4k–10.5k/s (16–70× slower) |

This is why `search` and `grep` skip sshfs mounts by default: each peer searches its own drives and only
the matches cross the network.

`mount` makes the whole tailnet look like one directory tree, so any local tool works on it:

```sh
tailmon mount
ls /media/tailscale/                              # one entry per host
du -sh /media/tailscale/*/data/results            # same path on every machine
eog /media/tailscale/gpu-box/home/me/plots/*.png  # open remote files with local programs
tailmon umount
```

`mount` needs `sshfs` locally (`sudo apt install sshfs`) and a mount root you own; create it once with
`sudo install -d -o $USER -g $(id -gn) /media/tailscale`, or pick another one with `--root DIR`. Each peer is
mounted read-write as your SSH user at `/media/tailscale/<host>` (the `--path` directory, `/` by default); the
local machine gets a symlink there instead, so all hosts share one path layout. Mounts reconnect after
network drops; running `mount` again only fills in missing or stale ones. `umount` also takes `--hosts`.
Mounts don't survive a reboot: run `tailmon mount` again (e.g. from your session startup).
Don't run it with `sudo`: it must use your own SSH keys and user.

On macOS, a search over SSH can't see `~/Documents`, `~/Desktop`, `~/Downloads` and other protected
folders until you allow it: System Settings → General → Sharing → Remote Login → ⓘ → *Allow full disk
access for remote users*.

### Tasks each host ran

```sh
tailmon tasks                    # the last 3 Todoist tasks each host ran
tailmon tasks -n 5 --hosts biatri-2,bumblefoot
tailmon tasks --json             # with session IDs, folders, start and end times, cost
```

Lists the tasks that [`claude-explorer dispatch`](https://github.com/tjyderrien/ClaudeXplorer) handed to
Claude Code agents, grouped by host, newest first, with their state (`running`, `done`, `failed`, `limit`),
cost, and the Claude session (its hash, as in `claude-explorer`) and folder the agent worked in. Tailnet hosts that ran nothing are listed as `no task`. The data comes from the comments dispatch
posts on each task ("Agent started on …", "Agent on …"), for open tasks labelled `@claude-running`,
`@claude-done` or `@claude-failed`, and for tasks you ticked off in the last `--days` days (default 60,
shown as `[closed]`).

It only reads: nothing is changed in Todoist or on the hosts. It uses `TODOIST_API_TOKEN`, or `todoist_token`
in `~/.config/tailmon.json` or `~/.config/claude-explorer.json`.

### Gate: hosts approve what tailmon runs

By default tailmon logs in with your normal SSH key, which can run anything on the host. With a gate, it
uses a key of its own that each host ties, in `~/.ssh/authorized_keys`, to a small forced command (the
gate) instead. The gate reads the script tailmon sends, and runs it only if its SHA-256 is in the host's
`~/.config/tailmon/approved`; anything else (a shell, another script, port forwarding, a terminal) is
refused and logged in `~/.config/tailmon/gate.log`. So a stolen or misused tailmon key can only run the
scripts the host approved, and a changed tailmon has to be approved again.

```sh
tailmon gate install                 # every online Linux, macOS and Android host (or --hosts a,b)
tailmon gate status                  # ask each gate, with the gate key, whether it runs this tailmon
tailmon gate approve                 # after updating tailmon.py: approve its new scripts
tailmon gate hashes                  # the SHA-256 to approve by hand on a host
tailmon gate remove --hosts asus     # back to your normal login on that host
```

`install` logs in once with your normal key. It creates the gate key `~/.ssh/tailmon_gate` here if
needed, copies the gate to `~/.local/share/tailmon/gate.py` on each host, approves this tailmon's three
scripts (monitoring probe, file tool, volume) and adds one line to `authorized_keys`, after a backup
(`authorized_keys.before-tailmon`):

```
restrict,command="/usr/bin/python3 /home/you/.local/share/tailmon/gate.py" ssh-ed25519 AAAA… tailmon-gate@pc
```

The forced command uses the absolute path of `python3` as the host's login shell finds it, since `sshd`
runs it with a bare `PATH` (Homebrew's `/opt/homebrew/bin` or Termux's would be missed). Gated hosts are
listed in `gate_hosts` in the config. tailmon then reaches them with the gate key only
(`IdentitiesOnly=yes`, no shared `ControlMaster` connection) and a command only the gate understands, so
if a host lets the connection in with another key, the command fails instead of running unchecked.

On a host, its owner can manage the gate without tailmon (use the `python3` from the `authorized_keys` line):

```sh
python3 ~/.local/share/tailmon/gate.py list             # approved scripts, latest runs and refusals
python3 ~/.local/share/tailmon/gate.py approve <sha256> [name]
python3 ~/.local/share/tailmon/gate.py revoke <sha256>
```

Your normal key keeps working for your own logins, and `mount` still uses it: SFTP gives access to all
files, which a gate can't narrow down. Windows hosts have no gate yet and keep the normal key.

## Configuration

Optional, in `~/.config/tailmon.json`. Any key you set overrides the built-in default:

```json
{
  "users": {"gpu-box": "alice"},
  "default_user": "admin",
  "identity": "~/.ssh/id_ed25519",
  "exclude": ["printer-pi"],
  "sample_seconds": 1.0,
  "discover_seconds": 30.0,
  "temp_high": {"disk": 55, "nvme": 70},
  "temp_warn_margin": 10.0,
  "gate_key": "~/.ssh/tailmon_gate",
  "gate_hosts": ["asus", "mac-mini"]
}
```

| Key                | Meaning                                                                                  |
|--------------------|------------------------------------------------------------------------------------------|
| `users`            | SSH user per Tailscale hostname                                                          |
| `default_user`     | SSH user for hosts not listed in `users` (default `null`: your `~/.ssh/config` / local username) |
| `identity`         | SSH private key passed with `-i` (default `null`: your usual SSH keys / agent)           |
| `exclude`          | Tailscale hostnames never to probe                                                       |
| `sample_seconds`   | Measurement window for rates (CPU %, disk / network throughput) in snapshot mode         |
| `discover_seconds` | In watch mode, how often to re-run `tailscale status` to find new or departed hosts      |
| `temp_high`        | Temperature limits in °C by kind (`cpu`, `gpu`, `disk`, `other`) or chip (`nvme`, `coretemp`, `nvidia`, …; a chip beats its kind), replacing the sensor's own limit (default `{}`) |
| `temp_warn_margin` | A sensor turns yellow this many °C below its limit (default `10`)                        |
| `gate_key`         | tailmon's own SSH key for gated hosts (default `~/.ssh/tailmon_gate`, created by `tailmon gate install`) |
| `gate_hosts`       | Hosts reached through their gate, kept up to date by `tailmon gate install` / `remove` (default `[]`) |

tailmon runs `ssh <short-name>` with the Tailscale address pinned as `HostName`, so `Host <short-name>` entries in
`~/.ssh/config` (`User`, `IdentityFile`, …) apply as they would for a plain `ssh <short-name>`.
SSH connects with `BatchMode=yes` (it never prompts) and `StrictHostKeyChecking=accept-new`.

Nothing is required: with no config file, tailmon connects to each host the way a plain `ssh <host>` would.

## What is measured

| Row    | Source                                                                                          |
|--------|-------------------------------------------------------------------------------------------------|
| CPU    | `/proc/stat`, over the sample interval; plus core count and load average                        |
| RAM    | `/proc/meminfo`: used = total − `MemAvailable`; swap                                            |
| GPU    | `nvidia-smi`: utilization and memory per GPU                                                    |
| TEMP   | `/sys/class/hwmon` (`/sys/class/thermal` if empty) and `nvidia-smi`: hottest reading per chip, against its limit |
| IO     | `/proc/diskstats` (physical disks only) and `/proc/net/dev` (excluding `lo`, docker, veth, bridges, tailscale) |
| HDD    | `statvfs` on local filesystems ≥ 1 GiB (ext*, xfs, btrfs, zfs, …), with inode usage when the FS reports it, plus a per-host total |
| (Mac)  | when tailmon itself runs on macOS, the local machine is probed too: CPU from `host_statistics`, RAM from `vm_stat` (used = app memory + wired + compressed, as in Activity Monitor), IO from `ioreg` and `netstat -ibn`, HDD from `mount` (an APFS container counted once) |
| TOTAL  | storage summed over all hosts, shown only when at most one Linux host is offline or unreachable (then marked incomplete). Excluded and non-Linux peers don't count |
| USERS  | `who`: interactive logins; people other than the SSH user are highlighted                        |
| SLURM  | `sinfo` / `squeue`, when installed (refreshed at most every 10 s): node state and CPU allocation, then the job queue. Hosts reporting the same cluster show it once |

## License

[MIT](LICENSE)
