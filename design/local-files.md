# Repository and local files

## Repository layout

```
src/temt/        Python package of the command temt
src/temt/apps/   bundled apps
tests/           tests of temt (pytest)
design/          the design (this directory; English, for Claude)
docs/            user docs: usage.md, conf.md, apps.md, apps/
README.md        overview and installation
CONTRIBUTING.md  rules and steps for developing temt
pyproject.toml   package and dependency definition; uv reads it
uv.lock          pinned dependencies of the development environment (uv run)
mise.toml        versions of python and uv; tasks install, check, check-py314
```

## Files in the user's home

```
~/.local/bin/
    <name>                      service command; apply writes it if absent, destroy removes it
~/.config/temt/                 conf directory; mode 700 when temt creates it
    <svc>.toml                  config file of the service; apply writes it if absent
    apps/<app ID>/              app that deploy looks up first by app ID; placed by the user
~/.ssh/temt/                    ssh directory; mode 700
    config                      ssh config; apply adds and updates Host blocks;
                                included from ~/.ssh/config
    keys/                       keys; mode 700
    keys/<key pair name>        private key made by apply or keypair create; mode 600;
                                keypair delete removes it
    keys/<key pair name>.pub    its public key
    known_hosts.d/<name>/       per-instance known_hosts, current, .lock
    .config.lock                lock held while the ssh config is rewritten
```

At the top of `~/.ssh/config` sit two lines: a marker comment and an `Include` of the
ssh directory's `config`. `apply` adds them if absent
([topics/ssh.md](topics/ssh.md#the-include-in-sshconfig)).

`deploy` looks up an app given by app ID first in `apps/<app ID>/` of the conf
directory, then among the bundled apps (`apps/<app ID>/` of the package). Nothing is
placed in the user's home for the bundled apps, so reinstalling or upgrading temt
needs no step to bring them in line.

## Service command

The service command is this 3-line shell script. It finds `temt` through `PATH`, so a
change in how or which version of `temt` is installed needs no rewrite.

```sh
#!/bin/sh
# Written by temt apply for the service <svc>; temt destroy removes it.
exec temt --svc <svc> "$@"
```

## Location environment variables

The conf directory and the ssh directory can each be moved by an environment variable.

| Variable | Location | Default |
| --- | --- | --- |
| `TEMT_CONF_DIR` | conf directory | `~/.config/temt` |
| `TEMT_SSH_DIR` | ssh directory | `~/.ssh/temt` |

Who reads them:

- Every command with `--svc`, and `service list`: both. `TEMT_SSH_DIR` is read where
  the ssh directory is used (the default `IdentityFile`, locks, the ssh config), so a
  command that uses none of them does not notice a wrong value.
- The `keypair` commands: only `TEMT_SSH_DIR`, which locates the keys.
- `ami prune` without `--svc`: only `TEMT_SSH_DIR`, which locates the locks. A wrong
  value is an error before the confirmation.
- `app list` without `--svc`: only `TEMT_CONF_DIR`, which locates the apps.

Values are absolute paths; the rules for values are in
[docs/conf.md](../docs/conf.md#場所を変える環境変数). A command that reads a variable
reads it on every run; the value is never stored. `ProxyCommand` passes both
locations to `proxy` even when they are the defaults
([topics/ssh.md](topics/ssh.md)).

`TEMT_CONF_DIR` selects where service definitions are read. Once changed, `temt`
commands from the next run on read the config files there. The ssh directory is one
across conf directories, so ssh reaches a service applied from any of them. In
exchange, the same `<svc>` cannot have config files in two locations (`apply` step 1,
[commands/service.md](commands/service.md#apply);
[topics/ssh.md](topics/ssh.md#services-across-conf-directories)).

`TEMT_SSH_DIR` moves the ssh config, keys, and known_hosts. It cannot point at
`~/.ssh` itself. `IdentityFile`, `UserKnownHostsFile`, and `Include` that `apply`
writes into the ssh config follow the value at the time of that `apply`. The `Include`
in `~/.ssh/config` is a single line, so after changing the variable and running
`apply`, ssh no longer reaches the Hosts in the previous ssh directory; `apply` warns
and lists them. temt does not move what remains in the previous location, and
`destroy` cleans up only the location the variable points to at that time.
