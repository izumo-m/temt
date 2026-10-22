# sg commands

The `sg` commands handle the ingress rules of the security group
([topics/security-group.md](../topics/security-group.md)).

- `sg list`, `sg add`, and `sg remove` read and write only `[[security-group.ingress]]`
  and do not call AWS. `sg list` reads the config file over the defaults file; `sg add`
  and `sg remove` write only the config file. Only `apply` changes AWS; run `apply` after
  `sg add` and `sg remove` to bring the change to AWS.
- `sg add` and `sg remove` do not check whether the template uses temt's security group;
  `apply` does ([topics/security-group.md](../topics/security-group.md#when-it-is-created)).
  Rejected: stopping them too; without calling AWS they cannot tell temt's security
  group written by ID, and would stop wrongly.
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

Keys already written keep their order and comments.

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

If the file's rules changed since the command read it (for example, edited while the
global IP address was being looked up), exit without writing. If the edited file would
not hold exactly the expected rules, or other keys would change, exit without writing
and tell the user to edit by hand. This can happen with files that split the rules
across other tables, write the `[security-group]` header apart from its rules, or use an
empty inline table (`security-group = {}`).

## `sg list`

`sg list --svc <svc> [--format <columns>]`

Lists the ingress rules in effect, those of the defaults file first, each file in
written order, one per line ([cli.md](../cli.md#list-tables)).

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
- A rule of the defaults file is not added either, and reported as already in that
  file. With `--description` that differs from its description, exit before writing,
  telling the user to edit the description in the defaults file.
- The description follows the same constraints as `Description` in the config file;
  violations are usage errors. An empty description removes the description.
- Reports the added rules and the rules whose description was rewritten, and adds a hint
  to run `apply`. When nothing changed, reports that.

## `sg remove`

`sg remove --svc <svc> <port> [<CIDR>]`

Removes rules from the config file.

- With `<CIDR>` omitted, removes the rules for the CIDRs made from the global IP
  addresses.
- If a named rule is in the defaults file, exit before removing anything, telling the
  user to remove it there. The defaults file applies to every service, so `sg` does not
  edit it.
- Reports rules that do not exist as missing. Reports the removed rules and adds a hint
  to run `apply`.
- If no rule was removed, exits with code 1.

## `sg diff`

`sg diff --svc <svc>`

Writes to stdout how the next `apply` would change the ingress rules, one per line. The
comparison is against the security group named `<name>` in the VPC of the subnet of the
`NetworkInterfaces` element with `DeviceIndex = 0` (the same below): the written
`SubnetId`, or, when it is not written, the subnet looked up in the region as `apply`
does at every run ([Filled when
missing](../topics/launch-template.md#filled-when-missing)).
Whether `apply` would create the security group
([topics/security-group.md](../topics/security-group.md#when-it-is-created)) is not
looked at: without it, every rule is shown as `+`, as before `apply` (decided with the
requester).

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
- Order: `+`, then `~`, then `-`. `+` and `~` follow the order of `sg list`; `-`
  follows the order read from AWS.
- No differences: write nothing to stdout and report that on stderr. The exit code is 0
  whether or not there are differences.
- No security group (before `apply`, or after the subnet was changed to one in another
  VPC): all rules are shown as `+`.

## `sg show`

`sg show --svc <svc>`

Shows the security group: name, ID, VPC, description, tags, and the number of
ingress rules on the AWS side.

- The VPC is that of the subnet, written or looked up as in `sg diff`. If the security
  group is not in that VPC, shows that the next `apply` creates it.
- If security groups with the same name exist in other VPCs of the region, lists their
  ID and VPC and notes that `destroy` removes them.

The contents of the ingress rules are not shown: the definition is seen with `sg list`,
and the differences from AWS with `sg diff`.
