"""unbake rename OLD NEW: one symbol gets a new name everywhere it is spelled, in one commit."""
from __future__ import annotations

from unbake import build, effort, journal, layout, process, store, symbols
from unbake import config as configuration
from unbake.contracts import Config, Finding, Json, Plan, Refusal, digest

_SPELLED = ("src", "include", "types.toml")  # tracked places that name symbols
def run(config: Config, params: Json) -> Json:
    with effort.stage("rename.run"), store.exclusive(config, "land") as held:
        if not held:
            raise Refusal(Finding("setup.busy", configuration.sentence("setup.busy")))
        journal.recover(config)
        old, new, project = params["old"], params["new"], config.project
        snapshot = layout.capture(config)
        table = symbols.edit(snapshot)
        for vid, record in snapshot.versions.items():  # a generated name (D_auto_...) is a fact of its version
            if old in record.symbols:
                kind = getattr(snapshot.layout.members.get(old), "kind", "data")
                table.setdefault(old, {"kind": "function" if kind == "function" else "data"})[vid] = record.symbols[old]
        symbols.rename(table, old, new)
        writes = symbols.files(table, {v: vf.symbols for v, vf in project.version_files.items()})
        tracked = process.git(config, "ls-files", "-z", "--", *_SPELLED).stdout.decode().split("\0")
        for path in filter(None, tracked):
            try:
                text = snapshot.read(path).decode()
            except UnicodeDecodeError:
                continue
            if (changed := symbols.rewrite(text, old, new)) != text:
                writes[path] = changed.encode()
        renamed = layout.overlay(snapshot, writes)
        writes.update({f"versions/{v}/symbols.ld": build.symbols_ld(renamed, v) for v in project.versions})
        message = f"rename: {old} -> {new}"
        commit = journal.apply(config, Plan("rename", snapshot.digest, writes, (), (), (), message,
                                            digest((snapshot.digest, writes, message))), snapshot.commit)
        return {"old": old, "new": new, "commit": commit, "files": sorted(writes)}
