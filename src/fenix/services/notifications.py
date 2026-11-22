"""Adaptador HTTP guiado. Elegir Telegram O Slack, no ambos obligatoriamente.
Autor principal: Integrante 4 (Tejerina Marcela del Valle)
"""
import os  # Recupera credenciales fuera del repositorio.
import requests  # Aplica timeout explícito a cada llamada HTTP.
from fenix.utils.errors import PermanentError, RetryableError


def notify(text: str) -> None:
    """Envía un mensaje breve y valida la respuesta del proveedor seleccionado.

    Args:
        text: Resumen público de hasta 1000 caracteres, sin secretos ni raw body.
    Returns:
        None únicamente después de confirmar éxito del proveedor.
    Raises:
        RetryableError: Timeout, conexión, 429 o respuesta 5xx.
        PermanentError: Configuración inválida, 4xx definitivo o rechazo funcional.
    Side Effects:
        Publica en el chat/canal del grupo. Un timeout puede causar duplicados.
    Notes:
        El modo console sirve para desarrollo; no reemplaza la evidencia final real.
        No registrar la excepción requests original: puede contener el token en URL.
    """
    mode = os.getenv("NOTIFY_PROVIDER", "console").lower()  # Adaptador explícito.
    if mode == "console":
        print(f"[CONSOLE NOTIFICATION]: {text[:1000]}")
        return

    # I4-A: Integración con Telegram
    if mode == "telegram":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise PermanentError("Faltan variables TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID")

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": text[:1000]}

        try:
            resp = requests.post(url, json=payload, timeout=(2, 5))
        except requests.Timeout:
            raise RetryableError("Timeout al enviar notificación a Telegram") from None
        except requests.RequestException:
            raise RetryableError("Fallo de conexión de red con Telegram") from None

        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableError(f"Fallo transitorio Telegram (HTTP {resp.status_code})")
        elif resp.status_code in (400, 401, 403, 404):
            raise PermanentError(f"Fallo permanente de credenciales Telegram (HTTP {resp.status_code})")

        try:
            body = resp.json()
        except ValueError:
            raise RetryableError("Respuesta no parseable de Telegram")

        if not body.get("ok"):
            raise PermanentError("Telegram respondió HTTP 200 pero ok no es True")
        return

    # I4-A: Integración con Slack
    if mode == "slack":
        slack_url = os.getenv("SLACK_WEBHOOK_URL")
        if not slack_url:
            raise PermanentError("Falta la variable SLACK_WEBHOOK_URL")

        try:
            resp = requests.post(slack_url, json={"text": text[:1000]}, timeout=(2, 5))
        except requests.Timeout:
            raise RetryableError("Timeout al enviar notificación a Slack") from None
        except requests.RequestException:
            raise RetryableError("Fallo de conexión de red con Slack") from None

        if resp.status_code == 429 or resp.status_code >= 500:
            raise RetryableError(f"Fallo transitorio Slack (HTTP {resp.status_code})")
        elif resp.status_code in (400, 401, 403, 404):
            raise PermanentError(f"Fallo permanente de webhook Slack (HTTP {resp.status_code})")

        if resp.text.strip().lower() != "ok":
            raise PermanentError("Slack respondió con cuerpo distinto de 'ok'")
        return

    raise PermanentError(f"Proveedor NOTIFY_PROVIDER desconocido: {mode}")
