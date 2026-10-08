# Config file and AWS environment

The config file `~/.config/temt/<svc>.toml` is the definition of a service. Every
command with `--svc` reads it; `service list` without `--svc` reads every
`<svc>.toml`. The format, keys and defaults, and the rules of what `apply` writes are
in [docs/conf.md](../docs/conf.md).

The config file is the definition of a service shared by several machines, so it holds
no per-machine ssh route; the environment variable `TEMT_LOGIN` gives that
([commands/ssh.md](commands/ssh.md#routes)). The AWS
profile can be written in `AwsProfile`, but when it differs per machine, leave
`AwsProfile` unset and set `AWS_PROFILE` on each machine. `--profile` does not fit that
case: the ssh ProxyCommand never gets it (`proxy` uses `AwsProfile`, else the
environment, which ssh passes on), and `apply --profile` writes it to `AwsProfile`.

Commands with `--svc` other than `apply` exit when the file is missing, telling the
user to run `apply` (`no config file: <path>; run 'temt apply --svc <svc>' first`).
`destroy` is the exception ([cli.md](cli.md#missing-config-file)).

## Defaults file

`defaults.toml` in the conf directory (the defaults file) holds values common to every
service, in the same format and with the same keys as a config file. A command with
`--svc` reads its config file laid over the defaults file; only the config file is ever
written. Both files are checked on their own first, so an error names the file it is in.
A `defaults.toml` that is a broken symlink is an error, not a missing file: falling back
silently would use the environment's profile, which the file exists to avoid. Hints that
tell the user to change a value name the file it came from (`conf.source`).

- A key written in the config file wins. A blank value (empty string or empty array)
  counts as not written, so the config file cannot clear a value of the defaults file.
- Tables (`[launch-template]`) are merged key by key.
- Ingress rules are joined: those of the defaults file first, then those of the config
  file. A rule (protocol, port, CIDR) written in both is an error naming the config
  file's rule, and stops every command with `--svc` that reads the config file,
  `sg add` and `sg remove` included. `sg add` and `sg remove` edit only the config file
  ([commands/sg.md](commands/sg.md)).
- `[role.policy]` is merged by policy name; a name in the config file replaces the
  same name of the defaults file. If the defaults file has `ec2`, `apply` does not
  write the default `ec2` into the config file.
- `apply` treats values from the defaults file as it treats built-in defaults: what it
  writes even when defaulted (`AwsRegion`, `InstanceType`, and the chosen `ImageId` and
  `SubnetId`) is written into the config file at the first `apply`, so later edits of
  the defaults file leave existing services as they are. This pins the region of an
  existing service; following a changed `AwsRegion` would lose its resources. Keys
  `apply` does not write (`AwsProfile`, `SshdPorts`, `VolumeSize`, ingress rules,
  policies) keep following the defaults file. The requester chose this over not
  writing values from the defaults file.
- The name `defaults` is not a service name ([naming.md](naming.md#service-names)),
  and `service list` skips the file.

The purpose is to keep temt on its own account while a project the user works in sets
`AWS_PROFILE` for other tools: the defaults file comes before the environment for
commands without `--svc` as well ([Commands without --svc](#commands-without---svc)).
The requester rejected a file looked up from the current directory, since temt is also
used from inside such projects.

## Profile

A command with `--svc` takes its profile from `--profile`, then `AwsProfile` in the
config file (laid over the defaults file). When one is found, it is passed to boto3 as
the `Session` argument and to aws-cli as `--profile`.

When none is found, no profile is passed to boto3 or aws-cli, and the credentials
follow AWS's default rules (the environment variables `AWS_PROFILE` and
`AWS_DEFAULT_PROFILE`, key environment variables, the `[default]` profile, and so on).
In that case, before calling aws-cli, if `AWS_DEFAULT_PROFILE` or `AWS_PROFILE` is
set, the one boto3 chooses (`AWS_DEFAULT_PROFILE` first) is put into both variables of
the environment passed to aws-cli. Because aws-cli v2 reads the two in the opposite
order to boto3, they would disagree when both are set with different values.

## Region

The region is passed to boto3 as the `Session` argument and to aws-cli as `--region`.
When calling aws-cli, `AWS_PAGER` is set empty.

A command with `--svc` always has a region. If neither the config file nor the defaults
file has one, `ap-northeast-1` is used; the first `apply` writes the value used into the
config file. One config file
corresponds to one instance, so to change the region, set up another `<svc>`.

A command with `--svc` decides its region from the config file and the defaults file
only. To make sure of
it, `temt` removes the environment variables `AWS_REGION` and `AWS_DEFAULT_REGION`
from its own process when it starts. boto3 reads only `AWS_DEFAULT_REGION` and aws-cli
reads `AWS_REGION` first, so if left in the environment, the two could point at
different regions.

## Commands without --svc

Commands without `--svc` other than `service list` and `app list` read no config file.
They read only `AwsProfile` and `AwsRegion` of the defaults file, as written (the
built-in default region does not apply), and the whole file is checked as for a config
file.

- Profile: `--profile`, then `AwsProfile` of the defaults file; without either, AWS's
  default rules decide it.
- Region: `--region`, `AwsRegion` of the defaults file, then like aws-cli
  `AWS_REGION`, `AWS_DEFAULT_REGION`, and the profile's `region`. If none is found, it
  is an error (`no region; give --region, write AwsRegion in <defaults file>, set
  AWS_REGION, or write region in the AWS profile`).
  The environment variables are saved before they are removed. boto3 does not read
  `AWS_REGION`, so temt reads it and passes it.

One run handles one environment (account and region) only. At the start of the run,
temt reports in one line the account from `get-caller-identity`, the region, and the
profile: `account <account>, region <region>, profile <profile>`. The profile is
shown by name only when it was chosen by `--profile`, the defaults file, or the
environment variable `AWS_PROFILE` or `AWS_DEFAULT_PROFILE`; otherwise it is shown as
`(none)`. boto3
answers `default` even when nothing was chosen, and then the credentials may come
from key environment variables and the like.

The commands that do not call AWS use neither a profile nor a region
([cli.md](cli.md#grammar)). `app list` without `--svc` reads only `apps/` in the conf
directory and the bundled apps.

## Account

The config file holds no account. The credentials decide which account is used;
running `apply` with credentials of another account creates the service anew in that
account. `SubnetId` and `ImageId`, however, point at resources inside an account, so
to create the service in another account, delete those lines before running `apply`
([docs/conf.md](../docs/conf.md#launch-template)).
