#!/usr/bin/env bash
# install.sh — FlightClock LED Matrix display installer
#
# Usage:
#   sudo bash install.sh [--repo-url <git-url>] [--repo-dir <path>]
#
# Installs all system packages, clones/updates the repo, creates a Python
# venv, installs the rgbmatrix C extension, sets up the systemd service,
# and optionally writes initial config values.
#
# Idempotent — safe to run again on an existing install.

set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
err()     { echo -e "${RED}[ERROR]${RESET} $*" >&2; }
die()     { err "$*"; exit 1; }

# ── Preflight ─────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run with sudo: sudo bash install.sh"

INSTALL_USER="${SUDO_USER:-}"
if [[ -z "$INSTALL_USER" ]]; then
    read -rp "Install for which user? " INSTALL_USER
fi
[[ -z "$INSTALL_USER" ]] && die "Cannot determine target user"
id "$INSTALL_USER" &>/dev/null || die "User '$INSTALL_USER' does not exist"

INSTALL_HOME=$(getent passwd "$INSTALL_USER" | cut -d: -f6)
INSTALL_UID=$(id -u "$INSTALL_USER")

# Allow overrides via environment or flags
REPO_DIR="${REPO_DIR:-${INSTALL_HOME}/its-a-plane-python}"
REPO_URL="${REPO_URL:-https://github.com/RoyMan0/flight-clock-and-more.git}"
VENV="${VENV:-${INSTALL_HOME}/venv}"
SERVICE_NAME="its-a-plane"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
SUDOERS_FILE="/etc/sudoers.d/${SERVICE_NAME}"

# Parse simple flags
while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo-url) REPO_URL="$2"; shift 2 ;;
        --repo-dir) REPO_DIR="$2"; shift 2 ;;
        *) die "Unknown flag: $1" ;;
    esac
done

echo -e "\n${BOLD}FlightClock LED Matrix Installer${RESET}"
echo    "  Target user : $INSTALL_USER (uid $INSTALL_UID, $INSTALL_HOME)"
echo    "  Repo dir    : $REPO_DIR"
echo    "  Venv        : $VENV"
echo

# ── Phase 1: System packages ──────────────────────────────────────
info "Phase 1: Installing system packages…"
apt-get update -q
apt-get install -y -q \
    git \
    python3-venv \
    python3-dev \
    python3-setuptools \
    python3-pil \
    build-essential \
    libglib2.0-dev \
    cmake \
    ninja-build \
    network-manager \
    dnsmasq \
    iptables \
    fonts-liberation \
    dphys-swapfile \
    curl

# NetworkManager must be running; dnsmasq should NOT run at system level
# (we start it on-demand during setup mode only)
systemctl enable --now NetworkManager 2>/dev/null || true
systemctl disable --now dnsmasq 2>/dev/null || true

success "System packages installed"

# ── Phase 1b: Swap — ensure enough memory for C++ compile ────────
info "Phase 1b: Checking swap…"
_total_mb=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
if [[ $_total_mb -lt 900 ]]; then
    info "  Low RAM detected (${_total_mb}MB) — ensuring 512MB swap for rgbmatrix build…"
    dphys-swapfile swapoff 2>/dev/null || true
    sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
    grep -q "^CONF_SWAPSIZE=" /etc/dphys-swapfile || echo "CONF_SWAPSIZE=512" >> /etc/dphys-swapfile
    dphys-swapfile setup
    dphys-swapfile swapon
    success "Swap set to 512MB"
else
    success "Sufficient RAM (${_total_mb}MB) — swap unchanged"
fi

# ── Phase 1c: Boot config for Adafruit RGB Matrix Bonnet ─────────
BOOT_CFG=""
for _f in /boot/firmware/config.txt /boot/config.txt; do
    [[ -f "$_f" ]] && BOOT_CFG="$_f" && break
done

if [[ -n "$BOOT_CFG" ]]; then
    info "Phase 1c: Configuring boot settings for RGB Matrix Bonnet ($BOOT_CFG)…"
    echo
    echo -e "  ${BOLD}Did you solder the PWM jumper bridge on the bonnet?${RESET}"
    echo    "  Quality    (Y) — jumper soldered: less flicker, but disables onboard audio"
    echo    "  Convenience(N) — no jumper:       audio works, slightly more flicker  [default]"
    echo -n "  Soldered jumper? [y/N] "
    read -r _pwm_ans
    _pwm_mode=false
    [[ "${_pwm_ans:-n}" =~ ^[Yy] ]] && _pwm_mode=true
    echo

    # Disable Bluetooth on all configurations (frees UART/GPIO for matrix)
    if ! grep -qF "dtoverlay=disable-bt" "$BOOT_CFG"; then
        echo "dtoverlay=disable-bt" >> "$BOOT_CFG"
        info "  Added: dtoverlay=disable-bt"
    fi

    if $_pwm_mode; then
        # Quality mode: audio must be off — GPIO 18 is shared with PWM
        if ! grep -qF "dtparam=audio=off" "$BOOT_CFG"; then
            echo "dtparam=audio=off" >> "$BOOT_CFG"
            info "  Added: dtparam=audio=off (PWM/Quality mode)"
        fi
        HAT_PWM_ENABLED=true
        success "Boot config set for Quality mode (PWM enabled)"
    else
        HAT_PWM_ENABLED=false
        success "Boot config set for Convenience mode (audio preserved)"
    fi
else
    warn "Phase 1c: Could not find /boot/firmware/config.txt or /boot/config.txt — skipping boot config"
    HAT_PWM_ENABLED=false
fi

# Allow the app user to write captive-portal config into NM's dnsmasq dir
# NM reads /etc/NetworkManager/dnsmasq-shared.d/ when starting a hotspot.
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
chown "${INSTALL_USER}:${INSTALL_USER}" /etc/NetworkManager/dnsmasq-shared.d

# ── Phase 2: Clone / update repo ──────────────────────────────────
info "Phase 2: Setting up repository…"
if [[ -d "${REPO_DIR}/.git" ]]; then
    info "  Repo already present — pulling latest…"
    sudo -u "$INSTALL_USER" git -C "$REPO_DIR" pull --ff-only || warn "  git pull failed (local changes?)"
elif [[ -d "$REPO_DIR" ]]; then
    warn "  $REPO_DIR exists but is not a git repo — backing it up and cloning fresh…"
    mv "$REPO_DIR" "${REPO_DIR}.bak.$(date +%Y%m%d%H%M%S)"
    sudo -u "$INSTALL_USER" git clone "$REPO_URL" "$REPO_DIR"
else
    info "  Cloning $REPO_URL …"
    sudo -u "$INSTALL_USER" git clone "$REPO_URL" "$REPO_DIR"
fi
success "Repository ready at $REPO_DIR"

# ── Phase 3: Python venv + pip deps ───────────────────────────────
info "Phase 3: Setting up Python environment…"
if [[ ! -d "$VENV" ]]; then
    info "  Creating virtual environment…"
    sudo -u "$INSTALL_USER" python3 -m venv "$VENV"
fi
info "  Upgrading pip…"
sudo -u "$INSTALL_USER" "${VENV}/bin/pip" install --quiet --upgrade pip
if [[ -f "${REPO_DIR}/requirements.txt" ]]; then
    info "  Installing Python dependencies…"
    sudo -u "$INSTALL_USER" "${VENV}/bin/pip" install --quiet -r "${REPO_DIR}/requirements.txt"
else
    warn "  requirements.txt not found — skipping pip install"
fi
success "Python environment ready"

# ── Phase 4: rgbmatrix C extension ───────────────────────────────
info "Phase 4: Building rgbmatrix LED library…"

# The C++ compile takes 3–10 minutes depending on Pi model. If this installer
# is running over SSH without screen/tmux, a dropped connection will kill the
# build mid-way and can corrupt the filesystem on a hard power cycle.
if [[ -z "${STY:-}" && -z "${TMUX:-}" && -t 0 ]]; then
    echo
    warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    warn "  You are running over SSH without screen or tmux."
    warn "  If the connection drops during the rgbmatrix build, the"
    warn "  installer will be killed and may leave the Pi in a bad state."
    warn ""
    warn "  Recommended: Ctrl+C now, then re-run inside screen:"
    warn "    screen -S install"
    warn "    sudo bash install.sh"
    warn "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo -n "  Continue anyway? [y/N] "
    read -r _ssh_ans
    [[ "${_ssh_ans:-n}" =~ ^[Yy] ]] || die "Aborted. Re-run inside screen: screen -S install && sudo bash install.sh"
    echo
fi

MATRIX_SRC="${INSTALL_HOME}/rpi-rgb-led-matrix"

# If the directory exists, verify it is a valid rpi-rgb-led-matrix git repo.
# A stale or incomplete clone (e.g. from a previous install) will fail the pip
# build with "Neither setup.py nor pyproject.toml found" — re-clone in that case.
_matrix_valid=false
if [[ -d "$MATRIX_SRC" ]]; then
    _remote=$(sudo -u "$INSTALL_USER" git -C "$MATRIX_SRC" remote get-url origin 2>/dev/null || true)
    if echo "$_remote" | grep -q "rpi-rgb-led-matrix"; then
        info "  rpi-rgb-led-matrix already cloned — pulling…"
        if sudo -u "$INSTALL_USER" git -C "$MATRIX_SRC" pull --ff-only --quiet; then
            _matrix_valid=true
        else
            warn "  git pull failed — re-cloning from scratch…"
        fi
    else
        warn "  Existing directory is not the rpi-rgb-led-matrix repo — re-cloning…"
    fi
    $_matrix_valid || rm -rf "$MATRIX_SRC"
fi
if ! $_matrix_valid; then
    info "  Cloning rpi-rgb-led-matrix…"
    sudo -u "$INSTALL_USER" git clone --quiet https://github.com/hzeller/rpi-rgb-led-matrix.git "$MATRIX_SRC"
fi

# pip install expects pyproject.toml or setup.py at the path root
if [[ ! -f "$MATRIX_SRC/pyproject.toml" && ! -f "$MATRIX_SRC/setup.py" ]]; then
    warn "  pyproject.toml not found at repo root — trying bindings/python…"
    MATRIX_SRC="${MATRIX_SRC}/bindings/python"
fi

info "  Installing build dependencies for rgbmatrix…"
sudo -u "$INSTALL_USER" "${VENV}/bin/pip" install --quiet scikit-build-core cython

# The rgbmatrix Pillow shim (shims/pillow.c) needs Imaging.h. Pillow>=10
# removed this header from the public API, and Pillow<10 doesn't support
# Python 3.13+. Solution: use the system python3-pil package (already
# installed in Phase 1) which ships the header, then symlink it into the
# Python include dir if the compiler can't find it there already.
_py_ver=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
_imaging_h=$(find /usr -name "Imaging.h" 2>/dev/null | head -1)
if [[ -n "$_imaging_h" ]]; then
    _py_inc="/usr/include/python${_py_ver}"
    if [[ ! -f "${_py_inc}/Imaging.h" ]]; then
        info "  Symlinking Imaging.h into ${_py_inc}/…"
        ln -sf "$_imaging_h" "${_py_inc}/Imaging.h"
    fi
    info "  Imaging.h found at $_imaging_h"
else
    warn "  Imaging.h not found — rgbmatrix Pillow shim may fail to compile"
fi

# Install current Pillow in the venv for runtime use
sudo -u "$INSTALL_USER" "${VENV}/bin/pip" install --quiet Pillow
info "  Building Python binding (this takes ~3–10 minutes on a Pi)…"
if MAX_JOBS=1 "${VENV}/bin/pip" install --no-build-isolation "${MATRIX_SRC}"; then
    success "rgbmatrix installed"
else
    warn "rgbmatrix build failed — you can still run with --no-hardware, or retry later"
fi

# ── Phase 5: Bootstrap config files ───────────────────────────────
info "Phase 5: Bootstrapping config files…"
CFG_DIR="${REPO_DIR}/config"
if [[ ! -f "${CFG_DIR}/config.json" && -f "${CFG_DIR}/config.example.json" ]]; then
    sudo -u "$INSTALL_USER" cp "${CFG_DIR}/config.example.json" "${CFG_DIR}/config.json"
    info "  Created config.json from example"
fi
if [[ ! -f "${CFG_DIR}/secrets.json" && -f "${CFG_DIR}/secrets.example.json" ]]; then
    sudo -u "$INSTALL_USER" cp "${CFG_DIR}/secrets.example.json" "${CFG_DIR}/secrets.json"
    info "  Created secrets.json from example"
fi
chown -R "${INSTALL_USER}:${INSTALL_USER}" "${CFG_DIR}"
success "Config files ready"

# ── Phase 5b: Migrate settings from old installation ─────────────
MIGRATED_JSON="/tmp/flightclock_migrated.json"
rm -f "$MIGRATED_JSON"

# Find the most recent backup of the old (non-git) installation
BACKUP_DIR=""
if compgen -G "${REPO_DIR}.bak."* > /dev/null 2>&1; then
    BACKUP_DIR=$(ls -dt "${REPO_DIR}.bak."* 2>/dev/null | head -1)
fi

if [[ -n "$BACKUP_DIR" ]]; then
    info "Phase 5b: Old installation found at $BACKUP_DIR — migrating settings…"
    "${VENV}/bin/python3" - "$BACKUP_DIR" "${CFG_DIR}/config.json" "${CFG_DIR}/secrets.json" "$MIGRATED_JSON" <<'PYEOF'
import json, os, sys

backup_dir = sys.argv[1]
cfg_path   = sys.argv[2]
sec_path   = sys.argv[3]
out_path   = sys.argv[4]

def load_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}

def save_json(path, data):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

cfg     = load_json(cfg_path)
sec     = load_json(sec_path)
summary = {}

# ── Old format: config.py with bare Python variable assignments ───
old_config_py = os.path.join(backup_dir, 'config.py')
if os.path.exists(old_config_py):
    print(f"  Detected old-format config.py")
    try:
        ns = {}
        with open(old_config_py, encoding='utf-8') as f:
            exec(f.read(), {"__builtins__": {}}, ns)

        loc  = cfg.setdefault('location', {})
        disp = cfg.setdefault('display', {})
        flt  = cfg.setdefault('flights', {})
        ft   = cfg.setdefault('plugins', {}).setdefault('flight_tracker', {})
        cw   = cfg['plugins'].setdefault('clock_weather', {})

        if 'LOCATION_HOME' in ns:
            loc['location_home'] = list(ns['LOCATION_HOME'])
            summary['location'] = True
            print(f"    Location: {list(ns['LOCATION_HOME'])}")
        if 'TEMPERATURE_LOCATION' in ns:
            loc['temperature_location'] = str(ns['TEMPERATURE_LOCATION'])
        if 'ZONE_HOME' in ns:
            loc['zone_home'] = dict(ns['ZONE_HOME'])
        if 'TEMPERATURE_UNITS' in ns:
            loc['units'] = 'imperial' if ns['TEMPERATURE_UNITS'] == 'imperial' else 'metric'
        if 'CLOCK_FORMAT' in ns:
            loc['clock_format'] = str(ns['CLOCK_FORMAT'])
            summary['clock_format'] = str(ns['CLOCK_FORMAT'])
            print(f"    Clock: {ns['CLOCK_FORMAT']}")
        if 'JOURNEY_CODE_SELECTED' in ns:
            loc['journey_code'] = str(ns['JOURNEY_CODE_SELECTED'])
        if 'JOURNEY_BLANK_FILLER' in ns:
            loc['journey_blank_filler'] = str(ns['JOURNEY_BLANK_FILLER'])

        if 'BRIGHTNESS' in ns:
            disp['brightness'] = int(ns['BRIGHTNESS'])
            summary['brightness'] = int(ns['BRIGHTNESS'])
            print(f"    Brightness: {ns['BRIGHTNESS']}")
        if 'BRIGHTNESS_NIGHT' in ns:
            disp['brightness_night'] = int(ns['BRIGHTNESS_NIGHT'])
        if 'NIGHT_BRIGHTNESS' in ns:
            disp['night_brightness'] = bool(ns['NIGHT_BRIGHTNESS'])
        if 'NIGHT_START' in ns:
            disp['night_start'] = str(ns['NIGHT_START'])
        if 'NIGHT_END' in ns:
            disp['night_end'] = str(ns['NIGHT_END'])
        if 'GPIO_SLOWDOWN' in ns:
            disp['gpio_slowdown'] = int(ns['GPIO_SLOWDOWN'])
        if 'HAT_PWM_ENABLED' in ns:
            disp['hat_pwm_enabled'] = bool(ns['HAT_PWM_ENABLED'])
        if 'FORECAST_DAYS' in ns:
            cw['forecast_days']   = int(ns['FORECAST_DAYS'])

        if 'MIN_ALTITUDE' in ns:
            ft['min_altitude'] = int(ns['MIN_ALTITUDE'])
            summary['min_altitude'] = int(ns['MIN_ALTITUDE'])
            print(f"    Min altitude: {ns['MIN_ALTITUDE']} ft")
        if 'MAX_FARTHEST' in ns:
            flt['max_farthest'] = int(ns['MAX_FARTHEST'])
        if 'MAX_CLOSEST' in ns:
            flt['max_closest'] = int(ns['MAX_CLOSEST'])
        if 'EMAIL' in ns and ns['EMAIL']:
            flt['email'] = str(ns['EMAIL'])
            summary['email'] = str(ns['EMAIL'])
            print(f"    Email: {ns['EMAIL']}")

        for key, dest in [('TOMORROW_API_KEY', 'tomorrow_api_key'),
                          ('AIRLABS_API_KEY',  None),
                          ('FLIGHTAWARE_API_KEY', None)]:
            val = ns.get(key, '')
            if val:
                if key == 'TOMORROW_API_KEY':
                    sec['tomorrow_api_key'] = str(val)
                    summary['tomorrow_api_key'] = True
                    print(f"    Tomorrow.io API key: found")
                elif key == 'AIRLABS_API_KEY':
                    sec.setdefault('airlabs_api_keys', [str(val)])
                    summary['airlabs_api_key'] = True
                    print(f"    AirLabs API key: found")
                elif key == 'FLIGHTAWARE_API_KEY':
                    sec.setdefault('flightaware_api_keys', [str(val)])
                    summary['flightaware_api_key'] = True
                    print(f"    FlightAware API key: found")
        # list variants
        if ns.get('AIRLABS_API_KEYS') and not summary.get('airlabs_api_key'):
            sec['airlabs_api_keys'] = list(ns['AIRLABS_API_KEYS'])
            summary['airlabs_api_key'] = True
            print(f"    AirLabs API key: found")
        if ns.get('FLIGHTAWARE_API_KEYS') and not summary.get('flightaware_api_key'):
            sec['flightaware_api_keys'] = list(ns['FLIGHTAWARE_API_KEYS'])
            summary['flightaware_api_key'] = True
            print(f"    FlightAware API key: found")

    except Exception as e:
        print(f"  Warning: could not parse config.py: {e}", file=sys.stderr)

# ── New format: config/config.json + config/secrets.json ─────────
old_cfg_json = os.path.join(backup_dir, 'config', 'config.json')
old_sec_json = os.path.join(backup_dir, 'config', 'secrets.json')

if os.path.exists(old_cfg_json) and not os.path.exists(old_config_py):
    print(f"  Detected new-format config/config.json")
    old_cfg = load_json(old_cfg_json)
    for section in ('location', 'display', 'flights'):
        if old_cfg.get(section):
            cfg.setdefault(section, {}).update(old_cfg[section])
            summary[section] = True
            print(f"    Copied [{section}] settings")

if os.path.exists(old_sec_json):
    print(f"  Detected config/secrets.json")
    old_sec = load_json(old_sec_json)
    if old_sec.get('tomorrow_api_key'):
        sec['tomorrow_api_key'] = old_sec['tomorrow_api_key']
        summary['tomorrow_api_key'] = True
        print(f"    Tomorrow.io API key: found")
    if old_sec.get('airlabs_api_keys'):
        sec['airlabs_api_keys'] = old_sec['airlabs_api_keys']
        summary['airlabs_api_key'] = True
        print(f"    AirLabs API key: found")
    if old_sec.get('flightaware_api_keys'):
        sec['flightaware_api_keys'] = old_sec['flightaware_api_keys']
        summary['flightaware_api_key'] = True
        print(f"    FlightAware API key: found")

save_json(cfg_path, cfg)
save_json(sec_path, sec)
save_json(out_path, summary)

count = len(summary)
print(f"  Migration: {count} setting group(s) carried over")
PYEOF
    if [[ -f "$MIGRATED_JSON" ]]; then
        success "Phase 5b: Settings migration complete"
    else
        warn "Phase 5b: Migration script did not produce output — configs unchanged"
    fi
else
    info "Phase 5b: No old installation backup found — skipping migration"
    echo '{}' > "$MIGRATED_JSON"
fi

# ── Phase 6: Sudoers ──────────────────────────────────────────────
info "Phase 6: Installing sudoers rules…"
SUDOERS_TMP=$(mktemp)
cat > "$SUDOERS_TMP" <<SUDOERS_EOF
# FlightClock — allow LED matrix service to manage network and system
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/bin/nmcli
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart ${SERVICE_NAME}
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/bin/systemctl stop ${SERVICE_NAME}
${INSTALL_USER} ALL=(ALL) NOPASSWD: /sbin/reboot
${INSTALL_USER} ALL=(ALL) NOPASSWD: /sbin/shutdown
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/sbin/nft
${INSTALL_USER} ALL=(ALL) NOPASSWD: /sbin/iptables
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/sbin/iptables
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/sbin/dnsmasq
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/bin/timedatectl
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/bin/tee /etc/NetworkManager/dnsmasq-shared.d/flightclock-captive.conf
SUDOERS_EOF
if visudo -c -f "$SUDOERS_TMP" &>/dev/null; then
    cp "$SUDOERS_TMP" "$SUDOERS_FILE"
    chmod 440 "$SUDOERS_FILE"
    success "Sudoers installed at $SUDOERS_FILE"
else
    err "Sudoers validation failed — skipping (add rules manually)"
fi
rm -f "$SUDOERS_TMP"

# ── Phase 7: Systemd service ──────────────────────────────────────
info "Phase 7: Installing systemd service…"
TEMPLATE="${REPO_DIR}/utilities/its-a-plane.service.template"
if [[ -f "$TEMPLATE" ]]; then
    sed \
        -e "s|__USER__|${INSTALL_USER}|g" \
        -e "s|__HOME__|${INSTALL_HOME}|g" \
        -e "s|__REPO_DIR__|${REPO_DIR}|g" \
        -e "s|__UID__|${INSTALL_UID}|g" \
        "$TEMPLATE" > "$SERVICE_FILE"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    success "Service installed: $SERVICE_FILE"
else
    warn "Service template not found at $TEMPLATE — skipping"
fi

# ── Phase 8: Journal log limits ──────────────────────────────────
info "Phase 8: Configuring journald log limits…"
mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/its-a-plane.conf << 'JOURNAL_EOF'
[Journal]
SystemMaxUse=50M
MaxRetentionSec=1month
JOURNAL_EOF
systemctl restart systemd-journald 2>/dev/null || true
success "Journal capped at 50 MB / 1 month"

# ── Phase 9: File permissions ─────────────────────────────────────
info "Phase 9: Setting file permissions…"
chown -R "${INSTALL_USER}:${INSTALL_USER}" "$REPO_DIR"
chown -R "${INSTALL_USER}:${INSTALL_USER}" "$VENV"
chown -R "${INSTALL_USER}:${INSTALL_USER}" "$MATRIX_SRC" 2>/dev/null || true
chmod +x "${REPO_DIR}/main.py" 2>/dev/null || true
success "Permissions set"

# ── Phase 10: Optional interactive config ─────────────────────────
echo
echo -e "${BOLD}Phase 10: Initial configuration (Enter to skip — configure via web UI later)${RESET}"
echo

CFG_JSON="${CFG_DIR}/config.json"
SEC_JSON="${CFG_DIR}/secrets.json"

# Check what the migration phase already handled
_migrated() { python3 -c "import json; d=json.load(open('$MIGRATED_JSON')); print('yes' if d.get('$1') else '')" 2>/dev/null; }
M_LOC=$(_migrated location)
M_TM=$(_migrated tomorrow_api_key)
M_AL=$(_migrated airlabs_api_key)
M_FA=$(_migrated flightaware_api_key)

INPUT_LAT=""; INPUT_LON=""
if [[ -n "$M_LOC" ]]; then
    success "  Location migrated from old installation — skipping prompt"
else
    read -rp "  Home latitude  (e.g. 40.7128, blank to skip): " INPUT_LAT
    read -rp "  Home longitude (e.g. -74.006, blank to skip): " INPUT_LON
fi

INPUT_TM=""
if [[ -n "$M_TM" ]]; then
    success "  Tomorrow.io API key migrated — skipping prompt"
else
    read -rsp "  Tomorrow.io API key    (blank to skip): " INPUT_TM; echo
fi

INPUT_AL=""
if [[ -n "$M_AL" ]]; then
    success "  AirLabs API key migrated — skipping prompt"
else
    read -rsp "  AirLabs API key        (blank to skip): " INPUT_AL; echo
fi

INPUT_FA=""
if [[ -n "$M_FA" ]]; then
    success "  FlightAware API key migrated — skipping prompt"
else
    read -rsp "  FlightAware API key    (blank to skip): " INPUT_FA; echo
fi

if [[ -n "$INPUT_LAT" && -n "$INPUT_LON" ]] || [[ -n "${HAT_PWM_ENABLED:-}" ]]; then
    info "  Writing config…"
    sudo -u "$INSTALL_USER" env LAT="$INPUT_LAT" LON="$INPUT_LON" HAT_PWM="${HAT_PWM_ENABLED:-false}" \
    "${VENV}/bin/python3" - "$CFG_JSON" <<'PYEOF'
import json, os, sys
path = sys.argv[1]
with open(path) as f:
    cfg = json.load(f)
lat = os.environ.get('LAT', '').strip()
lon = os.environ.get('LON', '').strip()
if lat and lon:
    cfg.setdefault('location', {})['location_home'] = [float(lat), float(lon)]
hat_pwm = os.environ.get('HAT_PWM', 'false').lower() == 'true'
cfg.setdefault('display', {})['hat_pwm_enabled'] = hat_pwm
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2)
PYEOF
fi

if [[ -n "$INPUT_TM" || -n "$INPUT_AL" || -n "$INPUT_FA" ]]; then
    info "  Writing API keys…"
    sudo -u "$INSTALL_USER" env TM="$INPUT_TM" AL="$INPUT_AL" FA="$INPUT_FA" \
    "${VENV}/bin/python3" - "$SEC_JSON" <<'PYEOF'
import json, os, sys
path = sys.argv[1]
with open(path) as f:
    sec = json.load(f)
tm = os.environ.get('TM', '').strip()
al = os.environ.get('AL', '').strip()
fa = os.environ.get('FA', '').strip()
if tm: sec['tomorrow_api_key'] = tm
if al: sec['airlabs_api_keys'] = [al]
if fa: sec['flightaware_api_keys'] = [fa]
with open(path, 'w') as f:
    json.dump(sec, f, indent=2)
PYEOF
fi

# ── Done ──────────────────────────────────────────────────────────
PI_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "your-pi-ip")

echo
echo -e "${GREEN}${BOLD}Installation complete!${RESET}"
echo
echo    "  Start:         sudo systemctl start ${SERVICE_NAME}"
echo    "  Status/logs:   journalctl -u ${SERVICE_NAME} -f"
echo    "  Web dashboard: http://${PI_IP}:8080"
echo    "  Dev mode:      ${VENV}/bin/python3 ${REPO_DIR}/main.py --no-hardware"
echo
echo -e "${YELLOW}First boot:${RESET} If the Pi can't find a Wi-Fi network it will broadcast"
echo    "  a hotspot called 'FltClk'. Scan the QR code on the display with your"
echo    "  phone to join and open the setup wizard."
echo
