// Pipedream workflow — fetch a meeting transcript on demand.
//
// FollowUpDraftComposer + CRMNoteComposer call this when they need the
// transcript for a calendar event. Production replaces `_lookup` with
// the rep's actual recording-tool integration (Krisp.ai, Granola,
// Fathom, etc).
import { verifySignature, SignatureError } from "../../lib/verifier.mjs";
import { asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - Transcript Fetch",
  description: "Fetch the transcript for a meeting and return TranscriptResult JSON.",
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
      const transcript = await _lookup(request);
      if (transcript == null) {
        await $.respond({ status: 404, body: { error: "no_transcript" } });
        return null;
      }
      logActivity({
        source: "transcript",
        operation: "transcript.fetch",
        tenant_id: request.tenant_id,
        status: "ok",
        details: { calendar_event_id: request.calendar_event_id },
      });
      return transcript;
    } catch (err) {
      if (err instanceof SignatureError) {
        await $.respond({ status: 401, body: { error: err.code, detail: err.message } });
        return;
      }
      const payload = asSerializable(err);
      logActivity({ source: "agent_runtime", operation: "transcript.fetch", status: "error", details: payload });
      $.export("error", payload);
      throw err;
    }
  },
});

async function _lookup(request) {
  if (!request.calendar_event_id || request.calendar_event_id.startsWith("missing-")) {
    return null;
  }
  return {
    calendar_event_id: request.calendar_event_id,
    provider: "stub",
    transcript:
      `Synthetic transcript for ${request.calendar_event_id}. ` +
      "Rep: thanks for the call. Buyer: we're shortlisting next week. " +
      "Rep: I'll send a one-pager + reference customer by Friday.",
    language: "en",
    speakers: ["Rep", "Buyer"],
    fetched_at: new Date().toISOString(),
  };
}
