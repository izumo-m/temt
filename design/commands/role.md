# role commands

The `role` commands handle the instance role and instance profile `<name>` that temt
manages ([resources.md](../resources.md#instance-profile-and-instance-role)).

## `role import`

`role import --svc <svc> <role name or ARN> [--yes]`

Moves a copy of the given instance role under temt's management as `<name>`. It
rewrites the config file only and does nothing in AWS: the user checks with `diff` and
brings it in with `apply`, which creates `<name>`, attaches the policies, and switches
the launch template. A running instance keeps its previous instance profile until it is
terminated and started again.

1. Read the role, its inline policies, and its attached managed policies.
2. Build the new `[role]`, which replaces the whole of the current one in the config
   file. Policies of the defaults file's `[role]` are still joined to it by `apply`
   ([config.md](../config.md)).
   - Each inline policy goes to `[[role.<policy name>]]`, statement by statement. A
     name that does not follow the policy name rule (lowercase letter first, then
     lowercase letters, digits, and hyphens) is lowercased and the characters it cannot
     use become hyphens. A name that still breaks the rule after this (a leading digit,
     more than 64 characters, ...), and names that collide, are an error.
   - Managed policies (AWS managed and customer managed) go to `ManagedPolicyArns` by
     ARN, not expanded.
   - The role is copied as it is: no default `ec2` is added when it has none. Without
     the SSM permissions, `start` stops at its deadline; without reading `/<name>/`,
     apps stop with an error; `idle-shutdown` cannot write its status and only warns
     (its checks and power-off still work, and `app show` shows no status). These show
     the cause, so `role import` does not check for them.
   - A role with no inline or managed policy gives an empty `[role]`, so the next
     `apply` stops because `IamInstanceProfile` points at `<name>` without `[role]`
     ([resources.md](../resources.md#instance-profile-and-instance-role)). That error
     shows it, so `role import` does not stop first.
3. Set `IamInstanceProfile` in the config file's `[launch-template]` to
   `IamInstanceProfile.Name = "${temt:name}"`, the same form as the scaffold
   ([resources.md](../resources.md#instance-profile-and-instance-role)). Comments
   around an existing `IamInstanceProfile` stay.
4. Confirm, listing the policies and the change of `IamInstanceProfile`; `--yes` skips
   it. The confirmation shows no region or account: only a local file changes.
5. Write the config file. Then tell the user to fix by hand the resource policies
   naming the original role's ARN (S3 bucket policies, KMS key policies, ...), and to
   run `diff` and `apply`.

`service import` writes no `[role]`; it copies the source's `IamInstanceProfile` as it
is ([service.md](service.md#instance-profile)).

Rejected: `--name` for another name (the requester's earlier idea). A name collides
only when IAM has a `<name>` that is not temt's, and a fixed name makes clear what to
delete.

## `role delete`

`role delete --svc <svc> [--yes]`

Deletes the instance role and instance profile `<name>` (the requester).

1. Find them. Only those with `temt:service=<svc>` are deleted. Untagged ones, or ones
   of another service, stop with an error telling the user to delete them by hand. If
   only one of the pair exists, only that one is deleted. A `<name>` longer than IAM
   allows counts as absent ([resources.md](../resources.md#long-names)).
2. Stop if:
   - `[role]` has anything (in the config file or the defaults file): the next `apply`
     would create them again, so tell the user to remove `[role]` first. A config file
     without `[role]` whose `IamInstanceProfile` still points at `<name>` is not
     checked here; the next `apply` stops on it.
   - The role has the region tag of another region: the config there still has
     `[role]` ([resources.md](../resources.md#regions)).
   - An instance in the region uses the instance profile, stopped ones included: it
     would lose its credentials.
   - The instance profile holds another role, or the role is also in another instance
     profile (as `destroy` does).
3. Confirm with `[y/N]`, showing the region and account and listing the instance
   profile and the role with its inline and managed policies (as `destroy` does);
   `--yes` skips it.
4. Delete in the order of [Deleting](../resources.md#deleting).
