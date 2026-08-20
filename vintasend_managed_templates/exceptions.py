class ManagedTemplateError(Exception):
    """Base class for all ManagedTemplate related exceptions."""

    pass


class ManagedTemplateNotFoundError(ManagedTemplateError):
    """Raised when a ManagedTemplate is not found in the backend."""

    pass


class ManagedTemplateInvalidFilterError(ManagedTemplateError):
    """Raised when an invalid filter is used to filter ManagedTemplates."""

    pass


class ManagedTemplateChangeUserNotFoundError(ManagedTemplateError):
    """Raised when an update is made by an invalid changed_by user."""

    pass


class ManagedTemplateStatusTransitionError(ManagedTemplateError):
    """Raised when a status change is not allowed from the version's current status."""

    pass
