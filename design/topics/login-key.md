# Login key

temt registers no EC2 key pair and does not write `KeyName`. Each time ssh connects,
`proxy` sends the public key of the key ssh will use to the instance with EC2 Instance
Connect (`send-ssh-public-key`), right before handing the connection to ssh
([commands/ssh.md](../commands/ssh.md#connecting)). The instance accepts that key for
60 seconds, and ssh authenticates with it right after. A `KeyName` the user writes in
`[launch-template]` (`ami import` copies the source's) goes into the launch template as
it is ([launch-template.md](launch-template.md#filled-when-missing)); the key pair's key
then logs in alongside the one `proxy` sends.

- Nothing about the key is kept in AWS, so there is nothing to register per region or
  account.
- Changing the key needs no new instance: the next connection sends the new one.
- Instance Connect does not edit `authorized_keys`. Keys already there (for example in
  an AMI baked from an instance launched with a key pair) keep working; an instance
  from an AMI without such keys accepts only the keys `proxy` sends.
- ssh authenticates after the host-key prompt of a first connection. If the user takes
  more than 60 seconds to answer it, the key has expired and ssh ends with
  `Permission denied (publickey)`; connecting again works, since the host key is then
  recorded. `proxy` does not resend the key in the background: that would keep a
  process alive beside the relay for a case that a retry settles, and sshd's
  `LoginGraceTime` (2 minutes by default) closes the connection anyway.

Requirements:

- The AMI has `ec2-instance-connect`, which makes sshd ask the instance metadata for
  the sent keys. The default Ubuntu AMI has it. Without it, ssh ends with
  `Permission denied (publickey)`.
- The credentials temt uses can call `ec2-instance-connect:SendSSHPublicKey`.
- The key is one Instance Connect accepts (ED25519 or RSA). Checked on real AWS: an
  ED25519 key on Ubuntu 24.04 over `ssm`, sent 10 times in a row without refusal, each
  call 0.3 to 1.5 seconds.

## The key ssh uses

The ssh config alone decides it; the config file has no key setting.

- `apply` writes `IdentityFile` into the `Host <name>` block only when the block has
  none, with the default key; after that it keeps whatever is written
  ([ssh.md](ssh.md#the-ssh-directorys-config)). To use another key, edit
  `IdentityFile` there.
- `proxy` asks `ssh -G <name>` for `user` and the `identityfile` lines, in ssh's order.
  With `--user`, it asks `ssh -G -l <user> <name>`, so that `Match user` blocks are read
  as ssh reads them.
  `ssh -G` prints them unexpanded, so `proxy` expands a leading `~`, `%d` (home), `%u`
  (local user), `%r` (remote user), and `%%`; any other token, or `${NAME}`, stops with
  `temt cannot expand IdentityFile <value>; write it as a path`.
- It sends the `.pub` next to the first key that has one, as `<type> <body>`; the
  comment is not sent. A passphrase-protected key works if its `.pub` is there. An
  `IdentityFile` that is itself a `.pub` (the private key held by ssh-agent or a
  hardware key) is sent as it is. With
  none: `no public key for 'ssh <name>': no .pub next to its IdentityFile (<values>);
  put the .pub next to the key, or delete IdentityFile from the Host <name> block and
  run 'temt apply' to use the default key`. This is decided before calling AWS.
- The OS user sent with the key, and the one `%r` expands to, is the `--user` value
  (`%r` from `ProxyCommand`), else `ssh -G`'s `user`. A user given on ssh's command line
  must follow the user-name rule ([commands/app.md](../commands/app.md#users)); one
  that does not ends `proxy` with a usage error.
- `~user/` is not expanded; such a path is taken as relative, has no `.pub`, and is
  skipped.
- `ssh -G` is asked about `<name>`, not the name typed. With an alias (`Host
  temt-dev-main dev`, `ssh dev`), `IdentityFile` lines that only `Host dev` or `Match
  originalhost dev` give are not seen. The `Include` at the top of `~/.ssh/config` puts
  the block's own lines first, so this matters only when none of those has a `.pub`.
  Passing `%n` in `ProxyCommand` would fix it but changes the ssh config temt writes;
  the requester chose to leave it.

## The default key

`~/.ssh/temt/keys/temt` (the `keys/` of the ssh directory): one ED25519 key without a
passphrase, comment `temt`, shared by every service. `destroy` keeps it.

In step 6 of [`apply`](../commands/service.md#apply), if the block (after its edit)
uses the default key, that is, its first `IdentityFile` line is the default value:

- Neither the key nor its `.pub` exists: create both with `ssh-keygen -t ed25519`
  before writing the block. `diff` shows `+ key ~/.ssh/temt/keys/temt  ssh-keygen -t
  ed25519`. `ssh-keygen` is needed only then.
- Only the key exists: stop with `<path>.pub does not exist; make it with 'ssh-keygen -y
  -f <path> > <path>.pub'`.
- Only the `.pub` exists: stop with `<path>.pub exists without <path>; remove it to
  create a new key`.

Both stops happen at detection, before confirmation. A key the user put in
`IdentityFile` is neither created nor checked by `apply`.
