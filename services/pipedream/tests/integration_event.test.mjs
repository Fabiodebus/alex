import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  EVENT_KINDS,
  IntegrationEventValidationError,
  validateIntegrationEvent,
} from "../src/lib/integration_event.mjs";

describe("validateIntegrationEvent", () => {
  it("accepts a fully-populated payload", () => {
    const e = validateIntegrationEvent({
      event_id: "x",
      source: "hubspot",
      kind: "crm.activity_logged",
      occurred_at: "2026-05-11T08:00:00Z",
      payload: { foo: "bar" },
    });
    assert.equal(e.event_id, "x");
    assert.equal(e.payload.foo, "bar");
    assert.ok(Object.isFrozen(e));
    assert.ok(Object.isFrozen(e.payload));
  });

  it("defaults missing payload to an empty object", () => {
    const e = validateIntegrationEvent({
      event_id: "x",
      source: "gmail",
      kind: "email.received",
      occurred_at: "2026-05-11T08:00:00Z",
    });
    assert.deepEqual(e.payload, {});
  });

  it("rejects an unknown kind", () => {
    assert.throws(
      () =>
        validateIntegrationEvent({
          event_id: "x",
          source: "s",
          kind: "not_a_real_kind",
          occurred_at: "2026-05-11T08:00:00Z",
        }),
      IntegrationEventValidationError,
    );
  });

  it("rejects an empty required string", () => {
    assert.throws(
      () =>
        validateIntegrationEvent({
          event_id: "",
          source: "s",
          kind: "email.received",
          occurred_at: "2026-05-11T08:00:00Z",
        }),
      /event_id is required/,
    );
  });

  it("rejects non-object payload", () => {
    assert.throws(
      () =>
        validateIntegrationEvent({
          event_id: "x",
          source: "s",
          kind: "email.received",
          occurred_at: "2026-05-11T08:00:00Z",
          payload: "not-an-object",
        }),
      /payload must be an object/,
    );
  });

  it("exposes the canonical EVENT_KINDS list (frozen)", () => {
    assert.ok(EVENT_KINDS.includes("email.received"));
    assert.ok(Object.isFrozen(EVENT_KINDS));
  });
});
