// Pipedream workflow code step — runs after a "HubSpot Record Updated"
// trigger fires. Normalizes the raw HubSpot event into an
// IntegrationEvent and forwards it to the Agent Runtime.
//
// Local syntax check: `node --check src/workflows/hubspot_record_update/index.mjs`.
// On Pipedream, `defineComponent` is injected as a global; locally it's
// treated as a free identifier at parse time but never invoked.

import { normalizeHubspotRecordUpdate } from "../../lib/normalizer.mjs";
import { forwardEvent } from "../../lib/forwarder.mjs";
import { IntegrationError, asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - HubSpot Record Update",
  description: "Normalize a HubSpot webhook record-update payload and forward to the Agent Runtime.",
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
    tenantId: {
      type: "string",
      label: "Tenant UUID",
      description:
        "Resolved upstream. Either map from the HubSpot account → tenant table or pass via the workflow trigger metadata.",
    },
  },
  async run({ steps, $ }) {
    const raw = steps.trigger.event;
    const records = Array.isArray(raw) ? raw : [raw];
    const results = [];

    for (const record of records) {
      try {
        const event = normalizeHubspotRecordUpdate(record);
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
              source: "hubspot",
              tenant_id: this.tenantId,
              event_id: event.event_id,
              retriable: response.status >= 500,
            },
          );
        }
        logActivity({
          source: "hubspot",
          operation: "record_update_forward",
          tenant_id: this.tenantId,
          status: response.deduplicated ? "deduplicated" : "ok",
          event_id: event.event_id,
        });
        results.push({ event_id: event.event_id, status: response.status });
      } catch (err) {
        const payload = asSerializable(err);
        logActivity({
          source: "hubspot",
          operation: "record_update_forward",
          tenant_id: this.tenantId,
          status: "error",
          details: payload,
        });
        $.export("error", payload);
        // Re-raise so Pipedream retries (subject to its retry policy) and
        // marks the run as failed for monitoring.
        throw err;
      }
    }

    return results;
  },
});
