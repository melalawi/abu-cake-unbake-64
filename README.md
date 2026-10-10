# AbuCakeUnbake64

Tools to help unbake a cake!

## Install

### Linux

On Debian or Ubuntu install make, a C compiler and preprocessor, MIPS binutils, git and Python.

```sh
sudo apt install build-essential binutils-mips-linux-gnu git python3 python3-venv
```

Only toolchains that assemble with `n64link` need it. Download it from the
[abu-mas-n64 releases](https://github.com/melalawi/abu-mas-n64/releases) and set `tools.n64link` in the config.
A project that needs it and lacks it is refused by name.

Install unbake with splat in a virtual environment.

```sh
python3 -m venv ~/.venvs/unbake
. ~/.venvs/unbake/bin/activate
pip install "splat64[mips]==0.50.0" spimdisasm==1.42.4 rabbitizer==1.16.2 \
  git+https://github.com/melalawi/abu-cache-64 \
  git+https://github.com/melalawi/abu-cake-unbake-64
```

### Windows

Install [WSL](https://learn.microsoft.com/windows/wsl/install) with Ubuntu and follow the Linux steps inside it.

## Config

unbake reads machine facts from `--config FILE`, `$UNBAKE_CONFIG` or `~/.config/unbake/unbake.toml`.
`unbake init` writes a full example. Fill in its machine limits and tools before `setup`.
Set `[resources] domain = "standalone"` to run one command alone with its own pool:
workers are bounded by `cores`, `workers` and the parent/worker memory budgets; make uses `cores`.
Or set `domain` to an absolute [broker manifest path](docs/resource-domain.md) for shared cgroup admission.
Missing `domain` refuses; project overrides may change limits but may never set `domain`.

## Start

```sh
unbake init NAME --functions-per-header 32
unbake setup                      # installs toolchains, extracts and proves every ROM
```

## Work

```sh
unbake report                                        # progress per version, kind and segment
unbake report --next --count 20                      # the ordered work list
unbake compare src/code_80200610.c --function NAME   # measure a source against every version
unbake check                                         # census the source rules over the whole project
```

A function that is exact in every version lands at once with a ROM proof and a commit.
Run `unbake --help` for every verb: `init`, `setup`, `report`, `compare` and `check`.
Every command prints one JSON object on stdout. Human text goes to stderr.

See [compiler ownership](docs/compiler-families.md) and
[publication](docs/publication.md) for the recipe and proof contracts.

## Build without unbake

```sh
make setup
make check
```

## Development

Run `bin/dev-setup` once, then `bin/test`, `bin/lint` and `bin/hygiene`.
Unit tests mock external tools; use `bin/test -p 'test_config.py'` to select an affected module.
Process and real build proofs belong in `bin/integration`, outside the unit runner.

Licensed under [GNU GPL version 3 or later](LICENSE).
