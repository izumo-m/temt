# AWS resources

`apply` creates up to four resources per service when they are absent: the IAM role and
the instance profile (when `IamInstanceProfile` points at them), the security group
(when the launch template uses it), and the launch template. They get their tags
([naming.md](naming.md#tags)) when created; when one already exists, `apply` adds a
missing tag and fixes a tag whose value differs. How each existing resource is
otherwise treated is in its section below.

`destroy` deletes them, and the SSM parameters under the `/<name>` hierarchy.
temt registers no EC2 key pair; how ssh logs in is in
[topics/login-key.md](topics/login-key.md).

`proxy` and `status` look up the instance ID and public IPv4 address with
`describe-instances` each time.

The existence of the launch template is the mark that the service has been applied and
not destroyed. `destroy` deletes the launch template first, so a service whose
`destroy` failed midway also counts as not applied.

## Instance profile and instance role

The instance role is the IAM role in the instance profile. The design was redone from
scratch (the requester): before, `IamInstanceProfile` was both a launch template item
and the switch for creating IAM resources, which needed filling it when missing, a
`false` for none, creating any missing name, and finding what to delete by name.

- `IamInstanceProfile` is a launch template item like any other: written, it goes into
  the template as it is; not written, the instance has no instance profile
  ([topics/launch-template.md](topics/launch-template.md#filled-when-missing)).
- temt makes and manages one pair only: the instance role and instance profile named
  `<name>`. `apply` creates them and reconciles them with `[role]` only when
  `IamInstanceProfile` points at `<name>`: `Name = "${temt:name}"`, `Name = "<name>"`,
  or an `Arn` whose name is `<name>`. Pointing elsewhere, temt creates and changes
  nothing; an instance profile that does not exist makes `start` fail with AWS's error.
- If `<name>` exists in IAM with `temt:service` of another service, `apply` stops. An
  untagged one gets the tag and is used.
- The `apply` that creates the service writes `IamInstanceProfile = { Name =
  "${temt:name}" }` and the default `[[role.policy.ec2]]` into the config file. The name
  shows in the config file, and a copied config file does not point at the original
  service's. The rule that writes the default `[[role.policy.ec2]]` when neither the
  config file nor the defaults file has one, and that brings it back when all are
  deleted, works only while `IamInstanceProfile` points at `<name>`.
- `[role]` (`[[role.policy.*]]`, `ManagedPolicyArns`) while `IamInstanceProfile` does
  not point at `<name>` (not written included) makes `apply` stop, naming the file of
  each item, the defaults file included. To change the permissions, fix the source
  instance role by hand, or move it under temt with [`role
  import`](commands/role.md#role-import). Rejected: adding temt's inline policies to an
  instance role temt does not manage (a shared instance role is risky to edit, and
  services sharing one would delete each other's policies); copying the instance role
  on `ami import` (resource policies naming the original ARN would stop matching).

### Instance role

IAM role `<name>`. The trust policy allows `ec2.amazonaws.com` only and is never
rewritten. The permissions are the inline policies `temt-<policy name>` built from
`[[role.policy.<policy name>]]` and the managed policies in `ManagedPolicyArns`
([docs/conf.md](../docs/conf.md#role)). IAM evaluates the sum of all policies attached
to the role. Only `apply` attaches policies; `deploy` and `app remove` do not touch the
role.

`temt-ec2` holds the permissions of the instance itself, not tied to any app. By
default it includes the SSM Agent permissions temt needs (Session Manager, Run
Command) and read/write of the SSM parameters under `/<name>`. The State Manager
permissions the former sshd association needed (`ssm:ListInstanceAssociations`,
`UpdateInstanceAssociationStatus`, `GetDocument`, `PutComplianceItems`) are dropped. At
implementation, check on real AWS whether the SSM agent then logs a denial at every
poll; if it does, ask the requester whether to drop them.

For each `[[role.policy.<policy name>]]`, `apply` adds `Allow` to statements without
`Effect`, substitutes the variables, compares the result with the current content of
`temt-<policy name>`, and writes it when absent or different. Then it deletes the
inline policies starting with `temt-` that are not in the config file. It writes
before deleting so that renaming a policy leaves no gap without permissions. These are
reported on separate lines `role policy temt-<policy name>: <state>`. Inline policies
not starting with `temt-` are not touched.

Managed policies are attached as listed in `ManagedPolicyArns` and those not listed are
detached, as inline policies starting with `temt-` are aligned. The name follows the
role property of CloudFormation. Their content is not expanded into inline policies,
which would exceed the 10,240-character limit on inline policies.

### Instance profile

Instance profile `<name>`. It holds exactly the role above. If it exists without the
role, `apply` adds the role. If it holds another role, `apply` removes that role and
puts this one in its place.

### Deleting

`destroy` and [`role delete`](commands/role.md#role-delete) delete only the pair named
`<name>` with `temt:service=<svc>`, whether or not the config file points at it. In
order: remove the role from the instance profile and delete the instance profile, then
detach the managed policies from the role (`detach-role-policy`; the policies stay, temt
did not make them), delete its inline policies (`delete-role-policy`; IAM's `DeleteRole`
refuses with `DeleteConflict` while either remains), and delete the role, as
[`destroy`](commands/service.md#destroy) step 4 orders them. Untagged ones, or ones of
another service, are left alone;
instance profiles of other names (the source's, after `ami import`) are never targets. If the
role is in another instance profile, they stop before detaching or deleting anything;
that includes the instance profile `<name>` when it was left alone because of its tags.

## Security group

Security group `<name>`, when the launch template uses it
([topics/security-group.md](topics/security-group.md#when-it-is-created)). Created in
the VPC of the `SubnetId` of the `NetworkInterfaces` element with `DeviceIndex = 0`;
its ingress rules are reconciled with `[[security-group.ingress]]` in the config file.
How they are reconciled, how a VPC change is followed, and how it is deleted are in
[topics/security-group.md](topics/security-group.md).

## Launch template

Launch template `<name>-template`. Its content is the config file's `[launch-template]`
and what temt decides. When anything differs, `apply` creates a new version and makes
it the default. The content and the version handling are in
[topics/launch-template.md](topics/launch-template.md).

## AMI

`bake` and `ami import` create AMIs. The launch template gets the AMI that `ImageId` in
the config file points to (an AMI ID or name); if `ImageId` is absent, `apply` picks the
newest AMI baked from that service and writes it by name (or, if there is none, the
Ubuntu 24.04 AMI ID). AMIs of other services can be used by writing them in `ImageId`.
Creating and selecting are in [topics/ami.md](topics/ami.md).
