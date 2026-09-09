#!/usr/bin/env bash
# StealthScraper-LXC Installer
# Im Stil der Proxmox VE Community Helper Scripts + BraveResearchProxmox
#
# ZWEI MODI:
#  1) HOST-MODUS (Standard auf dem Proxmox-Host): erstellt LXC + installiert App
#       bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/StealthResearch/main/install/stealthscraper-lxc.sh)"
#  2) CONTAINER-MODUS (im bestehenden LXC als root):
#       bash -c "$(wget -qLO - .../stealthscraper-lxc.sh)" -- --inner
#
# Env: SCRAPER_CTID, SCRAPER_HOSTNAME, SCRAPER_STORAGE, SCRAPER_CORES,
#      SCRAPER_MEMORY, SCRAPER_DISK, SCRAPER_BRIDGE, SCRAPER_IP, SCRAPER_PASSWORD

set -euo pipefail

REPO_URL="https://github.com/HatchetMan111/StealthResearch.git"
RAW_SCRIPT_URL="https://raw.githubusercontent.com/HatchetMan111/StealthResearch/main/install/stealthscraper-lxc.sh"
APP_DIR="/opt/stealth-scraper"
SERVICE_USER="scraper"

RD=$(printf '\033[01;31m'); GN=$(printf '\033[1;92m'); YW=$(printf '\033[33m')
CL=$(printf '\033[m')
msg_info()  { echo -e " ${YW}➜${CL} $1" >&2; }
msg_ok()    { echo -e " ${GN}✔${CL} $1" >&2; }
msg_error() { echo -e " ${RD}✘${CL} $1" >&2; }

usage() {
  cat <<'EOF'
StealthScraper-LXC Installer -- Host-Modus (LXC erstellen) oder --inner.

Aufruf Host:    bash -c "$(wget -qLO - <url>/stealthscraper-lxc.sh)"
Aufruf Container: bash -c "$(wget -qLO - <url>/stealthscraper-lxc.sh)" -- --inner

Optionen:
  --ctid ID --hostname NAME --storage NAME --cores N --memory MB --disk GB
  --bridge NAME --ip ADDR (dhcp oder 192.168.1.50/24,gw=192.168.1.1)
  --inner / --host / --uninstall / -h, --help
Env: SCRAPER_CTID, SCRAPER_HOSTNAME, SCRAPER_STORAGE, SCRAPER_CORES,
     SCRAPER_MEMORY, SCRAPER_DISK, SCRAPER_BRIDGE, SCRAPER_IP, SCRAPER_PASSWORD
EOF
}

FORCE_MODE=""
UNINSTALL=0
CTID="${SCRAPER_CTID:-}"
HOSTNAME="${SCRAPER_HOSTNAME:-scraper-lxc}"
STORAGE="${SCRAPER_STORAGE:-local-lvm}"
TPL_STORAGE="${SCRAPER_TEMPLATE_STORAGE:-local}"
CORES="${SCRAPER_CORES:-2}"
MEMORY="${SCRAPER_MEMORY:-3072}"
DISK="${SCRAPER_DISK:-10}"
BRIDGE="${SCRAPER_BRIDGE:-vmbr0}"
IPCONF="${SCRAPER_IP:-dhcp}"
ROOTPW="${SCRAPER_PASSWORD:-}"
TEMPLATE="${SCRAPER_TEMPLATE:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --inner) FORCE_MODE="inner"; shift ;;
    --host) FORCE_MODE="host"; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    --ctid) CTID="$2"; shift 2 ;;
    --hostname) HOSTNAME="$2"; shift 2 ;;
    --storage) STORAGE="$2"; shift 2 ;;
    --cores) CORES="$2"; shift 2 ;;
    --memory) MEMORY="$2"; shift 2 ;;
    --disk) DISK="$2"; shift 2 ;;
    --bridge) BRIDGE="$2"; shift 2 ;;
    --ip) IPCONF="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) msg_error "Unbekannte Option: $1"; exit 1 ;;
  esac
done

[[ $EUID -eq 0 ]] || { msg_error "Bitte als root ausführen."; exit 1; }
is_proxmox_host() { [[ -d /etc/pve ]] && command -v pct >/dev/null 2>&1; }
MODE="$FORCE_MODE"
if [[ -z "$MODE" ]]; then
  if is_proxmox_host; then MODE="host"; else MODE="inner"; fi
fi

install_inner() {
  msg_info "Container-Modus: installiere App..."
  if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    msg_ok "Service-User '$SERVICE_USER' angelegt"
  fi
  msg_info "Installiere Systemabhängigkeiten (Python + Playwright-Deps)..."
  apt-get update -qq
  apt-get install -y -qq python3 python3-venv python3-pip git curl wget \
    libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
    libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 >/dev/null
  msg_ok "Systemabhängigkeiten installiert"

  git config --global --add safe.directory "$APP_DIR"
  if [[ -d "$APP_DIR/.git" ]]; then
    msg_info "Aktualisiere Repo..."
    git -C "$APP_DIR" pull --ff-only
  else
    msg_info "Klone nach $APP_DIR..."
    git clone --depth 1 "$REPO_URL" "$APP_DIR"
  fi

  msg_info "Erstelle venv + Requirements..."
  python3 -m venv "$APP_DIR/venv"
  "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip
  "$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
  msg_info "Installiere Chromium für Playwright (einmalig, dauert)..."
  "$APP_DIR/venv/bin/playwright" install --with-deps chromium
  msg_ok "Chromium bereit"

  mkdir -p "$APP_DIR/data/results"
  if [[ -f "$APP_DIR/config.yaml" ]]; then
    msg_info "config.yaml bleibt erhalten."
  else
    cp "$APP_DIR/config.example.yaml" "$APP_DIR/config.yaml"
    msg_ok "config.yaml aus Vorlage erstellt"
  fi
  chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"

  msg_info "Installiere systemd-Units..."
  cp "$APP_DIR/deploy/stealth-scraper.service" /etc/systemd/system/
  cp "$APP_DIR/deploy/stealth-scraper-check.service" /etc/systemd/system/
  cp "$APP_DIR/deploy/stealth-scraper-check.timer" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable --now stealth-scraper.service
  systemctl enable --now stealth-scraper-check.timer
  # Neustart nach Update, damit neuer Code aktiv ist (systemd --now ist No-Op wenn schon läuft)
  systemctl restart stealth-scraper.service || true
  msg_ok "API läuft (Port 8001)"

  IP=$(hostname -I | awk '{print $1}')
  echo ""
  msg_ok "Installation abgeschlossen."
  echo -e "  ${GN}API:${CL} http://${IP}:8001  |  Health: http://${IP}:8001/health"
  echo "  Test: curl -X POST http://${IP}:8001/price -H 'Content-Type: application/json' -d '{\"url\":\"https://example.com\"}'"
  echo "  Targets in config.yaml eintragen, Check: curl -X POST http://${IP}:8001/targets/check"
  echo "  Logs: journalctl -u stealth-scraper.service -n 100"
}

resolve_ctid() {
  if [[ -n "$CTID" ]]; then echo "$CTID"; return 0; fi
  local found=""
  found=$(pct list 2>/dev/null | awk -v name="$HOSTNAME" '$3 == name {print $1; exit}')
  if [[ -n "$found" ]]; then echo "$found"; return 0; fi
  pvesh get /cluster/nextid
}

ensure_template() {
  if [[ -n "$TEMPLATE" ]]; then echo "$TPL_STORAGE:vztmpl/$TEMPLATE"; return 0; fi
  local tpl=""
  tpl=$(pveam list "$TPL_STORAGE" 2>/dev/null | grep -o "debian-12-standard[^[:space:]]*\.tar\.[a-z0-9.]*" | sort -V | tail -1 || true)
  if [[ -z "$tpl" ]]; then
    pveam update >/dev/null
    tpl=$(pveam available --section system 2>/dev/null | grep -o "debian-12-standard[^[:space:]]*\.tar\.[a-z0-9.]*" | sort -V | tail -1 || true)
    pveam download "$TPL_STORAGE" "$tpl" >/dev/null
  fi
  echo "$TPL_STORAGE:vztmpl/$tpl"
}

container_ready() {
  local ctid="$1" ip=""
  pct status "$ctid" 2>/dev/null | grep -q "status: running" || return 1
  ip=$(pct exec "$ctid" -- hostname -I 2>/dev/null | awk '{print $1}')
  [[ -n "$ip" ]] || return 1
  pct exec "$ctid" -- getent hosts github.com >/dev/null 2>&1 || return 1
  return 0
}

wait_for_network() {
  local ctid="$1" tries=0
  msg_info "Warte auf Container-Netz (max. 120 s)..."
  while [[ $tries -lt 60 ]]; do
    if container_ready "$ctid"; then
      msg_ok "Netz bereit ($(pct exec "$ctid" -- hostname -I 2>/dev/null | awk '{print $1}'))"
      return 0
    fi
    sleep 2; tries=$((tries+1))
  done
  msg_error "Kein Netz. Bridge/DHCP prüfen oder --ip statisch setzen."
  exit 1
}

run_inner_in_container() {
  local ctid="$1" tmp_host="/tmp/stealthscraper-install-$$.sh"
  wget -qLO "$tmp_host" "$RAW_SCRIPT_URL"
  pct push "$ctid" "$tmp_host" /root/stealthscraper-lxc.sh
  rm -f "$tmp_host"
  pct exec "$ctid" -- bash /root/stealthscraper-lxc.sh --inner
}

install_host() {
  local ctid tpl_ref
  ctid=$(resolve_ctid)
  if pct status "$ctid" >/dev/null 2>&1; then
    msg_info "CT $ctid existiert -- aktualisiere App darin..."
    pct start "$ctid" 2>/dev/null || true
    wait_for_network "$ctid"
    run_inner_in_container "$ctid"
    msg_ok "Update fertig: http://$(pct exec "$ctid" -- hostname -I 2>/dev/null | awk '{print $1}'):8001"
    return 0
  fi
  tpl_ref=$(ensure_template)
  local generated_pw=0
  if [[ -z "$ROOTPW" ]]; then ROOTPW=$(openssl rand -base64 12 | tr -d '/+=' | head -c 16); generated_pw=1; fi
  msg_info "Erstelle LXC CT $ctid ('$HOSTNAME', ${CORES}C/${MEMORY}MB/${DISK}GB)..."
  pct create "$ctid" "$tpl_ref" --hostname "$HOSTNAME" --storage "$STORAGE" \
    --rootfs "$STORAGE:$DISK" --cores "$CORES" --memory "$MEMORY" --swap 512 \
    --net0 "name=eth0,bridge=$BRIDGE,ip=$IPCONF" --unprivileged 1 \
    --features nesting=1 --onboot 1 --start 1 --password "$ROOTPW"
  wait_for_network "$ctid"
  run_inner_in_container "$ctid"
  local ip=""
  ip=$(pct exec "$ctid" -- hostname -I 2>/dev/null | awk '{print $1}')
  echo ""
  msg_ok "Fertig! API: http://${ip}:8001"
  [[ "$generated_pw" == "1" ]] && echo -e "  Container-root-Passwort (einmalig): ${GN}${ROOTPW}${CL}"
}

if [[ "$UNINSTALL" == "1" ]]; then
  [[ -z "$APP_DIR" || "$APP_DIR" == "/" ]] && { msg_error "Unsicheres APP_DIR"; exit 1; }
  systemctl disable --now stealth-scraper.service stealth-scraper-check.timer 2>/dev/null || true
  rm -f /etc/systemd/system/stealth-scraper*.service /etc/systemd/system/stealth-scraper-check.timer
  systemctl daemon-reload 2>/dev/null || true
  id "$SERVICE_USER" &>/dev/null && userdel "$SERVICE_USER" 2>/dev/null || true
  [[ -d "$APP_DIR" ]] && rm -rf "$APP_DIR"
  msg_ok "Deinstalliert."
  exit 0
fi

if [[ "$MODE" == "host" ]]; then install_host; else install_inner; fi
