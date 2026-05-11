// Pipedream workflow — validates a proposed CRM write without executing
// it. Returns a DryRunResult preview the Agent Runtime can render for the
// rep before they approve.

import { validateDryRunRequest } from "../../lib/action_request.mjs";
import { verifySignature, SignatureError } from "../../lib/verifier.mjs";
import { dryRunCrmWrite } from "../../lib/dry_run.mjs";
import { asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - Dry-Run CRM Write",
  description: "Validate a proposed CRM write payload and return a structured preview.",
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
      const request = validateDryRunRequest(JSON.parse(rawBody));
      const result = dryRunCrmWrite(request);
      logActivity({
        source: request.target_system,
        operation: "dry_run.crm.write",
        tenant_id: request.tenant_id,
        rep_id: request.rep_id,
        status: result.valid ? "ok" : "error",
        details: { errors: result.errors.length, target_id: result.target_id },
      });
      return result;
    } catch (err) {
      if (err instanceof SignatureError) {
        await $.respond({ status: 401, body: { error: err.code, detail: err.message } });
        return;
      }
      const payload = asSerializable(err);
      logActivity({ source: "agent_runtime", operation: "dry_run.crm.write", status: "error", details: payload });
      $.export("error", payload);
      throw err;
    }
  },
});
