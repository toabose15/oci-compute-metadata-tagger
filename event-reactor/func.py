import io
import json
import logging
import time

import oci
from fdk import response
from oci.exceptions import ServiceError


LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

LAUNCH_INSTANCE_END_EVENT = "com.oraclecloud.computeapi.launchinstance.end"

TAG_NAMESPACE = "Instances"
NETWORK_SCOPE_KEY = "NetworkScope"
PRIVATE_IP_KEY = "PrivateIP"
PUBLIC_IP_KEY = "PublicIP"
PLATFORM_KEY = "Platform"
OS_KEY = "OS"
OS_VERSION_KEY = "OSVersion"
SHAPE_FAMILY_KEY = "ShapeFamily"
SHAPE_KEY = "Shape"
PROCESSOR_KEY = "Processor"
OCPUS_KEY = "OCPUs"
MEMORY_GB_KEY = "MemoryGB"
PUBLIC_VALUE = "Public"
PRIVATE_VALUE = "Private"
UNKNOWN_VALUE = "Unknown"


def handler(ctx, data: io.BytesIO = None):
    try:
        event = json.loads((data.getvalue() if data else b"{}").decode("utf-8"))
        event_type = event.get("eventType")
        event_id = event.get("eventID")
        LOGGER.info("Received event eventType=%s eventID=%s", event_type, event_id)

        if event_type != LAUNCH_INSTANCE_END_EVENT:
            LOGGER.info("Ignoring unsupported eventType=%s eventID=%s", event_type, event_id)
            return _respond(ctx, 202, {"status": "ignored", "eventType": event_type})

        event_data = event.get("data") or {}
        instance_id = event_data.get("resourceId")
        compartment_id = event_data.get("compartmentId")
        if not instance_id or not compartment_id:
            LOGGER.error("Missing instance or compartment OCID eventID=%s", event_id)
            return _respond(ctx, 400, {"status": "error", "message": "Missing instance or compartment OCID"})

        max_lookup_seconds = _int_config(ctx, "MAX_LOOKUP_SECONDS", 30)
        lookup_sleep_seconds = _float_config(ctx, "LOOKUP_SLEEP_SECONDS", 5)
        LOGGER.info(
            "Processing eventID=%s instance_id=%s compartment_id=%s maxLookupSeconds=%s lookupSleepSeconds=%s",
            event_id,
            instance_id,
            compartment_id,
            max_lookup_seconds,
            lookup_sleep_seconds,
        )

        signer = oci.auth.signers.get_resource_principals_signer()
        compute = oci.core.ComputeClient(config={}, signer=signer)
        network = oci.core.VirtualNetworkClient(config={}, signer=signer)
        LOGGER.info("OCI clients initialized for eventID=%s instance_id=%s", event_id, instance_id)

        instance = compute.get_instance(instance_id).data
        LOGGER.info("Loaded instance instance_id=%s shape=%s image_id=%s", instance_id, instance.shape, _image_id(instance))

        vnics = _wait_for_vnics(
            compute,
            network,
            compartment_id,
            instance_id,
            max_lookup_seconds,
            lookup_sleep_seconds,
        )
        if not vnics:
            LOGGER.warning("No VNICs found after wait instance_id=%s", instance_id)
            return _respond(ctx, 202, {"status": "deferred", "message": "No VNICs found yet"})
        LOGGER.info("Resolved %s VNIC(s) for instance_id=%s", len(vnics), instance_id)

        tags = {}
        tags.update(_network_tags(vnics))
        tags.update(_compute_tags(compute, instance))
        LOGGER.info("Built desired tags for instance_id=%s tags=%s", instance_id, tags)

        changed = _sync_defined_tags(compute, instance_id, tags)
        LOGGER.info("Tag sync complete instance_id=%s changed=%s", instance_id, changed)

        return _respond(
            ctx,
            200,
            {
                "status": "updated" if changed else "unchanged",
                "instanceId": instance_id,
                "tags": {TAG_NAMESPACE: tags},
            },
        )
    except ServiceError as exc:
        LOGGER.exception("OCI service error")
        return _respond(ctx, exc.status or 500, {"status": "error", "message": exc.message})
    except Exception as exc:
        LOGGER.exception("Unexpected error")
        return _respond(ctx, 500, {"status": "error", "message": str(exc)})


def _wait_for_vnics(compute, network, compartment_id, instance_id, max_seconds, sleep_seconds):
    deadline = time.monotonic() + max_seconds
    last_vnics = []
    attempt = 0

    while True:
        attempt += 1
        vnics = _get_vnics_once(compute, network, compartment_id, instance_id)
        LOGGER.info("VNIC lookup attempt=%s instance_id=%s vnics_found=%s", attempt, instance_id, len(vnics))
        if vnics:
            last_vnics = vnics
            if any(getattr(vnic, "public_ip", None) for vnic in vnics):
                LOGGER.info("Public IP found for instance_id=%s on attempt=%s", instance_id, attempt)
                return vnics

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return last_vnics

        time.sleep(min(sleep_seconds, remaining))


def _get_vnics_once(compute, network, compartment_id, instance_id):
    attachments = oci.pagination.list_call_get_all_results(
        compute.list_vnic_attachments,
        compartment_id=compartment_id,
        instance_id=instance_id,
    ).data

    vnics = []
    for attachment in attachments:
        vnic_id = getattr(attachment, "vnic_id", None)
        if not vnic_id or getattr(attachment, "lifecycle_state", "") in ("DETACHING", "DETACHED"):
            continue
        try:
            vnics.append(network.get_vnic(vnic_id).data)
        except ServiceError as exc:
            if exc.status != 404:
                raise
    return vnics


def _network_tags(vnics):
    primary_vnic = next((v for v in vnics if getattr(v, "is_primary", False)), vnics[0])
    public_ips = [v.public_ip for v in vnics if getattr(v, "public_ip", None)]
    public_ip = getattr(primary_vnic, "public_ip", None) or (public_ips[0] if public_ips else None)

    tags = {NETWORK_SCOPE_KEY: PUBLIC_VALUE if public_ips else PRIVATE_VALUE}
    if getattr(primary_vnic, "private_ip", None):
        tags[PRIVATE_IP_KEY] = primary_vnic.private_ip
    if public_ip:
        tags[PUBLIC_IP_KEY] = public_ip
    return tags


def _compute_tags(compute, instance):
    image = _get_image(compute, instance)
    os_name = getattr(image, "operating_system", None) or UNKNOWN_VALUE
    os_version = getattr(image, "operating_system_version", None) or UNKNOWN_VALUE
    shape = getattr(instance, "shape", None) or UNKNOWN_VALUE
    shape_config = getattr(instance, "shape_config", None)

    return {
        PLATFORM_KEY: _platform(os_name),
        OS_KEY: os_name,
        OS_VERSION_KEY: os_version,
        SHAPE_FAMILY_KEY: _shape_family(shape),
        SHAPE_KEY: shape,
        PROCESSOR_KEY: _string_value(getattr(shape_config, "processor_description", None)),
        OCPUS_KEY: _string_value(getattr(shape_config, "ocpus", None)),
        MEMORY_GB_KEY: _string_value(getattr(shape_config, "memory_in_gbs", None)),
    }


def _get_image(compute, instance):
    image_id = _image_id(instance)
    if not image_id:
        return None

    try:
        return compute.get_image(image_id).data
    except ServiceError as exc:
        LOGGER.warning("Could not read image %s for instance %s: %s", image_id, instance.id, exc.message)
        return None


def _image_id(instance):
    if getattr(instance, "image_id", None):
        return instance.image_id

    source_details = getattr(instance, "source_details", None)
    if getattr(source_details, "image_id", None):
        return source_details.image_id

    source_id = getattr(source_details, "source_id", None)
    source_type = getattr(source_details, "source_type", None)
    if source_id and (source_type == "image" or source_id.startswith("ocid1.image")):
        return source_id

    return None


def _platform(os_name):
    if not os_name or os_name == UNKNOWN_VALUE:
        return UNKNOWN_VALUE
    return "Windows" if "windows" in os_name.lower() else "Linux"


def _shape_family(shape):
    parts = shape.split(".") if shape and shape != UNKNOWN_VALUE else []
    if len(parts) >= 3 and parts[1] == "Standard":
        return parts[2]
    if len(parts) >= 2:
        return parts[1]
    return shape or UNKNOWN_VALUE


def _sync_defined_tags(compute, instance_id, desired_tags):
    instance_response = compute.get_instance(instance_id)
    defined_tags = dict(instance_response.data.defined_tags or {})
    namespace_tags = dict(defined_tags.get(TAG_NAMESPACE, {}))
    next_namespace_tags = dict(namespace_tags)

    for key in _managed_keys():
        if key in desired_tags:
            next_namespace_tags[key] = desired_tags[key]
        else:
            next_namespace_tags.pop(key, None)

    if namespace_tags == next_namespace_tags:
        return False

    defined_tags[TAG_NAMESPACE] = next_namespace_tags
    compute.update_instance(
        instance_id,
        oci.core.models.UpdateInstanceDetails(defined_tags=defined_tags),
        if_match=instance_response.headers.get("etag"),
    )
    return True


def _managed_keys():
    return (
        NETWORK_SCOPE_KEY,
        PRIVATE_IP_KEY,
        PUBLIC_IP_KEY,
        PLATFORM_KEY,
        OS_KEY,
        OS_VERSION_KEY,
        SHAPE_FAMILY_KEY,
        SHAPE_KEY,
        PROCESSOR_KEY,
        OCPUS_KEY,
        MEMORY_GB_KEY,
    )


def _int_config(ctx, key, default):
    value = (ctx.Config() or {}).get(key, default)
    return int(value)


def _float_config(ctx, key, default):
    value = (ctx.Config() or {}).get(key, default)
    return float(value)


def _string_value(value):
    return UNKNOWN_VALUE if value is None else str(value)


def _respond(ctx, status_code, body):
    return response.Response(
        ctx,
        status_code=status_code,
        response_data=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
