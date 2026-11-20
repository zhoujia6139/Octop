import { useCallback, useEffect, useState } from "react";
import {
  Alert,
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from "antd";
import { useTranslation } from "react-i18next";
import { ProductApi, errorText, newId } from "../../api/product/client";
import type {
  LocalResource,
  Model,
  Resource,
  SkillFile,
  Spec,
} from "../../api/product/contracts";
import { useProductData } from "./useProductData";

export async function skillFile(
  path: string,
  content: Uint8Array,
): Promise<SkillFile> {
  const hash = await crypto.subtle.digest("SHA-256", new Uint8Array(content));
  let binary = "";
  for (const byte of content) binary += String.fromCharCode(byte);
  return {
    path,
    content_base64: btoa(binary),
    size: content.length,
    sha256: Array.from(new Uint8Array(hash), (byte) =>
      byte.toString(16).padStart(2, "0"),
    ).join(""),
  };
}

function Editor({
  api,
  spec,
  skills,
  onSave,
  onCancel,
  admin,
}: {
  api: ProductApi;
  spec: Spec;
  skills: Array<{ id: string; revision: number; name: string }>;
  onSave(spec: Spec): Promise<void>;
  onCancel(): void;
  admin: boolean;
}) {
  const { t } = useTranslation();
  const [form] = Form.useForm();
  const [models, setModels] = useState<Model[]>([]);
  const [files, setFiles] = useState<SkillFile[]>(
    spec.kind === "skill" ? spec.files : [],
  );
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const main = files.find((file) => file.path === "SKILL.md");
  const content = main
    ? new TextDecoder().decode(
        Uint8Array.from(atob(main.content_base64), (c) => c.charCodeAt(0)),
      )
    : "";
  useEffect(() => {
    let active = true;
    void (admin ? api.modelDefinitions() : api.models())
      .then((value) => {
        if (active) setModels(value.filter((m) => m.protocol !== "embeddings"));
      })
      .catch((err) => {
        if (active) setError(errorText(err));
      });
    return () => {
      active = false;
    };
  }, [api, admin]);
  return (
    <Modal
      open
      title={t(
        spec.kind === "agent" ? "product.editAgent" : "product.editSkill",
      )}
      onCancel={onCancel}
      footer={null}
      width={700}
      destroyOnHidden
    >
      {error && <Alert type="error" message={error} />}
      <Form
        form={form}
        layout="vertical"
        initialValues={{
          ...spec,
          content,
          skills:
            spec.kind === "agent"
              ? spec.skills.map((s) => `${s.resource_id}@${s.revision}`)
              : [],
        }}
        onFinish={async (values: {
          name: string;
          slug: string;
          model_id: string;
          system_prompt?: string;
          content?: string;
          skills: string[];
        }) => {
          setSaving(true);
          setError("");
          try {
            const next: Spec =
              spec.kind === "agent"
                ? {
                    kind: "agent",
                    name: values.name,
                    model_id: values.model_id,
                    system_prompt: values.system_prompt ?? null,
                    skills: (values.skills ?? []).map((key) => {
                      const [resource_id, revision] = key.split("@");
                      return { resource_id, revision: Number(revision) };
                    }),
                  }
                : {
                    kind: "skill",
                    name: values.name,
                    slug: values.slug,
                    files: [
                      ...files.filter((f) => f.path !== "SKILL.md"),
                      await skillFile(
                        "SKILL.md",
                        new TextEncoder().encode(values.content ?? ""),
                      ),
                    ],
                  };
            await onSave(next);
          } catch (err) {
            setError(errorText(err));
          } finally {
            setSaving(false);
          }
        }}
      >
        <Form.Item
          name="name"
          label={t("product.name")}
          rules={[{ required: true, max: 200 }]}
        >
          <Input />
        </Form.Item>
        {spec.kind === "agent" ? (
          <>
            <Form.Item
              name="model_id"
              label={t("product.model")}
              rules={[{ required: true }]}
            >
              <Select
                options={models.map((m) => ({
                  value: m.model_id,
                  label: m.name,
                }))}
              />
            </Form.Item>
            <Form.Item name="system_prompt" label={t("product.systemPrompt")}>
              <Input.TextArea rows={5} maxLength={100000} />
            </Form.Item>
            <Form.Item name="skills" label={t("product.nav.skills")}>
              <Select
                mode="multiple"
                options={skills.map((s) => ({
                  value: `${s.id}@${s.revision}`,
                  label: `${s.name} · v${s.revision}`,
                }))}
              />
            </Form.Item>
          </>
        ) : (
          <>
            <Form.Item
              name="slug"
              label={t("product.skillSlug")}
              rules={[
                { required: true, pattern: /^[a-z0-9][a-z0-9_-]{0,63}$/ },
              ]}
            >
              <Input />
            </Form.Item>
            <Form.Item
              name="content"
              label="SKILL.md"
              rules={[{ required: true }]}
            >
              <Input.TextArea rows={10} />
            </Form.Item>
            <Form.Item label={t("product.skillFiles")}>
              <input
                type="file"
                multiple
                aria-label={t("product.skillFiles")}
                onChange={async (event) => {
                  const uploaded = Array.from(event.target.files ?? []);
                  event.target.value = "";
                  try {
                    if (
                      uploaded.reduce((size, f) => size + f.size, 0) +
                        files.reduce((size, f) => size + f.size, 0) >
                      4 * 1024 * 1024
                    )
                      throw new Error("RESOURCE_INVALID");
                    const added = await Promise.all(
                      uploaded.map(async (file) =>
                        skillFile(
                          file.webkitRelativePath || file.name,
                          new Uint8Array(await file.arrayBuffer()),
                        ),
                      ),
                    );
                    setFiles((before) => [
                      ...before.filter(
                        (f) => !added.some((n) => n.path === f.path),
                      ),
                      ...added,
                    ]);
                    const skill = added.find((f) => f.path === "SKILL.md");
                    if (skill)
                      form.setFieldValue(
                        "content",
                        new TextDecoder().decode(
                          Uint8Array.from(atob(skill.content_base64), (c) =>
                            c.charCodeAt(0),
                          ),
                        ),
                      );
                  } catch (err) {
                    setError(errorText(err));
                  }
                }}
              />
            </Form.Item>
            <Space wrap>
              {files
                .filter((file) => file.path !== "SKILL.md")
                .map((file) => (
                  <Tag
                    key={file.path}
                    closable
                    onClose={() =>
                      setFiles((before) =>
                        before.filter((f) => f.path !== file.path),
                      )
                    }
                  >
                    {file.path}
                  </Tag>
                ))}
            </Space>
          </>
        )}
        <Form.Item>
          <Button type="primary" htmlType="submit" loading={saving}>
            {t("product.save")}
          </Button>
        </Form.Item>
      </Form>
    </Modal>
  );
}

export function Resources({
  api,
  kind,
  admin,
}: {
  api: ProductApi;
  kind: "agent" | "skill";
  admin: boolean;
}) {
  const { t } = useTranslation();
  const [filter, setFilter] = useState(kind);
  useEffect(() => setFilter(kind), [kind]);
  const load = useCallback(
    () => (admin ? api.definitions() : api.resources()),
    [api, admin],
  );
  const state = useProductData<Array<Resource | LocalResource>>(load, []);
  const [names, setNames] = useState<Record<string, string>>({});
  const definitions = state.data;
  const setResourceError = state.setError;
  useEffect(() => {
    if (!admin) return;
    let active = true;
    void Promise.all(
      definitions
        .filter((row) => !("deleted" in row && row.deleted))
        .map(async (row) => {
          const version = await api.version(row.resource_id, row.revision);
          return [
            row.resource_id,
            version.spec?.name ?? row.resource_id,
          ] as const;
        }),
    )
      .then((entries) => {
        if (active) setNames(Object.fromEntries(entries));
      })
      .catch((err) => {
        if (active) setResourceError(errorText(err));
      });
    return () => {
      active = false;
    };
  }, [api, admin, definitions, setResourceError]);
  const [editor, setEditor] = useState<{
    id: string;
    revision: number;
    spec: Spec;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const [skillOptions, setSkillOptions] = useState<
    Array<{ id: string; revision: number; name: string }>
  >([]);
  const [assignment, setAssignment] = useState<Resource | null>(null);
  const [users, setUsers] = useState<Array<{ label: string; value: number }>>(
    [],
  );
  const run = async (work: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await work();
      state.setError("");
      await state.reload();
    } catch (err) {
      state.setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const edit = async (row?: Resource | LocalResource) => {
    try {
      const skills = await Promise.all(
        state.data
          .filter((r) => r.kind === "skill" && !("deleted" in r && r.deleted))
          .map(async (r) => {
            const revision =
              "published_revision" in r
                ? r.published_revision
                : r.installed_revision ?? r.revision;
            if (!revision) return null;
            const spec =
              "spec" in r
                ? r.spec
                : (await api.version(r.resource_id, revision)).spec;
            return spec
              ? { id: r.resource_id, revision, name: spec.name }
              : null;
          }),
      );
      setSkillOptions(
        skills.filter((r): r is NonNullable<typeof r> => r !== null),
      );
      const spec = row
        ? "spec" in row
          ? row.spec
          : (await api.version(row.resource_id, row.revision)).spec
        : filter === "agent"
        ? {
            kind: "agent" as const,
            name: "",
            model_id: "",
            system_prompt: null,
            skills: [],
          }
        : { kind: "skill" as const, name: "", slug: "", files: [] };
      if (spec)
        setEditor({
          id: row?.resource_id ?? newId(),
          revision: row?.revision ?? 0,
          spec,
        });
    } catch (err) {
      state.setError(errorText(err));
    }
  };
  return (
    <div className="product-stack">
      <Typography.Title level={3}>
        {t(
          admin
            ? "product.platformResources"
            : `product.nav.${filter === "skill" ? "skills" : "resources"}`,
        )}
      </Typography.Title>
      {state.error && <Alert type="error" message={state.error} showIcon />}
      {!admin && (
        <Alert type="info" message={t("product.personalCopiesHelp")} />
      )}
      <div className="product-toolbar">
        {admin && (
          <Select
            value={filter}
            onChange={setFilter}
            options={[
              { value: "agent", label: "Agent" },
              { value: "skill", label: t("product.nav.skills") },
            ]}
          />
        )}
        <Button
          type="primary"
          onClick={() => {
            void edit();
          }}
        >
          {t("product.create")}
        </Button>
        <Button
          loading={busy}
          onClick={() => {
            void run(() => (admin ? state.reload() : api.resourceSync()));
          }}
        >
          {t(admin ? "product.refresh" : "product.retryInstall")}
        </Button>
        {!admin && (
          <Popconfirm
            title={t("product.restorePersonal")}
            description={t("product.restorePersonalHelp")}
            onConfirm={() => run(() => api.resourceSync(true))}
          >
            <Button>{t("product.restorePersonal")}</Button>
          </Popconfirm>
        )}
      </div>
      <Table
        rowKey="resource_id"
        loading={state.busy || busy}
        dataSource={state.data.filter(
          (r) => r.kind === filter && !("deleted" in r && r.deleted),
        )}
        pagination={{ pageSize: 15 }}
        columns={[
          {
            title: t("product.name"),
            key: "name",
            render: (_, row) => (
              <>
                <Typography.Text>
                  {"spec" in row
                    ? row.spec?.name ?? row.resource_id
                    : names[row.resource_id] ?? row.resource_id}
                </Typography.Text>{" "}
                <Tag>{t(`product.${row.scope}`)}</Tag>
              </>
            ),
          },
          {
            title: t("product.version"),
            key: "version",
            render: (_, row) => (
              <Space direction="vertical">
                <span>
                  {t("product.draftVersion", { version: row.revision })}
                </span>
                {"published_revision" in row ? (
                  <span>
                    {t("product.publishedVersion", {
                      version: row.published_revision ?? "—",
                    })}
                  </span>
                ) : (
                  <span>
                    {t("product.installedVersion", {
                      version: row.installed_revision ?? "—",
                      desired: row.desired_revision ?? "—",
                    })}
                  </span>
                )}
              </Space>
            ),
          },
          ...(!admin
            ? [
                {
                  title: t("product.state"),
                  key: "state",
                  render: (_: unknown, row: Resource | LocalResource) =>
                    "sync_state" in row && (
                      <Space direction="vertical">
                        <Tag>{t(`product.states.${row.sync_state}`)}</Tag>
                        <Tag>{t(`product.states.${row.install_state}`)}</Tag>
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
              ]
            : []),
          {
            title: t("product.actions"),
            key: "actions",
            render: (_, row) => (
              <Space wrap>
                {admin || row.scope === "personal" ? (
                  <Button
                    onClick={() => {
                      void edit(row);
                    }}
                  >
                    {t("product.edit")}
                  </Button>
                ) : (
                  <Button
                    disabled={
                      !("installed_revision" in row) || !row.installed_revision
                    }
                    onClick={() => {
                      void run(() =>
                        api.copy(
                          row.resource_id,
                          "installed_revision" in row
                            ? row.installed_revision ?? row.revision
                            : row.revision,
                        ),
                      );
                    }}
                  >
                    {t("product.makeCopy")}
                  </Button>
                )}
                {admin && "published_revision" in row && (
                  <>
                    <Button
                      disabled={row.published_revision === row.revision}
                      onClick={() => {
                        void run(() =>
                          api.publish(row.resource_id, row.revision),
                        );
                      }}
                    >
                      {t("product.publish")}
                    </Button>
                    <Button
                      disabled={!row.published_revision}
                      onClick={() => {
                        void api
                          .users()
                          .then((result) => {
                            setUsers(
                              result.map((u) => ({
                                label: u.username,
                                value: u.user_id,
                              })),
                            );
                            setAssignment(row);
                          })
                          .catch((err) => state.setError(errorText(err)));
                      }}
                    >
                      {t("product.assign")}
                    </Button>
                  </>
                )}
                {(admin || row.scope === "personal") && (
                  <Popconfirm
                    title={t("product.deleteConfirm")}
                    onConfirm={() =>
                      run(() =>
                        api.saveResource(
                          row.resource_id,
                          row.revision,
                          null,
                          admin,
                        ),
                      )
                    }
                  >
                    <Button danger>{t("product.delete")}</Button>
                  </Popconfirm>
                )}
              </Space>
            ),
          },
        ]}
      />
      {editor && (
        <Editor
          key={editor.id}
          api={api}
          spec={editor.spec}
          skills={skillOptions}
          admin={admin}
          onCancel={() => setEditor(null)}
          onSave={async (spec) => {
            await api.saveResource(editor.id, editor.revision, spec, admin);
            setEditor(null);
            if (!admin) {
              try {
                await api.resourceSync();
              } catch (err) {
                state.setError(errorText(err));
              }
            }
            await state.reload();
          }}
        />
      )}
      {assignment && (
        <Modal
          open
          title={t("product.assign")}
          onCancel={() => setAssignment(null)}
          footer={null}
          destroyOnHidden
        >
          <Form
            layout="vertical"
            onFinish={async (values: {
              user: number;
              action: "assign" | "withdraw";
            }) => {
              await run(async () => {
                await api.assign(
                  values.user,
                  assignment.resource_id,
                  values.action === "withdraw"
                    ? null
                    : assignment.published_revision,
                );
                setAssignment(null);
                void message.success(t("product.saved"));
              });
            }}
            initialValues={{ action: "assign" }}
          >
            <Form.Item
              name="user"
              label={t("product.username")}
              rules={[{ required: true }]}
            >
              <Select options={users} />
            </Form.Item>
            <Form.Item name="action" label={t("product.actions")}>
              <Select
                options={[
                  { value: "assign", label: t("product.assign") },
                  { value: "withdraw", label: t("product.withdraw") },
                ]}
              />
            </Form.Item>
            <Button htmlType="submit" loading={busy} type="primary">
              {t("product.save")}
            </Button>
          </Form>
        </Modal>
      )}
    </div>
  );
}
