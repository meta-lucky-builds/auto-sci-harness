"""Research loop (PLAN L4). Importing this package registers the research.* modules."""

from . import (  # noqa: F401  (registration side effect)
    claims,
    critic_answers,
    memory_wiring,
    modules,
    physical,
    recall,
    verifier_synthesis,
)
from .task import TaskSpec, load_task

__all__ = ["TaskSpec", "load_task"]
