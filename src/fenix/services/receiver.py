"""Receptor FastAPI: autenticación, validación y aceptación durable por SQS.
Autor principal: Integrante 3 (Silva Augusto Tomas)
"""
import os  # Recupera el secreto sin incluirlo en el repositorio.
import json  # Decodifica después de verificar los bytes.
from fastapi import FastAPI, Request, HTTPException  # Interfaz HTTP del curso.
from starlette.concurrency import run_in_threadpool  # Evita bloquear el event loop.
from botocore.exceptions import BotoCoreError, ClientError
from fenix.utils.crypto import verify
from fenix.utils.errors import ValidationError
from fenix.utils.state import load
from fenix.services.events import validate
from fenix.services.sqs import queue_url, publish

app = FastAPI(title="Fénix receptor de alertas")  # Un servidor, un worker Uvicorn.


def handle_webhook_payload(raw: bytes, signature_header: str, secret: str,
                           inventory: dict, prefix: str) -> dict:
    """Procesa, autentica y encola el webhook de forma síncrona y segura.

    Args:
        raw: Bytes brutos del cuerpo recibido.
        signature_header: Valor de la cabecera X-Webhook-Signature.
        secret: Secreto compartido leído de las variables de entorno.
        inventory: Diccionario cargado desde data/inventory.json.
        prefix: Prefijo de colas asignado al grupo.
    Returns:
        Diccionario con status=accepted, event_id y message_id.
    Raises:
        HTTPException: 413 (cuerpo > 16KiB), 401 (firma), 400 (JSON roto),
            422 (contrato inválido o alias ajeno), 503 (SQS no disponible).
    """
    # 1. Limitar tamaño del payload a 16 KiB
    if len(raw) > 16384:
        raise HTTPException(status_code=413, detail="Cuerpo demasiado grande")

    # 2. Autenticar firma HMAC sobre los raw bytes
    try:
        verify(secret, signature_header, raw)
    except ValidationError:
        raise HTTPException(status_code=401, detail="Firma ausente, malformada o inválida")

    # 3. Decodificar JSON
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="JSON roto o UTF-8 inválido")

    # 4. Validar esquema cerrado contra el inventario
    try:
        validated_event = validate(data, inventory)
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=str(error))

    # 5. Publicar en cola SQS
    try:
        work_url = queue_url(prefix)
        msg_id = publish(work_url, validated_event)
    except (BotoCoreError, ClientError):
        raise HTTPException(status_code=503, detail="Servicio de colas SQS no disponible")

    return {
        "status": "accepted",
        "event_id": validated_event["event_id"],
        "message_id": msg_id
    }


@app.post("/webhook", status_code=202)
async def webhook(request: Request) -> dict:
    """Recibe un evento firmado y contesta 202 solo después de encolarlo.

    Args:
        request: Petición con X-Webhook-Signature y cuerpo de hasta 16 KiB.
    Returns:
        JSON con status=accepted, event_id y message_id de SQS.
    Raises:
        HTTPException: 401 firma; 400 JSON; 413 tamaño; 422 contrato; 503 SQS.
    Side Effects:
        Publica en SQS; no repara recursos ni llama a Telegram/Slack aquí.
    Notes:
        Boto3 es bloqueante: el endpoint async delega en run_in_threadpool
        para no bloquear el bucle de eventos de FastAPI.
    """
    raw = await request.body()
    header = request.headers.get("X-Webhook-Signature", "")
    secret = os.environ.get("WEBHOOK_SECRET", "")
    prefix = os.environ.get("QUEUE_PREFIX", "fenix-grupo01")

    # Leer inventario y ejecutar llamadas bloqueantes fuera del event loop
    inventory = await run_in_threadpool(load, "data/inventory.json")
    return await run_in_threadpool(
        handle_webhook_payload, raw, header, secret, inventory, prefix
    )
