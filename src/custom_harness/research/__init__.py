"""Research loop (PLAN L4). Importing this package registers the research.* modules."""

from . import (  # noqa: F401  (registration side effect)
    claims,
    critic_answers,
    front,
    memory_wiring,
    modules,
    physical,
    recall,
)
from .task import TaskSpec, load_task

__all__ = ["TaskSpec", "load_task"]
