"""Every data file the package reads at run time is declared, so a normal (non-editable) install carries it."""

import tomllib
import unittest
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class PackageDataTests(unittest.TestCase):
    def test_every_source_data_file_is_declared(self) -> None:
        declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]
        missing = []
        for path in sorted((ROOT / "src" / "unbake").rglob("*")):
            if not path.is_file() or path.suffix in {".py", ".pyc"} or "__pycache__" in path.parts:
                continue
            package = ".".join(path.parent.relative_to(ROOT / "src").parts)
            if not any(fnmatch(path.name, pattern) for pattern in declared.get(package, [])):
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(missing, [])

    def test_every_declared_name_exists(self) -> None:
        declared = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["setuptools"]["package-data"]
        for package, patterns in declared.items():
            folder = ROOT / "src" / Path(*package.split("."))
            for pattern in patterns:
                self.assertTrue(list(folder.glob(pattern)), f"{package}: {pattern} matches nothing")


if __name__ == "__main__":
    unittest.main()
