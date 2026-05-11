import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { expectedSignature, verifySignature, SignatureError } from "../src/lib/verifier.mjs";
import { signRequest } from "../src/lib/forwarder.mjs";

const SECRET = "verifier-test-secret";
const TS = "2026-05-11T08:00:00.000Z";
const BODY = '{"hello":"world"}';

describe("expectedSignature", () => {
  it("matches the forwarder's signRequest output", () => {
    const forward = signRequest({ secret: SECRET, body: BODY, timestamp: TS });
    const verify = expectedSignature({ secret: SECRET, body: BODY, timestamp: TS });
    assert.equal(forward.signature, verify);
  });
});

describe("verifySignature", () => {
  it("accepts a freshly-signed payload", () => {
    const sig = expectedSignature({ secret: SECRET, body: BODY, timestamp: TS });
    verifySignature({
      secret: SECRET,
      body: BODY,
      signature: sig,
      timestamp: TS,
      now: new Date(TS),
    });
  });

  it("rejects when the timestamp is stale", () => {
    const sig = expectedSignature({ secret: SECRET, body: BODY, timestamp: TS });
    assert.throws(
      () =>
        verifySignature({
          secret: SECRET,
          body: BODY,
          signature: sig,
          timestamp: TS,
          now: new Date("2026-05-11T08:10:00.000Z"),
        }),
      (err) => err instanceof SignatureError && err.code === "stale_signature",
    );
  });

  it("rejects on signature mismatch", () => {
    assert.throws(
      () =>
        verifySignature({
          secret: SECRET,
          body: BODY,
          signature: "sha256=" + "0".repeat(64),
          timestamp: TS,
          now: new Date(TS),
        }),
      (err) => err instanceof SignatureError && err.code === "invalid_signature",
    );
  });

  it("rejects when headers are missing", () => {
    assert.throws(
      () =>
        verifySignature({
          secret: SECRET,
          body: BODY,
          signature: null,
          timestamp: TS,
        }),
      (err) => err instanceof SignatureError && err.code === "missing_signature",
    );
  });
});
