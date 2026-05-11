// Canonical IntegrationEvent contract between Pipedream workflows and the
// Agent Runtime. Keep this aligned with the Pydantic schema in
// services/agent-runtime/src/alex_agent_runtime/schemas.py — both ends
// must agree on field names. Tests in tests/integration_event.test.mjs
// exercise the validator against representative payloads.

/**
 * @typedef {Object} IntegrationEvent
 * @property {string} event_id        Unique id from the source system (or composed deterministically).
 * @property {string} source          Origin system identifier, e.g. 'hubspot', 'google_calendar', 'gmail', 'gong'.
 * @property {string} kind            One of the EventKind values shared with the runtime.
 * @property {string} occurred_at     ISO-8601 UTC timestamp of when the event happened upstream.
 * @property {object} payload         Normalized, source-agnostic event body.
 */

export const EVENT_KINDS = Object.freeze([
  "calendar.meeting_detected",
  "recording.completed",
  "crm.activity_logged",
  "deal.inactivity_detected",
  "email.received",
  "unknown",
]);

export class IntegrationEventValidationError extends Error {
  constructor(message, field) {
    super(message);
    this.name = "IntegrationEventValidationError";
    this.field = field;
  }
}

/**
 * Validate an IntegrationEvent object and return a frozen copy. Throws
 * IntegrationEventValidationError on any required field missing or
 * wrong-typed. Designed to run cheaply inside a Pipedream workflow.
 *
 * @param {Partial<IntegrationEvent>} candidate
 * @returns {Readonly<IntegrationEvent>}
 */
export function validateIntegrationEvent(candidate) {
  if (candidate === null || typeof candidate !== "object") {
    throw new IntegrationEventValidationError("must be an object", null);
  }
  const requiredStrings = ["event_id", "source", "kind", "occurred_at"];
  for (const field of requiredStrings) {
    const value = candidate[field];
    if (typeof value !== "string" || value.length === 0) {
      throw new IntegrationEventValidationError(`${field} is required`, field);
    }
  }
  if (!EVENT_KINDS.includes(candidate.kind)) {
    throw new IntegrationEventValidationError(
      `kind must be one of ${EVENT_KINDS.join(", ")}`,
      "kind",
    );
  }
  if (
    candidate.payload !== undefined &&
    (candidate.payload === null || typeof candidate.payload !== "object")
  ) {
    throw new IntegrationEventValidationError("payload must be an object", "payload");
  }
  return Object.freeze({
    event_id: candidate.event_id,
    source: candidate.source,
    kind: candidate.kind,
    occurred_at: candidate.occurred_at,
    payload: Object.freeze({ ...(candidate.payload ?? {}) }),
  });
}
