"""Unit tests for CRMValidator + per-platform validators."""
from __future__ import annotations

from uuid import uuid4

import pytest

from alex_agent_runtime.schemas import (
    CRMNote,
    CRMPlatform,
    CRMRecordKind,
    DryRunCRMRequest,
    FieldUpdate,
)
from alex_agent_runtime.services.crm_validator import (
    CRMValidator,
    HubspotValidator,
    get_platform_validator,
)


def _hubspot_stage_update(*, current: str = "qualification", proposed: str = "Presentation") -> FieldUpdate:
    return FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-1",
        field_name="dealstage",
        current_value=current,
        proposed_value=proposed,
    )


def test_validator_accepts_hubspot_stage_with_case_insensitive_normalisation():
    v = CRMValidator()
    result = v.validate(_hubspot_stage_update(proposed="Presentation"))
    assert result.is_valid
    assert result.validated is not None
    # The HubSpot normaliser lowercases + strips spaces.
    assert result.validated.normalized_value == "presentation"


def test_validator_rejects_missing_current_value():
    """The structural safety rule: a FieldUpdate without current_value
    must not reach the approval flow."""
    raw = FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-1",
        field_name="dealstage",
        # current_value intentionally omitted
        proposed_value="presentation",
    )
    result = CRMValidator().validate(raw)
    assert not result.is_valid
    assert result.error is not None
    assert result.error.code == "missing_current_value"


def test_validator_accepts_explicit_none_current_value():
    """current_value=None means 'CRM holds nothing in this field today'.
    That is a legal before-state and must be accepted."""
    raw = FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-1",
        field_name="hs_probability",
        current_value=None,
        proposed_value=0.4,
    )
    result = CRMValidator().validate(raw)
    assert result.is_valid


def test_validator_rejects_unknown_field():
    raw = FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-1",
        field_name="not_a_field",
        current_value="old",
        proposed_value="new",
    )
    result = CRMValidator().validate(raw)
    assert not result.is_valid
    assert result.error is not None and result.error.code == "unknown_field"


def test_validator_rejects_immutable_id_field():
    raw = FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-1",
        field_name="id",
        current_value="deal-1",
        proposed_value="deal-2",
    )
    result = CRMValidator().validate(raw)
    assert not result.is_valid
    assert result.error is not None and result.error.code == "immutable_field"


def test_validator_rejects_no_change_writes():
    """Saves a CRM round-trip and avoids an empty diff in the approval card."""
    raw = _hubspot_stage_update(current="presentation", proposed="presentation")
    result = CRMValidator().validate(raw)
    assert not result.is_valid
    assert result.error is not None and result.error.code == "no_change"


def test_validator_rejects_invalid_enum_after_normalise():
    """'Foobar' normalises to 'foobar' which isn't a valid HubSpot stage."""
    raw = _hubspot_stage_update(proposed="Foobar")
    result = CRMValidator().validate(raw)
    assert not result.is_valid
    assert result.error is not None and result.error.code == "invalid_enum_value"


def test_validator_string_length_check():
    raw = FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-1",
        field_name="dealname",
        current_value="old name",
        proposed_value="x" * 600,  # exceeds 512-char limit
    )
    result = CRMValidator().validate(raw)
    assert not result.is_valid
    assert result.error is not None and result.error.code == "too_long"


def test_validator_currency_uppercases_and_validates():
    raw = FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-1",
        field_name="deal_currency_code",
        current_value="USD",
        proposed_value="eur",
    )
    result = CRMValidator().validate(raw)
    assert result.is_valid
    assert result.validated is not None
    assert result.validated.normalized_value == "EUR"


def test_validator_email_validates_and_lowercases():
    raw = FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-1",
        field_name="hubspot_owner_email",
        current_value="old@example.com",
        proposed_value="NEW@Example.COM",
    )
    result = CRMValidator().validate(raw)
    assert result.is_valid
    assert result.validated is not None
    assert result.validated.normalized_value == "new@example.com"
    # platform_field_id maps owner_email -> owner_id when configured.
    assert result.validated.platform_field_id == "hubspot_owner_id"


def test_validator_email_rejects_invalid():
    raw = FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.CONTACT,
        external_id="c-1",
        field_name="email",
        current_value="old@example.com",
        proposed_value="not-an-email",
    )
    result = CRMValidator().validate(raw)
    assert not result.is_valid
    assert result.error is not None and result.error.code == "invalid_value"


def test_validator_salesforce_probability_normalises_to_pct_scale():
    raw = FieldUpdate(
        platform=CRMPlatform.SALESFORCE,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="006xx",
        field_name="probability",
        current_value=25.0,
        proposed_value=0.6,
    )
    result = CRMValidator().validate(raw)
    assert result.is_valid
    assert result.validated is not None
    # SF stores 0..100; 0.6 -> 60.0
    assert result.validated.normalized_value == pytest.approx(60.0)


def test_validator_salesforce_stage_enum_is_case_sensitive():
    """Salesforce labels are case-sensitive on the wire."""
    raw = FieldUpdate(
        platform=CRMPlatform.SALESFORCE,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="006xx",
        field_name="StageName",
        current_value="Prospecting",
        proposed_value="prospecting",  # lower-case — should reject
    )
    result = CRMValidator().validate(raw)
    assert not result.is_valid
    assert result.error is not None and result.error.code == "invalid_enum_value"


def test_validator_pipedrive_stage_id_coerces_string_to_int():
    raw = FieldUpdate(
        platform=CRMPlatform.PIPEDRIVE,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="42",
        field_name="stage_id",
        current_value=3,
        proposed_value="5",
    )
    result = CRMValidator().validate(raw)
    assert result.is_valid
    assert result.validated is not None
    assert result.validated.normalized_value == 5


def test_validator_close_value_coerces_to_int_cents():
    raw = FieldUpdate(
        platform=CRMPlatform.CLOSE,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="oppo_X",
        field_name="value",
        current_value=100000,
        proposed_value=125000.0,
    )
    result = CRMValidator().validate(raw)
    assert result.is_valid
    assert result.validated is not None and result.validated.normalized_value == 125000


def test_validator_date_field_accepts_iso():
    raw = FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-1",
        field_name="closedate",
        current_value="2026-06-15",
        proposed_value="2026-09-30",
    )
    result = CRMValidator().validate(raw)
    assert result.is_valid


def test_validator_date_field_rejects_garbage():
    raw = FieldUpdate(
        platform=CRMPlatform.HUBSPOT,
        kind=CRMRecordKind.OPPORTUNITY,
        external_id="deal-1",
        field_name="closedate",
        current_value="2026-06-15",
        proposed_value="not-a-date",
    )
    result = CRMValidator().validate(raw)
    assert not result.is_valid
    assert result.error is not None and result.error.code == "invalid_value"


def test_dry_run_batches_collect_errors_per_update():
    """Mixed-validity batch returns one validated entry + one error."""
    req = DryRunCRMRequest(
        tenant_id=uuid4(),
        rep_id=uuid4(),
        updates=[
            _hubspot_stage_update(proposed="Presentation"),  # ok
            _hubspot_stage_update(proposed="Foobar"),  # bad enum
        ],
        notes=[
            CRMNote(
                platform=CRMPlatform.HUBSPOT,
                kind=CRMRecordKind.OPPORTUNITY,
                external_id="deal-1",
                body="Customer renewed for Q4",
            )
        ],
    )
    result = CRMValidator().validate_dry_run(req)
    assert result.valid is False
    assert len(result.validated_updates) == 1
    assert len(result.errors) == 1
    assert result.errors[0].code == "invalid_enum_value"
    # Notes pass through unchanged for the approval-card renderer.
    assert len(result.notes) == 1
    assert result.notes[0].body == "Customer renewed for Q4"


def test_dry_run_marks_valid_when_all_updates_clear():
    req = DryRunCRMRequest(
        tenant_id=uuid4(),
        rep_id=uuid4(),
        updates=[_hubspot_stage_update(proposed="Presentation")],
    )
    result = CRMValidator().validate_dry_run(req)
    assert result.valid is True
    assert len(result.validated_updates) == 1
    assert result.errors == []


def test_get_platform_validator_unknown_raises():
    with pytest.raises(ValueError):
        get_platform_validator("oracle-cx")


def test_platform_validator_constraint_for_returns_none_for_unknown():
    v = HubspotValidator()
    assert v.constraint_for(kind=CRMRecordKind.OPPORTUNITY, field_name="totally_unknown") is None
