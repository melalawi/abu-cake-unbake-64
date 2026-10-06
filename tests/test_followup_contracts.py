"""Regressions independently authored for omitted input, transfer and native stage contracts."""
import json
import os
import subprocess
from dataclasses import replace
from unittest.mock import MagicMock, patch
from tests.project_fixture import ProjectCase
from tests.work.test_shape import SHAPES
from unbake import buildfiles, cache, inputs, process, steps
from unbake.config import Compiler, Held
from unbake.layout import boundary, structs_fold
from unbake.layout.split import Edit
from unbake.typemap import facts, solver, storage


class FollowupContracts(ProjectCase):
    versions = ('us',)

    def test_map_owning_inputs_invalidate_warmed_readiness(self):
        directory = self.project.build / 'map'
        directory.mkdir(exist_ok=True)
        (directory / 'facts.json').write_text('{}')
        shard = directory / 'current.sqlite'
        shard.write_bytes(b'map shard bytes')
        setup = self.project.build / 'setup/layout.json'
        setup.parent.mkdir(exist_ok=True)
        setup.write_text('{}')
        link_symbols = self.project.build_link('us') / 'symbol-addresses.txt'
        link_symbols.parent.mkdir(parents=True, exist_ok=True)
        link_symbols.write_text('alpha = 0x80001000;\n')
        changed_files = (self.project.version('us').baserom, shard, setup, link_symbols)

        def refresh(project, policy):
            owners = storage.map_inputs(project)
            return {'shard_sha256': cache.key(json.dumps(owners), shard), 'shard': {}, 'abi_supplement': None}

        with (patch.object(solver, 'refresh_map', side_effect=refresh) as refreshed,
              patch('unbake.typemap.abi_facts.refine', side_effect=lambda project, value: value),
              patch.object(facts, 'published_keys', return_value=[])):
            for path in changed_files:
                with self.subTest(input=path.name):
                    cache.forget(['types.readiness'])
                    before = solver.readiness(self.project, None)
                    calls = refreshed.call_count
                    if path == link_symbols:
                        path.write_text('alpha = 0x80001004;\n')
                    elif path == setup:
                        path.write_text('{"partitions": []}')
                    else:
                        path.write_bytes(path.read_bytes() + b'changed')
                    after = solver.readiness(self.project, None)
                    self.assertIsNot(after, before)
                    self.assertGreater(refreshed.call_count, calls)

    def test_jalr_zero_unknown_target_has_no_proved_return_continuation(self):
        values = [int(word, 16) for word in '27bdffe0 afbf001c 01000009 00000000 8fbf001c 03e00008 27bd0020'.split()]
        _, tags, failures = boundary.closure({i * 4: word for i, word in enumerate(values)}, 0, len(values) * 4, 0x80001000, set())
        self.assertTrue(failures)
        self.assertNotIn('control-flow-closed-return', tags)

    def test_undecodable_native_bytes_retain_exit_and_both_streams(self):
        def native(argv, **options):
            stdout, stderr = b'\xff', b'normal native diagnostic\n'
            if options.get('text'):
                encoding, errors = options.get('encoding', 'utf-8'), options.get('errors', 'strict')
                stdout, stderr = stdout.decode(encoding, errors), stderr.decode(encoding, errors)
            return subprocess.CompletedProcess(argv, 7, stdout, stderr)

        with patch.object(process.subprocess, 'run', side_effect=native):
            with self.assertRaises(Held) as caught:
                process.run_native(['native', 'input'], self.root, 'compile')
        record = caught.exception.fault
        self.assertEqual(record['exit'], 7)
        self.assertEqual(record['stdout'].encode('utf-8', 'surrogateescape'), b'\xff')
        self.assertEqual(record['stderr'], 'normal native diagnostic\n')

    def test_recorded_build_recipe_schema_is_invalidated_in_normal_step_readiness(self):
        with patch.object(buildfiles, 'SCHEMA', 1):
            old_key = buildfiles.input_key(self.project, self.host)
        steps.record(self.project, 'buildfiles', old_key)
        publish = MagicMock()
        step = steps.Step('buildfiles', 'generation contract changed', 'Writing build files', buildfiles.input_key, publish)
        command = MagicMock()
        with patch.object(steps, 'STEPS', {'buildfiles': step}):
            steps._ensure(self.project, self.host, ['buildfiles'], force=False, report=None, command=command)
        self.assertEqual(publish.call_count, 1)
        self.assertNotEqual(steps.recorded(self.project, 'buildfiles'), old_key)

    def test_real_header_compile_proof_uses_the_same_unsigned_char_preprocessing(self):
        cc = self.project.tools / 'gcc-2.8.1-sn64/cc1'
        cc.parent.mkdir()
        cc.write_text('compiler fixture')
        compiler = Compiler('gcc-2.8.1-sn64', 'sn64', cc, cc, ('-G0', '-mips3', '-mgp32', '-mfp32', '-O2'), self.project.tools / 'compilers.sha256')
        project = replace(self.project, compilers={**self.project.compilers, compiler.id: compiler},
                          units={'alpha': compiler.id}, unit_flags={'alpha': ('-funsigned-char',)})
        source = project.src / 'alpha.c'
        header = project.include[0] / 'changed.h'
        header.write_text('typedef char Byte;\n')
        source.write_text('#include "changed.h"\nint alpha(Byte *p) { return *p; }\n')
        native_calls = []

        def run(argv, work, phase, **named):
            native_calls.append(argv)
            return 'int alpha(char *p) { return *p; }\n'

        edit = Edit(header, header.read_text(), 'typedef unsigned char Byte;\n', ('us',))
        with patch.dict(os.environ, {'TMPDIR': str(self.root)}), patch('unbake.compilers.registry.verify'), patch.object(process, 'run_tool', side_effect=run):
            structs_fold._compile_includers(project, [edit], self.host)
        preprocessing = [argv for argv in native_calls if argv[0] == str(self.host.cpp)]
        self.assertEqual(len(preprocessing), 2)
        self.assertTrue(all('-funsigned-char' in argv for argv in preprocessing))
