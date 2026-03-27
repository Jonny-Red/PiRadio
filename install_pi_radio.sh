#!/bin/bash
# =============================================================================
#  Pi Radio Station Player — One-Stop Installer
#  Installs ALL system packages, Python dependencies, streaming software,
#  and program files needed to run Pi Radio Station Player.
#
#  Usage:
#    bash install_pi_radio.sh                  # standard install
#    bash install_pi_radio.sh --autostart      # also set up boot service
#    bash install_pi_radio.sh --dir=~/myradio  # custom install folder
#    bash install_pi_radio.sh --autostart --dir=~/myradio
# =============================================================================

set -euo pipefail

# ── Colour helpers ────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
ok()      { echo -e "${GREEN}[  OK]${RESET}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[FAIL]${RESET}  $*" >&2; }
section() {
  echo ""
  echo -e "${BOLD}${CYAN}══════════════════════════════════════════${RESET}"
  echo -e "${BOLD}${CYAN}  $*${RESET}"
  echo -e "${BOLD}${CYAN}══════════════════════════════════════════${RESET}"
}

# ── Defaults ──────────────────────────────────────────────────────────────────
INSTALL_DIR="${HOME}/radio"
AUTOSTART=false
SERVICE_USER="${USER}"
VERIFY_FAILED=false

# ── Parse flags ───────────────────────────────────────────────────────────────
for arg in "$@"; do
  case "$arg" in
    --autostart)  AUTOSTART=true ;;
    --dir=*)      INSTALL_DIR="${arg#--dir=}" ;;
    --help|-h)
      echo ""
      echo "Pi Radio Station Player — One-Stop Installer"
      echo ""
      echo "Usage: bash install_pi_radio.sh [options]"
      echo ""
      echo "Options:"
      echo "  --autostart        Install a systemd service so the backend starts on boot"
      echo "  --dir=PATH         Install program files to PATH  (default: ~/radio)"
      echo "  --help             Show this help message"
      echo ""
      exit 0 ;;
  esac
done

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${CYAN}  ╔═══════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${CYAN}  ║   Pi Radio Station Player — Installer    ║${RESET}"
echo -e "${BOLD}${CYAN}  ╚═══════════════════════════════════════════╝${RESET}"
echo ""
echo -e "  Install directory : ${BOLD}${INSTALL_DIR}${RESET}"
echo -e "  Autostart service : ${BOLD}${AUTOSTART}${RESET}"
echo ""

# =============================================================================
section "Step 1 — Checking sudo access"
# =============================================================================
if [[ $EUID -eq 0 ]]; then
  error "Do not run this script as root."
  error "Run it as a normal user — it will call sudo only when needed."
  exit 1
fi

if ! sudo -n true 2>/dev/null; then
  info "This installer needs sudo for apt and systemd steps."
  info "You may be prompted for your password once."
  sudo true
fi
ok "Sudo access confirmed."

# =============================================================================
section "Step 2 — Updating package lists"
# =============================================================================
info "Running apt-get update..."
sudo apt-get update -qq
ok "Package lists updated."

# =============================================================================
section "Step 3 — Installing core radio packages"
# =============================================================================
CORE_PACKAGES=(
  python3           # Core Python runtime
  python3-pip       # pip package installer
  python3-tk        # tkinter — the desktop UI library
  vlc               # VLC media engine — plays all audio files
  libvlc-dev        # VLC C headers — required by python-vlc bindings
  pulseaudio        # PulseAudio sound server (required by pi_stream for source detection)
  pulseaudio-utils  # pactl — used by pi_stream.py to list audio sources
  unzip             # For extracting zip archives
  curl              # Useful for diagnostics and checking Icecast
)

info "Installing core packages..."
sudo apt-get install -y "${CORE_PACKAGES[@]}" -qq
ok "Core packages installed."

# =============================================================================
section "Step 4 — Installing streaming packages (darkice + icecast2)"
# =============================================================================
info "Installing darkice and icecast2..."
info "Pre-answering icecast2 setup prompts with safe defaults..."

# Pre-answer the icecast2 debconf prompts so it installs without a wizard popup
if command -v debconf-set-selections &>/dev/null; then
  sudo bash -c "
    echo 'icecast2 icecast2/icecast-setup boolean true'       | debconf-set-selections
    echo 'icecast2 icecast2/hostname string localhost'         | debconf-set-selections
    echo 'icecast2 icecast2/sourcepassword string hackme'      | debconf-set-selections
    echo 'icecast2 icecast2/relaypassword string hackme'       | debconf-set-selections
    echo 'icecast2 icecast2/adminpassword string hackme'       | debconf-set-selections
  "
fi

sudo apt-get install -y darkice icecast2 -qq
ok "darkice and icecast2 installed."

# Enable and start icecast2
info "Enabling icecast2 service..."
sudo systemctl enable icecast2 2>/dev/null || true
sudo systemctl start  icecast2 2>/dev/null || true

if systemctl is-active --quiet icecast2 2>/dev/null; then
  ok "icecast2 service is running."
else
  warn "icecast2 did not start automatically."
  warn "Start it manually with:  sudo systemctl start icecast2"
fi

# =============================================================================
section "Step 5 — Installing Python packages"
# =============================================================================
install_python_pkg() {
  local pkg="$1"
  info "Installing Python package: ${pkg}"

  # Try system-wide first (works on Bullseye and older)
  if pip3 install "${pkg}" --break-system-packages -q 2>/dev/null; then
    ok "${pkg} installed (system-wide)."
    return
  fi

  # Fall back to --user (Bookworm externally-managed environments)
  if pip3 install "${pkg}" --break-system-packages --user -q 2>/dev/null; then
    ok "${pkg} installed (user install)."
    return
  fi

  warn "Could not install ${pkg} automatically."
  warn "Try manually:  pip3 install ${pkg} --break-system-packages"
}

install_python_pkg "python-vlc"

# =============================================================================
section "Step 6 — Configuring Icecast2"
# =============================================================================
ICECAST_CFG="/etc/icecast2/icecast.xml"

if [[ -f "${ICECAST_CFG}" ]]; then
  info "Configuring Icecast2 to match pi_stream defaults..."

  # Set passwords to match pi_stream_darkice_v2.cfg default (hackme)
  sudo sed -i \
    -e 's|<source-password>.*</source-password>|<source-password>hackme</source-password>|' \
    -e 's|<relay-password>.*</relay-password>|<relay-password>hackme</relay-password>|' \
    -e 's|<admin-password>.*</admin-password>|<admin-password>hackme</admin-password>|' \
    "${ICECAST_CFG}"

  # Bind to all interfaces so LAN and Tailscale listeners can connect
  sudo sed -i \
    -e 's|<bind-address>.*</bind-address>|<bind-address>0.0.0.0</bind-address>|' \
    "${ICECAST_CFG}"

  sudo systemctl restart icecast2 2>/dev/null || true
  ok "Icecast2 configured and restarted."
  warn "Default Icecast2 password is 'hackme'."
  warn "Change it in ${ICECAST_CFG} if this Pi is exposed to the internet."
else
  warn "Could not find ${ICECAST_CFG} — Icecast2 may need manual configuration."
fi

# =============================================================================
section "Step 7 — Verifying all dependencies"
# =============================================================================
check_binary() {
  local label="$1"; local cmd="$2"
  if command -v "${cmd}" &>/dev/null; then
    ok "  ${label}  →  $(command -v "${cmd}")"
  else
    error "  ${label}  →  NOT FOUND"
    VERIFY_FAILED=true
  fi
}

check_python_import() {
  local label="$1"; local module="$2"
  if python3 -c "import ${module}" 2>/dev/null; then
    ok "  ${label}"
  else
    error "  ${label}  →  NOT FOUND"
    VERIFY_FAILED=true
  fi
}

check_service() {
  local label="$1"; local svc="$2"
  if systemctl is-active --quiet "${svc}" 2>/dev/null; then
    ok "  ${label}  →  running"
  else
    warn "  ${label}  →  not running  (sudo systemctl start ${svc})"
  fi
}

echo ""
echo -e "  ${BOLD}── Radio player ──${RESET}"
check_binary        "python3"        python3
check_binary        "pip3"           pip3
check_binary        "vlc"            vlc
check_python_import "tkinter"        tkinter
check_python_import "python-vlc"     vlc

if python3 -c "import vlc; inst = vlc.Instance(); assert inst is not None" 2>/dev/null; then
  ok "  VLC engine  →  python-vlc can create a VLC instance"
else
  error "  VLC engine  →  python-vlc imported but VLC instance failed"
  warn  "  Try:  sudo apt install --reinstall vlc libvlc-dev"
  VERIFY_FAILED=true
fi

echo ""
echo -e "  ${BOLD}── Streaming (pi_stream) ──${RESET}"
check_binary   "darkice"            darkice
check_binary   "icecast2"          icecast2
check_binary   "pactl"             pactl
check_binary   "pulseaudio"        pulseaudio
check_service  "icecast2 service"  icecast2

echo ""
if [[ "${VERIFY_FAILED}" == "true" ]]; then
  warn "One or more checks failed — review errors above."
  warn "Program files will still be copied, but something may not work until fixed."
else
  ok "All dependency checks passed."
fi

# =============================================================================
section "Step 8 — Copying program files"
# =============================================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PROGRAM_FILES=(
  run_radio.py
  radio_backend.py
  radio_ui.py
  shared_radio.py
  start_all.sh
  start_backend.sh
  kill_radio.sh
  pi_radio_web.html
  pi_stream.py
  pi_stream_darkice_v2.cfg
)

mkdir -p "${INSTALL_DIR}"
COPIED=0
MISSING=()

for f in "${PROGRAM_FILES[@]}"; do
  src="${SCRIPT_DIR}/${f}"
  if [[ -f "${src}" ]]; then
    cp "${src}" "${INSTALL_DIR}/${f}"
    (( COPIED++ )) || true
    info "  Copied: ${f}"
  else
    MISSING+=("${f}")
  fi
done

# Make shell scripts executable
for sh in start_all.sh start_backend.sh kill_radio.sh; do
  [[ -f "${INSTALL_DIR}/${sh}" ]] && chmod +x "${INSTALL_DIR}/${sh}"
done

echo ""
ok "Copied ${COPIED} file(s) to ${INSTALL_DIR}."

if [[ ${#MISSING[@]} -gt 0 ]]; then
  warn "These files were NOT found next to the installer and were not copied:"
  for m in "${MISSING[@]}"; do
    warn "    • ${m}"
  done
  warn "Copy them to ${INSTALL_DIR} manually before running the program."
fi

# =============================================================================
section "Step 9 — Audio output check"
# =============================================================================
if command -v raspi-config &>/dev/null; then
  info "Raspberry Pi detected."
  info "If you have no audio, run:  sudo raspi-config"
  info "  → System Options → Audio → select your output device."
  echo ""
  if pulseaudio --check 2>/dev/null; then
    ok "PulseAudio is running for this user session."
  else
    info "PulseAudio is not running in this session."
    info "It starts automatically when you log into the desktop."
    info "pi_stream.py requires PulseAudio to list audio sources."
  fi
else
  info "Check your system audio output settings if you hear no sound."
fi

# =============================================================================
section "Step 10 — Autostart service (optional)"
# =============================================================================
if [[ "${AUTOSTART}" == "true" ]]; then
  SERVICE_FILE="/etc/systemd/system/pi-radio.service"
  info "Installing systemd boot service for the radio backend..."

  sudo tee "${SERVICE_FILE}" > /dev/null << SERVICE
[Unit]
Description=Pi Radio Station Player Backend
After=network.target sound.target

[Service]
Type=simple
User=${SERVICE_USER}
WorkingDirectory=${INSTALL_DIR}
ExecStart=/usr/bin/python3 -u radio_backend.py
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
SERVICE

  sudo systemctl daemon-reload
  sudo systemctl enable pi-radio
  sudo systemctl start  pi-radio
  ok "pi-radio service installed, enabled, and started."
  info "Check status:   sudo systemctl status pi-radio"
  info "View log:       tail -f ${INSTALL_DIR}/backend.out"
  info "Stop service:   sudo systemctl stop pi-radio"
  info "Remove service: sudo systemctl disable pi-radio"
else
  info "Autostart not requested. Run with --autostart to install a boot service."
fi

# =============================================================================
section "Installation complete!"
# =============================================================================
echo ""
echo -e "  ${BOLD}Everything installed and ready:${RESET}"
echo ""
echo -e "  ${GREEN}✔${RESET}  Python 3, pip, tkinter"
echo -e "  ${GREEN}✔${RESET}  VLC + python-vlc        (audio playback)"
echo -e "  ${GREEN}✔${RESET}  PulseAudio + pactl      (audio routing for streaming)"
echo -e "  ${GREEN}✔${RESET}  darkice                 (audio stream encoder)"
echo -e "  ${GREEN}✔${RESET}  icecast2                (streaming server — port 8000)"
echo -e "  ${GREEN}✔${RESET}  All program files       → ${INSTALL_DIR}"
if [[ "${AUTOSTART}" == "true" ]]; then
echo -e "  ${GREEN}✔${RESET}  Systemd boot service    (pi-radio)"
fi
echo ""
echo -e "  ${BOLD}── Start the radio player ──${RESET}"
echo -e "    cd ${INSTALL_DIR} && python3 run_radio.py"
echo ""
echo -e "  ${BOLD}── Start the stream ──${RESET}"
echo -e "    cd ${INSTALL_DIR} && python3 pi_stream.py"
echo ""
echo -e "  ${BOLD}── Stop everything ──${RESET}"
echo -e "    bash ${INSTALL_DIR}/kill_radio.sh"
echo ""
echo -e "  ${BOLD}── Listeners connect to ──${RESET}"
echo -e "    http://<your-Pi-IP>:8000/stream"
echo ""
echo -e "  ${BOLD}── Logs ──${RESET}"
echo -e "    Radio:   cat ${INSTALL_DIR}/backend.out"
echo -e "    Stream:  cat ~/.pi_stream_darkice.log"
echo -e "    Icecast: sudo journalctl -u icecast2 -n 50"
echo ""

if [[ "${VERIFY_FAILED}" == "true" ]]; then
  echo -e "  ${YELLOW}⚠  One or more dependency checks failed — see warnings above.${RESET}"
  echo ""
  exit 1
fi

echo -e "  ${GREEN}${BOLD}All done. Enjoy your radio station!${RESET}"
echo ""
