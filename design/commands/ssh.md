# ssh commands

## `proxy`

`ssh proxy --svc <svc> [<port>] [--user <user>]` (short form `proxy`)

Runs as ssh's ProxyCommand. `<port>` defaults to 22 and accepts only an integer from 1
to 65535; `proxy` connects to the `%p` it receives as it is, so `ssh -p` works with any
value. `--user` is the user ssh logs in as (`%r`), whom the login key is sent for; `ssh
-G` alone cannot see a user given on ssh's command line (`ssh other@<name>`), so `proxy`
gives it to `ssh -G`. It follows the user-name rule ([app.md](app.md#users)); one that
does not is a usage error. Without it, the user from `ssh -G` is used. Does not take the
lock.

### Region and profile

The ssh the user runs has no way to give `--region` or `--profile` to `proxy`, so
`proxy` decides them by the order of the commands with `--svc`
([config.md](../config.md#region), [config.md](../config.md#profile)) from its
environment, which ssh passes on to ProxyCommand. So `AWS_REGION=<region> ssh
<name>` reaches the clone in that region, unless `AwsRegion` is written in the config
file or the defaults file. The ssh the user runs does not follow a `--region` given to
another command: after `start --region <B>`, `ssh <name>` without `AWS_REGION` looks in
the region the order gives, not B. The usage doc says this.

There is one Host block per service ([topics/ssh.md](../topics/ssh.md)), so ssh, like
`start` and the others, reaches the instance of the region decided at that time. To be
connected to two regions at once, the user sets up two services.

When temt itself runs ssh (`deploy` and `app remove`; `set-port`, `app show`, and
`status --apps` use Run Command instead), it passes the region and profile that command decided to `proxy` in the
environment variables `TEMT_PROXY_REGION` and `TEMT_PROXY_PROFILE`, and `proxy` takes
them at the rank of `--region` and `--profile`, so both look at the same instance. They
are internal to temt and not in docs/. `AWS_REGION` is not used for this: it ranks
below the defaults file's `AwsRegion`, which a `--region` given to `deploy` overrides.
The rules for the ssh the user runs do not change.

### Routes

There are two routes to the instance, matching the values of the environment variable
`TEMT_LOGIN`.

- `ssm`, through Session Manager: connects with `aws ssm start-session` and the document
  `AWS-StartSSHSession` to the sshd port. Traffic goes over https to the AWS API, so
  neither an ingress rule in the security group nor a network that lets the client out
  on the sshd port is needed. The client needs `session-manager-plugin`.
- `direct`, direct connection: connects over TCP to the instance's public IPv4 address.
  Needs an ingress rule in the security group and a network that lets the client out to
  the sshd port.

`proxy` reads `TEMT_LOGIN` on every connection to choose the route: unset or empty is
`ssm`; any value other than `ssm` and `direct` is an error and exits. ssh passes its
environment to ProxyCommand, so `TEMT_LOGIN=direct ssh <name>` changes the route for
that connection only. temt never falls back automatically to the other route when one
is unusable.

### Connecting

On either route, first decide the login key to send from `ssh -G <name>`; if there is
none, exit before calling AWS ([topics/login-key.md](../topics/login-key.md)). Then
find the instance under the same conditions as `start`
([topics/instance.md](../topics/instance.md#finding)). This single lookup decides the
instance to connect to, and the address used is that instance's. This keeps the
instance known_hosts points to and the address connected to from belonging to different
instances.

- None: tell the user to run `start`, and exit. Two or more: as in `start`, list them and
  tell the user to terminate the extra ones and connect again, and exit.
- `pending`: wait until `running`.
- `stopped` or `stopping`: tell the user to run `start`, and exit.

Next, check that the route can connect: for `ssm`, that the SSM agent reports `Online`;
for `direct`, that a public IPv4 address is attached and its port accepts a TCP
connection (each attempt up to 5 seconds).

- If the instance started recently (it was `pending`, or it is within 5 minutes of
  launch), wait until it can connect. A resumed instance's launch time is the resume
  time, so a recently resumed instance is waited for too.
  - On `ssm`, as in `start`, only `Online` reports after the launch time count.
  - On `direct`, wait for a public IPv4 address, then for the port to accept.
- While waiting, progress is reported on stderr, which ssh shows on screen.
- Otherwise (not `pending` and more than 5 minutes after launch), do not wait; if it
  cannot connect, exit saying why. On `ssm`, the error says the SSM agent is not
  `Online`, with no hint on what to do next. On `direct`, a refused
  connection is told as sshd not accepting, and a timeout or similar as a matter of the
  ingress rules. A `direct` instance without a public IPv4 address exits saying so, with
  nothing more.
- When the wait for the SSM agent passes its deadline, the error says only that the
  agent did not become `Online` by the deadline, without a hint on what to do next (the
  requester; as in [`start`](instance.md#right-after-apply)). When the wait for the port
  on `direct` passes its deadline, the error tells the last reason as above, names the
  port it tried, and says that the Host block's `Port` may differ from the instance's
  sshd, which `ssh -p` can try
  ([topics/sshd-ports.md](../topics/sshd-ports.md#when-port-and-the-instance-differ)).
  The wait for `running` and for the address says only which wait did not finish.

Then send the login key with EC2 Instance Connect (it is accepted for 60 seconds, so it
is sent on every connection, just before ssh authenticates), prepare known_hosts
([topics/ssh.md](../topics/ssh.md#known_hosts)), and:

- `ssm`: hand the connection over to `aws ssm start-session`.
- `direct`: reuse the TCP connection opened by the check and relay bytes between it and
  stdin/stdout.
  - When stdin ends, close only the sending side of the connection; exit when the
    instance closes the connection.
  - If the connection breaks during the relay, exit silently as for a close; ssh
    reports the break.
  - `proxy` exits when ssh exits.
  - The connection gets `TCP_NODELAY` and `SO_KEEPALIVE`, as the default ssh sets when
    it connects over TCP itself.

## `set-port`

`ssh set-port --svc <svc> <port>... [--yes]`

Changes the ports the instance's sshd listens on, bakes the result, and points the Host
block's `Port` at the first port (`set-port --svc dev 2222 443`). Ports are separated by
spaces, as the instance IDs of `terminate` are. It has no short form. The verb says it
rewrites; the target is `ssh` because both the sshd ports and the Host block's `Port`
concern the ssh connection (the requester).

1. If the config file (with the defaults file) writes `ImageId`, the AMI is fixed, so
   stop with a usage error before doing anything else, as [`bake`](ami.md#bake) does.
2. Check the ports (integers from 1 to 65535, none repeated) and that the launch
   template exists. Also check here what would stop later: the checks of step 3 of
   [`bake`](ami.md#bake), and whether the Host block can be written (the same checks as
   step 1 of `apply`). Changing sshd and then stopping would leave the change unbaked.
3. Show the steps and confirm, with the region and account
   ([cli.md](../cli.md#confirmation)), saying that the instance is terminated at the
   end and that apps not yet baked are baked with it.
4. If there is no instance, launch one; if it is stopped, resume it (as `start`). Wait
   until the SSM agent is `Online`.
5. Run the [script](../topics/sshd-ports.md#script) with Run Command
   (`AWS-RunShellScript`).
6. Bake, as `bake` does; the instance terminates. As in `bake`, only that region's
   launch template gets the new AMI, and the config file is not touched.
7. Rewrite the Host block's `Port` to the first port.
8. Tell how to change the ingress rules for direct connection
   ([Ingress rules](#ingress-rules)).

It goes through SSM, so it works when ssh cannot connect, and it bakes in the same run,
so no "changed but not baked" state is left. It bakes every time, even when the ports
are already the same: the script cannot tell an instance changed but not baked from an
AMI that has the ports, and a rerun after stopping partway would end without baking. To
align only `Port` (a config file copied to another service, ...), edit the Host block's
`Port` by hand (the requester).

Stopped partway, running it again aligns everything, since it always bakes.

The Host block's `Port` is one for every region, while the ports are baked into one
region's AMI, so the user runs `set-port` in a clone's region too
([topics/sshd-ports.md](../topics/sshd-ports.md#when-port-and-the-instance-differ)).

The service's lock is held through all steps; launching and baking are done inside it.
Writing `Port` takes the lock of the ssh config, as `apply` does.

### Run Command

- The permissions on the instance side are those of `reboot`, in the `[[role.ec2]]`
  that the scaffold of a new config file writes. If the SSM agent does not become
  `Online`, or AWS refuses the command (an imported instance without the permissions,
  ...), stop with the reason.
- The script ends by reporting its outcome: the ports changed, already the same, or a
  failure before or after the restart of sshd. Success is a Run Command that succeeded
  with a changed or unchanged outcome; anything else is a failure, told apart by that
  outcome ([Failure](#failure)). Both its standard output and standard error are shown
  (`sshd -t` gives its reason on standard error).
- The command's time limit inside the instance is well above the script's longest run.
  The client also has a deadline; past it, the error names the Run Command so that the
  user can look it up. A command the agent cannot receive yet right after the launch is
  sent again, as in `reboot`.

### Failure

On failure, nothing is undone, nothing is baked, and `Port` is not rewritten. The
script's output is shown, and the error says what happened, split by the step that
failed, and that the instance stays running and costs money. The user fixes it (the
requester).

- Before the restart (`sshd -t` refused, or `Port` lines in another file): sshd still
  runs on the previous ports. The configuration on disk stays broken, so the error also
  says not to `reboot`, `stop`, or `bake` until it is fixed, that `ssh -p` with a
  previous port logs in to fix it, and names the files with `Port` lines.
- After the restart (not listening): the error says that sshd may listen on no port at
  all (`ssh.socket` drops the whole unit if one bind fails), and adds no way to fix it.
  The requester chose this over keeping a hint (open a shell with Session Manager, or
  terminate and start), as for the other retry hints.

### Ingress rules

On success, `set-port` shows how to change the ingress rules with `sg`, noting that they
are needed only for direct connection (`TEMT_LOGIN=direct`). It changes no rules itself
(the requester). What it shows is built from the previous and new ports:

- If the service's config file has a rule for the previous port, it lists `sg add` for
  the new port with the same source, `sg remove` for the previous port, and `apply`.
  Otherwise, only an example `sg add` for the new port.
- If an existing rule's port range already includes the new port, it says no rule is
  needed.
- If the rule for the previous port is in the defaults file, it adds that editing the
  defaults file affects every service (`sg remove` cannot remove its rules).
- If the template uses no temt security group (an imported service, ...), `sg` does not
  apply; it tells the user to fix that security group in AWS.
