"""Only exact published bytes feed the whole-program solver."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.match.support import MatchFixture
from unbake.decomp import type_context, work


class TypeContextTests(MatchFixture):
    def test_current_database_digest_and_context_are_consumed_together(self) -> None:
        path = self.project.build / "types/database.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"revision": 2}\n')
        api = SimpleNamespace(load=Mock(), context=Mock(return_value='#include "shared/typemap.h"\n'))
        with patch.object(type_context, "provider", return_value=api):
            digest, context = type_context.required(self.project)
        api.load.assert_called_once_with(self.project, required=True)
        self.assertEqual(digest, work.digest(path.read_bytes()))
        self.assertIn("typemap.h", context)
