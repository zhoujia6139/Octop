import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Alert,
  Button,
  Card,
  ConfigProvider,
  Form,
  Input,
  Layout,
  Menu,
  Modal,
  Select,
  Space,
  Spin,
  Typography,
  message,
  theme,
} from "antd";
import zhCN from "antd/locale/zh_CN";
import enUS from "antd/locale/en_US";
import {
  BrowserRouter,
  Navigate,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";
import { useTranslation } from "react-i18next";
import { ProductApi, errorCode, errorText } from "../../api/product/client";
import type { Identity, Role, Token } from "../../api/product/contracts";
import { getDesktopBridge } from "../../utils/desktopBridge";
import { ensureLocaleBundle } from "../../i18n";
import { storeUiLocale } from "../../utils/localePrefs";
import { routerBasename } from "../../utils/publicUrl";
import { ThemeProvider, useTheme } from "../../context/ThemeContext";
import {
  DesktopChromeProvider,
  useDesktopChrome,
} from "../../hooks/useDesktopChrome";
import { useNativeWindowChrome } from "../../hooks/useNativeWindowChrome";
import DesktopWindowDragBand from "../../components/DesktopWindowDragBand";
import WindowsAppMenu from "../../components/WindowsAppMenu";
import { AntdAppProvider } from "../../components/AntdAppProvider";
import GlobalErrorBoundary from "../../components/ErrorBoundary";
import { DesktopWorkspace } from "./Workspace";
import { Resources } from "./Resources";
import { Users, Devices, Models, Usage, Security } from "./Management";
import "../../styles/theme-vars.css";
import "../../styles/layout.css";
import "./product.css";

export const desktopPaths = [
  "/chat",
  "/resources",
  "/skills",
  "/files",
  "/sync",
  "/usage",
] as const;
export const cloudAdminPaths = [
  "/users",
  "/devices",
  "/resources",
  "/models",
  "/usage",
  "/security",
] as const;
export const cloudUserPaths = ["/devices", "/usage", "/security"] as const;

function Login({
  api,
  role,
  onLogin,
}: {
  api: ProductApi;
  role: Role;
  onLogin(identity: Identity): void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [mfa, setMfa] = useState<string | null>(null);
  const [bind, setBind] = useState(false);
  const [origin, setOrigin] = useState("");
  const [configured, setConfigured] = useState(role === "cloud");
  const bridge = getDesktopBridge()?.cloud;

  useEffect(() => {
    if (role === "local" && bridge) {
      void bridge
        .configuration()
        .then((config) => {
          setOrigin(config.cloud_url ?? "");
          setConfigured(!!config.cloud_url);
        })
        .catch((err) => setError(errorText(err)));
    }
  }, [role, bridge]);

  const tokenLogin = (session: Token) => {
    api.setToken(session.access_token);
    onLogin(session.identity);
  };
  const bindDevice = async (replace = false) => {
    if (!bridge) return;
    try {
      const result = await bridge.bind(replace);
      if (result.identity) onLogin(result.identity);
    } catch (err) {
      if (errorCode(err) === "DEVICE_BINDING_EXISTS" && !replace) {
        Modal.confirm({
          title: t("product.replaceDevice"),
          content: t("product.replaceDeviceHelp"),
          onOk: () => bindDevice(true),
        });
      } else throw err;
    }
  };

  const submit = async (values: {
    username?: string;
    password?: string;
    code?: string;
  }) => {
    setBusy(true);
    setError("");
    try {
      if (role === "local") {
        if (!bridge) return;
        if (!configured) {
          await bridge.configure(origin);
          setConfigured(true);
          return;
        }
        if (bind) {
          await bindDevice();
          return;
        }
        const result = mfa
          ? await bridge.verifyMfa(values.code ?? "")
          : await bridge.login(values.username ?? "", values.password ?? "");
        if (result.status === "mfa_required") setMfa("desktop");
        else if (result.status === "ready_to_bind") {
          setMfa(null);
          setBind(true);
          await bindDevice();
        }
      } else if (mfa) {
        tokenLogin(await api.mfa(mfa, values.code ?? ""));
      } else {
        const result = await api.login(
          values.username ?? "",
          values.password ?? "",
        );
        if (result.mfa_token) setMfa(result.mfa_token);
        else if (result.session) tokenLogin(result.session);
      }
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="product-login">
      <Card
        title={t(
          role === "local" ? "product.desktopLogin" : "product.cloudLogin",
        )}
      >
        {error && <Alert type="error" message={error} showIcon />}
        {role === "local" && !bridge ? (
          <Alert type="info" message={t("product.desktopRequired")} />
        ) : (
          <>
            <Typography.Paragraph type="secondary">
              {t(
                role === "local"
                  ? "product.localExecution"
                  : "product.adminOnly",
              )}
            </Typography.Paragraph>
            <Form
              key={`${configured}-${mfa}-${bind}`}
              layout="vertical"
              onFinish={submit}
              preserve={false}
              autoComplete="off"
            >
              {role === "local" && !configured ? (
                <Form.Item label={t("product.cloudAddress")} required>
                  <Input
                    value={origin}
                    onChange={(e) => setOrigin(e.target.value)}
                    placeholder="https://cloud.example.com"
                  />
                </Form.Item>
              ) : bind ? (
                <Alert type="info" message={t("product.bindHelp")} />
              ) : mfa ? (
                <Form.Item
                  name="code"
                  label={t("product.mfaCode")}
                  rules={[{ required: true }]}
                >
                  <Input autoFocus autoComplete="one-time-code" />
                </Form.Item>
              ) : (
                <>
                  <Form.Item
                    name="username"
                    label={t("product.username")}
                    rules={[{ required: true }]}
                  >
                    <Input autoComplete="username" />
                  </Form.Item>
                  <Form.Item
                    name="password"
                    label={t("product.password")}
                    rules={[{ required: true }]}
                  >
                    <Input.Password autoComplete="current-password" />
                  </Form.Item>
                </>
              )}
              <Button type="primary" htmlType="submit" loading={busy} block>
                {t(
                  !configured
                    ? "product.connect"
                    : bind
                    ? "product.bind"
                    : "product.signIn",
                )}
              </Button>
              {(mfa || bind) && (
                <Button
                  block
                  type="text"
                  onClick={() => {
                    setMfa(null);
                    setBind(false);
                  }}
                >
                  {t("product.back")}
                </Button>
              )}
            </Form>
            {role === "local" && configured && (
              <Button
                type="link"
                onClick={() => {
                  setConfigured(false);
                  setMfa(null);
                  setBind(false);
                }}
              >
                {origin || t("product.cloudAddress")}
              </Button>
            )}
          </>
        )}
      </Card>
    </main>
  );
}

function Product() {
  const { t, i18n } = useTranslation();
  const { isDark, preference, setPreference } = useTheme();
  const chrome = useDesktopChrome();
  useNativeWindowChrome();
  const [api, setApi] = useState(() => new ProductApi());
  const selectedApi = useRef(api);
  const epoch = useRef(0);
  const [role, setRole] = useState<Role | null>(null);
  const [identity, setIdentity] = useState<Identity | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [paused, setPaused] = useState(false);
  const location = useLocation();
  const navigate = useNavigate();

  const reset = useCallback(() => {
    if (selectedApi.current !== api) return;
    epoch.current++;
    setLoading(true);
    setError("");
    api.dispose();
    const next = new ProductApi();
    selectedApi.current = next;
    setApi(next);
    setIdentity(null);
  }, [api]);

  useEffect(() => {
    api.onUnauthorized = reset;
    return () => {
      api.onUnauthorized = null;
    };
  }, [api, reset]);
  const refresh = useCallback(async () => {
    const current = ++epoch.current;
    const selected = () =>
      current === epoch.current && selectedApi.current === api;
    setLoading(true);
    setError("");
    try {
      const config = await api.configuration();
      if (!selected()) return;
      setRole(config.role);
      if (config.role === "local") {
        const bridge = getDesktopBridge()?.cloud;
        const current = await bridge?.current();
        const identity = current ?? (await bridge?.renew()) ?? null;
        if (selected()) setIdentity(identity);
      }
    } catch (err) {
      if (selected()) setError(errorText(err));
    } finally {
      if (selected()) setLoading(false);
    }
  }, [api]);
  useEffect(() => {
    void refresh();
    return () => api.dispose();
  }, [api, refresh]);
  useEffect(
    () =>
      getDesktopBridge()?.onBackendReady(({ ok }) => {
        setPaused(!ok);
        if (!ok) reset();
        else void refresh();
      }),
    [refresh, reset],
  );

  const logout = async () => {
    try {
      if (role === "local") {
        const result = await getDesktopBridge()?.cloud?.logout();
        if (result && !result.revoked)
          void message.warning(t("product.revocationPending"));
      } else await api.logout();
    } catch (err) {
      void message.error(errorText(err));
    } finally {
      reset();
    }
  };

  const paths = useMemo(
    () =>
      role === "local"
        ? desktopPaths
        : identity?.user.role === "admin"
        ? cloudAdminPaths
        : cloudUserPaths,
    [role, identity],
  );
  return (
    <ConfigProvider
      locale={i18n.language.startsWith("zh") ? zhCN : enUS}
      theme={{
        algorithm: isDark ? theme.darkAlgorithm : theme.defaultAlgorithm,
      }}
    >
      <AntdAppProvider>
        <DesktopChromeProvider value={chrome}>
          <DesktopWindowDragBand />
          <WindowsAppMenu />
          <div className="product-root">
            <Layout>
              <Layout.Header className="product-header">
                <Typography.Title level={4}>Octop</Typography.Title>
                <Space wrap>
                  <Select
                    aria-label={t("product.language")}
                    value={i18n.language.startsWith("zh") ? "zh" : "en"}
                    onChange={(value) => {
                      const locale = value === "zh" ? "zh" : "en";
                      void ensureLocaleBundle(locale).then(() => {
                        storeUiLocale(locale);
                        return i18n.changeLanguage(locale);
                      });
                    }}
                    options={[
                      { value: "zh", label: "中文" },
                      { value: "en", label: "English" },
                    ]}
                  />
                  <Select
                    aria-label={t("product.theme")}
                    value={preference}
                    onChange={setPreference}
                    options={["system", "light", "dark"].map((value) => ({
                      value,
                      label: t(`product.${value}`),
                    }))}
                  />
                  {identity && (
                    <>
                      <Typography.Text>
                        {identity.user.username}
                      </Typography.Text>
                      <Button
                        onClick={() => {
                          void logout();
                        }}
                      >
                        {t("product.signOut")}
                      </Button>
                    </>
                  )}
                </Space>
              </Layout.Header>
              {paused ? (
                <Alert type="info" message={t("product.paused")} />
              ) : loading ? (
                <div className="product-loading">
                  <Spin />
                </div>
              ) : error ? (
                <Alert
                  type="error"
                  message={error}
                  action={
                    <Space>
                      <Button
                        onClick={() => {
                          void refresh();
                        }}
                      >
                        {t("product.retry")}
                      </Button>
                      {role === "local" && (
                        <Button
                          onClick={() => {
                            void logout();
                          }}
                        >
                          {t("product.signOut")}
                        </Button>
                      )}
                    </Space>
                  }
                />
              ) : !role ? null : !identity ? (
                <Login
                  key={role}
                  api={api}
                  role={role}
                  onLogin={(identity) => {
                    if (selectedApi.current === api) {
                      epoch.current++;
                      setIdentity(identity);
                    }
                  }}
                />
              ) : (
                <Layout>
                  <Layout.Sider width={190} theme={isDark ? "dark" : "light"}>
                    <Menu
                      selectedKeys={[location.pathname]}
                      items={paths.map((path) => ({
                        key: path,
                        label: t(`product.nav.${path.slice(1)}`),
                      }))}
                      onClick={({ key }) => navigate(key)}
                    />
                  </Layout.Sider>
                  <Layout.Content className="product-content">
                    <Routes>
                      {role === "local" ? (
                        <>
                          <Route
                            path="/chat"
                            element={
                              <DesktopWorkspace
                                api={api}
                                identity={identity}
                                section="chat"
                              />
                            }
                          />
                          <Route
                            path="/files"
                            element={
                              <DesktopWorkspace
                                api={api}
                                identity={identity}
                                section="files"
                              />
                            }
                          />
                          <Route
                            path="/sync"
                            element={
                              <DesktopWorkspace
                                api={api}
                                identity={identity}
                                section="sync"
                              />
                            }
                          />
                          <Route
                            path="/skills"
                            element={
                              <Resources api={api} kind="skill" admin={false} />
                            }
                          />
                          <Route
                            path="/resources"
                            element={
                              <Resources api={api} kind="agent" admin={false} />
                            }
                          />
                        </>
                      ) : (
                        <>
                          {identity.user.role === "admin" && (
                            <>
                              <Route
                                path="/users"
                                element={<Users api={api} />}
                              />
                              <Route
                                path="/resources"
                                element={
                                  <Resources api={api} admin kind="agent" />
                                }
                              />
                              <Route
                                path="/models"
                                element={<Models api={api} />}
                              />
                            </>
                          )}
                          <Route
                            path="/devices"
                            element={<Devices api={api} identity={identity} />}
                          />
                          <Route
                            path="/security"
                            element={
                              <Security
                                api={api}
                                identity={identity}
                                onSessionEnded={reset}
                              />
                            }
                          />
                        </>
                      )}
                      <Route
                        path="/usage"
                        element={
                          <Usage
                            api={api}
                            identity={identity}
                            local={role === "local"}
                          />
                        }
                      />
                      <Route
                        path="*"
                        element={<Navigate to={paths[0]} replace />}
                      />
                    </Routes>
                  </Layout.Content>
                </Layout>
              )}
            </Layout>
          </div>
        </DesktopChromeProvider>
      </AntdAppProvider>
    </ConfigProvider>
  );
}

export default function ProductApp() {
  return (
    <BrowserRouter basename={routerBasename()} useTransitions={false}>
      <GlobalErrorBoundary>
        <ThemeProvider>
          <Product />
        </ThemeProvider>
      </GlobalErrorBoundary>
    </BrowserRouter>
  );
}
