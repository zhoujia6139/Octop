import assert from "node:assert/strict";
import { join } from "node:path";
import { test } from "node:test";

import { buildSidecarEnv } from "./process-tree";

function env(overrides: NodeJS.ProcessEnv = {}): NodeJS.ProcessEnv {
  return buildSidecarEnv(
    { PATH: "/usr/bin", OCTOP_PORT: "1", ...overrides },
    { octopHome: "/tmp/home", portableDir: "/tmp/portable", port: 51234, bindHost: "127.0.0.1" },
  );
}

test("buildSidecarEnv leaves the access token TTL to the server", () => {
  assert.equal(env().OCTOP_ACCESS_TOKEN_TTL, undefined);
});

test("buildSidecarEnv keeps the identity and loopback pins", () => {
  const built = env();
  assert.equal(built.OCTOP_DESKTOP_SHELL, "1");
  assert.equal(built.OCTOP_HOME, "/tmp/home");
  // Built with node:path, so the separator follows the host.
  assert.equal(built.OCTOP_GREEN_PACKAGES, join("/tmp/portable", "packages"));
  assert.equal(built.OCTOP_BIND_HOST, "127.0.0.1");
  assert.equal(built.OCTOP_PORT, "51234");
  assert.equal(built.PYTHONNOUSERSITE, "1");
  assert.equal(built.PYTHONPYCACHEPREFIX, join("/tmp/home", "pyc"));
  assert.equal(built.PATH, "/usr/bin");
});

test("buildSidecarEnv drops an inherited PYTHONPATH", () => {
  assert.equal(env({ PYTHONPATH: "/opt/site-packages" }).PYTHONPATH, undefined);
});
