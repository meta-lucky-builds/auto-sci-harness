"""The method: nearest neighbours on the radius. The Court's contract: `build_model(seed)` returns an object with
`fit(X, y)` and `predict(X)`. X is a list of rows of numbers, y a list of labels. The trusted Court computes the
score."""
import math


class RadiusNeighbours:
    def __init__(self, k):
        self.k = k

    def fit(self, X, y):
        self.points = sorted((math.hypot(r[0], r[1]), label) for r, label in zip(X, y, strict=True))
        return self

    def predict(self, X):
        out = []
        for row in X:
            radius = math.hypot(row[0], row[1])
            near = sorted(self.points, key=lambda p: (abs(p[0] - radius), p[1]))[:self.k]
            votes = {}
            for _, label in near:
                votes[label] = votes.get(label, 0) + 1
            out.append(max(sorted(votes), key=votes.get))
        return out


def build_model(seed):
    return RadiusNeighbours(15)
