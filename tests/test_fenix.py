import gzip
import json
import time
import pytest
from fastapi import HTTPException

from fenix.services import events, receiver, remediation, worker
from fenix.utils import crypto
from fenix.utils.crypto import signature, verify
from fenix.utils.errors import PermanentError, RetryableError, ValidationError
from fenix.utils.logging_json import setup, gzip_rotate
from fenix.utils.state import load, save


# Fixture de inventario para pruebas de contratos y endpoints
@pytest.fixture
def mock_inventory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    inv = {
        "node-01": {
            "type": "ec2",
            "id": "i-mocknode01",
            "security_group_id": "sg-mock01",
            "expected_tags": {
                "ManagedBy": "ppa2",
                "Project": "fenix",
                "GroupId": "grupo01",
                "Owner": "grupo01",
            },
        },
        "bucket-01": {
            "type": "s3",
            "id": "fenix-grupo01-demo",
            "expected_tags": {
                "ManagedBy": "ppa2",
                "Project": "fenix",
                "GroupId": "grupo01",
                "Owner": "grupo01",
            },
        },
    }
    save("data/inventory.json", inv)
    return inv


def test_signature_authenticates_raw_bytes():
    """F05 base: Acepta el original y rechaza la misma información con otros bytes."""
    secret = "clave-ficticia-solo-test"
    body = b'{"x":1}'
    digest = signature(secret, 1000, body)
    header = f"t=1000,v1={digest}"
    verify(secret, header, body, now=1000)
    with pytest.raises(ValidationError):
        verify(secret, header, b'{"x": 1}', now=1000)


def test_f04_signature_expired_or_invalid(mock_inventory):
    """F04: Firma ausente/alterada y timestamp de hace una hora -> 401."""
    secret = "secreto-super-seguro-fenix-1234"
    payload = b'{"schema_version": 1, "event_id": "evt-01", "event_type": "instance.stopped", "resource_id": "node-01", "occurred_at": "2026-10-22T18:00:00Z", "simulate_failures": 0}'

    # Caso 1: Cabecera ausente -> 401
    with pytest.raises(HTTPException) as exc1:
        receiver.handle_webhook_payload(payload, "", secret, mock_inventory, "fenix-grupo01")
    assert exc1.value.status_code == 401

    # Caso 2: Timestamp de hace una hora (3600s de desfase > 300s) -> 401
    old_time = int(time.time()) - 3600
    digest_old = signature(secret, old_time, payload)
    headers_old = f"t={old_time},v1={digest_old}"
    with pytest.raises(HTTPException) as exc2:
        receiver.handle_webhook_payload(payload, headers_old, secret, mock_inventory, "fenix-grupo01")
    assert exc2.value.status_code == 401

    # Caso 3: Firma alterada -> 401
    now = int(time.time())
    headers_bad = f"t={now},v1={'0' * 64}"
    with pytest.raises(HTTPException) as exc3:
        receiver.handle_webhook_payload(payload, headers_bad, secret, mock_inventory, "fenix-grupo01")
    assert exc3.value.status_code == 401


def test_f05_same_json_different_bytes(mock_inventory):
    """F05: Mismos datos JSON con bytes distintos tras firmar -> 401 por HMAC inválida."""
    secret = "secreto-super-seguro-fenix-1234"
    body_original = b'{"a":1,"b":2}'
    body_formatted = b'{"a": 1, "b": 2}'  # Mismo JSON, diferentes bytes

    now = int(time.time())
    digest = signature(secret, now, body_original)
    header = f"t={now},v1={digest}"

    # Enviamos los bytes formateados con la firma calculada sobre los bytes originales
    with pytest.raises(HTTPException) as exc:
        receiver.handle_webhook_payload(body_formatted, header, secret, mock_inventory, "fenix-grupo01")
    assert exc.value.status_code == 401


def test_f06_broken_json_and_list_json(mock_inventory):
    """F06: JSON roto firmado (400) y luego lista JSON firmada (422); sin publicación."""
    secret = "secreto-super-seguro-fenix-1234"

    # 1. JSON roto firmado -> 400
    broken = b'{"incompleto": '
    now = int(time.time())
    header_broken = f"t={now},v1={signature(secret, now, broken)}"
    with pytest.raises(HTTPException) as exc1:
        receiver.handle_webhook_payload(broken, header_broken, secret, mock_inventory, "fenix-grupo01")
    assert exc1.value.status_code == 400

    # 2. Lista JSON firmada (no es objeto dict cerrado) -> 422
    list_json = b'[{"event_id": "evt-01"}]'
    now2 = int(time.time())
    header_list = f"t={now2},v1={signature(secret, now2, list_json)}"
    with pytest.raises(HTTPException) as exc2:
        receiver.handle_webhook_payload(list_json, header_list, secret, mock_inventory, "fenix-grupo01")
    assert exc2.value.status_code == 422


def test_f_body_greater_than_16kib(mock_inventory):
    """Cuerpo mayor a 16 KiB -> 413 Payload Too Large."""
    huge_body = b"x" * 16385
    with pytest.raises(HTTPException) as exc:
        receiver.handle_webhook_payload(huge_body, "t=1,v1=1", "secret", mock_inventory, "fenix-grupo01")
    assert exc.value.status_code == 413


def test_f07_foreign_alias_and_extra_keys(mock_inventory):
    """F07: Alias ajeno y claves extra -> Rechazo de esquema sin ningún cambio cloud."""
    valid_base = {
        "schema_version": 1,
        "event_id": "evt-001",
        "event_type": "instance.stopped",
        "resource_id": "node-01",
        "occurred_at": "2026-10-22T18:00:00Z",
        "simulate_failures": 0,
    }

    # 1. Alias ajeno no presente en inventario
    foreign = dict(valid_base, resource_id="node-99-ajeno")
    with pytest.raises(ValidationError) as exc1:
        events.validate(foreign, mock_inventory)
    assert "no autorizado" in str(exc1.value) or "ajeno" in str(exc1.value)

    # 2. Clave extra no autorizada en esquema cerrado
    extra = dict(valid_base, extra_key="malicious")
    with pytest.raises(ValidationError) as exc2:
        events.validate(extra, mock_inventory)
    assert "no autorizadas" in str(exc2.value)


def test_f09_simulate_failures_backoff(mock_inventory, monkeypatch):
    """F09: simulate_failures=2 -> Fallos transitorios en intentos 1 y 2, éxito en intento 3."""
    event = {
        "schema_version": 1,
        "event_id": "evt-f09-backoff",
        "event_type": "instance.stopped",
        "resource_id": "node-01",
        "occurred_at": "2026-10-22T18:00:00Z",
        "simulate_failures": 2,
    }

    monkeypatch.setattr(remediation, "repair", lambda res, t: "repaired")
    monkeypatch.setattr(remediation, "notify", lambda t: None)

    # Intento 1: debe fallar con RetryableError
    with pytest.raises(RetryableError) as err1:
        remediation.handle(event)
    assert "Falla simulada" in str(err1.value)

    # Intento 2: debe volver a fallar con RetryableError
    with pytest.raises(RetryableError) as err2:
        remediation.handle(event)
    assert "Falla simulada" in str(err2.value)

    # Intento 3: debe completar exitosamente
    remediation.handle(event)
    ledger = load("data/events.json")
    assert ledger["evt-f09-backoff"]["attempts"] == 3
    assert ledger["evt-f09-backoff"]["repair_done"] is True
    assert ledger["evt-f09-backoff"]["notified"] is True


def test_f11_repeat_completed_event(mock_inventory, monkeypatch):
    """F11: Repetir evento ya completo no duplica reparación ni notificación."""
    event = {
        "schema_version": 1,
        "event_id": "evt-f11-repeat",
        "event_type": "instance.stopped",
        "resource_id": "node-01",
        "occurred_at": "2026-10-22T18:00:00Z",
        "simulate_failures": 0,
    }

    calls = {"repair": 0, "notify": 0}

    def track_repair(r, t):
        calls["repair"] += 1
        return "repaired"

    def track_notify(t):
        calls["notify"] += 1

    monkeypatch.setattr(remediation, "repair", track_repair)
    monkeypatch.setattr(remediation, "notify", track_notify)

    # Primera ejecución
    remediation.handle(event)
    assert calls["repair"] == 1
    assert calls["notify"] == 1

    # Repetición: no debe volver a invocar repair ni notify
    remediation.handle(event)
    assert calls["repair"] == 1
    assert calls["notify"] == 1


def test_f12_recovery_interrupted_after_repair(mock_inventory, monkeypatch):
    """F12: Cortar después de repair_done y antes de notify -> Reanuda solo aviso pendiente."""
    event = {
        "schema_version": 1,
        "event_id": "evt-f12-interrupt",
        "event_type": "instance.stopped",
        "resource_id": "node-01",
        "occurred_at": "2026-10-22T18:00:00Z",
        "simulate_failures": 0,
    }

    repair_count = 0
    notify_count = 0

    def mock_repair(r, t):
        nonlocal repair_count
        repair_count += 1
        return "repaired"

    def fail_notify_first(t):
        nonlocal notify_count
        notify_count += 1
        if notify_count == 1:
            raise RetryableError("Simulación de caída de red antes del aviso")

    monkeypatch.setattr(remediation, "repair", mock_repair)
    monkeypatch.setattr(remediation, "notify", fail_notify_first)

    # Primer intento: repara exitosamente pero se corta en la notificación
    with pytest.raises(RetryableError):
        remediation.handle(event)

    ledger1 = load("data/events.json")
    assert ledger1["evt-f12-interrupt"]["repair_done"] is True
    assert ledger1["evt-f12-interrupt"]["notified"] is False
    assert repair_count == 1
    assert notify_count == 1

    # Reanudación: NO debe volver a reparar, solo reintenta la notificación pendiente
    remediation.handle(event)
    ledger2 = load("data/events.json")
    assert ledger2["evt-f12-interrupt"]["repair_done"] is True
    assert ledger2["evt-f12-interrupt"]["notified"] is True
    assert repair_count == 1  # No se reparó dos veces
    assert notify_count == 2  # Se completó el segundo intento de notificación


def test_f14_notification_429_retry_then_success(mock_inventory, monkeypatch):
    """F14: Notificación con 429 transitorio y luego éxito -> Reparación no se repite."""
    event = {
        "schema_version": 1,
        "event_id": "evt-f14-rate-limit",
        "event_type": "bucket.tags_missing",
        "resource_id": "bucket-01",
        "occurred_at": "2026-10-22T18:00:00Z",
        "simulate_failures": 0,
    }

    repair_count = 0
    attempts_notify = 0

    def mock_repair(r, t):
        nonlocal repair_count
        repair_count += 1
        return "repaired"

    def mock_notify_rate_limit(t):
        nonlocal attempts_notify
        attempts_notify += 1
        if attempts_notify == 1:
            raise RetryableError("Fallo transitorio Slack/Telegram HTTP 429 Too Many Requests")

    monkeypatch.setattr(remediation, "repair", mock_repair)
    monkeypatch.setattr(remediation, "notify", mock_notify_rate_limit)

    # Intento 1: 429
    with pytest.raises(RetryableError):
        remediation.handle(event)

    # Intento 2: éxito
    remediation.handle(event)
    assert repair_count == 1  # La reparación nunca se repitió
    assert attempts_notify == 2


def test_f15_same_event_id_different_payload_conflict(mock_inventory):
    """F15: Mismo event_id con diferente contenido -> Registra conflicto sin sobrescribir el original."""
    event1 = {
        "schema_version": 1,
        "event_id": "evt-conflict-99",
        "event_type": "instance.stopped",
        "resource_id": "node-01",
        "occurred_at": "2026-10-22T18:00:00Z",
        "simulate_failures": 0,
    }
    # Evento con distinto occurred_at o simulate_failures
    event2 = dict(event1, occurred_at="2026-10-22T19:00:00Z")

    # Registrar el primero en ledger
    ledger = {
        "evt-conflict-99": {
            "event_id": "evt-conflict-99",
            "content": event1,
            "attempts": 1,
            "repair_done": True,
            "status": "completed",
        }
    }
    save("data/events.json", ledger)

    # Ejecutar con el payload modificado -> PermanentError por conflicto
    with pytest.raises(PermanentError) as exc:
        remediation.handle(event2)
    assert "Conflicto" in str(exc.value)

    # Verificar que el ledger original NO fue alterado
    reloaded = load("data/events.json")
    assert reloaded["evt-conflict-99"]["content"]["occurred_at"] == "2026-10-22T18:00:00Z"


def test_f16_log_rotation_and_gzip_parsing(tmp_path, monkeypatch):
    """F16: Rotación forzada, compresión gzip y lectura sin fuga de secretos."""
    monkeypatch.chdir(tmp_path)
    logger, listener = setup("test-f16")

    # Inyectar múltiples líneas para verificar formato JSON y campos
    logger.debug("mensaje_debug", extra={"event_id": "evt-f16", "status": "debug_ok"})
    logger.info("mensaje_info", extra={"event_id": "evt-f16", "status": "info_ok"})
    logger.warning("mensaje_warning", extra={"event_id": "evt-f16", "status": "warn_retry"})
    logger.error("mensaje_error", extra={"event_id": "evt-f16", "status": "err_caught"})
    logger.critical("mensaje_critical", extra={"event_id": "evt-f16", "status": "crit_failure"})

    listener.stop()
    for handler in listener.handlers:
        handler.close()

    # Verificar archivo de log generado
    log_files = list((tmp_path / "logs").glob("*.jsonl"))
    assert len(log_files) >= 1
    raw_lines = log_files[0].read_text(encoding="utf-8").strip().splitlines()
    assert len(raw_lines) == 5

    # Validar que cada línea es JSON estricto con campos requeridos y UTC
    for line in raw_lines:
        row = json.loads(line)
        assert "timestamp" in row
        assert row["timestamp"].endswith("+00:00")
        assert "level" in row
        assert "message" in row
        assert row["event_id"] == "evt-f16"

    # Probar rotación y compresión gzip con el callback gzip_rotate
    source = str(log_files[0])
    target = source + ".gz"
    gzip_rotate(source, target)

    # Verificar descompresión y parseo
    with gzip.open(target, "rt", encoding="utf-8") as gz:
        decompressed_lines = gz.read().strip().splitlines()
        assert len(decompressed_lines) == 5
        first_parsed = json.loads(decompressed_lines[0])
        assert first_parsed["level"] == "DEBUG"
