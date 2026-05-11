// Pipedream workflow code step — runs after a Gong "Call Ended" webhook
// fires. Normalizes the call-completion notification and forwards to the
// Agent Runtime. The transcript fetch belongs to a downstream feature
// workflow that consumes the IntegrationEvent.

import { normalizeGongRecordingCompleted } from "../../lib/normalizer.mjs";
import { forwardEvent } from "../../lib/forwarder.mjs";
import { IntegrationError, asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - Gong Recording Completed",
  description: "Normalize a Gong Call Ended webhook and forward to the Agent Runtime.",
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
      const event = normalizeGongRecordingCompleted(raw);
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
            source: "gong",
            tenant_id: this.tenantId,
            event_id: event.event_id,
            retriable: response.status >= 500,
          },
        );
      }
      logActivity({
        source: "gong",
        operation: "recording_forward",
        tenant_id: this.tenantId,
        status: response.deduplicated ? "deduplicated" : "ok",
        event_id: event.event_id,
      });
      return { event_id: event.event_id, status: response.status };
    } catch (err) {
      const payload = asSerializable(err);
      logActivity({
        source: "gong",
        operation: "recording_forward",
        tenant_id: this.tenantId,
        status: "error",
        details: payload,
      });
      $.export("error", payload);
      throw err;
    }
  },
});
