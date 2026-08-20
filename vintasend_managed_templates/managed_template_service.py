"""Backend-agnostic service for managing template versions and their statuses.

``ManagedTemplateService`` sits between a host application and the two seams this package
defines: a ``BaseTemplateManagerBackend`` (where templates live) and a
``ManagedTemplateRenderer`` (how a template turns into something an adapter can send). Every
storage call goes through the backend, so the service works unchanged against any
implementation of that ABC.

What the service adds on top of the raw backend:

* **Version resolution.** ``version=None`` consistently means "the latest version of this key"
  across reads, status changes, and rendering, so callers never juggle version numbers unless
  they want a specific one.
* **Status transitions.** Status changes are validated against
  ``ALLOWED_STATUS_TRANSITIONS`` and then written through the backend's audit trail, with
  named helpers (``activate`` / ``deactivate`` / ``archive``) for the common moves.
* **Filter validation.** Filters are checked for shape and field names before they reach the
  backend, so a typo raises ``ManagedTemplateInvalidFilterError`` here instead of silently
  matching nothing (or blowing up) deep inside a backend's query translation.
* **Version-pinned rendering.** ``ManagedTemplateRenderer.render`` resolves a key to whatever
  version the backend hands back. The service instead fetches an explicit version and drives
  the renderer's ``create_template_content`` / ``render_from_template_content`` pair directly,
  which is what makes previewing an unpublished draft possible.

Two deliberate non-policies, both chosen so the service stays a thin orchestration layer:

* A key may have **any number of ACTIVE versions at once**. Activating a version does not touch
  the ones already active; deciding which active version wins at render time is the host's
  call.
* ``changed_by`` is **passed through untouched**, ``None`` included. The service never requires
  attribution on a status change.

There is no AsyncIO twin, matching the seams it composes: ``BaseTemplateManagerBackend`` and
``ManagedTemplateRenderer`` are both synchronous.
"""

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar, Generic

from vintasend.services.notification_template_renderers.base import NotificationSendInput

from .base_template_manager_backend import BaseTemplateManagerBackend
from .constants import ManagedTemplateStatus
from .dataclasses import (
    ManagedTemplate,
    ManagedTemplateCreateInput,
    ManagedTemplateStatusHistory,
    ManagedTemplateUpdateInput,
)
from .exceptions import (
    ManagedTemplateInvalidFilterError,
    ManagedTemplateStatusTransitionError,
)
from .filters import (
    ManagedTemplateFilter,
    ManagedTemplateFilterFields,
    is_field_filter,
)
from .managed_template_renderer import ManagedTemplateRenderer, TemplateContentType


if TYPE_CHECKING:
    from vintasend.services.dataclasses import (
        Notification,
        NotificationContextDict,
        OneOffNotification,
    )


logger = logging.getLogger(__name__)


# Read off the TypedDict rather than hand-listing the names, so adding a field to
# ``ManagedTemplateFilterFields`` does not silently leave filter validation rejecting it.
_KNOWN_FILTER_FIELDS: frozenset[str] = frozenset(ManagedTemplateFilterFields.__annotations__)


class ManagedTemplateService(Generic[TemplateContentType]):
    """
    Manages the lifecycle of managed templates: their versions and their statuses.

    :param template_manager_backend: where templates are stored and versioned.
    :param template_renderer: turns a ``ManagedTemplate`` into rendered send input. The
        service calls its ``create_template_content`` / ``render_from_template_content`` pair
        rather than its ``render``, so the renderer's own backend is never consulted -- it only
        needs to agree with this service's backend on the shape of a template, not on where
        templates live.
    :param validate_status_transitions: when True (the default), a status change must appear in
        ``ALLOWED_STATUS_TRANSITIONS`` for the version's current status. Set False to let any
        status move to any other and leave the ordering entirely to the host.
    """

    template_manager_backend: BaseTemplateManagerBackend
    template_renderer: ManagedTemplateRenderer[TemplateContentType]
    validate_status_transitions: bool

    # Which status a version may move to, keyed by the status it is in now. Overridable on a
    # subclass for hosts with a different lifecycle. ARCHIVED is terminal: an archived version
    # is a historical record, and bringing one back would make its audit trail read as though
    # it had never been retired. Publish a new version instead.
    ALLOWED_STATUS_TRANSITIONS: ClassVar[
        dict[ManagedTemplateStatus, frozenset[ManagedTemplateStatus]]
    ] = {
        ManagedTemplateStatus.DRAFT: frozenset(
            {ManagedTemplateStatus.ACTIVE, ManagedTemplateStatus.ARCHIVED}
        ),
        ManagedTemplateStatus.ACTIVE: frozenset(
            {ManagedTemplateStatus.INACTIVE, ManagedTemplateStatus.ARCHIVED}
        ),
        ManagedTemplateStatus.INACTIVE: frozenset(
            {ManagedTemplateStatus.ACTIVE, ManagedTemplateStatus.ARCHIVED}
        ),
        ManagedTemplateStatus.ARCHIVED: frozenset(),
    }

    def __init__(
        self,
        template_manager_backend: BaseTemplateManagerBackend,
        template_renderer: ManagedTemplateRenderer[TemplateContentType],
        validate_status_transitions: bool = True,
    ) -> None:
        self.template_manager_backend = template_manager_backend
        self.template_renderer = template_renderer
        self.validate_status_transitions = validate_status_transitions

    # ------------------------------------------------------------------
    # Versions
    # ------------------------------------------------------------------

    def create_template(self, input: ManagedTemplateCreateInput) -> ManagedTemplate:  # noqa: A002
        """
        Creates the first version of a new template.

        param input: ManagedTemplateCreateInput
        return: ManagedTemplate
        """
        return self.template_manager_backend.create_template(input)

    def get_template(self, template_key: str, version: int | None = None) -> ManagedTemplate:
        """
        Retrieves one version of a template. ``version=None`` returns the latest version.

        param template_key: str
        param version: int | None
        return: ManagedTemplate
        raises ManagedTemplateNotFoundError: if the key (or that version of it) does not exist.
        """
        return self.template_manager_backend.get_template(template_key, version)

    def update_template(
        self,
        template_key: str,
        input: ManagedTemplateUpdateInput,  # noqa: A002
    ) -> ManagedTemplate:
        """
        Creates a new version of an existing template from the latest one.

        Templates are versioned rather than edited in place, so this never mutates a version
        that has already been published -- the backend copies the latest version forward,
        applies the non-None fields of ``input``, and returns the new version.

        param template_key: str
        param input: ManagedTemplateUpdateInput
        return: ManagedTemplate
        raises ManagedTemplateNotFoundError: if the key does not exist.
        """
        return self.template_manager_backend.update_template(template_key, input)

    def delete_template(self, template_key: str, version: int | None = None) -> None:
        """
        Deletes one version of a template, or its latest version when ``version`` is None.

        param template_key: str
        param version: int | None
        raises ManagedTemplateNotFoundError: if the key (or that version of it) does not exist.
        """
        self.template_manager_backend.delete_template(template_key, version)

    def get_template_versions(self, template_key: str) -> list[ManagedTemplate]:
        """
        Retrieves every version of a template, newest version first.

        param template_key: str
        return: list[ManagedTemplate]
        """
        versions = self.get_filtered_templates({"key": template_key})
        return sorted(versions, key=lambda template: template.version, reverse=True)

    # ------------------------------------------------------------------
    # Statuses
    # ------------------------------------------------------------------

    def set_status(
        self,
        template_key: str,
        status: ManagedTemplateStatus,
        version: int | None = None,
        changed_by: str | None = None,
    ) -> ManagedTemplate:
        """
        Moves one version of a template to ``status`` and records it in the audit trail.

        Setting a version to the status it already holds is a no-op: the template is returned
        unchanged and no history entry is written, so repeating a call does not fill the audit
        trail with entries that record nothing.

        param template_key: str
        param status: ManagedTemplateStatus
        param version: int | None -- the latest version when None.
        param changed_by: str | None -- passed to the backend untouched.
        return: ManagedTemplate -- the version as it stands after the change.
        raises ManagedTemplateNotFoundError: if the key (or that version of it) does not exist.
        raises ManagedTemplateStatusTransitionError: if the move is not allowed from the
            version's current status and ``validate_status_transitions`` is on.
        """
        template = self.get_template(template_key, version)

        if template.status is status:
            logger.debug(
                "Template %s v%s is already %s; skipping status update.",
                template_key,
                template.version,
                status.value,
            )
            return template

        self._check_status_transition(template, status)

        self.template_manager_backend.create_template_status_update(
            template_key=template_key,
            version=template.version,
            status=status,
            changed_by=changed_by,
        )
        # The backend seam returns None from a status update, so re-read to hand back a
        # template whose status reflects the write rather than one captured before it.
        return self.get_template(template_key, template.version)

    def activate(
        self,
        template_key: str,
        version: int | None = None,
        changed_by: str | None = None,
    ) -> ManagedTemplate:
        """
        Publishes one version of a template.

        Other versions of the same key that are already ACTIVE are left alone -- a key may hold
        several active versions at once, and choosing between them is the host's call.

        param template_key: str
        param version: int | None
        param changed_by: str | None
        return: ManagedTemplate
        """
        return self.set_status(template_key, ManagedTemplateStatus.ACTIVE, version, changed_by)

    def deactivate(
        self,
        template_key: str,
        version: int | None = None,
        changed_by: str | None = None,
    ) -> ManagedTemplate:
        """
        Retires one version of a template without archiving it, so it can be activated again.

        param template_key: str
        param version: int | None
        param changed_by: str | None
        return: ManagedTemplate
        """
        return self.set_status(template_key, ManagedTemplateStatus.INACTIVE, version, changed_by)

    def archive(
        self,
        template_key: str,
        version: int | None = None,
        changed_by: str | None = None,
    ) -> ManagedTemplate:
        """
        Archives one version of a template. Terminal under the default transition table.

        param template_key: str
        param version: int | None
        param changed_by: str | None
        return: ManagedTemplate
        """
        return self.set_status(template_key, ManagedTemplateStatus.ARCHIVED, version, changed_by)

    def get_status_history(
        self, template_key: str, version: int | None = None
    ) -> list[ManagedTemplateStatusHistory]:
        """
        Retrieves the status audit trail for a template, most recent change first.

        param template_key: str
        param version: int | None -- every version's history when None, if the backend
            supports it.
        return: list[ManagedTemplateStatusHistory]
        """
        history = self.template_manager_backend.get_template_status_history(template_key, version)
        return sorted(history, key=lambda record: record.created, reverse=True)

    def get_templates_by_status(
        self, status: Iterable[ManagedTemplateStatus]
    ) -> list[ManagedTemplate]:
        """
        Retrieves every template version in any of the given statuses.

        param status: Iterable[ManagedTemplateStatus]
        return: list[ManagedTemplate]
        """
        return list(self.template_manager_backend.get_templates_by_status(status))

    def can_transition_to(self, template: ManagedTemplate, status: ManagedTemplateStatus) -> bool:
        """
        Reports whether ``template`` may move to ``status``, without attempting the move.

        Lets a caller (a UI deciding which buttons to enable, say) ask the same question
        ``set_status`` asks, instead of catching the exception to find out.

        param template: ManagedTemplate
        param status: ManagedTemplateStatus
        return: bool
        """
        if not self.validate_status_transitions or template.status is status:
            return True
        return status in self.ALLOWED_STATUS_TRANSITIONS.get(template.status, frozenset())

    def _check_status_transition(
        self, template: ManagedTemplate, status: ManagedTemplateStatus
    ) -> None:
        if self.can_transition_to(template, status):
            return
        allowed = self.ALLOWED_STATUS_TRANSITIONS.get(template.status, frozenset())
        allowed_names = ", ".join(sorted(member.value for member in allowed)) or "nothing"
        raise ManagedTemplateStatusTransitionError(
            f"Template '{template.key}' v{template.version} cannot move from "
            f"'{template.status.value}' to '{status.value}'. Allowed: {allowed_names}."
        )

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_all_templates(self) -> list[ManagedTemplate]:
        """
        Retrieves every version of every template.

        return: list[ManagedTemplate]
        """
        return list(self.template_manager_backend.get_all_templates())

    def get_filtered_templates(self, filters: ManagedTemplateFilter) -> list[ManagedTemplate]:
        """
        Retrieves the templates matching ``filters``.

        param filters: ManagedTemplateFilter
        return: list[ManagedTemplate]
        raises ManagedTemplateInvalidFilterError: if the filter is malformed or names an
            unknown field.
        """
        self.validate_filter(filters)
        return list(self.template_manager_backend.get_filtered_templates(filters))

    def get_paginated_templates(self, page: int, page_size: int) -> list[ManagedTemplate]:
        """
        Retrieves one page of templates.

        param page: int -- 1-indexed.
        param page_size: int
        return: list[ManagedTemplate]
        raises ValueError: if ``page`` or ``page_size`` is below 1.
        """
        self._validate_pagination(page, page_size)
        return list(self.template_manager_backend.get_paginated_templates(page, page_size))

    def get_paginated_filtered_templates(
        self,
        filters: ManagedTemplateFilter,
        page: int,
        page_size: int,
    ) -> list[ManagedTemplate]:
        """
        Retrieves one page of the templates matching ``filters``.

        param filters: ManagedTemplateFilter
        param page: int -- 1-indexed.
        param page_size: int
        return: list[ManagedTemplate]
        raises ManagedTemplateInvalidFilterError: if the filter is malformed or names an
            unknown field.
        raises ValueError: if ``page`` or ``page_size`` is below 1.
        """
        self.validate_filter(filters)
        self._validate_pagination(page, page_size)
        return list(
            self.template_manager_backend.get_paginated_filtered_templates(filters, page, page_size)
        )

    def validate_filter(self, filters: ManagedTemplateFilter, _path: str = "filters") -> None:
        """
        Checks a filter's shape and field names, raising rather than passing a broken filter on.

        A backend translating an unknown field usually either matches nothing or raises
        something backend-specific, both of which are hard to debug from the call site. This
        catches the common mistakes -- a typo'd field name, an ``and``/``or`` that is not a
        list, a logical group carrying sibling keys -- while the caller's own frame is still on
        the stack. It does not validate lookup values; the backend remains the authority there.

        param filters: ManagedTemplateFilter
        raises ManagedTemplateInvalidFilterError: if the filter is malformed or names an
            unknown field.
        """
        if not isinstance(filters, dict):
            raise ManagedTemplateInvalidFilterError(
                f"{_path} must be a dict, got {type(filters).__name__}."
            )

        if is_field_filter(filters):
            unknown = sorted(set(filters) - _KNOWN_FILTER_FIELDS)
            if unknown:
                known = ", ".join(sorted(_KNOWN_FILTER_FIELDS))
                raise ManagedTemplateInvalidFilterError(
                    f"{_path} names unknown field(s): {', '.join(unknown)}. Known fields: {known}."
                )
            return

        # A logical group is exactly one of and/or/not, and nothing else. Allowing siblings
        # would make the intended combination ambiguous.
        if len(filters) > 1:
            present = ", ".join(sorted(filters))
            raise ManagedTemplateInvalidFilterError(
                f"{_path} mixes a logical operator with other keys ({present}). Wrap the field "
                f"filter in its own group instead."
            )

        key = next(iter(filters))
        value = filters[key]  # type: ignore[literal-required]

        if key == "not":
            self.validate_filter(value, f"{_path}.not")
            return

        if not isinstance(value, list):
            raise ManagedTemplateInvalidFilterError(
                f"{_path}.{key} must be a list of filters, got {type(value).__name__}."
            )
        if not value:
            raise ManagedTemplateInvalidFilterError(f"{_path}.{key} must not be empty.")
        for index, sub_filter in enumerate(value):
            self.validate_filter(sub_filter, f"{_path}.{key}[{index}]")

    def _validate_pagination(self, page: int, page_size: int) -> None:
        if page < 1:
            raise ValueError(f"page must be 1 or greater, got {page}.")
        if page_size < 1:
            raise ValueError(f"page_size must be 1 or greater, got {page_size}.")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(
        self,
        notification: "Notification | OneOffNotification",
        context: "NotificationContextDict",
        version: int | None = None,
    ) -> NotificationSendInput:
        """
        Renders a notification against a specific version of its template.

        The notification's ``body_template`` is the template key. Passing ``version`` is what
        separates this from ``ManagedTemplateRenderer.render``, which always resolves the key
        to whatever version the backend considers current -- use this to preview an
        unpublished draft, or to re-render an old notification against the version that was
        live when it was sent.

        param notification: Notification | OneOffNotification
        param context: NotificationContextDict
        param version: int | None -- the latest version when None.
        return: NotificationSendInput
        raises ManagedTemplateNotFoundError: if the key (or that version of it) does not exist.
        """
        template = self.get_template(notification.body_template, version)
        return self.render_template(notification, template, context)

    def render_template(
        self,
        notification: "Notification | OneOffNotification",
        template: ManagedTemplate,
        context: "NotificationContextDict",
    ) -> NotificationSendInput:
        """
        Renders a notification against a template already in hand, with no backend read.

        param notification: Notification | OneOffNotification
        param template: ManagedTemplate
        param context: NotificationContextDict
        return: NotificationSendInput
        """
        template_content = self.template_renderer.create_template_content(template)
        return self.template_renderer.render_from_template_content(
            notification, template_content, context
        )
