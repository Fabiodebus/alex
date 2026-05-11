// Reference executor implementations.
//
// In production each workflow uses Pipedream's pre-built app authentication
// (e.g. `this.hubspot.$auth.oauth_access_token`) and HTTP wrappers. The
// executors below take a token + payload and POST against the canonical
// external endpoints, so they're unit-testable against a mock fetch and
// deployable on Pipedream with minimal changes.

import { ActionValidationError } from "./action_request.mjs";

/**
 * Update a CRM record via the HubSpot v3 Objects API.
 *
 * @param {{
 *   accessToken: string,
 *   targetId: string,
 *   objectType?: string,           // 'contacts', 'companies', 'deals' (default 'contacts')
 *   properties: Record<string, string|number|boolean|null>,
 *   fetchImpl?: typeof fetch,
 * }} args
 */
export async function hubspotWrite({ accessToken, targetId, objectType = "contacts", properties, fetchImpl = fetch }) {
  if (!accessToken) throw new ActionValidationError("accessToken required", "access_token");
  if (!targetId) throw new ActionValidationError("targetId required", "target_id");
  if (!properties || typeof properties !== "object") {
    throw new ActionValidationError("properties must be an object", "payload.properties");
  }
  const url = `https://api.hubapi.com/crm/v3/objects/${objectType}/${encodeURIComponent(targetId)}`;
  const response = await fetchImpl(url, {
    method: "PATCH",
    headers: {
      "Authorization": `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ properties }),
  });
  return _shape(response, "hubspot");
}

/**
 * Send an email via Gmail's users.messages.send.
 *
 * @param {{
 *   accessToken: string,
 *   senderEmail: string,
 *   to: string|string[],
 *   subject: string,
 *   bodyText: string,
 *   bodyHtml?: string,
 *   fetchImpl?: typeof fetch,
 * }} args
 */
export async function gmailSend({ accessToken, senderEmail, to, subject, bodyText, bodyHtml, fetchImpl = fetch }) {
  if (!accessToken) throw new ActionValidationError("accessToken required", "access_token");
  if (!senderEmail) throw new ActionValidationError("senderEmail required", "payload.senderEmail");
  if (!to) throw new ActionValidationError("to required", "payload.to");
  if (typeof subject !== "string") throw new ActionValidationError("subject must be a string", "payload.subject");

  const toList = Array.isArray(to) ? to.join(", ") : to;
  const headers = [
    `From: ${senderEmail}`,
    `To: ${toList}`,
    `Subject: ${subject}`,
    bodyHtml ? "Content-Type: text/html; charset=UTF-8" : "Content-Type: text/plain; charset=UTF-8",
    "",
    bodyHtml ?? bodyText ?? "",
  ];
  const raw = Buffer.from(headers.join("\r\n"), "utf-8")
    .toString("base64")
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
  const response = await fetchImpl("https://gmail.googleapis.com/gmail/v1/users/me/messages/send", {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${accessToken}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ raw }),
  });
  return _shape(response, "gmail");
}

/**
 * Upload a file to Google Drive using the multipart upload endpoint.
 *
 * @param {{
 *   accessToken: string,
 *   name: string,
 *   mimeType: string,
 *   content: string,              // base64-encoded body
 *   parents?: string[],
 *   fetchImpl?: typeof fetch,
 * }} args
 */
export async function googleDriveUpload({ accessToken, name, mimeType, content, parents, fetchImpl = fetch }) {
  if (!accessToken) throw new ActionValidationError("accessToken required", "access_token");
  if (!name) throw new ActionValidationError("name required", "payload.name");
  if (!mimeType) throw new ActionValidationError("mimeType required", "payload.mimeType");
  if (typeof content !== "string") {
    throw new ActionValidationError("content must be a base64 string", "payload.content");
  }
  const boundary = `alex_boundary_${Date.now()}`;
  const metadata = { name, mimeType, ...(parents ? { parents } : {}) };
  const body =
    `--${boundary}\r\n` +
    `Content-Type: application/json; charset=UTF-8\r\n\r\n` +
    `${JSON.stringify(metadata)}\r\n` +
    `--${boundary}\r\n` +
    `Content-Type: ${mimeType}\r\n` +
    `Content-Transfer-Encoding: base64\r\n\r\n` +
    `${content}\r\n` +
    `--${boundary}--`;

  const response = await fetchImpl(
    "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
    {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${accessToken}`,
        "Content-Type": `multipart/related; boundary=${boundary}`,
      },
      body,
    },
  );
  return _shape(response, "google_drive");
}

async function _shape(response, source) {
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  const ok = response.status >= 200 && response.status < 300;
  return {
    ok,
    source,
    status: response.status,
    body,
    retriable: response.status >= 500 || response.status === 429,
  };
}
