# Launch template

Reconciled by step 5 of [`apply`](../commands/service.md#apply), its `ImageId` replaced
by [`bake`](../commands/ami.md#bake) (and by `set-port`, `deploy --bake`, and
`app remove --bake`, which bake), deleted by step 4 of
[`destroy`](../commands/service.md#destroy). Name: `<name>-template`. `start` launches
from its default version. There is one per region in which the service is applied.

## The config file is the whole content

The template's content (`LaunchTemplateData`) is exactly the config file's
`[launch-template]` (over the defaults file), the values filled when not written
([Filled when missing](#filled-when-missing)), and what temt decides ([What temt
adds](#what-temt-adds)); nothing else (the requester). An item missing from the config
file disappears from the template at the next `apply`, and an edit made in the console
is put back, as for inline policies and ingress rules.

The exception is `ImageId`. When it is not written, `apply` keeps the template's value
and does not count it as a difference, since `bake` changes it in the template only
([ami.md](ami.md#selecting)). An `ImageId` changed by hand in the template therefore
stays.

- Keys are the names and nesting of the `get-launch-template-data` output: the JSON
  names of the SDK and the CLI (`BlockDeviceMappings`), not the Query names of the API
  reference (`BlockDeviceMapping.N`). So `service import` copies that output as it is
  ([commands/service.md](../commands/service.md#service-import)).
- What can be written is what the launch template data of the API accepts in the
  botocore temt runs with. temt keeps no list of items, so items AWS adds become
  writable with a newer botocore. An unknown name or a value of the wrong type is an
  error.
- Types: a structure is a TOML table (dotted keys and inline tables are fine), a list of
  structures an array of tables, a list of strings an array of strings, string,
  integer, boolean, and double the same TOML types, and a timestamp a TOML date-time.
  Values go to the API as they are (`UserData` stays base64).
- The four items that exist only when read (`MetadataOptions.State`,
  `NetworkInterfaces[].Ipv6Addresses[].IsPrimaryIpv6`, `Operator.HiddenByDefault`,
  `Operator.Managed`) cannot be written (error).
- Combinations of items (`SecurityGroupIds` with `NetworkInterfaces` `Groups`,
  `Placement.AvailabilityZone` with `AvailabilityZoneId`, ...) are not checked; AWS
  refuses them. Some are refused only at launch (`start`): a written
  `CreditSpecification` with a non-T `InstanceType`, and a written `Ipv6AddressCount`
  of 1 or more on a subnet without IPv6. Both were checked on real AWS: creating the
  template accepts them without a warning, and the launch is refused without creating
  an instance. temt leaves them as they are (does not drop, warn, or stop); AWS's error
  tells which item (by its key for `CreditSpecification`; for IPv6 it names no key but
  tells it is about IPv6), and the user fixes or deletes the line, runs `diff`, and
  `apply`.
- To remove an item from the template, delete its line and `apply`. An item temt fills
  ([Filled when missing](#filled-when-missing)) comes back when deleted, so write the
  value instead (`CpuCredits = "standard"`, `Ipv6AddressCount = 0`); there is no way to
  make such an item absent. A blank value is not a way to remove: booleans and integers
  have none, and an empty tag value is a valid value.

Rejected: keeping a template value for any item not in the config file (the requester's
first idea). Deleting a line would take the item out of `diff` while the value stayed
in the template unseen, and removing it would need another mechanism. `ImageId` alone
is kept, because `bake` changes it without touching the config file. Also rejected:
`--exact` for `apply` and `diff`, which the rule above makes unnecessary.

## Extensions to AWS's form

Each adds to AWS's form only; written as AWS's form, it behaves as AWS does.

- `ImageId` may be an AMI name ([ami.md](ami.md#referring-by-name)); `apply` puts in
  its ID. So may `--image-id` of `apply` and `diff`.
- `Groups` of a `NetworkInterfaces` element may hold security group names as well as
  IDs. AWS does not let a group name start with `sg-`, so a value starting with `sg-`
  is an ID and anything else a name. A name is looked up in the VPC of the `SubnetId`
  (written or filled) of the element with `DeviceIndex = 0` (a name is unique within a
  VPC, and the interfaces of an instance are in one VPC). A name not in that VPC is an
  error, except temt's security group (`<name>`, or `${temt:name}`), which `apply`
  creates when missing ([security-group.md](security-group.md#when-it-is-created)).
  `diff` compares after looking names up.
- `DeviceName` of one `BlockDeviceMappings` element may be omitted. temt then puts in
  the `RootDeviceName` of the template's AMI, decided at every `apply`, so the element
  keeps pointing at the root volume when the AMI changes to one of another OS. Omitting
  it in two or more elements is an error, and so is a `DeviceName` that collides after
  filling. AWS requires `DeviceName`; a name other than the AMI's root device does not
  enlarge the root volume but adds an empty disk without an error, and the name differs
  by AMI (`/dev/sda1` on Ubuntu, `/dev/xvda` on Amazon Linux). The root volume element
  is the one whose `DeviceName`, after filling, equals the AMI's `RootDeviceName`.
- The variables `${temt:name}` and `${temt:service}` may be written in values, as in
  policies ([docs/conf.md](../../docs/conf.md#role)). The scaffold and `role import`
  write `IamInstanceProfile.Name = "${temt:name}"` with them, so a config file copied to
  another service points at that service's instance profile, not the original's.

## What temt adds

Decided at every `apply` and not written to the config file, so a config file copied to
another service does not carry the original service's names.

- The tag `temt:service=<svc>` on `instance`, `volume`, and `network-interface`,
  whether or not it is written.
- When not written:
  - The `Name` tag, `<name>`, for each of `instance`, `volume`, and
    `network-interface`. Missing per `ResourceType`: added to that element if it lacks
    `Name`, and the element is added if missing.
  - `Groups` of the `NetworkInterfaces` element with `DeviceIndex = 0`: the ID of temt's
    security group `<name>` in the VPC of that element's `SubnetId`. A `SubnetId` in
    another VPC moves it to that VPC's security group.

## Filled when missing

When not written, `apply` and `diff` decide the value at every run, in the region of
that run, and put it in the template. Nothing is written to the config file, so the file
holds only what the user changed from the defaults, as in CDK, and the same file can be
applied in another region to make a clone ([config.md](../config.md#clones)).

| Item | Value |
| --- | --- |
| `InstanceType` | `t3.micro` |
| `NetworkInterfaces` (`DeviceIndex = 0`) `SubnetId` | A subnet of the default VPC of the region, chosen as in [docs/conf.md](../../docs/conf.md#launch-template) |
| `NetworkInterfaces` (`DeviceIndex = 0`) `AssociatePublicIpAddress` | `true` |
| `NetworkInterfaces` (`DeviceIndex = 0`) `Ipv6AddressCount` | `1`, only when the subnet has an IPv6 CIDR associated |
| `MetadataOptions.HttpTokens` | `required` |
| `MetadataOptions.InstanceMetadataTags` | `enabled` |
| `InstanceInitiatedShutdownBehavior` | `terminate` |
| `CreditSpecification.CpuCredits` | `unlimited`, only when `InstanceType` is a T type (one starting with `t` and a digit 2 to 9, such as `t3.micro`; not `t1`, which has no CPU credits) |

- The fixed values are temt's. When a version of temt changes one, the next `apply`
  shows it as a difference.
- `SubnetId` and `Ipv6AddressCount` are looked up in AWS at every run (the requester).
  Rejected: keeping the looked-up values in the template and reusing them; `ImageId`
  alone is kept, because `bake` changes it ([ami.md](ami.md#selecting)).
- `CreditSpecification` and `Ipv6AddressCount` follow the `InstanceType` and subnet of
  the run, since they are decided again each time.
- If there is no `NetworkInterfaces` element with `DeviceIndex = 0`, it is added with
  the values. So the top-level `SecurityGroupIds` and `SecurityGroups`, which AWS does not
  take together with `NetworkInterfaces`, end in AWS's error; security groups go in
  `Groups`. If that element has `NetworkInterfaceId` (attaching an existing ENI),
  `SubnetId`, `Groups`, `AssociatePublicIpAddress`, and `Ipv6AddressCount` are not
  added. If the top level has `SecurityGroupIds` or `SecurityGroups`, `Groups` is not
  added. AWS refuses these combinations.
- `ImageId` is not in the table: when not written, the template's value is kept, and a
  new template gets the AMI chosen as in [ami.md](ami.md#selecting).
- `IamInstanceProfile` is not filled: it is an item like any other, and when not
  written the instance has no instance profile. The scaffold that `apply` writes for a
  new service has it ([commands/service.md](../commands/service.md#apply)); `apply`
  never adds it to an existing file, but `apply` and `diff` warn when it is missing,
  unless `WaitForSsm = false`. Rejected: filling it like the items above, with
  `IamInstanceProfile = false` for none; it mixed two roles in one item (a template
  item and a switch for creating IAM resources) and needed a special value and an
  exception in merging.
- `KeyName` is not filled. temt logs in without a key pair
  ([login-key.md](login-key.md)); a `KeyName` written (`service import` copies the
  source's) goes into the template as it is.

## Comparing

`apply`, `diff`, and the check before baking ([`bake`](../commands/ami.md#bake) step 3)
compare the whole content of the default version with the config file's, after filling
and looking up names. An unwritten `ImageId` is not compared. Forms that AWS changes on
reading are aligned first; otherwise every `apply` would make a new version.

- A blank value (empty array, empty table, empty string) counts as not written, on
  both sides. AWS drops empty arrays on reading (checked on real AWS). `NoDevice` is
  the exception: its only value is the empty string, which leaves out a device of the
  AMI, so it is kept.
- A double written as an integer is read as a decimal, as AWS returns it.
- `IamInstanceProfile` given by `Name` and by `Arn` count as the same instance profile;
  AWS may return the ARN.
- The four read-only items are ignored.
- Arrays are not compared by order, since AWS does not keep it. Elements are matched
  by a key: `NetworkInterfaces` by `DeviceIndex`, `BlockDeviceMappings` by `DeviceName`
  (after filling), `TagSpecifications` by `ResourceType`, tags by `Key`. A key repeated
  in the config file (two `DeviceIndex = 0`) is an error, since elements would be
  mistaken for each other. An element without its key is left to AWS's error. Other
  arrays compare whole elements; a difference shows as an element removed and an
  element added.
- Not checked on real AWS (the requester: the earlier checks on real AWS are enough):
  whether AWS keeps `Ipv6AddressCount = 0`, and whether it fills in other items. Either
  would show as a difference at every `apply`.

## Versions

- If the template does not exist, create it with the content as its default version.
- If it exists, compare as above; if they differ, create a new version and make it the
  default. If they are the same, no new version is created.
- The template's own tag (`temt:service`) is reconciled separately from versions. If it
  is missing or has a different value, write it, and in that case report the template
  as updated even when the contents are the same.
- AWS does not fully check the contents when creating a template or version and
  returns problems as warnings. `apply` and `bake` show each as a warning. When EC2
  does not see the instance profile yet, `apply` adds that the instance profile exists
  and IAM has not propagated it to EC2 yet, but only when `[role]` exists and
  `IamInstanceProfile` points at `<name>`: then it has just reconciled that profile
  ([commands/instance.md](../commands/instance.md#right-after-apply)). `bake` does not
  check the instance profile, which may have been deleted by hand, so it adds nothing.
- `bake` does not compare: it creates a new version from the default version's content
  with only `ImageId` replaced, and makes it the default. It changes the template only
  after the new AMI is `available`.

After making a new version the default, all other versions are deleted so the template
keeps one version. If deletion fails, warn and continue. Versions other than the
default remain when temt is interrupted right after switching the default or when
deletion failed. So `apply`, even when it creates no new version, deletes the
non-default versions, including ones not created by temt.

There is no way to roll back to a previous version. To roll back, revert the config file
and run `apply`; for the AMI, run `apply --image-id` with the previous AMI. Existing
instances (running or stopped) are not affected; the new default version is used when
`start` next launches a new instance.
