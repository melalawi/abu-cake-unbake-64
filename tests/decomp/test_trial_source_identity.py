"""Actual trial hashes preserve source identity across publication wrappers."""

import hashlib
import io
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import cast
from unittest.mock import patch

from tests.decomp.support import assemble, assembly, fixture
from unbake.decomp import drafts
from unbake.decomp.trial import try_draft
from unbake.project import build
from unbake.project.config import Policy


class TrialSourceIdentityTests(unittest.TestCase):
    def test_comments_and_includes_before_guard_share_the_published_identity(self) -> None:
        prefix = b'/* renderer */\n#include "types.h"\n\n'
        body = b"#if PAL\nint alpha(void) { return 1; }\n#else\nint alpha(void) { return 2; }\n#endif\n"
        plain = prefix + body
        guarded = prefix + b"#ifdef NON_MATCHING\n" + body + b"#endif\n"
        self.assertTrue(drafts.is_partial(guarded.decode()))
        self.assertEqual(drafts.canonical_source(guarded), plain)
        self.assertEqual(drafts.source_identity(guarded), drafts.source_identity(plain))
        alternatives = prefix + b"#ifdef NON_MATCHING\n" + body + b"#else\nint alpha;\n#endif\n"
        self.assertFalse(drafts.is_partial(alternatives.decode()))
        self.assertEqual(drafts.canonical_source(alternatives), alternatives)
        declarations = b"int elsewhere;\n" + guarded
        self.assertFalse(drafts.is_partial(declarations.decode()))

    def test_guarded_and_plain_trials_share_identity_without_changing_compiled_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project, policy, source = fixture(root)
            plain = b"#if DEBUG\nint alpha(void) { return 1; }\n#endif\n\n"
            compiled = []

            def compile_source(project: object, policy: object, copied: Path, version: str, out: Path) -> Path:
                compiled.append(copied.read_bytes().split(b"\n", 2)[2])
                shutil.copyfile(assemble(out.parent, "compiled", assembly("alpha", [0x24020001, 0x03E00008, 0])), out)
                return out

            identities = []
            contents = (plain, b"#ifdef NON_MATCHING\n" + plain + b"#endif\n")
            with patch.object(build, "compile_object", side_effect=compile_source), redirect_stdout(io.StringIO()):
                for content in contents:
                    source.write_bytes(content)
                    result = try_draft(project, cast(Policy, policy), source, root / "scratch")
                    self.assertTrue(result.identical_everywhere)
                    identities.append(result.source_sha256)
                    self.assertEqual(source.read_bytes(), content)
            self.assertEqual(identities, [hashlib.sha256(plain).hexdigest()] * 2)
            self.assertEqual(compiled, list(contents))
            changed = plain.replace(b"return 1", b"return 2")
            self.assertNotEqual(drafts.source_identity(changed), identities[0])
            # Conditional source with an alternative body is not a publication wrapper.
            alternative = b"#ifdef NON_MATCHING\n" + plain + b"#else\nint alpha(void) { return 2; }\n#endif\n"
            self.assertEqual(drafts.source_identity(alternative), hashlib.sha256(alternative).hexdigest())
