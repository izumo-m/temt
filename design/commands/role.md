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
2. Build the new `[role]`, which replaces the whole of the current one.
   - Each inline policy goes to `[[role.policy.<policy name>]]`, statement by
     statement. A name that does not follow the policy name rule (lowercase letter
     first, then lowercase letters, digits, and hyphens) is lowercased and the
     characters it cannot use become hyphens. A name that still breaks the rule after
     this (a leading digit, more than 64 characters, ...), and names that collide, are
     an error.
   - Managed policies (AWS managed and customer managed) go to `ManagedPolicyArns` by
     ARN, not expanded.
   - If there is no policy named `ec2`, the next `apply` writes the default
     `[[role.policy.ec2]]`, which adds the permissions temt's login needs.
3. Rewrite `IamInstanceProfile` of `[launch-template]` to `{ Name = "${temt:name}" }`.
4. Confirm, listing the policies and the change of `IamInstanceProfile`; `--yes` skips
   it.
5. Write the config file. Then tell the user to fix by hand the resource policies
   naming the original role's ARN (S3 bucket policies, KMS key policies, ...), and to
   run `diff` and `apply`.

Rejected: `--name` for another name (the requester's earlier idea). A name collides
only when IAM has a `<name>` that is not temt's, and a fixed name makes clear what to
delete.

## `role delete`

`role delete --svc <svc> [--yes]`

Deletes the instance role and instance profile `<name>` (the requester).

1. Find them. Only those with `temt:service=<svc>` are deleted. Untagged ones, or ones
   of another service, stop with an error telling the user to delete them by hand. If
   only one of the pair exists, only that one is deleted.
2. Stop if:
   - The config file's `IamInstanceProfile` points at `<name>`: the next `apply` would
     create them again, so tell the user to fix the config file first.
   - An instance uses the instance profile, stopped ones included: it would lose its
     credentials.
   - The role is also in another instance profile (as `destroy` does).
3. Confirm with `[y/N]`; `--yes` skips it.
4. Delete in the order of [Deleting](../resources.md#deleting).
