// Dry-run validation for CRM write payloads. The scaffold returns a
// type-shape preview; feature WOs will extend this to query each CRM's
// schema endpoint (HubSpot Properties API, Salesforce Field Metadata,
// etc.) for full per-field validation.

import { ActionValidationError, validateDryRunRequest } from "./action_request.mjs";

const ALLOWED_SCALAR_TYPES = new Set(["string", "number", "boolean"]);

/**
 * @param {Object} request          Already-validated DryRunRequest.
 * @returns {{ valid: boolean, target_system: string, target_id: string|null, preview: object, errors: Array<{field: string, message: string}> }}
 */
export function dryRunCrmWrite(request) {
  const dr = validateDryRunRequest(request);
  if (dr.action_type !== "crm.write") {
    throw new ActionValidationError(
      `dryRunCrmWrite cannot handle action_type=${dr.action_type}`,
      "action_type",
    );
  }
  const properties = dr.payload?.properties;
  const errors = [];
  const preview = {};
  if (properties === undefined || properties === null || typeof properties !== "object") {
    errors.push({ field: "payload.properties", message: "must be an object" });
  } else {
    for (const [name, value] of Object.entries(properties)) {
      if (value === null) {
        preview[name] = null;
        continue;
      }
      const t = typeof value;
      if (!ALLOWED_SCALAR_TYPES.has(t)) {
        errors.push({
          field: `payload.properties.${name}`,
          message: `unsupported type '${t}'; expected string/number/boolean/null`,
        });
        continue;
      }
      preview[name] = value;
    }
  }
  return {
    valid: errors.length === 0,
    target_system: dr.target_system,
    target_id: dr.target_id,
    preview,
    errors,
  };
}
