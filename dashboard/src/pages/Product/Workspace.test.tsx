import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { DesktopWorkspace, orderedMessages } from "./Workspace";
import { ProductApi } from "../../api/product/client";
import type {
  Identity,
  LocalRecord,
  TurnEvent,
} from "../../api/product/contracts";
import { date } from "./Management";

vi.mock("react-i18next", () => ({
  useTranslation: () => ({ t: (key: string) => key }),
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
