#!/usr/bin/env bash
# Start an agentsview binary on a throwaway home and data dir, and check that the API answers.
# Usage: smoke-agentsview.sh <binary> [port]
set -euo pipefail

bin=$1 port=${2:-18080}
work=$(mktemp -d)
log="$work/serve.log"
export HOME="$work/home" AGENTSVIEW_DATA_DIR="$work/data" AGENTSVIEW_TELEMETRY_ENABLED=0
mkdir -p "$HOME" "$AGENTSVIEW_DATA_DIR"

"$bin" --version

"$bin" serve --no-browser --no-update-check --host 127.0.0.1 --port "$port" >"$log" 2>&1 &
pid=$!
trap 'kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true' EXIT

url="http://127.0.0.1:$port/api/v1/sessions?include_one_shot=true"
for _ in $(seq 60); do
	if body=$(curl -fsS "$url" 2>/dev/null); then
		jq -e 'type == "object"' >/dev/null <<<"$body"
		echo "API answered: $(jq -c 'with_entries(select(.value | type != "array"))' <<<"$body")"
		exit 0
	fi
	if ! kill -0 "$pid" 2>/dev/null; then
		echo "::error::agentsview exited before the API answered"
		cat "$log"
		exit 1
	fi
	sleep 1
done
echo "::error::no API response from $url within 60 s"
cat "$log"
exit 1
