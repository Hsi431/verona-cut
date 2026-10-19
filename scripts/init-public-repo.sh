#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -e "$project_dir/.git" ]; then
    printf 'An existing .git was found. Refusing to replace history.\n' >&2
    exit 1
fi
python3 "$project_dir/scripts/check-release.py"
git -C "$project_dir" init -b main
printf 'Empty main branch created. Review files before git add, commit or push.\n'
