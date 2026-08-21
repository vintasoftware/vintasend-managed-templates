# vintasend-managed-templates

Database-backed notification templates for
[vintasend](https://github.com/vintasoftware/vintasend): versioning, a
draft/active/inactive/archived lifecycle with an audit trail, tags, and filtering — all on top of
a storage seam you (or a ready-made package) implement.

A regular vintasend template renderer reads templates from wherever its engine looks, which is
usually files on disk. That means every copy change is a deploy. This package moves the templates
into a data store so someone who is not a developer can edit them, keeps every edit as a new
version, and lets you publish a version deliberately instead of the moment it is saved.

It is storage-agnostic on its own — it defines the interface, not the database. Pair it with a
manager backend such as
[vintasend-django-templates-manager](https://github.com/vintasoftware/vintasend-django-templates-manager/),
or implement `BaseTemplateManagerBackend` yourself.

## Install

```bash
poetry add vintasend-managed-templates
# or
pip install vintasend-managed-templates
```

Python 3.10–3.14. The only dependencies are `vintasend` itself and `typing-extensions`.

## The pieces

| Piece | What it is |
|---|---|
| `BaseTemplateManagerBackend` | The storage seam. An ABC covering template CRUD, versions, status history, tags, filtering, and pagination. |
| `ManagedTemplateService` | The API you call. Wraps a backend and a renderer with version resolution, status-transition rules, filter validation, and tag normalization. |
| `ManagedTemplateEmailRenderer` / `ManagedTemplateSMSRenderer` | A vintasend template renderer that wraps *another* renderer and feeds it a stored template instead of a template path. |
| `tags.slugify_tag` / `next_available_slug` | The shared slug rules, so every backend derives the same slug from the same text. |
| `dataclasses`, `constants`, `filters`, `exceptions` | The wire types: `ManagedTemplate`, `ManagedTemplateTag`, the two status enums, the filter TypedDicts, and the error hierarchy. |

Everything here is synchronous. There is no AsyncIO twin, because the seams it composes
(`BaseTemplateManagerBackend` and vintasend's template renderer seam) are both synchronous.

## Quick start

```python
from vintasend_managed_templates.dataclasses import ManagedTemplateCreateInput
from vintasend_managed_templates.managed_template_renderer import ManagedTemplateEmailRenderer
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

manager_backend = MyTemplateManagerBackend()          # any BaseTemplateManagerBackend
renderer = ManagedTemplateEmailRenderer(
    manager_backend,
    inner_renderer,                                    # any vintasend email renderer
)
service = ManagedTemplateService(manager_backend, renderer)

template = service.create_template(
    ManagedTemplateCreateInput(
        name="Welcome email",
        description="Sent right after signup",
        key="welcome",                                 # what notifications reference
        template_managed_backend="django",             # which manager backend stores it
        template_body="<p>Hi {{ name }}, welcome!</p>",
        template_subject="Welcome aboard",
        template_preheader=None,
        tenant=None,
        tags=["onboarding", "Black Friday"],
    )
)

service.activate("welcome", changed_by="hugo@example.com")
```

To send through it, hand the wrapping renderer to your adapter and set the notification's
`body_template` to the template **key** instead of a path:

```python
from vintasend.services.notification_service import NotificationService

notification_service = NotificationService(
    notification_adapters=[MyEmailAdapter(template_renderer=renderer, backend=notification_backend)],
    notification_backend=notification_backend,
)

notification_service.create_notification(
    user_id=user.id,
    notification_type="EMAIL",
    title="Welcome",
    body_template="welcome",   # a managed template key, not a file path
    context_name="welcome_context",
    context_kwargs={"user_id": user.id},
    send_after=None,
    subject_template="",
    preheader_template="",
)
```

Pass the renderer as a live instance rather than as a dotted import string: it takes a backend and
an inner renderer as constructor arguments, which a string path cannot supply.

Nothing else about creating or sending notifications changes.

### What the inner renderer has to do

`ManagedTemplateRenderer` looks the template up, builds an `EmailTemplateContent` (or a
`TemplateContent` for SMS) out of the stored strings, and calls the inner renderer's
`render_from_template_content`. So the inner renderer receives **template source in the
`body_template` field**, where it normally expects a name a loader can resolve.

Renderers that resolve names through a loader — `JinjaTemplatedEmailRenderer`,
`DjangoTemplatedEmailRenderer` — need a loader that will accept source. For Jinja that is one
line:

```python
from jinja2 import Environment, FunctionLoader
from vintasend_jinja.services.notification_template_renderers.jinja_templated_email_renderer import (
    JinjaTemplatedEmailRenderer,
)

inner_renderer = JinjaTemplatedEmailRenderer(Environment(loader=FunctionLoader(lambda source: source)))
```

A renderer written to compile source directly needs no such setup.

## Templates and versions

Templates are **versioned, never edited in place**. `update_template` copies the latest version
forward, applies the non-`None` fields of the input, and returns the new version — so a published
version's body can never change under a notification that already referenced it.

```python
from vintasend_managed_templates.dataclasses import ManagedTemplateUpdateInput

service.update_template("welcome", ManagedTemplateUpdateInput(
    name=None,                       # None leaves the field as the previous version had it
    description=None,
    template_body="<p>Hi {{ name }}, welcome aboard!</p>",
    template_subject=None,
    template_preheader=None,
    tags=None,                       # None carries tags forward; [] clears them
))

service.get_template("welcome")            # latest version
service.get_template("welcome", version=1) # a specific one
service.get_template_versions("welcome")   # every version, newest first
```

`version=None` means "the latest version of this key" everywhere in the service — reads, status
changes, tagging, and rendering — so callers only deal with version numbers when they actually
want a specific one.

## Statuses

A version moves through `DRAFT → ACTIVE → INACTIVE → ARCHIVED`, and every move is written to the
backend's audit trail:

```python
service.activate("welcome", changed_by="hugo@example.com")
service.deactivate("welcome")
service.archive("welcome", version=1)
service.get_status_history("welcome")      # newest change first
service.can_transition_to(template, ManagedTemplateStatus.ACTIVE)
```

The default transition table:

| From | May move to |
|---|---|
| `DRAFT` | `ACTIVE`, `ARCHIVED` |
| `ACTIVE` | `INACTIVE`, `ARCHIVED` |
| `INACTIVE` | `ACTIVE`, `ARCHIVED` |
| `ARCHIVED` | — terminal |

Anything else raises `ManagedTemplateStatusTransitionError`. Setting a version to the status it
already holds is a no-op: no history entry, no error. Override `ALLOWED_STATUS_TRANSITIONS` on a
subclass for a different lifecycle, or pass `validate_status_transitions=False` to leave the
ordering entirely to your application.

Two things the service deliberately does *not* decide for you:

* **A key may have several `ACTIVE` versions at once.** Activating one does not deactivate the
  others; choosing which active version wins at render time is the host's call.
* **`changed_by` is passed through untouched, `None` included.** Attribution is never required.

## Tags

Tags are many-to-many with template *versions* and are identified by a slug derived from the text
someone typed. Slugging lives in `vintasend_managed_templates.tags` rather than in a backend, so a
Django store and a SQLAlchemy store agree on what `Promoção` slugs to. Every call that takes a slug
also accepts the original text.

```python
service.add_template_tags("welcome", ["Black Friday"])   # creates the tag if it is new
service.remove_template_tags("welcome", ["black-friday"])
service.set_template_tags("welcome", ["onboarding"])     # replaces; [] clears
service.get_templates_by_tags(["onboarding", "email"], match_all=False)
service.get_active_tags()                                # what a tag picker should show
```

Retagging **edits the version in place** instead of creating one. Tags are how a template is
found, not part of what it renders, so relabelling for findability does not spawn a version and
drop it back to `DRAFT`.

Archiving a tag (`archive_tag` / `restore_tag`) takes it out of the pickers but keeps every link:
filtering by an archived tag still returns the templates carrying it. `delete_tag` is the
irreversible one — it removes the label from the templates too.

Text with nothing sluggable in it (`"  "`, `"!!!"`) raises `ManagedTemplateInvalidTagError` at the
call site, rather than becoming a tag no filter can ever name.

## Filtering and pagination

Filters are plain dicts, typed by the TypedDicts in `filters.py`, and compose with `and` / `or` /
`not`:

```python
service.get_filtered_templates({
    "and": [
        {"status": {"lookup": "in", "value": [ManagedTemplateStatus.ACTIVE]}},
        {"name": {"lookup": "includes", "value": "welcome", "case_sensitive": False}},
        {"includes_any_of_tags": ["onboarding", "transactional"]},
        {"created_at_range": {"from": datetime(2026, 1, 1)}},
    ]
})

service.get_paginated_filtered_templates(filters, page=1, page_size=20)  # page is 1-indexed
```

Fields: `name`, `description`, `key`, `version`, `template_managed_backend`, `status`,
`created_at_range`, `updated_at_range`, `includes_all_tags`, `includes_any_of_tags`,
`most_recent_active_version`. String lookups are `exact` / `starts_with` / `ends_with` /
`includes`; numeric ones are `gt` / `gte` / `lt` / `lte`.

### One row per key: `most_recent_active_version`

The store holds a row per *version*, so an unfiltered read shows a template once for every
version it has ever had. `most_recent_active_version` collapses that to one row per key — the
highest-numbered `ACTIVE` or `DRAFT` version, which is what is live plus the draft on its way to
replacing it. A key whose versions are all `INACTIVE` or `ARCHIVED` has no current version and
drops out.

```python
service.get_all_templates()                          # one row per key — the current version
service.get_all_templates(include_all_versions=True) # every version of every key
service.get_paginated_templates(page=1, page_size=20)              # same default
service.get_filtered_templates({"most_recent_active_version": True})  # the filter itself
```

**The two listing methods apply it by default**; pass `include_all_versions=True` for the raw
read. `get_filtered_templates` and `get_paginated_filtered_templates` do *not* add it — a filter
means what it says — so name the field yourself when a filtered listing should be one row per key
too. `False` is the exact complement (every other row, retired keys included), the same set
`{"not": {"most_recent_active_version": True}}` returns.

Unlike every other field, this one is answered against the whole key rather than against the row
being tested, so a backend evaluates it with a subquery over the key's other versions.

`validate_filter` runs before every filtered read and raises `ManagedTemplateInvalidFilterError`
for a typo'd field name, an `and`/`or` that is not a non-empty list, a logical group with sibling
keys, a tag filter given as a bare string (which would otherwise be iterated character by
character and silently match nothing), or a non-boolean `most_recent_active_version` (the string
`"false"` is truthy, so it would ask for exactly what the caller meant to switch off). Lookup
*values* stay the backend's authority.

The empty-collection rules follow Python's own `all()` / `any()`: an empty `includes_all_tags`
constrains nothing, an empty `includes_any_of_tags` matches nothing.

## Rendering a specific version

`ManagedTemplateRenderer.render` resolves a key to whatever version the backend hands back, which
is what you want at send time. The service's `render` takes an explicit version instead:

```python
service.render(notification, context, version=3)      # preview an unpublished draft
service.render_template(notification, template, context)  # a template already in hand, no read
```

That is how you preview a draft before publishing it, or re-render an old notification against the
version that was live when it was sent.

## Implementing a manager backend

Subclass `BaseTemplateManagerBackend` and implement every abstract method. It splits into four
groups:

* **Versions** — `create_template`, `get_template`, `update_template`, `delete_template`
* **Statuses** — `create_template_status_update`, `get_template_status_history`
* **Tags** — `get_or_create_tags`, `create_tag`, `get_tag`, `update_tag`, `set_tag_status`,
  `delete_tag`, `get_tags`, `get_template_tags`, `set_template_tags`
* **Queries** — `get_all_templates`, `get_templates_by_status`, `get_filtered_templates`,
  `get_paginated_templates`, `get_paginated_filtered_templates`

What a backend owns, beyond storage: assigning version numbers, deriving tag slugs with
`slugify_tag` and keeping them unique with `next_available_slug`, and translating the filter dicts
into its own query language.

`tests/fakes.py` in this repo has `InMemoryTemplateManagerBackend`, a complete, dependency-free
implementation of the seam — the shortest readable reference for what each method owes its caller.
The suite drives it end to end rather than mocking it, so it is a real implementation, not a stub.

### Exceptions

All of them subclass `ManagedTemplateError`:

| Exception | Raised when |
|---|---|
| `ManagedTemplateNotFoundError` | The key, or that version of it, does not exist |
| `ManagedTemplateInvalidFilterError` | A filter is malformed or names an unknown field |
| `ManagedTemplateStatusTransitionError` | The status move is not allowed from the current status |
| `ManagedTemplateChangeUserNotFoundError` | An update names a `changed_by` user that does not exist |
| `ManagedTemplateTagNotFoundError` | No tag has that slug |
| `ManagedTemplateTagAlreadyExistsError` | `create_tag` collides with an existing slug |
| `ManagedTemplateInvalidTagError` | A tag's text has nothing that can be slugified |

## Development

```bash
poetry install
poetry run pytest          # coverage is on by default and fails the run below 90%
poetry run ruff check
poetry run mypy
poetry run tox             # the full 3.10–3.14 matrix
```

The suite runs fully offline against the in-memory backend — no database, no services.
