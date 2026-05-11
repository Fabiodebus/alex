// Pipedream workflow — on-demand CRM record fetch.
//
// The Agent Runtime's CRMReader calls this when its local MemoryStore
// doesn't already hold a (platform, kind, external_id) tuple. The
// workflow verifies the HMAC, dispatches to the right connector based
// on `platform`, and returns the raw CRM record JSON for the
// Agent-Runtime-side adapter to normalise.
//
// This reference scaffold's `_lookup` is a stub that returns synthetic
// records keyed by platform + kind so the wire contract is exercised
// end-to-end. Production deployments replace `_lookup` with concrete
// connector calls (HubSpot crm/v3/objects/{type}/{id}, Salesforce
// sobjects/Opportunity/{id}, …) that return the same raw shape.

import { verifySignature, SignatureError } from "../../lib/verifier.mjs";
import { asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - CRM Fetch",
  description:
    "Fetch a single CRM record (opportunity / contact / account) from the named platform on demand.",
  version: "0.1.0",
  type: "action",
  props: {
    webhookSecret: {
      type: "string",
      label: "Alex shared webhook secret",
      secret: true,
      default: "{{process.env.ALEX_WEBHOOK_SECRET}}",
    },
  },
  async run({ steps, $ }) {
    const rawBody = JSON.stringify(steps.trigger.event.body ?? steps.trigger.event);
    const headers = steps.trigger.event.headers ?? {};
    try {
      verifySignature({
        secret: this.webhookSecret,
        body: rawBody,
        signature: headers["x-alex-signature"] ?? headers["X-Alex-Signature"] ?? null,
        timestamp: headers["x-alex-timestamp"] ?? headers["X-Alex-Timestamp"] ?? null,
      });
      const request = JSON.parse(rawBody);
      const record = await _lookup(request);
      if (record == null) {
        await $.respond({ status: 404, body: { error: "not_found" } });
        return null;
      }
      logActivity({
        source: request.platform,
        operation: "crm.fetch",
        tenant_id: request.tenant_id,
        status: "ok",
        details: { kind: request.kind, external_id: request.external_id },
      });
      return record;
    } catch (err) {
      if (err instanceof SignatureError) {
        await $.respond({ status: 401, body: { error: err.code, detail: err.message } });
        return;
      }
      const payload = asSerializable(err);
      logActivity({
        source: "agent_runtime",
        operation: "crm.fetch",
        status: "error",
        details: payload,
      });
      $.export("error", payload);
      throw err;
    }
  },
});

// ---------------------------------------------------------------------------
// Stub lookup — replace with real connector calls per `platform`.
// ---------------------------------------------------------------------------
async function _lookup(request) {
  const { platform, kind, external_id } = request;
  // Return null to simulate a miss for unknown external_ids.
  if (!external_id || external_id.startsWith("missing-")) {
    return null;
  }
  const baseShape = {
    id: external_id,
    external_id,
    updatedAt: new Date().toISOString(),
  };
  if (platform === "hubspot") {
    return kind === "opportunity"
      ? {
          ...baseShape,
          properties: {
            dealname: `Synthetic deal ${external_id}`,
            dealstage: "qualification",
            amount: "12500",
            deal_currency_code: "EUR",
            hs_probability: "0.35",
            closedate: "2026-08-01T00:00:00Z",
            hubspot_owner_email: "rep@example.com",
          },
          associations: { companies: [{ id: `acct-${external_id}` }] },
        }
      : { ...baseShape, properties: { firstname: "Stub", lastname: "Contact", email: "x@y" } };
  }
  if (platform === "salesforce") {
    return {
      ...baseShape,
      Id: external_id,
      Name: `Synthetic SF ${external_id}`,
      StageName: "Prospecting",
      Amount: 9000,
      CurrencyIsoCode: "EUR",
      Probability: 25,
      CloseDate: "2026-09-15",
      Owner: { Email: "rep@example.com" },
      AccountId: `acct-${external_id}`,
    };
  }
  if (platform === "pipedrive") {
    return {
      ...baseShape,
      title: `Synthetic Pipedrive ${external_id}`,
      stage_id: 3,
      value: 8000,
      currency: "EUR",
      probability: 30,
      expected_close_date: "2026-10-01",
      org_id: 42,
    };
  }
  if (platform === "close") {
    return {
      ...baseShape,
      note: `Synthetic Close ${external_id}`,
      status_label: "Demo Scheduled",
      value: 750000,
      value_currency: "EUR",
      confidence: 40,
      expected_close_date: "2026-11-01",
      lead_id: `lead-${external_id}`,
    };
  }
  return null;
}
