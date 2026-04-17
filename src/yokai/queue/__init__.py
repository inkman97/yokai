"""Public API of the yokai.queue subsystem."""

from yokai.queue.agent import (
    AgentExecution,
    AgentRunner,
    CheckoutInfo,
    CommitPushResult,
    RepoCheckout,
)
from yokai.queue.backoff import exponential_backoff, no_backoff
from yokai.queue.coordinator import (
    Coordinator,
    CoordinatorSettings,
    CycleStats,
)
from yokai.queue.exceptions import (
    DuplicateJobError,
    InvalidStateTransition,
    JobNotFound,
    LeaseExpiredError,
    QueueBackendError,
    QueueError,
    WorkerNotFound,
)
from yokai.queue.interfaces import (
    CoordinatorLock,
    JobQueue,
    ResultStore,
    WorkerRegistry,
)
from yokai.queue.models import (
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    Job,
    JobResult,
    JobStatus,
    WorkerInfo,
)
from yokai.queue.postprocessor import PostprocessOutcome, Postprocessor
from yokai.queue.result_handler import (
    HandlerCycleStats,
    ResultHandler,
    ResultHandlerSettings,
)
from yokai.queue.sources import (
    ChainRouter,
    ComponentMapRouter,
    IssueSource,
    LabelPrefixRouter,
    StoryRouter,
    StorySnapshot,
)
from yokai.queue.state_machine import (
    can_be_picked_up,
    is_allowed,
    is_terminal,
    needs_recovery,
    transition,
)
from yokai.queue.worker import Worker, WorkerSettings, WorkerStats

__all__ = [
    # exceptions
    "QueueError",
    "InvalidStateTransition",
    "JobNotFound",
    "DuplicateJobError",
    "QueueBackendError",
    "WorkerNotFound",
    "LeaseExpiredError",
    # interfaces
    "JobQueue",
    "ResultStore",
    "WorkerRegistry",
    "CoordinatorLock",
    # models
    "Job",
    "JobResult",
    "JobStatus",
    "WorkerInfo",
    "ALLOWED_TRANSITIONS",
    "TERMINAL_STATES",
    # state machine
    "transition",
    "is_allowed",
    "is_terminal",
    "can_be_picked_up",
    "needs_recovery",
    # coordinator
    "Coordinator",
    "CoordinatorSettings",
    "CycleStats",
    # worker
    "Worker",
    "WorkerSettings",
    "WorkerStats",
    # agent
    "AgentRunner",
    "AgentExecution",
    "RepoCheckout",
    "CheckoutInfo",
    # postprocessing
    "Postprocessor",
    "PostprocessOutcome",
    "ResultHandler",
    "ResultHandlerSettings",
    "HandlerCycleStats",
    # backoff
    "exponential_backoff",
    "no_backoff",
    # sources / routers
    "IssueSource",
    "StoryRouter",
    "StorySnapshot",
    "ComponentMapRouter",
    "LabelPrefixRouter",
    "ChainRouter",
]
