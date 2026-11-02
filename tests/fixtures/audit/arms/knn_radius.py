"""Audit fixture arm (method): k-nearest neighbours on the radius sqrt(x1^2 + x2^2)."""
import math


class RadiusKNN:
    def __init__(self, k):
        self.k = k

    def fit(self, X, y):
        self.pts = sorted((math.hypot(r[0], r[1]), lab) for r, lab in zip(X, y, strict=True))
        return self

    def predict(self, X):
        out = []
        for r in X:
            x = math.hypot(r[0], r[1])
            near = sorted(self.pts, key=lambda t: (abs(t[0] - x), t[1]))[: self.k]
            votes = {}
            for _, lab in near:
                votes[lab] = votes.get(lab, 0) + 1
            out.append(max(sorted(votes), key=votes.get))
        return out


def build_model(seed):
    return RadiusKNN(15)
