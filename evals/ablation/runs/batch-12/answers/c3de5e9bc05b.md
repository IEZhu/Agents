Your schedule has two bugs, so you need two lines. Here they are, then the reasons.

```cron
# m     h     dom  mon dow  command
*/15    8-17  1-7  *   *    [ "$(date +\%u)" -le 5 ] && /usr/bin/flock -n /run/lock/sync.lock /opt/sync/run.sh >> /var/log/sync.log 2>&1
0       18    1-7  *   *    [ "$(date +\%u)" -le 5 ] && /usr/bin/flock -n /run/lock/sync.lock /opt/sync/run.sh >> /var/log/sync.log 2>&1
```

Add them with `sudo crontab -e`. Root's personal crontab has no user field.

## What was wrong with `*/15 8-18 1-7 * 1-5`

1. **It runs after 18:00.** `*/15` with hour `8-18` fires at 18:15, 18:30 and 18:45 too. The fix is two lines: `*/15 8-17` covers 08:00 to 17:45, and `0 18` adds exactly 18:00.
2. **Day-of-month and day-of-week are OR'd, not AND'd.** When both fields are restricted (neither is `*`), cron runs the job if *either* one matches. Your line would run on days 1-7 of the month (weekends included) **and** on every Monday-Friday of the month. The fix is to keep `1-7` in the day-of-month field, set day-of-week to `*`, and check the weekday in the command. `date +%u` gives 1=Mon through 7=Sun, so `-le 5` means Mon-Fri.

## The wrapper

- **`flock -n`** (from util-linux, at `/usr/bin/flock` on Ubuntu) takes an exclusive lock on the lock file before starting the script. If the previous run still holds the lock, the new one exits at once instead of waiting or running alongside it. Both lines use the **same** lock file, so the 18:00 run can't overlap a slow 17:45 run either.
- The lock is tied to the open file descriptor. If the script crashes, the kernel releases it, so you never get a stale lock. `/run/lock` is tmpfs and gets cleared on reboot, which is fine.
- **`\%`** matters: in a crontab an unescaped `%` becomes a newline. Without the backslash the `date` test breaks.
- **`>> /var/log/sync.log 2>&1`** appends both stdout and stderr. It applies to the whole `flock ... run.sh` command.

## Optional tweaks

- **Log skipped runs.** At the moment a skipped run leaves no trace. `flock -n -E 75 ...` makes flock exit with 75 when the lock is busy, so you can tell a skip apart from a script failure, e.g. `...; [ $? -eq 75 ] && echo "$(date) skipped: previous run active" >> /var/log/sync.log`. That makes the line noticeably longer.
- **Make sure 18:00 always runs.** If the last run must happen even when 17:45 is still going, use `flock -w 600` (wait up to 10 minutes) on the 18:00 line instead of `-n`.
- **Rotate the log.** `/var/log/sync.log` will keep growing, so add a small `/etc/logrotate.d/sync` entry.

## Verify

```bash
sudo crontab -l                                   # lines present
grep CRON /var/log/syslog | grep sync             # cron actually firing them
sudo flock -n /run/lock/sync.lock true; echo $?   # 0 = lock free, 1 = a run is active
```

**Rollback:** run `sudo crontab -e` and delete the two lines. Nothing else was changed.
