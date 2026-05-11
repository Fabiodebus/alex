// Pipedream workflow — receive a Google / Outlook calendar webhook and
// forward a canonical CalendarEvent payload to the Agent Runtime as a
// `calendar.update` IntegrationEvent.
//
// The runtime's MeetingClassifier (WO #11) consumes this and decides
// whether to fire MeetingDetected / MeetingCancelled. Per ADR-001 in
// the Calendar & Meeting Detection blueprint, classification + CRM
// resolution live entirely in the runtime, so this workflow stays
// thin: verify the inbound HMAC (if Pipedream is acting as a relay
// for a signed call), normalise to CalendarEvent, and POST to the
// runtime's /events endpoint with the standard
// X-Alex-Signature / X-Alex-Timestamp / X-Tenant-Id headers.
//
// The reference `_normalise` here returns a synthetic shape so the
// wire contract is exercised end-to-end. Production deployments
// replace it with the connector-specific normalisation
// (Google Calendar events.watch payload, Microsoft Graph subscription
// change notification, etc).

import { verifySignature, SignatureError } from "../../lib/verifier.mjs";
import { asSerializable } from "../../lib/errors.mjs";
import { logActivity } from "../../lib/activity_log.mjs";

export default defineComponent({
  name: "Alex - Calendar Update",
  description:
    "Receive a calendar event change from Google / Outlook and forward a canonical CalendarEvent to the runtime.",
  version: "0.1.0",
  type: "action",
  props: {
    webhookSecret: {
      type: "string",
      label: "Alex shared webhook secret",
      secret: true,
      default: "{{process.env.ALEX_WEBHOOK_SECRET}}",
    },
    runtimeEventsUrl: {
      type: "string",
      label: "Agent Runtime /events URL",
      default: "{{process.env.ALEX_AGENT_RUNTIME_EVENTS_URL}}",
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
      const calendarEvent = _normalise(JSON.parse(rawBody));
      const integrationEvent = {
        event_id: `calendar:${calendarEvent.provider}:${calendarEvent.calendar_event_id}:${calendarEvent.status}`,
        source: calendarEvent.provider,
        kind: "calendar.update",
        occurred_at: new Date().toISOString(),
        payload: calendarEvent,
      };
      logActivity({
        source: calendarEvent.provider,
        operation: "calendar.update",
        tenant_id: calendarEvent.tenant_id,
        status: "ok",
        details: {
          calendar_event_id: calendarEvent.calendar_event_id,
          status: calendarEvent.status,
        },
      });
      return integrationEvent;
    } catch (err) {
      if (err instanceof SignatureError) {
        await $.respond({ status: 401, body: { error: err.code, detail: err.message } });
        return;
      }
      const payload = asSerializable(err);
      logActivity({
        source: "agent_runtime",
        operation: "calendar.update",
        status: "error",
        details: payload,
      });
      $.export("error", payload);
      throw err;
    }
  },
});

// ---------------------------------------------------------------------------
// Stub normaliser — replace with provider-specific parsing per
// `provider`. The output must match the runtime's CalendarEvent schema.
// ---------------------------------------------------------------------------
function _normalise(input) {
  return {
    provider: input.provider ?? "google_calendar",
    calendar_event_id: input.calendar_event_id ?? input.id ?? "synth-1",
    tenant_id: input.tenant_id,
    rep_id: input.rep_id,
    rep_email: input.rep_email ?? "rep@example.com",
    title: input.title ?? input.summary ?? null,
    description: input.description ?? null,
    location: input.location ?? null,
    start_at: input.start_at ?? input.start?.dateTime ?? null,
    end_at: input.end_at ?? input.end?.dateTime ?? null,
    status: input.status ?? "confirmed",
    organizer_email: input.organizer_email ?? input.organizer?.email ?? null,
    attendees: (input.attendees ?? []).map((a) => ({
      email: a.email,
      name: a.displayName ?? a.name ?? null,
      response_status: a.responseStatus ?? a.response_status ?? null,
      is_organizer: Boolean(a.organizer),
    })),
    raw: input,
  };
}
