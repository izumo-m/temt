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
3. With `--async`, finish here. Otherwise, if a resume was requested, wait until the old
   state is no longer visible ([Until the previous state
   disappears](../topics/instance.md#until-the-previous-state-disappears)). Then wait
   until `running`; also keep waiting while the instance is not yet visible by ID right
   after the launch. Then repeat `ssm describe-instance-information` until the SSM agent
   is reported `Online`, report that the instance is ready for login, and finish. Only
   an `Online` whose last ping (`LastPingDateTime`) is later than the instance's
   `LaunchTime` counts (`LaunchTime` becomes the resume time on a resume), because right
   after a stop and resume, the `Online` report from before the stop may remain.

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
3. Confirm, listing the instance ID and state; request a stop with `stop-instances` and
   report it.
4. With `--async`, finish here. Otherwise wait until `stopped`. Keep waiting even if a
   stale `running` is visible. On the deadline, add a pointer to `status` and note that a
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
3. Confirm, listing the chosen instances' IDs and states. Before that, if apps were
   deployed to a chosen instance and not yet baked (deploy records whose `instance` is
   that ID), warn that they will be lost by terminating. If the records cannot be read,
   warn and continue.
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
(`send-command` running `AWS-RunShellScript`) and compares it with the value before the
reboot.

1. Take the lock, find the instance as `start` does, and exclude `shutting-down` as
   `start` does.
   - None: tell the user to `start` and exit.
   - Two or more: list them and exit, as `start` does.
   - `stopped`: tell the user to `start` and exit.
   - `running`, `pending`, `stopping`: go to 2. Even in an intermediate state, do not
     wait for it to finish.
2. Without `--async`, check that the SSM agent is `Online`. If it is not, the end of the
   reboot cannot be told, so tell the user to retry with `--async` and exit. If AWS
   refuses the check, print only that error. With `--async`, SSM is not used.
3. Confirm, listing the instance ID and state.
4. Without `--async`, read the boot_id before the reboot. If it cannot be read by the
   deadline, exit with a suggestion to try `--async`. If AWS refuses, print only that
   error. In both cases the reboot has not been requested yet. In 2 and here, the
   suggestion to try `--async` is added whatever the instance's state.
5. Request the reboot with `reboot-instances` and report it.
6. With `--async`, finish here.
7. Wait until the boot_id reads differently from 4, report that the instance is ready for
   login, and finish. Send Run Command only after the SSM agent reports `Online` later
   than the time the reboot was requested (the time of the `reboot-instances`
   response). The `Online` report lingers during the reboot and the agent reports as
   soon as it starts, so this avoids sending to the agent before it goes down and having
   the command caught in the reboot. Until the agent reports so, also look at the
   instance's state; if it reaches one of the states that end the wait for `running`
   ([Waiting](../topics/instance.md#waiting)), exit without waiting for the deadline,
   stating the state. On the deadline, exit with a pointer to `status`. Report once
   each: when starting to wait for the agent to come back, when it is back and reading
   the boot_id starts, and when the old boot_id comes back.

The boot_id is read on each poll as follows, the same in 4 and 7.

- If no Run Command is outstanding, send one, but only while the SSM agent reports
  `Online`. If `send-command` is refused with `InvalidInstanceId` (the agent cannot
  receive it), treat it as not sent and send again on the next poll.
- If one is outstanding, look at its result with `get-command-invocation`. No invocation
  record yet (`InvocationDoesNotExist`) and a refusal with `InvalidInstanceId` mean not
  finished yet. `Success` with non-empty standard output gives the boot_id. Any other
  finish (including `Success` with empty standard output), or not finishing within 1
  minute of sending, abandons it and sends again on the next poll.
- The delivery timeout (`TimeoutSeconds`) and the execution timeout inside the instance
  (`executionTimeout`) are both 30 seconds. The command should be cut off at their sum,
  60 seconds, but one caught in the reboot can be reported finished late, so it is
  abandoned at 1 minute.
- In 7, an agent that reported just before going down may return the old boot_id. If it
  is old, send again on the next poll.

The local credentials need `ssm:SendCommand` and `ssm:GetCommandInvocation`. The default
of the instance role `temt-ec2` ([conf.md](../../docs/conf.md#role)) includes the
permissions Run Command needs.

## `status`

`instance show --svc <svc>` (short form `status`)

Shows the state of the service's instance. Display only: writes neither the config file
nor AWS. Shows:

- The launch template's name. If absent, shows that the service is not applied, and
  continues.
- Each instance's ID and state. If none, shows that there is none. If there are two or
  more, shows all. If there are two or more excluding `shutting-down`, prints one warning
  line to standard error telling the user to terminate the extra ones by instance ID
  with `terminate`.
- Each instance's public IPv4 address, or that it has none.
- Each instance's SSM agent state (`Online`, `ConnectionLost`, `Inactive`). An instance
  whose agent has never registered is shown as not registered.
- For each instance, whether the association `<name>-sshd` that aligns the sshd
  listening ports has finished running
  ([Status display](../topics/sshd-ports.md#status-display)).
- Unbaked apps, on the `apps:` line, only when the launch template exists. Decided from
  the deploy records by the same rule as `STATE` of [`app list`](app.md#app-list):
  `unbaked` and `lost` apps are listed in that order
  (`apps: unbaked timezone-tokyo; lost mise`). If there are neither, show `all baked` if
  any app is `baked`, otherwise `none`. Records for which it is unknown whether they
  were baked are not counted.
- App status. The parameters with key `status` under `/<name>`, i.e.
  `/<name>/<app ID>/status` ([apps.md](../../docs/apps.md#アプリの状態)), shown once
  after the instance display, in app ID order, with the value and the time since the
  update. Shown even when there is no instance. Values that cannot be taken as written
  by an instance running now get `stale` added to the elapsed time.

  ```
  app status:
    idle-shutdown  power off in 42m (at 2026-09-29 06:12:03 UTC; idle 18m since pam, limit 1h)  [3m ago]
    myapp          3 job(s) queued  [12m ago]
  ```

  - If there are none, print `app status: none`.
  - A line is: two spaces, the app ID padded to the longest app ID, two spaces, the
    value, two spaces, `[<elapsed>]`. A stale value is `[<elapsed>, stale]`.
  - The elapsed time is the difference between the parameter's `LastModifiedDate` (UTC)
    and the current UTC time. Under 60 seconds (including negative from clock skew) is
    `just now`; otherwise the single largest unit, truncated: `3m ago`, `2h ago`,
    `5d ago`. If the update time cannot be read, print `?`.
  - A value is stale when there is no `pending` or `running` instance, or every one of
    them launched (`LaunchTime`; a resumed instance gets the resume time) after the
    value's `LastModifiedDate`. Apps can write only from a running instance, so such an
    instance did not write the value. This keeps the last value from looking current
    after a `shutdown` from inside the instance or after an app stopped without writing.
    If the update time or launch time cannot be read and compared, the value is not
    stale. `reboot` does not change the launch time, so values from before a reboot are
    not marked.
  - Apps write the values from the instance, so characters that cannot be displayed
    (newlines, control characters, ...) are replaced with `?`. Values are not truncated.
    A `SecureString` value read without decryption is ciphertext, so `(SecureString)`
    is shown instead of the value.
  - Read `/<name>` recursively with `get-parameters-by-path`, and use only parameters
    named `/<name>/<app ID>/status` whose app ID follows the rule
    ([App IDs and keys](app.md#app-ids-and-keys)). The local credentials need
    `ssm:GetParametersByPath`.

## `instance list`

`instance list [--svc <svc>] [--format <columns>]`

Lists temt's instances, one per row ([List tables](../cli.md#list-tables)). temt's
instances are those that have the `temt:service` tag and are not `terminated`. With
`--svc`, the service's config file decides the environment and only `temt:service=<svc>`
instances are listed. Without it, all services' instances in the environment decided by
`--profile` and `--region` are listed ([Commands without
--svc](../config.md#commands-without---svc)). Instances of services without a config
file also appear. Instances without the `temt:service` tag (e.g. launched by hand with
`Name=<name>`) do not appear; `status` finds by `Name`, so they appear there. Rows are
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
