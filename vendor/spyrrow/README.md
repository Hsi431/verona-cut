# Vendored spyrrow

Verona includes a patched spyrrow 0.9.0 wheel for Linux x86_64, CPython 3.11+
through the stable `abi3` interface, and glibc 2.17 or newer. The wheel is
`spyrrow-0.9.0-cp311-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl`.

The patch adds static keep-out polygons to the spyrrow input and carries them
through the SPP pipeline into Jagua. Sparrow and Jagua collision accounting
also handle those hazards. This lets Verona keep fixed print-and-cut marks clear
of newly packed shapes. The complete source diffs are in this directory.

The source revisions are [spyrrow v0.9.0](https://github.com/PaulDL-RS/spyrrow/tree/v0.9.0),
[Sparrow `881cdcbdf492ca42ba5413954ea6e41889a3becd`](https://github.com/JeroenGar/sparrow/tree/881cdcbdf492ca42ba5413954ea6e41889a3becd),
and [jagua-rs 0.7.0](https://github.com/JeroenGar/jagua-rs/tree/0.7.0).
The upstream MIT and MPL-2.0 license texts are included here.

## Rebuild

Building requires Git, Rust/Cargo, Python 3.11, maturin 1.9.4, and Zig 0.12.1.
Install maturin and its Zig helper in a build environment with:

```sh
python3 -m pip install maturin==1.9.4 ziglang==0.12.1
```

Then run `vendor/spyrrow/rebuild.sh` from any directory. The script fetches the
three pinned upstream revisions, applies the included patches, builds an
`abi3-py311` wheel using maturin's Zig-backed manylinux 2.17 target, and replaces
the bundled wheel only after a successful build. Runtime installation uses the
local wheel with `pip install --no-deps`; it does not fetch spyrrow from PyPI.
