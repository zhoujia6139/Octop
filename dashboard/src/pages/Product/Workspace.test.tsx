import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { DesktopWorkspace, groupMessages, orderedMessages } from "./Workspace";
import { ProductApi } from "../../api/product/client";
import type {
  Identity,
  LocalRecord,
  TurnEvent,
} from "../../api/product/contracts";
import { date } from "./Management";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string, options?: { count?: number }) =>
      key === "product.toolActivity" ? `${key}:${options?.count}` : key,
  }),
}));
const identity: Identity = {
  cloud_id: "test-cloud",
  user: {
    user_id: 1,
    username: "alice",
    role: "user",
    enabled: true,
    mfa_enabled: false,
  },
  device_id: "one-device",
  binding_generation: 1,
  timezone: "Asia/Shanghai",
};
const thread = {
  item_id: "thread-one",
  agent_id: "agent-one",
  kind: "thread",
  data: {
    kind: "thread",
    title: "Previous turn",
    created_at: 1700000000,
    updated_at: 1700000000,
    status: "interrupted",
  },
  state: "pending",
} as LocalRecord;
const userMessage = {
  item_id: "message-one",
  agent_id: "agent-one",
  kind: "message",
  data: {
    kind: "message",
    thread_id: "thread-one",
    turn_id: "turn-one",
    position: 1,
    created_at: 1700000000,
    role: "user",
    content: "Earlier question",
    tool_calls: [],
    attachments: [],
    status: "interrupted",
  },
  state: "pending",
} as LocalRecord;

it("opening an interrupted conversation reads history without starting or replaying a turn", async () => {
  const send = vi.fn();
  const socket = {
    close: vi.fn(),
    send,
    readyState: 1,
    onopen: null as (() => void) | null,
  };
  const connect = vi.fn(() => socket);
  const api = {
    agents: async () => [
      { agent_id: "agent-one", name: "Local agent", enabled: true },
    ],
    records: async () => [thread, userMessage],
    syncState: async () => ({ pending: 2, conflicts: 0 }),
    connect,
    call: async () => ({ status: "interrupted", approval: null }),
  } as unknown as ProductApi;
  const result = render(
    <DesktopWorkspace api={api} identity={identity} section="chat" />,
  );
  await screen.findByText("Previous turn");
  fireEvent.click(screen.getByText("Previous turn"));
  await screen.findByText("Earlier question");
  expect(send).not.toHaveBeenCalled();
  expect(connect).toHaveBeenCalledOnce();
  expect(screen.getByText("product.interruptedHelp")).toBeInTheDocument();
  result.unmount();
  expect(socket.close).toHaveBeenCalledOnce();
});

it("a visible approval requires an explicit user decision and preserves the action count", async () => {
  const send = vi.fn();
  let event!: (value: TurnEvent) => void;
  const socket = {
    close: vi.fn(),
    send,
    readyState: 1,
    onopen: null as (() => void) | null,
  };
  const api = {
    agents: async () => [
      { agent_id: "agent-one", name: "Local agent", enabled: true },
    ],
    records: async () => [thread],
    syncState: async () => ({ pending: 1, conflicts: 0 }),
    connect: vi.fn((_: string, __: string, callback: typeof event) => {
      event = callback;
      return socket;
    }),
    call: async () => ({ status: "completed", approval: null }),
  } as unknown as ProductApi;
  render(<DesktopWorkspace api={api} identity={identity} section="chat" />);
  fireEvent.click(await screen.findByText("Previous turn"));
  await waitFor(() => expect(socket.onopen).not.toBeNull());
  act(() => {
    socket.onopen?.();
    event({
      type: "hitl_required",
      data: {
        request: {
          action_requests: [
            { name: "execute", args: { command: "touch one" } },
            { name: "write_file", args: { path: "artifacts/two" } },
          ],
        },
      },
    });
    event({ type: "turn.status", data: { status: "awaiting_approval" } });
  });
  expect(send).not.toHaveBeenCalled();
  fireEvent.click(await screen.findByText("product.approve"));
  expect(JSON.parse(send.mock.calls[0][0])).toEqual({
    type: "resume",
    decisions: [{ type: "approve" }, { type: "approve" }],
  });
});

it("continues a completed historical conversation only after the user sends a new message", async () => {
  const socket = {
    close: vi.fn(),
    send: vi.fn(),
    readyState: WebSocket.OPEN,
    onopen: null as (() => void) | null,
  };
  let event!: (value: TurnEvent) => void;
  const connect = vi.fn((_: string, __: string, callback: typeof event) => {
    event = callback;
    return socket;
  });
  const api = {
    agents: async () => [
      { agent_id: "agent-one", name: "Local agent", enabled: true },
    ],
    records: async () => [
      { ...thread, data: { ...thread.data, status: "completed" } },
      userMessage,
    ],
    syncState: async () => ({ pending: 0, conflicts: 0 }),
    connect,
    call: async () => ({ status: "completed", approval: null }),
  } as unknown as ProductApi;
  render(<DesktopWorkspace api={api} identity={identity} section="chat" />);
  fireEvent.click(await screen.findByText("Previous turn"));
  await screen.findByText("product.states.completed");
  act(() => socket.onopen?.());
  expect(socket.send).not.toHaveBeenCalled();
  const input = screen.getByRole("textbox", { name: "product.message" });
  expect(input).toBeEnabled();
  fireEvent.change(input, { target: { value: "Continue our conversation" } });
  fireEvent.click(screen.getByRole("button", { name: "product.send" }));
  expect(connect).toHaveBeenCalledWith(
    "agent-one",
    "thread-one",
    expect.any(Function),
    expect.any(Function),
  );
  expect(JSON.parse(socket.send.mock.calls[0][0])).toEqual({
    type: "start",
    text: "Continue our conversation",
    attachment_ids: [],
  });
  expect(screen.getByText("Earlier question")).toBeInTheDocument();
  act(() => event({ type: "turn.status", data: { status: "completed" } }));
  expect(input).toBeEnabled();
});

it("history is ordered by durable message position and excludes other conversations", () => {
  const other = {
    ...userMessage,
    data: { ...userMessage.data, thread_id: "other" },
  } as LocalRecord;
  const earlier = {
    ...userMessage,
    item_id: "earlier",
    data: { ...userMessage.data, position: 0 },
  } as LocalRecord;
  expect(
    orderedMessages([userMessage, other, earlier], "thread-one").map(
      (r) => r.item_id,
    ),
  ).toEqual(["earlier", "message-one"]);
});

it("groups consecutive tool activity without crossing ordinary messages, turns or threads", () => {
  const base = orderedMessages([userMessage], "thread-one")[0];
  const row = (
    id: string,
    role: "user" | "assistant" | "tool",
    turn = "turn-one",
    threadId = "thread-one",
  ) => ({
    ...base,
    item_id: id,
    data: {
      ...base.data,
      role,
      turn_id: turn,
      thread_id: threadId,
      tool_calls: role === "assistant" ? [{ id, name: "read_file" }] : [],
    },
  });
  const ordinary = row("answer", "assistant");
  ordinary.data.tool_calls = [];
  const groups = groupMessages([
    row("call-one", "assistant"),
    row("result-one", "tool"),
    row("call-two", "assistant"),
    row("result-two", "tool"),
    ordinary,
    row("call-three", "assistant"),
    row("result-three", "tool"),
    row("question", "user"),
    row("call-four", "assistant"),
    row("different-turn", "tool", "turn-two"),
    row("different-thread", "tool", "turn-two", "thread-two"),
  ]);
  expect(
    groups.map((group) =>
      group.kind === "tools"
        ? group.messages.map((message) => message.item_id)
        : group.message.item_id,
    ),
  ).toEqual([
    ["call-one", "result-one", "call-two", "result-two"],
    "answer",
    ["call-three", "result-three"],
    "question",
    ["call-four"],
    ["different-turn"],
    ["different-thread"],
  ]);
});

it("collapses a tool group and preserves its expanded state as more calls arrive", async () => {
  const record = (item_id: string, data: Record<string, unknown>) =>
    ({
      ...userMessage,
      item_id,
      data: { ...userMessage.data, ...data },
    }) as LocalRecord;
  const records = [
    thread,
    userMessage,
    record("tool-call", {
      role: "assistant",
      position: 2,
      content: "Inspecting a file",
      tool_calls: [
        { id: "read-one", name: "read_file", args: { path: "note.txt" } },
      ],
    }),
    record("tool-result", {
      role: "tool",
      position: 3,
      content: "Detailed tool output",
      tool_call_id: "read-one",
      attachments: [{ file_id: "file-one", sha256: "abc" }],
    }),
    record("final-answer", {
      role: "assistant",
      position: 4,
      content: "Here is the answer",
    }),
  ];
  const socket = { close: vi.fn(), send: vi.fn(), readyState: WebSocket.OPEN };
  let event!: (value: TurnEvent) => void;
  const downloadAttachment = vi.fn().mockResolvedValue(undefined);
  const api = {
    agents: async () => [
      { agent_id: "agent-one", name: "Local agent", enabled: true },
    ],
    records: async () => [...records],
    syncState: async () => ({ pending: 0, conflicts: 0 }),
    connect: (_: string, __: string, callback: typeof event) => {
      event = callback;
      return socket;
    },
    call: async () => ({ status: "completed", approval: null }),
    downloadAttachment,
  } as unknown as ProductApi;
  render(<DesktopWorkspace api={api} identity={identity} section="chat" />);
  fireEvent.click(await screen.findByText("Previous turn"));
  expect(await screen.findByText("Here is the answer")).toBeVisible();
  expect(screen.getByText("Earlier question")).toBeVisible();
  const process = screen.getByText("Inspecting a file");
  const output = screen.getByText("Detailed tool output");
  expect(process).not.toBeVisible();
  expect(output).not.toBeVisible();
  const calls = process.closest("details")!;
  const results = output.closest("details")!;
  expect(calls.open).toBe(false);
  expect(results.open).toBe(false);
  expect(results).toBe(calls);
  expect(document.querySelectorAll(".product-tool-details")).toHaveLength(1);
  expect(screen.getByText("product.toolActivity:1")).toBeVisible();
  fireEvent.click(calls.querySelector("summary")!);
  expect(process).toBeVisible();
  expect(output).toBeVisible();
  expect(screen.getByText(/"path": "note.txt"/)).toBeVisible();
  fireEvent.click(
    screen.getByRole("button", { name: "product.downloadAttachment" }),
  );
  expect(downloadAttachment).toHaveBeenCalledWith("tool-result", "file-one");
  records[3] = record("tool-result", {
    ...records[3].data,
    content: "Updated tool output",
  });
  records[4] = record("final-answer", { ...records[4].data, position: 6 });
  records.splice(
    4,
    0,
    record("next-tool-call", {
      role: "assistant",
      position: 4,
      content: "Inspecting another file",
      tool_calls: [
        { id: "read-two", name: "read_file", args: { path: "second.txt" } },
      ],
    }),
    record("next-tool-result", {
      role: "tool",
      position: 5,
      content: "Second tool output",
      tool_call_id: "read-two",
    }),
  );
  act(() => event({ type: "tool_result" }));
  expect(await screen.findByText("Updated tool output")).toBeVisible();
  expect(screen.getByText("Second tool output")).toBeVisible();
  expect(screen.getByText("product.toolActivity:2")).toBeVisible();
  expect(document.querySelectorAll(".product-tool-details")).toHaveLength(1);
  expect(results.open).toBe(true);
  fireEvent.click(results.querySelector("summary")!);
  expect(output).not.toBeVisible();
  expect(socket.send).not.toHaveBeenCalled();
});

it("timestamp rendering uses the platform timezone", () => {
  const stamp = 1700000000;
  expect(date(stamp, identity)).toBe(
    new Date(stamp * 1000).toLocaleString(undefined, {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }),
  );
});
