# Launch template

Reconciled by step 6 of [`apply`](../commands/service.md#apply), its `ImageId` replaced
by step 8 of [`bake`](../commands/ami.md#bake), deleted by step 4 of
[`destroy`](../commands/service.md#destroy). Name: `<name>-template`. `start` launches
from its default version.

## Contents

- `ImageId`: the ID of the AMI that the config file's `ImageId` points to. If it is
  written as a name, `apply` resolves the ID ([ami.md](ami.md#referring-by-name)).
- `InstanceType`: the config file's `InstanceType`.
- `KeyName`: the config file's `KeyName`.
- `BlockDeviceMappings`: only when the config file has `VolumeSize`. Exactly one entry:
  `DeviceName` is the `RootDeviceName` of the `ImageId` AMI, `Ebs.VolumeSize` is
  `VolumeSize`. Nothing else (volume type, snapshot, `DeleteOnTermination`, ...) is set;
  the AMI's values are used.
- `InstanceInitiatedShutdownBehavior=terminate`.
- `CreditSpecification.CpuCredits=unlimited`: only when `InstanceType` is a T type (one
  starting with `t` and a digit 2 to 9, such as `t3.micro`, `t4g.small`). CPU credits
  mean something only for T types, so the key is omitted for other types and for the
  old-generation `t1`, which has no CPU credits.
- Network interface (`DeviceIndex=0`): the config file's `SubnetId`, security group
  `<name>`, `AssociatePublicIpAddress=true`. If the subnet has an IPv6 CIDR associated
  (an `associated` entry in `Ipv6CidrBlockAssociationSet`), add `Ipv6AddressCount=1`;
  otherwise do not.
- `MetadataOptions.HttpTokens=required`, `MetadataOptions.InstanceMetadataTags=enabled`.
- `IamInstanceProfile`: `<name>`.
- Tags for the instance, volumes, and network interface: `Name`, `temt:service`.

Five of these come from the config file (`ImageId`, `InstanceType`, `KeyName`,
`SubnetId`, `VolumeSize`); temt decides the rest. There is no way to pass keys temt does
not know into the template.

## Versions

- If the template does not exist, create version 1 with these contents and make it the
  default.
- If it exists, compare with the default version's contents; if they differ, create a
  new version and make it the default. Only the keys listed above are compared; keys
  AWS filled in are ignored. If they are the same, no new version is created.
- The template's own tag (`temt:service`) is reconciled separately from versions. If it
  is missing or has a different value, write it, and in that case report the template
  as updated even when the contents are the same.
- AWS does not fully check the contents when creating a template or version and
  returns problems as `Warning`. `apply` and `bake` show each as a warning
  (`launch template <name>: <code>: <message>`). For `InvalidIamInstanceProfile.NotFound`,
  `apply` adds `(IAM has not propagated it to EC2 yet; the instance profile exists)`: it
  has just reconciled the profile and EC2 does not see it yet
  ([commands/instance.md](../commands/instance.md#right-after-apply)). `bake` does not
  check the profile, which may have been deleted by hand, so it adds nothing.
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
