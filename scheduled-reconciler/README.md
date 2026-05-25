# Scheduled Reconciler Function

This OCI Function scans a configured compartment and all active child compartments, then updates compute instance metadata tags under the `Instances` defined tag namespace.

It can be invoked manually or run periodically through OCI Resource Scheduler.

## What It Does

The function:

* Reads `ROOT_COMPARTMENT_ID` from function configuration.
* Lists the root compartment and active child compartments.
* Lists compute instances in each compartment.
* Skips terminating and terminated instances.
* Reads VNIC, IP, image, shape, processor, OCPU, and memory metadata.
* Merges the managed `Instances` tag keys without removing unrelated defined tags.
* Removes stale `PublicIP` when an instance no longer has a public IP.

## Configuration

| Key | Required | Default | Description |
|---|---|---:|---|
| `ROOT_COMPARTMENT_ID` | Yes | none | Root compartment to scan. Child compartments are included. |
| `REFRESH_STATIC_TAGS` | No | `false` | Controls whether platform, OS, and OS version are refreshed every run. |

Example:

```bash
oci fn function update \
  --function-id <reconciler_function_ocid> \
  --config '{"ROOT_COMPARTMENT_ID":"<root_compartment_ocid>","REFRESH_STATIC_TAGS":"false"}'
```

## Static Tag Refresh Behavior

With `REFRESH_STATIC_TAGS=false`, these tags are updated only if they are missing, empty, or set to `Unknown`:

```text
Instances.Platform
Instances.OS
Instances.OSVersion
```

With `REFRESH_STATIC_TAGS=true`, those tags are refreshed on every run.

The reconciler always evaluates:

```text
Instances.NetworkScope
Instances.PrivateIP
Instances.PublicIP
Instances.ShapeFamily
Instances.Shape
Instances.Processor
Instances.OCPUs
Instances.MemoryGB
```

## Deploy

From this folder:

```bash
fn -v deploy --app <function_application_name>
```

The function name in `func.yaml` is:

```text
scheduled-reconciler
```

## Manual Invoke

The reconciler does not require an input payload.

```bash
fn invoke <function_application_name> scheduled-reconciler
```

Example response:

```json
{
  "rootCompartmentId": "ocid1.compartment.oc1..example",
  "compartmentsScanned": 3,
  "instancesScanned": 12,
  "updated": 4,
  "unchanged": 8,
  "skippedNoVnic": 0,
  "imageLookups": 2,
  "imageCacheHits": 6,
  "errors": []
}
```

## Scheduler Guidance

Run the reconciler based on how often the environment changes.

| Environment | Suggested Frequency |
|---|---|
| Frequent Terraform or automation changes | Every few hours |
| Moderate change rate | Every 6 to 12 hours |
| Stable environment | Daily |
| Mostly static environment | Weekly |

If compute instances are auto-started on a schedule, run the reconciler shortly after the start window. For example, if instances start at 10:00, schedule the reconciler around 10:30 or 11:00 so OCI has time to make VNIC and IP metadata consistently available through the APIs.
