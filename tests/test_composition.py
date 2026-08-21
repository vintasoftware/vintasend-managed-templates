"""Composition: what the engine ends up with once inheritance and inclusion are resolved.

Most tests here drive ``TemplateComposer`` against a dict of templates, because what is under
test is the tag language rather than any store. The last two sections go through the renderer
and the service, which is where composition actually happens at send time.
"""

import dataclasses
import datetime

import pytest
from vintasend.services.notification_template_renderers.base_templated_email_renderer import (
    TemplatedEmail,
)

from vintasend_managed_templates.composition import (
    DEFAULT_TAG_PREFIX,
    TemplateComposer,
    TemplateReference,
    is_abstract,
)
from vintasend_managed_templates.constants import ManagedTemplateStatus
from vintasend_managed_templates.dataclasses import ManagedTemplate
from vintasend_managed_templates.exceptions import (
    ManagedTemplateCompositionCycleError,
    ManagedTemplateCompositionDepthError,
    ManagedTemplateCompositionError,
    ManagedTemplateCompositionReferenceError,
    ManagedTemplateCompositionSyntaxError,
    ManagedTemplateInvalidFilterError,
    ManagedTemplateNotFoundError,
)
from vintasend_managed_templates.managed_template_renderer import ManagedTemplateEmailRenderer
from vintasend_managed_templates.managed_template_service import ManagedTemplateService

from .fakes import (
    RecordingEmailRenderer,
    make_create_input,
    make_notification,
    make_update_input,
)


class EchoEmailRenderer(RecordingEmailRenderer):
    """Hands the template content back untouched, braces and all.

    ``RecordingEmailRenderer`` runs the body through ``str.format``, which is fine for a
    composed template and impossible for an uncomposed one -- ``{%`` is not valid format
    syntax. The tests that assert a template reached the engine *uncomposed* use this instead.
    """

    def render_from_template_content(self, notification, template_content, context, **kwargs):
        self.render_from_content_calls.append(template_content)
        return TemplatedEmail(
            subject=template_content.subject_template,
            body=template_content.body_template,
            preheader=template_content.preheader_template,
        )


def make_template(
    key: str,
    body: str = "",
    *,
    subject: str | None = None,
    preheader: str | None = None,
    version: int = 1,
) -> ManagedTemplate:
    return ManagedTemplate(
        id=f"{key}-{version}",
        name=key,
        description="",
        key=key,
        template_managed_backend="in-memory",
        body_template=body,
        version=version,
        status=ManagedTemplateStatus.ACTIVE,
        created=datetime.datetime(2026, 1, 1),
        updated=datetime.datetime(2026, 1, 1),
        subject_template=subject,
        preheader_template=preheader,
    )


class DictStore:
    """The smallest thing that can stand in for a backend's ``get_template``."""

    def __init__(self, *templates: ManagedTemplate) -> None:
        self.templates: dict[tuple[str, int], ManagedTemplate] = {
            (template.key, template.version): template for template in templates
        }
        # Every (key, version) asked for, so a test can assert what was read and what was not.
        self.reads: list[tuple[str, int | None]] = []

    def add(self, template: ManagedTemplate) -> ManagedTemplate:
        self.templates[(template.key, template.version)] = template
        return template

    def get_template(self, template_key: str, version: int | None = None) -> ManagedTemplate:
        self.reads.append((template_key, version))
        matches = [template for (key, _), template in self.templates.items() if key == template_key]
        if version is not None:
            matches = [template for template in matches if template.version == version]
        if not matches:
            raise ManagedTemplateNotFoundError(template_key)
        return max(matches, key=lambda template: template.version)


@pytest.fixture
def store() -> DictStore:
    return DictStore()


@pytest.fixture
def composer(store) -> TemplateComposer:
    return TemplateComposer(store.get_template)


# ----------------------------------------------------------------------
# Inheritance
# ----------------------------------------------------------------------


def test_a_child_fills_the_hole_its_parent_left(store, composer):
    store.add(make_template("base", "<body>{% managed_children %}</body>"))
    child = store.add(make_template("welcome", '{% managed_extends "base" %}<p>Hi</p>'))

    assert composer.compose(child).body_template == "<body><p>Hi</p></body>"


def test_a_parent_with_no_child_renders_its_hole_as_nothing(store, composer):
    base = store.add(make_template("base", "<body>{% managed_children %}</body>"))

    assert composer.compose(base).body_template == "<body></body>"


def test_a_block_renders_what_it_was_declared_with_when_nothing_overrides_it(store, composer):
    store.add(make_template("base", "{% managed_block title %}Acme{% managed_endblock %}!"))
    child = store.add(make_template("welcome", '{% managed_extends "base" %}'))

    assert composer.compose(child).body_template == "Acme!"


def test_a_childs_block_wins_over_its_parents(store, composer):
    store.add(make_template("base", "{% managed_block title %}Acme{% managed_endblock %}!"))
    child = store.add(
        make_template(
            "welcome",
            '{% managed_extends "base" %}{% managed_block title %}Hi{% managed_endblock %}',
        )
    )

    assert composer.compose(child).body_template == "Hi!"


def test_blocks_and_children_compose_together(store, composer):
    store.add(
        make_template(
            "base",
            "[{% managed_block title %}Acme{% managed_endblock %}]{% managed_children %}",
        )
    )
    child = store.add(
        make_template(
            "welcome",
            '{% managed_extends "base" %}'
            "{% managed_block title %}Hi{% managed_endblock %}<p>body</p>",
        )
    )

    assert composer.compose(child).body_template == "[Hi]<p>body</p>"


def test_a_block_a_parent_never_declared_is_dropped(store, composer):
    store.add(make_template("base", "[{% managed_children %}]"))
    child = store.add(
        make_template(
            "welcome",
            '{% managed_extends "base" %}{% managed_block nowhere %}x{% managed_endblock %}body',
        )
    )

    assert composer.compose(child).body_template == "[body]"


def test_a_nested_block_can_be_overridden_on_its_own(store, composer):
    store.add(
        make_template(
            "base",
            "{% managed_block outer %}<div>"
            "{% managed_block inner %}in{% managed_endblock %}"
            "</div>{% managed_endblock %}",
        )
    )
    child = store.add(
        make_template(
            "welcome",
            '{% managed_extends "base" %}{% managed_block inner %}INNER{% managed_endblock %}',
        )
    )

    assert composer.compose(child).body_template == "<div>INNER</div>"


def test_inheritance_chains_through_three_levels(store, composer):
    store.add(make_template("base", "<html>{% managed_children %}</html>"))
    store.add(
        make_template("layout", '{% managed_extends "base" %}<body>{% managed_children %}</body>')
    )
    child = store.add(make_template("welcome", '{% managed_extends "layout" %}<p>Hi</p>'))

    assert composer.compose(child).body_template == "<html><body><p>Hi</p></body></html>"


def test_super_pulls_in_the_block_being_overridden(store, composer):
    store.add(make_template("base", "{% managed_block title %}Acme{% managed_endblock %}"))
    child = store.add(
        make_template(
            "welcome",
            '{% managed_extends "base" %}'
            "{% managed_block title %}{% managed_super %} — Welcome{% managed_endblock %}",
        )
    )

    assert composer.compose(child).body_template == "Acme — Welcome"


def test_super_walks_down_every_level_that_declared_the_block(store, composer):
    store.add(make_template("base", "{% managed_block trail %}a{% managed_endblock %}"))
    store.add(
        make_template(
            "layout",
            '{% managed_extends "base" %}'
            "{% managed_block trail %}{% managed_super %}b{% managed_endblock %}",
        )
    )
    child = store.add(
        make_template(
            "welcome",
            '{% managed_extends "layout" %}'
            "{% managed_block trail %}{% managed_super %}c{% managed_endblock %}",
        )
    )

    assert composer.compose(child).body_template == "abc"


def test_super_with_nothing_underneath_it_renders_nothing(store, composer):
    base = store.add(
        make_template(
            "base", "[{% managed_block title %}{% managed_super %}{% managed_endblock %}]"
        )
    )

    assert composer.compose(base).body_template == "[]"


def test_a_block_that_declares_its_own_name_does_not_recurse(store, composer):
    """A child block containing a block of the same name renders once, not forever."""
    store.add(make_template("base", "{% managed_block title %}base{% managed_endblock %}"))
    child = store.add(
        make_template(
            "welcome",
            '{% managed_extends "base" %}'
            "{% managed_block title %}<h1>{% managed_super %}</h1>{% managed_endblock %}",
        )
    )

    assert composer.compose(child).body_template == "<h1>base</h1>"


# ----------------------------------------------------------------------
# Inclusion
# ----------------------------------------------------------------------


def test_an_include_splices_another_template_in(store, composer):
    store.add(make_template("footer", "<footer>bye</footer>"))
    page = store.add(make_template("page", 'body{% managed_include "footer" %}'))

    assert composer.compose(page).body_template == "body<footer>bye</footer>"


def test_an_include_is_composed_before_it_is_spliced(store, composer):
    store.add(make_template("wrapper", "<i>{% managed_children %}</i>"))
    store.add(make_template("footer", '{% managed_extends "wrapper" %}bye'))
    page = store.add(make_template("page", '{% managed_include "footer" %}'))

    assert composer.compose(page).body_template == "<i>bye</i>"


def test_an_include_inside_a_block_is_resolved(store, composer):
    store.add(make_template("logo", "[logo]"))
    store.add(
        make_template(
            "base",
            '{% managed_block head %}{% managed_include "logo" %}{% managed_endblock %}',
        )
    )
    child = store.add(make_template("welcome", '{% managed_extends "base" %}'))

    assert composer.compose(child).body_template == "[logo]"


def test_the_same_template_can_be_included_twice(store, composer):
    store.add(make_template("rule", "<hr>"))
    page = store.add(
        make_template("page", 'a{% managed_include "rule" %}b{% managed_include "rule" %}c')
    )

    assert composer.compose(page).body_template == "a<hr>b<hr>c"


# ----------------------------------------------------------------------
# Fields and versions
# ----------------------------------------------------------------------


def test_each_field_composes_against_the_same_field_of_its_parent(store, composer):
    store.add(
        make_template(
            "base",
            "<body>{% managed_children %}</body>",
            subject="[Acme] {% managed_children %}",
            preheader="pre: {% managed_children %}",
        )
    )
    child = store.add(
        make_template(
            "welcome",
            '{% managed_extends "base" %}<p>Hi</p>',
            subject='{% managed_extends "base" %}Welcome',
            preheader='{% managed_extends "base" %}now',
        )
    )

    composed = composer.compose(child)

    assert composed.body_template == "<body><p>Hi</p></body>"
    assert composed.subject_template == "[Acme] Welcome"
    assert composed.preheader_template == "pre: now"


def test_a_field_the_parent_leaves_empty_composes_to_nothing(store, composer):
    store.add(make_template("base", "<body>{% managed_children %}</body>", subject=None))
    child = store.add(make_template("welcome", "body", subject='{% managed_extends "base" %}Hi'))

    assert composer.compose(child).subject_template == ""


def test_a_field_with_no_source_is_left_alone(store, composer):
    child = store.add(make_template("welcome", "body", subject=None, preheader=""))

    composed = composer.compose(child)

    assert composed.subject_template is None
    assert composed.preheader_template == ""


def test_a_reference_resolves_to_the_latest_version_by_default(store, composer):
    store.add(make_template("base", "v1:{% managed_children %}", version=1))
    store.add(make_template("base", "v2:{% managed_children %}", version=2))
    child = store.add(make_template("welcome", '{% managed_extends "base" %}Hi'))

    assert composer.compose(child).body_template == "v2:Hi"
    assert ("base", None) in store.reads


def test_a_reference_can_pin_the_version_it_composes_against(store, composer):
    store.add(make_template("base", "v1:{% managed_children %}", version=1))
    store.add(make_template("base", "v2:{% managed_children %}", version=2))
    child = store.add(make_template("welcome", '{% managed_extends "base" version=1 %}Hi'))

    assert composer.compose(child).body_template == "v1:Hi"
    assert ("base", 1) in store.reads


def test_a_template_with_nothing_to_compose_is_handed_straight_back(store, composer):
    plain = store.add(make_template("plain", "<p>{{ name }}</p>", subject="Hi"))

    assert composer.compose(plain) is plain


def test_composing_never_touches_the_template_it_was_given(store, composer):
    store.add(make_template("base", "[{% managed_children %}]"))
    child = store.add(make_template("welcome", '{% managed_extends "base" %}Hi'))
    before = dataclasses.replace(child)

    composer.compose(child)

    assert child == before


def test_engine_syntax_is_carried_through_untouched(store, composer):
    store.add(make_template("base", "{% block django %}{% managed_children %}{% endblock %}"))
    child = store.add(
        make_template("welcome", '{% managed_extends "base" %}{% if x %}{{ y|safe }}{% endif %}')
    )

    assert composer.compose(child).body_template == (
        "{% block django %}{% if x %}{{ y|safe }}{% endif %}{% endblock %}"
    )


# ----------------------------------------------------------------------
# Whitespace
# ----------------------------------------------------------------------


def test_a_structural_tag_alone_on_its_line_takes_the_line_with_it(store, composer):
    store.add(
        make_template(
            "base",
            "<html>\n"
            "    {% managed_block body %}\n"
            "    <p>default</p>\n"
            "    {% managed_endblock %}\n"
            "</html>",
        )
    )
    child = store.add(make_template("welcome", '{% managed_extends "base" %}\n'))

    assert composer.compose(child).body_template == "<html>\n    <p>default</p>\n</html>"


def test_a_placeholder_tag_is_replaced_where_it_stands(store, composer):
    store.add(make_template("base", "<html>\n  {% managed_children %}\n</html>"))
    child = store.add(make_template("welcome", '{% managed_extends "base" %}<p>Hi</p>'))

    assert composer.compose(child).body_template == "<html>\n  <p>Hi</p>\n</html>"


def test_a_tag_with_content_beside_it_leaves_that_content_alone(store, composer):
    store.add(make_template("base", "a {% managed_children %} b"))
    child = store.add(make_template("welcome", '{% managed_extends "base" %}Hi'))

    assert composer.compose(child).body_template == "a Hi b"


# ----------------------------------------------------------------------
# Broken references
# ----------------------------------------------------------------------


def test_extending_a_template_that_does_not_exist_names_it(store, composer):
    child = store.add(make_template("welcome", '{% managed_extends "nope" %}Hi'))

    with pytest.raises(ManagedTemplateCompositionReferenceError) as error:
        composer.compose(child)

    assert "'nope'" in str(error.value)
    assert "'welcome' v1" in str(error.value)


def test_a_missing_reference_is_also_a_missing_template(store, composer):
    child = store.add(make_template("welcome", '{% managed_include "nope" %}'))

    with pytest.raises(ManagedTemplateNotFoundError):
        composer.compose(child)


def test_a_missing_pinned_version_says_which_version(store, composer):
    store.add(make_template("base", "[{% managed_children %}]", version=1))
    child = store.add(make_template("welcome", '{% managed_extends "base" version=7 %}Hi'))

    with pytest.raises(ManagedTemplateCompositionReferenceError, match="v7"):
        composer.compose(child)


def test_a_composer_with_no_resolver_says_so(store):
    child = make_template("welcome", '{% managed_extends "base" %}Hi')

    with pytest.raises(ManagedTemplateCompositionReferenceError, match="resolver"):
        TemplateComposer().compose(child)


def test_a_composer_with_no_resolver_still_parses(store):
    plain = make_template("welcome", "{% managed_block a %}x{% managed_endblock %}")

    assert TemplateComposer().compose(plain).body_template == "x"


# ----------------------------------------------------------------------
# Cycles and depth
# ----------------------------------------------------------------------


def test_two_templates_extending_each_other_are_a_cycle(store, composer):
    first = store.add(make_template("first", '{% managed_extends "second" %}'))
    store.add(make_template("second", '{% managed_extends "first" %}'))

    with pytest.raises(ManagedTemplateCompositionCycleError) as error:
        composer.compose(first)

    assert "'first' v1 -> 'second' v1 -> 'first' v1" in str(error.value)


def test_a_template_that_includes_itself_is_a_cycle(store, composer):
    page = store.add(make_template("page", 'a{% managed_include "page" %}'))

    with pytest.raises(ManagedTemplateCompositionCycleError):
        composer.compose(page)


def test_a_cycle_reached_through_a_pinned_version_is_still_a_cycle(store, composer):
    first = store.add(make_template("first", '{% managed_extends "second" %}'))
    store.add(make_template("second", '{% managed_extends "first" version=1 %}'))

    with pytest.raises(ManagedTemplateCompositionCycleError):
        composer.compose(first)


def test_a_chain_longer_than_max_depth_is_refused(store):
    depth = 4
    store.add(make_template("level-0", "end"))
    for level in range(1, depth + 3):
        store.add(
            make_template(f"level-{level}", '{% managed_extends "level-' + str(level - 1) + '" %}')
        )
    composer = TemplateComposer(store.get_template, max_depth=depth)

    with pytest.raises(ManagedTemplateCompositionDepthError, match=str(depth)):
        composer.compose(store.templates[(f"level-{depth + 2}", 1)])


# ----------------------------------------------------------------------
# Malformed tags
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("{% managed_block %}x{% managed_endblock %}", "takes a name"),
        ("{% managed_block 9lives %}x{% managed_endblock %}", "takes a name"),
        ("{% managed_block a %}x", "never closed"),
        ("{% managed_endblock %}", "no block open"),
        (
            "{% managed_block a %}x{% managed_endblock b %}",
            "the open block is 'a'",
        ),
        (
            "{% managed_block a %}x{% managed_endblock %}{% managed_block a %}y"
            "{% managed_endblock %}",
            "declared twice",
        ),
        ("{% managed_extends base %}", "quoted template key"),
        ("{% managed_extends %}", "quoted template key"),
        ('{% managed_extends "" %}', "quoted template key"),
        ('{% managed_include "a" version=x %}', "quoted template key"),
        ('{% managed_extends "a" %}{% managed_extends "b" %}', "only {% managed_extends %} one"),
        (
            '{% managed_block a %}{% managed_extends "b" %}{% managed_endblock %}',
            "cannot appear inside a block",
        ),
        ("{% managed_children yes %}", "takes no arguments"),
        ("{% managed_super loud %}", "takes no arguments"),
        ("{% managed_frobnicate %}", "unknown composition tag"),
    ],
)
def test_a_malformed_tag_is_reported_with_what_is_wrong(store, composer, source, message):
    template = store.add(make_template("broken", source))

    with pytest.raises(ManagedTemplateCompositionSyntaxError) as error:
        composer.compose(template)

    assert message in str(error.value)
    assert "body_template" in str(error.value)


def test_every_composition_failure_shares_one_base_class(store, composer):
    template = store.add(make_template("broken", "{% managed_endblock %}"))

    with pytest.raises(ManagedTemplateCompositionError):
        composer.compose(template)


def test_a_quoted_block_name_is_accepted(store, composer):
    template = store.add(make_template("t", '{% managed_block "a" %}x{% managed_endblock "a" %}'))

    assert composer.compose(template).body_template == "x"


def test_single_quotes_name_a_template_too(store, composer):
    store.add(make_template("footer", "bye"))
    page = store.add(make_template("page", "{% managed_include 'footer' %}"))

    assert composer.compose(page).body_template == "bye"


# ----------------------------------------------------------------------
# Inspecting a template
# ----------------------------------------------------------------------


def test_references_lists_what_a_template_names(store, composer):
    template = make_template(
        "welcome",
        '{% managed_extends "base" %}{% managed_include "footer" version=3 %}',
        subject='{% managed_include "prefix" %}',
    )

    assert composer.references(template) == [
        TemplateReference("extends", "base", None, "body_template"),
        TemplateReference("include", "footer", 3, "body_template"),
        TemplateReference("include", "prefix", None, "subject_template"),
    ]


def test_references_needs_none_of_them_to_exist(store, composer):
    template = make_template("welcome", '{% managed_extends "missing" %}')

    assert composer.references(template)[0].key == "missing"


def test_references_of_a_plain_template_is_empty(store, composer):
    assert composer.references(make_template("plain", "hi", subject="hi")) == []


def test_a_template_with_a_hole_is_abstract(composer):
    assert composer.is_abstract(make_template("base", "<body>{% managed_children %}</body>"))


def test_a_template_with_blocks_and_no_parent_is_abstract(composer):
    assert composer.is_abstract(
        make_template("base", "{% managed_block a %}x{% managed_endblock %}")
    )


def test_a_template_that_only_overrides_blocks_is_not_abstract(composer):
    assert not composer.is_abstract(
        make_template(
            "welcome",
            '{% managed_extends "base" %}{% managed_block a %}x{% managed_endblock %}',
        )
    )


def test_a_plain_template_is_not_abstract(composer):
    assert not composer.is_abstract(make_template("plain", "<p>hi</p>", subject="hi"))


def test_abstractness_is_read_off_any_field(composer):
    assert composer.is_abstract(make_template("base", "body", subject="{% managed_children %}"))


def test_is_abstract_answers_without_a_store():
    assert is_abstract(make_template("base", "{% managed_children %}"))
    assert not is_abstract(make_template("plain", "hi"))


# ----------------------------------------------------------------------
# Composing loose source
# ----------------------------------------------------------------------


def test_source_can_be_composed_before_it_is_stored(store, composer):
    store.add(make_template("base", "[{% managed_children %}]"))

    assert composer.compose_source('{% managed_extends "base" %}draft') == "[draft]"


def test_source_with_no_key_is_named_only_by_its_field(store, composer):
    with pytest.raises(ManagedTemplateCompositionSyntaxError) as error:
        composer.compose_source("{% managed_endblock %}")

    assert "(in body_template)" in str(error.value)


def test_source_composes_against_the_field_it_is_told_it_is(store, composer):
    store.add(make_template("base", "body", subject="[{% managed_children %}]"))

    composed = composer.compose_source('{% managed_extends "base" %}Hi', field="subject_template")

    assert composed == "[Hi]"


def test_source_that_leads_back_to_its_own_key_is_a_cycle(store, composer):
    store.add(make_template("base", '{% managed_extends "welcome" %}'))
    store.add(make_template("welcome", "the stored, now stale, body"))

    with pytest.raises(ManagedTemplateCompositionCycleError):
        composer.compose_source('{% managed_extends "base" %}new', key="welcome", version=1)


def test_one_field_can_be_composed_on_its_own(store, composer):
    store.add(make_template("base", "[{% managed_children %}]", subject="{% managed_children %}!"))
    child = store.add(
        make_template(
            "welcome",
            '{% managed_extends "base" %}Hi',
            subject='{% managed_extends "base" %}Subject',
        )
    )

    assert composer.compose_field(child, "subject_template") == "Subject!"
    assert composer.compose_field(child, "preheader_template") is None


def test_validate_raises_what_rendering_would_have(store, composer):
    template = store.add(make_template("welcome", '{% managed_extends "nope" %}'))

    with pytest.raises(ManagedTemplateCompositionReferenceError):
        composer.validate(template)


def test_validate_says_nothing_when_a_template_is_sound(store, composer):
    store.add(make_template("base", "[{% managed_children %}]"))
    child = store.add(make_template("welcome", '{% managed_extends "base" %}Hi'))

    assert composer.validate(child) is None


# ----------------------------------------------------------------------
# A different prefix
# ----------------------------------------------------------------------


def test_the_tag_prefix_can_be_changed(store):
    store.add(make_template("base", "[{% mt_children %}]"))
    child = store.add(make_template("welcome", '{% mt_extends "base" %}Hi'))
    composer = TemplateComposer(store.get_template, tag_prefix="mt_")

    assert composer.compose(child).body_template == "[Hi]"


def test_a_changed_prefix_leaves_the_default_one_as_plain_text(store):
    child = store.add(make_template("welcome", "{% managed_children %}"))
    composer = TemplateComposer(store.get_template, tag_prefix="mt_")

    assert composer.compose(child).body_template == "{% managed_children %}"


def test_the_default_prefix_is_the_documented_one():
    assert DEFAULT_TAG_PREFIX == "managed_"


# ----------------------------------------------------------------------
# Through the renderer
# ----------------------------------------------------------------------


def test_the_renderer_composes_before_it_hands_the_template_over(backend, inner_renderer):
    backend.create_template(
        make_create_input("base", template_body="[[{% managed_children %}]]", template_subject="s")
    )
    backend.create_template(
        make_create_input(
            "welcome",
            template_body='{% managed_extends "base" %}Hi',
            template_subject="Hello",
        )
    )
    renderer = ManagedTemplateEmailRenderer(backend, inner_renderer)

    email = renderer.render(make_notification("welcome"), {})

    assert email.body == "[[Hi]]"


def test_a_renderer_told_not_to_compose_hands_the_source_over_as_stored(backend):
    backend.create_template(
        make_create_input(
            "welcome",
            template_body='{% managed_extends "base" %}Hi',
            template_subject="Hello",
        )
    )
    renderer = ManagedTemplateEmailRenderer(backend, EchoEmailRenderer(), compose_templates=False)

    email = renderer.render(make_notification("welcome"), {})

    assert email.body == '{% managed_extends "base" %}Hi'


def test_the_renderer_composes_through_its_own_backend(backend, inner_renderer):
    renderer = ManagedTemplateEmailRenderer(backend, inner_renderer)

    assert renderer.composer.resolve_template == backend.get_template


def test_a_renderer_can_be_given_its_own_composer(backend, inner_renderer, store):
    store.add(make_template("base", "<{% mt_children %}>"))
    composer = TemplateComposer(store.get_template, tag_prefix="mt_")
    renderer = ManagedTemplateEmailRenderer(backend, inner_renderer, composer=composer)
    backend.create_template(
        make_create_input(
            "welcome", template_body='{% mt_extends "base" %}Hi', template_subject="Hello"
        )
    )

    assert renderer.render(make_notification("welcome"), {}).body == "<Hi>"


# ----------------------------------------------------------------------
# Through the service
# ----------------------------------------------------------------------


def test_the_service_composes_what_it_renders(service, backend):
    backend.create_template(
        make_create_input("base", template_body="[[{% managed_children %}]]", template_subject="s")
    )
    backend.create_template(
        make_create_input(
            "welcome",
            template_body='{% managed_extends "base" %}Hi',
            template_subject="Hello",
        )
    )

    email = service.render(make_notification("welcome"), {})

    assert email.body == "[[Hi]]"


def test_get_template_still_hands_back_exactly_what_is_stored(service, backend):
    backend.create_template(make_create_input("base", template_body="[{% managed_children %}]"))
    backend.create_template(
        make_create_input("welcome", template_body='{% managed_extends "base" %}Hi')
    )

    assert service.get_template("welcome").body_template == '{% managed_extends "base" %}Hi'


def test_get_composed_template_hands_back_what_the_engine_will_see(service, backend):
    backend.create_template(make_create_input("base", template_body="[{% managed_children %}]"))
    backend.create_template(
        make_create_input("welcome", template_body='{% managed_extends "base" %}Hi')
    )

    assert service.get_composed_template("welcome").body_template == "[Hi]"


def test_get_composed_template_can_pin_a_version(service, backend):
    backend.create_template(make_create_input("base", template_body="[{% managed_children %}]"))
    backend.create_template(
        make_create_input("welcome", template_body='{% managed_extends "base" %}v1')
    )
    backend.update_template(
        "welcome", make_update_input(template_body='{% managed_extends "base" %}v2')
    )

    assert service.get_composed_template("welcome", 1).body_template == "[v1]"
    assert service.get_composed_template("welcome").body_template == "[v2]"


def test_a_service_told_not_to_compose_renders_the_source_as_stored(backend):
    renderer = ManagedTemplateEmailRenderer(backend, EchoEmailRenderer())
    service = ManagedTemplateService(backend, renderer, compose_templates=False)
    backend.create_template(
        make_create_input(
            "welcome",
            template_body='{% managed_extends "base" %}Hi',
            template_subject="Hello",
        )
    )

    assert service.render(make_notification("welcome"), {}).body == (
        '{% managed_extends "base" %}Hi'
    )


def test_the_service_composes_through_its_own_backend_not_the_renderers(backend):
    other_backend_renderer = ManagedTemplateEmailRenderer(
        DictStore(),
        RecordingEmailRenderer(),  # type: ignore[arg-type]
    )
    service = ManagedTemplateService(backend, other_backend_renderer)

    assert service.composer.resolve_template == backend.get_template


def test_the_service_validates_a_template_on_request(service, backend):
    template = backend.create_template(
        make_create_input("welcome", template_body='{% managed_extends "nope" %}')
    )

    with pytest.raises(ManagedTemplateCompositionReferenceError):
        service.validate_composition(template)


def test_the_service_lists_a_templates_references(service, backend):
    template = backend.create_template(
        make_create_input(
            "welcome",
            template_body='{% managed_extends "base" %}',
            template_subject=None,
        )
    )

    assert service.get_template_references(template) == [
        TemplateReference("extends", "base", None, "body_template")
    ]


def test_the_service_answers_whether_a_template_is_abstract(service, backend):
    base = backend.create_template(
        make_create_input("base", template_body="{% managed_children %}", template_subject=None)
    )
    welcome = backend.create_template(
        make_create_input("welcome", template_body="<p>hi</p>", template_subject=None)
    )

    assert service.is_abstract(base)
    assert not service.is_abstract(welcome)


def test_a_service_can_be_given_its_own_composer(backend, inner_renderer, store):
    composer = TemplateComposer(store.get_template, tag_prefix="mt_")
    service = ManagedTemplateService(
        backend, ManagedTemplateEmailRenderer(backend, inner_renderer), composer=composer
    )

    assert service.composer is composer


def test_one_source_can_be_asked_whether_it_is_abstract(composer):
    assert composer.source_is_abstract("{% managed_children %}")
    assert composer.source_is_abstract("[{% managed_children %}]", field="subject_template")
    assert not composer.source_is_abstract("<p>hi</p>")
    assert not composer.source_is_abstract("")


# ----------------------------------------------------------------------
# The denormalized flag
# ----------------------------------------------------------------------


def test_a_template_carries_the_flag_its_backend_derived(service, backend):
    base = backend.create_template(
        make_create_input("base", template_body="{% managed_children %}", template_subject=None)
    )
    welcome = backend.create_template(
        make_create_input("welcome", template_body="<p>hi</p>", template_subject=None)
    )

    assert base.is_abstract is True
    assert welcome.is_abstract is False


def test_the_flag_is_derived_from_any_field(backend):
    template = backend.create_template(
        make_create_input(
            "base", template_body="plain", template_subject="[{% managed_children %}]"
        )
    )

    assert template.is_abstract is True


def test_a_new_version_re_derives_the_flag_rather_than_carrying_it(backend):
    backend.create_template(
        make_create_input("base", template_body="{% managed_children %}", template_subject=None)
    )

    sendable = backend.update_template(
        "base", make_update_input(template_body="<p>no hole any more</p>")
    )

    assert sendable.is_abstract is False


def test_a_template_built_by_hand_defaults_to_concrete():
    assert make_template("welcome", "<p>hi</p>").is_abstract is False


def test_the_flag_is_what_the_filter_queries(service, backend):
    backend.create_template(
        make_create_input("base", template_body="{% managed_children %}", template_subject=None)
    )
    backend.create_template(
        make_create_input("welcome", template_body="<p>hi</p>", template_subject=None)
    )

    abstract = service.get_filtered_templates({"is_abstract": True})
    sendable = service.get_filtered_templates({"is_abstract": False})

    assert [template.key for template in abstract] == ["base"]
    assert [template.key for template in sendable] == ["welcome"]


def test_the_filter_is_rejected_when_it_is_not_a_boolean(service):
    with pytest.raises(ManagedTemplateInvalidFilterError, match="is_abstract must be a boolean"):
        service.get_filtered_templates({"is_abstract": "true"})


def test_the_check_recomputes_rather_than_reading_the_flag(service, backend):
    stored = backend.create_template(
        make_create_input("welcome", template_body="<p>hi</p>", template_subject=None)
    )
    edited = dataclasses.replace(stored, body_template="{% managed_children %}")

    assert edited.is_abstract is False
    assert service.is_abstract(edited) is True


# ----------------------------------------------------------------------
# Version pinning through the notification
# ----------------------------------------------------------------------


def test_a_parent_can_be_pinned_to_a_version(store, composer):
    store.add(make_template("base", "v1:{% managed_children %}", version=1))
    store.add(make_template("base", "v2:{% managed_children %}", version=2))
    child = store.add(make_template("welcome", '{% managed_extends "base" version=1 %}Hi'))

    assert composer.compose(child).body_template == "v1:Hi"


def test_an_unpinned_parent_resolves_to_the_current_version(store, composer):
    """The counterpart: no ``version=`` means "whatever that key is now"."""
    store.add(make_template("base", "v1:{% managed_children %}", version=1))
    store.add(make_template("base", "v2:{% managed_children %}", version=2))
    child = store.add(make_template("welcome", '{% managed_extends "base" %}Hi'))

    assert composer.compose(child).body_template == "v2:Hi"


def test_an_include_can_be_pinned_to_a_version(store, composer):
    store.add(make_template("footer", "old", version=1))
    store.add(make_template("footer", "new", version=2))
    page = store.add(make_template("page", '{% managed_include "footer" version=1 %}'))

    assert composer.compose(page).body_template == "old"


def test_the_pinned_version_is_reported_as_a_reference(composer):
    template = make_template("welcome", '{% managed_extends "base" version=2 %}')

    assert composer.references(template) == [
        TemplateReference("extends", "base", 2, "body_template")
    ]


def test_whitespace_around_the_version_is_tolerated(store, composer):
    store.add(make_template("base", "v1:{% managed_children %}", version=1))
    store.add(make_template("base", "v2:{% managed_children %}", version=2))
    child = store.add(make_template("welcome", '{% managed_extends "base" version = 1 %}Hi'))

    assert composer.compose(child).body_template == "v1:Hi"


def test_a_bracketed_version_in_the_key_is_just_part_of_the_key(store, composer):
    """``version=`` is the only spelling, so ``"base[v1]"`` names a key, not a pin.

    It resolves like any other key, and reports itself as one -- there is no reserved
    syntax inside a key any more.
    """
    template = make_template("welcome", '{% managed_extends "base[v1]" %}')

    assert composer.references(template) == [
        TemplateReference("extends", "base[v1]", None, "body_template")
    ]


def test_a_malformed_version_is_refused(store, composer):
    template = store.add(make_template("welcome", '{% managed_extends "base" version=x %}'))

    with pytest.raises(ManagedTemplateCompositionSyntaxError, match="version=N"):
        composer.compose(template)


def test_the_renderer_reports_the_latest_version_for_pinning(backend, inner_renderer):
    backend.create_template(make_create_input("welcome"))
    backend.update_template("welcome", make_update_input(template_body="v2"))
    renderer = ManagedTemplateEmailRenderer(backend, inner_renderer)

    assert renderer.get_latest_template_version("welcome") == 2


def test_the_renderer_reports_no_version_for_a_template_that_does_not_exist(
    backend, inner_renderer
):
    renderer = ManagedTemplateEmailRenderer(backend, inner_renderer)

    assert renderer.get_latest_template_version("nowhere") is None


def test_the_renderer_honours_the_version_a_notification_is_pinned_to(backend, inner_renderer):
    backend.create_template(make_create_input("welcome", template_body="v1", template_subject="s"))
    backend.update_template("welcome", make_update_input(template_body="v2"))
    renderer = ManagedTemplateEmailRenderer(backend, inner_renderer)
    notification = dataclasses.replace(make_notification("welcome"), requested_template_version=1)

    email = renderer.render(notification, {})

    assert email.body == "v1"
    assert email.template_version == 1


def test_an_unpinned_notification_renders_the_current_version(backend, inner_renderer):
    backend.create_template(make_create_input("welcome", template_body="v1", template_subject="s"))
    backend.update_template("welcome", make_update_input(template_body="v2"))
    renderer = ManagedTemplateEmailRenderer(backend, inner_renderer)

    email = renderer.render(make_notification("welcome"), {})

    assert email.body == "v2"
    assert email.template_version == 2


def test_the_service_renders_the_version_the_notification_is_pinned_to(service, backend):
    backend.create_template(make_create_input("welcome", template_body="v1", template_subject="s"))
    backend.update_template("welcome", make_update_input(template_body="v2"))
    notification = dataclasses.replace(make_notification("welcome"), requested_template_version=1)

    email = service.render(notification, {})

    assert email.body == "v1"
    assert email.template_version == 1


def test_an_explicit_version_overrides_the_notifications_pin(service, backend):
    backend.create_template(make_create_input("welcome", template_body="v1", template_subject="s"))
    backend.update_template("welcome", make_update_input(template_body="v2"))
    notification = dataclasses.replace(make_notification("welcome"), requested_template_version=1)

    email = service.render(notification, {}, version=2)

    assert email.body == "v2"
    assert email.template_version == 2
