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
`unbake init` writes a full example. A missing key is refused by name.

## Start

```sh
unbake init NAME
unbake setup                      # prints a proposal and its digest
unbake setup --confirm <SHA256>   # proves every ROM
```

## Work

```sh
unbake cycle                                         # pick, draft, compare on save, land
unbake cycle --pick 5 --stop idle:900 > events.jsonl
```

A function that is exact in every version lands at once with a ROM proof, a commit and a push.
Run `unbake next` for the next step and `unbake --help` for every verb.
Every command prints one JSON object on stdout. Human text goes to stderr.

## Build without unbake

```sh
make setup
make check
```

## Development

Run `bin/test`, `bin/lint` and `bin/hygiene`.

Licensed under [GNU GPL version 3 or later](LICENSE).
