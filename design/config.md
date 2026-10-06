# Config file and AWS environment

The config file `~/.config/temt/<svc>.toml` is the definition of a service. Every
command with `--svc` reads it; `service list` without `--svc` reads every
`<svc>.toml`. The format, keys and defaults, and the rules of what `apply` writes are
in [docs/conf.md](../docs/conf.md).

The config file is the definition of a service shared by several machines, so it holds
no per-machine ssh route; the environment variable `TEMT_LOGIN` gives that
([commands/ssh.md](commands/ssh.md#routes)). The AWS
profile can be written in `AwsProfile`, but when it differs per machine, override it
with `--profile`.

Commands with `--svc` other than `apply` exit when the file is missing, telling the
user to run `apply` (`no config file: <path>; run 'temt apply --svc <svc>' first`).
`destroy` is the exception ([cli.md](cli.md#missing-config-file)).

## Profile

A command with `--svc` takes its profile from `--profile`, then `AwsProfile` in the
config file. When one is found, it is passed to boto3 as the `Session` argument and to
aws-cli as `--profile`.

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

A command with `--svc` always has a region. If the config file has none,
`ap-northeast-1` is used and the first `apply` writes that value. One config file
corresponds to one instance, so to change the region, set up another `<svc>`.

A command with `--svc` decides its region from the config file only. To make sure of
it, `temt` removes the environment variables `AWS_REGION` and `AWS_DEFAULT_REGION`
from its own process when it starts. boto3 reads only `AWS_DEFAULT_REGION` and aws-cli
reads `AWS_REGION` first, so if left in the environment, the two could point at
different regions.

## Commands without --svc

Commands without `--svc` other than `service list` and `app list` read no config file
and take the profile and region from `--profile` and `--region`.

- Profile: without `--profile`, AWS's default rules decide it.
- Region: like aws-cli, `--region`, `AWS_REGION`, `AWS_DEFAULT_REGION`, then the
  profile's `region`. If none is found, it is an error
  (`no region; give --region, set AWS_REGION, or write region in the AWS profile`).
  The environment variables are saved before they are removed. boto3 does not read
  `AWS_REGION`, so temt reads it and passes it.

One run handles one environment (account and region) only. At the start of the run,
temt reports in one line the account from `get-caller-identity`, the region, and the
profile: `account <account>, region <region>, profile <profile>`. The profile is
shown by name only when it was chosen by `--profile` or the environment variable
`AWS_PROFILE` or `AWS_DEFAULT_PROFILE`; otherwise it is shown as `(none)`. boto3
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
