import { ErrorNotice, errorText, serializeError } from '../errors';
import { EventRecords } from '../RecordDetails';
import { NativeGate } from "../NativeGate";
import {
  MessageOutlined,
  ReloadOutlined,
  SearchOutlined,
  StopOutlined,
} from "@ant-design/icons";
import { useQuery } from "@tanstack/react-query";
import {
  Alert,
  App as AntApp,
  Button,
  Card,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Modal,
  Select,
  Space,
  Switch,
  Table,
  Tabs,
  Tag,
  Tooltip,
} from "antd";
import React from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import type { Session } from "../types";

import {
  Heading,
  QueryState,
  Status,
  time,
  useAction,
  useData,
} from "../components";
export default function SessionsPage() {
  const query = useData<Session[]>("/sessions");
  const [search, setSearch] = React.useState("");
  const [filter, setFilter] = React.useState("all");
  const [params, setParams] = useSearchParams();
  const [command, setCommand] = React.useState<Session>();
  const [text, setText] = React.useState("");
  const nextDrawer = React.useRef<string | null>(null);
  const action = useAction();
  const { modal } = AntApp.useApp();
  const navigate = useNavigate();
  const stop = (session: Session) =>
    modal.confirm({
      title: `停止「${session.title}」当前轮次？`,
      content:
        "当前工具可能被中断。Harness 会保留已有排队消息，后续仍可能启动新的轮次。",
      okText: "停止当前轮次",
      cancelText: "返回",
      okButtonProps: { danger: true },
      onOk: () => action.mutateAsync({ path: `/sessions/${session.id}/stop` }),
    });
  return (
    <>
      <Heading
        eyebrow="SESSION MANAGEMENT"
        title="会话管理"
        description="查看真实会话状态，随时提交纠偏指令。"
        actions={
          <Button icon={<ReloadOutlined />} onClick={() => query.refetch()}>
            刷新
          </Button>
        }
      />
      <Card className="panel">
        <div className="table-toolbar">
          <Input
            prefix={<SearchOutlined />}
            placeholder="搜索会话标题、工作区或 ID"
            allowClear
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <Select
            value={filter}
            onChange={setFilter}
            options={[
              { value: "all", label: "全部状态" },
              { value: "running", label: "进行中" },
              { value: "idle", label: "空闲" },
              { value: "manual", label: "人工审批" },
            ]}
          />
          <span className="muted">共 {query.data?.length || 0} 个会话</span>
        </div>
        <QueryState query={query}>
          <Table
            rowKey="id"
            dataSource={query.data?.filter(
              (s) =>
                (s.title + s.cwd + s.id)
                  .toLowerCase()
                  .includes(search.toLowerCase()) &&
                (filter === "all" ||
                  s.status === filter ||
                  (filter === "manual" && s.approval_mode === "manual")),
            )}
            pagination={{ pageSize: 10, showSizeChanger: false }}
            scroll={{ x: 900 }}
            columns={[
              {
                title: "会话 / 工作区",
                render: (_, s) => (
                  <div className="session-cell">
                    <span className="session-icon">
                      <MessageOutlined />
                    </span>
                    <div>
                      <button
                        className="text-link"
                        onClick={() => setParams({ id: s.id })}
                      >
                        {s.title}
                      </button>
                      <small title={s.cwd}>{s.cwd || s.id}</small>
                    </div>
                  </div>
                ),
              },
              {
                title: "状态",
                render: (_, s) => (
                  <>
                    <Status value={s.status} />
                    {s.subagent && <Tag>子代理 · 只读</Tag>}
                  </>
                ),
              },
              {
                title: "插件监管",
                render: (_, s) =>
                  s.subagent ? (
                    <Tag>由主会话审查</Tag>
                  ) : s.supervision ? (
                    <>
                      <Status value={s.supervision.state} />
                      <small><ErrorNotice value={s.supervision.reason}/></small>
                    </>
                  ) : (
                    <Tag>尚未接管</Tag>
                  ),
              },
              {
                title: "审批方式",
                render: (_, s) => (
                  <Switch
                    checked={s.approval_mode === "manual"}
                    checkedChildren="人工"
                    unCheckedChildren="自动"
                    disabled={s.subagent || !!s.error || action.isPending}
                    onChange={(checked) =>
                      action.mutate({
                        path: `/sessions/${s.id}/approval-mode`,
                        body: { mode: checked ? "manual" : "auto" },
                        method: "PUT",
                      })
                    }
                  />
                ),
              },
              {
                title: "最近活动",
                render: (_, s) => (
                  <span className="muted nowrap">{time(s.updated)}</span>
                ),
              },
              {
                title: "操作",
                width: 210,
                render: (_, s) => (
                  <Space>
                    <Button
                      size="small"
                      disabled={s.subagent || !!s.error}
                      onClick={() => {
                        setCommand(s);
                        setText("");
                      }}
                    >
                      插话
                    </Button>
                    <Button
                      size="small"
                      onClick={() => navigate(`/reviews?session=${s.id}`)}
                    >
                      审查
                    </Button>
                    <Tooltip title="停止当前轮次">
                      <Button
                        aria-label={`停止 ${s.title}`}
                        size="small"
                        danger
                        icon={<StopOutlined />}
                        disabled={s.subagent || !!s.error || action.isPending}
                        onClick={() => stop(s)}
                      />
                    </Tooltip>
                  </Space>
                ),
              },
            ]}
          />
        </QueryState>
      </Card>
      <SessionDrawer id={params.get("id")} close={() => setParams({})} />
      <Modal
        title="向会话即时插话"
        afterClose={() => {
          if (nextDrawer.current) {
            setParams({ id: nextDrawer.current });
            nextDrawer.current = null;
          }
        }}
        open={!!command}
        okText="发送指令"
        cancelText="取消"
        confirmLoading={action.isPending}
        okButtonProps={{ disabled: !text.trim() }}
        onCancel={() => setCommand(undefined)}
        onOk={async () => {
          if (command) {
            try {
              await action.mutateAsync({
                path: `/sessions/${command.id}/messages`,
                body: { text },
              });
              nextDrawer.current = command.id;
              setCommand(undefined);
            } catch {}
          }
        }}
      >
        <p className="muted">{command?.title}</p>
        <Alert
          type="info"
          showIcon
          message="指令会在模型下一个可处理的步骤生效"
          description="如正在等待交接，会先取消原审查，再发送人工指令。"
        />
        <Input.TextArea
          aria-label="人工指令"
          className="spaced"
          value={text}
          onChange={(e) => setText(e.target.value)}
          rows={6}
          maxLength={20000}
          showCount
          placeholder="写下明确的纠偏方向或下一步要求…"
        />
      </Modal>
    </>
  );
}
function SessionDrawer({
  id,
  close,
}: {
  id: string | null;
  close: () => void;
}) {
  const query = useQuery<Session>({
    queryKey: [`/sessions/${id}`],
    queryFn: () => api(`/sessions/${id}`),
    enabled: !!id,
  });
  const s = query.data;
  return (
    <Drawer title="会话详情" open={!!id} onClose={close} width={720}>
      <QueryState query={query}>
        {s && (
          <>
            <h2>{s.title}</h2>
            <NativeGate id={s.supervision?.gate_id} />
            <Descriptions
              column={1}
              items={[
                { key: "id", label: "会话 ID", children: <code>{s.id}</code> },
                { key: "cwd", label: "工作区", children: s.cwd },
                {
                  key: "status",
                  label: "状态",
                  children: <Status value={s.status} />,
                },
                {
                  key: "mode",
                  label: "审批模式",
                  children:
                    s.approval_mode === "manual"
                      ? "人工审批（后续交接生效）"
                      : "自动审查",
                },
              ]}
            />
            {s.error && <Alert message={<ErrorNotice value={s.error}/>} type="error" />}
            <Tabs
              items={[
                {
                  key: "commands",
                  label: "人工指令",
                  children: s.commands?.length ? (
                    <div className="command-list">
                      {s.commands.map((c) => (
                        <Card key={c.id} size="small">
                          <Space>
                            <Status value={c.status} />
                            <span className="muted">{time(c.created)}</span>
                          </Space>
                          <p className="preserve">
                            {c.kind === "stop" ? "停止当前轮次" : c.text}
                          </p>
                          {c.detail && <small>{c.detail}</small>}
                        </Card>
                      ))}
                    </div>
                  ) : (
                    <Empty description="尚未提交人工指令" />
                  ),
                },
                {
                  key: "events",
                  label: "最近会话事件",
                  children: (
                    <EventRecords value={s.events}/>
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
