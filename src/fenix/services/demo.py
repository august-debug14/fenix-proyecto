"""Laboratorio acotado de recursos. Integrante 2 prepara, integrante 6 verifica.
Autores: Integrante 2 (Silva Augusto Tomas) e Integrante 6 (Tejerina Marcela del Valle)
"""
import time
from botocore.exceptions import ClientError
from fenix.config.settings import client
from fenix.services import cloud_support
from fenix.utils.errors import ValidationError, PermanentError
from fenix.utils.state import load, save
from fenix.utils.validation import identifier


def setup_demo(group: str) -> dict:
    """Prepara una instancia mock y un bucket a partir de identificadores estables.

    Args:
        group: ID validado del grupo; determina prefijos de los recursos.
    Returns:
        Inventario con aliases node-01 y bucket-01 y sus IDs reales del emulador.
    Raises:
        ClientError: Si el emulador no soporta una operación o falla su ejecución.
    Side Effects:
        Crea recursos de laboratorio etiquetados y data/inventory.json.
    Notes:
        Reutilizar por tags antes de crear y guardar IDs después de cada creación.
        Obtener una ImageId con describe_images; no copiar una AMI de AWS real.
    """
    # I2-C: validar group y preparar etiquetas obligatorias
    identifier(group)
    expected_tags = {
        "ManagedBy": "ppa2",
        "Project": "fenix",
        "GroupId": group,
        "Owner": group,
    }

    # Descubrir VPC e ImageId mock
    vpc_id, image_id = cloud_support.mock_inputs()

    ec2 = client("ec2")
    s3 = client("s3")

    # 1. Crear o reutilizar Security Group exclusivo
    sg_name = f"fenix-{group}-sg"
    sg_id = None
    try:
        sgs = ec2.describe_security_groups(
            Filters=[{"Name": "group-name", "Values": [sg_name]}]
        ).get("SecurityGroups", [])
        if sgs:
            sg_id = sgs[0]["GroupId"]
    except ClientError:
        pass

    if not sg_id:
        sg_tags = {
            "ManagedBy": "ppa2",
            "GroupId": group,
            "Project": "fenix",
            "Name": sg_name,
        }
        sg_id = cloud_support.create_group(sg_name, vpc_id, sg_tags)

    # 2. Crear o reutilizar instancia EC2 mock
    instance_id = None
    try:
        reservations = ec2.describe_instances(
            Filters=[
                {"Name": "tag:Project", "Values": ["fenix"]},
                {"Name": "tag:GroupId", "Values": [group]},
                {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
            ]
        ).get("Reservations", [])
        for r in reservations:
            for inst in r.get("Instances", []):
                instance_id = inst["InstanceId"]
                break
            if instance_id:
                break
    except ClientError:
        pass

    if not instance_id:
        token = f"fenix-{group}-node01-token"
        inst_tags = dict(expected_tags)
        inst_tags["Name"] = f"fenix-{group}-node01"
        instance_id = cloud_support.create_instance(image_id, sg_id, token, inst_tags)

    # 3. Crear o reutilizar bucket S3
    bucket_name = f"fenix-{group}-demo"
    bucket_exists = False
    try:
        buckets = s3.list_buckets().get("Buckets", [])
        bucket_exists = any(b.get("Name") == bucket_name for b in buckets)
    except ClientError:
        bucket_exists = False

    if not bucket_exists:
        cloud_support.create_bucket(bucket_name, expected_tags)
    else:
        # Asegurar las etiquetas obligatorias si el bucket ya existía
        try:
            s3.put_bucket_tagging(
                Bucket=bucket_name,
                Tagging={"TagSet": cloud_support.aws_tags(expected_tags)}
            )
        except ClientError:
            pass

    # Publicar inventario solo con recursos de propiedad verificada
    inventory = {
        "node-01": {
            "type": "ec2",
            "id": instance_id,
            "security_group_id": sg_id,
            "expected_tags": expected_tags,
        },
        "bucket-01": {
            "type": "s3",
            "id": bucket_name,
            "expected_tags": expected_tags,
        },
    }

    save("data/inventory.json", inventory)
    return inventory


def inject_fault(alias: str) -> None:
    """Detiene el nodo o elimina una etiqueta del bucket de laboratorio.

    Args:
        alias: node-01 o bucket-01 presente en inventory.json.
    Returns:
        None cuando el fallo simulado se verifica mediante el SDK.
    Raises:
        ValidationError: Si el alias no pertenece al inventario.
    Side Effects:
        Modifica únicamente el recurso de prueba de ese grupo.
    """
    # I6-A: resolver alias contra el inventario y simular falla observable
    if alias not in ("node-01", "bucket-01"):
        raise ValidationError(f"Alias no autorizado para inyección: {alias}")

    inventory = load("data/inventory.json")
    if not inventory or alias not in inventory:
        raise ValidationError(f"Alias {alias} no registrado en data/inventory.json")

    resource = inventory[alias]

    if alias == "node-01":
        instance_id = resource["id"]
        ec2 = client("ec2")
        ec2.stop_instances(InstanceIds=[instance_id])
        # Comprobar que pase a stopped con límite de tiempo
        for _ in range(15):
            res = ec2.describe_instances(InstanceIds=[instance_id])
            state = res["Reservations"][0]["Instances"][0]["State"]["Name"]
            if state == "stopped":
                return
            time.sleep(0.5)
        # Si no llegó a stopped tras 15 intentos, advertir o salir
    elif alias == "bucket-01":
        bucket_name = resource["id"]
        s3 = client("s3")
        try:
            tagging = s3.get_bucket_tagging(Bucket=bucket_name)
            current_tags = tagging.get("TagSet", [])
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code == "NoSuchTagSet":
                current_tags = []
            else:
                raise

        # Quitar Owner conservando las demás etiquetas
        remaining = [t for t in current_tags if t.get("Key") != "Owner"]
        if remaining:
            s3.put_bucket_tagging(Bucket=bucket_name, Tagging={"TagSet": remaining})
        else:
            s3.delete_bucket_tagging(Bucket=bucket_name)
