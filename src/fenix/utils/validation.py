"""Contratos de validación a completar por el integrante 1.
Autor principal: Integrante 1 (Silva Augusto Tomas)
"""
import re  # Comprueba el formato de los identificadores.
from datetime import datetime, timezone, timedelta  # Trabaja con fechas conscientes de UTC.
from fenix.utils.errors import ValidationError  # Error público del dominio.


def identifier(value: str) -> str:
    """Valida un identificador corto y devuelve su texto sin modificar.

    Args:
        value: Texto entre 3 y 32 caracteres; letras minúsculas, dígitos o guion.
    Returns:
        El identificador original válido.
    Raises:
        ValidationError: Si el tipo o el patrón no cumplen el contrato.
    Examples:
        identifier("grupo-01") devuelve "grupo-01"; "../ajeno" debe fallar.
    """
    # I1-A: validamos que sea string y cumpla el regex exacto sin alterar mayúsculas
    if not isinstance(value, str):
        raise ValidationError("El identificador debe ser una cadena de texto")
    
    if not re.fullmatch(r"[a-z0-9-]{3,32}", value):
        raise ValidationError(f"Identificador inválido: {value}")
    
    return value


def ttl_seconds(value: str) -> int:
    """Valida el TTL didáctico empleado en la opción de entornos efímeros.

    Args:
        value: Texto con un entero entre 60 y 600 segundos inclusive.
    Returns:
        El número entero de segundos.
    Raises:
        ValidationError: Si no es entero o está fuera de los límites.
    Notes:
        La CLI traduce ValidationError a parser.error para salir con código 2.
    """
    # I1-B: conversión a entero y verificación de rango acotado [60, 600]
    if not isinstance(value, str):
        raise ValidationError("El TTL debe proporcionarse como texto")
    
    try:
        ttl = int(value)
    except (ValueError, TypeError) as error:
        raise ValidationError("El TTL debe ser un número entero válido") from error

    if ttl < 60 or ttl > 600:
        raise ValidationError("El TTL debe encontrarse entre 60 y 600 segundos")

    return ttl


def utc_text(value: str) -> datetime:
    """Interpreta una fecha UTC explícita sin aceptar fechas ingenuas.

    Args:
        value: ISO 8601 terminado en Z o con offset +00:00.
    Returns:
        Fecha con zona UTC; por ejemplo 2026-10-22T18:00:00+00:00.
    Raises:
        ValidationError: Si falta zona, hay offset no nulo o formato inválido.
    """
    # I1-C: aseguramos fecha con zona horaria UTC explícita y sin offset local
    if not isinstance(value, str):
        raise ValidationError("La fecha debe ser una cadena de texto en formato ISO 8601")

    # Si termina en Z lo pasamos a +00:00 para fromisoformat
    normalized = value.replace("Z", "+00:00") if value.endswith("Z") else value

    try:
        dt = datetime.fromisoformat(normalized)
    except (ValueError, TypeError) as error:
        raise ValidationError(f"Formato de fecha ISO 8601 inválido: {value}") from error

    if dt.tzinfo is None:
        raise ValidationError("La fecha no contiene información de zona horaria (tzinfo ausente)")

    offset = dt.utcoffset()
    if offset is None or offset != timedelta(0):
        raise ValidationError("La fecha debe tener zona horaria UTC explícita (+00:00 o Z)")

    return dt
