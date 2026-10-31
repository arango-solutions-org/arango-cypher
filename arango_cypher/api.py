from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from arango_query_core import AqlQuery, CoreError, ExtensionPolicy, ExtensionRegistry, MappingBundle
from arango_query_core.exec import AqlExecutor

from ._translate_v0.const_eval import folding_enabled
from .parser import parse_cypher
from .profile import build_cypher_profile
from .translate_v0 import TranslateOptions, translate_v0

_CACHE_MAX = 256
_translate_cache: OrderedDict[str, TranspiledQuery] = OrderedDict()


def _cache_key(
    cypher: str,
    mapping: MappingBundle,
    params: dict[str, Any] | None,
    extensions: ExtensionPolicy | None = None,
    registry: ExtensionRegistry | None = None,
    case_insensitive_inline_strings: bool = False,
    cypher_column_names: bool = False,
    temporal_strings: bool = False,
) -> str:
    """Build a deterministic cache key from inputs."""
    h = hashlib.sha256()
    h.update(cypher.encode())
    h.update(json.dumps(mapping.physical_mapping, sort_keys=True, default=str).encode())
    h.update(json.dumps(mapping.conceptual_schema, sort_keys=True, default=str).encode())
    if params:
        h.update(json.dumps(params, sort_keys=True, default=str).encode())
    h.update(str(extensions.enabled if extensions else False).encode())
    h.update(str(id(registry) if registry else 0).encode())
    h.update(str(case_insensitive_inline_strings).encode())
    h.update(str(cypher_column_names).encode())
    h.update(str(temporal_strings).encode())
    # Folding on and off give different AQL for the same query.
    h.update(str(folding_enabled()).encode())
    return h.hexdigest()


def clear_translate_cache() -> int:
    """Clear the translation cache. Returns the number of evicted entries."""
    n = len(_translate_cache)
    _translate_cache.clear()
    return n


@dataclass(frozen=True)
class ValidationResult:
    """Result of :func:`validate_cypher_profile` (syntax-only or parse+translate)."""

    ok: bool
    errors: tuple[dict[str, str], ...] = ()

    @property
    def first_error_code(self) -> str | None:
        return self.errors[0]["code"] if self.errors else None


@dataclass(frozen=True)
class TranspiledQuery:
    aql: str
    bind_vars: dict[str, Any]
    warnings: list[dict[str, Any]]
    debug: dict[str, Any] | None = None

    def to_aql_query(self) -> AqlQuery:
        return AqlQuery(text=self.aql, bind_vars=self.bind_vars, debug=self.debug)

    @classmethod
    def from_aql_query(cls, q: AqlQuery) -> TranspiledQuery:
        w: list[dict[str, Any]] = [{"message": m} for m in q.warnings] if q.warnings else []
        return cls(aql=q.text, bind_vars=q.bind_vars, warnings=w, debug=q.debug)


def translate_options(
    extensions: ExtensionPolicy | None,
    registry: ExtensionRegistry | None,
    case_insensitive_inline_strings: bool,
    cypher_column_names: bool,
    temporal_strings: bool = False,
) -> TranslateOptions:
    """The :class:`TranslateOptions` of a :func:`translate` call's arguments."""
    return TranslateOptions(
        extensions=(extensions or ExtensionPolicy(enabled=False)),
        registry=registry,
        case_insensitive_inline_strings=case_insensitive_inline_strings,
        cypher_column_names=cypher_column_names,
        temporal_strings=temporal_strings,
    )


def get_cypher_profile() -> dict[str, Any]:
    """
    Return a JSON-serializable **Arango Cypher profile** manifest for agents and gateways.

    Describes the declared v0 translator surface, limitations, and extension policy
    defaults. See ``docs/python_prd.md`` §2A.0.
    """
    return build_cypher_profile()


def validate_cypher_profile(
    cypher: str,
    *,
    mapping: MappingBundle | None = None,
    extensions: ExtensionPolicy | None = None,
    params: dict[str, Any] | None = None,
) -> ValidationResult:
    """
    Validate Cypher against the current translator profile.

    - If ``mapping`` is **None**: **syntax only** (successful ANTLR parse).
    - If ``mapping`` is set: parse and attempt :func:`translate` (same rules as execution path).

    On failure returns :class:`ValidationResult` with ``ok=False`` and structured
    ``errors`` entries ``{"code", "message"}`` (typically ``CoreError.code``).
    """
    if not isinstance(cypher, str) or not cypher.strip():
        return ValidationResult(
            ok=False,
            errors=({"code": "INVALID_ARGUMENT", "message": "cypher must be a non-empty string"},),
        )

    try:
        parse_cypher(cypher)
    except CoreError as e:
        return ValidationResult(ok=False, errors=({"code": e.code, "message": str(e)},))

    if mapping is None:
        return ValidationResult(ok=True, errors=())

    try:
        translate(cypher, mapping=mapping, extensions=extensions, params=params)
    except CoreError as e:
        return ValidationResult(ok=False, errors=({"code": e.code, "message": str(e)},))

    return ValidationResult(ok=True, errors=())


def translate(
    cypher: str,
    *,
    mapping: MappingBundle | None = None,
    extensions: ExtensionPolicy | None = None,
    registry: ExtensionRegistry | None = None,
    params: dict[str, Any] | None = None,
    case_insensitive_inline_strings: bool = False,
    cypher_column_names: bool = False,
    temporal_strings: bool = False,
) -> TranspiledQuery:
    """Translate a Cypher query to AQL using the provided schema mapping.

    *registry* is an optional :class:`ExtensionRegistry` carrying ``arango.*``
    function/procedure compilers.  When supplied, the translator delegates
    ``arango.bm25()``, ``arango.tfidf()`` etc. to registered compilers.

    *case_insensitive_inline_strings* makes an inline string property
    (``(n {name: 'alice'})``) match regardless of case. Cypher compares
    exactly, which is the default, and only the exact form can use an index.

    *cypher_column_names* names an unaliased RETURN column by its expression
    as written (``n.name``, ``count(*)``), as openCypher does. Off by
    default: the column is the inferred short key (``name``, ``count``) that
    existing API, UI and NL clients read.

    *temporal_strings* reads a string in one of Neo4j's temporal forms
    (``'12:31:14+01:00'``, ``'P1Y2M'``) as the temporal value it spells where
    its type is only known at run time: accessors (``d.week``), ``+`` / ``-``
    with durations, ``*`` / ``/`` of a duration, and ORDER BY by instant. Off
    by default: such a string is then text.

    A query that AQL cannot run as one statement — a write followed by a read
    of what it wrote, a DELETE beside a MERGE, several MERGEs on one
    collection — raises :class:`CoreError` whose message says so and names
    :func:`arango_cypher.translate_plan`, which compiles it into several
    statements for :func:`arango_cypher.execute_plan` to run in one
    transaction.
    """
    try:
        return _translate_one(
            cypher,
            mapping=mapping,
            extensions=extensions,
            registry=registry,
            params=params,
            case_insensitive_inline_strings=case_insensitive_inline_strings,
            cypher_column_names=cypher_column_names,
            temporal_strings=temporal_strings,
        )
    except CoreError as e:
        hint = _plan_hint(
            cypher,
            mapping=mapping,
            params=params,
            options=translate_options(
                extensions, registry, case_insensitive_inline_strings, cypher_column_names, temporal_strings
            ),
        )
        if hint is None:
            raise
        raise CoreError(f"{e} {hint}", code=e.code) from e


#: Appended to the error of a query a multi-statement plan can run.
PLAN_HINT = (
    "This query needs {n} AQL statements: use arango_cypher.translate_plan() and "
    "arango_cypher.execute_plan() to run them in one transaction."
)


def _plan_hint(
    cypher: str, *, mapping: MappingBundle | None, params: dict[str, Any] | None, options: TranslateOptions
) -> str | None:
    """:data:`PLAN_HINT` when *cypher*, which failed as one statement, runs as a plan."""
    from .plans import lower_to_steps

    if mapping is None or not isinstance(cypher, str):
        return None
    try:
        steps = lower_to_steps(
            cypher, mapping=mapping, params=params, options=options, wrap=TranspiledQuery.from_aql_query
        )
    except CoreError:
        return None
    return PLAN_HINT.format(n=len(steps)) if steps else None


def _translate_one(
    cypher: str,
    *,
    mapping: MappingBundle | None,
    extensions: ExtensionPolicy | None,
    registry: ExtensionRegistry | None,
    params: dict[str, Any] | None,
    case_insensitive_inline_strings: bool,
    cypher_column_names: bool,
    temporal_strings: bool = False,
) -> TranspiledQuery:
    """:func:`translate` without the plan hint: one statement, or the error."""
    if not isinstance(cypher, str) or not cypher.strip():
        raise CoreError("cypher must be a non-empty string", code="INVALID_ARGUMENT")

    if mapping is None:
        raise CoreError("mapping is required", code="INVALID_ARGUMENT")

    key = _cache_key(
        cypher,
        mapping,
        params,
        extensions,
        registry,
        case_insensitive_inline_strings,
        cypher_column_names,
        temporal_strings,
    )
    cached = _translate_cache.get(key)
    if cached is not None:
        _translate_cache.move_to_end(key)
        return cached

    options = translate_options(
        extensions, registry, case_insensitive_inline_strings, cypher_column_names, temporal_strings
    )
    q = translate_v0(cypher, mapping=mapping, params=params, options=options)
    result = TranspiledQuery.from_aql_query(q)

    _translate_cache[key] = result
    if len(_translate_cache) > _CACHE_MAX:
        _translate_cache.popitem(last=False)

    return result


def execute(
    cypher: str,
    *,
    db: Any,
    mapping: MappingBundle | None = None,
    extensions: ExtensionPolicy | None = None,
    registry: ExtensionRegistry | None = None,
    params: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Translate a Cypher query and execute the resulting AQL against *db*.

    *db* must be a ``python-arango`` ``Database`` instance (or compatible).
    Returns the AQL cursor of a query that fits one statement. A query that
    needs several (:func:`arango_cypher.translate_plan`) runs as a plan in one
    stream transaction (:func:`arango_cypher.execute_plan`), and its result
    rows come back as a list.
    """
    from .plan_exec import execute_plan
    from .plans import translate_plan

    plan = translate_plan(
        cypher,
        mapping=mapping,
        extensions=extensions,
        registry=registry,
        params=params,
    )
    if not plan.is_single_statement:
        return execute_plan(db, plan, batch_size=kwargs.get("batch_size"))
    executor = AqlExecutor(db=db)
    return executor.execute(plan.steps[0].query.to_aql_query(), **kwargs)
