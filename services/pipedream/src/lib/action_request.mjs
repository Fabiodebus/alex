// Canonical schemas for outbound action execution. The Agent Runtime
// dispatches these via signed HTTP POST; Pipedream workflows verify the
// signature and route to the right executor. Keep aligned with the
// Pydantic models in services/agent-runtime/src/alex_agent_runtime/schemas.py.

export const ACTION_TYPES = Object.freeze([
  "crm.write",
  "email.send",
  "doc.upload",
]);

export const DRY_RUN_TYPES = Object.freeze([
  "crm.write",
]);

export const CONNECTION_STATUSES = Object.freeze([
  "connected",
  "disconnected",
  "expired",
  "revoked",
  "error",
]);

export class ActionValidationError extends Error {
  constructor(message, field) {
    super(message);
    this.name = "ActionValidationError";
    this.field = field;
  }
}

/**
 * @typedef {Object} ActionRequest
 * @property {string} action_id           Deterministic id; idempotency key.
 * @property {string} tenant_id           UUID.
 * @property {string} rep_id              UUID — the approver.
 * @property {string} action_type         One of ACTION_TYPES.
 * @property {string} target_system       'hubspot' | 'gmail' | 'google_drive' | ...
 * @property {string} [target_id]         External record id when applicable.
 * @property {object} payload             Source-specific body.
 * @property {string} [approver_rep_id]   When different from rep_id (e.g. delegated approval).
 */
export function validateActionRequest(candidate) {
  if (!candidate || typeof candidate !== "object") {
    throw new ActionValidationError("must be an object", null);
  }
  const required = ["action_id", "tenant_id", "rep_id", "action_type", "target_system"];
  for (const field of required) {
    const value = candidate[field];
    if (typeof value !== "string" || value.length === 0) {
      throw new ActionValidationError(`${field} is required`, field);
    }
  }
  if (!ACTION_TYPES.includes(candidate.action_type)) {
    throw new ActionValidationError(
      `action_type must be one of ${ACTION_TYPES.join(", ")}`,
      "action_type",
    );
  }
  if (candidate.payload !== undefined && (candidate.payload === null || typeof candidate.payload !== "object")) {
    throw new ActionValidationError("payload must be an object", "payload");
  }
  return Object.freeze({
    action_id: candidate.action_id,
    tenant_id: candidate.tenant_id,
    rep_id: candidate.rep_id,
    approver_rep_id: candidate.approver_rep_id ?? candidate.rep_id,
    action_type: candidate.action_type,
    target_system: candidate.target_system,
    target_id: candidate.target_id ?? null,
    payload: Object.freeze({ ...(candidate.payload ?? {}) }),
  });
}

/**
 * @typedef {Object} DryRunRequest
 * @property {string} tenant_id
 * @property {string} rep_id
 * @property {string} action_type
 * @property {string} target_system
 * @property {string} [target_id]
 * @property {object} payload
 */
export function validateDryRunRequest(candidate) {
  if (!candidate || typeof candidate !== "object") {
    throw new ActionValidationError("must be an object", null);
  }
  const required = ["tenant_id", "rep_id", "action_type", "target_system"];
  for (const field of required) {
    if (typeof candidate[field] !== "string" || candidate[field].length === 0) {
      throw new ActionValidationError(`${field} is required`, field);
    }
  }
  if (!DRY_RUN_TYPES.includes(candidate.action_type)) {
    throw new ActionValidationError(
      `dry-run action_type must be one of ${DRY_RUN_TYPES.join(", ")}`,
      "action_type",
    );
  }
  return Object.freeze({
    tenant_id: candidate.tenant_id,
    rep_id: candidate.rep_id,
    action_type: candidate.action_type,
    target_system: candidate.target_system,
    target_id: candidate.target_id ?? null,
    payload: Object.freeze({ ...(candidate.payload ?? {}) }),
  });
}

/**
 * @typedef {Object} OAuthToken
 * @property {string} tenant_id
 * @property {string} rep_id
 * @property {string} source        e.g. 'google', 'hubspot'
 * @property {string} access_token
 * @property {string} [refresh_token]
 * @property {number} [expires_in]
 * @property {string[]} [scopes]
 */
export function validateOAuthToken(candidate) {
  if (!candidate || typeof candidate !== "object") {
    throw new ActionValidationError("must be an object", null);
  }
  const required = ["tenant_id", "rep_id", "source", "access_token"];
  for (const field of required) {
    if (typeof candidate[field] !== "string" || candidate[field].length === 0) {
      throw new ActionValidationError(`${field} is required`, field);
    }
  }
  return Object.freeze({
    tenant_id: candidate.tenant_id,
    rep_id: candidate.rep_id,
    source: candidate.source,
    access_token: candidate.access_token,
    refresh_token: candidate.refresh_token ?? null,
    expires_in: typeof candidate.expires_in === "number" ? candidate.expires_in : null,
    scopes: Array.isArray(candidate.scopes) ? [...candidate.scopes] : [],
  });
}
