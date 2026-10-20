# timezone-tokyo

Bundled app that sets the instance's time zone to Asia/Tokyo. Usage:
[docs/apps/timezone-tokyo.md](../../docs/apps/timezone-tokyo.md). Source:
`src/temt/apps/timezone-tokyo/`. No options; runs as root.

`install` runs `timedatectl set-timezone Asia/Tokyo`, which makes `/etc/localtime` a
link to `/usr/share/zoneinfo/Asia/Tokyo`. Then it restarts `cron.service` and
`rsyslog.service` if they are active (they keep the time zone they started with), and
reports the time zone and the local time. It places no
files. Baking keeps `/etc/localtime` in the AMI, so instances launched from it start in
Japan time.

`uninstall` runs `timedatectl set-timezone Etc/UTC` (the Ubuntu AMI default; the
previous time zone is not remembered) and restarts the same units the same way.

Both are idempotent.
