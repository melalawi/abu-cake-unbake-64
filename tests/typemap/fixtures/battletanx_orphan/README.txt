BattleTanx orphan-header regression, after main repair 3ad1e20.
Both include/common headers are complete byte-for-byte payloads.
The old header matches TOOL-ROUTE-4.json and cached render e881cdab12eb320403b3a38cedaacc3ffb5a336440ebb69df02eac46c297499b.
Render state db50f25979c8d8b6f7af81fb18bc938122b0556565d22b97d12d3c16b714fac2 points at that artifact.
The two JSON files retain real QueryResult records, published evidence and the old header/index output; unrelated game records are removed to keep the fixture small.
Tests preserve the original artifact key and rebind only its cache location.
