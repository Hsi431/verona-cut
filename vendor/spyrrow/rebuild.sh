#!/bin/sh
set -eu

vendor_dir=$(CDPATH= cd "$(dirname "$0")" && pwd)
work_dir=/tmp/verona-vendor-rebuild-$$
source_dir=$work_dir/source
wheel_dir=$work_dir/wheel
python311=${PYTHON311:-python3.11}
wheel_name=spyrrow-0.9.0-cp311-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl

cleanup() {
	if [ -d "$work_dir" ]; then
		rm -r "$work_dir"
	fi
}
trap cleanup 0
trap 'exit 1' HUP INT TERM

command -v git >/dev/null 2>&1 || { printf 'git is required\n' >&2; exit 1; }
command -v cargo >/dev/null 2>&1 || { printf 'cargo is required\n' >&2; exit 1; }
command -v maturin >/dev/null 2>&1 || { printf 'maturin is required\n' >&2; exit 1; }
"$python311" --version >/dev/null

mkdir -p "$source_dir" "$wheel_dir"
git clone --depth 1 --branch v0.9.0 https://github.com/PaulDL-RS/spyrrow.git "$source_dir/spyrrow"
git init "$source_dir/sparrow"
git -C "$source_dir/sparrow" remote add origin https://github.com/JeroenGar/sparrow.git
git -C "$source_dir/sparrow" fetch --depth 1 origin 881cdcbdf492ca42ba5413954ea6e41889a3becd
git -C "$source_dir/sparrow" checkout --detach FETCH_HEAD
git clone --depth 1 --branch 0.7.0 https://github.com/JeroenGar/jagua-rs.git "$source_dir/jagua-rs"

git -C "$source_dir/jagua-rs" apply "$vendor_dir/jagua-rs-0.7.0.diff"
git -C "$source_dir/sparrow" apply "$vendor_dir/sparrow-881cdcb.diff"
git -C "$source_dir/spyrrow" apply "$vendor_dir/spyrrow-v0.9.0.diff"

XDG_CACHE_HOME=$work_dir/xdg-cache \
CARGO_HOME=$work_dir/cargo-home CARGO_TARGET_DIR=$work_dir/target \
	maturin build --release \
	--manifest-path "$source_dir/spyrrow/Cargo.toml" \
	--out "$wheel_dir" --interpreter "$python311" \
	--features pyo3/extension-module,pyo3/abi3-py311 \
	--compatibility manylinux_2_17 --zig

wheel=$wheel_dir/$wheel_name
if [ ! -f "$wheel" ]; then
	printf 'expected abi3 manylinux wheel was not built: %s\n' "$wheel" >&2
	exit 1
fi
cp "$wheel" "$vendor_dir/$wheel_name"
printf 'Rebuilt %s (%s bytes)\n' "$wheel_name" "$(wc -c < "$vendor_dir/$wheel_name" | tr -d ' ')"
