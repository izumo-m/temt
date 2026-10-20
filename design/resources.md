# AWS resources

`apply` creates up to four resources per service when they are absent: the IAM role and
the instance profile (when `[role]` has anything), the security group (when the launch
template uses it), and the launch template. They get their tags
([naming.md](naming.md#tags)) when created; when one already exists, `apply` adds a
missing tag and fixes a tag whose value differs. How each existing resource is
otherwise treated is in its section below.

The launch template, the security group, the instance, and the SSM parameters belong to
one region; a clone in another region has its own ([config.md](config.md#clones)). The
IAM role and instance profile are global and shared by every region.

`destroy` deletes the resources of its region and the SSM parameters under the `/<name>`
hierarchy; the role and instance profile only when no other region still uses them
([Deleting](#deleting)). temt registers no EC2 key pair; how ssh logs in is in
[topics/login-key.md](topics/login-key.md).

`proxy` and `status` look up the instance ID and public IPv4 address each time.

The existence of the launch template is the mark that the service has been applied in
that region and not destroyed. `destroy` deletes the launch template first, so a
service whose `destroy` failed midway also counts as not applied.

## Instance profile and instance role

The instance role is the IAM role in the instance profile. temt makes and manages one
pair only, both named `<name>`.

- `[role]` holds only the content of the instance role `<name>`: each
  `[[role.<policy name>]]` is an inline policy `temt-<policy name>`, and
  `role.ManagedPolicyArns` lists the managed policies
  ([docs/conf.md](../docs/conf.md#role)). Policy names start with a lowercase letter
  and AWS keys with an uppercase one, so they do not collide. The role name is not
  written, so a config file copied to another `<svc>` makes `temt-<another svc>` as it
  is. A `role.<policy name>` that is not an array of tables is a config file error that
  tells the user to write `[[role.<policy name>]]`.
- `IamInstanceProfile` is independent of `[role]`. It is a launch template item like
  any other: written, it goes into the template as it is; not written, the instance has
  no instance profile, `[role]` or not
  ([topics/launch-template.md](topics/launch-template.md)). `apply` never adds the line
  to an existing config file (the requester: what is not set is not set on the user's
  behalf).
- When `[role]` has a policy or `ManagedPolicyArns` in either file, `apply` creates
  `<name>`, reconciles it with `[role]`, and tags the role with its region
  ([Regions](#regions)). The instance gets it only when `IamInstanceProfile` points at
  `<name>`: `Name` is `${temt:name}` or `<name>`, or `Arn` ends in `<name>`. A
  `[role]` while `IamInstanceProfile` points elsewhere is not an error; `<name>` is
  still made.
- When `IamInstanceProfile` points at `<name>` but no file has `[role]` (judged after
  the scaffold's `[[role.ec2]]` is added), `apply` and `diff` stop with a config file
  error before calling AWS (the requester; leaving it to `start` failing was rejected).
  This also keeps `apply` from deleting a role still in use when the user imports an
  instance of the same service with `service import`.
- When `IamInstanceProfile` is not written and `WaitForSsm` is not `false`, `apply` and
  `diff` warn that `start` may wait for the SSM agent until its deadline. Only "may":
  the SSM Default Host Management Configuration can register an instance without a
  role. `start` does not warn.
- The policies on the role are exactly the joined `[[role.*]]`
  ([config.md](config.md#defaults-file)). temt adds no `ec2` of its own: its default
  `ec2` is used only when writing the scaffold
  ([commands/service.md](commands/service.md#apply)). A `[role]` with only `jupyter`
  has no SSM permissions; `start` stops at its deadline and ssh through SSM does not
  connect, which shows the cause.
- The scaffold and `role import` write `IamInstanceProfile.Name = "${temt:name}"`, not
  `"<name>"`: a config file copied to another `<svc>` then attaches the copy's own role,
  where a literal name would silently attach the original service's. When the defaults
  file has `IamInstanceProfile`, the scaffold writes no line and lets the defaults
  file's value work; it writes `[[role.ec2]]` only if that value points at `<name>`,
  so an unused `<name>` is not made.
- If `<name>` exists in IAM with `temt:service` of another service, `apply` stops. An
  untagged one gets the tag and is used.
- Rejected: adding temt's inline policies to an instance role temt does not manage (a
  shared instance role is risky to edit, and services sharing one would delete each
  other's policies); copying the instance role on `service import` (resource policies
  naming the original ARN would stop matching); `[[role.<role name>.<policy name>]]`
  with free role names: after removing or renaming a `[role.X]`, temt, which records
  nothing, could not know which role to delete (the Resource Groups Tagging API does not
  return IAM roles, and `list-roles` returns no tags; checked), and a name limited to
  `temt-<svc>` would be left unchanged by mistake in a copied file.

### Regions

The role and instance profile `<name>` are shared by the clones in every region
(the requester asked for a better plan than one pair per region). A change of `[role]`
reaches every region at once, and resource policies (bucket policies, ...) name one ARN.
Resources that differ by region are written in policies with `*` as the region of the
ARN, or listed; the default `ec2` already uses `*`.

The role carries one tag `temt:region:<region>`, with an empty value, for each region
whose config has `[role]` and was applied there. The tag means only that; it says nothing of whether an
instance uses the role.

- `apply` with `[role]` adds the tag of its region.
- `apply` without `[role]` (an empty one included) removes the tag of its region, and if
  no tag of another region remains, deletes the role and instance profile, even while
  an instance runs (the requester: the user checks the instance without the role, puts
  `[role]` back if needed, or bakes). `diff` and the confirmation show removing the
  tag, keeping the role, or deleting it ([commands/service.md](commands/service.md)).
  This is done after the launch template is reconciled: deleted first, a template
  update failing for another reason would leave the template pointing at a deleted
  instance profile (the same reason `destroy` deletes the template first).
- To check an instance without the role, the user deletes the `IamInstanceProfile`
  line; deleting `[role]` as well deletes the role.
- A role made again gets a new instance profile, and a running instance keeps pointing
  at the old one and gets no credentials; credentials of a deleted role may also stay
  usable for a while. temt does nothing about either (docs/usage.md tells the user).

App-specific roles are not made: an instance has one role, and splitting by AssumeRole
would not separate apps that run with sudo on the same instance. App permissions go in
`[[role.<app name>]]` by hand.

### Long names

`<name>` is not checked for length ([naming.md](naming.md#resource-names)). IAM cannot
hold a role of more than 64 characters, so where temt only reads IAM (`apply` and
`diff` without `[role]`, `destroy`, `role delete`), it treats such a role as absent;
asking IAM would fail and `destroy` could never finish. With `[role]`, `apply` stops
with AWS's error where it creates the role.

### Instance role

IAM role `<name>`. The trust policy allows `ec2.amazonaws.com` only and is never
rewritten. The permissions are the inline policies `temt-<policy name>` built from
`[[role.<policy name>]]` and the managed policies in `ManagedPolicyArns`
([docs/conf.md](../docs/conf.md#role)). IAM evaluates the sum of all policies attached
to the role. Only `apply` attaches policies; `deploy` and `app remove` do not touch the
role.

`ec2` is the policy name for the permissions of the instance itself, not tied to any
app. The default the scaffold writes includes the SSM Agent permissions temt needs
(Session Manager, Run Command) and read/write of the SSM parameters under `/<name>`. The
State Manager permissions the former sshd association needed
(`ssm:ListInstanceAssociations`, `UpdateInstanceAssociationStatus`, `GetDocument`,
`PutComplianceItems`) are dropped. Whether the SSM agent then logs a denial at every
poll is not checked on real AWS (the requester: the earlier checks on real AWS are
enough).

For each `[[role.<policy name>]]`, `apply` adds `Allow` to statements without `Effect`,
substitutes the variables, and writes the policy when absent or different. Then it
deletes the inline policies starting with `temt-` that are not in `[role]`. It writes
before deleting so that renaming a policy leaves no gap without permissions. Inline
policies not starting with `temt-` are not touched.

Managed policies are attached as listed in `ManagedPolicyArns` and those not listed are
detached, as inline policies starting with `temt-` are aligned. The name follows the
role property of CloudFormation. Their content is not expanded into inline policies,
which would exceed the 10,240-character limit on inline policies.

### Instance profile

Instance profile `<name>`. It holds exactly the role above. If it exists without the
role, `apply` adds the role. If it holds another role, `apply` removes that role and
puts this one in its place.

### Deleting

`destroy`, [`role delete`](commands/role.md#role-delete), and `apply` without `[role]`
delete only the pair named `<name>` with `temt:service=<svc>`, whether or not the config
file points at it, and only when no tag of another region remains on the role. `destroy`
and `apply` first remove the tag of their own region; when another region's tag remains,
they keep the pair, and `destroy`'s confirmation shows the role as kept with the regions
that use it. `role delete` stops instead ([commands/role.md](commands/role.md#role-delete)).

The deletion runs in this order: remove the role from the instance profile and delete
the instance profile, then detach the managed policies from the role (the policies
stay; temt did not make them), delete its inline policies (IAM refuses to delete a role
while either remains), and delete the role. Untagged ones, or ones of another service,
are left alone; instance profiles of other names (the source's, after `service import`)
are never targets. If the role is in another instance profile, deletion stops before
detaching or deleting anything; that includes the instance profile `<name>` when it was
left alone because of its tags.

## Security group

Security group `<name>`, when the launch template uses it
([topics/security-group.md](topics/security-group.md#when-it-is-created)). Created in
the VPC of the subnet of the `NetworkInterfaces` element with `DeviceIndex = 0`
(written, or looked up in the region); its ingress rules are reconciled with
`[[security-group.ingress]]`. How they are reconciled, how a VPC change is followed, and
how it is deleted are in [topics/security-group.md](topics/security-group.md).

## Launch template

Launch template `<name>-template`. Its content is the `[launch-template]` of the config
file over the defaults file, the values temt decides for what is not written (decided
again at every `apply`), and what temt adds. `ImageId` is the exception: not written, it
is kept from the template. When anything differs, `apply` creates a new version and
makes it the default. The content and the version handling are in
[topics/launch-template.md](topics/launch-template.md).

## AMI

`bake` and `service import` create AMIs. `ImageId` is normally not written: the launch
template keeps it, and `bake` (and the other commands that bake) replace it in the
template of their region only, never in the config file. `apply --image-id` chooses an
AMI by name or ID. When `apply` creates the template, it chooses `--image-id`, else the
newest by creation time of the own AMIs named exactly `temt-<svc>-` and 10 digits that
are `available` or `pending` (stopping if the newest is `pending`), else Ubuntu 24.04.
They are found by name, not by tag, since a copy may lose its tags. A written `ImageId` pins
the AMI, and the commands that bake are usage errors then. AMIs of other services can be
used by `--image-id` or `ImageId`. Creating and selecting are in
[topics/ami.md](topics/ami.md).
