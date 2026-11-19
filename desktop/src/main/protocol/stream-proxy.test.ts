import assert from "node:assert/strict";
import { test } from "node:test";
import { isCloudControlPath } from "./stream-proxy";

test("cloud credential controls cannot be reached through encoded proxy paths", () => {
  for (const path of [
    "/api/cloud/session",
    "/api/%63loud/session",
    "/api/cloud%2fsession",
    "/api/%63loud%2Fconfig",
    "/api/cloud",
    "/api/%",
  ]) {
    assert.equal(isCloudControlPath(path), true, path);
  }
  assert.equal(isCloudControlPath("/api/agents"), false);
});
