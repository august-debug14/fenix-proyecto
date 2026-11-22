"""Plantilla central de integración: un worker, un mensaje por vez.
Autor principal: Integrante 5 (Tejerina Marcela del Valle)
"""
import json  # Convierte Body solo dentro del control por mensaje.
import time  # Evita un bucle intensivo si el emulador no responde.
from botocore.exceptions import BotoCoreError, ClientError
from fenix.services.sqs import receive, acknowledge, retry_later
from fenix.utils.errors import ProjectError, ValidationError


def run(url: str, handler, logger, once: bool = False) -> None:
    """Consume mensajes y confirma únicamente resultados durables.

    Args:
        url: URL de la cola de trabajo, obtenida por get_queue_url.
        handler: Callable handler(event) que revalida y ejecuta el caso de negocio.
        logger: Logger JSON del proceso worker.
        once: Si es True, realiza una sola consulta y termina; útil para pruebas.
    Returns:
        None al terminar la consulta única o al detenerse mediante Ctrl+C.
    Raises:
        NotImplementedError: Si todavía no se completaron las plantillas del alumno.
    Side Effects:
        Recibe mensajes, ejecuta el handler y cambia o confirma su recepción.
    Notes:
        El parseo ocurre por mensaje para que un JSON corrupto no termine el worker
        ni oculte los otros mensajes de la cola.
    """
    # I5-A: Ciclo de vida y consumo desacoplado con long polling y confirmación posterior
    print(f"[WORKER] En escucha activa sobre la cola SQS... Esperando alertas.")
    while True:
        try:
            messages = receive(url)
        except (BotoCoreError, ClientError) as transport_err:
            logger.error("error_transporte_sqs", extra={"status": type(transport_err).__name__})
            time.sleep(2)
            if once:
                break
            continue

        if not messages:
            if once:
                break
            continue

        for message in messages:
            msg_id = message.get("MessageId")
            receipt = message.get("ReceiptHandle")
            body_str = message.get("Body", "")

            try:
                # Parseo y validación de objeto por mensaje para aislar corrupción
                try:
                    event_data = json.loads(body_str)
                except (json.JSONDecodeError, UnicodeDecodeError) as parse_err:
                    raise ValidationError(f"JSON corrupto en SQS: {parse_err}") from parse_err

                if not isinstance(event_data, dict):
                    raise ValidationError("El cuerpo del mensaje no es un objeto JSON (dict)")

                # Ejecutar handler de negocio con persistencia intermedia en ledger
                print(f"[WORKER] Mensaje recibido de SQS ({msg_id}). Procesando evento {event_data.get('event_id')}...")
                handler(event_data)

                # Confirmar a SQS únicamente tras el éxito durable comprobado (ACK)
                acknowledge(url, receipt)
                print(f"[WORKER] Evento {event_data.get('event_id')} autorreparado y confirmado en SQS (ACK).\n")
                logger.info(
                    "mensaje_procesado_exitosamente",
                    extra={
                        "event_id": event_data.get("event_id"),
                        "message_id": msg_id,
                        "resource_id": event_data.get("resource_id"),
                        "action": "acknowledge",
                        "status": "acknowledged"
                    }
                )

            except NotImplementedError:
                # Revela TODOs pendientes; no simular éxito
                raise
            except (ProjectError, BotoCoreError, ClientError, Exception) as proc_err:
                logger.warning(
                    "fallo_en_procesamiento",
                    extra={
                        "message_id": msg_id,
                        "status": type(proc_err).__name__,
                        "action": "retry_scheduled"
                    }
                )
                # Reprogramar reintento con backoff exponencial sobre el ReceiptHandle
                try:
                    retry_later(url, message)
                except (BotoCoreError, ClientError) as backoff_err:
                    logger.error(
                        "error_programando_backoff",
                        extra={
                            "message_id": msg_id,
                            "status": type(backoff_err).__name__
                        }
                    )
                    time.sleep(2)

        if once:
            break
