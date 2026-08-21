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


class ManagedTemplateTagNotFoundError(ManagedTemplateError):
    """Raised when a ManagedTemplateTag is not found in the backend."""

    pass


class ManagedTemplateTagAlreadyExistsError(ManagedTemplateError):
    """Raised when creating a tag whose text already slugs to an existing tag."""

    pass


class ManagedTemplateInvalidTagError(ManagedTemplateError):
    """Raised when a tag's text is empty or has nothing that can be slugified."""

    pass


class ManagedTemplateCompositionError(ManagedTemplateError):
    """Base class for every failure of ``composition.TemplateComposer``.

    Composition runs before the template engine does, so nothing here can be caught (or
    reported) by Django or Jinja downstream. Catch this to treat "the template could not be
    assembled" as one condition, or a subclass to tell a typo apart from a missing base.
    """

    pass


class ManagedTemplateCompositionSyntaxError(ManagedTemplateCompositionError):
    """Raised when a ``managed_*`` composition tag is malformed, unknown or unbalanced."""

    pass


class ManagedTemplateCompositionReferenceError(
    ManagedTemplateCompositionError, ManagedTemplateNotFoundError
):
    """Raised when a template extends or includes a template that does not exist.

    Also a ``ManagedTemplateNotFoundError``: the missing thing really is a template, so code
    already handling a missing template keeps working, while code that cares *which* template
    was missing can catch this and read the chain off the message.
    """

    pass


class ManagedTemplateCompositionCycleError(ManagedTemplateCompositionError):
    """Raised when a chain of extends/include references comes back to where it started."""

    pass


class ManagedTemplateCompositionDepthError(ManagedTemplateCompositionError):
    """Raised when a chain of references runs deeper than the composer's ``max_depth``."""

    pass
