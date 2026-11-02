"""Audit fixture arm that crashes (COULD NOT RUN)."""


def build_model(seed):
    raise RuntimeError("this arm does not run")
