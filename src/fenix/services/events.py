"""Contrato de eventos de autorreparación; integrante 1.
Autor principal: Integrante 1 (Silva Augusto Tomas)
"""
from fenix.utils.errors import ValidationError
from fenix.utils.validation import identifier, utc_text


def validate(event: dict, inventory: dict) -> dict:
    """Valida esquema cerrado y resuelve exclusivamente aliases autorizados.

    Args:
        event: JSON objeto con schema_version, event_id, event_type, resource_id,
            occurred_at y simulate_failures.
        inventory: Mapa alias -> recurso, preparado por setup-demo y revisado por el grupo.
    Returns:
        Copia validada del evento; no modifica inventory.
    Raises:
        ValidationError: Tipo, campo, timestamp o recurso no permitido.
    Notes:
        event_type solo permite instance.stopped y bucket.tags_missing.
        simulate_failures solo es de laboratorio: entero de 0 a 5, excluyendo bool.
    """
    # I1-D: Verificación estricta de estructura y tipos
    if not isinstance(event, dict):
        raise ValidationError("El evento debe ser un objeto JSON (diccionario)")

    if not isinstance(inventory, dict):
        raise ValidationError("El inventario debe ser un diccionario válido")

    # Exigir claves exactas (esquema cerrado)
    expected_keys = {
        "schema_version",
        "event_id",
        "event_type",
        "resource_id",
        "occurred_at",
        "simulate_failures",
    }
    actual_keys = set(event.keys())
    if actual_keys != expected_keys:
        missing = expected_keys - actual_keys
        extra = actual_keys - expected_keys
        if missing:
            raise ValidationError(f"Claves ausentes en el evento: {sorted(missing)}")
        if extra:
            raise ValidationError(f"Claves no autorizadas en el evento: {sorted(extra)}")

    # schema_version debe ser exactamente el entero 1 (evitando booleanos)
    schema_ver = event["schema_version"]
    if type(schema_ver) is not int or schema_ver != 1:
        raise ValidationError("schema_version debe ser el número entero 1")

    # simulate_failures debe ser entero entre 0 y 5 (evitando booleanos)
    sim_failures = event["simulate_failures"]
    if type(sim_failures) is not int or sim_failures < 0 or sim_failures > 5:
        raise ValidationError("simulate_failures debe ser un entero entre 0 y 5")

    # Validar formato de identificadores
    identifier(event["event_id"])
    identifier(event["resource_id"])

    # Validar formato y zona horaria UTC de occurred_at
    utc_text(event["occurred_at"])

    # Validar tipos de eventos admitidos
    ev_type = event["event_type"]
    if ev_type not in ("instance.stopped", "bucket.tags_missing"):
        raise ValidationError(f"event_type desconocido o no soportado: {ev_type}")

    # Comprobar pertenencia al inventario del laboratorio
    res_id = event["resource_id"]
    if res_id not in inventory:
        raise ValidationError(f"resource_id no autorizado o ajeno al inventario: {res_id}")

    resource = inventory[res_id]
    if not isinstance(resource, dict):
        raise ValidationError(f"Definición corrupta para el recurso {res_id} en inventario")

    # Verificar compatibilidad entre el evento y el tipo de recurso en inventario
    res_type = resource.get("type")
    if ev_type == "instance.stopped" and res_type != "ec2":
        raise ValidationError(f"Tipo de evento incompatible: {ev_type} requiere un recurso ec2, encontrado {res_type}")
    if ev_type == "bucket.tags_missing" and res_type != "s3":
        raise ValidationError(f"Tipo de evento incompatible: {ev_type} requiere un recurso s3, encontrado {res_type}")

    return dict(event)
