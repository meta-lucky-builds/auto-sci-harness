"""The baseline: always predict the most common training label. Beat something honest, not a straw man."""
from collections import Counter


class MostCommon:
    def fit(self, X, y):
        self.label = sorted(Counter(y).items(), key=lambda t: (-t[1], t[0]))[0][0]
        return self

    def predict(self, X):
        return [self.label for _ in X]


def build_model(seed):
    return MostCommon()
