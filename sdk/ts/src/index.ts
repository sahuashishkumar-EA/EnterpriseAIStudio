/**
 * C09 Agent config SDK (TypeScript) for pro-code agents.
 *
 *   const cfg = await AgentConfig.load({ agentId, businessUnit, environment }); // fetch + pin for session
 *   cfg.profile.persona; cfg.thresholds; cfg.versions;
 *   span.setAttributes(cfg.otelAttributes());
 *
 * Same names and behaviour as the Python package `eap_agent_config` (spec 7.4).
 * No runtime dependencies: uses the global fetch (Node 18+, browsers, edge runtimes).
 */
export * from "./generated.js";

export interface ResolvedProfile {
  persona?: { name: string; tone: string; description?: string };
  instructions?: string;
  examples?: Array<{ input: string; output: string }>;
  outputFormat?: { format: "text" | "markdown" | "json"; maxLength?: number; schema?: Record<string, unknown> };
  escalationContacts?: string[];
  scope?: { businessUnit: string; [k: string]: unknown };
}

export interface ResolvedConfig {
  agent: Record<string, unknown>;
  profile: ResolvedProfile;
  bindings: {
    skills: Array<{ name: string; description: string; instructions: string; toolsUsed: string[] }>;
    tools: Array<{ capability: string; toolDefId: string; mcpServerId: string; toolName: string; endpoint?: string }>;
    knowledge: { sources: string[]; filters: Record<string, unknown> };
    model: { tier?: string; fallbackTier?: string; usageLimits?: Record<string, number> };
  };
  thresholds: { values: Record<string, number>; approvers: string[]; escalationQueue?: string };
  versions: Record<string, string>;
  resolvedAt?: string;
  source: "cache" | "registry" | "last-known-good";
}

/** Where the last-known-good copy lives (C09-F4). Memory by default; plug in a file or KV store. */
export interface ConfigCache {
  get(key: string): Promise<ResolvedConfig | undefined>;
  set(key: string, value: ResolvedConfig): Promise<void>;
}

export class MemoryConfigCache implements ConfigCache {
  private readonly m = new Map<string, ResolvedConfig>();
  async get(key: string) { return this.m.get(key); }
  async set(key: string, value: ResolvedConfig) { this.m.set(key, value); }
}

export class ConfigUnavailable extends Error {}

export interface LoadOptions {
  agentId: string;
  businessUnit: string;
  environment: "dev" | "test" | "production";
  resolverUrl?: string;
  tokenProvider?: () => Promise<string>;
  fetch?: typeof fetch;
  cache?: ConfigCache;
  timeoutMs?: number;
}

const defaultCache = new MemoryConfigCache();

export class AgentConfig {
  private constructor(
    readonly agentId: string,
    readonly businessUnit: string,
    readonly environment: string,
    readonly raw: Readonly<ResolvedConfig>,
    readonly source: string,
  ) {}

  get agent() { return this.raw.agent; }
  get profile() { return this.raw.profile; }
  get bindings() { return this.raw.bindings; }
  get thresholds(): Record<string, number> { return this.raw.thresholds?.values ?? {}; }
  get versions(): Record<string, string> { return { ...this.raw.versions }; }

  /** Attributes for every span and outcome event (C09-F2). */
  otelAttributes(): Record<string, string> {
    const out: Record<string, string> = {
      "eap.agent.id": this.agentId,
      "eap.business_unit": this.businessUnit,
      "eap.environment": this.environment,
      "eap.config.source": this.source,
    };
    for (const [k, v] of Object.entries(this.raw.versions ?? {})) out[`eap.version.${k}`] = v;
    return out;
  }

  /** Fetch from the resolver (C07) and pin for the session (C09-F1). */
  static async load(o: LoadOptions): Promise<AgentConfig> {
    const env = (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env;
    const base = (o.resolverUrl ?? env?.EAP_RESOLVER_URL ?? "").replace(/\/$/, "");
    const f = o.fetch ?? fetch;
    const cache = o.cache ?? defaultCache;
    const key = `${o.environment}/${o.agentId}/${o.businessUnit}`;
    const qs = new URLSearchParams({ agentId: o.agentId, businessUnit: o.businessUnit, environment: o.environment });
    const headers: Record<string, string> = {};
    if (o.tokenProvider) headers.Authorization = `Bearer ${await o.tokenProvider()}`;
    let status = 0;
    try {
      const r = await f(`${base}/resolve?${qs}`, { headers, signal: AbortSignal.timeout(o.timeoutMs ?? 2000) });
      status = r.status;
      const body = (await r.json()) as ResolvedConfig & { message?: string };
      if (r.status !== 200) throw new ConfigUnavailable(`resolver answered ${r.status}: ${body.message ?? ""}`);
      await cache.set(key, body);
      return new AgentConfig(o.agentId, o.businessUnit, o.environment, Object.freeze(body), body.source);
    } catch (e) {
      if (status === 401 || status === 403) throw e; // not an outage: never fall back for a forbidden caller
      const lkg = await cache.get(key);
      if (lkg) return new AgentConfig(o.agentId, o.businessUnit, o.environment, Object.freeze(lkg), "sdk-last-known-good");
      throw new ConfigUnavailable(`no configuration for ${key}: ${(e as Error).message}`);
    }
  }
}
