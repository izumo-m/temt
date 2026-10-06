# AWS resources

`apply` creates six resources per service when they are absent: the key pair, the IAM
role, the instance profile, the security group, the launch template, and the
association. The five other than the key pair get their tags
([naming.md](naming.md#tags)) when created; when one already exists, `apply` adds a
missing tag and fixes a tag whose value differs. How each existing resource is
otherwise treated is in its section below.

`destroy` deletes the five other than the key pair, and the SSM parameters under the
`/<name>` hierarchy. The key pair may be shared by several services, so the `keypair`
commands handle it separately.

`proxy` and `status` look up the instance ID and public IPv4 address with
`describe-instances` each time.

The existence of the launch template is the mark that the service has been applied and
not destroyed. `destroy` deletes the launch template first, so a service whose
`destroy` failed midway also counts as not applied.

## Key pair

Key pair `<KeyName>`. The only key that can log in to the instance is this key pair's
key. If the key pair is absent, `apply` registers it and tags it `temt:keypair`; if it
exists, `apply` leaves it unchanged and adds no tags. How it is matched with the local
key (`IdentityFile` in the config file), and what happens when they do not match, are
in [topics/keypair.md](topics/keypair.md).

## IAM role

IAM role `<name>`. The trust policy allows `ec2.amazonaws.com` only and is never
rewritten. The permissions are only the inline policies `temt-<policy name>` built
from `[[role.policy.<policy name>]]` in the config file
([docs/conf.md](../docs/conf.md#role)); no managed policy is attached. IAM evaluates
the sum of all policies attached to the role. Only `apply` attaches policies; `deploy`
and `app remove` do not touch the role.

`temt-ec2` holds the permissions of the instance itself, not tied to any app. By
default it includes the SSM Agent permissions temt needs (Session Manager, Run
Command, State Manager) and read/write of the SSM parameters under `/<name>`. If the
config file has no `[[role.policy.ec2]]`, `apply` writes the default; if it has one,
that is used.

For each `[[role.policy.<policy name>]]`, `apply` adds `Allow` to statements without
`Effect`, substitutes the variables, compares the result with the current content of
`temt-<policy name>`, and writes it when absent or different. Then it deletes the
inline policies starting with `temt-` that are not in the config file. It writes
before deleting so that renaming a policy leaves no gap without permissions. These are
reported on separate lines `role policy temt-<policy name>: <state>`. Inline policies
not starting with `temt-`, and managed policies, are not touched.

`destroy` deletes all inline policies of the role before deleting the role.

## Instance profile

Instance profile `<name>`. It holds exactly the role above. If it exists without the
role, `apply` adds the role. If it holds another role, `apply` removes that role and
puts this one in its place.

## Security group

Security group `<name>`. Created in the VPC of `SubnetId` in the config file; its
ingress rules are reconciled with `[[security-group.ingress]]` in the config file.
How they are reconciled, how a VPC change is followed, and how it is deleted are in
[topics/security-group.md](topics/security-group.md).

## Launch template

Launch template `<name>-template`. Built from the config file and the items temt
decides. When anything differs, `apply` creates a new version and makes it the
default. The items and the version handling are in
[topics/launch-template.md](topics/launch-template.md).

## Association

Association `<name>-sshd`. A State Manager association that aligns the instance's
sshd listening ports with `SshdPorts` in the config file. `apply` finds it by name and
reconciles its content with the config file; when it creates the association or
updates its content, it asks for a run. The content, the reconciling, and the handling
right after launch are in [topics/sshd-ports.md](topics/sshd-ports.md).

## AMI

`bake` creates an AMI from the service's instance. The launch template gets the AMI
that `ImageId` in the config file points to (an AMI ID or name); if `ImageId` is
absent, `apply` picks the newest AMI baked from that service and writes it by name
(or, if there is none, the Ubuntu 24.04 AMI ID).
AMIs of other services can be used by writing them in `ImageId`. Creating and
selecting are in [topics/ami.md](topics/ami.md).
