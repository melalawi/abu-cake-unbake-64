# ExampleOne decompilation

A matching decompilation of *ExampleOne* for the Nintendo 64.

> **This repository contains no game content.** You must supply a legally acquired cartridge dump. Builds are verified against its SHA256.

## Progress

| us (NUS-NBXE-0, North America). NTSC release. SHA256 `c5b7cf3523de025e3f18e2c8df2deb0eced5613e7c46cb8c6c5a4f22644ead3f` |
|---|
| <pre><code>bytes     [░░░░░░░░░░░░░░░░░░░░]   2.14% (~2.14%)  15,160 of 709,084</code><br><code>functions [█░░░░░░░░░░░░░░░░░░░]   7.98%  140 of 1,755</code></pre> |

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
- [GCC 2.7.2 (KMC)](https://github.com/decompals/mips-gcc-2.7.2) at `43d1cdb67ed135879869b5266f01efaaada5e35a`, the compiler of toolchain `gcc-2.7.2`, assembled by SN ASN64 version 2.81
- [IDO static recompilation](https://github.com/decompals/ido-static-recomp), the compiler of toolchain `ido-7.1`
