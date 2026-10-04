# Example Two decompilation

A matching decompilation of *Example Two* for the Nintendo 64.

> **This repository contains no game content.** You must supply a legally acquired cartridge dump. Builds are verified against its SHA256.

## Progress

<pre><code>all     [██████████████▒░░░░░]  70.03% (~74.55%)  3,895,496 of 5,562,740 bytes</code><br><code>us      [██████████████▒░░░░░]  73.57% (~75.36%)  801,380 of 1,089,312 bytes</code><br><code>us-rev1 [██████████████▒░░░░░]  74.44% (~76.66%)  843,900 of 1,133,620 bytes</code><br><code>eu      [█████████████▒▒░░░░░]  67.50% (~73.82%)  752,424 of 1,114,668 bytes</code><br><code>eu-x    [█████████████▒▒░░░░░]  65.75% (~72.79%)  733,348 of 1,115,312 bytes</code><br><code>de      [█████████████▒▒░░░░░]  68.88% (~74.09%)  764,444 of 1,109,828 bytes</code></pre>

| us (NUS-NRWE-0, North America). First NTSC release (black cartridge). SHA256 `0433043aaba2649bdd1fe717c4020550ac663c0362527aa082490f5977a3e46b` |
|---|
| <pre><code>bytes     [██████████████▒░░░░░]  73.57% (~75.36%)  801,380 of 1,089,312</code><br><code>functions [█████████████████░░░]  87.45%  3,762 of 4,302</code></pre> |

| us-rev1 (NUS-NRWE-1, North America). Revised NTSC release (grey cartridge). SHA256 `5dfbae59e4a3860b740ccbb28f33aad624315e30bfca6d60abd62d12a89e0089` |
|---|
| <pre><code>bytes     [██████████████▒░░░░░]  74.44% (~76.66%)  843,900 of 1,133,620</code><br><code>functions [█████████████████░░░]  85.77%  3,960 of 4,617</code></pre> |

| eu (NUS-NRWP-0, Europe). PAL release. SHA256 `d763cbbe485a5f9e1b7be97d5ac16735087e23d0bb62c05dc844e01b7e1156d1` |
|---|
| <pre><code>bytes     [█████████████▒▒░░░░░]  67.50% (~73.82%)  752,424 of 1,114,668</code><br><code>functions [████████████████░░░░]  83.41%  3,685 of 4,418</code></pre> |

| eu-x (NUS-NRWX-0, Europe). PAL multi-language release. SHA256 `511f6c876586bf401faf01a270c67f26fcb7db55ed3f35759c71ab15a29de750` |
|---|
| <pre><code>bytes     [█████████████▒▒░░░░░]  65.75% (~72.79%)  733,348 of 1,115,312</code><br><code>functions [████████████████░░░░]  82.43%  3,641 of 4,417</code></pre> |

| de (NUS-NRWD-0, Germany). Censored German release. SHA256 `9dc401252bacb2ad7412ef003f97f28cb225d76b3cc76f430fbc27fa05067ca8` |
|---|
| <pre><code>bytes     [█████████████▒▒░░░░░]  68.88% (~74.09%)  764,444 of 1,109,828</code><br><code>functions [████████████████░░░░]  84.43%  3,710 of 4,394</code></pre> |

## Development & Contributions

Contributions and corrections are welcome. Run `make check` before opening a pull request.

For detailed instructions please see [CONTRIBUTING.md](CONTRIBUTING.md).

## Notes

### Workflow

Every function is verified by compiling it against the original ROM. Odd matches are marked in source as they are discovered.

### Personal Thoughts

Readable source can be ported, fixed and preserved long after its tools are gone.

## License

[CC0 1.0](LICENSE)

## Dependencies

- [AbuCakeUnbake64](https://github.com/melalawi/abu-cake-unbake-64)
- [GCC 2.8.1 for mips-nintendo-nu64](https://github.com/pmret/gcc-papermario) at `d97824ffc7517675646960442b3f512a473e4404`, the compiler of toolchain `gcc-2.8.1`, assembled by SN ASN64 version 2.81
