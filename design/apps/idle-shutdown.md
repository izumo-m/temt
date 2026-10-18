# idle-shutdown

Bundled app that powers the instance off when nobody has used it for the idle limit.
The launch template's `InstanceInitiatedShutdownBehavior` is `terminate`, so powering
off terminates the instance. Usage:
[docs/apps/idle-shutdown.md](../../docs/apps/idle-shutdown.md). Source:
`src/temt/apps/idle-shutdown/`; the program is `idle_shutdown.py`. `install` and
`uninstall` run as root.

Option `limit` (default `1h`, pattern `[1-9][0-9]*[smhd]?|off`): a duration up to 365
days, or `off`.

## Files

| Location | Contents |
| --- | --- |
| `/usr/local/bin/idle-shutdown` | The program. Runs with the instance's `python3 -I` and uses only the standard library |
| `/etc/idle-shutdown/limit` | The limit set by the option `limit`: seconds or `off`. Mode 644; `/etc/idle-shutdown/` is created with mode 755 if missing |
| `/etc/systemd/system/idle-shutdown.service`, `.timer` | Run the program as root 5 minutes after boot and every 5 minutes after that (`AccuracySec=30s`) |
| `/etc/tmpfiles.d/idle-shutdown.conf` | At every boot, create `/run/idle-shutdown/` (755) with `touch/` (1777) and `root/` (755) under it |
| `/usr/share/pam-configs/idle-shutdown` | `pam-auth-update` profile: when a login session opens or closes (sshd sessions without a terminal included), run `idle-shutdown touch pam` through `pam_exec` (`optional`, `quiet`) |
| `/etc/update-motd.d/60-idle-shutdown` | Puts the state in the login message ([Login message](#login-message)) |
| `/etc/ssh/sshd_config.d/idle-shutdown.conf` | `ClientAliveInterval 60` and `ClientAliveCountMax 10`: sessions left by a client that died without closing are cut after 10 minutes |
| `/etc/idle-shutdown/in-use.d/` | Where other apps place their in-use checks. Created with mode 755 |

## install

1. Check that the instance metadata gives the tag `temt:service`
   ([Reporting](#reporting)). If not, fail with nothing installed, telling the user to
   write `MetadataOptions.InstanceMetadataTags = "enabled"` in the config file and run
   `apply`, and for the current instance to run `aws ec2
   modify-instance-metadata-options --instance-id <id> --instance-metadata-tags
   enabled`, then deploy again. temt's `deploy` does not check this: temt does not know
   which apps need the tag (installing emacs does not). Rejected: `deploy` writing the
   service name into a file (it would be baked into the AMI, and another service using
   that AMI, such as one made by copying the config file, would read the original
   name; the default policy writes only under its own `/temt-<svc>`); writing it with
   cloud-init through the user data (temt would add its own part to the user's user
   data, against the config file being the whole template, and what changes at every
   launch comes from the instance metadata anyway).
2. Check and write the option `limit` with the program that was sent
   (`python3 -I <dir>/idle_shutdown.py limit --deploy <value>`), before installing
   anything. A value the program refuses (over 365 days) fails here with nothing
   installed.
3. If `aws` is not executable in any of the three places the program looks
   ([Reporting](#reporting)), wait for snapd's seeding (`snap wait system seed.loaded`;
   right after boot it may not be done) and `snap install aws-cli --classic`. A failure
   fails `install`; the user deploys again.
4. Place the files in the table, then `systemd-tmpfiles --create` for the tmpfiles
   entry.
5. `pam-auth-update --enable idle-shutdown`. If any `/etc/pam.d/common-*` was edited by
   hand, `pam-auth-update` writes nothing and still succeeds, so check that the line was
   added to `/etc/pam.d/common-session`; if not, fail telling the user to undo the hand
   edits.
6. `systemctl daemon-reload` and `systemctl enable --now idle-shutdown.timer`. If the
   timer is enabled more than 5 minutes after boot, systemd runs the check once at once.
7. `sshd -t`. If it fails, remove the sshd file placed and fail: baked as is, sshd would
   not start on the next instance. Otherwise `systemctl try-reload-or-restart
   ssh.service` (current connections stay; new ones get the setting).
8. `idle-shutdown report`, so that a new limit shows in `status` on the client without
   waiting for the timer, then `idle-shutdown status` for the deploy output.

The failures of `install` and `uninstall` are not listed in the usage doc: each message
says what to do, and that is enough (decided by the user).

## uninstall

1. If the PAM profile exists, `pam-auth-update --remove idle-shutdown`. If the line is
   still in `/etc/pam.d/common-session` (hand edits, as in `install`), fail without
   removing anything, telling the user to undo the edits: removing the program while
   the line stays would make every login and logout call a missing program.
2. Disable and stop the timer and stop the service, remove the files in the table
   except `in-use.d/`, `systemctl daemon-reload`, `systemctl try-reload-or-restart
   ssh.service` (no `sshd -t`), and remove `/run/idle-shutdown/`.
3. Remove `/etc/idle-shutdown/in-use.d/` and `/etc/idle-shutdown/` only if empty: checks
   placed by other apps stay. aws-cli stays, since other apps may use it.

The SSM parameter `status` is deleted by `app remove` after `uninstall`
([commands/app.md](../commands/app.md#app-remove)).

## In use

The instance is in use if any of these holds:

1. A logged-in sshd session exists. A process counts when its `/proc/<pid>/cmdline`
   (the name `ps` shows) is `sshd: ` or `sshd-session: ` followed by one word without
   spaces that does not start with `[`: `ubuntu`, `ubuntu@pts/0`, `ubuntu@notty`. A
   tunnel-only connection (`ssh -N`) opens no session and stays `ubuntu`; it counts too.
   Names before authentication contain a space or start with `[` and do not count.
   `sshd-session` is the name from OpenSSH 9.8 on.
2. A numbered terminal exists in `/dev/pts/`. screen and tmux keep a terminal per window
   even when detached, so they count here.
3. A shutdown inhibitor lock with mode `block` whose `what` includes `shutdown`, held by
   anyone. Mode `delay` (only delays power-off; unattended-upgrades always holds one)
   does not count. The list comes from logind's `ListInhibitors` through `busctl`. If it
   cannot be read, warn and count it as in use (`the inhibitor locks are unknown`).
4. An executable in `/etc/idle-shutdown/in-use.d/`, run in name order, exits 0 and
   prints something. Each nonempty line becomes the reason `<file name>: <line>`. Only
   when the program runs as root, since the scripts are written for root and would
   answer differently as another user. A file is run only if:
   - its name is only letters, digits, `_`, and `-` (as `run-parts`; others are skipped
     silently), it is a regular file, and executable;
   - its target belongs to root and is not writable by group or others (otherwise warn
     and skip).

   Each is cut off after 30 seconds. One that fails (warning with the first line of its
   stderr) or times out (warning) does not count as in use.

A client that dies without closing leaves its session in use until sshd cuts it: sshd
asks the client for a reply inside the ssh protocol every 60 seconds and cuts after 10
missed replies. Through Session Manager, sshd's TCP peer is the SSM agent on the
instance, so that TCP connection survives the client's death and TCP keepalive cannot
notice it.

## Last activity

The last activity time is the newest mtime among the files in
`/run/idle-shutdown/touch/` and `/run/idle-shutdown/root/`, or the boot time if there
are none (the source is then `boot`). A future mtime counts as now, except that a file in `touch/` more than 60
seconds in the future is ignored with a warning; counting it as now would keep the
instance up forever. `root/` is written only by root, so its future times are kept.

The file name tells who touched it (shown as `since <name>` in the report and
`by <name>` in `status`):

- The check: `root/in-use` when in use.
- PAM: `root/pam` at every login and logout, so a short login between two checks
  counts.
- Apps: `touch/<own name>`, created or touched whenever used. The directory is 1777, so
  no root is needed, but the file belongs to whoever created it first.

## Check and power-off

The timer runs the program without arguments (root required; otherwise
`checking needs root; try idle-shutdown -n or idle-shutdown status`). If in use, touch
`root/in-use`. Otherwise, if the idle time since the last activity is at least the
limit, run `systemctl poweroff`. Print the result as one line
([Reporting](#reporting)) to stdout (the journal), and write it to the SSM parameter.
The write happens before power-off, and a failure in it never stops the power-off.

The limit is the first found of:

1. `/run/idle-shutdown/limit`: set by `idle-shutdown limit`; lasts until reboot or
   termination.
2. `/etc/idle-shutdown/limit`: set by the option `limit`; written by `install` and kept
   in the AMI by baking.
3. 1 hour.

Each file holds seconds or `off` (never power off). A file that cannot be read, holds
neither, or exceeds 365 days is ignored with a warning. A redeploy does not remove
`/run/idle-shutdown/limit`, so a limit set with `limit` keeps taking precedence;
`limit --deploy` says so in two lines:

    the limit set until reboot still applies;
    run sudo idle-shutdown limit default to use the limit given at deploy

## Commands

| Command | Root | Behavior |
| --- | --- | --- |
| `idle-shutdown` | Required | Check, power off if past the limit, write the result to the SSM parameter. Run by the timer |
| `idle-shutdown -n` | No | Print the result in the timer's form; touch nothing, write nothing, never power off. Past the limit prints `power off at next check` |
| `idle-shutdown status` | No | Limit and its source, last activity and who touched it, in-use reasons, and the power-off plan |
| `idle-shutdown status --login` | No | `status` for the login message ([Login message](#login-message)) |
| `idle-shutdown limit` | No | Print the limit and its source |
| `idle-shutdown limit <duration>` | Required | Set the limit until reboot: `90m`, `4h`, `1d`, `3600` (seconds), `off`, or `default` (remove the until-reboot limit and return to the deploy one). Then print the limit and write the result of a check like `-n` (own session counted) to the SSM parameter |
| `idle-shutdown limit --deploy <duration>` | Required | Set the deploy limit. For `install` only. Durations as `limit <duration>` without `default`. No check, no SSM write |
| `idle-shutdown report` | Required | Write the result of a check like `-n` (own session counted) to the SSM parameter. For `install` only |
| `idle-shutdown touch [<name>]` | No | Touch `root/<name>` as root, else `touch/<name>`. `<name>` defaults to the caller's user name |

`limit --deploy`, `report`, and `status --login` are not shown in `--help`.

Durations: digits with an optional unit `s`, `m`, `h`, `d`; 0 and more than 365 days are
refused. The limit is shown as `1d2h`, `1h30m`, `45s`. The source is `set until reboot`,
`set at deploy`, or `default`.

Without root, `status` (except `--login`) and `-n` skip in-use.d and say so (`status`
prints `in-use.d:     not checked; run sudo idle-shutdown status to include it`; `-n`
warns on stderr).

`status` and `-n` are usually run over ssh, so they do not count the caller's own
session, which is:

- the sshd sessions among the caller and its ancestors. Another session multiplexed on
  the same connection with ControlMaster shares that sshd process, so it is not counted
  as an ssh session either; its terminal, if any, still counts;
- the terminals of the caller's stdin, stdout, and stderr;
- the controlling terminals of the ancestors (`sudo` runs the command on a new terminal,
  so the login terminal is known only from the ancestors).

`status` plan (`power off:` line): `never` when the limit is off; `not while in use`
when anything else is in use; `<limit> after you log out` when the caller has its own
session; otherwise the power-off time and the time left
(`2026-09-29 06:12:03 UTC (in 42m)`, or `(at next check)` once past). `in use:` gets
` (not counting this session)` when the caller has its own session. "After you log
out" relies on PAM touching `root/pam` at logout; run from a Session Manager shell,
which does not go through PAM, the power-off may come up to 5 minutes earlier than
shown.

`touch` names: start with a letter, digit, or `_`, followed by letters, digits, `.`,
`_`, `-`, at most 64 characters. The file is opened without following symlinks and
without blocking (a FIFO does not hang it); another user's file that cannot be written
fails with `cannot touch <path>; it belongs to another user`.

## Reporting

The result of a check is written as one string to the SSM parameter
`/temt-<svc>/idle-shutdown/status`, the app status that `status` on the client shows
([apps.md](../../docs/apps.md#アプリの状態)). Times are the instance's local time with
the zone name (`2026-09-29 06:12:03 UTC`), matching what ssh shows; durations look like
`1h2m`; `<source>` is the name of the file that set the last activity, or `boot`. The first
matching form, from the top:

| Result | Form |
| --- | --- |
| In use | `in use: <reason>; <reason> (limit <limit>)` |
| Limit `off` | `never powers off (limit off; idle <idle> since <source>)` |
| Before the limit | `power off in <left> (at <time>; idle <idle> since <source>, limit <limit>)` |
| Past the limit, powering off | `powering off (idle <idle> since <source>, limit <limit>)` |
| Past the limit, not powering off | `power off at next check (idle <idle> since <source>, limit <limit>)` |

Reasons are `<n> ssh session(s)`, `<n> terminal(s)`, `shutdown blocked by <who> (uid
<uid>, pid <pid>): <why>`, `the inhibitor locks are unknown`, and the in-use.d lines.

Written:

- At every timer check: the same line as the journal.
- Right before `systemctl poweroff`, in the `powering off` form. It stays after the
  instance terminates, so the reason for termination can be seen later.
- After `limit <duration>` and at the end of `install` (`report`): the result of a check
  like `-n` (no `root/in-use` touch, no power-off), but run as root so in-use.d is
  included, and the own session is counted, so run over ssh it is `in use`. Past the
  limit it is `power off at next check`; the next timer powers off.

`status`, `-n`, and `touch` do not write. Values over 200 characters (long in-use.d
output) are cut and end with `...`; the journal line is not cut.

The write is `aws ssm put-parameter --type String --overwrite` with `AWS_PAGER` empty.
The name `temt-<svc>` is made from the instance metadata tag `temt:service`, and the
region comes from `placement/region`, through IMDSv2 without proxies (2 seconds per
request); temt fills `MetadataOptions.InstanceMetadataTags = "enabled"` in the launch
template, and `install` checks it. The permission is in the default inline policy
`temt-ec2` of the instance role ([conf.md](../../docs/conf.md#role)). `aws` is looked up in `/snap/bin` (the aws-cli
snap), `/usr/local/bin`, `/usr/bin`, not through `PATH`. Each write gets 15 seconds. A
missing aws-cli, unreadable metadata, or a failed write (no permission, no network) only
warns (`cannot report the status: ...`); the check and power-off go on.

## Login message

Ubuntu's `pam_motd` runs the executables in `/etc/update-motd.d/` in name order as root
every time sshd opens a session, and uses their output as the login message. It runs for
command execution and tunnels too, but sshd prints the message only for a connection
that opens a terminal without a command, and not for a user with `~/.hushlogin`. A
connection multiplexed with ControlMaster opens no PAM session, so nothing runs. sshd
discards the message when the connection's first session (channel) opens, so only when
the master connection was opened with `-N` does the first multiplexed login show the
message produced when the master was opened. In Ubuntu's default `/etc/pam.d/sshd`,
`@include common-session` comes before `pam_motd`, so PAM has already touched
`root/pam` when the script runs.

`/etc/update-motd.d/60-idle-shutdown` prints an empty line, the heading
`idle-shutdown (sudo idle-shutdown limit 4h to change):`, and the output of
`idle-shutdown status --login`, each line (warnings from stderr included) indented by
two spaces. Warnings come before the state lines, since `status` checks everything
before printing. If the program is missing, it prints nothing.

When `pam_motd` runs, the logging-in connection has only sshd's privileged process
(`sshd: ubuntu [priv]`, or `sshd-session: ` from OpenSSH 9.8), and neither a counted
`sshd: ubuntu` process nor a terminal yet. Excluding the own session would exclude
nothing, so every counted session and terminal is someone else's. `status --login`
differs from `status`:

- It assumes the caller has its own session: `in use:` always ends with
  ` (not counting this session)`, and with nothing else in use the plan is
  `<limit> after you log out`.
- It does not read the last activity and omits that line: PAM has just touched it, so it
  is always the login time. Hence no future-time warning for `touch/` either.
- It skips in-use.d and does not say so: `pam_motd` runs on every connection, and each
  script may take up to 30 seconds.
