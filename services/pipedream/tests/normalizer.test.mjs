import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  normalizeGmailMessage,
  normalizeGongRecordingCompleted,
  normalizeGoogleCalendarEvent,
  normalizeHubspotRecordUpdate,
} from "../src/lib/normalizer.mjs";

describe("normalizeHubspotRecordUpdate", () => {
  it("maps a contact-property-change webhook to IntegrationEvent", () => {
    const event = normalizeHubspotRecordUpdate({
      subscriptionType: "contact.propertyChange",
      objectId: 51,
      occurredAt: 1746954000000,
      propertyName: "lifecyclestage",
      propertyValue: "opportunity",
      portalId: 12345,
      sourceId: "userId:99",
    });
    assert.equal(event.source, "hubspot");
    assert.equal(event.kind, "crm.activity_logged");
    assert.equal(event.occurred_at, "2025-05-11T09:00:00.000Z");
    assert.equal(event.payload.subscription_type, "contact.propertyChange");
    assert.equal(event.payload.property_name, "lifecyclestage");
    assert.equal(event.payload.property_value, "opportunity");
    assert.equal(event.payload.portal_id, "12345");
    assert.match(event.event_id, /^hubspot:contact\.propertyChange:51:/);
  });

  it("normalises numeric ids to strings", () => {
    const event = normalizeHubspotRecordUpdate({
      subscriptionType: "deal.creation",
      objectId: 9876543210,
      occurredAt: "2026-05-11T08:00:00Z",
    });
    assert.equal(event.payload.object_id, "9876543210");
  });
});

describe("normalizeGoogleCalendarEvent", () => {
  it("captures attendees, organizer, start/end, and meeting URL", () => {
    const event = normalizeGoogleCalendarEvent({
      id: "abc123",
      summary: "Discovery call: Acme",
      status: "confirmed",
      start: { dateTime: "2026-05-12T09:00:00+02:00" },
      end: { dateTime: "2026-05-12T09:45:00+02:00" },
      organizer: { email: "alice@example.com" },
      attendees: [
        { email: "alice@example.com", responseStatus: "accepted" },
        { email: "bob@acme.com", responseStatus: "needsAction" },
      ],
      updated: "2026-05-11T08:00:00.000Z",
      hangoutLink: "https://meet.google.com/abc-defg-hij",
    });
    assert.equal(event.source, "google_calendar");
    assert.equal(event.kind, "calendar.meeting_detected");
    assert.equal(event.payload.title, "Discovery call: Acme");
    assert.equal(event.payload.start_at, "2026-05-12T09:00:00+02:00");
    assert.equal(event.payload.attendees.length, 2);
    assert.equal(event.payload.attendees[1].email, "bob@acme.com");
    assert.equal(event.payload.meeting_url, "https://meet.google.com/abc-defg-hij");
  });

  it("falls back to date for all-day events and uses htmlLink when no hangoutLink", () => {
    const event = normalizeGoogleCalendarEvent({
      id: "all-day",
      summary: "Quarterly board",
      status: "confirmed",
      start: { date: "2026-05-20" },
      end: { date: "2026-05-20" },
      organizer: { email: "x@y" },
      updated: "2026-05-11T08:00:00Z",
      htmlLink: "https://calendar.google.com/event?eid=xyz",
    });
    assert.equal(event.payload.start_at, "2026-05-20");
    assert.equal(event.payload.meeting_url, "https://calendar.google.com/event?eid=xyz");
  });
});

describe("normalizeGmailMessage", () => {
  it("maps a Gmail metadata payload to email.received", () => {
    const event = normalizeGmailMessage({
      historyId: 12345,
      messageId: "abc",
      threadId: "thread-abc",
      from: "prospect@example.com",
      subject: "Re: Pricing",
      internalDate: "1746954000000",
      snippet: "Thanks for the quote — one quick question...",
    });
    assert.equal(event.source, "gmail");
    assert.equal(event.kind, "email.received");
    assert.equal(event.event_id, "gmail:abc");
    assert.equal(event.payload.from, "prospect@example.com");
    assert.equal(event.payload.history_id, "12345");
  });
});

describe("normalizeGongRecordingCompleted", () => {
  it("captures parties, primary user, and URL", () => {
    const event = normalizeGongRecordingCompleted({
      callId: "gong-99",
      started: "2026-05-11T08:00:00Z",
      primaryUserId: "user-1",
      primaryUserEmail: "alice@example.com",
      callUrl: "https://app.gong.io/call?id=gong-99",
      durationSeconds: 1800,
      parties: [
        { userId: "user-1", emailAddress: "alice@example.com", name: "Alice" },
        { userId: null, emailAddress: "bob@acme.com", name: "Bob" },
      ],
    });
    assert.equal(event.source, "gong");
    assert.equal(event.kind, "recording.completed");
    assert.equal(event.event_id, "gong:gong-99");
    assert.equal(event.payload.duration_seconds, 1800);
    assert.equal(event.payload.parties.length, 2);
    assert.equal(event.payload.parties[1].email, "bob@acme.com");
  });
});
