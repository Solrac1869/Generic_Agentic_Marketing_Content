#!/bin/bash
# Bring the droplet up to anything committed elsewhere.
#
# sync-github.sh does the other direction: droplet to GitHub, every fifteen
# minutes. There was no return path. Work committed anywhere but the droplet
# -- from a workstation, from a review, from a session that could not open an
# SSH connection -- reached GitHub and stopped there, and the live system went
# on running the old code while the repository said it was fixed. That is the
# worst shape a sync can have, because everything looks correct from the side
# you are looking at.
#
# Deliberately timid. This updates code on a machine that publishes to live
# channels, so every condition that is not plainly safe is a reason to stop
# and say so rather than to press on:
#
#   fast-forward only        never rewrites local commits
#   clean tree only          an uncommitted edit on the droplet is someone's
#                            work in progress and is not ours to stash
#   no agent running         pulling underneath a running agent swaps its code
#                            mid-run, and half-old half-new is unreproducible
#
# When it declines it says which condition failed. A silent skip here would
# reproduce the original bug in a new place.
set -uo pipefail
cd /root/marketing-agents || exit 1

LOG=/root/marketing-agents/state/backup.log
mkdir -p "$(dirname "$LOG")"
now() { date -u +%FT%TZ; }
say() { echo "$(now) pull $*" >> "$LOG"; }

exec 9>"$(pwd)/state/.lock.pull-github"
flock -n 9 || exit 0

# An agent mid-run holds state/.lock.<agent>. flock -n on each tells us
# whether anyone actually holds it, rather than trusting the file's existence:
# a lock file outlives the process that made it.
for f in state/.lock.*; do
    case "$f" in state/.lock.pull-github|"state/.lock.*") continue;; esac
    if ! flock -n "$f" true 2>/dev/null; then
        say "skipped: $(basename "$f" | sed 's/^\.lock\.//') is running"
        exit 0
    fi
done

git fetch -q github 2>/dev/null || { say "fetch failed"; exit 1; }

behind=$(git rev-list --count HEAD..github/main 2>/dev/null || echo 0)
[ "$behind" = "0" ] && exit 0

# "Is the tree dirty" is the wrong question on a live system. The agents write
# brand state -- briefs, publish-state, engage-state, the generated status page
# -- into tracked files as they run, so the tree is dirty essentially always.
# A blanket dirty check would have declined every pull forever while reporting
# that it was working, which is precisely the failure this script exists to
# prevent, reintroduced one layer up.
#
# The question that matters is narrower: does anything arriving touch a file
# that has been changed here? If not, a fast-forward cannot lose local work,
# and git will refuse on its own if that judgement is wrong.
clash=$(comm -12 <(git diff --name-only | sort) \
                 <(git diff --name-only HEAD..github/main | sort))
if [ -n "$clash" ]; then
    say "declined: $behind commit(s) waiting, local edits to ${clash//$'\n'/ }"
    python3 - "$behind" "${clash//$'\n'/, }" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, "/root/marketing-agents")
try:
    from agents.publish import notify
    notify("GitHub has %s commit(s) the droplet has not taken, and the same "
           "files have been edited here, so the pull was declined rather than "
           "overwrite them.\n\nFiles: %s\n\nThe live system is running older "
           "code than the repository says. Commit or discard those edits and "
           "it will catch up on its own within fifteen minutes."
           % (sys.argv[1], sys.argv[2]),
           subject="ARP: droplet is behind and cannot catch up")
except Exception:
    pass
PY
    exit 1
fi

out=$(git merge --ff-only github/main 2>&1)
rc=$?
say "rc=$rc behind=$behind ${out//$'\n'/ }"
[ "$rc" -eq 0 ] || {
    python3 - "$behind" "$out" <<'PY' 2>/dev/null
import sys
sys.path.insert(0, "/root/marketing-agents")
try:
    from agents.publish import notify
    notify("The droplet is %s commit(s) behind GitHub and the fast-forward "
           "failed.\n\n%s\n\nThe histories have diverged, so this needs a "
           "person." % (sys.argv[1], sys.argv[2][:600]),
           subject="ARP: droplet cannot fast-forward")
except Exception:
    pass
PY
    exit 1
}
exit 0
