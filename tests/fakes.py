"""In-memory fakes used across the suite.

The backend here is a real implementation of ``BaseTemplateManagerBackend``, not a mock: the
service under test drives it end to end, so a test failure means the service is wrong rather
than that an expectation string drifted. It keeps templates in a list and evaluates filters
itself, which is enough to exercise every read path the service exposes.
"""

import dataclasses
import datetime
import itertools
from collections.abc import Iterable

from vintasend.services.dataclasses import Notification, OneOffNotification
from vintasend.services.notification_template_renderers.base import (
    BaseNotificationTemplateRenderer,
    TemplateContent,
)
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    EmailTemplateContent,
    TemplatedEmail,
)

from vintasend_managed_templates.base_template_manager_backend import BaseTemplateManagerBackend
from vintasend_managed_templates.composition import is_abstract
from vintasend_managed_templates.constants import (
    MOST_RECENT_ACTIVE_VERSION_STATUSES,
    ManagedTemplateStatus,
    ManagedTemplateTagStatus,
)
from vintasend_managed_templates.dataclasses import (
    ManagedTemplate,
    ManagedTemplateCreateInput,
    ManagedTemplateStatusHistory,
    ManagedTemplateTag,
    ManagedTemplateUpdateInput,
)
from vintasend_managed_templates.exceptions import (
    ManagedTemplateCompositionError,
    ManagedTemplateInvalidTagError,
    ManagedTemplateNotFoundError,
    ManagedTemplateTagAlreadyExistsError,
    ManagedTemplateTagNotFoundError,
)
from vintasend_managed_templates.tags import next_available_slug, slugify_tag


# Fixed epoch plus a per-write counter, so ``created`` / ``updated`` are deterministic and
# strictly increasing without any test having to freeze the clock.
EPOCH = datetime.datetime(2026, 1, 1, 12, 0, 0)

_STRING_LOOKUPS = {
    "exact": lambda field, value: field == value,
    "starts_with": lambda field, value: field.startswith(value),
    "ends_with": lambda field, value: field.endswith(value),
    "includes": lambda field, value: value in field,
}
_STRING_FIELDS = frozenset({"name", "description", "key", "template_managed_backend"})
_DATE_RANGE_FIELDS = {"created_at_range": "created", "updated_at_range": "updated"}


class InMemoryTemplateManagerBackend(BaseTemplateManagerBackend):
    """A complete, dependency-free implementation of the storage seam."""

    def __init__(self) -> None:
        self.templates: list[ManagedTemplate] = []
        self.status_history: list[ManagedTemplateStatusHistory] = []
        # slug -> tag. One shared pool; a template holds copies of the tags it carries, and
        # ``_retag`` re-reads from here so a rename reaches every template at once.
        self.tags: dict[str, ManagedTemplateTag] = {}
        # Every call the service makes, in order. Lets a test assert what was NOT called --
        # e.g. that a no-op status change never reached the backend.
        self.calls: list[str] = []
        self._ids = itertools.count(1)
        self._ticks = itertools.count()

    # -- helpers ----------------------------------------------------------

    def _now(self) -> datetime.datetime:
        return EPOCH + datetime.timedelta(seconds=next(self._ticks))

    def _versions_of(self, template_key: str) -> list[ManagedTemplate]:
        return sorted((t for t in self.templates if t.key == template_key), key=lambda t: t.version)

    def _index_of(self, template: ManagedTemplate) -> int:
        return next(
            i
            for i, t in enumerate(self.templates)
            if t.key == template.key and t.version == template.version
        )

    # -- seam -------------------------------------------------------------

    def create_template(self, input: ManagedTemplateCreateInput) -> ManagedTemplate:  # noqa: A002
        self.calls.append("create_template")
        now = self._now()
        template = ManagedTemplate(
            id=next(self._ids),
            name=input.name,
            description=input.description,
            key=input.key,
            template_managed_backend=input.template_managed_backend,
            body_template=input.template_body,
            subject_template=input.template_subject,
            preheader_template=input.template_preheader,
            version=len(self._versions_of(input.key)) + 1,
            status=ManagedTemplateStatus.DRAFT,
            created=now,
            updated=now,
            tenant=input.tenant,
            tags=self.get_or_create_tags(input.tags or [], input.tenant),
        )
        template = self._with_derived_flags(template)
        self.templates.append(template)
        return template

    def get_template(self, template_key: str, version: int | None = None) -> ManagedTemplate:
        self.calls.append("get_template")
        versions = self._versions_of(template_key)
        if not versions:
            raise ManagedTemplateNotFoundError(
                f"Template with key '{template_key}' does not exist."
            )
        if version is None:
            return versions[-1]
        for template in versions:
            if template.version == version:
                return template
        raise ManagedTemplateNotFoundError(
            f"Template with key '{template_key}' and version {version} does not exist."
        )

    def update_template(
        self,
        template_key: str,
        input: ManagedTemplateUpdateInput,  # noqa: A002
    ) -> ManagedTemplate:
        self.calls.append("update_template")
        latest = self.get_template(template_key)
        new_version = dataclasses.replace(
            latest,
            id=next(self._ids),
            version=latest.version + 1,
            status=ManagedTemplateStatus.DRAFT,
            updated=self._now(),
            name=input.name or latest.name,
            description=input.description or latest.description,
            body_template=input.template_body or latest.body_template,
            subject_template=input.template_subject or latest.subject_template,
            preheader_template=input.template_preheader or latest.preheader_template,
            # ``None`` carries the previous version's tags forward; ``[]`` clears them. A
            # falsy-or would collapse those two, so this tests for None explicitly.
            tags=(
                list(latest.tags)
                if input.tags is None
                else self.get_or_create_tags(input.tags, latest.tenant)
            ),
        )
        new_version = self._with_derived_flags(new_version)
        self.templates.append(new_version)
        return new_version

    @staticmethod
    def _with_derived_flags(template: ManagedTemplate) -> ManagedTemplate:
        """Fill in the fields a backend derives rather than stores as it was given them.

        Just ``is_abstract`` so far. Doing it in one place, on the way into the store, is what
        the seam asks of every backend: the flag is a denormalization of the source, so it is
        recomputed by whatever writes the source and never carried forward from a previous
        version.
        """
        try:
            derived = is_abstract(template)
        except ManagedTemplateCompositionError:
            # A template whose tags are malformed has no answer, and a write is not the
            # place to report a syntax error -- the flag is a search convenience, and a
            # template nobody can parse cannot be extended either. Reads as concrete, which
            # is the rule every backend in this project follows.
            derived = False
        return dataclasses.replace(template, is_abstract=derived)

    def delete_template(self, template_key: str, version: int | None = None) -> None:
        self.calls.append("delete_template")
        target = self.get_template(template_key, version)
        self.templates.pop(self._index_of(target))

    def create_template_status_update(
        self,
        template_key: str,
        version: int,
        status: ManagedTemplateStatus,
        changed_by: str | None = None,
    ) -> None:
        self.calls.append("create_template_status_update")
        target = self.get_template(template_key, version)
        self.templates[self._index_of(target)] = dataclasses.replace(
            target, status=status, updated=self._now()
        )
        self.status_history.append(
            ManagedTemplateStatusHistory(
                template_key=template_key,
                version=version,
                status=status,
                created=self._now(),
                created_by=changed_by,
                tenant=target.tenant,
            )
        )

    def get_template_status_history(
        self, template_key: str, version: int | None = None
    ) -> list[ManagedTemplateStatusHistory]:
        self.calls.append("get_template_status_history")
        return [
            record
            for record in self.status_history
            if record.template_key == template_key
            and (version is None or record.version == version)
        ]

    # -- tags -------------------------------------------------------------

    def _slug_or_raise(self, text: str) -> str:
        slug = slugify_tag(text)
        if not slug:
            raise ManagedTemplateInvalidTagError(f"Tag text {text!r} cannot be slugified.")
        return slug

    def _find_tag(self, slug: str) -> ManagedTemplateTag:
        tag = self.tags.get(slugify_tag(slug))
        if tag is None:
            raise ManagedTemplateTagNotFoundError(f"Tag '{slug}' does not exist.")
        return tag

    def _retag(self) -> None:
        """Refresh the tag copies every template holds from the shared pool.

        A real store joins, so a rename or a delete is visible through every template at once.
        Copying tags onto the template rows means this fake has to do the propagation itself.
        """
        for index, template in enumerate(self.templates):
            refreshed = [self.tags[tag.slug] for tag in template.tags if tag.slug in self.tags]
            self.templates[index] = dataclasses.replace(template, tags=refreshed)

    def get_or_create_tags(
        self, texts: Iterable[str], tenant: str | None = None
    ) -> list[ManagedTemplateTag]:
        self.calls.append("get_or_create_tags")
        resolved: list[ManagedTemplateTag] = []
        for text in texts:
            slug = self._slug_or_raise(text)
            tag = self.tags.get(slug)
            if tag is None:
                tag = self._store_tag(text, slug, tenant)
            if tag not in resolved:
                resolved.append(tag)
        return resolved

    def _store_tag(self, text: str, slug: str, tenant: str | None) -> ManagedTemplateTag:
        now = self._now()
        tag = ManagedTemplateTag(
            id=next(self._ids),
            text=text,
            slug=slug,
            status=ManagedTemplateTagStatus.ACTIVE,
            created=now,
            updated=now,
            tenant=tenant,
        )
        self.tags[slug] = tag
        return tag

    def create_tag(self, text: str, tenant: str | None = None) -> ManagedTemplateTag:
        self.calls.append("create_tag")
        slug = self._slug_or_raise(text)
        if slug in self.tags:
            raise ManagedTemplateTagAlreadyExistsError(f"Tag '{slug}' already exists.")
        return self._store_tag(text, slug, tenant)

    def get_tag(self, slug: str) -> ManagedTemplateTag:
        self.calls.append("get_tag")
        return self._find_tag(slug)

    def update_tag(self, slug: str, text: str) -> ManagedTemplateTag:
        self.calls.append("update_tag")
        tag = self._find_tag(slug)
        new_slug = next_available_slug(
            self._slug_or_raise(text),
            lambda candidate: candidate in self.tags and candidate != tag.slug,
        )
        del self.tags[tag.slug]
        updated = dataclasses.replace(tag, text=text, slug=new_slug, updated=self._now())
        self.tags[new_slug] = updated
        # The templates still hold the old slug, so they are re-pointed before the refresh.
        for index, template in enumerate(self.templates):
            if any(t.slug == tag.slug for t in template.tags):
                kept = [t for t in template.tags if t.slug != tag.slug]
                self.templates[index] = dataclasses.replace(template, tags=[*kept, updated])
        self._retag()
        return updated

    def set_tag_status(self, slug: str, status: ManagedTemplateTagStatus) -> ManagedTemplateTag:
        self.calls.append("set_tag_status")
        tag = self._find_tag(slug)
        updated = dataclasses.replace(tag, status=status, updated=self._now())
        self.tags[tag.slug] = updated
        self._retag()
        return updated

    def delete_tag(self, slug: str) -> None:
        self.calls.append("delete_tag")
        tag = self._find_tag(slug)
        del self.tags[tag.slug]
        self._retag()

    def get_tags(
        self,
        status: Iterable[ManagedTemplateTagStatus] | None = None,
        search: str | None = None,
        tenant: str | None = None,
    ) -> list[ManagedTemplateTag]:
        self.calls.append("get_tags")
        wanted = set(status) if status is not None else None
        needle = search.lower() if search else None
        return [
            tag
            for tag in self.tags.values()
            if (wanted is None or tag.status in wanted)
            and (tenant is None or tag.tenant == tenant)
            and (needle is None or needle in tag.text.lower() or needle in tag.slug.lower())
        ]

    def get_template_tags(
        self, template_key: str, version: int | None = None
    ) -> list[ManagedTemplateTag]:
        self.calls.append("get_template_tags")
        return list(self.get_template(template_key, version).tags)

    def set_template_tags(
        self, template_key: str, tags: Iterable[str], version: int | None = None
    ) -> ManagedTemplate:
        self.calls.append("set_template_tags")
        target = self.get_template(template_key, version)
        resolved = self.get_or_create_tags(tags, target.tenant)
        updated = dataclasses.replace(target, tags=resolved, updated=self._now())
        self.templates[self._index_of(target)] = updated
        return updated

    # -- template reads ---------------------------------------------------

    def get_all_templates(self) -> list[ManagedTemplate]:
        self.calls.append("get_all_templates")
        return list(self.templates)

    def get_templates_by_status(
        self, status: Iterable[ManagedTemplateStatus]
    ) -> list[ManagedTemplate]:
        self.calls.append("get_templates_by_status")
        wanted = set(status)
        return [t for t in self.templates if t.status in wanted]

    def get_filtered_templates(self, filters) -> list[ManagedTemplate]:
        self.calls.append("get_filtered_templates")
        return [t for t in self.templates if self._matches(t, filters)]

    def get_paginated_templates(self, page: int, page_size: int) -> list[ManagedTemplate]:
        self.calls.append("get_paginated_templates")
        return self._page(self.templates, page, page_size)

    def get_paginated_filtered_templates(
        self, filters, page: int, page_size: int
    ) -> list[ManagedTemplate]:
        self.calls.append("get_paginated_filtered_templates")
        return self._page([t for t in self.templates if self._matches(t, filters)], page, page_size)

    # -- filter evaluation ------------------------------------------------

    @staticmethod
    def _page(rows: list[ManagedTemplate], page: int, page_size: int) -> list[ManagedTemplate]:
        start = (page - 1) * page_size
        return rows[start : start + page_size]

    def _matches(self, template: ManagedTemplate, filters) -> bool:
        if "and" in filters:
            return all(self._matches(template, sub) for sub in filters["and"])
        if "or" in filters:
            return any(self._matches(template, sub) for sub in filters["or"])
        if "not" in filters:
            return not self._matches(template, filters["not"])
        return all(self._matches_field(template, field, spec) for field, spec in filters.items())

    def _current_versions(self) -> set[tuple[str, int]]:
        """``(key, version)`` of the highest ACTIVE-or-DRAFT version of each key.

        Recomputed per call rather than cached: every write goes straight into
        ``self.templates``, so a cache here would answer with the store as it was.
        """
        highest: dict[str, int] = {}
        for template in self.templates:
            if template.status not in MOST_RECENT_ACTIVE_VERSION_STATUSES:
                continue
            if template.version > highest.get(template.key, 0):
                highest[template.key] = template.version
        return set(highest.items())

    def _matches_field(self, template: ManagedTemplate, field: str, spec) -> bool:
        if field == "is_abstract":
            # The stored flag, not a fresh parse: querying the denormalization is the whole
            # point of having one, and a store whose flag has drifted should show that here.
            return template.is_abstract is bool(spec)
        if field == "most_recent_active_version":
            # The one field that is about the key rather than the row, so it is answered
            # against the whole store instead of against an attribute of this template.
            is_current = (template.key, template.version) in self._current_versions()
            return is_current is bool(spec)
        if field in ("includes_all_tags", "includes_any_of_tags"):
            # Slugify what the caller passed so a filter may name a tag by its text.
            wanted = {slugify_tag(tag) for tag in spec}
            carried = {tag.slug for tag in template.tags}
            if field == "includes_all_tags":
                return wanted <= carried
            return bool(wanted & carried)
        if field in _DATE_RANGE_FIELDS:
            value = getattr(template, _DATE_RANGE_FIELDS[field])
            lower, upper = spec.get("from"), spec.get("to")
            return (lower is None or value >= lower) and (upper is None or value <= upper)
        if field == "status":
            if isinstance(spec, ManagedTemplateStatus):
                return template.status is spec
            if spec.get("lookup") == "in":
                return template.status in spec["value"]
            return template.status is spec["value"]
        if field == "version":
            if isinstance(spec, int):
                return template.version == spec
            comparisons = {
                "gt": template.version > spec["value"],
                "gte": template.version >= spec["value"],
                "lt": template.version < spec["value"],
                "lte": template.version <= spec["value"],
            }
            return comparisons[spec["lookup"]]
        if field in _STRING_FIELDS:
            actual = getattr(template, field)
            if isinstance(spec, str):
                return actual == spec
            expected = spec["value"]
            if not spec.get("case_sensitive", True):
                actual, expected = actual.lower(), expected.lower()
            return _STRING_LOOKUPS[spec.get("lookup", "exact")](actual, expected)
        raise AssertionError(f"fake backend has no rule for field {field!r}")


class RecordingEmailRenderer(BaseNotificationTemplateRenderer[EmailTemplateContent]):
    """Formats the template pair with the context, and records every call it receives."""

    def __init__(self) -> None:
        super().__init__()
        self.render_calls: list[str] = []
        self.render_from_content_calls: list[EmailTemplateContent] = []

    def render(self, notification, context):
        self.render_calls.append(notification.body_template)
        return TemplatedEmail(subject="from-render", body="from-render")

    def render_from_template_content(self, notification, template_content, context, **kwargs):
        self.render_from_content_calls.append(template_content)
        preheader = template_content.preheader_template
        return TemplatedEmail(
            subject=template_content.subject_template.format(**context),
            body=template_content.body_template.format(**context),
            preheader=preheader.format(**context) if preheader is not None else None,
        )


class RecordingSMSRenderer(BaseNotificationTemplateRenderer[TemplateContent]):
    """SMS twin of :class:`RecordingEmailRenderer`, for the body-only content type."""

    def __init__(self) -> None:
        super().__init__()
        self.render_from_content_calls: list[TemplateContent] = []

    def render(self, notification, context):
        return TemplatedEmail(subject="", body="from-render")

    def render_from_template_content(self, notification, template_content, context, **kwargs):
        self.render_from_content_calls.append(template_content)
        return TemplatedEmail(subject="", body=template_content.body_template.format(**context))


def make_create_input(
    key: str = "welcome",
    *,
    name: str = "Welcome email",
    description: str = "Sent on signup",
    template_managed_backend: str = "in-memory",
    template_body: str = "Hi {name}",
    template_subject: str | None = "Hello {name}",
    template_preheader: str | None = None,
    tenant: str | None = None,
    tags: list[str] | None = None,
) -> ManagedTemplateCreateInput:
    return ManagedTemplateCreateInput(
        name=name,
        description=description,
        key=key,
        template_managed_backend=template_managed_backend,
        template_body=template_body,
        template_subject=template_subject,
        template_preheader=template_preheader,
        tenant=tenant,
        tags=tags,
    )


def make_update_input(
    *,
    name: str | None = None,
    description: str | None = None,
    template_body: str | None = None,
    template_subject: str | None = None,
    template_preheader: str | None = None,
    tags: list[str] | None = None,
) -> ManagedTemplateUpdateInput:
    return ManagedTemplateUpdateInput(
        name=name,
        description=description,
        template_body=template_body,
        template_subject=template_subject,
        template_preheader=template_preheader,
        tags=tags,
    )


def make_notification(template_key: str = "welcome") -> Notification:
    return Notification(
        id=1,
        user_id=42,
        notification_type="EMAIL",
        title="Welcome",
        body_template=template_key,
        context_name="welcome_context",
        context_kwargs={},
        send_after=None,
        subject_template=template_key,
        preheader_template=template_key,
        status="PENDING_SEND",
    )


def make_one_off_notification(template_key: str = "welcome") -> OneOffNotification:
    return OneOffNotification(
        id=2,
        email_or_phone="someone@example.com",
        first_name="Ana",
        last_name="Silva",
        notification_type="EMAIL",
        title="Welcome",
        body_template=template_key,
        context_name="welcome_context",
        context_kwargs={},
        send_after=None,
        subject_template=template_key,
        preheader_template=template_key,
        status="PENDING_SEND",
    )
