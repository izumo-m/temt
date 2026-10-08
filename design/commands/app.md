# app commands

The `app` commands handle, from the client, the values and records of the apps that run
on a service's instance. Only `app list` without `--svc` looks at the local apps instead.
Values live in the SSM parameters `/<name>/<app ID>/<key>`.

`app list`, `app show`, `app get`, `app set`, and `app unset` only display or rewrite one
value, so they take no lock. `deploy` and `app remove` take the lock
([topics/instance.md](../topics/instance.md#locks)).

## Deploy record

The key `deploy` is the record of installing the app, written by temt. It is a JSON
object with these fields:

| Field | Written by | Value |
| --- | --- | --- |
| `name` | `deploy` | App name |
| `version` | `deploy` | The version installed last |
| `user` | `deploy` | The user that ran `install` |
| `deployed` | `deploy` | When the install finished, in UTC as `2026-09-20T03:00:00Z` |
| `instance` | `deploy` | ID of the instance it was installed on. `bake` removes it, so it is present only while the version installed last is not baked yet |
| `removed` | `app remove` | ID of the instance the app was removed from without baking, while the app is still in the template's AMI ([`app remove`](#app-remove)) |
| `options` | `deploy` | The options used by the last install that came from the command line or the previous record: a JSON object from option name to value. Values filled from the default are not written; with none, the field is omitted |
| `baked_version` | `bake` | The version baked into the AMI |
| `baked_image` | `bake` | ID of the newest AMI that has that version. Re-pointed on every rebake |
| `baked_options` | `bake` | The `options` of the baked version. With none, the field is omitted |

All fields other than `options` and `baked_options` are strings. The record is written as
UTF-8, without `\u` escapes for Japanese and other non-ASCII text. But SSM rejects a value
containing `{{` as a reference to another parameter, so wherever `{` is followed by `{`
inside a string, the first `{` is written as `\u007b`.

The record is an SSM parameter, so anyone can rewrite it. A record that is not a JSON
object, or whose fields contain a value that cannot be written as UTF-8 (a `\u` escape of
an unpaired surrogate), is an unreadable record. Other keys are not checked; they are
dropped when the record is rewritten. If `options` or `baked_options` is malformed (not a
mapping from name to value, a name that breaks the option name rule, or a value with a
control character), only that field is unreadable; when the record is rewritten, the
value as parsed from JSON is written back unchanged.

`deploy` carries over `baked_version`, `baked_image`, and `baked_options` from the
previous record, because the AMI keeps the previous version until the next bake. It
writes `instance` even when reinstalling the same version, because `install` or the app's
files may have changed. It does not carry over `removed`.

`app remove` keeps the record only while the app is still in the template's AMI, marked
with `removed`, so that `status` and `app list` can show that baking is needed to drop
it. Otherwise it deletes the record. While the instance in `removed` exists and the
template still points at `baked_image`, the app is `removed`. If that instance
terminates before a bake, the removal is lost: the next
instance from the AMI has the baked version again, so the record counts as baked, and
the next rewrite (`bake` or `deploy`) drops `removed`.

The key `status` is the app's current state, written by the app from the instance and
shown by `status` ([instance.md](instance.md#status),
[apps.md](../../docs/apps.md#アプリの状態)).

## App IDs and keys

An app ID or key starts with a lowercase letter, continues with lowercase letters,
digits, and hyphens, ends with a lowercase letter or digit, and has at most 64
characters. Hierarchies with `/` are not allowed. A name that breaks the rule is a usage
error, detected before calling AWS. `app set` and `app unset` reject the key `deploy` as
a usage error.

## `app list`

`app list [--svc <svc>] [--format <columns>]`

With `--svc`, it lists one row per app: the apps that have a `deploy` record under
`/<name>`, together with the apps that `deploy` can use by app ID (those in `apps/` of
the conf directory and the bundled apps). Rows are ordered by app ID. If a record is
unreadable ([Deploy record](#deploy-record)) or a `manifest.toml` cannot be read, it
warns and leaves those values empty. In both `apps/` of the conf directory and the
bundled apps' location, entries that are not directories (including broken links) and
names that break the app ID rule are not listed. The local app's values come from the app
that `deploy` uses for that app ID (the one in `apps/` of the conf directory if present,
otherwise the bundled one).

| Column | Default | Value |
| --- | --- | --- |
| `ID` | yes | App ID |
| `NAME` | yes | App name: the record's, or the `manifest.toml`'s if the record has none or is unreadable |
| `VERSION` | yes | The version installed last (the record's `version`) |
| `DEPLOYED` | yes | When the install finished |
| `STATE` | yes | Whether the version installed last is baked (table below) |
| `BAKED` | yes | The version in the AMI the launch template points to: `baked_version` when `baked_image` is that AMI |
| `LOCAL` | yes | Version in the local app's `manifest.toml` |
| `PATH` | | Directory of the local app |

`STATE` is decided from the record, the `ImageId` of the launch template's default
version, and the instance found by the same rule as `start`
([topics/instance.md](../topics/instance.md#finding)). A `shutting-down` instance counts
as absent.

| `STATE` | Condition | Meaning |
| --- | --- | --- |
| `removed` | `removed` is set, that instance exists, and `baked_image` is the template's AMI | Removed from that instance without baking; still in the template's AMI. Checked before the rows below; if `apply` switched the AMI, the rows below give `lost` |
| `unbaked` | `instance` is set and that instance exists | The version installed last exists only on that instance |
| `lost` | `instance` is set and that instance does not exist | The instance terminated before baking; the version installed last is gone |
| `baked` | No `instance`, and `baked_image` is the template's AMI | The version installed last is in the AMI |
| `lost` | No `instance`, and `baked_image` is not the template's AMI | Not in the AMI, e.g. because `apply` switched to another AMI |
| `-` | Neither is set | A record written by a `deploy` from before baking was recorded |

Without `--svc`, it lists only the apps that `deploy` can use by app ID, by the same
rules, and calls no AWS API. `VERSION`, `DEPLOYED`, `STATE`, and `BAKED` are per-service
values, so they are not columns.

| Column | Default | Value |
| --- | --- | --- |
| `ID` | yes | App ID |
| `NAME` | yes | App name in `manifest.toml` |
| `LOCAL` | yes | Version in `manifest.toml`; same as `LOCAL` with `--svc` |
| `PATH` | yes | Directory of the app; same as `PATH` with `--svc` |
| `DESCRIPTION` | | Description in `manifest.toml` |

## `app show`

`app show --svc <svc> <app ID>`

Prints the `deploy` record, the declared options (below), and the list of keys
other than `deploy` directly under the app's hierarchy (key, last modified time), in
this order. Values are not shown. Without a record, it says the app is not deployed
(`app <app ID>: not deployed`). Keys that break the key rule are listed too. The
record's `options` and `baked_options` are shown as `<name>=<value>` separated by
spaces, each value quoted for the shell when needed so it can be typed on a command line
as is; a malformed field is shown as `(unreadable)`.

If there is an app that `deploy` can use by this app ID, it lists the options its
`manifest.toml` declares (name, default, description). Defaults are quoted like values;
an option without a default shows `(required)`. If the `manifest.toml` cannot be read, it
warns.

The record and the keys can be written from the instance, so unprintable characters such
as newline and ESC are replaced by `?`. Option values, and the `manifest.toml` version,
defaults, and descriptions in the declared options, have only the characters that disturb
the terminal replaced ([App options](#app-options)).

If there is no record, no value, and no app that `deploy` can use by this app ID, it
takes the app ID as a typo and exits with an error.

## `app get`

`app get --svc <svc> <app ID> <key>`

Writes one value to stdout as is, in UTF-8: no newline is added, and newlines and
non-ASCII characters are written unchanged. Exits with an error if the value does not
exist. A `SecureString` value read without decryption is ciphertext, so it is not
written; the command exits with an error. It can also read the `deploy` record.

## `app set`

`app set --svc <svc> <app ID> <key>=<value>`

Writes one value as `String`, creating it if absent and overwriting it otherwise. The key
and value come as one word, split at the first `=`; the value may contain `=`. The value
is taken unprocessed, including a value starting with `-`. Usage errors:

- A word without `=`.
- An empty value, because SSM does not accept it.
- A value that is not valid UTF-8, because `app get` could not write it.

If the existing value is not a `String`, an overwrite cannot change its type, so it tells
the user to run `app unset` first and exits. For an app without a `deploy` record it
writes nothing and exits with an error. It reports whether it created or overwrote the
value.

## `app unset`

`app unset --svc <svc> <app ID> <key>`

Deletes one value without confirmation. Works even without a `deploy` record. If the
value does not exist, it says so and does not treat it as an error.

## `deploy`

`app deploy --svc <svc> <app ID or path> [<name>=<value>...] [--reset] [--user <user>]
[--bake] [--yes]` (short form `deploy`)

Installs an app on the running instance. It does not touch the role's policies: the
permissions an app uses are written in `[[role.policy.<policy name>]]` of the config file
and attached by `apply` ([conf.md](../../docs/conf.md#role),
[resources.md](../resources.md#iam-role)). temt does not create the AWS resources an app
uses, such as S3 buckets, either. An app is one directory:

```
<app ID>/
    manifest.toml  app name `name` and version `version` (required); description
                   `description`, the user `user` that runs install, and option
                   declarations [options.<name>] (optional)
    install        bash script that installs. Required
    uninstall      bash script that removes. Optional; without it, app remove
                   cannot remove the app
    ...            files install uses. temt-options.env is added by temt, so the
                   app cannot have it
```

### Users

The instance user that runs `install` is the `--user` value, else `user` in
`manifest.toml`, else root. `uninstall` runs as the recorded `user` (see
[`app remove`](#app-remove) step 1). A user name starts with a lowercase letter or `_`,
continues with lowercase letters, digits, `_`, and `-`, and has at most 32 characters; a
name that breaks the rule is a usage error or an error in `manifest.toml`. A non-root user
runs the script through `sudo -n -H -u <user>`, so `HOME` is that user's home and `PATH`
is sudo's `secure_path`; such an `install` runs `sudo` itself for operations that need
root. For root, the script runs directly inside the ssh session's `sudo -n`.

### Writing install and uninstall

`install` and `uninstall` find their own files relative to the script's directory and do
not depend on the current directory. temt runs them with `/` as the current directory,
because the running user cannot necessarily enter any other directory. `install` must be
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
warns and uses the bundled app. If neither exists, it lists the usable app IDs, adds a
close name as `did you mean` if there is one, and exits. The app ID rule is that of
[App IDs and keys](#app-ids-and-keys).

### Steps

1. Locate the app directory and report where it came from; for a link, add its target.
   Read `manifest.toml`, and check that `install` exists and `temt-options.env` does not.
   Check the app options given on the command line against the declarations
   ([App options](#app-options)). Up to here, nothing touches AWS or the instance.
2. Take the lock, check that the launch template exists, and find the instance by the
   same rule as `start` ([topics/instance.md](../topics/instance.md#finding)). Unless
   exactly one `running` instance is found, exit: with none, suggest `start`; if
   `pending`, suggest waiting for `start` to finish; if stopped or stopping, suggest
   resuming with `start`; with two or more, list them and stop as `start` does. It never
   launches or resumes an instance itself.

   Connect with `ssh -T <name>` and read `/var/lib/cloud/data/instance-id` with `sudo -n`
   to confirm that it reached the instance found. If it cannot connect, `sudo` is not
   usable, or it reached another instance, exit without changing anything. This is
   needed because the `proxy` in ssh's `ProxyCommand` does not receive `--profile`; it
   looks up the instance with `AwsProfile` of the config file, or the profile chosen by
   AWS's default rules ([commands/ssh.md](ssh.md), [config.md](../config.md#profile)).
   The error adds no hint about `--profile`: the user docs say it does not reach ssh,
   and a hint could not tell `AWS_PROFILE` from `AwsProfile` for the user. The
   `ProxyCommand`'s waiting notices and ssh's errors go to stderr as they are.

   If the user that runs `install` is not root, check with `id -u` that the user exists on
   the instance; if not, exit without changing anything.

   Read the previous record, decide the option values to use, and report each on one
   line with its source. Check that the record fits in an SSM parameter, and pack the
   directory into a tar.gz. If the record cannot be read from SSM (no permission, etc.),
   exit without changing anything, even with `--reset`.

   With `--bake`, check as in `bake` step 3 ([ami.md](ami.md#bake)) that the template
   matches the config file and that the instance's root volume is not larger than
   `VolumeSize`; then list the install and what `bake` will do, and ask for
   confirmation.
3. Connect with `ssh -T <name>` and stream the tar.gz to stdin. On the instance, with
   `sudo`, extract it into a temporary directory under `/opt/temt/apps/` and swap it with
   `/opt/temt/apps/<app ID>/`. On failure partway, the temporary directory is removed and
   the old directory remains. Temporary directories left by an earlier interrupted run
   are removed first. Then connect again and run `bash /opt/temt/apps/<app ID>/install`
   as the user, with `/` as the current directory. ssh's output goes to stdout and
   stderr as it is.
4. Write the record `/<name>/<app ID>/deploy`: `user` is the user that ran `install`,
   `deployed` is when `install` finished by the client's clock, in UTC, `instance` is the ID of
   the instance, and `options` is the values decided in step 2 whose source is `given` or
   `kept`. Carry over `baked_version`, `baked_image`, and `baked_options` from the
   previous record.
5. With `--bake`, do `bake` steps 5-9 ([ami.md](ami.md#bake)) holding the same lock.
   Without it, report that the app will not be in the next instance until baked.

The whole directory is sent, including hidden files. If the app directory itself is a
symlink, it is followed, but links inside are sent as links, so those pointing outside the
app break on the instance. Stopping ssh with Ctrl-C may leave `install` running on the
instance.

Nothing is rolled back on failure. A failure up to step 2 changes nothing. If sending
fails in step 3, the old directory remains. If `install` fails, the new directory
remains. Either way, running `deploy` again converges. The record is written only when
`install` succeeds; on failure nothing is baked even with `--bake`. The record's
`options` also stays as before, so a retry must give the same options again.

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
newline, tab, ESC, etc.). There is no length limit; the record size is checked instead.
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
2. The value in the previous record's `options` (`kept`). With `--reset`, nothing is
   carried over from the previous record, nor when its `removed` is the instance being
   deployed to (the app was removed from it). `<name>=` (empty value) stops that name
   from being carried over.
3. The declaration's `default` (`default`). An option without a default that still has
   no value here makes `deploy` exit. `<name>=` for an option without a default makes
   step 1 exit.

The record's `options` holds only the values decided by 1 and 2. The report line is
`option <name>=<value> (<source>)`, with the value quoted for the shell when needed.
Depending on the previous record:

| Previous record | Behavior |
| --- | --- |
| None (first `deploy`, or after `destroy` or an `app remove` that deleted the record), or `removed` is this instance | Nothing is carried over. A kept record whose `removed` names another instance (the removal was lost) carries its `options` over; `app remove` set them to the baked options |
| Readable from SSM, but the record is unreadable or its `options` is malformed | Exit without changing anything, telling the user to give the options again with `--reset`. If the app declares no options, nothing would be carried over, so it does not stop; a malformed `options` is discarded with a warning |
| Has a name the app does not declare | Warn, drop it, and continue. It disappears from the record too |
| A carried-over value does not match `pattern` | Exit without changing anything, telling the user to give `<name>=<value>`, or `<name>=` if there is a default |

If only the previous record's `baked_options` is unreadable, it does not stop; the value
is carried over as written.

The record must fit in a (standard tier) SSM parameter. In step 2 it estimates, in UTF-8
bytes, the size of the record this `deploy` writes and of the record after a following
bake (with `options` also copied to `baked_options`); if either exceeds 4096 bytes, it
exits without changing anything.

The values used, for all declared options, are passed to `install` in the file
`temt-options.env`, added directly under the app directory. It goes into the tar.gz owned
by root with mode 644, so the values never pass through a command line. One line per
option, in declaration order, of the form `TEMT_OPTION_<NAME>=<value>`: the variable name
is the option name upper-cased with `-` turned into `_`. Values are quoted with Python's
`shlex.quote`, so sourcing the file with bash's `.` expands nothing. The first line is a
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

Record values are not encrypted, and `temt-options.env` stays in the AMI readable by
anyone on the instance, so options must not carry secrets.

## `app remove`

`app remove --svc <svc> <app ID> [--user <user>] [--bake] [--yes]`

Removes an app from the running instance. The instance requirements are those of
[`deploy`](#deploy) step 2. The `[[role.policy.<policy name>]]` written for the app is
removed from the config file and `apply`ed by the user after removing the app.

1. Confirm, as in `deploy` step 2, that ssh reaches the instance found. Check that
   `/opt/temt/apps/<app ID>/` and its `uninstall` exist on the instance. If `uninstall`
   does not exist, exit without changing anything. If neither the directory nor the
   record exists, take the app ID as a typo and exit.

   If the directory exists, the user that runs `uninstall` is the `--user` value, else
   the record's `user`. If the record is missing or unreadable or has no `user`, warn
   that it runs as root and use root. If the record's `user` breaks the user name rule,
   exit telling the user to give it with `--user`, without showing the value. If the user
   is not root, check that the user exists on the instance as in `deploy` step 2. If the
   directory does not exist, warn; no user is decided.

   In both cases, with `--bake`, run the check of `bake` step 3 and ask for confirmation
   as in `deploy` step 2, listing the user when there is one. Then go to step 2, or to
   step 3 when the directory does not exist.
2. Run `bash /opt/temt/apps/<app ID>/uninstall` as that user with `/` as the current
   directory, and on success delete the directory as root. On failure, leave the record
   and exit. Stopping with Ctrl-C may leave `uninstall` running on the instance.
3. Delete the status `/<name>/<app ID>/status`, then handle the record
   `/<name>/<app ID>/deploy`. If the record is readable and its `baked_image` is the
   template's default-version AMI, rewrite it ([Deploy record](#deploy-record)): set
   `removed` to the instance, remove `instance` and `deployed` (when the baked version
   was installed is not recorded), and set `version` and `options` to `baked_version`
   and `baked_options` (what comes back if the removal is lost; an unreadable
   `baked_options` is copied as written). Otherwise delete it. If the rewritten record
   would not fit in an SSM parameter, warn and delete it, since a retry could never
   write it. When the directory is absent, the warning is
   `<directory> is not on <instance-id>; updating only the record`. The status is
   deleted because otherwise `status` keeps showing the old state of the removed app.
   The record goes last so that, if anything fails partway, the remaining record lets a
   retry continue from where it stopped. Other values of the app and the role's policies
   are not deleted.
4. With `--bake`, do `bake` steps 5-9 ([ami.md](ami.md#bake)) holding the same lock.
   Without it, report
   `removed <app ID> from <instance-id>; run 'temt bake --svc <svc>' to remove it from
   the next instance too`. This is printed even when the record was deleted because the
   app is not in the template's AMI, where baking changes nothing for it.

## Bundled apps

Kept in `src/temt/apps/<app ID>/` and shipped in the package. `deploy` uses one when the
app ID given is not in `apps/` of the conf directory.

| App ID | Contents |
| --- | --- |
| `idle-shutdown` | Powers off when nobody is using the instance ([apps/idle-shutdown.md](../apps/idle-shutdown.md)) |
| `swapfile` | Uses `/swapfile` as swap ([apps/swapfile.md](../apps/swapfile.md)) |
| `timezone-tokyo` | Sets the time zone to Asia/Tokyo ([apps/timezone-tokyo.md](../apps/timezone-tokyo.md)) |
