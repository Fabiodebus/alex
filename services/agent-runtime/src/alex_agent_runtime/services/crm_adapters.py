"""Platform-specific adapters that normalise raw CRM payloads to CRMRecord.

ADR-001 of the CRM Integration blueprint: feature workflows operate
exclusively on the canonical ``CRMRecord`` schema. Adding a new CRM is
a single new ``CRMPlatformAdapter`` here — no feature touches platform
field names directly.

Each adapter exposes ``normalize(raw, kind)`` returning a ``CRMRecord``.
Raw payloads come in two shapes:

* From the inbound ``crm.activity_logged`` event — payload was already
  normalised by the Pipedream side (see services/pipedream/src/lib/
  normalizer.mjs#normalizeHubspotRecordUpdate). That payload carries
  the original record under a known key.
* From an on-demand ``crm_fetch`` response — direct platform API shape.

Adapters tolerate both: the platform's "native" field names are the
primary lookup, with the Pipedream-normalised shape as a fallback.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Protocol, runtime_checkable

import structlog

from ..schemas import CRMPlatform, CRMRecord, CRMRecordKind, CRMStakeholder

log = structlog.get_logger(__name__)


@runtime_checkable
class CRMPlatformAdapter(Protocol):
    platform: CRMPlatform

    def normalize(self, *, raw: dict[str, Any], kind: CRMRecordKind) -> CRMRecord: ...


# ---------------------------------------------------------------------------
# helpers — shared scalar coercion
# ---------------------------------------------------------------------------
def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Heuristic: HubSpot returns ms epoch, others typically seconds.
        if value > 10_000_000_000:
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        if value.isdigit():
            return _parse_dt(int(value))
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return None


def _to_cents(value: Any, *, default_currency: str | None = None) -> tuple[int | None, str | None]:
    """Best-effort coercion. Returns (amount_cents, currency)."""
    if value is None or value == "":
        return None, default_currency
    if isinstance(value, dict):
        amount = value.get("amount") or value.get("value")
        currency = value.get("currency") or default_currency
        cents, _ = _to_cents(amount, default_currency=currency)
        return cents, currency
    if isinstance(value, (int, float)):
        return int(round(float(value) * 100)), default_currency
    if isinstance(value, str):
        try:
            return int(round(float(value) * 100)), default_currency
        except ValueError:
            return None, default_currency
    return None, default_currency


def _first_present(raw: dict[str, Any], *paths: str) -> Any:
    """Return the first non-empty value from a list of dotted-path keys.

    Digit-only segments index into lists (e.g. ``associations.companies.0.id``)."""
    for path in paths:
        cursor: Any = raw
        for key in path.split("."):
            if isinstance(cursor, dict):
                cursor = cursor.get(key)
            elif isinstance(cursor, list) and key.isdigit():
                idx = int(key)
                cursor = cursor[idx] if 0 <= idx < len(cursor) else None
            else:
                cursor = None
            if cursor is None:
                break
        if cursor not in (None, ""):
            return cursor
    return None


# ---------------------------------------------------------------------------
# HubSpot
# ---------------------------------------------------------------------------
class HubspotAdapter:
    platform = CRMPlatform.HUBSPOT

    def normalize(self, *, raw: dict[str, Any], kind: CRMRecordKind) -> CRMRecord:
        # HubSpot returns properties at raw["properties"], either as a
        # dict of strings or as {value: ...} objects depending on the
        # endpoint. Pipedream-side normalisation flattens this; we
        # handle both shapes.
        props = raw.get("properties", {})
        if props and isinstance(next(iter(props.values()), None), dict):
            props = {k: v.get("value") for k, v in props.items()}

        external_id = str(_first_present(raw, "id", "objectId", "vid") or raw.get("external_id") or "")
        updated_at = _parse_dt(_first_present(raw, "updatedAt", "lastmodifieddate", "occurredAt"))

        record_args: dict[str, Any] = {
            "platform": self.platform,
            "kind": kind,
            "external_id": external_id,
            "updated_at": updated_at,
            "raw": raw,
        }

        if kind is CRMRecordKind.OPPORTUNITY:
            amount_cents, currency = _to_cents(
                props.get("amount") or props.get("hs_deal_value"),
                default_currency=props.get("deal_currency_code"),
            )
            record_args.update(
                {
                    "name": props.get("dealname") or props.get("name"),
                    "stage": props.get("dealstage") or props.get("pipeline_stage"),
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "probability": _coerce_probability(props.get("hs_probability") or props.get("probability")),
                    "close_date": _parse_dt(props.get("closedate")),
                    "owner_email": props.get("hubspot_owner_email") or props.get("owner_email"),
                    "account_external_id": _first_present(raw, "associations.companies.0.id", "associatedCompanyId"),
                    "meddic": _collect_meddic(props),
                }
            )
        elif kind is CRMRecordKind.CONTACT:
            first = props.get("firstname") or ""
            last = props.get("lastname") or ""
            record_args.update(
                {
                    "name": f"{first} {last}".strip() or props.get("email"),
                    "email": props.get("email"),
                    "title": props.get("jobtitle"),
                    "phone": props.get("phone"),
                    "account_external_id": _first_present(raw, "associations.companies.0.id", "associatedCompanyId"),
                }
            )
        elif kind is CRMRecordKind.ACCOUNT:
            record_args.update(
                {
                    "name": props.get("name"),
                    "domain": props.get("domain") or props.get("website"),
                    "industry": props.get("industry"),
                    "country": props.get("country"),
                }
            )
        return CRMRecord(**record_args)


# ---------------------------------------------------------------------------
# Salesforce
# ---------------------------------------------------------------------------
class SalesforceAdapter:
    platform = CRMPlatform.SALESFORCE

    def normalize(self, *, raw: dict[str, Any], kind: CRMRecordKind) -> CRMRecord:
        # Salesforce REST returns flat top-level fields; attributes
        # block carries type + URL. Some endpoints wrap under "records".
        external_id = str(_first_present(raw, "Id", "id") or raw.get("external_id") or "")
        updated_at = _parse_dt(_first_present(raw, "LastModifiedDate", "SystemModstamp"))

        record_args: dict[str, Any] = {
            "platform": self.platform,
            "kind": kind,
            "external_id": external_id,
            "updated_at": updated_at,
            "raw": raw,
        }

        if kind is CRMRecordKind.OPPORTUNITY:
            amount_cents, currency = _to_cents(
                raw.get("Amount"), default_currency=raw.get("CurrencyIsoCode")
            )
            record_args.update(
                {
                    "name": raw.get("Name"),
                    "stage": raw.get("StageName"),
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "probability": _coerce_probability(raw.get("Probability"), pct_scale=True),
                    "close_date": _parse_dt(raw.get("CloseDate")),
                    "owner_email": _first_present(raw, "Owner.Email"),
                    "account_external_id": raw.get("AccountId"),
                    "meddic": _collect_meddic(raw),
                }
            )
        elif kind is CRMRecordKind.CONTACT:
            record_args.update(
                {
                    "name": _join_name(raw.get("FirstName"), raw.get("LastName")) or raw.get("Email"),
                    "email": raw.get("Email"),
                    "title": raw.get("Title"),
                    "phone": raw.get("Phone"),
                    "account_external_id": raw.get("AccountId"),
                }
            )
        elif kind is CRMRecordKind.ACCOUNT:
            record_args.update(
                {
                    "name": raw.get("Name"),
                    "domain": raw.get("Website"),
                    "industry": raw.get("Industry"),
                    "country": _first_present(raw, "BillingCountry", "ShippingCountry"),
                }
            )
        return CRMRecord(**record_args)


# ---------------------------------------------------------------------------
# Pipedrive
# ---------------------------------------------------------------------------
class PipedriveAdapter:
    platform = CRMPlatform.PIPEDRIVE

    def normalize(self, *, raw: dict[str, Any], kind: CRMRecordKind) -> CRMRecord:
        external_id = str(_first_present(raw, "id") or raw.get("external_id") or "")
        updated_at = _parse_dt(_first_present(raw, "update_time", "modified_at"))

        record_args: dict[str, Any] = {
            "platform": self.platform,
            "kind": kind,
            "external_id": external_id,
            "updated_at": updated_at,
            "raw": raw,
        }

        if kind is CRMRecordKind.OPPORTUNITY:
            amount_cents, currency = _to_cents(raw.get("value"), default_currency=raw.get("currency"))
            record_args.update(
                {
                    "name": raw.get("title"),
                    "stage": str(raw.get("stage_id")) if raw.get("stage_id") is not None else None,
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "probability": _coerce_probability(raw.get("probability"), pct_scale=True),
                    "close_date": _parse_dt(raw.get("expected_close_date")),
                    "owner_email": _first_present(raw, "user_id.email", "owner.email"),
                    "account_external_id": str(raw.get("org_id")) if raw.get("org_id") else None,
                    "meddic": _collect_meddic(raw),
                }
            )
        elif kind is CRMRecordKind.CONTACT:
            record_args.update(
                {
                    "name": raw.get("name"),
                    "email": _extract_pipedrive_value(raw.get("email")),
                    "title": raw.get("job_title"),
                    "phone": _extract_pipedrive_value(raw.get("phone")),
                    "account_external_id": str(raw.get("org_id")) if raw.get("org_id") else None,
                }
            )
        elif kind is CRMRecordKind.ACCOUNT:
            record_args.update(
                {
                    "name": raw.get("name"),
                    "domain": raw.get("website"),
                    "industry": raw.get("industry"),
                    "country": raw.get("country_code") or raw.get("country"),
                }
            )
        return CRMRecord(**record_args)


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------
class CloseAdapter:
    platform = CRMPlatform.CLOSE

    def normalize(self, *, raw: dict[str, Any], kind: CRMRecordKind) -> CRMRecord:
        external_id = str(_first_present(raw, "id") or raw.get("external_id") or "")
        updated_at = _parse_dt(raw.get("date_updated"))

        record_args: dict[str, Any] = {
            "platform": self.platform,
            "kind": kind,
            "external_id": external_id,
            "updated_at": updated_at,
            "raw": raw,
        }

        if kind is CRMRecordKind.OPPORTUNITY:
            # Close reports values as integers in *cents* in `value` and
            # currency in `value_currency`. Some accounts also expose a
            # decimal `value_formatted`; we trust `value` first.
            currency = raw.get("value_currency")
            amount_cents: int | None = None
            if raw.get("value") is not None:
                amount_cents = int(raw["value"])
            elif raw.get("value_formatted") is not None:
                amount_cents, currency = _to_cents(raw["value_formatted"], default_currency=currency)
            record_args.update(
                {
                    "name": raw.get("note") or raw.get("title"),
                    "stage": raw.get("status_label") or raw.get("status_id"),
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "probability": _coerce_probability(raw.get("confidence"), pct_scale=True),
                    "close_date": _parse_dt(raw.get("expected_close_date")),
                    "owner_email": raw.get("user_email"),
                    "account_external_id": raw.get("lead_id"),
                    "meddic": _collect_meddic(raw),
                }
            )
        elif kind is CRMRecordKind.CONTACT:
            emails = raw.get("emails") or []
            phones = raw.get("phones") or []
            record_args.update(
                {
                    "name": raw.get("name") or raw.get("display_name"),
                    "email": emails[0].get("email") if emails else None,
                    "title": raw.get("title"),
                    "phone": phones[0].get("phone") if phones else None,
                    "account_external_id": raw.get("lead_id"),
                }
            )
        elif kind is CRMRecordKind.ACCOUNT:
            record_args.update(
                {
                    "name": raw.get("display_name") or raw.get("name"),
                    "domain": raw.get("url"),
                    "industry": raw.get("industry"),
                    "country": _first_present(raw, "addresses.0.country", "country"),
                }
            )
        return CRMRecord(**record_args)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
ADAPTERS: dict[CRMPlatform, CRMPlatformAdapter] = {
    CRMPlatform.HUBSPOT: HubspotAdapter(),
    CRMPlatform.SALESFORCE: SalesforceAdapter(),
    CRMPlatform.PIPEDRIVE: PipedriveAdapter(),
    CRMPlatform.CLOSE: CloseAdapter(),
}


def get_adapter(platform: CRMPlatform | str) -> CRMPlatformAdapter:
    platform = CRMPlatform(platform) if not isinstance(platform, CRMPlatform) else platform
    try:
        return ADAPTERS[platform]
    except KeyError as exc:
        raise ValueError(f"no CRM adapter registered for {platform}") from exc


# ---------------------------------------------------------------------------
# Shared field-shape helpers
# ---------------------------------------------------------------------------
def _coerce_probability(value: Any, *, pct_scale: bool = False) -> float | None:
    """Normalise probability to [0.0, 1.0]. ``pct_scale=True`` divides by 100
    when the CRM reports values like ``25.0`` for "25%"."""
    if value is None or value == "":
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if pct_scale and f > 1.0:
        f = f / 100.0
    return max(0.0, min(1.0, f))


def _join_name(first: str | None, last: str | None) -> str | None:
    parts = [p for p in (first, last) if p]
    return " ".join(parts) if parts else None


def _extract_pipedrive_value(items: Any) -> str | None:
    """Pipedrive emails/phones are lists of {value, primary, label} dicts."""
    if isinstance(items, list):
        primary = next((x for x in items if isinstance(x, dict) and x.get("primary")), None)
        chosen = primary or (items[0] if items else None)
        if isinstance(chosen, dict):
            return chosen.get("value")
    if isinstance(items, str):
        return items or None
    return None


_MEDDIC_KEY_HINTS: dict[str, str] = {
    "metrics": "M",
    "metric": "M",
    "economic_buyer": "E",
    "economicbuyer": "E",
    "decision_criteria": "D",
    "decisioncriteria": "D",
    "decision_process": "DP",
    "decisionprocess": "DP",
    "paper_process": "PP",
    "paperprocess": "PP",
    "identify_pain": "I",
    "pain": "I",
    "champion": "C",
    "competition": "C2",
}


def _collect_meddic(props: dict[str, Any]) -> dict[str, str]:
    """Scan a CRM properties bag for MEDDIC/MEDDPICC field names and
    return a small map keyed by the canonical short letter. Field names
    vary by org, so this is a best-effort capture rather than a strict
    enum — feature workflows downstream should be tolerant of missing
    keys."""
    out: dict[str, str] = {}
    if not isinstance(props, dict):
        return out
    for raw_key, value in props.items():
        if not isinstance(raw_key, str) or value in (None, ""):
            continue
        normalised = raw_key.lower().replace("__c", "").replace("-", "_")
        for hint, letter in _MEDDIC_KEY_HINTS.items():
            if hint in normalised:
                out.setdefault(letter, str(value))
                break
    return out
