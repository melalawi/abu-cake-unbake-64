# AbuCakeUnbake64

Tools to help unbake a cake!

## Install

### Linux

On Debian or Ubuntu install make, a C compiler and preprocessor, MIPS binutils, git and Python.

```sh
sudo apt install build-essential binutils-mips-linux-gnu git python3 python3-venv
```

Build `n64link` and put it on your PATH.

```sh
git clone https://github.com/melalawi/abu-mas-n64
make -C abu-mas-n64
sudo install abu-mas-n64/build/n64link /usr/local/bin/
```

Install unbake with splat and m2c in a virtual environment.

```sh
python3 -m venv ~/.venvs/unbake
. ~/.venvs/unbake/bin/activate
pip install "splat64[mips]==0.50.0" spimdisasm==1.42.4 rabbitizer==1.16.2 \
  "m2c @ git+https://github.com/matt-kempster/m2c@708d2d2cb2698f091a92492b328f73b24209f72d" \
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
unbake setup                      # prints a proposal and its digest
unbake setup --confirm <SHA256>   # proves every ROM
```

## Work

```sh
unbake cycle                                         # pick, draft, compare on save, land
unbake cycle --pick 5 --stop idle:900 > events.jsonl
```

A function that is exact in every version lands at once with a ROM proof and a commit.
Run `unbake next` for the next step and `unbake --help` for every verb.
Every command prints one JSON object on stdout. Human text goes to stderr.

`compare FILE --flags` and a plateau in `search-variants FILE --method auto`
use the same measured source × recipe frontier. Option episodes contain at most
eight effective recipes crossed with four retained source parents. Saved sources,
recipes, native refusals and next commands remain available under `build/work`.
External source/include inputs require explicit roots. Advisory subsystem hints
read retained facts and launch no native jobs or type solve.

Config schema 2 stores phase options by canonical translation-unit path. For an
existing project, use `migrate-state --plan`, review the complete plan, then
`migrate-state --apply PLAN` before normal work. See
[compiler ownership](docs/compiler-families.md) and
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
