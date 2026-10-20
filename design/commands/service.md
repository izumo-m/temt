# service commands

## `apply`

`service apply --svc <svc> [--image-id <AMI ID or name>] [--yes]` (short form `apply`)

Creates the service's AWS resources in the region, its ssh settings, the default login
key, and the service command if they are missing, and brings existing ones in line with
the config file. Running it again gives the same result. Each step is an independent
process that takes the config file as the source of truth and reconciles one target.

As in CDK, the config file holds only what the user changed from the defaults. `apply`
writes nothing into an existing config file: values not written are decided again at
every run ([Filled when missing](../topics/launch-template.md#filled-when-missing)), and
the AMI lives in the launch template ([Selecting](../topics/ami.md#selecting)). The same
config file applied in another region makes a clone there
([config.md](../config.md#clones)). Only when no config file exists does `apply` write
one, the [scaffold](#scaffold).

`apply` runs in four phases: detect, show, confirm, update.

- Detect: walk steps 1-6 below and detect the difference for each target. Detection is
  the same as `diff`. Nothing is written to the config file, AWS, or local files in
  this phase. If a step stops, temt exits having written nothing.
- Show: with no differences, report so and finish. Otherwise print the differences to
  stdout in the same format as [`diff`](#diff).
- Confirm: ask `[y/N]`, naming the region and the account
  ([cli.md](../cli.md#confirmation)).
- Update: apply the detected differences in step order. Report what was created,
  updated, or deleted; do not report what was left unchanged. Changes in AWS between
  detection and update are not handled.

When the config file does not exist, `apply` first asks `[y/N]` whether to create the
service, naming the region and the account, before anything else reads AWS, so a
mistyped `<svc>` stops there. `--yes` skips this question too. A new service is thus
asked twice.

If it fails midway, running the same command again continues from where it stopped.
If it stops during the update phase, whatever was already written stays.

1. Check the service name against [naming.md](../naming.md#service-names). If the
   `Host <name>` block in the ssh directory's `config` was written by `apply` from
   another conf directory, and the service's config file still exists there (unless it
   is the same file as the one in the current location), exit naming that location
   ([topics/ssh.md](../topics/ssh.md#services-across-conf-directories)).
2. Read the config file over the defaults file ([config.md](../config.md#defaults-file)).
   If it does not exist, read the [scaffold](#scaffold) in its place, and the update
   writes the scaffold first. Then:
   - `--image-id` while `ImageId` is written is a usage error.
   - If `IamInstanceProfile` points at `<name>` and no file has `[role]` (judged with the
     scaffold's `[[role.ec2]]` included), stop with a config file error before calling
     AWS ([resources.md](../resources.md#instance-profile-and-instance-role)).
   - If `IamInstanceProfile` is not written and `WaitForSsm` is not `false`, warn that
     the instance gets no instance role and `start` may wait for the SSM agent until its
     deadline. Only `apply` and `diff` warn; `start` does not.
   - Decide the values not written ([Filled when
     missing](../topics/launch-template.md#filled-when-missing)), looking up `SubnetId`
     and `Ipv6AddressCount` in the region. Later steps use them as if written.
3. Detect creating, or tightening to mode 700, the ssh directory, `keys/`, and their
   missing parent directories. If the service command `~/.local/bin/<name>` is absent,
   detect writing it. If anything is there, leave it alone whatever its content; a
   dangling symlink counts as present. When writing it on update, create
   `~/.local/bin` if missing. Whether `temt` is on PATH is not checked: if it is not,
   the user notices when running ssh.
4. Check that the subnet (of the `NetworkInterfaces` element with `DeviceIndex = 0`)
   exists and has an IPv4 CIDR, and get its VPC and whether it has an IPv6 CIDR. If a
   written subnet does not exist, add that for another account the lines that point
   into the account should be removed ([config.md](../config.md#account)).
   Decide the AMI ([Selecting](../topics/ami.md#selecting)): a written `ImageId`, else
   `--image-id`, else the template's `ImageId` when the template exists, else the AMI
   chosen for a new template. If the AMI given by `ImageId` or `--image-id` is missing
   or unusable, stop naming it ([Referring by
   name](../topics/ami.md#referring-by-name)). If the template's AMI no longer exists
   or is not `available`, stop and tell the user to choose one with `--image-id`. Fill in the omitted
   `DeviceName`, look up security group names in `Groups`, and substitute the variables
   ([topics/launch-template.md](../topics/launch-template.md#extensions-to-awss-form)).
   - If the root volume element has `Ebs.VolumeSize`, compare it with the size of the
     AMI's root volume. If it is smaller, stop, naming both sizes, because AWS accepts
     the smaller value in the launch template and refuses it only at launch. If the AMI
     has no EBS root volume (its size is unknown), stop and tell the user to remove
     `Ebs.VolumeSize`.
   - If there are ingress rules and none of the security groups of the template
     (`Groups` of any `NetworkInterfaces` element, or the top-level `SecurityGroupIds`
     or `SecurityGroups`) is temt's `<name>`, stop, naming the file of the rules and
     telling the user to add `${temt:name}` to `Groups`
     ([topics/security-group.md](../topics/security-group.md#when-it-is-created)).
   - For each `[[role.<policy name>]]` (joined across the files), fill in `Effect`,
     substitute the variables to build the content of `temt-<policy name>`, and have
     IAM Access Analyzer validate it. If there are errors, list all of them, each by the
     file and the position of its statement ([docs/conf.md](../../docs/conf.md#role)).
5. Detect the differences of the IAM role, its inline and managed policies, the
   instance profile, the security group, and the launch template, in this order, and
   then of removing the role; the update reconciles them in the same order.
   - With `[role]` (a policy or `ManagedPolicyArns` in either file), the role
     `<name>`, its policies, and the instance profile are reconciled and the role gets
     the tag of the region, whatever `IamInstanceProfile` points at
     ([resources.md](../resources.md#instance-profile-and-instance-role)). Inline
     policies deleted are those starting with `temt-` that are not in `[role]`. Managed
     policies not in `ManagedPolicyArns` are detached. If the instance profile holds
     another role, the role `<name>` takes its place.
   - Without `[role]`, the role loses the tag of the region, and when no other region's
     tag remains, the role and the instance profile are deleted
     ([Regions](../resources.md#regions), [Deleting](../resources.md#deleting)). This
     comes after the launch template.
   - The security group is created only when `Groups` is filled by temt or names it,
     and its ingress rules are reconciled whenever it is among the template's security
     groups ([topics/security-group.md](../topics/security-group.md#when-it-is-created)).
     If it does not exist yet, the launch template difference is detected with its ID
     as `(new)`, and the update puts in the ID of the security group it created.
   - After creating the instance profile, wait until IAM shows it. Even so, EC2 may not
     see it right after; `start` then fails with that
     ([instance.md](instance.md#right-after-apply)).
6. Detect adding or updating the `Host <name>` block in the ssh directory's `config`
   ([topics/ssh.md](../topics/ssh.md#the-ssh-directorys-config)). If the block, after
   this, uses the default login key and the key does not exist, detect creating it; it
   is created before the block is written
   ([topics/login-key.md](../topics/login-key.md#the-default-key)). Detect making the
   line after the marker in `~/.ssh/config` an `Include` that points to that `config`.
   When replacing the `Include`, warn on update about the Hosts in the file the previous
   `Include` pointed to that ssh can no longer see ([topics/ssh.md](../topics/ssh.md)).
   - If either file changed between detection and the update, stop without writing it.
   - Writing these files is serialized by a lock per ssh directory, regardless of the
     service; a second `apply` waits until it is released. Concurrent `apply` runs for
     different services do not lose each other's edits. `apply` runs started
     concurrently with different `TEMT_SSH_DIR` do not protect each other's edits of
     `~/.ssh/config`.

### Scaffold

The config file `apply` writes when none exists, after the question above. It holds:

- A comment at the top on how to edit it.
- `IamInstanceProfile.Name = "${temt:name}"` in `[launch-template]`, so the instance
  gets temt's role. Written by name with the variable so that a copy for another
  `<svc>` attaches that service's role
  ([resources.md](../resources.md#instance-profile-and-instance-role)). When the
  defaults file has `IamInstanceProfile`, this line is not written and the defaults
  file's value works, as for any other key. Rejected: copying the defaults file's value
  into the scaffold.
- The default `[[role.ec2]]`, written as values, so a later temt with another default
  does not change it. Not written when the defaults file's `IamInstanceProfile` points
  at another role, so an unused `<name>` is not made. A `[[role.ec2]]` in the defaults
  file is joined with it. temt's default `ec2` is used only here.
- Examples as comments: ingress rules and policies.

Nothing else is written. It is written on update only if the file is still absent; if
one appeared meanwhile, `apply` stops without overwriting it.

Rejected: adding `IamInstanceProfile` to an existing config file that lacks it (the
requester: what is not set is not set on the user's behalf). `apply` and `diff` warn
instead (step 2).

## `diff`

`service diff --svc <svc> [--image-id <AMI ID or name>]` (short form `diff`)

Writes what the next `apply` (with the same `--image-id`) would change to stdout, one
change per line. It walks the steps of `apply` in the same order and only detects
differences; it writes nothing to the config file, AWS, or local files. It takes no lock
and asks no confirmation. If the config file does not exist, it exits telling the user
to run `apply`. A missing launch template is shown as to be created. It warns as
`apply` does about a missing `IamInstanceProfile`. Where `apply` would stop, `diff`
prints the changes detected so far and then exits with the same error. It prints:

| Step | Kind | Name | Printed |
| --- | --- | --- | --- |
| 2 | `conf` | Key position (`launch-template.IamInstanceProfile.Name`, `role.ec2`) | Only from `apply` for a new service: the values the scaffold writes |
| 3 | `dir` | The ssh directory, `keys/`, and their missing parents | Creating; setting the mode to 700 |
| 3 | `command` | The service command | Writing |
| 5 | `role` | `<name>` | Creating; tags to add, the region's tag included |
| 5 | `role-policy` | `temt-<policy name>` | Adding, rewriting, deleting; the content difference |
| 5 | `managed-policy` | The policy ARN | Attaching, detaching |
| 5 | `instance-profile` | `<name>` | Creating; tags to add; the role to put in (swapping out another role if one is there) |
| 5 | `security-group` | `<name>` | Creating; tags to add |
| 5 | `ingress` | `PORT` | Rules to add, rules whose description is fixed, rules to delete |
| 5 | `launch-template` | `<name>-template` | Creating; per-item differences; tags to add; non-default versions to delete |
| 5 | `role` | `<name>` | Removing the region's tag, and either keeping the role, naming the regions whose tags remain, or deleting it with its inline and managed policies |
| 5 | `instance-profile` | `<name>` | Deleting, with the role |
| 6 | `key` | The default login key | Creating it |
| 6 | `ssh-host` | `<name>` | Adding or rewriting the Host block; the content difference |
| 6 | `ssh-include` | `~/.ssh/config` | Adding or replacing the `Include` line (creating `~/.ssh` if missing) |

Line format:

- Each line has a leading mark, the kind, the name, and the difference, aligned with
  spaces. The mark is one of `+` (create, add), `~` (fix), `-` (delete). Removing the
  region's tag is `~`; deleting the role is `-`.
- A changing value is written `<current value> -> <new value>`. Tags to add are written
  `tags: <key>=<value>`.
- Ingress rules have `PORT` as the name and the source and description as the
  difference, in the same format as [`sg diff`](sg.md#sg-diff).
- The launch template prints one line per differing leaf item, items to be removed
  included ([topics/launch-template.md](../topics/launch-template.md#comparing)). The
  same key positions are used by the `conf` lines.
  - Tables are joined with `.`. An array element is written by its matching key as
    `[<key>=<value>]`, never by its index, since the order differs between the config
    file and the template.
  - The absent side is `-`; a removed item also gets the mark `~`.
  - A table added or removed whole is split into one line per leaf key.
  - An array without a matching key is joined with `,` if it holds strings
    (`Groups: sg-a,sg-b -> sg-a`); an element of an array of tables is written as
    one-line JSON.
  - `UserData` is decoded from base64 and shown as a unified diff, as inline policies
    are; if it is not text, only its byte count.
  - The new `ImageId` is followed by the AMI's name in parentheses. When Ubuntu 24.04
    was chosen because the service has no AMI of its own in the region, the parentheses
    say that instead, naming the region. A user who forgot to copy the AMI to a new
    region sees it before confirming.
  - The ID of a security group not created yet is written `(new)`. When expanding per
    item shows no difference, it prints `new version`. When no new version is created
    but non-default versions remain, it prints deleting them as `delete other versions:
    <number>, ...`.

  ```
  ~   launch-template   temt-web-template   NetworkInterfaces[DeviceIndex=0].SubnetId: subnet-aaa -> subnet-bbb
  ~   launch-template   temt-web-template   BlockDeviceMappings[DeviceName=/dev/sda1].Ebs.VolumeSize: - -> 30
  ~   launch-template   temt-web-template   KeyName: mykey -> -
  ~   launch-template   temt-web-template   TagSpecifications[ResourceType=instance].Tags[Key=Env]: - -> demo
  ```
- The content of inline policies (JSON with sorted keys, indented), `UserData`, and the
  Host block are shown as differences in unified diff form (with context lines, `-` on
  removed lines, `+` on added lines), indented on the following lines.
- If replacing the `Include` makes some Hosts invisible to ssh, they are listed on the
  following lines.

Lines are ordered by the steps of `apply`; ingress rules are in the same order as
`sg diff`. With no differences, nothing is written to stdout and that is reported on
stderr. The exit code is 0 whether or not there are differences. Example:

```
~   role-policy       temt-app
    @@ -2,5 +2,8 @@
       "Statement": [
         {
    -      "Action": "s3:GetObject",
    +      "Action": [
    +        "s3:GetObject",
    +        "s3:PutObject"
    +      ],
           "Effect": "Allow",
           "Resource": "arn:aws:s3:::web-data/*"
+   ingress           443/tcp             0.0.0.0/0   https
~   launch-template   temt-web-template   InstanceType: t3.micro -> t3.small
```

## `destroy`

`service destroy --svc <svc> [--yes]` (short form `destroy`)

Terminates the instance and deletes the AWS resources `apply` created in the region.
The role and instance profile are deleted only when no other region uses them
([Regions](../resources.md#regions)). The config file, the login key, the Host block in
`~/.ssh/temt/config`, the known_hosts directory, the service command, and AMIs are
kept, and the next `apply` uses them as they are. There is no `--async`; it always waits
for completion. Without the config file it stops ([cli.md](../cli.md#missing-config-file)).

1. Take the lock and find instances under the same conditions as
   [`start`](instance.md#start), including those shutting down.
2. List the IDs and states of the instances found and the names of the resources to
   delete, and ask for confirmation, naming the region and the account. The role is
   listed with the names of its inline and managed policies when it will be deleted;
   when another region's tag remains, it is listed as kept, with those regions.
3. If there are instances, terminate them as [`terminate`](instance.md#terminate)
   does, deleting their known_hosts files, and wait until all of them are `terminated`.
4. Delete the launch template, the SSM parameters, the security group, and then remove
   the region's tag from the role and delete the instance profile and the role if no
   other region's tag remains, in this order. Report each as deleted, kept, or already
   absent.
   - SSM parameters: delete everything under the `/<name>` hierarchy, found by path,
     not by tag.
   - Security groups: delete every one named `<name>`, in any VPC of the region
     ([topics/security-group.md](../topics/security-group.md#deleting)).
   - The instance profile and the role: only those named `<name>` with the tag
     `temt:service=<svc>`, whether or not the config file points at them or has
     `[role]` ([Deleting](../resources.md#deleting)). Untagged ones, or ones tagged for
     another service, are left alone; so are instance profiles of other names (the
     source's, after `service import`). A `<name>` too long for IAM counts as absent
     ([Long names](../resources.md#long-names)).
   - From the instance profile, remove only the role `<name>`. If it holds another
     role, stop without deleting it, telling the user to remove that role by hand and
     run `destroy` again. From the role, detach all managed policies (the policies
     themselves stay; temt did not make them) and delete all inline policies, whoever
     owns them; IAM refuses to delete a role while either remains.
   - If the role is in another instance profile, stop before deleting the instance
     profile or touching the role.
   - Deleting the instance profile or the role can still fail because something is
     attached: right after the role was removed from the instance profile, IAM may not
     show it yet, and something may have been attached after the check. AWS's text does
     not tell which, so the error quotes it and gives both remedies (run `destroy`
     again; if it persists, or something is attached, remove or detach it by hand).
5. Report what was left for the next `apply`: the config file, the Host block, the
   known_hosts directory, the service command, and the AMI the next `apply` would choose
   ([Selecting](../topics/ami.md#selecting)), if any. The login key is shared by the
   services and not listed. The service's lock is left as it is.

If it fails midway, running the same command again continues from where it stopped.

## `service list`

`service list [--svc <svc>] [--format <columns>]`

Lists the local config files as a table, one service per file, in service-name order.
It does not call AWS. For the AWS side, use [`instance list`](instance.md#instance-list)
for instances and [`template show`](template.md) for the launch template. `--profile`
and `--region` are usage errors.

With `--svc`, it reads only that service's config file (over the defaults file) and
prints one row. If the file does not exist, it tells the user to run `apply`; if it
cannot be read, it gives the reason; either way it exits with code 1.

Without `--svc`, it reads `*.toml` in the conf directory. Files not ending in `.toml`,
files starting with `.` (such as the editor lock file `.#<svc>.toml`), and the defaults
file `defaults.toml` are skipped silently. If the conf directory does not exist, only
the header is printed. Files that cannot be read, and files whose name minus `.toml`
does not satisfy the service-name rules, are warned about and skipped; the rest are
listed, then temt reports the number skipped and exits with code 1. Every file is read
over the defaults file, so a defaults file that cannot be read (including a broken
symlink) stops the command before listing, naming the defaults file.

| Column | Default | Value |
| --- | --- | --- |
| `SERVICE` | yes | File name minus `.toml` |
| `PROFILE` | yes | `AwsProfile` |
| `REGION` | yes | `AwsRegion` |
| `TYPE` | yes | `InstanceType` |
| `INGRESS` | | Number of `[[security-group.ingress]]` |

Values are those of the config file read over the defaults file (not with
`Defaults = false`). `PROFILE` and `REGION` show the written value, else `-`, so the
services pinned to a profile or region can be told apart. `TYPE` shows the default when
not written. There are no columns for the AMI and the subnet: they are not written
normally, and are decided in AWS per region. There is no column for the root volume
size: `service list` does not call AWS, so it cannot tell which `BlockDeviceMappings`
element is the root volume when `DeviceName` is written.

## `service import`

`service import --svc <svc> <instance ID> [--yes]`

Imports an existing stopped instance, one usually kept stopped and started when needed,
as a new service: makes an AMI from its root volume and a config file whose
`[launch-template]` launches the same instance from that AMI. `apply` then creates the
launch template, and the service runs in temt's usual cycle: `start` when needed,
`terminate` when done. It has no short form.

The requester's principles:

- The service launches the same instance: a service that used to `start` and `stop` one
  instance keeps working when the same instance is created and terminated instead.
  Breaking it by importing is to be avoided above all.
- Nothing is added or changed inside the instance.
- The source instance and the AWS resources it uses (security groups, instance
  profile, instance role, ...) are left as they are: not stopped, terminated, retagged,
  or reattached. Changes are made afterwards by editing the config file and running
  `apply`. The source remains the way back when the import does not work out.

The contents carry over in the AMI; the source instance itself is not taken under temt.
The import does not deploy `idle-shutdown`; the user deploys it afterwards, as for any
service.

The source's region is the region of the run, given with `--region` and the like
([config.md](../config.md#region)); the profile likewise. Neither is written to the
config file.

1. If the config file exists, exit doing nothing: an import goes only into a service
   without one. Find the instance by ID. If it is not `stopped`, exit telling the user
   to stop it first; temt neither stops nor restarts it.
2. Read its settings (`get-launch-template-data`) and fix them as in [What is
   copied](#what-is-copied). If something cannot be copied, exit.
3. Confirm, naming the region and the account, and showing the instance ID, its `Name`
   tag, the name of the AMI to make, the settings to copy, the source's private IP
   address (and IPv6 address, if any) with the note that it changes at every launch,
   and the EBS volumes other than the root volume, one per line (device name, volume
   ID, and size), with the note that they are not put in the AMI and that to use one,
   the user detaches it from the source instance and attaches it from inside the
   instance. Unlike the other differences below, these volumes are shown because temt
   chooses to leave them out of the AMI, and the import should not end with their
   contents missing unnoticed. If the source is not registered with SSM, warn here too.
4. Create the AMI, leaving out the EBS volumes other than the root volume and making the
   root volume deleted on termination ([Creating](../topics/ami.md#creating)).
5. Wait until `available`, with no deadline, as `bake` does. Errors such as an
   unusable KMS key of an encrypted EBS are shown as AWS returned them, as in `bake`.
6. Create the config file, headed by a comment that `service import` wrote it and that
   it is edited and applied. If one appeared in the meantime, stop without overwriting
   it: it is written only when new.
7. Tell the user to run `apply`, which creates the launch template.

The AMI's name and tags are those of `bake` (`temt-<svc>-<YYMMDDHHMM>`; `Name` and
`temt:service=<svc>` on the AMI and its snapshots), so it shows in `ami list` and is a
candidate of `ami prune`. Until `apply`, and after `destroy`, no launch template refers
to it, so `ami prune` deletes it (showing its name before deleting). `apply` then stops
because the AMI is missing; deleting the `ImageId` line makes `apply` choose as for a
new template, which picks Ubuntu when the service has no AMI left, seen at login. The
source remains, so the config file can be deleted and the import run again. Nothing is
done about this (the requester).

### What is copied

The output of `get-launch-template-data` goes into `[launch-template]`
([topics/launch-template.md](../topics/launch-template.md)); temt's own keys per item
were rejected, since items temt does not know would not be copied. Items that would not
make a template, or not the same instance, are fixed (checked on real AWS where noted
in the rationale):

| Item | Handling | Reason |
| --- | --- | --- |
| `ImageId` | The name of the AMI made | It pins the service to that AMI ([Selecting](../topics/ami.md#selecting)); to bake, the user deletes the line |
| `BlockDeviceMappings` | Removed | The AMI's are used. They point at the source's volumes, and a volume added later has an empty `SnapshotId`, which AWS refuses |
| `Placement.AvailabilityZoneId` | Removed | Cannot be given with `AvailabilityZone` (refused on real AWS) |
| `NetworkInterfaces` `AssociatePublicIpAddress` | The ENI's attribute that keeps the launch setting | A stopped instance returns `false`, having lost its public IP |
| `NetworkInterfaces` `PrivateIpAddresses` | Removed; the number of secondary ones goes to `SecondaryPrivateIpAddressCount` (not written when none) | [Addresses](#addresses) |
| `NetworkInterfaces` `PrivateIpAddress`, `NetworkInterfaceId` | Removed | Values of the source |
| `NetworkInterfaces` `Ipv4Prefixes`, `Ipv6Prefixes` | Removed; their numbers go to `Ipv4PrefixCount`, `Ipv6PrefixCount` | The source keeps delegated prefixes too |
| `NetworkInterfaces` `Ipv6Addresses` | Removed; the number goes to `Ipv6AddressCount`, `0` included | Without it, temt fills `1` and a subnet with IPv6 gives an address the source lacked |
| `NetworkInterfaces` `DeleteOnTermination` | `true` on every element | ENIs attached later return `false`; copied as is, one would be made at every launch and left at every termination, piling up unseen. Every ENI of a template is made anew at launch, so deleting it with the instance is closest to the source. A `false` the user writes later is left alone |
| `NetworkInterfaces` `Groups` | Copied as is | [Security groups](#security-groups) |
| The four items that exist only when read | Removed | Cannot be written |
| `InstanceInitiatedShutdownBehavior` | `terminate` | [Shutdown](#shutdown) |
| `TagSpecifications` | Copied as is (the source's `Name` too); temt adds `temt:service`. Keys starting with `aws:` are removed, and the removal is reported; an element left without tags is removed | Volume and ENI tags are not returned, so not copied, and their `Name` becomes `<name>`. `aws:` keys are reserved by AWS (whether they come back is unchecked) |
| `IamInstanceProfile` | Copied as is (by `Arn`); not written when absent (none is returned then, checked on real AWS) | [Instance profile](#instance-profile) |
| `UserData` | Copied as is | [User data](#user-data) |
| `InstanceMarketOptions` | Removed | A stopped Spot instance came from a persistent request (only those can stop); copied, AWS would launch the next one at every termination. It launches on demand; not reported. temt does nothing about an `InstanceMarketOptions` the user writes, persistent included (the requester). Rejected: making it a one-time Spot |
| `DisableApiTermination`, `DisableApiStop` | Removed | temt keeps the contents in the AMI and terminating is routine; copied, AWS would refuse `terminate` and `stop`. Not reported; the user can write them |
| Blank values (empty arrays, `GroupName = ""`, ...) | Removed | Count as not written; keeps the file short |

The items of [Filled when missing](../topics/launch-template.md#filled-when-missing) are
always written, since temt's values decided at every `apply` would make a different
instance silently. When the source's value cannot be read, AWS's default (the value when
launched without the item) is written, not temt's. `InstanceInitiatedShutdownBehavior`
is the exception.

At the top level, the config file gets only `WaitForSsm = false`, when the source is
not registered with SSM ([instance.md](instance.md#start)). `AwsRegion` and
`AwsProfile` are not written, as by `apply`.

Making a template copied this way was checked on real AWS; launching from it was not
(the requester: the earlier checks on real AWS are enough). `CpuOptions` and the like
are copied as well.

### Defaults file

`Defaults = false` is not written (the requester: the service starts with the
credentials and region used for the import). So the defaults file applies to the
imported service as to any other:

- Its `[launch-template]` items are added, even those the source lacks.
- Its `IamInstanceProfile` does not replace the source's when the source has one, so
  its `[role]` makes `<name>` but does not attach it. When the source has none, the
  defaults file's `IamInstanceProfile` works; if it points at `<name>`, the role is
  attached when there is `[role]` (`diff` shows it), and otherwise `apply` stops
  ([resources.md](../resources.md#instance-profile-and-instance-role)).
- Its ingress rules stop `apply`, since the imported `Groups` do not hold temt's
  security group ([topics/security-group.md](../topics/security-group.md#when-it-is-created)).

The user who does not want this writes `Defaults = false` by hand
([config.md](../config.md#defaults-file)).

### Addresses

The private IP and IPv6 addresses are not copied; they change at every launch. The
source, kept stopped, holds its addresses, so a copied address would make AWS refuse
`start` until the source is terminated, and the source must remain as the way back.
Secondary private IPs and IPv6 addresses are copied by number. To fix the address, the
user terminates the source and writes `PrivateIpAddress` in `NetworkInterfaces`. If two
or more ENIs make `AssociatePublicIpAddress` invalid, `start` gets AWS's error.

Elastic IPs are not handled: a launch template cannot attach one, and `start` does not.
Nothing is reported when the source has one. Services that need a fixed instance ID
(target groups, CloudWatch alarms, EventBridge, Route 53 health checks, SSM
associations targeting the ID, Elastic IPs, ...) are outside temt: the import neither
looks for them nor reports them, and `start` does not reattach them.

Other unavoidable differences of a new instance are not reported but written briefly in
the usage doc: each `start` makes a new instance, so the instance ID, MAC address, and
host name change, and what is bound to them (licenses, the source/destination check,
volume and ENI tags) does not carry over. Host keys are not mentioned, since temt keeps
known_hosts per instance.

### Security groups

The source's security groups are copied into `Groups` by the IDs AWS returned, so they
point at the same ones, and temt's security group is not made
([topics/security-group.md](../topics/security-group.md#when-it-is-created)). Direct
connection works if the source's security groups allow it; temt does not touch them.

### Instance profile

No instance profile on the source means none for the service: an import makes a
template that launches the same instance, and temt's instance role and the like are
extensions added later by editing the config file (the requester). An instance profile
that no longer exists is copied as is: temt creates only `<name>`, so it is not
silently made again, and `start` fails with AWS's error. Rejected: attaching temt's
instance profile (credentials the source lacked); writing `IamInstanceProfile = false`.

An instance of temt's own of the same service (after its config file was deleted) has
`IamInstanceProfile` pointing at `<name>` and no `[role]`, so `apply` stops instead of
deleting the role it uses ([resources.md](../resources.md#instance-profile-and-instance-role)).

### User data

Copied as is, without reporting. A first-boot-only step (`runcmd` and the like) runs at
every `start`; if not wanted, the user deletes the `UserData` line. Rejected: not
copying it, since settings that run at every boot (`bootcmd`, `#cloud-boothook`) and
apps that read the user data from the instance metadata would silently stop working,
and adding it later would mean extracting it from the source.

### Shutdown

`InstanceInitiatedShutdownBehavior` is `terminate` whatever the source's value (the
requester): temt's cycle ends in termination, and with `stop`, a power-off from inside
(an `idle-shutdown` deployed later included) would stop instead of terminate.
`MetadataOptions.InstanceMetadataTags` is copied as is, without reporting; apps that
need the service name check it in their own `install`
([apps/idle-shutdown.md](../apps/idle-shutdown.md#install)).

### Not handled

What `start` or another command reports clearly as an error or a timeout gets no
special handling (the requester: errors say what was run and what happened):

- An AMI of an instance stopped by hibernation, or with hibernation enabled, may not
  connect.
- Tags in the instance metadata: keys with a space or `/` make the launch or the change
  of the metadata options fail.
- A key pair the source names that no longer exists makes `start` fail.
- Writing Elastic IPs in the usage doc.
- Secrets in the user data showing in the local file and in `diff`; reporting the
  change from `stop` to `terminate`; importing when AWS has resources of the same name
  left.
- An AMI deleted by `ami prune` before `apply` or after `destroy` (above).
- The source requirements of temt's login (SSM agent, the instance role's SSM
  permissions, `ec2-instance-connect`, the user of the Host block's `User`) are written
  in the usage doc; only an unregistered SSM agent is warned about.
