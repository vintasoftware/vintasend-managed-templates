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
* **Tag hygiene.** Tag text is normalized and slugified here before it reaches the backend,
  so every implementation of the storage seam is handed the same slug for the same text, and
  text with nothing sluggable in it is rejected with ``ManagedTemplateInvalidTagError``
  instead of becoming a tag no filter can ever name.
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

import dataclasses
import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING, ClassVar, Generic, TypeVar

from vintasend.services.notification_template_renderers.base import NotificationSendInput

from .base_template_manager_backend import BaseTemplateManagerBackend
from .constants import ManagedTemplateStatus, ManagedTemplateTagStatus
from .dataclasses import (
    ManagedTemplate,
    ManagedTemplateCreateInput,
    ManagedTemplateStatusHistory,
    ManagedTemplateTag,
    ManagedTemplateUpdateInput,
)
from .exceptions import (
    ManagedTemplateInvalidFilterError,
    ManagedTemplateInvalidTagError,
    ManagedTemplateStatusTransitionError,
)
from .filters import (
    ManagedTemplateFilter,
    ManagedTemplateFilterFields,
    is_field_filter,
    is_tags_filter,
)
from .managed_template_renderer import ManagedTemplateRenderer, TemplateContentType
from .tags import normalize_tag_text, slugify_tag


if TYPE_CHECKING:
    from vintasend.services.dataclasses import (
        Notification,
        NotificationContextDict,
        OneOffNotification,
    )


logger = logging.getLogger(__name__)


# The two write inputs that carry tags. ``_with_clean_tags`` hands back whichever one it was
# given, so the create path keeps returning a create input and the update path an update one.
InputType = TypeVar("InputType", ManagedTemplateCreateInput, ManagedTemplateUpdateInput)


# Read off the TypedDict rather than hand-listing the names, so adding a field to
# ``ManagedTemplateFilterFields`` does not silently leave filter validation rejecting it.
_KNOWN_FILTER_FIELDS: frozenset[str] = frozenset(ManagedTemplateFilterFields.__annotations__)

# The filter fields whose value is a collection of tag slugs rather than a lookup dict.
_TAG_FILTER_FIELDS: frozenset[str] = frozenset({"includes_all_tags", "includes_any_of_tags"})

# The filter fields whose value is a bare boolean.
_FLAG_FILTER_FIELDS: frozenset[str] = frozenset({"most_recent_active_version"})


def _current_versions_only() -> ManagedTemplateFilterFields:
    """The filter a listing applies unless it was asked for every version: one row per key.

    Built per call rather than shared, so a backend that keeps the filter it was handed --
    or a caller that reads it off a fake and edits it -- cannot change what the next listing
    means.
    """
    return {"most_recent_active_version": True}


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

        Any tag text on the input that has no tag behind it yet becomes one, so a caller
        never has to create tags before using them.

        param input: ManagedTemplateCreateInput
        return: ManagedTemplate
        raises ManagedTemplateInvalidTagError: if a tag text has nothing that can be slugified.
        """
        return self.template_manager_backend.create_template(self._with_clean_tags(input))

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

        Tags follow the same rule as every other field on the input: ``None`` carries the
        previous version's tags forward, and an empty list means a version with none.

        param template_key: str
        param input: ManagedTemplateUpdateInput
        return: ManagedTemplate
        raises ManagedTemplateNotFoundError: if the key does not exist.
        raises ManagedTemplateInvalidTagError: if a tag text has nothing that can be slugified.
        """
        return self.template_manager_backend.update_template(
            template_key, self._with_clean_tags(input)
        )

    def _with_clean_tags(self, input: InputType) -> "InputType":  # noqa: A002
        """Return the input with its tag texts normalized, or unchanged when it carries none.

        Cleaning here means a backend receives text it can slugify, and a caller hears about
        an unusable tag before anything is written -- rather than after a template exists with
        a tag nothing can name.
        """
        if input.tags is None:
            return input
        return dataclasses.replace(input, tags=self._clean_tag_texts(input.tags))

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
    # Tags
    # ------------------------------------------------------------------

    def create_tag(self, text: str, tenant: str | None = None) -> ManagedTemplateTag:
        """
        Creates a tag, failing if its text already slugs onto an existing one.

        Tagging a template creates missing tags on its own, so this is for the case where a
        tag is being defined ahead of any template using it -- and where a collision with an
        existing tag is worth hearing about rather than silently resolving.

        param text: str
        param tenant: str | None
        return: ManagedTemplateTag
        raises ManagedTemplateInvalidTagError: if the text has nothing that can be slugified.
        raises ManagedTemplateTagAlreadyExistsError: if a tag with that slug exists.
        """
        return self.template_manager_backend.create_tag(self._clean_tag_text(text), tenant)

    def get_tag(self, slug: str) -> ManagedTemplateTag:
        """
        Retrieves one tag by slug, or by the text it was created from.

        param slug: str
        return: ManagedTemplateTag
        raises ManagedTemplateTagNotFoundError: if no tag has that slug.
        """
        return self.template_manager_backend.get_tag(slug)

    def get_tags(
        self,
        status: Iterable[ManagedTemplateTagStatus] | None = None,
        search: str | None = None,
        tenant: str | None = None,
    ) -> list[ManagedTemplateTag]:
        """
        Retrieves tags, optionally narrowed by status, by a text search, or by tenant.

        param status: Iterable[ManagedTemplateTagStatus] | None -- every status when None.
        param search: str | None -- a case-insensitive substring of the text or the slug.
        param tenant: str | None
        return: list[ManagedTemplateTag]
        """
        return list(self.template_manager_backend.get_tags(status, search, tenant))

    def get_active_tags(self, tenant: str | None = None) -> list[ManagedTemplateTag]:
        """
        Retrieves the tags still on offer -- what a tag picker should show.

        param tenant: str | None
        return: list[ManagedTemplateTag]
        """
        return self.get_tags(status=[ManagedTemplateTagStatus.ACTIVE], tenant=tenant)

    def update_tag(self, slug: str, text: str) -> ManagedTemplateTag:
        """
        Renames a tag, regenerating its slug from the new text.

        The templates carrying the tag keep it. The slug changes, though, so a saved filter
        naming the old slug stops matching -- a rename is a change of identity, not a display
        change.

        param slug: str -- the tag's current slug.
        param text: str -- the new text.
        return: ManagedTemplateTag
        raises ManagedTemplateTagNotFoundError: if no tag has that slug.
        raises ManagedTemplateInvalidTagError: if the new text has nothing to slugify.
        """
        return self.template_manager_backend.update_tag(slug, self._clean_tag_text(text))

    def archive_tag(self, slug: str) -> ManagedTemplateTag:
        """
        Retires a tag from the pickers without touching the templates carrying it.

        Filtering by an archived tag keeps working, and ``restore_tag`` puts it back. Use
        ``delete_tag`` when the label should be gone from the templates too.

        param slug: str
        return: ManagedTemplateTag
        raises ManagedTemplateTagNotFoundError: if no tag has that slug.
        """
        return self.set_tag_status(slug, ManagedTemplateTagStatus.ARCHIVED)

    def restore_tag(self, slug: str) -> ManagedTemplateTag:
        """
        Puts an archived tag back on offer.

        Unlike an archived template version -- terminal, because reviving one would rewrite
        what its audit trail says happened -- a tag carries no history to contradict, so
        archiving one is reversible.

        param slug: str
        return: ManagedTemplateTag
        raises ManagedTemplateTagNotFoundError: if no tag has that slug.
        """
        return self.set_tag_status(slug, ManagedTemplateTagStatus.ACTIVE)

    def set_tag_status(self, slug: str, status: ManagedTemplateTagStatus) -> ManagedTemplateTag:
        """
        Moves a tag to ``status``.

        param slug: str
        param status: ManagedTemplateTagStatus
        return: ManagedTemplateTag
        raises ManagedTemplateTagNotFoundError: if no tag has that slug.
        """
        return self.template_manager_backend.set_tag_status(slug, status)

    def delete_tag(self, slug: str) -> None:
        """
        Deletes a tag, removing it from every template carrying it.

        param slug: str
        raises ManagedTemplateTagNotFoundError: if no tag has that slug.
        """
        self.template_manager_backend.delete_tag(slug)

    def get_or_create_tags(
        self, texts: Iterable[str], tenant: str | None = None
    ) -> list[ManagedTemplateTag]:
        """
        Resolves tag texts to tags, creating the ones that do not exist yet.

        param texts: Iterable[str] -- tag texts (or slugs).
        param tenant: str | None
        return: list[ManagedTemplateTag]
        raises ManagedTemplateInvalidTagError: if a text has nothing that can be slugified.
        """
        return self.template_manager_backend.get_or_create_tags(
            self._clean_tag_texts(texts), tenant
        )

    def get_template_tags(
        self, template_key: str, version: int | None = None
    ) -> list[ManagedTemplateTag]:
        """
        Retrieves the tags on one version of a template, or on its latest version.

        param template_key: str
        param version: int | None
        return: list[ManagedTemplateTag]
        raises ManagedTemplateNotFoundError: if the key (or that version of it) does not exist.
        """
        return list(self.template_manager_backend.get_template_tags(template_key, version))

    def set_template_tags(
        self, template_key: str, tags: Iterable[str], version: int | None = None
    ) -> ManagedTemplate:
        """
        Replaces the tags on one version of a template, creating any that do not exist.

        Retagging edits the version in place instead of creating a new one: tags are how a
        template is found, not part of what it renders, so relabelling should not spawn a
        version and drop it back to DRAFT.

        param template_key: str
        param tags: Iterable[str] -- tag texts (or slugs). Empty clears the version's tags.
        param version: int | None -- the latest version when None.
        return: ManagedTemplate
        raises ManagedTemplateNotFoundError: if the key (or that version of it) does not exist.
        raises ManagedTemplateInvalidTagError: if a text has nothing that can be slugified.
        """
        return self.template_manager_backend.set_template_tags(
            template_key, self._clean_tag_texts(tags), version
        )

    def add_template_tags(
        self, template_key: str, tags: Iterable[str], version: int | None = None
    ) -> ManagedTemplate:
        """
        Adds tags to a version, leaving the ones already on it in place.

        param template_key: str
        param tags: Iterable[str] -- tag texts (or slugs).
        param version: int | None
        return: ManagedTemplate
        raises ManagedTemplateNotFoundError: if the key (or that version of it) does not exist.
        raises ManagedTemplateInvalidTagError: if a text has nothing that can be slugified.
        """
        template = self.get_template(template_key, version)
        existing = [tag.slug for tag in template.tags]
        added = [slug for slug in self._slugs_for(tags) if slug not in existing]
        if not added:
            return template
        return self.set_template_tags(template_key, existing + added, template.version)

    def remove_template_tags(
        self, template_key: str, tags: Iterable[str], version: int | None = None
    ) -> ManagedTemplate:
        """
        Removes tags from a version. Tags it does not carry are ignored.

        The tags themselves survive -- this unlinks them from one version, it does not delete
        them. Removing a tag no version carries any more leaves it in the tag list, where
        ``delete_tag`` or ``archive_tag`` can deal with it.

        param template_key: str
        param tags: Iterable[str] -- tag texts (or slugs).
        param version: int | None
        return: ManagedTemplate
        raises ManagedTemplateNotFoundError: if the key (or that version of it) does not exist.
        raises ManagedTemplateInvalidTagError: if a text has nothing that can be slugified.
        """
        template = self.get_template(template_key, version)
        unwanted = set(self._slugs_for(tags))
        remaining = [tag.slug for tag in template.tags if tag.slug not in unwanted]
        if len(remaining) == len(template.tags):
            return template
        return self.set_template_tags(template_key, remaining, template.version)

    def get_templates_by_tags(
        self, tags: Iterable[str], match_all: bool = True
    ) -> list[ManagedTemplate]:
        """
        Retrieves the templates carrying these tags -- all of them, or any of them.

        A shorthand for the ``includes_all_tags`` / ``includes_any_of_tags`` filters, which
        is what it builds. Follows the same empty-collection rule they do: matching *all* of
        no tags returns everything, matching *any* of no tags returns nothing.

        param tags: Iterable[str] -- tag texts (or slugs).
        param match_all: bool -- every tag when True (the default), at least one when False.
        return: list[ManagedTemplate]
        """
        # The two branches are spelled out rather than built from a variable key: a dict
        # keyed by a `str` variable does not narrow to the filter TypedDict, and writing the
        # literals keeps the call type-checked instead of casting the check away.
        slugs = self._slugs_for(tags)
        filters: ManagedTemplateFilter = (
            {"includes_all_tags": slugs} if match_all else {"includes_any_of_tags": slugs}
        )
        return self.get_filtered_templates(filters)

    def _clean_tag_text(self, text: str) -> str:
        """Trim a tag's text, rejecting it when nothing sluggable is left.

        Checked here rather than left to the backend so every backend is handed text it can
        slugify, and so a caller hears about ``"  "`` or ``"!!!"`` at the call site instead of
        ending up with a tag whose slug is empty and which no filter can name.
        """
        cleaned = normalize_tag_text(text)
        if not cleaned or not slugify_tag(cleaned):
            raise ManagedTemplateInvalidTagError(
                f"Tag text {text!r} has no characters that can be turned into a slug."
            )
        return cleaned

    def _clean_tag_texts(self, texts: Iterable[str]) -> list[str]:
        """Clean each text, dropping repeats that slug onto a tag already in the list."""
        cleaned: list[str] = []
        seen: set[str] = set()
        for text in texts:
            candidate = self._clean_tag_text(text)
            slug = slugify_tag(candidate)
            if slug in seen:
                continue
            seen.add(slug)
            cleaned.append(candidate)
        return cleaned

    @staticmethod
    def _slugs_for(tags: Iterable[str]) -> list[str]:
        """Slugify a caller's tag texts, dropping the ones with nothing sluggable.

        Unlike ``_clean_tag_texts`` this never raises: these slugs are used to *match*, and a
        tag no store could hold simply matches nothing -- which is a correct answer, not an
        error worth interrupting a search for.
        """
        slugs: list[str] = []
        for tag in tags:
            slug = slugify_tag(tag)
            if slug and slug not in slugs:
                slugs.append(slug)
        return slugs

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_all_templates(self, include_all_versions: bool = False) -> list[ManagedTemplate]:
        """
        Retrieves the current version of every template -- one row per key.

        "Current" is the ``most_recent_active_version`` filter: the highest-numbered ACTIVE or
        DRAFT version of each key. It is the default because a listing is nearly always a list
        of *templates*, and the store holds a row per *version*, so the unfiltered read shows
        the same template once per version it has ever had and hides the current one among its
        own history.

        Pass ``include_all_versions=True`` for the raw read -- every version of every key, in
        whatever order the backend keeps them. ``get_template_versions`` is the narrower way to
        ask the same question about one key.

        param include_all_versions: bool -- every version rather than the current one per key.
        return: list[ManagedTemplate]
        """
        if include_all_versions:
            return list(self.template_manager_backend.get_all_templates())
        return self.get_filtered_templates(_current_versions_only())

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

    def get_paginated_templates(
        self, page: int, page_size: int, include_all_versions: bool = False
    ) -> list[ManagedTemplate]:
        """
        Retrieves one page of templates, one row per key by default.

        Pages the same set ``get_all_templates`` lists, and defaults the same way and for the
        same reason: the current version of each key, unless ``include_all_versions`` asks for
        every version.

        param page: int -- 1-indexed.
        param page_size: int
        param include_all_versions: bool -- every version rather than the current one per key.
        return: list[ManagedTemplate]
        raises ValueError: if ``page`` or ``page_size`` is below 1.
        """
        self._validate_pagination(page, page_size)
        if include_all_versions:
            return list(self.template_manager_backend.get_paginated_templates(page, page_size))
        return self.get_paginated_filtered_templates(_current_versions_only(), page, page_size)

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
            self._validate_tag_fields(filters, _path)
            self._validate_flag_fields(filters, _path)
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

    def _validate_tag_fields(self, filters: ManagedTemplateFilterFields, _path: str) -> None:
        """Reject a tag filter that is not a collection of strings.

        The one value check ``validate_filter`` does make, because the failure it prevents is
        silent rather than loud: a bare ``"welcome"`` is iterable, so a backend would happily
        ask for the tags ``w``, ``e``, ``l``, ``c`` and return nothing, with no error anywhere.
        """
        for field in _TAG_FILTER_FIELDS:
            if field not in filters:
                continue
            value = filters[field]  # type: ignore[literal-required]
            if not is_tags_filter(value):
                raise ManagedTemplateInvalidFilterError(
                    f"{_path}.{field} must be a list of tag slugs, got "
                    f"{type(value).__name__}. Wrap a single tag in a list."
                )

    def _validate_flag_fields(self, filters: ManagedTemplateFilterFields, _path: str) -> None:
        """Reject a flag filter whose value is not a boolean.

        Checked for the same reason the tag fields are: the failure is silent otherwise. A
        backend reads a flag for its truthiness, so the string ``"false"`` -- what a query
        parameter that skipped parsing arrives as -- would ask for exactly what the caller
        meant to switch off.
        """
        for field in _FLAG_FILTER_FIELDS:
            if field not in filters:
                continue
            value = filters[field]  # type: ignore[literal-required]
            if not isinstance(value, bool):
                raise ManagedTemplateInvalidFilterError(
                    f"{_path}.{field} must be a boolean, got {type(value).__name__}."
                )

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
