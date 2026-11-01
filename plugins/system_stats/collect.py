"""
System stats collection — this Pi (or the Mac/PC the desktop app runs on), or
any Linux/macOS machine over SSH.

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
Windows (local only — the desktop app on a PC):
  psutil (CPU times, memory, boot time, disk and network byte counters, NICs
  that aren't loopback/virtual), the "GPU Engine" performance counters through
  pdh.dll (busiest engine, as Task Manager shows it), and nvidia-smi for GPU
  temperature if present. Windows doesn't share CPU temperature with apps.
"""

import logging
import platform
import re
import shutil
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


_WIN_VIRTUAL_NIC = re.compile(r"loopback|pseudo|vethernet|virtualbox|vmware|hyper-v|teredo|isatap|6to4|"
                              r"bluetooth|tailscale|zerotier|wireguard|vpn|tap-", re.I)


class _WinGpu:
    """GPU busy % from the "GPU Engine" counters (pdh.dll), as Task Manager shows it:
    each engine's utilisation summed over processes, then the busiest engine.
    The query stays open, so each read averages over the time since the last."""

    PDH_FMT_DOUBLE, PDH_FMT_NOCAP100, PDH_MORE_DATA = 0x200, 0x8000, 0x800007D2

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class FmtValue(ctypes.Structure):           # PDH_FMT_COUNTERVALUE (union read as double)
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        class FmtItem(ctypes.Structure):            # PDH_FMT_COUNTERVALUE_ITEM_W
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", FmtValue)]

        self._ct, self._wt, self._Item = ctypes, wintypes, FmtItem
        self._pdh = ctypes.WinDLL("pdh")
        self._query, self._counter = wintypes.HANDLE(), wintypes.HANDLE()
        if self._pdh.PdhOpenQueryW(None, None, ctypes.byref(self._query)):
            raise OSError("PdhOpenQuery failed")
        if self._pdh.PdhAddEnglishCounterW(self._query, r"\GPU Engine(*)\Utilization Percentage",
                                           None, ctypes.byref(self._counter)):
            self._pdh.PdhCloseQuery(self._query)
            raise OSError("no GPU Engine counters")
        self._pdh.PdhCollectQueryData(self._query)  # rates need two collections

    def read(self):
        ct, wt = self._ct, self._wt
        if self._pdh.PdhCollectQueryData(self._query):
            return None
        size, count = wt.DWORD(0), wt.DWORD(0)
        fmt = self.PDH_FMT_DOUBLE | self.PDH_FMT_NOCAP100
        st = self._pdh.PdhGetFormattedCounterArrayW(self._counter, fmt, ct.byref(size), ct.byref(count), None)
        if (st & 0xFFFFFFFF) != self.PDH_MORE_DATA or not size.value:
            return None
        buf = ct.create_string_buffer(size.value)
        if self._pdh.PdhGetFormattedCounterArrayW(self._counter, fmt, ct.byref(size), ct.byref(count), buf):
            return None
        items = (self._Item * count.value).from_buffer(buf)
        engines = {}
        for it in items:
            if it.FmtValue.CStatus > 1:              # not PDH_CSTATUS_VALID_DATA / NEW_DATA
                continue
            name = it.szName or ""
            key = name[name.find("luid_"):] if "luid_" in name else name   # one engine, all processes
            engines[key] = engines.get(key, 0.0) + it.FmtValue.doubleValue
        return max(0.0, min(100.0, max(engines.values()))) if engines else None


def _nvidia_smi():
    """(utilisation %, temperature °C) from nvidia-smi, or None."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "--query-gpu=utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"],
                           capture_output=True, text=True, timeout=8,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        g = [p.strip() for p in r.stdout.splitlines()[0].split(",")]
        return float(g[0]), _deg(g[1])
    except Exception:
        return None


def sample_windows(gpu=None) -> dict:
    """A snapshot of this PC (same shape as parse_linux/parse_mac)."""
    try:
        import psutil
    except ImportError:
        raise CollectError("Stats for this PC need the psutil package")
    snap = {"os": "windows", "host": platform.node(), "notes": {}}

    t = psutil.cpu_times()
    snap["cpu_ticks"] = (t.user + t.system + t.idle, t.idle)
    snap["mem_pct"] = float(psutil.virtual_memory().percent)
    snap["uptime_s"] = max(0.0, time.time() - psutil.boot_time())

    try:
        d = psutil.disk_io_counters()
    except Exception:
        d = None
    if d:
        snap["disk_bytes"] = (d.read_bytes, d.write_bytes)
    else:
        snap["notes"]["disk"] = "disk statistics unavailable (run `diskperf -y` as admin to turn them on)"

    try:
        nics = psutil.net_io_counters(pernic=True)
        up = {n for n, st in psutil.net_if_stats().items() if st.isup}
    except Exception:
        nics, up = {}, set()
    real = [c for n, c in nics.items() if n in up and not _WIN_VIRTUAL_NIC.search(n)]
    if real:
        snap["net_bytes"] = (sum(c.bytes_recv for c in real), sum(c.bytes_sent for c in real))
    else:
        snap["notes"]["net"] = "no network interfaces found"

    nv = _nvidia_smi()
    pct = gpu.read() if gpu else None
    if pct is None and nv:
        pct = nv[0]
    if pct is not None:
        snap["gpu_pct"] = pct
    else:
        snap["notes"]["gpu"] = "GPU usage unavailable on this PC"
    if nv and nv[1] is not None:
        snap["gpu_temp"] = nv[1]
    else:
        snap["notes"]["gpu_temp"] = "Windows only shares GPU temperature through NVIDIA's tools"
    snap["notes"]["cpu_temp"] = "Windows doesn't share CPU temperature with apps"
    return snap


# ------------------------------------------------------------------
# Collector: one per connection, turns snapshots into rates
# ------------------------------------------------------------------

class Collector:
    def __init__(self, conn: dict):
        self.conn = dict(conn)
        self._os = None
        self._prev = None            # (time, snapshot)
        self._gpu = None             # _WinGpu, for this PC on Windows
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
            if self.is_local and platform.system() == "Windows":
                self._os = "windows"
                try:
                    self._gpu = _WinGpu()
                except Exception as e:
                    log.info(f"[system_stats] GPU counters unavailable: {e}")
                return self._os
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
            if os_kind == "windows":
                snap = sample_windows(self._gpu)
            else:
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
