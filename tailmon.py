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
    tailmon.py flows -w 2         live traffic between the hosts (also: tailmon.py -w 1 --flows)
    tailmon.py htop               web page with an htop of every host, on http://localhost:8766/
"""
import argparse
import base64
import concurrent.futures as cf
import hashlib
import json
import locale
import math
import shlex
import os
import re
import shutil
import socket
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
    # Traffic between the hosts (FLOWS section) in the monitor, as with --flows. Off by default: each probe
    # then also runs `ss` and `tailscale status` every sample.
    "flows": False,
    # Electricity cost next to each host: price of a kWh (all in: energy, distribution, taxes) and its
    # currency, and the power of hosts you measured yourself (e.g. with a plug meter), in W, which then
    # replaces tailmon's reading or estimate.
    "kwh_price": 6.0,
    "currency": "CZK",
    "power": {},
}

# ---------------------------------------------------------------------------------------------
# Power: what each machine draws, for its electricity cost. Pasted into the probe and the htop script.
# Measured when the machine can tell (a server's power meter, a laptop on battery), estimated otherwise.
# ---------------------------------------------------------------------------------------------
POWER = r'''
def sysfs(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default

# Estimate: a base for the chassis (board, fans, BMC, NICs), each CPU package's rated power (RAPL's PL1,
# readable without root) scaled by load, RAM, disks, the GPUs' own reading, then PSU losses.
POWER_BASE = {"laptop": 4.0, "desktop": 25.0, "server": 60.0, "mac": 4.0}
POWER_CHASSIS = {"8": "laptop", "9": "laptop", "10": "laptop", "11": "laptop", "14": "laptop", "30": "laptop",
                 "31": "laptop", "32": "laptop", "17": "server", "23": "server", "25": "server", "28": "server",
                 "29": "server"}  # SMBIOS chassis types
power_info = {}

def power_setup():
    import glob
    i = power_info
    i["kind"] = POWER_CHASSIS.get(sysfs("/sys/class/dmi/id/chassis_type"), "desktop")
    i["tdp"] = [int(sysfs(d + "/constraint_0_power_limit_uw", "0") or 0) / 1e6
                for d in sorted(glob.glob("/sys/class/powercap/intel-rapl:[0-9]"))]
    i["tdp"] = [t for t in i["tdp"] if t > 0] or [min(250.0, max(15.0, 4.0 * (os.cpu_count() or 1)))]
    i["hdd"] = i["ssd"] = 0
    for b in os.listdir("/sys/block") if os.path.isdir("/sys/block") else []:
        if b.startswith(("sd", "nvme", "hd")) and os.path.exists("/sys/block/%s/device" % b):
            i["hdd" if sysfs("/sys/block/%s/queue/rotational" % b) == "1" else "ssd"] += 1
    i["meter"] = None
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        if sysfs(h + "/name") == "power_meter":  # ACPI power meter: the server's own wall reading
            for f in ("power1_average", "power1_input"):
                if sysfs(h + "/" + f).isdigit():
                    i["meter"] = h + "/" + f
                    break
    i["batteries"] = [b for b in glob.glob("/sys/class/power_supply/*") if sysfs(b + "/type") == "Battery"]
    mem = sysfs("/proc/meminfo").split("\n")[0].split()
    i["ram_gb"] = int(mem[1]) / 2.0**20 if len(mem) > 1 and mem[1].isdigit() else 0.0

def power(util, gpu_watts=0.0, mac=False):
    # {"watts", "source": meter | battery | estimate, "parts"} at util % of all CPUs.
    if mac:
        cpu = 30.0 * max(0.0, min(1.0, util / 100.0)) ** 0.5  # Apple silicon: a few W idle, ~35 W flat out
        return {"watts": (POWER_BASE["mac"] + cpu) / 0.9, "source": "estimate", "kind": "mac",
                "parts": {"base": POWER_BASE["mac"], "cpu": cpu}}
    if not power_info:
        power_setup()
    i = power_info
    if i["meter"]:
        v = sysfs(i["meter"])
        if v.isdigit() and int(v) > 0:
            return {"watts": int(v) / 1e6, "source": "meter"}
    for b in i["batteries"]:
        if sysfs(b + "/status") == "Discharging":
            p, cur, volt = sysfs(b + "/power_now"), sysfs(b + "/current_now"), sysfs(b + "/voltage_now")
            w = int(p) / 1e6 if p.isdigit() else int(cur) * int(volt) / 1e12 if cur.isdigit() and volt.isdigit() else 0
            if w > 0:
                return {"watts": w, "source": "battery"}
    u = max(0.0, min(1.0, util / 100.0)) ** 0.5  # half the threads busy is most of the package power (turbo, HT)
    parts = {"base": POWER_BASE[i["kind"]], "cpu": sum(t * (0.15 + 0.85 * u) for t in i["tdp"]),
             "ram": 0.15 * i["ram_gb"], "disks": 6.0 * i["hdd"] + 2.0 * i["ssd"], "gpu": gpu_watts}
    return {"watts": sum(parts.values()) / 0.88, "source": "estimate", "kind": i["kind"], "parts": parts}
'''


# ---------------------------------------------------------------------------------------------
# Probe: runs on the target machine. Python >= 3.6, standard library only, reads /proc.
# ---------------------------------------------------------------------------------------------
PROBE = r'''
import json, os, pwd, socket, subprocess, sys, time
''' + POWER + r'''
SAMPLE = float(sys.argv[1]) if len(sys.argv) > 1 else 1.0
COUNT = int(sys.argv[2]) if len(sys.argv) > 2 else 1  # 0 = stream forever, one JSON line per SAMPLE seconds
FLOWS = len(sys.argv) > 3 and sys.argv[3] == "1"  # traffic per peer: tailscale counters + TCP sockets (ss)
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
        r = subprocess.run(["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                            "--format=csv,noheader,nounits"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           universal_newlines=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    out = []
    for line in r.stdout.strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        num = lambda s: float(s) if s.replace(".", "", 1).isdigit() else None
        out.append({"index": int(p[0]), "name": p[1], "util": num(p[2]),
                    "mem_used": (num(p[3]) or 0) * 2**20, "mem_total": (num(p[4]) or 0) * 2**20, "temp": num(p[5]),
                    "power": num(p[6]) if len(p) > 6 else None})
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

if sys.platform == "darwin":  # macOS has no /proc: the same numbers from mach, sysctl, vm_stat, ioreg, netstat, mount
    import ctypes, plistlib, re
    libc = ctypes.CDLL("/usr/lib/libSystem.dylib")
    mach_host = libc.mach_host_self()  # once: every call adds a port reference

    def cpu_times():
        ticks, count = (ctypes.c_uint * 4)(), ctypes.c_uint(4)  # HOST_CPU_LOAD_INFO: user, system, idle, nice
        libc.host_statistics(mach_host, 3, ticks, ctypes.byref(count))
        return sum(ticks), ticks[2]

    def disks_io():
        r = w = 0
        try:
            drivers = plistlib.loads(subprocess.run(["ioreg", "-a", "-r", "-d", "1", "-c", "IOBlockStorageDriver"],
                                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                                    timeout=10).stdout or b"<array/>")
        except Exception:
            return 0, 0
        for d in drivers:
            st = d.get("Statistics") or {}
            r += st.get("Bytes (Read)", 0); w += st.get("Bytes (Write)", 0)
        return r, w

    def net_io():
        rx = tx = 0
        for line in (run(["netstat", "-ibn"]) or "").splitlines()[1:]:  # -n: no DNS lookups (they take seconds)
            p = line.split()
            # one <Link#n> row per interface holds its totals (the others repeat them per address)
            if len(p) < 10 or not p[2].startswith("<Link#") or \
                    p[0].rstrip("*").startswith(("lo", "utun", "bridge", "awdl", "llw", "gif", "stf", "anpi", "ap")):
                continue
            rx += int(p[-5]); tx += int(p[-2])
        return rx, tx

    def meminfo():
        out = run(["vm_stat"]) or ""
        page = int((re.search(r"page size of (\d+)", out) or [0, 4096])[1])
        v = {k.strip('"'): int(n) * page for k, n in re.findall(r"^([^:]+):\s+(\d+)\.", out, re.M)}
        total = int(run(["sysctl", "-n", "hw.memsize"]) or 0)
        # like Activity Monitor's "Memory Used": app memory + wired + compressed
        used = v.get("Anonymous pages", 0) - v.get("Pages purgeable", 0) + v.get("Pages wired down", 0) + \
            v.get("Pages occupied by compressor", 0)
        sw = dict(re.findall(r"(total|used) = ([\d.]+)M", run(["sysctl", "-n", "vm.swapusage"]) or ""))
        return {"total": total, "used": min(max(used, 0), total),
                "swap_total": int(float(sw.get("total", 0)) * 2**20), "swap_used": int(float(sw.get("used", 0)) * 2**20)}

    def filesystems():
        out, seen = [], set()
        for line in (run(["mount"]) or "").splitlines():
            m = re.match(r"(\S+) on (.+) \((\w+)", line)
            if not m or m.group(3) not in ("apfs", "hfs", "exfat", "msdos", "ntfs", "ufs"):
                continue
            dev, mnt, fstype = m.groups()
            # the volumes of an APFS container (disk3s1, disk3s5, ...) share its space: count it once
            key = re.sub(r"s\d+(s\d+)?$", "", dev) if fstype == "apfs" else dev
            if key in seen:
                continue
            seen.add(key)
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

def split_addr(s):
    """'10.0.0.1:22', '[fd7a::1]:22', '[::ffff:10.0.0.1]:22', '100.1.2.3%tailscale0:22' -> (ip, port)."""
    host, _, port = s.rpartition(":")
    host = host.strip("[]").split("%")[0]
    return (host[7:] if host.startswith("::ffff:") else host), (int(port) if port.isdigit() else 0)

def tailscaled_status():
    """tailscaled's status, straight from its LocalAPI socket (what `tailscale status` asks, without
    starting the CLI each sample: ~1 ms instead of ~40 ms of CPU); the CLI if the socket is elsewhere."""
    for path in ("/var/run/tailscale/tailscaled.sock", "/run/tailscale/tailscaled.sock"):
        s = socket.socket(socket.AF_UNIX)
        try:
            s.settimeout(5)
            s.connect(path)
            s.sendall(b"GET /localapi/v0/status HTTP/1.0\r\nHost: local-tailscaled.sock\r\n"
                      b"Sec-Tailscale: localapi\r\n\r\n")
            chunks = []
            while True:
                b = s.recv(1 << 16)
                if not b:
                    break
                chunks.append(b)
            head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
            if head.split(b" ", 2)[1:2] == [b"200"]:
                return json.loads(body.decode("utf-8"))
        except (OSError, ValueError, IndexError):
            pass
        finally:
            s.close()
    try:
        return json.loads(run(["tailscale", "status", "--json"]) or "")
    except ValueError:
        return None

def tailnet_bytes():
    """{peer name: (rx, tx)} bytes exchanged with each tailnet peer since tailscaled met it (all protocols)."""
    st = tailscaled_status()
    if not isinstance(st, dict):
        return None
    out = {}
    for p in (st.get("Peer") or {}).values():
        name = (p.get("DNSName") or "").split(".")[0] or p.get("HostName") or "?"
        out[name] = (p.get("RxBytes") or 0, p.get("TxBytes") or 0)
    return out

def tcp_bytes():
    """{(local, peer): (bytes acked, bytes received)} of every established TCP socket, from the kernel's
    sock_diag through `ss` (no root needed). None without ss."""
    text = run(["ss", "-tinH", "state", "established"])
    if text is None:
        return None
    socks, key = {}, None
    for line in text.splitlines():
        if not line[:1].isspace():  # the socket; its tcp_info follows on an indented line
            addrs = [w for w in line.split() if ":" in w]
            key = (addrs[0], addrs[1]) if len(addrs) >= 2 else None
        acked = received = None
        for w in line.split():
            if w.startswith("bytes_acked:"):
                acked = int(w[12:])
            elif w.startswith("bytes_received:"):
                received = int(w[15:])
        if key and (acked is not None or received is not None):
            socks[key] = (acked or 0, received or 0)
    return socks

listen_cache = [0.0, set()]

def listening_ports():
    if time.time() - listen_cache[0] >= 10:
        listen_cache[0], ports = time.time(), set()
        for line in (run(["ss", "-tlnH"]) or "").splitlines():
            addrs = [w for w in line.split() if ":" in w]
            if addrs:
                ports.add(split_addr(addrs[0])[1])
        listen_cache[1] = ports
    return listen_cache[1]

def local_addrs():
    """This host's IPv4 / IPv6 addresses, so the collector can tell which host a LAN address is."""
    out, last = set(), None
    try:
        with open("/proc/net/fib_trie") as f:
            for line in f:
                s = line.strip()
                if s.startswith("|-- "):
                    last = s[4:]
                elif s == "/32 host LOCAL" and last:
                    out.add(last)
    except OSError:
        pass
    try:
        with open("/proc/net/if_inet6") as f:
            for line in f:
                h = line.split()[0]
                out.add(socket.inet_ntop(socket.AF_INET6, bytes.fromhex(h)))
    except (OSError, ValueError):
        pass
    return sorted(a for a in out if not a.startswith(("127.", "fe80:")) and a != "::1")

def flow_counters():
    return {"tailnet": tailnet_bytes(), "tcp": tcp_bytes()} if FLOWS else None

def flows(prev, cur, dt):
    """Bytes/s to and from each peer over the last interval. TCP is grouped by peer address and service port
    (the listening side's port); a socket that opened and closed between two samples isn't seen."""
    if cur is None:
        return None
    out = {"addrs": local_addrs(), "tailnet": None, "tcp": None}
    if cur["tailnet"] is not None:
        old, out["tailnet"] = prev["tailnet"] or {}, {}
        for name, (rx, tx) in cur["tailnet"].items():
            rx0, tx0 = old.get(name, (rx, tx))  # a new peer: its counters start at the next sample
            d_rx, d_tx = (rx - rx0 if rx >= rx0 else rx), (tx - tx0 if tx >= tx0 else tx)  # restart: from 0
            if d_rx or d_tx:
                out["tailnet"][name] = {"rx": d_rx / dt, "tx": d_tx / dt}
    if cur["tcp"] is not None:
        old, listen, agg = prev["tcp"] or {}, listening_ports(), {}
        for key, (acked, received) in cur["tcp"].items():
            a0, r0 = old.get(key, (0, 0))  # a socket opened during the interval: all its bytes are new
            tx, rx = max(0, acked - a0), max(0, received - r0)
            if not (tx or rx):
                continue
            (lip, lport), (pip, pport) = split_addr(key[0]), split_addr(key[1])
            if pip.startswith("127.") or pip == "::1":
                continue
            side = "in" if lport in listen else "out"  # in: the peer connected to our service
            e = agg.setdefault((pip, lport if side == "in" else pport, side), [0, 0])
            e[0] += rx
            e[1] += tx
        rows = [{"ip": k[0], "port": k[1], "side": k[2], "rx": v[0] / dt, "tx": v[1] / dt} for k, v in agg.items()]
        out["tcp"] = sorted(rows, key=lambda r: -(r["rx"] + r["tx"]))[:64]
    return out

def counters():
    return time.time(), cpu_times(), disks_io(), net_io(), flow_counters()

def report(prev, cur):
    t0, (c0, i0), (dr0, dw0), (nr0, nt0), f0 = prev
    t1, (c1, i1), (dr1, dw1), (nr1, nt1), f1 = cur
    dt = t1 - t0
    cpu = 100.0 * (1 - (i1 - i0) / max(c1 - c0, 1))
    gs = gpus()
    return json.dumps({
        "hostname": socket.gethostname(), "time": t1, "ncpu": os.cpu_count(), "load": os.getloadavg(),
        "cpu": cpu,
        "mem": meminfo(),
        "disk_io": {"read": (dr1 - dr0) / dt, "write": (dw1 - dw0) / dt},
        "net_io": {"rx": (nr1 - nr0) / dt, "tx": (nt1 - nt0) / dt},
        "fs": filesystems(), "gpus": gs, "temps": temps(),
        "power": power(cpu, sum(g["power"] or 0 for g in gs)),
        "user": pwd.getpwuid(os.getuid()).pw_name, "sessions": sessions(), "slurm": slurm(),
        "flows": flows(f0, f1, dt),
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


def tailscale_exe():
    """The tailscale CLI: on the PATH, or inside the macOS app bundle (which does not put it on the PATH)."""
    exe = shutil.which("tailscale")
    if exe:
        return exe
    for path in ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                 os.path.expanduser("~/Applications/Tailscale.app/Contents/MacOS/Tailscale")):
        if os.access(path, os.X_OK):
            return path
    sys.exit("tailmon: tailscale CLI not found (install it, or add it to the PATH)")


def discover():
    """Return (self_node, [peer nodes]) from tailscale; each node is a dict."""
    st = json.loads(subprocess.run([tailscale_exe(), "status", "--json"], check=True,
                                   stdout=subprocess.PIPE).stdout)

    def node(p):
        return {"name": p["DNSName"].split(".")[0] or p["HostName"], "dns": p["DNSName"].rstrip("."),
                "ip": (p.get("TailscaleIPs") or [None])[0], "ips": p.get("TailscaleIPs") or [],
                "os": p.get("OS", ""), "online": p.get("Online", False)}
    me, peers = node(st["Self"]), [node(p) for p in (st.get("Peer") or {}).values()]
    TAILNET[:] = [me] + peers  # every node, also those not probed: names for the flow view
    return me, peers


TAILNET = []  # the nodes of the last discover()


PROBE_SELF_OSES = ("linux", "macOS")  # the probe also reads a Mac, but only this one: remote Macs are not probed


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


def probe_cmd(node, cfg, is_self, sample, count, flows=False):
    args = [sample, count, 1 if flows else 0]
    if is_self:
        return [sys.executable, "-"] + [str(x) for x in args]
    return python_cmd(node, cfg, args, keepalive=True)


def probe(node, cfg, is_self, flows=False):
    args = probe_cmd(node, cfg, is_self, cfg["sample_seconds"], 1, flows)
    try:
        r = subprocess.run(args, input=PROBE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True, timeout=30 + cfg["sample_seconds"])
    except subprocess.TimeoutExpired:
        return {"error": "timeout"}
    if r.returncode != 0:
        return {"error": ssh_error(r.stderr.splitlines(), r.returncode)}
    return json.loads(r.stdout)


def targets_for(cfg, only=None, exclude=(), oses=("linux",), self_oses=None):
    """Return ([(node, is_self)], [skipped nodes]); `exclude` also drops this machine if it is named.

    This machine is a target only when its OS is in `self_oses` (default: `oses`), since it runs the same
    scripts locally; e.g. the probe and the file walker also run on a Mac, the volume script does not."""
    me, peers = discover()
    targets, skipped = [], []
    if me["name"] not in exclude:
        if me["os"] in (oses if self_oses is None else self_oses):
            targets.append((me, True))
        elif not only or me["name"] in only:
            skipped.append(me)
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


def collect(cfg, only=None, flows=False):
    targets, skipped = targets_for(cfg, only, self_oses=PROBE_SELF_OSES)
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        futs = [(n, ex.submit(probe, n, cfg, s, flows)) for n, s in targets]
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
    SCRIPT = PROBE

    def __init__(self, node, cfg, is_self, interval, flows=False):
        self.node, self.cfg, self.is_self, self.interval, self.flows = node, cfg, is_self, interval, flows
        self.proc, self.latest, self.received, self.seq, self.retry_at = None, None, 0.0, 0, 0.0
        self.watts_sum, self.watts_n = 0.0, 0  # power samples since the start: the 24 h cost uses their mean

    def ensure(self):
        if (self.proc and self.proc.poll() is None) or time.time() < self.retry_at:
            return
        self.retry_at = time.time() + self.RETRY
        self.proc = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, universal_newlines=True, bufsize=1)
        try:
            self.proc.stdin.write(self.SCRIPT)
            self.proc.stdin.close()
        except OSError:
            pass  # the command already died; _read reports its error
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def command(self):
        return probe_cmd(self.node, self.cfg, self.is_self, self.interval, 0, self.flows)

    def _read(self, proc):
        for line in proc.stdout:
            try:
                self.latest = json.loads(line)
            except ValueError:
                continue
            if (self.latest.get("power") or {}).get("watts") is not None:
                self.watts_sum += self.latest["power"]["watts"]
                self.watts_n += 1
            self.received, self.seq = time.time(), self.seq + 1
        err = proc.stderr.read().splitlines()
        code = proc.wait()
        if proc is not self.proc:
            return  # stopped on purpose (or replaced): not an error
        self.latest = {"error": ssh_error(err, code)}
        self.received, self.seq = time.time(), self.seq + 1

    def result(self):
        if self.latest is None:
            r = {"pending": "connecting…"}
        elif "error" not in self.latest and time.time() - self.received > max(3 * self.interval, 15):
            r = {"error": "no data for %ds" % (time.time() - self.received)}
        else:
            r = self.latest
        if self.watts_n and "error" not in r and "pending" not in r:
            r = dict(r, power_avg=self.watts_sum / self.watts_n)
        return dict(r, node=self.node["name"])

    def stop(self):
        proc, self.proc = self.proc, None
        if proc and proc.poll() is None:
            proc.kill()


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


FLOW_MIN = 512.0  # B/s: links slower than this are counted but not drawn
FLOW_MAX_LINKS = 25
SERVICE_NAMES = {}


def service(port):
    if port not in SERVICE_NAMES:
        try:
            SERVICE_NAMES[port] = socket.getservbyport(port, "tcp")
        except (OSError, OverflowError):
            SERVICE_NAMES[port] = str(port)
    return SERVICE_NAMES[port]


def address_book(results):
    """{ip: host} from the Tailscale addresses of every node and the addresses each probed host reports
    (LAN, ...). An address several hosts claim (docker0's 172.17.0.1, say) names nobody."""
    book, clash = {}, set()
    for n in TAILNET:
        for ip in n.get("ips") or []:
            book[ip] = n["name"]
    for r in results:
        for ip in (r.get("flows") or {}).get("addrs") or []:
            if book.get(ip, r["node"]) != r["node"]:
                clash.add(ip)
            book.setdefault(ip, r["node"])
    for ip in clash:
        book.pop(ip, None)
    return book


def flow_links(results):
    """Traffic between hosts, merged from both ends: [{from, to, rate, tailscale, tcp: {service@server: B/s}}].
    Each end sees the same bytes, so a direction gets the larger of the two reports (the other end may be
    unprobed: macOS, a phone). Its rate is the larger of the tailscale counter (everything over the tailnet,
    WireGuard overhead included) and the sum of its TCP sockets (which also covers direct LAN traffic)."""
    book, links = address_book(results), {}

    def link(src, dst):
        return links.setdefault((src, dst), {"from": src, "to": dst, "tailscale": 0.0, "tcp": {}})

    for r in results:
        f = r.get("flows")
        if not f:
            continue
        me = r["node"]
        for peer, v in (f.get("tailnet") or {}).items():
            if peer != me:
                for src, dst, rate in ((me, peer, v["tx"]), (peer, me, v["rx"])):
                    l = link(src, dst)
                    l["tailscale"] = max(l["tailscale"], rate)
        for t in f.get("tcp") or []:
            peer = book.get(t["ip"])
            if not peer or peer == me:
                continue  # the internet, or this host's own address
            label = "%s@%s" % (service(t["port"]), me if t["side"] == "in" else peer)
            for src, dst, rate in ((me, peer, t["tx"]), (peer, me, t["rx"])):
                if rate > 0:
                    tcp = link(src, dst)["tcp"]
                    tcp[label] = max(tcp.get(label, 0.0), rate)
    out = []
    for l in links.values():
        l["rate"] = max(l["tailscale"], sum(l["tcp"].values()))
        if l["rate"] > 0:
            out.append(l)
    return sorted(out, key=lambda l: -l["rate"])


def rate_bar(rate, width=10):
    """Log scale, 1 KB/s (empty) to 1 GB/s (full), colored as the other bars at ~16 MB/s and ~250 MB/s."""
    pct = max(0.0, min(100.0, 100.0 * math.log10(max(rate, 1.0) / 1e3) / 6)) if rate > 0 else 0.0
    return bar(pct, width)


def render_flows(results):
    """FLOWS: one line per pair of hosts, both directions, busiest first, with the TCP services behind it."""
    links = flow_links(results)
    pairs = {}
    for l in links:
        pairs.setdefault(tuple(sorted((l["from"], l["to"]))), []).append(l)
    rows = []
    for key, ls in pairs.items():
        ls.sort(key=lambda l: -l["rate"])
        fwd = ls[0]  # the busier direction goes left to right
        back = ls[1] if len(ls) > 1 else {"rate": 0.0, "tcp": {}}
        rows.append((fwd["rate"] + back["rate"], fwd, back))
    rows.sort(key=lambda x: -x[0])
    shown = [x for x in rows if x[0] >= FLOW_MIN][:FLOW_MAX_LINKS]
    probed = [r for r in results if r.get("flows")]
    blind = sorted(r["node"] for r in probed
                   if r["flows"].get("tailnet") is None and r["flows"].get("tcp") is None)
    head = "FLOWS            %d link%s between hosts" % (len(shown), "" if len(shown) == 1 else "s")
    if len(rows) > len(shown):
        head += ", %d quieter hidden" % (len(rows) - len(shown))
    lines = [head + ("  (no tailscale or ss on: %s)" % ", ".join(blind) if blind else "")]
    for total, fwd, back in shown:
        services = {}
        for l in (fwd, back):
            for label, rate in l["tcp"].items():
                services[label] = services.get(label, 0.0) + rate
        top = sorted(services.items(), key=lambda x: -x[1])
        detail = "  ".join("%s %s" % (label, hb(rate, True)) for label, rate in top[:4] if rate >= FLOW_MIN / 4)
        lines.append("  %-16s ⇄ %-16s %s  → %-9s ← %-9s %s" % (
            fwd["from"][:16], fwd["to"][:16], rate_bar(total), hb(fwd["rate"], True), hb(back["rate"], True),
            detail))
    if not probed:
        lines.append("  no flow data yet")
    elif not shown:
        lines.append("  quiet: nothing above %s between hosts" % hb(FLOW_MIN, True))
    return lines + [""]


POWER_CFG = {"kwh_price": 6.0, "currency": "CZK", "power": {}}  # set from the config


def energy(r):
    """A host's power and its electricity over 24 h at that rate: the mean of the samples seen so far in a
    live view, the current reading in a snapshot. None without a reading."""
    p = r.get("power") or {}
    fixed = (POWER_CFG.get("power") or {}).get(r.get("node"))
    if fixed is not None:
        watts = avg = float(fixed)
        source = "config"
    elif p.get("watts") is not None:
        watts, avg, source = p["watts"], r.get("power_avg", p["watts"]), p["source"]
    else:
        return None
    kwh = avg * 24 / 1000.0
    return {"watts": watts, "avg": avg, "source": source, "kwh_24h": kwh,
            "cost_24h": kwh * POWER_CFG["kwh_price"], "currency": POWER_CFG["currency"],
            "parts": p.get("parts") if source == "estimate" else None}


def energy_text(e):
    """'≈172 W  4.1 kWh 25 CZK/24h'; ≈ marks an estimate (no meter, not on battery, not set in the config)."""
    return "%s%d W  %.1f kWh %.0f %s/24h" % ("≈" if e["source"] == "estimate" else "", round(e["watts"]),
                                             e["kwh_24h"], e["cost_24h"], e["currency"])


def render_energy(results):
    es = [(r["node"], energy(r)) for r in results if "error" not in r and "pending" not in r]
    es = [(n, e) for n, e in es if e]
    if not es:
        return []
    w, kwh, cost = (sum(e[k] for _, e in es) for k in ("avg", "kwh_24h", "cost_24h"))
    measured = [n for n, e in es if e["source"] != "estimate"]
    return ["POWER            %d host%s  %d W  %.1f kWh %.0f %s/24h at %g %s/kWh%s" % (
        len(es), "" if len(es) == 1 else "s", round(w), kwh, cost, es[0][1]["currency"], POWER_CFG["kwh_price"],
        es[0][1]["currency"], "  (measured: %s; the rest estimated)" % ", ".join(sorted(measured))
        if measured and len(measured) < len(es) else "  (estimated)" if not measured else "  (measured)"), ""]


def render(results, skipped, view="hosts"):
    """view: "hosts" (the usual table), "flows" (it plus the FLOWS section) or "flows-only"."""
    w = shutil.get_terminal_size((120, 40)).columns
    lines = [time.strftime("tailmon  %Y-%m-%d %H:%M:%S"), ""]
    if view == "flows-only":
        lines += render_flows(results)
        down = sorted((r["node"], r["error"]) for r in results if "error" in r)
        lines += ["%-16s %s" % (n, c("UNREACHABLE: " + e, 100)) for n, e in down]
        if skipped:
            lines.append("not probed (their traffic with probed hosts is shown): " + ", ".join(
                "%s (%s)" % (p["name"], p["os"] if p["online"] else "offline") for p in skipped))
        return "\n".join(l if len(l) < w + 20 else l[:w + 20] for l in lines)
    lines += render_alarms(results)
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
        e = energy(r)
        lines.append("%-16s %d cores  load %.2f %.2f %.2f%s" % ((r["node"], r["ncpu"]) + tuple(r["load"]) +
                                                               ("   " + energy_text(e) if e else "",)))
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
    lines += render_energy(results)
    if view == "flows":
        lines += render_flows(results)
    if skipped:
        lines.append("not probed: " + ", ".join(
            "%s (%s)" % (p["name"], p["os"] if p["online"] else "offline") for p in skipped))
    return "\n".join(l if len(l) < w + 20 else l[:w + 20] for l in lines)


def draw(results, skipped, live, view="hosts"):
    out = render(results, skipped, view)
    if live and COLOR:
        # home the cursor and overwrite in place (clear to end of each line / screen) to avoid flicker
        out = "\033[H" + out.replace("\n", "\033[K\n") + "\033[K\033[J"
    print(out, flush=True)


def rediscover(streams, logged, cfg, only, interval, flows=False):
    """Start streams for newly seen hosts and stop those that went away; return the skipped list."""
    targets, skipped = targets_for(cfg, only, self_oses=PROBE_SELF_OSES)
    names = {n["name"] for n, _ in targets}
    for name in set(streams) - names:
        streams.pop(name).stop()
        logged.pop(name, None)
    for n, is_self in targets:
        if n["name"] not in streams:
            streams[n["name"]] = Stream(n, cfg, is_self, interval, flows)
    return skipped


def watch(cfg, only, a):
    streams, logged, alerted = {}, {}, set()
    skipped = rediscover(streams, logged, cfg, only, a.watch, a.flows)
    next_discover = time.time() + cfg["discover_seconds"]
    if not a.quiet and COLOR:
        print("\033[2J", end="")
    try:
        deadline = time.time()
        while True:
            if time.time() >= next_discover:
                next_discover = time.time() + cfg["discover_seconds"]
                try:
                    skipped = rediscover(streams, logged, cfg, only, a.watch, a.flows)
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
                draw(results, skipped, True, a.view)
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
# htop: a web page with an htop-like view of every host (cores, memory, processes), live. Each host streams
# its process list over SSH only while the page is open.
# ---------------------------------------------------------------------------------------------
HTOP = r'''
import json, os, pwd, re, shutil, socket, subprocess, sys, time
''' + POWER + r'''
INTERVAL = float(sys.argv[1]) if len(sys.argv) > 1 else 2.0
TOP = int(sys.argv[2]) if len(sys.argv) > 2 else 40  # processes sent: the busiest by CPU, plus by memory
names = {}

def user(uid):
    if uid not in names:
        try:
            names[uid] = pwd.getpwuid(uid).pw_name
        except KeyError:
            names[uid] = str(uid)
    return names[uid]

def emit(d):
    sys.stdout.write(json.dumps(d) + "\n")
    sys.stdout.flush()

def pick(rows):
    """The TOP busiest by CPU, and the TOP biggest in memory, so the page can sort by either."""
    keep = {r["pid"]: r for r in sorted(rows, key=lambda r: -r["cpu"])[:TOP]}
    keep.update((r["pid"], r) for r in sorted(rows, key=lambda r: -r["res"])[:TOP])
    return list(keep.values())

def run(*cmd):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                          timeout=10, env=dict(os.environ, LC_ALL="C")).stdout

gpu_cache = [0.0, 0.0]

def gpu_watts():
    # NVIDIA GPUs' own reading, asked at most every 10 s (nvidia-smi is slow to start)
    if not shutil.which("nvidia-smi"):
        return 0.0
    if time.time() - gpu_cache[0] >= 10:
        gpu_cache[0] = time.time()
        try:
            out = run("nvidia-smi", "--query-gpu=power.draw", "--format=csv,noheader,nounits")
            gpu_cache[1] = sum(float(x) for x in out.split() if x.replace(".", "", 1).isdigit())
        except (OSError, subprocess.SubprocessError):
            gpu_cache[1] = 0.0
    return gpu_cache[1]

def linux():
    tck, page = os.sysconf("SC_CLK_TCK"), os.sysconf("SC_PAGE_SIZE")

    def cores():
        out = []
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("cpu") and line[3:4].isdigit():
                    v = [int(x) for x in line.split()[1:]]
                    out.append((sum(v[:8]), v[3] + (v[4] if len(v) > 4 else 0)))
        return out

    def procs():
        out = {}
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                with open("/proc/%s/stat" % pid) as f:
                    st = f.read()
                uid = os.stat("/proc/" + pid).st_uid
            except OSError:
                continue  # gone meanwhile
            end = st.rindex(")")
            p = st[end + 2:].split()  # p[n - 3] is field n of proc(5)
            out[int(pid)] = {"s": p[0], "ticks": int(p[11]) + int(p[12]), "pri": int(p[15]), "ni": int(p[16]),
                             "thr": int(p[17]), "virt": int(p[20]), "res": int(p[21]) * page, "uid": uid,
                             "comm": st[st.index("(") + 1:end]}
        return out

    def memory():
        m = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                m[k] = int(v.split()[0]) * 1024
        cache = m.get("Buffers", 0) + m.get("Cached", 0) + m.get("SReclaimable", 0) - m.get("Shmem", 0)
        return {"total": m["MemTotal"], "used": m["MemTotal"] - m["MemFree"] - cache, "cache": cache,
                "swap_total": m.get("SwapTotal", 0), "swap_used": m.get("SwapTotal", 0) - m.get("SwapFree", 0)}

    c0, p0, t0 = cores(), procs(), time.time()
    deadline = t0
    while True:
        deadline += INTERVAL
        time.sleep(max(0.0, deadline - time.time()))
        c1, p1, t1 = cores(), procs(), time.time()
        dt = max(t1 - t0, 1e-3)
        mem = memory()
        rows = []
        for pid, v in p1.items():
            old = p0.get(pid)
            rows.append({"pid": pid, "user": user(v["uid"]), "pri": v["pri"], "ni": v["ni"], "virt": v["virt"],
                         "res": v["res"], "s": v["s"], "thr": v["thr"],
                         "cpu": 100.0 * (v["ticks"] - old["ticks"]) / tck / dt if old else 0.0,
                         "mem": 100.0 * v["res"] / mem["total"], "time": v["ticks"] / float(tck),
                         "comm": v["comm"]})
        rows = pick(rows)
        for r in rows:  # the full command line, only for the processes sent
            try:
                with open("/proc/%d/cmdline" % r["pid"], "rb") as f:
                    cmd = f.read().rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                cmd = ""
            r["cmd"] = cmd or "[%s]" % r["comm"]  # kernel threads have none
        cores_pct = [100.0 * (1 - (i1 - i0) / max(a1 - a0, 1)) for (a0, i0), (a1, i1) in zip(c0, c1)]
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
        cpu = sum(cores_pct) / max(len(cores_pct), 1)
        emit({"hostname": socket.gethostname(), "time": t1, "cores": cores_pct, "power": power(cpu, gpu_watts()),
              "cpu": cpu, "mem": mem, "load": os.getloadavg(),
              "uptime": uptime, "tasks": len(p1), "threads": sum(v["thr"] for v in p1.values()),
              "running": sum(1 for v in p1.values() if v["s"] == "R"), "procs": rows, "me": user(os.getuid())})
        c0, p0, t0 = c1, p1, t1

def seconds(text):
    """ps times: '1:02.03', '01:02:03', '2-01:02:03'."""
    days, _, rest = text.rpartition("-")
    total = 0.0
    for part in rest.split(":"):
        total = total * 60 + float(part or 0)
    return total + 86400 * int(days or 0)

def macos():
    total = int(run("sysctl", "-n", "hw.memsize").strip() or 0)
    ncpu = os.cpu_count() or 1
    boot = re.search(r"sec = (\d+)", run("sysctl", "-n", "kern.boottime"))
    while True:
        vm = run("vm_stat")
        page = int((re.search(r"page size of (\d+)", vm) or [0, 4096])[1])
        pages = dict((k.strip(), int(v.strip(" ."))) for k, v in re.findall(r"^([^:]+):\s+(\d+)\.?$", vm, re.M))
        used = page * (pages.get("Pages active", 0) + pages.get("Pages wired down", 0) +
                       pages.get("Pages occupied by compressor", 0))
        cache = page * (pages.get("Pages inactive", 0) + pages.get("Pages purgeable", 0))
        swap = re.findall(r"(\w+) = ([\d.]+)M", run("sysctl", "-n", "vm.swapusage"))
        swap = dict((k, float(v) * 2**20) for k, v in swap)
        rows = []
        for line in run("ps", "-axo", "pid=,user=,pri=,nice=,vsz=,rss=,state=,%cpu=,%mem=,time=,command=").splitlines():
            p = line.split(None, 10)
            if len(p) < 11:
                continue
            rows.append({"pid": int(p[0]), "user": p[1], "pri": int(p[2]), "ni": int(p[3]), "virt": int(p[4]) * 1024,
                         "res": int(p[5]) * 1024, "s": p[6][:1], "thr": None, "cpu": float(p[7]), "mem": float(p[8]),
                         "time": seconds(p[9]), "comm": os.path.basename(p[10].split()[0]), "cmd": p[10]})
        rows = pick(rows) if rows else rows
        cpu = min(100.0, sum(r["cpu"] for r in rows) / ncpu)
        emit({"hostname": socket.gethostname(), "time": time.time(), "cores": [], "cpu": cpu,
              "power": power(cpu, mac=True),
              "mem": {"total": total, "used": used, "cache": cache, "swap_total": swap.get("total", 0),
                      "swap_used": swap.get("used", 0)},
              "load": os.getloadavg(), "uptime": time.time() - int(boot.group(1)) if boot else None,
              "tasks": None, "threads": None, "running": None, "procs": rows, "me": user(os.getuid())})
        time.sleep(INTERVAL)

if sys.platform == "darwin":
    macos()
elif os.path.exists("/proc/stat"):
    linux()
else:
    emit({"error": "htop: only Linux and macOS (%s)" % sys.platform})
'''

HTOP_OSES = ("linux", "macOS")
HTOP_IDLE = 20.0  # seconds without a page asking: stop the streams, the hosts go back to costing nothing


class HtopStream(Stream):
    SCRIPT = HTOP

    def __init__(self, node, cfg, is_self, interval, top):
        Stream.__init__(self, node, cfg, is_self, interval)
        self.top = top

    def command(self):
        args = [self.interval, self.top]
        if self.is_self:
            return [sys.executable, "-"] + [str(x) for x in args]
        return python_cmd(self.node, self.cfg, args, keepalive=True)


HTOP_PAGE = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tailnet htop</title>
<style>
:root { --bg:#f4f5f7; --card:#fff; --fg:#1c2230; --muted:#6a7284; --line:#e0e3ea; --head:#eef0f4;
  --ok:#2f9e44; --warn:#d9a400; --bad:#d6336c; --cache:#e8b931; --swap:#c2255c; --low:#4c6ef5; --sel:#e7efff;
  --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg:#0f1115; --card:#171a21; --fg:#e4e7ee;
  --muted:#8b93a5; --line:#262b35; --head:#1d212a; --ok:#51cf66; --warn:#fcc419; --bad:#ff6b9a; --cache:#f0c541;
  --swap:#f06595; --low:#748ffc; --sel:#1f2a40; } }
:root[data-theme="dark"] { --bg:#0f1115; --card:#171a21; --fg:#e4e7ee; --muted:#8b93a5; --line:#262b35;
  --head:#1d212a; --ok:#51cf66; --warn:#fcc419; --bad:#ff6b9a; --cache:#f0c541; --swap:#f06595; --low:#748ffc; --sel:#1f2a40; }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:14px/1.4 system-ui, sans-serif; }
header { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line);
  display:flex; flex-wrap:wrap; gap:10px; align-items:center; padding:10px 16px; }
h1 { font-size:17px; margin:0 8px 0 0; } .sub { color:var(--muted); font-size:13px; flex:1; min-width:120px; }
input, select, button { font:inherit; color:var(--fg); background:var(--card); border:1px solid var(--line);
  border-radius:7px; padding:5px 9px; } input { width:200px; max-width:100%; } button { cursor:pointer; }
button.on { background:var(--low); border-color:var(--low); color:#fff; }
main { display:grid; grid-template-columns:repeat(auto-fill, minmax(min(100%, 620px), 1fr)); gap:12px; padding:12px 16px; }
.host { background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; min-width:0; }
.host.wide { grid-column:1 / -1; }
.top { display:flex; flex-wrap:wrap; gap:4px 14px; align-items:baseline; padding:9px 12px 6px; cursor:pointer; }
.name { font-weight:650; font-size:15px; }
.cost { margin-left:auto; font-size:12.5px; font-variant-numeric:tabular-nums; white-space:nowrap; cursor:help; }
.cost b { font-weight:650; } .cost .est { color:var(--muted); } .meta { color:var(--muted); font-size:12.5px; font-variant-numeric:tabular-nums; }
.meters { padding:0 12px 8px; font-family:var(--mono); font-size:11.5px; }
.cores { display:grid; grid-template-columns:repeat(auto-fill, minmax(150px, 1fr)); gap:2px 12px; margin-bottom:5px; }
.m { display:flex; align-items:center; gap:6px; min-width:0; } .m b { font-weight:600; width:3.2em; color:var(--muted); text-align:right; flex:none; }
.track { flex:1; height:9px; background:var(--head); border-radius:3px; overflow:hidden; display:flex; min-width:20px; }
.track i { display:block; height:100%; } .m span { width:9.5em; text-align:right; flex:none; color:var(--muted); white-space:nowrap; }
.cores .m span { width:3.4em; }
.cells { display:grid; grid-template-columns:repeat(auto-fill, 11px); gap:2px; margin:2px 0 6px; }
.cells i { height:18px; border-radius:2px; background:var(--head); display:flex; align-items:flex-end; overflow:hidden; }
.cells i u { display:block; width:100%; }
.cellnote { color:var(--muted); margin:0 0 4px; }
.tablewrap { overflow-x:auto; border-top:1px solid var(--line); }
table { border-collapse:collapse; width:100%; font-family:var(--mono); font-size:11.5px; font-variant-numeric:tabular-nums; }
th { background:var(--head); color:var(--muted); font-weight:600; text-align:right; padding:3px 6px; cursor:pointer;
  white-space:nowrap; user-select:none; position:sticky; top:0; } th.sorted { color:var(--fg); background:var(--sel); }
td { padding:1px 6px; text-align:right; white-space:nowrap; border-top:1px solid transparent; }
th.l, td.l { text-align:left; } td.cmd { text-align:left; max-width:0; width:100%; overflow:hidden; text-overflow:ellipsis; }
tr:hover td { background:var(--sel); } td.hot { color:var(--bad); font-weight:600; } td.warm { color:var(--warn); }
td.other { color:var(--low); } .state-R { color:var(--ok); font-weight:600; } .state-D { color:var(--bad); font-weight:600; }
.base { color:var(--fg); } .args { color:var(--muted); }
.err { color:var(--bad); padding:4px 12px 12px; } .pending { color:var(--muted); padding:4px 12px 12px; }
.more { color:var(--muted); font-size:12px; padding:4px 12px 8px; }
#skipped { color:var(--muted); font-size:12.5px; padding:0 16px 16px; }
</style></head><body>
<header><h1>htop</h1><span class="sub" id="sub">connecting…</span>
<input id="filter" type="search" placeholder="filter: command, user, pid" autocomplete="off">
<select id="rows" title="processes per host"><option>10</option><option selected>15</option><option>25</option><option>40</option></select>
<button id="pause" title="stop refreshing (the hosts stop streaming after a few seconds)">Pause</button></header>
<main id="hosts"></main><div id="skipped"></div>
<script>
const qs = location.search, $ = id => document.getElementById(id);
const COLS = [["pid","PID"],["user","USER","l"],["pri","PRI"],["ni","NI"],["virt","VIRT"],["res","RES"],["s","S"],
              ["cpu","CPU%"],["mem","MEM%"],["time","TIME+"],["cmd","Command","l"]];
let sortKey = "cpu", paused = false, wide = new Set(), data = null, timer = null;
try { sortKey = localStorage.getItem("htop.sort") || "cpu"; $("rows").value = localStorage.getItem("htop.rows") || "15"; } catch (e) {}
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function size(n) { if (n == null) return ""; const u = ["B","K","M","G","T","P"]; let i = 0;
  while (Math.abs(n) >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return (i && n < 10 ? n.toFixed(1) : Math.round(n)) + u[i]; }
function cputime(t) { if (t == null) return ""; const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60;
  return h ? h + "h" + String(m).padStart(2, "0") + ":" + String(Math.floor(s)).padStart(2, "0")
           : m + ":" + s.toFixed(2).padStart(5, "0"); }
function uptime(t) { if (t == null) return ""; const d = Math.floor(t / 86400), h = Math.floor(t % 86400 / 3600), m = Math.floor(t % 3600 / 60);
  return (d ? d + "d " : "") + String(h).padStart(2, "0") + ":" + String(m).padStart(2, "0"); }
const level = p => p >= 90 ? "var(--bad)" : p >= 70 ? "var(--warn)" : "var(--ok)";
function meter(label, parts, text) {
  return '<div class="m"><b>' + label + '</b><div class="track">' +
    parts.map(([p, color]) => '<i style="width:' + Math.max(0, Math.min(100, p)).toFixed(1) + '%;background:' + color + '"></i>').join("") +
    '</div><span>' + text + '</span></div>'; }
function compare(a, b) { const x = a[sortKey], y = b[sortKey];
  if (typeof x === "string" || typeof y === "string") return String(x).localeCompare(String(y));
  return sortKey === "pid" ? x - y : (y || 0) - (x || 0); }
function procRow(p, me) {
  const cls = p.cpu >= 90 ? "hot" : p.cpu >= 40 ? "warm" : "";
  const base = esc(p.cmd).split(" ")[0], args = esc(p.cmd).slice(base.length);
  return "<tr><td>" + p.pid + '</td><td class="l' + (p.user !== me && p.user !== "root" ? " other" : "") + '">' + esc(p.user).slice(0, 10) +
    "</td><td>" + p.pri + "</td><td>" + p.ni + "</td><td>" + size(p.virt) + "</td><td>" + size(p.res) +
    '</td><td class="state-' + esc(p.s) + '">' + esc(p.s) + '</td><td class="' + cls + '">' + p.cpu.toFixed(1) +
    "</td><td>" + p.mem.toFixed(1) + "</td><td>" + cputime(p.time) + '</td><td class="cmd" title="' + esc(p.cmd) + '">' +
    '<span class="base">' + base + '</span><span class="args">' + args + "</span></td></tr>"; }
const SOURCES = {meter:"measured by the machine's power meter", battery:"measured: battery discharge",
                 config:"set in the config", estimate:"estimated"};
function costHtml(e) {
  if (!e) return "";
  const est = e.source === "estimate";
  let tip = Math.round(e.watts) + " W now, " + SOURCES[e.source] + ". 24 h at the mean so far (" + Math.round(e.avg) + " W): " +
    e.kwh_24h.toFixed(1) + " kWh × " + data.kwh_price + " " + e.currency + "/kWh.";
  if (e.parts) tip += " Estimate: " + Object.entries(e.parts).filter(([, w]) => w > 0.05)
    .map(([k, w]) => k + " " + Math.round(w) + " W").join(", ") + ", then ÷ 0.88 for the power supply.";
  return '<span class="cost" title="' + esc(tip) + '"><span class="est">' + (est ? "≈" : "") + Math.round(e.watts) + " W · " +
    e.kwh_24h.toFixed(1) + " kWh · </span><b>" + Math.round(e.cost_24h) + " " + esc(e.currency) + "</b>/24h</span>"; }
function card(h) {
  const isWide = wide.has(h.node);
  let html = '<div class="top" data-host="' + esc(h.node) + '" title="click: full width, more rows"><span class="name">' + esc(h.node) + "</span>";
  if (h.error) return html + '</div><div class="err">' + esc(h.error) + "</div>";
  if (h.pending) return html + '</div><div class="pending">' + esc(h.pending) + "</div>";
  const tasks = h.tasks != null ? "Tasks " + h.tasks + ", " + h.threads + " thr; " + h.running + " running" : "";
  html += '<span class="meta">' + [tasks, "Load " + h.load.map(x => x.toFixed(2)).join(" "),
    h.uptime != null ? "Uptime " + uptime(h.uptime) : ""].filter(Boolean).join(" · ") + "</span>" + costHtml(h.energy) + "</div>";
  html += '<div class="meters">';
  if (h.cores.length > 24) {  // many cores: one small gauge each, the overall CPU as a bar
    html += meter("CPU", [[h.cpu, level(h.cpu)]], h.cpu.toFixed(1) + "% of " + h.cores.length);
    html += '<div class="cells">' + h.cores.map((p, i) => '<i title="core ' + i + ": " + p.toFixed(0) + '%"><u style="height:' +
      Math.max(p > 0.5 ? 8 : 0, Math.min(100, p)).toFixed(0) + "%;background:" + level(p) + '"></u></i>').join("") + "</div>";
    const busy = h.cores.filter(p => p >= 50).length;
    html += '<div class="cellnote">' + busy + " of " + h.cores.length + " cores over 50%</div>";
  } else if (h.cores.length) html += '<div class="cores">' + h.cores.map((p, i) => meter(i, [[p, level(p)]], p.toFixed(0) + "%")).join("") + "</div>";
  else html += meter("CPU", [[h.cpu, level(h.cpu)]], h.cpu.toFixed(1) + "%");
  const m = h.mem;
  html += meter("Mem", [[100 * m.used / m.total, "var(--ok)"], [100 * m.cache / m.total, "var(--cache)"]],
                size(m.used) + " / " + size(m.total));
  if (m.swap_total) html += meter("Swp", [[100 * m.swap_used / m.swap_total, "var(--swap)"]], size(m.swap_used) + " / " + size(m.swap_total));
  html += "</div>";
  const f = $("filter").value.trim().toLowerCase();
  let procs = h.procs.filter(p => !f || (p.cmd + " " + p.user + " " + p.pid).toLowerCase().includes(f)).sort(compare);
  const n = isWide ? procs.length : +$("rows").value, shown = procs.slice(0, n);
  html += '<div class="tablewrap"><table><thead><tr>' + COLS.map(([k, t, l]) =>
    '<th data-sort="' + k + '" class="' + (l || "") + (k === sortKey ? " sorted" : "") + '">' + t + "</th>").join("") +
    "</tr></thead><tbody>" + shown.map(p => procRow(p, h.me)).join("") + "</tbody></table></div>";
  if (procs.length > n) html += '<div class="more">' + (procs.length - n) + " more (click the host name)</div>";
  else if (!procs.length) html += '<div class="more">no matching process among the busiest</div>';
  return html; }
function render() {
  if (!data) return;
  const el = $("hosts");
  const keep = new Map([...el.children].map(c => [c.dataset.host, c]));
  data.hosts.forEach(h => {
    let d = keep.get(h.node);
    if (!d) { d = document.createElement("section"); d.className = "host"; d.dataset.host = h.node; }
    keep.delete(h.node);
    d.classList.toggle("wide", wide.has(h.node));
    d.innerHTML = card(h);
    el.append(d); });
  keep.forEach(d => d.remove());
  const ok = data.hosts.filter(h => !h.error && !h.pending).length;
  const es = data.hosts.map(h => h.energy).filter(Boolean);
  const total = es.length ? " · " + Math.round(es.reduce((a, e) => a + e.avg, 0)) + " W, " +
    Math.round(es.reduce((a, e) => a + e.cost_24h, 0)) + " " + data.currency + "/24h for all" : "";
  $("sub").textContent = ok + "/" + data.hosts.length + " hosts · every " + data.interval + " s" + total + (paused ? " · paused" : "");
  $("skipped").textContent = data.skipped.length ? "not shown: " + data.skipped.join(", ") : "";
  if (!data.hosts.length) el.innerHTML = '<div class="pending">No host.</div>'; }
async function load() {
  clearTimeout(timer);
  if (paused || document.hidden) return;
  try { const r = await fetch("/api/htop" + qs); if (!r.ok) throw new Error(await r.text()); data = await r.json(); render(); }
  catch (e) { $("sub").textContent = "error: " + (e.message || e); }
  timer = setTimeout(load, 1000 * ((data && data.interval) || 2)); }
document.addEventListener("click", e => {
  const th = e.target.closest("th[data-sort]");
  if (th) { sortKey = th.dataset.sort; try { localStorage.setItem("htop.sort", sortKey); } catch (_) {} render(); return; }
  const top = e.target.closest(".top");
  if (top) { const h = top.dataset.host; wide.has(h) ? wide.delete(h) : wide.add(h); render(); } });
$("filter").oninput = render;
$("rows").onchange = () => { try { localStorage.setItem("htop.rows", $("rows").value); } catch (_) {} render(); };
$("pause").onclick = () => { paused = !paused; $("pause").classList.toggle("on", paused);
  $("pause").textContent = paused ? "Resume" : "Pause"; paused ? render() : load(); };
document.addEventListener("visibilitychange", load);
load();
</script></body></html>
'''


def htop_main(argv):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlparse, parse_qs
    ap = argparse.ArgumentParser(prog="tailmon htop",
                                 description="Web page with an htop of every Linux and macOS host, live")
    ap.add_argument("--bind", metavar="ADDR",
                    help="only listen on this address (default: this PC and its Tailscale address, so every "
                         "tailnet machine can open http://<this-pc>:PORT/ and nothing else can; process lists "
                         "show every command line, so think twice before 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--token", help="require ?token=TOKEN (or an X-Token header) on every request")
    ap.add_argument("--hosts", help="comma-separated Tailscale hostnames to show")
    ap.add_argument("--exclude", metavar="LIST", help="comma-separated Tailscale hostnames to leave out")
    ap.add_argument("-w", "--interval", type=float, default=2.0, metavar="SEC",
                    help="refresh every SEC seconds (default %(default)g)")
    ap.add_argument("-n", "--processes", type=int, default=40, metavar="N",
                    help="processes sent per host: the N busiest by CPU and the N biggest in memory "
                         "(default %(default)d)")
    ap.add_argument("--no-browser", action="store_true", help="don't open the page in a browser")
    a = ap.parse_args(argv[1:])
    if a.interval < 0.5 or a.processes < 1:
        ap.error("--interval must be at least 0.5 and --processes at least 1")
    cfg = load_config()
    POWER_CFG.update((k, cfg[k]) for k in POWER_CFG)
    only = set(x for x in a.hosts.split(",") if x) if a.hosts else None
    exclude = set(x for x in a.exclude.split(",") if x) if a.exclude else set()
    streams, lock = {}, threading.Lock()
    state = {"skipped": [], "discovered": 0.0, "asked": 0.0}

    def rediscover():
        targets, skipped = targets_for(cfg, only, exclude, HTOP_OSES)
        names = {n["name"] for n, _ in targets}
        for name in set(streams) - names:
            streams.pop(name).stop()
        for n, is_self in targets:
            if n["name"] not in streams:
                streams[n["name"]] = HtopStream(n, cfg, is_self, a.interval, a.processes)
        state["skipped"] = sorted("%s (%s)" % (p["name"], p["os"] if p["online"] else "offline") for p in skipped)
        state["discovered"] = time.time()

    def snapshot():
        with lock:
            state["asked"] = time.time()
            if time.time() - state["discovered"] >= cfg["discover_seconds"]:
                try:
                    rediscover()
                except (OSError, ValueError, subprocess.CalledProcessError):
                    pass  # tailscale hiccup: keep the current hosts
            for s in streams.values():
                s.ensure()
            hosts = sorted((s.result() for s in streams.values()), key=lambda r: (not streams[r["node"]].is_self,
                                                                                   r["node"]))
            for r in hosts:
                if "error" not in r and "pending" not in r:
                    r["energy"] = energy(r)
            return {"hosts": hosts, "skipped": state["skipped"], "interval": a.interval,
                    "kwh_price": POWER_CFG["kwh_price"], "currency": POWER_CFG["currency"]}

    def reaper():
        # Nobody looking (tab closed, hidden or paused): stop every stream, so the hosts do no work.
        while True:
            time.sleep(5)
            with lock:
                if time.time() - state["asked"] > HTOP_IDLE:
                    for s in streams.values():
                        if s.proc:
                            s.stop()
                            s.latest, s.retry_at = None, 0.0

    class Handler(BaseHTTPRequestHandler):
        def reply(self, code, body, ctype="application/json"):
            data = (body if isinstance(body, str) else json.dumps(body)).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            url = urlparse(self.path)
            if a.token and a.token not in parse_qs(url.query).get("token", []) \
                    and self.headers.get("X-Token") != a.token:
                return self.reply(403, {"error": "forbidden: add ?token=..."})
            if url.path == "/":
                return self.reply(200, HTOP_PAGE, "text/html")
            if url.path == "/api/htop":
                return self.reply(200, snapshot())
            self.reply(404, {"error": "not found"})

        def log_message(self, fmt, *args):
            pass  # one poll per refresh: too chatty for the terminal

    with lock:
        rediscover()
    threading.Thread(target=reaper, daemon=True).start()
    # By default, this PC and the tailnet: one listener on localhost, one on the Tailscale address(es).
    me = TAILNET[0] if TAILNET else {}
    binds = [a.bind] if a.bind else ["127.0.0.1"] + [ip for ip in me.get("ips") or [] if ":" not in ip]
    servers = []
    for addr in binds:
        try:
            servers.append(ThreadingHTTPServer((addr, a.port), Handler))
        except OSError as e:
            print("tailmon: can't listen on %s:%d: %s" % (addr, a.port, e.strerror or e), file=sys.stderr)
    if not servers:
        sys.exit(1)
    token = "?token=" + a.token if a.token else ""
    local = "http://%s:%d/%s" % ("localhost" if binds[0] in ("0.0.0.0", "::", "127.0.0.1") else binds[0],
                                 a.port, token)
    print("tailmon: htop of %d hosts on %s" % (len(streams), local), file=sys.stderr)
    if len(servers) > 1 or binds[0] in ("0.0.0.0", "::"):
        print("         from the tailnet: http://%s:%d/%s" % (me.get("name") or me.get("ip"), a.port, token),
              file=sys.stderr)
    print("         Ctrl-C to stop", file=sys.stderr)
    if not a.no_browser:
        import webbrowser
        threading.Thread(target=webbrowser.open, args=(local,), daemon=True).start()
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        servers[0].serve_forever()
    finally:
        for server in servers:
            server.server_close()
        for s in streams.values():
            s.stop()


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
            for name, text in (("probe", PROBE), ("file-tool", FILE_TOOL), ("volume", VOLUME),
                                               ("htop", HTOP))}


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
            print("  %s  %s  %-5s  %s  %s: %s%s" % (local_time(r["started"]), c("%-7s" % r["state"], colors[r["state"]]),
                                                   cost, r["session"][:8], r["dir"], r["task"][:90],
                                                   "  [closed]" if r["completed"] else ""))


ZSH_COMPLETION = r'''#compdef tailmon
# zsh completion for tailmon: `tailmon completion zsh`

_tailmon_hosts() {
    local -a hosts
    hosts=(${(f)"$(tailscale status 2>/dev/null | awk '$1 ~ /^[0-9]/ {print $2}')"})
    _sequence compadd -a hosts
}

_tailmon() {
    local -a commands common
    commands=(
        'search:find files by name on every host'
        'grep:search file contents with a regular expression'
        'volume:show or change the sound volume of the hosts'
        'serve:web page and JSON API to control the volume'
        'mount:sshfs-mount every peer, one directory per host'
        'umount:unmount what tailmon mount mounted'
        'gate:let each host approve the scripts tailmon runs on it'
        'tasks:the Todoist tasks each host ran'
        'completion:print a shell completion script'
    )
    if (( CURRENT == 2 )) && [[ $words[2] != -* ]]; then
        _describe -t commands 'tailmon command' commands
        return
    fi
    if [[ $words[2] == -* ]]; then  # the monitor itself: options only
        _arguments '(-w --watch)'{-w,--watch}'[refresh every SEC seconds]:seconds' \
            '--json[print a JSON snapshot and exit]' '--log[append each sample to FILE]:file:_files' \
            '--hosts[comma-separated hostnames]:hosts:_tailmon_hosts' \
            '--quiet[do not draw the table]' '--alert[command run when a temperature is too high]:command' \
            '(-h --help)'{-h,--help}'[show help]'
        return
    fi
    local cmd=$words[2]
    (( CURRENT-- )); shift words
    common=('(-h --help)'{-h,--help}'[show help]' '--hosts[comma-separated hostnames]:hosts:_tailmon_hosts')
    case $cmd in
    search|grep)
        common+=('--exclude[hostnames not to search]:hosts:_tailmon_hosts' '--max-results[maximum results]:N'
                 '--hidden[include hidden files]' '--proc[also walk into /proc]' '--boom[disable the loop guard]'
                 '--json[print JSON]' '--timeout[give up on a host after SEC seconds]:seconds'
                 '*'{-v,--verbose}'[per-host summary; -vv: progress]'
                 '(-i --ignore-case)'{-i,--ignore-case}'[ignore case]')
        if [[ $cmd == search ]]; then
            _arguments $common '--name[filename glob]:glob' '1:root directory on each host:_files -/'
        else
            _arguments $common '--fixed-strings[pattern is literal text]' '1:pattern' \
                '2:root directory on each host:_files -/'
        fi ;;
    volume)
        _arguments $common '--exclude[hostnames to leave alone]:hosts:_tailmon_hosts' '--json[print JSON]' \
            '1:level:(mute unmute toggle +5 -5 25 50 75 100)' ;;
    serve)
        _arguments $common '--exclude[hostnames to leave out]:hosts:_tailmon_hosts' '--bind[address]:address' \
            '--port[port]:port' '--token[require this token]:token' ;;
    mount|umount)
        common+=('--root[local directory holding one mount per host]:directory:_files -/' '--json[print JSON]')
        [[ $cmd == mount ]] && common+=('--path[remote directory to mount]:path')
        _arguments $common ;;
    gate)
        _arguments $common '1:action:((install\:"set up the gate on the hosts"
            approve\:"approve this version of the scripts" status\:"ask every gate whether it would run this tailmon"
            remove\:"take the gate off the hosts" hashes\:"print the SHA-256 of the scripts"))' ;;
    tasks)
        _arguments $common '(-n --last)'{-n,--last}'[tasks shown per host]:N' '--days[days of completed tasks]:days' \
            '--label[dispatch label]:label' '--json[print JSON]' ;;
    completion)
        _arguments '1:shell:(zsh bash)' ;;
    esac
}

if [[ $zsh_eval_context[-1] == loadautofunc ]]; then
    _tailmon "$@"
else
    compdef _tailmon tailmon
fi
'''

BASH_COMPLETION = r'''# bash completion for tailmon: `tailmon completion bash`
_tailmon() {
    local cur=${COMP_WORDS[COMP_CWORD]} prev=${COMP_WORDS[COMP_CWORD-1]} words
    if [[ $prev == --hosts || $prev == --exclude ]]; then
        local done=${cur%"${cur##*,}"}
        COMPREPLY=($(compgen -P "$done" -W "$(tailscale status 2>/dev/null | awk '$1 ~ /^[0-9]/ {print $2}')" \
                     -- "${cur##*,}"))
        compopt -o nospace 2>/dev/null
        return
    fi
    if (( COMP_CWORD == 1 )); then
        words="search grep volume serve mount umount gate tasks completion"
        [[ $cur == -* ]] && words="-w --watch --json --log --hosts --quiet --alert --help"
    else
        case ${COMP_WORDS[1]} in
        search) words="--name -i --ignore-case --hosts --exclude --max-results --hidden --proc --boom --json --timeout -v --verbose --help" ;;
        grep) words="--fixed-strings -i --ignore-case --hosts --exclude --max-results --hidden --proc --boom --json --timeout -v --verbose --help" ;;
        volume) words="mute unmute toggle +5 -5 --hosts --exclude --json --help" ;;
        serve) words="--bind --port --token --hosts --exclude --help" ;;
        mount) words="--root --path --hosts --json --help" ;;
        umount) words="--root --hosts --json --help" ;;
        gate) words="install approve status remove hashes --hosts --help" ;;
        tasks) words="-n --last --hosts --days --label --json --help" ;;
        completion) words="zsh bash" ;;
        esac
    fi
    COMPREPLY=($(compgen -W "$words" -- "$cur"))
}
complete -o default -F _tailmon tailmon
'''


def completion_main(argv):
    ap = argparse.ArgumentParser(prog="tailmon completion",
                                 description="Print a shell completion script. zsh: add "
                                             "`eval \"$(tailmon completion zsh)\"` to ~/.zshrc (after compinit); "
                                             "bash: `eval \"$(tailmon completion bash)\"` in ~/.bashrc")
    ap.add_argument("shell", choices=("zsh", "bash"))
    a = ap.parse_args(argv[1:])
    sys.stdout.write(ZSH_COMPLETION if a.shell == "zsh" else BASH_COMPLETION)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "completion":
        return completion_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] in ("search", "grep"):
        return file_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] in ("mount", "umount"):
        return mount_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "volume":
        return volume_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        return serve_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "htop":
        return htop_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "gate":
        return gate_main(sys.argv[1:])
    if len(sys.argv) > 1 and sys.argv[1] == "tasks":
        return tasks_main(sys.argv[1:])
    flows_only = sys.argv[1:2] == ["flows"]
    argv = sys.argv[2:] if flows_only else sys.argv[1:]
    if flows_only:
        ap = argparse.ArgumentParser(prog="tailmon flows",
                                     description="Traffic between the Tailscale hosts: tailscale's byte counters "
                                                 "per peer and TCP sockets (ss), read on every Linux host")
    else:
        ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
        ap.add_argument("--flows", action="store_true", default=None,
                        help="also show the traffic between hosts (each probe then runs ss and tailscale status "
                             "every sample; `tailmon flows` shows only that)")
        ap.add_argument("--no-flows", dest="flows", action="store_false",
                        help="don't, even if the config has \"flows\": true")
    ap.add_argument("-w", "--watch", type=float, metavar="SEC", help="refresh every SEC seconds")
    ap.add_argument("--json", action="store_true", help="print a JSON snapshot and exit")
    ap.add_argument("--log", metavar="FILE", help="append each sample as JSON lines to FILE")
    ap.add_argument("--hosts", help="comma-separated Tailscale hostnames to restrict to")
    ap.add_argument("--quiet", action="store_true", help="don't draw the table (for cron/systemd, with --log or --alert)")
    ap.add_argument("--alert", metavar="CMD",
                    help="shell command run when a temperature reaches its limit (TAILMON_HOST, TAILMON_SENSOR, "
                         "TAILMON_TEMP, TAILMON_LIMIT, ... in its environment); a snapshot also exits with status 3")
    a = ap.parse_args(argv)
    cfg = load_config()
    TEMP_CFG.update((k, cfg[k]) for k in TEMP_CFG)
    POWER_CFG.update((k, cfg[k]) for k in POWER_CFG)
    only = set(a.hosts.split(",")) if a.hosts else None
    if flows_only:
        a.flows = True
    elif a.flows is None:
        a.flows = bool(cfg.get("flows"))
    a.view = "flows-only" if flows_only else "flows" if a.flows else "hosts"

    if a.watch and not a.json:
        return watch(cfg, only, a)
    results, skipped = collect(cfg, only, a.flows)
    if flows_only and a.json:
        print(json.dumps({"links": flow_links(results), "hosts": sorted(r["node"] for r in results if r.get("flows")),
                          "errors": {r["node"]: r["error"] for r in results if "error" in r}}, indent=2))
        return
    if a.log:
        with open(os.path.expanduser(a.log), "a") as f:
            for r in results:
                f.write(json.dumps(r) + "\n")
    if a.json:
        print(json.dumps(results, indent=2))
    elif not a.quiet:
        draw(results, skipped, False, a.view)
    if flows_only:
        return  # temperatures belong to the monitor: no HOT messages or exit status 3 here
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
