#!/bin/bash
# Push edited skills from the workstation to the system.
#
# The skills are authored in ~/.claude/skills, which only the workstation can
# see. The droplet cannot read it, so the copies in skills/ are what the agents
# actually load. Two copies means drift, and CLAUDE.md carried the instruction
# "must be re-synced by hand" for exactly that reason -- an instruction that
# survives only as long as somebody remembers it.
#
# This is the one thing a workstation legitimately does: it is the only machine
# that can see the source. It edits and pushes; it does not run the system.
# The droplet takes the change from GitHub on its own within fifteen minutes.
#
#   bin/sync-skills.sh            report drift, change nothing
#   bin/sync-skills.sh --apply    copy, commit and push
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

SRC="${SKILLS_SOURCE:-$HOME/.claude/skills}"
APPLY=0; [ "${1:-}" = "--apply" ] && APPLY=1

[ -d "$SRC" ] || { echo "No skill source at $SRC."; echo "This runs on the workstation, where the skills are authored."; exit 2; }

changed=(); missing=()
for d in skills/*/; do
    name=$(basename "$d")
    [ "$name" = "README.md" ] && continue
    src="$SRC/$name/SKILL.md"
    if [ ! -f "$src" ]; then missing+=("$name"); continue; fi
    if ! cmp -s "$src" "$d/SKILL.md"; then
        changed+=("$name")
        [ "$APPLY" = 1 ] && cp "$src" "$d/SKILL.md"
    fi
done

if [ ${#missing[@]} -gt 0 ]; then
    echo "In the repo but no longer in the source (left alone): ${missing[*]}"
fi

if [ ${#changed[@]} -eq 0 ]; then
    echo "Every shipped skill matches its source. Nothing to do."
    exit 0
fi

if [ "$APPLY" = 0 ]; then
    echo "${#changed[@]} skill(s) differ from the source:"
    printf '   %s\n' "${changed[@]}"
    echo
    echo "The agents are loading the older text. Run with --apply to send them."
    exit 1
fi

git add skills/
git commit -q -m "skills: sync $(IFS=,; echo "${changed[*]}") from source

Authored in ~/.claude/skills, which only the workstation can see. The copies
in skills/ are what the agents load, so until this lands the droplet is
writing against the older text." || { echo "nothing staged"; exit 0; }
git push -q origin HEAD || { echo "commit made, push failed"; exit 1; }
echo "${#changed[@]} skill(s) sent. The droplet takes them within fifteen minutes."
