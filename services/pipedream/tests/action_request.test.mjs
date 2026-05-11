import { describe, it } from "node:test";
import assert from "node:assert/strict";

import {
  ACTION_TYPES,
  ActionValidationError,
  DRY_RUN_TYPES,
  validateActionRequest,
  validateDryRunRequest,
  validateOAuthToken,
} from "../src/lib/action_request.mjs";

describe("validateActionRequest", () => {
  const base = {
    action_id: "act-1",
    tenant_id: "11111111-1111-1111-1111-111111111111",
    rep_id: "22222222-2222-2222-2222-222222222222",
    action_type: "crm.write",
    target_system: "hubspot",
    target_id: "contact-99",
    payload: { properties: { lifecyclestage: "opportunity" } },
  };

  it("accepts a well-formed request and freezes the result", () => {
    const r = validateActionRequest(base);
    assert.equal(r.action_id, "act-1");
    assert.equal(r.approver_rep_id, base.rep_id);
    assert.ok(Object.isFrozen(r));
    assert.ok(Object.isFrozen(r.payload));
  });

  it("rejects an unknown action_type", () => {
    assert.throws(
      () => validateActionRequest({ ...base, action_type: "magic" }),
      ActionValidationError,
    );
  });

  it("rejects empty required fields", () => {
    assert.throws(() => validateActionRequest({ ...base, tenant_id: "" }), /tenant_id is required/);
  });

  it("respects an explicit approver_rep_id different from rep_id", () => {
    const r = validateActionRequest({
      ...base,
      approver_rep_id: "33333333-3333-3333-3333-333333333333",
    });
    assert.equal(r.approver_rep_id, "33333333-3333-3333-3333-333333333333");
  });

  it("exposes ACTION_TYPES (frozen)", () => {
    assert.ok(ACTION_TYPES.includes("crm.write"));
    assert.ok(Object.isFrozen(ACTION_TYPES));
  });
});

describe("validateDryRunRequest", () => {
  it("rejects an action_type that's not dry-runnable", () => {
    assert.throws(
      () =>
        validateDryRunRequest({
          tenant_id: "11111111-1111-1111-1111-111111111111",
          rep_id: "22222222-2222-2222-2222-222222222222",
          action_type: "email.send",
          target_system: "gmail",
        }),
      /dry-run action_type/,
    );
  });

  it("exposes DRY_RUN_TYPES (frozen)", () => {
    assert.deepEqual([...DRY_RUN_TYPES], ["crm.write"]);
    assert.ok(Object.isFrozen(DRY_RUN_TYPES));
  });
});

describe("validateOAuthToken", () => {
  it("normalises optional fields and copies scopes", () => {
    const t = validateOAuthToken({
      tenant_id: "11111111-1111-1111-1111-111111111111",
      rep_id: "22222222-2222-2222-2222-222222222222",
      source: "google",
      access_token: "ya29.xxxx",
      scopes: ["gmail.send", "drive.file"],
    });
    assert.equal(t.refresh_token, null);
    assert.equal(t.expires_in, null);
    assert.deepEqual(t.scopes, ["gmail.send", "drive.file"]);
  });

  it("rejects missing access_token", () => {
    assert.throws(
      () =>
        validateOAuthToken({
          tenant_id: "11111111-1111-1111-1111-111111111111",
          rep_id: "22222222-2222-2222-2222-222222222222",
          source: "google",
        }),
      /access_token is required/,
    );
  });
});
