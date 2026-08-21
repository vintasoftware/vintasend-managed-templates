"""Inheritance and inclusion for managed templates, resolved before the engine runs.

A file-based renderer gets composition for free. Django's ``{% extends %}`` and Jinja's
``{% include %}`` hand a *name* to a loader, and a loader reads files, so the header, the
footer and the wrapper every email shares live in one file that every other file points at.

Managed templates are not files. They reach the engine as source, pulled out of a database
row and handed to ``render_from_template_content`` as a string, so a loader has nothing to
resolve and those tags have nothing to load. Without this module the shared chrome would have
to be pasted into every template in the store, and changing the footer would mean editing
every row that has one.

This module puts composition back where the store can serve it. It defines a small tag
language, resolves it against the template store, and hands the engine one flat string with
no trace of itself left in it. Whatever runs next -- Django, Jinja, anything -- sees only its
own syntax, and its own ``{% extends %}`` / ``{% include %}`` are left untouched for it to
deal with.

The tags
--------

Every tag carries the ``managed_`` prefix, so nothing here can be mistaken for an engine tag
that is meant to survive composition. The prefix is configurable per composer, but it is
reserved: an unknown ``{% managed_* %}`` tag is a syntax error rather than text passed
through, which is what turns a typo into an error instead of into a broken email.

``{% managed_extends "base-email" %}``
    This template is a child of ``base-email``. At most one per template, at the top level
    (never inside a block). Takes an optional ``version=2`` to pin the parent.

``{% managed_children %}``
    In a parent: where the child's content goes. This is what makes a template *abstract* --
    it is a layout with a hole in it, and the hole is filled by whatever the child writes
    outside its blocks. Rendered on its own, with no child, the hole is simply empty.

``{% managed_block name %}...{% managed_endblock %}``
    A named, overridable region. A parent's block renders its own content unless a child
    declares a block with the same name; a child's wins. Blocks may nest, and a block with no
    override renders the content it was declared with.

``{% managed_super %}``
    Inside a child's block: the content of the block it is overriding. Chains through as many
    levels of inheritance as there are.

``{% managed_include "footer" %}``
    Splice another template in at this point. The included template is composed in full first,
    so an include may itself extend and include.

How a child fills a parent
--------------------------

Everything the child writes *outside* a block is its children content, and it lands in the
parent's ``{% managed_children %}``. Blocks are pulled out of that content first, so the two
mechanisms compose: a child can both fill the hole and override named regions.

    parent "base-email"    <html><body>
                             {% managed_block header %}<h1>Acme</h1>{% managed_endblock %}
                             {% managed_children %}
                           </body></html>

    child  "welcome"       {% managed_extends "base-email" %}
                           {% managed_block header %}<h1>Welcome</h1>{% managed_endblock %}
                           <p>Hi {{ name }}</p>

    composed               <html><body>
                             <h1>Welcome</h1>
                             <p>Hi {{ name }}</p>
                           </body></html>

``{{ name }}`` is untouched: composition never looks at engine syntax, and the context is the
engine's business.

One field at a time
-------------------

A template carries three sources -- body, subject and preheader -- and each is composed
against the *same* field of the template it references. A child's body extends the parent's
body; its subject extends the parent's subject. So a base can define a subject prefix and a
body wrapper at once, and neither leaks into the other. A field the referenced template
leaves empty composes to nothing rather than to an error.

Versions
--------

A reference with no ``version=`` resolves through the backend the same way any other read
does: to whatever version that key currently is. Pin it when a template must keep composing
against an exact parent -- re-rendering an old notification resolves the child's version
explicitly, but its unpinned parents still resolve to today's.

Cycles and depth
----------------

A reference chain that comes back to a template already being composed raises
``ManagedTemplateCompositionCycleError`` naming the chain, and a chain longer than
``max_depth`` raises ``ManagedTemplateCompositionDepthError``. Neither can be caught by the
engine downstream, so both are found here rather than as a hang at send time.
"""

import dataclasses
import functools
import re
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Literal

from .dataclasses import ManagedTemplate
from .exceptions import (
    ManagedTemplateCompositionCycleError,
    ManagedTemplateCompositionDepthError,
    ManagedTemplateCompositionReferenceError,
    ManagedTemplateCompositionSyntaxError,
    ManagedTemplateNotFoundError,
)


if TYPE_CHECKING:
    from .base_template_manager_backend import BaseTemplateManagerBackend


# Prefixed so a composition tag can never be confused with an engine tag meant to survive
# into the rendered output. Configurable on a composer, but the whole prefix is reserved:
# unknown tags carrying it are rejected instead of passed through.
DEFAULT_TAG_PREFIX = "managed_"

# How many references deep a single chain may go -- extends and include both count, since
# both resolve another template. High enough that no real layout hits it, low enough that a
# pathological store fails fast instead of exhausting the stack.
DEFAULT_MAX_DEPTH = 25

# The three sources a template carries, composed independently and each against the same
# field of whatever it references.
TEMPLATE_FIELDS: tuple[str, ...] = ("body_template", "subject_template", "preheader_template")

TemplateField = Literal["body_template", "subject_template", "preheader_template"]

# What ``get_template`` looks like from here: a key and an optional version in, one template
# out. ``BaseTemplateManagerBackend.get_template`` satisfies it as it stands.
TemplateResolver = Callable[[str, "int | None"], ManagedTemplate]

# The tags that are structure rather than content. Alone on a line, these take the line with
# them; the placeholder tags never do, so content lands exactly where the tag stood.
_LINE_TAGS = frozenset({"extends", "block", "endblock"})

_BLOCK_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]*$")
_REFERENCE_ARGS = re.compile(
    r"""^(?P<quote>["'])(?P<key>.*?)(?P=quote)(?:\s+version\s*=\s*(?P<version>\d+))?$"""
)


@dataclasses.dataclass(frozen=True)
class TemplateReference:
    """One direct reference from a template's field to another template.

    What ``TemplateComposer.references`` hands back, so a host can answer "what does this
    template need in order to render" without composing it: an admin validating a save, a
    UI drawing the layout tree, a deploy check that every base a template names exists.
    """

    kind: Literal["extends", "include"]
    key: str
    version: "int | None"
    field: str


# ----------------------------------------------------------------------
# Parse tree
# ----------------------------------------------------------------------
#
# Composition is a tree walk rather than a chain of regex substitutions. Blocks nest, an
# override may itself contain blocks, and ``{% managed_super %}`` has to reach the definition
# one level up -- all of which a substitution pass gets subtly wrong on the second level of
# inheritance.


@dataclasses.dataclass(frozen=True)
class _Text:
    text: str


@dataclasses.dataclass(frozen=True)
class _Block:
    name: str
    body: tuple["_Node", ...]


@dataclasses.dataclass(frozen=True)
class _Children:
    """The hole in an abstract template, filled by its child's out-of-block content."""


@dataclasses.dataclass(frozen=True)
class _Super:
    """The overridden definition of the block this node sits in."""


@dataclasses.dataclass(frozen=True)
class _Include:
    key: str
    version: "int | None"


_Node = _Text | _Block | _Children | _Super | _Include


@dataclasses.dataclass(frozen=True)
class _Parsed:
    extends: TemplateReference | None
    nodes: tuple[_Node, ...]


# A template's identity while it is being composed: what the cycle check compares and what
# error messages name. Always the *resolved* version, never the requested one, so a chain
# that reaches the same row by two different routes is still caught.
_Origin = tuple[str, "int | None"]


@dataclasses.dataclass
class _Run:
    """Everything one ``compose`` call accumulates, shared by every field it composes.

    Kept per call rather than on the composer so a composer is safe to hold for the life of a
    process: an edit to a base is picked up by the next render, not shadowed by a cache from
    the last one.
    """

    templates: dict[tuple[str, "int | None"], ManagedTemplate] = dataclasses.field(
        default_factory=dict
    )


@functools.lru_cache(maxsize=8)
def _tag_pattern(prefix: str) -> "re.Pattern[str]":
    """Every ``{% <prefix><name> <args> %}`` in a source, with its name and args split out.

    The args group accepts a bare ``%`` and stops only at ``%}``, so a percent sign inside a
    key does not end the tag early.
    """
    return re.compile(
        r"\{%\s*" + re.escape(prefix) + r"(?P<name>[a-z_]+)(?P<args>(?:[^%]|%(?!\}))*)%\}"
    )


def _describe(stack: tuple[_Origin, ...]) -> str:
    """The chain a message points at: ``'welcome' v1 -> 'base-email' v2``."""
    return " -> ".join(
        f"'{key}'" if version is None else f"'{key}' v{version}" for key, version in stack
    )


def _at(stack: tuple[_Origin, ...], field: str) -> str:
    """The ``(in ...)`` suffix every composition error carries."""
    if not stack:
        return f" (in {field})"
    return f" (in {field} of {_describe(stack)})"


class TemplateComposer:
    """Resolves ``managed_*`` composition tags against a template store.

    :param resolve_template: how a referenced key becomes a template -- normally a backend's
        ``get_template``. Left out, the composer still parses (so ``references``,
        ``is_abstract`` and syntax checking work), but any template that actually references
        another raises ``ManagedTemplateCompositionReferenceError``.
    :param tag_prefix: the reserved prefix every composition tag carries. Change it only if
        ``managed_`` collides with something the engine downstream must receive verbatim.
    :param max_depth: how many references deep one chain may go before it is called runaway.
    """

    def __init__(
        self,
        resolve_template: TemplateResolver | None = None,
        *,
        tag_prefix: str = DEFAULT_TAG_PREFIX,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> None:
        self.resolve_template = resolve_template
        self.tag_prefix = tag_prefix
        self.max_depth = max_depth

    @classmethod
    def from_backend(
        cls,
        backend: "BaseTemplateManagerBackend",
        *,
        tag_prefix: str = DEFAULT_TAG_PREFIX,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ) -> "TemplateComposer":
        """A composer that resolves references through ``backend.get_template``.

        param backend: BaseTemplateManagerBackend
        return: TemplateComposer
        """
        return cls(backend.get_template, tag_prefix=tag_prefix, max_depth=max_depth)

    # ------------------------------------------------------------------
    # Composing
    # ------------------------------------------------------------------

    def compose(self, template: ManagedTemplate) -> ManagedTemplate:
        """Resolve every composition tag in a template, returning the flattened result.

        Each of the three sources is composed against the same field of whatever it
        references. The template handed in is never mutated; a template with no composition
        tags in it is returned as it stands, same object and all.

        param template: ManagedTemplate
        return: ManagedTemplate -- composed, ready for the engine.
        raises ManagedTemplateCompositionSyntaxError: if a tag is malformed or unbalanced.
        raises ManagedTemplateCompositionReferenceError: if a referenced template is missing.
        raises ManagedTemplateCompositionCycleError: if the references loop.
        raises ManagedTemplateCompositionDepthError: if the chain runs past ``max_depth``.
        """
        run = _Run()
        stack = ((template.key, template.version),)
        run.templates[stack[0]] = template

        # Spelled out one field at a time rather than looped over ``TEMPLATE_FIELDS``: the
        # three have different types (a body is always a string, a subject may be None), and
        # a loop would hand ``dataclasses.replace`` a dict no type checker can line up with
        # the fields it is filling.
        body = template.body_template
        if body:
            body = self._compose_source(body, "body_template", stack, run)

        subject = template.subject_template
        if subject:
            subject = self._compose_source(subject, "subject_template", stack, run)

        preheader = template.preheader_template
        if preheader:
            preheader = self._compose_source(preheader, "preheader_template", stack, run)

        if (
            body == template.body_template
            and subject == template.subject_template
            and preheader == template.preheader_template
        ):
            return template

        return dataclasses.replace(
            template,
            body_template=body,
            subject_template=subject,
            preheader_template=preheader,
        )

    def compose_source(
        self,
        source: str,
        *,
        field: str = "body_template",
        key: str | None = None,
        version: int | None = None,
    ) -> str:
        """Compose one source string that is not (yet) a stored template.

        This is the path for source in hand rather than in the store: an admin validating
        what was typed into a form before saving it, a preview of an unsaved draft.

        Pass ``key`` (and ``version``) when the source belongs to a template that exists, so
        a chain that leads back to it is reported as the cycle it is instead of composing the
        stored -- and by then stale -- copy of the very row being edited.

        param source: str
        param field: str -- which of ``TEMPLATE_FIELDS`` this is, since references resolve
            against the same field of the template they name.
        param key: str | None -- the key this source belongs to, for cycle detection.
        param version: int | None
        return: str
        """
        stack: tuple[_Origin, ...] = ((key, version),) if key is not None else ()
        return self._compose_source(source, field, stack, _Run())

    def compose_field(self, template: ManagedTemplate, field: str) -> str | None:
        """Compose a single field of a template, leaving the other two alone.

        param template: ManagedTemplate
        param field: str
        return: str | None -- None when the template leaves that field unset.
        """
        source = getattr(template, field)
        if not source:
            return source
        run = _Run()
        origin = (template.key, template.version)
        run.templates[origin] = template
        return self._compose_source(source, field, (origin,), run)

    def validate(self, template: ManagedTemplate) -> None:
        """Compose the template and throw the result away, to surface any problem now.

        What an admin form or a deploy check calls: it raises exactly what rendering would
        have raised, at a point where someone can still fix it.

        param template: ManagedTemplate
        raises ManagedTemplateCompositionError: and its subclasses, as ``compose`` does.
        """
        self.compose(template)

    # ------------------------------------------------------------------
    # Inspecting, without resolving anything
    # ------------------------------------------------------------------

    def references(self, template: ManagedTemplate) -> list[TemplateReference]:
        """Every template this one directly names, across all three fields.

        Direct only: what the references themselves reference is not followed, so this needs
        no store and never raises for a missing template. Order is the order they appear,
        field by field.

        param template: ManagedTemplate
        return: list[TemplateReference]
        raises ManagedTemplateCompositionSyntaxError: if a tag is malformed or unbalanced.
        """
        found: list[TemplateReference] = []
        for field in TEMPLATE_FIELDS:
            source = getattr(template, field)
            if not source:
                continue
            found.extend(self.source_references(source, field=field))
        return found

    def source_references(
        self, source: str, *, field: str = "body_template"
    ) -> list[TemplateReference]:
        """Every template one source string directly names.

        param source: str
        param field: str
        return: list[TemplateReference]
        """
        parsed = self._parse(source, field, ())
        found: list[TemplateReference] = []
        if parsed.extends is not None:
            found.append(parsed.extends)
        found.extend(
            TemplateReference("include", node.key, node.version, field)
            for node in _walk(parsed.nodes)
            if isinstance(node, _Include)
        )
        return found

    def is_abstract(self, template: ManagedTemplate) -> bool:
        """Whether this template is a base to build on rather than one to send.

        True when any of its fields declares a ``{% managed_children %}`` hole, or declares
        blocks without extending anything -- the two shapes that only make sense with a child
        underneath. Nothing is stored: abstractness is a property of the source, so a
        template becomes abstract the moment someone writes the hole into it and stops being
        abstract the moment they take it out.

        Composing an abstract template directly is allowed and yields the layout with an
        empty hole. Use this to keep one out of the picker where a *sendable* template is
        being chosen.

        param template: ManagedTemplate
        return: bool
        raises ManagedTemplateCompositionSyntaxError: if a tag is malformed or unbalanced.
        """
        return any(
            self.source_is_abstract(getattr(template, field) or "", field=field)
            for field in TEMPLATE_FIELDS
        )

    def source_is_abstract(self, source: str, *, field: str = "body_template") -> bool:
        """Whether one source string is a base to build on rather than one to send.

        The per-field half of ``is_abstract``, for source in hand rather than a whole
        template -- what a form checks about what was typed into it, and what a host with its
        own model asks about a row it holds.

        param source: str
        param field: str
        return: bool
        raises ManagedTemplateCompositionSyntaxError: if a tag is malformed or unbalanced.
        """
        if not source:
            return False
        parsed = self._parse(source, field, ())
        if any(isinstance(node, _Children) for node in _walk(parsed.nodes)):
            return True
        return parsed.extends is None and any(
            isinstance(node, _Block) for node in _walk(parsed.nodes)
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _compose_source(
        self, source: str, field: str, stack: tuple[_Origin, ...], run: _Run
    ) -> str:
        """Walk the inheritance chain from this source upwards, rendering once at the top.

        Overrides accumulate on the way up: each level's blocks are pushed *under* the ones
        already collected, so the most derived definition stays at the head of every chain and
        ``{% managed_super %}`` reaches the next one down it. The content each level writes
        outside its blocks is rendered as it is passed, becoming the children of the level
        above -- which is what lets a middle template wrap its own child's content before
        handing it on.
        """
        parsed = self._parse(source, field, stack)
        blocks: dict[str, list[tuple[_Node, ...]]] = {}
        children = ""

        while parsed.extends is not None:
            parent, parent_stack = self._resolve(parsed.extends, stack, field, run)
            children = self._render(
                _outside_blocks(parsed.nodes), blocks, children, frozenset(), (), field, stack, run
            )
            for name, body in _collect_blocks(parsed.nodes).items():
                blocks.setdefault(name, []).append(body)

            stack = parent_stack
            parsed = self._parse(getattr(parent, field) or "", field, stack)

        return self._render(parsed.nodes, blocks, children, frozenset(), (), field, stack, run)

    def _render(
        self,
        nodes: tuple[_Node, ...],
        blocks: dict[str, list[tuple[_Node, ...]]],
        children: str,
        active: frozenset[str],
        super_chain: tuple[tuple[_Node, ...], ...],
        field: str,
        stack: tuple[_Origin, ...],
        run: _Run,
    ) -> str:
        parts: list[str] = []

        for node in nodes:
            if isinstance(node, _Text):
                parts.append(node.text)
            elif isinstance(node, _Children):
                parts.append(children)
            elif isinstance(node, _Super):
                # No definition left underneath means nothing to fall back to, which is what
                # a top-level block's super is: the empty string, not an error.
                if super_chain:
                    parts.append(
                        self._render(
                            super_chain[0],
                            blocks,
                            children,
                            active,
                            super_chain[1:],
                            field,
                            stack,
                            run,
                        )
                    )
            elif isinstance(node, _Include):
                reference = TemplateReference("include", node.key, node.version, field)
                included, included_stack = self._resolve(reference, stack, field, run)
                parts.append(
                    self._compose_source(getattr(included, field) or "", field, included_stack, run)
                )
            else:
                # A block renders the most derived definition of its name, with the rest of
                # the chain -- ending in the definition written here -- available to super.
                # Re-entering a name already being rendered would loop, so inside itself a
                # block is only ever its own definition.
                chain = (node.body,) if node.name in active else _chain_for(blocks, node)
                parts.append(
                    self._render(
                        chain[0],
                        blocks,
                        children,
                        active | {node.name},
                        chain[1:],
                        field,
                        stack,
                        run,
                    )
                )

        return "".join(parts)

    def _resolve(
        self,
        reference: TemplateReference,
        stack: tuple[_Origin, ...],
        field: str,
        run: _Run,
    ) -> tuple[ManagedTemplate, tuple[_Origin, ...]]:
        """Fetch a referenced template and extend the chain with it.

        Cycles are compared on the *resolved* version rather than on the requested one, so a
        template reached once by name and once by an explicit ``version=`` is recognized as
        the same row.
        """
        if len(stack) > self.max_depth:
            raise ManagedTemplateCompositionDepthError(
                f"Template composition went more than {self.max_depth} references deep"
                f"{_at(stack, field)}."
            )

        if self.resolve_template is None:
            raise ManagedTemplateCompositionReferenceError(
                f"Cannot resolve '{reference.key}': this composer was built without a template "
                f"resolver{_at(stack, field)}."
            )

        cached = run.templates.get((reference.key, reference.version))
        if cached is None:
            try:
                cached = self.resolve_template(reference.key, reference.version)
            except ManagedTemplateNotFoundError as error:
                raise ManagedTemplateCompositionReferenceError(
                    f"{reference.kind} references template '{reference.key}'"
                    f"{'' if reference.version is None else f' v{reference.version}'}, "
                    f"which does not exist{_at(stack, field)}."
                ) from error
            run.templates[(reference.key, reference.version)] = cached
            # Also under the version it turned out to be, so a later reference that pins it
            # explicitly hits the same object -- and is recognized as the same row by the
            # cycle check below.
            run.templates.setdefault((cached.key, cached.version), cached)

        origin = (cached.key, cached.version)
        if origin in stack:
            raise ManagedTemplateCompositionCycleError(
                f"Template composition loops: {_describe((*stack, origin))} (in {field})."
            )
        return cached, (*stack, origin)

    def _parse(self, source: str, field: str, stack: tuple[_Origin, ...]) -> _Parsed:
        """Turn a source string into a node tree, rejecting anything malformed.

        A tag sitting alone on its line takes the whole line with it -- its indentation and
        the newline after it -- so a layout written to be readable does not compose into one
        full of blank lines. A tag with content beside it is removed exactly, and the
        whitespace around it is the author's.
        """
        pattern = _tag_pattern(self.tag_prefix)
        root: list[_Node] = []
        open_blocks: list[tuple[str, list[_Node]]] = []
        declared: set[str] = set()
        extends: TemplateReference | None = None
        position = 0

        for match in pattern.finditer(source):
            current = open_blocks[-1][1] if open_blocks else root
            name = match.group("name")
            args = match.group("args").strip()

            text, position = _text_before(source, match, position, name in _LINE_TAGS)
            if text:
                current.append(_Text(text))

            if name == "extends":
                if open_blocks:
                    raise self._syntax(
                        f"{self._tag('extends')} cannot appear inside a block", field, stack
                    )
                if extends is not None:
                    raise self._syntax(
                        f"a template can only {self._tag('extends')} one other template",
                        field,
                        stack,
                    )
                key, version = self._reference_args(args, "extends", field, stack)
                extends = TemplateReference("extends", key, version, field)
            elif name == "include":
                key, version = self._reference_args(args, "include", field, stack)
                current.append(_Include(key, version))
            elif name == "block":
                block_name = self._block_name(args, field, stack)
                if block_name in declared:
                    raise self._syntax(
                        f"block '{block_name}' is declared twice in the same template",
                        field,
                        stack,
                    )
                declared.add(block_name)
                open_blocks.append((block_name, []))
            elif name == "endblock":
                if not open_blocks:
                    raise self._syntax(f"{self._tag('endblock')} with no block open", field, stack)
                block_name, body = open_blocks.pop()
                if args and self._block_name(args, field, stack) != block_name:
                    raise self._syntax(
                        f"{self._tag('endblock')} says '{args}' but the open block is "
                        f"'{block_name}'",
                        field,
                        stack,
                    )
                target = open_blocks[-1][1] if open_blocks else root
                target.append(_Block(block_name, tuple(body)))
            elif name == "children":
                self._expect_no_args(args, "children", field, stack)
                current.append(_Children())
            elif name == "super":
                self._expect_no_args(args, "super", field, stack)
                current.append(_Super())
            else:
                raise self._syntax(
                    f"unknown composition tag {self._tag(name)}. The '{self.tag_prefix}' prefix "
                    f"is reserved for composition, so this is not passed through to the engine",
                    field,
                    stack,
                )

        if open_blocks:
            raise self._syntax(
                f"block '{open_blocks[-1][0]}' is never closed with {self._tag('endblock')}",
                field,
                stack,
            )

        tail = source[position:]
        if tail:
            root.append(_Text(tail))

        return _Parsed(extends, tuple(root))

    # -- parsing helpers -------------------------------------------------

    def _tag(self, name: str) -> str:
        return "{% " + self.tag_prefix + name + " %}"

    def _syntax(
        self, message: str, field: str, stack: tuple[_Origin, ...]
    ) -> ManagedTemplateCompositionSyntaxError:
        return ManagedTemplateCompositionSyntaxError(f"{message}{_at(stack, field)}.")

    def _reference_args(
        self, args: str, tag: str, field: str, stack: tuple[_Origin, ...]
    ) -> tuple[str, int | None]:
        match = _REFERENCE_ARGS.match(args)
        if match is None or not match.group("key").strip():
            raise self._syntax(
                f"{self._tag(tag)} takes a quoted template key, optionally followed by "
                f"version=N -- got {_shown(args)}",
                field,
                stack,
            )

        key = match.group("key").strip()
        version = None if match.group("version") is None else int(match.group("version"))
        return key, version

    def _block_name(self, args: str, field: str, stack: tuple[_Origin, ...]) -> str:
        name = args.strip("\"'").strip()
        if not _BLOCK_NAME.match(name):
            raise self._syntax(
                f"{self._tag('block')} takes a name starting with a letter or underscore -- "
                f"got {_shown(args)}",
                field,
                stack,
            )
        return name

    def _expect_no_args(self, args: str, tag: str, field: str, stack: tuple[_Origin, ...]) -> None:
        if args:
            raise self._syntax(f"{self._tag(tag)} takes no arguments -- got {args!r}", field, stack)


def _shown(args: str) -> str:
    """How a tag's arguments read in an error message when there may not be any."""
    return repr(args) if args else "nothing"


def _text_before(
    source: str, match: "re.Match[str]", position: int, trim_line: bool
) -> tuple[str, int]:
    """The text between the last tag and this one, and where to resume after this one.

    Structural tags alone on their line are taken out *with* the line -- the indentation in
    front of them and the newline behind them -- so a layout written across several lines does
    not compose into one padded with blank ones. The placeholder tags
    (``children`` / ``include`` / ``super``) are never line-trimmed: they stand where content
    goes, so what replaces them lands exactly where the author put them, indentation and all.
    """
    text = source[position : match.start()]
    if not trim_line:
        return text, match.end()

    line_start = source.rfind("\n", 0, match.start()) + 1
    alone = source[line_start : match.start()].strip(" \t") == ""
    trailing = re.match(r"[ \t]*\r?\n", source[match.end() :]) if alone else None

    if trailing is None:
        return text, match.end()

    indent = min(match.start() - line_start, len(text))
    return text[: len(text) - indent], match.end() + trailing.end()


def _walk(nodes: Iterable[_Node]) -> Iterable[_Node]:
    """Every node in the tree, blocks and their contents alike."""
    for node in nodes:
        yield node
        if isinstance(node, _Block):
            yield from _walk(node.body)


def _collect_blocks(nodes: tuple[_Node, ...]) -> dict[str, tuple[_Node, ...]]:
    """Every block a template declares, at any depth, by name.

    Nested ones are collected too, so a child can override a block that only exists inside
    another block of the parent -- and so a block a child nests inside one of its own is
    still available to the level above. Names are unique per template, enforced at parse.
    """
    return {node.name: node.body for node in _walk(nodes) if isinstance(node, _Block)}


def _outside_blocks(nodes: tuple[_Node, ...]) -> tuple[_Node, ...]:
    """A child's content minus its block declarations -- what fills the parent's hole."""
    return tuple(node for node in nodes if not isinstance(node, _Block))


def _chain_for(
    blocks: dict[str, list[tuple[_Node, ...]]], block: _Block
) -> tuple[tuple[_Node, ...], ...]:
    """Definitions of one block, most derived first, ending in the one declared here.

    The head is what renders; the rest is what ``{% managed_super %}`` walks down.
    """
    return (*blocks.get(block.name, ()), block.body)


def is_abstract(template: ManagedTemplate, *, tag_prefix: str = DEFAULT_TAG_PREFIX) -> bool:
    """Whether a template is a base to build on rather than one to send.

    The store-free shortcut for ``TemplateComposer.is_abstract``: abstractness is read off the
    source, so no backend is needed to answer it.

    param template: ManagedTemplate
    param tag_prefix: str
    return: bool
    """
    return TemplateComposer(tag_prefix=tag_prefix).is_abstract(template)
