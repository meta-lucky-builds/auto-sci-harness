"""Write data.csv: a small tabular classification task, standard library only, the same file on every machine.

Each row has two measurements and a label. The label sets the distance from the centre, so a model that looks at the
radius does well and one that looks at x1 or x2 alone does not. `n1` is noise, there to be ignored.
"""
import csv
import math
import random
from pathlib import Path

ROWS, SEED = 1600, 5


def main() -> None:
    rng = random.Random(SEED)
    out = Path(__file__).with_name("data.csv")
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(["x1", "x2", "n1", "label"])
        for _ in range(ROWS):
            cls = rng.randrange(3)
            radius, angle = 1.0 + 1.2 * cls + rng.gauss(0, 0.3), rng.uniform(0, 6.28)
            w.writerow([f"{radius * math.cos(angle):.6f}", f"{radius * math.sin(angle):.6f}",
                        f"{rng.gauss(0, 1):.6f}", "abc"[cls]])
    print(f"wrote {out} ({ROWS} rows)")


if __name__ == "__main__":
    main()
