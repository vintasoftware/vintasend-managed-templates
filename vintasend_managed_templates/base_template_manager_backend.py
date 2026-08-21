from abc import ABC, abstractmethod
from collections.abc import Iterable

from .constants import ManagedTemplateStatus, ManagedTemplateTagStatus
from .dataclasses import (
    ManagedTemplate,
    ManagedTemplateCreateInput,
    ManagedTemplateStatusHistory,
    ManagedTemplateTag,
    ManagedTemplateUpdateInput,
)
from .filters import ManagedTemplateFilter


class BaseTemplateManagerBackend(ABC):
    @abstractmethod
    def create_template(self, data: ManagedTemplateCreateInput) -> ManagedTemplate:
        """
        Creates a new template in the backend.

        param data: ManagedTemplateCreateInput
        return: ManagedTemplate
        """
        ...

    @abstractmethod
    def get_template(self, template_key: str, version: int | None = None) -> ManagedTemplate:
        """
        Retrieves a template from the backend by its key.

        param template_key: str
        param version: int | None
        return: ManagedTemplate
        """

        ...

    @abstractmethod
    def update_template(
        self, template_key: str, data: ManagedTemplateUpdateInput
    ) -> ManagedTemplate:
        """
        Creates a new version of an existing template in the backend.

        A new version is a new row, and the version it was copied from is left exactly as it
        was -- content, status and history. That is what versioning is for: a notification
        that already went out against v1 renders v1 forever, however many versions follow it,
        and several versions of one key are live at the same time as a matter of course.

        The new version starts in DRAFT whatever its predecessor's status was, so a copy
        nobody has reviewed is never published by the act of creating it. Fields left ``None``
        on the input -- tags included -- carry forward from the version copied.

        param template_key: str
        param data: ManagedTemplateCreateInput
        return: ManagedTemplate -- the new version.
        """
        ...

    @abstractmethod
    def delete_template(self, template_key: str, version: int | None = None) -> None:
        """
        Deletes a template from the backend by its key.

        param template_key: str
        """
        ...

    @abstractmethod
    def create_template_status_update(
        self,
        template_key: str,
        version: int,
        status: ManagedTemplateStatus,
        changed_by: str | None = None,
    ) -> None:
        """
        Creates a new status update for a template in the backend.

        param template_key: str
        param version: int,
        param status: ManagedTemplateStatus
        param changed_by: str | None
        """
        ...

    @abstractmethod
    def get_template_status_history(
        self, template_key: str, version: int | None = None
    ) -> Iterable[ManagedTemplateStatusHistory]:
        """
        Retrieves the status history of a template from the backend.

        param template_key: str
        param version: int | None
        return: Iterable[ManagedTemplateStatusHistory]
        """
        ...

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------
    #
    # Tags are many-to-many with template versions and are identified by their slug, which
    # the backend derives from the text with ``vintasend_managed_templates.tags.slugify_tag``
    # and keeps unique across the store -- appending ``-2``, ``-3`` and so on when a distinct
    # text slugs onto a taken slug. Every method below that takes a slug accepts the original
    # text too: implementations slugify what they are given before looking it up.

    @abstractmethod
    def get_or_create_tags(
        self, texts: Iterable[str], tenant: str | None = None
    ) -> list[ManagedTemplateTag]:
        """
        Resolves tag texts to tags, creating the ones that do not exist yet.

        This is the on-the-fly path every tagging call goes through: a caller tags a template
        with what a person typed and never has to check first whether that tag exists. Texts
        that slugify onto an existing tag resolve to it rather than creating a duplicate, and
        an existing tag is returned as it stands -- its text and status are left alone, so
        re-using an ARCHIVED tag does not quietly bring it back.

        param texts: Iterable[str] -- tag texts (or slugs).
        param tenant: str | None
        return: list[ManagedTemplateTag] -- one per distinct text, in the order given.
        raises ManagedTemplateInvalidTagError: if a text has nothing that can be slugified.
        """
        ...

    @abstractmethod
    def create_tag(self, text: str, tenant: str | None = None) -> ManagedTemplateTag:
        """
        Creates a tag, failing if its text already slugs onto an existing one.

        Use ``get_or_create_tags`` when a duplicate should resolve to the existing tag; this
        is the explicit-create path, where a collision is worth reporting to the caller.

        param text: str
        param tenant: str | None
        return: ManagedTemplateTag
        raises ManagedTemplateTagAlreadyExistsError: if a tag with that slug exists.
        raises ManagedTemplateInvalidTagError: if the text has nothing that can be slugified.
        """
        ...

    @abstractmethod
    def get_tag(self, slug: str) -> ManagedTemplateTag:
        """
        Retrieves one tag by slug (or by the text it was created from).

        param slug: str
        return: ManagedTemplateTag
        raises ManagedTemplateTagNotFoundError: if no tag has that slug.
        """
        ...

    @abstractmethod
    def update_tag(self, slug: str, text: str) -> ManagedTemplateTag:
        """
        Renames a tag, regenerating its slug from the new text.

        The slug changes, so anything holding the old one -- a bookmarked filter, a cached
        query -- stops matching. The tag keeps its identity and its templates: only the
        strings change.

        param slug: str -- the tag's current slug.
        param text: str -- the new text.
        return: ManagedTemplateTag -- with its regenerated, unique slug.
        raises ManagedTemplateTagNotFoundError: if no tag has that slug.
        raises ManagedTemplateInvalidTagError: if the new text has nothing to slugify.
        """
        ...

    @abstractmethod
    def set_tag_status(self, slug: str, status: ManagedTemplateTagStatus) -> ManagedTemplateTag:
        """
        Archives a tag, or brings an archived one back.

        Archiving keeps every link to a template: filtering by an archived tag still returns
        the templates carrying it. What archiving is for is dropping the tag out of the
        pickers a UI builds from the ACTIVE list.

        param slug: str
        param status: ManagedTemplateTagStatus
        return: ManagedTemplateTag
        raises ManagedTemplateTagNotFoundError: if no tag has that slug.
        """
        ...

    @abstractmethod
    def delete_tag(self, slug: str) -> None:
        """
        Deletes a tag and removes it from every template carrying it.

        Unlike archiving, this is not reversible and the templates lose the label. Archive
        instead when the tag should stop being offered but the history should stand.

        param slug: str
        raises ManagedTemplateTagNotFoundError: if no tag has that slug.
        """
        ...

    @abstractmethod
    def get_tags(
        self,
        status: Iterable[ManagedTemplateTagStatus] | None = None,
        search: str | None = None,
        tenant: str | None = None,
    ) -> Iterable[ManagedTemplateTag]:
        """
        Retrieves tags, optionally narrowed by status, by a text search, or by tenant.

        param status: Iterable[ManagedTemplateTagStatus] | None -- every status when None.
        param search: str | None -- a case-insensitive substring of the text or the slug.
        param tenant: str | None -- every tenant when None.
        return: Iterable[ManagedTemplateTag]
        """
        ...

    @abstractmethod
    def get_template_tags(
        self, template_key: str, version: int | None = None
    ) -> Iterable[ManagedTemplateTag]:
        """
        Retrieves the tags on one version of a template, or on its latest version.

        param template_key: str
        param version: int | None
        return: Iterable[ManagedTemplateTag]
        raises ManagedTemplateNotFoundError: if the key (or that version of it) does not exist.
        """
        ...

    @abstractmethod
    def set_template_tags(
        self, template_key: str, tags: Iterable[str], version: int | None = None
    ) -> ManagedTemplate:
        """
        Replaces the tags on one version of a template, creating any that do not exist.

        This edits a version in place rather than creating a new one, which is the one thing
        about a template that does: tags are search metadata, not template content, so
        retagging for findability should not spawn a version and reset it to DRAFT.

        param template_key: str
        param tags: Iterable[str] -- tag texts (or slugs). Empty clears the version's tags.
        param version: int | None -- the latest version when None.
        return: ManagedTemplate -- the version with its new tags.
        raises ManagedTemplateNotFoundError: if the key (or that version of it) does not exist.
        raises ManagedTemplateInvalidTagError: if a text has nothing that can be slugified.
        """
        ...

    @abstractmethod
    def get_all_templates(self) -> Iterable[ManagedTemplate]:
        """
        Retrieves all templates from the backend.

        return: Iterable[ManagedTemplate]
        """
        ...

    @abstractmethod
    def get_templates_by_status(
        self, status: Iterable[ManagedTemplateStatus]
    ) -> Iterable[ManagedTemplate]:
        """
        Retrieves all templates from the backend with a specific status.

        param status: Iterable[ManagedTemplateStatus]
        return: Iterable[ManagedTemplate]
        """
        ...

    @abstractmethod
    def get_filtered_templates(self, filters: ManagedTemplateFilter) -> Iterable[ManagedTemplate]:
        """
        Retrieves templates from the backend that match the given filters.

        Every field of ``ManagedTemplateFilterFields`` tests an attribute of the row, with one
        exception: ``most_recent_active_version`` is about the *key*. ``True`` keeps only the
        highest-numbered version of each key whose status is in
        ``MOST_RECENT_ACTIVE_VERSION_STATUSES``, and ``False`` keeps every other row -- so an
        implementation answers it by comparing the row against its key's other versions rather
        than by reading a column. It is what the service's listing methods apply by default, so
        a backend that cannot evaluate it cannot serve a default listing.

        param filters: dict
        return: Iterable[ManagedTemplate]
        """
        ...

    @abstractmethod
    def get_paginated_templates(self, page: int, page_size: int) -> Iterable[ManagedTemplate]:
        """
        Retrieves a paginated list of templates from the backend.

        param page: int
        param page_size: int
        return: Iterable[ManagedTemplate]
        """
        ...

    @abstractmethod
    def get_paginated_filtered_templates(
        self,
        filters: ManagedTemplateFilter,
        page: int,
        page_size: int,
    ) -> Iterable[ManagedTemplate]:
        """
        Retrieves a paginated list of templates matching the given filters from the backend.

        param filters: ManagedTemplateFilter
        param page: int
        param page_size: int
        return: Iterable[ManagedTemplate]
        """
        ...
