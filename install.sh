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
REPO_URL="${REPO_URL:-https://github.com/RoyMan0/its-a-plane-python.git}"
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
    build-essential \
    libglib2.0-dev \
    network-manager \
    dnsmasq \
    iptables \
    fonts-liberation \
    curl

# NetworkManager must be running; dnsmasq should NOT run at system level
# (we start it on-demand during setup mode only)
systemctl enable --now NetworkManager 2>/dev/null || true
systemctl disable --now dnsmasq 2>/dev/null || true

success "System packages installed"

# Allow the app user to write captive-portal config into NM's dnsmasq dir
# NM reads /etc/NetworkManager/dnsmasq-shared.d/ when starting a hotspot.
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
chown "${INSTALL_USER}:${INSTALL_USER}" /etc/NetworkManager/dnsmasq-shared.d

# ── Phase 2: Clone / update repo ──────────────────────────────────
info "Phase 2: Setting up repository…"
if [[ -d "${REPO_DIR}/.git" ]]; then
    info "  Repo already present — pulling latest…"
    sudo -u "$INSTALL_USER" git -C "$REPO_DIR" pull --ff-only || warn "  git pull failed (local changes?)"
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
MATRIX_SRC="/tmp/rpi-rgb-led-matrix"
if [[ ! -d "$MATRIX_SRC" ]]; then
    info "  Cloning rpi-rgb-led-matrix…"
    git clone --quiet https://github.com/hzeller/rpi-rgb-led-matrix.git "$MATRIX_SRC"
else
    info "  rpi-rgb-led-matrix already cloned — pulling…"
    git -C "$MATRIX_SRC" pull --ff-only --quiet || true
fi
info "  Building Python binding (this takes ~1–2 minutes on a Pi)…"
if sudo -u "$INSTALL_USER" "${VENV}/bin/pip" install --quiet -e "${MATRIX_SRC}/bindings/python"; then
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
${INSTALL_USER} ALL=(ALL) NOPASSWD: /sbin/iptables
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/sbin/iptables
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/sbin/dnsmasq
${INSTALL_USER} ALL=(ALL) NOPASSWD: /usr/bin/timedatectl
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

# ── Phase 8: File permissions ─────────────────────────────────────
info "Phase 8: Setting file permissions…"
chown -R "${INSTALL_USER}:${INSTALL_USER}" "$REPO_DIR"
chmod +x "${REPO_DIR}/main.py" 2>/dev/null || true
success "Permissions set"

# ── Phase 9: Optional interactive config ──────────────────────────
echo
echo -e "${BOLD}Phase 9: Initial configuration (Enter to skip — configure via web UI later)${RESET}"
echo

read -rp "  Home latitude  (e.g. 40.7128, blank to skip): " INPUT_LAT
read -rp "  Home longitude (e.g. -74.006, blank to skip): " INPUT_LON

INPUT_TM=""; INPUT_AL=""; INPUT_FA=""
read -rsp "  Tomorrow.io API key    (blank to skip): " INPUT_TM; echo
read -rsp "  AirLabs API key        (blank to skip): " INPUT_AL; echo
read -rsp "  FlightAware API key    (blank to skip): " INPUT_FA; echo

CFG_JSON="${CFG_DIR}/config.json"
SEC_JSON="${CFG_DIR}/secrets.json"

if [[ -n "$INPUT_LAT" && -n "$INPUT_LON" ]]; then
    info "  Writing location…"
    LAT="$INPUT_LAT" LON="$INPUT_LON" \
    sudo -u "$INSTALL_USER" "${VENV}/bin/python3" - "$CFG_JSON" <<'PYEOF'
import json, os, sys
path = sys.argv[1]
lat = float(os.environ['LAT'])
lon = float(os.environ['LON'])
with open(path) as f:
    cfg = json.load(f)
cfg.setdefault('location', {})['location_home'] = [lat, lon]
cfg['location']['temperature_location'] = f'{lat},{lon}'
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2)
PYEOF
fi

if [[ -n "$INPUT_TM" || -n "$INPUT_AL" || -n "$INPUT_FA" ]]; then
    info "  Writing API keys…"
    TM="$INPUT_TM" AL="$INPUT_AL" FA="$INPUT_FA" \
    sudo -u "$INSTALL_USER" "${VENV}/bin/python3" - "$SEC_JSON" <<'PYEOF'
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
