import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { forwardEvent, signRequest, verifySignature } from "../src/lib/forwarder.mjs";

const SECRET = "test-secret-do-not-use-in-prod";
const TIMESTAMP = "2026-05-11T08:00:00.000Z";
const BODY = '{"event_id":"x","source":"hubspot","kind":"crm.activity_logged","occurred_at":"2026-05-11T08:00:00Z","payload":{}}';

describe("signRequest", () => {
  it("is deterministic for the same secret + timestamp + body", () => {
    const a = signRequest({ secret: SECRET, body: BODY, timestamp: TIMESTAMP });
    const b = signRequest({ secret: SECRET, body: BODY, timestamp: TIMESTAMP });
    assert.equal(a.signature, b.signature);
    assert.match(a.signature, /^sha256=[0-9a-f]{64}$/);
  });

  it("changes when the body changes", () => {
    const a = signRequest({ secret: SECRET, body: BODY, timestamp: TIMESTAMP });
    const b = signRequest({ secret: SECRET, body: BODY + " ", timestamp: TIMESTAMP });
    assert.notEqual(a.signature, b.signature);
  });

  it("changes when the timestamp changes (replay resistance)", () => {
    const a = signRequest({ secret: SECRET, body: BODY, timestamp: TIMESTAMP });
    const b = signRequest({ secret: SECRET, body: BODY, timestamp: "2026-05-11T08:00:01.000Z" });
    assert.notEqual(a.signature, b.signature);
  });

  it("rejects missing secret", () => {
    assert.throws(() => signRequest({ secret: "", body: BODY }), /non-empty/);
  });
});

describe("verifySignature", () => {
  it("accepts a signature produced by signRequest", () => {
    const { signature } = signRequest({ secret: SECRET, body: BODY, timestamp: TIMESTAMP });
    assert.ok(verifySignature({ secret: SECRET, body: BODY, timestamp: TIMESTAMP, signature }));
  });

  it("rejects a wrong-secret signature", () => {
    const { signature } = signRequest({ secret: SECRET, body: BODY, timestamp: TIMESTAMP });
    assert.equal(
      verifySignature({ secret: "other", body: BODY, timestamp: TIMESTAMP, signature }),
      false,
    );
  });
});

describe("forwardEvent", () => {
  it("sends a signed POST with tenant + timestamp headers", async () => {
    let captured;
    const fakeFetch = async (url, init) => {
      captured = { url, init };
      return {
        status: 202,
        json: async () => ({ accepted: true, deduplicated: false }),
      };
    };

    const event = {
      event_id: "x",
      source: "hubspot",
      kind: "crm.activity_logged",
      occurred_at: TIMESTAMP,
      payload: {},
    };

    const result = await forwardEvent({
      baseUrl: "https://agent.local",
      secret: SECRET,
      tenantId: "11111111-1111-1111-1111-111111111111",
      event,
      fetchImpl: fakeFetch,
    });

    assert.equal(captured.url, "https://agent.local/events");
    assert.equal(captured.init.method, "POST");
    assert.equal(captured.init.headers["X-Tenant-Id"], "11111111-1111-1111-1111-111111111111");
    assert.match(captured.init.headers["X-Alex-Signature"], /^sha256=[0-9a-f]{64}$/);
    assert.ok(captured.init.headers["X-Alex-Timestamp"]);
    // Body must be valid JSON of the event.
    assert.deepEqual(JSON.parse(captured.init.body), event);
    assert.equal(result.status, 202);
    assert.equal(result.deduplicated, false);
  });

  it("surfaces deduplicated=true when the runtime reports it", async () => {
    const fakeFetch = async () => ({
      status: 200,
      json: async () => ({ accepted: true, deduplicated: true }),
    });
    const result = await forwardEvent({
      baseUrl: "https://agent.local",
      secret: SECRET,
      tenantId: "11111111-1111-1111-1111-111111111111",
      event: {
        event_id: "x",
        source: "hubspot",
        kind: "crm.activity_logged",
        occurred_at: TIMESTAMP,
      },
      fetchImpl: fakeFetch,
    });
    assert.equal(result.deduplicated, true);
  });
});
