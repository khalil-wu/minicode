"use strict";
const net = require("node:net");
const crypto = require("node:crypto");
const { hostContractFingerprint } = require("./backend-host-contract");

async function availablePort() {
  const server = net.createServer();
  await new Promise((resolve, reject) => { server.once("error", reject); server.listen(0, "127.0.0.1", resolve); });
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function externalPeerEnvironment() {
  const endpoint = `http://127.0.0.1:${await availablePort()}`;
  const token = crypto.randomBytes(32).toString("hex");
  return { env: { MINICODE_EMBEDDED_BROWSER_ENDPOINT: endpoint, MINICODE_EMBEDDED_BROWSER_TOKEN: token },
    fingerprint: hostContractFingerprint(endpoint, token) };
}

module.exports = { availablePort, externalPeerEnvironment };
