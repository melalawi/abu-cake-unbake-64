# AbuCakeUnbake64

Tools to help unbake a cake!

This personal toolkit may change or break without notice.

## Install

```sh
python3 -m pip install git+https://github.com/melalawi/abu-cake-unbake-64
```

Python 3.11 or later is required.
The build uses make and MIPS binutils.
Tool paths belong in `~/.config/unbake/policy.toml`.
Setup names any missing policy field.
Supply `setup_version_jobs` explicitly in host policy to limit simultaneous version
extractions and builds. `cores` supplies a shared build CPU budget for those jobs.
The measured host policy uses `setup_version_jobs = 4`; supply that value explicitly.
Choose a lower version limit if the host has less memory.
Supply `symbol_similarity_threshold` and `symbol_similarity_margin` explicitly
in host policy as numbers in (0, 1]. They have no bundled defaults.
For example, 0.90 and 0.10 require at least 90% masked instruction similarity
and a ten percentage point lead over every competing placement in either version.
Compiler downloads have pinned SHA-256 hashes.

## Start

```sh
unbake init NAME
cd NAME
```

Put ROMs in the folder printed by init.
Run `unbake setup` to read every ROM and review the compiler proposal.
Accept its digest to prove every version before work starts.
Init and setup leave commits to you.

Function symbols and executable bodies have separate identities. Unambiguous
positions between matched anchors can share one symbol when all resolved
callees agree. Incoming edges are compared between versions containing the
already corresponding caller; unjoined callers supply no identity evidence.
Each version retains its own body and assembly row.
Correspondence runs to a fixpoint: stronger position and graph evidence is
established first, then successful joins supply anchors for later rounds.
Graph-free leaves require the policy similarity threshold and margin. Unequal
counts between anchors use weighted edit alignment; only pairs present in every
optimal alignment can join. Insertions, deletions and ambiguities retain named
reasons. The proposal records rounds, anchors, graph evidence, masked instruction
diff ranges and the similarity distribution. Neither names nor addresses
establish correspondence.
One C file can express those bodies with version conditionals.
For a ready project, `unbake setup --replan-symbols` reviews correspondence on
its retained boundaries. Confirm the printed digest to prove every ROM before
publishing the name changes. Affected authored C requires review first.

When automatic evidence is insufficient, `unbake split join --map joins.json`
previews explicit correspondence assertions. Each entry supplies one symbol
name, at least two placements pinned by version, integer ROM start/end and raw
body SHA256, and nonempty correspondence evidence:

```json
[
  {
    "name": "update_state",
    "placements": [
      {"version": "us", "start": 4096, "end": 4104, "body_sha256": "<64 hexadecimal digits>"},
      {"version": "eu", "start": 4128, "end": 4140, "body_sha256": "<64 hexadecimal digits>"}
    ],
    "evidence": {"source": "Reviewed version-specific implementation of update_state"}
  }
]
```

Review `build/setup/join-proposal.json`, then run the same command with `--apply`.
The batch joins whole existing items and refuses duplicate versions, overlapping
requests, stale bytes/boundaries, name collisions, crossings of immediate shared
bounding anchors, and conflicting edges to already corresponding callers or
callees present in both versions. Unknown transfers are recorded explicitly;
a user assertion supplies identity without claiming an indirect target was
proved. A refusal prevents the entire batch. Submit a corrected batch containing
only valid requests to apply those requests in one transaction.

Accepted assertions and their byte pins, supplied evidence and validation are
stored in `docs/setup/layout.json`. Setup refresh retains them and symbol
replanning seeds them before proposing further joins. Changed assertion bytes
or boundaries refuse by name. Publication uses one isolated proof of every ROM
and occurs only after all versions succeed. Independent body hashes, boundaries
and assembly state remain per placement; affected authored C refuses before
publication. Compiler candidate sets combine and old selection evidence is
archived for the changed item.

## Work

```sh
unbake map
unbake solve
unbake next
```

Map measures the whole program across every version.
Solve incrementally refreshes map facts affected by symbol placements or layout
boundaries, then publishes shared types. Unchanged instructions retain their facts;
changed ROMs require a new whole-program map.

To recover a referenced function hidden in a generated data row, preview
`unbake split code FUNCTION --version V --start ROM_OFFSET --end ROM_OFFSET`.
It requires a direct code reference or a referenced code-pointer table and a
closed, gapless instruction body. Add `--apply` to prove the cartridge and publish
the layout and entry symbol together. Prefix and suffix data keep their bytes.
Next ranks cross-version items and prints the command to run.
Follow it through draft then try then submit.
Try measures each containing version with its compiler candidates and retains
source-scoped receipts without changing project build inputs. Submit publishes
compiler choices, equivalence evidence, shared declarations, layout and exclusion
removals together, holding the publication lock through the cartridge proof.
Use `submit --batch SOURCE...` to fold and prove several receipts together.
A batch publishes its cartridge-proven passing subset and names refused sources.
Proof failures use object bytes, symbol bytes and link diagnostics to identify
faults; further isolation relinks retained objects. Refusals stream immediately,
and proof evidence and receipts persist in `.unbake/state/publications/*.jsonl`
even if the command stops. A partial publication returns a failing exit status.
Unrelated compiler evidence and layout entries do not invalidate a receipt.
Exact equivalents retain their proofs and a deterministic build choice.
Submit proves the cartridge and feeds types back into solve.
Passing fuzzy drafts keep their assembly under `NON_MATCHING`.
Projects can reserve authored items with `unbake-exclusions.json`.

## Development

Run `bin/test` then `bin/lint` and `bin/hygiene`.

## License

[GNU GPL version 3 or later](LICENSE).
