// Pipedream workflow — uploads a base64-encoded document to Google Drive.
// Used by the Deal Rooms feature to materialise generated assets.

import { validateActionRequest } from "../../lib/action_request.mjs";
import { verifySignature, SignatureError } from "../../lib/verifier.mjs";
import { googleDriveUpload } from "../../lib/executors.mjs";
import { IntegrationError, asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - Google Drive Upload",
  description: "Upload an approved document to Google Drive (Deal Rooms reference).",
  version: "0.1.0",
  type: "action",
  props: {
    webhookSecret: {
      type: "string",
      label: "Alex shared webhook secret",
      secret: true,
      default: "{{process.env.ALEX_WEBHOOK_SECRET}}",
    },
    google_drive: { type: "app", app: "google_drive" },
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
      if (request.target_system !== "google_drive" || request.action_type !== "doc.upload") {
        throw new IntegrationError("request not routable to google_drive.doc.upload", {
          code: "wrong_route",
          source: "google_drive",
          tenant_id: request.tenant_id,
          retriable: false,
        });
      }
      const result = await googleDriveUpload({
        accessToken: this.google_drive.$auth.oauth_access_token,
        name: request.payload.name,
        mimeType: request.payload.mime_type,
        content: request.payload.content_base64,
        parents: request.payload.parents,
      });
      logActivity({
        source: "google_drive",
        operation: "doc.upload",
        tenant_id: request.tenant_id,
        rep_id: request.rep_id,
        status: result.ok ? "ok" : "error",
        event_id: request.action_id,
        details: { status: result.status, file_id: result.body?.id },
      });
      if (!result.ok) {
        throw new IntegrationError(`drive rejected upload with status ${result.status}`, {
          code: "drive_upload_failed",
          source: "google_drive",
          tenant_id: request.tenant_id,
          retriable: result.retriable,
          event_id: request.action_id,
        });
      }
      return { action_id: request.action_id, status: result.status, file_id: result.body?.id, body: result.body };
    } catch (err) {
      if (err instanceof SignatureError) {
        await $.respond({ status: 401, body: { error: err.code, detail: err.message } });
        return;
      }
      const payload = asSerializable(err);
      logActivity({ source: "google_drive", operation: "doc.upload", status: "error", details: payload });
      $.export("error", payload);
      throw err;
    }
  },
});
