"use strict";

// electron-builder configuration for release builds. It extends package.json's
// build section and switches between a signed and an unsigned installer from
// the environment the release workflow resolves:
//
//   MINICODE_CODE_SIGNING=1  a certificate is configured; sign the installer and
//                            keep electron-updater's Authenticode verification.
//   otherwise                build the same installer unsigned. Windows
//                            SmartScreen may warn before it runs, and installed
//                            copies verify updates by the HTTPS feed plus the
//                            sha512 in latest.yml: verifyUpdateCodeSignature is
//                            off so app-update.yml carries no publisherName,
//                            and an UNSIGNED_BUILD marker tells the desktop
//                            updater which policy it runs under.
const base = require("./package.json").build;

const codeSigned = process.env.MINICODE_CODE_SIGNING === "1";

module.exports = codeSigned ? base : {
  ...base,
  extraResources: [
    ...base.extraResources,
    { from: "build/UNSIGNED_BUILD", to: "UNSIGNED_BUILD" },
  ],
  win: {
    ...base.win,
    signExecutable: false,
    verifyUpdateCodeSignature: false,
  },
};
