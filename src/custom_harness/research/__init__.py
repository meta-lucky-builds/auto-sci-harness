"""Research loop (PLAN L4). Importing this package registers the research.* modules."""

from . import modules  # noqa: F401  (registration side effect)
from .task import TaskSpec, load_task

__all__ = ["TaskSpec", "load_task"]
