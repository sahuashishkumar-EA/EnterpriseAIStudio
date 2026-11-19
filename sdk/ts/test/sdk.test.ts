// Minimal self-checking test (node --test after tsc). No dependencies.
import { strict as assert } from "node:assert";
import { test } from "node:test";
import { AgentConfig, MemoryConfigCache } from "../src/index.js";

const body = {
  agent: { id: "customer-profile" }, profile: { persona: { name: "Sales profile assistant", tone: "friendly" } },
  bindings: { skills: [], tools: [], knowledge: { sources: [], filters: {} }, model: { tier: "standard" } },
  thresholds: { values: { maxExposureEur: 250000 }, approvers: [] },
  versions: { agent: "v_a", profile: "v_p" }, source: "registry",
};

test("load pins config and falls back to last-known-good", async () => {
  const cache = new MemoryConfigCache();
  let up = true;
  const fake = (async () => {
    if (!up) throw new Error("connection refused");
    return new Response(JSON.stringify(body), { status: 200 });
  }) as unknown as typeof fetch;
  const opts = { agentId: "customer-profile", businessUnit: "sales", environment: "production" as const,
                 resolverUrl: "http://resolver", fetch: fake, cache };
  const cfg = await AgentConfig.load(opts);
  assert.equal(cfg.profile.persona?.name, "Sales profile assistant");
  assert.equal(cfg.otelAttributes()["eap.version.profile"], "v_p");
  up = false;
  const again = await AgentConfig.load(opts);
  assert.equal(again.source, "sdk-last-known-good");
  assert.equal(again.thresholds.maxExposureEur, 250000);
});

test("forbidden is not an outage", async () => {
  const cache = new MemoryConfigCache();
  await cache.set("production/other/sales", body as never);
  const fake = (async () => new Response(JSON.stringify({ message: "may read only x" }), { status: 403 })) as unknown as typeof fetch;
  await assert.rejects(AgentConfig.load({ agentId: "other", businessUnit: "sales", environment: "production",
                                          resolverUrl: "http://r", fetch: fake, cache }));
});
