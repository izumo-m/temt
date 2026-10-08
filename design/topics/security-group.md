# Security group

Step 5 of [`apply`](../commands/service.md#apply) reconciles it; step 4 of
[`destroy`](../commands/service.md#destroy) deletes it. Its name is `<name>`. The `sg`
commands ([commands/sg.md](../commands/sg.md)) show and edit the ingress rule definition
in the config file; they never write to AWS.

## Reconciling

- Created in the VPC of the config file's `SubnetId`, with description `Managed by temt`
  and two tags, `Name` and `temt:service`.
- If it exists, missing tags are added and tags with a different value are corrected
  (`create-tags` overwrites the value of the same key). Other tags are not touched.
- A security group of the same name in another VPC is not an error and is left as it is
  (a group name is unique only within a VPC, so it is a different security group).
- Reported as `security group <name>: created (<group ID>)` or
  `security group <name>: updated (<group ID>)`; unchanged is not reported.

## Ingress rules

The ingress rules are defined in the config file's `[[security-group.ingress]]`
(protocol, port range, source CIDR, description), and `apply` makes AWS match it. With
no definition there are no ingress rules, and login is through Session Manager only.
Egress rules are never touched.

- Protocols are `tcp`, `udp`, and `icmp`. `icmp` has no port and allows every ICMP type.
  With an IPv6 source CIDR, it is passed to AWS as `icmpv6`.
- A rule is identified by the tuple (protocol, port range, source CIDR). AWS identifies
  rules by the same tuple, and a rule carries exactly one description. The port range of
  `icmp` is compared in AWS's form (`-1` to `-1`).
- AWS's protocol numbers are mapped to names (`6` `tcp`, `17` `udp`, `1` `icmp`, `58`
  `icmpv6`) and AWS's CIDRs are normalized before comparing.

Reconciliation is a diff. `describe-security-group-rules` (filter `group-id`) reads the
security group's ingress rules one by one; they are compared with the definition, and
the following runs in this order:

1. Rules in the definition and not in AWS are added in one
   `authorize-security-group-ingress` call.
2. Rules in both whose description alone differs get their description fixed with
   `update-security-group-rule-descriptions-ingress` (an empty description is sent by
   omitting `Description`, which clears it). Not revoke and re-add, because traffic
   would be blocked in between.
3. Rules in AWS and not in the definition are revoked in one
   `revoke-security-group-ingress` call by rule ID. Kinds of rules temt does not handle
   (references to other security groups, prefix lists, protocols other than `tcp`,
   `udp`, `icmp`, `icmpv6`, and ICMP rules narrowed to specific types) are not in the
   definition, so they are removed here too.

Because rules are added before removed, the rules in the definition keep passing
traffic throughout. With no differences nothing is done. Each added, re-described, and
removed rule is reported on one line:

- `security group <name>: added <port> <source> <description>`
- `security group <name>: updated the description of <port> <source>: <before> -> <after>`
- `security group <name>: removed <port> <source> <description>`

While adding, the group temporarily holds the rules to be removed as well, so replacing
many rules near the per-security-group ingress rule quota (60 by default) stops at the
add. In that case, drop the rules to remove from the definition and `apply` first, then
add the new rules in a separate `apply`.

## Global IP address

When the CIDR is omitted, `sg add` and `sg remove` look up the client's global IP
address and use it as the CIDR ([commands/sg.md](../commands/sg.md)). IPv4 and IPv6 are
looked up separately. The global IP address is the client's address as seen from the
Internet; through a VPN it is the VPN's exit address.

| Kind | Queried URL | CIDR |
| --- | --- | --- |
| IPv4 | `https://checkip.amazonaws.com` | `/32` |
| IPv6 | `https://api6.ipify.org` | `/64` |

- Each host has DNS records of its own address family only, so the query always goes
  over that family and returns a global IP address of that family.
- The response body (`GET`, `User-Agent: temt`, at most 100 bytes read) is parsed as an
  IP address; a different family counts as not found. An address with a zone ID, and
  loopback, unspecified, link-local, and multicast addresses, also count as not found.
- Connect and read each wait at most 5 seconds. Name resolution has no deadline, so one
  query as a whole is not guaranteed to finish in 5 seconds.
- IPv6 uses `/64` because most OSes use temporary addresses (privacy extensions, rotated
  about daily) for outgoing connections, and a `/128` would stop matching soon. A `/64`
  is one LAN, the same scope as allowing every device behind an IPv4 NAT by one address.
- Whether the subnet has an IPv6 CIDR is not checked, to avoid calling AWS. An IPv6 rule
  has no effect unless the instance has an IPv6 address, but it does no harm.
- Only the families found are used. A found family is reported as
  `egress IPv<n> address: <address> (<url>); using <cidr>`; a family not found is
  reported on one line with the reason (`no IPv<n> egress address: ...`). If neither is
  found, temt exits with `could not find the egress IP address; give the CIDR`.

## Changing the VPC

When `SubnetId` is changed to a subnet in another VPC, `apply` prepares the security
group in the new VPC and the launch template points to it. A running instance keeps
running with the old security group; from the next `start` it launches in the new VPC.
The security group left in the old VPC is deleted by `destroy`.

## Deleting

`destroy` deletes every security group named `<name>` in the region, regardless of VPC
(in group ID order). It has waited for the instances to be `terminated`, and by then
their network interfaces have left the group (checked on real AWS: the first deletion
succeeded), so it does not wait again. If deletion still fails with
`DependencyViolation` (an interface not yet detached, or something else referring to the
group, such as another security group's rule; AWS's text does not tell them apart), it
stops with `cannot delete the security group <group ID>: <AWS error>; if its instances
were just terminated, run destroy again; otherwise remove what uses it and run destroy
again`. Each is reported as `security group <name> (<group ID>): deleted` (or `absent`
if it vanished, `InvalidGroup.NotFound`); with none found,
`security group <name>: absent`.
