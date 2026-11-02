"""Rules of the ui/ folder that Python can hold without Node: installs stay pinned, the build is not committed, and the
page the server sends adds nothing of its own. The UI's own tests are `npm run check` in ui/ (docs/ui.md)."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "ui"


def test_every_package_is_pinned_exactly_and_install_scripts_are_off():
    pkg = json.loads((UI / "package.json").read_text(encoding="utf-8"))
    deps = {**pkg["dependencies"], **pkg["devDependencies"]}
    assert deps and all(re.fullmatch(r"\d+\.\d+\.\d+", v) for v in deps.values()), deps
    npmrc = (UI / ".npmrc").read_text(encoding="utf-8")
    assert "ignore-scripts=true" in npmrc and "save-exact=true" in npmrc
    lock = json.loads((UI / "package-lock.json").read_text(encoding="utf-8"))
    scripted = {k for k, v in lock["packages"].items() if v.get("hasInstallScript")}
    assert scripted == {"node_modules/fsevents"}  # macOS only, read, and never run: ignore-scripts is on


def test_the_lockfile_fetches_from_the_npm_registry_only():
    lock = json.loads((UI / "package-lock.json").read_text(encoding="utf-8"))
    urls = {v["resolved"].split("/")[2] for v in lock["packages"].values() if v.get("resolved")}
    assert urls == {"registry.npmjs.org"}


def test_the_source_page_loads_nothing_from_outside():
    html = (UI / "index.html").read_text(encoding="utf-8")
    assert "http://" not in html and "https://" not in html and "__CHH_TOKEN__" in html


def test_the_types_are_generated_from_the_current_schema():
    gen = (UI / "src" / "api" / "types.gen.ts").read_text(encoding="utf-8")
    doc = json.loads((ROOT / "docs" / "experience_api.schema.json").read_text(encoding="utf-8"))
    for model in doc:
        assert f"export interface {model} {{" in gen, model


def test_the_build_and_the_packages_are_not_committed():
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "dist/" in ignore and "ui/node_modules/" in ignore
