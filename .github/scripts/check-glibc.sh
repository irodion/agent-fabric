#!/usr/bin/env bash
# Fail if a binary needs a newer glibc or libstdc++ than the oldest supported desktop has.
# Usage: check-glibc.sh <binary> <max GLIBC version> <max GLIBCXX version>
# Ubuntu 24.04 ships glibc 2.39 and libstdc++ up to GLIBCXX_3.4.33 (docs/design.md §4.1).
set -euo pipefail

bin=$1 max_glibc=$2 max_glibcxx=$3
symbols=$(objdump -T "$bin")

highest() {
	grep -oE "\\b$1_[0-9]+(\\.[0-9]+)+\\b" <<<"$symbols" | sed "s/^$1_//" | sort -uV | tail -n1
}

status=0
for pair in "GLIBC $max_glibc" "GLIBCXX $max_glibcxx"; do
	read -r name max <<<"$pair"
	need=$(highest "$name")
	if [[ -z $need ]]; then
		echo "$name: not required"
	elif [[ $(printf '%s\n%s\n' "$need" "$max" | sort -V | tail -n1) != "$max" ]]; then
		echo "::error::$bin needs ${name}_$need, above the ${name}_$max floor"
		status=1
	else
		echo "$name: needs $need (max $max)"
	fi
done
exit "$status"
