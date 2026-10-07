class ManagedTemplateError(Exception):
    """Base class for all ManagedTemplate related exceptions."""

    pass


class ManagedTemplateNotFoundError(ManagedTemplateError):
    """Raised when a ManagedTemplate is not found in the backend."""

    pass


class ManagedTemplateNoActiveVersionError(ManagedTemplateNotFoundError):
    """Raised when a send resolves a key that has versions, but none of them is ACTIVE.

    A subclass of ``ManagedTemplateNotFoundError`` on purpose: for a send, a key holding only
    drafts (or only retired versions) has nothing published to render, which is the same
    answer as a key with nothing stored. That is what lets a renderer's fallback treat a key
    nobody has published yet as "not customized", and what lets an API keep mapping it to a
    404. Catch this subclass to tell "exists but unpublished" apart from "never created".
    """

    pass


class ManagedTemplateDeletionNotAllowedError(ManagedTemplateError):
    """Raised when deleting a template version that the deletion rule protects.

    Only a version that was never published can be deleted: one still in DRAFT whose status
    history records nothing but DRAFT. Any other version may have rendered a notification that
    is pinned to it, and its status history records who published it. Retire it with
    ``archive`` instead. See ``lifecycle.is_template_version_deletable``.
    """

    pass


class ManagedTemplateInvalidFilterError(ManagedTemplateError):
    """Raised when an invalid filter is used to filter ManagedTemplates."""

    pass


class ManagedTemplateUnsupportedOrderingError(ManagedTemplateError):
    """Raised when an order the configured backend cannot apply is asked for.

    An unsupported *filter* is dropped and the call succeeds, because the caller can see the
    extra rows it gets back. An unsupported *order* is refused instead: ignoring it returns
    exactly the rows that were asked for, in an arbitrary sequence, and nothing downstream can
    tell that apart from a sort that happened. A caller renders those rows under a highlighted
    "sorted by name" column and shows a sort that never ran.

    ``ManagedTemplateService.get_supported_order_by_fields`` is how a caller asks first.
    """

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
