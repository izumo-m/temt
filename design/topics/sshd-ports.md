# sshd listening ports

The ports the instance's sshd listens on are changed by
[`set-port`](../commands/ssh.md#set-port), which runs a script on the instance through
Run Command and bakes the result into the AMI. The ssh Host block's `Port` says which
port ssh connects to; `apply` writes `22` only when the block has none
([ssh.md](ssh.md#the-ssh-directorys-config)), and after that `set-port` and the user
rewrite it. The config file has no key for the ports.

This replaced the config file's `SshdPorts` and a State Manager association that
aligned the instance's sshd with it at every launch. The association ran a script
inside imported instances too, against the principle of adding nothing inside them.
Rejected, in the order discussed with the requester:

- Keeping `SshdPorts` and the association for imported services too: a `SshdPorts` of
  the defaults file would actually move sshd, an sshd stopped on purpose would be
  started, and without State Manager permissions in the instance role `ssh` would wait 5
  minutes and fail after every `start`.
- A key that turns the association off for imported services only (`SshdPortsSync =
  false`).
- A bundled app `sshd-ports`, with `proxy` deciding the port from the app's record (and
  AMI or instance tags). The record is one per service while the ports belong to the
  instance and the AMI, so they drift when the AMI is replaced, the config file is
  copied, or a bake is forgotten. `%p` is 22 both without `Port` or `-p` and with
  `-p 22` (checked with OpenSSH 10.2), so "decide automatically when `%p` is 22" would
  make a `ProxyCommand` ending in `22` connect to 2222.
- Only an environment variable `TEMT_SSH_PORT`, without `%p` (`ssh -p` would not work).
- temt rewriting `Port` to the actual port at every `start` and the like.
- sshd always listening on 22 at `localhost` inside the instance.

## When `Port` and the instance differ

The Host block's `Port` and the instance's ports drift when the launch template's AMI
changes (an older AMI, another service's AMI, an AMI baked before `destroy`, reselecting
Ubuntu by deleting the `ImageId` line, an `ImageId` of the defaults file or a
`resolve:ssm:` that moves) and when `set-port` fails. The user fixes the Host block's
`Port` by hand (`deploy` and `app remove` follow it too), or gives `ssh -p` for one
connection. Running `set-port` again also aligns them, but it launches and bakes.

When `direct` passes its deadline, the error of `proxy` adds the port it tried and that
`Port` may differ from the instance (`ssh -p` tries another). How it looks through
Session Manager (an error from session-manager-plugin) is in the usage doc.

An imported instance whose sshd already listens on a port other than 22 gets that port
in the Host block's `Port` by hand; nothing inside the instance changes.

## Script

Run by step 4 of `set-port` as root with `sh` (dash on Ubuntu) through
`AWS-RunShellScript`, so it is POSIX sh. Lines it reports start with `temt-sshd:`, and
port lists are joined with `,` (`22 -> 2222,443`). It writes
`/etc/ssh/sshd_config.d/temt-sshd-ports.conf`: `Port <port>` lines, in the order given.
The whole script runs under `flock`, since a Ctrl-C on the client leaves the command
running on the instance.

Below, "effective ports" are the `port` values printed by `sshd -T`, and "listening
ports" are the listeners seen by `ss -Hltnp "sport = :<port>"` whose owner is `sshd` or
`systemd` (which holds the listener under socket activation). Listeners of other
processes do not count. Port lists are compared ignoring order.

1. Create `/run/sshd` if missing. It does not exist until sshd has started once, and
   `sshd -t` and `sshd -T` fail without it.
2. If the effective ports equal the given ports and all of them are listening, report
   `unchanged <ports>` and exit 0, without touching sshd.
3. Write the `Port` lines to a temporary file and move it into place. Create
   `sshd_config.d` if missing.
4. Check with `sshd -t`. If rejected, exit 1; sshd is not restarted.
5. Check that the effective ports equal the given ports. If not, another file also has
   `Port` lines and sshd would listen on their union; exit 1, naming the files that
   have `Port` lines.
6. Make sshd reload. First `systemctl daemon-reload`; the `ssh.socket` listeners are
   generated from `sshd_config` at this point. Then, if `ssh.socket` is enabled
   (`systemctl is-enabled`; socket activation on Ubuntu 24.04), restart `ssh.socket`,
   otherwise restart `ssh.service`. Existing connections are not cut.
7. Check that all of the given ports are now listening (20 checks 0.5 seconds apart).
   If not, exit 1.
8. Report `changed <previous ports> -> <current ports>`.

At the end, whatever the outcome, it prints the ports actually listening (`ss`). It
neither backs up nor restores: a failure is left for the user to fix
([Failure](../commands/ssh.md#failure)).

The script assumes temt's usual Ubuntu (24.04, `sshd_config` `Include`s
`sshd_config.d`, the unit is `ssh`). Under socket activation on Ubuntu 24.04,
`daemon-reload` maps the `Port` lines of `sshd_config.d` onto the `ssh.socket`
listeners. `Port` lines in `sshd_config.d` add up, and Ubuntu's `sshd_config` has no
`Port`, so only this file's `Port` lines take effect. On an imported instance whose
`sshd_config` has a `Port` line, step 5 fails, naming the file. Apps do not write
`Port` to `sshd_config.d` ([apps.md](../../docs/apps.md#install-と-uninstall)).
