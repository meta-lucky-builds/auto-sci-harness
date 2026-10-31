"""The wider promotion contract (GOAL 18.6, UPGRADES N8): "the program reads inputs and writes outputs; the trusted
Court computes the metric". It sits next to the `build_model(seed)` wrapper (audit/wrapper.py).

A Lab program is one or more text files with one entry file. It runs with:
    $CHH_INPUTS/train.csv    the training rows, with a `label` column
    $CHH_INPUTS/test.csv     the test rows, without one
    $CHH_OUTPUTS             where it writes predictions.csv: header `prediction`, one row per test row
    $CHH_SEED                the run's seed
Nothing else about the program is fixed: it may be a pipeline, a simulation or a rule set. The metric is computed by
kerneld on the predictions; a number the program reports means nothing to the Court.

What is promoted is one Python program, built by `compose`: the files as plain string literals (never encoded, so the
Kernel's checks of the source still read them), then SHIM, byte for byte the same for both arms. Its hash is the
claim's `wrapper_sha256`. The composed program goes through the Kernel's existing command contract, so K3 re-executes
it like any arm.

Limits: the Court is offline, so a program that needs a network or a model API at run time fails as COULD NOT RUN.
Text files only. Packages come from the Court's candidate image.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

FORMAT = "chh-program/1"
PREDICTIONS = "predictions.csv"

SHIM = '''

# ---- chh-program contract shim (identical for both arms) ----
def _chh_program_main():
    import argparse as _ap
    import os as _os
    import runpy as _runpy
    import shutil as _sh
    import sys as _sys
    p = _ap.ArgumentParser()
    for k in ("--train", "--test", "--out"):
        p.add_argument(k, required=True)
    p.add_argument("--seed", type=int, required=True)
    a = p.parse_args()
    out = _os.path.abspath(a.out)
    base = _os.path.abspath("chh")
    inputs, outputs, prog = [_os.path.join(base, d) for d in ("inputs", "outputs", "program")]
    for d in (inputs, outputs, prog):
        _os.makedirs(d)
    _sh.copyfile(a.train, _os.path.join(inputs, "train.csv"))
    _sh.copyfile(a.test, _os.path.join(inputs, "test.csv"))
    for name, text in _CHH_FILES.items():
        path = _os.path.join(prog, *name.split("/"))
        _os.makedirs(_os.path.dirname(path), exist_ok=True)
        with open(path, "w", newline="", encoding="utf-8") as f:
            f.write(text)
    _os.environ.update(CHH_INPUTS=inputs, CHH_OUTPUTS=outputs, CHH_SEED=str(a.seed))
    _os.chdir(prog)
    _sys.path.insert(0, prog)
    _sys.argv = [_CHH_ENTRY]
    _runpy.run_path(_CHH_ENTRY, run_name="__main__")
    made = _os.path.join(outputs, "predictions.csv")
    if not _os.path.isfile(made):
        raise SystemExit("the program wrote no predictions.csv in $CHH_OUTPUTS")
    _sh.copyfile(made, out)


if __name__ == "__main__":
    _chh_program_main()
'''


class ProgramError(ValueError):
    pass


def shim_hash() -> str:
    return hashlib.sha256(SHIM.encode("utf-8")).hexdigest()


def shim_spec_ref(store: Any, harness_id: str) -> str:
    """The `harness_hash` of a program arm run (as audit/wrapper.wrapper_spec_ref): a stored spec, so bundles build."""
    return str(store.blobs.put_json({"format": "chh-arm-wrapper/1", "harness_id": harness_id,
                                     "wrapper_sha256": shim_hash(), "wrapper_source": SHIM}))


def bundle(entry: str, files: dict[str, bytes]) -> bytes:
    """The frozen form of a program: canonical JSON of its entry and text files."""
    if entry not in files:
        raise ProgramError(f"the entry file {entry!r} is not among the program's files")
    try:
        text = {k: v.decode("utf-8") for k, v in sorted(files.items())}
    except UnicodeDecodeError:
        raise ProgramError("a program file is not UTF-8 text") from None
    return json.dumps({"format": FORMAT, "entry": entry, "files": text}, sort_keys=True).encode("utf-8")


def compose(frozen: bytes) -> bytes:
    """The one Python program an arm runs (module docstring). `repr` keeps every file readable in the source."""
    doc = json.loads(frozen)
    if doc.get("format") != FORMAT:
        raise ProgramError("not a frozen program")
    lines = ["_CHH_FILES = {"]
    lines += [f"    {name!r}: {text!r}," for name, text in sorted(doc["files"].items())]
    lines += ["}", f"_CHH_ENTRY = {doc['entry']!r}"]
    return ("\n".join(lines) + "\n" + SHIM).encode("utf-8")
