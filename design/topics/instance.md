# Instance

`start` launches the instance; `terminate`, `bake`, and `destroy` terminate it. `bake`
stops it before terminating. `stop` stops it without terminating, and `start` resumes a
stopped one. `reboot` reboots it without terminating. Each command's steps are in
[commands/](../commands/); this file holds the mechanisms shared across commands.

## Finding

Find instances whose tag `temt:service` is `<svc>` and whose state is one of `pending`,
`running`, `stopping`, `stopped`, `shutting-down`. The `Name` tag is not used to manage
anything, so it may hold any value (an imported service may keep the source's `Name`).

- `start`, `stop`, `terminate`, `reboot`, `bake`, `destroy`, `set-port`, `deploy`,
  `app remove` find after taking the lock.
- `proxy`, `status`, and `app list` find without taking the lock. `proxy` also decides
  where to connect from this result.

`describe-instances` lags `run-instances` by a few seconds. `start --async` releases the
lock once AWS accepts the launch request, so a `start` right after it may not yet see
the launched instance and may launch a second one. This is accepted: `status` warns
about two or more instances, and `terminate` can terminate only the extra ones given by
instance ID. A `start` without `--async` holds the lock until login is possible.

## Intermediate states

`start`, `stop`, and `reboot` do not refuse in advance when the instance is in an
intermediate state (`pending`, `stopping`); they send the request (`start-instances`,
`stop-instances`, `reboot-instances`) to AWS as is. AWS decides which states accept
what, and temt is built to work correctly with either outcome. If AWS refuses with an
instance-state error (`IncorrectInstanceState`, `IncorrectState`), temt exits with the
error code and message, adding that the user should check `status` and try again.
Requests that need not be sent are not sent: `running` and `pending` found by `start`,
`stopped` and `stopping` found by `stop`. `reboot` without `--async` reaches its request
only after steps 2 and 4 ([commands/instance.md](../commands/instance.md#reboot)), which
need the SSM agent, so on an intermediate state it usually stops there suggesting
`--async`. `bake` (and `deploy`/`app remove` with `--bake`) stops a `running` instance
the same way: if its state changed while the confirmation waited, AWS refuses and temt
adds the same hint, with `bake` as the command to try again.

An instance that is shutting down (`shutting-down`) is treated as nonexistent, like
`terminated`. Every command counts "two or more" excluding such instances. If only such
instances exist, the command behaves as if there were none:

- `start` launches a new one.
- `stop` reports there is none.
- `reboot`, `deploy`, `app remove`, `proxy` tell the user to `start`.
- `bake` says there is nothing to bake.
- `status` and `terminate` list it too.
- `terminate` and `destroy` terminate it too, and wait until it is terminated
  (`terminate --async` does not wait).

## Terminating

Step 4 of [`terminate`](../commands/instance.md#terminate). For each selected instance,
do the following in order. With two or more, do it for all of them.

1. If the root volume has `DeleteOnTermination` false, set it to true. Continue on
   failure, reporting that the volume will remain. Other EBS volumes are left as they
   are: they are attached from inside the instance and outlive it
   ([ami.md](ami.md#creating)).
2. Call `terminate-instances`.
3. Once the request is accepted, delete that instance's known_hosts file. If `current`
   pointed at it and is now a dangling link, delete `current` too. EC2 never reuses
   instance IDs, so this need not wait for termination.

## Waiting

Each waiting stage has a deadline, except `bake` (and `--bake`, `set-port`, and
`ami import`) waiting for the AMI (below). (`apply`
waiting for `.config.lock` is not a stage here and has none either; see
[cli.md](../cli.md#deadlines).)

| Stage | Deadline |
| --- | --- |
| Instance becomes `running` | 10 minutes |
| Instance becomes `stopped` | 10 minutes |
| A public IPv4 address is assigned (`proxy` with `direct`) | 10 minutes |
| After launch, login becomes possible (the SSM agent reports `Online` later than the launch) | 5 minutes |
| After launch, sshd accepts connections (`proxy` with `direct`; each connection attempt up to 5 seconds) | 5 minutes |
| The script of `set-port` finishes through Run Command | Decided at implementation, well above the script's longest run |
| Reading boot_id before the reboot (`reboot`) | 1 minute |
| From requesting the reboot until boot_id reads as changed (`reboot`) | 10 minutes |
| From requesting resume until `stopping` is no longer seen (`start`) | 10 minutes |
| From requesting resume until `stopped` is no longer seen (`start`) | 1 minute |
| Instance is terminated | 10 minutes |

- A deadline counts from when that stage starts waiting. Past the deadline, temt exits
  saying which stage did not finish.
- While waiting for `running`, if the instance goes to `stopping`, `stopped`,
  `shutting-down`, or `terminated`, temt exits at once stating that state, without
  waiting for the deadline. The same applies:
  - while waiting for `stopped`, when it goes to `shutting-down` or `terminated`;
  - while waiting for a public IPv4 address, on the same states as for `running`;
  - while `reboot` waits for the reboot to finish, on the same states as for `running`.
    `reboot` looks at the state only on polls where the SSM agent is not `Online`.
- When stating a state, temt appends the reason for the state change (`Message` of
  `StateReason`) if there is one.
- Each time a stage advances, temt reports it once.

Time since launch is counted from the time of the `describe-instances` response, not
the local clock. The launch time (`LaunchTime`) becomes the resume time when a stopped
instance is resumed.

When `bake` waits for the AMI to become `available`, there is no deadline: a large AMI
can take long, and stopping the wait changes nothing, so running `bake` again is
enough. The AMI counts as failed if, while waiting, it enters a state other than
`pending` or `available`, or if it is still not found by `describe-images` 5 minutes
after creation.

### Until the previous state disappears

After `start` without `--async` requests a resume, it looks the instance up by ID and
waits until `describe-instances` no longer returns the previous state, then waits for
`running`. `describe-instances` lags `start-instances` by a few seconds; waiting for
`running` while the previous state is still visible would mistake the stale state for an
instance that stopped and will not become `running`. With `--async` there is no wait.

If the state found was `stopping`, first wait until `stopping` is no longer seen, with
the same deadline as for becoming `stopped`, because AWS may accept a resume during
stopping and start the instance only after it finishes stopping. Then, in either case,
wait until `stopped` is no longer seen. Past the deadline, temt exits with the reason
for the last seen state's change. A resume that fails at once and returns to `stopped`
(for example, when the key of an encrypted EBS volume is unusable) also ends here.

`stop` does not wait this way: if it sees a stale `running` while waiting for
`stopped`, it just keeps waiting.

## Locks

`start`, `stop`, `terminate`, `reboot`, `bake`, `destroy`, `ami import`, `set-port`,
`deploy`, and `app remove` take an exclusive lock on the lock file `.lock` in the
known_hosts directory before acting. If the lock cannot be taken, temt exits saying another command is running,
without waiting. Locks are per service, so different services can be operated in
parallel. Before taking the lock, the ssh directory and `keys/` are prepared as in
`apply` step 3 (created with mode 700, or tightened to 700) and the known_hosts directory
is created if missing, without reporting it.

[`ami prune`](../commands/ami.md#ami-prune) does not lock before starting. For each AMI
it deletes, it takes the lock of the service in that AMI's `temt:service` tag and
releases it after the deletion. If the lock cannot be taken, it keeps that AMI without
waiting and moves on. If the known_hosts directory does not exist, it does not create
it and proceeds without the lock.
