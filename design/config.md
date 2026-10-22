# Config file and AWS environment

The config file `~/.config/temt/<svc>.toml` is the definition of a service. Every
command with `--svc` reads it; `service list` without `--svc` reads every
`<svc>.toml`. The format, keys, and defaults are in [docs/conf.md](../docs/conf.md).

`apply` writes nothing into an existing config file, as CDK does not rewrite its
source. The file holds only what differs from the defaults; every value not written is
decided again at each `apply` and `diff`
([topics/launch-template.md](topics/launch-template.md)). Only when the file does not
exist does `apply`, after asking, write a scaffold
([commands/service.md](commands/service.md#apply)).

The config file holds no ssh route, which depends on the client's network; the
environment variable `TEMT_LOGIN` gives that ([commands/ssh.md](commands/ssh.md#routes)).

Commands with `--svc` other than `apply` exit when the file is missing, telling the
user to run `apply`. `destroy` and `service import` are the exceptions
([cli.md](cli.md#missing-config-file)).

## Defaults file

`defaults.toml` in the conf directory (the defaults file) holds values common to every
service, in the same format and with the same keys as a config file. A command with
`--svc` reads its config file laid over the defaults file; temt never writes the
defaults file. Both files are checked on their own first, so an error names the file it
is in. A `defaults.toml` that is a broken symlink is an error, not a missing file:
falling back silently would use the environment's profile, which the file exists to
avoid. Hints that tell the user to change a value name the file the value came from.

- A key written in the config file wins. A blank value (empty string or empty array)
  counts as not written, so the config file cannot clear a value of the defaults file.
- Tables (`[launch-template]`) are merged key by key, except `IamInstanceProfile`: when
  both files have it, the config file's replaces the defaults file's whole, so an `Arn`
  from one and a `Name` from the other are never mixed.
- Arrays in `[launch-template]` whose elements are matched by a key
  ([topics/launch-template.md](topics/launch-template.md#comparing):
  `NetworkInterfaces`, `BlockDeviceMappings`, `TagSpecifications`, tags) are merged
  element by element. Elements with the same key are merged key by key, the config
  file's value winning; elements with different keys are both kept, the defaults
  file's first. So an instance tag `Owner` in the defaults file and an instance tag
  `Env` in the config file are both put on. Other arrays (`Groups` etc.) written in the
  config file replace the defaults file's whole. Rejected: replacing every array whole;
  a config file that writes `SubnetId` in its `NetworkInterfaces` would lose the
  defaults file's `NetworkInterfaces` (`AssociatePublicIpAddress = false` etc.).
- Ingress rules are joined: those of the defaults file first, then those of the config
  file. A rule (protocol, port, CIDR) written in both is an error naming the config
  file's rule, and stops every command with `--svc` that reads the config file,
  `sg add` and `sg remove` included. `sg add` and `sg remove` edit only the config file
  ([commands/sg.md](commands/sg.md)).
- Policies `[[role.<policy name>]]` of the same name are joined, not replaced: the
  defaults file's statements first, then the config file's, as with ingress rules
  (the requester: `[[...]]` means adding). Statements that repeat are not an error to
  temt; IAM judges the joined policy (a repeated `Sid` is its error). An error in a
  joined policy names the file and the position of the statement in that file.
  `ManagedPolicyArns` is an ordinary array: written in the config file, it replaces the
  defaults file's. A `[role]` with nothing in it counts as not written
  ([resources.md](resources.md#instance-profile-and-instance-role)).
- Since `apply` writes nothing into an existing config file, a change in the defaults
  file reaches every service at its next `apply`. An `ImageId` in the defaults file
  pins the AMI of every service ([topics/ami.md](topics/ami.md)); an `AwsRegion` there
  fixes the region of every service that does not write its own
  ([Region](#region)).
- `Defaults = false` at the top level of a config file makes that service read no
  defaults file at all; unwritten or `true` reads it. Only the user writes it. In the
  defaults file itself the key is an error.
- `service import` does not write `Defaults = false` (the requester: the imported
  service then starts with the credentials and region used for the import). So the
  defaults file's keys reach an imported service: its `AwsProfile` and `AwsRegion` are
  used unless `--profile` and `--region` are given (a user whose import region differs
  writes `AwsRegion` in the config file or passes `--region`); `[launch-template]` items the source lacks are added; ingress rules stop
  `apply`, since the imported `Groups` has no temt security group
  ([topics/security-group.md](topics/security-group.md#when-it-is-created)); a `[role]`
  creates `<name>`, attached only if the service's `IamInstanceProfile` points at it
  ([resources.md](resources.md#instance-profile-and-instance-role)). A user who does
  not want this writes `Defaults = false`.
- When the defaults file's ingress rules stop `apply` for a service whose security
  groups temt does not manage, the error names the defaults file.
- The name `defaults` is not a service name ([naming.md](naming.md#service-names)),
  and `service list` skips the file.

The purpose is to keep temt on its own account while a project the user works in sets
`AWS_PROFILE` for other tools: the defaults file comes before the environment for
commands without `--svc` as well ([Commands without --svc](#commands-without---svc)).
The requester rejected a file looked up from the current directory, since temt is also
used from inside such projects.

## Profile

A command with `--svc` takes its profile from the first found of:

1. `AwsProfile` in the config file. Written, it is fixed: a `--profile` with another
   value is a usage error, as for the region.
2. `--profile`.
3. `AwsProfile` in the defaults file.

When none is found, temt passes no profile, and the credentials follow AWS's default
rules (`AWS_PROFILE`, `AWS_DEFAULT_PROFILE`, key environment variables, the `[default]`
profile, and so on). temt sets no profile of its own as a last resort.

temt makes the aws-cli it runs use the same profile and region as its own AWS calls.
Left alone they could differ: boto3 and aws-cli v2 read `AWS_PROFILE` and
`AWS_DEFAULT_PROFILE` in opposite orders, and boto3 does not read `AWS_REGION`.

`apply` and `service import` do not write `AwsProfile`: `--profile` works for that run
only, as `--region` does.

## Region

A command with `--svc` always has a region, the first found of:

1. `AwsRegion` in the config file. Written, it is fixed: a `--region` with another
   value is a usage error.
2. `--region`.
3. `AwsRegion` in the defaults file.
4. The environment variables `AWS_REGION`, then `AWS_DEFAULT_REGION`.
5. The AWS profile's `region`.
6. `ap-northeast-1`, silently.

This is the order of [Commands without --svc](#commands-without---svc), with a default
instead of an error at the end. When `AwsRegion` is written in either file, the
environment variables do not switch the region (the requester); a user who switches by
environment writes `AwsRegion` in neither.

`apply` and `service import` do not write `AwsRegion`. `service import` looks for the
source instance in the region decided by this order.

### Clones

Applying the same config file in another region makes a clone of the service there
([principles.md](principles.md#assumptions)). Each region has its own launch template,
security group, instance, and SSM parameters (app values and deploy records); the
instance role and instance profile `<name>` are one pair shared by every region
([resources.md](resources.md#instance-profile-and-instance-role)). AMIs are not copied
by temt: the user copies them by hand under the same name
([topics/ami.md](topics/ami.md)).

A config file that writes region-dependent values (`SubnetId`, `Groups` by ID, an
`ImageId` by ID, a `KeyName` whose key pair is not imported into the other region under
the same name) cannot be cloned as it is; AWS's errors show which.

temt does not remember which regions a service was applied in, and adds no way to
search for them; the user remembers them and checks with `--region`. Rejected:
`--all-regions` for `instance list` and `ami list`; `apply` recording the region in a
local file for `service list` to show.

The ssh the user runs has no `--region`: `proxy` follows the order above, so
`AWS_REGION=<region> ssh <name>` reaches the clone in that region, ssh passing the
environment on. When temt itself runs ssh (`deploy`, `app remove`), it hands the region
and profile it decided to `proxy` through the environment variables
`TEMT_PROXY_REGION` and `TEMT_PROXY_PROFILE`, which `proxy` ranks as `--region` and
`--profile`, before the defaults file. They are internal to temt and not in docs/;
`AWS_REGION` is not used for this, since an `AwsRegion` in either file would override
it. See [commands/ssh.md](commands/ssh.md).

## Commands without --svc

Commands without `--svc` other than `service list` and `app list` read no config file.
They read only `AwsProfile` and `AwsRegion` of the defaults file, as written (the
built-in default region does not apply), and the whole file is checked as for a config
file.

- Profile: `--profile`, then `AwsProfile` of the defaults file; without either, AWS's
  default rules decide it.
- Region: `--region`, `AwsRegion` of the defaults file, then like aws-cli
  `AWS_REGION`, `AWS_DEFAULT_REGION`, and the profile's `region`. If none is found, it
  is an error.

One run handles one environment (account and region) only. At the start of the run,
temt reports in one line the account, the region, and the profile. The profile is
shown by name only when it was chosen by `--profile`, the defaults file, or the
environment variable `AWS_PROFILE` or `AWS_DEFAULT_PROFILE`; otherwise it is shown as
none, since AWS answers `default` even when nothing was chosen, and then the
credentials may come from key environment variables and the like.

The commands that do not call AWS use neither a profile nor a region
([cli.md](cli.md#grammar)). `app list` without `--svc` reads only `apps/` in the conf
directory and the bundled apps.

## Account

The config file holds no account. The credentials decide which account is used;
running `apply` with credentials of another account creates the service anew in that
account. Items written that point at resources inside an account (`SubnetId`,
`ImageId`, `Groups` by ID, `IamInstanceProfile` other than `${temt:name}`, `KeyName`,
...), however, must be deleted or rewritten first
([docs/conf.md](../docs/conf.md#launch-template)).

The confirmations of destructive commands with `--svc` show the region and the account,
so that a forgotten `--profile` or `--region` is noticed before anything changes
([cli.md](cli.md#common-behavior)).
