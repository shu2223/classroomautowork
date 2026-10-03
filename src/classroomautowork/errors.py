class WorkflowError(Exception):
    """An actionable error whose message is safe to display."""


class ConfigurationError(WorkflowError):
    pass


class PermissionDenied(WorkflowError):
    pass


class RunCancelled(WorkflowError):
    """Stop at a safe processing boundary while preserving completed work."""
