import { ErrorNotice, errorText } from "./errors";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Alert, App as AntApp, Badge, Button, Space, Spin, Tag } from "antd";
import React from "react";
import { api } from "./api";
import type { Connector } from "./types";

export const labels: Record<string, string> = {
  planning: "待方案审批",
  developing: "开发中",
  waiting: "等待审查员审批 / 验收",
  verified: "审查员验收通过",
  manual_released: "人工放行，未通过审查员验收",
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
  autopilot_created: "建立研发记录",
  autopilot_transition: "研发阶段变更",
  autopilot_token_limit_changed: "调整今日开发 Token 上限",
  autopilot_monitor_error: "巡检异常",
  autopilot_daily_error: "日报调度异常",
  autopilot_error: "研发调度异常",
  native_released: "人工放行当前审批点",
  native_consumed: "会话已消费审批结果",
  native_cancelled: "插件审批点已取消",
  codex_started: "审查员 开始审查",
  codex_finished: "审查员 审查结束",
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
      message.error(errorText(error));
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
        description={<ErrorNotice value={query.error} subject="管理服务"/>}
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
      {value?.online && (
        <Tag color={value.native_supervision ? "blue" : "default"}>
          {value.native_supervision
            ? "插件强制监管已启用"
            : "当前连接不支持强制监管"}
        </Tag>
      )}
    </span>
  );
}
