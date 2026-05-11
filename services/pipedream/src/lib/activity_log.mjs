// Structured activity logging. Pipedream's $logger surfaces JSON lines to
// the workflow inspector. The blueprint requires every external-system
// touch to be logged with timestamp, integration type, operation type,
// and rep identity for the audit trail; this helper enforces that shape.

/**
 * @param {{ logger?: Console, source: string, operation: string, tenant_id?: string, rep_id?: string, status: 'ok'|'error'|'deduplicated', event_id?: string, details?: object }} entry
 */
export function logActivity({ logger = console, source, operation, tenant_id, rep_id, status, event_id, details }) {
  const line = {
    ts: new Date().toISOString(),
    source,
    operation,
    status,
    tenant_id: tenant_id ?? null,
    rep_id: rep_id ?? null,
    event_id: event_id ?? null,
    details: details ?? null,
  };
  logger.log(JSON.stringify(line));
}
