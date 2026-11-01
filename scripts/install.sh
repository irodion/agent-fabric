#!/usr/bin/env bash
# Install kb from this clone and set this machine up for agent notes (docs/design.md §4.10): `uv tool install`,
# then `kb doctor --fix`, which creates the inbox, copies the kb-note skill into the agents' skills folders and adds
# the inbox to Claude Code's and Codex's sandbox write lists. Run it in your own terminal, not in an agent's
# sandbox, which can't write those settings. Rerun it after pulling to update kb and the skill.
# Usage: scripts/install.sh
set -euo pipefail

root=$(cd "$(dirname "$0")/.." && pwd)
if ! command -v uv >/dev/null; then
	echo "install.sh: needs uv: https://docs.astral.sh/uv/getting-started/installation/" >&2
	exit 1
fi

# Not --editable: agents keep the installed kb while this clone switches branches.
uv tool install --reinstall "$root"
bin_dir=$(uv tool dir --bin)
case ":$PATH:" in
*":$bin_dir:"*) ;;
*) echo "install.sh: agents won't find kb: $bin_dir is not on PATH (\`uv tool update-shell\` adds it)" >&2 ;;
esac
"$bin_dir/kb" doctor --fix
