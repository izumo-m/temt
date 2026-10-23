# instance commands

Steps of the commands that act on the service's instance. Mechanisms shared across
commands (finding, intermediate states, terminating, waiting and deadlines, locks) are in
[topics/instance.md](../topics/instance.md).

## `start`

`instance start --svc <svc> [--async]` (short form `start`)

1. Take the lock and check that the launch template exists. After the lock, find the
   service's instance ([Finding](../topics/instance.md#finding)). Instances in
   `shutting-down` are treated as absent
   ([Intermediate states](../topics/instance.md#intermediate-states)).
2. Branch on the number and state found.
   - Two or more: list them, tell the user to terminate the extra ones by instance ID with
     `terminate`, and exit.
   - `running`, `pending`: do not start one. Go to 3.
   - `stopped`, `stopping`: request a resume with `start-instances` and report it. Even
     in `stopping`, do not wait for the stop to finish. The launch template is not used,
     so launch template changes made by `apply` after the stop (AMI, instance type, ...)
     do not take effect.
   - None: launch with `run-instances` from the default version of `<name>-template`.
     If EC2 refuses because it does not see the instance profile `<name>`, exit saying
     so, without a hint on what to do next. Do not wait and retry ([Right after
     apply](#right-after-apply)). A refusal about another instance profile is shown as
     AWS gives it.
3. With `--async`, finish here. Otherwise, if a resume was requested, wait until the old
   state is no longer visible ([Until the previous state
   disappears](../topics/instance.md#until-the-previous-state-disappears)). Then wait
   until `running`; also keep waiting while the instance is not yet visible right after
   the launch. With `WaitForSsm = false` at the top level of the config file, report
   that it is running and finish here. Otherwise wait until the SSM agent is reported
   `Online`, report that the instance is ready for login, and finish. Only an `Online`
   whose last report is later than the instance's launch time counts (a resume sets the
   launch time to the resume time), because right after a stop and resume, the `Online`
   report from before the stop may remain. Past the deadline, exit saying that the SSM
   agent did not become `Online` by the deadline, and nothing more: no hint on what to
   do next, for a launched or a resumed instance alike ([Right after
   apply](#right-after-apply)).

`WaitForSsm = false` is for an instance whose SSM agent does not register: `service
import` writes it when the source is not registered with SSM
([service.md](service.md#service-import)); unwritten or `true` waits. Nothing else
changes: `start` does not warn about a missing `IamInstanceProfile` (`apply` and `diff`
do), an instance profile temt does not manage changes nothing in `start`, and the other
commands that use SSM (`proxy` on `ssm`, `reboot`, `set-port`) do not refuse in
advance; they run and end with an error or a deadline, which says what was run and
what happened (the requester). Rejected: skipping the wait when `IamInstanceProfile =
false` (that value does not exist), and refusing `set-port` and the like before
launching.

### Right after apply

Observed on real AWS when `start` launches right after `apply` created the role and
instance profile:

1. EC2 does not know the new instance profile name yet: `run-instances` is refused for
   a few seconds. `apply` does not warn about it when it creates the template or a
   version in that time ([topics/launch-template.md](../topics/launch-template.md#versions)).
2. After the role and instance profile are deleted and an `apply` recreates them under
   the same name (`destroy` in the last region that used them, or an `apply` after
   `[role]` was removed, followed by an `apply` with `[role]` again), EC2 may still
   resolve the name to the deleted instance profile. The launch succeeds, but the
   instance points at an instance profile IAM no longer has, so it can never get
   credentials; waiting or rebooting does not help. AWS documents this case and the fix
   (attach a valid instance profile). Observed: 6 minutes after the recreation, EC2
   resolved the name to the new one.
3. With the right instance profile, the SSM agent may still fail to get the role's
   credentials at its first try and then back off up to 30 minutes. Seen when launched
   about 8 seconds after `apply`; a reboot 20 minutes later fixed it.

Decided (with the user): `start` does not recover automatically and adds no hint. For
1 it exits saying that EC2 does not see the instance profile; for 2 and 3 it exits at
the deadline of the SSM agent's wait. Knowing what was attempted and what happened is
enough (the requester). These happen only right after an `apply` that creates the
instance profile, so a manual retry is enough: for 1, start again a minute later, by
when the credentials are usually usable, which also avoids 3; for 2 and 3, terminate,
wait a few minutes, and start again (2 needs the wait: observed still the deleted one
right after the recreation, the new one 6 minutes later). The usage doc tells this for
the cases where the role and instance profile are recreated. Rejected:

- Hints in the errors (wait a minute and start again; terminate, wait, and start again;
  look at `status` or `reboot`): the requester wants only what happened, as for the
  other retry hints of `proxy`, `bake`, and `set-port`.
- Waiting until EC2 accepts the instance profile and retrying `run-instances`: it
  launches at the earliest moment, which makes 3 more likely (it happened in 1 of 2
  runs).
- Rebooting once when the console shows the credentials failure: fixes 3 only.
- Comparing the instance profile after the launch and replacing the association: fixes
  2 only, adds IAM reads and association changes to every launch.
- Keeping the role and instance profile on `destroy`, or a unique instance profile name
  per creation: avoids 2 only, and changes what `destroy` leaves or the naming.

`--async` waits only until AWS accepts `run-instances` or `start-instances`. If the user
connects with ssh while the instance is `pending`, `proxy` waits until it can connect.
However, `describe-instances` lags the request by a few seconds, so right after the
request a launched instance may not be visible yet, or a resumed instance may still look
`stopped`. `proxy` then says the instance is absent or stopped and exits. Running `start`
again may launch a second instance ([Finding](../topics/instance.md#finding)), so check
`status` before connecting again.

## `stop`

`instance stop --svc <svc> [--async] [--yes]` (short form `stop`)

Stops the instance without terminating it. Disk contents, instance ID, and host keys
remain, so known_hosts is not touched. The next `start` resumes the stopped instance. The
launch template's existence is not checked.

1. Take the lock, find the instance as `start` does, and exclude `shutting-down` as
   `start` does.
   - None: report that there is none and finish. Not an error.
   - Two or more: list them and exit, as `start` does.
2. Branch on the state of the instance found.
   - `stopped`: report that it is already stopped and finish.
   - `stopping`: do not request a stop and do not confirm; go to 4.
   - `running`, `pending`: go to 3. Even in `pending`, do not wait for it to come up.
3. Confirm, naming the instance ID and state, and the region and account
   ([cli.md](../cli.md#confirmation)); request a stop with `stop-instances` and report
   it.
4. With `--async`, finish here. Otherwise wait until `stopped`. Keep waiting even if a
   stale `running` is visible. On the deadline, point at `status` and note that a
   `systemd-inhibit` lock inside the instance can delay the stop.

## `terminate`

`instance terminate --svc <svc> [<instance ID>...] [--async] [--yes]` (short form
`terminate`)

An instance ID is `i-` followed by 8 or 17 lowercase hexadecimal digits. Any other shape
is a usage error. Writing the same ID twice is also a usage error.

1. Take the lock and find instances as `start` does, including `shutting-down` ones.
2. Choose the instances to terminate.
   - IDs omitted: choose all found. If none are found, report that there is none and
     finish. Not an error.
   - IDs given: choose those. If a given ID is not among those found, list the ones found
     and exit without doing anything.
3. Confirm, naming the chosen instances' IDs and states, and the region and account
   ([cli.md](../cli.md#confirmation)). Unbaked apps are not looked up: that needs the
   instance running and reachable, and `terminate` must work in any state (the
   requester).
4. Terminate each chosen instance and delete its known_hosts
   ([Terminating](../topics/instance.md#terminating)). Do this for all of them even if
   there are two or more.
5. With `--async`, finish here. Otherwise wait until all chosen instances are
   `terminated`, reporting each state change.

## `reboot`

`instance reboot --svc <svc> [--async] [--yes]` (short form `reboot`)

Reboots the instance without terminating it. Disk contents, instance ID, and host keys do
not change, so known_hosts is not touched. The launch template's existence is not
checked.

After `reboot-instances` the instance stays `running`, and the SSM agent's `Online`
report lingers for a while after the agent goes down. Neither tells before the reboot
from after it, so temt reads the instance's boot_id
(`/proc/sys/kernel/random/boot_id`, an ID that changes on every boot) with Run Command
(`AWS-RunShellScript`) and compares it with the value before the reboot.

1. Take the lock, find the instance as `start` does, and exclude `shutting-down` as
   `start` does.
   - None: tell the user to `start` and exit.
   - Two or more: list them and exit, as `start` does.
   - `stopped`: tell the user to `start` and exit.
   - `running`, `pending`, `stopping`: go to 2. Even in an intermediate state, do not
     wait for it to finish.
2. Without `--async`, check that the SSM agent is `Online`. If it is not, the end of the
   reboot cannot be told, so tell the user to retry with `--async` and exit. If AWS
   refuses the check, show only that error. With `--async`, SSM is not used.
3. Confirm, naming the instance ID and state, and the region and account
   ([cli.md](../cli.md#confirmation)).
4. Without `--async`, read the boot_id before the reboot. If it cannot be read by the
   deadline, exit suggesting `--async`. If AWS refuses, show only that error. In both
   cases the reboot has not been requested yet. In 2 and here, `--async` is suggested
   whatever the instance's state.
5. Request the reboot with `reboot-instances` and report it.
6. With `--async`, finish here.
7. Wait until the boot_id reads differently from 4, report that the instance is ready for
   login, and finish. Read it only after the SSM agent reports `Online` later than the
   time the reboot was requested. The `Online` report lingers during the reboot and the
   agent reports as soon as it starts, so this avoids sending to the agent before it
   goes down and having the command caught in the reboot. Until the agent reports so,
   also look at the instance's state; if it reaches one of the states that end the wait
   for `running` ([Waiting](../topics/instance.md#waiting)), exit without waiting for
   the deadline, naming the state. On the deadline, exit pointing at `status`. Report
   once each: when starting to wait for the agent to come back, when it is back and
   reading the boot_id starts, and when the old boot_id comes back.

The boot_id is read the same way in 4 and 7:

- A Run Command is sent only while the SSM agent reports `Online`, one at a time. One
  that the agent cannot receive yet counts as not sent and is sent again.
- While the result of a sent command is not visible yet, or the agent cannot be reached
  for it, the command counts as not finished, not as failed.
- Only a command that succeeded and printed something gives the boot_id. Any other
  finish (including success with nothing printed), or not finishing within 1 minute of
  sending, abandons it, and it is sent again. The first such finish (not the
  1-minute abandonment) is reported once per wait as progress, with the command's
  status and the first line of its standard error. Resending usually works, but when
  the command keeps failing inside the instance (for example, a full disk), the timeout
  alone would point at the reboot instead.
- The command is given 30 seconds to be delivered and 30 seconds to run. It should be
  cut off at their sum, 60 seconds, but one caught in the reboot can be reported
  finished late, so it is abandoned at 1 minute.
- In 7, an agent that reported just before going down may return the old boot_id. If it
  is old, it is read again.

The local credentials need `ssm:SendCommand` and `ssm:GetCommandInvocation`. The
`[[role.ec2]]` that the scaffold of a new config file writes
([conf.md](../../docs/conf.md#role)) includes the permissions Run Command needs on the
instance side.

## `status`

`instance show --svc <svc> [--apps]` (short form `status`)

Shows the state of the service's instance. Display only: writes neither the config file
nor AWS. Without `--apps` it does not connect to the instance, so it stays fast. Shows:

- The launch template's name. If absent, shows that the service is not applied, and
  continues.
- Each instance's ID and state. If none, shows that there is none. If there are two or
  more, shows all. If there are two or more excluding `shutting-down`, warns on
  standard error, telling the user to terminate the extra ones by instance ID with
  `terminate`.
- Each instance's public IPv4 address, or that it has none.
- Each instance's SSM agent state (`Online`, `ConnectionLost`, `Inactive`). An instance
  whose agent has never registered is shown as not registered.
- With `--apps`, the apps on the instance, read as in
  [Reading the apps on the instance](app.md#reading-the-apps-on-the-instance), as a table
  after the instance display, one row per app in app ID order. When it does not read,
  it shows why and goes on. Reading takes seconds, so it is not done without `--apps`
  (the requester).

  | Column | Value |
  | --- | --- |
  | `ID` | App ID (the directory name) |
  | `VERSION` | Version in the instance's `manifest.toml`. Followed by `*` when the local app that `deploy` would use by this app ID has another version: deploying and baking it updates the app. Versions are compared only for equality. No `*` without a local app or when either version is unknown |
  | `BAKED` | `yes` when the record's `instance` is another instance (the app came from the AMI), `no` when it is this one (deployed here and not baked yet), `-` without a readable record |
  | `DEPLOYED` | The record's `deployed`, or `-` |

  Values come from the instance, so unprintable characters are replaced with `?` as in
  [List tables](../cli.md#list-tables).
- App status. The parameters with key `status` under `/<name>`, i.e.
  `/<name>/<app ID>/status` ([apps.md](../../docs/apps.md#アプリの状態)), shown once
  after the instance display, one line per app in app ID order, with the value and the
  time since the update. Shown even when there is no instance (and shown as none when
  there are none). Values that cannot be taken as written by an instance running now
  are marked stale.
  - The elapsed time is from the parameter's last modification to now, both in UTC.
    Under 60 seconds (including negative from clock skew) is shown as just now;
    otherwise it is shown in the single largest unit, truncated (minutes, hours, days).
    If the update time cannot be read, it is shown as unknown.
  - A value is stale when there is no `pending` or `running` instance, or every one of
    them launched (a resumed instance gets the resume time) after the value's last
    modification. Apps can write only from a running instance, so such an instance did
    not write the value. This keeps the last value from looking current after a
    `shutdown` from inside the instance or after an app stopped without writing. If the
    update time or launch time cannot be read and compared, the value is not stale.
    `reboot` does not change the launch time, so values from before a reboot are not
    marked.
  - Apps write the values from the instance, so characters that cannot be displayed
    (newlines, control characters, ...) are replaced with `?`. Values are not truncated.
    A `SecureString` value read without decryption is ciphertext, so only its type is
    shown instead of the value.
  - Only parameters named `/<name>/<app ID>/status` whose app ID follows the rule
    ([App IDs and keys](app.md#app-ids-and-keys)) are used. The local credentials need
    `ssm:GetParametersByPath`.

## `instance list`

`instance list [--svc <svc>] [--format <columns>]`

Lists temt's instances, one per row ([List tables](../cli.md#list-tables)). temt's
instances are those that have the `temt:service` tag and are not `terminated`. With
`--svc`, the service's config file decides the environment and only `temt:service=<svc>`
instances are listed. Without it, all services' instances in the environment decided as in
[Commands without --svc](../config.md#commands-without---svc) are listed. Instances of services without a config
file also appear. Instances are found by the same tag as `status` and the others
([Finding](../topics/instance.md#finding)). Rows are
ordered by the `temt:service` value, launch time, and ID. The SSM agent is not looked
at; `status` shows that.

| Column | Default | Value |
| --- | --- | --- |
| `ID` | yes | Instance ID |
| `SERVICE` | yes | Value of `temt:service` |
| `STATE` | yes | State |
| `TYPE` | yes | Instance type |
| `LAUNCHED` | yes | Launch time; a resumed instance gets the resume time |
| `IMAGE` | | ID of the AMI it was launched from |
| `IP` | | Public IPv4 address |
