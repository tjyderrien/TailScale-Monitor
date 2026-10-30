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
```

- **Zero install on the monitored hosts.** Hosts are discovered with `tailscale status --json`; each online
  Linux peer is probed over SSH by piping a small, standard-library-only Python script to `python3 -`.
  Nothing is copied or left behind. The local machine is probed directly.
- **Single file, no dependencies.** Just Python 3 and the `tailscale` and `ssh` CLIs.
- **Live view.** `-w 1` keeps one SSH session per host open and streams a sample every second. Hosts that
  join or leave the tailnet are picked up automatically, and dropped connections are retried.
- **NVIDIA GPUs** are reported (utilization, memory, temperature) when `nvidia-smi` is available.
- **Tailnet file tools.** `search` finds files by name and `grep` searches their contents across all
  online Linux peers, without installing anything on them.
- **Tailnet mounts.** `mount` puts every online Linux peer's filesystem under `/media/tailscale/<host>` with
  `sshfs`, so ordinary programs (image viewers, scripts, …) can open files from all machines at once,
  with the same path layout on every host.
- **Claude Code sessions.** `claude` lists the [Claude Code](https://claude.com/claude-code) sessions on every
  host and groups the unfinished ones by what you have to do to finish them. `todo` turns them into
  [Todoist](https://todoist.com) tasks and gives you points for finishing them.

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

Non-Linux and offline peers are listed as "not probed".

## Installation

tailmon is a single script. To run it as `tailmon` from anywhere, link it into a directory on your `PATH`:

```sh
mkdir -p ~/.local/bin && ln -s "$PWD/tailmon.py" ~/.local/bin/tailmon
```

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
./tailmon.py                         # one snapshot of every online Linux node
./tailmon.py -w 1                    # live view, refreshed every second
./tailmon.py -w 5                    # ... every 5 seconds
./tailmon.py --hosts web-1,db-1      # only these Tailscale hostnames
./tailmon.py --json                  # machine-readable snapshot
./tailmon.py -w 60 --log m.jsonl     # keep a history: one JSON object per host per sample
./tailmon.py -w 60 --log m.jsonl --quiet   # same, without drawing (for cron / systemd)
```

Colors are green / yellow / red at < 70 % / ≥ 70 % / ≥ 90 %. Set `NO_COLOR=1` to disable them.

### Files across the tailnet

```sh
./tailmon.py search /var/log --name '*.log' # find matching files on every peer
./tailmon.py grep 'ERROR|WARN' /var/log -i  # grep file contents across the tailnet
./tailmon.py search /media --name '*.pdf' -vv --timeout 0  # live progress, no time limit
./tailmon.py grep --fixed-strings 'needle' /home --hosts server-1 --json
./tailmon.py mount                          # every peer's / at /media/tailscale/<host>
./tailmon.py mount --path /data --hosts gpu-box,server-1
./tailmon.py umount                         # unmount everything under /media/tailscale
```

`search` and `grep` use the same SSH configuration as monitoring. They skip hidden files by default,
limit output to 1,000 results, and only search online Linux peers. Use `--hidden`, `--max-results N`,
`--hosts host1,host2`, and `--json` as needed. `grep` uses Python regular expressions; use
`--fixed-strings` for literal text.

Each host gets 300 seconds (`--timeout SEC`, `0` for no limit); a host that runs out of time keeps what
it found so far, and the error says which path it was reading. `-v` prints a per-host summary (matches,
time, directories and files walked, depth reached below the root) on stderr. `-vv` also shows a live
progress bar per host with the current depth / deepest so far and the path being searched, redrawn in place without scrolling. It costs a little extra traffic: each host
sends its position twice a second, only when it changed, which also shows where a slow disk is stuck:

```
asus             ████████████████████ 100%    699 found    1.5s  done: 5366 dirs, 28871 files, depth 9
luzicka          ██████··············  31%  49954 found   56.7s  depth  4/11  …/Bibliography/EPJ Applied Physics/1998
```

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

### Claude Code sessions

```sh
./tailmon.py claude                         # open sessions of the last 60 days, grouped by what to do
./tailmon.py claude --by host               # one table per machine
./tailmon.py claude --by state              # grouped by session state
./tailmon.py claude --all --days 14         # include finished sessions, last 14 days
./tailmon.py claude --markdown report.md    # also write the report as Markdown
./tailmon.py claude --json                  # raw session data
```

Each host's `~/.claude` is read over the same SSH connection as the other commands, and the sessions are
sorted into:

| Group                             | Meaning                                                                         |
|-----------------------------------|---------------------------------------------------------------------------------|
| Run a command yourself            | The session asked you to run something (often `sudo` or `ssh-add`). The command is shown. |
| Commit / push git                 | Work is finished but the folder has uncommitted changes or unpushed commits.    |
| Answer the session                | The session ended on a question.                                                |
| Restart the task in a new session | Cut off by a usage limit, stopped mid-task, or promised to report back and never did. The last request is shown. |
| Running, just wait                | The session is working right now.                                               |
| Idle, can be closed               | The session is still open but has nothing pending.                              |
| Done                              | Shown only with `--all`.                                                        |

To pick a session back up, go to its folder on that machine and run `claude --resume`. Grouping relies on
keywords in each session's last reply, so a few sessions land in the wrong group. When a task was resumed in
several sessions, only the most recent is shown.

### Todoist tasks and points

```sh
export TODOIST_API_TOKEN=...      # Todoist > Settings > Integrations > Developer
./tailmon.py todo --dry-run       # what would be added or closed
./tailmon.py todo                 # sync: one task per pending session, in the "Claude sessions" project
./tailmon.py todo --score         # points, level and streak, without syncing
./tailmon.py todo --log           # every finished task and what it earned
```

Each run adds a task for every session `claude` lists as pending (except running ones) and checks the
existing tasks:

- ticked off in Todoist: you earn its points;
- the session no longer needs anything (you pushed, answered, …) but the task is still open: the task is
  closed and you earn its points too;
- deleted in Todoist: no points, and it isn't added again;
- the session got older than `--days`: closed, no points.

| Task                 | Points |
|----------------------|-------:|
| Run a command        | 10     |
| Commit / push git    | 10     |
| Answer the session   | 15     |
| Restart the task     | 25     |
| Close an idle session| 5      |

Every 100 points is a level; the streak counts consecutive days with at least one finished task. Hosts that
are offline or unreachable are left alone, so their tasks don't count as finished. Points and the task list
are kept in `~/.local/state/tailmon/todo.json`. To sync regularly, run `tailmon todo` from cron or a systemd
timer.

## Configuration

Optional, in `~/.config/tailmon.json`. Any key you set overrides the built-in default:

```json
{
  "users": {"gpu-box": "alice"},
  "default_user": "admin",
  "identity": "~/.ssh/id_ed25519",
  "exclude": ["printer-pi"],
  "claude_exclude": ["Downloads"],
  "todoist_token": "0123abcd...",
  "todoist_project": "Claude sessions",
  "sample_seconds": 1.0,
  "discover_seconds": 30.0
}
```

| Key                | Meaning                                                                                  |
|--------------------|------------------------------------------------------------------------------------------|
| `users`            | SSH user per Tailscale hostname                                                          |
| `default_user`     | SSH user for hosts not listed in `users` (default `null`: your `~/.ssh/config` / local username) |
| `identity`         | SSH private key passed with `-i` (default `null`: your usual SSH keys / agent)           |
| `exclude`          | Tailscale hostnames never to probe                                                       |
| `claude_exclude`   | For `claude`: regular expressions; sessions whose folder or first request matches are hidden |
| `todoist_token`    | For `todo`: Todoist API token (default `null`: use `TODOIST_API_TOKEN`)                  |
| `todoist_project`  | For `todo`: Todoist project the tasks go to, created if missing (default `Claude sessions`) |
| `sample_seconds`   | Measurement window for rates (CPU %, disk / network throughput) in snapshot mode         |
| `discover_seconds` | In watch mode, how often to re-run `tailscale status` to find new or departed hosts      |

tailmon runs `ssh <short-name>` with the Tailscale address pinned as `HostName`, so `Host <short-name>` entries in
`~/.ssh/config` (`User`, `IdentityFile`, …) apply as they would for a plain `ssh <short-name>`.
SSH connects with `BatchMode=yes` (it never prompts) and `StrictHostKeyChecking=accept-new`.

Nothing is required: with no config file, tailmon connects to each host the way a plain `ssh <host>` would.

## What is measured

| Row    | Source                                                                                          |
|--------|-------------------------------------------------------------------------------------------------|
| CPU    | `/proc/stat`, over the sample interval; plus core count and load average                        |
| RAM    | `/proc/meminfo`: used = total − `MemAvailable`; swap                                            |
| GPU    | `nvidia-smi`: utilization, memory, temperature per GPU                                          |
| IO     | `/proc/diskstats` (physical disks only) and `/proc/net/dev` (excluding `lo`, docker, veth, bridges, tailscale) |
| HDD    | `statvfs` on local filesystems ≥ 1 GiB (ext*, xfs, btrfs, zfs, …), with inode usage when the FS reports it, plus a per-host total |
| TOTAL  | storage summed over all hosts, shown only when at most one Linux host is offline or unreachable (then marked incomplete). Excluded and non-Linux peers don't count |
| USERS  | `who`: interactive logins; people other than the SSH user are highlighted                        |
| SLURM  | `sinfo` / `squeue`, when installed (refreshed at most every 10 s): node state and CPU allocation, then the job queue. Hosts reporting the same cluster show it once |

## License

[MIT](LICENSE)
