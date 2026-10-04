# Contributing to @TITLE@

## Install

```sh
python3 -m pip install git+https://github.com/melalawi/abu-cake-unbake-64
```

## New project

Run these commands inside this repo.
Put your own ROM dumps at these paths.

@ROM_INPUTS@

```sh
unbake setup
```

Setup shows compiler evidence and asks for one confirmation of the whole proposal.
Try measures compiler candidates and records exact equivalence.
It prints the policy path and names any missing input.
It checks every ROM before the project is ready.

```sh
make check
```

Plain `make` and `make check` cover every version.
Use `VERSION=<version>` to select one version.
Compiler files live in `@TOOLS@`.
Their hashes are recorded in `@PINS@`.

## Next command

```sh
unbake next
unbake cycle --pick 5 --stop idle:900
unbake draft FUNCTION
unbake compare FILE
unbake publish FILE
```

`next` prints the next step to run.
`cycle` picks functions, writes drafts and compares each draft every time you save it.
`draft` writes `build/work/FUNCTION/FUNCTION.c` with the shared type context.
`compare` compiles the file and compares it with every version that holds the function.
A function exact in every version lands with a ROM proof, a commit and a push.
`publish` lands an exact file by hand.
Source must pass the checks.

Every command ends with the next command to run.
`unbake next` prints it again.
