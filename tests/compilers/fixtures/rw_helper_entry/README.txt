RW route8 current small payloads, captured 2026-10-07 from the clean lane.

candidate.c SHA256 d4f8e524e6542406344e3f77e519c9fe11836309fcdf09f7cd38c91c6850937f
is the current 651B0 candidate. Historical compare log's source was 812a534d...;
the historical all-holding 63-64% score is evidence, not a new current game compare.
The current candidate was preprocessed with its exact types.h then compiled with
pinned GCC SN64 and the real game's compiler flags. Its only call relocation is
to the decoder; it contains no compiler helper reference. generated-compare-call.c
long-long less-than is actually inlined by these flags. generated-runtime-call.c
is an actual compiler-inserted out-of-line conversion call, not fabricated asm.

All native objects used cc1 -quiet -G0 -mips3 -O2 -mgas -meb -mcpu=VR4300
-mhard-float -mgp32 -mfp64 -mno-fix4300; n64link asn64 --as mips-linux-gnu-as
-march=vr4300 -mabi=32 -EB -G0 --no-pad-sections FILE.s -o FILE.o.

helper-*.bin are complete 72-byte native ROM routines. Every byte equals the
object .text from the actual committed four-word C helper shown here, including
branch offsets and return delay slots. No literals or relocations exist in these
bodies. native-identity.json records their independently read addresses and hashes;
all five existing symbols.ld files provide the recorded helper binding. This is
identity/binding evidence for these actual ROM bytes, not a generic ABI fallback
or an inferred address for absent runtime implementations.

entry-*.bin and gzip target-bodies are exact ROM code and prior mapped ABI facts
selected by the current manifest's matching target_sha256. The current manifest
advertises all versions but selects a completely empty SQLite shard; no game
records were repaired or substituted. Tests create private partial/empty shards
from these slices to assert the requested entry's body alone is read. Fixture
ROM storage offsets are rebased to 0x40; target instructions/addresses/digests and
ABI observations are retained. Genuine requested-entry absence/changed bytes and
canonical ABI contradictions must remain refused.
