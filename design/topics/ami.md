# AMI

`bake` creates AMIs from the service's instance. The launch template's `ImageId` comes
from `ImageId` in the config file. Naming and tagging rules are in
[naming.md](../naming.md).

## Creating

Step 6 of [`bake`](../commands/ami.md#bake): `create-image` from the stopped instance.

- Name: `temt-<svc>-<YYMMDDHHMM>`. The time is the client's local time when
  `create-image` is called.
- `TagSpecifications` puts `Name=<AMI name>` and `temt:service=<svc>` on the AMI and on
  its snapshots.

`bake` bakes the instance as it is, without touching its contents or cleaning anything
up first. For example, lines in `~/.ssh/authorized_keys` (a key pair the source AMI
was launched with, or keys the user added) remain in instances launched from the baked
AMI, and those keys can log in alongside the one `proxy` sends. This is the intended behavior and is
not to be changed.

AMI names must be unique within the account and region. Baking the same service twice
within the same minute makes the second `create-image` fail on the duplicate name; temt
then exits telling the user to wait a minute and run it again.

`bake` does not delete old AMIs; [`ami prune`](../commands/ami.md#ami-prune) and
[`ami delete`](../commands/ami.md#ami-delete) do. `ami prune` deregisters the AMIs `bake`
made (`available` or `failed`) that no version of any launch template points at, and
deletes their snapshots. `ami delete`
deletes the one AMI given by ID or name.

`bake` points the template at the AMI only after it is `available`; `ami prune` run on
the same machine does not delete an AMI in that window ([`ami
prune`](../commands/ami.md#ami-prune)).

## Selecting

When `ImageId` is missing or empty, step 2 of [`apply`](../commands/service.md#apply)
selects one and writes it to the config file:

- Among the account's own AMIs with the tag `temt:service=<svc>` and state `available`,
  take the one with the newest `CreationDate` and write its name.
- If there is none, get the Ubuntu 24.04 AMI ID from Canonical's SSM public parameter
  `/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id`
  and write the ID. A public AMI is not in the user's account, so it cannot be referred
  to by name.

When `ImageId` is written, its value is used as is and not reselected. To reselect the
latest AMI, delete the line or make it empty and run `apply`; the reselection picks an
AMI baked from that service (or Ubuntu 24.04 if none). To use an AMI baked from another
service, write its name or ID. Copying a config file to make another service also
copies `ImageId`, so the new service launches from the same AMI; deleting the line in
the copy makes it reselect from AMIs baked from the copy's own service.

AMIs baked from a service are identified by the `temt:service` tag, not by name prefix,
because searching `temt-dev-*` for service `dev` would also match AMIs of service
`dev-main`.

## Referring by name

`ImageId` accepts an AMI ID or an AMI name. A value of `ami-` followed by 8 or 17
lowercase hex digits is an ID; anything else is a name.

- Names are looked up only among the account's own AMIs (`describe-images` with
  `Owners=self` and a `name` filter), because names are unique within an account and
  region but can collide across owners.
- Filter values expand `*` and `?`, so a value containing them is an error.
- A deregistered AMI stays visible in state `deregistered` for a while after
  deregistration. It is treated as nonexistent.
- Usable states are `available` and `pending`. A hand-written `ImageId` pointing at an
  AMI still being created is accepted, because a launch template does not use the AMI
  until launch.
- The launch template's `ImageId` holds only IDs, so step 4 of `apply` resolves a name
  to its ID.
- Step 4 of `apply` looks up the AMI even when `ImageId` is an ID, and checks that it is
  in a usable state. Whether by name or ID, if the AMI is missing or unusable, `apply`
  stops and tells the user to delete the line and reselect. This happens when it points
  at a deregistered AMI or an AMI of another account.
- Step 3 of `bake` also looks up the AMI, including when it is written as an ID, and
  compares its ID with the template. If there is no usable AMI, `bake` reports the key
  as differing and prompts for `apply`.
