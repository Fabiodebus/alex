// Per-source normalization. Each function takes the raw webhook payload
// from one external system and returns an IntegrationEvent-shaped object
// (subject to validateIntegrationEvent before forwarding). Keep these as
// pure functions of their input so they're unit-testable without a live
// Pipedream context.

import { validateIntegrationEvent } from "./integration_event.mjs";

/**
 * @param {{ subscriptionType?: string, objectId?: string|number, occurredAt?: string|number, propertyName?: string, propertyValue?: unknown, portalId?: number|string, sourceId?: string }} raw
 *   Single record from HubSpot's standard webhook batch payload.
 *   See https://developers.hubspot.com/docs/api/webhooks/v3
 */
export function normalizeHubspotRecordUpdate(raw) {
  if (!raw || typeof raw !== "object") {
    throw new TypeError("normalizeHubspotRecordUpdate: payload must be an object");
  }
  const subscriptionType = raw.subscriptionType ?? "unknown";
  const objectId = raw.objectId !== undefined ? String(raw.objectId) : "";
  const occurredAtIso = _toIso(raw.occurredAt);
  return validateIntegrationEvent({
    event_id: `hubspot:${subscriptionType}:${objectId}:${occurredAtIso}`,
    source: "hubspot",
    kind: "crm.activity_logged",
    occurred_at: occurredAtIso,
    payload: {
      subscription_type: subscriptionType,
      object_id: objectId,
      property_name: raw.propertyName ?? null,
      property_value: raw.propertyValue ?? null,
      portal_id: raw.portalId !== undefined ? String(raw.portalId) : null,
      source_id: raw.sourceId ?? null,
    },
  });
}

/**
 * @param {{ id?: string, summary?: string, start?: {dateTime?: string, date?: string}, end?: {dateTime?: string, date?: string}, status?: string, organizer?: {email?: string}, attendees?: Array<{email?: string, responseStatus?: string}>, updated?: string, htmlLink?: string, hangoutLink?: string }} raw
 *   Google Calendar event resource. See https://developers.google.com/calendar/api/v3/reference/events
 */
export function normalizeGoogleCalendarEvent(raw) {
  if (!raw || typeof raw !== "object") {
    throw new TypeError("normalizeGoogleCalendarEvent: payload must be an object");
  }
  const eventId = raw.id ?? "";
  const occurredAt = _toIso(raw.updated ?? new Date().toISOString());
  const start = raw.start?.dateTime ?? raw.start?.date ?? null;
  const end = raw.end?.dateTime ?? raw.end?.date ?? null;
  return validateIntegrationEvent({
    event_id: `gcal:${eventId}:${occurredAt}`,
    source: "google_calendar",
    kind: raw.status === "cancelled" ? "calendar.meeting_detected" : "calendar.meeting_detected",
    occurred_at: occurredAt,
    payload: {
      external_event_id: eventId,
      title: raw.summary ?? null,
      status: raw.status ?? "confirmed",
      start_at: start,
      end_at: end,
      organizer_email: raw.organizer?.email ?? null,
      attendees: (raw.attendees ?? []).map((a) => ({
        email: a.email ?? null,
        response_status: a.responseStatus ?? null,
      })),
      meeting_url: raw.hangoutLink ?? raw.htmlLink ?? null,
    },
  });
}

/**
 * @param {{ historyId?: string|number, messageId?: string, threadId?: string, from?: string, subject?: string, internalDate?: string|number, snippet?: string }} raw
 *   Compacted Gmail watch/push notification payload (the Pipedream workflow
 *   step is expected to have already fetched the message via users.messages.get).
 */
export function normalizeGmailMessage(raw) {
  if (!raw || typeof raw !== "object") {
    throw new TypeError("normalizeGmailMessage: payload must be an object");
  }
  const messageId = raw.messageId ?? "";
  const occurredAt = _toIso(raw.internalDate);
  return validateIntegrationEvent({
    event_id: `gmail:${messageId}`,
    source: "gmail",
    kind: "email.received",
    occurred_at: occurredAt,
    payload: {
      message_id: messageId,
      thread_id: raw.threadId ?? null,
      from: raw.from ?? null,
      subject: raw.subject ?? null,
      snippet: raw.snippet ?? null,
      history_id: raw.historyId !== undefined ? String(raw.historyId) : null,
    },
  });
}

/**
 * @param {{ callId?: string, started?: string, scheduled?: string, primaryUserId?: string, primaryUserEmail?: string, callUrl?: string, durationSeconds?: number, parties?: Array<{userId?: string, emailAddress?: string, name?: string}> }} raw
 *   Gong "Call Ended" webhook. See https://app.gong.io/help-center
 */
export function normalizeGongRecordingCompleted(raw) {
  if (!raw || typeof raw !== "object") {
    throw new TypeError("normalizeGongRecordingCompleted: payload must be an object");
  }
  const callId = raw.callId ?? "";
  const occurredAt = _toIso(raw.started ?? raw.scheduled ?? new Date().toISOString());
  return validateIntegrationEvent({
    event_id: `gong:${callId}`,
    source: "gong",
    kind: "recording.completed",
    occurred_at: occurredAt,
    payload: {
      call_id: callId,
      call_url: raw.callUrl ?? null,
      duration_seconds: typeof raw.durationSeconds === "number" ? raw.durationSeconds : null,
      primary_user_id: raw.primaryUserId ?? null,
      primary_user_email: raw.primaryUserEmail ?? null,
      parties: (raw.parties ?? []).map((p) => ({
        user_id: p.userId ?? null,
        email: p.emailAddress ?? null,
        name: p.name ?? null,
      })),
    },
  });
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function _toIso(value) {
  if (value === undefined || value === null || value === "") {
    return new Date().toISOString();
  }
  if (value instanceof Date) {
    return value.toISOString();
  }
  // HubSpot occurredAt + Gmail internalDate are epoch millis as
  // numbers/strings; calendar updated is already ISO-8601.
  if (typeof value === "number") {
    return new Date(value).toISOString();
  }
  if (typeof value === "string") {
    if (/^\d+$/.test(value)) {
      return new Date(Number(value)).toISOString();
    }
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) {
      throw new TypeError(`unparseable timestamp: ${value}`);
    }
    return parsed.toISOString();
  }
  throw new TypeError(`unsupported timestamp type: ${typeof value}`);
}
