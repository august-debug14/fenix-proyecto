import json
import pytest
from fenix.services import worker, remediation
from fenix.utils.logging_json import setup
from fenix.utils.state import load, save


@pytest.mark.pendiente
def test_duplicate_is_idempotent(tmp_path, monkeypatch):
    """Comprueba que repetir un evento no repite recursos ni acciones completadas.

    Returns:
        None cuando dos ejecuciones conservan el estado final esperado.
    """
    # I6-B: Aislar directorio de ejecución con tmp_path
    monkeypatch.chdir(tmp_path)

    # Preparar inventario válido en data/inventory.json
    inventory = {
        "node-01": {
            "type": "ec2",
            "id": "i-mock123456",
            "security_group_id": "sg-mock123",
            "expected_tags": {
                "ManagedBy": "ppa2",
                "Project": "fenix",
                "GroupId": "grupo01",
                "Owner": "grupo01"
            }
        }
    }
    save("data/inventory.json", inventory)

    # Espiar llamadas a repair y notify
    repair_invocations = []
    notify_invocations = []

    def fake_repair(res, event_type):
        repair_invocations.append((res["id"], event_type))
        return "repaired"

    def fake_notify(text):
        notify_invocations.append(text)

    monkeypatch.setattr(remediation, "repair", fake_repair)
    monkeypatch.setattr(remediation, "notify", fake_notify)

    event = {
        "schema_version": 1,
        "event_id": "evt-idempotent-01",
        "event_type": "instance.stopped",
        "resource_id": "node-01",
        "occurred_at": "2026-10-22T18:00:00Z",
        "simulate_failures": 0
    }

    # Primera ejecución: debe reparar y notificar
    remediation.handle(event)
    assert len(repair_invocations) == 1
    assert len(notify_invocations) == 1

    ledger = load("data/events.json")
    assert ledger["evt-idempotent-01"]["attempts"] == 1
    assert ledger["evt-idempotent-01"]["repair_done"] is True
    assert ledger["evt-idempotent-01"]["notified"] is True

    # Segunda ejecución del mismo evento: idempotente, no repite efectos externos
    remediation.handle(event)
    assert len(repair_invocations) == 1  # No se volvió a llamar a repair
    assert len(notify_invocations) == 1  # No se volvió a enviar notificación
    
    # El ledger refleja el segundo intento sin perder los flags de éxito
    ledger_after = load("data/events.json")
    assert ledger_after["evt-idempotent-01"]["attempts"] == 2
    assert ledger_after["evt-idempotent-01"]["repair_done"] is True
    assert ledger_after["evt-idempotent-01"]["notified"] is True


@pytest.mark.pendiente
def test_ack_is_after_success(tmp_path, monkeypatch):
    """Verifica que un handler fallido jamás confirma la entrega de SQS.

    Returns:
        None si fallo=retry sin ACK y éxito=ACK exactamente al finalizar.
    """
    monkeypatch.chdir(tmp_path)
    logger, listener = setup("test-ack")

    try:
        call_sequence = []

        fake_message = {
            "MessageId": "msg-test-123",
            "ReceiptHandle": "receipt-test-123",
            "Body": json.dumps({
                "schema_version": 1,
                "event_id": "evt-ack-01",
                "event_type": "instance.stopped",
                "resource_id": "node-01",
                "occurred_at": "2026-10-22T18:00:00Z",
                "simulate_failures": 0
            }),
            "Attributes": {"ApproximateReceiveCount": "1"}
        }

        # Simular lectura de un mensaje
        monkeypatch.setattr(worker, "receive", lambda url: [fake_message])

        def fake_ack(url, receipt):
            call_sequence.append(("acknowledge", receipt))

        def fake_retry(url, msg):
            call_sequence.append(("retry_later", msg["ReceiptHandle"]))
            return 2

        monkeypatch.setattr(worker, "acknowledge", fake_ack)
        monkeypatch.setattr(worker, "retry_later", fake_retry)

        # 1. Caso Fallido: el handler lanza excepción -> nunca debe haber ACK
        def failing_handler(evt):
            call_sequence.append(("handler", "error"))
            raise RuntimeError("Falla deliberada de prueba")

        worker.run("http://localhost:4566/000000000000/test-queue", failing_handler, logger, once=True)
        assert ("acknowledge", "receipt-test-123") not in call_sequence
        assert ("retry_later", "receipt-test-123") in call_sequence
        assert call_sequence == [("handler", "error"), ("retry_later", "receipt-test-123")]

        # 2. Caso Exitoso: handler completa -> ACK obligatorio y posterior
        call_sequence.clear()

        def success_handler(evt):
            call_sequence.append(("handler", "success"))

        worker.run("http://localhost:4566/000000000000/test-queue", success_handler, logger, once=True)
        assert call_sequence == [("handler", "success"), ("acknowledge", "receipt-test-123")]

    finally:
        listener.stop()


@pytest.mark.integracion
def test_redrive_with_localstack():
    """Comprueba DLQ efectiva usando el broker, no una lista simulada en Python.

    Returns:
        None si un mensaje venenoso termina en la DLQ y el worker sigue vivo.
    """
    from fenix.config.settings import client
    try:
        sqs = client("sqs")
        sqs.list_queues()
    except Exception as exc:
        pytest.skip(f"LocalStack no disponible en loopback:4566 ({exc})")

    from fenix.services import sqs as fenix_sqs
    test_prefix = "fenix-test-redrive"
    work_url, dlq_url = fenix_sqs.ensure_queues(test_prefix)

    # Inyectar un mensaje directamente con JSON corrupto
    sqs.send_message(QueueUrl=work_url, MessageBody="corrupt-not-json-content")

    # Consumir hasta que agote las recepciones máximas (maxReceiveCount=3)
    logger, listener = setup("test-redrive")
    try:
        for _ in range(4):
            worker.run(work_url, lambda x: None, logger, once=True)

        # Consultar la DLQ
        dlq_msgs = sqs.receive_message(QueueUrl=dlq_url, MaxNumberOfMessages=1, WaitTimeSeconds=2).get("Messages", [])
        assert len(dlq_msgs) > 0
        assert dlq_msgs[0]["Body"] == "corrupt-not-json-content"
    finally:
        listener.stop()
        try:
            sqs.delete_queue(QueueUrl=work_url)
            sqs.delete_queue(QueueUrl=dlq_url)
        except Exception:
            pass
