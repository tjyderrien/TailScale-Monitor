#!/usr/bin/env python3
"""tailmon — agentless CPU / GPU / RAM / IO / disk monitor for the machines on your tailnet.

Hosts are discovered with `tailscale status --json`. Each Linux peer is probed over SSH by
piping a small stdlib-only Python script to `python3 -` (nothing is installed remotely);
the local machine is probed directly.

    tailmon.py                    one snapshot of every online Linux node
    tailmon.py -w 1               live view, refreshed every second (one persistent SSH stream per host)
    tailmon.py -w 60 --log m.jsonl   keep a history (one JSON object per host per sample)
    tailmon.py --json             machine-readable snapshot
    tailmon.py --hosts web-1,db-1
    tailmon.py mount              sshfs-mount every peer at /media/tailscale/<host> (umount to undo)
    tailmon.py claude|todo|dispatch   run ClaudeExplorer (claude-explorer), which now holds these commands
"""
import argparse
import concurrent.futures as cf
import json
import shlex
import os
import shutil
import subprocess
import sys
import threading
import time

CONFIG_PATH = os.path.expanduser("~/.config/tailmon.json")
DEFAULT_CONFIG = {
    # SSH user per Tailscale hostname; hosts not listed use "default_user"
    # (null: whatever ssh picks, i.e. ~/.ssh/config or your local username).
    "users": {},
    "default_user": None,
    # SSH private key passed with -i; null: ssh's usual keys / agent.
    "identity": None,
    "exclude": [],
    # `tailmon claude|todo|dispatch`: the ClaudeExplorer program to run; null: `claude-explorer` on the PATH.
    "claude_explorer": None,
    "sample_seconds": 1.0,
    # In watch mode, how often to re-run `tailscale status` to pick up hosts that came online / went away.
    "discover_seconds": 30.0,
}

# ---------------------------------------------------------------------------------------------
# Probe: runs on the target machine. Python >= 3.6, standard library only, reads /proc.
# ---------------------------------------------------------------------------------------------
PROBE = r'''
import json, os, pwd, socket, subprocess, sys, time
SAMPLE = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 1  # 0 = stream forever, one JSON line per SAMPLE seconds
LOCAL_FS = {"ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "f2fs", "jfs", "reiserfs", "vfat", "exfat", "ntfs", "fuseblk"}

def cpu_times():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v[:8]), idle

def disks_io():
    phys = {d for d in os.listdir("/sys/block") if os.path.exists("/sys/block/%s/device" % d)}
    r = w = 0
    with open("/proc/diskstats") as f:
        for line in f:
            p = line.split()
            if p[2] in phys:
                r += int(p[5]); w += int(p[9])
    return r * 512, w * 512

def net_io():
    rx = tx = 0
    with open("/proc/net/dev") as f:
        for line in f.readlines()[2:]:
            name, data = line.split(":", 1)
            name = name.strip()
            if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr", "tailscale")):
                continue
            d = data.split()
            rx += int(d[0]); tx += int(d[8])
    return rx, tx

def meminfo():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0]) * 1024
    avail = m.get("MemAvailable", m["MemFree"] + m.get("Cached", 0))
    return {"total": m["MemTotal"], "used": m["MemTotal"] - avail,
            "swap_total": m.get("SwapTotal", 0), "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0)}

def filesystems():
    out, seen = [], set()
    with open("/proc/mounts") as f:
        for line in f:
            dev, mnt, fstype = line.split()[:3]
            if fstype not in LOCAL_FS or dev in seen or mnt.startswith(("/snap/", "/boot/efi")):
                continue
            seen.add(dev)
            try:
                s = os.statvfs(mnt)
            except OSError:
                continue
            total = s.f_blocks * s.f_frsize
            if total < 1 << 30:
                continue
            out.append({"mount": mnt, "total": total, "used": total - s.f_bfree * s.f_frsize,
                        "free": s.f_bavail * s.f_frsize,
                        "inodes_total": s.f_files, "inodes_used": s.f_files - s.f_ffree})
    return out

def gpus():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                            "--format=csv,noheader,nounits"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in r.stdout.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        num = lambda s: float(s) if s.replace(".", "", 1).isdigit() else None
        out.append({"index": int(p[0]), "name": p[1], "util": num(p[2]),
                    "mem_used": (num(p[3]) or 0) * 2**20, "mem_total": (num(p[4]) or 0) * 2**20, "temp": num(p[5])})
    return out

def run(args):
    try:
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                           timeout=10, env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 else None

def sessions():
    """Interactive logins from utmp (`who`); non-interactive ssh commands, like this probe, don't appear."""
    out = []
    for line in (run(["who"]) or "").splitlines():
        p = line.split()
        if len(p) < 2:
            continue
        frm = p[-1][1:-1] if p[-1].startswith("(") and p[-1].endswith(")") else ""
        since = " ".join(p[2:-1] if frm else p[2:])
        out.append({"user": p[0], "tty": p[1], "from": frm, "since": since})
    return out

SLURM_EVERY = 10.0  # don't hammer slurmctld at the sample rate
slurm_cache = [0.0, None]

def slurm():
    if time.time() - slurm_cache[0] < SLURM_EVERY:
        return slurm_cache[1]
    slurm_cache[0] = time.time()
    nodes_out = run(["sinfo", "-h", "-N", "-o", "%N|%T|%C|%m|%e|%G"])
    jobs_out = run(["squeue", "-h", "-o", "%i|%u|%T|%j|%M|%D|%C|%N|%r"])
    if nodes_out is None and jobs_out is None:
        slurm_cache[1] = None  # no SLURM here
        return None
    nodes, seen = [], set()
    for line in (nodes_out or "").splitlines():
        p = line.split("|")
        if len(p) < 6 or p[0] in seen:  # a node in several partitions is listed once per partition
            continue
        seen.add(p[0])
        cpus = (p[2].split("/") + ["0"] * 4)[:4]  # allocated/idle/other/total
        num = lambda s: int(s) if s.isdigit() else None
        nodes.append({"name": p[0], "state": p[1], "cpu_alloc": num(cpus[0]), "cpu_total": num(cpus[3]),
                      "mem_total": (num(p[3]) or 0) * 2**20, "mem_free": (num(p[4]) or 0) * 2**20, "gres": p[5]})
    jobs = []
    for line in (jobs_out or "").splitlines():
        p = line.split("|", 8)
        if len(p) == 9:
            jobs.append({"id": p[0], "user": p[1], "state": p[2], "name": p[3], "time": p[4],
                         "nodes": p[5], "cpus": p[6], "nodelist": p[7], "reason": p[8]})
    slurm_cache[1] = {"nodes": nodes, "jobs": jobs}
    return slurm_cache[1]

def counters():
    return time.time(), cpu_times(), disks_io(), net_io()

def report(prev, cur):
    t0, (c0, i0), (dr0, dw0), (nr0, nt0) = prev
    t1, (c1, i1), (dr1, dw1), (nr1, nt1) = cur
    dt = t1 - t0
    return json.dumps({
        "hostname": socket.gethostname(), "time": t1, "ncpu": os.cpu_count(), "load": os.getloadavg(),
        "cpu": 100.0 * (1 - (i1 - i0) / max(c1 - c0, 1)),
        "mem": meminfo(),
        "disk_io": {"read": (dr1 - dr0) / dt, "write": (dw1 - dw0) / dt},
        "net_io": {"rx": (nr1 - nr0) / dt, "tx": (nt1 - nt0) / dt},
        "fs": filesystems(), "gpus": gpus(),
        "user": pwd.getpwuid(os.getuid()).pw_name, "sessions": sessions(), "slurm": slurm(),
    })

prev, n = counters(), 0
deadline = prev[0]
while True:
    deadline += SAMPLE
    time.sleep(max(0.0, deadline - time.time()))
    cur = counters()
    print(report(prev, cur), flush=True)
    prev, n = cur, n + 1
    if COUNT and n >= COUNT:
        break
'''


# ---------------------------------------------------------------------------------------------
# File tools: a small stdlib-only worker streamed to each host over the existing SSH connection.
# ---------------------------------------------------------------------------------------------
FILE_TOOL = r'''
import fnmatch, io, json, os, re, sys, threading, time

operation, root, pattern = sys.argv[1:4]
root = os.path.expanduser(root)
limit = int(sys.argv[4])
include_hidden = sys.argv[5] == "1"
ignore_case = sys.argv[6] == "1"
fixed = sys.argv[7] == "1"
progress_every = float(sys.argv[8]) if len(sys.argv) > 8 else 0.0  # 0 = no progress reports
started = time.time()
lock = threading.Lock()
# "frac" is the share of the tree already behind the walk: each directory splits its parent's share
# evenly among its subdirectories. "path" is the directory (or, for grep, the file) being read.
state = {"path": root, "frac": 0.0, "dirs": 0, "files": 0, "found": 0, "depth": 0, "max_depth": 0}

def wanted(name):
    return include_hidden or not name.startswith(".")

def emit(value):
    line = json.dumps(value, ensure_ascii=True) + "\n"
    with lock:  # the progress thread writes too: keep lines whole
        sys.stdout.write(line)
        sys.stdout.flush()

def heartbeat():
    # Only sends when the position changed, so an idle or stuck walk costs nothing on the network;
    # the local side times how long the walk has been on the same path.
    last = None
    while True:
        time.sleep(progress_every)
        now = (state["path"], round(state["frac"], 3))
        if now != last:
            last = now
            emit({"progress": dict(state)})

def walk(top):
    # Depth-first like os.walk(topdown=True, followlinks=False), and also yields the share of the tree done.
    # Depth counts directory levels below the root (the root itself is 0).
    stack = [(top, 0.0, 1.0, 0)]
    while stack:
        directory, done, share, depth = stack.pop()
        state["path"], state["frac"], state["depth"] = directory, done, depth
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        state["dirs"] += 1
        state["max_depth"] = max(state["max_depth"], depth)
        dirs, files = [], []
        for entry in entries:
            if not wanted(entry.name):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
            except OSError:
                continue
            (dirs if is_dir else files).append(entry.name)
        state["files"] += len(files)
        yield directory, files
        dirs.sort()
        step = share / len(dirs) if dirs else 0.0
        for i in reversed(range(len(dirs))):
            stack.append((os.path.join(directory, dirs[i]), done + i * step, step, depth + 1))

def found(row):
    global limit
    emit(row)
    state["found"] += 1
    limit -= 1
    if limit <= 0:
        raise SystemExit

if progress_every > 0:
    threading.Thread(target=heartbeat, daemon=True).start()
try:
    if not os.path.isdir(root):
        emit({"error": "no such directory: %s" % root})
    elif operation == "search":
        for directory, files in walk(root):
            for name in files:
                if not fnmatch.fnmatch(name, pattern):
                    continue
                path = os.path.join(directory, name)
                try:
                    is_file = os.path.isfile(path)
                except OSError:
                    continue
                if is_file:
                    found({"path": path})
    elif operation == "grep":
        flags = re.IGNORECASE if ignore_case else 0
        matcher = re.compile(re.escape(pattern) if fixed else pattern, flags)
        for directory, files in walk(root):
            for name in files:
                path = os.path.join(directory, name)
                state["path"] = path
                try:
                    raw = open(path, "rb")
                    if b"\x00" in raw.read(8192):
                        raw.close()
                        continue
                    raw.seek(0)
                    stream = io.TextIOWrapper(raw, encoding="utf-8", errors="replace")
                    try:
                        for number, line in enumerate(stream, 1):
                            if matcher.search(line):
                                found({"path": path, "line": number, "text": line.rstrip("\n\r")})
                    finally:
                        stream.close()
                except (OSError, UnicodeError):
                    pass
except re.error as exc:
    emit({"error": "invalid pattern: %s" % exc})
except SystemExit:
    pass
emit({"done": {"dirs": state["dirs"], "files": state["files"], "max_depth": state["max_depth"],
              "seconds": time.time() - started}})
'''


# ---------------------------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------------------------
def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            cfg.update(json.load(f))
    return cfg


def discover():
    """Return (self_node, [peer nodes]) from tailscale; each node is a dict."""
    st = json.loads(subprocess.run(["tailscale", "status", "--json"], check=True,
                                   stdout=subprocess.PIPE).stdout)

    def node(p):
        return {"name": p["DNSName"].split(".")[0] or p["HostName"], "dns": p["DNSName"].rstrip("."),
                "ip": (p.get("TailscaleIPs") or [None])[0], "os": p.get("OS", ""), "online": p.get("Online", False)}
    return node(st["Self"]), [node(p) for p in (st.get("Peer") or {}).values()]


def probe_cmd(node, cfg, is_self, sample, count):
    probe_args = ["-", str(sample), str(count)]
    if is_self:
        return [sys.executable] + probe_args
    user = cfg["users"].get(node["name"], cfg["default_user"])
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
           "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3"]
    if cfg.get("identity"):
        ssh += ["-i", os.path.expanduser(cfg["identity"])]
    # Connect by the short Tailscale name so `Host <name>` entries in ~/.ssh/config (User, IdentityFile, ...)
    # apply exactly as for a plain `ssh <name>`, but pin the address to the tailnet one.
    ssh += ["-o", "HostName=%s" % (node["dns"] or node["ip"])]
    host = node["name"]
    return ssh + ["%s@%s" % (user, host) if user else host, "python3"] + probe_args


def probe(node, cfg, is_self):
    args = probe_cmd(node, cfg, is_self, cfg["sample_seconds"], 1)
    try:
        r = subprocess.run(args, input=PROBE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=30 + cfg["sample_seconds"])
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    if r.returncode != 0:
        err = (r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1]
        return {"error": err[:120]}
    return json.loads(r.stdout)


def targets_for(cfg, only=None):
    """Return ([(node, is_self)], [skipped nodes])."""
    me, peers = discover()
    targets, skipped = [(me, True)], []
    for p in peers:
        if only and p["name"] not in only:
            continue
        if p["name"] in cfg["exclude"]:
            continue
        if p["os"] != "linux" or not p["online"]:
            skipped.append(p)
            continue
        targets.append((p, False))
    if only:
        targets = [t for t in targets if t[0]["name"] in only]
    return targets, skipped


def collect(cfg, only=None):
    targets, skipped = targets_for(cfg, only)
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        futs = [(n, ex.submit(probe, n, cfg, s)) for n, s in targets]
        results = [dict(f.result(), node=n["name"]) for n, f in futs]
    return results, skipped


FILE_TIMEOUT = 300.0  # seconds per host for search/grep; big multi-disk trees need a while
PROGRESS_SECONDS = 0.5  # how often a host reports its position with -vv (only when it changed)


def file_tool_cmd(node, cfg, is_self, operation, root, pattern, limit, hidden, ignore_case, fixed, progress):
    args = ["-", operation, root, pattern, str(limit), "1" if hidden else "0",
            "1" if ignore_case else "0", "1" if fixed else "0", str(PROGRESS_SECONDS if progress else 0)]
    if is_self:
        return [sys.executable] + args
    user = cfg["users"].get(node["name"], cfg["default_user"])
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
           "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=5",
           "-o", "ServerAliveCountMax=3"]
    if cfg.get("identity"):
        ssh += ["-i", os.path.expanduser(cfg["identity"])]
    ssh += ["-o", "HostName=%s" % (node["dns"] or node["ip"])]
    host = "%s@%s" % (user, node["name"]) if user else node["name"]
    # ssh passes the command through the remote login shell; quote globs and regexes.
    return ssh + [host] + [shlex.quote(value) for value in ["python3"] + args]


class FileJob:
    """One host's search/grep: results and progress as they stream in; partial results survive a timeout."""

    def __init__(self, node):
        self.node = node
        self.rows, self.error, self.done, self.finished = [], None, None, False
        self.progress, self.path_since = None, time.time()
        self.started = time.time()

    def run(self, cmd, timeout):
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, bufsize=1)
        stderr = []
        threading.Thread(target=lambda: stderr.extend(proc.stderr), daemon=True).start()
        timer = threading.Timer(timeout, proc.kill) if timeout else None
        if timer:
            timer.start()
        try:
            proc.stdin.write(FILE_TOOL)
            proc.stdin.close()
        except OSError:
            pass  # the command already died; its error is on stderr
        for line in proc.stdout:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if "progress" in row:
                if not self.progress or row["progress"]["path"] != self.progress["path"]:
                    self.path_since = time.time()
                self.progress = row["progress"]
            elif "done" in row:
                self.done = row["done"]
            else:
                self.rows.append(dict(row, node=self.node["name"]))
        code = proc.wait()
        timed_out = timer is not None and not timer.is_alive() and self.done is None
        if timer:
            timer.cancel()
        if timed_out:
            where = " in %s" % self.progress["path"] if self.progress else ""
            self.error = "timeout after %ds%s" % (timeout, where)
        elif code != 0:
            err = ("".join(stderr).strip().splitlines() or ["exit %d" % code])[-1]
            self.error = err[:160]
        self.finished = True

    def status_line(self, width):
        """One fixed-width line: name, progress bar, share done, matches, time, and the path being read."""
        name = "%-16s" % self.node["name"][:16]
        found = len([r for r in self.rows if "error" not in r])
        elapsed = (self.done or {}).get("seconds") or time.time() - self.started
        if self.finished and self.error:
            return (name + " ERROR: " + self.error)[:width]
        frac = 1.0 if self.finished else (self.progress or {}).get("frac", 0.0)
        filled = int(frac * 20)
        p = self.progress or {}
        head = "%s %s %3d%%  %5d found  %5.1fs  " % (
            name, "█" * filled + "·" * (20 - filled), 100 * frac, found, elapsed)
        if not self.finished:
            head += "depth %2d/%-2d  " % (p.get("depth", 0), p.get("max_depth", 0))
        if self.finished:
            d = self.done or {}
            tail = "done: %d dirs, %d files, depth %d" % (d.get("dirs", 0), d.get("files", 0),
                                                          d.get("max_depth", 0))
        elif self.progress:
            stuck = time.time() - self.path_since
            tail = p["path"] + (" (%ds here)" % stuck if stuck >= 3 else "")
        else:
            tail = "connecting…"
        room = width - len(head)
        if len(tail) > room:
            tail = "…" + tail[len(tail) - room + 1:] if room > 1 else ""
        return head + tail


class ProgressBoard:
    """A block of one line per host on stderr, redrawn in place (no scrolling) and erased at the end."""

    def __init__(self, jobs):
        self.jobs, self.drawn = jobs, 0

    def draw(self):
        width = shutil.get_terminal_size((120, 40)).columns - 1  # never wrap, or cursor-up would miss lines
        lines = [j.status_line(width) for j in sorted(self.jobs, key=lambda j: j.node["name"])]
        up = "\033[%dA" % self.drawn if self.drawn else ""
        sys.stderr.write(up + "\r" + "".join("\033[K%s\n" % l for l in lines))
        sys.stderr.flush()
        self.drawn = len(lines)

    def clear(self):
        if self.drawn:
            sys.stderr.write("\033[%dA\r\033[J" % self.drawn)
            sys.stderr.flush()
            self.drawn = 0


def file_tool(cfg, only, operation, root, pattern, limit, hidden, ignore_case=False, fixed=False,
              verbose=0, timeout=0):
    targets, skipped = targets_for(cfg, only)
    jobs = [FileJob(n) for n, _ in targets]
    board = ProgressBoard(jobs) if verbose >= 2 and sys.stderr.isatty() else None
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(job.run, file_tool_cmd(n, cfg, is_self, operation, root, pattern, limit, hidden,
                                                 ignore_case, fixed, board is not None), timeout)
                for job, (n, is_self) in zip(jobs, targets)]
        try:
            while board and not all(f.done() for f in futs):
                board.draw()
                cf.wait(futs, timeout=0.2)
        finally:
            if board:
                board.clear()
        for f in futs:
            f.result()
    rows = [row for job in jobs for row in job.rows]
    # A limit applies to the combined tailnet result, not independently to every host.
    data = [row for row in rows if "error" not in row][:limit]
    errors = [row for row in rows if "error" in row]
    errors += [{"node": job.node["name"], "error": job.error} for job in jobs if job.error]
    stats = [{"node": job.node["name"], "found": len([r for r in job.rows if "error" not in r]),
              "seconds": (job.done or {}).get("seconds", time.time() - job.started),
              "dirs": (job.done or {}).get("dirs"), "files": (job.done or {}).get("files"),
              "max_depth": (job.done or {}).get("max_depth")} for job in jobs]
    return data, errors, skipped, stats


def render_file_results(operation, rows, errors, skipped, stats, as_json, verbose=0):
    if as_json:
        out = {"results": rows, "errors": errors, "skipped": [p["name"] for p in skipped]}
        if verbose:
            out["stats"] = stats
        print(json.dumps(out, indent=2))
        return
    if rows:
        for row in rows:
            if operation == "grep":
                print("%s:%s:%s: %s" % (row["node"], row["path"], row["line"], row["text"]))
            else:
                print("%s: %s" % (row["node"], row["path"]))
    else:
        print("no matches")
    for row in errors:
        print("%s: ERROR: %s" % (row["node"], row["error"]), file=sys.stderr)
    if skipped:
        print("not searched: " + ", ".join(p["name"] for p in skipped), file=sys.stderr)
    if verbose:
        for s in sorted(stats, key=lambda s: s["node"]):
            walked = ("  %d dirs, %d files, depth %d" % (s["dirs"], s["files"], s["max_depth"])
                      if s["dirs"] is not None else "")
            print("%-16s %5d found  %6.1fs%s" % (s["node"], s["found"], s["seconds"], walked), file=sys.stderr)


# ---------------------------------------------------------------------------------------------
# Mounts: each peer's filesystem under one local directory via sshfs (FUSE, no root needed).
# ---------------------------------------------------------------------------------------------
MOUNT_ROOT = "/media/tailscale"


def mounted_paths():
    """Mount points currently in /proc/mounts (with its octal escapes such as \\040 decoded)."""
    with open("/proc/mounts") as f:
        return {line.split()[1].encode().decode("unicode_escape") for line in f}


def fusermount(*args):
    exe = shutil.which("fusermount3") or "fusermount"
    return subprocess.run([exe] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True)


def sshfs_cmd(node, cfg, remote, point):
    user = cfg["users"].get(node["name"], cfg["default_user"])
    host = "%s@%s" % (user, node["name"]) if user else node["name"]
    # Same SSH settings as the probes; sshfs forwards these -o options to ssh.
    opts = ["BatchMode=yes", "ConnectTimeout=8", "StrictHostKeyChecking=accept-new",
            "ServerAliveInterval=15", "ServerAliveCountMax=3", "reconnect", "idmap=user",
            "HostName=%s" % (node["dns"] or node["ip"])]
    if cfg.get("identity"):
        opts.append("IdentityFile=%s" % os.path.expanduser(cfg["identity"]))
    return ["sshfs", "%s:%s" % (host, remote), point, "-o", ",".join(opts)]


def mount_host(node, cfg, is_self, root, remote, mounted):
    """Mount one host at root/<name> (the local machine gets a symlink); return a status row."""
    point = os.path.join(root, node["name"])
    row = {"node": node["name"], "mount": point}
    if is_self:
        if os.path.islink(point) and os.readlink(point) == remote:
            return dict(row, status="already linked")
        if os.path.lexists(point):
            return dict(row, error="%s exists and is not a link to %s" % (point, remote))
        os.symlink(remote, point)
        return dict(row, status="linked (local machine)")
    if point in mounted:
        try:
            os.stat(point)
            return dict(row, status="already mounted")
        except OSError:
            fusermount("-uz", point)  # stale mount (host went away): drop it and mount again
    try:
        os.makedirs(point, exist_ok=True)
    except OSError as exc:
        return dict(row, error=str(exc))
    try:
        r = subprocess.run(sshfs_cmd(node, cfg, remote, point), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
    except subprocess.TimeoutExpired:
        r = None
    if r is None or r.returncode != 0:
        try:
            os.rmdir(point)
        except OSError:
            pass
        err = "timeout" if r is None else (r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1]
        return dict(row, error=err[:160])
    return dict(row, status="mounted %s" % remote)


def unmount_all(root, only):
    rows = []
    mounted = mounted_paths()
    for name in sorted(os.listdir(root)) if os.path.isdir(root) else []:
        if only and name not in only:
            continue
        point = os.path.join(root, name)
        if os.path.islink(point):
            os.unlink(point)
            rows.append({"node": name, "mount": point, "status": "unlinked"})
        elif point in mounted:
            r = fusermount("-u", point)
            if r.returncode != 0:
                err = (r.stderr.strip().splitlines() or ["exit %d" % r.returncode])[-1]
                rows.append({"node": name, "mount": point, "error": err[:160]})
                continue
            os.rmdir(point)
            rows.append({"node": name, "mount": point, "status": "unmounted"})
        elif os.path.isdir(point) and not os.listdir(point):
            os.rmdir(point)  # leftover empty mount point
    return rows


def mount_main(argv):
    ap = argparse.ArgumentParser(prog="tailmon " + argv[0],
                                 description="Mount the filesystems of the Tailscale peers with sshfs, "
                                             "one directory per host" if argv[0] == "mount"
                                 else "Unmount what `tailmon mount` mounted")
    ap.add_argument("--root", default=MOUNT_ROOT, help="local directory holding one mount per host "
                                                        "(default %(default)s)")
    ap.add_argument("--hosts", help="comma-separated Tailscale hostnames")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    if argv[0] == "mount":
        ap.add_argument("--path", default="/", help="remote directory to mount (default %(default)s)")
    a = ap.parse_args(argv[1:])
    only = set(x for x in a.hosts.split(",") if x) if a.hosts else None
    root = os.path.abspath(os.path.expanduser(a.root))

    skipped = []
    if argv[0] == "umount":
        rows = unmount_all(root, only)
    else:
        if not shutil.which("sshfs"):
            sys.exit("tailmon: sshfs is not installed (e.g. `sudo apt install sshfs`)")
        # As root, ssh would use root's keys and log in as root: run as yourself instead.
        if os.geteuid() == 0:
            sys.exit("tailmon: don't run `mount` as root (no sudo); run it as your own user")
        try:
            os.makedirs(root, exist_ok=True)
        except PermissionError:
            pass
        if not os.access(root, os.W_OK):
            sys.exit("tailmon: %s is missing or not writable by you; create it once with\n"
                     "  sudo install -d -o $USER -g $(id -gn) %s" % (root, root))
        cfg = load_config()
        targets, skipped = targets_for(cfg, only)
        mounted = mounted_paths()
        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            futs = [ex.submit(mount_host, n, cfg, is_self, root, a.path, mounted) for n, is_self in targets]
            rows = [f.result() for f in futs]

    if a.json:
        print(json.dumps({"results": rows, "skipped": [p["name"] for p in skipped]}, indent=2))
        return
    for row in rows:
        if "error" in row:
            print("%-16s %s" % (row["node"], c("ERROR: " + row["error"], 100)), file=sys.stderr)
        else:
            print("%-16s %-28s %s" % (row["node"], row["mount"], row["status"]))
    if not rows:
        print("nothing to %s under %s" % (argv[0], root))
    if skipped:
        print("not mounted: " + ", ".join(
            "%s (%s)" % (p["name"], p["os"] if p["online"] else "offline") for p in skipped), file=sys.stderr)


class Stream:
    """One long-lived probe per host that prints a sample every `interval` seconds; reconnects if it dies."""
    RETRY = 10.0

    def __init__(self, node, cfg, is_self, interval):
        self.node, self.cfg, self.is_self, self.interval = node, cfg, is_self, interval
        self.proc, self.latest, self.received, self.seq, self.retry_at = None, None, 0.0, 0, 0.0

    def ensure(self):
        if (self.proc and self.proc.poll() is None) or time.time() < self.retry_at:
            return
        self.retry_at = time.time() + self.RETRY
        self.proc = subprocess.Popen(probe_cmd(self.node, self.cfg, self.is_self, self.interval, 0),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     universal_newlines=True, bufsize=1)
        self.proc.stdin.write(PROBE)
        self.proc.stdin.close()
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        for line in proc.stdout:
            try:
                self.latest = json.loads(line)
            except ValueError:
                continue
            self.received, self.seq = time.time(), self.seq + 1
        err = proc.stderr.read().strip().splitlines()
        code = proc.wait()
        self.latest = {"error": (err or ["exit %d" % code])[-1][:120]}
        self.received, self.seq = time.time(), self.seq + 1

    def result(self):
        if self.latest is None:
            r = {"pending": "connecting…"}
        elif "error" not in self.latest and time.time() - self.received > max(3 * self.interval, 15):
            r = {"error": "no data for %ds" % (time.time() - self.received)}
        else:
            r = self.latest
        return dict(r, node=self.node["name"])

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()


# ---------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------
COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text, pct):
    if not COLOR or pct is None:
        return text
    code = "31" if pct >= 90 else "33" if pct >= 70 else "32"
    return "\033[%sm%s\033[0m" % (code, text)


def hb(n, rate=False):
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1024 or unit == "P":
            s = ("%.0f%s" if unit == "B" or n >= 100 else "%.1f%s") % (n, unit)
            return s + "/s" if rate else s
        n /= 1024.0


def bar(pct, width=10):
    filled = int(round(pct / 100.0 * width))
    return c("█" * filled + "·" * (width - filled), pct)


def fs_total(fss, label):
    return {"mount": label, **{k: sum(fs.get(k, 0) for fs in fss)
                               for k in ("total", "used", "free", "inodes_total", "inodes_used")}}


def hdd_line(fs):
    p = 100.0 * fs["used"] / fs["total"] if fs["total"] else 0
    # btrfs, vfat, ... report no inode counts (f_files == 0); older probes don't send them at all
    ip = 100.0 * fs["inodes_used"] / fs["inodes_total"] if fs.get("inodes_total") else None
    inodes = "  inodes %s" % c("%5.1f%%" % ip, ip) if ip is not None else ""
    return "  HDD  %s %s  %-24s %-6s free of %-6s%s" % (
        bar(p), c("%5.1f%%" % p, p), fs["mount"][:24], hb(fs["free"]), hb(fs["total"]), inodes)


def render_total(results, skipped):
    """Storage summed over every host; drawn only when at most one Linux host is missing (offline / unreachable)."""
    ok = [r for r in results if "fs" in r]
    missing = sorted([r["node"] for r in results if "fs" not in r] +
                     [p["name"] for p in skipped if p["os"] == "linux" and not p["online"]])
    if len(ok) < 2 or len(missing) > 1:
        return []
    note = c("incomplete: %s missing" % missing[0], 75) if missing else "complete"
    lines = ["TOTAL            %d/%d hosts  %s" % (len(ok), len(ok) + len(missing), note)]
    lines += [hdd_line(fs_total([fs_total(r["fs"], "") for r in ok], "all hosts"))]
    return lines + [""]


def render_users(r):
    """One USERS row: interactive logins, other people highlighted (yellow); nothing if nobody is logged in."""
    by_user = {}
    for s in r.get("sessions") or []:
        by_user.setdefault(s["user"], []).append(s)
    if not by_user:
        return []
    me, parts = r.get("user"), []
    for user in sorted(by_user, key=lambda u: (u == me, u)):
        ss = by_user[user]
        if user == me:
            parts.append("%s (you)%s" % (user, " ×%d" % len(ss) if len(ss) > 1 else ""))
            continue
        where = ", ".join(" ".join(x for x in (s["tty"], s["from"], "since " + s["since"]) if x) for s in ss)
        parts.append(c("%s [%s]" % (user, where), 75))
    others = sum(1 for u in by_user if u != me)
    head = c("%d other" % others, 75) + ": " if others else ""
    return ["  USERS %s%s" % (head, "; ".join(parts))]


SLURM_MAX_JOBS = 15


def render_slurm(r, shown):
    """SLURM nodes + queue. Hosts of the same cluster report the same data: draw it once, under the first one."""
    sl = r.get("slurm")
    if not sl:
        return []
    key = tuple(sorted(n["name"] for n in sl["nodes"])) or ("jobs",) + tuple(j["id"] for j in sl["jobs"])
    if key in shown:
        return ["  SLURM same cluster as %s" % shown[key]]
    shown[key] = r["node"]
    me, lines = r.get("user"), []
    for n in sl["nodes"]:
        cp = 100.0 * n["cpu_alloc"] / n["cpu_total"] if n["cpu_total"] else 0
        mem = "  mem free %s / %s" % (hb(n["mem_free"]), hb(n["mem_total"])) if n["mem_total"] else ""
        gres = "  %s" % n["gres"] if n["gres"] not in ("", "(null)") else ""
        bad = any(s in n["state"] for s in ("down", "drain", "fail"))
        lines.append("  SLURM %s %s  %-14s %s%s %s/%s cpus alloc%s%s" % (
            bar(cp), c("%5.1f%%" % cp, cp), n["name"][:14], c(n["state"], 100) if bad else n["state"],
            " " * max(1, 10 - len(n["state"])), n["cpu_alloc"], n["cpu_total"], mem, gres))
    jobs = sl["jobs"]
    running = [j for j in jobs if j["state"] == "RUNNING"]
    pending = [j for j in jobs if j["state"] == "PENDING"]
    lines.append("  JOBS  %d running, %d pending%s" % (len(running), len(pending),
                                                        ", %d other" % (len(jobs) - len(running) - len(pending))
                                                        if len(jobs) > len(running) + len(pending) else ""))
    order = {"RUNNING": 0, "PENDING": 1}
    for j in sorted(jobs, key=lambda j: order.get(j["state"], 2))[:SLURM_MAX_JOBS]:
        where = j["nodelist"] if j["state"] == "RUNNING" else "(%s)" % j["reason"]
        user = "%-16s" % j["user"][:16]
        user = user if j["user"] == me else c(user, 75)
        lines.append("    %-9s %s %-8s %10s  %4s cpu  %-16s %s" % (
            j["id"][:9], user, j["state"][:8], j["time"], j["cpus"], where[:16], j["name"][:30]))
    if len(jobs) > SLURM_MAX_JOBS:
        lines.append("    … %d more" % (len(jobs) - SLURM_MAX_JOBS))
    return lines


def render(results, skipped):
    w = shutil.get_terminal_size((120, 40)).columns
    lines = [time.strftime("tailmon  %Y-%m-%d %H:%M:%S"), ""]
    slurm_shown = {}
    for r in sorted(results, key=lambda r: r["node"]):
        if "pending" in r:
            lines.append("%-16s %s" % (r["node"], r["pending"]))
            lines.append("")
            continue
        if "error" in r:
            lines.append("%-16s %s" % (r["node"], c("UNREACHABLE: " + r["error"], 100)))
            lines.append("")
            continue
        m = r["mem"]
        mem_pct = 100.0 * m["used"] / m["total"]
        lines.append("%-16s %d cores  load %.2f %.2f %.2f" % ((r["node"], r["ncpu"]) + tuple(r["load"])))
        lines.append("  CPU  %s %s" % (bar(r["cpu"]), c("%5.1f%%" % r["cpu"], r["cpu"])))
        swap = "  swap %s/%s" % (hb(m["swap_used"]), hb(m["swap_total"])) if m["swap_total"] else ""
        lines.append("  RAM  %s %s  %s / %s%s" % (bar(mem_pct), c("%5.1f%%" % mem_pct, mem_pct),
                                                    hb(m["used"]), hb(m["total"]), swap))
        for g in r["gpus"]:
            mp = 100.0 * g["mem_used"] / g["mem_total"] if g["mem_total"] else 0
            util = "%5.1f%%" % g["util"] if g["util"] is not None else "  n/a "
            temp = "  %d°C" % g["temp"] if g["temp"] is not None else ""
            lines.append("  GPU%d %s %s  mem %s / %s  %s%s" % (g["index"], bar(g["util"] or 0), c(util, g["util"]),
                                                              hb(g["mem_used"]), hb(g["mem_total"]), g["name"], temp))
        lines.append("  IO   disk r %-9s w %-9s  net rx %-9s tx %s" % (
            hb(r["disk_io"]["read"], True), hb(r["disk_io"]["write"], True),
            hb(r["net_io"]["rx"], True), hb(r["net_io"]["tx"], True)))
        fss = list(r["fs"])
        if len(fss) > 1:
            fss.append(fs_total(fss, "total"))
        lines += [hdd_line(fs) for fs in fss]
        lines += render_users(r)
        lines += render_slurm(r, slurm_shown)
        lines.append("")
    lines += render_total(results, skipped)
    if skipped:
        lines.append("not probed: " + ", ".join(
            "%s (%s)" % (p["name"], p["os"] if p["online"] else "offline") for p in skipped))
    return "\n".join(l if len(l) < w + 20 else l[:w + 20] for l in lines)


def draw(results, skipped, live):
    out = render(results, skipped)
    if live and COLOR:
        # home the cursor and overwrite in place (clear to end of each line / screen) to avoid flicker
        out = "\033[H" + out.replace("\n", "\033[K\n") + "\033[K\033[J"
    print(out, flush=True)


def rediscover(streams, logged, cfg, only, interval):
    """Start streams for newly seen hosts and stop those that went away; return the skipped list."""
    targets, skipped = targets_for(cfg, only)
    names = {n["name"] for n, _ in targets}
    for name in set(streams) - names:
        streams.pop(name).stop()
        logged.pop(name, None)
    for n, is_self in targets:
        if n["name"] not in streams:
            streams[n["name"]] = Stream(n, cfg, is_self, interval)
    return skipped


def watch(cfg, only, a):
    streams, logged = {}, {}
    skipped = rediscover(streams, logged, cfg, only, a.watch)
    next_discover = time.time() + cfg["discover_seconds"]
    if not a.quiet and COLOR:
        print("\033[2J", end="")
    try:
        deadline = time.time()
        while True:
            if time.time() >= next_discover:
                next_discover = time.time() + cfg["discover_seconds"]
                try:
                    skipped = rediscover(streams, logged, cfg, only, a.watch)
                except (OSError, ValueError, subprocess.CalledProcessError):
                    pass  # tailscale hiccup: keep the current host list and try again later
            for s in streams.values():
                s.ensure()
            results = [s.result() for s in streams.values()]
            if a.log:
                with open(os.path.expanduser(a.log), "a") as f:
                    for s, r in zip(streams.values(), results):
                        if s.seq != logged.get(s.node["name"], 0):
                            logged[s.node["name"]] = s.seq
                            f.write(json.dumps(r) + "\n")
            if not a.quiet:
                draw(results, skipped, True)
            deadline += a.watch
            time.sleep(max(0.0, deadline - time.time()))
    finally:
        for s in streams.values():
            s.stop()


def file_main(argv):
    ap = argparse.ArgumentParser(prog="tailmon", description="Search or grep files across the Tailscale network")
    sub = ap.add_subparsers(dest="operation")

    def common(parser):
        parser.add_argument("--hosts", help="comma-separated Tailscale hostnames to search")
        parser.add_argument("--max-results", type=int, default=1000, metavar="N",
                            help="maximum results across all hosts")
        parser.add_argument("--hidden", action="store_true", help="include hidden files and directories")
        parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
        parser.add_argument("--timeout", type=float, default=FILE_TIMEOUT, metavar="SEC",
                            help="give up on a host after SEC seconds, keeping what it found "
                                 "(default %(default)g, 0: no limit)")
        parser.add_argument("-v", "--verbose", action="count", default=0,
                            help="-v: per-host summary; -vv: also a live progress bar per host with "
                                 "the path being searched (extra network traffic)")

    search = sub.add_parser("search", help="find files by name")
    common(search)
    search.add_argument("path", nargs="?", default=".", help="root directory on each host")
    search.add_argument("--name", default="*", help="filename glob, e.g. '*.log'")

    grep = sub.add_parser("grep", help="search file contents with a regular expression")
    common(grep)
    grep.add_argument("pattern", help="regular expression (or literal with --fixed-strings)")
    grep.add_argument("path", nargs="?", default=".", help="root directory on each host")
    grep.add_argument("--fixed-strings", action="store_true", help="treat pattern as literal text")
    grep.add_argument("-i", "--ignore-case", action="store_true")

    a = ap.parse_args(argv)
    if not a.operation:
        ap.error("choose 'search' or 'grep'")
    if a.max_results < 1:
        ap.error("--max-results must be at least 1")
    cfg = load_config()
    only = set(x for x in a.hosts.split(",") if x) if a.hosts else None
    pattern = a.name if a.operation == "search" else a.pattern
    rows, errors, skipped, stats = file_tool(cfg, only, a.operation, a.path, pattern,
                                              a.max_results, a.hidden,
                                              getattr(a, "ignore_case", False),
                                              getattr(a, "fixed_strings", False),
                                              a.verbose, a.timeout)
    render_file_results(a.operation, rows, errors, skipped, stats, a.json, a.verbose)


def claude_explorer(argv):
    """`tailmon claude|todo|dispatch` moved to ClaudeExplorer: run it with the same arguments."""
    cfg = load_config()
    exe = os.path.expanduser(cfg["claude_explorer"] or "") or shutil.which("claude-explorer")
    if not exe:
        sys.exit("tailmon: `%s` is now part of ClaudeExplorer (https://github.com/tjyderrien/ClaudeXplorer).\n"
                 "  Put claude_explorer.py on the PATH as claude-explorer, or set \"claude_explorer\" in %s"
                 % (argv[0], CONFIG_PATH))
    os.execv(exe, [exe] + ([] if argv[0] == "claude" else [argv[0]]) + argv[1:])


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("search", "grep"):
        return file_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] in ("mount", "umount"):
        return mount_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] in ("claude", "todo", "dispatch"):
        return claude_explorer(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-w", "--watch", type=float, metavar="SEC", help="refresh every SEC seconds")
    ap.add_argument("--json", action="store_true", help="print a JSON snapshot and exit")
    ap.add_argument("--log", metavar="FILE", help="append each sample as JSON lines to FILE")
    ap.add_argument("--hosts", help="comma-separated Tailscale hostnames to restrict to")
    ap.add_argument("--quiet", action="store_true", help="with --log: don't draw the table (for cron/systemd)")
    a = ap.parse_args()
    cfg = load_config()
    only = set(a.hosts.split(",")) if a.hosts else None

    if a.watch and not a.json:
        return watch(cfg, only, a)
    results, skipped = collect(cfg, only)
    if a.log:
        with open(os.path.expanduser(a.log), "a") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")
    if a.json:
        print(json.dumps(results, indent=2))
    elif not a.quiet:
        draw(results, skipped, False)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
