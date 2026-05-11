// Pipedream workflow code step — runs after a Gmail trigger that has
// already resolved a message id to its compact metadata (subject, from,
// snippet, etc.). The message body fetch belongs to the trigger / earlier
// step; this step only normalizes and forwards.

import { normalizeGmailMessage } from "../../lib/normalizer.mjs";
import { forwardEvent } from "../../lib/forwarder.mjs";
import { IntegrationError, asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - Gmail Message Received",
  description: "Normalize a Gmail message metadata payload and forward to the Agent Runtime.",
  version: "0.1.0",
  type: "action",
  props: {
    agentRuntimeUrl: {
      type: "string",
      label: "Agent Runtime base URL",
      default: "{{process.env.ALEX_AGENT_RUNTIME_URL}}",
    },
    webhookSecret: {
      type: "string",
      label: "Alex webhook shared secret",
      secret: true,
      default: "{{process.env.ALEX_WEBHOOK_SECRET}}",
    },
    tenantId: { type: "string", label: "Tenant UUID" },
  },
  async run({ steps, $ }) {
    const raw = steps.trigger.event;
    try {
      const event = normalizeGmailMessage(raw);
      const response = await forwardEvent({
        baseUrl: this.agentRuntimeUrl,
        secret: this.webhookSecret,
        tenantId: this.tenantId,
        event,
      });
      if (response.status >= 400) {
        throw new IntegrationError(
          `agent runtime rejected event with status ${response.status}`,
          {
            code: "agent_runtime_rejected",
            source: "gmail",
            tenant_id: this.tenantId,
            event_id: event.event_id,
            retriable: response.status >= 500,
          },
        );
      }
      logActivity({
        source: "gmail",
        operation: "message_forward",
        tenant_id: this.tenantId,
        status: response.deduplicated ? "deduplicated" : "ok",
        event_id: event.event_id,
      });
      return { event_id: event.event_id, status: response.status };
    } catch (err) {
      const payload = asSerializable(err);
      logActivity({
        source: "gmail",
        operation: "message_forward",
        tenant_id: this.tenantId,
        status: "error",
        details: payload,
      });
      $.export("error", payload);
      throw err;
    }
  },
});
