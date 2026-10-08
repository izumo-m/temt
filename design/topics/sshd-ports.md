# sshd listening ports

The config file's `SshdPorts` is applied to both the listening ports of the instance's
sshd and the `Port` of the ssh Host block. The instance side is reconciled by the Systems
Manager State Manager association `<name>-sshd`. An association is a registration with
AWS saying "on the instances selected by tag, run this SSM document with these
parameters"; the SSM agent on the instance picks it up and runs it. Step 6 of
[`apply`](../commands/service.md#apply) reconciles it; step 4 of
[`destroy`](../commands/service.md#destroy) deletes it. It does not go through ssh, so
it arrives even when sshd listens on a port that cannot be reached.

`SshdPorts` is an array of one or more ports, default `[22]`
([conf.md](../../docs/conf.md#最上位)). The Host block's `Port` gets the first port
([topics/ssh.md](ssh.md#the-ssh-directorys-config)); the others are reached with
`ssh -p <port> <name>`.

## Association

| Item | Value |
| --- | --- |
| Name (`AssociationName`) | `<name>-sshd` |
| SSM document | `AWS-RunShellScript`, provided by AWS; default version |
| Targets | Instances whose tag `Name` is `<name>` (`tag:Name`; the same mark temt uses to find instances) |
| Parameters | `commands`: the [script](#script), one line per element |
| Tags | `temt:service=<svc>` |
| Schedule | None. `ApplyOnlyAtCronInterval` left at its default (false) |

With no schedule, State Manager runs it when the association is created, when it is
updated, when a new instance matching the targets launches, and when requested with
`start-associations-once`. An update made while the instance is stopped runs when the
instance is started again.

`apply` reconciles it as follows.

1. Find the association named `<name>-sshd` with `list-associations` (filter
   `AssociationName`, then exact name match). If none, detect it as to be created.
   Association names are not unique in AWS, but temt creates only one per service, so
   there is normally one. If there are two or more (e.g. made by hand), reconcile the
   first in ID order and leave the others alone. `proxy` and `status` look at the same
   one.
2. If found, read it with `describe-association` and compare only the document name, the
   targets, and `commands` (in order, as AWS keeps the order). The other items of the
   table (document version, schedule, `ApplyOnlyAtCronInterval`) are not compared. If
   they differ, detect it as to be updated. If the `temt:service` tag is missing or has a
   different value (`list-tags-for-resource`), detect it as to be added.
3. In the update phase: if missing, create it as in the table and report
   `association <name>-sshd: created`. If it differs, call `update-association` with
   every item temt decides, creating a new association version, and report `updated`.
   `update-association` resets items not passed to their defaults, so the uncompared items
   also return to the table's values at this point. Tags are added or corrected
   (`add-tags-to-resource`); fixing only the tag is also reported as `updated`.
4. When updated, and when created with ports other than `[22]`, one line is added saying
   it takes effect on running instances shortly and that `bake` keeps it in the AMI:
   `sshd ports <ports> take effect on a running instance shortly; run 'temt bake --svc
   <svc>' to keep them in the AMI`. Not added when only the tag was fixed.
5. Only when created or when its content was updated, request a run with
   `start-associations-once`. Creation alone may not run it on a running instance. If
   the request is refused, warn (`association <name>-sshd: cannot ask to run it now:
   <error>; it runs when an instance starts next time`) and continue; it runs at the
   next launch. If the content is the same, do nothing; on a launching instance State
   Manager runs it by itself. Accepted gap: if `apply` stops between creating the
   association and requesting the run while an instance is running, a rerun finds it
   unchanged and requests nothing, so that instance stays `(not run)` in `status`
   until it is terminated and started again (or `SshdPorts` is changed). It needs an
   interrupt or a connection error (a `BotoCoreError`, which is not caught as a refusal)
   in that window right at the first creation.

`destroy` deletes every association named `<name>-sshd` and reports each as
`association <name>-sshd (<association ID>): deleted` (or `absent` if it vanished in
between, `AssociationDoesNotExist`); with none found, `association <name>-sshd: absent`.

temt does not show the script's output. It is in the run history of `<name>-sshd` in
Systems Manager State Manager (the Run Command output).

Permissions:

- Credentials running `apply` need the SSM association permissions `ListAssociations`,
  `DescribeAssociation`, `CreateAssociation`, `UpdateAssociation`,
  `ListTagsForResource`, `AddTagsToResource`, `StartAssociationsOnce`.
- `destroy` needs `ListAssociations` and `DeleteAssociation`; `proxy` and `status` need
  `ListAssociations` and `DescribeInstanceAssociationsStatus`.
- The instance role, through the `temt-ec2` default ([conf.md](../../docs/conf.md#role)),
  has `ssm:ListInstanceAssociations`, `ssm:UpdateInstanceAssociationStatus`, and
  `ssm:GetDocument` for running associations, and `ssm:PutComplianceItems` for reporting
  the result.

## Script

temt builds it from `SshdPorts` and `<svc>` and passes it as the association's
`commands` (`sshd.commands` in `src/temt/sshd.py`, whose comments cite the step numbers
below). It runs as root. The SSM agent runs it with `sh` (dash on Ubuntu), so it is
POSIX sh. Lines the script reports start with `temt-sshd:`, and port lists are joined
with `,` (`22 -> 2222,443`). It uses these locations:

| Location | Content |
| --- | --- |
| `/etc/ssh/sshd_config.d/temt-sshd-ports.conf` | `Port <port>` lines, in `SshdPorts` order |
| `/run/temt-sshd/ami-ports` | The ports in effect before the first switch since boot; taken as the AMI's ports |
| `/run/temt-sshd/backup` | Copy of `temt-sshd-ports.conf` from before the switch |
| `/run/motd.d/50-temt-sshd` | The marker shown in the login message ([Marker and login message](#marker-and-login-message)) |

Below, "effective ports" are the `port` values printed by `sshd -T`, and "listening
ports" are the listeners seen by `ss -Hltnp "sport = :<port>"` whose owner is `sshd` or
`systemd` (which holds the listener under socket activation). Listeners of other
processes do not count. Port lists are compared ignoring order.

1. Create `/run/sshd` if missing. It does not exist until sshd has started once, and
   `sshd -t` and `sshd -T` fail without it.
2. If the effective ports equal `SshdPorts` and all of them are listening, report
   `unchanged <ports>` and exit 0, without touching sshd. If `sshd -T` fails, exit 1
   without changing anything (`sshd -T failed; left the configuration as it is`).
3. Keep the effective ports. If `ami-ports` does not exist, write them there.
4. Delete any previous `backup`. If `temt-sshd-ports.conf` exists, copy it to `backup`,
   then write the `Port` lines to a temporary file and move it into place. Create
   `sshd_config.d` if missing.
5. Check with `sshd -t`. If rejected, restore from `backup` (or delete the file if there
   is no backup), report it, and exit 1.
6. Check that the effective ports equal `SshdPorts`. If not, another file also has
   `Port` lines and sshd would listen on their union; restore as in 5, report with the
   effective ports, and exit 1. If `sshd -T` fails, also restore, report it, and exit 1.
7. Make sshd reload. First `systemctl daemon-reload`; the `ssh.socket` listeners are
   generated from `sshd_config` at this point. Then, if `ssh.socket` is enabled
   (`systemctl is-enabled`; socket activation on Ubuntu 24.04), restart `ssh.socket`,
   otherwise restart `ssh.service`. It does not branch on whether they are running, so an
   `ssh.socket` that stopped after a failed restart is brought back if enabled. Existing
   connections are not cut.
8. Check that all of `SshdPorts` are now listening. If not after 20 checks 0.5 seconds
   apart, restore as in 5, redo 7, and exit 1.
9. If `SshdPorts` equals `ami-ports`, delete the marker; otherwise write it.
10. Report `changed <previous ports> -> <current ports>`.

Under socket activation on Ubuntu 24.04, `daemon-reload` maps the `Port` lines of
`sshd_config.d` onto the `ssh.socket` listeners. `Port` lines in `sshd_config.d` add up,
and Ubuntu's `sshd_config` has no `Port`, so only this file's `Port` lines take effect.
Apps do not write `Port` to `sshd_config.d`
([apps.md](../../docs/apps.md#install-と-uninstall)).

## Marker and login message

The marker means `SshdPorts` differs from `ami-ports`, i.e. sshd listens on ports other
than the AMI's. The file's content is the message itself; Ubuntu's `pam_motd` prints the
contents of `/run/motd.d/` at every ssh login that opens a terminal without a command.
It appears under the same conditions as idle shutdown's
[login message](../apps/idle-shutdown.md#login-message).

```
temt: sshd ports were changed from the AMI's (22 -> 2222); run 'temt bake --svc <svc>' to keep them in the AMI
```

- It is written when an instance launched from an AMI baked with ports other than the
  current `SshdPorts` (plain Ubuntu included) switches after launch, and when `apply`
  changes the ports on a running instance. It then appears at every login until baked.
- `/run` is in memory and `bake` stops the instance before baking, so neither the marker
  nor `ami-ports` goes into the AMI. On an instance launched from an AMI baked with the
  current `SshdPorts`, the script ends at step 2, so no marker is written.
- When the same instance is switched back to the AMI's ports, step 9 deletes the marker.
- A reboot or a stop and start (`reboot`, a reboot inside the instance, `stop` then
  `start`) clears `/run`, so the marker and `ami-ports` disappear. The settings in
  `/etc` remain, so from then on the ports differ from the AMI without a marker.
- It does not appear on connections that open no terminal, such as `ssh -T` (the ssh of
  `deploy` and `app remove`).
- The marker is placed only in `/run`, never in `/etc/motd` or `/etc/update-motd.d/`.

## Right after launch

An instance starts sshd with the settings in its AMI, then the SSM agent starts, picks up
the association, and runs it. Launched from plain Ubuntu or from an AMI baked with other
ports, it takes about 10 seconds from the SSM agent becoming `Online` until the run
finishes, and meanwhile sshd listens on the AMI's ports. Launched from an AMI baked with
the `SshdPorts` ports, sshd listens on those ports from the start, but the association
still runs and ends with `unchanged`.

`start` waits only until the SSM agent is `Online`; it does not wait for the association
run. `proxy` waits for it, and only right after launch (the instance was `pending`, or
within 5 minutes of launch; [commands/ssh.md](../commands/ssh.md#connecting)).

- **`ssm`.** After the SSM agent is `Online`, read the current version of `<name>-sshd`
  once with `list-associations`. If there is no association, warn (`no association
  <name>-sshd; sshd listens on the ports in the AMI; run 'temt apply --svc <svc>'`) and
  connect anyway. Otherwise read its state on that instance with
  `describe-instance-associations-status` and wait until the current version's state has
  finished. A previous version's state, or no entry at all (including
  `InvalidInstanceId`, when SSM does not know the instance yet), counts as not run yet.
  If the finished state is not `Success` (`Failed` etc.), warn that the script has
  restored the previous ports and to see `status` (`<name>-sshd ended with <state> on
  <instance ID>; sshd stays on its previous ports; see 'temt status --svc <svc>'`), and
  connect anyway. Past the deadline ([topics/instance.md](instance.md#waiting), 5
  minutes), exit with `see 'temt status --svc <svc>'` appended. Once some time has
  passed since launch, the association is not looked at, so a connection right after
  `apply` changes the ports may go to the not-yet-switched sshd and fail.

  Of the states, `Queued`, `Pending`, and `InProgress` are waiting to run; every other
  state counts as finished.
- **`direct`.** The association is not looked at; `proxy` waits until the port accepts
  a TCP connection. Before the switch it does not, so that is enough. If the association
  failed, the connection never succeeds, the deadline passes, and `proxy` exits with only
  the hint that sshd is not accepting.

Because `proxy` connects even on `Failed`, the user can log in with
`ssh -p <previous port> <name>` and fix it. The port argument reaches `proxy` through
`%p` regardless of `Port`.

## Status display

`status` shows the state of `<name>-sshd` on each instance in one line (label
`sshd-ports`):

| State | Shown |
| --- | --- |
| Current version `Success` | `applied` |
| `Queued`, `Pending`, `InProgress`, or only a previous version's state | `pending` |
| Not in the list | `(not run)` |
| Any other state | AWS's state in lower case (`failed` etc.) |
| No association at all | `none (no association <name>-sshd; run 'temt apply --svc <svc>')` |
