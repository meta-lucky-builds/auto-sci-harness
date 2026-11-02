"""A quick look on your own machine: no Docker, no agent, no model, no Court. Nothing here is evidence."""
import csv
import importlib.util
from pathlib import Path

HERE = Path(__file__).parent


def arm(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.build_model(0)


def accuracy(model, rows):
    train, test = rows[:1200], rows[1200:]
    model.fit([r[:3] for r in train], [r[3] for r in train])
    got = model.predict([r[:3] for r in test])
    return sum(a == r[3] for a, r in zip(got, test, strict=True)) / len(test)


def main():
    with (HERE / "data.csv").open(encoding="utf-8") as f:
        rows = [[float(r["x1"]), float(r["x2"]), float(r["n1"]), r["label"]] for r in csv.DictReader(f)]
    scores = {name: accuracy(arm(name), rows) for name in ("method", "baseline")}
    for name, value in scores.items():
        print(f"{name:9s} accuracy {value:.3f}")
    print("This is a quick look on data you can read. Only the Court can certify a claim.")
    return scores


if __name__ == "__main__":
    main()
