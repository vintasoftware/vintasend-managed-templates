"""Tag management and tag-based search, driven through the service.

The service is exercised over the in-memory backend from ``fakes.py`` rather than a mock, so
these tests fail when tagging is wrong rather than when a call expectation drifted. What the
service itself owns -- and what these tests are mostly about -- is normalization: text is
cleaned and slugified here, so every backend is handed the same slug for the same text.
"""

import pytest

from vintasend_managed_templates.constants import ManagedTemplateTagStatus
from vintasend_managed_templates.exceptions import (
    ManagedTemplateInvalidFilterError,
    ManagedTemplateInvalidTagError,
    ManagedTemplateNotFoundError,
    ManagedTemplateTagAlreadyExistsError,
    ManagedTemplateTagNotFoundError,
)

from .fakes import make_create_input, make_update_input


def slugs(tags) -> list[str]:
    return [tag.slug for tag in tags]


def keys(templates) -> list[str]:
    return [template.key for template in templates]


@pytest.fixture
def tagged(service):
    """Three templates with overlapping tags, for the search cases.

    ``welcome`` and ``receipt`` share ``transactional``; only ``welcome`` is also ``onboarding``.
    """
    service.create_template(make_create_input("welcome", tags=["Transactional", "Onboarding"]))
    service.create_template(make_create_input("receipt", tags=["Transactional", "Billing"]))
    service.create_template(make_create_input("newsletter", tags=["Marketing"]))
    return service


# ----------------------------------------------------------------------
# Creating tags on the fly
# ----------------------------------------------------------------------


def test_creating_a_template_creates_the_tags_it_names(service):
    template = service.create_template(make_create_input("welcome", tags=["Black Friday"]))

    assert slugs(template.tags) == ["black-friday"]
    assert slugs(service.get_tags()) == ["black-friday"]


def test_a_tag_keeps_the_text_it_was_written_as(service):
    template = service.create_template(make_create_input("welcome", tags=["Black Friday"]))

    assert template.tags[0].text == "Black Friday"
    assert template.tags[0].slug == "black-friday"


def test_a_new_tag_starts_active(service):
    template = service.create_template(make_create_input("welcome", tags=["Onboarding"]))

    assert template.tags[0].status is ManagedTemplateTagStatus.ACTIVE


def test_two_templates_naming_the_same_tag_differently_share_one_tag(service):
    first = service.create_template(make_create_input("welcome", tags=["Black Friday"]))
    second = service.create_template(make_create_input("receipt", tags=["black friday"]))

    assert first.tags[0].id == second.tags[0].id
    assert len(service.get_tags()) == 1


def test_repeating_a_tag_within_one_call_attaches_it_once(service):
    template = service.create_template(
        make_create_input("welcome", tags=["Onboarding", "onboarding", "ONBOARDING"])
    )

    assert slugs(template.tags) == ["onboarding"]


def test_a_template_created_without_tags_has_none(service, welcome):
    assert welcome.tags == []


@pytest.mark.parametrize("text", ["", "   ", "!!!"])
def test_a_tag_with_nothing_sluggable_is_rejected_at_the_service(service, text):
    with pytest.raises(ManagedTemplateInvalidTagError, match="slug"):
        service.create_template(make_create_input("welcome", tags=[text]))


def test_a_rejected_tag_leaves_no_template_behind(service):
    """The check runs before the write, so a bad tag does not half-create anything."""
    with pytest.raises(ManagedTemplateInvalidTagError):
        service.create_template(make_create_input("welcome", tags=["!!!"]))

    assert service.get_all_templates() == []


def test_surrounding_whitespace_is_trimmed_from_a_tags_text(service):
    template = service.create_template(make_create_input("welcome", tags=["  Black   Friday  "]))

    assert template.tags[0].text == "Black Friday"


# ----------------------------------------------------------------------
# Tags across versions
# ----------------------------------------------------------------------


def test_a_new_version_carries_the_previous_versions_tags_forward(service):
    service.create_template(make_create_input("welcome", tags=["Onboarding"]))

    updated = service.update_template("welcome", make_update_input(name="Renamed"))

    assert slugs(updated.tags) == ["onboarding"]


def test_a_new_version_can_replace_the_tags(service):
    service.create_template(make_create_input("welcome", tags=["Onboarding"]))

    updated = service.update_template("welcome", make_update_input(tags=["Billing"]))

    assert slugs(updated.tags) == ["billing"]


def test_an_empty_tag_list_on_an_update_clears_the_tags(service):
    """``[]`` and ``None`` mean different things here, unlike the other update fields."""
    service.create_template(make_create_input("welcome", tags=["Onboarding"]))

    updated = service.update_template("welcome", make_update_input(tags=[]))

    assert updated.tags == []


def test_retagging_a_version_does_not_create_a_new_version(service):
    created = service.create_template(make_create_input("welcome", tags=["Onboarding"]))

    retagged = service.set_template_tags("welcome", ["Billing"])

    assert retagged.version == created.version
    assert len(service.get_template_versions("welcome")) == 1


def test_retagging_leaves_the_versions_status_alone(service):
    service.create_template(make_create_input("welcome"))
    activated = service.activate("welcome")

    retagged = service.set_template_tags("welcome", ["Billing"])

    assert retagged.status is activated.status


def test_retagging_can_target_an_older_version(service):
    service.create_template(make_create_input("welcome", tags=["Onboarding"]))
    service.update_template("welcome", make_update_input(name="v2"))

    service.set_template_tags("welcome", ["Billing"], version=1)

    assert slugs(service.get_template_tags("welcome", version=1)) == ["billing"]
    assert slugs(service.get_template_tags("welcome", version=2)) == ["onboarding"]


def test_retagging_an_unknown_key_raises(service):
    with pytest.raises(ManagedTemplateNotFoundError):
        service.set_template_tags("nope", ["Billing"])


def test_adding_tags_keeps_the_ones_already_on_the_version(service):
    service.create_template(make_create_input("welcome", tags=["Onboarding"]))

    updated = service.add_template_tags("welcome", ["Billing"])

    assert slugs(updated.tags) == ["onboarding", "billing"]


def test_adding_a_tag_the_version_already_carries_changes_nothing(service, backend):
    service.create_template(make_create_input("welcome", tags=["Onboarding"]))
    backend.calls.clear()

    updated = service.add_template_tags("welcome", ["onboarding"])

    assert slugs(updated.tags) == ["onboarding"]
    assert "set_template_tags" not in backend.calls


def test_removing_a_tag_unlinks_it_without_deleting_it(service):
    service.create_template(make_create_input("welcome", tags=["Onboarding", "Billing"]))

    updated = service.remove_template_tags("welcome", ["Onboarding"])

    assert slugs(updated.tags) == ["billing"]
    assert "onboarding" in slugs(service.get_tags())


def test_removing_a_tag_the_version_does_not_carry_changes_nothing(service, backend):
    service.create_template(make_create_input("welcome", tags=["Onboarding"]))
    backend.calls.clear()

    updated = service.remove_template_tags("welcome", ["Marketing"])

    assert slugs(updated.tags) == ["onboarding"]
    assert "set_template_tags" not in backend.calls


def test_tags_can_be_named_by_text_when_adding_or_removing(service):
    service.create_template(make_create_input("welcome", tags=["Black Friday"]))

    updated = service.remove_template_tags("welcome", ["black friday"])

    assert updated.tags == []


# ----------------------------------------------------------------------
# Managing the tags themselves
# ----------------------------------------------------------------------


def test_a_tag_can_be_created_before_any_template_uses_it(service):
    tag = service.create_tag("Black Friday")

    assert tag.slug == "black-friday"
    assert tag.status is ManagedTemplateTagStatus.ACTIVE


def test_creating_a_tag_that_already_exists_is_an_error(service):
    service.create_tag("Black Friday")

    with pytest.raises(ManagedTemplateTagAlreadyExistsError):
        service.create_tag("black friday")


def test_get_or_create_resolves_an_existing_tag_instead_of_failing(service):
    created = service.create_tag("Black Friday")

    resolved = service.get_or_create_tags(["black friday"])

    assert [tag.id for tag in resolved] == [created.id]


def test_a_tag_is_found_by_its_text_as_well_as_its_slug(service):
    service.create_tag("Black Friday")

    assert service.get_tag("Black Friday").slug == "black-friday"
    assert service.get_tag("black-friday").slug == "black-friday"


def test_looking_up_a_tag_that_does_not_exist_raises(service):
    with pytest.raises(ManagedTemplateTagNotFoundError):
        service.get_tag("nope")


def test_renaming_a_tag_regenerates_its_slug(service):
    service.create_tag("Blak Friday")

    renamed = service.update_tag("blak-friday", "Black Friday")

    assert renamed.text == "Black Friday"
    assert renamed.slug == "black-friday"


def test_a_rename_reaches_the_templates_carrying_the_tag(service):
    service.create_template(make_create_input("welcome", tags=["Blak Friday"]))

    service.update_tag("blak-friday", "Black Friday")

    assert slugs(service.get_template_tags("welcome")) == ["black-friday"]


def test_a_rename_onto_a_taken_slug_gets_a_numeric_suffix(service):
    """Two tags may read the same and still have to be told apart by slug."""
    service.create_tag("Black Friday")
    service.create_tag("Cyber Monday")

    renamed = service.update_tag("cyber-monday", "Black Friday")

    assert renamed.slug == "black-friday-2"
    assert renamed.text == "Black Friday"


def test_renaming_a_tag_to_its_own_text_keeps_its_slug(service):
    service.create_tag("Black Friday")

    renamed = service.update_tag("black-friday", "Black friday")

    assert renamed.slug == "black-friday"


def test_renaming_to_text_with_nothing_sluggable_is_rejected(service):
    service.create_tag("Black Friday")

    with pytest.raises(ManagedTemplateInvalidTagError):
        service.update_tag("black-friday", "!!!")


def test_renaming_an_unknown_tag_raises(service):
    with pytest.raises(ManagedTemplateTagNotFoundError):
        service.update_tag("nope", "Whatever")


def test_archiving_a_tag_keeps_it_on_the_templates_carrying_it(service):
    service.create_template(make_create_input("welcome", tags=["Onboarding"]))

    service.archive_tag("onboarding")

    assert slugs(service.get_template_tags("welcome")) == ["onboarding"]


def test_an_archived_tag_drops_out_of_the_active_list(service):
    service.create_tag("Onboarding")
    service.create_tag("Billing")

    service.archive_tag("onboarding")

    assert slugs(service.get_active_tags()) == ["billing"]
    assert sorted(slugs(service.get_tags())) == ["billing", "onboarding"]


def test_an_archived_tag_can_be_restored(service):
    service.create_tag("Onboarding")
    service.archive_tag("onboarding")

    restored = service.restore_tag("onboarding")

    assert restored.status is ManagedTemplateTagStatus.ACTIVE


def test_archiving_an_unknown_tag_raises(service):
    with pytest.raises(ManagedTemplateTagNotFoundError):
        service.archive_tag("nope")


def test_deleting_a_tag_removes_it_from_every_template(service):
    service.create_template(make_create_input("welcome", tags=["Onboarding", "Billing"]))
    service.create_template(make_create_input("receipt", tags=["Onboarding"]))

    service.delete_tag("onboarding")

    assert slugs(service.get_template_tags("welcome")) == ["billing"]
    assert service.get_template_tags("receipt") == []
    assert slugs(service.get_tags()) == ["billing"]


def test_deleting_an_unknown_tag_raises(service):
    with pytest.raises(ManagedTemplateTagNotFoundError):
        service.delete_tag("nope")


def test_tags_can_be_listed_by_status(service):
    service.create_tag("Onboarding")
    service.create_tag("Billing")
    service.archive_tag("billing")

    listed = service.get_tags(status=[ManagedTemplateTagStatus.ARCHIVED])

    assert slugs(listed) == ["billing"]


def test_tags_can_be_searched_by_text(service):
    service.create_tag("Black Friday")
    service.create_tag("Onboarding")

    assert slugs(service.get_tags(search="friday")) == ["black-friday"]


def test_tags_can_be_listed_by_tenant(service):
    service.create_tag("Onboarding", tenant="acme")
    service.create_tag("Billing", tenant="other")

    assert slugs(service.get_tags(tenant="acme")) == ["onboarding"]


# ----------------------------------------------------------------------
# Searching templates by tag
# ----------------------------------------------------------------------


def test_includes_all_tags_matches_a_template_carrying_every_one(tagged):
    results = tagged.get_filtered_templates({"includes_all_tags": ["transactional", "onboarding"]})

    assert keys(results) == ["welcome"]


def test_includes_all_tags_excludes_a_template_missing_one_of_them(tagged):
    results = tagged.get_filtered_templates({"includes_all_tags": ["transactional", "marketing"]})

    assert results == []


def test_includes_any_of_tags_matches_a_template_carrying_at_least_one(tagged):
    results = tagged.get_filtered_templates({"includes_any_of_tags": ["onboarding", "marketing"]})

    assert sorted(keys(results)) == ["newsletter", "welcome"]


def test_a_single_tag_means_the_same_thing_under_either_field(tagged):
    all_of = tagged.get_filtered_templates({"includes_all_tags": ["transactional"]})
    any_of = tagged.get_filtered_templates({"includes_any_of_tags": ["transactional"]})

    assert sorted(keys(all_of)) == sorted(keys(any_of)) == ["receipt", "welcome"]


def test_a_tag_filter_may_name_a_tag_by_its_text(tagged):
    results = tagged.get_filtered_templates({"includes_any_of_tags": ["Transactional"]})

    assert sorted(keys(results)) == ["receipt", "welcome"]


def test_an_unknown_tag_matches_nothing(tagged):
    assert tagged.get_filtered_templates({"includes_any_of_tags": ["nope"]}) == []


def test_matching_all_of_no_tags_constrains_nothing(tagged):
    """``all([])`` is True in Python, and this filter follows it."""
    results = tagged.get_filtered_templates({"includes_all_tags": []})

    assert sorted(keys(results)) == ["newsletter", "receipt", "welcome"]


def test_matching_any_of_no_tags_matches_nothing(tagged):
    """``any([])`` is False in Python, and this filter follows it."""
    assert tagged.get_filtered_templates({"includes_any_of_tags": []}) == []


def test_a_tag_filter_combines_with_the_other_fields(tagged):
    results = tagged.get_filtered_templates(
        {"includes_any_of_tags": ["transactional"], "key": "receipt"}
    )

    assert keys(results) == ["receipt"]


def test_a_tag_filter_composes_under_and_or_and_not(tagged):
    results = tagged.get_filtered_templates(
        {
            "and": [
                {"includes_any_of_tags": ["transactional", "marketing"]},
                {"not": {"includes_all_tags": ["onboarding"]}},
            ]
        }
    )

    assert sorted(keys(results)) == ["newsletter", "receipt"]


def test_get_templates_by_tags_defaults_to_matching_all_of_them(tagged):
    results = tagged.get_templates_by_tags(["transactional", "onboarding"])

    assert keys(results) == ["welcome"]


def test_get_templates_by_tags_can_match_any_of_them(tagged):
    results = tagged.get_templates_by_tags(["onboarding", "marketing"], match_all=False)

    assert sorted(keys(results)) == ["newsletter", "welcome"]


def test_get_templates_by_tags_accepts_texts_rather_than_slugs(tagged):
    results = tagged.get_templates_by_tags(["Transactional", "Onboarding"])

    assert keys(results) == ["welcome"]


def test_get_templates_by_tags_drops_a_tag_with_nothing_sluggable(tagged):
    """Searching never raises on unusable text -- a tag no store could hold matches nothing."""
    assert tagged.get_templates_by_tags(["!!!"], match_all=False) == []


def test_tags_are_paginated_with_the_templates_carrying_them(tagged):
    page = tagged.get_paginated_filtered_templates(
        {"includes_any_of_tags": ["transactional"]}, 1, 1
    )

    assert len(page) == 1
    assert page[0].tags


def test_archiving_a_tag_does_not_hide_the_templates_carrying_it(tagged):
    """Archiving retires a tag from the pickers; it is not a filter on the templates."""
    tagged.archive_tag("transactional")

    results = tagged.get_filtered_templates({"includes_any_of_tags": ["transactional"]})

    assert sorted(keys(results)) == ["receipt", "welcome"]


# ----------------------------------------------------------------------
# Filter validation
# ----------------------------------------------------------------------


@pytest.mark.parametrize("field", ["includes_all_tags", "includes_any_of_tags"])
def test_a_bare_string_tag_filter_is_rejected(service, field):
    """Left through, a string would be iterated character by character and match nothing."""
    with pytest.raises(ManagedTemplateInvalidFilterError, match="list of tag slugs"):
        service.get_filtered_templates({field: "welcome"})


@pytest.mark.parametrize("value", [42, {"lookup": "in", "value": ["a"]}, None])
def test_a_tag_filter_that_is_not_a_collection_of_strings_is_rejected(service, value):
    with pytest.raises(ManagedTemplateInvalidFilterError, match="list of tag slugs"):
        service.get_filtered_templates({"includes_all_tags": value})


@pytest.mark.parametrize("value", [["a"], ("a",), {"a"}])
def test_any_collection_of_strings_is_accepted(service, value):
    service.validate_filter({"includes_all_tags": value})


def test_a_tag_filter_is_validated_inside_a_logical_group(service):
    with pytest.raises(ManagedTemplateInvalidFilterError, match=r"or\[0\].includes_all_tags"):
        service.get_filtered_templates({"or": [{"includes_all_tags": "welcome"}]})
