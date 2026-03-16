#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║        LinkedIn Scraper — Script de instalación para servidor               ║
# ║        Compatible con: Oracle Cloud ARM64 · Google Cloud x86_64             ║
# ║        OS: Ubuntu 22.04 LTS                                                 ║
# ║                                                                              ║
# ║  USO:                                                                        ║
# ║    1. Edita GITHUB_REPO y ACCOUNT_SLUG justo abajo                          ║
# ║    2. Sube este archivo al servidor:                                         ║
# ║         scp setup_server.sh <USUARIO>@<IP>:~                                ║
# ║    3. Conéctate por SSH y ejecútalo:                                         ║
# ║         bash setup_server.sh                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURA ESTAS VARIABLES ANTES DE EJECUTAR EL SCRIPT
# ──────────────────────────────────────────────────────────────────────────────
GITHUB_REPO="https://github.com/MiquelRoca18/scraperLinkedin.git"
APP_DIR="/opt/scraper"
APP_PORT=5001
SERVICE_USER="$(whoami)"   # Se detecta automáticamente (ubuntu / miquel1818 / etc.)
ACCOUNT_SLUG=""            # Slug LinkedIn (ej: miquel-roca-mascaros). Vacío = sin cron.
# ──────────────────────────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${GREEN}[✓]${NC} $1"; }
warn() { echo -e "${YELLOW}[!]${NC} $1"; }
info() { echo -e "${BLUE}[→]${NC} $1"; }
err()  { echo -e "${RED}[✗]${NC} $1"; exit 1; }

echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║       LinkedIn Scraper — Instalación en servidor         ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# Validar que se ha configurado el repo
if [[ "$GITHUB_REPO" == *"TU_USUARIO"* ]]; then
    err "Edita la variable GITHUB_REPO en el script antes de ejecutarlo."
fi

# ── Detectar arquitectura ──────────────────────────────────────────────────────
ARCH=$(uname -m)
info "Arquitectura detectada: $ARCH"
if [[ "$ARCH" == "aarch64" || "$ARCH" == "arm64" ]]; then
    PLATFORM="arm"
    info "Plataforma: ARM64 (Oracle Cloud Ampere)"
else
    PLATFORM="x86"
    info "Plataforma: x86_64 (Google Cloud / AWS / estándar)"
fi

# ── 1. Actualizar sistema ──────────────────────────────────────────────────────
info "Actualizando paquetes del sistema..."
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
log "Sistema actualizado"

# ── 2. Swap (necesario en máquinas con 1 GB RAM como Google Cloud e2-micro) ───
TOTAL_RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
info "RAM disponible: ${TOTAL_RAM_MB} MB"
if [ "$TOTAL_RAM_MB" -lt 1800 ]; then
    if swapon --show | grep -q /swapfile 2>/dev/null; then
        log "Swap ya existe, omitiendo"
    else
        info "RAM insuficiente para Chrome — creando swap de 2 GB..."
        sudo fallocate -l 2G /swapfile
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile
        sudo swapon /swapfile
        echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab > /dev/null
        # Reducir swappiness para que solo use swap cuando sea necesario
        echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf > /dev/null
        sudo sysctl -p > /dev/null
        log "Swap de 2 GB activado (total memoria: $(free -h | awk '/^Swap:/{print $2}'))"
    fi
else
    log "RAM suficiente (${TOTAL_RAM_MB} MB), no se necesita swap"
fi

# ── 3. Dependencias base del sistema ──────────────────────────────────────────
info "Instalando dependencias base..."
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-venv python3-dev python3-pip \
    git curl wget unzip ca-certificates gnupg \
    fonts-liberation libgbm1 libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgdk-pixbuf2.0-0 libnspr4 libnss3 \
    libxcomposite1 libxdamage1 libxrandr2 libxss1 \
    libxtst6 libxkbcommon0 libx11-xcb1 \
    ufw
log "Dependencias base instaladas"

# ── 4. Instalar Chrome / Chromium según arquitectura ──────────────────────────
if [ "$PLATFORM" = "arm" ]; then
    # ARM: usar Chromium de los repos de Ubuntu
    info "Instalando Chromium (ARM)..."
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        chromium-browser chromium-chromedriver
    CHROME_BIN="/usr/bin/chromium-browser"
    log "Chromium ARM instalado: $(chromium-browser --version 2>&1)"
else
    # x86_64: instalar Google Chrome oficial
    info "Instalando Google Chrome (x86_64)..."
    wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
        -O /tmp/google-chrome.deb
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq /tmp/google-chrome.deb
    rm /tmp/google-chrome.deb
    CHROME_BIN="/usr/bin/google-chrome"
    log "Google Chrome instalado: $(google-chrome --version 2>&1)"
    # selenium-manager (incluido en selenium 4.6+) descarga ChromeDriver automáticamente
    info "ChromeDriver será gestionado automáticamente por selenium-manager"
fi

# ── 5. Clonar repositorio ──────────────────────────────────────────────────────
info "Clonando repositorio desde GitHub..."
if [ -d "$APP_DIR" ]; then
    warn "$APP_DIR ya existe. Haciendo git pull..."
    cd "$APP_DIR"
    git pull
else
    sudo mkdir -p "$APP_DIR"
    sudo chown "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
    git clone "$GITHUB_REPO" "$APP_DIR"
fi
cd "$APP_DIR"
log "Repositorio listo en $APP_DIR"

# ── 6. Entorno virtual Python ──────────────────────────────────────────────────
info "Creando entorno virtual Python..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
log "Dependencias Python instaladas"

# ── 7. Directorios de trabajo ──────────────────────────────────────────────────
info "Creando directorios necesarios..."
mkdir -p sessions output logs data
log "Directorios creados"

# ── 8. Crear .env con valores reales (preguntar al usuario) ───────────────────
WRITE_ENV=false
if [ -f "$APP_DIR/.env" ]; then
    warn ".env ya existe. ¿Sobreescribir con nuevos valores? [s/N]"
    read -r OVERWRITE_ENV
    if [[ "$OVERWRITE_ENV" =~ ^[sS]$ ]]; then
        WRITE_ENV=true
    else
        info "Manteniendo .env existente."
        # Actualizar CHROME_BINARY al valor correcto para esta arquitectura
        if grep -q "^CHROME_BINARY=" "$APP_DIR/.env"; then
            sed -i "s|^CHROME_BINARY=.*|CHROME_BINARY=${CHROME_BIN}|" "$APP_DIR/.env"
        else
            echo "CHROME_BINARY=${CHROME_BIN}" >> "$APP_DIR/.env"
        fi
    fi
else
    WRITE_ENV=true
fi

if [ "$WRITE_ENV" = "true" ]; then
    echo ""
    echo -e "${BLUE}──────────────────────────────────────────────────────────${NC}"
    echo -e "${BLUE}  Configuración del .env — responde a las preguntas        ${NC}"
    echo -e "${BLUE}  (pulsa Enter para dejar vacío y rellenarlo después)       ${NC}"
    echo -e "${BLUE}──────────────────────────────────────────────────────────${NC}"
    echo ""

    echo -e "${YELLOW}Telegram Bot Token${NC} (de @BotFather):"
    read -r INPUT_TG_TOKEN
    TG_TOKEN="${INPUT_TG_TOKEN:-}"

    echo -e "${YELLOW}Telegram Chat ID${NC} (tu ID personal, de @userinfobot):"
    read -r INPUT_TG_CHAT
    TG_CHAT="${INPUT_TG_CHAT:-}"

    echo ""
    echo -e "${YELLOW}CREDENTIAL_KEY${NC} (clave Fernet para cifrar contraseñas)."
    echo "  → Pega la misma que tienes en el .env de tu Mac."
    echo "  → Si la dejas vacía se genera una nueva automáticamente."
    read -r INPUT_CRED_KEY
    if [ -z "$INPUT_CRED_KEY" ]; then
        info "Generando CREDENTIAL_KEY automáticamente..."
        INPUT_CRED_KEY=$(source "$APP_DIR/venv/bin/activate" && \
            python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
        log "CREDENTIAL_KEY generada: $INPUT_CRED_KEY"
        warn "Guarda esta clave en un lugar seguro."
    fi
    CRED_KEY="$INPUT_CRED_KEY"

    cat > "$APP_DIR/.env" << ENVEOF
## Configuración del scraper de LinkedIn
## ──────────────────────────────────────────────────────────────────────────────

# ── Límites anti-ban ──────────────────────────────────────────────────────────
MAX_CONTACTS_PER_RUN=20
MAX_CONTACTS_PER_DAY=60
MAX_CONTACTS_CAP=50
SCRAPE_WINDOW_START=8
SCRAPE_WINDOW_END=21
MIN_HOURS_BETWEEN_RUNS=0
COOLDOWN_HOURS_AFTER_429=48
SCHEDULED_RANDOM_DELAY_MINUTES=25
CONTACT_REFRESH_DAYS=30

# ── Scraping ──────────────────────────────────────────────────────────────────
BROWSER_PROFILE_WAIT=15
SLEEP_BETWEEN_CONNECTIONS=6
HEADLESS=true
CHROME_BINARY=${CHROME_BIN}

# ── Notificaciones Telegram ───────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=${TG_TOKEN}
TELEGRAM_CHAT_ID=${TG_CHAT}

# ── Cifrado de credenciales ───────────────────────────────────────────────────
CREDENTIAL_KEY=${CRED_KEY}
ENVEOF

    log ".env creado correctamente"
fi

# ── 9. Servicio systemd para el viewer ────────────────────────────────────────
info "Configurando servicio systemd..."
sudo tee /etc/systemd/system/scraper-viewer.service > /dev/null << SERVICEEOF
[Unit]
Description=LinkedIn Scraper — Viewer Web App
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python viewer_app.py
Restart=always
RestartSec=10
StandardOutput=append:$APP_DIR/logs/viewer.log
StandardError=append:$APP_DIR/logs/viewer.log

[Install]
WantedBy=multi-user.target
SERVICEEOF

sudo systemctl daemon-reload
sudo systemctl enable scraper-viewer
log "Servicio systemd configurado (scraper-viewer)"

# ── 10. Crontab para scraping automático ──────────────────────────────────────
if [ -n "$ACCOUNT_SLUG" ]; then
    info "Configurando cron para la cuenta: $ACCOUNT_SLUG"

    CRON_INDEX="cd $APP_DIR && venv/bin/python run_scheduled.py --mode=index --account=$ACCOUNT_SLUG >> logs/cron.log 2>&1"
    CRON_ENRICH="cd $APP_DIR && venv/bin/python run_scheduled.py --mode=enrich --account=$ACCOUNT_SLUG >> logs/cron.log 2>&1"

    crontab -l 2>/dev/null | grep -v "run_scheduled.py" | crontab - || true

    (crontab -l 2>/dev/null; cat << CRONEOF

# ── LinkedIn Scraper ──────────────────────────────────────────────────────────
# Index (recopilar slugs): domingos a las 9h
0 9 * * 0 $CRON_INDEX
# Enrich (lunes-viernes): 8h, 13h, 18h
0 8,13,18 * * 1-5 $CRON_ENRICH
# Enrich (sábados): 9h y 16h
0 9,16 * * 6 $CRON_ENRICH
CRONEOF
    ) | crontab -

    log "Crontab configurado para $ACCOUNT_SLUG"
else
    warn "ACCOUNT_SLUG vacío → crontab no configurado."
    warn "Configúralo desde la web con: crontab -e"
fi

# ── 11. Firewall ───────────────────────────────────────────────────────────────
info "Configurando firewall (UFW)..."
sudo ufw allow ssh
sudo ufw allow "$APP_PORT/tcp"
sudo ufw --force enable
log "UFW configurado (puerto $APP_PORT abierto)"

# Oracle Cloud necesita iptables además de UFW; en Google Cloud esto es inofensivo
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$APP_PORT" -j ACCEPT 2>/dev/null || true

# ── 12. Resumen final ──────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                   Instalación completa ✓                 ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
log "Plataforma:   $PLATFORM ($ARCH)"
log "Chrome:       $CHROME_BIN"
log "App dir:      $APP_DIR"
log "Servicio:     scraper-viewer (systemd)"
log "Puerto:       $APP_PORT"
echo ""

PUBLIC_IP=$(curl -s ifconfig.me 2>/dev/null || curl -s api.ipify.org 2>/dev/null || echo "<IP_SERVIDOR>")

echo -e "${YELLOW}PASOS SIGUIENTES:${NC}"
echo ""
echo "  1. Arranca el viewer:"
echo "       sudo systemctl start scraper-viewer"
echo "       sudo systemctl status scraper-viewer"
echo ""
echo "  2. Accede al viewer desde el navegador:"
echo "       http://${PUBLIC_IP}:${APP_PORT}"
echo ""
echo "  3. Añade tu cuenta LinkedIn desde el viewer (correo + contraseña)."
echo ""
echo "  4. Cron externo keepalive en https://cron-job.org:"
echo "       URL:      http://${PUBLIC_IP}:${APP_PORT}/ping"
echo "       Intervalo: cada 5 minutos"
echo ""
log "Instalación finalizada."
echo ""
