"""Business-neutral model task runtime."""

from .constants import VERSION
from .contracts import ExecutionRequest, ResultEnvelope, TaskDefinition
from .engine import ModelProcessingEngine
from .factory import build_default_engine
from .task_loader import load_task_pack

__all__ = [
    "ExecutionRequest",
    "ModelProcessingEngine",
    "ResultEnvelope",
    "TaskDefinition",
    "build_default_engine",
    "load_task_pack",
]

__version__ = VERSION
