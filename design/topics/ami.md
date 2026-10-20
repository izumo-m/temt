# AMI

`bake` creates AMIs from the service's instance, and `service import` from an existing
stopped instance. Which AMI the launch template uses is in [Selecting](#selecting).
Naming and tagging rules are in [naming.md](../naming.md).

## Creating

[`bake`](../commands/ami.md#bake) and
[`service import`](../commands/service.md#service-import) create the AMI from the
stopped instance.

- Name: `temt-<svc>-<YYMMDDHHMM>`. The time is the client's local time when the AMI is
  created. Rejected: UTC (the requester: people reading the name would misread it).
  The time in the name is never used for ordering ([Selecting](#selecting)), so the
  time zone does not matter.
- The AMI and its snapshots get the tags `Name=<AMI name>` and `temt:service=<svc>`.
- Only the root volume goes in. Every other EBS volume is left out, since AWS by
  default snapshots every attached EBS volume into the AMI. Instance store volumes are
  not EBS and are not left out.

temt manages the root volume only. Other EBS volumes (external EBS) are attached and
mounted from inside the instance (an app, a systemd mount, a `mount.<type>` helper,
...); temt neither attaches nor detaches them. With an instance role temt manages, the
permissions this needs (`ec2:AttachVolume`, ...) go in `[[role.<policy name>]]`.
Writing an external EBS volume in `/etc/fstab` without `nofail` stops the boot, a
well-known matter temt does not handle (the requester). An external EBS volume attaches
only to an instance in its AZ, and only to one instance unless it is Multi-Attach (io1
/ io2), which cannot be given at launch or used for boot. Rejected: putting other EBS
volumes in the AMI (each launch would copy them from snapshots, apart from the shared
volume, and the copy would take the device name an app attaches its volume at);
attaching them by `start` (a `[[volume]]` table).

For the same reason, [Terminating](instance.md#terminating) deletes only the root
volume with the instance, and `service import` makes the AMI's root volume deleted on
termination.

`bake` bakes the instance as it is, without touching its contents or cleaning anything
up first. For example, lines in `~/.ssh/authorized_keys` (a key pair the source AMI
was launched with, or keys the user added) remain in instances launched from the baked
AMI, and those keys can log in alongside the one `proxy` sends. This is the intended
behavior and is not to be changed.

AMI names must be unique within the account and region. Baking the same service twice
within the same minute makes the second creation fail on the duplicate name; temt
shows AWS's error and adds no hint on what to do next.

`bake` does not delete old AMIs; [`ami prune`](../commands/ami.md#ami-prune) and
[`ami delete`](../commands/ami.md#ami-delete) do. `ami prune` deregisters the AMIs
`bake` and `service import` made (`available` or `failed`) that no version of any launch
template points at, and deletes their snapshots. `ami delete` deletes the one AMI given
by ID or name.

`bake` points the template at the AMI only after it is `available`; `ami prune` does not
delete an AMI in that window ([`ami prune`](../commands/ami.md#ami-prune)).

A volume created from a snapshot is slow on its first read of each block, loaded lazily
from S3. temt does nothing about it (the requester); where it matters (batch jobs, ...),
the service handles it on its own, for example with another instance family.

## Selecting

`ImageId` is normally not written in the config file. The AMI then lives in the launch
template of each region:

- `apply` keeps the template's `ImageId` and does not show it as a difference.
- `bake` (and `set-port`, `deploy --bake`, `app remove --bake`) replaces the `ImageId`
  of that region's template only and does not touch the config file.
- `apply --image-id <AMI ID or name>` switches the template to that AMI, also when the
  template exists; this is how to go back to an earlier AMI. `diff --image-id` shows
  what that `apply` would change.
- If the template's AMI no longer exists or is not `available` (deleted with `ami delete
  --force` or by hand, ...),
  `apply` stops and tells the user to choose one with `--image-id`. It does not choose
  again silently.

When `apply` creates the template (the first `apply` in a region, or the first after
`destroy`), it chooses the AMI in this order:

1. `--image-id`.
2. The newest of the service's own AMIs: AMIs in the user's account whose name is
   exactly `temt-<svc>-` followed by 10 digits, in state `available` or `pending`
   (`failed` and the like are left out), newest by creation time. If the newest is
   `pending` (a copy in progress, ...), `apply` stops; the user runs it again when the
   AMI is `available` (the requester).
3. The Ubuntu 24.04 AMI, from Canonical's SSM public parameter
   `/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id`.

The line of `ImageId` in `apply` and `diff` shows the chosen AMI's name, and when
Ubuntu was chosen, that there is no AMI of the service in the region. A user who forgot
to copy the AMI to a new region sees it before confirming. Rejected: stopping when the
service has no AMI of its own, since the very first `apply` would then need
`--image-id`.

Candidates are found by name, not by the `temt:service` tag, because a copied AMI may
lack the tags. The exact form (not a prefix) keeps AMIs of service `dev-main` out of
service `dev`. They are ordered by creation time, not by the time in the name: a copied
AMI's creation time is the time of the copy, and the names use local time. Only when an
older AMI of another region is copied after a newer one was made here is the older
content chosen, and the name shown before confirming makes that visible.

`destroy` deletes the template, so an AMI chosen with `--image-id` is forgotten and the
next `apply` chooses again as above. `destroy` reports the AMI it would choose.

When `ImageId` is written (in the config file or the defaults file), the service is
pinned to that AMI: `apply` uses it as is, `--image-id` is a usage error, and the
commands that bake (`bake`, `set-port`, `deploy --bake`, `app remove --bake`) stop with
a usage error before doing anything, telling the user to delete the `ImageId` line to
bake. `service import` writes the name of the AMI it made, so an imported service is
pinned until the user deletes the line. `ImageId` in the defaults file pins every
service.

A config file copied to make another service usually has no `ImageId`, so the new
service starts from its own newest AMI (Ubuntu 24.04 if none). To start it from the
original service's AMI, run its first `apply` with `--image-id`.

Another region gets the service's AMI only when the user copies it there under the same
name (`copy-image` with the name, and with the tags so that `ami list` and `ami prune`
see it); temt has no command that copies AMIs. The clone then finds it by name. A
service whose `ImageId` is written as an ID cannot be cloned as it is, since AMI IDs
differ by region; AWS's error tells this.

## Referring by name

`ImageId` and `--image-id` accept an AMI ID or an AMI name. A value of `ami-` followed
by 8 or 17 lowercase hex digits is an ID; anything else is a name.

- Names are looked up only among the account's own AMIs, because names are unique
  within an account and region but can collide across owners. A public AMI (Ubuntu)
  can therefore be given only by ID.
- A name containing `*` or `?` is an error, since the lookup would expand them.
- A deregistered AMI stays visible in state `deregistered` for a while after
  deregistration. It is treated as nonexistent.
- Usable states are `available` and `pending`. A given AMI still being created is
  accepted, because a launch template does not use the AMI until launch.
- The launch template holds only IDs, so step 4 of `apply` puts in the ID of a name.
- Step 4 of `apply` also checks an AMI given by ID. Whether by name or ID, if the AMI is
  missing or unusable, `apply` stops naming it. This happens when it points at a
  deregistered AMI or an AMI of another account or region.
