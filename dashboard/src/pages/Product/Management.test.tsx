import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { ProductApi } from "../../api/product/client";
import type { Budget, Identity } from "../../api/product/contracts";
import { Usage } from "./Management";

const identity: Identity = {
  cloud_id: "test-cloud",
  user: {
    user_id: 1,
    username: "admin",
    role: "admin",
    enabled: true,
    mfa_enabled: false,
  },
  device_id: null,
  binding_generation: null,
  timezone: "Asia/Shanghai",
};
const balance: Budget = {
  user_id: 1,
  currency: "USD",
  limit_nanos: "0",
  spent_nanos: "58",
  reserved_nanos: "4800",
  unlimited: true,
};

function api(budgets: Budget[]) {
  return {
    users: vi.fn().mockResolvedValue([identity.user]),
    budgets: vi.fn().mockResolvedValue(budgets),
    requests: vi.fn().mockResolvedValue([]),
  } as unknown as ProductApi;
}

it.each([true, false])(
  "shows unlimited administrator usage and accounting in local=%s",
  async (local) => {
    render(<Usage api={api([balance])} identity={identity} local={local} />);
    await screen.findByText("product.unlimited");
    expect(screen.getByText("product.adminUnlimitedUsage")).toBeInTheDocument();
    expect(screen.getByText("USD 0.000000058")).toBeInTheDocument();
    expect(screen.getByText("USD 0.0000048")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "product.setBudget" }),
    ).toBeNull();
  },
);

it("shows the administrator exemption before the first accounting row exists", async () => {
  const client = api([]);
  render(<Usage api={client} identity={identity} local={false} />);
  await waitFor(() => expect(client.users).toHaveBeenCalled());
  expect(
    await screen.findByText("product.adminUnlimitedUsage"),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "product.setBudget" }),
  ).toBeNull();
});

it("keeps budget management and the stored limit for regular users", async () => {
  const client = api([]);
  const user = {
    ...identity.user,
    user_id: 2,
    username: "consumer",
    role: "user" as const,
  };
  vi.mocked(client.users).mockResolvedValue([identity.user, user]);
  vi.mocked(client.budgets).mockImplementation(async (selected) =>
    selected === 2
      ? [
          {
            ...balance,
            user_id: 2,
            limit_nanos: "1000000000",
            unlimited: false,
          },
        ]
      : [],
  );
  render(<Usage api={client} identity={identity} local={false} />);
  await screen.findByText("product.adminUnlimitedUsage");
  fireEvent.mouseDown(screen.getByRole("combobox"));
  fireEvent.click(await screen.findByText("consumer"));
  expect(await screen.findByText("USD 1")).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: "product.setBudget" }),
  ).toBeInTheDocument();
  expect(screen.queryByText("product.adminUnlimitedUsage")).toBeNull();
});
