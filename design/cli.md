# Command line

## Implementation

All client operations are done by the command `temt`, written as the Python 3 (3.12 or
later) package `src/temt/`. The repository's `.venv` is for development and is made by
`uv sync`.

- A bundled app that `deploy` names by app ID comes from the package of the `temt` that
  runs. The `temt` in `.venv` points at the repository, so what is being edited there is
  what gets sent.
- The service command and the `ProxyCommand` call `temt` by name, not by path, so the
  `temt` on PATH at that time runs.
- AWS is called through boto3. aws-cli is run only by `proxy` when it connects through
  Session Manager.

## Grammar

The form is `temt <target> <verb> [args] [options]`. The target is a noun for the kind
of thing temt handles; the verb is the operation on it. Positional arguments after the
verb name what is operated on (an AMI, instance IDs, an app ID, a port, etc.).

Some commands have a one-word short form, typed as `temt <short form> [args] [options]`.
A short form takes the same arguments and command-line options as the command it stands
for, and behaves the same.

The service is selected with `--svc <svc>`:

- Commands written `--svc <svc>` in the table require it. Those written `[--svc <svc>]`
  (`service list`, `instance list`, `ami list`, `ami prune`, `ami delete`, `app list`)
  may omit it.
- With `--svc`, the service's config file (over the defaults file) decides the
  environment (region and profile; [config.md](config.md#region)), and the command
  deals only with that service's things.
- Without `--svc`, the command deals with the things of all services in the environment
  given by `--profile` and `--region`, else the defaults file and AWS's rules
  ([config.md](config.md#commands-without---svc)).
  Exception: `app list` without `--svc` deals only with the apps that can be named by app
  ID.
- Commands that do not call AWS (`service list`, `sg list`, `sg add`, `sg remove`, and
  `app list` without `--svc`) take neither `--profile` nor `--region`.

## Commands

| Target | Command | Short form |
| --- | --- | --- |
| `service` | `service apply --svc <svc> [--yes]` | `apply` |
| | `service diff --svc <svc>` | `diff` |
| | `service destroy --svc <svc> [--yes]` | `destroy` |
| | `service list [--svc <svc>] [--format <columns>]` | |
| `instance` | `instance start --svc <svc> [--async]` | `start` |
| | `instance stop --svc <svc> [--async] [--yes]` | `stop` |
| | `instance terminate --svc <svc> [<instance ID>...] [--async] [--yes]` | `terminate` |
| | `instance reboot --svc <svc> [--async] [--yes]` | `reboot` |
| | `instance show --svc <svc>` | `status` |
| | `instance list [--svc <svc>] [--format <columns>]` | |
| `ami` | `ami bake --svc <svc> [--yes]` | `bake` |
| | `ami import --svc <svc> <instance ID> [--yes]` | |
| | `ami list [--svc <svc>] [--format <columns>]` | |
| | `ami prune [--svc <svc>] [--yes]` | |
| | `ami delete [--svc <svc>] <AMI ID or name> [--force] [--yes]` | |
| `template` | `template show --svc <svc>` | |
| `role` | `role import --svc <svc> <role name or ARN> [--yes]` | |
| | `role delete --svc <svc> [--yes]` | |
| `sg` | `sg list --svc <svc> [--format <columns>]` | |
| | `sg add --svc <svc> <port> [<CIDR>] [--description <description>]` | |
| | `sg remove --svc <svc> <port> [<CIDR>]` | |
| | `sg diff --svc <svc>` | |
| | `sg show --svc <svc>` | |
| `app` | `app list [--svc <svc>] [--format <columns>]` | |
| | `app show --svc <svc> <app ID>` | |
| | `app get --svc <svc> <app ID> <key>` | |
| | `app set --svc <svc> <app ID> <key>=<value>` | |
| | `app unset --svc <svc> <app ID> <key>` | |
| | `app deploy --svc <svc> <app ID or path> [<name>=<value>...] [--reset] [--user <user>] [--bake] [--yes]` | `deploy` |
| | `app remove --svc <svc> <app ID> [--user <user>] [--bake] [--yes]` | |
| `ssh` | `ssh proxy --svc <svc> [<port>] [--user <user>]` | `proxy` |
| | `ssh set-port --svc <svc> <port>... [--yes]` | |

## Parsing

- Command-line options may appear anywhere after `temt`, each at most once.
- Options that take a value (`--svc`, `--profile`, `--region`, `--format`, `--user`,
  `--description`) take the next word as the value. The value must not be empty, except
  that an empty `--description` clears the description.
- Any other word starting with `-` is read as a command-line option; an unknown one is a
  usage error. No positional argument starts with `-`. The `<key>=<value>` of `app set`
  and the `<name>=<value>` of `deploy` are single positional words that start with the
  name, so the value may start with `-`.
- There are no one-letter command-line options.
- Usage errors: the first word other than command-line options is neither a target nor a
  short form; the word after a target is not a verb of that target; a command-line option
  the command does not take; a missing or extra positional argument.

## Help and version

- `--help`: prints the usage to stdout and exits without reading the other arguments.
  If the first word other than command-line options is a target or a short form and
  `--help` comes after it, only the commands of that target are printed (for a short
  form, the target of the command it stands for). Otherwise all commands are printed.
- `--version`: likewise prints the single line `temt <version>` to stdout and exits
  without reading the other arguments. The version is `version` in the `pyproject.toml`
  of the installed temt.
- If both are given, the one that comes first wins.
- `--help` or `--version` right after an option that takes a value is that value.

## Service command

`apply` writes the service command `~/.local/bin/<name>` if it is missing
([local-files.md](local-files.md#service-command)). `<name> ...` is an alias of
`temt --svc <svc> ...`; since shell completion works on command names, the service's
operations can be typed in this form.

- A command that may omit `--svc` behaves, when called through the service command, as
  if `--svc` were given.
- Giving `--svc` again to the service command is a usage error.
- `temt` does not look at the name it was called by.
- `proxy` runs as ssh's ProxyCommand; ssh calls `temt` directly, not through the service
  command ([topics/ssh.md](topics/ssh.md)).

## Common behavior

### Profile and region

- `--profile <profile>` uses that profile for this run only, over `AwsProfile` in the
  config file and the defaults file ([config.md](config.md#profile)). `apply` and
  `ami import` write the value to `AwsProfile`, so later commands use the same profile. It is a usage error for
  the commands that do not call AWS: `service list`, `sg list`, `sg add`, `sg remove`,
  and `app list` without `--svc`.
- `--region <region>` is accepted by the commands that call AWS. With `--svc`, it is
  used only while the config file has no `AwsRegion`; the first `apply` and `ami import`
  write it there, and a `--region` with another value is then a usage error
  ([config.md](config.md#region)). It is a usage error for the commands that do not
  call AWS, as `--profile` is.

### Messages

- Normal output goes to stdout: progress lines, the displays of `status` and the `show`
  commands, the list of what a command will do before its confirmation, and the stdout
  of `install` and `uninstall` during `deploy` and `app remove` (their stderr and ssh's
  go to stderr as they are).
- Warnings, errors, the usage after a usage error, and the `[y/N]` prompt go to stderr,
  so that they stay on screen when stdout goes to a file or a pipe.
- Also on stderr, so that stdout holds only the data: the environment line of the
  commands without `--svc` ([List tables](#list-tables)), and every message of `diff`
  and `sg diff`, `no differences` included (empty stdout means no differences).
- Lines that belong to a warning or an error go to stderr with it: the instances found
  before a too-many or unknown-ID error, the state `bake` leaves before its error, and the newline that closes
  `^C` on an interrupt.
- If stdout is closed at start (`>&-`), messages that would go to stdout go to stderr,
  and data for other programs (tables, `app get`, diff changes, `--help`, `--version`)
  is dropped silently, since nobody reads it. If stderr is closed at start (`2>&-`),
  what would go there is dropped; the exit code is unchanged.
- The stdout of `install` and `uninstall` is read by temt and written to stdout, so that
  a reader that closes early (`| head`) does not cut them off: the rest is dropped and
  they run to the end.
- Everything `proxy` writes goes to stderr, because as the ProxyCommand its stdout is
  the ssh channel.
- Before writing to one stream, the other is flushed, so the order holds on a terminal;
  each write is flushed, so progress shows at once through a pipe. Characters the
  locale cannot encode are written as backslash escapes. If the reader of stdout or
  stderr closes early (`| head`, `2>&1 | head`), the rest of that stream is dropped
  silently and the command goes on. The stderr of ssh, and of `install` and `uninstall`
  through it, is not read by temt, so such a reader may still cut ssh off.
- Progress lines carry the time; error lines carry the program name.
- Wording is English, ASCII only.
- Exception: text that is not temt's own wording is shown as is, Japanese included:
  values from the user's side (config file, ssh config, environment variables,
  arguments, `manifest.toml` values, file names and paths, profile names) and text
  returned by AWS or external commands (botocore exception text, etc.).
  - In these, unprintable characters (control characters, characters that change text
    direction, whitespace other than the ASCII space, etc.) are replaced by `?`, in
    errors, warnings, and progress lines alike.
  - A multi-line AWS error text is joined into one line with `; ` (after a line ending in
    `:`, with a space).
  - Not replaced: values that follow rules set by temt or AWS (AWS IDs, AMI names,
    states, names that passed a rule), and fixed text returned by the OS or Python (the
    reason of an `OSError`, etc.).
  - Exception: `manifest.toml` values and app option values. In messages and in
    `app show`, only the unprintable characters that disturb the terminal are replaced
    ([commands/app.md](commands/app.md#app-options)). In a list table, all unprintable
    characters are replaced, as for other values ([List tables](#list-tables)).
- Data for other programs goes to stdout in UTF-8 regardless of the locale: list tables
  ([List tables](#list-tables)), the value of `app get`
  ([commands/app.md](commands/app.md#app-get)), and the changes of `diff`
  ([commands/service.md](commands/service.md#diff)) and of `sg diff`
  ([commands/sg.md](commands/sg.md#sg-diff)). So do the usage of `--help` and the
  version of `--version`.

### Bytes that cannot be read

Python reads bytes that its encoding cannot decode (in arguments, environment variables
and file names) as lone surrogates. temt treats them the same way everywhere:

- Shown (messages, tables, `app get`): replaced by `?`, like other unprintable
  characters. An `OSError` is shown as its reason and its paths, not as Python's text,
  which writes paths in `repr` form (`\udcff`).
- Output of an external command that temt reads (`ssh-keygen`, `ssh -G`): decoded as
  file names are, and shown with `?`.
- Files temt reads (config files, `manifest.toml`, the ssh config): must be UTF-8;
  otherwise the command stops naming the file. From the `.pub` that `proxy` sends, only
  the key type and body go to AWS; the comment, which may hold such bytes, is not sent.
- Values that would be written to a file or sent to AWS stop the command where they are
  read, before anything changes: `TEMT_CONF_DIR` and `TEMT_SSH_DIR` (no unprintable
  characters), the AMI id or name of `ami delete` (valid UTF-8), and the `app set` value
  and app options (valid UTF-8). Names checked by a rule (services, apps, instance IDs)
  never hold them.
  A file path given as an argument may hold them.
- A `[y/N]` answer that cannot be read is not `y`.
- temt expects a UTF-8 locale (C included, where Python uses UTF-8). Under a locale with
  another encoding, it does not crash, but text it writes to files is UTF-8.

### Exit codes

- 1 for failure, 2 for a usage error.
- On a usage error, the usage follows the error line on stderr: only the commands of the
  target if the first word other than command-line options is a target or a short form
  (for a short form, the target of the command it stands for), otherwise all commands.
- Interrupted by Ctrl-C: says so and exits with 1.

### External commands

Before running, a command checks that the external commands it uses exist; if one is
missing, it exits naming it.

- `ssh-keygen`: `apply` and `diff`, only when the default login key must be created.
- `proxy`: `ssh` (for `ssh -G`), and `aws` and `session-manager-plugin` when
  `TEMT_LOGIN` is `ssm`.
- `ssh`: `deploy` and `app remove`.

If boto3 cannot be imported, every command exits saying so, except those that do not
call AWS (`service list`, `sg list`, `sg add`, `sg remove`, and `app list` without
`--svc`).

### Deadlines

Waiting for AWS or the instance to reach a state is done in stages, each with a deadline
([topics/instance.md](topics/instance.md#waiting)), except `bake` (and `deploy`/
`app remove` with `--bake`, `set-port`, and `ami import`) waiting for the AMI. When a deadline passes, the command
exits saying which stage did not finish. Each stage reached is reported once.

Other waits have no deadline of temt's own: `apply` waiting for `.config.lock` (held
only while one file is rewritten), the ssh sessions of `deploy` and `app remove` (while
`install` or `uninstall` runs), and name resolution in the global IP address lookup.
`apply`'s wait for a new instance profile uses the boto3 waiter's own limit, and its
failure surfaces as the botocore error.

### Missing config file

- A command run with `--svc` other than `apply`, `destroy`, and `ami import` exits,
  prompting to run `apply`, if the config file is missing. `apply` starts from the
  defaults.
- `ami import` requires the config file to be missing: it imports only into a service
  that has none, and with one it exits doing nothing
  ([commands/ami.md](commands/ami.md#ami-import)).
- `destroy` does not prompt `apply`, since it would delete what `apply` makes. It needs
  the config file for the region and profile (`AwsRegion`, `AwsProfile`); if the file is
  missing, it exits
  with `no config file: <path>; destroy needs AwsRegion from it`.

### Not applied

- Commands that assume `apply` has been run (`start`, `bake`, `template show`,
  `set-port`, and the `app` commands with `--svc`) look for the launch template before
  their first operation on AWS, and if it is missing, exit prompting to run `apply`.
  `start` checks the template also when it resumes a stopped instance.
- These do not check: `apply`, `diff`, `destroy`, `stop`, `terminate`, `reboot`,
  `status`, `instance list`, `ami import`, `ami list`, `ami prune`, `ami delete`,
  `role import`, `role delete`, `sg diff`, `sg show`, `proxy`.
  - `status` does not stop; it shows that the service is not applied.
  - `sg show` shows that the next `apply` will create the security group if it is
    missing.
  - `diff` shows missing resources as to be created; `sg diff` shows every rule as to be
    added.

### `--async`

- `start`, `stop`, `terminate`, and `reboot` wait for completion by default. With
  `--async` they wait only until AWS accepts the request (`run-instances`,
  `start-instances`, `stop-instances`, `terminate-instances`, `reboot-instances`).
- Completion is:
  - `start`: the instance can be logged into (the SSM agent reports `Online` after the
    launch); with `WaitForSsm = false`, the instance is `running`
    ([commands/instance.md](commands/instance.md#start)).
  - `stop`: the instance is `stopped`.
  - `terminate`: the instance is `terminated`.
  - `reboot`: the instance has rebooted and can be logged into again (the boot_id is
    read as changed).
- `terminate` cleans up known_hosts even with `--async`.
- `bake` and `destroy` always wait for completion.

### Stale reads

- `describe-instances` lags the request by a few seconds, so a command run right after
  an `--async` request may see the old state. The user confirms with `status` that the
  state has settled before the next operation. The behavior of a state-changing command
  (`apply`, `bake`, `deploy`, etc.) run without that check is not guaranteed.
- `start`, `stop`, `reboot`, and `terminate` are built so that, even if they misread a
  stale state, running them again converges.
- `start` right after `start --async` can launch a second instance. This is made visible
  by a warning in `status`, and is cleaned up by `terminate` with the instance ID.

### Intermediate states

`start`, `stop`, and `reboot` (with `--async`) send their request to AWS even for an
instance in an intermediate state, and let AWS refuse, except where no request is needed
(`start` on `pending`, `stop` on `stopping`). An instance that is shutting down counts
as absent for most commands. The rules are in
[topics/instance.md](topics/instance.md#intermediate-states).

### Confirmation

- Before changing anything, these list the targets and ask for confirmation with
  `[y/N]`: `apply`, `stop`, `terminate`, `reboot`, `bake`, `destroy`, `ami import`,
  `ami prune`, `ami delete`, `role import`, `role delete`, `set-port`, and `deploy` and
  `app remove` with `--bake`.
- `apply` ends without asking if there are no differences.
- The list of targets goes to stdout and the prompt to stderr; `stop`, `terminate`, and
  `reboot` print no separate list and name their targets in the prompt itself
  (`terminate i-... (running)? [y/N]`). The answer is read from stdin. Any answer other than `y`
  or `Y` ends with exit code 1 without doing anything.
- `--yes` skips the confirmation. If stdin is not a terminal and `--yes` is not given,
  the command exits saying `--yes` is needed.

### Locks

`start`, `stop`, `terminate`, `reboot`, `bake`, `destroy`, `ami import`, `set-port`,
`deploy`, and `app remove` take the per-service lock before acting. `ami prune` takes the lock of an AMI's service each
time it deletes one AMI ([topics/instance.md](topics/instance.md#locks)).

## List tables

`service list`, `instance list`, `ami list`, `app list`, and `sg list` follow docker:

- The first line is the column names (the header); from the second line on, one item per
  line.
- Columns are aligned with spaces; the last column is not padded.
- A header is one uppercase English word without spaces.
- A missing value is shown as `-`. With no items, only the header is printed.
- Unprintable characters, such as newlines and tabs, are replaced by `?`.
- The table is written to stdout in UTF-8, while the environment line and warnings go to
  stderr, so the table alone can be piped to `grep` or `awk`.
- If the reader closes early (`| head`, etc.), the command stops writing silently.
- They only display; they take no lock.

Values:

- Values temt builds contain no spaces, except times.
- A time is the local time to the second in the form `2026-09-20 12:00:00`. Times shown
  outside tables use the same form, except the time stamp at the head of a progress
  line, which is `12:00:00`.
- Launch template versions are written `<template name>:<version>`, joined by `,` when
  there are several (`temt-web-template:3,mine:1`). A template name can contain neither
  `:` nor `,`, so the separators cannot be mistaken.
- Values from AWS or the config file (AMI names, tag values, `ImageId`, etc.) are printed
  as written, so they may contain spaces. When splitting columns with `awk`, expect a
  time column to split into date and time, or put time columns and such columns last
  with `--format`.

`--format <column>[,<column>...]` selects the columns and their order.

- Columns are named by their header, case-insensitively. An unknown, repeated, or empty
  name is a usage error that lists the available names.
- Some columns are output by default; others only when chosen with `--format`.
- Column names are checked before calling AWS. AWS is not called for columns that are
  not chosen.
