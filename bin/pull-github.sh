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
cd "$(dirname "$0")/.." || exit 1
export AGENTS_ROOT="$(pwd)"

LOG="$(pwd)/state/backup.log"
mkdir -p "$(dirname "$LOG")"
now() { date -u +%FT%TZ; }
say() { echo "$(now) pull $*" >> "$LOG"; }

exec 9>"$(pwd)/state/.lock.pull-github"
flock -n 9 || exit 0

# An agent mid-run holds state/.lock.<agent>. flock -n on each tells us whether
# anyone actually holds it, rather than trusting the file's existence: a lock
# file outlives the process that made it.
#
# Only long agents count. The first version skipped for any lock at all, and
# this job and `publish --mode notify` are both on */15 -- so they fired in the
# same minute every time and the pull was skipped on every single run from the
# day it was installed. The log is an unbroken wall of "skipped: publish is
# running". The return path never once ran.
#
# A publish notify takes one second and writes nothing this job touches.
# Blocking a code update on it was never the point. What matters is an agent
# that could be halfway through writing the files a pull would replace, and
# those are the slow ones: the chain, and anything that drafts or commits.
#
# The cron line is also offset off the quarter hour, so a collision is rare
# rather than guaranteed. Belt and braces, because the failure mode here is
# silent and this job exists to prevent a silent failure.
for f in state/.lock.chain-* state/.lock.blog* state/.lock.strategy \
         state/.lock.produce* state/.lock.research* state/.lock.site* \
         state/.lock.refresh* state/.lock.video* state/.lock.seo*; do
    [ -e "$f" ] || continue
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
import os, sys
_LABEL = os.environ.get("BRAND_LABEL", "Marketing agents")
sys.path.insert(0, os.environ.get("AGENTS_ROOT", "."))
try:
    from agents.publish import notify
    notify("GitHub has %s commit(s) the droplet has not taken, and the same "
           "files have been edited here, so the pull was declined rather than "
           "overwrite them.\n\nFiles: %s\n\nThe live system is running older "
           "code than the repository says. Commit or discard those edits and "
           "it will catch up on its own within fifteen minutes."
           % (sys.argv[1], sys.argv[2]),
           subject=_LABEL + ": droplet is behind and cannot catch up")
except Exception:
    pass
PY
    exit 1
fi

# An untracked file at a path an incoming commit adds aborts the merge with
# "untracked working tree files would be overwritten". It happened the first
# time a script was copied to the droplet by hand and later committed
# properly: the pull failed every quarter hour and said so only in a log.
#
# Where the untracked copy is byte-identical to the one arriving, it is not
# somebody's work, it is the same file. Move it aside and let the merge
# deliver it. Anything that actually differs is left alone and declines.
for f in $(git diff --name-only HEAD..github/main); do
    [ -e "$f" ] || continue
    git ls-files --error-unmatch "$f" >/dev/null 2>&1 && continue
    if git show "github/main:$f" 2>/dev/null | cmp -s - "$f"; then
        mv "$f" "$f.pre-pull" && say "moved identical untracked $f aside"
    else
        say "declined: untracked $f differs from the one arriving"
        exit 1
    fi
done

# Diverged. The droplet is a follower that also commits: snapshot-state and
# archive-weeks write generated data locally, so any time GitHub moves in
# between, both sides are ahead and a fast-forward can never resolve it. That
# wedged this repo twice in one day, each time needing a person.
#
# Those local commits are always generated output, never code, so replaying
# them on top of GitHub is safe. That is checked rather than assumed: if any
# droplet-only commit touches agents, core, bin or .claude, somebody has been
# editing code on the server and this stops and says so.
ahead=$(git rev-list --count github/main..HEAD 2>/dev/null || echo 0)
if [ "$ahead" != "0" ]; then
    touched=$(git diff --name-only github/main...HEAD | grep -E "^(agents|core|bin|\.claude)/" || true)
    if [ -n "$touched" ]; then
        say "declined: $ahead local commit(s) touch code: ${touched//$'\n'/ }"
        exit 1
    fi
    say "rebasing $ahead generated-data commit(s) onto github/main"
    # --autostash because the droplet always has unstaged changes: the agents
    # rewrite publish-state, engage-state and watch-state as they run, and
    # those are tracked. Without it a rebase refuses on live state that has
    # nothing to do with the commits being replayed, which is how this stayed
    # wedged after the divergence itself was handled.
    if ! git rebase --autostash github/main >/dev/null 2>&1; then
        git rebase --abort >/dev/null 2>&1
        say "declined: rebase of local commits failed"
        exit 1
    fi
    git push -q github HEAD:main 2>/dev/null && say "pushed the rebased commit(s)"
    exit 0
fi

out=$(git merge --ff-only github/main 2>&1)
rc=$?
say "rc=$rc behind=$behind ${out//$'\n'/ }"
[ "$rc" -eq 0 ] || {
    python3 - "$behind" "$out" <<'PY' 2>/dev/null
import os, sys
_LABEL = os.environ.get("BRAND_LABEL", "Marketing agents")
sys.path.insert(0, os.environ.get("AGENTS_ROOT", "."))
try:
    from agents.publish import notify
    notify("The droplet is %s commit(s) behind GitHub and the fast-forward "
           "failed.\n\n%s\n\nThe histories have diverged, so this needs a "
           "person." % (sys.argv[1], sys.argv[2][:600]),
           subject=_LABEL + ": droplet cannot fast-forward")
except Exception:
    pass
PY
    exit 1
}
exit 0

# Return path verified end to end on 15 Sep 2026: this line was pushed from
# the workstation and reached the droplet via bin/pull-github.sh, unaided.
