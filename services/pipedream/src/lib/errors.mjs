// Structured error reporting. The blueprint requires "no silent failures"
// — any failed forwarding or normalization surfaces a serializable error
// payload that the Pipedream workflow can persist to its activity log and
// (optionally) surface to a monitoring channel.

export class IntegrationError extends Error {
  /**
   * @param {string} message
   * @param {{ code: string, source: string, tenant_id?: string, event_id?: string, cause?: unknown, retriable?: boolean }} opts
   */
  constructor(message, opts) {
    super(message);
    this.name = "IntegrationError";
    this.code = opts.code;
    this.source = opts.source;
    this.tenantId = opts.tenant_id ?? null;
    this.eventId = opts.event_id ?? null;
    this.retriable = Boolean(opts.retriable);
    if (opts.cause) {
      this.cause = opts.cause;
    }
  }

  toJSON() {
    return {
      error: true,
      code: this.code,
      source: this.source,
      tenant_id: this.tenantId,
      event_id: this.eventId,
      retriable: this.retriable,
      message: this.message,
      cause: this.cause instanceof Error ? this.cause.message : this.cause ?? null,
    };
  }
}

export function asSerializable(error) {
  if (error instanceof IntegrationError) {
    return error.toJSON();
  }
  return {
    error: true,
    code: "unknown",
    source: null,
    message: error instanceof Error ? error.message : String(error),
    retriable: false,
  };
}
