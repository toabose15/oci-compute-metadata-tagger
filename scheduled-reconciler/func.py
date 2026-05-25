import io
import json
import logging

import oci
from fdk import response
from oci.exceptions import ServiceError


LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

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
SKIP_INSTANCE_STATES = {"TERMINATING", "TERMINATED"}


def handler(ctx, data: io.BytesIO = None):
    try:
        root_compartment_id = _config(ctx, "ROOT_COMPARTMENT_ID")
        if not root_compartment_id:
            return _respond(ctx, 400, {"status": "error", "message": "Function config ROOT_COMPARTMENT_ID is required"})
        refresh_static_tags = _bool_config(ctx, "REFRESH_STATIC_TAGS", False)
        LOGGER.info(
            "Starting scheduled reconciler rootCompartmentId=%s refreshStaticTags=%s",
            root_compartment_id,
            refresh_static_tags,
        )

        signer = oci.auth.signers.get_resource_principals_signer()
        identity = oci.identity.IdentityClient(config={}, signer=signer)
        compute = oci.core.ComputeClient(config={}, signer=signer)
        network = oci.core.VirtualNetworkClient(config={}, signer=signer)
        image_cache = {}

        summary = {
            "rootCompartmentId": root_compartment_id,
            "compartmentsScanned": 0,
            "instancesScanned": 0,
            "updated": 0,
            "unchanged": 0,
            "skippedNoVnic": 0,
            "imageLookups": 0,
            "imageCacheHits": 0,
            "errors": [],
        }

        for compartment_id in _compartment_tree(identity, root_compartment_id):
            summary["compartmentsScanned"] += 1
            LOGGER.info("Scanning compartment %s", compartment_id)
            for instance in _list_instances(compute, compartment_id, summary):
                if getattr(instance, "lifecycle_state", None) in SKIP_INSTANCE_STATES:
                    continue

                summary["instancesScanned"] += 1
                try:
                    instance = compute.get_instance(instance.id).data
                    vnics = _get_vnics(compute, network, compartment_id, instance.id)
                    if not vnics:
                        summary["skippedNoVnic"] += 1
                        continue

                    tags = {}
                    tags.update(_network_tags(vnics))
                    tags.update(_dynamic_compute_tags(instance))

                    changed = _sync_defined_tags(
                        compute,
                        instance.id,
                        tags,
                        refresh_static_tags,
                        image_cache,
                        summary,
                    )
                    summary["updated" if changed else "unchanged"] += 1
                except ServiceError as exc:
                    _record_error(summary, instance.id, exc.message)
                except Exception as exc:
                    _record_error(summary, instance.id, str(exc))

        return _respond(ctx, 200, summary)
    except Exception as exc:
        LOGGER.exception("Unexpected reconciler failure")
        return _respond(ctx, 500, {"status": "error", "message": str(exc)})


def _compartment_tree(identity, root_compartment_id):
    compartment_ids = [root_compartment_id]
    stack = [root_compartment_id]

    while stack:
        parent_id = stack.pop()
        try:
            children = oci.pagination.list_call_get_all_results(
                identity.list_compartments,
                parent_id,
                access_level="ACCESSIBLE",
                lifecycle_state="ACTIVE",
            ).data
        except ServiceError as exc:
            LOGGER.warning("Could not list child compartments under %s: %s", parent_id, exc.message)
            continue

        child_ids = [compartment.id for compartment in children]
        compartment_ids.extend(child_ids)
        stack.extend(child_ids)

    return compartment_ids


def _list_instances(compute, compartment_id, summary):
    try:
        return oci.pagination.list_call_get_all_results(
            compute.list_instances,
            compartment_id=compartment_id,
        ).data
    except ServiceError as exc:
        _record_error(summary, compartment_id, exc.message)
        return []


def _get_vnics(compute, network, compartment_id, instance_id):
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
        vnics.append(network.get_vnic(vnic_id).data)
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


def _dynamic_compute_tags(instance):
    shape = getattr(instance, "shape", None) or UNKNOWN_VALUE
    shape_config = getattr(instance, "shape_config", None)

    return {
        SHAPE_FAMILY_KEY: _shape_family(shape),
        SHAPE_KEY: shape,
        PROCESSOR_KEY: _string_value(getattr(shape_config, "processor_description", None)),
        OCPUS_KEY: _string_value(getattr(shape_config, "ocpus", None)),
        MEMORY_GB_KEY: _string_value(getattr(shape_config, "memory_in_gbs", None)),
    }


def _static_compute_tags(compute, instance, image_cache, summary):
    image = _get_image(compute, instance, image_cache, summary)
    os_name = getattr(image, "operating_system", None) or UNKNOWN_VALUE
    os_version = getattr(image, "operating_system_version", None) or UNKNOWN_VALUE

    return {
        PLATFORM_KEY: _platform(os_name),
        OS_KEY: os_name,
        OS_VERSION_KEY: os_version,
    }


def _get_image(compute, instance, image_cache, summary):
    image_id = _image_id(instance)
    if not image_id:
        return None
    if image_id in image_cache:
        summary["imageCacheHits"] += 1
        return image_cache[image_id]

    try:
        summary["imageLookups"] += 1
        image_cache[image_id] = compute.get_image(image_id).data
        return image_cache[image_id]
    except ServiceError as exc:
        LOGGER.warning("Could not read image %s for instance %s: %s", image_id, instance.id, exc.message)
        image_cache[image_id] = None
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


def _sync_defined_tags(compute, instance_id, desired_tags, refresh_static_tags, image_cache, summary):
    instance_response = compute.get_instance(instance_id)
    defined_tags = dict(instance_response.data.defined_tags or {})
    namespace_tags = dict(defined_tags.get(TAG_NAMESPACE, {}))
    next_namespace_tags = dict(namespace_tags)
    instance = instance_response.data

    if refresh_static_tags or _static_tags_need_refresh(namespace_tags):
        desired_tags.update(_static_compute_tags(compute, instance, image_cache, summary))

    for key in _dynamic_managed_keys():
        if key in desired_tags:
            next_namespace_tags[key] = desired_tags[key]
        else:
            next_namespace_tags.pop(key, None)

    for key in _static_managed_keys():
        if key in desired_tags:
            next_namespace_tags[key] = desired_tags[key]

    if namespace_tags == next_namespace_tags:
        return False

    defined_tags[TAG_NAMESPACE] = next_namespace_tags
    compute.update_instance(
        instance_id,
        oci.core.models.UpdateInstanceDetails(defined_tags=defined_tags),
        if_match=instance_response.headers.get("etag"),
    )
    LOGGER.info("Tagged %s with %s", instance_id, {TAG_NAMESPACE: desired_tags})
    return True


def _dynamic_managed_keys():
    return (
        NETWORK_SCOPE_KEY,
        PRIVATE_IP_KEY,
        PUBLIC_IP_KEY,
        SHAPE_FAMILY_KEY,
        SHAPE_KEY,
        PROCESSOR_KEY,
        OCPUS_KEY,
        MEMORY_GB_KEY,
    )


def _static_managed_keys():
    return (PLATFORM_KEY, OS_KEY, OS_VERSION_KEY)


def _static_tags_need_refresh(namespace_tags):
    return any(namespace_tags.get(key) in (None, "", UNKNOWN_VALUE) for key in _static_managed_keys())


def _config(ctx, key):
    value = (ctx.Config() or {}).get(key)
    return value.strip() if isinstance(value, str) else value


def _bool_config(ctx, key, default):
    value = _config(ctx, key)
    if value in (None, ""):
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "y")


def _string_value(value):
    return UNKNOWN_VALUE if value is None else str(value)


def _record_error(summary, resource_id, message):
    LOGGER.warning("Skipping %s: %s", resource_id, message)
    if len(summary["errors"]) < 25:
        summary["errors"].append({"resourceId": resource_id, "message": message})


def _respond(ctx, status_code, body):
    return response.Response(
        ctx,
        status_code=status_code,
        response_data=json.dumps(body),
        headers={"Content-Type": "application/json"},
    )
