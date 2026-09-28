#!/bin/bash
# Bring a fix across from the system this was extracted from.
#
# Extraction, not a fork, so the two share no history and a port means copying
# files whole. Upstream carries one brand's values inline; here those became
# calls to core.settings. So every copy re-imports the brand and deletes the
# helpers, and the result parses and imports perfectly while referencing
# functions that do not exist -- failing only when one actually runs, which for
# a notification helper is the first time something goes wrong.
#
# Doing that by hand took an afternoon and missed things. It is four steps in a
# fixed order, so it is a script:
#
#   1  copy what upstream changed
#   2  sweep the brand values back out
#   3  put the helpers back
#   4  prove no name is undefined, then that every module imports
#
# Usage:  bin/port-from-upstream.sh ~/Projects/marketing-agents-live
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
SRC="${1:-}"
[ -d "$SRC/agents" ] || { echo "Usage: bin/port-from-upstream.sh <path to upstream checkout>"; exit 2; }

echo "== 1. what has moved upstream =="
python3 bin/upstream-diff.py --source "$SRC" || true

read -r -p "Copy these across? [y/N] " ok
[ "$ok" = "y" ] || { echo "Nothing copied."; exit 0; }

echo
echo "== 2. copying =="
python3 - "$SRC" <<'PY'
import pathlib, shutil, subprocess, sys
src = pathlib.Path(sys.argv[1]); root = pathlib.Path.cwd()
out = subprocess.run(["python3","bin/upstream-diff.py","--source",str(src)],
                     capture_output=True, text=True).stdout
n = 0
for line in out.splitlines():
    rel = line.strip()
    if not rel.startswith(("agents/","core/","bin/")): continue
    s = src / rel
    if not s.is_file(): continue
    (root/rel).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(s, root/rel); n += 1
print("  %d file(s)" % n)
PY
chmod +x bin/*.sh bin/*.py 2>/dev/null

echo
echo "== 3. sweeping the brand back out =="
python3 bin/_debrand_sweep.py --apply  | tail -2
python3 bin/_debrand_sweep2.py --apply | tail -2
python3 bin/_debrand_helpers.py        | tail -2

echo
echo "== 4. proving it =="
fail=0
python3 bin/debrand-report.py --strict || fail=1
echo
echo "undefined names:"
python3 bin/_check_undefined.py . && echo "  none"
echo
echo "imports:"
for m in $(ls agents/*.py core/*.py | sed 's|/|.|;s|\.py$||' | grep -v __init__); do
  out=$(python3 -c "import $m" 2>&1) || { echo "  FAIL $m: $(echo "$out" | tail -1)"; fail=1; }
done
[ "$fail" = 0 ] && echo "  all modules import"

echo
if [ "$fail" = 0 ]; then
  echo "Clean. Record it so the next report shows only what moves after this:"
  echo "    python3 bin/upstream-diff.py --source $SRC --record"
else
  echo "Not clean. Fix the above before recording or committing."
fi
exit $fail
