# proxy

`ssh proxy --svc <svc> [<port>] [--user <user>]` (short form `proxy`)

Runs as ssh's ProxyCommand. `<port>` defaults to 22 and accepts only an integer from 1
to 65535. `--user` is the user ssh logs in as (`%r`), whom the login key is sent for;
`ssh -G` cannot see a user given on ssh's command line (`ssh other@<name>`). It follows
the user-name rule ([app.md](app.md#users)); one that does not is a usage error. Without
it, the user from `ssh -G` is used. Does not take the lock.

## Routes

There are two routes to the instance, matching the values of the environment variable
`TEMT_LOGIN`.

- `ssm`, through Session Manager: connects with `aws ssm start-session --target
  <instance-id> --document-name AWS-StartSSHSession --parameters portNumber=<port>`.
  Traffic goes over https to the AWS API, so neither an ingress rule in the security
  group nor a network that lets the client out on the sshd port (`SshdPorts`) is
  needed. The client needs `session-manager-plugin`.
- `direct`, direct connection: connects over TCP to the instance's public IPv4 address.
  Needs an ingress rule in the security group and a network that lets the client out to
  the sshd port.

`proxy` reads `TEMT_LOGIN` on every connection to choose the route: unset or empty is
`ssm`; any value other than `ssm` and `direct` is an error and exits. ssh passes its
environment to ProxyCommand, so `TEMT_LOGIN=direct ssh <name>` changes the route for
that connection only. temt never falls back automatically to the other route when one
is unusable.

## Connecting

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
  - On `ssm`, as in `start`, only `Online` reports after the launch time count. Then
    also wait until the association that aligns the sshd listening ports has finished
    running ([topics/sshd-ports.md](../topics/sshd-ports.md#right-after-launch)).
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
  are given (on `direct`, based on the last reason the connection failed). The wait for
  `running` and for the address carries no hint, and the association wait adds only
  `see 'temt status --svc <svc>'`.

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
