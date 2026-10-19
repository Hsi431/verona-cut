#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
language="$1"
shift
if [ "$#" -gt 0 ]; then
    exec "$project_dir/run.sh" --lang "$language" --open "$1"
fi
exec "$project_dir/run.sh" --lang "$language"
