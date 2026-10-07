# service commands

## `apply`

`service apply --svc <svc> [--yes]` (short form `apply`)

Creates the service's config file, key pair, AWS resources, and ssh settings if they
are missing. Existing ones are brought in line with the config file, except the key
pair, which is never changed. Running it again gives the same result. Each step is an
independent process that takes the config file as the source of truth and reconciles
one target.

`apply` runs in four phases: detect, show, confirm, update.

- Detect: walk steps 1-7 below and detect the difference for each target. Detection is
  the same code as `diff`. Nothing is written to the config file, AWS, or local files
  in this phase. If a step stops, temt exits having written nothing.
- Show: with no differences, report so (`<name>: no differences`) and finish.
  Otherwise print the differences to stdout in the same format as [`diff`](#diff).
- Confirm: ask `[y/N]` (`apply these changes?`; see
  [cli.md](../cli.md#common-behavior)).
- Update: apply the detected differences in step order. Report what was created,
  updated, or deleted; do not report what was left unchanged. Changes in AWS between
  detection and update are not handled.

If it fails midway, running the same command again continues from where it stopped.
If it stops during the update phase, whatever was already written stays.

1. Check the service name against [naming.md](../naming.md#service-names). If the
   `Host <name>` block in the ssh directory's `config` was written by `apply` from
   another conf directory, and the service's config file still exists there (unless it
   is the same file as the one in the current location), exit naming that location
   ([topics/ssh.md](../topics/ssh.md#services-across-conf-directories)).
2. Read the config file; if it does not exist, start from the defaults alone. Detect keys
   that are missing or have a different value, deciding each value as it goes.
   Missing `ImageId` and `SubnetId` are chosen from AWS here. If there is no
   `[[role.policy.ec2]]`, plan to add the default and check that it can be added
   ([docs/conf.md](../../docs/conf.md#role)). Later steps use the detected values as if
   they had been written. On update, write the keys starting with `AwsProfile` from
   `--profile`, then in the order of the table in docs/conf.md, then add the default
   `[[role.policy.ec2]]`. When the file was newly created, append the usage example as
   comments at the end. If the update stops before that, later runs do not add the
   example, since the file then exists.
3. Detect creating, or tightening to mode 700, the ssh directory, `keys/`, and their
   missing parent directories. If the service command `~/.local/bin/<name>` is absent,
   detect writing it. If anything is there, leave it alone whatever its content; a
   dangling symlink counts as present. When writing it on update, create
   `~/.local/bin` if missing. Whether `temt` is on PATH is not checked: if it is not,
   the user notices when running ssh.
4. Check that the `SubnetId` subnet exists and has an IPv4 CIDR, and get its VPC and
   whether it has an IPv6 CIDR. If the subnet does not exist, add that for another
   account the `SubnetId` and `ImageId` lines should be removed. Then check that the
   AMI that `ImageId` points to is usable, and if it is given by name, look up its ID
   ([topics/ami.md](../topics/ami.md#referring-by-name)). If it is missing or unusable,
   add that the `ImageId` line should be removed so it is picked again.
   - If `VolumeSize` is set, compare it with the size of the AMI's root volume (the
     entry of `BlockDeviceMappings` whose `DeviceName` is `RootDeviceName`). If
     `VolumeSize` is smaller, stop, naming both sizes, because AWS accepts the smaller
     value in the launch template and refuses it only at launch. If the AMI has no EBS
     root volume (its size is unknown), stop and tell the user to remove the
     `VolumeSize` line.
   - For each `[[role.policy.<policy name>]]`, fill in `Effect`, substitute the
     variables to build the content of `temt-<policy name>`, and pass it to IAM Access
     Analyzer `validate-policy`. If there are `ERROR` findings, list all of them
     ([docs/conf.md](../../docs/conf.md#role)).
5. Compare the key pair with the local key as in [topics/keypair.md](../topics/keypair.md).
   If the keys do not match, exit, treating it as an error in the config file.
6. Detect the differences of the IAM role, its inline policies `temt-<policy name>`,
   the instance profile, the security group, the launch template, and the association,
   in this order; the update reconciles them in the same order.
   - Inline policies deleted are those starting with `temt-` that are not in the
     config file.
   - If the instance profile holds another role, remove it and add the role `<name>`.
   - If the security group does not exist yet, the launch template difference is
     detected with its ID as `(new)`, and the update puts in the ID of the security
     group it created.
   - The association is found by name. Ask for it to run only when it was created or
     its content was updated ([topics/sshd-ports.md](../topics/sshd-ports.md)).
   - Right after creating the instance profile, wait for IAM to propagate it with the
     boto3 waiter `instance-profile-exists` (a mechanism that polls until the state
     changes). Even so, a `start` right after may fail because the instance profile is
     not found; running `start` again is enough.
7. Detect adding or updating the `Host <name>` block in the ssh directory's `config`
   to match the config file. Detect making the line after the marker in
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
| 5 | `key` | `IdentityFile` | Creating the key (also creates missing parent directories) |
| 5 | `keypair` | `<KeyName>` | Registering |
| 6 | `role` | `<name>` | Creating; tags to add |
| 6 | `role-policy` | `temt-<policy name>` | Adding, rewriting, deleting; the content difference |
| 6 | `instance-profile` | `<name>` | Creating; tags to add; the role to put in (swapping out another role if one is there) |
| 6 | `security-group` | `<name>` | Creating; tags to add |
| 6 | `ingress` | `PORT` | Rules to add, rules whose description is fixed, rules to delete |
| 6 | `launch-template` | `<name>-template` | Creating; per-item differences; tags to add; non-default versions to delete |
| 6 | `association` | `<name>-sshd` | Creating; differences in document, targets, `SshdPorts`, script; tags to add |
| 7 | `ssh-host` | `<name>` | Adding or rewriting the Host block; the content difference |
| 7 | `ssh-include` | `~/.ssh/config` | Adding or replacing the `Include` line (creating `~/.ssh` if missing) |

Line format:

- Each line has a leading mark, the kind, the name, and the difference, aligned with
  spaces. The mark is one of `+` (create, add), `~` (fix), `-` (delete).
- A changing value is written `<current value> -> <new value>`. Tags to add are written
  `tags: <key>=<value>`.
- Ingress rules have `PORT` as the name and the source and description as the
  difference, in the same format as [`sg diff`](sg.md#sg-diff).
- The launch template prints one line per differing temt-managed item. Among them, the
  ID of a security group not created yet is written `(new)`. `BlockDeviceMappings` is
  shown by the config key name `VolumeSize`, size only, and `-` when absent
  (`VolumeSize: - -> 30`). When expanding per item shows no difference, it prints
  `new version`. When no new version is created but non-default versions remain, it
  prints deleting them as `delete other versions: <number>, ...`.
- The content of inline policies (JSON with sorted keys, indented), the association
  script, and the Host block are shown as differences in unified diff form (with
  context lines, `-` on removed lines, `+` on added lines), indented on the following
  lines.
- If replacing the `Include` makes some Hosts invisible to ssh, they are listed on the
  following lines.

Lines are ordered by the steps of `apply`; ingress rules are in the same order as
`sg diff`. With no differences, nothing is written to stdout and that is reported on
stderr. The exit code is 0 whether or not there are differences. The request to run the
association is not printed, since it is not a change to the definition. Example:

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
~   association       temt-web-sshd       SshdPorts: 22 -> 22,2222
    @@ -1,4 +1,4 @@
     set -u
    -want="22"
    +want="22 2222"
     svc="web"
     conf=/etc/ssh/sshd_config.d/temt-sshd-ports.conf
```

## `destroy`

`service destroy --svc <svc> [--yes]` (short form `destroy`)

Terminates the instance and deletes the AWS resources `apply` created and the service
command. The config file, the key, the Host block in `~/.ssh/temt/config`, the key
pair, and AMIs are kept, and the next `apply` uses them as they are. There is no
`--async`; it always waits for completion.

1. Take the lock and find instances under the same conditions as
   [`start`](instance.md#start), including those shutting down.
2. List the IDs and states of the instances found and the names of the resources to
   delete (including the association), and ask for confirmation. The role is listed
   with the names of its inline policies.
3. If there are instances, do step 4 of [`terminate`](instance.md#terminate) and wait
   until all of them are `terminated`.
4. Delete the launch template, the association, the SSM parameters, the security
   group, the instance profile, and the IAM role, in this order. Report each as
   deleted or already absent.
   - Associations: delete every one named `<name>-sshd`.
   - SSM parameters: delete everything under the `/<name>` hierarchy, found by path,
     not by tag.
   - Security groups: delete every one named `<name>`, in any VPC
     ([topics/security-group.md](../topics/security-group.md#deleting)).
   - From the instance profile, remove only the role `<name>`. If it holds another
     role, stop without deleting it: `the instance profile <name> holds another role:
     <role>; remove it by hand and run destroy again`. From the role, delete all inline
     policies, whoever owns them.
   - If the role has a managed policy or another instance profile attached, stop
     without deleting even the inline policies, because deleting them first would lose
     only the hand-attached policies while the role remains.
5. Delete `~/.ssh/temt/known_hosts.d/<name>/` and the service command
   `~/.local/bin/<name>`. The service command is deleted whatever its content. If the
   known_hosts directory is a symlink, stop without following it
   (`<path> is a symlink; remove it by hand and run destroy again`).
6. Report what was kept: the key pair `<KeyName>` and the local key, the config file,
   the Host block, and the latest AMI with `temt:service=<svc>` (if any).

If it fails midway, running the same command again continues from where it stopped.

## `service list`

`service list [--svc <svc>] [--format <columns>]`

Lists the local config files as a table, one service per file, in service-name order.
It does not call AWS. For the AWS side, use [`instance list`](instance.md#instance-list)
for instances and [`template show`](template.md) for the launch template. `--profile`
and `--region` are usage errors.

With `--svc`, it reads only that service's config file and prints one row. If the file
does not exist, it tells the user to run `apply`; if it cannot be read, it gives the
reason; either way it exits with code 1.

Without `--svc`, it reads `*.toml` in the conf directory. Files not ending in `.toml`
and files starting with `.` (such as the editor lock file `.#<svc>.toml`) are skipped
silently. If the conf directory does not exist, only the header is printed. Files that
cannot be read, and files whose name minus `.toml` does not satisfy the service-name
rules, are warned about and skipped; the rest are listed, then temt reports the number
skipped and exits with code 1.

| Column | Default | Value |
| --- | --- | --- |
| `SERVICE` | yes | File name minus `.toml` |
| `PROFILE` | yes | `AwsProfile` |
| `REGION` | yes | `AwsRegion` |
| `IMAGE` | yes | `ImageId` |
| `TYPE` | yes | `InstanceType` |
| `KEYNAME` | | `KeyName` |
| `IDENTITYFILE` | | `IdentityFile` |
| `SUBNET` | | `SubnetId` |
| `INGRESS` | | Number of `[[security-group.ingress]]` |
| `SSHDPORTS` | | `SshdPorts` joined with `,` |
| `VOLUMESIZE` | | `VolumeSize` |

Values are the effective values when the config file is read: keys with a default
show the default when not written. Keys with no value (unwritten `AwsProfile` and
`VolumeSize`; `ImageId` and `SubnetId` before `apply`) show `-`. `ImageId` is shown as
written and not resolved to an AMI ID.
