#!/bin/bash
# Wrapper for scheduled runs.
#
# On the droplet the credentials file is local, so it is sourced directly. The
# SSH fallback exists only so the script still works when run by hand from the
# Mac; a scheduled run should never depend on another host being reachable.
#
#   run-agent.sh <agent> [extra args...]
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

ENV_FILE=/etc/marketing-agents.env
# Only used by the fallback that fetches credentials over ssh when this
# is run from a workstation. On the server itself the env file is local
# and this is never touched.
DROPLET="${AGENT_HOST:-}"
LOG="$(pwd)/state/agent.log"
mkdir -p "$(dirname "$LOG")"

if [ -r "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
else
  fetch() { ssh -n -o BatchMode=yes -o ConnectTimeout=15 "$DROPLET" \
    "grep -m1 ^$1= $ENV_FILE | cut -d= -f2-" 2>/dev/null; }
  export ANTHROPIC_API_KEY="$(fetch ANTHROPIC_API_KEY)"
  export TELEGRAM_BOT_TOKEN="$(fetch TELEGRAM_BOT_TOKEN)"
  export TELEGRAM_CHAT_ID="$(fetch TELEGRAM_CHAT_ID)"
  export BREVO_API_KEY="$(fetch BREVO_API_KEY)"
  export RELAY_SECRET="$(fetch RELAY_SECRET)"
fi

# Shout, do not shrug. A run that cannot authenticate must not look like a run
# that had nothing to do.
# Email, not Telegram. Telegram was retired once the board could take a
# decision rather than only report one, and a failure alert that arrives where
# nothing can be done about it is the weakest kind of alert there is.
alert() {
  python3 - "$1" <<PY 2>/dev/null
import sys
sys.path.insert(0, "/root/marketing-agents")
try:
    from agents.publish import notify
    notify(sys.argv[1], subject="ARP: an agent failed")
except Exception:
    pass
PY
}

if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  echo "$(date -u +%FT%TZ) FATAL: no credentials available" >> "$LOG"
  alert "Marketing agents: FATAL, no credentials on $(hostname). Nothing ran."
  exit 1
fi

AGENT="$1"; shift

# One run of an agent at a time. The Sunday chain puts four blog runs fifteen
# minutes apart, and an Opus article plus a hero image plus a git push can
# outlast that gap. Two overlapping runs pick the same outstanding item, write
# it twice, and race each other in the same clone. Waiting is wrong here: the
# next cron slot will come round, so a second starter exits and leaves a line
# in the log saying why.
# Keyed on agent AND mode. strategy --mode watch runs every 15 minutes and
# shares its clock with the Sunday build, so a single per-agent lock meant the
# watch could hold it while the build was skipped, which is a coin toss for
# whether the week gets planned at all.
LOCK="$(pwd)/state/.lock.$AGENT$(echo "$*" | tr -c "A-Za-z0-9" "-")"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date -u +%FT%TZ) $AGENT SKIPPED, a run is already in progress" >> "$LOG"
  exit 0
fi

START="$(date -u +%FT%TZ)"
echo "$START --- $AGENT $* ---" >> "$LOG"

# Capture the status BEFORE anything else runs. The previous version read $? on
# a line beginning with a $(date) substitution, so it always logged the exit
# code of date, which is to say always zero. Four consecutive failures were
# recorded as successes.
python3 core/orchestrator.py --brand arp --agent "$AGENT" "$@" >> "$LOG" 2>&1
rc=$?

NOW="$(date -u +%FT%TZ)"
echo "$NOW $AGENT exit=$rc" >> "$LOG"

# Record the run where status can read it directly. It used to recover this by
# parsing agent.log, which grows without bound, so the parser read only a
# window of it and every weekly agent fell outside that window and was reported
# as never having run. One file per agent rather than one shared file, because
# several agents run concurrently and a shared file would need a lock. Written
# to a temp name and moved, so a reader never sees a half written file.
RUNS="$(pwd)/state/runs"
mkdir -p "$RUNS"
printf '{"agent":"%s","mode":"%s","started":"%s","finished":"%s","exit":%d}\n' \
  "$AGENT" "$*" "$START" "$NOW" "$rc" > "$RUNS/.$AGENT.tmp" 2>/dev/null \
  && mv -f "$RUNS/.$AGENT.tmp" "$RUNS/$AGENT.json" 2>/dev/null
# verify sends its own itemised report, so a generic alert here is the same
# event delivered twice.
if [ "$rc" -ne 0 ] && [ "$AGENT" != "verify" ]; then
  alert "Marketing agents: $AGENT FAILED on $(hostname), exit $rc.
$(tail -n 4 "$LOG")"
fi
exit $rc
