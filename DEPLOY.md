# Despliegue en Oracle Cloud (gratuito)

Guía completa para subir el scraper a Oracle Cloud Always Free y dejarlo funcionando 24/7.

---

## 1. Crear el servidor en Oracle Cloud

> Si ya tienes la cuenta creada y el servidor levantado, salta al paso 2.

### 1.1 Crear cuenta
1. Ve a [cloud.oracle.com](https://cloud.oracle.com) y crea una cuenta gratuita.
2. Necesitarás una tarjeta de crédito para verificar (no se cobra nada con el plan Always Free).

### 1.2 Crear la instancia (VM)
1. En el panel de Oracle, ve a **Compute → Instances → Create Instance**.
2. Configura:
   - **Name**: `scraper-linkedin` (o el que quieras)
   - **Image**: `Ubuntu 22.04` (Canonical Ubuntu)
   - **Shape**: `VM.Standard.A1.Flex` ← **esto es ARM, el gratuito**
     - OCPUs: **1** (o hasta 4, son gratis)
     - RAM: **6 GB** (Chrome necesita al menos 2 GB)
   - **Networking**: deja la red por defecto, asegúrate de que tiene IP pública
   - **SSH keys**: sube tu clave pública (`~/.ssh/id_rsa.pub`) o genera una nueva
3. Pulsa **Create**. En 2-3 minutos estará lista.

### 1.3 Abrir el puerto 5001 en Oracle Cloud
Oracle tiene dos capas de firewall: la Security List de la VCN y el firewall del SO.
El script `setup_server.sh` se encarga del firewall del SO, pero el de Oracle hay que abrirlo manualmente:

1. Ve a **Networking → Virtual Cloud Networks → tu VCN → Security Lists → Default Security List**.
2. Pulsa **Add Ingress Rules** y añade:
   - **Source CIDR**: `0.0.0.0/0`
   - **IP Protocol**: TCP
   - **Destination Port Range**: `5001`
3. Guarda los cambios.

---

## 2. Conectarte al servidor

```bash
# Desde tu Mac (reemplaza con la IP pública de tu instancia Oracle)
ssh ubuntu@<IP_PUBLICA_ORACLE>
```

Si generaste la clave con Oracle, primero dale permisos:
```bash
chmod 600 ~/Downloads/ssh-key-*.key
ssh -i ~/Downloads/ssh-key-*.key ubuntu@<IP_PUBLICA_ORACLE>
```

---

## 3. Subir el código al servidor

Tienes dos opciones:

### Opción A — GitHub (recomendada)
El script `setup_server.sh` clona el repo automáticamente.
Solo necesitas que el repo esté en GitHub (puede ser privado).

### Opción B — SCP (si no usas GitHub)
```bash
# Desde tu Mac, comprime el proyecto y súbelo
cd ~/Desktop/practicas
tar --exclude='.git' --exclude='venv' --exclude='sessions' \
    --exclude='output' --exclude='logs' \
    -czf scraper.tar.gz scraperLinkedInPersonal/
scp scraper.tar.gz ubuntu@<IP_PUBLICA_ORACLE>:~

# En el servidor, descomprime
ssh ubuntu@<IP_PUBLICA_ORACLE>
sudo mkdir -p /opt/scraper
sudo chown ubuntu:ubuntu /opt/scraper
tar -xzf ~/scraper.tar.gz -C /opt/
mv /opt/scraperLinkedInPersonal/* /opt/scraper/
```

---

## 4. Ejecutar el script de instalación

### 4.1 Prepara el script (hazlo una vez en tu Mac)
Edita `setup_server.sh` y cambia estas dos líneas al inicio:
```bash
GITHUB_REPO="https://github.com/TU_USUARIO/TU_REPOSITORIO.git"
ACCOUNT_SLUG="miquel-roca-mascaros"   # tu slug de LinkedIn
```

### 4.2 Sube y ejecuta el script en el servidor
```bash
# Desde tu Mac
scp setup_server.sh ubuntu@<IP_PUBLICA_ORACLE>:~

# Conéctate al servidor y ejecuta
ssh ubuntu@<IP_PUBLICA_ORACLE>
bash setup_server.sh
```

El script hace automáticamente:
- Actualiza el sistema
- Instala Python 3, Chromium y ChromeDriver para ARM
- Clona el repositorio en `/opt/scraper`
- Crea el entorno virtual e instala dependencias
- Crea el archivo `.env` base
- Configura el servicio systemd para el viewer
- Configura el crontab para scraping automático
- Abre el puerto 5001 en UFW e iptables

---

## 5. Configurar el .env en el servidor

Cuando el script termine, edita el `.env` con tus valores reales:

```bash
nano /opt/scraper/.env
```

Valores que debes rellenar:
```env
# Notificaciones Telegram (ya los tienes)
TELEGRAM_BOT_TOKEN=8697928169:AAEZzcyxb53k1xcYAdaHlMOy-vZJgNVAy2I
TELEGRAM_CHAT_ID=2050786051

# Clave de cifrado (la que ya tienes en local)
CREDENTIAL_KEY=DIhIOv6j2hVCw5QIquIfyhEkLwXZhprcssxGdZE202g=

# Esto ya viene en el .env que genera el script (no tocar):
CHROME_BINARY=/usr/bin/chromium-browser
```

---

## 6. Arrancar el viewer

```bash
sudo systemctl start scraper-viewer
sudo systemctl status scraper-viewer   # debe verse "active (running)"
```

Accede desde el navegador:
```
http://<IP_PUBLICA_ORACLE>:5001
```

---

## 7. Añadir tu cuenta LinkedIn

1. Abre el viewer en el navegador.
2. Ve a la pestaña **Cuentas** → pulsa **Añadir cuenta**.
3. Introduce tu email y contraseña de LinkedIn.
4   El servidor abrirá Chromium en modo headless y hará el login automáticamente.
5. Si LinkedIn pide verificación, recibirás un aviso en Telegram.
6. Cuando la sesión esté activa, el card de la cuenta aparecerá en verde.

---

## 8. Configurar keepalive (para que no duerma)

> Oracle Always Free **no duerme** (es una VM real), así que este paso es opcional.
> Solo es necesario si en el futuro cambias a Render/Railway u otro servicio que sí duerme.

Si quieres igualmente monitorizar que el viewer está vivo:

1. Ve a [cron-job.org](https://console.cron-job.org/jobs) y crea una cuenta gratuita.
2. Crea un nuevo cron job:
   - **URL**: `http://<IP_PUBLICA_ORACLE>:5001/ping`
   - **Interval**: cada 5 minutos
3. Guarda. Recibirás alertas si el viewer cae.

---

## 9. Verificar que todo funciona

```bash
# Ver logs del viewer en tiempo real
sudo journalctl -u scraper-viewer -f

# Ver logs del cron
tail -f /opt/scraper/logs/cron.log

# Comprobar que el viewer responde
curl http://localhost:5001/ping

# Ver estado del servicio
sudo systemctl status scraper-viewer

# Comprobar que ChromeDriver funciona en ARM
chromedriver --version
chromium-browser --version
```

---

## 10. Resumen de qué hace el servidor automáticamente

| Cuándo | Qué hace | Modo |
|--------|----------|------|
| Domingos 9h | Recopila slugs de tus conexiones | `--mode index` |
| L-V 8h, 13h, 18h | Enriquece hasta 20 contactos | `--mode enrich` |
| Sábados 9h, 16h | Enriquece hasta 20 contactos | `--mode enrich` |
| Si sesión caduca | Intenta re-login automático en headless | auto |
| Si re-login falla | Envía aviso a Telegram | auto |
| Al terminar enrich | Envía resumen a Telegram | auto |

**~350-560 contactos enriquecidos por semana** dentro de límites seguros anti-ban.

---

## 11. Actualizar el código en el servidor

Cuando hagas cambios en local y los subas a GitHub:

```bash
ssh ubuntu@<IP_PUBLICA_ORACLE>
cd /opt/scraper
git pull
sudo systemctl restart scraper-viewer
```
