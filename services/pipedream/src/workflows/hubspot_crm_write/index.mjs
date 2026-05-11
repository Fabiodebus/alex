// Pipedream workflow — receives a signed ActionRequest from the Agent
// Runtime, verifies signature, executes a HubSpot record update, logs
// activity, and returns a structured confirmation.

import { validateActionRequest } from "../../lib/action_request.mjs";
import { verifySignature, SignatureError } from "../../lib/verifier.mjs";
import { hubspotWrite } from "../../lib/executors.mjs";
import { IntegrationError, asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - HubSpot CRM Write",
  description: "Execute an approved HubSpot CRM write dispatched by the Agent Runtime.",
  version: "0.1.0",
  type: "action",
  props: {
    webhookSecret: {
      type: "string",
      label: "Alex shared webhook secret",
      secret: true,
      default: "{{process.env.ALEX_WEBHOOK_SECRET}}",
    },
    hubspot: {
      type: "app",
      app: "hubspot",
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
      const request = validateActionRequest(JSON.parse(rawBody));
      if (request.target_system !== "hubspot" || request.action_type !== "crm.write") {
        throw new IntegrationError("request not routable to hubspot.crm.write", {
          code: "wrong_route",
          source: "hubspot",
          tenant_id: request.tenant_id,
          retriable: false,
        });
      }
      const result = await hubspotWrite({
        accessToken: this.hubspot.$auth.oauth_access_token,
        targetId: request.target_id,
        objectType: request.payload.object_type ?? "contacts",
        properties: request.payload.properties ?? {},
      });
      logActivity({
        source: "hubspot",
        operation: "crm.write",
        tenant_id: request.tenant_id,
        rep_id: request.rep_id,
        status: result.ok ? "ok" : "error",
        event_id: request.action_id,
        details: { status: result.status, target_id: request.target_id },
      });
      if (!result.ok) {
        throw new IntegrationError(`hubspot rejected write with status ${result.status}`, {
          code: "hubspot_write_failed",
          source: "hubspot",
          tenant_id: request.tenant_id,
          retriable: result.retriable,
          event_id: request.action_id,
        });
      }
      return {
        action_id: request.action_id,
        status: result.status,
        target_id: request.target_id,
        body: result.body,
      };
    } catch (err) {
      if (err instanceof SignatureError) {
        await $.respond({ status: 401, body: { error: err.code, detail: err.message } });
        return;
      }
      const payload = asSerializable(err);
      logActivity({
        source: "hubspot",
        operation: "crm.write",
        status: "error",
        details: payload,
      });
      $.export("error", payload);
      throw err;
    }
  },
});
