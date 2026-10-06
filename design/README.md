# temt design

The design of temt: what it builds and how. The reader is Claude only, so it is
written in English and may record rationale, rejected alternatives, and review
findings that were declined. The user docs are Japanese and live in docs/:
[usage.md](../docs/usage.md) (how to use), [conf.md](../docs/conf.md) (the config
file: format, keys, defaults, what `apply` writes), [apps.md](../docs/apps.md) (how to
write an app), and [apps/](../docs/apps/) (the bundled apps; for now also their design, in each file's
`## 設計` section). design/ refers to them
instead of repeating them. [glossary.md](glossary.md) maps the English terms here to
the Japanese terms there.

Design is discussed in chat until it settles; only settled decisions are written
here. Rationale is written briefly, where the steps alone do not show the intent.
Where this and the code disagree, decide which is right before changing either.

temt builds the cycle of preparing a service, launching its instance, logging in
with ssh, installing apps (`deploy`), turning the instance into an AMI (`bake`),
terminating it, and cleaning up. An unused instance terminates itself through the
bundled app `idle-shutdown` ([docs/apps/idle-shutdown.md](../docs/apps/idle-shutdown.md)).

## Files

| File | Contents |
| --- | --- |
| [principles.md](principles.md) | Assumptions and what to prefer when options diverge |
| [glossary.md](glossary.md) | English and Japanese terms |
| [naming.md](naming.md) | Service names, resource names, key pair names, tags, `--svc` |
| [local-files.md](local-files.md) | Repository layout, files in the user's home, location environment variables |
| [config.md](config.md) | The config file, profile, region, account |
| [resources.md](resources.md) | The AWS resources of a service and how `apply` and `destroy` treat them |
| [cli.md](cli.md) | Command grammar, the command table, behavior common to all commands, list tables |
| [commands/service.md](commands/service.md) | `apply`, `diff`, `destroy`, `service list` |
| [commands/instance.md](commands/instance.md) | `start`, `stop`, `terminate`, `reboot`, `status`, `instance list` |
| [commands/template.md](commands/template.md) | `template show` |
| [commands/ami.md](commands/ami.md) | `bake`, `ami list`, `ami prune`, `ami delete` |
| [commands/sg.md](commands/sg.md) | `sg list`, `sg add`, `sg remove`, `sg diff`, `sg show` |
| [commands/app.md](commands/app.md) | The deploy record, `app` commands, `deploy`, app options, `app remove`, bundled apps |
| [commands/keypair.md](commands/keypair.md) | `keypair list`, `keypair show`, `keypair create`, `keypair delete` |
| [commands/ssh.md](commands/ssh.md) | `proxy` and the login routes (`TEMT_LOGIN`) |
| [topics/ami.md](topics/ami.md) | Creating, selecting, and naming AMIs |
| [topics/instance.md](topics/instance.md) | Finding instances, intermediate states, terminating, waiting, locks |
| [topics/keypair.md](topics/keypair.md) | Matching the key pair with the local key |
| [topics/launch-template.md](topics/launch-template.md) | Launch template contents and versions |
| [topics/security-group.md](topics/security-group.md) | Security group, ingress rules, global IP address |
| [topics/ssh.md](topics/ssh.md) | known_hosts, the ssh config, the `Include` in `~/.ssh/config` |
| [topics/sshd-ports.md](topics/sshd-ports.md) | sshd listening ports through a State Manager association |

## Conventions

- `<svc>` is the service name and `<name>` is `temt-<svc>` ([naming.md](naming.md)).
- Commands that have a short form are written by the short form (`apply`, not
  `service apply`), and `--svc` is omitted.
- The conf directory and the ssh directory are written at their default locations
  (`~/.config/temt`, `~/.ssh/temt`).
- AWS APIs are written by their aws-cli subcommand names (`describe-instances`);
  in boto3 they are the methods of the same name (`describe_instances`).
- Keys of the config file are written without their table (`InstanceType`).
- Messages that temt prints are quoted verbatim in English.

## Premises

- The config file is the definition of a service shared as IaC: it is kept in git and
  used from several machines. Config files drifting apart between machines is not a
  design issue and not a review finding.
- Rare races from concurrent runs or from AWS's eventual consistency are acceptable
  when `status` shows the result and running the command again converges. Races caused
  by operating the same service from another PC are out of scope for review.
