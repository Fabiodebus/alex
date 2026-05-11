// Signature verification for inbound requests from the Agent Runtime to
// Pipedream. Mirrors services/agent-runtime/src/alex_agent_runtime/webhook_signing.py.

import { createHmac, timingSafeEqual } from "node:crypto";

const SIGNATURE_PREFIX = "sha256=";

export class SignatureError extends Error {
  constructor(message, code) {
    super(message);
    this.name = "SignatureError";
    this.code = code;
  }
}

/**
 * @param {{ secret: string, body: string, timestamp: string }} params
 */
export function expectedSignature({ secret, body, timestamp }) {
  if (!secret) throw new SignatureError("secret must be non-empty", "missing_secret");
  const payload = `${timestamp}.${body}`;
  const digest = createHmac("sha256", secret).update(payload).digest("hex");
  return `${SIGNATURE_PREFIX}${digest}`;
}

/**
 * @param {{
 *   secret: string,
 *   body: string,
 *   signature: string|null,
 *   timestamp: string|null,
 *   maxAgeSeconds?: number,
 *   now?: Date,
 * }} params
 * @throws {SignatureError} on any failure
 */
export function verifySignature({ secret, body, signature, timestamp, maxAgeSeconds = 300, now }) {
  if (!signature || !timestamp) {
    throw new SignatureError(
      "X-Alex-Signature and X-Alex-Timestamp headers are both required",
      "missing_signature",
    );
  }
  const tsMs = Date.parse(timestamp);
  if (Number.isNaN(tsMs)) {
    throw new SignatureError(`X-Alex-Timestamp is not ISO-8601: ${timestamp}`, "invalid_signature");
  }
  const nowMs = (now ?? new Date()).getTime();
  if (Math.abs(nowMs - tsMs) > maxAgeSeconds * 1000) {
    throw new SignatureError(
      `X-Alex-Timestamp drift exceeds ${maxAgeSeconds}s (got ${timestamp})`,
      "stale_signature",
    );
  }
  const expected = expectedSignature({ secret, body, timestamp });
  if (expected.length !== signature.length) {
    throw new SignatureError("signature mismatch", "invalid_signature");
  }
  if (!timingSafeEqual(Buffer.from(expected), Buffer.from(signature))) {
    throw new SignatureError("signature mismatch", "invalid_signature");
  }
}
