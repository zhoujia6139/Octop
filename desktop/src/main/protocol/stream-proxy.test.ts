import assert from "node:assert/strict";
import { test } from "node:test";
import { isCloudControlPath } from "./stream-proxy";

test("renderer API forwarding cannot reach credential or lifecycle control, even with encoded paths", () => {
  for (const path of ["/api/cloud/config", "/api/cloud/session", "/api/cloud%2fsession", "/api/%63loud%2Fconfig", "/api/cloud", "/api/%", "/api/lifecycle/prepare", "/api/%63loud/session", "/api/%6cifecycle/prepare", "/api/%invalid"]) assert.equal(isCloudControlPath(path), true, path);
  for (const path of ["/api/resources", "/api/agents", "/api/sync/retry", "/api/usage/budgets"]) assert.equal(isCloudControlPath(path), false, path);
});
