Real RageWars route 3 payloads, reduced to one audio unit and its declaration prerequisites.
Publication b83a3408 used tool d39d79a. Its common/unused.h supplied both the
ALMainBus_s typedef and complete struct. Refresh dc416aa1 moved them to
span_1000/code_802B369C.h without adding that source import; tree adccc7c2
failed native EU-X compilation. Repair83ce46c8 added the provider and proved
all 80 words in both eu-x and us-rev1. No original proof/commit byte mismatch
is established: the later refresh broke reachable declaration retention.

Sources, authored headers and the owner header are complete byte-exact files.
Large generated headers are exact line excerpts; only unrelated declarations
and imports are omitted. provenance.json records original/excerpt digests and
line ranges. config.toml contains exact build/compiler/holding-version tables.
The original disposable layout index was not captured. Tests deliberately
model its old umbrella mapping, then supply current installed provider bytes.
Native tests use real cpp with project defines and GCC's 32-bit C89 syntax
boundary. ROM/object matching remains the existing publication test boundary;
these tests make no new claim about instruction matching or scalar ABI.
