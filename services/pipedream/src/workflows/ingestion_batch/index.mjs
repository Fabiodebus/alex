// Pipedream workflow — initial backfill source for the Agent Runtime's
// IngestionPipeline. Verifies the inbound HMAC signature, then returns a
// canonical `IngestionBatch` shape sourced from the rep's connected CRM,
// email, and recording tools.
//
// In this reference scaffold the body of `_collect` is a stub that
// produces a deterministic synthetic batch — it demonstrates the wire
// contract without depending on a real Pipedream workspace with live
// OAuth credentials. Production deployments replace `_collect` with
// concrete connector code (HubSpot crm/v3/objects/deals + contacts,
// Gmail users.messages.list, Gong /v2/calls, …) that returns the same
// `records` array shape.

import { verifySignature, SignatureError } from "../../lib/verifier.mjs";
import { asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - Ingestion Batch",
  description:
    "Return a normalised IngestionBatch (recent CRM, email, and recording records) for a rep's initial backfill.",
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
      const batch = await _collect(request);
      logActivity({
        source: "agent_runtime",
        operation: "ingestion.batch",
        tenant_id: request.tenant_id,
        rep_id: request.rep_id,
        status: "ok",
        details: { records: batch.records.length },
      });
      return batch;
    } catch (err) {
      if (err instanceof SignatureError) {
        await $.respond({ status: 401, body: { error: err.code, detail: err.message } });
        return;
      }
      const payload = asSerializable(err);
      logActivity({
        source: "agent_runtime",
        operation: "ingestion.batch",
        status: "error",
        details: payload,
      });
      $.export("error", payload);
      throw err;
    }
  },
});

// ---------------------------------------------------------------------------
// Reference collector — replace with real connector calls in production.
// ---------------------------------------------------------------------------
async function _collect(request) {
  const tenantId = request.tenant_id;
  const repId = request.rep_id;
  const now = new Date();
  return {
    tenant_id: tenantId,
    rep_id: repId,
    fetched_at: now.toISOString(),
    records: [
      {
        kind: "crm_opportunity",
        external_id: `deal-${repId}-pd-1`,
        content: "Deal: Pipedream-sourced opportunity — Stage: Discovery — Amount: €80,000",
        occurred_at: new Date(now.getTime() - 2 * 86400e3).toISOString(),
        attributes: { account_external_id: "acct-pipedream", stage: "discovery" },
      },
      {
        kind: "email_thread",
        external_id: `thread-${repId}-pd-1`,
        content: "Email thread: pricing nuance for the European entity.",
        occurred_at: new Date(now.getTime() - 1 * 86400e3).toISOString(),
        attributes: { account_external_id: "acct-pipedream" },
      },
    ],
  };
}
