# app commands

The `app` commands handle, from the client, the apps that run on a service's instance and
their values. `app list` looks at the local apps instead. Values live in the SSM
parameters `/<name>/<app ID>/<key>`; what is installed lives on the instance.

`app list`, `app show`, `app get`, `app set`, and `app unset` only display or rewrite one
value, so they take no lock. `deploy` and `app remove` take the lock
([topics/instance.md](../topics/instance.md#locks)).

An app's contents (`manifest.toml`, `install`, `uninstall`, and the rest) and everything
under `/opt/temt/apps` on the instance are trusted as their writers wrote them: the app's
author and the instance's root. temt checks them to report mistakes clearly, not to defend
against malicious contents. The time a `pattern` takes to match, a broken tar.gz read from
the instance, and whatever `install` does are the writer's responsibility.

## Deploy record

The deploy record is the file `temt-deploy.json` directly under the app directory on the
instance, `/opt/temt/apps/<app ID>/temt-deploy.json`, written by `deploy`. It is a JSON
object with these fields:

| Field | Value |
| --- | --- |
| `user` | The user that runs `install` |
| `deployed` | When `deploy` sent the app, by the client's clock, in UTC as `2026-09-20T03:00:00Z` |
| `instance` | ID of the instance it was sent to |
| `options` | The options used by the install that came from the command line or the previous record: a JSON object from option name to value. Values filled from the default are not written; with none, the field is omitted |

The app name and version are those of `manifest.toml` in the same directory. The record is
sent with the directory and swapped with it in one step, so it always describes the files
in place: when `install` fails, the record of the failed run stays with the new files.
It is baked into the AMI with the directory, so what the AMI has is read from the
instance launched from it, and nothing outside the instance is rewritten by `bake` or
lost by an interrupt. An app is baked when the record's `instance` is not the instance it
is read on: it came from the AMI. When `instance` is that instance, the app was deployed
there and is not baked yet. Rejected: keeping the records in SSM parameters, which
`bake` had to rewrite after pointing the template and which disagreed with the AMI when
it stopped partway; and deciding baked from `deployed` and the AMI's creation time,
which takes an app lost by terminating before baking for a baked one.

The record is written as UTF-8, without `\u` escapes for Japanese and other non-ASCII
text, owned by root with mode 644 like `temt-options.env`. A record that is not a JSON
object, whose `user`, `deployed`, or `instance` is not a string, or whose fields contain a
value that cannot be written as UTF-8 (a `\u` escape of an unpaired surrogate), is an
unreadable record. If `options` is malformed (not a mapping from name to value, a name
that breaks the option name rule, or a value with a control character), only that field
is unreadable. Other keys are ignored.

Apps deployed by temt before the record moved onto the instance have no record; their
records in SSM (`/<name>/<app ID>/deploy`) are no longer read. Deploying the app again
writes the record (the requester); the old parameter is an ordinary app value now, deleted
with `app unset` or by `destroy`.

The key `status` is the app's current state, written by the app from the instance and
shown by `status` ([instance.md](instance.md#status),
[apps.md](../../docs/apps.md#アプリの状態)).

## Reading the apps on the instance

`status --apps`, `app show`, `deploy`, and `app remove` read `manifest.toml` and
`temt-deploy.json` of the apps on the running instance as root: of the directories
directly under `/opt/temt/apps/` for `status --apps`, of the one app for the others.
Directories whose names break the app ID rule are skipped.

- `deploy` and `app remove` read over ssh, as they connect anyway ([Steps](#steps)
  step 2): after confirming that ssh reaches the instance found.
- `status --apps` and `app show` only display, so they read with Run Command
  (`AWS-RunShellScript`, 30 seconds each to deliver and to run, given up after their
  sum and one polling interval). An ssh session counts as use for `idle-shutdown`
  ([apps/idle-shutdown.md](../apps/idle-shutdown.md)), so reading over ssh would put off
  the idle shutdown (the requester); Run Command does not open a login session. They
  read only when exactly one `running` instance is found (excluding `shutting-down`)
  and its SSM agent is `Online`; otherwise they show why they did not read (no
  instance, not running, two or more, the agent not online or not checkable). The
  read files are sent back as a base64 tar.gz; an output over the 24000 characters
  that `get-command-invocation` returns is not read. If the command cannot be sent
  (including an AWS error), fails, or does not finish in time, they warn and go on. The local credentials need
  `ssm:SendCommand`, `ssm:GetCommandInvocation`, and
  `ssm:DescribeInstanceInformation`, as `reboot` does.

 A `manifest.toml` that cannot be read is warned about and its
values are left empty; an unreadable record is warned about and its values are left
empty, except where `deploy` and `app remove` say otherwise.

## App IDs and keys

An app ID or key starts with a lowercase letter, continues with lowercase letters,
digits, and hyphens, ends with a lowercase letter or digit, and has at most 64
characters. Hierarchies with `/` are not allowed. A name that breaks the rule is a usage
error, detected before calling AWS.

## `app list`

`app list [--svc <svc>] [--format <columns>]`

Lists the apps that `deploy` can use by app ID (those in `apps/` of the conf directory and
the bundled apps), one row per app ordered by app ID, and calls no AWS API. With `--svc`
the list is the same, since local apps do not depend on the service; it is accepted so
that the service command (`temt-<svc> app list`) works. What is installed is shown by
`status --apps` ([instance.md](instance.md#status)), which needs the running instance
(the requester): the local list is always available, so it is not mixed with it.

In both `apps/` of the conf directory and the bundled apps' location, entries that are not
directories (including broken links) and names that break the app ID rule are not listed.
The values come from the app that `deploy` uses for that app ID (the one in `apps/` of the
conf directory if present, otherwise the bundled one). If a `manifest.toml` cannot be
read, it warns and leaves those values empty.

| Column | Default | Value |
| --- | --- | --- |
| `ID` | yes | App ID |
| `NAME` | yes | App name in `manifest.toml` |
| `LOCAL` | yes | Version in `manifest.toml` |
| `PATH` | yes | Directory of the app. Long for a bundled app, which lies inside temt's install; rejected: showing `(bundled)` instead, which hides where the source is |
| `DESCRIPTION` | | Description in `manifest.toml` |

## `app show`

`app show --svc <svc> <app ID>`

Prints the deploy record, the declared options (below), and the list of keys directly
under the app's hierarchy (key, last modified time), in this order. Values are not shown.
Keys that break the key rule are listed too.

The record is read from the instance found by the same rule as `start`
([topics/instance.md](../topics/instance.md#finding)), as in
[Reading the apps on the instance](#reading-the-apps-on-the-instance): the app name and
version from its `manifest.toml`, and the record's `user`, `deployed`, `options`, and
whether it is baked (as in [Deploy record](#deploy-record)). Without the directory, it
says the app is not deployed on that instance. When it does not read, it says that the
record was not read and why, and goes on. The baked version and its options are not shown: they are what the
AMI has, read from an instance launched from it. `options` is shown as `<name>=<value>`
separated by spaces, each value quoted for the shell when needed so it can be typed on a
command line as is; a malformed field is shown as unreadable.

It lists the options that the app's `manifest.toml` declares (name, default,
description): the instance's when it was read, otherwise the local one that `deploy`
would use by this app ID. Defaults are quoted like values; an option without a default
is shown as required. If the `manifest.toml` cannot be read, it warns.

The record, the instance's `manifest.toml`, and the keys can be written from the
instance, so unprintable characters such as newline and ESC are replaced by `?`. Option
values, and the version, defaults, and descriptions in `manifest.toml`, have only the
characters that disturb the terminal replaced ([App options](#app-options)).

If the app is not on the instance (or the instance could not be read), has no value, and
there is no app that `deploy` can use by this app ID, it takes the app ID as a typo and
exits with an error.

## `app get`

`app get --svc <svc> <app ID> <key>`

Writes one value to stdout as is, in UTF-8: no newline is added, and newlines and
non-ASCII characters are written unchanged. Exits with an error if the value does not
exist. A `SecureString` value read without decryption is ciphertext, so it is not
written; the command exits with an error.

## `app set`

`app set --svc <svc> <app ID> <key>=<value>`

Writes one value as `String`, creating it if absent and overwriting it otherwise. The key
and value come as one word, split at the first `=`; the value may contain `=`. The value
is taken unprocessed, including a value starting with `-`. Usage errors:

- A word without `=`.
- An empty value, because SSM does not accept it.
- A value that is not valid UTF-8, because `app get` could not write it.

If the existing value is not a `String`, an overwrite cannot change its type, so it tells
the user to run `app unset` first and exits. Whether the app is deployed is not checked
(the requester): it is known only on the running instance, and a value written for a
mistyped app ID shows in the keys of `app show` and is deleted with `app unset`. It
reports whether it created or overwrote the value.

## `app unset`

`app unset --svc <svc> <app ID> <key>`

Deletes one value without confirmation. If the value does not exist, it says so and does
not treat it as an error.

## `deploy`

`app deploy --svc <svc> <app ID or path> [<name>=<value>...] [--reset] [--user <user>]
[--bake] [--yes]` (short form `deploy`)

Installs an app on the running instance. It does not touch the role's policies: the
permissions an app uses are written in `[[role.<policy name>]]` of the config file
and attached by `apply` ([conf.md](../../docs/conf.md#role),
[resources.md](../resources.md#instance-profile-and-instance-role)). temt does not create the AWS resources an app
uses, such as S3 buckets, either. An app is one directory:

```
<app ID>/
    manifest.toml  app name `name` and version `version` (required); description
                   `description`, the user `user` that runs install, and option
                   declarations [options.<name>] (optional)
    install        bash script that installs. Required
    uninstall      bash script that removes. Optional; without it, app remove
                   cannot remove the app
    ...            files install uses. temt-options.env and temt-deploy.json are
                   added by temt, so the app cannot have them
```

### Users

The instance user that runs `install` is the `--user` value, else `user` in
`manifest.toml`, else root. `uninstall` runs as the recorded `user` (see
[`app remove`](#app-remove) step 1). While the instance has the app with a record,
`deploy` refuses another user, naming the recorded one: the new user's `uninstall` would
not undo what the old user's `install` did. To change the user, remove the app first. A
user name starts with a lowercase letter or `_`,
continues with lowercase letters, digits, `_`, and `-`, and has at most 32 characters; a
name that breaks the rule is a usage error or an error in `manifest.toml`. A non-root user
runs the script through `sudo` as that user with its home, so `HOME` is that user's home
and `PATH` is sudo's `secure_path`; such an `install` runs `sudo` itself for operations
that need root. Root runs the script under `sudo` without a password prompt.

### Writing install and uninstall

temt runs `install` and `uninstall` with the app directory as the current directory, so
they reach their own files by relative paths (the requester). A user that can read the
script can enter its directory, so the directory is always enterable. `install` must be
idempotent. Daemons are started by systemd or the like; if `install` backgrounds a
process, it redirects the output to `/dev/null`. A process left holding `install`'s output
keeps ssh from ending, and `deploy` hangs holding the lock. Files are sent with group and
other write, setuid, setgid, and sticky bits cleared.

### Locating the app

An argument containing `/` is a path; otherwise it is an app ID. For a path, that
directory is used and its last component is the app ID; symlinks are not resolved when
taking the name. For an app ID, it looks for `apps/<app ID>/` in the conf directory
([local-files.md](../local-files.md#files-in-the-users-home)) and otherwise uses the
bundled app. A user can override a bundled app by placing an app with the same ID. If
`apps/<app ID>` is not a directory (a file or a broken link) and a bundled app exists, it
warns and uses the bundled app. If neither exists, it lists the usable app IDs, suggests
a close name if there is one, and exits. The app ID rule is that of
[App IDs and keys](#app-ids-and-keys).

### Steps

1. With `--bake`, if the config file (with the defaults file) writes `ImageId`, the AMI
   is fixed, so stop with a usage error before doing anything else, as
   [`bake`](ami.md#bake) does.

   Locate the app directory and report where it came from; for a link, add its target.
   Read `manifest.toml`, and check that `install` exists and that neither
   `temt-options.env` nor `temt-deploy.json` does.
   Check the app options given on the command line against the declarations
   ([App options](#app-options)). Up to here, nothing touches AWS or the instance.
2. Take the lock, check that the launch template exists, and find the instance by the
   same rule as `start` ([topics/instance.md](../topics/instance.md#finding)). Unless
   exactly one `running` instance is found, exit: with none, suggest `start`; if
   `pending`, suggest waiting for `start` to finish; if stopped or stopping, suggest
   resuming with `start`; with two or more, list them and stop as `start` does. It never
   launches or resumes an instance itself.

   Connect with `ssh -T <name>`, passing the region and profile decided for this run to
   `proxy` ([ssh.md](ssh.md#region-and-profile)), and read the instance ID from inside
   the instance with `sudo` to confirm that it reached the instance found. If it cannot
   connect, `sudo` is not usable, or it reached another instance, exit without changing
   anything. ssh may not call `proxy` at all: the user's ssh config can route the
   connection elsewhere, and a `ControlMaster` connection of the user is reused, even
   one opened to another region's instance
   ([topics/ssh.md](../topics/ssh.md#regions)). The `ProxyCommand`'s waiting notices
   and ssh's errors go to stderr as they are.

   Read the previous record of the app from the instance
   ([Reading the apps on the instance](#reading-the-apps-on-the-instance)). If the
   reading itself fails, exit without changing anything, even with `--reset`. If it has a `user` other than the one that would run
   `install`, exit without changing anything ([Users](#users)).

   If the user that runs `install` is not root, check that the user exists on the
   instance; if not, exit without changing anything.

   Decide the option values to use, and report each on one line with its source, and
   prepare the directory for sending with `temt-options.env` and the record:
   `user` is the user that runs `install`, `deployed` is now by the client's clock in
   UTC, `instance` is the ID of the instance, and `options` is the values decided here
   whose source is `given` or `kept`.

   With `--bake`, run the checks of `bake` step 3 ([ami.md](ami.md#bake)); then list
   the install and what `bake` will do, and ask for confirmation, with the region and
   account ([cli.md](../cli.md#confirmation)).
3. Send the directory over ssh and, as root, replace `/opt/temt/apps/<app ID>/` with it
   in one step: on failure partway the old directory remains, and leftovers of an
   earlier interrupted run are cleaned up. Then run `bash /opt/temt/apps/<app ID>/install`
   over ssh as the user, with the app directory as the current directory. ssh's output goes to stdout
   and stderr as it is.
4. With `--bake`, bake as `bake` does after its confirmation ([ami.md](ami.md#bake)),
   holding the same lock; only that region's launch template gets the new AMI.
   Without it, report that the app will not be in the next instance until baked.

The whole directory is sent, including hidden files. Every symlink, the app directory
itself and those inside, is followed and sent as a copy of its target, so the instance
gets regular files and directories. A broken symlink or a symlink loop stops `deploy`
with the error from reading it, before anything is changed. Stopping ssh with Ctrl-C may leave
`install` running on the instance.

Nothing is rolled back on failure. A failure up to step 2 changes nothing. If sending
fails in step 3, the old directory remains with its record. If `install` fails, the new
directory remains with its record, so a retry carries over the options given to the
failed run. Either way, running `deploy` again converges. On failure nothing is baked
even with `--bake`.

## App options

An app declares the options whose values `deploy` can change in `[options.<name>]` of
`manifest.toml`. The name rule is that of app IDs. The keys are the following; any other
key is an error in `manifest.toml`.

| Key | Required | Contents |
| --- | --- | --- |
| `description` | yes | Description: a non-empty string without control characters. Shown by `app show` and in value errors |
| `default` | no | Default value: a string, possibly empty. Without it, the option has no default, and every `deploy` needs a value from the command line or the previous record |
| `pattern` | no | A Python regular expression the whole value must match. A `default` that does not match is an error in `manifest.toml` |

A value is a string and may contain anything except control characters (Unicode Cc:
newline, tab, ESC, etc.). There is no length limit.
`<name>=` on the command line means "back to the default", so an empty value cannot be
given from the command line.

On the command line, `<name>=<value>` words follow the app. Each is split at the first
`=`, and the value is not processed. Usage errors: a word without `=`, a name that breaks
the name rule, a repeated name, a value with a control character, and a value that is not
valid UTF-8. A `<name>=<value>` placed before the app (a first word without `/` that
contains `=`) is also a usage error, with a message saying so. An undeclared name and a
value that does not match `pattern` make step 1 exit, with the closest name or the
description added.

The value used is decided per declared option in this order:

1. `<name>=<value>` on the command line (source `given`).
2. The value in the previous record's `options` (`kept`): the record on the instance,
   which is the one baked into the AMI unless the app was deployed again on this
   instance. With `--reset`, nothing is carried over. `<name>=` (empty value) stops that
   name from being carried over.
3. The declaration's `default` (`default`). An option without a default that still has
   no value here makes `deploy` exit. `<name>=` for an option without a default makes
   step 1 exit.

The record's `options` holds only the values decided by 1 and 2. The report line names
the option, the value (quoted for the shell when needed), and the source.
Depending on the previous record:

| Previous record | Behavior |
| --- | --- |
| None (first `deploy`, after `app remove`, or deployed by an older temt) | Nothing is carried over |
| Unreadable, or its `options` is malformed | Exit without changing anything, telling the user to give the options again with `--reset`. If the app declares no options, nothing would be carried over, so it does not stop; a malformed `options` is discarded with a warning |
| Has a name the app does not declare | Warn, drop it, and continue. It disappears from the record too |
| A carried-over value does not match `pattern` | Exit without changing anything, telling the user to give `<name>=<value>`, or `<name>=` if there is a default |

The values used, for all declared options, are passed to `install` in the file
`temt-options.env`, added directly under the app directory. It is sent owned by root
with mode 644, so the values never pass through a command line. One line per
option, in declaration order, of the form `TEMT_OPTION_<NAME>=<value>`: the variable name
is the option name upper-cased with `-` turned into `_`. Values are quoted for the shell,
so sourcing the file with bash's `.` expands nothing. The first line is a
comment with the app ID and version. Apps that declare no options do not get the file.
It is swapped together with the directory and stays in the AMI when baked, so
`uninstall` can read the same values.

When option values, and the version, descriptions, and defaults from `manifest.toml`,
are shown in messages or by `app show`, only the characters that disturb the terminal are
replaced by `?` (in `list` tables every unprintable character is replaced, as for other
values; [cli.md](../cli.md#list-tables)): control characters (Unicode Cc), unpaired
surrogates (Cs), line and paragraph separators (Zl, Zp), and bidirectional controls
(U+061C, U+200E, U+200F, U+202A-202E, U+2066-2069). Ideographic spaces, emoji joined with
zero-width joiners, and the like are shown as is. Values contain no control characters,
so the only values whose display cannot be typed back as is are those with line or
paragraph separators or bidirectional controls.

The record and `temt-options.env` stay in the AMI readable by anyone on the instance, so
options must not carry secrets.

## `app remove`

`app remove --svc <svc> <app ID> [--user <user>] [--bake] [--yes]`

Removes an app from the running instance. The instance requirements are those of
[`deploy`](#deploy) step 2. The `[[role.<policy name>]]` written for the app is
removed from the config file and `apply`ed by the user after removing the app.

1. With `--bake`, if the config file (with the defaults file) writes `ImageId`, stop
   with a usage error before doing anything else, as in `deploy` step 1.

   Confirm, as in `deploy` step 2, that ssh reaches the instance found. Check that
   `/opt/temt/apps/<app ID>/` and its `uninstall` exist on the instance. If the
   directory does not exist, say the app is not deployed on that instance and exit. If
   `uninstall` does not exist, exit without changing anything.

   The user that runs `uninstall` is the `--user` value, else the record's `user`. If
   there is no record (deployed by an older temt) or it is unreadable or has no `user`,
   warn and use the `user` of the instance's `manifest.toml`, else root. If the record's
   `user` breaks the user name rule, exit telling the user to give it with `--user`,
   without showing the value. If the user is not root, check that the user exists on the
   instance as in `deploy` step 2.

   With `--bake`, run the checks of `bake` step 3 and ask for confirmation as in
   `deploy` step 2 (with the region and account), listing the user.
2. Run `bash /opt/temt/apps/<app ID>/uninstall` as that user with the app directory as
   the current directory, and on success delete the directory, with its record, as root.
   On failure, exit. Stopping with Ctrl-C may leave `uninstall` running on the instance.
3. Delete the status `/<name>/<app ID>/status`, because otherwise `status` keeps
   showing the old state of the removed app. Other values of the app and the role's
   policies are not deleted.
4. With `--bake`, bake as in `deploy` step 4. Without it, report that the app was
   removed from that instance and that baking removes it from the next instance too.

Nothing records the removal outside the instance: an app removed without baking comes
back with the AMI's version in the next instance (the requester).

## Clones in other regions

SSM parameters are per region, so a clone in another region
([config.md](../config.md#clones)) has none of the app values of the region it was copied
from. temt does nothing about it (the requester); they are set again with `app set`. The
deploy records are in the copied AMI, so `status --apps` and `app show` show the apps
baked into it, as baked.

## Bundled apps

Kept in `src/temt/apps/<app ID>/` and shipped in the package. `deploy` uses one when the
app ID given is not in `apps/` of the conf directory.

| App ID | Contents |
| --- | --- |
| `idle-shutdown` | Powers off when nobody is using the instance ([apps/idle-shutdown.md](../apps/idle-shutdown.md)) |
| `swapfile` | Uses `/swapfile` as swap ([apps/swapfile.md](../apps/swapfile.md)) |
| `timezone-tokyo` | Sets the time zone to Asia/Tokyo ([apps/timezone-tokyo.md](../apps/timezone-tokyo.md)) |
