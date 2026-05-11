// Pipedream workflow — accepts a completed OAuthToken payload from the
// Messaging Surface OAuth redirect handler. Stores the credential in the
// Pipedream vault (per-rep, per-source) and notifies the Agent Runtime
// with a ConnectionStatus update.

import { validateOAuthToken } from "../../lib/action_request.mjs";
import { verifySignature, SignatureError } from "../../lib/verifier.mjs";
import { signRequest } from "../../lib/forwarder.mjs";
import { asSerializable, IntegrationError } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - OAuth Relay",
  description:
    "Receive an OAuth token from the Messaging Surface, persist it to the Pipedream vault, and notify the Agent Runtime with a ConnectionStatus update.",
  version: "0.1.0",
  type: "action",
  props: {
    webhookSecret: {
      type: "string",
      label: "Alex shared webhook secret",
      secret: true,
      default: "{{process.env.ALEX_WEBHOOK_SECRET}}",
    },
    agentRuntimeUrl: {
      type: "string",
      label: "Agent Runtime base URL",
      default: "{{process.env.ALEX_AGENT_RUNTIME_URL}}",
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
      const token = validateOAuthToken(JSON.parse(rawBody));

      // Persistence: real deployments use the Pipedream vault API or
      // app-specific $auth scope. The scaffold returns a vault_ref that
      // the Agent Runtime stores so it can correlate later.
      const vaultRef = `pd_vault::${token.tenant_id}::${token.rep_id}::${token.source}`;

      const notificationBody = JSON.stringify({
        tenant_id: token.tenant_id,
        rep_id: token.rep_id,
        source: token.source,
        status: "connected",
        scopes: token.scopes,
        vault_ref: vaultRef,
      });
      const { signature, timestamp } = signRequest({ secret: this.webhookSecret, body: notificationBody });
      const notifyUrl = `${this.agentRuntimeUrl.replace(/\/+$/, "")}/connections/status`;
      const response = await fetch(notifyUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Tenant-Id": token.tenant_id,
          "X-Alex-Signature": signature,
          "X-Alex-Timestamp": timestamp,
        },
        body: notificationBody,
      });
      if (response.status >= 400) {
        throw new IntegrationError(`agent runtime rejected ConnectionStatus update (${response.status})`, {
          code: "connection_status_update_failed",
          source: token.source,
          tenant_id: token.tenant_id,
          retriable: response.status >= 500,
        });
      }
      logActivity({
        source: token.source,
        operation: "oauth.relay",
        tenant_id: token.tenant_id,
        rep_id: token.rep_id,
        status: "ok",
        details: { vault_ref: vaultRef, scopes: token.scopes },
      });
      return { tenant_id: token.tenant_id, rep_id: token.rep_id, source: token.source, vault_ref: vaultRef };
    } catch (err) {
      if (err instanceof SignatureError) {
        await $.respond({ status: 401, body: { error: err.code, detail: err.message } });
        return;
      }
      const payload = asSerializable(err);
      logActivity({ source: "agent_runtime", operation: "oauth.relay", status: "error", details: payload });
      $.export("error", payload);
      throw err;
    }
  },
});
