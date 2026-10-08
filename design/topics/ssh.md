# known_hosts and ssh config

Step 6 of [`apply`](../commands/service.md#apply) writes the ssh config;
[`proxy`](../commands/ssh.md) prepares known_hosts on every connection.

## known_hosts

An instance gets new host keys on every launch. The key changes under the same Host
name, so known_hosts is kept per instance.

```
~/.ssh/temt/known_hosts.d/<name>/<instance-id>   the file
~/.ssh/temt/known_hosts.d/<name>/current         symlink to <instance-id>
~/.ssh/temt/known_hosts.d/<name>/.lock           lock file
```

Directories are mode 700, files mode 600.

On every connection, `proxy` prepares the ssh directory, `keys/`, and the known_hosts
directory as the commands that take the lock do ([instance.md](instance.md#locks)),
opens the `<instance-id>` file in append mode, sets it to mode 600, closes it, and
repoints `current` to it.

- Append mode creates an empty file if missing and leaves an existing file's content
  untouched, so a host key just written by a concurrent connection is not erased.
- `current` is replaced by creating a symlink under a temporary name (containing the
  process ID, so concurrent connections do not collide) and renaming it over `current`.
  Deleting and recreating would let an ssh reading known_hosts in between treat the host
  as unknown.
- ssh reads known_hosts after starting the ProxyCommand, so the repointing is in time.

Host keys are not fetched in advance. The file for an `instance-id` seen for the first
time is empty, so ssh treats the host as unknown and records the key there after the
user confirms.

When the port it connects to (`Port`, or the `-p` value) is not 22, ssh writes the
known_hosts host name as `[<name>]:<port>`. The file is per instance, so this is
harmless, but if the port of a running instance changes, the next connection asks to
confirm the host key again.

## The ssh directory's config

In its own service's `Host <name>` block, `apply` rewrites only the 5 keys temt manages,
and writes `IdentityFile` only when the block has none.
If the block is missing it is appended (after a blank line if the file is not empty); if
the file is missing it is created with mode 600. Unmanaged keys, other blocks, and
comments are left untouched.

A block is the service's block if any whitespace-separated word of its `Host` line
equals `<name>`, so aliases are allowed (`Host temt-dev-main dev`). If two or more such
`Host` lines exist, the first one is taken, as ssh does.

```
Host temt-dev-main
    User               ubuntu
    Port               22
    IdentityFile       ~/.ssh/temt/keys/temt
    IdentitiesOnly     yes
    UserKnownHostsFile ~/.ssh/temt/known_hosts.d/temt-dev-main/current
    ProxyCommand       env TEMT_CONF_DIR='~/.config/temt' TEMT_SSH_DIR='~/.ssh/temt' temt proxy --svc dev-main --user '%r' %p
```

- `User` is fixed to `ubuntu`.
- `Port` is the first port of the config file's `SshdPorts` (default 22) and becomes the
  `%p` of `ProxyCommand` ([topics/sshd-ports.md](sshd-ports.md)).
- `IdentityFile` is written only when the block has none, with the default login key
  `~/.ssh/temt/keys/temt` (under `TEMT_SSH_DIR`; its rules keep out the characters ssh
  would split or expand). After that, the lines in the block are kept as written, so
  the user can point it at another key, or list several. `proxy` sends the key that
  ssh will use ([login-key.md](login-key.md)).
  `IdentitiesOnly yes` makes ssh use only these keys and not try the other keys in
  ssh-agent.
- `UserKnownHostsFile` points to the fixed name `current`, because ssh_config has no
  token that expands to the instance ID.
- `ProxyCommand` calls `temt` by name, not by path, and passes
  `proxy --svc <svc> --user '%r' %p`. `%r` is the user ssh logs in as, so that `proxy`
  sends the login key for that user even when it is given on ssh's command line. ssh
  puts `%r` in as it is, so it is in single quotes and the shell does not expand it. A
  user name containing `'` would break out of the quotes; OpenSSH 9.6 and later refuse
  such names on the command line.
  `env` looks up `temt` in the PATH inherited from ssh, so changing how or which version
  of `temt` is installed needs no rewrite. The aws-cli that `proxy` calls is looked up in
  the same PATH. `%p` is the `Port` value, or the `ssh -p` value when given.
- Before `temt`, `TEMT_CONF_DIR` and `TEMT_SSH_DIR`
  ([local-files.md](../local-files.md#location-environment-variables)) are always
  written as `env` arguments, in this order, even at their defaults. ssh passes its own
  environment to the ProxyCommand, so without them `proxy` would read the values of the
  shell ssh was typed in. Values are in single quotes. A location under the home is
  written starting with `~`, and `proxy` expands it itself; the home path itself never
  appears in the ssh config. ssh hands `ProxyCommand` to `exec` in the user's shell, so
  shell assignments (`VAR=value command`) cannot be used.
- `UpdateHostKeys` is not written. With it, ssh rewrites known_hosts through a temporary
  file and a rename, and `current` stops being a symlink. With `UserKnownHostsFile` set,
  it is off by default.

The user may add their own settings for the service (`LocalForward` etc.) to this block.
If the 5 managed keys are edited by hand, the next `apply` puts temt's values back;
`IdentityFile` is left as the user wrote it.

- `apply` regenerates the managed lines in one fixed form without reading the written
  values, and writes only when the file content would change. A line ssh reads as the
  same value (e.g. quoted by hand) is still put back into temt's form.
- A managed line already in the block is rewritten in place. Missing managed keys are
  inserted right after the `Host` line, in the order above.
- If a managed key appears more than once in the block, only the first line is kept.
  Every `IdentityFile` line is kept: ssh tries them in order.

### Services across conf directories

The ssh directory is one across conf directories, so it can hold only one `Host <name>`
block. In step 1, `apply` reads, from the existing block's `ProxyCommand`, the conf
directory of the `apply` that wrote it:

- The value of `TEMT_CONF_DIR` in the `env` if present (a leading `~/` is expanded to
  the home; empty means the default; if it does not become an absolute path, nothing is
  checked); otherwise the default location.
- With no block or no `ProxyCommand`, nothing is checked.

If that location differs from the current one (compared after resolving `..` and
symlinks) and the service's config file still exists there, `apply` names the location
and exits without writing anything, because with two definitions of the same name, both
the ssh destination and the AWS resources get overwritten by whichever `apply` ran last.

- If the config file is not there, the service is taken as moved, and `apply` proceeds.
- If it is the same file as the one in the current location (a symlink etc.), there is
  one definition, and `apply` proceeds.
- If the previous location cannot be checked, `apply` exits.

The message is `<name> is set up from another config directory: <path> exists`,
followed by `, and so does <current path>; remove one of them and run apply again` when
the current location also has the config file, and otherwise by
`; run apply with TEMT_CONF_DIR=<previous dir>, or move that file to <current dir>`.

## The Include in ~/.ssh/config

The following 2 lines are placed at the top of `~/.ssh/config`. They must come before
any `Host` or `Match` block; otherwise they are read as settings of that block.

```
# ===== temt ===== do not remove; 'temt apply' owns the next line
Include ~/.ssh/temt/config
```

`apply` uses the marker on the first line to treat the second line as its own, and
rewrites it when the ssh directory changes.

- If the marker is followed by an `Include`, that line is replaced. If it is followed
  by something else, that line stays and the `Include` is inserted between them.
- When replacing, if the file the old line pointed to still exists and has `Host` lines,
  ssh stops reading it, so `apply` warns, naming that file and those `Host` names
  (`~/.ssh/config no longer includes <file>; ssh cannot reach <hosts> there`). `diff`
  shows the same as `ssh cannot reach <hosts> in <file>`. Not named: `Host` names also
  present in the new `config`, and wildcard (`*`, `?`) and negated (`!`) patterns. With
  nothing to name, no warning. A file that resolves to the new `config` is skipped. If
  the old file cannot be checked or read, the line is rewritten without a warning.
- An `Include` without the marker is the user's and is left as it is.
- A marker after the first `Host` or `Match` is treated as absent, because an `Include`
  there is read only as a setting of that block.
- With no marker, the marker and its line are prepended. If `~/.ssh/config` does not
  exist, it is created with mode 600 (and `~/.ssh` with mode 700 if missing).

ssh reads the `Include` path as a glob (a pattern matching file names with `*` etc.),
so `*`, `?`, and `[` are escaped with a backslash. `IdentityFile` and other settings in
the `Host` block are not globs, so these 3 characters are not escaped there.
