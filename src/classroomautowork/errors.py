class WorkflowError(Exception):
    """An actionable error whose message is safe to display."""


class ConfigurationError(WorkflowError):
    pass


class PermissionDenied(WorkflowError):
    pass


class NeedsInput(WorkflowError):
    """Recoverable missing questions or user facts, rather than a processing crash."""


class FormsUnread(NeedsInput):
    """The assignment's own Google Form was not read (no school login, an expired one, another
    account, or a read error). The item stops before any model call: without the questions an
    answer can only be empty, and the turn would be billed for nothing."""


class MissingSources(NeedsInput):
    """The model answered nothing and named what it lacked. The automatic repair is not spent:
    the same inputs would give the same empty answer. The student supplies what is missing."""


class RunCancelled(WorkflowError):
    """Stop at a safe processing boundary while preserving completed work."""


class RequestFailed(WorkflowError):
    """The model service refused or could not serve the request itself (a limit, the quota, the
    size, the server or the network). Repeating it at once would fail the same way, so the one
    automatic repair is not spent on it; the user retries when the cause is gone."""


class Busy(WorkflowError):
    """The same kind of work is already running (a connection test); the page waits and retries.
    The HTTP layer answers it with 409."""
