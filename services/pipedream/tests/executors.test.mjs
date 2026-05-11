import { describe, it } from "node:test";
import assert from "node:assert/strict";

import { gmailSend, googleDriveUpload, hubspotWrite } from "../src/lib/executors.mjs";

function recorder(response) {
  let captured;
  const fetchImpl = async (url, init) => {
    captured = { url, init };
    return {
      status: response.status,
      json: async () => response.body,
    };
  };
  return { fetchImpl, get captured() { return captured; } };
}

describe("hubspotWrite", () => {
  it("PATCHes the correct objects URL with bearer + properties body", async () => {
    const rec = recorder({ status: 200, body: { id: "1", properties: { ls: "x" } } });
    const result = await hubspotWrite({
      accessToken: "tok",
      targetId: "deal-99",
      objectType: "deals",
      properties: { stage: "negotiation" },
      fetchImpl: rec.fetchImpl,
    });
    assert.equal(rec.captured.url, "https://api.hubapi.com/crm/v3/objects/deals/deal-99");
    assert.equal(rec.captured.init.method, "PATCH");
    assert.equal(rec.captured.init.headers.Authorization, "Bearer tok");
    assert.deepEqual(JSON.parse(rec.captured.init.body), { properties: { stage: "negotiation" } });
    assert.equal(result.ok, true);
    assert.equal(result.status, 200);
  });

  it("marks 5xx responses retriable", async () => {
    const rec = recorder({ status: 503, body: { error: "unavailable" } });
    const result = await hubspotWrite({
      accessToken: "tok",
      targetId: "x",
      properties: { a: "b" },
      fetchImpl: rec.fetchImpl,
    });
    assert.equal(result.ok, false);
    assert.equal(result.retriable, true);
  });
});

describe("gmailSend", () => {
  it("base64url-encodes the RFC-822 message and POSTs to Gmail", async () => {
    const rec = recorder({ status: 200, body: { id: "msg-1" } });
    await gmailSend({
      accessToken: "tok",
      senderEmail: "alice@predict-ability.com",
      to: ["bob@acme.com", "carol@acme.com"],
      subject: "Following up",
      bodyText: "Hello,\r\nthanks for the call.",
      fetchImpl: rec.fetchImpl,
    });
    assert.equal(rec.captured.url, "https://gmail.googleapis.com/gmail/v1/users/me/messages/send");
    const body = JSON.parse(rec.captured.init.body);
    assert.ok(typeof body.raw === "string");
    const decoded = Buffer.from(body.raw.replace(/-/g, "+").replace(/_/g, "/"), "base64").toString("utf-8");
    assert.match(decoded, /^From: alice@predict-ability\.com\r\n/);
    assert.match(decoded, /To: bob@acme\.com, carol@acme\.com\r\n/);
    assert.match(decoded, /Subject: Following up\r\n/);
  });
});

describe("googleDriveUpload", () => {
  it("posts a multipart body with metadata + base64 content", async () => {
    const rec = recorder({ status: 200, body: { id: "file-1" } });
    await googleDriveUpload({
      accessToken: "tok",
      name: "brief.md",
      mimeType: "text/markdown",
      content: Buffer.from("hello").toString("base64"),
      parents: ["folder-1"],
      fetchImpl: rec.fetchImpl,
    });
    assert.match(rec.captured.url, /^https:\/\/www\.googleapis\.com\/upload\/drive\/v3\/files\?uploadType=multipart$/);
    assert.match(rec.captured.init.headers["Content-Type"], /^multipart\/related; boundary=alex_boundary_/);
    assert.match(rec.captured.init.body, /"name":"brief\.md"/);
    assert.match(rec.captured.init.body, /"parents":\["folder-1"\]/);
  });
});
