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
3. Compare the default version of the launch template with the config file on the five
   keys that come from the config file. `ImageId` is compared after looking up the ID
   of the AMI it refers to; if there is no usable AMI, it counts as differing.
   `VolumeSize` is compared as: if not written, the template has no
   `BlockDeviceMappings`; if written, the size is equal. If any key differs, or `ImageId` or
   `SubnetId` is not written (an unwritten `InstanceType` or `KeyName` is compared by its
   default), tell the user to run `apply` first and exit.
   Then, if `VolumeSize` is written, compare it with the size of the instance's root
   volume (the `VolumeId`, looked up with `describe-volumes`, of the
   `BlockDeviceMappings` entry whose `DeviceName` is `RootDeviceName`). If the instance's
   is larger, exit without baking, telling the user to raise `VolumeSize` to that size or
   more, or remove the line, then `apply` and bake. Baking as is would make the AMI
   larger than `VolumeSize`, and AWS would refuse the next `start`.
4. Confirm, listing the instance ID and state and showing that it will stop the instance
   if `running`, create an AMI, rewrite `ImageId` in the config file when the AMI becomes
   usable, and terminate the instance.
5. If `running`, stop it with `stop-instances` and wait until `stopped`.
6. Create the AMI with `create-image` and report its ID and name.
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
   too, so the records' AMI does not disagree with the template. Two kinds of record are
   rewritten; report as baked only the ones whose `instance` was removed. If records
   cannot be read or written, warn and continue. `options` and `baked_options` that
   cannot be read because of their shape are written back with their values as written.
   - Records whose `instance` is this instance: write `version` to `baked_version`, the
     new AMI's ID to `baked_image`, and `options` to `baked_options`, and remove
     `instance`. If `options` cannot be read, copy its value as written to
     `baked_options` and warn the user to `deploy` again with `--reset`.
   - Records whose `baked_image` is the AMI this instance was launched from: set only
     `baked_image` to the new AMI's ID, because the version baked into the source AMI is
     also in this instance and carries over into the new AMI.
9. Do step 4 of [`terminate`](instance.md#terminate) and wait until the instance is
   `terminated`.

Nothing is pointed at the AMI until it is usable so that nothing changes when creation
fails or the wait is stopped. The fix is then only "fix the cause and run `bake` again".
Besides transient AWS errors, causes include ones the user must fix, such as the KMS key
of an encrypted EBS being unusable. Retrying without fixing gives the same result, so
AWS's reason is shown. Even when creation fails the instance is left stopped, and `start`
resumes it without using the template, so the service stays usable.

The trade-off: for a short time between the AMI becoming `available` and the template
being pointed at it, no template refers to the new AMI. `ami prune` on the same machine
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
  `lost`. The apps are in the AMI, so `deploy` again and bake to align them. Run
  `terminate`.
- 9: run `terminate`.

If `bake` stopped before pointing the template, no template refers to the new AMI, so
`ami prune` deletes it. When the config file's `ImageId` points at that AMI (stopped
after rewriting in 8, or written by hand), run `apply` before `ami prune`. If it stopped
before rewriting the config file and the user will `bake` again, losing it does no harm.

## `ami list`

`ami list [--svc <svc>] [--format <columns>]`

Lists temt's AMIs, one per row ([List tables](../cli.md#list-tables)). temt's AMIs are
AMIs in the user's own account that have the `temt:service` tag. With `--svc`, the
service's config file decides the environment and only `temt:service=<svc>` AMIs are
listed. Without it, all temt AMIs in the environment decided by `--profile` and
`--region` are listed ([Commands without --svc](../config.md#commands-without---svc)).

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
it cannot be resolved, warn, naming the version. `ami prune` deletes the prune candidates
(names `bake` makes, state `available` or `failed`) whose `USED` is `-`. Rows are
ordered by the `temt:service` value and creation time. AMIs in `deregistered`, visible
right after deregistration, and `disabled` AMIs, which `describe-images` does not return
by default, are not shown.

## `ami prune`

`ami prune [--svc <svc>] [--yes]`

Deregisters temt's AMIs that no version of any launch template refers to, and deletes
their snapshots. The environment and the range of AMIs are as in `ami list`: with
`--svc`, the service's config file decides the environment and only
`temt:service=<svc>` AMIs are handled; without it, all temt AMIs in the environment
decided by `--profile` and `--region`. No lock is taken at the start; in 4, the lock of
the AMI's service is taken for each AMI deleted. The value of `TEMT_SSH_DIR`, which
decides where the locks are, is checked before starting so that the command does not
stop after the confirmation.

1. Collect temt's AMIs as `ami list` does and narrow to candidates: names of the form
   `temt-<value of temt:service>-<YYMMDDHHMM>` (names `bake` makes), in state
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
      holds it, it cannot be taken, so keep the AMI. If this machine has no known_hosts
      directory for the service (where the lock file lives), no command for that service
      is running on this machine, so proceed without the lock. Do not create the
      directory, so as not to add directories for services baked on other machines. If
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
it, no template refers to it, but `bake` holds the lock throughout, so `ami prune` run
from the same machine does not delete it: either the lock cannot be taken and the AMI is
kept, or the lock is taken after `bake` pointed the template and released the lock, and
the re-read template refers to it, so it is kept (step 4).

## `ami delete`

`ami delete [--svc <svc>] <AMI ID or name> [--force] [--yes]`

Deregisters one temt AMI given by ID or name and deletes its snapshots. The environment
is as in `ami prune`: decided by the service's config file with `--svc`, otherwise by
`--profile` and `--region`. No per-service lock is taken.

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
