#!/bin/bash
# Publish Farm Apps.command: double-click to publish an update to the farm apps (playdates + Mollie's bakery).
#
# Claude puts a new delivery (farm-apps.bundle) into _pending/. This script:
#   1. checks the delivery isn't damaged,
#   2. runs the playdate safety test and the bakery check ON THE DELIVERED CODE (stops if anything fails),
#   3. shows what changed, and warns if GitHub has changes the delivery doesn't include,
#   4. on [l]ive: pushes to GitHub with YOUR Mac's GitHub login; Render updates the site in a few minutes,
#   5. updates the code in this folder to match, so Start Farm Apps runs the same version.
# Your test data (data/) and backups are never touched. Nothing here touches Pasture Keeper.

set -euo pipefail
cd "$(dirname "$0")"
HERE="$(pwd)"
BUNDLE="$HERE/_pending/farm-apps.bundle"
CONFIG="$HERE/.publish-config"
WORK="$HERE/.publish-work"

pause() { echo; read -r -p "Press Return to close this window." _; }
trap 'echo; echo "Something went wrong (see above). Nothing was published."; pause' ERR

echo "== Farm apps: publish =="
echo

if [ ! -f "$BUNDLE" ]; then
  echo "No delivery waiting in _pending/. Nothing to publish."
  pause; exit 0
fi

if [ ! -f "$CONFIG" ]; then
  echo "First run: paste the GitHub address of the gooseberry-farm-apps repository"
  echo "(looks like https://github.com/YOURNAME/gooseberry-farm-apps.git) and press Return:"
  read -r REPO_URL
  case "$REPO_URL" in
    https://github.com/*/gooseberry-farm-apps.git) ;;
    *) echo "That doesn't look like the gooseberry-farm-apps repository address. Stopping so nothing goes to the wrong place."; pause; exit 1 ;;
  esac
  echo "REPO_URL=\"$REPO_URL\"" > "$CONFIG"
fi
# shellcheck disable=SC1090
source "$CONFIG"

if [ ! -d "$WORK/.git" ]; then
  git init -q "$WORK"
  git -C "$WORK" remote add gh "$REPO_URL"
fi
git -C "$WORK" remote set-url gh "$REPO_URL"

git -C "$WORK" bundle verify "$BUNDLE" >/dev/null 2>&1 || { echo "The delivery file is damaged or incomplete. Ask Claude to deliver it again."; pause; exit 1; }
git -C "$WORK" fetch -q "$BUNDLE" "main:refs/remotes/delivery/main" --force
NEW="$(git -C "$WORK" rev-parse --short refs/remotes/delivery/main)"

# Safety checks on exactly the code that would go live.
if [ ! -x "$HERE/.venv/bin/python" ]; then
  echo "Setting up Python for the checks (first time only, about a minute)..."
  python3 -m venv "$HERE/.venv" && "$HERE/.venv/bin/pip" install -q --upgrade pip && "$HERE/.venv/bin/pip" install -q -r "$HERE/requirements-mac.txt"
fi
git -C "$WORK" checkout -q -f --detach refs/remotes/delivery/main
echo "Running the playdate safety test and the bakery check on this delivery..."
if ! (cd "$WORK" && env -u DATABASE_URL "$HERE/.venv/bin/python" tests/playdate_safety_test.py combined > /tmp/farm-safety.json 2>/tmp/farm-safety.log); then
  echo "STOPPED: the playdate safety test failed on this delivery, so nothing was published."
  grep -A20 '"failures"' /tmp/farm-safety.json || tail -20 /tmp/farm-safety.log
  pause; exit 1
fi
if ! (cd "$WORK" && env -u DATABASE_URL "$HERE/.venv/bin/python" tests/bakery_test.py > /tmp/farm-bakery.json 2>/tmp/farm-bakery.log); then
  echo "STOPPED: the bakery check failed on this delivery, so nothing was published."
  cat /tmp/farm-bakery.json; tail -20 /tmp/farm-bakery.log
  pause; exit 1
fi
echo "All checks passed: $(python3 -c 'import json;d=json.load(open("/tmp/farm-safety.json"));print(d["passed"],"of",d["total"])') playdate, $(python3 -c 'import json;d=json.load(open("/tmp/farm-bakery.json"));print(d["passed"],"of",d["total"])') bakery."

echo
echo "Checking what's on GitHub now..."
if git -C "$WORK" fetch -q gh main 2>/dev/null; then
  LIVE="$(git -C "$WORK" rev-parse --short FETCH_HEAD)"
  git -C "$WORK" update-ref refs/remotes/gh/main FETCH_HEAD
  echo
  if [ "$LIVE" = "$NEW" ]; then
    echo "The live site already has this version ($NEW)."
  else
    echo "Changes in this delivery (newest first):"
    git -C "$WORK" log --format='  - %s' refs/remotes/gh/main..refs/remotes/delivery/main || true
    if ! git -C "$WORK" merge-base --is-ancestor refs/remotes/gh/main refs/remotes/delivery/main; then
      echo
      echo "WARNING: GitHub has changes that this delivery doesn't include."
      echo "Ask Claude before publishing, or those changes would be replaced."
    fi
  fi
else
  echo "GitHub has nothing yet: this will be the first version."
fi

echo
echo "Delivery version: $NEW"
echo "Publishing restarts nothing you'll notice: the server swaps to the new version once it's ready."
read -r -p "Publish [l]ive, or [c]ancel? " CHOICE
case "$CHOICE" in
  l|L)
    git -C "$WORK" push gh "refs/remotes/delivery/main:refs/heads/main"
    # Bring this folder's code up to date (data/, backups/ and .venv/ are left alone).
    git -C "$WORK" archive refs/remotes/delivery/main | tar -x -C "$HERE"
    mkdir -p "$HERE/_published"
    mv "$BUNDLE" "$HERE/_published/farm-apps-$NEW-$(date +%Y%m%d-%H%M).bundle"
    echo
    echo "Published ($NEW). Render is building it now; the live site updates in a few minutes."
    echo "This folder now has the same version, so Start Farm Apps runs it too."
    ;;
  *)
    echo "Cancelled. Nothing was published."
    ;;
esac
pause
