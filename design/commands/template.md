# template show

`template show --svc <svc>`

Shows the details of the launch template
([topics/launch-template.md](../topics/launch-template.md)). It only displays; it writes
nothing to the config file or AWS. It shows:

- The content of the default version: version number, creation time, and the items temt
  manages. `SubnetId` is shown with the VPC that the subnet belongs to. `VolumeSize`
  is shown as `(not set)` when absent. Items that AWS filled in are not shown.
- The AMI that the template's `ImageId` points to: name, creation date, state, and
  root volume size. If there is no EBS root volume, the size is shown as
  `(no EBS root volume)`. If the AMI is not found, say so. In addition, show the name
  and ID of the latest AMI with `temt:service=<svc>`, marked so it is recognizable when
  it is newer than the template's.
- The list of versions, one per line: number, default mark, creation time, `ImageId`,
  `InstanceType`, `SubnetId`.

It does not compare with the config file. What the next `apply` would change in the
launch template is shown by [`diff`](service.md#diff).
