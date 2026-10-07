# ruff: noqa: E501  (fixture programs are kept as written)
"""Copy detection that survives renaming (STATUS §D: renamed or reformatted copies passed both the near-copy refusal
for replicas and the verbatim-line check on the replication brief).

The comparison is over AST-normalised code: local names (variables, functions, parameters) become one placeholder,
import aliases are resolved to the imported name, literals become their type, docstrings and formatting vanish;
library API names, attributes and keyword names are kept. Statements and token shingles that also occur in the
task's baseline (shared boilerplate such as the CLI contract) are ignored. Positive controls: genuinely independent
implementations of the same idea stay below the refusal threshold."""

import ast
import json
import sys
from pathlib import Path

import pytest

from custom_harness.research.claims import (
    MAX_STRUCTURAL_SIMILARITY,
    code_similarity,
    copied_code_lines,
    structural_similarity,
)
from test_research import (  # noqa: F401
    KNN_RADIUS,
    KNN_REPLICA,
    rings_task,
    scripted_llm,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "tasks"))
from make_rings_task import BASELINE as RINGS_BASE

RINGS_RENAMED = '''
import argparse as ap, csv as c, math as m
parser_obj = ap.ArgumentParser()
for flag in ("--train", "--test", "--out"): parser_obj.add_argument(flag)
parser_obj.add_argument("--seed", type=int)
opts = parser_obj.parse_args()
def read_file(fname):
    with open(fname, newline='') as fh:
        rr = list(c.reader(fh))
    return rr[0], rr[1:]
hdr, data_rows = read_file(opts.train); thdr, test_rows = read_file(opts.test)
radial = lambda row, cols: m.hypot(float(row[cols.index('x1')]), float(row[cols.index('x2')]))
points = sorted((radial(row, hdr), row[hdr.index('label')]) for row in data_rows)
def neighbours_vote(val, kk=15):
    """vote"""
    close = sorted(points, key=lambda pair: abs(pair[0] - val))[:kk]
    tally = {}
    for _, lab in close: tally[lab] = tally.get(lab, 0) + 1
    return max(sorted(tally), key=tally.get)
with open(opts.out, 'w', newline='') as fh:
    writer = c.writer(fh); writer.writerow(['prediction'])
    for row in test_rows: writer.writerow([neighbours_vote(radial(row, thdr))])
'''

SK_ORIG = '''import argparse
import pandas as pd
from sklearn.neighbors import KNeighborsClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline


def load(path, target=None):
    df = pd.read_csv(path)
    if target is None:
        return df, None
    return df.drop(columns=[target]), df[target]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--train", required=True)
    parser.add_argument("--test", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    X, y = load(args.train, "target")
    test, _ = load(args.test)
    best_k, best_score = 1, -1.0
    for k in (3, 5, 7, 9, 11):
        model = make_pipeline(StandardScaler(), KNeighborsClassifier(n_neighbors=k, weights="distance"))
        score = cross_val_score(model, X, y, cv=5).mean()
        if score > best_score:
            best_k, best_score = k, score
    model = make_pipeline(StandardScaler(), KNeighborsClassifier(n_neighbors=best_k, weights="distance"))
    model.fit(X, y)
    preds = model.predict(test[X.columns])
    pd.DataFrame({"prediction": preds}).to_csv(args.out, index=False)


if __name__ == "__main__":
    main()
'''

SK_REPLICA = '''import argparse

import numpy as np
import pandas as pd
from sklearn.model_selection import GridSearchCV
from sklearn.neighbors import KNeighborsClassifier
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

ap = argparse.ArgumentParser()
ap.add_argument("--train")
ap.add_argument("--test")
ap.add_argument("--out")
ap.add_argument("--seed", type=int, default=0)
a = ap.parse_args()
np.random.seed(a.seed)
train = pd.read_csv(a.train)
features = [c for c in train.columns if c != "target"]
pipe = Pipeline([("scale", StandardScaler()), ("knn", KNeighborsClassifier(weights="distance"))])
search = GridSearchCV(pipe, {"knn__n_neighbors": [3, 5, 7, 9, 11]}, cv=5)
search.fit(train[features].to_numpy(), train["target"].to_numpy())
test = pd.read_csv(a.test)
predicted = search.predict(test[features].to_numpy())
with open(a.out, "w") as fh:
    fh.write("prediction" + chr(10))
    for p in predicted:
        fh.write(str(p) + chr(10))
'''

SK_BASE = '''import argparse
import pandas as pd
from sklearn.neighbors import KNeighborsClassifier

p = argparse.ArgumentParser()
p.add_argument("--train"); p.add_argument("--test"); p.add_argument("--out"); p.add_argument("--seed", type=int, default=0)
a = p.parse_args()
train = pd.read_csv(a.train)
X, y = train.drop(columns=["target"]), train["target"]
model = KNeighborsClassifier(n_neighbors=1).fit(X, y)
pd.DataFrame({"prediction": model.predict(pd.read_csv(a.test)[X.columns])}).to_csv(a.out, index=False)
'''

SK_RENAMED = '''import argparse as cli
import pandas as pandas_lib
from sklearn.neighbors import KNeighborsClassifier as KNN
from sklearn.preprocessing import StandardScaler as Scale
from sklearn.pipeline import make_pipeline as chain

def read_table(location, label_col=None):
    frame = pandas_lib.read_csv(location)
    if label_col is None:
        return (frame, None)
    return (frame.drop(columns=[label_col]), frame[label_col])

def entry():
    """Entry point."""
    p = cli.ArgumentParser()
    p.add_argument('--train', required=True)
    p.add_argument('--test', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--seed', type=int, default=0)
    ns = p.parse_args()
    feats, labels = read_table(ns.train, 'target')
    tst, _ = read_table(ns.test)
    top_k, top = 1, -1.0
    for kk in (3, 5, 7, 9, 11):
        est = chain(Scale(), KNN(n_neighbors=kk, weights='distance'))
        sc = cross_val_score(est, feats, labels, cv=5).mean()
        if sc > top:
            top_k, top = kk, sc
    est = chain(Scale(), KNN(n_neighbors=top_k, weights='distance'))
    est.fit(feats, labels)
    out = est.predict(tst[feats.columns])
    pandas_lib.DataFrame({'prediction': out}).to_csv(ns.out, index=False)

if __name__ == '__main__':
    entry()
'''


def _padded(src: str) -> str:
    """Insert a no-op statement after every statement (defeats line and shingle overlap)."""
    class Pad(ast.NodeTransformer):
        def generic_visit(self, node):
            super().generic_visit(node)
            for f in ("body", "orelse", "finalbody"):
                b = getattr(node, f, None)
                if isinstance(b, list) and b and isinstance(b[0], ast.stmt):
                    setattr(node, f, [x for i, s in enumerate(b)
                                      for x in (s, ast.parse(f"_pad{i} = len(str({i}))").body[0])])
            return node
    return ast.unparse(ast.fix_missing_locations(Pad().visit(ast.parse(src))))


def _reordered(src: str) -> str:
    t = ast.parse(src)
    t.body = sorted(t.body, key=lambda s: 0 if isinstance(s, ast.FunctionDef) else 1)
    return ast.unparse(t)


COPIES = {
    "rings-renamed": (RINGS_RENAMED, KNN_RADIUS, RINGS_BASE),
    "rings-renamed-padded": (_padded(RINGS_RENAMED), KNN_RADIUS, RINGS_BASE),
    "rings-renamed-reordered": (_reordered(RINGS_RENAMED), KNN_RADIUS, RINGS_BASE),
    "sklearn-renamed": (SK_RENAMED, SK_ORIG, SK_BASE),
    "sklearn-renamed-padded": (_padded(SK_RENAMED), SK_ORIG, SK_BASE),
    "sklearn-reformatted": (ast.unparse(ast.parse(SK_ORIG)), SK_ORIG, SK_BASE),
}


@pytest.mark.parametrize("name", list(COPIES))
def test_renamed_or_padded_copies_are_detected(name):
    copy, original, base = COPIES[name]
    assert structural_similarity(copy, original, base) >= MAX_STRUCTURAL_SIMILARITY, name


def test_the_old_line_detector_misses_renamed_copies():
    """Why the structural check exists (documents the gap; the line check itself is kept unchanged)."""
    assert code_similarity(RINGS_RENAMED, KNN_RADIUS) < 0.8 and code_similarity(SK_RENAMED, SK_ORIG) < 0.8


@pytest.mark.parametrize("pair", ["rings", "sklearn"])
def test_independent_reimplementations_are_not_refused(pair):
    replica, original, base = {"rings": (KNN_REPLICA, KNN_RADIUS, RINGS_BASE),
                               "sklearn": (SK_REPLICA, SK_ORIG, SK_BASE)}[pair]
    s = structural_similarity(replica, original, base)
    assert s < 0.5, s  # far below the refusal threshold
    assert structural_similarity(RINGS_BASE, KNN_RADIUS, None) < MAX_STRUCTURAL_SIMILARITY  # different methods


def test_unparseable_code_falls_back_to_the_line_check():
    assert structural_similarity("def broken(:", KNN_RADIUS, RINGS_BASE) == 0.0


def test_renamed_copy_as_replica_is_refused_end_to_end(store, tmp_path, rings_task, monkeypatch):  # noqa: F811
    from custom_harness.gateway import MockBackend
    from custom_harness.research.claims import headline
    from test_research import _run_v1

    def llm(req):
        if "Re-implement the method" in req.messages[-1].content:
            return json.dumps({"code": RINGS_RENAMED, "notes": "renamed copy"})
        return scripted_llm(req)
    s = _run_v1(store, tmp_path, rings_task, MockBackend(llm)).state
    assert s.get("replica_best"), "the replication step was not reached (attack not exercised)"
    assert "replica_certificate" not in s and not headline(s)
    assert s["replica_best"]["error"].startswith("not independent"), s["replica_best"]["error"]


# ---------------------------------------------------------------- replication brief
def test_brief_with_renamed_code_lines_is_refused():
    renamed_lines = ("The idea, in short:\n"
                     "    close = sorted(points, key=lambda pair: abs(pair[0] - val))[:kk]\n"
                     "points = sorted((radial(row, hdr), row[hdr.index('label')])   for row in data_rows)\n"
                     "and the feature is `radial = lambda row, cols: m.hypot(float(row[cols.index('x1')]), "
                     "float(row[cols.index('x2')]))`")
    assert copied_code_lines(KNN_RADIUS, renamed_lines) >= 2


def test_honest_brief_is_not_refused():
    honest = ("On task rings, the solution for hypothesis h_radius beats the task baseline on accuracy by at least "
              "0.1 on held-out data\nkNN on radial feature: compute the radius sqrt(x1^2 + x2^2) of every point and "
              "vote among the 15 nearest training radii (k = 15).")
    assert copied_code_lines(KNN_RADIUS, honest) == 0
    assert copied_code_lines(SK_ORIG, "standardise features, then tune n_neighbors in (3, 5, 7, 9, 11) by 5-fold "
                                      "cross-validation with distance weights") == 0
