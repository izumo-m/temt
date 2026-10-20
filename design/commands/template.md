# template show

`template show --svc <svc>`

Shows the details of the launch template of the region
([topics/launch-template.md](../topics/launch-template.md)). It only displays; it writes
nothing to the config file or AWS. It shows:

- The content of the default version: version number, creation time, and the whole
  content in the TOML form of the config file's `[launch-template]`, so it can be copied
  into a config file as is. The four items that exist only when read are not shown.
  `SubnetId` (of the `NetworkInterfaces` element with `DeviceIndex = 0`) is shown with
  the VPC that the subnet belongs to.
- The AMI that the template's `ImageId` points to: name, creation date, state, and
  root volume size. If there is no EBS root volume, the size is shown as such. If the
  AMI is not found, say so. In addition, show the name and ID of the service's newest
  AMI, found as `apply` finds it for a new template (by its exact name, newest by
  creation time; [Selecting](../topics/ami.md#selecting)), marked when it is newer than
  the template's.
- The list of versions, one per line: number, default mark, creation time, `ImageId`,
  `InstanceType`, `SubnetId` (of the `DeviceIndex = 0` element).

It does not compare with the config file. What the next `apply` would change in the
launch template is shown by [`diff`](service.md#diff).
