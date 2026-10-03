class WorkflowError(Exception):
    """An actionable error whose message is safe to display."""


class ConfigurationError(WorkflowError):
    pass


class PermissionDenied(WorkflowError):
    pass
