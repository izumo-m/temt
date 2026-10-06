# Key pair and local key

Step 5 of [`apply`](../commands/service.md#apply): make the EC2 key pair `<KeyName>`
and the local key at the config file's `IdentityFile` agree. `destroy` does not delete
the key pair. Listing, showing, creating, and deleting key pairs is done by the
[`keypair` commands](../commands/keypair.md).

The key pair `<KeyName>` is registered in the service's region. The only key that can
log in to the instance is this key pair's key, which EC2 installs at launch. The local
key is the config file's `IdentityFile` (default `~/.ssh/temt/keys/<KeyName>`) and must
be the same key as the key pair.

Passphrase-protected keys are not supported. The public key cannot be extracted from
the private key, so an `IdentityFile` pointing at one is an error, and such keys are not
listed as hints either (each is skipped with a warning
`skipped <path>: <ssh-keygen's reason>`).

`apply` checks the following two in order to detect differences. Creating and
registering happen in the update after confirmation.

1. The key pair `<KeyName>` exists in EC2. If not, extract the public key from the
   `IdentityFile` key with `ssh-keygen -y`, register it under that name with
   `import-key-pair`, and tag it `temt:keypair=<KeyName>`. If the `IdentityFile` key
   does not exist either, first create a passphrase-less key there with
   `ssh-keygen -t ed25519`, then register it. With the defaults, the first `apply`
   creates `~/.ssh/temt/keys/temt` and `temt.pub`. No tag is added to a key pair that
   already exists.
2. The fingerprints of the `IdentityFile` key and the EC2 key pair `<KeyName>` match.
   The local side uses the public key extracted from the private key with
   `ssh-keygen -y`; the EC2 side uses the public key from
   `describe-key-pairs --include-public-key`. Both are computed as the SHA256 that
   `ssh-keygen -l` shows (the form starting with `SHA256:`). `KeyFingerprint` from
   `describe-key-pairs` is not used. A mismatch, or a missing `IdentityFile` key, is a
   key mismatch. If EC2 returns no public key, stop with `AWS did not return the public
   key of the key pair <KeyName>`; if it cannot be parsed, stop with
   `the key pair <KeyName> in EC2: <reason>`.

## Key mismatch

`KeyName` and `IdentityFile` in the config file not agreeing with each other is treated
as an error in the config file. `apply` cannot reconcile it: it does not change the EC2
key pair, and a private key cannot be recreated from a key pair. It exits at the
detection stage, before showing changes and asking for confirmation. `diff` ends with
the same error.

Before exiting, temt shows the key pair name and both fingerprints (only the EC2 one
when the `IdentityFile` key is missing: `the key pair <KeyName> exists, but <path> does
not`). As a hint for the fix, it lists the keys whose fingerprint matches the EC2 key
pair among all private keys in `~/.ssh/temt/keys/` and `~/.ssh/id_*` (excluding `.pub`),
or prints `no key on this machine matches the key pair <KeyName>`. The error is
`the key pair <KeyName> does not match; edit IdentityFile or KeyName in <path> and run
apply again`: change `IdentityFile` to one of the listed keys, or set `KeyName` to a new
name. With a new `KeyName`, step 1 above registers the `IdentityFile` key under that
name, creating it first if missing. With the default `IdentityFile` the path follows
the new `KeyName`, so this is usually a new key. It is used from the next `start` after
a `terminate`. `apply` never rewrites `IdentityFile`.

The first `apply` stops before confirmation, so the config file does not exist yet. In
that case the error ends with `write IdentityFile or KeyName in a new <path> and run
apply again`.
