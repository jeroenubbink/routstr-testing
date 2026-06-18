#!/usr/bin/env bash
set -euo pipefail

VENDOR_DIR="$(cd "$(dirname "$0")/.." && pwd)/vendor"
mkdir -p "$VENDOR_DIR"

# Each vendor tracks `main` by default. The ref is overridable per repo via env
# (ROUTSTR_CORE_REF / ROUTSTRD_REF / ROUTSTR_CLI_REF) so you can VERIFY an
# unmerged PR locally before it lands. For a PR that comes from a FORK, the
# branch does not exist on the upstream remote, but GitHub exposes the PR head
# as `refs/pull/<N>/head` on the BASE repo — so fetch that, no fork URL needed:
#   ROUTSTR_CORE_REF=refs/pull/549/head make ...    # fork or upstream PR
#   ROUTSTR_CORE_REF=some-upstream-branch make ...  # plain upstream branch
# This is a verification override only; the committed default stays `main`, so
# CI always tests against shipped code. Do NOT commit a non-main value anywhere
# (a branch/PR ref rots once the PR merges and the branch/PR closes; a SHA
# freezes you off main). The resolved commit is recorded in vendor/COMMITS.txt.
REPOS=(
  "vendor/routstr-core|https://github.com/Routstr/routstr-core.git|${ROUTSTR_CORE_REF:-main}"
  "vendor/routstrd|https://github.com/Routstr/routstrd.git|${ROUTSTRD_REF:-main}"
  "vendor/routstr-cli|https://github.com/Routstr/routstr-cli.git|${ROUTSTR_CLI_REF:-main}"
)

COMMITS_FILE="$VENDOR_DIR/COMMITS.txt"
> "$COMMITS_FILE"

for entry in "${REPOS[@]}"; do
  rel_path="${entry%%|*}"
  rest="${entry#*|}"
  url="${rest%%|*}"
  ref="${rest##*|}"
  abs_path="$(cd "$(dirname "$0")/.." && pwd)/$rel_path"

  # Bootstrap a shallow checkout if absent (clones the remote's default branch);
  # the fetch below then moves it onto the requested ref.
  if [ ! -d "$abs_path/.git" ]; then
    echo "Cloning $url into $rel_path ..."
    git clone --depth 1 "$url" "$abs_path"
  fi

  echo "Syncing $rel_path -> $ref ..."
  # Fetch from the URL literal (not a named remote) so the ref resolves the same
  # for a fresh or pre-existing checkout regardless of how `origin` is set, and
  # accept any ref form — branch name OR refs/pull/<N>/head. Reset to FETCH_HEAD
  # because an arbitrary ref has no local remote-tracking branch.
  git -C "$abs_path" fetch --depth 1 "$url" "$ref"
  git -C "$abs_path" reset --hard FETCH_HEAD

  commit=$(git -C "$abs_path" rev-parse HEAD)
  echo "$rel_path $commit" >> "$COMMITS_FILE"
done

echo ""
echo "vendor/COMMITS.txt:"
cat "$COMMITS_FILE"
