Verbatim BattleTanx publication regression payloads.
Rebase headers: committed a763d806 and later clean-lane HEAD (65730775), with no shortened declaration bodies.
Staging headers: complete current b1520 span_1000/code_800F45C8.h and common/types_f8bfabebf96f.h. index.json is the real current manifest restricted to these homes and its missing common/types_d507c48987bb.h dependency; all matching symbols/type homes are retained.
missing-owned.h is that complete missing payload from render e881cdab12eb320403b3a38cedaacc3ffb5a336440ebb69df02eac46c297499b, whose SHA-256 matches the manifest. Tests use it as manifest-pinned cache evidence; the real duplicate-payload case retains the existing installed home.
