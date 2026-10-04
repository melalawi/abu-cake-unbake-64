# AbuCakeUnbake64

Tools to help unbake a cake! It turns N64 ROMs into C that rebuilds them byte for byte.

## Install

```sh
python3 -m pip install git+https://github.com/melalawi/abu-cake-unbake-64
```

You need Python 3.11 or later, make, a C preprocessor, MIPS binutils, splat, m2c and the `n64link` binary.

## Host config

Every machine fact lives in one file. unbake reads `--config FILE`, then `$UNBAKE_CONFIG`, then `~/.config/unbake/unbake.toml`.
A project may override single keys in `<project>/.unbake/unbake.toml`.
A missing key is refused by name with the command that needs it.

```toml
[resources]
cores = 12
workers = 8
memory_total_bytes = 16000000000
memory_parent_bytes = 4000000000
memory_worker_bytes = 1500000000

[cache]
root = "/abs/cache"
max_bytes = 80000000000
trim_to_bytes = 60000000000
memory_bytes = 2000000000

[tools]
make = "/usr/bin/make"
path = ["/abs/toolbin"]          # the only PATH for make check; it must not contain python
cpp = "/usr/bin/cpp"
mips_as = "/usr/bin/mips-linux-gnu-as"
mips_ld = "/usr/bin/mips-linux-gnu-ld"
mips_objcopy = "/usr/bin/mips-linux-gnu-objcopy"
mips_objdump = "/usr/bin/mips-linux-gnu-objdump"
mips_readelf = "/usr/bin/mips-linux-gnu-readelf"
n64link = "/abs/bin/n64link"
splat = "/abs/venv/bin/splat"
m2c = "/abs/venv/bin/m2c"
permuter_archive = "/abs/decomp-permuter.tar.gz"
permuter_sha256 = "<64 hex digits>"

[setup]
version_jobs = 2
probe_count = 8
same_game_similarity = 0.9
symbol_similarity_threshold = 0.9
symbol_similarity_margin = 0.05

[search]
stall_trials = 200
beam = 4

[cycle]
min_bytes = 16
max_bytes = 4096
min_history = 5
debounce_ms = 300

[publish]
remote = "origin"
branch = "main"
author_name = "Your Name"
author_email = "you@example.com"
credential_env = "GIT_TOKEN"     # or credential_helper = "store"
```

## Start a project

```sh
unbake init NAME --functions-per-header 32
# copy your ROM dumps into the printed roms/ folder
unbake setup                      # prints a proposal and its digest
unbake setup --confirm <SHA256>   # proves every ROM and makes the project ready
```

## Work

The cycle is the main loop. It picks functions, drafts each one into `build/work/FUNC/FUNC.c` and compares it after every save.
A function that is exact in every version lands at once. That means a ROM proof then a commit `Match FUNC` then a push.

```sh
unbake cycle                                    # in a terminal: pick on screen and watch the board
unbake cycle --pick 5 --stop idle:900 > events.jsonl
unbake cycle --status                           # the running or last cycle
```

Without a terminal give `--pick N` or `--functions a,b` and `--stop all-landed|idle:SECONDS|after:MINUTES`.
Edit the draft files while it runs. Each save is compared again by itself.
Read `fn.compare.done` lines for per-version percentages and the first difference.
The last line is `cycle.end` with what landed and the next command.

Each event line has `v`, `seq`, `t` and `event`. The events are
`cycle.start`, `fn.queued`, `fn.draft.start`, `fn.draft.done`, `fn.edit`, `fn.compare.start`, `fn.compare.done`,
`fn.exact`, `fn.landed`, `fn.land_failed`, `fn.committed`, `fn.pushed`, `fn.held`, `fn.failed`, `step.run` and `cycle.end`.
Exit code 0 means everything picked landed and was pushed. 1 means something was held or failed or is unpushed. 130 means interrupted.

The same steps exist one at a time.

```sh
unbake next                       # what to do now
unbake draft FUNC
unbake compare build/work/FUNC/FUNC.c
unbake tidy build/work/FUNC/FUNC.c
unbake search-variants build/work/FUNC/FUNC.c --method order --seconds 120
unbake explain FUNC --section status
unbake publish build/work/FUNC/FUNC.c
unbake boundary function FUNC --version us --start 0x1000 --end 0x1040 --apply
unbake check                      # rebuild every version with plain make and compare
```

Every command prints one JSON object on stdout with `status`, `key`, `data` and `next`. Human text goes to stderr.
A refusal names its key and the command that fixes it.

## Build without unbake

A ready project builds with make alone.

```sh
make setup    # fetch and verify the compilers
make check    # rebuild every version and compare it with the original ROM
```

## Development

Run `bin/test`, `bin/lint` and `bin/hygiene`.

Licensed under [GNU GPL version 3 or later](LICENSE).
