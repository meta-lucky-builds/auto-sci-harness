"""Audit fixture arm (planted): k-NN on the 4th column, a row id that tracked the label in the paper's data."""


class IdKNN:
    def fit(self, X, y):
        self.pts = sorted((r[3], lab) for r, lab in zip(X, y, strict=True))
        return self

    def predict(self, X):
        out = []
        for r in X:
            near = sorted(self.pts, key=lambda t: (abs(t[0] - r[3]), t[1]))[:5]
            votes = {}
            for _, lab in near:
                votes[lab] = votes.get(lab, 0) + 1
            out.append(max(sorted(votes), key=votes.get))
        return out


def build_model(seed):
    return IdKNN()
