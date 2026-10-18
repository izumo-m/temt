# ami commands

Steps of `bake` and of the commands that list and delete temt's AMIs. AMI names, tags,
selection, and referring by name are in [topics/ami.md](../topics/ami.md).

## `bake`

`ami bake --svc <svc> [--yes]` (short form `bake`)

Stops the instance, turns it into an AMI, points the config file's `ImageId` and the
launch template at the new AMI, and terminates the instance. Nothing inside the instance
is changed. Old AMIs are not deleted, and other services' config files and templates are
not touched. There is no `--async`; it always waits for completion. AMI names and tags
are in [Creating](../topics/ami.md#creating).

1. Take the lock, check that the launch template exists, and find the instance as
   [`start`](instance.md#start) does (excluding `shutting-down`).
2. Branch on the number and state found.
   - None: say there is no instance to bake and exit with an error.
   - Two or more: list them and exit, as `start` does.
   - `pending`, `stopping`: tell the user to wait for it to finish and retry, and exit.
   - `running`, `stopped`: go to 3.
3. Compare the whole content of the default version of the launch template with the
   config file, as `diff` does ([Comparing](../topics/launch-template.md#comparing)).
   `ImageId` is compared after looking up the ID of the AMI it refers to; if there is no
   usable AMI, it counts as differing. If anything differs, or an item of [Filled when
   missing](../topics/launch-template.md#filled-when-missing) is not written yet, tell
   the user to run `apply` first and exit.
   Then, if the root volume element has `Ebs.VolumeSize`, compare it with the size of
   the instance's root volume (the `VolumeId`, looked up with `describe-volumes`, of the
   instance's `BlockDeviceMappings` entry whose `DeviceName` is `RootDeviceName`). If
   the instance's is larger, exit without baking, telling the user to raise
   `Ebs.VolumeSize` to that size or more, or remove it, then `apply` and bake. Baking as
   is would make the AMI larger than `Ebs.VolumeSize`, and AWS would refuse the next
   `start`.
4. Confirm, listing the instance ID and state and showing that it will stop the instance
   if `running`, create an AMI, rewrite `ImageId` in the config file when the AMI becomes
   usable, and terminate the instance.
5. If `running`, stop it with `stop-instances` and wait until `stopped`.
6. Create the AMI with `create-image` and report its ID and name. EBS volumes other
   than the root volume are left out ([Creating](../topics/ami.md#creating)).
7. Wait until the AMI is `available`, with no deadline: depending on the AMI's size it
   can take long, and stopping the wait changes nothing yet. If creation fails
   ([Waiting](../topics/instance.md#waiting)), exit with an error carrying the reason AWS
   returned (`Message` of `StateReason`). An AWS error while waiting (expired
   credentials, ...) also exits with that error. In both cases, report the current state
   before the error: the config file, template, and records are unchanged; the instance
   is left stopped; and, for an AWS error, the AMI is not `available` yet. No guidance on
   what to do next is printed.
8. Re-read the config file, rewrite `ImageId` to the new AMI's name, and report the old
   value and the new name. If the file is missing, do not create it; error.
   For the launch template, create a new version that is the default version's content
   with only `ImageId` replaced, make it the default, and delete the other versions
   ([Versions](../topics/launch-template.md#versions)).
   Then point the deploy records ([Deploy record](app.md#deploy-record)) at the new AMI
   too, so the records' AMI does not disagree with the template. Three kinds of record are
   handled; report as baked only the ones whose `instance` was removed. If records
   cannot be read or written, warn and continue. `options` and `baked_options` that
   cannot be read because of their shape are written back with their values as written.
   - Records whose `instance` is this instance: write `version` to `baked_version`, the
     new AMI's ID to `baked_image`, and `options` to `baked_options`, and remove
     `instance`. If `options` cannot be read, copy its value as written to
     `baked_options` and warn the user to `deploy` again with `--reset`.
   - Records whose `baked_image` is the AMI this instance was launched from: set only
     `baked_image` to the new AMI's ID, because the version baked into the source AMI is
     also in this instance and carries over into the new AMI. Drop `removed` too: a
     `removed` naming another instance is a lost removal.
   - Records whose `removed` is this instance (checked first): delete them, because the
     app is not in the new AMI. Report them as
     `recorded <app IDs> as removed from <AMI ID>`. If one cannot be deleted, warn
     (`cannot delete <parameter>: <reason>`) and continue; after the instance
     terminates, that record shows `lost`. Accepted: it needs a failed delete, and
     `status` shows it.
9. Do step 4 of [`terminate`](instance.md#terminate) and wait until the instance is
   `terminated`.

Nothing is pointed at the AMI until it is usable so that nothing changes when creation
fails or the wait is stopped. The fix is then only "fix the cause and run `bake` again".
Besides transient AWS errors, causes include ones the user must fix, such as the KMS key
of an encrypted EBS being unusable. Retrying without fixing gives the same result, so
AWS's reason is shown. Even when creation fails the instance is left stopped, and `start`
resumes it without using the template, so the service stays usable.

The trade-off: for a short time between the AMI becoming `available` and the template
being pointed at it, no template refers to the new AMI. `ami prune`
takes the service's lock before deleting, so it does not delete the AMI in that window
([`ami prune`](#ami-prune)).

When `bake` stops partway (interrupt, deadline, AWS error):

- Before the AMI exists in 6: the instance may be stopped. If interrupted during 6, an
  AMI with the `temt:service` tag may exist. Run `bake` again.
- After 6 and before the config file is rewritten in 8 (including an interrupt while
  waiting in 7): nothing has changed and the instance is left stopped. When the
  reported AMI later becomes `available` (visible in `ami list`), run `bake` again. This
  makes one more AMI; `ami prune` deletes the extra one. Alternatively, write that AMI's
  name to `ImageId`, run `apply`, and run `terminate`. In that case the records stay
  unbaked, so `app list` shows `lost`; `deploy` again and bake to align them.
- After the config file is rewritten in 8, before the template is pointed: the config
  file points at the new AMI and the records stay unbaked. Run `apply`, then either run
  `bake` again (the records align too) or run `terminate` (`app list` shows `lost`;
  `deploy` again and bake to align them).
- After the template is pointed in 8, before the records are rewritten: the records stay
  unbaked, the records' AMI and the template's AMI disagree, and `app list` shows
  `lost`. The instance is still `stopped`, so run `bake` again: every record aligns
  (`removed` ones are deleted), and `ami prune` deletes the extra AMI. Records already
  rewritten when it stopped (one `put-parameter` each) show `lost` after that; `deploy`
  them again and bake. Or run
  `terminate`; the apps are in the AMI, so `deploy` again and bake to align them.
- 9: run `terminate`.

Wherever the template ends up at the new AMI while the records were not rewritten and
the instance is gone (the alternative after 6; `terminate` after the config file was
rewritten or the template was pointed in 8; and step 8 failing to read the records,
after which `bake` warns `cannot read the deploy records: ...` and terminates), a record
whose `instance` is this instance shows `lost` although the app is in the new AMI
(`deploy` again and bake), and a record whose `removed` is this instance also shows
`lost`. That app is not in the new AMI, so do not `deploy` it again; later bakes never
touch the record (`removed` no longer matches and `baked_image` is not the source AMI),
so delete it: `start`, then `app remove` (the directory is absent and `baked_image` is
not the template's AMI, so the record is deleted).

If `bake` stopped before pointing the template, no template refers to the new AMI, so
`ami prune` deletes it. When the config file's `ImageId` points at that AMI (stopped
after rewriting in 8, or written by hand), run `apply` before `ami prune`. If it stopped
before rewriting the config file and the user will `bake` again, losing it does no harm.

## `ami import`

`ami import --svc <svc> <instance ID> [--yes]`

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

1. If the config file exists, exit doing nothing: an import goes only into a service
   without one. Find the instance by ID. If it is not `stopped`, exit telling the user
   to stop it first; temt neither stops nor restarts it.
2. Read its settings with `get-launch-template-data` and fix them as in [What is
   copied](#what-is-copied). If something cannot be copied, exit.
3. Confirm, showing the instance ID, its `Name` tag, the name of the AMI to make, the
   settings to copy, the source's private IP address (and IPv6 address, if any) with
   the note that it changes at every launch, and the EBS volumes other than the root
   volume, one per line (device name and volume ID from `describe-instances`, size from
   `BlockDeviceMappings[].Ebs.VolumeSize` of `get-launch-template-data`, so no more AWS
   calls), followed by `not put in the AMI; to use one, detach it from the source
   instance and attach it from inside the instance`. Unlike the other differences
   below, these volumes are shown because temt chooses to leave them out of the AMI,
   and the import should not end with their contents missing unnoticed. If the source
   is not registered with SSM (`describe-instance-information`), warn here too.
4. `create-image`: EBS volumes other than the root volume as `NoDevice`, and the root
   volume's `DeleteOnTermination` true ([Creating](../topics/ami.md#creating)).
5. Wait until `available`, with no deadline, as `bake` does. Errors such as an
   unusable KMS key of an encrypted EBS are shown as AWS returned them, as in `bake`.
6. Create the config file, headed `# Written by temt ami import. Edit and run apply to
   update.`. If one appeared in the meantime, stop without overwriting it: it is
   written only when new.
7. Tell the user to run `apply`, which creates the launch template.

The AMI's name and tags are those of `bake` (`temt-<svc>-<YYMMDDHHMM>`; `Name` and
`temt:service=<svc>` on the AMI and its snapshots), so it shows in `ami list` and is a
candidate of `ami prune`. Until `apply`, and after `destroy`, no launch template refers
to it, so `ami prune` deletes it (showing its name before deleting). `apply` then stops
because the AMI is missing; deleting the `ImageId` line as it suggests launches Ubuntu,
which is seen at login, and the source remains, so the config file can be deleted and
the import run again. Nothing is done about this (the requester).

### What is copied

The output of `get-launch-template-data` goes into `[launch-template]`
([topics/launch-template.md](../topics/launch-template.md)); temt's own keys per item
were rejected, since items temt does not know would not be copied. Items that would not
make a template, or not the same instance, are fixed (checked on real AWS where noted
in the rationale):

| Item | Handling | Reason |
| --- | --- | --- |
| `ImageId` | The name of the AMI made | |
| `BlockDeviceMappings` | Removed | The AMI's are used. They point at the source's volumes, and a volume added later has an empty `SnapshotId`, which AWS refuses |
| `Placement.AvailabilityZoneId` | Removed | Cannot be given with `AvailabilityZone` (refused on real AWS) |
| `NetworkInterfaces` `AssociatePublicIpAddress` | The ENI attribute (`describe-network-interface-attribute --attribute associatePublicIpAddress`) | A stopped instance returns `false`, having lost its public IP; the attribute keeps the launch setting |
| `NetworkInterfaces` `PrivateIpAddresses` | Removed; the number of secondary ones goes to `SecondaryPrivateIpAddressCount` (not written when none) | [Addresses](#addresses) |
| `NetworkInterfaces` `PrivateIpAddress`, `NetworkInterfaceId` | Removed | Values of the source |
| `NetworkInterfaces` `Ipv4Prefixes`, `Ipv6Prefixes` | Removed; their numbers go to `Ipv4PrefixCount`, `Ipv6PrefixCount` | The source keeps delegated prefixes too |
| `NetworkInterfaces` `Ipv6Addresses` | Removed; the number goes to `Ipv6AddressCount`, `0` included | Without it, temt fills `1` and a subnet with IPv6 gives an address the source lacked |
| `NetworkInterfaces` `DeleteOnTermination` | `true` on every element | ENIs attached later return `false`; copied as is, one would be made at every launch and left at every termination, piling up unseen. Every ENI of a template is made anew at launch, so deleting it with the instance is closest to the source. A `false` the user writes later is left alone |
| `NetworkInterfaces` `Groups` | Copied as is | [Security groups](#security-groups) |
| The four items that exist only when read | Removed | Cannot be written |
| `InstanceInitiatedShutdownBehavior` | `terminate` | [Shutdown](#shutdown) |
| `TagSpecifications` | Copied as is (the source's `Name` too); temt adds `temt:service`. Keys starting with `aws:` are removed, and the removal is reported; an element left without tags is removed | Volume and ENI tags are not returned, so not copied, and their `Name` becomes `<name>`. `aws:` keys are reserved by AWS (whether they come back is unchecked) |
| `IamInstanceProfile` | Copied as is; not written when absent (`get-launch-template-data` then returns none, checked on real AWS) | [Instance profile](#instance-profile) |
| `UserData` | Copied as is | [User data](#user-data) |
| `InstanceMarketOptions` | Removed | A stopped Spot instance came from a persistent request (only those can stop); copied, AWS would launch the next one at every termination. It launches on demand; not reported. temt does nothing about an `InstanceMarketOptions` the user writes, persistent included (the requester). Rejected: making it a one-time Spot |
| `DisableApiTermination`, `DisableApiStop` | Removed | temt keeps the contents in the AMI and terminating is routine; copied, AWS would refuse `terminate` and `stop`. Not reported; the user can write them |
| Blank values (empty arrays, `GroupName = ""`, ...) | Removed | Count as not written; keeps the file short |

The items of [Filled when missing](../topics/launch-template.md#filled-when-missing)
are always written. When the source's value cannot be read, AWS's default (the value
when launched without the item) is written, not temt's, since temt's would make a
different instance silently. `InstanceInitiatedShutdownBehavior` is the exception.

The config file also gets, at the top level:

- `AwsRegion`, decided as in [Region](../config.md#region): the user points at the
  source's region with `--region` and the like.
- `AwsProfile` when it came from `--profile` or the defaults file.
- `Defaults = false`, so that the service reads no defaults file
  ([config.md](../config.md#defaults-file)).
- `WaitForSsm = false` when the source is not registered with SSM
  ([instance.md](instance.md#start)).

Making a template copied this way was checked on real AWS; launching from it was not
(the requester: the earlier checks on real AWS are enough). `CpuOptions` and the like
are copied as well.

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
extensions added later by editing the config file (the requester). An instance profile that no
longer exists is copied as is: temt creates only `<name>`, so it is not silently made
again, and `start` fails with AWS's error. Rejected: attaching temt's instance profile
(credentials the source lacked); writing `IamInstanceProfile = false`.

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
- Tags in the instance metadata: keys with a space or `/` make the launch or
  `modify-instance-metadata-options` fail.
- A key pair the source names that no longer exists makes `start` fail.
- Writing Elastic IPs in the usage doc.
- Secrets in the user data showing in the local file and in `diff`; reporting the
  change from `stop` to `terminate`; importing when AWS has resources of the same name
  left.
- An AMI deleted by `ami prune` before `apply` or after `destroy` (above).
- The source requirements of temt's login (SSM agent, the instance role's SSM
  permissions, `ec2-instance-connect`, the user of the Host block's `User`) are written
  in the usage doc; only an unregistered SSM agent is warned about.

## `ami list`

`ami list [--svc <svc>] [--format <columns>]`

Lists temt's AMIs, one per row ([List tables](../cli.md#list-tables)). temt's AMIs are
AMIs in the user's own account that have the `temt:service` tag. With `--svc`, the
service's config file decides the environment and only `temt:service=<svc>` AMIs are
listed. Without it, all temt AMIs in the environment decided as in [Commands without
--svc](../config.md#commands-without---svc) are listed.

| Column | Default | Value |
| --- | --- | --- |
| `ID` | yes | AMI ID |
| `NAME` | yes | AMI name |
| `STATE` | yes | State |
| `CREATED` | yes | Creation time |
| `USED` | yes | Launch template versions that refer to the AMI |
| `SERVICE` | | Value of `temt:service` |

For `USED`, every version of every launch template in the account and region is looked
at, not only temt's. A version whose `ImageId` is an SSM parameter (`resolve:ssm:`) is
resolved by AWS (`ResolveAlias`) and counts as referring to the AMI it points at now. If
it cannot be resolved, warn, naming the version and the reason (AWS's error text when
AWS refuses to resolve it). `ami prune` deletes the prune candidates
(names `bake` and `ami import` make, state `available` or `failed`) whose `USED` is `-`. Rows are
ordered by the `temt:service` value and creation time. AMIs in `deregistered`, visible
right after deregistration, and `disabled` AMIs, which `describe-images` does not return
by default, are not shown.

## `ami prune`

`ami prune [--svc <svc>] [--yes]`

Deregisters temt's AMIs that no version of any launch template refers to, and deletes
their snapshots. The environment and the range of AMIs are as in `ami list`: with
`--svc`, the service's config file decides the environment and only
`temt:service=<svc>` AMIs are handled; without it, all temt AMIs in the environment
decided as in [Commands without --svc](../config.md#commands-without---svc). No lock is taken at the start; in 4, the lock of
the AMI's service is taken for each AMI deleted. The value of `TEMT_SSH_DIR`, which
decides where the locks are, is checked before starting so that the command does not
stop after the confirmation.

1. Collect temt's AMIs as `ami list` does and narrow to candidates: names of the form
   `temt-<value of temt:service>-<YYMMDDHHMM>` (names `bake` and `ami import` make), in state
   `available` or `failed`. `pending` is excluded because it is a `bake` in progress.
2. Look at every version of every template as `ami list` does; keep candidates that are
   referred to and delete the rest. Even with `--svc`, all templates of the environment
   are looked at. Being the newest of a service, being the AMI an instance was launched
   from, or being the AMI the local config file's `ImageId` points at are not reasons to
   keep. If some version cannot be resolved to an AMI, list it and exit with an error
   without deleting anything. AMIs are collected before templates so that an AMI that
   becomes `available` and is pointed at by `bake` after the templates were read does
   not become a candidate.
3. If there is nothing to delete, say so and finish. Otherwise list only what will be
   deleted and confirm once. AMIs kept because they are referred to are not listed (use
   `ami list` to check).
4. Handle the AMIs to delete one at a time, in this order. When one is kept, warn with
   the reason and continue with the rest; at the end, list all kept ones and exit with an
   error.
   1. Take the lock of the service named by the AMI's `temt:service`. If another command
      holds it, it cannot be taken, so keep the AMI. If there is no known_hosts
      directory for the service (where the lock file lives), no command for that service
      is running, so proceed without the lock. Do not create the directory, so as not to
      add directories for services that have none (an AMI can outlive its service). If
      the value does not follow the service name rule, temt commands cannot handle that
      service, so also proceed without the lock.
   2. Re-read every version of every template as in step 2. If a version refers to the
      AMI or cannot be resolved to an AMI, keep it, because `bake` may have pointed a
      template at it while the confirmation was waiting.
   3. Deregister with `deregister-image` with `DeleteAssociatedSnapshots`. If AWS
      refuses, keep it. If no answer comes from AWS (connection failure, ...), stop
      there. For snapshots that could not be deleted, warn with the reason AWS returned.
   4. Release the lock.

Deregistering does not affect running or `stopped` instances. Run after `destroy`, the
service's template is gone, so the service's AMIs are deleted unless another template
refers to them. The config file's `ImageId` keeps pointing at the deleted AMI, so the
next `apply` stops and tells the user to remove the line; once removed, it starts over
from Ubuntu 24.04.

A concurrent `bake`'s new AMI is `pending` while `bake` waits, so it is not a candidate.
For the short time between it becoming `available` and `bake` pointing a template at
it, no template refers to it, but `bake` holds the lock throughout, so `ami prune` does not
delete it: either the lock cannot be taken and the AMI is
kept, or the lock is taken after `bake` pointed the template and released the lock, and
the re-read template refers to it, so it is kept (step 4).

## `ami delete`

`ami delete [--svc <svc>] <AMI ID or name> [--force] [--yes]`

Deregisters one temt AMI given by ID or name and deletes its snapshots. The environment
is as in `ami prune`: decided by the service's config file with `--svc`, otherwise as in [Commands without
--svc](../config.md#commands-without---svc). No per-service lock is taken.

1. Find the AMI. Telling ID from name and finding by name are as for the `ImageId` value
   ([Referring by name](../topics/ami.md#referring-by-name)), except that IDs too are
   searched only among the user's own account's AMIs (`Owners=self`). If not found,
   error. As in `ami list`, `disabled` AMIs are not visible and are not handled. With
   `--svc`, an AMI whose `temt:service` is not `<svc>` (including untagged ones) is an
   error as not found. Without `--svc`, an AMI without the `temt:service` tag is an
   error. `--force` does not change these. Whether the name is of the form `bake` makes
   does not matter.
2. Look at every version of every template as `ami list` does. If any of the following
   holds, without `--force` exit with an error listing the reasons and delete nothing;
   with `--force`, warn with the reasons and continue.
   - A version refers to the AMI.
   - A version cannot be resolved to an AMI.
   - The state is `pending`.

   As in `ami prune`, being the AMI an instance was launched from or the AMI the local
   config file's `ImageId` points at is not a reason.
3. Confirm, listing the AMI to delete.
4. Deregister with `deregister-image` with `DeleteAssociatedSnapshots`. If AWS refuses,
   error. For snapshots that could not be deleted, warn with the reason AWS returned.

Deleting with `--force` an AMI a template refers to makes that version unable to launch.
If the default version refers to it, the next `start` fails. With or without `--force`,
if the config file's `ImageId` points at the deleted AMI, the next `apply` stops and
tells the user to remove the line.
