"""Host (unbake.toml) and project (config.toml) loading: precedence, merge and every refusal."""

import os
import tomllib
from pathlib import Path
from unittest.mock import patch

from tests.kit import TempCase, edited, host_values
from unbake import config
from unbake.config import Held, Host

BAD_VALUES = {
    "int": [0, -1, "3", True, 1.5],
    "path": ["relative/dir"],
    "dirs": [[], ["relative"], ["/nonexistent-dir-for-unbake-tests"]],
    "fraction": [0, 1.5, "half"],
    "hex64": ["abc", "g" * 64],
    "text": ["", "  "],
}
SAMPLE_KEY = {
    "int": "resources.cores",
    "path": "cache.machine_root",
    "dirs": "tools.path",
    "fraction": "setup.same_game_similarity",
    "hex64": "tools.permuter_sha256",
    "text": "publish.branch",
}


class HostPathTests(TempCase):
    def test_precedence(self) -> None:
        explicit = self.root / "explicit.toml"
        cases = [
            ("explicit wins", explicit, {"UNBAKE_CONFIG": "/env.toml", "XDG_CONFIG_HOME": "/xdg"}, explicit),
            ("env next", None, {"UNBAKE_CONFIG": "/env.toml", "XDG_CONFIG_HOME": "/xdg"}, Path("/env.toml")),
            ("xdg next", None, {"XDG_CONFIG_HOME": "/xdg"}, Path("/xdg/unbake/unbake.toml")),
            ("home fallback", None, {"HOME": str(self.root)}, self.root / ".config/unbake/unbake.toml"),
        ]
        for label, argument, env, expected in cases:
            with self.subTest(label), patch.dict(os.environ, env):
                for name in {"UNBAKE_CONFIG", "XDG_CONFIG_HOME"} - set(env):
                    os.environ.pop(name, None)
                self.assertEqual(config.host_path(argument), expected)


class LoadHostTests(TempCase):
    def write(self, path: Path, values: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for section, table in values.items():
            lines.append(f"[{section}]")
            lines += [f"{key} = {self.literal(value)}" for key, value in table.items()]
        path.write_text("\n".join(lines) + "\n")
        return path

    def literal(self, value: object) -> str:
        if isinstance(value, list):
            return "[" + ", ".join(self.literal(item) for item in value) + "]"
        return repr(value).replace("'", '"') if isinstance(value, str) else str(value).lower()

    def test_project_override_replaces_single_keys(self) -> None:
        values = host_values(self.root)
        user = self.write(self.root / "user.toml", values)
        project = self.root / "project"
        self.write(project / ".unbake/unbake.toml", {"resources": {"cores": 7}})
        host = config.load_host(user, project, "check")
        self.assertEqual(host.cores, 7)
        self.assertEqual(host.make, Path(values["tools"]["make"]))
        self.assertEqual(tomllib.loads(user.read_text())["resources"]["cores"], 4)

    def test_user_file_alone_when_project_has_no_override(self) -> None:
        user = self.write(self.root / "user.toml", host_values(self.root))
        self.assertEqual(config.load_host(user, self.root / "project", "check").cores, 4)
        self.assertEqual(config.load_host(user, None, "check").cores, 4)

    def test_missing_file(self) -> None:
        missing = self.root / "none.toml"
        with self.assertRaises(Held) as raised:
            config.load_host(missing, None, "check")
        self.assertEqual(raised.exception.reason, f"unbake.toml: missing file {missing}")
        self.assertEqual(raised.exception.phase, "config")

    def test_unknown_names_in_either_file(self) -> None:
        values = host_values(self.root)
        project = self.root / "project"
        bad_key = {**values, "cache": {**values["cache"], "bogus": 1}}
        cases = [
            ("user key", bad_key, None, r"\[cache\]\.bogus: unknown key"),
            ("user section", {**values, "bogus": {"a": 1}}, None, r"\[bogus\]"),
            ("project key", values, {"resources": {"bogus": 1}}, r"\[resources\]\.bogus: unknown key"),
        ]
        for label, user_values, override, pattern in cases:
            with self.subTest(label):
                user = self.write(self.root / "user.toml", user_values)
                if override:
                    self.write(project / ".unbake/unbake.toml", override)
                with self.assertRaisesRegex(Held, pattern):
                    config.load_host(user, project, "check")


class HostRefusalTests(TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.values = host_values(self.root)

    def test_missing_value_names_key_and_command(self) -> None:
        for command, dotted in [("compare", "tools.n64link"), ("check", "tools.make"), ("publish", "publish.branch")]:
            with self.subTest(command=command, key=dotted):
                host = Host.from_values(edited(self.values, dotted), command)
                section, key = dotted.split(".")
                expected = f"unbake.toml [{section}].{key}: missing value (needed by {command})"
                with self.assertRaises(Held) as raised:
                    host.require_command(command)
                self.assertEqual(raised.exception.reason, expected)

    def test_each_kind_refuses_bad_values_with_the_key_label(self) -> None:
        for kind, bad_values in BAD_VALUES.items():
            dotted = SAMPLE_KEY[kind]
            section, key = dotted.split(".")
            for bad in bad_values:
                with self.subTest(kind=kind, value=bad):
                    host = Host.from_values(edited(self.values, dotted, bad), "check")
                    with self.assertRaisesRegex(Held, rf"unbake\.toml \[{section}\]\.{key}: expected"):
                        host.get(dotted)

    def test_executable_refusals(self) -> None:
        plain = self.root / "plain"
        plain.write_text("not executable")
        plain.chmod(0o644)
        cases = [
            ("relative", "bin/make", "expected absolute path"),
            ("absent", str(self.root / "nope"), "missing executable"),
            ("not executable", str(plain), f"missing executable {plain}"),
            ("directory", str(self.root), "missing executable"),
        ]
        for label, value, message in cases:
            with self.subTest(label):
                host = Host.from_values(edited(self.values, "tools.make", value), "check")
                with self.assertRaisesRegex(Held, rf"unbake\.toml \[tools\]\.make: {message}"):
                    host.get("tools.make")

    def test_valid_values_read_back_typed(self) -> None:
        host = Host.from_values(self.values, "check")
        self.assertEqual((host.cores, host.cache_max_bytes), (4, 1_000))
        self.assertEqual(host.same_game_similarity, 0.5)
        self.assertEqual(host.permuter_sha256, "b" * 64)
        self.assertEqual(host.tool_path, (self.root / "bin",))

    def test_cross_key_rules(self) -> None:
        cache = ["cache.max_bytes", "cache.trim_to_bytes"]
        memory = ["resources.memory_total_bytes", "resources.memory_parent_bytes", "resources.memory_worker_bytes"]
        cases = [
            ("trim equals max", {"cache.trim_to_bytes": 1_000}, cache, "trim_to_bytes"),
            ("trim above max", {"cache.trim_to_bytes": 2_000}, cache, "trim_to_bytes"),
            ("parent equals total", {"resources.memory_parent_bytes": 8_000}, memory, "memory_parent_bytes"),
            ("worker over remainder", {"resources.memory_worker_bytes": 7_001}, memory, "memory_worker_bytes"),
        ]
        for label, changes, keys, name in cases:
            with self.subTest(label):
                values = self.values
                for dotted, value in changes.items():
                    values = edited(values, dotted, value)
                with self.assertRaisesRegex(Held, name):
                    Host.from_values(values, "compare").require(keys)
        with self.subTest("worker exactly the remainder is accepted"):
            values = edited(self.values, "resources.memory_worker_bytes", 7_000)
            Host.from_values(values, "compare").require(memory)

    def test_the_retired_push_keys_are_unknown(self) -> None:
        for key in ("remote", "credential_env", "credential_helper"):
            with self.subTest(key), self.assertRaisesRegex(Held, rf"\[publish\]\.{key}: unknown key"):
                Host.from_values({"publish": {key: "x"}}, "publish")

    def test_from_values_refuses_unknown_names(self) -> None:
        with self.assertRaisesRegex(Held, r"\[cache\]\.bogus: unknown key"):
            Host.from_values({"cache": {"bogus": 1}}, "check")


class ProjectConfigTests(TempCase):
    BASE = 'schema = 1\n[project]\nid = "x"\nstate = "ready"\nlayout_cap = 2\n'

    def load(self, text: str) -> None:
        (self.root / "config.toml").write_text(text)
        config.load(self.root)

    def test_retired_sections_are_refused(self) -> None:
        for section in ("paths", "workspace"):
            with self.subTest(section):
                path = self.root / "config.toml"
                with self.assertRaises(Held) as raised:
                    self.load(self.BASE + f'[{section}]\nroms = "roms"\n')
                self.assertEqual(raised.exception.reason, f"{path} [{section}]: retired section; remove it")

    def test_unknown_build_key_is_refused(self) -> None:
        with self.assertRaisesRegex(Held, r"\[build\]\.cpp: unknown key"):
            self.load(self.BASE + '[build]\ncpp = "cpp"\n')

    def test_fixed_layout_has_one_include_root(self) -> None:
        layout = config.Layout(self.root)
        self.assertEqual(layout.include, (self.root / "include",))
        self.assertEqual(layout.work, self.root / "build" / "work")


class BudgetConfigTests(TempCase):
    def test_budgets_are_host_keys_refused_by_name_and_never_project_facts(self) -> None:
        values = host_values(self.root)
        del values["budgets"]["facts_miss_fraction"]
        for command in ("recompute", "cycle", "check"):
            with self.subTest(command), self.assertRaisesRegex(Held, r"\[budgets\]\.facts_miss_fraction: missing"):
                config.Host.from_values(values, command).require_command(command)
        self.assertNotIn("budgets", config.CONFIG_SECTIONS)
        config.Host.from_values(values, "next").require_command("next")  # a command that runs no steps
