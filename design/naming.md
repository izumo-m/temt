# Names and tags

## Service names

A service is the definition of an EC2 instance set up for one purpose. One service is
defined by one config file and has one instance. To run the same purpose with
different settings, make it another service with another name.

The service name `<svc>` starts with a lowercase letter, continues with lowercase
letters, digits, and hyphens, and ends with a lowercase letter or digit. `defaults` is
not a service name, as it names the defaults file `defaults.toml`. `temt` checks the
argument against this rule (usage error `invalid service name: <svc>`, with
` (reserved for the file defaults.toml)` for `defaults`).

## Resource names

Every resource name derives from `temt-<svc>`, written `<name>`. `<name>` is at most
64 characters, the IAM role name limit (usage error
`name longer than 64 characters: <name>`).

| Object | Name |
| --- | --- |
| Instance `Name` tag | `<name>` unless written in the config file |
| Launch template | `<name>-template` |
| Security group | `<name>` |
| Instance role and instance profile | `<name>` |
| Default login key | `~/.ssh/temt/keys/temt` and `temt.pub`, shared by every service. The key ssh uses is `IdentityFile` in the `Host` block ([topics/login-key.md](topics/login-key.md)) |
| SSM parameter hierarchy | `/<name>`. `destroy` deletes everything under it |
| App values and deploy record | `/<name>/<app ID>/<key>`. Key `deploy` is the deploy record; `status` is the app status the app writes |
| Role policies | Inline policies `temt-<policy name>` of the instance role. `<policy name>` comes from `[[role.policy.<policy name>]]` in the config file. `temt-ec2` holds the permissions of the instance itself; if the config file has none, `apply` writes the default |
| Apps on the instance | `/opt/temt/apps/<app ID>/`, owned by root |
| Config file | `~/.config/temt/<svc>.toml`; the defaults file is `~/.config/temt/defaults.toml` ([config.md](config.md#defaults-file)) |
| ssh `Host` | `<name>` (a block in `~/.ssh/temt/config`) |
| known_hosts | `~/.ssh/temt/known_hosts.d/<name>/` |
| AMI and the `Name` tag of its snapshots | `temt-<svc>-<YYMMDDHHMM>` (`<svc>` is the baked or imported service; the time is the client's local time) |
| Client command | `temt`. Installed any way; found through `PATH` |
| Service command | `~/.local/bin/<name>`. A shell script that runs `temt` from `PATH` with `--svc <svc>`. `apply` writes it and `destroy` removes it. `<name> ...` is an alias of `temt --svc <svc> ...` ([local-files.md](local-files.md#service-command)) |

## Tags

AWS resources that temt creates carry the tag `temt:service=<svc>`, which marks them
as managed by temt. Instances are found by it, not by `Name`
([topics/instance.md](topics/instance.md#finding)); apps on the instance read it from
the instance metadata to know their service ([docs/apps.md](../docs/apps.md#aws-を使う)).

| Resource | Tags |
| --- | --- |
| Instance, its volumes and network interfaces | `Name=<name>` unless the config file writes `Name` ([topics/launch-template.md](topics/launch-template.md#what-temt-adds)), `temt:service=<svc>` |
| Security group | `Name=<name>`, `temt:service=<svc>` |
| AMI and its snapshots | `Name=<AMI name>`, `temt:service=<svc>` (`<svc>` is the baked or imported service) |
| Launch template, instance role, instance profile | `temt:service=<svc>` |

The resources of the last row have names of their own and show them in the
console, so they get no `Name` tag. temt does not touch tags on an existing AWS
resource that are not in this table.

## Service selection

Commands that operate on a service take `<svc>` by the command-line option
`--svc <svc>`.

Which commands require or allow omitting `--svc` is in
[cli.md](cli.md#grammar).
