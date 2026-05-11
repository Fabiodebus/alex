"""Unit tests for the per-platform CRM adapters."""
from __future__ import annotations

import pytest

from alex_agent_runtime.schemas import CRMPlatform, CRMRecordKind
from alex_agent_runtime.services.crm_adapters import (
    CloseAdapter,
    HubspotAdapter,
    PipedriveAdapter,
    SalesforceAdapter,
    get_adapter,
)


def test_hubspot_opportunity_normalisation():
    raw = {
        "id": "deal-1",
        "updatedAt": "2026-05-11T08:00:00Z",
        "properties": {
            "dealname": "Acme Q3",
            "dealstage": "presentation",
            "amount": "12500",
            "deal_currency_code": "EUR",
            "hs_probability": "0.4",
            "closedate": "2026-08-01T00:00:00Z",
            "hubspot_owner_email": "alice@example.com",
            "economic_buyer__c": "Jane Smith",
            "decision_criteria__c": "Customer wants SLA + DPA",
            "metrics__c": "Reduce churn by 15%",
        },
        "associations": {"companies": [{"id": "acct-9"}]},
    }
    record = HubspotAdapter().normalize(raw=raw, kind=CRMRecordKind.OPPORTUNITY)
    assert record.platform is CRMPlatform.HUBSPOT
    assert record.name == "Acme Q3"
    assert record.stage == "presentation"
    assert record.amount_cents == 1_250_000
    assert record.currency == "EUR"
    assert record.probability == 0.4
    assert record.owner_email == "alice@example.com"
    assert record.account_external_id == "acct-9"
    # MEDDIC capture: three letters present.
    assert {"M", "E", "D"} <= set(record.meddic.keys())
    assert record.meddic["E"] == "Jane Smith"


def test_hubspot_handles_value_object_properties():
    """Some HubSpot endpoints wrap each property in {value: ...}."""
    raw = {
        "id": "deal-2",
        "properties": {
            "dealname": {"value": "Wrapped Deal"},
            "amount": {"value": "750"},
        },
    }
    record = HubspotAdapter().normalize(raw=raw, kind=CRMRecordKind.OPPORTUNITY)
    assert record.name == "Wrapped Deal"
    assert record.amount_cents == 75_000


def test_hubspot_contact_joins_name():
    raw = {
        "id": "c-1",
        "properties": {
            "firstname": "Alice",
            "lastname": "Anderson",
            "email": "alice@example.com",
            "jobtitle": "VP Sales",
        },
        "associations": {"companies": [{"id": "acct-9"}]},
    }
    record = HubspotAdapter().normalize(raw=raw, kind=CRMRecordKind.CONTACT)
    assert record.name == "Alice Anderson"
    assert record.email == "alice@example.com"
    assert record.title == "VP Sales"
    assert record.account_external_id == "acct-9"


def test_salesforce_opportunity_percentage_probability():
    raw = {
        "Id": "006xx0000004",
        "Name": "Acme Renewal",
        "StageName": "Proposal/Price Quote",
        "Amount": 25000.50,
        "CurrencyIsoCode": "USD",
        "Probability": 60.0,  # SF reports 0..100
        "CloseDate": "2026-12-31",
        "Owner": {"Email": "bob@example.com"},
        "AccountId": "001xx",
        "LastModifiedDate": "2026-05-11T08:00:00.000+0000",
    }
    record = SalesforceAdapter().normalize(raw=raw, kind=CRMRecordKind.OPPORTUNITY)
    assert record.amount_cents == 2_500_050  # 25000.50 * 100, rounded
    assert record.currency == "USD"
    assert record.probability == 0.6
    assert record.owner_email == "bob@example.com"
    assert record.account_external_id == "001xx"


def test_pipedrive_opportunity_dict_value():
    """Pipedrive's `value` is sometimes returned as a number, sometimes as
    a dict; the adapter handles both."""
    raw = {
        "id": 99,
        "title": "Pipeline test",
        "stage_id": 5,
        "value": {"amount": 1500.0, "currency": "EUR"},
        "probability": 25,
        "expected_close_date": "2026-07-01",
        "org_id": 7,
        "user_id": {"email": "rep@example.com"},
        "update_time": "2026-05-11 08:00:00",
    }
    record = PipedriveAdapter().normalize(raw=raw, kind=CRMRecordKind.OPPORTUNITY)
    assert record.amount_cents == 150_000
    assert record.currency == "EUR"
    assert record.stage == "5"
    assert record.probability == 0.25
    assert record.account_external_id == "7"
    assert record.owner_email == "rep@example.com"


def test_pipedrive_contact_emails_pick_primary():
    raw = {
        "id": 22,
        "name": "Charlie",
        "email": [
            {"value": "alt@example.com", "primary": False, "label": "work"},
            {"value": "primary@example.com", "primary": True, "label": "work"},
        ],
        "phone": [{"value": "+49123", "primary": True}],
    }
    record = PipedriveAdapter().normalize(raw=raw, kind=CRMRecordKind.CONTACT)
    assert record.email == "primary@example.com"
    assert record.phone == "+49123"


def test_close_opportunity_value_is_cents():
    raw = {
        "id": "oppo_X",
        "note": "Close opportunity",
        "status_label": "Negotiation",
        "value": 1234500,  # already cents per Close convention
        "value_currency": "EUR",
        "confidence": 75,
        "expected_close_date": "2026-06-15",
        "user_email": "alice@example.com",
        "lead_id": "lead_X",
        "date_updated": "2026-05-11T08:00:00Z",
    }
    record = CloseAdapter().normalize(raw=raw, kind=CRMRecordKind.OPPORTUNITY)
    assert record.amount_cents == 1_234_500
    assert record.currency == "EUR"
    assert record.stage == "Negotiation"
    assert record.probability == 0.75


def test_close_contact_picks_first_email_and_phone():
    raw = {
        "id": "cont_X",
        "name": "Bob",
        "emails": [{"email": "b@example.com", "type": "office"}],
        "phones": [{"phone": "+49 111", "type": "office"}],
        "lead_id": "lead_X",
        "title": "Director",
    }
    record = CloseAdapter().normalize(raw=raw, kind=CRMRecordKind.CONTACT)
    assert record.email == "b@example.com"
    assert record.phone == "+49 111"
    assert record.title == "Director"


def test_get_adapter_unknown_platform_raises():
    with pytest.raises(ValueError):
        get_adapter("oracle-cx")
