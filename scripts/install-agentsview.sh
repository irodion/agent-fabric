#!/usr/bin/env bash
# Install the agentsview binary that .github/workflows/build-agentsview.yml released, after
# checking its SHA-256 and that the attestation was made by that workflow on main.
# Run on the desktop from a clone of this repo, with gh logged in.
# Usage: scripts/install-agentsview.sh [release]   (default: agentsview-<tag in vendor/agentsview.pin>)
# Env: AGENT_FABRIC_REPO (default: this clone's GitHub repo), BIN_DIR (default: ~/.local/bin)
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
tag=$(sed -n 's/^tag=//p' "$root/vendor/agentsview.pin")
release=${1:-agentsview-$tag}
repo=${AGENT_FABRIC_REPO:-$(cd "$root" && gh repo view --json nameWithOwner --jq .nameWithOwner)}
bin_dir=${BIN_DIR:-$HOME/.local/bin}
license_dir=${XDG_DATA_HOME:-$HOME/.local/share}/agent-fabric/licenses

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

gh release download "$release" --repo "$repo" --dir "$work" \
	--pattern agentsview --pattern agentsview.LICENSE --pattern SHA256SUMS
(cd "$work" && sha256sum -c SHA256SUMS)
gh attestation verify "$work/agentsview" --repo "$repo" \
	--signer-workflow "$repo/.github/workflows/build-agentsview.yml" \
	--source-ref refs/heads/main --deny-self-hosted-runners

mkdir -p "$bin_dir" "$license_dir"
install -m 0755 "$work/agentsview" "$bin_dir/.agentsview.new"
mv -f "$bin_dir/.agentsview.new" "$bin_dir/agentsview"
install -m 0644 "$work/agentsview.LICENSE" "$license_dir/agentsview.LICENSE"
"$bin_dir/agentsview" --version
echo "Installed $release from $repo to $bin_dir/agentsview"
