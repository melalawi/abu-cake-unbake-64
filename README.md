# AbuCakeUnbake64

Tools to help unbake a cake!

> This is a personal toolkit. It is not officially maintained and may change or break without notice.

## Install

```sh
python3 -m pip install .
```

It needs Python 3.11 or later along with make, MIPS binutils, splat, m2c and objdiff. Machine paths go in `~/.config/unbake/policy.toml`.

## Workflow

1. `init` creates a project from ROMs.
2. `split` cuts and names functions.
3. `decomp` drafts and searches for C.
4. `match` lands identical C.
5. `report` updates progress.

## License

Released under [GNU GPL version 3 or later](LICENSE).
