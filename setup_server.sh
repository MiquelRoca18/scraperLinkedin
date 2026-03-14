#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║         LinkedIn Scraper — Script de instalación para Oracle Cloud          ║
# ║                     Ubuntu 22.04 ARM64 (Ampere)                             ║
# ║                                                                              ║
# ║  USO:                                                                        ║
# ║    1. Sube este archivo al servidor:                                         ║
# ║         scp setup_server.sh ubuntu@<IP_SERVIDOR>:~                          ║
# ║    2. Conéctate por SSH y ejecútalo:                                         ║
# ║         ssh ubuntu@<IP_SERVIDOR>                                             ║
# ║         bash setup_server.sh                                                 ║
# ║    3. Al terminar, edita /opt/scraper/.env con tus credenciales reales.      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝
set -euo pipefail

# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURA ESTAS VARIABLES ANTES DE EJECUTAR EL SCRIPT
# ──────────────────────────────────────────────────────────────────────────────
GITHUB_REPO="https://github.com/TU_USUARIO/TU_REPOSITORIO.git"   # ← CAMBIA ESTO
APP_DIR="/opt/scraper"
APP_PORT=5001
SERVICE_USER="ubuntu"   # Usuario por defecto en Oracle Cloud Ubuntu
ACCOUNT_SLUG=""         # Nombre de tu cuenta LinkedIn (ej: miquel-roca-mascaros)
                        # Se usa para configurar el cron. Déjalo vacío para omitir el cron.
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

# ── 1. Actualizar sistema ──────────────────────────────────────────────────────
info "Actualizando paquetes del sistema..."
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
log "Sistema actualizado"

# ── 2. Instalar dependencias del sistema ───────────────────────────────────────
info "Instalando dependencias del sistema..."
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    python3 python3-venv python3-dev python3-pip \
    git curl wget unzip \
    chromium-browser chromium-chromedriver \
    fonts-liberation libgbm1 libasound2 \
    libatk-bridge2.0-0 libatk1.0-0 libcups2 libdbus-1-3 \
    libdrm2 libgdk-pixbuf2.0-0 libnspr4 libnss3 \
    libxcomposite1 libxdamage1 libxrandr2 libxss1 \
    libxtst6 libxkbcommon0 \
    ufw \
    netfilter-persistent iptables-persistent
log "Dependencias instaladas"

# ── 3. Verificar versiones ─────────────────────────────────────────────────────
PYTHON_VERSION=$(python3 --version 2>&1)
CHROME_VERSION=$(chromium-browser --version 2>&1 || echo "N/A")
DRIVER_VERSION=$(chromedriver --version 2>&1 || echo "N/A")
info "Python:      $PYTHON_VERSION"
info "Chromium:    $CHROME_VERSION"
info "ChromeDriver: $DRIVER_VERSION"

# ── 4. Clonar repositorio ──────────────────────────────────────────────────────
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

# ── 5. Entorno virtual Python ──────────────────────────────────────────────────
info "Creando entorno virtual Python..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip --quiet
pip install -r requirements.txt --quiet
log "Dependencias Python instaladas"

# ── 6. Directorios de trabajo ──────────────────────────────────────────────────
info "Creando directorios necesarios..."
mkdir -p sessions output logs data
log "Directorios creados"

# ── 7. Crear .env con valores base ────────────────────────────────────────────
if [ ! -f "$APP_DIR/.env" ]; then
    info "Creando archivo .env con valores base..."
    cat > "$APP_DIR/.env" << 'ENVEOF'
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

# En Oracle Cloud ARM el binario se llama chromium-browser
CHROME_BINARY=/usr/bin/chromium-browser

# ── Notificaciones Telegram ───────────────────────────────────────────────────
# TELEGRAM_BOT_TOKEN → habla con @BotFather en Telegram → /newbot
# TELEGRAM_CHAT_ID   → tu ID personal (@userinfobot en Telegram)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# ── Cifrado de credenciales ───────────────────────────────────────────────────
# Genera la clave con:
#   python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
CREDENTIAL_KEY=
ENVEOF
    warn "IMPORTANTE: Edita $APP_DIR/.env con tus valores reales antes de continuar."
    warn "  → nano $APP_DIR/.env"
else
    warn ".env ya existe, no se sobreescribe. Asegúrate de que CHROME_BINARY=/usr/bin/chromium-browser"
fi

# ── 8. Servicio systemd para el viewer ────────────────────────────────────────
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

# ── 9. Crontab para scraping automático ───────────────────────────────────────
if [ -n "$ACCOUNT_SLUG" ]; then
    info "Configurando cron para la cuenta: $ACCOUNT_SLUG"

    CRON_CMD_INDEX="cd $APP_DIR && venv/bin/python run_scheduled.py --mode=index --account=$ACCOUNT_SLUG >> logs/cron.log 2>&1"
    CRON_CMD_ENRICH="cd $APP_DIR && venv/bin/python run_scheduled.py --mode=enrich --account=$ACCOUNT_SLUG >> logs/cron.log 2>&1"

    # Eliminar entradas anteriores del scraper para evitar duplicados
    crontab -l 2>/dev/null | grep -v "run_scheduled.py" | crontab - || true

    # Añadir nuevas entradas
    (crontab -l 2>/dev/null; cat << CRONEOF

# ── LinkedIn Scraper ──────────────────────────────────────────────────────────
# Index (recopilar slugs): domingos a las 9h
0 9 * * 0 $CRON_CMD_INDEX
# Enrich (lunes a viernes): 8h, 13h, 18h
0 8,13,18 * * 1-5 $CRON_CMD_ENRICH
# Enrich (sábados): 9h y 16h
0 9,16 * * 6 $CRON_CMD_ENRICH
CRONEOF
    ) | crontab -

    log "Crontab configurado para $ACCOUNT_SLUG"
else
    warn "ACCOUNT_SLUG no definido → crontab no configurado. Configúralo manualmente después."
    warn "  Ejemplo para configurarlo:"
    warn "  crontab -e"
    warn "  # Añade estas líneas (cambia SLUG por tu nombre de cuenta):"
    warn "  0 9 * * 0  cd $APP_DIR && venv/bin/python run_scheduled.py --mode=index  --account=SLUG >> logs/cron.log 2>&1"
    warn "  0 8,13,18 * * 1-5 cd $APP_DIR && venv/bin/python run_scheduled.py --mode=enrich --account=SLUG >> logs/cron.log 2>&1"
    warn "  0 9,16 * * 6 cd $APP_DIR && venv/bin/python run_scheduled.py --mode=enrich --account=SLUG >> logs/cron.log 2>&1"
fi

# ── 10. Firewall: UFW + reglas de Oracle Cloud ────────────────────────────────
info "Configurando firewall..."

# UFW
sudo ufw allow ssh
sudo ufw allow "$APP_PORT/tcp"
sudo ufw --force enable
log "UFW configurado (puerto $APP_PORT abierto)"

# Oracle Cloud usa iptables ADEMÁS de UFW. Sin esto el puerto sigue bloqueado.
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$APP_PORT" -j ACCEPT 2>/dev/null || true
sudo netfilter-persistent save 2>/dev/null || true
log "Reglas iptables guardadas (Oracle Cloud)"

# ── 11. Verificación final ─────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║                   Instalación completa                   ║${NC}"
echo -e "${BLUE}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
log "Directorio de la app:  $APP_DIR"
log "Servicio systemd:      scraper-viewer"
log "Puerto del viewer:     $APP_PORT"
log "Chromium:              /usr/bin/chromium-browser"
log "ChromeDriver:          /usr/bin/chromedriver"
echo ""

# Comprobar si .env tiene los valores obligatorios
if grep -q "^TELEGRAM_BOT_TOKEN=$" "$APP_DIR/.env" 2>/dev/null; then
    warn "TELEGRAM_BOT_TOKEN está vacío en .env"
fi
if grep -q "^CREDENTIAL_KEY=$" "$APP_DIR/.env" 2>/dev/null; then
    warn "CREDENTIAL_KEY está vacía en .env"
fi

echo ""
echo -e "${YELLOW}PASOS SIGUIENTES:${NC}"
echo ""
echo "  1. Edita el .env con tus credenciales reales:"
echo "       nano $APP_DIR/.env"
echo ""
echo "  2. Genera la CREDENTIAL_KEY (si no la tienes):"
echo "       cd $APP_DIR && source venv/bin/activate"
echo "       python3 -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
echo "       # Copia el resultado y pégalo en .env como CREDENTIAL_KEY=..."
echo ""
echo "  3. Arranca el viewer:"
echo "       sudo systemctl start scraper-viewer"
echo "       sudo systemctl status scraper-viewer"
echo ""
echo "  4. Accede al viewer desde el navegador:"
echo "       http://$(curl -s ifconfig.me 2>/dev/null || echo '<IP_SERVIDOR>'):$APP_PORT"
echo ""
echo "  5. Añade tu cuenta LinkedIn desde el viewer (correo + contraseña)."
echo "     Esto creará la sesión .pkl automáticamente."
echo ""
echo "  6. Configura el cron externo en https://cron-job.org para el keepalive:"
echo "       URL: http://$(curl -s ifconfig.me 2>/dev/null || echo '<IP_SERVIDOR>'):$APP_PORT/ping"
echo "       Intervalo: cada 5 minutos"
echo ""

# Verificar que chromedriver funciona
info "Verificando que ChromeDriver arranca correctamente..."
if chromedriver --version &>/dev/null; then
    log "ChromeDriver OK: $(chromedriver --version 2>&1)"
else
    warn "ChromeDriver no responde. Revisa la instalación de chromium-chromedriver."
fi

echo ""
log "Script finalizado. Cuando hayas editado el .env, ejecuta:"
echo "       sudo systemctl start scraper-viewer"
echo ""
