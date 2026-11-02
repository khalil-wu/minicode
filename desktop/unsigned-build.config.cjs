"use strict";

// Separate, non-publishing config for manual unsigned Windows test artifacts.
// The signed release path continues to use package.json's original build config.
const base = require("./package.json").build;

module.exports = {
  ...base,
  directories: {
    ...base.directories,
    output: "release-unsigned",
  },
  // No update server configuration or release publishing for unsigned builds.
  publish: [],
  extraResources: [
    ...base.extraResources,
    { from: "build/UNSIGNED_TEST_BUILD", to: "UNSIGNED_TEST_BUILD" },
  ],
  win: {
    ...base.win,
    // electron-builder v26: explicitly avoid signing the app and installer.
    signExecutable: false,
    signtoolOptions: null,
    verifyUpdateCodeSignature: false,
  },
};
