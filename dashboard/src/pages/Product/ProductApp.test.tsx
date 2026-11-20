import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ProductApp, { cloudAdminPaths, desktopPaths } from "./ProductApp";
import type { Identity } from "../../api/product/contracts";
import type { OctopDesktopBridge } from "../../utils/desktopBridge";

const mock = vi.hoisted(() => ({
  role: "cloud",
  token: vi.fn(),
  login: vi.fn(),
  mfa: vi.fn(),
}));
vi.mock("../../api/product/client", async (original) => ({
  ...(await original<object>()),
  ProductApi: class {
    configuration = async () => ({
      role: mock.role,
      timezone: "Asia/Shanghai",
    });
    setToken = mock.token;
    login = mock.login;
    mfa = mock.mfa;
    dispose = vi.fn();
    onUnauthorized = null;
  },
}));
vi.mock("./Workspace", () => ({
  DesktopWorkspace: () => <div>local-workspace</div>,
}));
vi.mock("./Resources", () => ({ Resources: () => <div>resources-page</div> }));
vi.mock("./Management", () => ({
  Users: () => <div>users-page</div>,
  Devices: () => <div>devices-page</div>,
  Models: () => <div>models-page</div>,
  Usage: () => <div>usage-page</div>,
  Security: () => <div>security-page</div>,
}));
vi.mock("../../hooks/useDesktopChrome", () => ({
  useDesktopChrome: () => ({}),
  DesktopChromeProvider: ({ children }: { children: React.ReactNode }) =>
    children,
}));
vi.mock("../../hooks/useNativeWindowChrome", () => ({
  useNativeWindowChrome: () => undefined,
}));
vi.mock("../../components/DesktopWindowDragBand", () => ({
  default: () => null,
}));
vi.mock("../../components/WindowsAppMenu", () => ({ default: () => null }));
vi.mock("../../context/ThemeContext", () => ({
  ThemeProvider: ({ children }: { children: React.ReactNode }) => children,
  useTheme: () => ({
    isDark: false,
    preference: "system",
    setPreference: vi.fn(),
  }),
}));
vi.mock("react-i18next", () => ({
  useTranslation: () => ({
    t: (key: string) => key,
    i18n: { language: "en", changeLanguage: vi.fn() },
  }),
}));
const identity: Identity = {
  cloud_id: "cloud",
  user: {
    user_id: 1,
    username: "admin",
    role: "admin",
    enabled: true,
    mfa_enabled: true,
  },
  device_id: null,
  binding_generation: null,
  timezone: "Asia/Shanghai",
};

beforeEach(() => {
  delete window.octopDesktop;
  mock.role = "cloud";
  vi.clearAllMocks();
  window.history.replaceState({}, "", "/");
});
afterEach(() => {
  delete window.octopDesktop;
});

it("cloud login completes MFA and exposes only management routes", async () => {
  mock.login.mockResolvedValue({ mfa_token: "challenge", session: null });
  mock.mfa.mockResolvedValue({ access_token: "account", identity });
  render(<ProductApp />);
  fireEvent.change(await screen.findByLabelText("product.username"), {
    target: { value: "admin" },
  });
  fireEvent.change(screen.getByLabelText("product.password"), {
    target: { value: "password" },
  });
  fireEvent.click(screen.getByRole("button", { name: "product.signIn" }));
  fireEvent.change(await screen.findByLabelText("product.mfaCode"), {
    target: { value: "123456" },
  });
  fireEvent.click(screen.getByRole("button", { name: "product.signIn" }));
  await screen.findByText("users-page");
  expect(mock.token).toHaveBeenCalledWith("account");
  expect(screen.queryByText("product.nav.chat")).toBeNull();
  expect(cloudAdminPaths).not.toContain("/chat");
  expect(desktopPaths).not.toContain("/models");
});

it("desktop login delegates secrets and binding to main and never installs a renderer bearer", async () => {
  mock.role = "local";
  const bridge = {
    configuration: async () => ({ cloud_url: "https://cloud.test" }),
    current: async () => null,
    renew: async () => null,
    login: vi.fn(async () => ({ status: "ready_to_bind" })),
    bind: vi.fn(async () => ({
      status: "bound",
      identity: { ...identity, device_id: "current" },
    })),
  };
  window.octopDesktop = {
    cloud: bridge,
    onBackendReady: () => () => undefined,
  } as unknown as OctopDesktopBridge;
  render(<ProductApp />);
  fireEvent.change(await screen.findByLabelText("product.username"), {
    target: { value: "admin" },
  });
  fireEvent.change(screen.getByLabelText("product.password"), {
    target: { value: "secret" },
  });
  fireEvent.click(screen.getByRole("button", { name: "product.signIn" }));
  await waitFor(() => expect(bridge.bind).toHaveBeenCalledWith(false));
  await screen.findByText("local-workspace");
  expect(bridge.login).toHaveBeenCalledWith("admin", "secret");
  expect(mock.token).not.toHaveBeenCalled();
  expect(screen.queryByText("product.nav.users")).toBeNull();
});
