# Launch template

Reconciled by step 5 of [`apply`](../commands/service.md#apply), its `ImageId` replaced
by step 8 of [`bake`](../commands/ami.md#bake), deleted by step 4 of
[`destroy`](../commands/service.md#destroy). Name: `<name>-template`. `start` launches
from its default version.

## The config file is the whole content

The template's content (`LaunchTemplateData`) is exactly the config file's
`[launch-template]` (over the defaults file) plus what temt decides
([What temt adds](#what-temt-adds)); nothing else (the requester). An item missing from
the config file disappears from the template at the next `apply`, and an edit made in
the console is put back, as for inline policies and ingress rules.

- Keys are the names and nesting of the `get-launch-template-data` output: the JSON
  names of the SDK and the CLI (`BlockDeviceMappings`), not the Query names of the API
  reference (`BlockDeviceMapping.N`). So `ami import` copies that output as it is
  ([commands/ami.md](../commands/ami.md#ami-import)).
- What can be written is decided by the shape `RequestLaunchTemplateData` of the
  botocore temt runs with; temt's code holds no table of items, so items AWS adds come
  with a newer botocore. An unknown name or a value of the wrong type is an error.
- Types: a structure is a TOML table (dotted keys and inline tables are fine), a list of
  structures an array of tables, a list of strings an array of strings, string,
  integer, boolean, and double the same TOML types, and a timestamp a TOML date-time.
  Values go to the API as they are (`UserData` stays base64).
- The four items that exist only when read (`MetadataOptions.State`,
  `NetworkInterfaces[].Ipv6Addresses[].IsPrimaryIpv6`, `Operator.HiddenByDefault`,
  `Operator.Managed`) cannot be written (error).
- Combinations of items (`SecurityGroupIds` with `NetworkInterfaces` `Groups`,
  `Placement.AvailabilityZone` with `AvailabilityZoneId`, ...) are not checked; AWS
  refuses them. Some are refused only at launch (`start`): a `CreditSpecification` left
  after changing `InstanceType` to a non-T type
  (`InstanceCreditSpecification.NotSupported`), and `Ipv6AddressCount` of 1 or more on
  a subnet without IPv6 (`InvalidParameterValue`, `Subnet does not contain any IPv6 CIDR
  block ranges`). Both were checked on real AWS: `create-launch-template` accepts them
  without a warning, and `run-instances` refuses without creating an instance. temt
  leaves them as they are (does not drop, warn, or stop); the error tells which item
  (by its key for `CreditSpecification`; for IPv6 the text names no key but tells it
  is about IPv6), and the user fixes or deletes the line, runs `diff`, and `apply`.
- To remove an item from the template, delete its line and `apply`. An item temt fills
  ([Filled when missing](#filled-when-missing)) comes back when deleted, so write the
  value instead (`CpuCredits = "standard"`, `Ipv6AddressCount = 0`); there is no way to
  make such an item absent. A blank value is not a way to remove: booleans and integers
  have none, and an empty tag value is a valid value.

Rejected: keeping a template value for an item not in the config file (the requester's
first idea). Deleting a line would take the item out of `diff` while the value stayed
in the template unseen, and removing it would need another mechanism. Also rejected:
`--exact` for `apply` and `diff`, which the rule above makes unnecessary.

## Extensions to AWS's form

Each adds to AWS's form only; written as AWS's form, it behaves as AWS does.

- `ImageId` may be an AMI name ([ami.md](ami.md#referring-by-name)); `apply` resolves
  the ID.
- `Groups` of a `NetworkInterfaces` element may hold security group names as well as
  IDs. AWS does not let a group name start with `sg-`, so a value starting with `sg-`
  is an ID and anything else a name. `apply` resolves names in the VPC of the
  element's `SubnetId` (a name is unique within a VPC). A name not in that VPC is an
  error, except temt's security group (`<name>`, or `${temt:name}`), which `apply`
  creates when missing ([security-group.md](security-group.md#when-it-is-created)).
  `diff` compares after resolving.
- `DeviceName` of one `BlockDeviceMappings` element may be omitted. temt then puts in
  the `RootDeviceName` of the `ImageId` AMI, decided at every `apply` and not written
  to the config file, so the element keeps pointing at the root volume when `ImageId`
  changes to an AMI of another OS. Omitting it in two or more elements is an error, and
  so is a `DeviceName` that collides after filling. AWS requires `DeviceName`; a name
  other than the AMI's root device does not enlarge the root volume but adds an empty
  disk without an error, and the name differs by AMI (`/dev/sda1` on Ubuntu,
  `/dev/xvda` on Amazon Linux). The root volume element is the one whose `DeviceName`,
  after filling, equals the AMI's `RootDeviceName`.
- The variables `${temt:name}` and `${temt:service}` may be written in values, as in
  policies ([docs/conf.md](../../docs/conf.md#role)). `apply` writes
  `IamInstanceProfile = { Name = "${temt:name}" }` with them, so a copied config file
  does not point at the original service's instance profile.

## What temt adds

Not written to the config file; decided at every `apply`, so a config file copied to
another service does not carry the original service's names.

- The tag `temt:service=<svc>` on `instance`, `volume`, and `network-interface`,
  whether or not it is written.
- When not written:
  - The `Name` tag, `<name>`, for each of `instance`, `volume`, and
    `network-interface`. Missing per `ResourceType`: added to that element if it lacks
    `Name`, and the element is added if missing.
  - `Groups` of the `NetworkInterfaces` element with `DeviceIndex = 0`: the ID of temt's
    security group `<name>` in the VPC of that element's `SubnetId`. Changing `SubnetId`
    to a subnet of another VPC moves it to that VPC's security group.

## Filled when missing

When not written, `apply` decides the value, writes it into the config file, and puts
it in the template. The same happens at the first `apply`
and when the line is deleted later (deleting `CreditSpecification` on a T type writes
`unlimited` back).

| Item | Value |
| --- | --- |
| `ImageId` | The newest AMI baked from the service, else Ubuntu 24.04 ([ami.md](ami.md#selecting)) |
| `InstanceType` | `t3.micro` |
| `NetworkInterfaces` (`DeviceIndex = 0`) `SubnetId` | The default VPC's subnet, chosen as in [docs/conf.md](../../docs/conf.md#launch-template) |
| `NetworkInterfaces` (`DeviceIndex = 0`) `AssociatePublicIpAddress` | `true` |
| `NetworkInterfaces` (`DeviceIndex = 0`) `Ipv6AddressCount` | `1`, only when the subnet has an IPv6 CIDR associated (an `associated` entry in `Ipv6CidrBlockAssociationSet`) |
| `MetadataOptions.HttpTokens` | `required` |
| `MetadataOptions.InstanceMetadataTags` | `enabled` |
| `InstanceInitiatedShutdownBehavior` | `terminate` |
| `CreditSpecification.CpuCredits` | `unlimited`, only when `InstanceType` is a T type (one starting with `t` and a digit 2 to 9, such as `t3.micro`; not `t1`, which has no CPU credits) |

- `CreditSpecification` and `Ipv6AddressCount` are decided from the `InstanceType` and
  subnet of that time, and stay when those change later. The launch errors this can cause are in [The config file is
  the whole content](#the-config-file-is-the-whole-content).
- If there is no `NetworkInterfaces` element with `DeviceIndex = 0`, it is added with
  the values. If that element has `NetworkInterfaceId` (attaching an existing ENI),
  `SubnetId`, `Groups`, `AssociatePublicIpAddress`, and `Ipv6AddressCount` are not
  added. If the top level has `SecurityGroupIds` or `SecurityGroups`, `Groups` is not
  added. AWS refuses these combinations.
- If the place to write is inside an inline table, `apply` stops without writing and
  tells the user to write it, as for `[[role.policy.ec2]]`. A table written with dotted
  keys is written into.
- `IamInstanceProfile` is not filled: it is an item like any other, and when not
  written the instance has no instance profile. The `apply` that creates the service
  writes `IamInstanceProfile = { Name = "${temt:name}" }`
  ([resources.md](../resources.md#instance-profile-and-instance-role)). Rejected:
  filling it like the items above, with `IamInstanceProfile = false` for none; it mixed
  two roles in one item (a template item and a switch for creating IAM resources) and
  needed a special value and an exception in merging.
- `KeyName` is not filled. temt logs in without a key pair
  ([login-key.md](login-key.md)); a `KeyName` written (`ami import` copies the
  source's) goes into the template as it is.

## Comparing

`apply`, `diff`, and step 3 of `bake` compare the whole content of the default version
with the config file's, after filling and resolving. Forms that AWS changes on reading
are aligned first; otherwise every `apply` would make a new version.

- A blank value (empty array, empty table, empty string) counts as not written, on
  both sides. AWS drops empty arrays on reading (checked on real AWS).
- `IamInstanceProfile` compares `Name` and `Arn` as the same instance profile; AWS may return
  the ARN.
- The four read-only items are ignored.
- Arrays are not compared by order, since AWS does not keep it. Elements are matched
  by a key: `NetworkInterfaces` by `DeviceIndex`, `BlockDeviceMappings` by `DeviceName`
  (after filling), `TagSpecifications` by `ResourceType`, tags by `Key`. A key repeated
  in the config file (two `DeviceIndex = 0`) is an error. Other arrays compare whole
  elements; a difference shows as an element removed and an element added.
- To check at implementation on real AWS: whether AWS keeps `Ipv6AddressCount = 0`, and
  whether it fills in other items.

## Versions

- If the template does not exist, create version 1 with the content and make it the
  default.
- If it exists, compare as above; if they differ, create a new version and make it the
  default. If they are the same, no new version is created.
- The template's own tag (`temt:service`) is reconciled separately from versions. If it
  is missing or has a different value, write it, and in that case report the template
  as updated even when the contents are the same.
- AWS does not fully check the contents when creating a template or version and
  returns problems as `Warning`. `apply` and `bake` show each as a warning
  (`launch template <name>: <code>: <message>`). For `InvalidIamInstanceProfile.NotFound`
  when `IamInstanceProfile` points at `<name>`, `apply` adds `(IAM has not propagated it
  to EC2 yet; the instance profile exists)`: it has just reconciled the profile and EC2
  does not see it yet ([commands/instance.md](../commands/instance.md#right-after-apply)).
  `bake` does not check the instance profile, which may have been deleted by hand, so it adds
  nothing.
- `bake` does not compare: it creates a new version from the default version's contents
  with only `ImageId` replaced, and makes it the default. It calls
  `create-launch-template-version` with the default version as `SourceVersion` and only
  `ImageId` in `LaunchTemplateData`.

After making a new version the default, delete all other versions so the template keeps
one version. AWS takes at most 200 version numbers per call, so they are deleted 200 at a
time. If deletion fails, warn and continue. `bake` does the same. `bake` changes
the template only after the new AMI is `available` (steps 7 and 8 of
[`bake`](../commands/ami.md#bake)).

Versions other than the default remain when temt is interrupted right after switching
the default or when deletion failed. So `apply`, even when it creates no new version,
deletes the non-default versions, including ones not created by temt.

There is no way to roll back to a previous version; to roll back, revert the config
file and run `apply`. Existing instances (running or stopped) are not affected; the new
default version is used when `start` next launches a new instance.
