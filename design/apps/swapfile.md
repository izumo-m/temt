# swapfile

Bundled app that adds the swap file `/swapfile` and uses it at every boot. Usage:
[docs/apps/swapfile.md](../../docs/apps/swapfile.md). Source: `src/temt/apps/swapfile/`.
Runs as root.

Option `size` (default `1g`, pattern `[1-9][0-9]*[mg]`): MiB or GiB.

`install` and `uninstall` read the paths and the shared steps from `common.sh`. Both are
idempotent.

## install

Before changing anything, check the following and fail if one does not hold. A failure
here changes nothing, so the deploy record stays as it was.

1. Convert `size` to bytes. More than 9 digits is refused, since bash integers would
   overflow.
2. `/swapfile` is missing or a regular file (not a symlink, directory, or other type):
   `install: /swapfile is not a regular file; move it away and deploy again`.
3. If `/swapfile` is in use as swap and has the same size, leave the file alone; a
   redeploy and an instance launched from a baked AMI take this path. If it is not in
   use, recreate it even at the same size, since it is unknown whether it holds the swap
   format.
4. When recreating: disk space. After creating, at least 1 GiB must remain free on the
   file system of `/`, counting the space the current `/swapfile` frees. Then memory, if
   the current file is in use: what is swapped out plus 256 MiB must fit in
   `MemAvailable` (`/proc/meminfo`); otherwise `swapoff` could make the OOM killer stop
   other processes. The message tells the user to reboot the instance, then deploy
   again.

To recreate: `swapoff` if in use, remove the file, allocate it with `fallocate` (no data
written, so even a large file is quick) under umask 077 so it is readable only by root
from the start, `mkswap`, and `swapon`.

Then, for an existing file too, `chown root:root` and `chmod 600`. Set the `/swapfile`
line of `/etc/fstab` to the line below; this happens after `swapon` succeeded. Other
lines are kept. The file is rewritten only when it changes, through a temporary file in
`/etc` renamed over it with the same mode, followed by `systemctl daemon-reload`.

    /swapfile none swap sw,nofail 0 0

Finally, check that `/swapfile` is in use as swap and print
`swapfile: /swapfile <size>, active, <used> used`.

systemd turns the fstab swap line into the unit `swapfile.swap` under `swap.target`.
`swap.target` is only wanted (`Wants=`) by `sysinit.target`, so unlike file systems
under `local-fs.target`, a missing or broken `/swapfile` does not drop boot into
emergency mode; boot continues without swap. `nofail` keeps `swap.target` itself from
being marked failed, leaving the failure on `swapfile.swap` only.

## uninstall

If `/swapfile` is in use, run the same memory check as `install` (the message says to
reboot, then run `app remove` again), then `swapoff`. Remove the `/swapfile` line from
`/etc/fstab` the same way, and remove `/swapfile` if it is a regular file (not a
symlink).
