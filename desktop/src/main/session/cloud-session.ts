/** Cloud credentials belong only to main; sidecar receives expiring device access. */
import type {
  CloudIdentity,
  CloudSessionResult,
  CloudToken,
} from "../../shared/ipc-contract";
import type { DeviceCredentialStore } from "./device-credential";

interface CloudSessionOptions {
  store: DeviceCredentialStore;
  localBaseUrl: () => string | null;
  localToken: () => string | null;
  label: string;
  fetchImpl?: typeof fetch;
}

interface RememberedDevice {
  cloudUrl: string;
  refreshToken: string;
}

export class CloudSessionError extends Error {
  readonly code: string;
  constructor(code: string) {
    super(code);
    this.code = code;
  }
}

export class CloudSessionClient {
  private readonly options: CloudSessionOptions;
  private readonly fetchImpl: typeof fetch;
  private queue: Promise<unknown> = Promise.resolve();
  private pendingMfa: string | null = null;
  private pendingAccount: string | null = null;
  private pendingOrigin: string | null = null;
  private renewal: ReturnType<typeof setTimeout> | null = null;
  private identity: CloudIdentity | null = null;
  private stopped = false;

  constructor(options: CloudSessionOptions) {
    this.options = options;
    this.fetchImpl = options.fetchImpl ?? fetch;
  }

  private serialized<T>(work: () => Promise<T>): Promise<T> {
    const next = this.queue.then(work, work);
    this.queue = next.catch(() => undefined);
    return next;
  }

  private async request(
    url: string,
    method: string,
    body?: unknown,
    token?: string,
  ): Promise<unknown> {
    let response: Response;
    try {
      response = await this.fetchImpl(url, {
        method,
        redirect: "error",
        signal: AbortSignal.timeout(10_000),
        headers: {
          "Content-Type": "application/json",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      });
    } catch {
      throw new CloudSessionError("CLOUD_UNAVAILABLE");
    }
    if (!response.ok) {
      const data = (await response.json().catch(() => null)) as {
        error?: { code?: string };
      } | null;
      throw new CloudSessionError(
        data?.error?.code ??
          (response.status === 401 ? "AUTH_FAILED" : "CLOUD_UNAVAILABLE"),
      );
    }
    return response.status === 204 ? null : response.json();
  }

  private async local(
    path: string,
    method: string,
    body?: unknown,
  ): Promise<unknown> {
    const base = this.options.localBaseUrl();
    const token = this.options.localToken();
    if (!base || !token) throw new CloudSessionError("CLOUD_UNAVAILABLE");
    return this.request(base + path, method, body, token);
  }

  private async origin(): Promise<string> {
    const config = (await this.local("/api/cloud/config", "GET")) as {
      cloud_url: string | null;
    };
    if (!config.cloud_url) throw new CloudSessionError("CLOUD_UNAVAILABLE");
    const parsed = new URL(config.cloud_url);
    if (
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash ||
      parsed.pathname !== "/" ||
      (parsed.protocol !== "https:" &&
        !(
          parsed.protocol === "http:" &&
          ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname)
        ))
    ) {
      throw new CloudSessionError("AUTH_FAILED");
    }
    return parsed.origin;
  }

  private async install(session: CloudToken): Promise<CloudIdentity> {
    if (
      !session.access_token ||
      !session.identity?.device_id ||
      session.expires_in <= 0
    ) {
      throw new CloudSessionError("AUTH_FAILED");
    }
    const identity = (await this.local(
      "/api/cloud/session",
      "PUT",
      session,
    )) as CloudIdentity;
    this.identity = identity;
    this.schedule(session.expires_in);
    return identity;
  }

  private schedule(seconds: number): void {
    this.stopRenewal();
    if (this.stopped) return;
    this.renewal = setTimeout(
      () => {
        void this.renew().catch(() => undefined);
      },
      Math.max(1, Math.min(seconds * 0.8, 240)) * 1000,
    );
    this.renewal.unref();
  }

  private stopRenewal(): void {
    if (this.renewal) clearTimeout(this.renewal);
    this.renewal = null;
  }

  login(username: string, password: string): Promise<CloudSessionResult> {
    return this.serialized(async () => {
      this.pendingMfa = null;
      this.pendingAccount = null;
      this.pendingOrigin = await this.origin();
      const result = (await this.request(
        this.pendingOrigin + "/api/auth/login",
        "POST",
        { username, password },
      )) as { mfa_token: string | null; session: CloudToken | null };
      if (result.mfa_token) {
        this.pendingMfa = result.mfa_token;
        return { status: "mfa_required" };
      }
      if (!result.session?.access_token)
        throw new CloudSessionError("AUTH_FAILED");
      this.pendingAccount = result.session.access_token;
      return { status: "ready_to_bind" };
    });
  }

  verifyMfa(code: string): Promise<CloudSessionResult> {
    return this.serialized(async () => {
      if (!this.pendingMfa || !this.pendingOrigin)
        throw new CloudSessionError("AUTH_FAILED");
      const result = (await this.request(
        this.pendingOrigin + "/api/auth/mfa/verify",
        "POST",
        { mfa_token: this.pendingMfa, code },
      )) as CloudToken;
      this.pendingMfa = null;
      this.pendingAccount = result.access_token;
      return { status: "ready_to_bind" };
    });
  }

  bind(replace = false): Promise<CloudSessionResult> {
    return this.serialized(async () => {
      if (
        !this.pendingAccount ||
        !this.pendingOrigin ||
        this.pendingOrigin !== (await this.origin())
      )
        throw new CloudSessionError("AUTH_FAILED");
      const origin = this.pendingOrigin;
      const accountToken = this.pendingAccount;
      const grant = (await this.request(
        origin + "/api/auth/devices",
        "POST",
        { label: this.options.label, replace },
        accountToken,
      )) as { session: CloudToken; refresh_token: string };
      if (!grant.refresh_token) throw new CloudSessionError("AUTH_FAILED");
      // Persist before attaching: a sidecar restart after this point can renew.
      await this.options.store.write(
        JSON.stringify({
          cloudUrl: origin,
          refreshToken: grant.refresh_token,
        } satisfies RememberedDevice),
      );
      this.pendingAccount = null;
      await this.request(
        origin + "/api/auth/logout",
        "POST",
        undefined,
        accountToken,
      ).catch(() => undefined);
      const identity = await this.install(grant.session);
      return { status: "bound", identity };
    });
  }

  renew(): Promise<CloudIdentity | null> {
    return this.serialized(async () => {
      const raw = await this.options.store.read();
      if (!raw) return null;
      const saved = JSON.parse(raw) as RememberedDevice;
      const origin = await this.origin();
      // Never forward a remembered credential to a newly configured server.
      if (saved.cloudUrl !== origin) throw new CloudSessionError("AUTH_FAILED");
      try {
        const session = (await this.request(
          origin + "/api/auth/device/refresh",
          "POST",
          { refresh_token: saved.refreshToken },
        )) as CloudToken;
        return await this.install(session);
      } catch (error) {
        if (
          error instanceof CloudSessionError &&
          ["AUTH_FAILED", "DEVICE_REVOKED", "USER_DISABLED"].includes(
            error.code,
          )
        ) {
          this.stopRenewal();
          this.identity = null;
          await this.local("/api/cloud/session", "DELETE").catch(
            () => undefined,
          );
          await this.options.store.clear();
        } else if (
          error instanceof CloudSessionError &&
          error.code === "CLOUD_UNAVAILABLE"
        ) {
          // Keep offline data open and retry while this desktop remains alive.
          this.schedule(60);
        }
        throw error;
      }
    });
  }

  logout(): Promise<{ revoked: boolean }> {
    return this.serialized(async () => {
      this.stopRenewal();
      this.pendingMfa = null;
      this.pendingAccount = null;
      this.pendingOrigin = null;
      this.identity = null;
      const raw = await this.options.store.read();
      // Detachment is mandatory even when the cloud is offline. It cancels
      // local tasks and leaves every account's data/outbox in its own directory.
      let detachError: unknown;
      try {
        await this.local("/api/cloud/session", "DELETE");
      } catch (error) {
        detachError = error;
      } finally {
        await this.options.store.clear();
      }
      let revoked = !raw;
      if (raw) {
        try {
          const saved = JSON.parse(raw) as RememberedDevice;
          const origin = await this.origin();
          if (saved.cloudUrl === origin) {
            const session = (await this.request(
              origin + "/api/auth/device/refresh",
              "POST",
              { refresh_token: saved.refreshToken },
            )) as CloudToken;
            await this.request(
              origin + "/api/auth/logout",
              "POST",
              undefined,
              session.access_token,
            );
            revoked = true;
          }
        } catch {
          /* Caller can show that remote revocation was not confirmed. */
        }
      }
      if (detachError) throw detachError;
      return { revoked };
    });
  }

  current(): CloudIdentity | null {
    return this.identity;
  }

  stop(): void {
    this.stopped = true;
    this.stopRenewal();
  }
}
