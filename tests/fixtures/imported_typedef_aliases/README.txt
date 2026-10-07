Declaration and caller-statement reductions from BattleTanx TOOL-ROUTE-5 and
RageWars legacy restart TOOL-ROUTE-3. Local declaration spellings are preserved
from the refusal records before their int-spelling/removal workarounds. Shared
prototypes and ../types.h imports are copied from the real generated headers;
types.h is the real foundational header, unchanged. Bodies keep a real caller
statement inside the original entry signature. Unrelated statements are omitted.

The RageWars caller was exact on de, eu, eu-x, us, and us-rev1 in the route evidence.
These are declaration-reconciliation regressions; no ROM or compiler proof is
claimed. Tests cover both the private header copy with a shared types.h symlink
and a private directory that falls back to the installed foundational header.

The return-conflict pair comes from the superseding BattleTanx TOOL-ROUTE-6
refusal: void func_800B1264_us(void) versus extern int func_800B1264_us().
This real return mismatch must still refuse after importing foundational aliases.
