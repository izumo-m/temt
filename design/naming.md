# Names and tags

## Service names

A service is the definition of an EC2 instance set up for one purpose. One service is
defined by one config file and has one instance. To run the same purpose with
different settings, make it another service with another name.

The service name `<svc>` starts with a lowercase letter, continues with lowercase
letters, digits, and hyphens, and ends with a lowercase letter or digit. `temt`
checks the argument against this rule (usage error `invalid service name: <svc>`).

## Resource names

Every resource name derives from `temt-<svc>`, written `<name>`. `<name>` is at most
64 characters, the IAM role name limit (usage error
`name longer than 64 characters: <name>`).

| Object | Name |
| --- | --- |
| Instance `Name` tag | `<name>` |
| Launch template | `<name>-template` |
| State Manager association (sshd listening ports) | `<name>-sshd` |
| Security group | `<name>` |
| IAM role and instance profile | `<name>` |
| Key pair | `KeyName` in the config file; default `temt`. For `keypair create`, its argument |
| Private and public key | `~/.ssh/temt/keys/<key pair name>`, `~/.ssh/temt/keys/<key pair name>.pub`. The private key ssh uses is `IdentityFile` in the config file |
| SSM parameter hierarchy | `/<name>`. `destroy` deletes everything under it |
| App values and deploy record | `/<name>/<app ID>/<key>`. Key `deploy` is the deploy record; `status` is the app status the app writes |
| Role policies | Inline policies `temt-<policy name>` of the role. `<policy name>` comes from `[[role.policy.<policy name>]]` in the config file. `temt-ec2` holds the permissions of the instance itself; if the config file has none, `apply` writes the default |
| Apps on the instance | `/opt/temt/apps/<app ID>/`, owned by root |
| Config file | `~/.config/temt/<svc>.toml` |
| ssh `Host` | `<name>` (a block in `~/.ssh/temt/config`) |
| known_hosts | `~/.ssh/temt/known_hosts.d/<name>/` |
| AMI and the `Name` tag of its snapshots | `temt-<svc>-<YYMMDDHHMM>` (`<svc>` is the baked service; the time is the client's local time) |
| Client command | `temt`. Installed any way; found through `PATH` |
| Service command | `~/.local/bin/<name>`. A shell script that runs `temt` from `PATH` with `--svc <svc>`. `apply` writes it and `destroy` removes it. `<name> ...` is an alias of `temt --svc <svc> ...` ([local-files.md](local-files.md#service-command)) |

## Key pair names

A key pair name that temt uses as a local key file name (`KeyName`, and the argument
of `keypair create` and `keypair delete`) starts with a letter or digit, continues with
letters, digits, `.`, `_`, and `-` only, does not end with `.pub`, and is at most 251
characters.

- `/` and the like are excluded so that the name cannot point outside `keys/`.
- A name ending in `.pub` would collide with the public key file of another key pair.
- 251 is the 255-byte file name limit minus the 4 characters of `.pub`.

## Tags

AWS resources that temt creates carry the tag `temt:service=<svc>`, which marks them
as managed by temt. A key pair belongs to no service, so it carries
`temt:keypair=<key pair name>` instead.

| Resource | Tags |
| --- | --- |
| Instance, its volumes and network interfaces, security group | `Name=<name>`, `temt:service=<svc>` |
| AMI and its snapshots | `Name=<AMI name>`, `temt:service=<svc>` (`<svc>` is the baked service) |
| Launch template, IAM role, instance profile, association | `temt:service=<svc>` |
| Key pair (only one that temt registered) | `temt:keypair=<key pair name>` |

The resources of the last two rows have names of their own and show them in the
console, so they get no `Name` tag. temt does not touch tags on an existing AWS
resource that are not in this table.

## Service selection

Commands that operate on a service take `<svc>` by the command-line option
`--svc <svc>`.

Which commands require, allow omitting, or do not take `--svc` is in
[cli.md](cli.md#grammar).
