"""A successful landing owns the actual project inputs used by every native proof."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake import land, pool, process, runner
from unbake.compilers import drivers
from unbake.config import Held
from unbake.fold.apply import Folded
from unbake.layout import split
from unbake.process import named


class PublicationDependencyTests(ProjectCase):
    def test_private_header_publication_follows_native_reads_not_work_directory_contents(self):
        file = self.project.work / "alpha/alpha.c"
        file.parent.mkdir(parents=True)
        source = '#include "needed.h"\nint alpha(void) { return VALUE; }\n'
        file.write_text(source)
        private = file.parent / "include"
        private.mkdir()
        (private / "needed.h").write_text("#define VALUE 1\n")
        (private / "abandoned.h").write_text("struct Abandoned { int value; };\n")
        staged = []
        with (
            patch.object(land, "exact_attempt", return_value=SimpleNamespace(compiler="ido-7.1")),
            patch("unbake.fold.apply.fold", return_value=Folded("alpha", source, {}, ())),
            patch.object(land, "_prove_versions", return_value={self.project.work / "_land/alpha/include/needed.h"}),
            patch("unbake.layout.header_step.validate"),
            patch.object(land, "_commit", side_effect=lambda project, host, paths, message: staged.extend(paths)),
            patch.object(land, "_git", return_value="committed\n"),
            patch.object(land.buildfiles, "write", return_value=[]),
            patch.object(land.steps, "record"),
            patch("unbake.report.progress.write", return_value=[]),
        ):
            land.land(self.project, self.host, file)
        root = self.project.include[-1]
        self.assertEqual((root / "needed.h").read_text(), "#define VALUE 1\n")
        self.assertIn(root / "needed.h", staged)
        self.assertFalse((root / "abandoned.h").exists())
        self.assertNotIn(root / "abandoned.h", staged)

    def test_native_transitive_version_inputs_land_without_unrelated_headers(self):
        include = self.project.include[-1]
        headers = {
            name: include / name for name in ("authored/root.h", "authored/us.h", "authored/eu.h", "unrelated.h")
        }
        for name, path in headers.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("/* " + name + " */\n")
        source = '#include "authored/root.h"\nint alpha(void) { return 1; }\n'
        file = self.project.work / "alpha" / "alpha.c"
        file.parent.mkdir(parents=True)
        file.write_text(source)
        staged = []

        def preprocess(argv, cwd, phase, **kwargs):
            # Native dependency output, including a macro-selected nested header.
            version = "eu" if "-DVERSION_EU" in argv else "us"
            return "".join(
                f"alpha.o:\t{path}\n"
                for path in (argv[-1], headers["authored/root.h"], headers["authored/" + version + ".h"])
            )

        with (
            patch.object(land, "exact_attempt", return_value=SimpleNamespace(compiler="ido-7.1")),
            patch("unbake.fold.apply.fold", return_value=Folded("alpha", source, {}, ())),
            patch.object(pool, "run", side_effect=lambda host, fn, jobs: [fn(job) for job in jobs]),
            patch.object(runner, "compile_unit", side_effect=lambda *a, **kw: nullcontext(self.root / "alpha.o")),
            patch.object(runner, "place"),
            patch.object(
                runner,
                "link",
                side_effect=lambda project, host, obj, version, row, work, file: split.words(project, row),
            ),
            patch.object(
                drivers,
                "run_preprocess",
                side_effect=lambda project, argv, phase, **kw: preprocess(argv, project.root, phase, **kw),
            ),
            patch.object(land, "_commit", side_effect=lambda project, host, paths, message: staged.extend(paths)),
            patch.object(land, "_git", return_value="committed\n"),
            patch.object(land.buildfiles, "write", return_value=[]),
            patch.object(land.steps, "record"),
            patch("unbake.report.progress.write", return_value=[]),
        ):
            self.assertEqual(land.land(self.project, self.host, file), "committed")
        self.assertTrue({headers["authored/root.h"], headers["authored/us.h"], headers["authored/eu.h"]} <= set(staged))
        self.assertNotIn(headers["unrelated.h"], staged)

    def test_failed_commit_preserves_the_previous_index_bytes(self):
        index = self.project.root / ".git" / "index"
        index.parent.mkdir()
        index.write_bytes(b"previous staged user state")
        owned = self.project.root / "owned.h"
        owned.write_text("owned\n")

        def git(project, *args, env=None):
            if args[:2] == ("rev-parse", "--git-path"):
                return str(index)
            if args[0] == "add":
                index.write_bytes(b"new publication stage")
            if "commit" in args:
                raise Held(named("fixture.refusal", "git commit exited 1: hook refused", owner="fixture", stage="land"))
            return ""

        with (
            patch.object(land, "_git", side_effect=git),
            patch.object(land, "_refuse_dangling_includes"),
            self.assertRaisesRegex(Held, "hook refused"),
        ):
            land._commit(self.project, self.host, [owned], "Publish owned input")
        self.assertEqual(index.read_bytes(), b"previous staged user state")

    def test_native_git_refusal_keeps_its_invocation_and_both_streams(self):
        import subprocess

        with (
            patch.object(
                process.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["git"], 1, "hook stdout\n", "hook stderr\n"),
            ),
            self.assertRaises(Held) as caught,
        ):
            land._git(self.project, "commit", "--only", "--", "owned.h")
        fault = caught.exception.fault.document()["chain"][0]["result"]
        self.assertEqual(fault["args"], ("git", "commit", "--only", "--", "owned.h"))
        self.assertEqual(fault["cwd"], str(self.project.root))
        self.assertEqual((fault["exit"], fault["stdout"], fault["stderr"]), (1, "hook stdout\n", "hook stderr\n"))

    def test_dependency_output_must_identify_the_proved_source(self):
        file = self.project.src / "alpha.c"
        file.write_text("int alpha(void) { return 1; }\n")
        with (
            patch.object(drivers, "run_preprocess", return_value="alpha.o:\n"),
            self.assertRaisesRegex(Held, "compile.dependencies"),
        ):
            runner.dependencies(self.project, self.host, file, "us", unit="alpha")

    def test_dependency_ownership_preserves_staged_shared_native_and_external_inputs(self):
        stage = self.project.work / "_land" / "alpha"
        authored = self.project.src / "literal.inc"
        authored.write_text("/* source-relative prerequisite */\n")
        native = self.project.tools / "ido-7.1" / "include" / "native.h"
        external = self.root / "external.h"
        external.write_text("/* host prerequisite */\n")
        with (
            patch.object(
                land, "_prove_versions", return_value={stage / "include" / "new.h", authored, native, external}
            ),
            patch("unbake.layout.header_step.validate"),
        ):
            proof = land.prove(
                self.project,
                self.host,
                "alpha",
                "int alpha(void) { return 1; }\n",
                {"new.h": "typedef int New;\n"},
                stage,
            )
        self.assertEqual(proof.versions, list(self.versions))
        self.assertEqual(proof.dependencies, {authored, self.project.include[-1] / "new.h"})

    def test_disposable_and_symlink_dependencies_refuse_before_publication(self):
        external = self.root / "external.h"
        external.write_text("/* external */\n")
        linked = self.project.include[-1] / "linked.h"
        linked.symlink_to(external)
        for dependency in (self.project.build / "temporary.h", linked):
            with (
                self.subTest(dependency=dependency),
                patch.object(land, "_prove_versions", return_value={dependency}),
                self.assertRaisesRegex(Held, "land.dependency"),
            ):
                land.prove(
                    self.project,
                    self.host,
                    "alpha",
                    "int alpha(void) { return 1; }\n",
                    {},
                    self.project.work / "_land" / "alpha",
                )

    def test_native_ido_rules_retain_every_path_and_literal_spaces(self):
        file = self.project.src / "alpha.c"
        file.write_text("int alpha(void) { return 1; }\n")
        nested = self.project.include[-1] / "nested.h"
        spaced = self.project.include[-1] / "space name.h"
        text = "\n".join(f"alpha.o:\t{path}" for path in (file, nested, spaced)) + "\n"
        with patch.object(drivers, "run_preprocess", return_value=text):
            self.assertEqual(runner.dependencies(self.project, self.host, file, "us", unit="alpha"), {nested, spaced})
