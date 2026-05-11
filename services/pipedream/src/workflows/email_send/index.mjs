// Pipedream workflow — send an approved follow-up email.
//
// The Agent Runtime's ApprovedActionDispatcher calls this when a rep
// approves an `email.send` task. Production deployments replace
// `_send` with the rep's actual email provider (Gmail Send API,
// Microsoft Graph sendMail, etc).
import { verifySignature, SignatureError } from "../../lib/verifier.mjs";
import { asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - Email Send",
  description: "Dispatch an approved follow-up email via the rep's connected email account.",
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
      const result = await _send(request);
      logActivity({
        source: "email",
        operation: "email.send",
        tenant_id: request.tenant_id,
        status: result.delivered ? "ok" : "error",
        details: { to: request.to, subject: request.subject },
      });
      return result;
    } catch (err) {
      if (err instanceof SignatureError) {
        await $.respond({ status: 401, body: { error: err.code, detail: err.message } });
        return;
      }
      const payload = asSerializable(err);
      logActivity({ source: "agent_runtime", operation: "email.send", status: "error", details: payload });
      $.export("error", payload);
      throw err;
    }
  },
});

async function _send(request) {
  // Synthetic failure hook so local QA can exercise the "delivery
  // failed" notification path: any idempotency_key ending in ":fail"
  // returns delivered=false.
  if (request.idempotency_key?.endsWith(":fail")) {
    return {
      delivered: false,
      provider: "stub",
      error: "simulated provider rejection",
    };
  }
  return {
    delivered: true,
    provider: "stub",
    provider_message_id: `stub-${request.idempotency_key ?? Date.now()}`,
  };
}
