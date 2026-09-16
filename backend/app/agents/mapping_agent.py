"""Mapping Agent (L3) -- proposes BI-layer tables.

Every uploaded *structured* file (CSV/Excel/Database/JSON tables -- PDFs,
images, and other unstructured/semi-structured sources are excluded even if
a parser happened to pull a table out of one, since those aren't systematic
tabular data) is standardized into candidate target columns; where two
files share a standardized column with real overlapping values (not just a
name match), a cross-file joined table is proposed with an explicit
SQL-style join condition and measured join quality (executed over every
row, not a sample). Files that don't join to anything still get a
standalone standardized-table proposal.

Each proposal is named after a recognized BI entity (customer, transaction,
staff, ...) when its columns match one closely enough, instead of a generic
"X enriched with Y" label -- so the tab reads like an actual BI table
catalog rather than a description of how two files happened to be joined.

Purely structural (column names + sample values + row overlap), same as
Data Quality -- anything derivable from the data's own structure shouldn't
be left to a model to guess. Per-column cleaning comments are pulled
straight from the Data Quality agent's already-computed issues rather than
re-deriving them here, so the two tabs never disagree about the same column.

This is what powers both the Business Use Cases tab (the "what for") and
the Source -> Target Mapping tab (the "exactly how") -- same list of
BITableProposal, two views.
"""
import re

from app.models.schemas import BITableColumn, BITableProposal, DomainResult, TableBlock

# Canonical aliases: normalized token -> standardized token. A small,
# hand-picked MVP set covering the most common business-entity columns;
# anything not matched here just keeps its own normalized form -- this is
# meant to be tuned per deployment as real column-name conventions show up.
_ALIASES = {
    "cust": "customer", "client": "customer",
    "org": "company", "organization": "company", "vendor": "company", "supplier": "company",
    "amt": "amount", "total": "amount", "sum": "amount", "value": "amount",
    "dt": "date", "timestamp": "date", "created": "date", "updated": "date",
    "tel": "phone", "mobile": "phone", "telephone": "phone",
    "addr": "address",
    "mail": "email",
    "fullname": "name",
    "uuid": "id", "identifier": "id", "pk": "id",
    "desc": "description",
    "qty": "quantity",
    "curr": "currency",
}

_ID_RE = re.compile(r"(^id$|_id$|^id_|_id_)")

# Below this share of orphaned rows, a mismatch is treated as normal data
# noise (e.g. a handful of test/void records) rather than a reportable gap.
_ORPHAN_RATIO_THRESHOLD = 0.05

# Only these categories are genuinely systematic tabular data -- a table
# Docling happens to pull out of a PDF, or a docx table, isn't a candidate
# for a BI-layer table the same way a CSV/Excel/DB export is.
_STRUCTURED_CATEGORIES = {"csv", "excel", "json", "database"}

# Recognized BI entity archetypes, scored by how many of a table's
# standardized columns hit each archetype's keyword set. Deliberately a
# fixed, hand-picked list rather than something inferred per deployment --
# same "small MVP set, tune as real conventions show up" rationale as
# _ALIASES above.
_ENTITY_ARCHETYPES: dict[str, set[str]] = {
    "Customer": {"customer", "email", "phone", "address", "name"},
    "Staff": {"staff", "employee", "department", "role", "hire", "salary", "manager"},
    "Student": {"student", "grade", "enrollment", "gpa", "guardian"},
    "Transaction": {"transaction", "payment", "invoice", "order", "txn", "amount"},
    "Product": {"product", "sku", "category", "inventory", "price"},
    "Vendor": {"company", "vendor", "supplier", "contract"},
    "School": {"school", "campus", "district"},
    "Facility": {"facility", "site", "building", "location"},
    "Asset": {"asset", "equipment", "serial"},
    # Investigation/compliance-flavored archetypes -- this app's structured
    # uploads often skew toward due-diligence and AML-style material
    # (transaction ledgers, counterparty lists, case files), not just
    # generic retail/HR data, so a table naming itself after who it
    # actually represents (a counterparty, a case) reads far more like a
    # usable investigative artifact than a generic "X enriched with Y".
    "Counterparty": {"counterparty", "payee", "beneficiary", "payer", "sender", "receiver"},
    "Case": {"case", "matter", "investigation", "incident", "alert"},
    "Contract": {"contract", "agreement", "clause", "term"},
}
_ENTITY_MIN_SCORE = 2


def _tokenize(column: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", column)  # camelCase -> camel_Case
    return [t for t in re.split(r"[^a-zA-Z0-9]+", spaced.lower()) if t]


def standardize_column(column: str) -> str:
    tokens = _tokenize(column) or ["field"]
    mapped = [_ALIASES.get(t, t) for t in tokens]
    # Collapse consecutive duplicates produced by alias expansion, e.g.
    # "customer" + "id" already reads as "customer_id".
    deduped: list[str] = []
    for t in mapped:
        if not deduped or deduped[-1] != t:
            deduped.append(t)
    return "_".join(deduped)


def guess_column_type(column: str, samples: list[str]) -> str:
    norm = column.lower()
    if _ID_RE.search(norm):
        return "id"
    if "email" in norm:
        return "email"
    if "phone" in norm or "tel" in norm:
        return "phone"
    if any(k in norm for k in ("date", "dt", "time")):
        return "date"
    if any(k in norm for k in ("amount", "price", "total", "qty", "quantity", "amt", "count")):
        return "number"
    checked = [s for s in samples if s.strip()][:20]
    if checked:
        numeric_hits = 0
        for s in checked:
            try:
                float(s.replace(",", ""))
                numeric_hits += 1
            except ValueError:
                pass
        if numeric_hits / len(checked) >= 0.8:
            return "number"
    return "text"


def _basename(source_file: str) -> str:
    return source_file.split("/")[-1]


def _table_label(source_file: str, table: TableBlock) -> str:
    name = table.sheet or table.caption or table.table_id
    return f"{source_file}::{name}"


def _short_name(source_file: str, table: TableBlock) -> str:
    base = table.sheet or table.caption
    if not base:
        base = source_file.split("/")[-1].rsplit(".", 1)[0]
    return base.replace("_", " ").strip().title()


def _target_file(proposed_table_name: str) -> str:
    """The proposed table name, slugified into a filename -- the target
    file IS the proposed table (this app's BI layer is a set of exported
    CSVs, one per proposal), so it's always derived from the name rather
    than an independent value that could drift from it."""
    slug = re.sub(r"[^a-z0-9]+", "_", proposed_table_name.lower()).strip("_")
    return f"{slug or 'table'}.csv"


def classify_entity(standardized_columns: set[str]) -> str | None:
    best_type, best_score = None, 0
    for entity, keywords in _ENTITY_ARCHETYPES.items():
        score = sum(1 for col in standardized_columns for kw in keywords if kw in col)
        if score > best_score:
            best_type, best_score = entity, score
    return best_type if best_score >= _ENTITY_MIN_SCORE else None


def _dq_issue_lookup(results: list[DomainResult]) -> dict[tuple[str, str, str], list[str]]:
    """(source_file, sheet/caption/table_id, raw header) -> that column's
    already-computed Data Quality issues, so the mapping/comments column
    never re-derives what the Validator agent already found."""
    lookup: dict[tuple[str, str, str], list[str]] = {}
    for result in results:
        if not result.quality:
            continue
        for table_q in result.quality.tables:
            for col_q in table_q.columns:
                if col_q.issues:
                    lookup[(result.source_file, table_q.table, col_q.column)] = col_q.issues
    return lookup


def _table_columns(
    source_file: str, label: str, table: TableBlock,
    rule: str, dq_lookup: dict[tuple[str, str, str], list[str]],
) -> list[BITableColumn]:
    table_key = table.sheet or table.caption or table.table_id
    cols = []
    for col_idx, header in enumerate(table.headers):
        if not header or not header.strip():
            continue
        samples = [row[col_idx] for row in table.rows[:20] if col_idx < len(row) and row[col_idx]]
        issues = dq_lookup.get((source_file, table_key, header))
        cols.append(BITableColumn(
            target_column=standardize_column(header),
            source_file=source_file,
            source_table=label,
            source_column=header,
            data_type_guess=guess_column_type(header, samples),
            sample_values=samples[:5],
            rule=rule,
            comments="; ".join(issues) if issues else None,
        ))
    return cols


def build_bi_tables(results: list[DomainResult]) -> list[BITableProposal]:
    # Flat list of every (result, table, label) with a header row, plus a
    # target_column -> occurrences index for join detection. Restricted to
    # genuinely structured sources -- see _STRUCTURED_CATEGORIES.
    entries: list[tuple[DomainResult, TableBlock, str]] = []
    groups: dict[str, list[tuple[int, DomainResult, TableBlock, str]]] = {}

    for result in results:
        if result.domain not in _STRUCTURED_CATEGORIES:
            continue
        for table in result.tables:
            if not table.headers:
                continue
            label = _table_label(result.source_file, table)
            entries.append((result, table, label))
            for col_idx, header in enumerate(table.headers):
                if not header or not header.strip():
                    continue
                target = standardize_column(header)
                groups.setdefault(target, []).append((col_idx, result, table, label))

    dq_lookup = _dq_issue_lookup(results)
    proposals: list[BITableProposal] = []
    joined_keys: set[tuple[str, str]] = set()   # (source_file, table_id) already used in a join
    seen_pairs: set[frozenset] = set()

    for target, occurrences in groups.items():
        distinct_files = {r.source_file for _, r, _, _ in occurrences}
        if len(distinct_files) < 2:
            continue
        for i in range(len(occurrences)):
            for j in range(i + 1, len(occurrences)):
                idx_a, result_a, table_a, label_a = occurrences[i]
                idx_b, result_b, table_b, label_b = occurrences[j]
                if result_a.source_file == result_b.source_file:
                    continue
                pair_key = frozenset({(result_a.source_file, table_a.table_id), (result_b.source_file, table_b.table_id)})
                if pair_key in seen_pairs:
                    continue

                values_a = {row[idx_a].strip().lower() for row in table_a.rows if idx_a < len(row) and row[idx_a]}
                values_b = {row[idx_b].strip().lower() for row in table_b.rows if idx_b < len(row) and row[idx_b]}
                if not values_a or not values_b:
                    continue
                shared = values_a & values_b
                union = values_a | values_b
                overlap = len(shared) / len(union) if union else 0.0
                # Require both a minimum absolute overlap and a minimum
                # ratio -- name match alone (e.g. two unrelated "name"
                # columns) isn't enough evidence of an actual join key.
                if len(shared) < 2 or overlap < 0.05:
                    continue

                seen_pairs.add(pair_key)
                joined_keys.add((result_a.source_file, table_a.table_id))
                joined_keys.add((result_b.source_file, table_b.table_id))

                # The side with more rows is the more likely "many"/FK side
                # (e.g. many orders per customer); the smaller side is the
                # "one"/PK side -- a simple, deterministic heuristic, not a
                # guess about business meaning.
                if len(table_a.rows) >= len(table_b.rows):
                    child_label, child_table, child_idx = label_a, table_a, idx_a
                    parent_label, parent_table, parent_idx = label_b, table_b, idx_b
                    child_file, parent_file = result_a.source_file, result_b.source_file
                    child_values, parent_values = values_a, values_b
                    matched, child_only, parent_only = len(shared), len(values_a - shared), len(values_b - shared)
                else:
                    child_label, child_table, child_idx = label_b, table_b, idx_b
                    parent_label, parent_table, parent_idx = label_a, table_a, idx_a
                    child_file, parent_file = result_b.source_file, result_a.source_file
                    child_values, parent_values = values_b, values_a
                    matched, child_only, parent_only = len(shared), len(values_b - shared), len(values_a - shared)

                child_name = _short_name(child_file, child_table)
                parent_name = _short_name(parent_file, parent_table)
                child_base, parent_base = _basename(child_file), _basename(parent_file)
                child_col, parent_col = child_table.headers[child_idx], parent_table.headers[parent_idx]
                join_condition = (
                    f"FROM {child_base} LEFT JOIN {parent_base} "
                    f"ON {child_base}.{child_col} = {parent_base}.{parent_col}"
                )
                total_child = len(child_values)
                quality_bits = [f"{matched} of {total_child} matched ({matched / total_child:.0%})" if total_child else f"{matched} matched"]
                if total_child and child_only / total_child >= _ORPHAN_RATIO_THRESHOLD:
                    quality_bits.append(
                        f"{child_only} {child_name} row(s) reference a {parent_name} key not found anywhere "
                        f"else in this upload -- worth a closer look (missing/uploaded-separately record, or "
                        f"a genuine data-entry discrepancy)"
                    )
                if parent_only:
                    quality_bits.append(f"{parent_only} {parent_name} row(s) have no corresponding {child_name} activity")

                columns = (
                    _table_columns(child_file, child_label, child_table, join_condition, dq_lookup)
                    + _table_columns(parent_file, parent_label, parent_table, join_condition, dq_lookup)
                )

                # Name after a recognized BI entity when the fact/many
                # (child) side's own columns are a clear match -- the grain
                # of this table is the child side, so naming from the
                # parent instead would read as "Customer" for a table whose
                # purpose/grain text still says "one row per Transaction
                # record". Falls back to the old descriptive name when the
                # child side isn't a clear match for anything recognized.
                child_cols = {c.target_column for c in columns if c.source_file == child_file}
                entity = classify_entity(child_cols)
                name = entity or f"{child_name} enriched with {parent_name}"

                proposals.append(BITableProposal(
                    name=name,
                    purpose=f"Consolidates every {child_name} record with its {parent_name} context via "
                             f"{target}, giving analysts one queryable view to trace how {child_name.lower()} "
                             f"activity connects to {parent_name.lower()} records -- cross-referencing, "
                             f"due-diligence, and anomaly review that would otherwise mean manually joining "
                             f"separate source files by hand.",
                    grain=f"One row per {child_name} record.",
                    source_files=sorted({child_file, parent_file}),
                    columns=columns,
                    join_logic=join_condition,
                    join_quality="; ".join(quality_bits),
                    target_file=_target_file(name),
                ))

    # Standalone tables: any table never used in a join above still gets a
    # proposal, since a single cleaned/standardized table is still useful
    # BI-layer output even without a cross-file link.
    for result, table, label in entries:
        key = (result.source_file, table.table_id)
        if key in joined_keys:
            continue
        rule = f"Direct load from {_basename(result.source_file)} -- standardized column names, no join."
        columns = _table_columns(result.source_file, label, table, rule, dq_lookup)
        entity = classify_entity({c.target_column for c in columns})
        name = entity or _short_name(result.source_file, table)
        proposals.append(BITableProposal(
            name=name,
            purpose="Standardized, analysis-ready on its own -- no shared key was found linking it to "
                    "another uploaded source, so treat it as an independent line of evidence for now. If "
                    "a connecting reference (a shared ID, name, account number, ...) turns up in another "
                    "file on a future upload, it will be cross-linked automatically.",
            grain=f"One row per record in {label}.",
            source_files=[result.source_file],
            columns=columns,
            join_logic=None,
            join_quality=None,
            target_file=_target_file(name),
        ))

    return proposals
