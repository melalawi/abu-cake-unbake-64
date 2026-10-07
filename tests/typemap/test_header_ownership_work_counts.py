"""A missing-index RageWars header catalogue is recovered once per operation, retaining edit invalidation."""

from functools import partial
from pathlib import Path
from unittest.mock import patch

from tests.project_fixture import ProjectCase
from unbake.layout import index
from unbake.typemap import declarations, storage
from unbake.typemap.split import guarded

FIXTURE = Path(__file__).parent / "fixtures/ragewars_vec3/include/common"


class HeaderOwnershipWorkCounts(ProjectCase):
    versions = ("us",)

    def setUp(self):
        super().setUp()
        self.headers = []
        for name in ("types_8a8189af7b05.h", "types_8fd754e1e915.h"):
            path = self.project.include[0] / "common" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes((FIXTURE / name).read_bytes())
            self.headers.append(path)
        self.assertFalse(index.path(self.project).exists())

    def view(self):
        factory = getattr(storage, "generated_view", None)
        return factory(self.project) if factory is not None else partial(storage.generated, self.project)

    def test_repeated_real_header_checks_do_one_recovery_and_do_not_reparse_layout_per_file(self):
        scalar = self.project.include[0] / "types.h"
        with patch.object(index, "_unindexed_headers", wraps=index._unindexed_headers) as recovered:
            generated = self.view()
            for _ in range(4):
                self.assertTrue(all(generated(path) for path in self.headers))
                self.assertFalse(generated(scalar))
            self.assertEqual(recovered.call_count, 1)

    def test_real_declaration_collection_bounds_missing_index_recovery(self):
        with patch.object(index, "_unindexed_headers", wraps=index._unindexed_headers) as recovered:
            seeds = declarations.collect(self.project, None, [])
        self.assertTrue(seeds)
        self.assertLessEqual(recovered.call_count, 3)

    def test_a_new_operation_observes_added_headers_and_changed_guards(self):
        first = self.view()
        self.assertTrue(first(self.headers[0]))
        self.headers[0].write_text("typedef struct { float x, y, z; } AuthoredVec;\n")
        added = self.project.include[0] / "common/types_0123456789ab.h"
        added.write_bytes(guarded(added.relative_to(self.project.include[0]), "typedef int Extra;\n"))
        changed = self.view()
        self.assertFalse(changed(self.headers[0]))
        self.assertTrue(changed(added))
        # No global cache pins the old catalogue across layout/guard edits.
        self.assertFalse(storage.generated(self.project, self.headers[0]))

    def test_manifest_owned_header_is_owned_even_without_a_matching_guard(self):
        header = self.headers[0]
        header.write_text("typedef int ManifestOwned;\n")
        lookup = {
            "schema": 1,
            "symbols": {},
            "clusters": {},
            "headers": {header.relative_to(self.project.include[0]).as_posix(): "a" * 64},
        }
        target = index.path(self.project)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(index.encoded(lookup))
        self.assertTrue(self.view()(header))
