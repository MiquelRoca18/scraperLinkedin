# notifications.py
# Notificaciones por Telegram para el scraper de LinkedIn.
#
# Eventos notificados:
#   · Sesión caducada (la cuenta necesita re-login)
#   · Bloqueo de LinkedIn (on_block / 429 → cooldown activado)
#   · Resumen diario de scraping (contactos nuevos, actualizados, errores)
#
# Configuración (.env):
#   TELEGRAM_BOT_TOKEN  → token del bot (@BotFather)
#   TELEGRAM_CHAT_ID    → ID del chat/canal donde enviar los mensajes
#
# Si alguna de las dos variables está vacía, las notificaciones se deshabilitan
# silenciosamente (nunca lanza excepciones hacia el scraper).

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "").strip()

_ENABLED = bool(_BOT_TOKEN and _CHAT_ID)


def _send(text: str) -> bool:
    """
    Envía un mensaje de texto a Telegram.
    Devuelve True si se envió correctamente, False en cualquier error.
    Nunca lanza excepciones.
    """
    if not _ENABLED:
        return False
    try:
        import urllib.request
        import urllib.parse
        import json as _json

        payload = _json.dumps({
            "chat_id": _CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")
        url = f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage"
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = resp.status == 200
        if ok:
            logger.debug("Telegram: mensaje enviado correctamente")
        else:
            logger.warning("Telegram: respuesta inesperada %s", resp.status)
        return ok
    except Exception as e:
        logger.warning("Telegram: error al enviar mensaje: %s", e)
        return False


# ── Eventos específicos ────────────────────────────────────────────────────────

def notify_session_expired(account: Optional[str] = None) -> None:
    """
    Notifica que la sesión de una cuenta ha caducado y necesita re-login manual.
    """
    label = f"<b>{account}</b>" if account else "la cuenta principal"
    _send(
        f"⚠️ <b>LinkedIn Scraper — Sesión caducada</b>\n\n"
        f"La sesión de {label} ha expirado.\n"
        f"Es necesario volver a iniciar sesión manualmente:\n"
        f"<code>python main.py --account={account or 'nombre-cuenta'}</code>"
    )


def notify_block(account: Optional[str] = None, cooldown_hours: int = 48) -> None:
    """
    Notifica que LinkedIn ha bloqueado temporalmente la sesión (429 / on_block).
    """
    label = f"<b>{account}</b>" if account else "la cuenta principal"
    _send(
        f"🚫 <b>LinkedIn Scraper — Cuenta bloqueada</b>\n\n"
        f"LinkedIn ha limitado {label} (error 429 / demasiadas peticiones).\n"
        f"Cooldown activado: <b>{cooldown_hours} horas</b> sin hacer peticiones.\n"
        f"El scraper reanudará automáticamente cuando pase el cooldown."
    )


def notify_daily_summary(
    account: Optional[str],
    new_count: int,
    updated_count: int,
    skipped_count: int,
    error_count: int,
    queue_pending: int,
) -> None:
    """
    Resumen del enriquecimiento: cuántos contactos se procesaron, cuántos quedan.
    Solo se envía si se procesó al menos un contacto (evita spam en días sin actividad).
    """
    if new_count + updated_count + error_count == 0:
        return
    label = account or "cuenta principal"
    total = new_count + updated_count
    _send(
        f"📊 <b>LinkedIn Scraper — Resumen [{label}]</b>\n\n"
        f"✅ Nuevos: <b>{new_count}</b>\n"
        f"🔄 Actualizados: <b>{updated_count}</b>\n"
        f"⏭ Saltados (frescos): <b>{skipped_count}</b>\n"
        f"❌ Errores: <b>{error_count}</b>\n"
        f"📋 Pendientes en cola: <b>{queue_pending}</b>"
    )


def notify_index_complete(
    account: Optional[str],
    total_slugs: int,
    new_queued: int,
) -> None:
    """
    Notifica que el reindexado de slugs ha finalizado.
    """
    label = account or "cuenta principal"
    _send(
        f"🗂 <b>LinkedIn Scraper — Índice actualizado [{label}]</b>\n\n"
        f"Conexiones encontradas: <b>{total_slugs}</b>\n"
        f"Nuevas encoladas: <b>{new_queued}</b>"
    )


def is_enabled() -> bool:
    """True si las notificaciones Telegram están configuradas y activas."""
    return _ENABLED
