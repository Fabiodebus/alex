"""CRMValidator — pre-approval safety layer for proposed CRM writes.

A feature workflow that wants Alex to update a CRM field calls
``CRMValidator.validate(FieldUpdate)`` *before* the approval card is
ever rendered. The validator:

1. Enforces the structural "no overwrite without current value" rule —
   every :class:`FieldUpdate` must carry both ``current_value`` and
   ``proposed_value`` so the approval card can show before/after.
2. Looks up the platform-specific constraint for ``(kind, field_name)``
   and runs the platform's type/enum/length/format checks.
3. Normalises the value (e.g. resolves a free-text stage to the
   platform's option_id, uppercases currency codes) so the downstream
   :class:`CRMWriter` and Pipedream connector get a payload the CRM API
   will accept.

Dry-run mode lives on the same object: :meth:`validate_dry_run` accepts
a :class:`DryRunCRMRequest` (batch of updates + notes) and returns a
:class:`DryRunCRMResult` carrying validated updates and any structured
errors. Notes pass through unchanged — they don't require current_value
because CRM-side notes are append-only by definition.

Per-platform validators are kept small on purpose. The constraint set
for v1 covers the fields the feature workflows in Phase 2 actually
write (stage, amount, close_date, probability, owner_email, note
bodies). Adding a new field is a one-line addition to the relevant
platform's ``CONSTRAINTS`` dict.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Protocol, runtime_checkable

import structlog

from ..schemas import (
    CRMNote,
    CRMPlatform,
    CRMRecordKind,
    CRMValidationError,
    CRMValidationResult,
    DryRunCRMRequest,
    DryRunCRMResult,
    FieldUpdate,
    ValidatedFieldUpdate,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Constraint descriptor — one row per (kind, field_name) we know about.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FieldConstraint:
    type: str  # 'string' | 'number' | 'enum' | 'date' | 'email' | 'currency' | 'probability'
    enum: tuple[str, ...] | None = None
    max_length: int | None = None
    immutable: bool = False
    platform_field_id: str | None = None
    # Optional normaliser that maps the proposed value to what the CRM
    # API expects on the wire (e.g. "presentation" -> stage option_id).
    normalize: Callable[[Any], Any] | None = None


# ---------------------------------------------------------------------------
# Platform validator interface + shared helpers
# ---------------------------------------------------------------------------
@runtime_checkable
class CRMPlatformValidator(Protocol):
    platform: CRMPlatform

    def constraint_for(
        self, *, kind: CRMRecordKind, field_name: str
    ) -> FieldConstraint | None: ...


class _BaseValidator:
    """Shared constraint lookup behaviour."""

    platform: CRMPlatform
    CONSTRAINTS: dict[tuple[CRMRecordKind, str], FieldConstraint] = {}

    def constraint_for(
        self, *, kind: CRMRecordKind, field_name: str
    ) -> FieldConstraint | None:
        return self.CONSTRAINTS.get((kind, field_name.lower()))


# ---------------------------------------------------------------------------
# HubSpot — option labels are case-insensitive on dealstage.
# ---------------------------------------------------------------------------
def _normalize_hubspot_stage(value: Any) -> str:
    if not isinstance(value, str):
        raise _value_error("dealstage must be a string")
    return value.strip().lower().replace(" ", "")


class HubspotValidator(_BaseValidator):
    platform = CRMPlatform.HUBSPOT
    CONSTRAINTS = {
        (CRMRecordKind.OPPORTUNITY, "dealname"): FieldConstraint("string", max_length=512),
        (CRMRecordKind.OPPORTUNITY, "dealstage"): FieldConstraint(
            "enum",
            enum=(
                "appointmentscheduled",
                "qualifiedtobuy",
                "presentation",
                "decisionmakerboughtin",
                "contractsent",
                "closedwon",
                "closedlost",
            ),
            normalize=_normalize_hubspot_stage,
        ),
        (CRMRecordKind.OPPORTUNITY, "amount"): FieldConstraint("number"),
        (CRMRecordKind.OPPORTUNITY, "closedate"): FieldConstraint("date"),
        (CRMRecordKind.OPPORTUNITY, "hs_probability"): FieldConstraint("probability"),
        (CRMRecordKind.OPPORTUNITY, "hubspot_owner_email"): FieldConstraint(
            "email", platform_field_id="hubspot_owner_id"
        ),
        (CRMRecordKind.OPPORTUNITY, "deal_currency_code"): FieldConstraint("currency"),
        (CRMRecordKind.OPPORTUNITY, "id"): FieldConstraint("string", immutable=True),
        (CRMRecordKind.CONTACT, "email"): FieldConstraint("email"),
        (CRMRecordKind.CONTACT, "firstname"): FieldConstraint("string", max_length=128),
        (CRMRecordKind.CONTACT, "lastname"): FieldConstraint("string", max_length=128),
        (CRMRecordKind.CONTACT, "jobtitle"): FieldConstraint("string", max_length=256),
    }


# ---------------------------------------------------------------------------
# Salesforce — enums are case-sensitive labels; probability is 0..100.
# ---------------------------------------------------------------------------
class SalesforceValidator(_BaseValidator):
    platform = CRMPlatform.SALESFORCE
    CONSTRAINTS = {
        (CRMRecordKind.OPPORTUNITY, "name"): FieldConstraint("string", max_length=120),
        (CRMRecordKind.OPPORTUNITY, "stagename"): FieldConstraint(
            "enum",
            enum=(
                "Prospecting",
                "Qualification",
                "Needs Analysis",
                "Value Proposition",
                "Id. Decision Makers",
                "Proposal/Price Quote",
                "Negotiation/Review",
                "Closed Won",
                "Closed Lost",
            ),
        ),
        (CRMRecordKind.OPPORTUNITY, "amount"): FieldConstraint("number"),
        (CRMRecordKind.OPPORTUNITY, "closedate"): FieldConstraint("date"),
        # Salesforce stores probability as 0..100 — validator accepts both
        # 0..1 and 0..100 but normalises to the pct scale on the way out.
        (CRMRecordKind.OPPORTUNITY, "probability"): FieldConstraint(
            "probability", normalize=lambda v: _as_percent(v)
        ),
        (CRMRecordKind.OPPORTUNITY, "currencyisocode"): FieldConstraint("currency"),
        (CRMRecordKind.OPPORTUNITY, "id"): FieldConstraint("string", immutable=True),
        (CRMRecordKind.CONTACT, "email"): FieldConstraint("email"),
        (CRMRecordKind.CONTACT, "firstname"): FieldConstraint("string", max_length=40),
        (CRMRecordKind.CONTACT, "lastname"): FieldConstraint("string", max_length=80),
        (CRMRecordKind.CONTACT, "title"): FieldConstraint("string", max_length=128),
    }


# ---------------------------------------------------------------------------
# Pipedrive — stage_id is numeric, currency code is uppercase 3-letter.
# ---------------------------------------------------------------------------
class PipedriveValidator(_BaseValidator):
    platform = CRMPlatform.PIPEDRIVE
    CONSTRAINTS = {
        (CRMRecordKind.OPPORTUNITY, "title"): FieldConstraint("string", max_length=255),
        (CRMRecordKind.OPPORTUNITY, "stage_id"): FieldConstraint(
            "number", normalize=lambda v: int(v)
        ),
        (CRMRecordKind.OPPORTUNITY, "value"): FieldConstraint("number"),
        (CRMRecordKind.OPPORTUNITY, "currency"): FieldConstraint("currency"),
        (CRMRecordKind.OPPORTUNITY, "expected_close_date"): FieldConstraint("date"),
        (CRMRecordKind.OPPORTUNITY, "probability"): FieldConstraint(
            "probability", normalize=lambda v: _as_percent(v)
        ),
        (CRMRecordKind.OPPORTUNITY, "id"): FieldConstraint("number", immutable=True),
        (CRMRecordKind.CONTACT, "email"): FieldConstraint("email"),
        (CRMRecordKind.CONTACT, "name"): FieldConstraint("string", max_length=128),
        (CRMRecordKind.CONTACT, "job_title"): FieldConstraint("string", max_length=128),
    }


# ---------------------------------------------------------------------------
# Close — `value` is an integer count of cents; status_id is opaque.
# ---------------------------------------------------------------------------
class CloseValidator(_BaseValidator):
    platform = CRMPlatform.CLOSE
    CONSTRAINTS = {
        (CRMRecordKind.OPPORTUNITY, "note"): FieldConstraint("string", max_length=4000),
        (CRMRecordKind.OPPORTUNITY, "status_id"): FieldConstraint("string"),
        (CRMRecordKind.OPPORTUNITY, "value"): FieldConstraint(
            "number", normalize=lambda v: int(v)
        ),
        (CRMRecordKind.OPPORTUNITY, "value_currency"): FieldConstraint("currency"),
        (CRMRecordKind.OPPORTUNITY, "expected_close_date"): FieldConstraint("date"),
        (CRMRecordKind.OPPORTUNITY, "confidence"): FieldConstraint(
            "probability", normalize=lambda v: _as_percent(v)
        ),
        (CRMRecordKind.OPPORTUNITY, "id"): FieldConstraint("string", immutable=True),
        (CRMRecordKind.CONTACT, "name"): FieldConstraint("string", max_length=128),
        (CRMRecordKind.CONTACT, "title"): FieldConstraint("string", max_length=128),
    }


PLATFORM_VALIDATORS: dict[CRMPlatform, CRMPlatformValidator] = {
    CRMPlatform.HUBSPOT: HubspotValidator(),
    CRMPlatform.SALESFORCE: SalesforceValidator(),
    CRMPlatform.PIPEDRIVE: PipedriveValidator(),
    CRMPlatform.CLOSE: CloseValidator(),
}


# ---------------------------------------------------------------------------
# Top-level validator
# ---------------------------------------------------------------------------
class CRMValidator:
    """Stateless service. One instance per process is plenty."""

    def __init__(
        self,
        *,
        platform_validators: dict[CRMPlatform, CRMPlatformValidator] | None = None,
    ) -> None:
        self._validators = platform_validators or PLATFORM_VALIDATORS

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def validate(self, update: FieldUpdate) -> CRMValidationResult:
        """Single-update validation. Returns one of (validated, error)."""
        if not update.has_current_value:
            return _err(
                "missing_current_value",
                "every CRM update must carry the field's current value before it "
                "reaches the approval flow",
                field=update.field_name,
            )

        platform_validator = self._validators.get(update.platform)
        if platform_validator is None:
            return _err(
                "unsupported_platform",
                f"no validator registered for {update.platform.value}",
                field=update.field_name,
            )

        constraint = platform_validator.constraint_for(
            kind=update.kind, field_name=update.field_name
        )
        if constraint is None:
            return _err(
                "unknown_field",
                f"{update.platform.value} has no known constraint for "
                f"{update.kind.value}.{update.field_name}; refusing to write a field "
                "Alex doesn't know how to validate",
                field=update.field_name,
            )
        if constraint.immutable:
            return _err(
                "immutable_field",
                f"{update.field_name} is immutable on {update.platform.value}",
                field=update.field_name,
            )

        # Same-value short-circuit — saves a CRM call and avoids an empty
        # diff in the approval card.
        if _values_equivalent(update.current_value, update.proposed_value):
            return _err(
                "no_change",
                "proposed value equals current value; nothing to write",
                field=update.field_name,
            )

        try:
            normalized = _coerce_for_constraint(update.proposed_value, constraint)
        except _ValidationFault as exc:
            return _err(exc.code, exc.message, field=update.field_name)
        if constraint.normalize is not None:
            try:
                normalized = constraint.normalize(normalized)
            except _ValidationFault as exc:
                return _err(exc.code, exc.message, field=update.field_name)
            except Exception as exc:  # pragma: no cover — defensive
                return _err(
                    "normalize_failed",
                    f"failed to normalise value: {exc}",
                    field=update.field_name,
                )

        # Enum membership runs AFTER the platform's normalize hook so the
        # comparison happens on the canonical wire-format value (HubSpot
        # lowers + strips spaces; Salesforce keeps the label as-is).
        if constraint.type == "enum" and constraint.enum and normalized is not None:
            if normalized not in constraint.enum:
                return _err(
                    "invalid_enum_value",
                    f"'{normalized}' is not a valid option for "
                    f"{update.platform.value}.{update.field_name}; "
                    f"allowed: {', '.join(constraint.enum)}",
                    field=update.field_name,
                )

        return CRMValidationResult(
            validated=ValidatedFieldUpdate(
                update=update,
                normalized_value=normalized,
                platform_field_id=constraint.platform_field_id,
            )
        )

    def validate_dry_run(self, request: DryRunCRMRequest) -> DryRunCRMResult:
        """Batch validate updates + pass-through notes for the approval card."""
        validated: list[ValidatedFieldUpdate] = []
        errors: list[CRMValidationError] = []
        for update in request.updates:
            result = self.validate(update)
            if result.is_valid and result.validated is not None:
                validated.append(result.validated)
            elif result.error is not None:
                errors.append(result.error)
        return DryRunCRMResult(
            valid=not errors,
            validated_updates=validated,
            errors=errors,
            notes=list(request.notes),
        )


# ---------------------------------------------------------------------------
# Coercion helpers
# ---------------------------------------------------------------------------
class _ValidationFault(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _value_error(message: str) -> _ValidationFault:
    return _ValidationFault("invalid_value", message)


def _coerce_for_constraint(value: Any, constraint: FieldConstraint) -> Any:
    t = constraint.type
    if t == "string":
        if value is None:
            return None
        if not isinstance(value, str):
            raise _value_error("expected string")
        if constraint.max_length is not None and len(value) > constraint.max_length:
            raise _ValidationFault(
                "too_long",
                f"value exceeds platform max_length ({constraint.max_length} chars)",
            )
        return value
    if t == "number":
        if value is None:
            return None
        if isinstance(value, bool):  # bool is an int subclass — exclude.
            raise _value_error("expected number, got bool")
        if isinstance(value, (int, float)):
            return value
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError as exc:
                raise _value_error(f"expected number, got '{value}'") from exc
        raise _value_error("expected number")
    if t == "enum":
        if value is None:
            return None
        if not isinstance(value, str):
            raise _value_error("expected enum option")
        # The enum comparison is whatever the platform validator's
        # normalize hook produces. For HubSpot we lowercase + strip
        # spaces; Salesforce keeps the label as-is. The check here is
        # done against the literal value; the per-platform normalise
        # hook runs *before* the membership check below.
        return value
    if t == "date":
        if value is None:
            return None
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if isinstance(value, str):
            for parser in (datetime.fromisoformat, date.fromisoformat):
                try:
                    parser(value.replace("Z", "+00:00") if parser is datetime.fromisoformat else value)
                except ValueError:
                    continue
                else:
                    return value
            raise _value_error(f"expected ISO date/datetime, got '{value}'")
        raise _value_error("expected ISO date string")
    if t == "email":
        if value is None:
            return None
        if not isinstance(value, str) or not _EMAIL_RE.match(value):
            raise _value_error(f"expected valid email, got '{value}'")
        return value.lower()
    if t == "currency":
        if value is None:
            return None
        if not isinstance(value, str) or not _CURRENCY_RE.match(value):
            raise _value_error(
                f"expected ISO 4217 currency code (3 uppercase letters), got '{value}'"
            )
        return value.upper()
    if t == "probability":
        if value is None:
            return None
        if isinstance(value, bool):
            raise _value_error("probability cannot be a bool")
        if not isinstance(value, (int, float)):
            try:
                value = float(value)
            except (TypeError, ValueError) as exc:
                raise _value_error(f"expected probability number, got '{value}'") from exc
        # Accept 0..1 OR 0..100; anything else is suspicious.
        if value < 0 or value > 100:
            raise _value_error("probability must lie in [0, 100]")
        return value
    raise _ValidationFault("unsupported_constraint_type", f"no coercion for type '{t}'")


def _values_equivalent(a: Any, b: Any) -> bool:
    if a == b:
        return True
    # Treat None/"" as equivalent when comparing for the no-change skip
    # so callers can pass either to mean "field is empty".
    if (a in (None, "")) and (b in (None, "")):
        return True
    return False


def _as_percent(value: Any) -> float:
    """Map a 0..1 probability to its 0..100 representation."""
    if isinstance(value, bool):
        raise _value_error("probability cannot be a bool")
    try:
        f = float(value)
    except (TypeError, ValueError) as exc:
        raise _value_error(f"expected number, got '{value}'") from exc
    return f * 100.0 if 0.0 <= f <= 1.0 else f


def _err(code: str, message: str, *, field: str | None = None) -> CRMValidationResult:
    return CRMValidationResult(
        error=CRMValidationError(code=code, message=message, field_name=field)
    )


_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3}$")


# ---------------------------------------------------------------------------
# Public registry helper
# ---------------------------------------------------------------------------
def get_platform_validator(platform: CRMPlatform | str) -> CRMPlatformValidator:
    platform = CRMPlatform(platform) if not isinstance(platform, CRMPlatform) else platform
    try:
        return PLATFORM_VALIDATORS[platform]
    except KeyError as exc:
        raise ValueError(f"no CRM validator registered for {platform}") from exc
