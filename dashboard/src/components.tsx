import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Badge, Button, Space, Spin, Tag } from "antd";
import React from "react";
import { api } from "./api";
import type { Connector } from "./types";

export const labels: Record<string, string> = {
  queued: "等待审查",
  running: "进行中",
  awaiting_human: "待人工审批",
  completed: "已完成",
  blocked: "已阻塞",
  cancelled: "已取消",
  approve: "批准",
  revise: "要求修改",
  done: "验收通过",
  idle: "空闲",
  unavailable: "不可用",
  pending: "提交中",
  dispatching: "正在送达",
  accepted: "Harness 已接收",
  consumed: "会话已消费",
  failed: "失败",
  unknown: "送达待核实",
  plan: "方案审查",
  checkpoint: "阶段检查",
  acceptance: "最终验收",
};
export const colors: Record<string, string> = {
  running: "processing",
  awaiting_human: "warning",
  blocked: "error",
  completed: "success",
  approve: "success",
  done: "success",
  revise: "orange",
  cancelled: "default",
  consumed: "success",
  accepted: "blue",
  failed: "error",
  unknown: "warning",
};
export const eventLabels: Record<string, string> = {
  codex_started: "GPT 开始审查",
  codex_finished: "GPT 审查结束",
  service_started: "监工已启动",
  service_stopped: "监工已停止",
  settings_updated: "更新服务配置",
  review_queued: "审查已入队",
  review_finished: "审查结束",
  review_cancelled: "取消审查",
  review_takeover: "人工接管审查",
  review_decision: "提交人工审批",
  review_awaiting_human: "等待人工审批",
  review_recovered: "恢复中断记录",
  response_delivered: "交接响应已返回",
  approval_mode_changed: "切换审批模式",
  command_submitted: "提交人工指令",
  command_accepted: "Harness 已接收指令",
  command_consumed: "会话已消费指令",
  command_failed: "指令发送失败",
  command_unknown: "指令送达待核实",
  api_error: "管理接口异常",
};
export function time(value?: string | number | null) {
  return value
    ? new Date(typeof value === "number" ? value * 1000 : value).toLocaleString(
        "zh-CN",
        {
          timeZone: "Asia/Shanghai",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        },
      )
    : "—";
}
export function Status({ value }: { value?: string }) {
  return value ? (
    <Tag bordered={false} color={colors[value] || "default"}>
      {labels[value] || value}
    </Tag>
  ) : (
    <span className="muted">—</span>
  );
}
export function useData<T>(path: string) {
  return useQuery<T>({ queryKey: [path], queryFn: () => api<T>(path) });
}
export function useAction() {
  const cache = useQueryClient();
  const { message } = AntApp.useApp();
  return useMutation({
    mutationFn: ({
      path,
      body = {},
      method,
    }: {
      path: string;
      body?: Record<string, unknown>;
      method?: string;
    }) => api(path, body, method),
    onSuccess: () => {
      void cache.invalidateQueries();
      message.success("操作已提交");
    },
    onError: (error) => {
      message.error(error.message);
      void cache.invalidateQueries();
    },
  });
}
export function QueryState({
  query,
  children,
}: {
  query: { isPending: boolean; error: Error | null; refetch: () => unknown };
  children: React.ReactNode;
}) {
  if (query.isPending)
    return (
      <div className="loading">
        <Spin />
        <span>正在读取本机数据…</span>
      </div>
    );
  if (query.error)
    return (
      <Alert
        type="error"
        showIcon
        message="数据暂时不可用"
        description={query.error.message}
        action={<Button onClick={() => query.refetch()}>重试</Button>}
      />
    );
  return <>{children}</>;
}
export function Heading({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow: string;
  title: string;
  description: string;
  actions?: React.ReactNode;
}) {
  return (
    <div className="heading">
      <div>
        <div className="eyebrow">{eyebrow}</div>
        <h1>{title}</h1>
        <p>{description}</p>
      </div>
      <Space wrap>{actions}</Space>
    </div>
  );
}
export function Connection({ value }: { value?: Connector }) {
  return (
    <span>
      <Badge
        status={value?.online && value.home_matches ? "success" : "warning"}
      />
      {value?.online
        ? value.home_matches
          ? "Harness 已连接"
          : "会话目录不匹配"
        : "Harness 未连接"}
    </span>
  );
}
