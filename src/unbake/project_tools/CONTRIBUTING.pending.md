# Contributing

## Install

```sh
python3 -m pip install git+https://github.com/melalawi/abu-cake-unbake-64
```

## New project

Run these commands inside this repo.
Put your ROMs in `roms/`.
Input filenames can be arbitrary.
Setup reads the ROM headers to name each version.

```sh
unbake setup
```

Setup asks which version names the functions.
It shows compiler evidence and asks for confirmation.
It prints the policy path and names any missing input.
It proves every version before the project is ready.
Setup then writes instructions with your ROM paths.

## Next command

```sh
unbake next
```

Every command ends with the next command to run.
`unbake next` prints it again.
