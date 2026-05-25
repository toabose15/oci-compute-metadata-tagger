# Event Reactor Function

This OCI Function tags a compute instance when an OCI Events rule sends a compute instance create completion event.

It processes this event type:

```text
com.oraclecloud.computeapi.launchinstance.end
```

It does not process instance start or stop events. The scheduled reconciler handles ongoing correction and drift.

## What It Does

The function reads the instance OCID and compartment OCID from the event payload, queries OCI Compute and VCN APIs, builds the desired `Instances` tag values, and updates the instance defined tags.

It tags:

```text
Instances.NetworkScope
Instances.PrivateIP
Instances.PublicIP
Instances.Platform
Instances.OS
Instances.OSVersion
Instances.ShapeFamily
Instances.Shape
Instances.Processor
Instances.OCPUs
Instances.MemoryGB
```

If any VNIC has a public IP, `NetworkScope` is set to `Public`. Otherwise, it is set to `Private`.

## Configuration

| Key | Default | Description |
|---|---:|---|
| `MAX_LOOKUP_SECONDS` | `30` | Maximum total time to wait for VNIC and IP metadata after the event arrives. |
| `LOOKUP_SLEEP_SECONDS` | `5` | Wait time between lookup attempts. |

Example:

```bash
oci fn function update \
  --function-id <event_function_ocid> \
  --config '{"MAX_LOOKUP_SECONDS":"30","LOOKUP_SLEEP_SECONDS":"5"}'
```

## Deploy

From this folder:

```bash
fn -v deploy --app <function_application_name>
```

The function name in `func.yaml` is:

```text
event-reactor
```

## Event Rule

Create an OCI Events rule with a condition for:

```text
com.oraclecloud.computeapi.launchinstance.end
```

Set the action type to **Functions**, select the function application, and select this function.

## Manual Test Payload

Use an existing non-terminated instance OCID and compartment OCID.

```json
{
  "eventType": "com.oraclecloud.computeapi.launchinstance.end",
  "cloudEventsVersion": "0.1",
  "eventTypeVersion": "2.0",
  "source": "ComputeApi",
  "eventTime": "2026-05-23T00:00:00Z",
  "contentType": "application/json",
  "data": {
    "compartmentId": "ocid1.compartment.oc1..REPLACE_WITH_COMPARTMENT_OCID",
    "resourceName": "manual-test-instance",
    "resourceId": "ocid1.instance.oc1.iad.REPLACE_WITH_INSTANCE_OCID",
    "availabilityDomain": "example:US-ASHBURN-AD-1",
    "freeformTags": {},
    "definedTags": {},
    "additionalDetails": {
      "shape": "VM.Standard.E5.Flex",
      "type": "ManualTest"
    }
  },
  "eventID": "manual-test-launchinstance-end",
  "extensions": {
    "compartmentId": "ocid1.compartment.oc1..REPLACE_WITH_COMPARTMENT_OCID"
  }
}
```

Invoke:

```bash
fn invoke <function_application_name> event-reactor < manual-launch-event.json
```

## Notes

If many instances are created at the same time, consider provisioned concurrency for this function. Without provisioned concurrency, keep `MAX_LOOKUP_SECONDS` modest and rely on the scheduled reconciler for consistency.
