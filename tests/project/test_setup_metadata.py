"""A fresh public setup supplies the ROM metadata required by publication reports."""

from types import SimpleNamespace

from tests.project_fixture import ProjectCase
from unbake import config
from unbake.project import setup_config


class SetupMetadataTests(ProjectCase):
    def test_measured_cartridge_fields_make_fresh_setup_publishable(self):
        pending = config.load_pending(self.project.root)
        cartridges = []
        for name in self.project.versions:
            version = self.project.version(name)
            cartridges.append(
                SimpleNamespace(
                    path=version.baserom,
                    sha1=version.baserom_sha1,
                    header=SimpleNamespace(
                        title="Proof cartridge", category="N", game_code="PF", region="D", revision=2, cic="6102/7101"
                    ),
                )
            )
        census = SimpleNamespace(
            cartridges=cartridges,
            names={c.path: v for c, v in zip(cartridges, self.project.versions, strict=True)},
            names_from="us",
            versions=self.project.versions,
        )
        data = setup_config.facts(pending, census, name=None, title=None)
        for version in self.project.versions:
            self.assertEqual(data["version"][version]["cartridge_id"], "NPFD")
            self.assertEqual(data["version"][version]["region"], "de")
            self.assertIn("revision 2", data["version"][version]["description"])
            self.assertIn("Proof cartridge", data["version"][version]["description"])
        path = self.project.root / "config.toml"
        path.write_text(
            path.read_text().replace(
                "[version.us]",
                '[version.us]\ncartridge_id="authored ID"\nregion="authored region"\n'
                'description="authored description"',
            )
        )
        retained = setup_config.facts(pending, census, name=None, title=None)["version"]["us"]
        self.assertEqual(
            [retained[k] for k in ("cartridge_id", "region", "description")],
            ["authored ID", "authored region", "authored description"],
        )
