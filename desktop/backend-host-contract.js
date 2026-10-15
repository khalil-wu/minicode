"use strict";

const crypto = require("node:crypto");
const { isIP } = require("node:net");

const CONTRACT_HELP = "External backend mode requires the same host-private MINICODE_EMBEDDED_BROWSER_ENDPOINT and MINICODE_EMBEDDED_BROWSER_TOKEN in the desktop and backend launch environments. Use a fixed loopback HTTP endpoint and restart the peer with that configuration.";

function bridgeBindTarget(value) {
  let url;
  try {
    url = new URL(value);
  } catch (cause) {
    throw new Error(CONTRACT_HELP, { cause });
  }
  const host = url.hostname.replace(/^\[|\]$/g, "");
  const loopback = host === "localhost" || host === "::1" || (isIP(host) === 4 && host.startsWith("127."));
  if (url.protocol !== "http:" || !loopback || url.port === "0" || url.username || url.password || url.pathname !== "/" || url.search || url.hash) {
    throw new Error(CONTRACT_HELP);
  }
  return { host, port: Number(url.port || 80), endpoint: url.origin };
}

function hostContractFingerprint(endpoint, token) {
  return crypto.createHash("sha256").update(`${endpoint}\0${token}`).digest("hex");
}

function externalBackendHostContract(env = process.env) {
  const endpoint = String(env.MINICODE_EMBEDDED_BROWSER_ENDPOINT || "").trim();
  const token = String(env.MINICODE_EMBEDDED_BROWSER_TOKEN || "");
  if (!endpoint || !token.trim()) throw new Error(CONTRACT_HELP);
  const target = bridgeBindTarget(endpoint);
  return { ...target, token, fingerprint: hostContractFingerprint(target.endpoint, token) };
}

async function verifyExternalBackendHostContract(apiBaseUrl, runtimeToken, contract, request = fetch) {
  const response = await request(`${apiBaseUrl.replace(/\/+$/, "")}/readyz`, {
    headers: { "x-minicode-token": runtimeToken }, signal: AbortSignal.timeout(10_000),
  });
  if (!response.ok) throw new Error(`External backend readiness failed with HTTP ${response.status}. ${CONTRACT_HELP}`);
  const readiness = await response.json();
  if (readiness.ready !== true) throw new Error(`External backend does not report ready. ${CONTRACT_HELP}`);
  if (readiness.desktop_bridge_fingerprint !== contract.fingerprint) throw new Error(`External backend host binding does not match the desktop. ${CONTRACT_HELP}`);
}

module.exports = { bridgeBindTarget, externalBackendHostContract, hostContractFingerprint, verifyExternalBackendHostContract };
