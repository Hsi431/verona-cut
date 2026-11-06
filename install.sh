#!/bin/sh
set -eu
project_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
python3 -m venv "$project_dir/.venv"
"$project_dir/.venv/bin/python" -m pip install "$project_dir"

wheel="$project_dir/vendor/spyrrow/spyrrow-0.9.0-cp311-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl"
system=$(uname -s)
machine=$(uname -m)
if [ "$system" = Linux ] && [ "$machine" = x86_64 ]; then
	if [ -f "$wheel" ] && "$project_dir/.venv/bin/python" -m pip install --no-deps "$wheel" >/dev/null 2>&1; then
		printf 'Built-in spyrrow nesting is available on Linux x86_64 with glibc 2.17 or newer.\n'
	else
		printf 'Built-in spyrrow nesting is unavailable on this Linux x86_64 system; Verona will use the legacy layout.\n'
	fi
else
	printf 'Built-in spyrrow nesting is unavailable on %s/%s; Verona will use the legacy layout.\n' "$system" "$machine"
fi

printf 'Run: %s/run.sh\n' "$project_dir"
