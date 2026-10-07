"""BUNDLED-DATA (STATUS red-team ledger): the sandbox image ships scikit-learn's datasets (and test fixtures of other
packages holding the same data), so agent code could load the complete labelled real-task datasets, held-out rows
included, with no network. Agent code runs in a candidate image with every bundled dataset removed; only trusted task
building (the `build-suite --docker` exporter) uses the full image; a missing candidate image is a refusal, never a
fallback to the full image."""

import ast
import json
import re
import shutil
from pathlib import Path

import pytest

import custom_harness.sandbox.docker as D
from custom_harness.research import suite as S
from custom_harness.sandbox.docker import DockerSandbox, docker_available

ROOT = Path(__file__).resolve().parents[1]
SCANNER = Path(__file__).parent / "fixtures" / "scan_bundled_data.py"
LOADERS = [v[0] for v in S.REAL.values()]
needs_docker = pytest.mark.skipif(not docker_available(), reason="Docker daemon not running")

# Files the scanner flags by type that a candidate image may keep: none is a dataset of examples.
CANDIDATE_ALLOWLIST = {
    # stdlib IDLE GUI icons and lib2to3 parser grammar tables (pickled grammar, not data)
    **{f"/usr/local/lib/python3.12/idlelib/Icons/{n}": "IDLE GUI icon" for n in (
        "folder.gif", "idle_16.gif", "idle_16.png", "idle_256.png", "idle_32.gif", "idle_32.png", "idle_48.gif",
        "idle_48.png", "minusnode.gif", "openfolder.gif", "plusnode.gif", "python.gif", "tk.gif")},
    "/usr/local/lib/python3.12/lib2to3/Grammar3.12.15.final.0.pickle": "lib2to3 parser grammar table",
    "/usr/local/lib/python3.12/lib2to3/PatternGrammar3.12.15.final.0.pickle": "lib2to3 parser grammar table",
    "/usr/local/lib/python3.12/site-packages/dateutil/zoneinfo/dateutil-zoneinfo.tar.gz":
        "IANA time-zone database used by dateutil.tz (pandas dependency)",
    "/usr/local/lib/python3.12/site-packages/networkx/generators/atlas.dat.gz":
        "networkx graph atlas: the enumeration of all graphs up to 7 nodes (mathematical constant, no examples)",
    "/usr/local/lib/python3.12/site-packages/scipy/stats/_sobol_direction_numbers.npz":
        "Sobol' sequence direction numbers required by scipy.stats.qmc (algorithm constants)",
    **{f"/usr/local/lib/python3.12/site-packages/sympy/utilities/mathml/data/{n}": "sympy MathML XSLT stylesheet"
       for n in ("mmlctop.xsl", "mmltex.xsl", "simple_mmlctop.xsl")},
    "/usr/local/lib/python3.12/site-packages/sklearn/datasets/meson.build": "build description file (text)",
    "/usr/local/lib/python3.12/site-packages/torch-2.14.1+cpu.dist-info/licenses/third_party/fbgemm/fbgemm_gpu/test/"
    "quantize/mx/LICENSE": "a third-party licence text (flagged only because its directory is named `test`)",
    "/usr/share/pixmaps/debian-logo.png": "Debian base-image logo",
    "/var/lib/dpkg/alternatives/builtins.7.gz": "dpkg alternatives entry for a man page",
    "/var/log/apt/eipp.log.xz": "apt install log of the base image",
}


def _scan(image: str, tmp_path: Path, allow: dict[str, str] | None = None) -> dict:
    ws = tmp_path / "scan"
    ws.mkdir(parents=True)
    shutil.copy(SCANNER, ws / "scan.py")
    sb = DockerSandbox(ws, image=image, trusted_task_building=image == D.FULL_IMAGE)
    res = sb.exec(["python", "scan.py", json.dumps(sorted(allow or {}))], timeout_s=900)
    assert res.returncode == 0, res.stderr[-2000:]
    return json.loads(res.stdout)


# ---------------------------------------------------------------- (a) real Docker: the loaders
@needs_docker
@pytest.mark.parametrize("loader", LOADERS)
def test_candidate_sandbox_cannot_load_a_bundled_real_dataset(tmp_path, loader):
    sb = DockerSandbox(tmp_path / "ws")  # what every agent-code path constructs
    assert sb.image == D.CANDIDATE_IMAGE
    res = sb.exec(["python", "-c", f"from sklearn.datasets import {loader}; {loader}(); print('LOADED')"],
                  timeout_s=300)
    assert res.returncode != 0 and "LOADED" not in res.stdout, (res.returncode, res.stdout[-300:])
    ok = sb.exec(["python", "-c", "import sklearn.datasets, sklearn.ensemble, sklearn.model_selection, pandas, "
                                  "scipy.stats, numpy, torch; print('OK')"], timeout_s=300)
    assert ok.returncode == 0 and ok.stdout.strip() == "OK", ok.stderr[-1500:]  # the libraries still work


@needs_docker
def test_exporter_image_still_loads_every_real_dataset(tmp_path):
    sb = DockerSandbox(tmp_path / "ws", image=D.FULL_IMAGE, trusted_task_building=True)
    code = "from sklearn import datasets; " + "; ".join(f"datasets.{f}()" for f in LOADERS) + "; print('LOADED')"
    res = sb.exec(["python", "-c", code], timeout_s=300)
    assert res.returncode == 0 and res.stdout.strip() == "LOADED", res.stderr[-1500:]


# ---------------------------------------------------------------- (b) every agent-code path selects the candidate
def _docker_sandbox_calls() -> list[tuple[str, str, ast.Call]]:
    """Every `DockerSandbox(...)` call in src/ and scripts/, with its outermost enclosing top-level function."""
    out = []
    for f in [*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").rglob("*.py")]:
        rel = str(f.relative_to(ROOT)).replace("\\", "/")
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for top in tree.body:
            name = getattr(top, "name", "<module>")
            for node in ast.walk(top):
                if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == \
                        "DockerSandbox":
                    out.append((rel, name, node))
    return out


def test_every_docker_sandbox_construction_selects_the_candidate_image_except_the_exporter():
    calls = _docker_sandbox_calls()
    assert len(calls) >= 8, calls  # run, resume, evolve, reproduce/finalize factory, rerun, build-suite, validate...
    full = []
    for path, fn, call in calls:
        kw = {k.arg: k.value for k in call.keywords}
        if "image" not in kw:
            assert "trusted_task_building" not in kw, (path, fn)
            continue  # default = the candidate image
        name = getattr(kw["image"], "id", getattr(kw["image"], "attr", None))
        assert name == "FULL_IMAGE", f"{path}:{call.lineno} ({fn}) selects image {ast.unparse(kw['image'])}"
        assert isinstance(kw.get("trusted_task_building"), ast.Constant) and kw["trusted_task_building"].value is True
        full.append((path, fn))
    assert full == [("src/custom_harness/cli.py", "cmd_build_suite")]  # the trusted exporter, and nothing else


def test_image_policy_defaults_to_the_candidate_and_refuses_the_full_image_for_agent_code(tmp_path):
    assert D.DEFAULT_IMAGE == D.CANDIDATE_IMAGE != D.FULL_IMAGE
    assert D.select_image(None) == D.CANDIDATE_IMAGE
    assert D.select_image(D.FULL_IMAGE, trusted_task_building=True) == D.FULL_IMAGE
    with pytest.raises(PermissionError, match="bundled"):
        D.select_image(D.FULL_IMAGE)
    with pytest.raises(PermissionError, match="trusted"):
        D.select_image(D.CANDIDATE_IMAGE, trusted_task_building=True)
    with pytest.raises(PermissionError, match="bundled"):  # refused before Docker is even contacted
        DockerSandbox(tmp_path / "ws", image=D.FULL_IMAGE)


@needs_docker
def test_a_missing_or_mistagged_candidate_image_is_refused_never_replaced_by_the_full_image(tmp_path, monkeypatch):
    monkeypatch.setattr(D, "CANDIDATE_IMAGE", "custom-harness-sandbox:does-not-exist-candidate")
    with pytest.raises(D.CandidateImageMissing, match=re.escape("docker build -f docker/sandbox-candidate.Dockerfile")):
        DockerSandbox(tmp_path / "ws")
    # the full image tagged as the candidate (no "bundled data removed" label) is refused as well
    monkeypatch.setattr(D, "CANDIDATE_IMAGE", D.FULL_IMAGE)
    with pytest.raises(D.CandidateImageMissing, match="label"):
        DockerSandbox(tmp_path / "ws2")


@needs_docker
def test_spawned_sandboxes_keep_the_candidate_image(tmp_path):
    sb = DockerSandbox(tmp_path / "ws")
    assert sb.spawn(tmp_path / "fold0").image == D.CANDIDATE_IMAGE == sb.environment()["image"]


# ---------------------------------------------------------------- (c) scan of the candidate image
@needs_docker
def test_candidate_image_holds_no_bundled_dataset_file(tmp_path):
    full = _scan(D.FULL_IMAGE, tmp_path / "full")
    # positive control: the scanner finds every real dataset (and the type-based copies) in the full image
    assert all(full["content"][k] for k in S.REAL), full["content"]
    assert any(p.endswith("scipy/io/arff/tests/data/iris.arff") for p in full["shape"])
    cand = _scan(D.CANDIDATE_IMAGE, tmp_path / "cand", CANDIDATE_ALLOWLIST)
    assert cand["content"] == {k: [] for k in S.REAL}, cand["content"]
    assert cand["shape"] == [], cand["shape"]


# ---------------------------------------------------------------- generalisation: no copy left by task building
def test_export_real_leaves_no_labelled_copy_behind(tmp_path):
    class FakeExporter:
        python = "python"

        def __init__(self, ws: Path):
            self.ws = ws

        def exec(self, command, timeout_s=0):
            from custom_harness.sandbox.base import ExecResult
            (self.ws / "export.json").write_text(json.dumps({"iris": {"columns": ["a"], "X": [[1.0]], "y": [0]}}))
            return ExecResult(returncode=0, stdout="", stderr="", wall_s=0.0)

    work = tmp_path / "suite" / "_export"
    work.mkdir(parents=True)
    data = S.export_real(FakeExporter, work)
    assert data["iris"]["y"] == [0]
    assert not work.exists() or not any(work.iterdir()), list(work.iterdir())
