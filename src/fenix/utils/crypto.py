"""Firma de apoyo y verificación a completar por el integrante 3.
Autor principal: Integrante 3 (Silva Augusto Tomas)
"""
import hashlib  # Selecciona SHA-256 para autenticar el contenido.
import hmac  # Usa una comparación segura para las firmas.
import time  # Comprueba la ventana temporal.
from fenix.utils.errors import ValidationError  # Rechazo de autenticación público.


def signature(secret: str, timestamp: int, body: bytes) -> str:
    """Firma exactamente timestamp, punto y cuerpo binario original.

    Args:
        secret: Secreto compartido de al menos 16 caracteres, leído del entorno.
        timestamp: Unix epoch en segundos, igual al enviado en la cabecera.
        body: Bytes exactos enviados por HTTP; no volver a serializar el JSON.
    Returns:
        HMAC-SHA256 hexadecimal de 64 caracteres.
    Raises:
        ValidationError: Si el secreto es demasiado corto.
    """
    if len(secret) < 16:  # Se comprueba también al enviar la prueba.
        raise ValidationError("Configurar WEBHOOK_SECRET de 16 caracteres o más")
    signed = str(timestamp).encode("ascii") + b"." + body  # Prefijo autenticado.
    return hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()


def verify(secret: str, header: str, body: bytes, now: int | None = None) -> None:
    """Rechaza cabeceras ambiguas y firmas o fechas inválidas.

    Args:
        secret: Secreto compartido del entorno, nunca del payload.
        header: Cabecera exacta t=<epoch>,v1=<hex> sin claves duplicadas.
        body: Bytes del cuerpo antes de llamar a json.loads.
        now: Reloj inyectable para pruebas; None usa time.time.
    Returns:
        None si las comprobaciones son satisfactorias.
    Raises:
        ValidationError: Formato inválido, firma incorrecta o diferencia mayor a 300 s.
    Notes:
        La ventana de tiempo mitiga replay antiguo. No impide repetir un evento
        válido dentro de la ventana; event_id e idempotencia cubren ese caso.
    """
    # I3-A: Análisis estricto de la cabecera t/v1 y verificación de HMAC con compare_digest
    if not isinstance(header, str) or not header:
        raise ValidationError("Cabecera X-Webhook-Signature ausente o no es texto")

    parts = header.split(",")
    parsed = {}
    for part in parts:
        if "=" not in part:
            raise ValidationError("Formato de cabecera inválido: fragmento sin '='")
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in parsed:
            raise ValidationError(f"Clave duplicada en cabecera de firma: {k}")
        parsed[k] = v

    if set(parsed.keys()) != {"t", "v1"}:
        raise ValidationError("La cabecera de firma debe contener exactamente 't' y 'v1'")

    # Validar timestamp entero
    try:
        ts = int(parsed["t"])
    except (ValueError, TypeError) as error:
        raise ValidationError("Timestamp 't' debe ser un número entero") from error

    # Validar v1: 64 caracteres hexadecimales
    v1 = parsed["v1"]
    if len(v1) != 64 or not all(c in "0123456789abcdefABCDEF" for c in v1):
        raise ValidationError("Firma 'v1' debe ser un hash hexadecimal de 64 caracteres")

    # Validar ventana temporal de 300 segundos
    current_time = int(time.time()) if now is None else int(now)
    if abs(current_time - ts) > 300:
        raise ValidationError("Timestamp fuera de la ventana de tolerancia permitida (300 segundos)")

    # Calcular firma esperada y comparar de forma segura (mitigando ataques de timing)
    expected = signature(secret, ts, body)
    if not hmac.compare_digest(expected.lower(), v1.lower()):
        raise ValidationError("Firma HMAC inválida o no coincidente")
