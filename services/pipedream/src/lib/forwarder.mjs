// HMAC-SHA256 signing and HTTP POST to the Agent Runtime's /events endpoint.
//
// The signed string is `${timestamp}.${rawJsonBody}` so a leaked secret
// can't be replayed against a fresh request. The middleware on the runtime
// rejects signatures older than its configured tolerance.

import { createHmac, timingSafeEqual } from "node:crypto";

const SIGNATURE_VERSION = "sha256";

/**
 * @param {{ secret: string, body: string, timestamp?: string }} params
 * @returns {{ signature: string, timestamp: string, signed_payload: string }}
 */
export function signRequest({ secret, body, timestamp }) {
  if (typeof secret !== "string" || secret.length === 0) {
    throw new Error("signRequest: secret must be a non-empty string");
  }
  if (typeof body !== "string") {
    throw new Error("signRequest: body must be a string (already JSON-stringified)");
  }
  const ts = timestamp ?? new Date().toISOString();
  const signedPayload = `${ts}.${body}`;
  const digest = createHmac("sha256", secret).update(signedPayload).digest("hex");
  return {
    signature: `${SIGNATURE_VERSION}=${digest}`,
    timestamp: ts,
    signed_payload: signedPayload,
  };
}

/**
 * Constant-time signature comparison helper used by tests; the runtime has
 * its own Python implementation that must match this contract.
 */
export function verifySignature({ secret, body, timestamp, signature }) {
  const expected = signRequest({ secret, body, timestamp }).signature;
  if (expected.length !== signature.length) return false;
  return timingSafeEqual(Buffer.from(expected), Buffer.from(signature));
}

/**
 * Forward a validated IntegrationEvent to the Agent Runtime.
 *
 * @param {{
 *   baseUrl: string,
 *   secret: string,
 *   tenantId: string,
 *   event: import('./integration_event.mjs').IntegrationEvent,
 *   fetchImpl?: typeof fetch,
 * }} params
 * @returns {Promise<{status: number, deduplicated: boolean, body: any}>}
 */
export async function forwardEvent({ baseUrl, secret, tenantId, event, fetchImpl = fetch }) {
  if (typeof baseUrl !== "string" || baseUrl.length === 0) {
    throw new Error("forwardEvent: baseUrl is required");
  }
  if (typeof tenantId !== "string" || tenantId.length === 0) {
    throw new Error("forwardEvent: tenantId is required");
  }
  const body = JSON.stringify(event);
  const { signature, timestamp } = signRequest({ secret, body });
  const url = `${baseUrl.replace(/\/+$/, "")}/events`;
  const response = await fetchImpl(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Tenant-Id": tenantId,
      "X-Alex-Signature": signature,
      "X-Alex-Timestamp": timestamp,
    },
    body,
  });
  let parsed = null;
  try {
    parsed = await response.json();
  } catch {
    parsed = null;
  }
  return {
    status: response.status,
    deduplicated: Boolean(parsed && parsed.deduplicated),
    body: parsed,
  };
}
