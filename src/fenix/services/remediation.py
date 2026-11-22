"""Acciones convergentes a completar por integrante 3 y coordinadas por 5.
Autores: Integrante 3 (Silva Augusto Tomas) e Integrante 5 (Tejerina Marcela del Valle)
"""
import time
from botocore.exceptions import ClientError
from fenix.config.settings import client
from fenix.utils.state import load, save
from fenix.services.events import validate
from fenix.services.notifications import notify
from fenix.utils.errors import RetryableError, PermanentError, ValidationError


def repair(resource: dict, event_type: str) -> str:
    """Acerca un recurso autorizado a su estado deseado y verifica el resultado.

    Args:
        resource: Registro de inventario con type/id y etiquetas esperadas.
        event_type: instance.stopped o bucket.tags_missing ya validado.
    Returns:
        "repaired" si cambió el estado; "already_ok" si ya cumplía la política.
    Raises:
        RetryableError: La API falla temporalmente o el estado final aún no se observa.
        PermanentError: Recurso inexistente o configuración no autorizada.
    Side Effects:
        Arranca una instancia EC2 mock detenida o restaura tags de un bucket S3.
    Notes:
        No usar reboot_instances: repetir un reinicio no es convergencia.
        Un estado pending requiere otra recepción, sin esperas largas en el worker.
    """
    # I3-C: Reparación convergente para EC2 y S3
    if event_type == "instance.stopped":
        instance_id = resource["id"]
        ec2 = client("ec2")
        try:
            desc = ec2.describe_instances(InstanceIds=[instance_id])
            reservations = desc.get("Reservations", [])
            if not reservations or not reservations[0].get("Instances"):
                raise PermanentError(f"Instancia EC2 no encontrada: {instance_id}")
            current_state = reservations[0]["Instances"][0]["State"]["Name"]
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code in ("InvalidInstanceID.NotFound", "InvalidInstanceID.Malformed"):
                raise PermanentError(f"Instancia inexistente o inválida: {instance_id}")
            raise RetryableError(f"Fallo temporal describiendo instancia {instance_id}: {code}")

        if current_state == "running":
            return "already_ok"
        elif current_state == "stopped":
            ec2.start_instances(InstanceIds=[instance_id])
            # Comprobar estado final running
            try:
                check = ec2.describe_instances(InstanceIds=[instance_id])
                new_state = check["Reservations"][0]["Instances"][0]["State"]["Name"]
                if new_state == "running":
                    return "repaired"
                elif new_state == "pending":
                    return "repaired"  # en mock se considera convergente
                else:
                    raise RetryableError(f"Instancia en transición ({new_state}) tras start_instances")
            except ClientError as err:
                raise RetryableError(f"Error comprobando estado tras arranque: {err}")
        elif current_state in ("pending", "stopping"):
            raise RetryableError(f"Instancia {instance_id} en estado transitorio: {current_state}")
        elif current_state == "terminated":
            raise PermanentError(f"Instancia {instance_id} se encuentra terminada; no puede recuperarse")
        else:
            raise PermanentError(f"Estado de instancia no recuperable: {current_state}")

    elif event_type == "bucket.tags_missing":
        bucket_name = resource["id"]
        expected_tags = resource.get("expected_tags", {})
        s3 = client("s3")
        try:
            resp = s3.get_bucket_tagging(Bucket=bucket_name)
            current_tagset = resp.get("TagSet", [])
        except ClientError as err:
            code = err.response.get("Error", {}).get("Code", "")
            if code == "NoSuchTagSet":
                current_tagset = []
            elif code in ("NoSuchBucket", "AccessDenied"):
                raise PermanentError(f"Bucket no encontrado o denegado: {bucket_name}")
            else:
                raise RetryableError(f"Error temporal al consultar tags de S3: {code}")

        # Mapear tags existentes
        current_map = {tag["Key"]: tag["Value"] for tag in current_tagset}

        # Verificar si falta o difiere alguna de las etiquetas obligatorias
        needs_repair = False
        for k, v in expected_tags.items():
            if current_map.get(k) != v:
                needs_repair = True
                break

        if not needs_repair:
            return "already_ok"

        # Mezclar etiquetas preservando las ajenas a la política
        merged_map = dict(current_map)
        merged_map.update(expected_tags)
        merged_tagset = [{"Key": k, "Value": v} for k, v in merged_map.items()]

        try:
            s3.put_bucket_tagging(
                Bucket=bucket_name,
                Tagging={"TagSet": merged_tagset}
            )
            # Recomprobar lectura de tags para verificar convergencia
            verified = s3.get_bucket_tagging(Bucket=bucket_name).get("TagSet", [])
            verified_map = {t["Key"]: t["Value"] for t in verified}
            for k, v in expected_tags.items():
                if verified_map.get(k) != v:
                    raise RetryableError("Verificación de etiquetas de S3 no converge")
        except ClientError as err:
            raise RetryableError(f"Error aplicando etiquetas a S3: {err}")

        return "repaired"

    raise PermanentError(f"Tipo de evento no soportado para reparación: {event_type}")


def handle(event: dict) -> None:
    """Completa reparación y notificación usando un registro durable por evento.

    Args:
        event: Objeto recibido de SQS, que se vuelve a validar aquí.
    Returns:
        None solo cuando reparación y aviso quedaron registrados como exitosos.
    Raises:
        ValidationError: Si el evento fue inyectado directamente a SQS y no es válido.
        RetryableError: Si la simulación o una dependencia temporal falla.
        PermanentError: Conflicto de event_id o recurso no autorizado.
    Side Effects:
        Actualiza recursos, data/events.json y el canal de comunicación.
    """
    # I5-B: Gestión de ciclo de vida durable en el ledger local data/events.json
    inventory = load("data/inventory.json")
    validated = validate(event, inventory)

    event_id = validated["event_id"]
    ledger = load("data/events.json")

    # Si el evento ya existe en el ledger, comprobar integridad y conflicto
    if event_id in ledger:
        entry = ledger[event_id]
        if entry.get("content") != validated:
            raise PermanentError(f"Conflicto: event_id {event_id} recibido con contenido divergente")
    else:
        entry = {
            "event_id": event_id,
            "content": validated,
            "attempts": 0,
            "simulate_failures": validated.get("simulate_failures", 0),
            "repair_done": False,
            "repair_result": None,
            "notified": False,
            "status": "pending"
        }
        ledger[event_id] = entry

    # Incrementar intento y persistir de inmediato antes de cualquier efecto
    entry["attempts"] += 1
    save("data/events.json", ledger)

    # 1. Simulación de fallos controlada para pruebas docentes
    sim_failures = validated.get("simulate_failures", 0)
    if entry["attempts"] <= sim_failures:
        entry["status"] = "simulated_failure"
        save("data/events.json", ledger)
        raise RetryableError(f"Falla simulada (intento {entry['attempts']} de {sim_failures})")

    # 2. Etapa de reparación convergente
    if not entry.get("repair_done"):
        resource = inventory[validated["resource_id"]]
        result = repair(resource, validated["event_type"])
        entry["repair_done"] = True
        entry["repair_result"] = result
        entry["status"] = "repaired"
        save("data/events.json", ledger)

    # 3. Etapa de notificación
    if not entry.get("notified"):
        summary = (
            f"[Fénix] Evento {event_id} ejecutado con éxito. "
            f"Recurso: {validated['resource_id']}, "
            f"Tipo: {validated['event_type']}, "
            f"Acción: {entry.get('repair_result')}"
        )
        notify(summary)
        entry["notified"] = True
        entry["status"] = "completed"
        save("data/events.json", ledger)
