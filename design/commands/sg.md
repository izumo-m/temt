# sg commands

The `sg` commands handle the ingress rules of the security group
([topics/security-group.md](../topics/security-group.md)).

- `sg list`, `sg add`, and `sg remove` read and write only `[[security-group.ingress]]`
  in the config file and do not call AWS. Only `apply` changes AWS; run `apply` after
  `sg add` and `sg remove` to bring the change to AWS.
- `sg diff` and `sg show` read AWS but write nothing.
- None of them takes the lock.

## Arguments

A rule is pointed to by `<port>` and `<CIDR>`. Usage errors are reported before the
config file is read.

- `<port>`: one port such as `22` or a range such as `8000-8010` (within 0 to 65535,
  lower bound not above upper bound), optionally followed by `/tcp` or `/udp`; without a
  suffix it is `tcp`. `icmp` means ICMP and takes no port.
- `<CIDR>`: an IPv4 or IPv6 CIDR. Without `/` it is a single address (`/32` or
  `/128`). As with `Cidr` in the config file, bits set in the host part are a usage
  error. When omitted, temt looks up the client's global IP address, makes it `/32`
  for IPv4 and `/64` for IPv6, and reports the CIDR it used
  ([topics/security-group.md](../topics/security-group.md#global-ip-address)).

## Editing the config file

As when `apply` writes the file ([docs/conf.md](../../docs/conf.md#apply-が書くこと)), keys
already written keep their order and comments.

- Adding: a rule goes after the last rule. With no rules it goes at the end of
  `[security-group]`, and without that, at the end of the file. `Protocol` is not
  written when it is the default `tcp`.
- Removing: the comments before and after the rule stay; blank lines do not pile up.
  When the last rule is removed:
  - If the `[security-group]` header is written, the header and the comments under it
    stay.
  - If no header is written, none is added, and the kept comments go after the
    preceding table. If there is no preceding table, a header is written to keep the
    comments.
- No config file: tell the user to run `apply`, and exit.

Before writing, the config file is read again. If its rules differ from the first read
(for example, edited while the global IP address was being looked up), exit without
writing. The result of the edit is also read back. If the rules differ from the
expected ones, or other keys of the file changed, exit without writing and tell the user
to edit by hand. This can happen with files that split the rules across other tables,
write the `[security-group]` header apart from its rules, or use an empty inline table
(`security-group = {}`).

## `sg list`

`sg list --svc <svc> [--format <columns>]`

Lists the ingress rules of the config file in written order, one per line
([cli.md](../cli.md#list-tables)).

| Column | Default | Value |
| --- | --- | --- |
| `PORT` | yes | `22/tcp`, `8000-8010/udp`, or `icmp`; can be passed as is to `<port>` of `sg remove` |
| `CIDR` | yes | `Cidr` |
| `DESCRIPTION` | yes | `Description` |

## `sg add`

`sg add --svc <svc> <port> [<CIDR>] [--description <description>]`

Adds a rule to the config file.

- With `<CIDR>` omitted and both IPv4 and IPv6 addresses found, adds two rules.
- If a rule with the same protocol, port, and CIDR exists, it is not added; with
  `--description`, only its description is rewritten.
- The description follows the same constraints as `Description` in the config file;
  violations are usage errors. An empty description removes the description.
- Reports the added rules and the rules whose description was rewritten, and adds a hint
  to run `apply`. When nothing changed, reports that.

## `sg remove`

`sg remove --svc <svc> <port> [<CIDR>]`

Removes rules from the config file.

- With `<CIDR>` omitted, removes the rules for the CIDRs made from the global IP
  addresses.
- Reports rules that do not exist as missing. Reports the removed rules and adds a hint
  to run `apply`.
- If no rule was removed, exits with code 1.

## `sg diff`

`sg diff --svc <svc>`

Writes to stdout how the next `apply` would change the ingress rules, one per line. The
comparison is against the security group named `<name>` in the VPC that `SubnetId`
belongs to.

- Each line starts with `+` (add), `~` (fix the description), or `-` (remove), followed
  by `PORT`, the source, and the description, aligned with spaces. On a `~` line the
  description is `<current description> -> <new description>`. A missing description is
  written `-`.
- Rules temt cannot express are written with `PORT` in the form
  `<protocol>:<lower port or ICMP type>:<upper port or code>`.
  - Protocols without ports are written as AWS returned them (a number).
  - All protocols are `all`.
  - When both values are -1 (no ports, or all ICMP types), only the protocol is written
    (`all`, `icmpv4`, `50`).
  - ICMP whose family matches the source (`icmp` with IPv4, `icmpv6` with IPv6) is
    `icmp` (e.g. `icmp:8:-1`). IPv4 ICMP with an IPv6 source is `icmpv4`; ICMPv6 with
    an IPv4 source is `icmpv6`.
  - The source is the ID of another security group or of a prefix list, or `?` if it
    cannot be read.
- Order: `+`, then `~`, then `-`. `+` and `~` follow the config file order; `-`
  follows the order read from AWS.
- No differences: write nothing to stdout and report that on stderr. The exit code is 0
  whether or not there are differences.
- No security group (before `apply`, or after `SubnetId` was changed to one in another
  VPC): all rules are shown as `+`.
- No `SubnetId` written: report that the next `apply` picks one, and show all rules as
  `+`.

## `sg show`

`sg show --svc <svc>`

Shows the security group: name, ID, VPC, description, tags, and the number of
ingress rules on the AWS side.

- If it is not in the VPC of `SubnetId`, shows that the next `apply` creates it.
- If security groups with the same name exist in other VPCs, lists their ID and VPC and
  notes that `destroy` removes them.
- If `SubnetId` is not written, the VPC is undetermined: shows that, and only lists the
  security groups with the same name by ID and VPC.

The contents of the ingress rules are not shown: the definition is seen with `sg list`,
and the differences from AWS with `sg diff`.
