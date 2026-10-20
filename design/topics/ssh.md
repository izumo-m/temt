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
makes sure the `<instance-id>` file exists with mode 600, and repoints `current` to it.

- A missing file is created empty; an existing file's content is never touched, so a
  host key just written by a concurrent connection is not erased.
- `current` is switched in one step, never deleted and recreated: an ssh reading
  known_hosts in between would treat the host as unknown. Concurrent connections do not
  collide while switching it.
- ssh reads known_hosts after starting the ProxyCommand, so the repointing is in time.

Host keys are not fetched in advance. The file for an `instance-id` seen for the first
time is empty, so ssh treats the host as unknown and records the key there after the
user confirms.

When the port it connects to (`Port`, or the `-p` value) is not 22, ssh writes the
known_hosts host name as `[<name>]:<port>`. The file is per instance, so this is
harmless, but if the port of a running instance changes, the next connection asks to
confirm the host key again.

`terminate` and `destroy` delete the file of each instance they terminate
([instance.md](instance.md#terminating)). The directory stays: `destroy` leaves it for
the next `apply`, since a clone in another region may still use it.

## Regions

The ssh directory has one `Host <name>` block per service, whichever regions the service
has clones in. ssh, like `start` and the others, reaches the instance in the region
decided at that time ([commands/ssh.md](../commands/ssh.md#region-and-profile)). To be
connected to two regions at once, the user sets up two services. known_hosts files are
per instance ID, so the clones' host keys do not mix.

- `current` is one per service. Connecting to two regions' instances at once may point
  it at the other one's file while ssh reads it. ssh then stops on the host key
  mismatch, or, for an empty file with `StrictHostKeyChecking accept-new`, writes the
  key into the other instance's file, and the next connection to that instance reports
  a changed host key. temt does nothing about it: ssh stops rather than connecting
  wrongly.
- A `ControlMaster` connection of the user is reused after switching the region: ssh
  does not run `proxy` and silently reaches the previous instance. temt does nothing
  and does not write `ControlPath none` into the Host block; the usage doc mentions it.
- VS Code's Remote-SSH runs ssh with the environment VS Code was started with, so to
  reach another region it is started again with `AWS_REGION` set; the usage doc
  mentions it.
- The Host block's `Port` is one for every region; the user runs `set-port` in a
  clone's region too ([sshd-ports.md](sshd-ports.md#when-port-and-the-instance-differ)).

## The ssh directory's config

In its own service's `Host <name>` block, `apply` rewrites only the 2 keys temt manages
(`UserKnownHostsFile` and `ProxyCommand`: where temt's files are and how temt is
called, not values the user holds), and writes `User`, `Port`, and `IdentityFile` only
when the block has none.
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
    UserKnownHostsFile ~/.ssh/temt/known_hosts.d/temt-dev-main/current
    ProxyCommand       env TEMT_CONF_DIR='~/.config/temt' TEMT_SSH_DIR='~/.ssh/temt' temt proxy --svc dev-main --user '%r' %p
```

- `User` is written as `ubuntu` only when the block has none; after that the user edits
  it (for an imported instance that is not Ubuntu, for example). Rewriting it every
  time would leave the user no way to change it, since temt's `Include` is at the top of
  `~/.ssh/config`, and `ssh`, `deploy`, and `app remove` could not connect to other OSes.
- `Port` is written as `22` only when the block has none, so that a `Port` of a later
  `Host *` in `~/.ssh/config` or of `/etc/ssh/ssh_config` does not apply (ssh takes the
  first value it finds, and temt's `Include` comes first). After that `set-port` and the
  user rewrite it ([sshd-ports.md](sshd-ports.md)). It becomes the `%p` of
  `ProxyCommand`.
- `IdentityFile` is written only when the block has none, with the default login key
  `~/.ssh/temt/keys/temt` (under `TEMT_SSH_DIR`; its rules keep out the characters ssh
  would split or expand). After that, the lines in the block are kept as written, so
  the user can point it at another key, or list several. `proxy` sends the key that
  ssh will use ([login-key.md](login-key.md)).
- `IdentitiesOnly` is neither written nor touched; an existing line stays (the
  requester). Without it ssh also tries the keys in ssh-agent, which matters only with
  many keys (`Too many authentication failures`, an error that tells). `proxy` does not
  rely on it.
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
If the 2 managed keys are edited by hand, the next `apply` puts temt's values back;
`User`, `Port`, and `IdentityFile` are left as the user wrote them.

- `apply` regenerates the managed lines in one fixed form without reading the written
  values, and writes only when the file content would change. A line ssh reads as the
  same value (e.g. quoted by hand) is still put back into temt's form.
- A managed line already in the block is rewritten in place. Missing keys (managed, or
  written only when absent) are inserted right after the `Host` line, in the order
  above.
- If a managed key appears more than once in the block, only the first line is kept.
  Lines of `User`, `Port`, and `IdentityFile` are kept as they are: ssh tries every
  `IdentityFile` in order.

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

The error names the config file found there. When the current location also has the
config file, it tells the user to remove one of them; otherwise, to run `apply` with
`TEMT_CONF_DIR` set to the previous location, or to move that file to the current one.

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
  ssh stops reading it, so `apply` warns that ssh can no longer reach those hosts,
  naming that file and those `Host` names; `diff` shows the same. Not named: `Host` names also
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
