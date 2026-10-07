# Compiler ownership

The registry owns compiler IDs, versions, families, execution-path kinds, pins,
downloads, flag variants, external decompiler/scorer targets, extraction modes
and persisted build-key compatibility. Kind selects a native execution path;
compiler identity selects preprocessing semantics and assembler options.

A family exports `adapter()` implementing `Family`. The protocol covers native
commands and setup probes; preprocessing, analysis and dependency behavior;
object padding, target shape, move idioms and constant selectors; decompiler
register adaptation; allocation/scheduling production, parsing and hints; and
native runtime-helper identity. Unsupported scheduling returns explicit
unavailable evidence. Empty registered external targets mean unsupported.

An additional compiler in an existing family needs only a registry entry. A
new family additionally needs its module and complete adapter. Families sharing
one execution-path kind must agree on native templates, host preprocessing and
padding; each compiler retains its own flag semantics. Driver-template changes
need a distinct execution-path kind, not central caller edits.

The object readers, instruction analysis and O32 transport remain target facts:
big-endian MIPS ELF32 relocations, ISA instructions and the selected target ABI.
Authored C extension syntax and preprocessor option transport are source/provider
contracts, independent of the compiler selected for native code. Family-specific
ABI differences and the host analysis provider's dump syntax live in adapters.
Synthetic decompiler constant sections are distinguished from emitted object
sections and are selected through the family contract.

Runtime recognition currently proves one complete signed integer-to-double
implementation and all of its actual ROM literals. Other implementations and
other helper operations require further family evidence. No generic library
catalogue is implied. Conflicting native addresses are refused. Bindings preserve
ROM bytes; code/data split repair remains the layout owner's operation.
