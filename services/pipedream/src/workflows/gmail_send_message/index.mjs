// Pipedream workflow — receives a signed ActionRequest, executes a Gmail
// send on the rep's behalf, logs activity.

import { validateActionRequest } from "../../lib/action_request.mjs";
import { verifySignature, SignatureError } from "../../lib/verifier.mjs";
import { gmailSend } from "../../lib/executors.mjs";
import { IntegrationError, asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - Gmail Send Message",
  description: "Send an approved Gmail message on behalf of the connected rep.",
  version: "0.1.0",
  type: "action",
  props: {
    webhookSecret: {
      type: "string",
      label: "Alex shared webhook secret",
      secret: true,
      default: "{{process.env.ALEX_WEBHOOK_SECRET}}",
    },
    gmail: { type: "app", app: "gmail" },
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
      const request = validateActionRequest(JSON.parse(rawBody));
      if (request.target_system !== "gmail" || request.action_type !== "email.send") {
        throw new IntegrationError("request not routable to gmail.send", {
          code: "wrong_route",
          source: "gmail",
          tenant_id: request.tenant_id,
          retriable: false,
        });
      }
      const result = await gmailSend({
        accessToken: this.gmail.$auth.oauth_access_token,
        senderEmail: request.payload.from ?? this.gmail.$auth.oauth_uid,
        to: request.payload.to,
        subject: request.payload.subject,
        bodyText: request.payload.body_text,
        bodyHtml: request.payload.body_html,
      });
      logActivity({
        source: "gmail",
        operation: "email.send",
        tenant_id: request.tenant_id,
        rep_id: request.rep_id,
        status: result.ok ? "ok" : "error",
        event_id: request.action_id,
        details: { status: result.status },
      });
      if (!result.ok) {
        throw new IntegrationError(`gmail rejected send with status ${result.status}`, {
          code: "gmail_send_failed",
          source: "gmail",
          tenant_id: request.tenant_id,
          retriable: result.retriable,
          event_id: request.action_id,
        });
      }
      return { action_id: request.action_id, status: result.status, body: result.body };
    } catch (err) {
      if (err instanceof SignatureError) {
        await $.respond({ status: 401, body: { error: err.code, detail: err.message } });
        return;
      }
      const payload = asSerializable(err);
      logActivity({ source: "gmail", operation: "email.send", status: "error", details: payload });
      $.export("error", payload);
      throw err;
    }
  },
});
