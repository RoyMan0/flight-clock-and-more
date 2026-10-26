"""
System stats collection — this Pi, or any Linux/macOS machine over SSH.

Each poll runs ONE small read-only shell script on the target and parses its
output. Cumulative counters (CPU ticks, disk bytes, network bytes) are kept
between polls, so CPU %, disk and network rates are averages over the refresh
interval (steadier than instantaneous values). The first poll therefore has
no rates yet.

Linux (Pi, TerraMaster TOS, Synology, Unraid, TrueNAS SCALE, servers):
  /proc/stat, /proc/meminfo, /proc/uptime, /proc/diskstats (physical disks
  only — partitions/md/dm skipped so nothing is double counted),
  /proc/net/dev (only real NICs: interfaces with /sys/class/net/<if>/device),
  CPU temperature from thermal zones / hwmon, GPU from nvidia-smi if present.
macOS:
  top (CPU), memory_pressure (memory), kern.boottime (uptime), ioreg block
  storage statistics (disk read/write bytes), netstat -ibn (en* interfaces),
  ioreg IOAccelerator "Device Utilization %" (GPU). Temperatures need admin
  rights on macOS, so they're only shown if `smctemp`/`osx-cpu-temp` is installed.
"""

import logging
import platform
import re
import subprocess
import threading
import time

from utilities import ssh_remote

log = logging.getLogger(__name__)

LINUX_SCRIPT = r'''
echo "@@HOST"; (hostname 2>/dev/null || cat /proc/sys/kernel/hostname)
echo "@@STAT"; head -n 1 /proc/stat
echo "@@MEM"; grep -E '^(MemTotal|MemAvailable|MemFree|Buffers|Cached):' /proc/meminfo
echo "@@UPTIME"; cat /proc/uptime
echo "@@DISK"; cat /proc/diskstats
echo "@@NETIF"; for i in /sys/class/net/*; do [ -e "$i/device" ] && echo "${i##*/}"; done
echo "@@NET"; cat /proc/net/dev
echo "@@THERMAL"; for z in /sys/class/thermal/thermal_zone*; do [ -r "$z/temp" ] && echo "$(cat "$z/type" 2>/dev/null) $(cat "$z/temp" 2>/dev/null)"; done
echo "@@HWMON"; for h in /sys/class/hwmon/hwmon*; do n=$(cat "$h/name" 2>/dev/null); for t in "$h"/temp*_input; do [ -r "$t" ] && echo "$n $(cat "$t" 2>/dev/null)"; done; done
echo "@@GPU"; command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=utilization.gpu,temperature.gpu --format=csv,noheader,nounits 2>/dev/null
echo "@@END"
'''

MAC_SCRIPT = r'''
echo "@@HOST"; (scutil --get ComputerName 2>/dev/null || hostname -s)
echo "@@CPU"; top -l 2 -n 0 -s 1 | grep "CPU usage" | tail -n 1
echo "@@MEM"; memory_pressure 2>/dev/null | grep -i "free percentage"
echo "@@BOOT"; sysctl -n kern.boottime
echo "@@NOW"; date +%s
echo "@@DISK"; ioreg -c IOBlockStorageDriver -r -w 0 -k Statistics 2>/dev/null | grep -o '"Bytes (Read)"=[0-9]*\|"Bytes (Write)"=[0-9]*'
echo "@@NET"; netstat -ibn
echo "@@GPU"; ioreg -r -d 1 -w 0 -c IOAccelerator 2>/dev/null | grep -o '"Device Utilization %"=[0-9]*'
echo "@@TEMP"; (smctemp -c 2>/dev/null || osx-cpu-temp 2>/dev/null)
echo "@@END"
'''

_PHYS_DISK = re.compile(r"^(sd[a-z]+|hd[a-z]+|vd[a-z]+|xvd[a-z]+|nvme\d+n\d+|mmcblk\d+)$")
_VIRTUAL_NIC = re.compile(r"^(lo|docker|veth|br-|virbr|vnet|tun|tap|wg|zt|tailscale)")
_CPU_HWMON = ("coretemp", "k10temp", "zenpower", "cpu_thermal", "soc_thermal", "cpu-thermal")
_SKIP_HWMON = ("nvme", "drivetemp", "acpitz", "iwlwifi")


class CollectError(Exception):
    pass


def _sections(text: str) -> dict:
    out, cur = {}, None
    for line in (text or "").splitlines():
        if line.startswith("@@"):
            cur = line[2:].strip()
            out[cur] = []
        elif cur is not None:
            out[cur].append(line)
    if "END" not in out:
        raise CollectError("Incomplete response (the command didn't finish)")
    return out


def _deg(raw: str):
    """Kernel temperature value → °C (most are millidegrees)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if v > 1000:
        v /= 1000.0
    return v if -20 < v < 150 else None


# ------------------------------------------------------------------
# Parsers → a "snapshot" of cumulative counters + instantaneous values
# ------------------------------------------------------------------

def parse_linux(text: str) -> dict:
    s = _sections(text)
    snap = {"os": "linux", "host": (s.get("HOST") or [""])[0].strip(), "notes": {}}

    stat = (s.get("STAT") or [""])[0].split()
    if stat and stat[0] == "cpu":
        vals = [int(v) for v in stat[1:9]]
        snap["cpu_ticks"] = (sum(vals), vals[3] + vals[4])          # (total, idle+iowait)

    mem = {}
    for line in s.get("MEM", []):
        k, _, v = line.partition(":")
        mem[k.strip()] = int(v.split()[0])
    if mem.get("MemTotal"):
        avail = mem.get("MemAvailable", mem.get("MemFree", 0) + mem.get("Buffers", 0) + mem.get("Cached", 0))
        snap["mem_pct"] = 100.0 * (1 - avail / mem["MemTotal"])

    up = (s.get("UPTIME") or [""])[0].split()
    if up:
        snap["uptime_s"] = float(up[0])

    rd = wr = 0
    disks = 0
    for line in s.get("DISK", []):
        f = line.split()
        if len(f) >= 10 and _PHYS_DISK.match(f[2]):
            rd += int(f[5]) * 512
            wr += int(f[9]) * 512
            disks += 1
    if disks:
        snap["disk_bytes"] = (rd, wr)
    else:
        snap["notes"]["disk"] = "no physical disks found in /proc/diskstats"

    phys = {l.strip() for l in s.get("NETIF", []) if l.strip()}
    rx = tx = 0
    nics = 0
    for line in s.get("NET", [])[2:]:
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        name = name.strip()
        if (phys and name not in phys) or (not phys and _VIRTUAL_NIC.match(name)):
            continue
        f = rest.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
            nics += 1
    if nics:
        snap["net_bytes"] = (rx, tx)
    else:
        snap["notes"]["net"] = "no network interfaces found"

    # CPU temperature: package sensor > CPU hwmon > CPU thermal zone > hottest zone
    zones = []
    for line in s.get("THERMAL", []):
        typ, _, val = line.rpartition(" ")
        t = _deg(val)
        if t is not None:
            zones.append((typ.strip().lower(), t))
    hw = []
    for line in s.get("HWMON", []):
        name, _, val = line.rpartition(" ")
        t = _deg(val)
        if t is not None:
            hw.append((name.strip().lower(), t))
    temp = next((t for typ, t in zones if typ == "x86_pkg_temp"), None)
    if temp is None:
        cpu_hw = [t for n, t in hw if n in _CPU_HWMON]
        temp = max(cpu_hw) if cpu_hw else None
    if temp is None:
        cpu_z = [t for typ, t in zones if "cpu" in typ or "soc" in typ]
        temp = max(cpu_z) if cpu_z else None
    if temp is None:
        other = [t for typ, t in zones if typ != "acpitz"] + [t for n, t in hw if n not in _SKIP_HWMON]
        temp = max(other) if other else None
    if temp is not None:
        snap["cpu_temp"] = temp
    else:
        snap["notes"]["cpu_temp"] = "no CPU temperature sensor exposed"

    gpu = (s.get("GPU") or [""])
    g = [p.strip() for p in (gpu[0] if gpu else "").split(",")]
    if len(g) >= 2 and g[0].replace(".", "").isdigit():
        snap["gpu_pct"] = float(g[0])
        snap["gpu_temp"] = _deg(g[1])
    else:
        snap["notes"]["gpu"] = "no GPU usage meter on this machine (needs an NVIDIA GPU with nvidia-smi)"
    return snap


def parse_mac(text: str) -> dict:
    s = _sections(text)
    snap = {"os": "mac", "host": (s.get("HOST") or [""])[0].strip(), "notes": {}}

    m = re.search(r"([\d.]+)% idle", " ".join(s.get("CPU", [])))
    if m:
        snap["cpu_pct"] = max(0.0, 100.0 - float(m.group(1)))

    m = re.search(r"(\d+)%", " ".join(s.get("MEM", [])))
    if m:
        snap["mem_pct"] = 100.0 - float(m.group(1))

    m = re.search(r"sec = (\d+)", " ".join(s.get("BOOT", [])))
    now = (s.get("NOW") or ["0"])[0].strip()
    if m and now.isdigit():
        snap["uptime_s"] = float(int(now) - int(m.group(1)))

    disk = " ".join(s.get("DISK", []))
    reads = [int(v) for v in re.findall(r'"Bytes \(Read\)"=(\d+)', disk)]
    writes = [int(v) for v in re.findall(r'"Bytes \(Write\)"=(\d+)', disk)]
    if reads or writes:
        snap["disk_bytes"] = (sum(reads), sum(writes))
    else:
        snap["notes"]["disk"] = "disk statistics unavailable"

    rx = tx = 0
    nics = 0
    for line in s.get("NET", [])[1:]:
        f = line.split()
        # Name Mtu Network [Address] Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll — Address may be blank
        if len(f) >= 10 and "<Link#" in line and re.match(r"^en\d+$", f[0].rstrip("*")):
            try:
                rx += int(f[-5])
                tx += int(f[-2])
                nics += 1
            except ValueError:
                pass
    if nics:
        snap["net_bytes"] = (rx, tx)
    else:
        snap["notes"]["net"] = "no network interfaces found"

    m = re.search(r"=(\d+)", " ".join(s.get("GPU", [])))
    if m:
        snap["gpu_pct"] = float(m.group(1))
    else:
        snap["notes"]["gpu"] = "GPU usage unavailable on this Mac"

    m = re.search(r"(-?\d+(?:\.\d+)?)", " ".join(s.get("TEMP", [])))
    if m:
        snap["cpu_temp"] = float(m.group(1))
    else:
        note = "macOS only shares temperatures with admin tools — install `smctemp` (Homebrew) to show them"
        snap["notes"]["cpu_temp"] = note
        snap["notes"]["gpu_temp"] = note
    return snap


# ------------------------------------------------------------------
# Collector: one per connection, turns snapshots into rates
# ------------------------------------------------------------------

class Collector:
    def __init__(self, conn: dict):
        self.conn = dict(conn)
        self._os = None
        self._prev = None            # (time, snapshot)
        self._lock = threading.Lock()

    @property
    def is_local(self) -> bool:
        return (self.conn.get("type") or "local") == "local"

    def _run(self, cmd: str, stdin: str = "", timeout: float = 15) -> str:
        if self.is_local:
            try:
                r = subprocess.run(["sh", "-c", cmd], input=stdin, capture_output=True, text=True, timeout=timeout)
            except subprocess.TimeoutExpired:
                raise CollectError("Timed out reading local stats")
            return r.stdout
        try:
            return ssh_remote.run(self.conn.get("host", ""), self.conn.get("user", ""), cmd,
                                  stdin=stdin, port=int(self.conn.get("port") or 22), timeout=timeout)
        except ssh_remote.SSHError as e:
            raise CollectError(str(e))

    def _detect_os(self) -> str:
        if self._os is None:
            uname = (platform.system() if self.is_local else self._run("uname -s", timeout=8)).strip()
            if uname.startswith("Darwin"):
                self._os = "mac"
            elif uname.startswith("Linux"):
                self._os = "linux"
            else:
                raise CollectError(f"Unsupported system: {uname or 'unknown'} (Linux and macOS are supported)")
        return self._os

    def sample(self) -> dict:
        """One poll. Returns current stats; rates are None until a second poll."""
        with self._lock:
            os_kind = self._detect_os()
            script = MAC_SCRIPT if os_kind == "mac" else LINUX_SCRIPT
            text = self._run("sh -s", stdin=script)
            snap = parse_mac(text) if os_kind == "mac" else parse_linux(text)
            now = time.monotonic()
            prev = self._prev
            self._prev = (now, snap)
            return compute(snap, prev[1] if prev else None, (now - prev[0]) if prev else None)


def compute(snap: dict, prev: dict = None, dt: float = None) -> dict:
    """Snapshot (+ previous snapshot) → display values."""
    out = {
        "host": snap.get("host") or "",
        "os": snap.get("os"),
        "cpu_pct": snap.get("cpu_pct"),
        "cpu_temp": snap.get("cpu_temp"),
        "gpu_pct": snap.get("gpu_pct"),
        "gpu_temp": snap.get("gpu_temp"),
        "mem_pct": snap.get("mem_pct"),
        "uptime_s": snap.get("uptime_s"),
        "disk_r": None, "disk_w": None, "net_rx": None, "net_tx": None,
        "notes": dict(snap.get("notes") or {}),
        "warming_up": prev is None,
    }
    if prev and dt and dt > 0:
        if "cpu_ticks" in snap and "cpu_ticks" in prev:
            dtot = snap["cpu_ticks"][0] - prev["cpu_ticks"][0]
            didle = snap["cpu_ticks"][1] - prev["cpu_ticks"][1]
            if dtot > 0:
                out["cpu_pct"] = max(0.0, min(100.0, 100.0 * (1 - didle / dtot)))
        for key, (a, b) in (("disk_bytes", ("disk_r", "disk_w")), ("net_bytes", ("net_rx", "net_tx"))):
            if key in snap and key in prev:
                # Counters can reset (reboot, driver reload) — never show a negative rate
                out[a] = max(0.0, (snap[key][0] - prev[key][0]) / dt)
                out[b] = max(0.0, (snap[key][1] - prev[key][1]) / dt)
    return out
