import { ErrorNotice } from '../errors';
import { RecordValue, recordSummary } from '../RecordDetails';
import { reviewerNames } from "../ReviewerSelect";
import { NativeGate } from "../NativeGate";
import { PlusOutlined, RightOutlined, SearchOutlined } from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tabs,
  Tag,
} from "antd";
import React from "react";
import { useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { Review, Session } from "../types";

import {
  Heading,
  QueryState,
  Status,
  labels,
  time,
  useAction,
  useData,
} from "../components";
export default function ReviewsPage() {
  const query = useData<Review[]>("/reviews");
  const sessions = useData<Session[]>("/sessions");
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = React.useState("");
  const [create, setCreate] = React.useState(false);
  const [form] = Form.useForm();
  const action = useAction();
  const filter = params.get("status") || "all";
  const sid = params.get("session");
  const [expanded, setExpanded] = React.useState<React.Key[]>([]);
  const groups = React.useMemo(() => {
    const bySession = new Map<string, Review[]>();
    for (const review of query.data || []) {
      if (sid && review.session_id !== sid) continue;
      const rows = bySession.get(review.session_id) || [];
      rows.push(review);
      bySession.set(review.session_id, rows);
    }
    const term = search.trim().toLowerCase();
    return Array.from(bySession, ([id, rows]) => {
      rows.sort((a, b) => b.created - a.created || b.id.localeCompare(a.id));
      const session = sessions.data?.find((item) => item.id === id);
      const title = session?.title || id;
      return {
        id,
        title,
        latest: rows[0],
        total: rows.length,
        reviews: rows.filter(
          (r) =>
            (filter === "all" || r.status === filter) &&
            [title, id, r.id, r.packet.summary || ""]
              .join(" ")
              .toLowerCase()
              .includes(term),
        ),
      };
    })
      .filter((group) => group.reviews.length > 0)
      .sort(
        (a, b) =>
          b.latest.created - a.latest.created || a.id.localeCompare(b.id),
      );
  }, [query.data, sessions.data, sid, filter, search]);
  const toggleSession = (id: string) =>
    setExpanded((keys) =>
      keys.includes(id) ? keys.filter((key) => key !== id) : [...keys, id],
    );
  return (
    <>
      <Heading
        eyebrow="REVIEW CENTER"
        title="审查中心"
        description="按会话查看最新审查状态，展开查看各阶段和复验记录。"
        actions={
          <Button
            type="primary"
            icon={<PlusOutlined />}
            onClick={() => {
              form.resetFields();
              form.setFieldsValue({
                session_id: sid || undefined,
                phase: "checkpoint",
                scope: ".",
              });
              setCreate(true);
            }}
          >
            发起观察审查
          </Button>
        }
      />
      {sid && (
        <Alert
          className="spaced-bottom"
          type="info"
          closable
          message={`当前筛选会话：${sessions.data?.find((s) => s.id === sid)?.title || sid}`}
          onClose={() => setParams({})}
        />
      )}
      <Card className="panel">
        <div className="table-toolbar">
          <Input
            prefix={<SearchOutlined />}
            placeholder="搜索摘要、会话或审查 ID"
            value={search}
            allowClear
            onChange={(e) => setSearch(e.target.value)}
          />
          <Select
            value={filter}
            onChange={(status) => {
              const next = new URLSearchParams(params);
              next.set("status", status);
              setParams(next);
            }}
            options={[
              "all",
              "queued",
              "running",
              "awaiting_human",
              "completed",
              "blocked",
              "cancelled",
            ].map((value) => ({
              value,
              label: value === "all" ? "全部状态" : labels[value],
            }))}
          />
        </div>
        <QueryState query={query}>
          <Table
            rowKey="id"
            dataSource={groups}
            pagination={{
              pageSize: 10,
              showSizeChanger: false,
              showTotal: (total) => `共 ${total} 个会话`,
            }}
            scroll={{ x: 950 }}
            expandable={{
              showExpandColumn: false,
              expandedRowKeys: expanded,
              onExpandedRowsChange: (keys) => setExpanded([...keys]),
              expandedRowRender: (group) => (
                <Table<Review>
                  rowKey="id"
                  pagination={{ pageSize: 10, showSizeChanger: false }}
                  scroll={{ x: 850 }}
                  dataSource={group.reviews}
                  columns={[
                    {
                      title: "审查阶段",
                      render: (_, r) => (
                        <button
                          className="text-link"
                          onClick={() => {
                            const next = new URLSearchParams(params);
                            next.set("id", r.id);
                            setParams(next);
                          }}
                        >
                          {labels[r.packet.phase]}
                          <small>
                            {recordSummary(r.packet.summary).slice(0, 80) || "未提供摘要"}
                          </small>
                        </button>
                      ),
                    },
                    {
                      title: "审查器 / 模型",
                      render: (_, r) =>
                        r.reviewer ? (
                          <span>
                            {reviewerNames[r.reviewer.provider]}
                            <small style={{ display: "block" }}>
                              {r.reviewer.model_provider
                                ? `${r.reviewer.model_provider} / `
                                : ""}
                              {r.reviewer.model}
                            </small>
                          </span>
                        ) : (
                          "历史审查 · 模型未记录"
                        ),
                    },
                    {
                      title: "模式",
                      render: (_, r) => (
                        <Tag bordered={false}>
                          {r.mode === "native_handoff"
                            ? "插件强制监管"
                            : r.mode === "handoff"
                              ? "阻塞交接"
                              : r.mode === "historical"
                                ? "历史记录"
                                : "观察审查"}
                        </Tag>
                      ),
                    },
                    {
                      title: "状态",
                      render: (_, r) => <Status value={r.status} />,
                    },
                    {
                      title: "结论",
                      render: (_, r) => <Status value={r.result?.decision} />,
                    },
                    {
                      title: "创建时间",
                      render: (_, r) => (
                        <span className="muted nowrap">{time(r.created)}</span>
                      ),
                    },
                  ]}
                />
              ),
            }}
            columns={[
              {
                title: "会话",
                render: (_, group) => (
                  <button
                    className="text-link review-session-toggle"
                    aria-expanded={expanded.includes(group.id)}
                    aria-label={`${expanded.includes(group.id) ? "收起" : "展开"}会话 ${group.title}（${group.id}）`}
                    onClick={() => toggleSession(group.id)}
                  >
                    <RightOutlined
                      rotate={expanded.includes(group.id) ? 90 : 0}
                    />
                    <span>
                      {group.title}
                      <small>{group.id}</small>
                    </span>
                  </button>
                ),
              },
              {
                title: "审查记录",
                render: (_, group) =>
                  group.reviews.length === group.total
                    ? `${group.total} 条`
                    : `${group.reviews.length} / ${group.total} 条匹配`,
              },
              {
                title: "最新阶段",
                render: (_, group) =>
                  labels[group.latest.packet.phase] ||
                  group.latest.packet.phase,
              },
              {
                title: "最新状态",
                render: (_, group) => <Status value={group.latest.status} />,
              },
              {
                title: "最新结论",
                render: (_, group) => (
                  <Status value={group.latest.result?.decision} />
                ),
              },
              {
                title: "最近审查",
                render: (_, group) => (
                  <span className="muted nowrap">
                    {time(group.latest.created)}
                  </span>
                ),
              },
            ]}
          />
        </QueryState>
      </Card>
      <ReviewDrawer
        id={params.get("id")}
        close={() => {
          const next = new URLSearchParams(params);
          next.delete("id");
          setParams(next);
        }}
      />
      <Modal
        title="发起观察审查"
        open={create}
        onCancel={() => setCreate(false)}
        confirmLoading={action.isPending}
        okText="开始审查"
        cancelText="取消"
        onOk={async () => {
          try {
            const values = await form.validateFields();
            await action.mutateAsync({
              path: "/reviews",
              body: {
                ...values,
                scope: values.scope
                  .split("\n")
                  .map((s: string) => s.trim())
                  .filter(Boolean),
              },
            });
            setCreate(false);
          } catch {}
        }}
      >
        <Alert
          type="info"
          message="观察审查不会暂停、批准或恢复 DeepSeek 会话，会计入每日额度。"
          className="spaced-bottom"
        />
        <Form layout="vertical" form={form}>
          <Form.Item
            name="session_id"
            label="会话"
            rules={[{ required: true }]}
          >
            <Select
              showSearch
              optionFilterProp="label"
              options={sessions.data
                ?.filter((s) => !s.subagent && !s.error)
                .map((s) => ({ value: s.id, label: s.title }))}
            />
          </Form.Item>
          <Form.Item name="phase" label="审查阶段" rules={[{ required: true }]}>
            <Select
              options={["plan", "checkpoint", "acceptance"].map((value) => ({
                value,
                label: labels[value],
              }))}
            />
          </Form.Item>
          <Form.Item
            name="scope"
            label="审查范围（相对路径，每行一个）"
            rules={[{ required: true }]}
          >
            <Input.TextArea rows={2} />
          </Form.Item>
          <Form.Item
            name="summary"
            label="审查目标与摘要"
            rules={[{ required: true, whitespace: true }]}
          >
            <Input.TextArea rows={5} maxLength={20000} />
          </Form.Item>
        </Form>
      </Modal>
    </>
  );
}
function ReviewDrawer({ id, close }: { id: string | null; close: () => void }) {
  const query = useQuery<Review>({
    queryKey: [`/reviews/${id}`],
    queryFn: () => api(`/reviews/${id}`),
    enabled: !!id,
  });
  const action = useAction();
  const r = query.data;
  const [instruction, setInstruction] = React.useState("");
  const [decision, setDecision] = React.useState("");
  const result = r?.result || r?.suggestion;
  const active =
    r && ["queued", "running", "awaiting_human"].includes(r.status);
  React.useEffect(() => {
    setInstruction("");
    setDecision("");
  }, [id]);
  return (
    <Drawer title="审查详情" open={!!id} onClose={close} width={800}>
      <QueryState query={query}>
        {r && (
          <>
            <div className="drawer-title">
              <h2>{labels[r.packet.phase]}</h2>
              <Status value={r.status} />
            </div>
            <p className="muted">
              <code>{r.id}</code>
            </p>
            <Descriptions
              column={1}
              size="small"
              items={[
                { key: "sid", label: "会话", children: r.session_id },
                {
                  key: "reviewer",
                  label: "审查器 / 模型",
                  children: r.reviewer
                    ? `${reviewerNames[r.reviewer.provider]} / ${r.reviewer.model_provider ? r.reviewer.model_provider + " / " : ""}${r.reviewer.model}`
                    : "历史审查 · 模型未记录",
                },
                {
                  key: "scope",
                  label: "范围",
                  children: (r.packet.scope || ["."]).join("、"),
                },
                {
                  key: "created",
                  label: "创建时间",
                  children: time(r.created),
                },
                {
                  key: "mode",
                  label: "模式",
                  children:
                    r.mode === "native_handoff"
                      ? "插件强制监管"
                      : r.mode === "handoff"
                        ? "阻塞交接"
                        : r.mode === "historical"
                          ? "历史记录"
                          : "观察审查 · 未验证会话暂停",
                },
              ]}
            />
            <NativeGate id={r.packet.native_gate_id} />
            <div className="drawer-actions">
              <Space wrap>
                {active &&
                  ["handoff", "native_handoff"].includes(r.mode) &&
                  !r.manual && (
                    <Button
                      loading={action.isPending}
                      onClick={() =>
                        action.mutate({
                          path: `/reviews/${r.id}/takeover`,
                          body: { version: r.version },
                        })
                      }
                    >
                      人工接管
                    </Button>
                  )}
                {active && (
                  <Button
                    danger
                    loading={action.isPending}
                    onClick={() =>
                      action.mutate({ path: `/reviews/${r.id}/cancel` })
                    }
                  >
                    取消审查
                  </Button>
                )}
                {!active &&
                  !["historical", "native_handoff"].includes(r.mode) && (
                    <Button
                      loading={action.isPending}
                      onClick={() =>
                        action.mutate({ path: `/reviews/${r.id}/retry` })
                      }
                    >
                      重新观察审查
                    </Button>
                  )}
                {r.manual > 0 && active && (
                  <Tag color="gold">人工审批已启用</Tag>
                )}
              </Space>
            </div>
            <Tabs
              items={[
                {
                  key: "result",
                  label: "结论与证据",
                  children: (
                    <>
                      <h3>任务摘要</h3>
                      <RecordValue value={r.packet.summary}/>
                      {!result ? (
                        <Empty
                          description={
                            active ? "审查进行中，结论将在这里显示" : "暂无结论"
                          }
                        />
                      ) : (
                        <>
                          <div className="result-heading">
                            <h3>{r.result ? "最终结论" : "审查员建议"}</h3>
                            <Status value={result.decision} />
                          </div>
                          <div className="preserve">{result.decision==='blocked'?<ErrorNotice value={result}/>:result.summary}</div>
                          <h3>下一步指令</h3>
                          <div className="instruction preserve">
                            {result.instruction}
                          </div>
                          <h3>核验依据</h3>
                          {result.checks?.length ? (
                            <ul>
                              {result.checks.map((x, i) => (
                                <li key={i}>{x}</li>
                              ))}
                            </ul>
                          ) : (
                            <p className="muted">没有记录核验依据</p>
                          )}
                          {!!result.issues?.length && (
                            <>
                              <h3>发现的问题</h3>
                              <ul>
                                {result.issues.map((x, i) => (
                                  <li key={i}>{x}</li>
                                ))}
                              </ul>
                            </>
                          )}
                          <Alert
                            type={
                              result.pause_proof?.pause_verified
                                ? "success"
                                : "warning"
                            }
                            showIcon
                            message={
                              result.pause_proof?.pause_verified
                                ? "暂停证据已通过"
                                : r.mode === "observation"
                                  ? "观察审查：未验证会话暂停"
                                  : "暂停证据未通过或不可用"
                            }
                          />
                          {result.pause_proof && (
                            <details className="spaced">
                              <summary>查看暂停核对详情</summary>
                              <RecordValue value={result.pause_proof}/>
                            </details>
                          )}
                        </>
                      )}
                      {r.status === "awaiting_human" && (
                        <Card title="提交人工结论" className="spaced">
                          <p className="muted">
                            请在 {time(r.deadline)}{" "}
                            前提交。提交时会再次核对暂停证据。
                          </p>
                          <Select
                            aria-label="人工结论"
                            className="full"
                            placeholder="选择结论"
                            value={decision || undefined}
                            onChange={setDecision}
                            options={(r.packet.phase === "acceptance"
                              ? ["done", "revise"]
                              : ["approve", "revise"]
                            ).map((value) => ({ value, label: labels[value] }))}
                          />
                          <Input.TextArea
                            aria-label="审批指令"
                            className="spaced"
                            placeholder="明确说明 DeepSeek 接下来应做什么…"
                            value={instruction}
                            onChange={(e) => setInstruction(e.target.value)}
                            rows={5}
                            maxLength={20000}
                          />
                          <Button
                            className="spaced"
                            type="primary"
                            disabled={
                              !decision || !instruction.trim() || !!r.human
                            }
                            loading={action.isPending}
                            onClick={() =>
                              action.mutate({
                                path: `/reviews/${r.id}/decision`,
                                body: {
                                  version: r.version,
                                  decision,
                                  instruction,
                                },
                              })
                            }
                          >
                            {r.human ? "正在核对并返回" : "提交人工结论"}
                          </Button>
                        </Card>
                      )}
                    </>
                  ),
                },
                {
                  key: "logs",
                  label: "执行日志",
                  children: (
                    <Tabs
                      items={Object.entries(r.logs || {}).map(
                        ([name, text]) => ({
                          key: name,
                          label: name,
                          children: (
                            <pre className="log">{text || "暂无日志"}</pre>
                          ),
                        }),
                      )}
                    />
                  ),
                },
              ]}
            />
          </>
        )}
      </QueryState>
    </Drawer>
  );
}
