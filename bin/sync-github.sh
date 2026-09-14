#!/bin/bash
# Keep GitHub level with the droplet, without anyone having to ask.
#
# bin/backup.sh already pushes, but once a night at 03:40. Anything committed
# during the day therefore lived on one machine in one data centre for up to
# twenty-four hours. On 14 Sept three commits sat unpushed through a working
# morning and it took a person noticing to move them.
#
# This does one thing: if there are committed changes GitHub has not got, send
# them. It never commits, never touches the working tree, and a push with
# nothing to do costs a few milliseconds, so it can run often.
set -uo pipefail
cd /root/marketing-agents || exit 1

LOG=/root/marketing-agents/state/backup.log
mkdir -p "$(dirname "$LOG")"
now() { date -u +%FT%TZ; }

# One run at a time. A slow push on a congested link must not stack up behind
# itself every fifteen minutes.
exec 9>"$(pwd)/state/.lock.sync-github"
flock -n 9 || exit 0

git fetch -q github 2>/dev/null
ahead=$(git rev-list --count github/main..HEAD 2>/dev/null || echo 0)
[ "$ahead" = "0" ] && exit 0

out=$(git push github HEAD:main 2>&1)
rc=$?
echo "$(now) sync rc=$rc ahead=$ahead ${out//$'\n'/ }" >> "$LOG"
[ "$rc" -eq 0 ] && exit 0

# Email, not Telegram. Telegram was retired once the board could take a
# decision rather than only report one, and an alert saying the only copy of
# the work is on this droplet is the last one that should go to a dead channel.
python3 - "$ahead" "$out" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, "/root/marketing-agents")
try:
    from agents.publish import notify
    notify("The droplet is %s commit(s) ahead of GitHub and the push failed.\n\n"
           "%s\n\nUntil this succeeds the only copy of that work is on the "
           "droplet." % (sys.argv[1], sys.argv[2][:600]),
           subject="ARP: code is not backed up")
except Exception:
    pass
PY
exit 1
