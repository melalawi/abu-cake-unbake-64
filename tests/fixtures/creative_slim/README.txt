Chosen public compare captures from the measured Creative3 corpus.

The .json.z files are zlib-compressed JSON dictionaries keyed by holding version.
Each keeps the complete target/candidate words, authoritative raw score, chosen
compiler, source hash, chosen-public-result marker, function PC and symbol aliases
from the version-bound fact capture. Compression keeps complete conservation
fixtures small. Invalid last-probe observations are excluded.

Sources are byte-for-byte retained baselines, the AC90 winning terminal-tail draft,
the measured globally losing B1520 trial, and the signedness/predicate control-before
sources. manifest.json retains the source hashes. types.h and vec3.h retain the
measured declarations used by the native preprocessor stand-in. No game source is
modified or published by these tests.

AC90 has six named predecessors, retained fallthrough and a measured 156-byte
representative gain. 802251DC has a cross-compound exit; 80430118 has a switch-break
suffix. Both remain unsupported. B1520 conserves 45 differing words before the
trial and 301 after: its helper locally gains 28 bytes while the whole function
loses 1024. C0 conserves 142 differing words in each of five holding versions.

Production raw_target_delta_bytes is relative to the exact target, locally and
for the full function. Subtract before from after to inspect a trial's raw gain.
Insertions, size changes and placement diagnostics are retained independently.
Regional gains never change exactness or search acceptance.

The Git whitespace exemption applies only to these retained C fixtures, preserving
the original bytes and source hashes.
