import assert from "node:assert/strict";
import { test } from "node:test";
import { RuntimeLifecycle } from "./lifecycle";

function setup(prepare: () => Promise<void> = async () => undefined) {
  const calls: string[] = [];
  const lifecycle = new RuntimeLifecycle({
    admission: (value) => { calls.push(`admission:${value}`); },
    prepare: async () => { calls.push("prepare"); await prepare(); },
    stop: async () => { calls.push("stop"); },
    resume: async () => { calls.push("renew-identity"); },
    changed: (value) => { calls.push(`ready:${value}`); },
    warn: () => { calls.push("warning"); },
  });
  return { lifecycle, calls };
}

test("sleep closes admission immediately; wake waits for persistence and only revalidates identity", async () => {
  let finish!: () => void;
  const gate = new Promise<void>((resolve) => { finish = resolve; });
  const { lifecycle, calls } = setup(() => gate);
  const sleep = lifecycle.suspend();
  assert.deepEqual(calls, ["admission:false", "ready:false"]);
  const wake = lifecycle.resume();
  await Promise.resolve();
  assert.ok(!calls.includes("renew-identity"));
  finish(); await Promise.all([sleep, wake]);
  assert.deepEqual(calls, ["admission:false", "ready:false", "prepare", "renew-identity", "admission:true", "ready:true"]);
});

test("an unconfirmed sleep request stops the sidecar before another start", async () => {
  const { lifecycle, calls } = setup(async () => { throw new Error("timeout"); });
  await lifecycle.suspend(); await lifecycle.resume();
  assert.ok(calls.indexOf("stop") < calls.indexOf("renew-identity"));
});

test("quit is idempotent and prevents later resume from reopening admission", async () => {
  const { lifecycle, calls } = setup();
  await Promise.all([lifecycle.shutdown(), lifecycle.shutdown(), lifecycle.resume()]);
  assert.equal(calls.filter((call) => call === "prepare").length, 1);
  assert.ok(!calls.includes("admission:true"));
  assert.ok(!calls.includes("renew-identity"));
});

test("rapid sleep/wake changes cannot reopen admission before the latest wake", async () => {
  const { lifecycle, calls } = setup();
  const firstSleep = lifecycle.suspend(); const firstWake = lifecycle.resume();
  const secondSleep = lifecycle.suspend(); const secondWake = lifecycle.resume();
  await Promise.all([firstSleep, firstWake, secondSleep, secondWake]);
  assert.equal(calls.filter((call) => call === "admission:true").length, 1);
  assert.equal(calls.filter((call) => call === "prepare").length, 2);
  assert.deepEqual(calls.slice(-2), ["admission:true", "ready:true"]);
});
