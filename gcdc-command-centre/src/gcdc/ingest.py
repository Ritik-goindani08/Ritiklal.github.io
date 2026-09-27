"""Ingest layer — the only way agents write to the central database.

Every record is a flat dict with a "type" (see RECORD_TYPES). References to
other entities can be given by id or by description, e.g.

    {"type": "outreach",
     "organisation": {"name": "Example Support Coordination", "website": "example-sc.example"},
     "to_email": "intake@example-sc.example",
     "subject": "Support worker capacity — Gold Coast",
     "status": "SENT", "sent_at": "2026-09-22T09:00:00+10:00",
     "external_message_id": "<message-id@mail.example>"}

Organisations are resolved through gcdc.identity (no duplicates), contacts by
email, opportunities by (organisation, type) when not given. Re-sending a
record with the same natural key updates it instead of duplicating it.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable

from gcdc import identity
from gcdc.config import Config
from gcdc.timeutil import now_iso, parse_date, parse_datetime_utc


class IngestError(ValueError):
    """A record could not be accepted. The message says why."""


class SuppressedError(IngestError):
    """Outreach to a suppressed email / domain / organisation was refused."""


# ---------------------------------------------------------------------------
# Schema introspection: column types, enum values and flags come from the DDL.
# ---------------------------------------------------------------------------
@dataclass
class Column:
    name: str
    decl_type: str
    notnull: bool
    has_default: bool
    enum: tuple[str, ...] | None = None
    is_flag: bool = False


_ENUM_RE = re.compile(r"CHECK\s*\(\s*(\w+)\s+IN\s*\(([^)]*)\)\s*\)", re.IGNORECASE)
_SPEC_CACHE: dict[int, dict[str, dict[str, Column]]] = {}

_DATE_COLUMNS = {
    "due_date", "next_action_due", "start_date", "end_date", "invoice_date", "paid_date", "service_period_start",
    "service_period_end", "ndis_screening_expiry", "blue_card_expiry", "police_check_date", "first_aid_expiry",
    "cpr_expiry", "effective_from", "effective_to", "needed_by", "approval_date", "first_work_received_date",
    "expiry_date",
}
_TIMESTAMP_COLUMNS = {"scheduled_for", "scheduled_start"}
_REF_TABLES = {
    "stage_code": None,  # depends on table (opportunity vs partnership)
    "opportunity_type_code": ("ref_opportunity_type", "type_code", "type_name"),
    "source_code": ("ref_opportunity_source", "source_code", "source_name"),
    "service_type_code": ("ref_service_type", "service_type_code", "service_type_name"),
}


def table_spec(conn: sqlite3.Connection, table: str) -> dict[str, Column]:
    cache = _SPEC_CACHE.setdefault(id(conn), {})
    if table in cache:
        return cache[table]
    ddl = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)).fetchone()
    if not ddl:
        raise IngestError(f"unknown table {table}")
    enums = {m.group(1).lower(): tuple(v.strip().strip("'") for v in m.group(2).split(","))
             for m in _ENUM_RE.finditer(ddl[0])}
    cols = {}
    for cid, name, ctype, notnull, default, pk in conn.execute(f"PRAGMA table_info({table})"):
        enum = enums.get(name.lower())
        is_flag = enum is not None and set(enum) == {"0", "1"}
        cols[name] = Column(name, (ctype or "").upper(), bool(notnull), default is not None or bool(pk),
                            None if is_flag else enum, is_flag)
    cache[table] = cols
    return cols


# ---------------------------------------------------------------------------
# Value coercion
# ---------------------------------------------------------------------------
_UNKNOWN_TOKENS = {"", "unknown", "tbc", "tba", "n/a", "na", "none", "null", "-", "—", "?", "not stated",
                   "not publicly verified"}


def clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return None if value.lower() in _UNKNOWN_TOKENS else value
    return value


def to_bool(value: Any) -> int | None:
    value = clean(value)
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return 1 if value else 0
    text = str(value).strip().lower()
    if text in {"1", "y", "yes", "true", "t", "verified", "✓"}:
        return 1
    if text in {"0", "n", "no", "false", "f"}:
        return 0
    raise IngestError(f"not a yes/no value: {value!r}")


def to_number(value: Any) -> float | None:
    value = clean(value)
    if value is None:
        return None
    if isinstance(value, bool):
        raise IngestError(f"not a number: {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = re.sub(r"[,$\s]|AUD|/hr|/hour|hrs?|hours?", "", str(value), flags=re.IGNORECASE)
    try:
        return float(text)
    except ValueError as exc:
        raise IngestError(f"not a number: {value!r}") from exc


def to_enum(value: Any, allowed: tuple[str, ...], column: str) -> str | None:
    value = clean(value)
    if value is None:
        return None
    norm = re.sub(r"[\s\-/]+", "_", str(value).strip()).upper()
    if norm in allowed:
        return norm
    raise IngestError(f"{column}: {value!r} is not one of {', '.join(allowed)}")


@dataclass
class IngestContext:
    conn: sqlite3.Connection
    cfg: Config
    agent_code: str | None = None
    run_id: int | None = None
    source_system: str | None = None
    allow_suppressed: bool = False
    stats: Counter = field(default_factory=Counter)
    warnings: list[str] = field(default_factory=list)

    @property
    def offset(self) -> float:
        return self.cfg.utc_offset_hours

    @property
    def free_mail(self) -> frozenset[str]:
        return self.cfg.free_mail_domains

    @property
    def system(self) -> str:
        return self.source_system or (self.agent_code.lower() if self.agent_code else "manual")


def resolve_region(conn: sqlite3.Connection, value: Any) -> str | None:
    value = clean(value)
    if value is None:
        return None
    text = str(value).strip()
    row = conn.execute("SELECT region_code FROM ref_region WHERE region_code = ?", (text.upper(),)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT region_code FROM ref_region_alias WHERE alias = ?", (text,)).fetchone()
    if row:
        return row[0]
    row = conn.execute("SELECT region_code FROM ref_region WHERE region_name = ? COLLATE NOCASE", (text,)).fetchone()
    if row:
        return row[0]
    # "Gold Coast, Logan" -> first recognised region
    for part in re.split(r"[,;/]| and ", text):
        part = part.strip()
        if part and part != text:
            code = resolve_region(conn, part)
            if code and code != "UNKNOWN":
                return code
    return "UNKNOWN"


def resolve_ref(conn: sqlite3.Connection, table: str, key_col: str, name_col: str, value: Any, column: str) -> str | None:
    value = clean(value)
    if value is None:
        return None
    text = str(value).strip()
    code = re.sub(r"[\s\-/]+", "_", text).upper()
    row = conn.execute(f"SELECT {key_col} FROM {table} WHERE {key_col} = ?", (code,)).fetchone()
    if row:
        return row[0]
    row = conn.execute(f"SELECT {key_col} FROM {table} WHERE {name_col} = ? COLLATE NOCASE", (text,)).fetchone()
    if row:
        return row[0]
    allowed = [r[0] for r in conn.execute(f"SELECT {key_col} FROM {table}")]
    raise IngestError(f"{column}: {value!r} is not one of {', '.join(allowed)}")


def prepare(ctx: IngestContext, table: str, record: dict[str, Any], *, skip: set[str] = frozenset()) -> dict[str, Any]:
    """Coerce a record's fields to the table's columns. Unknown fields -> warning."""
    spec = table_spec(ctx.conn, table)
    out: dict[str, Any] = {}
    for key, raw in record.items():
        if key in skip or key == "type" or key.startswith("_"):
            continue
        col = spec.get(key)
        if col is None:
            ctx.warnings.append(f"{table}: ignored unknown field '{key}'")
            continue
        value = clean(raw)
        if value is None:
            continue
        name = col.name
        if name in ("region_code", "home_region_code"):
            value = resolve_region(ctx.conn, value)
        elif name == "stage_code":
            ref = ("ref_partnership_stage" if table == "partnerships" else "ref_pipeline_stage")
            value = resolve_ref(ctx.conn, ref, "stage_code", "stage_name", value, name)
        elif name in _REF_TABLES and _REF_TABLES[name]:
            t, k, n = _REF_TABLES[name]
            value = resolve_ref(ctx.conn, t, k, n, value, name)
        elif col.is_flag:
            value = to_bool(value)
        elif col.enum:
            value = to_enum(value, col.enum, f"{table}.{name}")
        elif name.endswith("_at") or name in _TIMESTAMP_COLUMNS:
            try:
                value = parse_datetime_utc(value, offset_hours=ctx.offset)
            except ValueError as exc:
                raise IngestError(f"{table}.{name}: {exc}") from exc
        elif name in _DATE_COLUMNS:
            try:
                value = parse_date(value).isoformat()
            except ValueError as exc:
                raise IngestError(f"{table}.{name}: {exc}") from exc
        elif col.decl_type == "REAL":
            value = to_number(value)
        elif col.decl_type == "INTEGER":
            num = to_number(value)
            value = None if num is None else int(num)
        elif name in ("email", "to_email", "from_email", "general_email"):
            norm = identity.normalise_email(value)
            if norm is None:
                raise IngestError(f"{table}.{name}: {value!r} is not a valid email address")
            value = norm
        elif name == "abn":
            value = identity.normalise_abn(value)
            if value is None:
                ctx.warnings.append(f"{table}: ABN {raw!r} failed the ATO checksum and was dropped")
                continue
        else:
            value = str(value) if not isinstance(value, str) else value
        if value is not None:
            out[name] = value
    return out


def _insert(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> int:
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    try:
        cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(values.values()))
    except sqlite3.IntegrityError as exc:
        raise IngestError(f"{table}: {exc}") from exc
    return cur.lastrowid


def _update(conn: sqlite3.Connection, table: str, pk: str, pk_value: int, values: dict[str, Any],
            *, fill_only: bool = False) -> bool:
    values = {k: v for k, v in values.items() if k not in (pk, "created_at", "created_by")}
    if not values:
        return False
    if fill_only:
        sets = ", ".join(f"{k} = COALESCE({k}, ?)" for k in values if k != "updated_at")
        params = [v for k, v in values.items() if k != "updated_at"]
        if "updated_at" in values:
            sets += ", updated_at = ?"
            params.append(values["updated_at"])
    else:
        sets = ", ".join(f"{k} = ?" for k in values)
        params = list(values.values())
    try:
        conn.execute(f"UPDATE {table} SET {sets} WHERE {pk} = ?", (*params, pk_value))
    except sqlite3.IntegrityError as exc:
        raise IngestError(f"{table}: {exc}") from exc
    return True


def _find(conn: sqlite3.Connection, table: str, pk: str, where: str, params: tuple) -> int | None:
    row = conn.execute(f"SELECT {pk} FROM {table} WHERE {where} LIMIT 1", params).fetchone()
    return row[0] if row else None


def _exists(conn: sqlite3.Connection, table: str, pk: str, value: Any) -> bool:
    return value is not None and conn.execute(f"SELECT 1 FROM {table} WHERE {pk} = ?", (value,)).fetchone() is not None


# ---------------------------------------------------------------------------
# Organisations & contacts
# ---------------------------------------------------------------------------
def upsert_organisation(ctx: IngestContext, rec: dict[str, Any]) -> int:
    conn = ctx.conn
    rec = dict(rec)
    external = clean(rec.pop("external_id", None))
    if external and ":" not in str(external):
        external = f"{ctx.system}:{external}"
    if clean(rec.get("region")) and not rec.get("region_code"):
        rec["region_code"] = rec.pop("region")
    rec.pop("region", None)
    values = prepare(ctx, "organisations", rec, skip={"organisation_id"})
    now = now_iso()
    org_id = clean(rec.get("organisation_id"))
    if org_id is not None:
        org_id = int(org_id)
        if not _exists(conn, "organisations", "organisation_id", org_id):
            raise IngestError(f"organisation_id {org_id} does not exist")
        if values.get("name"):
            values["name_key"] = identity.name_key(values["name"])
        values["updated_at"] = now
        _update(conn, "organisations", "organisation_id", org_id, values)
        ctx.stats["organisations_updated"] += 1
    else:
        name = values.get("name")
        match = identity.find_organisation(
            conn, name=name, abn=values.get("abn"), website=values.get("website"),
            email=values.get("general_email"), external=external, free_mail=ctx.free_mail)
        if match:
            org_id = match[0]
            values["updated_at"] = now
            improved = _improvements(conn, org_id, values)
            _update(conn, "organisations", "organisation_id", org_id, values, fill_only=True)
            if improved:
                _update(conn, "organisations", "organisation_id", org_id, improved)
            ctx.stats["organisations_matched"] += 1
        else:
            if not name:
                raise IngestError("organisation needs a name (or an organisation_id)")
            if values.get("website") and "domain" not in values:
                values["domain"] = identity.domain_from_url(values["website"])
            values.setdefault("first_seen_at", now)
            values.setdefault("first_seen_source", ctx.system)
            values.update(name_key=identity.name_key(name), created_by=ctx.agent_code or ctx.system,
                          created_at=now, updated_at=now)
            org_id = _insert(conn, "organisations", values)
            ctx.stats["organisations_created"] += 1
    row = conn.execute("SELECT name, abn, website, general_email FROM organisations WHERE organisation_id = ?",
                       (org_id,)).fetchone()
    identity.register_identifiers(conn, org_id, name=row[0], abn=row[1], website=row[2], email=row[3],
                                  external=external, source=ctx.system, free_mail=ctx.free_mail)
    if values.get("website") and not conn.execute(
            "SELECT domain FROM organisations WHERE organisation_id = ?", (org_id,)).fetchone()[0]:
        conn.execute("UPDATE organisations SET domain = ? WHERE organisation_id = ?",
                     (identity.domain_from_url(values["website"]), org_id))
    return org_id


_EVIDENCE_RANK = {"UNVERIFIED": 1, "NAME_INFERRED": 2, "GOVT_SOURCE": 3, "WEBSITE_VERIFIED": 4, "DIRECT_CONTACT": 5}


def _improvements(conn: sqlite3.Connection, org_id: int, values: dict[str, Any]) -> dict[str, Any]:
    """Columns with a default ('UNKNOWN', 'UNVERIFIED') are upgraded, never downgraded, by a re-discovery."""
    cur = conn.execute("SELECT name, region_code, org_category, evidence_level FROM organisations "
                       "WHERE organisation_id = ?", (org_id,)).fetchone()
    out = {}
    # An organisation first seen only as a domain/email (e.g. from the mailbox) takes its real name later.
    if values.get("name") and re.fullmatch(r"[^\s]+\.[a-z]{2,}", cur["name"] or "") and " " in values["name"]:
        out["name"] = values["name"]
        out["name_key"] = identity.name_key(values["name"])
    if values.get("region_code") not in (None, "UNKNOWN") and cur["region_code"] == "UNKNOWN":
        out["region_code"] = values["region_code"]
    if values.get("org_category") not in (None, "UNKNOWN") and cur["org_category"] == "UNKNOWN":
        out["org_category"] = values["org_category"]
    new_ev = values.get("evidence_level")
    if new_ev and _EVIDENCE_RANK.get(new_ev, 0) > _EVIDENCE_RANK.get(cur["evidence_level"], 0):
        out["evidence_level"] = new_ev
    return out


def org_ref(ctx: IngestContext, rec: dict[str, Any], *, required: bool = True,
            id_key: str = "organisation_id", obj_key: str = "organisation") -> int | None:
    """Resolve an organisation reference inside a record."""
    org_id = clean(rec.get(id_key))
    if org_id is not None:
        org_id = int(org_id)
        if not _exists(ctx.conn, "organisations", "organisation_id", org_id):
            raise IngestError(f"{id_key} {org_id} does not exist")
        merged = ctx.conn.execute("SELECT merged_into_id FROM organisations WHERE organisation_id = ?",
                                  (org_id,)).fetchone()[0]
        return merged or org_id
    obj = rec.get(obj_key)
    if isinstance(obj, dict):
        return upsert_organisation(ctx, obj)
    if isinstance(obj, str) and clean(obj):
        return upsert_organisation(ctx, {"name": obj})
    # Fall back to identifying the organisation from an email on the record.
    for key in ("to_email", "from_email", "email", "contact_email"):
        email = identity.normalise_email(rec.get(key))
        if email:
            row = ctx.conn.execute("SELECT organisation_id FROM contacts WHERE email = ?", (email,)).fetchone()
            if row:
                return row[0]
            found = identity.find_organisation(ctx.conn, email=email, free_mail=ctx.free_mail)
            if found:
                return found[0]
    if required:
        raise IngestError(f"record needs {id_key} or an '{obj_key}' object")
    return None


def upsert_contact(ctx: IngestContext, rec: dict[str, Any], organisation_id: int | None = None) -> int:
    conn = ctx.conn
    rec = dict(rec)
    org_id = organisation_id or org_ref(ctx, rec)
    values = prepare(ctx, "contacts", rec, skip={"contact_id", "organisation_id", "organisation"})
    values["organisation_id"] = org_id
    if values.get("email") and "is_role_address" not in values:
        values["is_role_address"] = int(bool(identity.is_role_address(values["email"])))
    if values.get("full_name") and "first_name" not in values:
        values["first_name"] = values["full_name"].split()[0]
    now = now_iso()
    contact_id = clean(rec.get("contact_id"))
    if contact_id is None and values.get("email"):
        contact_id = _find(conn, "contacts", "contact_id", "email = ?", (values["email"],))
    if contact_id is None and values.get("full_name"):
        contact_id = _find(conn, "contacts", "contact_id", "organisation_id = ? AND full_name = ? COLLATE NOCASE",
                           (org_id, values["full_name"]))
    if contact_id is not None:
        values.pop("organisation_id", None)  # a contact never silently moves organisation
        values["updated_at"] = now
        # Verification status is authoritative when supplied; everything else fills gaps.
        status_fields = {k: values.pop(k) for k in ("email_status", "email_verified_at",
                                                     "email_verification_method") if k in values}
        _update(conn, "contacts", "contact_id", int(contact_id), values, fill_only=True)
        if status_fields:
            _update(conn, "contacts", "contact_id", int(contact_id), status_fields)
        ctx.stats["contacts_updated"] += 1
        return int(contact_id)
    if not values.get("email") and not values.get("full_name") and not values.get("phone"):
        raise IngestError("contact needs an email, name or phone")
    values.update(source_system=values.get("source_system") or ctx.system,
                  created_by=ctx.agent_code or ctx.system, created_at=now, updated_at=now)
    ctx.stats["contacts_created"] += 1
    return _insert(conn, "contacts", values)


def contact_ref(ctx: IngestContext, rec: dict[str, Any], org_id: int, *email_keys: str) -> int | None:
    cid = clean(rec.get("contact_id"))
    if cid is not None:
        return int(cid)
    obj = rec.get("contact")
    if isinstance(obj, dict):
        return upsert_contact(ctx, obj, org_id)
    for key in email_keys:
        email = identity.normalise_email(rec.get(key))
        if email:
            return upsert_contact(ctx, {"email": email, "full_name": rec.get("contact_name")}, org_id)
    return None


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------
def upsert_opportunity(ctx: IngestContext, rec: dict[str, Any], organisation_id: int | None = None) -> int:
    conn = ctx.conn
    rec = dict(rec)
    for alias, real in (("type", None), ("opportunity_type", "opportunity_type_code"), ("source", "source_code"),
                        ("stage", "stage_code"), ("region", "region_code"), ("service_type", "service_type_code")):
        if real and alias in rec and real not in rec:
            rec[real] = rec.pop(alias)
    org_id = organisation_id or org_ref(ctx, rec)
    values = prepare(ctx, "opportunities", rec,
                     skip={"opportunity_id", "organisation_id", "organisation", "contact", "priority_mode",
                           "match_any_type"})
    values["organisation_id"] = org_id
    if isinstance(rec.get("contact"), dict):
        values["contact_id"] = upsert_contact(ctx, rec["contact"], org_id)
    now = now_iso()
    opp_id = clean(rec.get("opportunity_id"))
    source_key = values.get("source_key")
    key_system = values.get("source_system") or ctx.system
    if opp_id is None and source_key:
        values.setdefault("source_system", ctx.system)
        opp_id = _find(conn, "opportunity_keys", "opportunity_id", "source_system = ? AND source_key = ?",
                       (key_system, source_key))
    if opp_id is None:
        # Same organisation + same type + still open = the same opportunity.
        opp_type = values.get("opportunity_type_code", "OTHER")
        opp_id = _find(conn, "opportunities", "opportunity_id",
                       "organisation_id = ? AND opportunity_type_code = ? AND status = 'OPEN' "
                       "ORDER BY opportunity_id", (org_id, opp_type))
        if opp_id is None and rec.get("match_any_type"):
            # Generic list membership: join the organisation's existing opportunity rather than add one.
            opp_id = _find(conn, "opportunities", "opportunity_id", "organisation_id = ? ORDER BY "
                           "CASE status WHEN 'OPEN' THEN 0 ELSE 1 END, opportunity_id", (org_id,))
            if opp_id is not None:
                values.pop("opportunity_type_code", None)
                values.pop("source_code", None)
        if opp_id is not None:
            # A second discovery of an open opportunity must not steal its source key.
            values.pop("source_system", None)
            values.pop("source_key", None)
    if opp_id is not None:
        opp_id = int(opp_id)
        if rec.get("priority_mode") == "raise_only" and values.get("priority"):
            current = conn.execute("SELECT priority FROM opportunities WHERE opportunity_id = ?", (opp_id,)).fetchone()[0]
            if current and current <= values["priority"]:
                values.pop("priority")
        new_stage = values.pop("stage_code", None)
        values["updated_at"] = now
        if values.get("is_verified") == 0:
            values.pop("is_verified")  # verification is sticky: a weaker source never un-verifies
        earliest = values.pop("discovered_at", None)
        overwrite = {k: values.pop(k) for k in ("status", "next_action", "next_action_due", "priority",
                                                 "expected_weekly_hours", "expected_hourly_rate", "rate_basis",
                                                 "closed_at", "lost_reason", "requires_approval", "score",
                                                 "is_verified", "verified_at") if k in values}
        _update(conn, "opportunities", "opportunity_id", opp_id, values, fill_only=True)
        if earliest:  # the first time anyone found it is the discovery date
            conn.execute("UPDATE opportunities SET discovered_at = MIN(discovered_at, ?) WHERE opportunity_id = ?",
                         (earliest, opp_id))
        if overwrite:
            overwrite["updated_at"] = now
            _update(conn, "opportunities", "opportunity_id", opp_id, overwrite)
        if new_stage:
            advance_stage(ctx, opp_id, new_stage, values.get("stage_changed_at") or now, allow_backwards=True)
        _register_opp_key(conn, key_system, source_key, opp_id)
        ctx.stats["opportunities_updated"] += 1
        return opp_id
    values.setdefault("title", rec.get("title") or _default_title(conn, org_id, values))
    values.setdefault("discovered_at", now)
    values.setdefault("discovered_by", ctx.agent_code or ctx.system)
    values.setdefault("stage_changed_at", values["discovered_at"])
    if "region_code" not in values:
        values["region_code"] = conn.execute("SELECT region_code FROM organisations WHERE organisation_id = ?",
                                             (org_id,)).fetchone()[0]
    if values.get("is_verified") and "stage_code" not in values:
        values["stage_code"] = "VERIFIED"
    values.update(created_at=now, updated_at=now)
    if source_key:
        values.setdefault("source_system", ctx.system)
    ctx.stats["opportunities_created"] += 1
    opp_id = _insert(conn, "opportunities", values)
    _register_opp_key(conn, key_system, source_key, opp_id)
    return opp_id


def _register_opp_key(conn: sqlite3.Connection, system: str, key: str | None, opp_id: int) -> None:
    if key:
        conn.execute("INSERT OR IGNORE INTO opportunity_keys (source_system, source_key, opportunity_id, created_at) "
                     "VALUES (?, ?, ?, ?)", (system, key, opp_id, now_iso()))


def _default_title(conn: sqlite3.Connection, org_id: int, values: dict[str, Any]) -> str:
    org = conn.execute("SELECT name FROM organisations WHERE organisation_id = ?", (org_id,)).fetchone()[0]
    t = conn.execute("SELECT type_name FROM ref_opportunity_type WHERE type_code = ?",
                     (values.get("opportunity_type_code", "OTHER"),)).fetchone()[0]
    return f"{org} — {t}"


def advance_stage(ctx: IngestContext, opp_id: int, stage_code: str, at: str, *, allow_backwards: bool = False) -> bool:
    """Move an opportunity forward (never backwards unless explicitly asked)."""
    conn = ctx.conn
    row = conn.execute(
        "SELECT cur.stage_order, new.stage_order FROM opportunities o "
        "JOIN ref_pipeline_stage cur ON cur.stage_code = o.stage_code "
        "JOIN ref_pipeline_stage new ON new.stage_code = ? WHERE o.opportunity_id = ?",
        (stage_code, opp_id)).fetchone()
    if row is None:
        raise IngestError(f"unknown stage {stage_code!r} or opportunity {opp_id}")
    if row[1] > row[0] or (allow_backwards and row[1] != row[0]):
        conn.execute("UPDATE opportunities SET stage_code = ?, stage_changed_at = ?, updated_at = ? "
                     "WHERE opportunity_id = ?", (stage_code, at, now_iso(), opp_id))
        return True
    return False


def opportunity_ref(ctx: IngestContext, rec: dict[str, Any], org_id: int, *, create: bool = True) -> int | None:
    """Explicit id > nested object > the organisation's primary opportunity (created if missing)."""
    opp_id = clean(rec.get("opportunity_id"))
    if opp_id is not None:
        opp_id = int(opp_id)
        if not _exists(ctx.conn, "opportunities", "opportunity_id", opp_id):
            raise IngestError(f"opportunity_id {opp_id} does not exist")
        return opp_id
    obj = rec.get("opportunity")
    if isinstance(obj, dict):
        return upsert_opportunity(ctx, obj, org_id)
    row = ctx.conn.execute("SELECT primary_opportunity_id FROM v_org_primary_opportunity WHERE organisation_id = ?",
                           (org_id,)).fetchone()
    if row and row[0]:
        return row[0]
    if not create:
        return None
    event_at = next((rec[k] for k in ("sent_at", "scheduled_for", "received_at", "scheduled_start", "bounced_at")
                     if clean(rec.get(k))), None)
    auto = {"opportunity_type_code": rec.get("opportunity_type_code") or "OTHER",
            "source_code": rec.get("source_code") or "OTHER",
            "source_detail": f"Auto-created from {rec.get('type', 'activity')} record"}
    if event_at:
        auto["discovered_at"] = event_at
    return upsert_opportunity(ctx, auto, org_id)


# ---------------------------------------------------------------------------
# Suppression
# ---------------------------------------------------------------------------
def suppression_hit(conn: sqlite3.Connection, *, email: str | None, organisation_id: int | None,
                    at: str | None = None) -> sqlite3.Row | None:
    at = at or now_iso()
    dom = identity.email_domain(email) if email else None
    return conn.execute(
        "SELECT * FROM suppression WHERE (expires_at IS NULL OR expires_at > ?) AND ("
        " (scope = 'EMAIL' AND email = ?) OR (scope = 'DOMAIN' AND domain = ?) OR "
        " (scope = 'ORGANISATION' AND organisation_id = ?)) LIMIT 1",
        (at, email, dom, organisation_id)).fetchone()


def upsert_suppression(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    scope = to_enum(rec.get("scope") or ("EMAIL" if rec.get("email") else "DOMAIN" if rec.get("domain")
                                         else "ORGANISATION"), ("EMAIL", "DOMAIN", "ORGANISATION"), "scope")
    values = prepare(ctx, "suppression", rec, skip={"suppression_id", "organisation", "domain"})
    values["scope"] = scope
    if scope == "DOMAIN":
        values["domain"] = identity.registrable_domain(str(rec.get("domain")))
    if scope == "ORGANISATION":
        values["organisation_id"] = org_ref(ctx, rec)
    key_col = {"EMAIL": "email", "DOMAIN": "domain", "ORGANISATION": "organisation_id"}[scope]
    if values.get(key_col) is None:
        raise IngestError(f"suppression with scope {scope} needs {key_col}")
    existing = _find(ctx.conn, "suppression", "suppression_id", f"scope = ? AND {key_col} = ?", (scope, values[key_col]))
    if existing:
        _update(ctx.conn, "suppression", "suppression_id", existing, values, fill_only=True)
        return existing
    values.setdefault("reason", "OTHER")
    values.update(source=values.get("source") or ctx.system, created_by=ctx.agent_code or ctx.system,
                  created_at=now_iso())
    if scope == "ORGANISATION":
        ctx.conn.execute("UPDATE organisations SET relationship_status = 'DO_NOT_CONTACT', updated_at = ? "
                         "WHERE organisation_id = ?", (now_iso(), values["organisation_id"]))
    ctx.stats["suppressions_created"] += 1
    return _insert(ctx.conn, "suppression", values)


# ---------------------------------------------------------------------------
# Generic event upsert (outreach, replies, meetings, ...)
# ---------------------------------------------------------------------------
def _event_upsert(ctx: IngestContext, table: str, pk: str, values: dict[str, Any], rec: dict[str, Any],
                  natural_keys: list[str], *, overwrite: bool = True) -> tuple[int, bool]:
    conn = ctx.conn
    now = now_iso()
    row_id = clean(rec.get(pk))
    if row_id is None:
        for key in natural_keys:
            if key == "source_key" and values.get("source_key"):
                values.setdefault("source_system", ctx.system)
                row_id = _find(conn, table, pk, "source_system = ? AND source_key = ?",
                               (values["source_system"], values["source_key"]))
            elif values.get(key) is not None:
                row_id = _find(conn, table, pk, f"{key} = ?", (values[key],))
            if row_id is not None:
                break
    if row_id is not None:
        row_id = int(row_id)
        if not _exists(conn, table, pk, row_id):
            raise IngestError(f"{pk} {row_id} does not exist")
        values["updated_at"] = now
        _update(conn, table, pk, row_id, values, fill_only=not overwrite)
        ctx.stats[f"{table}_updated"] += 1
        return row_id, False
    if values.get("source_key"):
        values.setdefault("source_system", ctx.system)
    values.setdefault("created_by", ctx.agent_code or ctx.system)
    values.update(created_at=now, updated_at=now)
    ctx.stats[f"{table}_created"] += 1
    return _insert(conn, table, {k: v for k, v in values.items() if k in table_spec(conn, table)}), True


_REF_SKIP = {"organisation", "contact", "opportunity", "contact_name"}


def upsert_outreach(ctx: IngestContext, rec: dict[str, Any]) -> int:
    conn = ctx.conn
    rec = dict(rec)
    existing = None
    if clean(rec.get("outreach_id")) is not None:
        existing = conn.execute("SELECT * FROM outreach WHERE outreach_id = ?", (int(rec["outreach_id"]),)).fetchone()
    elif clean(rec.get("external_message_id")):
        existing = conn.execute("SELECT * FROM outreach WHERE external_message_id = ?",
                                (rec["external_message_id"].strip(),)).fetchone()
    org_id = existing["organisation_id"] if existing else org_ref(ctx, rec)
    values = prepare(ctx, "outreach", rec, skip={"outreach_id", "organisation_id"} | _REF_SKIP)
    if not existing:
        values["organisation_id"] = org_id
        values["contact_id"] = contact_ref(ctx, rec, org_id, "to_email")
        values["opportunity_id"] = opportunity_ref(ctx, rec, org_id)
        values.setdefault("agent_run_id", ctx.run_id)
    status = values.get("status") or (existing["status"] if existing else "DRAFT")
    if status == "PENDING_APPROVAL":
        values["requires_approval"] = 1
    email = values.get("to_email") or (existing["to_email"] if existing else None)
    hit = suppression_hit(conn, email=email, organisation_id=org_id)
    if hit and status in ("DRAFT", "PENDING_APPROVAL", "APPROVED", "SCHEDULED") and not ctx.allow_suppressed:
        raise SuppressedError(
            f"outreach to {email or 'organisation ' + str(org_id)} refused: suppressed ({hit['reason']})")
    if status == "SENT" and not values.get("sent_at") and not (existing and existing["sent_at"]):
        values["sent_at"] = values.get("scheduled_for") or now_iso()
    row_id, _created = _event_upsert(ctx, "outreach", "outreach_id", values, rec,
                                     ["external_message_id", "source_key"])
    if hit and status == "SENT":
        raise_alert(conn, key=f"suppressed-send-{row_id}", source_type="BUSINESS_RULE", severity="ERROR",
                    category="SUPPRESSION", message=f"Outreach {row_id} was sent to a suppressed address ({email})",
                    entity_type="outreach", entity_id=row_id, agent_code=ctx.agent_code, run_id=ctx.run_id)
    if status == "SENT":
        opp = conn.execute("SELECT opportunity_id, sent_at FROM outreach WHERE outreach_id = ?", (row_id,)).fetchone()
        if opp["opportunity_id"]:
            advance_stage(ctx, opp["opportunity_id"], "CONTACTED", opp["sent_at"])
    return row_id


def record_bounce(ctx: IngestContext, rec: dict[str, Any]) -> int | None:
    """Mark the bounced outreach, flag the contact and suppress hard bounces."""
    conn = ctx.conn
    email = identity.normalise_email(rec.get("to_email") or rec.get("email"))
    bounced_at = parse_datetime_utc(rec.get("bounced_at") or now_iso(), offset_hours=ctx.offset)
    bounce_type = to_enum(rec.get("bounce_type") or "HARD", ("HARD", "SOFT"), "bounce_type")
    row = None
    ref = clean(rec.get("original_message_id")) or clean(rec.get("external_message_id"))
    if ref:
        row = conn.execute("SELECT outreach_id FROM outreach WHERE external_message_id = ?", (ref,)).fetchone()
    if row is None and email:
        row = conn.execute(
            "SELECT outreach_id FROM outreach WHERE to_email = ? AND status IN ('SENT','SCHEDULED','BOUNCED') "
            "AND COALESCE(sent_at, scheduled_for, created_at) <= ? ORDER BY COALESCE(sent_at, scheduled_for) DESC LIMIT 1",
            (email, bounced_at)).fetchone()
    outreach_id = None
    if row:
        outreach_id = row[0]
        conn.execute("UPDATE outreach SET status = 'BOUNCED', bounced_at = ?, bounce_type = ?, bounce_reason = ?, "
                     "updated_at = ? WHERE outreach_id = ?",
                     (bounced_at, bounce_type, clean(rec.get("bounce_reason")), now_iso(), outreach_id))
        ctx.stats["bounces_recorded"] += 1
    elif email:
        ctx.warnings.append(f"bounce for {email} did not match any recorded outreach")
    if email:
        conn.execute("UPDATE contacts SET email_status = 'BOUNCED', updated_at = ? WHERE email = ?", (now_iso(), email))
        if bounce_type == "HARD":
            upsert_suppression(ctx, {"scope": "EMAIL", "email": email, "reason": "HARD_BOUNCE",
                                     "notes": clean(rec.get("bounce_reason"))})
    return outreach_id


def upsert_follow_up(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    fid = clean(rec.get("follow_up_id"))
    values = prepare(ctx, "follow_ups", rec, skip={"follow_up_id", "organisation_id"} | _REF_SKIP)
    if fid is None:
        org_id = org_ref(ctx, rec)
        values["organisation_id"] = org_id
        values.setdefault("contact_id", contact_ref(ctx, rec, org_id, "email", "to_email"))
        values.setdefault("opportunity_id", opportunity_ref(ctx, rec, org_id))
        if "due_date" not in values:
            raise IngestError("follow_up needs a due_date")
    if values.get("status") == "DONE" and "completed_at" not in values:
        values["completed_at"] = now_iso()
    return _event_upsert(ctx, "follow_ups", "follow_up_id", values, rec, ["source_key"])[0]


def upsert_reply(ctx: IngestContext, rec: dict[str, Any]) -> int:
    conn = ctx.conn
    rec = dict(rec)
    existing = None
    if clean(rec.get("reply_id")) is not None:
        existing = conn.execute("SELECT * FROM replies WHERE reply_id = ?", (int(rec["reply_id"]),)).fetchone()
    elif clean(rec.get("external_message_id")):
        existing = conn.execute("SELECT * FROM replies WHERE external_message_id = ?",
                                (rec["external_message_id"].strip(),)).fetchone()
    values = prepare(ctx, "replies", rec, skip={"reply_id", "organisation_id"} | _REF_SKIP)
    if not existing:
        # Link to the outreach it answers: explicit id > thread > latest touch to that sender.
        if "outreach_id" not in values and values.get("thread_id"):
            row = conn.execute("SELECT outreach_id, organisation_id FROM outreach WHERE thread_id = ? "
                               "ORDER BY sent_at DESC LIMIT 1", (values["thread_id"],)).fetchone()
            if row:
                values["outreach_id"] = row[0]
        if "outreach_id" not in values and values.get("from_email"):
            row = conn.execute("SELECT outreach_id FROM outreach WHERE to_email = ? AND status IN ('SENT','BOUNCED') "
                               "ORDER BY sent_at DESC LIMIT 1", (values["from_email"],)).fetchone()
            if row:
                values["outreach_id"] = row[0]
        linked = conn.execute("SELECT organisation_id, opportunity_id FROM outreach WHERE outreach_id = ?",
                              (values.get("outreach_id"),)).fetchone()
        org_id = org_ref(ctx, rec, required=linked is None) or linked["organisation_id"]
        values["organisation_id"] = org_id
        values.setdefault("contact_id", contact_ref(ctx, rec, org_id, "from_email"))
        if "opportunity_id" not in values:
            values["opportunity_id"] = (linked["opportunity_id"] if linked and linked["opportunity_id"]
                                        else opportunity_ref(ctx, rec, org_id))
        if "received_at" not in values:
            raise IngestError("reply needs received_at")
    cls = values.get("classification") or (existing["classification"] if existing else "UNCLASSIFIED")
    if cls in ("OUT_OF_OFFICE", "AUTO_REPLY", "UNSUBSCRIBE", "NOT_INTERESTED", "WRONG_CONTACT"):
        values.setdefault("requires_response", 0)
        values.setdefault("response_status", "NO_RESPONSE_NEEDED")
    if values.get("responded_at") and "response_status" not in values:
        values["response_status"] = "RESPONDED"
    row_id, _ = _event_upsert(ctx, "replies", "reply_id", values, rec, ["external_message_id", "source_key"])
    r = conn.execute("SELECT * FROM replies WHERE reply_id = ?", (row_id,)).fetchone()
    if r["classification"] == "UNSUBSCRIBE" and r["from_email"]:
        upsert_suppression(ctx, {"scope": "EMAIL", "email": r["from_email"], "reason": "UNSUBSCRIBE"})
    if r["opportunity_id"]:
        if r["classification"] in ("POSITIVE", "REFERRAL"):
            advance_stage(ctx, r["opportunity_id"], "POSITIVE", r["received_at"])
        elif r["classification"] not in ("OUT_OF_OFFICE", "AUTO_REPLY"):
            advance_stage(ctx, r["opportunity_id"], "REPLIED", r["received_at"])
    if r["classification"] in ("POSITIVE", "REFERRAL", "QUESTION", "NEUTRAL"):
        conn.execute("UPDATE organisations SET relationship_status = 'IN_CONVERSATION', updated_at = ? "
                     "WHERE organisation_id = ? AND relationship_status = 'PROSPECT'", (now_iso(), r["organisation_id"]))
    return row_id


def upsert_meeting(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    values = prepare(ctx, "meetings", rec, skip={"meeting_id", "organisation_id"} | _REF_SKIP)
    mid = clean(rec.get("meeting_id"))
    if mid is None and not (values.get("calendar_event_id") and _find(
            ctx.conn, "meetings", "meeting_id", "calendar_event_id = ?", (values["calendar_event_id"],))):
        org_id = org_ref(ctx, rec)
        values["organisation_id"] = org_id
        values.setdefault("contact_id", contact_ref(ctx, rec, org_id, "email"))
        values.setdefault("opportunity_id", opportunity_ref(ctx, rec, org_id))
        values.setdefault("booked_at", now_iso())
        if "scheduled_start" not in values:
            raise IngestError("meeting needs scheduled_start")
    row_id, _ = _event_upsert(ctx, "meetings", "meeting_id", values, rec, ["calendar_event_id", "source_key"])
    m = ctx.conn.execute("SELECT opportunity_id, status, COALESCE(booked_at, created_at) AS at FROM meetings "
                         "WHERE meeting_id = ?", (row_id,)).fetchone()
    if m["opportunity_id"] and m["status"] in ("BOOKED", "COMPLETED", "RESCHEDULED"):
        advance_stage(ctx, m["opportunity_id"], "MEETING", m["at"])
    return row_id


def upsert_referral(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    if "referring_organisation" in rec and "organisation" not in rec:
        rec["organisation"] = rec.pop("referring_organisation")
    values = prepare(ctx, "participant_referrals", rec,
                     skip={"referral_id", "referring_organisation_id", "referring_organisation"} | _REF_SKIP)
    rid = clean(rec.get("referral_id"))
    if rid is None:
        org_id = org_ref(ctx, rec, required=False, id_key="referring_organisation_id")
        values["referring_organisation_id"] = org_id
        if org_id:
            values.setdefault("referring_contact_id", contact_ref(ctx, rec, org_id, "email"))
            values.setdefault("opportunity_id", opportunity_ref(ctx, rec, org_id))
        if "participant_ref" not in values or "received_at" not in values:
            raise IngestError("referral needs participant_ref (a code, never a name) and received_at")
        hours = ctx.cfg.get("action_centre.referral_response_hours", 24)
        values.setdefault("response_due_at", _plus_hours(values["received_at"], hours))
        values.setdefault("status_changed_at", values["received_at"])
    elif "status" in values:
        values.setdefault("status_changed_at", now_iso())
    row_id, _ = _event_upsert(ctx, "participant_referrals", "referral_id", values, rec, ["source_key"])
    r = ctx.conn.execute("SELECT opportunity_id, received_at, funding_type FROM participant_referrals "
                         "WHERE referral_id = ?", (row_id,)).fetchone()
    if r["opportunity_id"]:
        advance_stage(ctx, r["opportunity_id"], "REFERRAL", r["received_at"])
    if r["funding_type"] == "NDIS_AGENCY_MANAGED" and not ctx.cfg.get("business.registered_ndis_provider", False):
        raise_alert(ctx.conn, key=f"agency-managed-referral-{row_id}", source_type="BUSINESS_RULE",
                    severity="WARNING", category="ELIGIBILITY", entity_type="participant_referrals", entity_id=row_id,
                    message="Referral is NDIA-managed: GCDC is not a registered NDIS provider and cannot bill it "
                            "directly — decline or route via a registered partner")
    return row_id


def _plus_hours(ts_utc: str, hours: float) -> str:
    from datetime import datetime, timedelta, timezone
    dt = datetime.fromisoformat(ts_utc.replace("Z", "+00:00")) + timedelta(hours=float(hours))
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def upsert_trial_shift(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    conn = ctx.conn
    values = prepare(ctx, "trial_shifts", rec, skip={"trial_shift_id", "worker_ref"} | _REF_SKIP)
    if rec.get("worker_ref"):
        values["worker_id"] = _worker_id(conn, rec["worker_ref"])
    if clean(rec.get("trial_shift_id")) is None:
        if "referral_id" in values and "opportunity_id" not in values:
            row = conn.execute("SELECT opportunity_id, referring_organisation_id, participant_ref "
                               "FROM participant_referrals WHERE referral_id = ?", (values["referral_id"],)).fetchone()
            if row is None:
                raise IngestError(f"referral_id {values['referral_id']} does not exist")
            values["opportunity_id"] = row[0]
            values.setdefault("organisation_id", row[1])
            values.setdefault("participant_ref", row[2])
        if "organisation_id" not in values and (rec.get("organisation") or rec.get("organisation_id")):
            values["organisation_id"] = org_ref(ctx, rec)
        if "opportunity_id" not in values and values.get("organisation_id"):
            values["opportunity_id"] = opportunity_ref(ctx, rec, values["organisation_id"])
        if "scheduled_start" not in values:
            raise IngestError("trial_shift needs scheduled_start")
    row_id, _ = _event_upsert(ctx, "trial_shifts", "trial_shift_id", values, rec, ["source_key"])
    t = conn.execute("SELECT opportunity_id, status, scheduled_start FROM trial_shifts WHERE trial_shift_id = ?",
                     (row_id,)).fetchone()
    if t["opportunity_id"] and t["status"] in ("SCHEDULED", "COMPLETED"):
        advance_stage(ctx, t["opportunity_id"], "TRIAL", min(t["scheduled_start"], now_iso()))
    return row_id


def _worker_id(conn: sqlite3.Connection, worker_ref: str) -> int:
    row = conn.execute("SELECT worker_id FROM workers WHERE worker_ref = ?", (str(worker_ref).strip(),)).fetchone()
    if row is None:
        raise IngestError(f"worker_ref {worker_ref!r} does not exist — add the worker first")
    return row[0]


def _participant_id(conn: sqlite3.Connection, ref: str) -> int:
    row = conn.execute("SELECT participant_id FROM participants WHERE participant_ref = ?", (str(ref).strip(),)).fetchone()
    if row is None:
        raise IngestError(f"participant_ref {ref!r} does not exist — add the participant first")
    return row[0]


def upsert_participant(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    values = prepare(ctx, "participants", rec, skip={"participant_id", "referring_organisation"} | _REF_SKIP)
    if "participant_ref" not in values:
        raise IngestError("participant needs participant_ref (a code such as P-0007, never a name)")
    if rec.get("organisation") or rec.get("referring_organisation"):
        values["referring_organisation_id"] = org_ref(ctx, {"organisation": rec.get("organisation")
                                                            or rec.get("referring_organisation")})
    now = now_iso()
    pid = _find(ctx.conn, "participants", "participant_id", "participant_ref = ?", (values["participant_ref"],))
    if pid:
        values["updated_at"] = now
        _update(ctx.conn, "participants", "participant_id", pid, values)
        return pid
    values.update(source_system=ctx.system, created_by=ctx.agent_code or ctx.system, created_at=now, updated_at=now)
    ctx.stats["participants_created"] += 1
    return _insert(ctx.conn, "participants", values)


def upsert_roster(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    conn = ctx.conn
    values = prepare(ctx, "rosters", rec, skip={"roster_id", "participant_ref", "worker_ref"} | _REF_SKIP)
    if rec.get("participant_ref"):
        values["participant_id"] = _participant_id(conn, rec["participant_ref"])
    if rec.get("worker_ref"):
        values["primary_worker_id"] = _worker_id(conn, rec["worker_ref"])
    if rec.get("organisation") or rec.get("organisation_id"):
        values["organisation_id"] = org_ref(ctx, rec)
    now = now_iso()
    rid = clean(rec.get("roster_id")) or (values.get("roster_ref") and _find(
        conn, "rosters", "roster_id", "roster_ref = ?", (values["roster_ref"],)))
    if rid:
        values["updated_at"] = now
        _update(conn, "rosters", "roster_id", int(rid), values)
        rid = int(rid)
    else:
        if "opportunity_id" not in values and values.get("organisation_id"):
            values["opportunity_id"] = opportunity_ref(ctx, rec, values["organisation_id"], create=False)
        if "region_code" not in values and values.get("participant_id"):
            values["region_code"] = conn.execute("SELECT region_code FROM participants WHERE participant_id = ?",
                                                 (values["participant_id"],)).fetchone()[0]
        values.update(source_system=ctx.system, created_by=ctx.agent_code or ctx.system, created_at=now, updated_at=now)
        rid = _insert(conn, "rosters", values)
        ctx.stats["rosters_created"] += 1
    r = conn.execute("SELECT opportunity_id, status, COALESCE(start_date, date('now')) AS at, organisation_id "
                     "FROM rosters WHERE roster_id = ?", (rid,)).fetchone()
    if r["opportunity_id"] and r["status"] in ("SCHEDULED", "ACTIVE", "PAUSED", "ENDED"):
        advance_stage(ctx, r["opportunity_id"], "ROSTER", parse_datetime_utc(r["at"], offset_hours=ctx.offset))
    if r["organisation_id"] and r["status"] == "ACTIVE":
        conn.execute("UPDATE organisations SET relationship_status = 'CLIENT', updated_at = ? WHERE organisation_id = ? "
                     "AND relationship_status NOT IN ('DO_NOT_CONTACT')", (now, r["organisation_id"]))
    return rid


def upsert_revenue(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    conn = ctx.conn
    values = prepare(ctx, "revenue_transactions", rec,
                     skip={"revenue_id", "participant_ref", "roster_ref"} | _REF_SKIP)
    if rec.get("participant_ref"):
        values["participant_id"] = _participant_id(conn, rec["participant_ref"])
    if rec.get("roster_ref"):
        row = conn.execute("SELECT roster_id FROM rosters WHERE roster_ref = ?", (rec["roster_ref"],)).fetchone()
        if row is None:
            raise IngestError(f"roster_ref {rec['roster_ref']!r} does not exist")
        values["roster_id"] = row[0]
    if rec.get("organisation") or rec.get("organisation_id"):
        values["organisation_id"] = org_ref(ctx, rec)
    if values.get("invoice_number") and "source_key" not in values:
        values["source_key"] = values["invoice_number"]
    if values.get("status") == "PAID" and "amount_paid" not in values and "amount_ex_gst" in values:
        values["amount_paid"] = values["amount_ex_gst"]
    if clean(rec.get("revenue_id")) is None and not values.get("source_key"):
        raise IngestError("revenue needs an invoice_number or source_key so it is never double-counted")
    return _event_upsert(ctx, "revenue_transactions", "revenue_id", values, rec, ["source_key"])[0]


def upsert_worker(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    values = prepare(ctx, "workers", rec, skip={"worker_id"})
    if "worker_ref" not in values:
        raise IngestError("worker needs worker_ref (e.g. W-001)")
    now = now_iso()
    wid = _find(ctx.conn, "workers", "worker_id", "worker_ref = ?", (values["worker_ref"],))
    if wid:
        values["updated_at"] = now
        _update(ctx.conn, "workers", "worker_id", wid, values)
        return wid
    if "display_name" not in values:
        raise IngestError("new worker needs display_name")
    values.update(source_system=ctx.system, created_by=ctx.agent_code or ctx.system, created_at=now, updated_at=now)
    return _insert(ctx.conn, "workers", values)


def upsert_availability(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    values = prepare(ctx, "worker_availability", rec, skip={"availability_id", "worker_ref"})
    if rec.get("worker_ref"):
        values["worker_id"] = _worker_id(ctx.conn, rec["worker_ref"])
    for k in ("start_time", "end_time"):
        if k in values and re.fullmatch(r"\d:\d\d", values[k]):
            values[k] = "0" + values[k]
    if not {"worker_id", "day_of_week", "start_time", "end_time"} <= values.keys():
        raise IngestError("availability needs worker_ref, day_of_week (1=Mon), start_time and end_time (HH:MM)")
    now = now_iso()
    aid = _find(ctx.conn, "worker_availability", "availability_id",
                "worker_id = ? AND day_of_week = ? AND start_time = ? AND end_time = ? AND "
                "COALESCE(effective_from, '') = COALESCE(?, '')",
                (values["worker_id"], values["day_of_week"], values["start_time"], values["end_time"],
                 values.get("effective_from")))
    if aid:
        values["updated_at"] = now
        _update(ctx.conn, "worker_availability", "availability_id", aid, values)
        return aid
    values.update(source_system=ctx.system, created_at=now, updated_at=now)
    return _insert(ctx.conn, "worker_availability", values)


def upsert_worker_requirement(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    values = prepare(ctx, "worker_requirements", rec, skip={"requirement_id", "filled_worker_ref"} | _REF_SKIP)
    if rec.get("filled_worker_ref"):
        values["filled_worker_id"] = _worker_id(ctx.conn, rec["filled_worker_ref"])
    if rec.get("organisation") or rec.get("organisation_id"):
        values["organisation_id"] = org_ref(ctx, rec)
    return _event_upsert(ctx, "worker_requirements", "requirement_id", values, rec, ["source_key"])[0]


def upsert_partnership(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    if "stage" in rec and "stage_code" not in rec:
        rec["stage_code"] = rec.pop("stage")
    org_id = org_ref(ctx, rec)
    values = prepare(ctx, "partnerships", rec, skip={"partnership_id", "organisation_id", "documents"} | _REF_SKIP)
    values["organisation_id"] = org_id
    ptype = values.get("partnership_type")
    if not ptype:
        raise IngestError("partnership needs partnership_type")
    now = now_iso()
    pid = clean(rec.get("partnership_id")) or _find(
        ctx.conn, "partnerships", "partnership_id", "organisation_id = ? AND partnership_type = ?", (org_id, ptype))
    if "stage_code" in values:
        values.setdefault("stage_changed_at", now)
    if pid:
        values["updated_at"] = now
        _update(ctx.conn, "partnerships", "partnership_id", int(pid), values)
        pid = int(pid)
    else:
        values.setdefault("opportunity_id", opportunity_ref(ctx, rec, org_id, create=False))
        values.update(source_system=ctx.system, created_by=ctx.agent_code or ctx.system, created_at=now, updated_at=now)
        pid = _insert(ctx.conn, "partnerships", values)
        ctx.stats["partnerships_created"] += 1
    for doc in rec.get("documents") or []:
        doc = doc if isinstance(doc, dict) else {"doc_code": doc}
        code = str(doc["doc_code"]).upper()
        status = to_enum(doc.get("status") or "OUTSTANDING", ("OUTSTANDING", "SUBMITTED", "ACCEPTED", "NOT_REQUIRED"),
                         "document status")
        ctx.conn.execute(
            "INSERT INTO partnership_documents (partnership_id, doc_code, status, submitted_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(partnership_id, doc_code) DO UPDATE SET status = excluded.status, "
            "submitted_at = COALESCE(excluded.submitted_at, submitted_at), updated_at = excluded.updated_at",
            (pid, code, status, parse_datetime_utc(doc.get("submitted_at"), offset_hours=ctx.offset), now))
    stage = ctx.conn.execute("SELECT stage_code, organisation_id FROM partnerships WHERE partnership_id = ?",
                             (pid,)).fetchone()
    if stage["stage_code"] in ("APPROVED", "AWAITING_WORK", "WORK_RECEIVED", "RECURRING_REVENUE"):
        ctx.conn.execute("UPDATE organisations SET relationship_status = 'PARTNER', updated_at = ? "
                         "WHERE organisation_id = ? AND relationship_status IN ('PROSPECT','IN_CONVERSATION')",
                         (now, stage["organisation_id"]))
    return pid


def upsert_application(ctx: IngestContext, rec: dict[str, Any]) -> int:
    rec = dict(rec)
    org_id = org_ref(ctx, rec)
    values = prepare(ctx, "associate_provider_applications", rec,
                     skip={"application_id", "organisation_id", "partnership_type"} | _REF_SKIP)
    values["organisation_id"] = org_id
    if "partnership_id" not in values:
        ptype = rec.get("partnership_type") or "ASSOCIATE_PROVIDER"
        values["partnership_id"] = upsert_partnership(ctx, {"organisation_id": org_id, "partnership_type": ptype})
    if values.get("status") == "SUBMITTED" and "submitted_at" not in values:
        values["submitted_at"] = now_iso()
    aid = clean(rec.get("application_id"))
    if aid is None and not values.get("source_key"):
        aid = _find(ctx.conn, "associate_provider_applications", "application_id",
                    "organisation_id = ? AND partnership_id = ? ORDER BY application_id DESC",
                    (org_id, values["partnership_id"]))
        if aid is not None:
            rec = {**rec, "application_id": aid}
    row_id, _ = _event_upsert(ctx, "associate_provider_applications", "application_id", values, rec, ["source_key"])
    app = ctx.conn.execute("SELECT status, partnership_id FROM associate_provider_applications WHERE application_id = ?",
                           (row_id,)).fetchone()
    stage = {"IN_PROGRESS": "APPLICATION", "SUBMITTED": "APPLICATION", "UNDER_REVIEW": "DUE_DILIGENCE",
             "MORE_INFO_REQUESTED": "DUE_DILIGENCE", "APPROVED": "APPROVED"}.get(app["status"])
    if stage:
        cur = ctx.conn.execute(
            "SELECT s.stage_order, n.stage_order FROM partnerships p JOIN ref_partnership_stage s "
            "ON s.stage_code = p.stage_code JOIN ref_partnership_stage n ON n.stage_code = ? WHERE p.partnership_id = ?",
            (stage, app["partnership_id"])).fetchone()
        if cur and cur[1] > cur[0]:
            ctx.conn.execute("UPDATE partnerships SET stage_code = ?, stage_changed_at = ?, updated_at = ? "
                             "WHERE partnership_id = ?", (stage, now_iso(), now_iso(), app["partnership_id"]))
    if app["status"] == "APPROVED":
        ctx.conn.execute("UPDATE partnerships SET approval_date = COALESCE(approval_date, date('now')) "
                         "WHERE partnership_id = ?", (app["partnership_id"],))
    return row_id


def upsert_compliance_document(ctx: IngestContext, rec: dict[str, Any]) -> str:
    values = prepare(ctx, "compliance_documents", dict(rec))
    code = values.get("doc_code")
    if not code:
        raise IngestError("compliance_document needs doc_code")
    code = code.upper()
    values["doc_code"] = code
    values["updated_at"] = now_iso()
    if ctx.conn.execute("SELECT 1 FROM compliance_documents WHERE doc_code = ?", (code,)).fetchone():
        _update(ctx.conn, "compliance_documents", "doc_code", code, values)
    else:
        values.setdefault("doc_name", code.replace("_", " ").title())
        _insert(ctx.conn, "compliance_documents", values)
    return code


def raise_alert(conn: sqlite3.Connection, *, key: str, source_type: str, severity: str, category: str, message: str,
                entity_type: str | None = None, entity_id: int | None = None, agent_code: str | None = None,
                run_id: int | None = None) -> int:
    """Open (or bump) an alert. Open alerts are unique by key."""
    now = now_iso()
    row = conn.execute("SELECT alert_id FROM alerts WHERE alert_key = ? AND status <> 'RESOLVED'", (key,)).fetchone()
    if row:
        conn.execute("UPDATE alerts SET last_seen_at = ?, occurrences = occurrences + 1, message = ?, severity = ? "
                     "WHERE alert_id = ?", (now, message, severity, row[0]))
        return row[0]
    return conn.execute(
        "INSERT INTO alerts (alert_key, source_type, agent_code, run_id, severity, category, entity_type, entity_id, "
        "message, first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (key, source_type, agent_code, run_id, severity, category, entity_type, entity_id, message, now, now)).lastrowid


def upsert_alert(ctx: IngestContext, rec: dict[str, Any]) -> int:
    values = prepare(ctx, "alerts", dict(rec), skip={"alert_id"})
    if values.get("status") == "RESOLVED" and values.get("alert_key"):
        ctx.conn.execute("UPDATE alerts SET status = 'RESOLVED', resolved_at = ?, resolved_by = ? "
                         "WHERE alert_key = ? AND status <> 'RESOLVED'",
                         (now_iso(), ctx.agent_code or ctx.system, values["alert_key"]))
        return 0
    if not values.get("message"):
        raise IngestError("alert needs a message")
    return raise_alert(ctx.conn, key=values.get("alert_key") or f"{ctx.system}-{values['message'][:60]}",
                       source_type=values.get("source_type", "AGENT"), severity=values.get("severity", "WARNING"),
                       category=values.get("category", "AGENT"), message=values["message"],
                       entity_type=values.get("entity_type"), entity_id=values.get("entity_id"),
                       agent_code=ctx.agent_code, run_id=ctx.run_id)


RECORD_TYPES: dict[str, Callable[[IngestContext, dict[str, Any]], Any]] = {
    "organisation": upsert_organisation,
    "contact": upsert_contact,
    "opportunity": upsert_opportunity,
    "outreach": upsert_outreach,
    "bounce": record_bounce,
    "follow_up": upsert_follow_up,
    "reply": upsert_reply,
    "meeting": upsert_meeting,
    "referral": upsert_referral,
    "trial_shift": upsert_trial_shift,
    "participant": upsert_participant,
    "roster": upsert_roster,
    "revenue": upsert_revenue,
    "worker": upsert_worker,
    "worker_availability": upsert_availability,
    "worker_requirement": upsert_worker_requirement,
    "partnership": upsert_partnership,
    "application": upsert_application,
    "suppression": upsert_suppression,
    "compliance_document": upsert_compliance_document,
    "alert": upsert_alert,
}


@dataclass
class BatchResult:
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, int] = field(default_factory=dict)
    ids: list[Any] = field(default_factory=list)

    @property
    def created(self) -> int:
        return sum(v for k, v in self.stats.items() if k.endswith("_created"))

    @property
    def updated(self) -> int:
        return sum(v for k, v in self.stats.items() if k.endswith(("_updated", "_matched")))

    def as_dict(self) -> dict[str, Any]:
        return {"processed": self.processed, "succeeded": self.succeeded, "failed": self.failed,
                "created": self.created, "updated": self.updated, "stats": self.stats,
                "errors": self.errors, "warnings": self.warnings[:50], "ids": self.ids}


def ingest_records(ctx: IngestContext, records: list[dict[str, Any]]) -> BatchResult:
    """Apply records one by one. A bad record is reported and skipped; the rest still land."""
    result = BatchResult()
    for i, rec in enumerate(records):
        result.processed += 1
        rtype = str(rec.get("type", "")).strip().lower()
        handler = RECORD_TYPES.get(rtype)
        stats_before = Counter(ctx.stats)
        ctx.conn.execute("SAVEPOINT rec")
        try:
            if handler is None:
                raise IngestError(f"unknown record type {rec.get('type')!r}; expected one of {', '.join(RECORD_TYPES)}")
            result.ids.append(handler(ctx, rec))
            ctx.conn.execute("RELEASE rec")
            result.succeeded += 1
        except (IngestError, sqlite3.IntegrityError, ValueError, KeyError, TypeError) as exc:
            ctx.conn.execute("ROLLBACK TO rec")
            ctx.conn.execute("RELEASE rec")
            ctx.stats = stats_before
            result.failed += 1
            result.ids.append(None)
            result.errors.append({"index": i, "type": rtype, "error": str(exc),
                                  "record": json.loads(json.dumps(rec, default=str))})
    result.warnings = list(ctx.warnings)
    result.stats = dict(ctx.stats)
    return result
