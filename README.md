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
| `composition.TemplateComposer` | Resolves template inheritance and inclusion against the store, before the engine runs. |
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

## Composition: bases, blocks and includes

A file-based renderer gets composition for free. Django's `{% extends %}` and Jinja's
`{% include %}` hand a *name* to a loader, and a loader reads files — so the header, the footer and
the wrapper every email shares live in one file that every other file points at.

Managed templates are not files. They reach the engine as source, so a loader has nothing to
resolve and those tags have nothing to load. Without composition the shared chrome would have to be
pasted into every row in the store, and changing the footer would mean editing all of them.

This package resolves its own set of tags **before** the engine sees anything. What the engine
receives is one flat string with no `managed_*` tag left in it; its own syntax is untouched.

```python
service.create_template(ManagedTemplateCreateInput(
    name="Base email", description="The wrapper every email uses", key="base-email",
    template_managed_backend="django",
    template_body=(
        "<html>\n"
        "  <body>\n"
        "    {% managed_block header %}<h1>Acme</h1>{% managed_endblock %}\n"
        "    {% managed_children %}\n"
        "    {% managed_include \"footer\" %}\n"
        "  </body>\n"
        "</html>"
    ),
    template_subject="[Acme] {% managed_children %}",
    template_preheader=None, tenant=None,
))

service.create_template(ManagedTemplateCreateInput(
    name="Welcome email", description="Sent right after signup", key="welcome",
    template_managed_backend="django",
    template_body=(
        "{% managed_extends \"base-email\" %}\n"
        "{% managed_block header %}<h1>Welcome!</h1>{% managed_endblock %}\n"
        "<p>Hi {{ name }}, welcome aboard.</p>"
    ),
    template_subject="{% managed_extends \"base-email\" %}Welcome aboard",
    template_preheader=None, tenant=None,
))
```

`welcome` now renders inside the base, with its own header and the shared footer, and its subject
comes out as `[Acme] Welcome aboard`. `{{ name }}` is never looked at — the context is the engine's
business.

### The tags

| Tag | What it does |
|---|---|
| `{% managed_extends "key" %}` | This template is a child of `key`. At most one per template, never inside a block. Pin the parent with `version=2`. |
| `{% managed_children %}` | In a base: where the child's content goes. Rendered with no child, the hole is simply empty. |
| `{% managed_block name %}…{% managed_endblock %}` | A named region a child may replace. Unreplaced, it renders what it was declared with. Blocks may nest. |
| `{% managed_super %}` | Inside a child's block: the content it is overriding. Chains through as many levels of inheritance as there are. |
| `{% managed_include "key" %}` | Splice another template in here. It is composed in full first, so an include may itself extend and include. Pins the same way: `version=7`. |

Everything a child writes **outside** a block is its children content, and it lands in the base's
`{% managed_children %}`. So a child can both fill the hole and override named regions — which is
the one difference from Django, where content outside a block in a child template is discarded.

The `managed_` prefix is reserved: an unknown `{% managed_something %}` is an error rather than
text passed through, so a typo surfaces at edit time instead of shipping. Change the prefix by
handing the renderer or the service its own composer:

```python
from vintasend_managed_templates.composition import TemplateComposer

composer = TemplateComposer.from_backend(manager_backend, tag_prefix="tpl_")
renderer = ManagedTemplateEmailRenderer(manager_backend, inner_renderer, composer=composer)
```

### One field at a time

A template carries three sources — body, subject and preheader — and each composes against the
**same field** of the template it references. A child's body extends the base's body; its subject
extends the base's subject. So a base can define a subject prefix and a body wrapper at once, and
neither leaks into the other. A field the base leaves empty composes to nothing rather than to an
error.

### Whitespace

A structural tag (`extends`, `block`, `endblock`) alone on its line is taken out *with* the line,
so a layout written across several lines does not compose into one padded with blank ones. The
placeholder tags (`children`, `include`, `super`) are never line-trimmed: what replaces them lands
exactly where the tag stood, indentation and all.

### Abstract templates

A template is *abstract* when it declares a `{% managed_children %}` hole, or declares blocks
without extending anything — a layout meant to be built on rather than sent. That is a fact about
the source, so it follows the template as it is edited: a template becomes abstract the moment
someone writes the hole into it and stops being abstract the moment they take it out.

**The check** recomputes from the source every time, which makes it the authority:

```python
service.is_abstract(base)      # True
service.is_abstract(welcome)   # False
```

**The flag** is that same answer, denormalized onto the template so it can be queried:

```python
base.is_abstract                                          # True -- stored, not recomputed
service.get_filtered_templates({"is_abstract": False})    # every sendable template
```

Filtering is the reason the flag exists. Without it, a picker that has to leave the bases out would
read and parse every row in the store to draw one page. Nobody writes the flag — there is no field
for it on either write input — because a stored copy that disagreed with the source would be a lie
a filter goes on repeating. It is a **backend's job to derive it on every write** with
`composition.is_abstract`; see [Implementing a manager backend](#implementing-a-manager-backend).

Reach for the check when the flag cannot be trusted: a template edited in memory since it was read,
or one written before its backend maintained the column.

Composing an abstract template directly is allowed and gives you the layout with an empty hole.
Neither the check nor the flag refuses anything — keeping bases out of a picker is the host's call.

### Versions

A reference with no version resolves the same way any other read does: to whatever version that key
currently is. Pin it when a template must keep composing against an exact parent — re-rendering an
old notification resolves the child's version explicitly, but its unpinned bases still resolve to
today's.

`version=N` is the only spelling, on both tags that take a reference:

```
{% managed_extends "base-email" version=2 %}
{% managed_include "footer" version=7 %}
```

Nothing inside the quoted key is interpreted, so a key is only ever a key — a template genuinely
named `base-email[v2]` is referenced exactly as written, with no escaping and no special case.

### Checking a template before it ships

Composition failures are this package's, not the engine's, so nothing downstream can report them.
Catch them where someone can still fix them:

```python
service.validate_composition(template)     # raises exactly what rendering would have
service.get_composed_template("welcome")   # what the engine will actually receive
service.get_template_references(template)  # the bases and fragments it names, unresolved
```

| Exception | Raised when |
|---|---|
| `ManagedTemplateCompositionSyntaxError` | A tag is malformed, unknown, or unbalanced |
| `ManagedTemplateCompositionReferenceError` | A base or fragment does not exist (also a `ManagedTemplateNotFoundError`) |
| `ManagedTemplateCompositionCycleError` | The references loop |
| `ManagedTemplateCompositionDepthError` | The chain runs past the composer's `max_depth` (25 by default) |

All four subclass `ManagedTemplateCompositionError`.

### Turning it off

Composition is on by default. A store that predates it and holds `managed_`-prefixed text meant to
reach the engine verbatim can opt out:

```python
renderer = ManagedTemplateEmailRenderer(manager_backend, inner_renderer, compose_templates=False)
service = ManagedTemplateService(manager_backend, renderer, compose_templates=False)
```

Reads are never composed either way: `get_template` hands back exactly what is stored, which is
what an editing UI needs. `get_composed_template` is the explicit way to ask for the assembled form.

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

### What the backend can answer: capabilities

Not every store can answer every filter, and a backend says which it cannot:

```python
service.get_backend_supported_filter_capabilities()
# {'logical.and': True, ..., 'fields.includesAllTags': False, ..., 'orderBy.createdAt': True}
```

Keys are camelCase dotted — `fields.templateManagedBackend`, `orderBy.createdAt` — even though
the Python field names are snake_case, because a report goes on the wire and is spelled
identically in the TypeScript sibling. A backend declares only what it *cannot* do, and the
service merges its report over `DEFAULT_TEMPLATE_BACKEND_FILTER_CAPABILITIES`, so a filter added
in a later release does not force every backend to re-declare it.

**The service acts on the report.** Before every filtered read, the parts the backend has
declined are pruned away — a capability map nothing acts on is decoration, and every caller left
to walk it themselves would reach a slightly different conclusion.

Dropping only ever **widens**: a pruned filter matches everything the original matched and
possibly more, so you see extra rows rather than missing ones. Every corner follows from that
one rule — an `or` is dropped whole (removing a branch of a disjunction *narrows* it), and a
`not` whose inside pruned away is dropped (negating "everything" is "nothing"). A bare string
counts as `exact` *and* case-sensitive, so a backend lacking either cannot answer it.

### Ordering

`get_paginated_templates` and `get_paginated_filtered_templates` take an optional `order_by`:

```python
service.get_paginated_templates(
    page=1, page_size=20, order_by={"field": "name", "direction": "asc"}
)
```

Orderable fields are `key`, `name`, `version`, `status`, `created_at` and `updated_at` — each a
scalar a backend already stores per row, so a store can answer it from an index. Tags are absent
because ordering by a many-to-many has no single value to compare, and
`most_recent_active_version` because it is a filter rather than a field.

**Ask before you send.** Every `orderBy.*` capability defaults to `False`, so a backend that
predates ordering — or simply cannot sort — reports nothing orderable:

```python
service.get_supported_order_by_fields()   # e.g. ['key', 'name', 'created_at']
```

**Filters are dropped; orders are refused.** An order the backend cannot apply raises
`ManagedTemplateUnsupportedOrderingError` rather than being silently ignored:

| | Unsupported filter | Unsupported order |
|---|---|---|
| What happens | pruned away, the call succeeds | raises |
| If it were ignored | more rows than you asked for | the same rows, arbitrary sequence |
| Can you tell? | yes — visible in the rows | **no** |

`sort_templates(templates, order_by)` is the shared comparator, for a backend that reads a
complete set anyway. It is total and stable: ties break on `(key, version)`, and neither the
tiebreak nor the placement of absent values flips with the direction — either would let a page
boundary move between two requests and drop or repeat a row. Strings compare by code point
rather than by locale, so two machines serving two pages of one listing cannot disagree.

## Rendering a specific version

Which version of a template a notification renders is decided in this order:

1. an explicit `version=` argument to `service.render()`,
2. the notification's own `requested_template_version`,
3. whatever version the backend considers current.

```python
service.render(notification, context)                 # the notification's pin, or the latest
service.render(notification, context, version=3)      # preview v3 regardless of the pin
service.render_template(notification, template, context)   # a template already in hand
```

The argument is for rendering a version the notification is *not* pinned to -- previewing an
unpublished draft, or reproducing what an old notification looked like. Leave it off and you get
what a real send would produce.

### Pinning a notification to a version

`ManagedTemplateRenderer` reads `requested_template_version` off the notification, so a
notification recorded against v3 renders v3 however many versions follow. That field is
vintasend's, not this package's -- see
[Template Version Pinning](https://github.com/vintasoftware/vintasend#template-version-pinning)
-- and this package is what makes it mean anything:

```python
notification_service.create_notification(
    ...,
    body_template="welcome",           # the template key
    requested_template_version=3,      # render v3, now and forever
)

# Or pin to whatever version is current at this moment, without naming it:
notification_service.create_notification(..., pin_template_versions=True)

# The same, as the default for every call on the service:
notification_service = NotificationService(..., pin_template_versions=True)
```

With `pin_template_versions=True`, the service asks this package's renderer for the current
version through `get_latest_template_version()`, which resolves it the same way `get_template(key)`
does. A key with nothing behind it answers `None` and the notification is created unpinned, rather
than failing the creation over a template that may well exist by the time it is sent.

Whichever version renders is reported back on the send input as `template_version`, so vintasend
can store it as `used_template_version`. For an unpinned notification that is the only record of
which version went out, since the template has moved on by the time anyone asks.

## Implementing a manager backend

Subclass `BaseTemplateManagerBackend` and implement every abstract method. It splits into four
groups:

* **Versions** — `create_template`, `get_template`, `update_template`, `delete_template`
* **Statuses** — `create_template_status_update`, `get_template_status_history`
* **Tags** — `get_or_create_tags`, `create_tag`, `get_tag`, `update_tag`, `set_tag_status`,
  `delete_tag`, `get_tags`, `get_template_tags`, `set_template_tags`
* **Queries** — `get_all_templates`, `get_templates_by_status`, `get_filtered_templates`,
  `get_paginated_templates`, `get_paginated_filtered_templates`

The two paginated methods take an optional `order_by`, and `get_filter_capabilities` is concrete
rather than abstract, so neither breaks a backend written before they existed. See
[Ordering](#ordering).

`get_filter_capabilities` is concrete rather than abstract and returns `{}`, so a backend that
declares nothing keeps working and reads as fully capable of every *filter*. Ordering is the
exception: those keys default to `False`, so a backend that can sort has to say so —

```python
def get_filter_capabilities(self) -> dict[str, bool]:
    return {
        "logical.or": False,          # this store ANDs its query parameters
        "orderBy.key": True,
        "orderBy.createdAt": True,    # declare only what the store genuinely does
    }
```

Verify each ordering claim by **running** the sort rather than reading your store's
documentation: a store keeping versions as strings sorts 10 before 2 and looks correct until a
key reaches its tenth version. A backend that accepts `order_by` must apply it to the whole
result set *before* paging — sorting a page after it has been chosen orders rows within the page
while the rows selected for it came back in the store's own order, which is right on page 1 and
wrong on every page after.

What a backend owns, beyond storage: assigning version numbers, deriving tag slugs with
`slugify_tag` and keeping them unique with `next_available_slug`, deriving `is_abstract` with
`composition.is_abstract` on every write that touches a source, and translating the filter dicts
into its own query language.

`is_abstract` is the one easy to miss, because no method is named for it. It is a denormalization
of the template's own source, kept so the `is_abstract` filter can be a column lookup instead of a
full-store parse, and neither write input carries it. A backend that never sets it reports every
template as concrete and that filter quietly stops working.

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
| `ManagedTemplateCompositionError` | A template could not be assembled -- see [Composition](#composition-bases-blocks-and-includes) for its four subclasses |

## Development

```bash
poetry install
poetry run pytest          # coverage is on by default and fails the run below 90%
poetry run ruff check
poetry run mypy
poetry run tox             # the full 3.10–3.14 matrix
```

The suite runs fully offline against the in-memory backend — no database, no services.
