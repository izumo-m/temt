# service commands

## `apply`

`service apply --svc <svc> [--yes]` (short form `apply`)

Creates the service's config file, AWS resources, ssh settings, and the default login
key if they are missing. Existing ones are brought in line with the config file. Running
it again gives the same result. Each step is an
independent process that takes the config file as the source of truth and reconciles
one target.

`apply` runs in four phases: detect, show, confirm, update.

- Detect: walk steps 1-6 below and detect the difference for each target. Detection is
  the same code as `diff`. Nothing is written to the config file, AWS, or local files
  in this phase. If a step stops, temt exits having written nothing.
- Show: with no differences, report so (`<name>: no differences`) and finish.
  Otherwise print the differences to stdout in the same format as [`diff`](#diff).
- Confirm: ask `[y/N]` (`apply these changes?`; see
  [cli.md](../cli.md#common-behavior)).
- Update: apply the detected differences in step order. Report what was created,
  updated, or deleted; do not report what was left unchanged. Changes in AWS between
  detection and update are not handled.

When the config file does not exist, step 1 also asks `[y/N]` (`no config file: <path>;
create service <name>?`) before anything reads AWS, so a mistyped `<svc>` stops there.
`--yes` skips this question too. A new service is thus asked twice.

If it fails midway, running the same command again continues from where it stopped.
If it stops during the update phase, whatever was already written stays.

1. Check the service name against [naming.md](../naming.md#service-names). If the
   `Host <name>` block in the ssh directory's `config` was written by `apply` from
   another conf directory, and the service's config file still exists there (unless it
   is the same file as the one in the current location), exit naming that location
   ([topics/ssh.md](../topics/ssh.md#services-across-conf-directories)).
2. Read the config file over the defaults file ([config.md](../config.md#defaults-file));
   if it does not exist, start from the defaults file and the built-in defaults. Detect keys
   that are missing or have a different value, deciding each value as it goes: `AwsRegion`
   ([config.md](../config.md#region)) and the items of [Filled when
   missing](../topics/launch-template.md#filled-when-missing). Missing `ImageId` and
   `SubnetId` are chosen from AWS here. When the file does not exist, also plan to write
   `IamInstanceProfile = { Name = "${temt:name}" }` (the defaults file's
   `IamInstanceProfile` instead, if it has one, as for the other values). A file the user made before the
   first `apply` gets none: the user writes the line there
   ([docs/conf.md](../../docs/conf.md#apply-が書くこと)). Decided with the requester over
   writing it at the first `apply` without a launch template, which would also write it
   into an imported service whose source has no instance profile. If `IamInstanceProfile` points at
   `<name>` ([resources.md](../resources.md#instance-profile-and-instance-role)) and there
   is no `[[role.policy.ec2]]`, plan to add the default and check that it can be added
   ([docs/conf.md](../../docs/conf.md#role)). Later steps use the detected values as if
   they had been written. On update, write the keys starting with `AwsProfile` from
   `--profile`, then in the order of the tables in docs/conf.md, then add the default
   `[[role.policy.ec2]]`. When the file was newly created, append the usage example as
   comments at the end. If the update stops before that, later runs do not add the
   example, since the file then exists.
3. Detect creating, or tightening to mode 700, the ssh directory, `keys/`, and their
   missing parent directories. If the service command `~/.local/bin/<name>` is absent,
   detect writing it. If anything is there, leave it alone whatever its content; a
   dangling symlink counts as present. When writing it on update, create
   `~/.local/bin` if missing. Whether `temt` is on PATH is not checked: if it is not,
   the user notices when running ssh.
4. Check that the `SubnetId` subnet (of the `NetworkInterfaces` element with
   `DeviceIndex = 0`) exists and has an IPv4 CIDR, and get its VPC and whether it has an
   IPv6 CIDR. If the subnet does not exist, add that for another account the lines that
   point into the account should be removed ([config.md](../config.md#account)). Then
   check that the AMI that `ImageId` points to is usable, and if it is given by name,
   look up its ID ([topics/ami.md](../topics/ami.md#referring-by-name)). If it is
   missing or unusable, add that the `ImageId` line should be removed so it is picked
   again. Fill in the omitted `DeviceName`, resolve security group names in `Groups`,
   and substitute the variables ([topics/launch-template.md](../topics/launch-template.md#extensions-to-awss-form)).
   - If the root volume element has `Ebs.VolumeSize`, compare it with the size of the
     AMI's root volume (the entry of the AMI's `BlockDeviceMappings` whose
     `DeviceName` is `RootDeviceName`). If it is smaller, stop, naming both sizes,
     because AWS accepts the smaller value in the launch template and refuses it only at
     launch. If the AMI has no EBS root volume (its size is unknown), stop and tell the
     user to remove `Ebs.VolumeSize`.
   - If `IamInstanceProfile` does not point at `<name>` (not written included) and
     `[role]` has anything, stop, naming the file of each item
     ([resources.md](../resources.md#instance-profile-and-instance-role)).
   - If there are ingress rules and none of the security groups of the template
     (`Groups` of any `NetworkInterfaces` element, or the top-level `SecurityGroupIds`
     or `SecurityGroups`, after resolving) is temt's `<name>`, stop, naming the file of
     the rules and telling the user to add `${temt:name}` to `Groups`
     ([topics/security-group.md](../topics/security-group.md#when-it-is-created)).
   - For each `[[role.policy.<policy name>]]`, fill in `Effect`, substitute the
     variables to build the content of `temt-<policy name>`, and pass it to IAM Access
     Analyzer `validate-policy`. If there are `ERROR` findings, list all of them
     ([docs/conf.md](../../docs/conf.md#role)).
5. Detect the differences of the IAM role, its inline and managed policies, the
   instance profile, the security group, and the launch template, in this order; the
   update reconciles them in the same order.
   - The role, its policies, and the instance profile only when `IamInstanceProfile`
     points at `<name>` ([resources.md](../resources.md#instance-profile-and-instance-role)).
   - Inline policies deleted are those starting with `temt-` that are not in the
     config file. Managed policies not in `ManagedPolicyArns` are detached.
   - If the instance profile holds another role, remove it and add the role `<name>`.
   - The security group is created only when `Groups` is filled by temt or names it,
     and its ingress rules are reconciled whenever it is among the template's security
     groups ([topics/security-group.md](../topics/security-group.md#when-it-is-created)).
     If it does not exist yet, the launch template difference is detected with its ID
     as `(new)`, and the update puts in the ID of the security group it created.
   - Right after creating the instance profile, wait for IAM to propagate it with the
     boto3 waiter `instance-profile-exists` (a mechanism that polls until the state
     changes). Even so, EC2 may not see it yet right after; `start` then says to wait
     a minute ([instance.md](instance.md#right-after-apply)).
6. Detect adding or updating the `Host <name>` block in the ssh directory's `config`
   ([topics/ssh.md](../topics/ssh.md#the-ssh-directorys-config)). If the block, after this, uses the default login key and
   the key does not exist, detect creating it; it is created before the block is
   written ([topics/login-key.md](../topics/login-key.md#the-default-key)). Detect making the line after the marker in
   `~/.ssh/config` an `Include` that points to that `config`. When replacing the
   `Include`, warn on update about the Hosts in the file the previous `Include` pointed
   to that ssh can no longer see ([topics/ssh.md](../topics/ssh.md)).
   - Before writing either file on update, read it again; if its content changed since
     detection, stop without writing.
   - From that re-read until the write finishes, hold an exclusive lock on
     `.config.lock` in the ssh directory. There is one lock per ssh directory,
     regardless of the service; if someone holds it, wait until it is released.
     Concurrent `apply` runs for different services do not lose each other's edits.
     `apply` runs started concurrently with different `TEMT_SSH_DIR` do not protect
     each other's edits of `~/.ssh/config`.

## `diff`

`service diff --svc <svc>` (short form `diff`)

Writes what the next `apply` would change to stdout, one change per line. It walks the
steps of `apply` in the same order and only detects differences; it writes nothing to
the config file, AWS, or local files. It takes no lock and asks no confirmation. If the
config file does not exist, it exits telling the user to run `apply`. It does not check
whether the launch template exists.

Step 2 writes nothing; `diff` prints the keys it would write and later steps use the
decided values. With `--profile`, the value to be written to `AwsProfile` is printed
too. Where `apply` would stop, `diff` prints the changes detected so far and then exits
with the same error. It prints:

| Step | Kind | Name | Printed |
| --- | --- | --- | --- |
| 2 | `conf` | Key position (`launch-template.ImageId` etc.) | The value to write; if already written, `<current value> -> <new value>` |
| 2 | `conf` | `role.policy.ec2` | Writing the default |
| 3 | `dir` | The ssh directory, `keys/`, and their missing parents | Creating; setting the mode to 700 |
| 3 | `command` | The service command | Writing |
| 5 | `role` | `<name>` | Creating; tags to add |
| 5 | `role-policy` | `temt-<policy name>` | Adding, rewriting, deleting; the content difference |
| 5 | `managed-policy` | The policy ARN | Attaching, detaching |
| 5 | `instance-profile` | `<name>` | Creating; tags to add; the role to put in (swapping out another role if one is there) |
| 5 | `security-group` | `<name>` | Creating; tags to add |
| 5 | `ingress` | `PORT` | Rules to add, rules whose description is fixed, rules to delete |
| 5 | `launch-template` | `<name>-template` | Creating; per-item differences; tags to add; non-default versions to delete |
| 6 | `key` | The default login key | Creating it (`ssh-keygen -t ed25519`) |
| 6 | `ssh-host` | `<name>` | Adding or rewriting the Host block; the content difference |
| 6 | `ssh-include` | `~/.ssh/config` | Adding or replacing the `Include` line (creating `~/.ssh` if missing) |

Line format:

- Each line has a leading mark, the kind, the name, and the difference, aligned with
  spaces. The mark is one of `+` (create, add), `~` (fix), `-` (delete).
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

Terminates the instance and deletes the AWS resources `apply` created and the service
command. The config file, the login key, the Host block in `~/.ssh/temt/config`, and
AMIs are kept, and the next `apply` uses them as they are. There is no
`--async`; it always waits for completion.

1. Take the lock and find instances under the same conditions as
   [`start`](instance.md#start), including those shutting down.
2. List the IDs and states of the instances found and the names of the resources to
   delete, and ask for confirmation. The role is listed with the names of its inline
   and managed policies.
3. If there are instances, do step 4 of [`terminate`](instance.md#terminate) and wait
   until all of them are `terminated`.
4. Delete the launch template, the SSM parameters, the security group, the instance
   profile, and the IAM role, in this order. Report each as deleted or already absent.
   - SSM parameters: delete everything under the `/<name>` hierarchy, found by path,
     not by tag.
   - Security groups: delete every one named `<name>`, in any VPC
     ([topics/security-group.md](../topics/security-group.md#deleting)).
   - The instance profile and the role: only those named `<name>` with the tag
     `temt:service=<svc>`, whether or not the config file points at them
     ([resources.md](../resources.md#instance-profile-and-instance-role)). Untagged
     ones, or ones tagged for another service, are left alone; so are instance profiles of
     other names (the source's, after `ami import`).
   - From the instance profile, remove only the role `<name>`. If it holds another
     role, stop without deleting it: `the instance profile <name> holds another role:
     <role>; remove it by hand and run destroy again`. From the role, detach all
     managed policies (the policies themselves stay; temt did not make them) and
     delete all inline policies, whoever owns them; IAM's `DeleteRole` refuses with
     `DeleteConflict` while either remains.
   - If the role is in another instance profile, stop before deleting the instance
     profile or touching the role.
   - Deleting the instance profile or the role can still fail with `DeleteConflict`:
     right after the role was removed from the instance profile, IAM may not show it yet, and
     something may have been attached after the check. AWS's text does not tell which,
     so the error quotes it and gives both remedies (run destroy again; if it persists,
     or something is attached, remove or detach it by hand).
5. Delete `~/.ssh/temt/known_hosts.d/<name>/` and the service command
   `~/.local/bin/<name>`. The service command is deleted whatever its content. If the
   known_hosts directory is a symlink, stop without following it
   (`<path> is a symlink; remove it by hand and run destroy again`).
6. Report what was kept: the config file, the Host block, and the latest AMI with
   `temt:service=<svc>` (if any). The login key is shared by the services and not
   listed.

If it fails midway, running the same command again continues from where it stopped.

## `service list`

`service list [--svc <svc>] [--format <columns>]`

Lists the local config files as a table, one service per file, in service-name order.
It does not call AWS. For the AWS side, use [`instance list`](instance.md#instance-list)
for instances and [`template show`](template.md) for the launch template. `--profile`
and `--region` are usage errors.

With `--svc`, it reads only that service's config file (over the defaults file) and
prints one row. If the file
does not exist, it tells the user to run `apply`; if it cannot be read, it gives the
reason; either way it exits with code 1.

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
| `IMAGE` | yes | `ImageId` |
| `TYPE` | yes | `InstanceType` |
| `SUBNET` | | `SubnetId` of the `NetworkInterfaces` element with `DeviceIndex = 0` |
| `INGRESS` | | Number of `[[security-group.ingress]]` |

Values are the effective values when the config file is read over the defaults file
(not with `Defaults = false`):
keys with a default show the default when not written. Keys with no value (unwritten
`AwsProfile`; `AwsRegion`, `ImageId`, and `SubnetId` before `apply`) show `-`;
`AwsRegion` is decided by the first `apply` ([config.md](../config.md#region)). `ImageId` is shown as
written and not resolved to an AMI ID. There is no column for the root volume size:
`service list` does not call AWS, so it cannot tell which `BlockDeviceMappings` element
is the root volume when `DeviceName` is written.
