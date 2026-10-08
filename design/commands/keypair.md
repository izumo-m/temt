# keypair commands

The `keypair` commands handle EC2 key pairs and the local keys
`~/.ssh/temt/keys/<key pair name>`. None of them takes `--svc`; the environment is
decided by `--profile` and `--region`. None of them takes the lock.

## Fingerprints

As in [topics/keypair.md](../topics/keypair.md), a fingerprint is computed from the
public key in the `SHA256:` form of `ssh-keygen -l`. For an EC2 key pair, the public key
comes from `describe-key-pairs --include-public-key`.

EC2's `KeyFingerprint` is not compared, because for RSA its computation depends on how
the key pair was registered (SHA-1 of the private key when EC2 created it, MD5 of the
public key when imported), and for ED25519 it may lack the `SHA256:` prefix and end in
`=`.

Whether a key pair is used is decided by `KeyName` in every version of every launch
template in the account and region.

## `keypair list`

`keypair list [--format <columns>]`

Lists the key pairs tagged `temt:keypair` in name order, one per line
([cli.md](../cli.md#list-tables)).

| Column | Default | Value |
| --- | --- | --- |
| `NAME` | yes | Key pair name |
| `FINGERPRINT` | yes | Fingerprint |
| `CREATED` | yes | Creation time |
| `LOCAL` | yes | Comparison with the local `~/.ssh/temt/keys/<key pair name>` |
| `USED` | yes | Launch template versions that use the key pair |
| `ID` | | Key pair ID |
| `TYPE` | | Key type |

- `LOCAL` is `matches` if they match, `differs` if not, `unreadable` if the local key
  cannot be read, and `-` if there is no local key. An unreadable key is warned about
  with the reason `ssh-keygen` gives.
- If EC2 returns no public key, or one that cannot be parsed, `FINGERPRINT` is `-`, and `LOCAL` is `unknown` when a
  local key exists.
- If a key pair tagged by hand has a name that does not follow the key pair name rule
  ([naming.md](../naming.md#key-pair-names)), the local key is not looked for and
  `LOCAL` is `-`, so that the path never points outside `keys/`.

## `keypair show`

`keypair show <key pair name or file>`

An argument containing `/` is a local key file; otherwise it is a key pair name, because
key pair names cannot contain `/` ([naming.md](../naming.md#key-pair-names)). A key pair
not made by temt whose name contains `/` cannot be shown by name. A key in the current
directory is given with `./`. An argument starting with `-` is taken as a command-line
option, so a key pair whose name starts with `-` cannot be shown. For a file, temt
checks that it exists and is a file before querying AWS.

- Key pair name: shown whether managed or not. Shows the key pair's ID, type, creation
  time, tags, fingerprint, `KeyFingerprint`, public key, the launch template versions
  that use it, and the matching local keys. Local keys are searched in the same places
  `apply` searches for clues on a key mismatch (`~/.ssh/temt/keys/` and `~/.ssh/id_*`;
  [topics/keypair.md](../topics/keypair.md#key-mismatch)). If the key pair does not
  exist, it is an error.
- File: shows whether it is a private or public key, the type and bits, the comment,
  the fingerprint, the public key, and every key pair in the environment whose
  fingerprint matches.
  - It is a private key when its first non-empty line starts with `-----BEGIN ` and
    ends with `PRIVATE KEY-----`. The public key is derived with `ssh-keygen -y`, and
    whether it matches the neighboring `<file>.pub` is also shown.
  - If the public key cannot be derived (for example, a passphrase is set), the
    neighboring `.pub` is used if present; otherwise only the reason it cannot be
    derived is shown.

## `keypair create`

`keypair create <key pair name>`

Registers the local key `~/.ssh/temt/keys/<key pair name>` as a key pair. Each of the
two is used as is when present and made when absent, so a run that stopped part way
(including a registration whose answer was lost) is finished by running it again. The
local key is never deleted.

1. Look up the key pair in EC2.
2. Local key:
   - If the private key exists, derive its public key with `ssh-keygen -y`. If it cannot
     be read (for example, a passphrase is set), exit with the reason. `<key pair
     name>.pub` is not read, as in `apply`.
   - If it does not exist and the key pair does, exit: a private key cannot be recreated
     from a key pair. The error says to put the private key there, or to run `keypair
     delete` and create it again.
   - If neither exists but `<key pair name>.pub` does, exit and tell the user to remove
     it: creating a key would overwrite it.
   - Otherwise, as in `apply`, create a passphrase-less key with `ssh-keygen -t ed25519`.
3. Key pair:
   - If it does not exist, register the public key with `import-key-pair`, tag it
     `temt:keypair=<key pair name>`, and report its ID and fingerprint. If AWS refuses or
     gives no answer, the error is shown and the local key is kept. botocore resends a
     request whose answer was lost; when the first one was applied, the resend fails
     with `InvalidKeyPair.Duplicate`, and the next run finds the key pair matching.
   - If it exists, compare fingerprints. If they match, report that it already matches
     with its ID and fingerprint, and change nothing; a key pair without the tag is left
     without it. If EC2 returns no readable public key, or they differ, exit; for a
     difference the error points to `keypair show`, which lists the local keys that
     match the key pair.

## `keypair delete`

`keypair delete <key pair name> [--yes]`

Deletes the EC2 key pair and the local key. It exists to make recreating a key easy.
Each is deleted when present and skipped when absent.

1. If the key pair exists without a `temt:keypair` tag, say that temt did not create it
   and exit, deleting nothing (the local key included). To bring it under management,
   the user tags it by hand. If neither the key pair nor a local key exists, say there
   is nothing to delete and exit successfully.
2. If the key pair exists, warn with the launch template versions that use it and the
   instances launched with it that are not yet terminated (`pending`, `running`,
   `stopping`, `stopped`). Being in use does not stop the command.
   - If a service's `KeyName` points to the key pair and its `IdentityFile` is the
     default, the next `apply` creates a new key and registers it under the same name.
     The listed instances hold only the old key, so logging in to them becomes
     impossible.
3. When a local key is deleted, also warn that it is a single key shared across
   environments, and urge running `keypair delete` in other regions and accounts where
   the same key is registered. With the local key gone, logging in to instances there
   becomes impossible too, and unless `keypair delete` is run there, `apply` there stops
   on a key mismatch. Other environments are not inspected.
4. List the key pair and the local keys to delete, and confirm. Locally only
   `~/.ssh/temt/keys/<key pair name>` and `<key pair name>.pub`, whichever exist, are
   deleted, even if `IdentityFile` in a config file or the ssh config points to them.
   Keys elsewhere are not touched.
5. Delete the key pair with `delete-key-pair`, then delete the local keys. If the key
   pair cannot be deleted, the local keys are kept.
