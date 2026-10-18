# ssh commands

## `proxy`

`ssh proxy --svc <svc> [<port>] [--user <user>]` (short form `proxy`)

Runs as ssh's ProxyCommand. `<port>` defaults to 22 and accepts only an integer from 1
to 65535; `proxy` connects to the `%p` it receives as it is, so `ssh -p` works with any
value. `--user` is the user ssh logs in as (`%r`), whom the login key is sent for;
`ssh -G` cannot see a user given on ssh's command line (`ssh other@<name>`), so `proxy`
passes it as `ssh -G -l <user>`. It follows the user-name rule ([app.md](app.md#users));
one that does not is a usage error. Without it, the user from `ssh -G` is used. Does not take the lock.

### Routes

There are two routes to the instance, matching the values of the environment variable
`TEMT_LOGIN`.

- `ssm`, through Session Manager: connects with `aws ssm start-session --target
  <instance-id> --document-name AWS-StartSSHSession --parameters portNumber=<port>`.
  Traffic goes over https to the AWS API, so neither an ingress rule in the security
  group nor a network that lets the client out on the sshd port is needed. The client
  needs `session-manager-plugin`.
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
  cannot connect, exit with a hint.
  On `ssm`, the hint is to look at the agent's state
  (`see 'temt status --svc <svc>' or run 'temt reboot --svc <svc> --async'`). On
  `direct`, a refused connection means sshd is not accepting; a timeout or similar means
  look at the ingress rules. A `direct` instance without a public IPv4 address exits
  with `<instance-id> has no public IPv4 address` and no hint.
- When the wait for the SSM agent or for the port passes its deadline, the same hints
  are given (on `direct`, based on the last reason the connection failed). On
  `direct`, the error also names the port it tried and adds that the Host block's
  `Port` may differ from the instance's sshd, which `ssh -p` can try
  ([topics/sshd-ports.md](../topics/sshd-ports.md#when-port-and-the-instance-differ)).
  The wait for `running` and for the address carries no hint.

Then send the login key with EC2 Instance Connect (it is accepted for 60 seconds, so it
is sent on every connection, just before ssh authenticates), prepare known_hosts
([topics/ssh.md](../topics/ssh.md#known_hosts)), and:

- `ssm`: `exec` `aws ssm start-session`.
- `direct`: reuse the TCP connection opened by the check and relay bytes between it and
  stdin/stdout.
  - When stdin ends, close only the sending side of the connection; exit when the
    instance closes the connection.
  - If the connection breaks during the relay, exit silently as for a close; ssh
    reports the break.
  - ssh sends SIGHUP to `proxy` when it exits, and `proxy` exits then.
  - The connection gets `TCP_NODELAY` and `SO_KEEPALIVE`, as the default ssh sets when
    it connects over TCP itself.

## `set-port`

`ssh set-port --svc <svc> <port>... [--yes]`

Changes the ports the instance's sshd listens on, bakes the result, and points the Host
block's `Port` at the first port (`set-port --svc dev 2222 443`). Ports are separated by
spaces, as the instance IDs of `terminate` are. It has no short form. The verb says it
rewrites; the target is `ssh` because both the sshd ports and the Host block's `Port`
concern the ssh connection (the requester).

1. Check the ports (integers from 1 to 65535, none repeated) and that the launch
   template exists. Also check here what would stop later: the checks of step 3 of
   [`bake`](ami.md#bake) (the template matches the config file; the root volume size),
   and whether the Host block can be written (the same checks as step 1 of `apply`).
   Changing sshd and then stopping would leave the change unbaked.
2. Show the steps and confirm (`[y/N]`), saying that the instance is terminated at the
   end and that apps not yet baked are baked with it.
3. If there is no instance, launch one; if it is stopped, resume it (as `start`). Wait
   until the SSM agent is `Online`.
4. Run the [script](../topics/sshd-ports.md#script) with Run Command (`send-command`,
   `AWS-RunShellScript`).
5. Bake, as `bake` does; the instance terminates.
6. Rewrite the Host block's `Port` to the first port.
7. Tell how to change the ingress rules for direct connection
   ([Ingress rules](#ingress-rules)).

It goes through SSM, so it works when ssh cannot connect, and it bakes in the same run,
so no "changed but not baked" state is left. It bakes every time, even when the ports
are already the same: the script's `unchanged` cannot tell an instance changed but not
baked from an AMI that has the ports, and a rerun after stopping partway would end
without baking. To align only `Port` (a config file copied to another service, ...),
edit the Host block's `Port` by hand (the requester).

Stopped partway, running it again aligns everything, since it always bakes.

The service's lock is held through all steps; launching and baking are called from
inside. Writing `Port` takes the lock of the ssh config, as `apply` does.

### Run Command

- The permissions are those of `reboot`, in the default `temt-ec2` of the instance role.
  If the SSM agent does not become `Online`, or `send-command` is refused (an imported
  instance without the permissions, ...), stop with the reason.
- Success is `Status` `Success` with a last line `changed` or `unchanged`. Both
  standard output and standard error are shown (`sshd -t` gives its reason on standard
  error).
- `executionTimeout` is well above the script's longest run. The client also has a
  deadline; past it, it prints the command ID and ends. `InvalidInstanceId` right after
  launch is resent, as in `reboot`.

### Failure

On failure, nothing is undone, nothing is baked, and `Port` is not rewritten. The
script's output and hints for fixing are printed, split by the step that failed; the
user fixes it (the requester). The hints also say that the instance stays running and
costs money.

- Before the restart (`sshd -t` refused, or `Port` lines in another file): sshd still
  runs on the previous ports, so `ssh -p <previous port>` logs in to fix it. The
  configuration on disk stays broken, so do not `reboot`, `stop`, or `bake` until it is
  fixed. The files with `Port` lines are named.
- After the restart (not listening): sshd may listen on no port at all (`ssh.socket`
  drops the whole unit if one bind fails). Open a shell with `aws ssm start-session
  --target <instance ID>` and fix it, or `terminate` and `start` (what was not baked is
  lost).

### Ingress rules

On success, `set-port` shows how to change the ingress rules with `sg`, noting that they
are needed only for direct connection (`TEMT_LOGIN=direct`). It changes no rules itself
(the requester). The hint is built from the previous and new ports:

- If the service's config file has a rule for the previous port, it lists `sg add` for
  the new port with the same source, `sg remove` for the previous port, and `apply`.
  Otherwise, only an example `sg add` for the new port.
- If an existing rule's port range already includes the new port, it says no rule is
  needed.
- If the rule for the previous port is in the defaults file, it adds that editing the
  defaults file affects every service (`sg remove` cannot remove its rules).
- If the template uses no temt security group (an imported service, ...), `sg` does not
  apply; it tells the user to fix that security group in AWS.
