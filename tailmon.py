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
    tailmon.py volume 40          set every host's sound volume to 40 % (+5, -5, mute, toggle; nothing: show)
    tailmon.py serve              web page to control every host's volume, on http://<this-pc>:8765/
    tailmon.py mount              sshfs-mount every peer at /media/tailscale/<host> (umount to undo)
    tailmon.py gate install       let each host approve the scripts tailmon runs on it (own key + gate)
    tailmon.py tasks              the last 3 Todoist tasks each host ran (claude-explorer dispatch), read-only
"""
import argparse
import base64
import concurrent.futures as cf
import hashlib
import json
import locale
import shlex
import os
import re
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
    "sample_seconds": 1.0,
    # In watch mode, how often to re-run `tailscale status` to pick up hosts that came online / went away.
    "discover_seconds": 30.0,
    # Temperature limits in °C, by sensor kind ("cpu", "gpu", "disk", "other") or chip name ("nvme",
    # "coretemp", "acpitz", ...; a chip beats its kind). They replace the limit the sensor reports itself.
    # A sensor is red ("hot") at its limit and yellow ("warm") within temp_warn_margin of it.
    "temp_high": {},
    "temp_warn_margin": 10.0,
    # `tailmon gate`: the SSH key that hosts with a gate accept only for approved tailmon scripts, and
    # those hosts (kept up to date by `tailmon gate install` / `remove`).
    "gate_key": "~/.ssh/tailmon_gate",
    "gate_hosts": [],
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

HWMON_KIND = {"coretemp": "cpu", "k10temp": "cpu", "k8temp": "cpu", "zenpower": "cpu", "cpu_thermal": "cpu",
              "soc_thermal": "cpu", "amdgpu": "gpu", "radeon": "gpu", "nouveau": "gpu", "nvme": "disk",
              "drivetemp": "disk"}

def read_milli(path):
    """A /sys temperature in m°C as °C; None if missing or implausible (unconnected inputs and unset limits
    read as 0, some NVMe limits as 65261.85)."""
    try:
        with open(path) as f:
            v = int(f.read().strip()) / 1000.0
    except (OSError, ValueError):
        return None
    return v if 0 < v < 150 else None

def read_text(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""

def hwmon_device(d):
    """Which disk an nvme / drivetemp sensor belongs to (nvme0, sda); '' if unknown."""
    dev = os.path.join(d, "device")
    for sub in ("block", "nvme"):
        try:
            return sorted(os.listdir(os.path.join(dev, sub)))[0]
        except (OSError, IndexError):
            pass
    base = os.path.basename(os.path.realpath(dev))
    return base if base.startswith("nvme") else ""

def temps():
    """One entry per sensor chip, reporting its hottest reading relative to its own limits (°C)."""
    out, base = [], "/sys/class/hwmon"
    try:
        chips = sorted(os.listdir(base), key=lambda s: int(s[5:]) if s[5:].isdigit() else 0)
    except OSError:
        chips = []
    for h in chips:
        d = os.path.join(base, h)
        chip = read_text(os.path.join(d, "name"))
        try:
            inputs = [f for f in os.listdir(d) if f.startswith("temp") and f.endswith("_input")]
        except OSError:
            continue
        best = None
        for f in inputs:
            p = os.path.join(d, f[:-6])
            t = read_milli(p + "_input")
            if t is None:
                continue
            s = {"temp": t, "label": read_text(p + "_label"), "high": read_milli(p + "_max"),
                 "crit": read_milli(p + "_crit")}
            if s["high"] is None and s["crit"] is not None:
                s["high"] = s["crit"] - 10  # e.g. amdgpu, cpu_thermal: only a critical point
            # hottest relative to its limit, so a 70°C NVMe (max 70) outranks its 40°C other sensor
            key = t - s["high"] if s["high"] is not None else t - 1000
            if best is None or key > best[0]:
                best = (key, s)
        if best:
            dev = hwmon_device(d) if chip in ("nvme", "drivetemp") else ""
            out.append(dict(best[1], chip=chip, kind=HWMON_KIND.get(chip, "other"), device=dev))
    if out:
        return out
    # No hwmon (some VMs, ARM boards): ACPI / device-tree thermal zones with their trip points.
    base = "/sys/class/thermal"
    try:
        zones = sorted(z for z in os.listdir(base) if z.startswith("thermal_zone"))
    except OSError:
        zones = []
    for z in zones:
        d = os.path.join(base, z)
        t = read_milli(os.path.join(d, "temp"))
        if t is None:
            continue
        trips = {}
        for i in range(16):
            kind = read_text(os.path.join(d, "trip_point_%d_type" % i))
            if not kind:
                break
            v = read_milli(os.path.join(d, "trip_point_%d_temp" % i))
            if v is not None:
                trips[kind] = min(v, trips.get(kind, v))
        typ = read_text(os.path.join(d, "type"))
        crit = trips.get("critical")
        high = trips.get("hot", trips.get("passive", crit - 10 if crit is not None else None))
        cpu = any(k in typ for k in ("cpu", "pkg", "soc", "x86"))
        out.append({"temp": t, "label": "", "high": high, "crit": crit, "chip": typ or z,
                    "kind": "cpu" if cpu else "other", "device": ""})
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
        "fs": filesystems(), "gpus": gpus(), "temps": temps(),
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
include_proc = len(sys.argv) > 9 and sys.argv[9] == "1"  # walk into /proc when the root is above it
boom = len(sys.argv) > 10 and sys.argv[10] == "1"  # no loop guard: follow sshfs mounts and revisited dirs

def sshfs_mounts():
    # Mount points of sshfs filesystems (e.g. `tailmon mount` under /media/tailscale): each one is
    # another host's tree, which can hold sshfs mounts of this host again, without end.
    points = set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                fields = line.split()
                if len(fields) > 2 and fields[2] == "fuse.sshfs":
                    points.add(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[1]))
    except OSError:
        pass
    return points

blocked = set() if boom else sshfs_mounts()
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
    seen = set()
    while stack:
        directory, done, share, depth = stack.pop()
        state["path"], state["frac"], state["depth"] = directory, done, depth
        if not boom:
            # The same directory reached twice (a bind mount of a parent, say) would loop forever.
            try:
                st = os.stat(directory)
            except OSError:
                continue
            if (st.st_dev, st.st_ino) in seen:
                continue
            seen.add((st.st_dev, st.st_ino))
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
            child = os.path.abspath(os.path.join(directory, dirs[i]))
            if not include_proc and child == "/proc":
                continue  # endless pseudo-files; searching /proc itself as the root still works
            if child in blocked:
                continue  # only mounts below the root: giving an sshfs mount as the root still works
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
        name_matches = re.compile(fnmatch.translate(pattern), re.IGNORECASE if ignore_case else 0).match
        for directory, files in walk(root):
            for name in files:
                if not name_matches(name):
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


def gated(node, cfg):
    """Whether tailmon reaches this host through its gate (`tailmon gate install`)."""
    return node["name"] in (cfg.get("gate_hosts") or ()) and node["os"] in GATE_OSES


def ssh_base(node, cfg, keepalive=False, gate=False):
    """ssh and its options for a peer, up to the remote command. With `gate`, only tailmon's gate key."""
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new"]
    if keepalive:
        ssh += ["-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3"]
    if gate:
        # No shared ControlMaster connection either: it would be logged in with your full key.
        ssh += ["-i", os.path.expanduser(cfg["gate_key"]), "-o", "IdentitiesOnly=yes", "-o", "ControlPath=none"]
    elif cfg.get("identity"):
        ssh += ["-i", os.path.expanduser(cfg["identity"])]
    # Connect by the short Tailscale name so `Host <name>` entries in ~/.ssh/config (User, IdentityFile, ...)
    # apply exactly as for a plain `ssh <name>`, but pin the address to the tailnet one.
    ssh += ["-o", "HostName=%s" % (node["dns"] or node["ip"])]
    user = cfg["users"].get(node["name"], cfg["default_user"])
    return ssh + ["%s@%s" % (user, node["name"]) if user else node["name"]]


def python_cmd(node, cfg, args, keepalive=False):
    """ssh command running `python3 - ARGS` (the script comes on stdin) on a peer, through its gate if it has
    one. A gated command starts with `tailmon-gate`, which only the gate accepts: if the host answers with
    another key, the command fails instead of running unchecked. ssh hands the words to the remote shell
    (or the gate), so each is quoted."""
    gate = gated(node, cfg)
    words = (["tailmon-gate"] if gate else []) + ["python3", "-"] + [str(x) for x in args]
    return ssh_base(node, cfg, keepalive, gate) + [shlex.quote(w) for w in words]


def ssh_error(stderr_lines, code, width=120):
    """The last stderr line of a failed remote command, with a hint when a gate is missing or refused."""
    lines = [l for l in stderr_lines if l.strip()]
    err = lines[-1].strip() if lines else "exit %d" % code
    if "tailmon-gate" in err and "not found" in err:
        err = "no tailmon gate here, or it refused the gate key (tailmon gate install)"
    return err[:width]


def probe_cmd(node, cfg, is_self, sample, count):
    if is_self:
        return [sys.executable, "-", str(sample), str(count)]
    return python_cmd(node, cfg, [sample, count], keepalive=True)


def probe(node, cfg, is_self):
    args = probe_cmd(node, cfg, is_self, cfg["sample_seconds"], 1)
    try:
        r = subprocess.run(args, input=PROBE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=30 + cfg["sample_seconds"])
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    if r.returncode != 0:
        return {"error": ssh_error(r.stderr.splitlines(), r.returncode)}
    return json.loads(r.stdout)


def targets_for(cfg, only=None, exclude=(), oses=("linux",)):
    """Return ([(node, is_self)], [skipped nodes]); `exclude` also drops this machine if it is named."""
    me, peers = discover()
    targets, skipped = ([] if me["name"] in exclude else [(me, True)]), []
    for p in peers:
        if only and p["name"] not in only:
            continue
        if p["name"] in cfg["exclude"] or p["name"] in exclude:
            continue
        if p["os"] not in oses or not p["online"]:
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


FILE_OSES = ("linux", "macOS")  # the file tool is plain Python; the probe needs Linux's /proc and /sys
FILE_TIMEOUT = 300.0  # seconds per host for search/grep; big multi-disk trees need a while
PROGRESS_SECONDS = 0.5  # how often a host reports its position with -vv (only when it changed)


def walk_rate(dirs, files, seconds):
    """Directories plus files walked per second, e.g. '12.3k/s': compares how fast hosts (or paths) are read."""
    if not seconds or dirs is None:
        return "   -  /s"
    rate = (dirs + (files or 0)) / seconds
    return ("%5.1fk/s" % (rate / 1000)) if rate >= 1000 else ("%6d/s" % rate)


def file_tool_cmd(node, cfg, is_self, operation, root, pattern, limit, hidden, ignore_case, fixed, progress,
                  proc=False, boom=False):
    args = ["-", operation, root, pattern, str(limit), "1" if hidden else "0",
            "1" if ignore_case else "0", "1" if fixed else "0", str(PROGRESS_SECONDS if progress else 0),
            "1" if proc else "0", "1" if boom else "0"]
    if is_self:
        return [sys.executable] + args
    return python_cmd(node, cfg, args[1:], keepalive=True)  # quotes globs and regexes for the remote shell


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
            self.error = ssh_error("".join(stderr).splitlines(), code, 160)
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
        d = self.done or p
        head = "%s %s %3d%%  %5d found  %5.1fs  %s  " % (
            name, "█" * filled + "·" * (20 - filled), 100 * frac, found, elapsed,
            walk_rate(d.get("dirs"), d.get("files"), elapsed))
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
              verbose=0, timeout=0, proc=False, boom=False, exclude=()):
    targets, skipped = targets_for(cfg, only, exclude, FILE_OSES)
    jobs = [FileJob(n) for n, _ in targets]
    board = ProgressBoard(jobs) if verbose >= 2 and sys.stderr.isatty() else None
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(job.run, file_tool_cmd(n, cfg, is_self, operation, root, pattern, limit, hidden,
                                                 ignore_case, fixed, board is not None, proc, boom),
                                 timeout)
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
            print("%-16s %5d found  %6.1fs  %s%s" % (s["node"], s["found"], s["seconds"],
                                                     walk_rate(s["dirs"], s["files"], s["seconds"]), walked),
                  file=sys.stderr)


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
        targets, skipped = targets_for(cfg, only, oses=FILE_OSES)
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
        err = proc.stderr.read().splitlines()
        code = proc.wait()
        self.latest = {"error": ssh_error(err, code)}
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


# Used when a sensor reports no limit of its own: Intel / AMD CPUs throttle around 85-100°C, NVIDIA GPUs
# around 85-90°C, NVMe drives around 70-80°C and SATA disks are rated for 60°C or so.
TEMP_FALLBACK = {"cpu": 85.0, "gpu": 85.0, "disk": 65.0, "other": 80.0}
TEMP_CFG = {"temp_high": {}, "temp_warn_margin": 10.0}  # set from the config in main()


def sensors(r):
    """Every temperature of a host result, with a display name, its limit and a level: ok / warm / hot."""
    raw = list(r.get("temps") or [])
    for g in r.get("gpus") or []:
        if g.get("temp") is not None:
            raw.append({"temp": g["temp"], "label": g["name"], "high": None, "crit": None, "chip": "nvidia",
                        "kind": "gpu", "device": "GPU%d" % g["index"]})
    counts = {}
    for s in raw:
        counts[s["kind"]] = counts.get(s["kind"], 0) + 1
    out, seen = [], {}
    for s in raw:
        kind = s["kind"]
        if kind in ("cpu", "gpu"):
            name = s["device"] or kind.upper()
            if counts[kind] > 1 and not s["device"]:  # one chip per socket: CPU0, CPU1
                name += str(seen.get(kind, 0))
        else:
            name = s["device"] or s["chip"]
        seen[kind] = seen.get(kind, 0) + 1
        override = TEMP_CFG["temp_high"]
        high = override.get(s["chip"], override.get(kind)) or s.get("high") or TEMP_FALLBACK.get(kind, 80.0)
        t = s["temp"]
        level = "hot" if t >= high else "warm" if t >= high - TEMP_CFG["temp_warn_margin"] else "ok"
        out.append(dict(s, name=name, limit=high, level=level))
    return out


LEVEL_PCT = {"ok": 0, "warm": 75, "hot": 100}


def temp_text(s, limit=False):
    text = "%s %d°C" % (s["name"], round(s["temp"]))
    if limit or s["level"] != "ok":
        text += "/%d" % round(s["limit"])
    return c(text, LEVEL_PCT[s["level"]])


def render_temps(r):
    ss = sensors(r)
    if not ss:
        return []
    order = {"cpu": 0, "gpu": 1, "disk": 2, "other": 3}
    ss.sort(key=lambda s: (order.get(s["kind"], 9), s["name"]))
    return ["  TEMP " + "  ".join(temp_text(s) for s in ss)]


def alarms(results):
    """[(host, sensor)] for every warm or hot sensor, hottest relative to its limit first."""
    out = [(r["node"], s) for r in results if "error" not in r and "pending" not in r
           for s in sensors(r) if s["level"] != "ok"]
    return sorted(out, key=lambda x: x[1]["temp"] - x[1]["limit"], reverse=True)


def render_alarms(results):
    al = alarms(results)
    if not al:
        return []
    hot = sum(1 for _, s in al if s["level"] == "hot")
    head = c("HOT", 100) if hot else c("WARM", 75)
    return ["%s  %s" % (head, "  ".join("%s %s" % (host, temp_text(s, True)) for host, s in al)), ""]


def run_alert(cmd, host, s):
    """Run the --alert command for a sensor that just reached its limit; details in TAILMON_* variables."""
    env = dict(os.environ, TAILMON_HOST=host, TAILMON_SENSOR=s["name"], TAILMON_CHIP=s["chip"],
               TAILMON_KIND=s["kind"], TAILMON_TEMP="%.1f" % s["temp"], TAILMON_LIMIT="%.1f" % s["limit"],
               TAILMON_LEVEL=s["level"])
    try:
        subprocess.Popen(cmd, shell=True, env=env, stdin=subprocess.DEVNULL)
    except OSError as e:
        print("tailmon: --alert: %s" % e, file=sys.stderr)


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
    lines = [time.strftime("tailmon  %Y-%m-%d %H:%M:%S"), ""] + render_alarms(results)
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
            lines.append("  GPU%d %s %s  mem %s / %s  %s" % (g["index"], bar(g["util"] or 0), c(util, g["util"]),
                                                              hb(g["mem_used"]), hb(g["mem_total"]), g["name"]))
        lines.append("  IO   disk r %-9s w %-9s  net rx %-9s tx %s" % (
            hb(r["disk_io"]["read"], True), hb(r["disk_io"]["write"], True),
            hb(r["net_io"]["rx"], True), hb(r["net_io"]["tx"], True)))
        fss = list(r["fs"])
        if len(fss) > 1:
            fss.append(fs_total(fss, "total"))
        lines += [hdd_line(fs) for fs in fss]
        lines += render_temps(r)
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
    streams, logged, alerted = {}, {}, set()
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
            hot = {(host, sn["name"]): sn for host, sn in alarms(results) if sn["level"] == "hot"}
            if a.alert:
                for key in set(hot) - alerted:  # once per sensor each time it reaches its limit
                    run_alert(a.alert, key[0], hot[key])
            alerted = set(hot)
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
        parser.add_argument("--exclude", metavar="LIST",
                            help="comma-separated Tailscale hostnames not to search, e.g. a host whose "
                                 "huge disks would make everyone wait")
        parser.add_argument("--max-results", type=int, default=1000, metavar="N",
                            help="maximum results across all hosts")
        parser.add_argument("--hidden", action="store_true", help="include hidden files and directories")
        parser.add_argument("--proc", action="store_true",
                            help="also walk into /proc (skipped by default when searching above it)")
        parser.add_argument("--boom", action="store_true",
                            help="disable the loop guard: walk into sshfs mounts (e.g. /media/tailscale, "
                                 "where peers mount each other) and directories already visited")
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
    search.add_argument("-i", "--ignore-case", action="store_true", help="match the --name glob case-insensitively")

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
    exclude = set(x for x in a.exclude.split(",") if x) if a.exclude else set()
    pattern = a.name if a.operation == "search" else a.pattern
    rows, errors, skipped, stats = file_tool(cfg, only, a.operation, a.path, pattern,
                                              a.max_results, a.hidden,
                                              getattr(a, "ignore_case", False),
                                              getattr(a, "fixed_strings", False),
                                              a.verbose, a.timeout, a.proc, a.boom, exclude)
    render_file_results(a.operation, rows, errors, skipped, stats, a.json, a.verbose)


# ---------------------------------------------------------------------------------------------
# Volume: read or change the default output's volume on each host (PipeWire, PulseAudio or ALSA),
# from the command line (`tailmon volume`) or a web page for the whole network (`tailmon serve`).
# ---------------------------------------------------------------------------------------------
VOLUME = r'''
import json, os, re, shutil, subprocess, sys
ACTION = sys.argv[1] if len(sys.argv) > 1 else "get"  # get | set | up | down | mute | unmute | toggle
VALUE = int(sys.argv[2]) if len(sys.argv) > 2 else 0
# A non-interactive ssh session may lack the variables that lead to the user's sound server.
os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid())
bus = os.path.join(os.environ["XDG_RUNTIME_DIR"], "bus")
if "DBUS_SESSION_BUS_ADDRESS" not in os.environ and os.path.exists(bus):
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=" + bus

def out(*cmd):
    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=10)
    if r.returncode != 0:
        raise RuntimeError((r.stderr.strip().splitlines() or ["%s: exit %d" % (cmd[0], r.returncode)])[-1])
    return r.stdout

class Wpctl:
    name, sink = "pipewire", "@DEFAULT_AUDIO_SINK@"
    def get(self):
        s = out("wpctl", "get-volume", self.sink)  # "Volume: 0.40 [MUTED]"
        if not s.startswith("Volume:"):
            raise RuntimeError("no default output")
        return int(round(float(s.split()[1]) * 100)), "MUTED" in s
    def set(self, pct): out("wpctl", "set-volume", self.sink, "%.2f" % (pct / 100.0))
    def mute(self, how): out("wpctl", "set-mute", self.sink, {"mute": "1", "unmute": "0", "toggle": "toggle"}[how])

class Pactl:
    name, sink = "pulseaudio", "@DEFAULT_SINK@"
    def get(self):
        vol = re.findall(r"(\d+)%", out("pactl", "get-sink-volume", self.sink))
        return int(vol[0]) if vol else 0, "yes" in out("pactl", "get-sink-mute", self.sink)
    def set(self, pct): out("pactl", "set-sink-volume", self.sink, "%d%%" % pct)
    def mute(self, how): out("pactl", "set-sink-mute", self.sink, {"mute": "1", "unmute": "0", "toggle": "toggle"}[how])

class Amixer:
    name = "alsa"
    def get(self):
        s = out("amixer", "-M", "get", "Master")  # "... [50%] [on]"
        vol = re.findall(r"\[(\d+)%\]", s)
        return int(vol[0]) if vol else 0, "[off]" in s
    def set(self, pct): out("amixer", "-q", "-M", "set", "Master", "%d%%" % pct)
    def mute(self, how): out("amixer", "-q", "set", "Master", how)

class Termux:
    # Android with Termux and Termux:API: the media ("music") stream. Android has no mute for it, so
    # mute saves the level in a file and sets 0; unmute (or any new level) restores / forgets it.
    name, saved = "android", os.path.expanduser("~/.tailmon-muted-volume")
    def music(self):
        for s in json.loads(out("termux-volume")):
            if s["stream"] == "music":
                return s
        raise RuntimeError("no music stream")
    def raw(self, steps):
        out("termux-volume", "music", str(int(steps)))
    def get(self):
        s = self.music()
        pct = int(round(100.0 * s["volume"] / (s["max_volume"] or 1)))
        return pct, pct == 0 and os.path.exists(self.saved)
    def set(self, pct):
        if os.path.exists(self.saved):
            os.remove(self.saved)
        s = self.music()
        steps = int(round(pct * s["max_volume"] / 100.0))
        now = int(round(100.0 * s["volume"] / (s["max_volume"] or 1)))
        if steps == s["volume"] and pct != now:  # 15 steps: a small +3 / -3 still moves one
            steps = max(0, min(s["max_volume"], steps + (1 if pct > now else -1)))
        self.raw(steps)
    def mute(self, how):
        muted = self.get()[1]
        if how == "toggle":
            how = "unmute" if muted else "mute"
        if how == "mute" and not muted:
            with open(self.saved, "w") as f:
                f.write(str(self.music()["volume"]))
            self.raw(0)
        elif how == "unmute" and muted:
            with open(self.saved) as f:
                steps = int(f.read() or 0)
            os.remove(self.saved)
            self.raw(steps)

class Osascript:
    # macOS: AppleScript's volume settings ("output volume:40, input volume:75, alert volume:100, output muted:false").
    name = "macos"
    def get(self):
        s = dict(kv.strip().split(":", 1) for kv in out("osascript", "-e", "get volume settings").split(","))
        if not s.get("output volume", "").strip().isdigit():
            raise RuntimeError("the output device has no volume control")
        return int(s["output volume"]), s.get("output muted", "").strip() == "true"
    def set(self, pct): out("osascript", "-e", "set volume output volume %d" % pct)
    def mute(self, how):
        muted = {"mute": True, "unmute": False, "toggle": not self.get()[1]}[how]
        out("osascript", "-e", "set volume output muted %s" % ("true" if muted else "false"))

errors = []
for backend in (Termux, Osascript, Wpctl, Pactl, Amixer):
    tool = {"android": "termux-volume", "macos": "osascript", "pipewire": "wpctl", "pulseaudio": "pactl",
            "alsa": "amixer"}[backend.name]
    if not shutil.which(tool):
        continue
    b = backend()
    try:
        vol, muted = b.get()  # also checks that the sound server answers before changing anything
        if ACTION in ("set", "up", "down"):
            target = VALUE if ACTION == "set" else vol + (VALUE if ACTION == "up" else -VALUE)
            b.set(max(0, min(100, target)))
        elif ACTION in ("mute", "unmute", "toggle"):
            b.mute(ACTION)
        if ACTION != "get":
            vol, muted = b.get()
        print(json.dumps({"volume": vol, "muted": muted, "backend": b.name}))
        sys.exit(0)
    except Exception as e:
        errors.append("%s: %s" % (tool, e))
print(json.dumps({"error": "; ".join(errors) or "no wpctl, pactl, amixer, osascript or termux-volume found"}))
'''

# Windows: PowerShell drives the default playback device through the Core Audio API (IAudioEndpointVolume).
VOLUME_WINDOWS = r'''
$ErrorActionPreference = "Stop"
try {
Add-Type -TypeDefinition @"
using System; using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
  int f(); int g(); int h(); int i();
  int SetMasterVolumeLevelScalar(float level, Guid ctx);
  int j();
  int GetMasterVolumeLevelScalar(out float level);
  int k(); int l(); int m(); int n();
  int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, Guid ctx);
  int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice { int Activate(ref Guid id, int ctx, IntPtr p, out IAudioEndpointVolume v); }
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator { int f(); int GetDefaultAudioEndpoint(int flow, int role, out IMMDevice d); }
[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumerator { }
public static class TailmonAudio {
  static IAudioEndpointVolume V() {
    IMMDevice d; IAudioEndpointVolume v; Guid id = typeof(IAudioEndpointVolume).GUID;
    Marshal.ThrowExceptionForHR(((IMMDeviceEnumerator)new MMDeviceEnumerator()).GetDefaultAudioEndpoint(0, 1, out d));
    Marshal.ThrowExceptionForHR(d.Activate(ref id, 23, IntPtr.Zero, out v));
    return v;
  }
  public static int Get() { float l; Marshal.ThrowExceptionForHR(V().GetMasterVolumeLevelScalar(out l)); return (int)Math.Round(l * 100); }
  public static void Set(int pct) { Marshal.ThrowExceptionForHR(V().SetMasterVolumeLevelScalar(Math.Max(0, Math.Min(100, pct)) / 100f, Guid.Empty)); }
  public static bool Muted() { bool m; Marshal.ThrowExceptionForHR(V().GetMute(out m)); return m; }
  public static void Mute(bool m) { Marshal.ThrowExceptionForHR(V().SetMute(m, Guid.Empty)); }
}
"@
switch ($Action) {
  "set" { [TailmonAudio]::Set($Value) }
  "up" { [TailmonAudio]::Set([TailmonAudio]::Get() + $Value) }
  "down" { [TailmonAudio]::Set([TailmonAudio]::Get() - $Value) }
  "mute" { [TailmonAudio]::Mute($true) }
  "unmute" { [TailmonAudio]::Mute($false) }
  "toggle" { [TailmonAudio]::Mute(-not [TailmonAudio]::Muted()) }
}
@{volume = [TailmonAudio]::Get(); muted = [TailmonAudio]::Muted(); backend = "windows"} | ConvertTo-Json -Compress
} catch { @{error = $_.Exception.Message} | ConvertTo-Json -Compress }
'''
# Small, so it fits any remote shell's command line (cmd.exe or PowerShell): it runs the script sent on stdin.
WINDOWS_BOOTSTRAP = base64.b64encode("Invoke-Expression ([Console]::In.ReadToEnd())".encode("utf-16-le")).decode()

VOLUME_OSES = ("linux", "macOS", "windows", "android")

VOLUME_ACTIONS = ("get", "set", "up", "down", "mute", "unmute", "toggle")


def volume_cmd(node, cfg, is_self, action, value):
    if is_self:
        return [sys.executable, "-", action, str(int(value))]
    if node["os"] == "windows":
        return ssh_base(node, cfg) + ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                                      WINDOWS_BOOTSTRAP]
    return python_cmd(node, cfg, [action, int(value)])


def volume(node, cfg, is_self, action="get", value=0):
    script = VOLUME
    if node["os"] == "windows" and not is_self:
        script = "$Action = '%s'; $Value = %d\n%s" % (action, int(value), VOLUME_WINDOWS)
    try:
        r = subprocess.run(volume_cmd(node, cfg, is_self, action, value), input=script, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
    except subprocess.TimeoutExpired:
        return {"host": node["name"], "error": "timeout"}
    try:
        return dict(json.loads(r.stdout.strip().splitlines()[-1]), host=node["name"])
    except (ValueError, IndexError):
        return {"host": node["name"], "error": ssh_error(r.stderr.splitlines(), r.returncode, 160)}


def volume_all(cfg, only=None, exclude=(), action="get", value=0):
    targets, _ = targets_for(cfg, only, exclude, VOLUME_OSES)
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        futs = [ex.submit(volume, n, cfg, s, action, value) for n, s in targets]
        return sorted((f.result() for f in futs), key=lambda r: r["host"])


def parse_volume_action(text):
    """'50' -> set 50, '+5' -> up 5, '-5' -> down 5, 'mute' / 'unmute' / 'toggle' / 'get'."""
    if text in VOLUME_ACTIONS and text not in ("set", "up", "down"):
        return text, 0
    if re.fullmatch(r"[+-]?\d{1,3}%?", text):
        n = int(text.strip("+-%"))
        return ("up" if text[0] == "+" else "down" if text[0] == "-" else "set"), n
    raise ValueError("expected a level (50), a step (+5, -5), mute, unmute or toggle, not %r" % text)


def volume_main(argv):
    ap = argparse.ArgumentParser(prog="tailmon volume",
                                 description="Show or change the sound volume of the Tailscale hosts")
    ap.add_argument("action", nargs="?", default="get",
                    help="50 (set to 50%%), +5 / -5 (step), mute, unmute or toggle; nothing: show")
    ap.add_argument("--hosts", help="comma-separated Tailscale hostnames")
    ap.add_argument("--exclude", metavar="LIST", help="comma-separated Tailscale hostnames to leave alone")
    ap.add_argument("--json", action="store_true", help="print machine-readable JSON")
    a = ap.parse_args(argv[1:])
    try:
        action, value = parse_volume_action(a.action)
    except ValueError as e:
        ap.error(str(e))
    only = set(x for x in a.hosts.split(",") if x) if a.hosts else None
    exclude = set(x for x in a.exclude.split(",") if x) if a.exclude else set()
    rows = volume_all(load_config(), only, exclude, action, value)
    if a.json:
        print(json.dumps(rows, indent=2))
        return
    for r in rows:
        if "error" in r:
            print("%-16s %s" % (r["host"], c("error: " + r["error"], 100)))
        else:
            print("%-16s %s %3d%%  %-6s %s" % (r["host"], bar(0 if r["muted"] else r["volume"]), r["volume"],
                                              "muted" if r["muted"] else "", r["backend"]))


VOLUME_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tailnet Volume</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --fg:#1d2330; --muted:#6b7385; --accent:#3b6fe0; --line:#e1e4ea; --bad:#c43c3c; }
@media (prefers-color-scheme: dark) { :root { --bg:#14171d; --card:#1e222a; --fg:#e6e9ef; --muted:#8d95a6;
  --accent:#6d97f5; --line:#2c313b; --bad:#ef6b6b; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:15px/1.4 system-ui,sans-serif; }
main { max-width:640px; margin:0 auto; padding:20px 16px; }
h1 { font-size:20px; margin:0 0 4px; } .sub { color:var(--muted); margin:0 0 18px; font-size:13px; }
.host { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:12px 14px; margin-bottom:10px; }
.top { display:flex; align-items:center; gap:10px; } .name { font-weight:600; flex:1; overflow:hidden; text-overflow:ellipsis; }
.pct { font-variant-numeric:tabular-nums; width:3.5em; text-align:right; color:var(--muted); }
.row { display:flex; align-items:center; gap:10px; margin-top:8px; }
input[type=range] { flex:1; accent-color:var(--accent); min-width:0; }
button { border:1px solid var(--line); background:var(--bg); color:var(--fg); border-radius:8px; padding:6px 10px; font:inherit; cursor:pointer; }
button.on { background:var(--bad); border-color:var(--bad); color:#fff; }
.err { color:var(--bad); font-size:13px; margin-top:6px; } .backend { color:var(--muted); font-size:12px; }
.muted input[type=range] { opacity:.4; }
</style></head><body><main>
<h1>Volume</h1><p class="sub">Every online Linux host on the tailnet · <a href="#" id="reload">refresh</a></p>
<div id="hosts">Loading…</div>
</main><script>
const el = document.getElementById("hosts");
async function api(path, body) {
  const r = await fetch(path + location.search, body ? {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)} : {});
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
function card(h) {
  const d = document.createElement("div"); d.className = "host"; d.dataset.host = h.host;
  fill(d, h); return d;
}
function fill(d, h) {
  d.innerHTML = "";
  d.classList.toggle("muted", !!h.muted);
  const top = document.createElement("div"); top.className = "top";
  const name = document.createElement("span"); name.className = "name"; name.textContent = h.host;
  const be = document.createElement("span"); be.className = "backend"; be.textContent = h.backend || "";
  top.append(name, be); d.append(top);
  if (h.error) { const e = document.createElement("div"); e.className = "err"; e.textContent = h.error; d.append(e); return; }
  const row = document.createElement("div"); row.className = "row";
  const mute = document.createElement("button"); mute.textContent = h.muted ? "Muted" : "Mute"; mute.className = h.muted ? "on" : "";
  const range = document.createElement("input"); range.type = "range"; range.min = 0; range.max = 100; range.value = h.volume;
  const pct = document.createElement("span"); pct.className = "pct"; pct.textContent = h.volume + "%";
  range.oninput = () => pct.textContent = range.value + "%";
  range.onchange = () => send(d, {action:"set", value:+range.value});
  mute.onclick = () => send(d, {action:"toggle"});
  row.append(mute, range, pct); d.append(row);
}
async function send(d, body) {
  try { fill(d, await api("/api/volume/" + encodeURIComponent(d.dataset.host), body)); }
  catch (e) { fill(d, {host:d.dataset.host, error:String(e.message || e)}); }
}
async function load() {
  try { const hs = await api("/api/volume"); el.innerHTML = ""; hs.forEach(h => el.append(card(h)));
        if (!hs.length) el.textContent = "No hosts."; }
  catch (e) { el.textContent = "Error: " + (e.message || e); }
}
document.getElementById("reload").onclick = e => { e.preventDefault(); load(); };
load();
</script></body></html>
'''


def serve_main(argv):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import unquote, urlparse, parse_qs
    ap = argparse.ArgumentParser(prog="tailmon serve",
                                 description="Web page and JSON API to control the volume of every host")
    ap.add_argument("--bind", default="0.0.0.0", metavar="ADDR",
                    help="address to listen on (default: all interfaces; 127.0.0.1: this PC only, "
                         "or this PC's Tailscale IP: the tailnet only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", help="require ?token=TOKEN (or an X-Token header) on every request")
    ap.add_argument("--hosts", help="comma-separated Tailscale hostnames to offer")
    ap.add_argument("--exclude", metavar="LIST", help="comma-separated Tailscale hostnames to leave out")
    a = ap.parse_args(argv[1:])
    cfg = load_config()
    only = set(x for x in a.hosts.split(",") if x) if a.hosts else None
    exclude = set(x for x in a.exclude.split(",") if x) if a.exclude else set()

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body, ctype="application/json"):
            data = (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def allowed(self):
            if not a.token:
                return True
            q = parse_qs(urlparse(self.path).query)
            if a.token in q.get("token", []) or self.headers.get("X-Token") == a.token:
                return True
            self.reply(403, {"error": "forbidden: add ?token=..."})
            return False

        def do_GET(self):
            path = urlparse(self.path).path
            if not self.allowed():
                return
            if path == "/":
                return self.reply(200, VOLUME_PAGE, "text/html")
            if path == "/api/volume":
                return self.reply(200, volume_all(cfg, only, exclude))
            self.reply(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            if not self.allowed():
                return
            if not path.startswith("/api/volume/"):
                return self.reply(404, {"error": "not found"})
            host = unquote(path[len("/api/volume/"):])
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                action, value = body.get("action", "get"), int(body.get("value", 0))
                if action not in VOLUME_ACTIONS or not 0 <= value <= 100:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                return self.reply(400, {"error": "expected {\"action\": one of %s, \"value\": 0-100}"
                                                 % ", ".join(VOLUME_ACTIONS)})
            if only and host not in only or host in exclude:
                return self.reply(404, {"error": "unknown host: %s" % host})
            targets, _ = targets_for(cfg, None, exclude, VOLUME_OSES)
            match = [t for t in targets if t[0]["name"] == host]
            if not match:
                return self.reply(404, {"error": "unknown or offline host: %s" % host})
            self.reply(200, volume(match[0][0], cfg, match[0][1], action, value))

        def log_message(self, fmt, *args):
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    server = ThreadingHTTPServer((a.bind, a.port), Handler)
    shown = "localhost" if a.bind in ("0.0.0.0", "::") else a.bind
    print("tailmon: volume control on http://%s:%d/%s" % (shown, a.port, "?token=" + a.token if a.token else ""),
          file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        server.server_close()


# ---------------------------------------------------------------------------------------------
# Gate: each host approves the scripts tailmon may run on it. tailmon gets its own SSH key, which the
# host's authorized_keys ties to a forced command (the gate); the gate runs a script sent on stdin only
# if its SHA-256 is in the host's approved list, so that key can do nothing else (no shell, no
# forwarding, no other code). Linux, macOS and Android (Termux); Windows hosts keep the normal key.
# ---------------------------------------------------------------------------------------------
GATE_OSES = ("linux", "macOS", "android")
GATE_MARK = "tailmon-gate"

GATE = r'''"""tailmon gate: the forced command of tailmon's SSH key on this machine (see ~/.ssh/authorized_keys).
It runs a tailmon script only if its SHA-256 is listed in ~/.config/tailmon/approved, so that key
can't run anything else. Every run and refusal goes to ~/.config/tailmon/gate.log.

On this machine (with the python3 of the authorized_keys line):
    python3 ~/.local/share/tailmon/gate.py list               approved scripts and the latest log
    python3 ~/.local/share/tailmon/gate.py approve HASH [NAME]
    python3 ~/.local/share/tailmon/gate.py revoke HASH
To stop the gate altogether, delete the authorized_keys line that ends in "tailmon-gate".
"""
import hashlib, json, os, shlex, sys, time

CONF = os.path.join(os.path.expanduser("~"), ".config", "tailmon")
APPROVED = os.path.join(CONF, "approved")
LOG = os.path.join(CONF, "gate.log")
MAX_SCRIPT = 4 << 20

def load():
    out = {}
    try:
        with open(APPROVED) as f:
            for line in f:
                p = line.split(None, 1)
                if p and len(p[0]) == 64 and all(c in "0123456789abcdef" for c in p[0].lower()):
                    out[p[0].lower()] = p[1].strip() if len(p) > 1 else ""
    except OSError:
        pass
    return out

def save(approved):
    if not os.path.isdir(CONF):
        os.makedirs(CONF, 0o700)
    tmp = APPROVED + ".tmp"
    with open(tmp, "w") as f:
        f.write("# SHA-256 and name of each tailmon script this machine runs for tailmon's key\n")
        for h, name in sorted(approved.items(), key=lambda x: x[1]):
            f.write("%s %s\n" % (h, name))
    os.chmod(tmp, 0o600)
    os.rename(tmp, APPROVED)

def log(verdict, detail):
    try:
        if not os.path.isdir(CONF):
            os.makedirs(CONF, 0o700)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 1 << 20:
            os.rename(LOG, LOG + ".1")
        client = (os.environ.get("SSH_CONNECTION") or "local").split()[0]
        with open(LOG, "a") as f:
            f.write("%s %s %s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), client, verdict, detail))
    except OSError:
        pass

def refuse(msg):
    log("refused", msg)
    sys.stderr.write("tailmon-gate: %s\n" % msg)
    sys.exit(77)

def owner(argv):
    """Commands for the machine's owner, run by hand (not reachable through the key)."""
    approved = load()
    if argv[:1] == ["approve"] and len(argv) >= 2 and len(argv[1]) == 64:
        approved[argv[1].lower()] = " ".join(argv[2:]) or "approved by hand"
        save(approved)
        log("approved", argv[1][:12])
        print("approved %s" % argv[1])
    elif argv[:1] == ["revoke"] and len(argv) == 2:
        if approved.pop(argv[1].lower(), None) is None:
            sys.exit("not approved: %s" % argv[1])
        save(approved)
        log("revoked", argv[1][:12])
        print("revoked %s" % argv[1])
    elif argv in ([], ["list"]):
        print("approved (%s):" % APPROVED)
        for h, name in sorted(approved.items(), key=lambda x: x[1]):
            print("  %s  %s" % (h, name))
        try:
            with open(LOG) as f:
                lines = f.readlines()[-15:]
            print("latest of %s:" % LOG)
            sys.stdout.write("".join("  " + l for l in lines))
        except OSError:
            pass
    else:
        sys.exit(__doc__)


def main():
    cmd = os.environ.get("SSH_ORIGINAL_COMMAND")
    if cmd is None:
        return owner(sys.argv[1:])
    try:
        words = shlex.split(cmd)
    except ValueError:
        refuse("unreadable command")
    if words[:1] != ["tailmon-gate"]:
        refuse("this key only runs approved tailmon scripts, not %r" % cmd[:80])
    words = words[1:]
    if words[:1] == ["check"]:  # which of these hashes are approved: read-only, for `tailmon gate status`
        approved = load()
        print(json.dumps({"approved": [h for h in words[1:] if h.lower() in approved],
                          "python": sys.executable, "gate": os.path.abspath(__file__)}))
        return
    if words[:2] != ["python3", "-"]:
        refuse("unknown request %r" % cmd[:80])
    script = sys.stdin.buffer.read(MAX_SCRIPT + 1)
    if len(script) > MAX_SCRIPT:
        refuse("script over %d bytes" % MAX_SCRIPT)
    h = hashlib.sha256(script).hexdigest()
    approved = load()
    if h not in approved:
        refuse("script %s is not approved on this host: `tailmon gate approve`, or here: %s %s approve %s"
               % (h[:12], sys.executable, os.path.abspath(__file__), h))
    log("ran", "%s %s" % (approved[h], h[:12]))
    sys.argv = ["-"] + words[2:]
    sys.stdin = open(os.devnull)
    exec(compile(script, "<tailmon %s>" % approved[h], "exec"), {"__name__": "__main__"})

main()
'''

# Runs over your normal SSH login (whatever ~/.ssh/config gives) to set up, update or remove the gate.
# DATA = {"mode": install | approve | remove, "gate": GATE, "python": ..., "pubkey": ..., "approved": {hash: name}}
GATE_INSTALLER = r'''
import json, os, platform, shlex, shutil, sys
home = os.path.expanduser("~")
share, conf, ssh_dir = (os.path.join(home, *p) for p in ((".local", "share", "tailmon"), (".config", "tailmon"), (".ssh",)))
gate, approved_path, keys = os.path.join(share, "gate.py"), os.path.join(conf, "approved"), os.path.join(ssh_dir, "authorized_keys")
blob = DATA["pubkey"].split()[1] if DATA.get("pubkey") else None
result = {"system": platform.system(), "gate": gate}

def makedirs(d):
    if not os.path.isdir(d):
        os.makedirs(d, 0o700)

def write(path, text, mode):
    """Replace a file at once; a symlinked one (dotfiles managers) is rewritten in place."""
    makedirs(os.path.dirname(path))
    if os.path.islink(path):
        with open(path, "w") as f:
            f.write(text)
        return
    tmp = path + ".tailmon-tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.rename(tmp, path)

def read_approved():
    out = {}
    try:
        with open(approved_path) as f:
            for line in f:
                p = line.split(None, 1)
                if p and len(p[0]) == 64 and not p[0].startswith("#"):
                    out[p[0].lower()] = p[1].strip() if len(p) > 1 else ""
    except OSError:
        pass
    return out

def write_approved(d):
    write(approved_path, "# SHA-256 and name of each tailmon script this machine runs for tailmon's key\n" +
          "".join("%s %s\n" % (h, n) for h, n in sorted(d.items(), key=lambda x: x[1])), 0o600)

def set_key_line(line):
    """Drop any earlier line with tailmon's key, add `line` (None: just drop). Mode and other keys kept."""
    try:
        with open(keys) as f:
            old = f.read()
        mode = os.stat(keys).st_mode & 0o777
    except OSError:
        old, mode = "", 0o600
    lines = [l for l in old.splitlines() if blob not in l.split()]
    if line:
        lines.append(line)
    if lines == old.splitlines():
        return
    makedirs(ssh_dir)
    backup = keys + ".before-tailmon"
    if old and not os.path.exists(backup):
        shutil.copy2(keys, backup)
        result["backup"] = backup
    # sshd ignores authorized_keys if others can write it (StrictModes, also on macOS)
    write(keys, "\n".join(lines) + "\n" if lines else "", mode & 0o755)

mode = DATA["mode"]
if mode == "install":
    write(gate, DATA["gate"], 0o644)
    d = read_approved()
    d.update(DATA["approved"])
    write_approved(d)
    command = "%s %s" % (shlex.quote(DATA["python"]), shlex.quote(gate))
    set_key_line('restrict,command="%s" %s' % (command.replace("\\", "\\\\").replace('"', '\\"'), DATA["pubkey"]))
elif mode == "approve":
    if not os.path.exists(gate):
        raise SystemExit("no gate on this host: tailmon gate install")
    d = read_approved()
    result["new"] = sorted(n for h, n in DATA["approved"].items() if h not in d)
    d.update(DATA["approved"])
    write_approved(d)
elif mode == "remove":
    set_key_line(None)
    for path in (gate, approved_path):
        if os.path.exists(path):
            os.remove(path)
print(json.dumps(result))
'''


def gate_scripts():
    """{sha256: name} of the scripts this tailmon sends over a gate, hashed as they go down the pipe."""
    enc = locale.getpreferredencoding(False)
    return {hashlib.sha256(text.encode(enc)).hexdigest(): "%s %s" % (name, time.strftime("%Y-%m-%d"))
            for name, text in (("probe", PROBE), ("file-tool", FILE_TOOL), ("volume", VOLUME))}


def gate_names():
    return {h: n.split()[0] for h, n in gate_scripts().items()}


def save_config_keys(**changes):
    """Update a few keys of ~/.config/tailmon.json, keeping everything else in it as it is."""
    raw = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            raw = json.load(f)
    raw.update(changes)
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(raw, f, indent=2)
        f.write("\n")
    os.chmod(tmp, os.stat(CONFIG_PATH).st_mode & 0o777 if os.path.exists(CONFIG_PATH) else 0o600)
    os.replace(tmp, CONFIG_PATH)


def gate_key(cfg):
    """Path of tailmon's gate key, created (ed25519, no passphrase: tailmon runs unattended) if missing."""
    path = os.path.expanduser(cfg["gate_key"])
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                        "%s@%s" % (GATE_MARK, os.uname().nodename), "-f", path], check=True)
        print("tailmon: created the gate key %s" % path, file=sys.stderr)
    with open(path + ".pub") as f:
        return f.read().strip()


def remote_python(node, cfg):
    """Absolute path of python3 for the forced command, as the host's login shell finds it. sshd runs a
    forced command with a bare PATH: Homebrew (/opt/homebrew/bin) or Termux pythons wouldn't be found."""
    probe = 'exec "${SHELL:-sh}" -lc "command -v python3"'
    r = subprocess.run(ssh_base(node, cfg) + [probe], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True, timeout=30)
    paths = [l.strip() for l in r.stdout.splitlines() if l.strip().startswith("/")]
    if not paths:
        raise RuntimeError(ssh_error(r.stderr.splitlines(), r.returncode, 160) if r.returncode else "no python3")
    return paths[-1]


def gate_remote(node, cfg, data):
    """Run the installer on a host over the normal login; returns its JSON reply."""
    script = "import json\nDATA = json.loads(%r)\n%s" % (json.dumps(data), GATE_INSTALLER)
    py = data.get("python") or "python3"
    r = subprocess.run(ssh_base(node, cfg) + [shlex.quote(py), "-"], input=script, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
    if r.returncode != 0:
        err = ssh_error(r.stderr.splitlines(), r.returncode, 160)
        if "xcrun" in r.stderr or "developer tools" in r.stderr.lower():
            err = "python3 is the macOS stub: install the Command Line Tools (xcode-select --install) or Homebrew's python"
        raise RuntimeError(err)
    return json.loads(r.stdout.strip().splitlines()[-1])


def gate_check(node, cfg):
    """Ask a host's gate, with the gate key, which of this tailmon's scripts it approves."""
    hashes = gate_names()
    words = ["tailmon-gate", "check"] + sorted(hashes)
    r = subprocess.run(ssh_base(node, cfg, gate=True) + words, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(ssh_error(r.stderr.splitlines(), r.returncode, 160))
    try:
        reply = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise RuntimeError("the gate key opens a shell here, not the gate: tailmon gate install")
    ok = set(reply["approved"])
    return {"approved": sorted(n for h, n in hashes.items() if h in ok),
            "missing": sorted(n for h, n in hashes.items() if h not in ok), "python": reply.get("python")}


def gate_main(argv):
    ap = argparse.ArgumentParser(
        prog="tailmon gate", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Let each host approve the scripts tailmon runs on it. `install` gives tailmon its own SSH "
                    "key and ties it, in the host's authorized_keys, to a gate that only runs scripts whose "
                    "SHA-256 the host approved.",
        epilog="install   set up the gate on the hosts (over your normal SSH login) and approve this tailmon\n"
               "approve   approve this version of tailmon's scripts (after updating tailmon.py)\n"
               "status    ask every gate, with the gate key, whether it would run this tailmon\n"
               "remove    take the gate and tailmon's key off the hosts; tailmon uses your normal login again\n"
               "hashes    print the SHA-256 of this tailmon's scripts, to approve them by hand on a host")
    ap.add_argument("action", choices=("install", "approve", "status", "remove", "hashes"))
    ap.add_argument("--hosts", help="comma-separated Tailscale hostnames (default: every online Linux, macOS "
                                    "and Android host; status / approve / remove: the gated ones)")
    a = ap.parse_args(argv[1:])
    cfg = load_config()
    only = set(x for x in a.hosts.split(",") if x) if a.hosts else None

    if a.action == "hashes":
        for h, name in sorted(gate_names().items(), key=lambda x: x[1]):
            print("%s  %s" % (h, name))
        return
    gate_hosts = list(cfg.get("gate_hosts") or [])
    if a.action != "install" and not only:
        only = set(gate_hosts)
        if not only:
            sys.exit("tailmon: no host has a gate yet (tailmon gate install)")
    targets, skipped = targets_for(cfg, only, oses=GATE_OSES)
    targets = [n for n, is_self in targets if not is_self]  # this machine runs its scripts directly
    for p in skipped:
        if not only or p["name"] in only:
            why = "offline" if not p["online"] else "%s: no gate, keeps your normal login" % p["os"]
            print("%-16s skipped (%s)" % (p["name"], why), file=sys.stderr)
    if not targets:
        sys.exit("tailmon: no host to %s" % a.action)

    pubkey = gate_key(cfg) if a.action in ("install", "status") else None

    def one(node):
        if a.action == "install":
            py = remote_python(node, cfg)
            out = gate_remote(node, cfg, {"mode": "install", "gate": GATE, "python": py, "pubkey": pubkey,
                                          "approved": gate_scripts()})
            check = gate_check(node, cfg)  # prove that the gate key now reaches the gate
            return "gate on (%s, %s)%s" % (out["system"], py,
                                           "; backup " + out["backup"] if out.get("backup") else "") \
                if not check["missing"] else "installed, but not approved: " + ", ".join(check["missing"])
        if a.action == "approve":
            out = gate_remote(node, cfg, {"mode": "approve", "approved": gate_scripts(),
                                          "python": remote_python(node, cfg)})
            return "approved " + (", ".join(n.split()[0] for n in out["new"]) if out["new"] else "(already)")
        if a.action == "status":
            check = gate_check(node, cfg)
            return "gate: runs " + ", ".join(check["approved"]) + \
                ("" if not check["missing"] else "; NOT " + ", ".join(check["missing"]) + " (tailmon gate approve)")
        if a.action == "remove":
            with open(os.path.expanduser(cfg["gate_key"]) + ".pub") as f:
                key = f.read().strip()
            gate_remote(node, cfg, {"mode": "remove", "pubkey": key, "python": remote_python(node, cfg)})
            return "gate removed; tailmon uses your normal login"

    done = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        futs = [(n, ex.submit(one, n)) for n in targets]
        for n, f in sorted(futs, key=lambda x: x[0]["name"]):
            try:
                print("%-16s %s" % (n["name"], f.result()))
                done.append(n["name"])
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as e:
                print("%-16s %s" % (n["name"], c("ERROR: %s" % e, 100)), file=sys.stderr)
    if a.action == "install":
        save_config_keys(gate_hosts=sorted(set(gate_hosts) | set(done)))
    elif a.action == "remove":
        save_config_keys(gate_hosts=sorted(set(gate_hosts) - set(done)))
    if len(done) < len(targets):
        sys.exit(1)



# ---------------------------------------------------------------------------------------------
# tasks: which Todoist task each host ran, read from the comments `claude-explorer dispatch` posts
# ---------------------------------------------------------------------------------------------
TASKS_API = os.environ.get("TODOIST_API_URL", "https://api.todoist.com/api/v1")
TASK_START_RE = re.compile(r"^Agent started on (\S+?)(?: with account (\S+))? in (.+?)(?: \(folder created\))?"
                           r" \(session ([0-9a-f-]+)\)")
TASK_REPORT_RE = re.compile(r"^Agent on (\S+?), account (\S+?)(?:, restricted shell)?(?:, in .+? \((\w+), "
                            r"(\d+) turns, \$([\d.]+)\))?[:,]")
TASK_STATES = ("running", "done", "failed", "limit")


def todoist_token(cfg):
    """TODOIST_API_TOKEN, else todoist_token in tailmon's config, else in claude-explorer's."""
    token = os.environ.get("TODOIST_API_TOKEN") or cfg.get("todoist_token")
    if not token:
        try:
            with open(os.path.expanduser("~/.config/claude-explorer.json")) as f:
                token = json.load(f).get("todoist_token")
        except (OSError, ValueError):
            pass
    return token


def todoist_get(token, path):
    """GET only: `tasks` never changes anything in Todoist."""
    import urllib.error
    import urllib.request
    for wait in (2, 5, 15, None):
        req = urllib.request.Request(TASKS_API + path, headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code not in (408, 429, 500, 502, 503, 504) or wait is None:
                raise RuntimeError("Todoist: GET %s: HTTP %d" % (path.split("?")[0], e.code))
        except (urllib.error.URLError, OSError) as e:
            if wait is None:
                raise RuntimeError("Todoist: %s" % e)
        time.sleep(wait)


def todoist_pages(token, path, key="results"):
    from urllib.parse import quote
    items, cursor = [], None
    while True:
        sep = "&" if "?" in path else "?"
        page = todoist_get(token, path + (sep + "cursor=" + quote(cursor) if cursor else ""))
        items += page.get(key) or []
        cursor = page.get("next_cursor")
        if not cursor:
            return items


def dispatched_tasks(token, label, days):
    """Open tasks with a <label>-running/-done/-failed label, plus the ones completed in the last `days` days."""
    from datetime import datetime, timedelta, timezone
    from urllib.parse import urlencode
    tasks = {}
    for state in ("running", "done", "failed"):
        for t in todoist_pages(token, "/tasks?label=" + label + "-" + state):
            tasks[t["id"]] = t
    now, since = datetime.now(timezone.utc), datetime.now(timezone.utc) - timedelta(days=days)
    while since < now:  # Todoist caps one completed-tasks query at about 3 months
        until = min(since + timedelta(days=80), now)
        q = urlencode({"since": since.strftime("%Y-%m-%dT%H:%M:%SZ"), "until": until.strftime("%Y-%m-%dT%H:%M:%SZ")})
        for t in todoist_pages(token, "/tasks/completed/by_completion_date?" + q, key="items"):
            if any(l.startswith(label + "-") for l in t.get("labels") or []):
                tasks.setdefault(t["id"], t)
        since = until
    return list(tasks.values())


def task_runs(token, task, label):
    """One row per agent run of this task, from its "Agent started on …" and "Agent on …" comments."""
    comments = sorted(todoist_pages(token, "/comments?task_id=" + task["id"]), key=lambda c: c["posted_at"])
    title = " ".join(w for w in task["content"].split() if not w.startswith("@")) or task["content"]
    runs = []
    for cm in comments:
        text = cm.get("content") or ""
        m = TASK_START_RE.match(text)
        if m:
            runs.append({"host": m.group(1), "account": m.group(2) or "default", "dir": m.group(3),
                         "session": m.group(4), "started": cm["posted_at"], "ended": None, "state": "running",
                         "cost": None, "task": title, "task_id": task["id"],
                         "completed": bool(task.get("completed_at") or task.get("checked"))})
            continue
        m = TASK_REPORT_RE.match(text)
        open_run = next((r for r in reversed(runs) if r["host"] == m.group(1) and r["ended"] is None), None) if m else None
        if open_run:
            open_run["ended"] = cm["posted_at"]
            open_run["state"] = ("failed" if "stopped without a result" in text[:200] else
                                 "limit" if "hit its usage limit" in text[:200] else "done")
            if m.group(5):
                open_run["cost"] = float(m.group(5))
    if runs and runs[-1]["state"] == "done" and label + "-failed" in (task.get("labels") or []):
        runs[-1]["state"] = "failed"  # the agent finished but reported an error
    return runs


def local_time(stamp):
    from datetime import datetime
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%m-%d %H:%M")
    except (ValueError, AttributeError):
        return "?"


def tasks_main(argv):
    ap = argparse.ArgumentParser(prog="tailmon tasks",
                                 description="Which Todoist tasks each host ran, from the comments that "
                                             "`claude-explorer dispatch` posts. Read-only: nothing is changed "
                                             "in Todoist or on the hosts.")
    ap.add_argument("-n", "--last", type=int, default=3, metavar="N", help="tasks shown per host (default 3)")
    ap.add_argument("--hosts", help="comma-separated Tailscale hostnames to restrict to")
    ap.add_argument("--days", type=int, default=60,
                    help="also look at tasks completed (ticked off) in the last DAYS days (default 60)")
    ap.add_argument("--label", default="claude", help="dispatch label (default claude: @claude-running, …)")
    ap.add_argument("--json", action="store_true", help="print the runs as JSON")
    a = ap.parse_args(argv[1:])
    cfg = load_config()
    token = todoist_token(cfg)
    if not token:
        sys.exit("tailmon tasks: set TODOIST_API_TOKEN, or \"todoist_token\" in %s" % CONFIG_PATH)
    only = set(h.lower() for h in a.hosts.split(",") if h) if a.hosts else None
    try:
        tasks = dispatched_tasks(token, a.label, a.days)
        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            runs = [r for rs in ex.map(lambda t: task_runs(token, t, a.label), tasks) for r in rs]
    except RuntimeError as e:
        sys.exit("tailmon tasks: %s" % e)
    by_host = {}
    for r in sorted(runs, key=lambda r: r["started"], reverse=True):
        if only is None or r["host"].lower() in only:
            by_host.setdefault(r["host"].lower(), []).append(r)
    hosts = set(by_host)
    try:  # also list tailnet hosts that ran nothing
        me, peers = discover()
        hosts |= {n["name"].lower() for n in [me] + peers if only is None or n["name"].lower() in only}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if a.json:
        print(json.dumps({h: by_host.get(h, [])[:a.last] for h in sorted(hosts)}, indent=2))
        return
    colors = {"running": 60, "done": 0, "failed": 100, "limit": 80}
    for h in sorted(hosts, key=lambda h: (h not in by_host, h)):
        rs = by_host.get(h, [])
        print("%s  %s" % (h, "%d run(s)" % len(rs) if rs else "no task"))
        for r in rs[:a.last]:
            cost = "$%.2f" % r["cost"] if r["cost"] is not None else ""
            print("  %s  %s  %-5s  %s%s" % (local_time(r["started"]), c("%-7s" % r["state"], colors[r["state"]]),
                                           cost, r["task"][:90], "  [closed]" if r["completed"] else ""))


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("search", "grep"):
        return file_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] in ("mount", "umount"):
        return mount_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "volume":
        return volume_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        return serve_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "gate":
        return gate_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "tasks":
        return tasks_main(sys.argv[1:])
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-w", "--watch", type=float, metavar="SEC", help="refresh every SEC seconds")
    ap.add_argument("--json", action="store_true", help="print a JSON snapshot and exit")
    ap.add_argument("--log", metavar="FILE", help="append each sample as JSON lines to FILE")
    ap.add_argument("--hosts", help="comma-separated Tailscale hostnames to restrict to")
    ap.add_argument("--quiet", action="store_true", help="don't draw the table (for cron/systemd, with --log or --alert)")
    ap.add_argument("--alert", metavar="CMD",
                    help="shell command run when a temperature reaches its limit (TAILMON_HOST, TAILMON_SENSOR, "
                         "TAILMON_TEMP, TAILMON_LIMIT, ... in its environment); a snapshot also exits with status 3")
    a = ap.parse_args()
    cfg = load_config()
    TEMP_CFG.update((k, cfg[k]) for k in TEMP_CFG)
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
    hot = [(host, sn) for host, sn in alarms(results) if sn["level"] == "hot"]
    for host, sn in hot:
        if a.alert:
            run_alert(a.alert, host, sn)
        if a.quiet or a.json:
            print("tailmon: HOT %s %s %.0f°C (limit %.0f°C)" % (host, sn["name"], sn["temp"], sn["limit"]),
                  file=sys.stderr)
    if hot:
        sys.exit(3)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
