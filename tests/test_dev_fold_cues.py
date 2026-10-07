"""Dev folds must not be distinguishable from held-out runs by cheap file-format cues (STATUS §D: "a program can still
tell dev folds from held-out runs by data size or content").

Fixed here: the fold files are written in the byte conventions of the files a held-out run receives (line terminator,
final newline, BOM): fold training files like the public training file, fold validation files like the public test
features file. Before, folds were always LF while e.g. the rings task's files are CRLF: one `b"\\r\\n" in data` test
told a program which kind of run it was in.

NOT fixable without changing the estimator (documented in research/modules.py `_dev_predict`): row counts. A fold
trains on (k-1)/k of the public training rows and predicts 1/k of them, so len(test)/len(train) = 1/(k-1) on dev
versus n_test/n_train on held-out data, and the training size differs from the n_rows in the data profile."""

import pytest

import custom_harness.research  # noqa: F401
from custom_harness.records import RunStatus
from custom_harness.research.data import cv_folds
from custom_harness.runtime import Engine, HarnessSpec, ModuleNode
from custom_harness.sandbox import LocalSandbox
from test_research import rings_task  # noqa: F401


def _style(raw: bytes) -> tuple[bool, bool, bool]:
    return raw.startswith(b"\xef\xbb\xbf"), b"\r\n" in raw, raw.endswith(b"\n")


def test_dev_fold_files_use_the_held_out_file_conventions(store, tmp_path, rings_task):  # noqa: F811
    spec = HarnessSpec(id="setup-only", entry="setup", nodes={"setup": ModuleNode(module="research.setup")})
    res = Engine(spec, backends={}, store=store,
                 sandbox=LocalSandbox(tmp_path / "ws", i_understand_no_isolation=True)).run(
        "rings", {"task_dir": str(rings_task)})
    assert res.status == RunStatus.COMPLETED, res.detail
    train = (rings_task / "public" / "train.csv").read_bytes()
    test_x = (rings_task / "public" / "test_X.csv").read_bytes()
    assert _style(train)[1], "fixture precondition: the rings task files are CRLF"
    for fold in res.state["dev_folds"]:
        assert _style(store.blobs.get(fold["train_ref"])) == _style(train)
        assert _style(store.blobs.get(fold["valid_x_ref"])) == _style(test_x)


@pytest.mark.parametrize("term,final,bom", [(b"\n", True, False), (b"\r\n", False, False), (b"\n", True, True)])
def test_cv_folds_mirror_each_files_conventions(tmp_path, term, final, bom):
    rows = [[str(i), str(i % 2)] for i in range(30)]
    body = term.join(b",".join(c.encode() for c in r) for r in [["x", "y"], *rows]) + (term if final else b"")
    (tmp_path / "train.csv").write_bytes((b"\xef\xbb\xbf" if bom else b"") + body)
    other = b"x" + b"\r\n" + b"\r\n".join(str(i).encode() for i in range(5)) + b"\r\n"  # test file: CRLF, final NL
    (tmp_path / "test.csv").write_bytes(other)
    folds = cv_folds(tmp_path / "train.csv", "y", 5, seed=0, stratify=False, test_csv=tmp_path / "test.csv")
    for f in folds:
        assert _style(f["train"]) == (bom, term == b"\r\n", final)
        assert _style(f["valid_x"]) == _style(other)
    # the content itself is unchanged: rows parse back to the same values (BOM stripped, as csv readers do)
    assert sum(len(f["labels"]) for f in folds) == 30
