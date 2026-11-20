import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ProductApi, ProductError, parseProductJson } from "./client";
import { money } from "../../pages/Product/Management";
import type { OctopDesktopBridge } from "../../utils/desktopBridge";

vi.mock("../../i18n", () => ({
  default: { language: "zh", t: (key: string) => key },
}));
vi.mock("../../utils/saveBlobAsFile", () => ({ saveBlobAsFile: vi.fn() }));

beforeEach(() => {
  vi.stubGlobal("BASE_URL", "");
  delete window.octopDesktop;
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  delete window.octopDesktop;
});

it("preserves exact nano integers and leaves field-like text in JSON strings untouched", () => {
  const result = parseProductJson(
    '{"limit_nanos":1000000000000000001,"cost_nanos":1,"content":"\\"cost_nanos\\": 123","count":12,"state":null}',
  ) as Record<string, unknown>;
  expect(result.limit_nanos).toBe("1000000000000000001");
  expect(result.cost_nanos).toBe("1");
  expect(result.content).toBe('"cost_nanos": 123');
  expect(result.count).toBe(12);
  expect(money(String(result.limit_nanos), "USD")).toBe(
    "USD 1000000000.000000001",
  );
  expect(money("0", "CNY")).toBe("CNY 0");
});

it("cloud management keeps the account token in memory and sends the selected language", async () => {
  window.localStorage.setItem("auth_token", "old-product-token");
  const fetcher = vi.fn().mockResolvedValue(new Response('{"users": []}'));
  vi.stubGlobal("fetch", fetcher);
  const api = new ProductApi();
  api.setToken("account-only");
  await api.call("/users");
  const [url, init] = fetcher.mock.calls[0] as [string, RequestInit];
  expect(url).toBe("/api/users");
  expect(init.redirect).toBe("error");
  expect(init.headers).toMatchObject({
    Authorization: "Bearer account-only",
    "Accept-Language": "zh",
  });
  expect(window.localStorage.getItem("auth_token")).toBe("old-product-token");
  api.dispose();
  expect((init.signal as AbortSignal).aborted).toBe(true);
});

it("desktop HTTP uses the main-process proxy without exposing any bearer", async () => {
  window.octopDesktop = {} as OctopDesktopBridge;
  const fetcher = vi.fn().mockResolvedValue(new Response("[]"));
  vi.stubGlobal("fetch", fetcher);
  const api = new ProductApi();
  api.setToken("must-never-leave-main");
  await api.agents();
  expect(fetcher.mock.calls[0][0]).toBe("octop://app/api/agents");
  expect(fetcher.mock.calls[0][1].headers.Authorization).toBeUndefined();
  api.dispose();
});

it("expired cloud authorization invalidates the selected UI account, ordinary resource denial does not", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      new Response('{"error":{"code":"RESOURCE_NOT_ALLOWED"}}', {
        status: 403,
      }),
    ),
  );
  const api = new ProductApi();
  api.setToken("account");
  api.onUnauthorized = vi.fn();
  await expect(api.call("/users")).rejects.toMatchObject({
    code: "RESOURCE_NOT_ALLOWED",
  });
  expect(api.onUnauthorized).not.toHaveBeenCalled();
  vi.stubGlobal(
    "fetch",
    vi
      .fn()
      .mockResolvedValue(
        new Response('{"error":{"code":"AUTH_FAILED"}}', { status: 401 }),
      ),
  );
  await expect(api.call("/users")).rejects.toBeInstanceOf(ProductError);
  expect(api.onUnauthorized).toHaveBeenCalledOnce();
  api.dispose();
});
