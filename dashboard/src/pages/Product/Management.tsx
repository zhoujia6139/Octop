import { useCallback, useState } from "react";
import {
  Alert,
  Button,
  Card,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { QRCodeSVG } from "qrcode.react";
import { useTranslation } from "react-i18next";
import { ProductApi, errorText, newId } from "../../api/product/client";
import type {
  Budget,
  Identity,
  InstallReport,
  ModelDefinition,
  ModelRequest,
  Provider,
  User,
} from "../../api/product/contracts";
import { formatServerDateTime } from "../../utils/formatMessageTime";
import { useProductData } from "./useProductData";

/** Monetary values arrive as integer nanos; do not round nanos via floats. */
export function money(nanos: string | number, currency: string): string {
  const value = BigInt(nanos);
  const negative = value < 0n;
  const absolute = negative ? -value : value;
  const whole = absolute / 1_000_000_000n;
  const fraction = (absolute % 1_000_000_000n)
    .toString()
    .padStart(9, "0")
    .replace(/0+$/, "");
  return `${currency} ${negative ? "-" : ""}${whole}${
    fraction ? "." + fraction : ""
  }`;
}

export function date(seconds: number, identity: Identity) {
  return formatServerDateTime(seconds, identity.timezone);
}

export function Users({ api }: { api: ProductApi }) {
  const { t } = useTranslation();
  const state = useProductData(api.users, []);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState("");
  return (
    <div className="product-stack">
      <Typography.Title level={3}>{t("product.nav.users")}</Typography.Title>
      {(error || state.error) && (
        <Alert type="error" message={error || state.error} />
      )}
      <Space>
        <Button type="primary" onClick={() => setCreating(true)}>
          {t("product.createUser")}
        </Button>
        <Button
          onClick={() => {
            void state.reload();
          }}
        >
          {t("product.refresh")}
        </Button>
      </Space>
      <Table
        rowKey="user_id"
        loading={state.busy}
        dataSource={state.data}
        columns={[
          { title: t("product.username"), dataIndex: "username" },
          {
            title: t("product.role"),
            dataIndex: "role",
            render: (role: string) => t(`product.${role}`),
          },
          {
            title: t("product.mfa"),
            dataIndex: "mfa_enabled",
            render: (value: boolean) => (
              <Tag>{t(value ? "product.enabled" : "product.disabled")}</Tag>
            ),
          },
          {
            title: t("product.actions"),
            key: "actions",
            render: (_, row) => (
              <Space>
                <Select
                  value={row.role}
                  options={[
                    { value: "admin", label: t("product.admin") },
                    { value: "user", label: t("product.user") },
                  ]}
                  onChange={async (role: string) => {
                    try {
                      await api.call(`/users/${row.user_id}`, "PUT", {
                        role,
                        enabled: row.enabled,
                      });
                      await state.reload();
                    } catch (err) {
                      setError(errorText(err));
                    }
                  }}
                />
                <Popconfirm
                  title={t("product.revokeSessionsHelp")}
                  onConfirm={async () => {
                    try {
                      await api.call(`/users/${row.user_id}`, "PUT", {
                        role: row.role,
                        enabled: !row.enabled,
                      });
                      await state.reload();
                    } catch (err) {
                      setError(errorText(err));
                    }
                  }}
                >
                  <Button danger={row.enabled}>
                    {t(row.enabled ? "product.disable" : "product.enable")}
                  </Button>
                </Popconfirm>
              </Space>
            ),
          },
        ]}
      />
      <Modal
        open={creating}
        title={t("product.createUser")}
        onCancel={() => setCreating(false)}
        footer={null}
        destroyOnHidden
      >
        <Form
          layout="vertical"
          initialValues={{ role: "user" }}
          onFinish={async (values: {
            username: string;
            password: string;
            role: string;
          }) => {
            try {
              await api.call("/users", "POST", values);
              setCreating(false);
              await state.reload();
            } catch (err) {
              setError(errorText(err));
            }
          }}
        >
          <Form.Item
            label={t("product.username")}
            name="username"
            rules={[{ required: true }]}
          >
            <Input autoComplete="off" />
          </Form.Item>
          <Form.Item
            label={t("product.password")}
            name="password"
            rules={[{ required: true, min: 8 }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          <Form.Item label={t("product.role")} name="role">
            <Select
              options={[
                { value: "admin", label: t("product.admin") },
                { value: "user", label: t("product.user") },
              ]}
            />
          </Form.Item>
          <Button htmlType="submit" type="primary">
            {t("product.save")}
          </Button>
        </Form>
      </Modal>
    </div>
  );
}

export function Devices({
  api,
  identity,
}: {
  api: ProductApi;
  identity: Identity;
}) {
  const { t } = useTranslation();
  const [user, setUser] = useState(identity.user.user_id);
  const admin = identity.user.role === "admin";
  const users = useProductData(
    useCallback(
      () => (admin ? api.users() : Promise.resolve([identity.user])),
      [api, admin, identity],
    ),
    [],
  );
  const load = useCallback(
    async () => ({
      devices: await api.devices(admin ? user : undefined),
      reports: admin ? await api.reports(user) : [],
    }),
    [api, user, admin],
  );
  const state = useProductData<{
    devices: Awaited<ReturnType<ProductApi["devices"]>>;
    reports: InstallReport[];
  }>(load, { devices: [], reports: [] });
  return (
    <div className="product-stack">
      <Typography.Title level={3}>{t("product.nav.devices")}</Typography.Title>
      <Alert type="info" message={t("product.singleDeviceHelp")} />
      {(state.error || users.error) && (
        <Alert type="error" message={state.error || users.error} />
      )}
      <Space>
        {admin && (
          <Select
            aria-label={t("product.username")}
            value={user}
            onChange={setUser}
            options={users.data.map((u) => ({
              value: u.user_id,
              label: u.username,
            }))}
          />
        )}
        <Button
          onClick={() => {
            void state.reload();
          }}
        >
          {t("product.refresh")}
        </Button>
      </Space>
      <Table
        rowKey="device_id"
        dataSource={state.data.devices}
        loading={state.busy}
        pagination={false}
        columns={[
          { title: t("product.name"), dataIndex: "label" },
          {
            title: t("product.lastSeen"),
            dataIndex: "last_seen_at",
            render: (value: number) => date(value, identity),
          },
          {
            title: t("product.state"),
            dataIndex: "revoked",
            render: (value: boolean) =>
              t(value ? "product.revoked" : "product.enabled"),
          },
          {
            title: t("product.actions"),
            key: "actions",
            render: (_, row) => (
              <Popconfirm
                title={t("product.revokeDevice")}
                description={t("product.revokeSessionsHelp")}
                onConfirm={async () => {
                  try {
                    await api.revoke(row.device_id, admin ? user : undefined);
                    await state.reload();
                  } catch (err) {
                    state.setError(errorText(err));
                  }
                }}
              >
                <Button danger disabled={row.revoked}>
                  {t("product.revokeDevice")}
                </Button>
              </Popconfirm>
            ),
          },
        ]}
      />
      {admin && (
        <Card title={t("product.installReports")}>
          <Table
            rowKey="resource_id"
            dataSource={state.data.reports}
            columns={[
              { title: t("product.resource"), dataIndex: "resource_id" },
              {
                title: t("product.version"),
                key: "versions",
                render: (_, row) =>
                  t("product.installedVersion", {
                    version: row.installed_revision ?? "—",
                    desired: row.desired_revision ?? "—",
                  }),
              },
              {
                title: t("product.state"),
                key: "state",
                render: (_, row) => (
                  <Space direction="vertical">
                    <Tag>{t(`product.states.${row.state}`)}</Tag>
                    {row.error_code && (
                      <Typography.Text type="danger">
                        {t(`apiErrors.${row.error_code}`, {
                          defaultValue: t("product.requestFailed"),
                        })}
                      </Typography.Text>
                    )}
                  </Space>
                ),
              },
            ]}
          />
        </Card>
      )}
    </div>
  );
}

function ProviderEditor({
  api,
  provider,
  onDone,
}: {
  api: ProductApi;
  provider: Provider | null;
  onDone(): void;
}) {
  const { t } = useTranslation();
  const [error, setError] = useState("");
  return (
    <Modal
      open
      title={t("product.providerConfig")}
      onCancel={onDone}
      footer={null}
      destroyOnHidden
    >
      <Alert type="info" message={t("product.providerKeyHelp")} />
      {error && <Alert type="error" message={error} />}
      <Form
        layout="vertical"
        initialValues={
          provider ?? { provider_id: newId(), free_rejection_statuses: [] }
        }
        onFinish={async (values: {
          provider_id: string;
          base_url: string;
          api_key: string;
          free_rejection_statuses: number[];
        }) => {
          try {
            await api.call("/model-admin/providers", "PUT", values);
            onDone();
          } catch (err) {
            setError(errorText(err));
          }
        }}
      >
        <Form.Item
          name="provider_id"
          label={t("product.identifier")}
          rules={[{ required: true }]}
        >
          <Input disabled={!!provider} />
        </Form.Item>
        <Form.Item
          name="base_url"
          label={t("product.providerAddress")}
          rules={[{ required: true }]}
        >
          <Input placeholder="https://api.example.com/v1" />
        </Form.Item>
        <Form.Item
          name="api_key"
          label={t("product.providerKey")}
          rules={[{ required: true }]}
        >
          <Input.Password autoComplete="new-password" />
        </Form.Item>
        <Form.Item
          name="free_rejection_statuses"
          label={t("product.freeRejections")}
          extra={t("product.freeRejectionsHelp")}
        >
          <Select
            mode="multiple"
            options={[400, 401, 403, 404, 422, 429].map((value) => ({
              value,
              label: String(value),
            }))}
          />
        </Form.Item>
        <Button type="primary" htmlType="submit">
          {t("product.save")}
        </Button>
      </Form>
    </Modal>
  );
}

function ModelEditor({
  api,
  model,
  providers,
  onDone,
}: {
  api: ProductApi;
  model: ModelDefinition | null;
  providers: Provider[];
  onDone(): void;
}) {
  const { t } = useTranslation();
  const [error, setError] = useState("");
  return (
    <Modal
      open
      title={t("product.modelConfig")}
      onCancel={onDone}
      footer={null}
      width={760}
      destroyOnHidden
    >
      {error && <Alert type="error" message={error} />}
      <Form
        layout="vertical"
        initialValues={
          model
            ? {
                ...model,
                model_name: model.model_id.slice(9),
                ...model.rate,
                routes: model.routes,
              }
            : {
                model_name: "",
                protocol: "chat_completions",
                enabled: true,
                tools: true,
                input_modalities: ["text"],
                currency: "USD",
                uncached_input: 0,
                cache_read: 0,
                cache_write: 0,
                output: 0,
                input_token_ceiling: 16384,
                output_token_ceiling: 4096,
                routes: [{}],
              }
        }
        onFinish={async (values: Record<string, unknown>) => {
          try {
            const protocol = values.protocol;
            await api.call("/model-admin/models", "PUT", {
              model_id: "platform/" + String(values.model_name),
              name: values.name,
              protocol,
              enabled: values.enabled,
              tools: protocol === "embeddings" ? false : values.tools,
              input_modalities:
                protocol === "embeddings" ? ["text"] : values.input_modalities,
              input_token_ceiling: values.input_token_ceiling,
              output_token_ceiling:
                protocol === "embeddings" ? 0 : values.output_token_ceiling,
              routes: values.routes,
              rate: {
                revision: newId(),
                currency: values.currency,
                effective_at: Math.floor(Date.now() / 1000),
                uncached_input: values.uncached_input,
                cache_read: values.cache_read,
                cache_write: values.cache_write,
                output: values.output,
              },
            });
            onDone();
          } catch (err) {
            setError(errorText(err));
          }
        }}
      >
        <Form.Item
          name="model_name"
          label={t("product.logicalModel")}
          rules={[
            { required: true, pattern: /^[A-Za-z0-9][A-Za-z0-9_-]{1,63}$/ },
          ]}
        >
          <Input addonBefore="platform/" disabled={!!model} />
        </Form.Item>
        <Form.Item
          name="name"
          label={t("product.name")}
          rules={[{ required: true }]}
        >
          <Input />
        </Form.Item>
        <Space wrap>
          <Form.Item name="protocol" label={t("product.protocol")}>
            <Select
              style={{ width: 190 }}
              options={[
                "chat_completions",
                "responses",
                "messages",
                "embeddings",
              ].map((value) => ({ value, label: value }))}
            />
          </Form.Item>
          <Form.Item
            name="input_modalities"
            label={t("product.inputModalities")}
          >
            <Select
              mode="multiple"
              style={{ width: 160 }}
              options={[
                { value: "text", label: t("product.text") },
                { value: "image", label: t("product.image") },
              ]}
            />
          </Form.Item>
          <Form.Item
            name="tools"
            label={t("product.tools")}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Form.Item
            name="enabled"
            label={t("product.enabled")}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
        </Space>
        <Space>
          <Form.Item
            name="input_token_ceiling"
            label={t("product.inputCeiling")}
            rules={[{ required: true }]}
          >
            <InputNumber min={1} max={10000000} precision={0} />
          </Form.Item>
          <Form.Item
            name="output_token_ceiling"
            label={t("product.outputCeiling")}
            rules={[{ required: true }]}
          >
            <InputNumber min={0} max={10000000} precision={0} />
          </Form.Item>
        </Space>
        <Typography.Paragraph type="secondary">
          {t("product.reservationHelp")}
        </Typography.Paragraph>
        <Form.List name="routes">
          {(fields, { add, remove }) => (
            <>
              <Typography.Title level={5}>
                {t("product.routes")}
              </Typography.Title>
              {fields.map((field) => (
                <Space key={field.key} align="baseline">
                  <Form.Item
                    name={[field.name, "provider_id"]}
                    rules={[{ required: true }]}
                  >
                    <Select
                      placeholder={t("product.provider")}
                      style={{ width: 210 }}
                      options={providers.map((p) => ({
                        value: p.provider_id,
                        label: p.provider_id,
                      }))}
                    />
                  </Form.Item>
                  <Form.Item
                    name={[field.name, "upstream_model"]}
                    rules={[{ required: true }]}
                  >
                    <Input placeholder={t("product.upstreamModel")} />
                  </Form.Item>
                  {fields.length > 1 && (
                    <Button danger onClick={() => remove(field.name)}>
                      {t("product.delete")}
                    </Button>
                  )}
                </Space>
              ))}
              <Button disabled={fields.length >= 4} onClick={() => add()}>
                {t("product.addRoute")}
              </Button>
            </>
          )}
        </Form.List>
        <Form.Item
          name="currency"
          label={t("product.currency")}
          rules={[{ required: true, pattern: /^[A-Z]{3}$/ }]}
        >
          <Input maxLength={3} />
        </Form.Item>
        <Typography.Paragraph>{t("product.rateHelp")}</Typography.Paragraph>
        <Space wrap>
          {["uncached_input", "cache_read", "cache_write", "output"].map(
            (field) => (
              <Form.Item
                key={field}
                name={field}
                label={t(`product.rate.${field}`)}
                rules={[{ required: true }]}
              >
                <InputNumber min={0} max={1000000000000} precision={0} />
              </Form.Item>
            ),
          )}
        </Space>
        <Form.Item>
          <Button type="primary" htmlType="submit">
            {t("product.save")}
          </Button>
        </Form.Item>
      </Form>
    </Modal>
  );
}

export function Models({ api }: { api: ProductApi }) {
  const { t } = useTranslation();
  const state = useProductData(
    useCallback(
      async () => ({
        providers: await api.providers(),
        models: await api.modelDefinitions(),
        users: await api.users(),
      }),
      [api],
    ),
    { providers: [], models: [], users: [] } as {
      providers: Provider[];
      models: ModelDefinition[];
      users: User[];
    },
  );
  const [provider, setProvider] = useState<Provider | null | undefined>();
  const [model, setModel] = useState<ModelDefinition | null | undefined>();
  const [grant, setGrant] = useState<ModelDefinition | null>(null);
  return (
    <div className="product-stack">
      <Typography.Title level={3}>{t("product.nav.models")}</Typography.Title>
      {state.error && <Alert type="error" message={state.error} />}
      <Space>
        <Button onClick={() => setProvider(null)}>
          {t("product.addProvider")}
        </Button>
        <Button
          type="primary"
          disabled={!state.data.providers.length}
          onClick={() => setModel(null)}
        >
          {t("product.addModel")}
        </Button>
        <Button
          onClick={() => {
            void state.reload();
          }}
        >
          {t("product.refresh")}
        </Button>
      </Space>
      <Table
        rowKey="provider_id"
        dataSource={state.data.providers}
        pagination={false}
        columns={[
          { title: t("product.provider"), dataIndex: "provider_id" },
          { title: t("product.providerAddress"), dataIndex: "base_url" },
          {
            title: t("product.actions"),
            key: "actions",
            render: (_, row) => (
              <Button onClick={() => setProvider(row)}>
                {t("product.edit")}
              </Button>
            ),
          },
        ]}
      />
      <Table
        rowKey="model_id"
        loading={state.busy}
        dataSource={state.data.models}
        columns={[
          { title: t("product.name"), dataIndex: "name" },
          { title: t("product.logicalModel"), dataIndex: "model_id" },
          { title: t("product.protocol"), dataIndex: "protocol" },
          {
            title: t("product.enabled"),
            dataIndex: "enabled",
            render: (value: boolean) =>
              t(value ? "product.enabled" : "product.disabled"),
          },
          {
            title: t("product.actions"),
            key: "actions",
            render: (_, row) => (
              <Space>
                <Button onClick={() => setModel(row)}>
                  {t("product.edit")}
                </Button>
                <Button onClick={() => setGrant(row)}>
                  {t("product.grant")}
                </Button>
              </Space>
            ),
          },
        ]}
      />
      {provider !== undefined && (
        <ProviderEditor
          api={api}
          provider={provider}
          onDone={() => {
            setProvider(undefined);
            void state.reload();
          }}
        />
      )}
      {model !== undefined && (
        <ModelEditor
          api={api}
          model={model}
          providers={state.data.providers}
          onDone={() => {
            setModel(undefined);
            void state.reload();
          }}
        />
      )}
      {grant && (
        <Modal
          open
          title={t("product.grant")}
          onCancel={() => setGrant(null)}
          footer={null}
          destroyOnHidden
        >
          <Form
            layout="vertical"
            initialValues={{ allowed: true }}
            onFinish={async (values: { user: number; allowed: boolean }) => {
              try {
                await api.call(
                  `/model-admin/users/${
                    values.user
                  }/grants/${grant.model_id.slice(9)}`,
                  "PUT",
                  { allowed: values.allowed },
                );
                setGrant(null);
                void message.success(t("product.saved"));
              } catch (err) {
                state.setError(errorText(err));
              }
            }}
          >
            <Form.Item
              name="user"
              label={t("product.username")}
              rules={[{ required: true }]}
            >
              <Select
                options={state.data.users.map((u) => ({
                  value: u.user_id,
                  label: u.username,
                }))}
              />
            </Form.Item>
            <Form.Item
              name="allowed"
              label={t("product.allowed")}
              valuePropName="checked"
            >
              <Switch />
            </Form.Item>
            <Button htmlType="submit" type="primary">
              {t("product.save")}
            </Button>
          </Form>
        </Modal>
      )}
    </div>
  );
}

export function Usage({
  api,
  identity,
  local,
}: {
  api: ProductApi;
  identity: Identity;
  local: boolean;
}) {
  const { t } = useTranslation();
  const admin = !local && identity.user.role === "admin";
  const [selected, setSelected] = useState(identity.user.user_id);
  const users = useProductData(
    useCallback(
      () => (admin ? api.users() : Promise.resolve([identity.user])),
      [api, admin, identity],
    ),
    [],
  );
  const load = useCallback(
    async () => ({
      budgets: await api.budgets(admin ? selected : undefined),
      requests: await api.requests(admin ? selected : undefined),
    }),
    [api, selected, admin],
  );
  const state = useProductData<{ budgets: Budget[]; requests: ModelRequest[] }>(
    load,
    { budgets: [], requests: [] },
  );
  const [budget, setBudget] = useState(false);
  const [reconcile, setReconcile] = useState<string | null>(null);
  const selectedUser = admin
    ? users.data.find((user) => user.user_id === selected)
    : identity.user;
  const unlimited = selectedUser?.role === "admin";
  return (
    <div className="product-stack">
      <Typography.Title level={3}>{t("product.nav.usage")}</Typography.Title>
      <Alert type="info" message={t("product.usageHelp")} />
      {unlimited && (
        <Alert type="info" message={t("product.adminUnlimitedUsage")} />
      )}
      {state.error && <Alert type="error" message={state.error} />}
      <Space>
        {admin && (
          <Select
            aria-label={t("product.username")}
            value={selected}
            onChange={setSelected}
            options={users.data.map((u) => ({
              value: u.user_id,
              label: u.username,
            }))}
          />
        )}
        <Button
          onClick={() => {
            void state.reload();
          }}
        >
          {t("product.refresh")}
        </Button>
        {admin && selectedUser?.role === "user" && (
          <Button onClick={() => setBudget(true)}>
            {t("product.setBudget")}
          </Button>
        )}
      </Space>
      <Table
        rowKey="currency"
        dataSource={state.data.budgets}
        pagination={false}
        columns={[
          {
            title: t("product.limit"),
            key: "limit",
            render: (_, row) =>
              row.unlimited
                ? t("product.unlimited")
                : money(row.limit_nanos, row.currency),
          },
          {
            title: t("product.spent"),
            key: "spent",
            render: (_, row) => money(row.spent_nanos, row.currency),
          },
          {
            title: t("product.reserved"),
            key: "reserved",
            render: (_, row) => money(row.reserved_nanos, row.currency),
          },
        ]}
      />
      <Table
        rowKey="request_id"
        dataSource={state.data.requests}
        loading={state.busy}
        columns={[
          { title: t("product.model"), dataIndex: "model_id" },
          {
            title: t("product.time"),
            dataIndex: "created_at",
            render: (value: number) => date(value, identity),
          },
          {
            title: t("product.state"),
            dataIndex: "state",
            render: (value: string) =>
              t(`product.states.${value}`, { defaultValue: value }),
          },
        ]}
        expandable={{
          expandedRowRender: (row) => (
            <Space direction="vertical">
              {row.attempts.map((attempt) => (
                <Card key={attempt.attempt_id} size="small">
                  <Space wrap>
                    <Tag>
                      {t(`product.states.${attempt.state}`, {
                        defaultValue: attempt.state,
                      })}
                    </Tag>
                    <Typography.Text>
                      {attempt.cost_nanos === null
                        ? t("product.unsettled")
                        : money(attempt.cost_nanos, row.currency)}
                    </Typography.Text>
                    {attempt.usage && (
                      <Typography.Text>
                        {t("product.tokenUsage", { ...attempt.usage })}
                      </Typography.Text>
                    )}
                    {admin && attempt.state === "reconciliation_required" && (
                      <Button onClick={() => setReconcile(attempt.attempt_id)}>
                        {t("product.reconcile")}
                      </Button>
                    )}
                  </Space>
                </Card>
              ))}
            </Space>
          ),
        }}
      />
      <Modal
        open={budget}
        title={t("product.setBudget")}
        onCancel={() => setBudget(false)}
        footer={null}
        destroyOnHidden
      >
        <Alert type="info" message={t("product.budgetHelp")} />
        <Form
          layout="vertical"
          onFinish={async (values: { currency: string; amount: string }) => {
            try {
              const [whole, fraction = ""] = values.amount.split(".");
              const nanos =
                BigInt(whole) * 1000000000n + BigInt(fraction.padEnd(9, "0"));
              if (nanos > BigInt(Number.MAX_SAFE_INTEGER))
                throw new Error("SLASH_BAD_ARGS");
              await api.call(`/model-admin/users/${selected}/budget`, "PUT", {
                currency: values.currency,
                limit_nanos: Number(nanos),
              });
              setBudget(false);
              await state.reload();
            } catch (err) {
              state.setError(errorText(err));
            }
          }}
          initialValues={{ currency: "USD" }}
        >
          <Form.Item
            name="currency"
            label={t("product.currency")}
            rules={[{ required: true, pattern: /^[A-Z]{3}$/ }]}
          >
            <Input />
          </Form.Item>
          <Form.Item
            name="amount"
            label={t("product.limit")}
            rules={[{ required: true, pattern: /^\d+(\.\d{1,9})?$/ }]}
          >
            <Input inputMode="decimal" />
          </Form.Item>
          <Button type="primary" htmlType="submit">
            {t("product.save")}
          </Button>
        </Form>
      </Modal>
      <Modal
        open={!!reconcile}
        title={t("product.reconcile")}
        onCancel={() => setReconcile(null)}
        footer={null}
        destroyOnHidden
      >
        <Form
          layout="vertical"
          initialValues={{
            no_consumption: false,
            uncached_input: 0,
            cache_read: 0,
            cache_write: 0,
            output: 0,
          }}
          onFinish={async (values: Record<string, unknown>) => {
            try {
              await api.call(
                `/model-admin/attempts/${reconcile}/reconcile`,
                "POST",
                {
                  evidence: values.evidence,
                  usage: values.no_consumption
                    ? null
                    : Object.fromEntries(
                        [
                          "uncached_input",
                          "cache_read",
                          "cache_write",
                          "output",
                        ].map((key) => [key, values[key]]),
                      ),
                },
              );
              setReconcile(null);
              await state.reload();
            } catch (err) {
              state.setError(errorText(err));
            }
          }}
        >
          <Form.Item
            name="evidence"
            label={t("product.evidence")}
            rules={[{ required: true, min: 3 }]}
          >
            <Input.TextArea />
          </Form.Item>
          <Form.Item
            name="no_consumption"
            label={t("product.noConsumption")}
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          {["uncached_input", "cache_read", "cache_write", "output"].map(
            (key) => (
              <Form.Item key={key} name={key} label={t(`product.rate.${key}`)}>
                <InputNumber min={0} max={1000000000} precision={0} />
              </Form.Item>
            ),
          )}
          <Button type="primary" htmlType="submit">
            {t("product.save")}
          </Button>
        </Form>
      </Modal>
    </div>
  );
}

export function Security({
  api,
  identity,
  onSessionEnded,
}: {
  api: ProductApi;
  identity: Identity;
  onSessionEnded(): void;
}) {
  const { t } = useTranslation();
  const [error, setError] = useState("");
  const [enroll, setEnroll] = useState<{
    secret: string;
    otpauth_uri: string;
  } | null>(null);
  const [codes, setCodes] = useState<string[]>([]);
  return (
    <div className="product-stack">
      <Typography.Title level={3}>{t("product.nav.security")}</Typography.Title>
      {error && <Alert type="error" message={error} />}
      <Alert type="info" message={t("product.revokeSessionsHelp")} />
      <Card title={t("product.changePassword")}>
        <Form
          layout="vertical"
          onFinish={async (values: {
            password: string;
            new_password: string;
            code?: string;
          }) => {
            try {
              await api.call("/auth/password", "POST", values);
              onSessionEnded();
            } catch (err) {
              setError(errorText(err));
            }
          }}
        >
          <Form.Item
            name="password"
            label={t("product.password")}
            rules={[{ required: true }]}
          >
            <Input.Password autoComplete="current-password" />
          </Form.Item>
          <Form.Item
            name="new_password"
            label={t("product.newPassword")}
            rules={[{ required: true, min: 8 }]}
          >
            <Input.Password autoComplete="new-password" />
          </Form.Item>
          {identity.user.mfa_enabled && (
            <Form.Item
              name="code"
              label={t("product.mfaCode")}
              rules={[{ required: true }]}
            >
              <Input autoComplete="one-time-code" />
            </Form.Item>
          )}
          <Button htmlType="submit" type="primary">
            {t("product.save")}
          </Button>
        </Form>
      </Card>
      <Card title={t("product.mfa")}>
        {identity.user.mfa_enabled ? (
          <Form
            layout="vertical"
            onFinish={async (values: { password: string; code: string }) => {
              try {
                await api.call("/auth/mfa/disable", "POST", values);
                onSessionEnded();
              } catch (err) {
                setError(errorText(err));
              }
            }}
          >
            <Form.Item
              name="password"
              label={t("product.password")}
              rules={[{ required: true }]}
            >
              <Input.Password />
            </Form.Item>
            <Form.Item
              name="code"
              label={t("product.mfaCode")}
              rules={[{ required: true }]}
            >
              <Input />
            </Form.Item>
            <Button danger htmlType="submit">
              {t("product.disableMfa")}
            </Button>
          </Form>
        ) : (
          <Button
            onClick={() => {
              void api
                .call<{ secret: string; otpauth_uri: string }>(
                  "/auth/mfa/enroll",
                  "POST",
                )
                .then(setEnroll)
                .catch((err) => setError(errorText(err)));
            }}
          >
            {t("product.enableMfa")}
          </Button>
        )}
      </Card>
      {enroll && (
        <Modal
          open
          title={t("product.enableMfa")}
          onCancel={() => setEnroll(null)}
          footer={null}
          destroyOnHidden
        >
          <QRCodeSVG value={enroll.otpauth_uri} />
          <Typography.Paragraph copyable>{enroll.secret}</Typography.Paragraph>
          <Form
            layout="vertical"
            onFinish={async (values: { code: string }) => {
              try {
                const result = await api.call<{ codes: string[] }>(
                  "/auth/mfa/confirm",
                  "POST",
                  values,
                );
                setEnroll(null);
                setCodes(result.codes);
              } catch (err) {
                setError(errorText(err));
              }
            }}
          >
            <Form.Item
              name="code"
              label={t("product.mfaCode")}
              rules={[{ required: true }]}
            >
              <Input autoComplete="one-time-code" />
            </Form.Item>
            <Button htmlType="submit" type="primary">
              {t("product.confirm")}
            </Button>
          </Form>
        </Modal>
      )}
      <Modal
        open={codes.length > 0}
        title={t("product.recoveryCodes")}
        closable={false}
        maskClosable={false}
        footer={
          <Button
            type="primary"
            onClick={() => {
              setCodes([]);
              onSessionEnded();
            }}
          >
            {t("product.codesSaved")}
          </Button>
        }
      >
        <Alert type="warning" message={t("product.recoveryCodesHelp")} />
        <Typography.Paragraph copyable style={{ whiteSpace: "pre-wrap" }}>
          {codes.join("\n")}
        </Typography.Paragraph>
      </Modal>
    </div>
  );
}
