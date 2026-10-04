# Reduction tests, first TDD pass

Branch `reduction-tests`. Unit tests: 16,115 lines in tests/ including integration (was 37,230; target 18,000).

## Deleted (about 21,500 lines, 140 files)
- Whole old CLI suite (tests/cli/*), tests/acceptance/*, plus tests/test_runner.py and tests/process_fakes.py (forked pool and fakes).
- match/: batch, batch_fold, forked, incremental, relink, attribution, staging, publication, submit_* suites, declarations, assembly_headers, overlay_recipe, baseline, data_symbols, binding_inventory.
- project/: clone, workspace, cache, config, makefile, build, bootstrap, compile_*, helper_freshness, version_build, write_hygiene, extract_symbols, toolchain, sn64_download, fingerprint, setup_transactions (and their fixtures: makefile_fixture, setup_fixture, helper_fixture).
- project_tools/*: all (atomic, compile, identity, link_inputs, pool_slices, literal layout, rodata, defects, linker).
- decomp/: assign, plan, trial_*, volatile_rewrite, frictions, literal_layout, candidate_ranking, partials, gbi_cache, private_trial.
- layout/: apply_names, engine, header_cache, signature_catalog, boundary, rodata*, compiler_tables, resident_mapping, units, xver.
- typemap/regeneration, search/test_core_contract. runner.py no longer patches match.forked.

## Added
| File | Lines | Covers |
|---|---|---|
| tests/kit.py | 77 | builders: fake executables, full valid host table, TempCase |
| tests/test_config.py | 207 | INTERFACES 3 and 4: host_path precedence, load_host merge, every refusal, kinds, cross-key rules, credential exactly-one, retired [paths]/[workspace], [build] unknown key |
| tests/test_cache.py | 168 | 5: key framing and Path parts, produce single-flight (two threads), failed make, trim LRU order and .lock, bad kind/key, directory entries, memo |
| tests/test_inputs.py | 40 | 6 |
| tests/test_steps.py | 37 | 8 |
| tests/typemap/test_facts.py | 53 | 9 (source_key: bytes, header closure, unrelated header, version) |
| tests/cli/test_contract.py | 100 | 1 (help and refusal for 13 verbs, 11 deleted verbs, stdout JSON only) |
| tests/test_lock.py | 45 | 12 (DRAFT verb table `lock.READ_ONLY`) |
| tests/compilers/test_drivers.py | 83 | 10 (DRAFT) |
| tests/test_buildfiles.py | 83 | 11 (DRAFT, slice runs, version-only, units.mk) |
| tests/test_land.py | 117 | 13 (DRAFT seams) |
| tests/cycle/test_events.py, test_rank.py | 70, 62 | 14 |
| tests/layout/test_headers_plan.py | 53 | 15 (DRAFT) |
| tests/test_merge_units.py | 32 | 17 (DRAFT) |
| tests/integration/{project,test_flow,test_pool,workers}.py, bin/integration | 270 | init, check, compare, publish with bare remote, cycle events, Pool order and crash retry |

tests/fixture/config.toml was rewritten to the new schema (no [paths]/[workspace], three [build] keys).
tests/integration/__init__.py skips itself unless UNBAKE_INTEGRATION=1, so bin/test never loads process-spawning tests; bin/integration sets it.

## Verified now (against F/tool, read-only)
test_config, test_cache, test_inputs, test_steps, typemap/test_facts and integration/test_pool pass (25 tests). The rest need source that does not exist yet.

## Needs mechanical migration at merge
Kept domain tests (decomp, layout, report, search, typemap, project census/setup/proposal) still import `unbake.project.config`, `Policy`, `tests.support.test_policy`, `tests.decomp.support.fixture` and old fixtures. tests/support.py still uses `load_policy`. Move these to `unbake.config.Host` and `tests/kit.py` once the module shapes are final. Stale spots inside kept files: search/test_permute patches `makefile.helpers`; decomp/support imports match.staging. bin/accept-flow points at deleted tests/acceptance/flow.py (not mine to edit).

## Contradictions and gaps in the interface files
1. drivers.argv: DESIGN 4.3 says `argv(unit, version)`, INTERFACES 10 says `argv(project, unit, version)`. Tests use the project form. Neither gives a host or cpp path.
2. Lock refusal message names `<command>` but `project_lock(project)` takes no command (section 12).
3. Unknown config section: INTERFACES says `[<section>].<key>: unknown key`; the source says `[bogus]: unknown section`. Tests match `\[bogus\]` only.
4. Lock verb table, land boundary seams (build/tree/git), layout and split spellings, headers `solution` shape and buildfiles member syntax are unnamed. Tests guess them and carry `# DRAFT interface`.
5. `cycle --stop all-landed` and `--functions F` appear in the task, not in INTERFACES 2 (flags say "section 7.1").
6. DESIGN 7.4 says `fn.pushed` has `error?` and drafts `ok`, while events.EVENTS must list required fields only; tests accept either.
