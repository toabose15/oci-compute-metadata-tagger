# OCI Compute Metadata Tagger

This repository contains two OCI Functions that tag OCI Compute instances with metadata that is useful for OCI Cost Analysis and cost reports.

The functions populate a defined tag namespace named `Instances` with values such as network scope, private IP, public IP, platform, OS, shape, processor, OCPUs, and memory.

## Functions

| Folder | Function | Purpose |
|---|---|---|
| `event-reactor/` | React function | Invoked by an OCI Events rule when a compute instance create operation completes. |
| `scheduled-reconciler/` | Reconcile function | Runs manually or on a schedule to scan a compartment and child compartments, then backfill or correct tags. |

## Repository Layout

```text
oci-compute-metadata-tagger/
├── event-reactor/
│   ├── func.py
│   ├── func.yaml
│   ├── requirements.txt
│   └── README.md
├── scheduled-reconciler/
│   ├── func.py
│   ├── func.yaml
│   ├── requirements.txt
│   └── README.md
└── README.md
```

## Tag Namespace

Create a defined tag namespace named `Instances` before deploying the functions. All tag keys are strings.

| Tag Key | Example |
|---|---|
| `NetworkScope` | `Public` or `Private` |
| `PrivateIP` | `10.0.1.149` |
| `PublicIP` | `129.x.x.x` |
| `Platform` | `Linux` or `Windows` |
| `OS` | `Oracle Linux` |
| `OSVersion` | `9` |
| `ShapeFamily` | `A1` |
| `Shape` | `VM.Standard.A1.Flex` |
| `Processor` | `3.0 GHz Ampere Altra` |
| `OCPUs` | `1.0` |
| `MemoryGB` | `8.0` |

## IAM Requirements

The functions use OCI resource principals. Create a dynamic group for the functions and allow it to read compute/network metadata and update instance tags.

Example dynamic group:

```text
ALL {resource.type = 'fnfunc', resource.compartment.id = '<function_compartment_ocid>'}
```

Example policies:

```text
Allow dynamic-group <dynamic_group_name> to inspect compartments in tenancy
Allow dynamic-group <dynamic_group_name> to inspect instance-images in tenancy
Allow dynamic-group <dynamic_group_name> to use instances in compartment <target_compartment_name>
Allow dynamic-group <dynamic_group_name> to inspect vnic-attachments in compartment <target_compartment_name>
Allow dynamic-group <dynamic_group_name> to inspect vnics in compartment <target_compartment_name>
Allow dynamic-group <dynamic_group_name> to use tag-namespaces in tenancy
```

Scope the policies to the parent compartment or tenancy if the scheduled reconciler must scan multiple child compartments.

## Deploy

Set up Cloud Shell or a local Fn CLI environment, then deploy each function from its folder.

```bash
cd event-reactor
fn -v deploy --app <function_application_name>

cd ../scheduled-reconciler
fn -v deploy --app <function_application_name>
```

See the individual function READMEs for configuration and validation steps.

## Operating Model

Use both functions together:

* The event reactor tags newly created instances close to creation time.
* The scheduled reconciler is the consistency layer that handles missed events, delayed metadata visibility, public IP changes, and backfill for existing instances.

For environments where instances are auto-started on a schedule, run the reconciler after the start window. For example, if instances start at 10:00, schedule the reconciler around 10:30 or 11:00 so VNIC and IP metadata has time to settle.
