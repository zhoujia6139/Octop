/** Independent application contracts. Supplier credentials never enter desktop types. */
export type Role = "cloud" | "local";
export interface User {
  user_id: number;
  username: string;
  role: "admin" | "user";
  enabled: boolean;
  mfa_enabled: boolean;
}
export interface Identity {
  cloud_id: string;
  user: User;
  device_id: string | null;
  binding_generation: number | null;
  timezone: string;
}
export interface Token {
  access_token: string;
  expires_in: number;
  identity: Identity;
}
export interface Login {
  session: Token | null;
  mfa_token: string | null;
}
export interface CloudSessionResult {
  status: "mfa_required" | "ready_to_bind" | "bound";
  identity?: Identity;
}
export interface CloudBridge {
  configuration(): Promise<{ cloud_url: string | null }>;
  configure(url: string): Promise<{ cloud_url: string | null }>;
  login(username: string, password: string): Promise<CloudSessionResult>;
  verifyMfa(code: string): Promise<CloudSessionResult>;
  bind(replace?: boolean): Promise<CloudSessionResult>;
  renew(): Promise<Identity | null>;
  current(): Promise<Identity | null>;
  logout(): Promise<{ revoked: boolean }>;
}
export interface Device {
  device_id: string;
  label: string;
  binding_generation: number;
  revoked: boolean;
  last_seen_at: number;
}
export interface SkillRef {
  resource_id: string;
  revision: number;
}
export interface SkillFile {
  path: string;
  content_base64: string;
  sha256: string;
  size: number;
}
export interface AgentSpec {
  kind: "agent";
  name: string;
  model_id: string;
  system_prompt: string | null;
  skills: SkillRef[];
}
export interface SkillSpec {
  kind: "skill";
  name: string;
  slug: string;
  files: SkillFile[];
}
export type Spec = AgentSpec | SkillSpec;
export interface Resource {
  resource_id: string;
  scope: "platform" | "personal";
  kind: "agent" | "skill";
  revision: number;
  published_revision: number | null;
  deleted: boolean;
}
export interface LocalResource
  extends Omit<Resource, "published_revision" | "deleted"> {
  agent_id: string | null;
  desired_revision: number | null;
  installed_revision: number | null;
  spec: Spec | null;
  sync_state: "pending" | "synced" | "conflict";
  install_state: "installed" | "pending" | "failed" | "removed";
  error_code: string | null;
}
export interface Version {
  resource: Resource;
  revision: number;
  spec: Spec | null;
  digest: string;
  published: boolean;
}
export interface InstallReport {
  device_id: string;
  resource_id: string;
  desired_revision: number | null;
  installed_revision: number | null;
  state: string;
  error_code: string | null;
  runtime_state: string;
  reported_at: number;
}
export interface Agent {
  agent_id: string;
  name: string;
  model_id: string;
  enabled: boolean;
}
export type TurnStatus =
  | "running"
  | "awaiting_approval"
  | "completed"
  | "interrupted"
  | "cancelled"
  | "failed";
export interface ThreadData {
  kind: "thread";
  title: string;
  created_at: number;
  updated_at: number;
  status: TurnStatus;
}
export interface MessageData {
  kind: "message";
  thread_id: string;
  turn_id: string;
  position: number;
  created_at: number;
  role: "user" | "assistant" | "tool";
  content: string | Array<Record<string, unknown>>;
  tool_calls: Array<Record<string, unknown>>;
  tool_call_id: string | null;
  attachments: Array<{ file_id: string; sha256: string }>;
  status: TurnStatus;
}
export interface FileData {
  kind: "file";
  path: string;
  size: number;
  sha256: string;
}
export interface MemoryData {
  kind: "memory";
  table: string;
  key: string[];
  record: Record<string, unknown>;
}
export interface LocalRecord {
  item_id: string;
  agent_id: string;
  kind: "thread" | "message" | "file" | "memory";
  data: ThreadData | MessageData | FileData | MemoryData | null;
  state: "pending" | "synced" | "conflict";
  error_code: string | null;
}
export interface SyncState {
  pending: number;
  conflicts: number;
  last_error: string | null;
}
export interface TurnEvent {
  type: string;
  turn_id?: string;
  thread_id?: string;
  sequence?: number;
  data?: Record<string, unknown>;
  error?: { code: string };
}
export interface Rate {
  revision: string;
  currency: string;
  effective_at: number;
  uncached_input: number;
  cache_read: number;
  cache_write: number;
  output: number;
}
export interface Model {
  model_id: string;
  name: string;
  protocol: "chat_completions" | "responses" | "messages" | "embeddings";
  input_modalities: Array<"text" | "image">;
  tools: boolean;
  input_token_ceiling: number;
  output_token_ceiling: number;
  rate: Rate;
}
export interface ModelDefinition extends Model {
  routes: Array<{ provider_id: string; upstream_model: string }>;
  enabled: boolean;
}
export interface Provider {
  provider_id: string;
  base_url: string;
  credential_configured: boolean;
  free_rejection_statuses: number[];
}
export interface Budget {
  user_id: number;
  currency: string;
  limit_nanos: string;
  spent_nanos: string;
  reserved_nanos: string;
  unlimited: boolean;
}
export interface Usage {
  uncached_input: number;
  cache_read: number;
  cache_write: number;
  output: number;
}
export interface Attempt {
  attempt_id: string;
  provider_id: string;
  state: string;
  cost_nanos: string | null;
  reserved_nanos: string;
  usage: Usage | null;
  evidence: string | null;
  reason: string | null;
}
export interface ModelRequest {
  request_id: string;
  user_id: number;
  model_id: string;
  state: string;
  created_at: number;
  currency: string;
  attempts: Attempt[];
}
