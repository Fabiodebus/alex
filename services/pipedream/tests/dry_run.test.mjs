import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { dryRunCrmWrite } from "../src/lib/dry_run.mjs";

const TENANT = "11111111-1111-1111-1111-111111111111";
const REP = "22222222-2222-2222-2222-222222222222";

describe("dryRunCrmWrite", () => {
  it("returns valid + preview when every property is a supported scalar", () => {
    const result = dryRunCrmWrite({
      tenant_id: TENANT,
      rep_id: REP,
      action_type: "crm.write",
      target_system: "hubspot",
      target_id: "contact-1",
      payload: {
        properties: {
          lifecyclestage: "opportunity",
          deal_value: 1500,
          subscribed: true,
          notes: null,
        },
      },
    });
    assert.equal(result.valid, true);
    assert.equal(result.errors.length, 0);
    assert.deepEqual(result.preview, {
      lifecyclestage: "opportunity",
      deal_value: 1500,
      subscribed: true,
      notes: null,
    });
  });

  it("flags nested objects as unsupported", () => {
    const result = dryRunCrmWrite({
      tenant_id: TENANT,
      rep_id: REP,
      action_type: "crm.write",
      target_system: "hubspot",
      payload: { properties: { meta: { nested: 1 } } },
    });
    assert.equal(result.valid, false);
    assert.equal(result.errors[0].field, "payload.properties.meta");
  });

  it("flags missing properties block", () => {
    const result = dryRunCrmWrite({
      tenant_id: TENANT,
      rep_id: REP,
      action_type: "crm.write",
      target_system: "hubspot",
      payload: {},
    });
    assert.equal(result.valid, false);
    assert.equal(result.errors[0].field, "payload.properties");
  });
});
