"""CREATE compilation for the v0 translator: nodes, relationships, the RETURN after."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from arango_query_core import AqlQuery, CoreError, MappingResolver

from .._antlr.CypherParser import CypherParser
from .columns import written_column
from .label_tests import labels_field as entity_labels_field
from .naming import _aql_collection_ref, _pick_bind_key, _strip_label_backticks
from .property_writes import PropertyWrites
from .writes import (
    _append_return,
    _compile_expression,
    _compile_return_object,
    _compile_write_reading_clauses,
    _core_helper,
    _extract_node_var_and_labels,
    _infer_unlabeled_collection,
    _pick_primary_entity_label,
    _property_writes,
    _relationship_direction,
    _supertype_label_fields,
)


def _translate_create_query(
    spq: CypherParser.OC_SinglePartQueryContext,
    *,
    create_clauses: list[CypherParser.OC_CreateContext],
    set_clauses: list[CypherParser.OC_SetContext] | None = None,
    remove_clauses: list | None = None,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> AqlQuery:
    """Translate a single-part query containing CREATE clause(s), optionally
    followed by SET/REMOVE on the created variables."""
    ret = spq.oC_Return()
    # SET/REMOVE on created variables are folded into the inserted documents:
    # a separate UPDATE would write the same collection twice (ERR 1579).
    writes = _property_writes(set_clauses or [], remove_clauses or [], bind_vars=bind_vars)

    # Maps each created variable to the bind key of the collection it was
    # inserted into, so a trailing SET/REMOVE can target the right collection.
    var_collections: dict[str, str] = {}

    # MATCH/UNWIND reading clauses become the FOR prefix the CREATE nests in
    # (``UNWIND [..] AS x CREATE (n {p: x})`` → ``FOR x IN [..] INSERT {p: x}``).
    lines, var_env = _compile_write_reading_clauses(spq, resolver=resolver, bind_vars=bind_vars)
    indent = "  " if lines else ""

    num_creates = len(create_clauses)
    for ci, cc in enumerate(create_clauses):
        force_let = ci < num_creates - 1
        _compile_create(
            cc,
            resolver=resolver,
            bind_vars=bind_vars,
            var_env=var_env,
            lines=lines,
            indent=indent,
            has_return=ret is not None or force_let,
            var_collections=var_collections,
            writes=writes,
        )

    not_created = [v for v in writes.ops if v not in var_collections]
    if not_created:
        raise CoreError(
            f"SET/REMOVE after CREATE targets variable {not_created[0]!r} that was not created in this query",
            code="NOT_IMPLEMENTED",
        )

    if ret is not None:
        _compile_return_for_create(
            ret.oC_ProjectionBody(),
            lines=lines,
            bind_vars=bind_vars,
            indent=indent,
        )

    return AqlQuery(text="\n".join(lines), bind_vars=bind_vars)


@dataclass
class CreateOp:
    """One node or relationship a CREATE pattern makes, in pattern order."""

    kind: str  # "node" or "rel"
    var: str
    labels: list[str] | None = None
    node_ctx: Any = None
    rel_pat: Any = None
    from_var: str = ""
    to_var: str = ""


def create_ops(create_ctx: CypherParser.OC_CreateContext, var_env: dict[str, str]) -> list[CreateOp]:
    """The nodes and relationships a CREATE makes. A node already in
    *var_env* is referenced, not created; each created node is added to it."""
    pattern = create_ctx.oC_Pattern()
    parts = pattern.oC_PatternPart() or []

    ops: list[CreateOp] = []
    create_counter = 0
    anon_counter = 0

    def _unique_anon(var: str) -> str:
        nonlocal anon_counter
        if var != "_anon" or var not in var_env:
            return var
        while f"_anon{anon_counter}" in var_env:
            anon_counter += 1
        result = f"_anon{anon_counter}"
        anon_counter += 1
        return result

    def _refuse_rebinding(var: str, node_ctx: Any) -> None:
        # openCypher VariableAlreadyBound: CREATE may reference a bound node
        # but not give it labels or properties.
        if var in var_env and (node_ctx.oC_NodeLabels() is not None or node_ctx.oC_Properties() is not None):
            raise CoreError(
                f"CREATE cannot add labels or properties to the already bound variable {var!r}",
                code="UNSUPPORTED",
            )

    for part in parts:
        anon = part.oC_AnonymousPatternPart()
        elem = anon.oC_PatternElement()
        node = elem.oC_NodePattern()
        chains = elem.oC_PatternElementChain() or []

        start_var, start_labels = _extract_node_var_and_labels(node, default_var="_anon")
        start_var = _unique_anon(start_var)
        _refuse_rebinding(start_var, node)

        if start_var not in var_env:
            ops.append(CreateOp(kind="node", var=start_var, labels=start_labels, node_ctx=node))
            var_env[start_var] = start_var

        current_var = start_var
        for chain in chains:
            rel_pat = chain.oC_RelationshipPattern()
            end_node = chain.oC_NodePattern()
            if rel_pat is None or end_node is None:
                raise CoreError("Invalid CREATE pattern", code="UNSUPPORTED")

            end_var, end_labels = _extract_node_var_and_labels(end_node, default_var="_anon")
            end_var = _unique_anon(end_var)
            _refuse_rebinding(end_var, end_node)

            if end_var not in var_env:
                ops.append(CreateOp(kind="node", var=end_var, labels=end_labels, node_ctx=end_node))
                var_env[end_var] = end_var

            detail = rel_pat.oC_RelationshipDetail()
            rel_var_name = ""
            if detail is not None and detail.oC_Variable() is not None:
                rel_var_name = detail.oC_Variable().getText().strip()
            if not rel_var_name:
                rel_var_name = f"_c{create_counter}"
                create_counter += 1

            direction = _relationship_direction(rel_pat)
            if direction == "ANY":
                # openCypher RequiresDirectedRelationship.
                raise CoreError("CREATE requires a directed relationship", code="UNSUPPORTED")
            if direction == "INBOUND":
                from_v, to_v = end_var, current_var
            else:
                from_v, to_v = current_var, end_var

            ops.append(
                CreateOp(
                    kind="rel",
                    var=rel_var_name,
                    rel_pat=rel_pat,
                    from_var=from_v,
                    to_var=to_v,
                )
            )

            current_var = end_var

    return ops


def _compile_create(
    create_ctx: CypherParser.OC_CreateContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    var_env: dict[str, str],
    lines: list[str],
    indent: str,
    has_return: bool,
    var_collections: dict[str, str] | None = None,
    writes: PropertyWrites | None = None,
) -> None:
    """Compile a single CREATE clause into AQL INSERT lines.

    *writes* are SET/REMOVE items on the created variables, folded into the
    documents being inserted.
    """
    ops = create_ops(create_ctx, var_env)

    for i, op in enumerate(ops):
        is_last = i == len(ops) - 1
        needs_let = has_return or not is_last

        if op.kind == "node":
            _compile_create_node(
                op.var,
                op.labels or [],
                op.node_ctx,
                resolver=resolver,
                bind_vars=bind_vars,
                lines=lines,
                indent=indent,
                needs_let=needs_let,
                var_collections=var_collections,
                writes=writes,
            )
        elif op.kind == "rel":
            _compile_create_rel(
                op.var,
                op.rel_pat,
                op.from_var,
                op.to_var,
                resolver=resolver,
                bind_vars=bind_vars,
                lines=lines,
                indent=indent,
                needs_let=needs_let,
                var_collections=var_collections,
                writes=writes,
            )


def _find_or_create_collection_bind_key(
    base: str,
    collection_name: str,
    bind_vars: dict[str, Any],
) -> str:
    """Reuse an existing bind key if it already points to the same collection."""
    if base in bind_vars and bind_vars[base] == collection_name:
        return base
    i = 2
    while f"{base}{i}" in bind_vars:
        if bind_vars[f"{base}{i}"] == collection_name:
            return f"{base}{i}"
        i += 1
    key = _pick_bind_key(base, bind_vars)
    bind_vars[key] = collection_name
    return key


def _compile_create_node(
    var: str,
    labels: list[str],
    node_ctx: CypherParser.OC_NodePatternContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    lines: list[str],
    indent: str,
    needs_let: bool,
    var_collections: dict[str, str] | None = None,
    writes: PropertyWrites | None = None,
) -> None:
    """Compile a single node INSERT."""
    doc, coll_key, extra_fields = create_node_doc(labels, node_ctx, resolver=resolver, bind_vars=bind_vars)
    if writes is not None:
        doc = writes.folded_into(var, doc, extra_fields)
    coll_ref = _aql_collection_ref(coll_key)
    if var_collections is not None:
        var_collections[var] = coll_key

    if needs_let:
        lines.append(f"{indent}LET {var} = FIRST(INSERT {doc} INTO {coll_ref} RETURN NEW)")
    else:
        lines.append(f"{indent}INSERT {doc} INTO {coll_ref}")


def create_node_doc(
    labels: list[str],
    node_ctx: CypherParser.OC_NodePatternContext,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[str, str, list[str]]:
    """The document a created node is inserted as: ``(doc, collection bind
    key, mapping fields)``, the fields being the type discriminator and
    supertype labels the mapping requires."""
    param_ref = _create_props_param_ref(node_ctx.oC_Properties(), bind_vars)
    props = [] if param_ref else _compile_create_props(node_ctx.oC_Properties(), bind_vars)
    # A null property is not stored (Cypher: setting null removes it).
    props = [(k, v) for k, v in props if v.strip() != "null"]
    extra_fields: list[str] = []

    if not labels:
        coll_name = _infer_unlabeled_collection(resolver)
        coll_key = _find_or_create_collection_bind_key("@collection", coll_name, bind_vars)
    else:
        primary = _pick_primary_entity_label(labels, resolver)
        entity_mapping = resolver.resolve_entity(_strip_label_backticks(primary))
        style = entity_mapping.get("style")
        if style in ("MULTI_LABEL", "UNION"):
            raise CoreError(
                f"Cannot CREATE a node by its supertype {primary!r}: name a concrete label",
                code="UNSUPPORTED",
            )

        coll_key = _find_or_create_collection_bind_key(
            "@collection",
            entity_mapping["collectionName"],
            bind_vars,
        )
        if style == "LABEL":
            type_field = entity_mapping.get("typeField", "type")
            tv_key = _pick_bind_key("typeValue", bind_vars)
            bind_vars[tv_key] = entity_mapping.get("typeValue")
            extra_fields.append(f"{type_field}: @{tv_key}")
            lf = entity_labels_field(entity_mapping)
            all_labels = list(dict.fromkeys(_strip_label_backticks(lab) for lab in labels))
            if lf is not None and len(all_labels) > 1:
                # Every label of a multi-label node (label_tests docstring).
                lv_key = _pick_bind_key("nodeLabels", bind_vars)
                bind_vars[lv_key] = all_labels
                extra_fields.append(f"{lf}: @{lv_key}")
        elif style != "COLLECTION":
            raise CoreError(f"Unsupported entity mapping style: {style}", code="INVALID_MAPPING")
        concrete = _strip_label_backticks(primary)
        for labels_field, values in _supertype_label_fields(
            concrete, entity_mapping["collectionName"], resolver
        ).items():
            lv_key = _pick_bind_key("labelsValue", bind_vars)
            bind_vars[lv_key] = values
            extra_fields.append(f"{labels_field}: @{lv_key}")

    return _build_insert_doc(props, extra_fields, base=param_ref), coll_key, extra_fields


def _compile_create_rel(
    var: str,
    rel_pat: CypherParser.OC_RelationshipPatternContext,
    from_var: str,
    to_var: str,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
    lines: list[str],
    indent: str,
    needs_let: bool,
    var_collections: dict[str, str] | None = None,
    writes: PropertyWrites | None = None,
) -> None:
    """Compile a single relationship INSERT."""
    doc, edge_coll_key, extra_fields = create_rel_doc(
        rel_pat, from_var, to_var, resolver=resolver, bind_vars=bind_vars
    )
    if writes is not None:
        doc = writes.folded_into(var, doc, extra_fields)
    coll_ref = _aql_collection_ref(edge_coll_key)
    if var_collections is not None:
        var_collections[var] = edge_coll_key

    if needs_let:
        lines.append(f"{indent}LET {var} = FIRST(INSERT {doc} INTO {coll_ref} RETURN NEW)")
    else:
        lines.append(f"{indent}INSERT {doc} INTO {coll_ref}")


def create_rel_doc(
    rel_pat: CypherParser.OC_RelationshipPatternContext,
    from_var: str,
    to_var: str,
    *,
    resolver: MappingResolver,
    bind_vars: dict[str, Any],
) -> tuple[str, str, list[str]]:
    """The document a created relationship is inserted as: ``(doc, edge
    collection bind key, mapping fields)``, the fields being ``_from``,
    ``_to`` and the type discriminator."""
    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        raise CoreError("Relationship type is required for CREATE", code="UNSUPPORTED")
    if detail.oC_RangeLiteral() is not None:
        # openCypher: CreatingVarLength, a compile-time SyntaxError.
        raise CoreError("CREATE cannot create a variable-length relationship", code="UNSUPPORTED")
    types_ctx = detail.oC_RelationshipTypes()
    if types_ctx is None:
        raise CoreError("Relationship type is required for CREATE", code="UNSUPPORTED")
    types = types_ctx.oC_RelTypeName()
    if not types or len(types) != 1:
        raise CoreError("Exactly one relationship type is required for CREATE", code="UNSUPPORTED")
    rel_type = types[0].getText().strip()

    r_map = resolver.resolve_relationship(_strip_label_backticks(rel_type))
    r_style = r_map.get("style")

    edge_coll_name = r_map.get("edgeCollectionName") or r_map.get("collectionName")
    if not isinstance(edge_coll_name, str) or not edge_coll_name:
        raise CoreError(
            f"Invalid relationship mapping collection for: {rel_type}",
            code="INVALID_MAPPING",
        )

    edge_coll_key = _find_or_create_collection_bind_key(
        "@edgeCollection",
        edge_coll_name,
        bind_vars,
    )

    extra_fields = [f"_from: {from_var}._id", f"_to: {to_var}._id"]

    if r_style == "GENERIC_WITH_TYPE":
        type_field = r_map.get("typeField", "type")
        rtv_key = _pick_bind_key("relTypeValue", bind_vars)
        bind_vars[rtv_key] = r_map.get("typeValue")
        extra_fields.append(f"{type_field}: @{rtv_key}")
    elif r_style == "EMBEDDED":
        raise CoreError(
            "EMBEDDED relationships are not supported for CREATE",
            code="UNSUPPORTED",
        )
    elif r_style != "DEDICATED_COLLECTION":
        raise CoreError(
            f"Unsupported relationship mapping style for CREATE: {r_style}",
            code="INVALID_MAPPING",
        )

    rel_props_ctx = detail.oC_Properties()
    param_ref = _create_props_param_ref(rel_props_ctx, bind_vars)
    props = [] if param_ref else _compile_create_rel_props(rel_pat, bind_vars)
    # A null property is not stored (Cypher: setting null removes it).
    props = [(k, v) for k, v in props if v.strip() != "null"]
    return _build_insert_doc(props, extra_fields, base=param_ref), edge_coll_key, extra_fields


def _compile_create_props(
    props_ctx: CypherParser.OC_PropertiesContext | None,
    bind_vars: dict[str, Any],
) -> list[tuple[str, str]]:
    """Extract property key-value pairs from a pattern's properties for INSERT."""
    if props_ctx is None:
        return []
    if props_ctx.oC_Parameter() is not None:
        raise CoreError(
            "Parameterized properties are not supported in CREATE",
            code="NOT_IMPLEMENTED",
        )
    m = props_ctx.oC_MapLiteral()
    if m is None:
        return []
    keys = m.oC_PropertyKeyName() or []
    vals = m.oC_Expression() or []
    if len(keys) != len(vals):
        raise CoreError("Invalid properties map in CREATE", code="UNSUPPORTED")
    out: list[tuple[str, str]] = []
    for k_ctx, v_ctx in zip(keys, vals, strict=False):
        key = k_ctx.getText().strip()
        if not key:
            raise CoreError("Invalid property key in CREATE", code="UNSUPPORTED")
        expr = _compile_expression(v_ctx, bind_vars)
        out.append((key, expr))
    return out


def _compile_create_rel_props(
    rel_pat: CypherParser.OC_RelationshipPatternContext,
    bind_vars: dict[str, Any],
) -> list[tuple[str, str]]:
    """Extract properties from a relationship pattern for CREATE."""
    detail = rel_pat.oC_RelationshipDetail()
    if detail is None:
        return []
    return _compile_create_props(detail.oC_Properties(), bind_vars)


def _create_props_param_ref(
    props_ctx: CypherParser.OC_PropertiesContext | None,
    bind_vars: dict[str, Any],
) -> str | None:
    """Return the AQL bind ref (``@name``) when a pattern's properties are a
    whole-map parameter (``CREATE (n $props)``), else ``None``."""
    if props_ctx is None:
        return None
    param = props_ctx.oC_Parameter()
    if param is None:
        return None
    return _compile_expression(param, bind_vars)


def _build_insert_doc(
    props: list[tuple[str, str]],
    extra_fields: list[str] | None = None,
    base: str | None = None,
) -> str:
    """Build an AQL object literal for INSERT.

    When ``base`` is set (a whole-map parameter like ``@props``), the explicit
    ``extra_fields``/``props`` are merged over it via ``MERGE`` so server-set
    fields (type discriminator, ``_from``/``_to``) still apply.
    """
    fields: list[str] = list(extra_fields or [])
    fields.extend(f"{k}: {v}" for k, v in props)
    inner = "{" + ", ".join(fields) + "}" if fields else "{}"
    if base:
        return base if not fields else f"MERGE({base}, {inner})"
    return inner


def projection_reshapes_rows(proj: CypherParser.OC_ProjectionBodyContext) -> bool:
    """Whether a projection aggregates, sorts, pages or de-duplicates its rows."""
    if (
        proj.DISTINCT() is not None
        or proj.oC_Order() is not None
        or proj.oC_Skip() is not None
        or proj.oC_Limit() is not None
    ):
        return True
    aggregate_call = _core_helper("_aggregate_call")
    parse_collect = _core_helper("_parse_collect")
    nested = _core_helper("nested_aggregates")
    return any(
        aggregate_call(it.oC_Expression().getText().strip()) is not None
        or parse_collect(it.oC_Expression().getText().strip()) is not None
        or bool(nested(it.oC_Expression()))
        for it in proj.oC_ProjectionItems().oC_ProjectionItem()
    )


def _compile_return_for_create(
    proj: CypherParser.OC_ProjectionBodyContext,
    *,
    lines: list[str],
    bind_vars: dict[str, Any],
    indent: str = "",
) -> None:
    """Compile a RETURN clause for a CREATE/MERGE query (respects indent level).

    A RETURN that aggregates, sorts, pages or de-duplicates is the general
    RETURN (``_append_return``); the plain one stays a bare ``RETURN``.
    """
    if projection_reshapes_rows(proj):
        _append_return(proj, lines=lines, bind_vars=bind_vars)
        return
    items_ctx = proj.oC_ProjectionItems()
    items = items_ctx.oC_ProjectionItem()
    if not items:
        raise CoreError("RETURN items required", code="UNSUPPORTED")

    compiled_items: list[tuple[str | None, str]] = []
    written_names: dict[int, str] = {}
    for it in items:
        expr = _compile_expression(it.oC_Expression(), bind_vars)
        alias = it.oC_Variable().getText().strip() if it.oC_Variable() is not None else None
        written = written_column(it)
        if written is not None:
            written_names[len(compiled_items)] = written
        compiled_items.append((alias, expr))

    if len(compiled_items) == 1 and compiled_items[0][0] is None:
        lines.append(f"{indent}RETURN {compiled_items[0][1]}")
    else:
        lines.append(f"{indent}RETURN " + _compile_return_object(compiled_items, written_names))
