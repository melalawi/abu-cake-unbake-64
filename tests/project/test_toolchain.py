"""Offline compiler setup tests; scratch stays under the explicit TMPDIR."""

import hashlib
import io
import os
import platform
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from unbake.project import compiler_files, toolchain
from unbake.config import Held


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class ToolchainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = tempfile.TemporaryDirectory(dir=os.environ["TMPDIR"])
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name).resolve()
        self.registry_path = self.root / "compilers.toml"
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.project = SimpleNamespace(
            root=self.project_root, tools=self.project_root / "tools", compilers={}, versions=("us",)
        )
        self.policy = SimpleNamespace(cache_root=self.root / "cache")
        self.entries = []
        self.config_entries = []
        self.patcher = patch.object(toolchain, "REGISTRY_PATH", self.registry_path)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def archive(self, name: str, files: Any, *, zip_archive: bool = False) -> Path:
        path = self.root / name
        if zip_archive:
            with zipfile.ZipFile(path, "w") as archive:
                for filename, content in files.items():
                    archive.writestr(filename, content)
        else:
            with tarfile.open(path, "w:gz") as archive:
                for filename, content in files.items():
                    member = tarfile.TarInfo(filename)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
        return path

    def compiler(
        self,
        ident: Any = "fixture",
        *,
        source: Any = "download",
        supplied: Any = None,
        archive: Any = None,
        files: Any = None,
        downloaded: Any = None,
        archive_digest: Any = None,
    ) -> tuple[Path | None, dict[str, str]]:
        files = {"cc": b"compiler", "as": b"assembler"} if files is None else files
        pins = {name: digest(content) for name, content in files.items()}
        downloaded = tuple(files) if downloaded is None else downloaded
        if source != "supplied" and archive is None:
            archive = self.archive(ident + ".tar.gz", {"nested/" + name: files[name] for name in downloaded})
        host = platform.system().lower() + "-" + platform.machine().lower()
        entry = (
            f'[compilers."{ident}"]\nkind="ido"\nsource="{source}"\nhost="{host}"\n'
            'family="ido"\ndecompme="fixture"\ncc="cc"\nas="as"\ncflags=["-O2"]\ndrivers=[]\n'
            f'[compilers."{ident}".pins]\n'
        )
        entry += "".join(f'"{name}"="{pin}"\n' for name, pin in pins.items())
        if source != "supplied":
            sha = archive_digest or digest(archive.read_bytes())
            names = ",".join('"' + name + '"' for name in downloaded)
            entry += f'[[compilers."{ident}".downloads]]\nurl="{archive.as_uri()}"\nsha256="{sha}"\nfiles=[{names}]\n'
        self.entries.append(entry)
        self.registry_path.write_text("\n".join(self.entries))
        config = f'[compilers."{ident}"]\n'
        if supplied is not None:
            config += f'supply="{supplied}"\n'
        self.config_entries.append(config)
        (self.project_root / "config.toml").write_text("\n".join(self.config_entries))
        self.project.compilers[ident] = SimpleNamespace(id=ident, kind="ido")
        return archive, pins

    def test_registry_host_assembler_requires_only_the_compiler_pin(self) -> None:
        self.compiler(files={"cc": b"compiler"})
        self.registry_path.write_text(self.registry_path.read_text().replace('as="as"', 'as="policy:mips_as"'))
        specification = toolchain.registry()["fixture"]
        self.assertEqual(specification.pins, {"cc": digest(b"compiler")})
        self.assertEqual(specification.as_, "policy:mips_as")
        toolchain.ensure(self.project, self.policy)
        self.assertEqual((self.project.tools / "fixture/cc").read_bytes(), b"compiler")
        self.assertFalse((self.project.tools / "fixture/as").exists())

    def test_download_copies_and_project_relative_manifest(self) -> None:
        _, pins = self.compiler()
        manifest = toolchain.ensure(self.project, self.policy)
        self.assertEqual(manifest, self.project.tools / "compiler.sha256")
        self.assertEqual(
            manifest.read_text(), "".join(f"{pins[name]}  tools/fixture/{name}\n" for name in sorted(pins))
        )
        for name in pins:
            link = self.project.tools / "fixture" / name
            self.assertFalse(link.is_symlink())
            self.assertEqual(link.read_bytes(), (self.policy.cache_root / "compilers/fixture" / name).read_bytes())
        self.assertFalse((self.project.tools / "fixture").is_symlink())

    def test_acquire_candidate_needs_no_project_and_writes_no_project_files(self) -> None:
        self.compiler()
        config_before = (self.project_root / "config.toml").read_bytes()
        directory = toolchain.acquire(toolchain.specification("fixture"), self.policy)
        self.assertEqual(directory, self.policy.cache_root / "compilers/fixture")
        self.assertEqual((directory / "cc").read_bytes(), b"compiler")
        self.assertEqual((self.project_root / "config.toml").read_bytes(), config_before)
        self.assertFalse(self.project.tools.exists())

    def test_acquire_refuses_a_changed_registry_specification(self) -> None:
        self.compiler()
        spec = toolchain.specification("fixture")
        self.registry_path.write_text(self.registry_path.read_text().replace('cflags=["-O2"]', 'cflags=["-O1"]'))
        with self.assertRaisesRegex(Held, "setup.proposal_stale:"):
            toolchain.acquire(spec, self.policy)
        self.assertFalse(self.project.tools.exists())

    def test_existing_verified_symlink_is_replaced_without_changing_cache(self) -> None:
        self.compiler()
        toolchain.ensure(self.project, self.policy)
        target = self.project.tools / "fixture/cc"
        cached = self.policy.cache_root / "compilers/fixture/cc"
        before = cached.stat().st_mtime_ns
        target.unlink()
        target.symlink_to(cached)
        toolchain.ensure(self.project, self.policy)
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.read_bytes(), b"compiler")
        self.assertEqual(cached.stat().st_mtime_ns, before)

    def test_warm_install_never_fetches(self) -> None:
        archive, _ = self.compiler()
        manifest = toolchain.ensure(self.project, self.policy)
        archive.unlink()
        with patch.object(toolchain.urllib.request, "urlopen", side_effect=AssertionError("network")):
            self.assertEqual(toolchain.ensure(self.project, self.policy), manifest)

    def test_stale_cache_is_replaced_from_verified_archive(self) -> None:
        self.compiler()
        toolchain.ensure(self.project, self.policy)
        cached = self.policy.cache_root / "compilers/fixture/cc"
        cached.write_bytes(b"changed")
        with redirect_stdout(io.StringIO()) as output:
            toolchain.ensure(self.project, self.policy)
        self.assertEqual(cached.read_bytes(), b"compiler")
        self.assertIn(
            f"REPLACED(setup): {cached}: sha256 {digest(b'changed')} -> {digest(b'compiler')}", output.getvalue()
        )

    def test_stale_project_copy_is_replaced(self) -> None:
        self.compiler()
        toolchain.ensure(self.project, self.policy)
        link = self.project.tools / "fixture/cc"
        link.unlink()
        link.write_bytes(b"changed")
        with redirect_stdout(io.StringIO()) as output:
            toolchain.ensure(self.project, self.policy)
        self.assertEqual(link.read_bytes(), b"compiler")
        self.assertIn(
            f"REPLACED(setup): {link}: sha256 {digest(b'changed')} -> {digest(b'compiler')}", output.getvalue()
        )

    def repin(self, **kwargs: Any) -> dict[str, str]:
        self.entries.clear()
        self.config_entries.clear()
        _, pins = self.compiler(files={"cc": b"new compiler", "as": b"new assembler"}, **kwargs)
        return pins

    def test_changed_registry_pins_replace_cache_and_project_atomically(self) -> None:
        self.compiler()
        manifest = toolchain.ensure(self.project, self.policy)
        old = {"cc": b"compiler", "as": b"assembler"}
        pins = self.repin()
        original_replace = os.replace
        replacements = []

        def replace(source: Any, destination: Any) -> None:
            target = Path(destination)
            if target.name in pins and target.exists():
                self.assertEqual(target.read_bytes(), old[target.name])
                self.assertEqual(compiler_files.sha(Path(source)), pins[target.name])
                self.assertTrue(Path(source).stat().st_mode & 0o111)
                replacements.append(target)
            original_replace(source, destination)

        with redirect_stdout(io.StringIO()) as output, patch.object(toolchain.os, "replace", side_effect=replace):
            toolchain.ensure(self.project, self.policy)
        self.assertEqual(len(replacements), 4)
        self.assertEqual(output.getvalue().count("REPLACED(setup):"), 4)
        for directory in (self.project.tools / "fixture", self.policy.cache_root / "compilers/fixture"):
            self.assertEqual(toolchain.verify(directory, toolchain.specification("fixture")), pins)
            self.assertEqual({path.name for path in directory.iterdir()}, set(pins))
        self.assertEqual(
            manifest.read_text(), "".join(f"{pins[name]}  tools/fixture/{name}\n" for name in sorted(pins))
        )
        with patch.object(toolchain.urllib.request, "urlopen", side_effect=AssertionError("network")):
            toolchain.ensure(self.project, self.policy)

    def test_wrong_download_hash_preserves_stale_cache_project_and_manifest(self) -> None:
        self.compiler()
        manifest = toolchain.ensure(self.project, self.policy)
        paths = [manifest, self.project.tools / "fixture/cc", self.policy.cache_root / "compilers/fixture/cc"]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        self.repin(archive_digest="0" * 64)
        with redirect_stdout(io.StringIO()) as output, self.assertRaisesRegex(Held, "archive sha256 expected"):
            toolchain.ensure(self.project, self.policy)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths])
        self.assertEqual(list((self.policy.cache_root / "compilers").glob(".fixture-*")), [])

    def test_wrong_member_pin_verifies_all_files_before_replacing_any(self) -> None:
        self.compiler()
        manifest = toolchain.ensure(self.project, self.policy)
        paths = [manifest, self.project.tools / "fixture/cc", self.policy.cache_root / "compilers/fixture/cc"]
        before = [path.read_bytes() for path in paths]
        archive = self.archive("bad-update.tar.gz", {"cc": b"new compiler", "as": b"wrong assembler"})
        self.repin(archive=archive)
        with redirect_stdout(io.StringIO()) as output, self.assertRaisesRegex(Held, "pins.as: missing"):
            toolchain.ensure(self.project, self.policy)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(before, [path.read_bytes() for path in paths])

    def test_stale_project_requires_verified_archive_even_with_valid_cache(self) -> None:
        archive, _ = self.compiler()
        toolchain.ensure(self.project, self.policy)
        target = self.project.tools / "fixture/cc"
        target.write_bytes(b"changed")
        downloaded = self.policy.cache_root / "compilers/downloads" / digest(archive.read_bytes())
        downloaded.write_bytes(b"wrong archive")
        with self.assertRaisesRegex(Held, "sha256 expected"):
            toolchain.ensure(self.project, self.policy)
        self.assertEqual(target.read_bytes(), b"changed")

    def test_correct_project_copy_is_preserved(self) -> None:
        self.compiler()
        toolchain.ensure(self.project, self.policy)
        link = self.project.tools / "fixture/cc"
        link.unlink()
        link.write_bytes(b"compiler")
        toolchain.ensure(self.project, self.policy)
        self.assertFalse(link.is_symlink())

    def test_multiple_compilers_share_host_install_across_versions(self) -> None:
        self.compiler("ido")
        self.compiler("gcc")
        first = toolchain.ensure(self.project, self.policy)
        self.assertEqual(len(first.read_text().splitlines()), 4)
        other = self.root / "other"
        other.mkdir()
        (other / "config.toml").write_text((self.project_root / "config.toml").read_text())
        project = SimpleNamespace(
            root=other,
            tools=other / "tools",
            compilers=self.project.compilers,
            versions=("us", "us-rev1", "eu", "eu-mul", "de"),
        )
        with patch.object(toolchain.urllib.request, "urlopen", side_effect=AssertionError("network")):
            toolchain.ensure(project, self.policy)
        self.assertEqual((other / "tools/ido/cc").read_bytes(), (self.project.tools / "ido/cc").read_bytes())

    def test_supplied_directory_finds_pins_despite_renamed_files(self) -> None:
        source = self.root / "supply/nested"
        source.mkdir(parents=True)
        (source / "renamed.compiler").write_bytes(b"compiler")
        (source / "renamed.assembler").write_bytes(b"assembler")
        self.compiler(source="supplied", supplied=source.parent)
        toolchain.ensure(self.project, self.policy)
        self.assertEqual((self.project.tools / "fixture/cc").read_bytes(), b"compiler")

    def test_relative_supply_path(self) -> None:
        source = self.project_root / "supplied"
        source.mkdir()
        (source / "cc").write_bytes(b"compiler")
        (source / "as").write_bytes(b"assembler")
        self.compiler(source="supplied", supplied="supplied")
        toolchain.ensure(self.project, self.policy)

    def test_supplied_zip_and_cli_override(self) -> None:
        archive = self.archive("supplied.zip", {"elsewhere/cc": b"compiler", "bin/as": b"assembler"}, zip_archive=True)
        self.compiler(source="supplied")
        manifest = toolchain.supply(self.project, self.policy, archive)
        self.assertTrue(manifest.is_file())
        archive.unlink()
        toolchain.ensure(self.project, self.policy)

    def test_mixed_download_and_supply(self) -> None:
        source = self.root / "supplied"
        source.mkdir()
        (source / "as").write_bytes(b"assembler")
        self.compiler(source="mixed", supplied=source, downloaded=("cc",))
        toolchain.ensure(self.project, self.policy)
        self.assertEqual((self.project.tools / "fixture/as").read_bytes(), b"assembler")

    def test_mixed_replacement_preserves_verified_supplied_file_without_source(self) -> None:
        source = self.root / "supplied"
        source.mkdir()
        supplied = source / "as"
        supplied.write_bytes(b"assembler")
        self.compiler(source="mixed", supplied=source, downloaded=("cc",))
        toolchain.ensure(self.project, self.policy)
        supplied.unlink()
        self.entries.clear()
        self.config_entries.clear()
        self.compiler(source="mixed", downloaded=("cc",), files={"cc": b"new compiler", "as": b"assembler"})
        with redirect_stdout(io.StringIO()):
            toolchain.ensure(self.project, self.policy)
        self.assertEqual((self.project.tools / "fixture/cc").read_bytes(), b"new compiler")
        self.assertEqual((self.project.tools / "fixture/as").read_bytes(), b"assembler")

    def test_changed_supplied_pin_still_requires_exact_supplied_content(self) -> None:
        source = self.root / "supplied"
        source.mkdir()
        (source / "cc").write_bytes(b"compiler")
        (source / "as").write_bytes(b"assembler")
        self.compiler(source="supplied", supplied=source)
        manifest = toolchain.ensure(self.project, self.policy)
        before = manifest.read_bytes()
        self.repin(source="supplied", supplied=source)
        with self.assertRaisesRegex(Held, "pins.cc: missing"):
            toolchain.ensure(self.project, self.policy)
        self.assertEqual(manifest.read_bytes(), before)
        self.assertEqual((self.project.tools / "fixture/cc").read_bytes(), b"compiler")
        self.assertEqual((self.policy.cache_root / "compilers/fixture/cc").read_bytes(), b"compiler")

    def test_missing_supply_names_value_and_files(self) -> None:
        self.compiler(source="supplied")
        with self.assertRaisesRegex(Held, r"compilers.fixture.*supply.*as, cc"):
            toolchain.ensure(self.project, self.policy)
        self.assertFalse((self.policy.cache_root / "compilers/fixture").exists())

    def test_incorrect_supplied_pin_does_not_publish_partial_install(self) -> None:
        source = self.root / "supply"
        source.mkdir()
        (source / "cc").write_bytes(b"compiler")
        (source / "as").write_bytes(b"wrong")
        self.compiler(source="supplied", supplied=source)
        with self.assertRaisesRegex(Held, r"pins.as: missing.*SHA-256"):
            toolchain.ensure(self.project, self.policy)
        self.assertFalse((self.policy.cache_root / "compilers/fixture").exists())
        self.assertEqual(list((self.policy.cache_root / "compilers").glob(".fixture-*")), [])

    def test_wrong_download_archive_digest(self) -> None:
        self.compiler(archive_digest="0" * 64)
        with self.assertRaisesRegex(Held, "archive sha256 expected"):
            toolchain.ensure(self.project, self.policy)
        self.assertFalse((self.policy.cache_root / "compilers/fixture").exists())
        self.assertEqual(list((self.policy.cache_root / "compilers/downloads").iterdir()), [])

    def test_wrong_file_pin_in_verified_archive(self) -> None:
        archive = self.archive("bad.tar.gz", {"cc": b"other", "as": b"assembler"})
        self.compiler(archive=archive)
        with self.assertRaisesRegex(Held, "pins.cc: missing"):
            toolchain.ensure(self.project, self.policy)

    def test_cached_archive_digest_is_checked(self) -> None:
        archive, _ = self.compiler()
        path = self.policy.cache_root / "compilers/downloads" / digest(archive.read_bytes())
        path.parent.mkdir(parents=True)
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(Held, "sha256 expected"):
            toolchain.ensure(self.project, self.policy)

    def test_downloaded_zip(self) -> None:
        archive = self.archive("download.zip", {"cc": b"compiler", "as": b"assembler"}, zip_archive=True)
        self.compiler(archive=archive)
        toolchain.ensure(self.project, self.policy)

    def test_archive_traversal_rejected(self) -> None:
        for filename in ("../outside", "/absolute"):
            with self.subTest(filename=filename):
                archive = self.archive("unsafe.tar.gz", {filename: b"compiler"})
                with self.assertRaisesRegex(Held, "unsafe file name"):
                    compiler_files.archive_files(archive, {digest(b"compiler")})
        self.assertFalse((self.root.parent / "outside").exists())

    def test_archive_symlink_rejected(self) -> None:
        archive = self.root / "link.tar"
        with tarfile.open(archive, "w") as stream:
            member = tarfile.TarInfo("cc")
            member.type = tarfile.SYMTYPE
            member.linkname = "/etc/passwd"
            stream.addfile(member)
        with self.assertRaisesRegex(Held, "unsupported archive entry cc"):
            compiler_files.archive_files(archive, set())

    def test_zip_symlink_rejected(self) -> None:
        archive = self.root / "link.zip"
        with zipfile.ZipFile(archive, "w") as stream:
            member = zipfile.ZipInfo("cc")
            member.external_attr = 0o120777 << 16
            stream.writestr(member, "/etc/passwd")
        with self.assertRaisesRegex(Held, "archive link cc"):
            compiler_files.archive_files(archive, set())

    def test_missing_registry_field_refused_by_name(self) -> None:
        self.compiler()
        self.registry_path.write_text(self.registry_path.read_text().replace('kind="ido"\n', ""))
        with self.assertRaisesRegex(Held, "kind: missing value"):
            toolchain.registry()

    def test_registry_rejects_unknown_executable_and_uncovered_pins(self) -> None:
        self.compiler()
        text = self.registry_path.read_text()
        for modified, message in (
            (text.replace('cc="cc"', 'cc="unknown"'), "has no file pin"),
            (text.replace('files=["cc","as"]', 'files=["cc"]'), "missing as"),
        ):
            with self.subTest(message=message):
                self.registry_path.write_text(modified)
                with self.assertRaisesRegex(Held, message):
                    toolchain.registry()

    def test_unknown_project_compiler(self) -> None:
        self.compiler()
        self.project.compilers["unknown"] = SimpleNamespace(id="unknown", kind="ido")
        with self.assertRaisesRegex(Held, "unknown registry id"):
            toolchain.ensure(self.project, self.policy)

    def test_empty_compiler_set_is_refused(self) -> None:
        with self.assertRaisesRegex(Held, r"compilers.*nonempty"):
            toolchain.ensure(self.project, self.policy)

    def test_missing_project_compiler_table(self) -> None:
        self.compiler()
        (self.project_root / "config.toml").write_text("[project]\n")
        with self.assertRaisesRegex(Held, "compilers: missing value"):
            toolchain.ensure(self.project, self.policy)

    def test_policy_requires_absolute_cache(self) -> None:
        self.compiler()
        self.policy.cache_root = Path("relative")
        with self.assertRaisesRegex(Held, "policy.cache_root"):
            toolchain.ensure(self.project, self.policy)

    def test_tools_must_be_inside_project(self) -> None:
        self.compiler()
        self.project.tools = self.root / "external"
        with self.assertRaisesRegex(Held, r"paths.*tools"):
            toolchain.ensure(self.project, self.policy)

    def test_tools_symlink_cannot_escape_project(self) -> None:
        self.compiler()
        outside = self.root / "outside"
        outside.mkdir()
        self.project.tools.symlink_to(outside)
        with self.assertRaisesRegex(Held, r"paths.*tools"):
            toolchain.ensure(self.project, self.policy)

    def test_missing_compiler_id_is_refused_by_name(self) -> None:
        self.compiler()
        self.project.compilers["fixture"] = SimpleNamespace(kind="ido")
        with self.assertRaisesRegex(Held, "missing contract value.*id"):
            toolchain.ensure(self.project, self.policy)

    def test_wrong_host_is_refused(self) -> None:
        self.compiler()
        text = self.registry_path.read_text()
        host = platform.system().lower() + "-" + platform.machine().lower()
        self.registry_path.write_text(text.replace(host, "other-machine"))
        with self.assertRaisesRegex(Held, "host: requires other-machine"):
            toolchain.ensure(self.project, self.policy)

    def test_status_verifies_pins_without_downloading(self) -> None:
        self.compiler()
        self.assertIn("cc", toolchain.status(self.policy)[0][1])
        toolchain.ensure(self.project, self.policy)
        self.assertEqual(toolchain.status(self.policy), [("fixture", "installed; pins verified")])
        (self.policy.cache_root / "compilers/fixture/as").write_bytes(b"changed")
        self.assertIn("sha256 expected", toolchain.status(self.policy)[0][1])

    def test_registry_exposes_fingerprint_metadata(self) -> None:
        self.compiler()
        spec = toolchain.specification("fixture")
        self.assertEqual(spec.family, "ido")
        self.assertEqual(spec.decompme, "fixture")
