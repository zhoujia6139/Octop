import assert from "node:assert/strict";
import { promises as fs } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { CloudSessionClient } from "./cloud-session";
import { DeviceCredentialStore } from "./device-credential";
import type { CloudIdentity, CloudToken } from "../../shared/ipc-contract";

const identity: CloudIdentity = {
  cloud_id: "cloud-one",
  user: {
    user_id: 1,
    username: "alice",
    role: "user",
    enabled: true,
    mfa_enabled: true,
  },
  device_id: "device-one",
  binding_generation: 1,
  timezone: "Asia/Shanghai",
};
const session: CloudToken = {
  access_token: "short-device-access",
  token_type: "bearer",
  expires_in: 300,
  identity,
};
const account: CloudToken = {
  ...session,
  access_token: "account-access",
  identity: { ...identity, device_id: null, binding_generation: null },
};

type Call = {
  url: string;
  method: string;
  body: Record<string, unknown> | null;
  authorization: string | null;
};
function response(status: number, data: unknown): Response {
  return new Response(status === 204 ? null : JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

async function fixture(
  options: {
    keychain?: boolean;
    mfa?: boolean;
    respond?: (call: Call) => Response | Promise<Response>;
  } = {},
) {
  const home = await fs.mkdtemp(join(tmpdir(), "octop-cloud-session-"));
  const store = new DeviceCredentialStore(home, {
    available: async () => options.keychain !== false,
    encrypt: async (value) => Buffer.from(value).toString("base64"),
    decrypt: async (value) => Buffer.from(value, "base64").toString(),
  });
  const calls: Call[] = [];
  let cloudUrl = "https://cloud.example";
  const fetchImpl = (async (
    input: string | URL | Request,
    init?: RequestInit,
  ) => {
    assert.equal(init?.redirect, "error");
    const call: Call = {
      url: String(input),
      method: init?.method ?? "GET",
      body: init?.body ? JSON.parse(String(init.body)) : null,
      authorization: new Headers(init?.headers).get("authorization"),
    };
    calls.push(call);
    if (call.url.endsWith("/api/cloud/config"))
      return response(200, { cloud_url: cloudUrl });
    if (options.respond) return options.respond(call);
    if (call.url.endsWith("/api/auth/login"))
      return response(
        200,
        options.mfa
          ? { mfa_token: "challenge-only", session: null }
          : { session: account, mfa_token: null },
      );
    if (call.url.endsWith("/api/auth/mfa/verify"))
      return response(200, account);
    if (call.url.endsWith("/api/auth/devices"))
      return response(201, {
        session,
        refresh_token: "long-renewable-device-secret",
      });
    if (call.url.endsWith("/api/auth/device/refresh"))
      return response(200, session);
    if (call.url.endsWith("/api/auth/logout")) return response(204, null);
    if (call.url.endsWith("/api/cloud/session"))
      return response(call.method === "DELETE" ? 204 : 200, identity);
    throw new Error("unexpected request");
  }) as typeof fetch;
  const create = () =>
    new CloudSessionClient({
      store,
      localBaseUrl: () => "http://127.0.0.1:8089",
      localToken: () => "separate-local-secret",
      label: "test-desktop",
      fetchImpl,
    });
  const client = create();
  return {
    home,
    store,
    calls,
    client,
    create,
    setOrigin: (value: string) => {
      cloudUrl = value;
    },
    cleanup: async () => {
      client.stop();
      await fs.rm(home, { recursive: true, force: true });
    },
  };
}

test("MFA login binds and only sends short authorization to sidecar", async () => {
  const fx = await fixture({ mfa: true });
  try {
    assert.deepEqual(await fx.client.login("alice", "password"), {
      status: "mfa_required",
    });
    assert.deepEqual(await fx.client.verifyMfa("123456"), {
      status: "ready_to_bind",
    });
    const result = await fx.client.bind();
    assert.deepEqual(result, { status: "bound", identity });
    assert.ok(!JSON.stringify(result).includes("long-renewable"));
    const disk = await fs.readFile(fx.store.path, "utf8");
    assert.ok(!disk.includes("long-renewable-device-secret"));
    const installed = fx.calls.find((call) =>
      call.url.endsWith("/api/cloud/session"),
    );
    assert.equal(installed?.authorization, "Bearer separate-local-secret");
    assert.deepEqual(installed?.body, session);
    for (const call of fx.calls.filter((call) =>
      call.url.startsWith("http://127.0.0.1"),
    )) {
      assert.ok(!JSON.stringify(call).includes("long-renewable-device-secret"));
    }
    for (const call of fx.calls.filter((call) =>
      call.url.startsWith("https://"),
    )) {
      assert.notEqual(call.authorization, "Bearer separate-local-secret");
    }
  } finally {
    await fx.cleanup();
  }
});

test("restart renews encrypted credentials and logout revokes then clears them", async () => {
  const fx = await fixture();
  const restarted = fx.create();
  try {
    await fx.client.login("alice", "password");
    await fx.client.bind();
    fx.client.stop();
    assert.deepEqual(await restarted.renew(), identity);
    assert.deepEqual(await restarted.logout(), { revoked: true });
    assert.equal(await fx.store.read(), null);
    assert.equal(restarted.current(), null);
    assert.equal(await restarted.renew(), null);
  } finally {
    restarted.stop();
    await fx.cleanup();
  }
});

test("a revoked credential clears local authorization and stored renewal", async () => {
  const fx = await fixture({
    respond: (call) => {
      if (call.url.endsWith("/api/auth/device/refresh"))
        return response(403, { error: { code: "DEVICE_REVOKED" } });
      return response(204, null);
    },
  });
  try {
    await fx.store.write(
      JSON.stringify({
        cloudUrl: "https://cloud.example",
        refreshToken: "revoked-refresh-secret",
      }),
    );
    await assert.rejects(fx.client.renew(), /DEVICE_REVOKED/);
    assert.equal(await fx.store.read(), null);
    assert.ok(
      fx.calls.some(
        (call) =>
          call.url.endsWith("/api/cloud/session") && call.method === "DELETE",
      ),
    );
  } finally {
    await fx.cleanup();
  }
});

test("cloud origin change never receives the previous server's credential", async () => {
  const fx = await fixture();
  try {
    await fx.store.write(
      JSON.stringify({
        cloudUrl: "https://cloud.example",
        refreshToken: "bound-to-original-cloud",
      }),
    );
    fx.setOrigin("https://other.example");
    await assert.rejects(fx.client.renew(), /AUTH_FAILED/);
    assert.ok(
      !fx.calls.some((call) => call.url.startsWith("https://other.example")),
    );
    fx.setOrigin("http://public.example");
    await assert.rejects(fx.client.renew(), /AUTH_FAILED/);
  } finally {
    await fx.cleanup();
  }
});

test("without OS keychain credentials are memory-only", async () => {
  const fx = await fixture({ keychain: false });
  try {
    await fx.client.login("alice", "password");
    await fx.client.bind();
    await assert.rejects(fs.access(fx.store.path));
    assert.ok(await fx.store.read());
    assert.deepEqual(await fx.client.renew(), identity);
  } finally {
    await fx.cleanup();
  }
});

test("offline logout detaches locally, keeps data and reports unconfirmed revocation", async () => {
  const fx = await fixture({
    respond: (call) => {
      if (call.url.endsWith("/api/cloud/session")) return response(204, null);
      throw new Error("offline");
    },
  });
  try {
    await fx.store.write(
      JSON.stringify({
        cloudUrl: "https://cloud.example",
        refreshToken: "remembered-refresh-secret",
      }),
    );
    await fs.writeFile(join(fx.home, "pending-data"), "pending");
    assert.deepEqual(await fx.client.logout(), { revoked: false });
    assert.equal(await fx.store.read(), null);
    assert.equal(
      await fs.readFile(join(fx.home, "pending-data"), "utf8"),
      "pending",
    );
  } finally {
    await fx.cleanup();
  }
});

test("queued renewal cannot reattach after logout", async () => {
  const fx = await fixture();
  try {
    await fx.client.login("alice", "password");
    await fx.client.bind();
    const renewing = fx.client.renew();
    const loggingOut = fx.client.logout();
    await Promise.all([renewing, loggingOut]);
    assert.equal(fx.client.current(), null);
    assert.equal(await fx.store.read(), null);
    assert.equal(
      fx.calls.filter((call) => call.url.endsWith("/api/cloud/session")).at(-1)
        ?.method,
      "DELETE",
    );
  } finally {
    await fx.cleanup();
  }
});

test("failed sidecar detachment still clears the remembered login", async () => {
  const fx = await fixture({
    respond: () => {
      throw new Error("sidecar unavailable");
    },
  });
  try {
    await fx.store.write(
      JSON.stringify({
        cloudUrl: "https://cloud.example",
        refreshToken: "remembered-refresh-secret",
      }),
    );
    await assert.rejects(fx.client.logout(), /CLOUD_UNAVAILABLE/);
    assert.equal(await fx.store.read(), null);
  } finally {
    await fx.cleanup();
  }
});

test("prepare preserves the renewable credential but closes execution before wake validation", async () => {
  const fx = await fixture({ respond: (call) => {
    if (call.url.endsWith("/api/lifecycle/prepare")) return response(204, null);
    if (call.url.endsWith("/api/auth/login")) return response(200, { session: account, mfa_token: null });
    if (call.url.endsWith("/api/auth/devices")) return response(201, { session, refresh_token: "kept-until-logout" });
    if (call.url.endsWith("/api/auth/device/refresh")) return response(200, session);
    if (call.url.endsWith("/api/cloud/session")) return response(200, identity);
    if (call.url.endsWith("/api/auth/logout")) return response(204, null);
    throw new Error("unexpected");
  } });
  try {
    await fx.client.login("alice", "password"); await fx.client.bind();
    const remembered = await fx.store.read();
    await fx.client.prepare();
    assert.equal(fx.client.current(), null);
    assert.equal(await fx.store.read(), remembered);
    await assert.rejects(fx.client.renew(), /LOCAL_RUNTIME_PAUSED/);
    assert.deepEqual(await fx.client.resume(), identity);
    const persisted = fx.calls.findIndex((call) => call.url.endsWith("/api/lifecycle/prepare"));
    const refresh = fx.calls.findIndex((call) => call.url.endsWith("/api/auth/device/refresh"));
    assert.ok(refresh > persisted);
    assert.ok(!fx.calls.some((call) => /threads\/.*\/ws/.test(call.url)));
  } finally { await fx.cleanup(); }
});
