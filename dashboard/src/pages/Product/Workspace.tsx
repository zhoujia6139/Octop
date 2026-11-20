import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Empty,
  Input,
  List,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useTranslation } from "react-i18next";
import { ProductApi, errorText } from "../../api/product/client";
import type {
  Identity,
  LocalRecord,
  MessageData,
  ThreadData,
  TurnEvent,
  TurnStatus,
} from "../../api/product/contracts";
import { copyText } from "../../utils/copyText";
import { useProductData } from "./useProductData";
import { date } from "./Management";

type Thread = LocalRecord & { data: ThreadData };
type Message = LocalRecord & { data: MessageData };
export function orderedMessages(
  records: LocalRecord[],
  thread: string,
): Message[] {
  return records
    .filter(
      (row): row is Message =>
        row.data?.kind === "message" && row.data.thread_id === thread,
    )
    .sort(
      (a, b) =>
        a.data.position - b.data.position || a.item_id.localeCompare(b.item_id),
    );
}
export function messageText(data: MessageData): string {
  return typeof data.content === "string"
    ? data.content
    : data.content
        .map((block) => (typeof block.text === "string" ? block.text : ""))
        .filter(Boolean)
        .join("\n");
}

type MessageGroup =
  | { kind: "message"; message: Message }
  | { kind: "tools"; messages: Message[] };

export function groupMessages(messages: Message[]): MessageGroup[] {
  const groups: MessageGroup[] = [];
  for (const row of messages) {
    const tool =
      row.data.role === "tool" ||
      (row.data.role === "assistant" && row.data.tool_calls.length > 0);
    if (!tool) {
      groups.push({ kind: "message", message: row });
      continue;
    }
    const previous = groups[groups.length - 1];
    if (
      previous?.kind === "tools" &&
      previous.messages[0].data.turn_id === row.data.turn_id &&
      previous.messages[0].data.thread_id === row.data.thread_id
    ) {
      previous.messages.push(row);
    } else {
      groups.push({ kind: "tools", messages: [row] });
    }
  }
  return groups;
}

function ChatMessage({
  row,
  api,
  identity,
  onError,
}: {
  row: Message;
  api: ProductApi;
  identity: Identity;
  onError(error: string): void;
}) {
  const { t } = useTranslation();
  return (
    <Card
      key={row.item_id}
      size="small"
      className="product-message"
      title={
        <Space>
          <span>{t(`product.roles.${row.data.role}`)}</span>
          <Typography.Text type="secondary">
            {date(row.data.created_at, identity)}
          </Typography.Text>
        </Space>
      }
      extra={
        <Button
          type="text"
          onClick={() => {
            void copyText(messageText(row.data));
          }}
        >
          {t("product.copy")}
        </Button>
      }
    >
      {row.data.role === "tool" ? (
        <pre>{messageText(row.data)}</pre>
      ) : (
        <ReactMarkdown
          remarkPlugins={[remarkGfm]}
          components={{
            img: ({ alt }) => <span>{alt}</span>,
            a: ({ href, children }) =>
              /^https?:\/\//i.test(href ?? "") ? (
                <a href={href} rel="noreferrer" target="_blank">
                  {children}
                </a>
              ) : (
                <span>{children}</span>
              ),
          }}
        >
          {messageText(row.data)}
        </ReactMarkdown>
      )}
      {row.data.tool_calls.map((call, index) => (
        <div key={index}>
          <Typography.Text strong>
            {t("product.toolCall", {
              name: String(call.name ?? ""),
            })}
          </Typography.Text>
          <pre>{JSON.stringify(call.args, null, 2)}</pre>
        </div>
      ))}
      <Space wrap>
        {row.data.attachments.map((attachment) => (
          <Button
            key={attachment.file_id}
            onClick={() => {
              void api
                .downloadAttachment(row.item_id, attachment.file_id)
                .catch((err) => onError(errorText(err)));
            }}
          >
            {t("product.downloadAttachment")}
          </Button>
        ))}
      </Space>
    </Card>
  );
}

function Chat({
  api,
  identity,
  agent,
  records,
  reload,
  enabled,
}: {
  api: ProductApi;
  identity: Identity;
  agent: string;
  records: LocalRecord[];
  reload(): Promise<void>;
  enabled: boolean;
}) {
  const { t } = useTranslation();
  const translate = useRef(t);
  translate.current = t;
  const [thread, setThread] = useState("");
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<LocalRecord[]>([]);
  const [error, setError] = useState("");
  const [status, setStatus] = useState<TurnStatus | null>(null);
  const [approval, setApproval] = useState<
    Array<{ name: string; args: unknown }>
  >([]);
  const [reconnect, setReconnect] = useState(0);
  const [connected, setConnected] = useState(false);
  const [uploading, setUploading] = useState(false);
  const socket = useRef<WebSocket | null>(null);
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reloadRef = useRef(reload);
  reloadRef.current = reload;
  const messages = useMemo(
    () => orderedMessages(records, thread),
    [records, thread],
  );
  const groups = useMemo(() => groupMessages(messages), [messages]);
  const threads = records.filter(
    (row): row is Thread => row.data?.kind === "thread",
  );
  const latest = threads.find((row) => row.item_id === thread);
  const active = status === "running" || status === "awaiting_approval";

  useEffect(() => {
    if (!thread) return;
    let alive = true;
    let received = false;
    setConnected(false);
    setApproval([]);
    setError("");
    setStatus(null);
    setAttachments([]);
    const changed = () => {
      if (refreshTimer.current !== null) return;
      refreshTimer.current = setTimeout(() => {
        refreshTimer.current = null;
        if (alive) void reloadRef.current();
      }, 160);
    };
    const onEvent = (event: TurnEvent) => {
      if (!alive) return;
      received = true;
      if (event.type === "error") {
        setStatus("failed");
        setError(errorText(new Error(event.error?.code)));
        changed();
        return;
      }
      if (event.type === "turn.started") {
        setStatus("running");
        setApproval([]);
      }
      if (event.type === "turn.status")
        setStatus(String(event.data?.status) as TurnStatus);
      if (event.type === "hitl_required") {
        const request = event.data?.request as
          | { action_requests?: Array<{ name: string; args: unknown }> }
          | undefined;
        setApproval(request?.action_requests ?? []);
      }
      changed();
    };
    const ws = api.connect(agent, thread, onEvent, () => {
      if (alive) {
        setStatus("interrupted");
        setConnected(false);
        setApproval([]);
        setError(translate.current("product.connectionLost"));
        void reloadRef.current();
      }
    });
    socket.current = ws;
    ws.onopen = () => {
      if (alive) setConnected(true);
    };
    // Merely opening/reopening a conversation can read state. It never sends
    // start or resume. A live pending approval still needs a user decision.
    void api
      .call<{
        status: TurnStatus | null;
        approval: {
          action_requests?: Array<{ name: string; args: unknown }>;
        } | null;
      }>(`/agents/${agent}/threads/${thread}/state`)
      .then((state) => {
        if (alive && !received) {
          setStatus(state.status);
          setApproval(state.approval?.action_requests ?? []);
        }
      })
      .catch((err) => {
        if (alive) setError(errorText(err));
      });
    return () => {
      alive = false;
      ws.close();
      socket.current = null;
      if (refreshTimer.current) clearTimeout(refreshTimer.current);
      refreshTimer.current = null;
    };
  }, [api, agent, thread, reconnect]);

  const send = (command: unknown) => {
    if (socket.current?.readyState !== WebSocket.OPEN) {
      setError(t("product.connectionLost"));
      return;
    }
    socket.current.send(JSON.stringify(command));
  };
  return (
    <div className="product-chat">
      <Card
        size="small"
        title={t("product.conversations")}
        extra={
          <Button
            disabled={!enabled || active}
            onClick={async () => {
              try {
                const result = await api.thread(agent);
                await reload();
                setThread(result.thread_id);
              } catch (err) {
                setError(errorText(err));
              }
            }}
          >
            {t("product.newChat")}
          </Button>
        }
      >
        <List
          dataSource={threads}
          locale={{ emptyText: t("product.noChats") }}
          renderItem={(row) => (
            <List.Item>
              <Button
                type={row.item_id === thread ? "primary" : "text"}
                block
                disabled={active && row.item_id !== thread}
                onClick={() => setThread(row.item_id)}
              >
                {row.data.title || t("product.untitledChat")}
              </Button>
            </List.Item>
          )}
        />
      </Card>
      <div className="product-stack product-chat-main">
        {error && (
          <Alert
            type="error"
            message={error}
            action={
              !connected && thread ? (
                <Button onClick={() => setReconnect((value) => value + 1)}>
                  {t("product.reconnect")}
                </Button>
              ) : undefined
            }
          />
        )}
        {!enabled && (
          <Alert type="warning" message={t("product.archiveOnly")} />
        )}
        {!thread ? (
          <Empty description={t("product.selectChat")} />
        ) : (
          <>
            <Tag className="product-status">
              {t(
                `product.states.${
                  status ?? latest?.data.status ?? "completed"
                }`,
              )}
            </Tag>
            {(status ?? latest?.data.status) === "interrupted" && (
              <Alert type="warning" message={t("product.interruptedHelp")} />
            )}
            <div className="product-messages">
              {groups.map((group) =>
                group.kind === "message" ? (
                  <ChatMessage
                    key={group.message.item_id}
                    row={group.message}
                    api={api}
                    identity={identity}
                    onError={setError}
                  />
                ) : (
                  <details
                    className="product-tool-details product-tool-group"
                    key={group.messages[0].item_id}
                  >
                    <summary>
                      {t("product.toolActivity", {
                        count:
                          group.messages.reduce<number>(
                            (total, row) => total + row.data.tool_calls.length,
                            0,
                          ) || group.messages.length,
                      })}
                    </summary>
                    <div className="product-stack">
                      {group.messages.map((row) => (
                        <ChatMessage
                          key={row.item_id}
                          row={row}
                          api={api}
                          identity={identity}
                          onError={setError}
                        />
                      ))}
                    </div>
                  </details>
                ),
              )}
            </div>
            {status === "awaiting_approval" && (
              <Card title={t("product.approvalRequired")}>
                {approval.map((action, index) => (
                  <div key={index}>
                    <Typography.Text strong>
                      {t("product.toolCall", { name: action.name })}
                    </Typography.Text>
                    <pre>{JSON.stringify(action.args, null, 2)}</pre>
                  </div>
                ))}
                <Space>
                  <Button
                    type="primary"
                    disabled={!connected || !approval.length}
                    onClick={() =>
                      send({
                        type: "resume",
                        decisions: approval.map(() => ({ type: "approve" })),
                      })
                    }
                  >
                    {t("product.approve")}
                  </Button>
                  <Button
                    danger
                    disabled={!connected || !approval.length}
                    onClick={() =>
                      send({
                        type: "resume",
                        decisions: approval.map(() => ({
                          type: "reject",
                          message: t("product.rejectedByUser"),
                        })),
                      })
                    }
                  >
                    {t("product.reject")}
                  </Button>
                </Space>
              </Card>
            )}
            <Input.TextArea
              aria-label={t("product.message")}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder={t("product.messagePlaceholder")}
              rows={3}
              maxLength={100000}
              disabled={!enabled || active}
            />
            <Space wrap>
              <input
                aria-label={t("product.attachFiles")}
                type="file"
                multiple
                disabled={!enabled || active || uploading}
                onChange={async (event) => {
                  const files = Array.from(event.target.files ?? []);
                  event.target.value = "";
                  setUploading(true);
                  try {
                    const saved: LocalRecord[] = [];
                    for (const file of files) {
                      if (file.size > 256 * 1024 * 1024)
                        throw new Error("ATTACHMENT_TOO_LARGE");
                      saved.push(await api.upload(agent, file));
                    }
                    setAttachments((before) => [...before, ...saved]);
                    await reload();
                  } catch (err) {
                    setError(errorText(err));
                  } finally {
                    setUploading(false);
                  }
                }}
              />
              <Button
                type="primary"
                disabled={
                  !enabled || !connected || active || !text.trim() || uploading
                }
                onClick={() => {
                  send({
                    type: "start",
                    text,
                    attachment_ids: attachments.map((r) => r.item_id),
                  });
                  setText("");
                  setAttachments([]);
                  setStatus("running");
                }}
              >
                {t("product.send")}
              </Button>
              {active && (
                <Button
                  danger
                  disabled={!connected}
                  onClick={() => send({ type: "cancel" })}
                >
                  {t("product.cancel")}
                </Button>
              )}
            </Space>
            <Space wrap>
              {attachments.map((row) => (
                <Tag
                  key={row.item_id}
                  closable
                  onClose={() =>
                    setAttachments((before) =>
                      before.filter((r) => r.item_id !== row.item_id),
                    )
                  }
                >
                  {row.data?.kind === "file"
                    ? row.data.path.split("/").pop()
                    : ""}
                </Tag>
              ))}
            </Space>
          </>
        )}
      </div>
    </div>
  );
}

export function DesktopWorkspace({
  api,
  identity,
  section,
}: {
  api: ProductApi;
  identity: Identity;
  section: "chat" | "files" | "sync";
}) {
  const { t } = useTranslation();
  const agents = useProductData(api.agents, []);
  const [agent, setAgent] = useState("");
  useEffect(() => {
    if (!agent && agents.data.length) setAgent(agents.data[0].agent_id);
  }, [agent, agents.data]);
  const load = useCallback(
    () => (agent ? api.records(agent) : Promise.resolve([])),
    [api, agent],
  );
  const records = useProductData(load, []);
  const conflicts = useProductData(
    useCallback(
      () => (section === "sync" ? api.records() : Promise.resolve([])),
      [api, section],
    ),
    [],
  );
  const sync = useProductData(api.syncState, null);
  const reloadSync = sync.reload;
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const timer = setInterval(() => {
      void reloadSync();
    }, 10000);
    return () => clearInterval(timer);
  }, [reloadSync]);
  const retry = async () => {
    setWorking(true);
    try {
      await api.retry();
      setError("");
      await Promise.all([
        sync.reload(),
        records.reload(),
        agents.reload(),
        conflicts.reload(),
      ]);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setWorking(false);
    }
  };
  const local = agents.data.find((row) => row.agent_id === agent);
  return (
    <div
      className={`product-stack${
        section === "chat" ? " product-chat-workspace" : ""
      }`}
    >
      <Typography.Title level={3}>
        {t(`product.nav.${section}`)}
      </Typography.Title>
      {(error || agents.error || records.error || sync.error) && (
        <Alert
          type="error"
          message={error || agents.error || records.error || sync.error}
        />
      )}
      {section !== "sync" && (
        <Select
          aria-label={t("product.agent")}
          value={agent || undefined}
          placeholder={t("product.selectAgent")}
          onChange={setAgent}
          options={agents.data.map((row) => ({
            value: row.agent_id,
            label: row.name + (row.enabled ? "" : ` · ${t("product.archive")}`),
          }))}
        />
      )}
      {section === "chat" && local && (
        <Chat
          key={agent}
          api={api}
          identity={identity}
          agent={agent}
          records={records.data}
          reload={records.reload}
          enabled={local.enabled}
        />
      )}
      {section === "chat" && !agents.data.length && (
        <Empty description={t("product.noAgents")} />
      )}
      {section === "files" && (
        <>
          <Alert type="info" message={t("product.fileScopeHelp")} />
          <Table
            rowKey="item_id"
            dataSource={records.data.filter((row) => row.data?.kind === "file")}
            columns={[
              {
                title: t("product.path"),
                key: "path",
                render: (_, row) => row.data?.kind === "file" && row.data.path,
              },
              {
                title: t("product.size"),
                key: "size",
                render: (_, row) =>
                  row.data?.kind === "file" && `${row.data.size} B`,
              },
              {
                title: t("product.state"),
                dataIndex: "state",
                render: (value: string) => (
                  <Tag>{t(`product.states.${value}`)}</Tag>
                ),
              },
              {
                title: t("product.actions"),
                key: "actions",
                render: (_, row) => (
                  <Space>
                    <Button
                      onClick={() => {
                        void api
                          .download(row)
                          .catch((err) => setError(errorText(err)));
                      }}
                    >
                      {t("product.download")}
                    </Button>
                    <Popconfirm
                      title={t("product.deleteConfirm")}
                      onConfirm={async () => {
                        try {
                          await api.removeRecord(row.item_id);
                          await records.reload();
                          await sync.reload();
                        } catch (err) {
                          setError(errorText(err));
                        }
                      }}
                    >
                      <Button danger>{t("product.delete")}</Button>
                    </Popconfirm>
                  </Space>
                ),
              },
            ]}
          />
        </>
      )}
      {section === "sync" && (
        <>
          <Alert
            type={sync.data?.conflicts ? "warning" : "info"}
            message={t("product.syncCounts", {
              pending: sync.data?.pending ?? "—",
              conflicts: sync.data?.conflicts ?? "—",
            })}
            description={t("product.syncHelp")}
          />
          {sync.data?.last_error && (
            <Alert
              type="error"
              message={t(`apiErrors.${sync.data.last_error}`, {
                defaultValue: t("product.requestFailed"),
              })}
            />
          )}
          <Space>
            <Button
              loading={working}
              type="primary"
              onClick={() => {
                void retry();
              }}
            >
              {t("product.retrySync")}
            </Button>
            <Popconfirm
              title={t("product.restore")}
              description={t("product.restoreHelp")}
              onConfirm={async () => {
                setWorking(true);
                try {
                  const result = await api.restore();
                  void message.info(t("product.restoreResult", result));
                  await Promise.all([
                    records.reload(),
                    agents.reload(),
                    sync.reload(),
                  ]);
                } catch (err) {
                  setError(errorText(err));
                } finally {
                  setWorking(false);
                }
              }}
            >
              <Button loading={working}>{t("product.restore")}</Button>
            </Popconfirm>
          </Space>
          {conflicts.error && <Alert type="error" message={conflicts.error} />}
          <Table
            rowKey="item_id"
            dataSource={conflicts.data.filter(
              (row) => row.state === "conflict",
            )}
            columns={[
              {
                title: t("product.resource"),
                key: "resource",
                render: (_, row) =>
                  row.data?.kind === "file"
                    ? row.data.path
                    : row.data?.kind === "thread"
                    ? row.data.title || t("product.untitledChat")
                    : t(`product.recordKinds.${row.kind}`),
              },
              {
                title: t("product.state"),
                key: "state",
                render: (_, row) => (
                  <Typography.Text type="danger">
                    {t(`apiErrors.${row.error_code ?? "SYNC_CONFLICT"}`, {
                      defaultValue: t("product.requestFailed"),
                    })}
                  </Typography.Text>
                ),
              },
            ]}
          />
          <Typography.Paragraph>
            {t("product.conflictHelp")}
          </Typography.Paragraph>
        </>
      )}
    </div>
  );
}
