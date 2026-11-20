import i18n from "../../i18n";
import { getApiUrl } from "../config";
import { getDesktopBridge } from "../../utils/desktopBridge";
import { resolveWsUrl } from "../../utils/desktopWsUrl";
import { saveBlobAsFile } from "../../utils/saveBlobAsFile";
import type {
  Agent,
  Budget,
  Device,
  Identity,
  InstallReport,
  LocalRecord,
  LocalResource,
  Login,
  Model,
  ModelDefinition,
  ModelRequest,
  Provider,
  Resource,
  Role,
  Spec,
  SyncState,
  Token,
  TurnEvent,
  Version,
} from "./contracts";

export class ProductError extends Error {
  readonly code: string;
  constructor(code: string) {
    super(code);
    this.code = code;
  }
}

export function errorCode(error: unknown): string {
  if (error instanceof ProductError) return error.code;
  // Electron preserves the remote error code inside its IPC error message.
  const message = error instanceof Error ? error.message : "";
  return (
    message.match(/\b[A-Z][A-Z_]{3,}\b/g)?.slice(-1)[0] ?? "INTERNAL_ERROR"
  );
}

export function errorText(error: unknown): string {
  const code = errorCode(error);
  return i18n.t(`apiErrors.${code}`, {
    defaultValue: i18n.t("product.requestFailed"),
  });
}

/** Quote monetary integer tokens before JSON.parse can lose nano precision.
 * The outer tokenizer consumes complete JSON strings, so user content containing
 * escaped field-like text is left untouched.
 */
export function parseProductJson(text: string): unknown {
  const quoted = text.replace(
    /"(?:[^"\\]|\\.)*"(?:\s*:\s*-?\d+(?![\d.eE]))?/g,
    (token) =>
      token.replace(
        /^"(limit_nanos|spent_nanos|reserved_nanos|cost_nanos)"(\s*:\s*)(-?\d+)$/,
        '"$1"$2"$3"',
      ),
  );
  return JSON.parse(quoted);
}

export const newId = () => crypto.randomUUID().replaceAll("-", "");

/** One instance per selected account; disposal aborts all obsolete UI requests. */
export class ProductApi {
  private token = "";
  private lifetime = new AbortController();
  readonly desktop = !!getDesktopBridge();
  onUnauthorized: (() => void) | null = null;

  setToken(token: string) {
    this.token = token;
  }
  dispose() {
    this.token = "";
    this.lifetime.abort();
  }

  private url(path: string) {
    return this.desktop ? `octop://app/api${path}` : getApiUrl(path);
  }

  async response(
    path: string,
    method = "GET",
    body?: unknown,
  ): Promise<Response> {
    let response: Response;
    try {
      response = await fetch(this.url(path), {
        method,
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        signal: this.lifetime.signal,
        headers: {
          "Accept-Language": i18n.language?.startsWith("zh") ? "zh" : "en",
          ...(this.token && !this.desktop
            ? { Authorization: `Bearer ${this.token}` }
            : {}),
          ...(body instanceof Blob
            ? { "Content-Type": "application/octet-stream" }
            : body === undefined
            ? {}
            : { "Content-Type": "application/json" }),
        },
        ...(body === undefined
          ? {}
          : { body: body instanceof Blob ? body : JSON.stringify(body) }),
      });
    } catch (error) {
      if (this.lifetime.signal.aborted) throw error;
      throw new ProductError("CLOUD_UNAVAILABLE");
    }
    if (!response.ok) {
      const data = (await response.json().catch(() => null)) as {
        error?: { code: string };
      } | null;
      if (response.status === 401 && this.token) this.onUnauthorized?.();
      throw new ProductError(
        data?.error?.code ??
          (response.status === 422 ? "SLASH_BAD_ARGS" : "INTERNAL_ERROR"),
      );
    }
    return response;
  }

  async call<T>(path: string, method = "GET", body?: unknown): Promise<T> {
    const response = await this.response(path, method, body);
    return response.status === 204
      ? (undefined as T)
      : (parseProductJson(await response.text()) as T);
  }

  configuration = () =>
    this.call<{ role: Role; timezone: string }>("/application");
  login = (username: string, password: string) =>
    this.call<Login>("/auth/login", "POST", { username, password });
  mfa = (mfa_token: string, code: string) =>
    this.call<Token>("/auth/mfa/verify", "POST", { mfa_token, code });
  me = () => this.call<Identity>("/auth/me");
  logout = () => this.call<void>("/auth/logout", "POST");
  users = () => this.call<Identity["user"][]>("/users");
  devices = (userId?: number) =>
    this.call<Device[]>(
      userId === undefined ? "/auth/devices" : `/users/${userId}/devices`,
    );
  revoke = (id: string, userId?: number) =>
    this.call<void>(
      userId === undefined
        ? `/auth/devices/${id}`
        : `/users/${userId}/devices/${id}`,
      "DELETE",
    );
  agents = () => this.call<Agent[]>("/agents");
  models = () => this.call<Model[]>("/models");
  resources = () => this.call<LocalResource[]>("/resources");
  definitions = () => this.call<Resource[]>("/resource-admin/resources");
  version = (id: string, revision: number) =>
    this.call<Version>(`/resource-admin/resources/${id}/versions/${revision}`);
  saveResource = (
    id: string,
    revision: number,
    spec: Spec | null,
    admin = false,
  ) =>
    this.call<Resource | LocalResource>(
      admin ? `/resource-admin/resources/${id}` : `/resources/personal/${id}`,
      "PUT",
      { operation_id: newId(), expected_revision: revision, spec },
    );
  copy = (id: string, revision: number) =>
    this.call<LocalResource>(`/resources/${id}/copy`, "POST", {
      resource_id: newId(),
      operation_id: newId(),
      revision,
    });
  resourceSync = (restore = false) =>
    this.call<LocalResource[]>(
      `/resources/sync?restore_personal=${restore}`,
      "POST",
    );
  publish = (id: string, revision: number) =>
    this.call<Resource>(`/resource-admin/resources/${id}/publish`, "POST", {
      revision,
    });
  assign = (user: number, id: string, revision: number | null) =>
    this.call<Resource>(
      `/resource-admin/users/${user}/assignments/${id}`,
      "PUT",
      { revision },
    );
  reports = (user: number) =>
    this.call<InstallReport[]>(`/resource-admin/users/${user}/reports`);
  records = (agent?: string, kind?: string) =>
    this.call<LocalRecord[]>(
      `/sync/records?${new URLSearchParams({
        ...(agent ? { agent_id: agent } : {}),
        ...(kind ? { kind } : {}),
      })}`,
    );
  syncState = () => this.call<SyncState>("/sync/status");
  retry = () => this.call<SyncState>("/sync/retry", "POST");
  restore = () =>
    this.call<{ restored: number; skipped: number }>("/sync/restore", "POST");
  removeRecord = (id: string) =>
    this.call<void>(`/sync/records/${id}`, "DELETE");
  thread = (agent: string) =>
    this.call<{ thread_id: string }>(`/agents/${agent}/threads`, "POST");
  upload = (agent: string, file: File) =>
    this.call<LocalRecord>(
      `/agents/${agent}/files/inbound/${encodeURIComponent(
        newId() + "-" + file.name.normalize("NFC"),
      )}`,
      "PUT",
      file,
    );
  budgets = (user?: number) =>
    this.call<Budget[]>(
      `/usage/budgets${user === undefined ? "" : `?user_id=${user}`}`,
    );
  requests = (user?: number) =>
    this.call<ModelRequest[]>(
      `/usage/requests${user === undefined ? "" : `?user_id=${user}`}`,
    );
  providers = () => this.call<Provider[]>("/model-admin/providers");
  modelDefinitions = () => this.call<ModelDefinition[]>("/model-admin/models");

  connect(
    agent: string,
    thread: string,
    onEvent: (event: TurnEvent) => void,
    onClose: () => void,
  ): WebSocket {
    if (!this.desktop) throw new ProductError("RESOURCE_NOT_ALLOWED");
    const socket = new WebSocket(
      resolveWsUrl(`/api/agents/${agent}/threads/${thread}/ws`),
    );
    socket.onmessage = (event) => {
      onEvent(JSON.parse(String(event.data)) as TurnEvent);
    };
    socket.onclose = onClose;
    this.lifetime.signal.addEventListener("abort", () => socket.close(), {
      once: true,
    });
    return socket;
  }

  async downloadAttachment(messageId: string, fileId: string) {
    const path = `/sync/messages/${messageId}/attachments/${fileId}`;
    const first = await this.response(path);
    const size = Number(first.headers.get("X-Octop-File-Size"));
    if (!Number.isSafeInteger(size) || size < 0 || size > 256 * 1024 * 1024)
      throw new ProductError("SYNC_INVALID");
    const parts: BlobPart[] = [await first.arrayBuffer()];
    for (let offset = 1024 * 1024; offset < size; offset += 1024 * 1024)
      parts.push(
        await (await this.response(`${path}?offset=${offset}`)).arrayBuffer(),
      );
    await saveBlobAsFile(new Blob(parts), `attachment-${fileId}`);
  }

  async download(record: LocalRecord, messageId?: string) {
    if (record.data?.kind !== "file") return;
    const parts: BlobPart[] = [];
    for (let offset = 0; offset < record.data.size; offset += 1024 * 1024) {
      const path = messageId
        ? `/sync/messages/${messageId}/attachments/${record.item_id}`
        : `/sync/records/${record.item_id}/content`;
      parts.push(
        await (await this.response(`${path}?offset=${offset}`)).arrayBuffer(),
      );
    }
    await saveBlobAsFile(
      new Blob(parts),
      record.data.path.split("/").slice(-1)[0] ?? "download",
    );
  }
}
