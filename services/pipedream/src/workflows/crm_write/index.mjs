// Pipedream workflow — dispatch an approved CRM write.
//
// The Agent Runtime's CRMWriter calls this workflow after the rep has
// approved a validated FieldUpdate batch. The workflow verifies the
// HMAC, routes to the right connector based on `platform`, executes
// the writes, and returns a CRMWriteResult-shaped JSON for the runtime
// to ingest. The reference `_dispatch` is a stub that always succeeds
// — production deployments replace it with concrete platform writes
// (HubSpot crm/v3/objects/deals/{id} PATCH, Salesforce
// sobjects/Opportunity/{id} PATCH, etc).

import { verifySignature, SignatureError } from "../../lib/verifier.mjs";
import { asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - CRM Write",
  description:
    "Dispatch a validated, rep-approved CRM write to the named platform's connector.",
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
      const result = await _dispatch(request);
      logActivity({
        source: request.platform,
        operation: "crm.write",
        tenant_id: request.tenant_id,
        status: result.status,
        details: {
          external_id: request.external_id,
          succeeded_fields: result.succeeded_fields,
          failed_fields: result.failed_fields,
        },
      });
      return result;
    } catch (err) {
      if (err instanceof SignatureError) {
        await $.respond({ status: 401, body: { error: err.code, detail: err.message } });
        return;
      }
      const payload = asSerializable(err);
      logActivity({
        source: "agent_runtime",
        operation: "crm.write",
        status: "error",
        details: payload,
      });
      $.export("error", payload);
      throw err;
    }
  },
});

// ---------------------------------------------------------------------------
// Stub dispatcher — replace with per-platform connector calls. The wire
// shape must match the runtime's `CRMWriteResult` schema:
//   { status, platform, external_id, succeeded_fields, failed_fields, raw_response }
// ---------------------------------------------------------------------------
async function _dispatch(request) {
  const fields = (request.field_updates ?? []).map((u) => u.update?.field_name).filter(Boolean);
  // Synthetic "drop the last field" rule for end-to-end failure
  // exercises during local QA. Production removes this block.
  if (request.idempotency_key?.endsWith(":fail-last") && fields.length > 0) {
    const failed = [fields[fields.length - 1]];
    const succeeded = fields.slice(0, -1);
    return {
      status: "failed",
      platform: request.platform,
      external_id: request.external_id,
      succeeded_fields: succeeded,
      failed_fields: failed,
      raw_response: { backend: "stub", note: "simulated partial failure" },
    };
  }
  return {
    status: "succeeded",
    platform: request.platform,
    external_id: request.external_id,
    succeeded_fields: fields,
    failed_fields: [],
    raw_response: { backend: "stub" },
  };
}
