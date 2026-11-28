#!/usr/bin/env bash
# The checks CI runs (.github/workflows/lint.yml), on this machine. Prints one line per check, and a check's output
# only when it fails. A tool that isn't installed here is skipped and named: CI still runs it.
# Usage: scripts/check.sh [--red REV]
#   --red REV  instead of the checks: run today's tests against REV's code, and name the tests new since REV that
#              pass there. Those don't show the change: a test written first fails against the code before it.
set -euo pipefail

usage() {
	sed -n 's/^# Usage: /usage: /p; s/^#   /  /p; s/^#              /             /p' "$0" >&2
	exit 2
}

cd "$(git rev-parse --show-toplevel)"
uv_run=(uv run --locked)
if [[ ! -w "$(uv cache dir)" ]]; then # an agent's sandbox: the real cache is read-only there
	uv_run+=(--cache-dir "${TMPDIR:-/tmp}/uv")
fi

failed=0

step() { # name command...
	local name=$1 output
	shift
	if output=$("$@" 2>&1); then
		printf 'ok       %s\n' "$name"
	else
		printf 'FAILED   %s\n%s\n' "$name" "$output"
		failed=1
	fi
}

optional() { # tool-name command...
	if command -v "$1" >/dev/null; then
		step "$@"
	else
		printf 'skipped  %s (not installed here; CI runs it)\n' "$1"
	fi
}

shell_scripts() { # pathspec...: tracked or not yet, so that a new script is checked before it's added
	git ls-files -z --cached --others --exclude-standard '*.sh' "$@"
}

run_shellcheck() {
	shell_scripts | xargs -0 shellcheck -x
}

run_shfmt() {
	# Files by name, as CI's `shfmt -d .` can't walk an agent sandbox's unreadable stubs. A named file is formatted
	# even where .editorconfig says to ignore it, so the one it ignores is left out here too.
	shell_scripts ':!scripts/spike/phase0.sh' | xargs -0 shfmt -d
}

go_env() { # Go's caches. In an agent's sandbox the real ones are read-only: the build cache moves into TMPDIR, and
	# the module cache too, but only where a module isn't in the real one yet, as moving it downloads them all again.
	export GOTOOLCHAIN=local GOFLAGS=-mod=readonly
	local gocache
	gocache=$(go env GOCACHE)
	mkdir -p "$gocache" 2>/dev/null || true # a first run: no cache yet, which doesn't make it read-only
	[[ -w $gocache ]] || export GOCACHE="${TMPDIR:-/tmp}/go-build"
	if [[ ! -w "$(go env GOMODCACHE)" ]] && ! (cd go && GOPROXY=off go list -deps -test ./... >/dev/null 2>&1); then
		export GOPATH="${TMPDIR:-/tmp}/go" GOMODCACHE="${TMPDIR:-/tmp}/go/pkg/mod" GOFLAGS="-mod=readonly -modcacherw"
	fi
}

go_in() { # command...: in go/
	(cd go && "$@")
}

run_go_test() { # as CI runs it, with the race detector, where cgo has a C compiler to build it with
	local race=()
	[[ $(go env CGO_ENABLED) != 1 ]] || race=(-race)
	go_in go test "${race[@]}" ./...
}

run_gofmt() { # fails on any file gofmt would change, and names it, or on one it can't read
	local changed
	changed=$(go_in gofmt -l .) || return
	[[ -z $changed ]] || {
		printf '%s\n' "$changed"
		return 1
	}
}

checks() {
	step "ruff format" "${uv_run[@]}" ruff format --check
	step "ruff check" "${uv_run[@]}" ruff check
	step mypy "${uv_run[@]}" mypy
	step pytest "${uv_run[@]}" pytest
	if command -v go >/dev/null; then
		go_env
		step gofmt run_gofmt
		step "go vet" go_in go vet ./...
		step "go test" run_go_test
	else
		printf 'skipped  go (not installed here; CI runs gofmt, go vet and go test)\n'
	fi
	optional shellcheck run_shellcheck
	optional shfmt run_shfmt
	optional actionlint actionlint
	optional zizmor zizmor .
	exit "$failed"
}

collect() { # the test ids in this folder's tests
	"${uv_run[@]}" --project "$repo" pytest --collect-only 2>/dev/null | grep '::' | sort -u
}

red() {
	local rev=$1 old new_ids passed still_pass
	git rev-parse --verify --quiet "$rev^{commit}" >/dev/null || {
		echo "check.sh: no such commit: $rev" >&2
		exit 2
	}
	repo=$PWD
	old=$(mktemp -d "${TMPDIR:-/tmp}/check-red.XXXXXX")
	# shellcheck disable=SC2064 # old is set now, and this is the only value it takes
	trap "rm -rf '$old'" EXIT
	git archive "$rev" src tests pyproject.toml uv.lock | tar -x -C "$old"

	new_ids=$(comm -23 <(collect) <(cd "$old" && PYTHONPATH="$old/src" collect))
	if [[ -z $new_ids ]]; then
		echo "no tests new since $rev"
		exit 0
	fi

	local ran
	ran=$(PYTHONPATH="$old/src" "${uv_run[@]}" python -c 'import agent_fabric; print(agent_fabric.__file__)')
	if [[ $ran != "$old"/* ]]; then
		echo "check.sh: the tests would run against $ran, not $rev's code" >&2
		exit 1
	fi

	passed=$(PYTHONPATH="$old/src" "${uv_run[@]}" pytest --tb=no -rp 2>/dev/null | sed -n 's/^PASSED //p' | sort -u || true)
	still_pass=$(comm -12 <(printf '%s\n' "$new_ids") <(printf '%s\n' "$passed"))
	printf '%s new tests since %s; against its code, %s pass\n' \
		"$(wc -l <<<"$new_ids" | tr -d ' ')" "$rev" "$(grep -c . <<<"$still_pass" || true)"
	if [[ -n $still_pass ]]; then
		printf '%s\n' "$still_pass"
		exit 1
	fi
}

case ${1:-} in
"") checks ;;
--red)
	(($# == 2)) || usage
	red "$2"
	;;
*) usage ;;
esac
