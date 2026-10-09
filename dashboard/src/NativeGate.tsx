import { ErrorNotice, errorText, serializeError } from './errors';
import React from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Input, Space, Tag } from "antd";
import { api } from "./api";
import { reviewerNames } from "./ReviewerSelect";
import type { ReviewerSelection } from "./types";
import { time, useAction } from "./components";

interface Gate {
  id: string;
  status: string;
  reason: string;
  ready: number;
  online: boolean;
  review_id: string | null;
  override?: { actor: string; reason: string; at: string };
  review?: { execution_done: boolean; reviewer?: ReviewerSelection };
}
/** One approval point; bypasses never replace the model's original verdict. */
export function NativeGate({ id }: { id?: string | null }) {
  const query = useQuery<Gate>({
    queryKey: [`/native-gates/${id}`],
    queryFn: () => api(`/native-gates/${id}`),
    enabled: !!id,
    refetchInterval: 1500,
  });
  const action = useAction();
  const [reason, setReason] = React.useState("");
  React.useEffect(() => setReason(""), [id]);
  const gate = query.data;
  if (!id) return null;
  if (query.error)
    return (
      <Alert
        type="error"
        message="审批点暂时不可用"
        description={<ErrorNotice value={query.error}/>}
      />
    );
  if (!gate) return null;
  if (gate.override)
    return (
      <Alert
        className="spaced"
        type="warning"
        showIcon
        message="人工放行，未通过审查员验收"
        description={`${gate.override.actor} · ${time(gate.override.at)} · ${gate.override.reason}`}
      />
    );
  const blocked = gate.status === "blocked";
  const canAct =
    blocked &&
    gate.online &&
    !!gate.ready &&
    (!gate.review || gate.review.execution_done);
  return (
    <div className="spaced">
      <Alert
        type={blocked ? "warning" : "info"}
        showIcon
        message={
          blocked
            ? "监管已阻塞"
            : gate.status === "waiting_background"
              ? "等待后台任务及子代理结束"
              : gate.status === "consumed"
                ? "当前审批点已消费"
                : gate.status === "cancelled"
                  ? "审批点已取消"
                  : `正在等待 ${reviewerNames[gate.review?.reviewer?.provider || ""] || "审查员"} 审批`
        }
        description={gate.reason?<ErrorNotice value={gate.reason}/>:"审查结束前，会话不能继续执行。"}
      />
      {!gate.online && !["consumed", "cancelled"].includes(gate.status) && (
        <Tag color="warning">插件暂停连接已中断，等待原会话恢复</Tag>
      )}
      {blocked && (
        <>
          <Input.TextArea
            className="spaced"
            aria-label="人工放行原因"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            maxLength={2000}
            rows={2}
            placeholder="填写仅放行当前审批点的原因，后续监管继续有效"
          />
          <Space className="spaced">
            <Button
              disabled={!canAct}
              loading={action.isPending}
              onClick={() =>
                action.mutate({
                  path: `/native-gates/${id}/retry`,
                  body: { review_id: gate.review_id },
                })
              }
            >
              重试监管审查
            </Button>
            <Button
              danger
              disabled={!canAct || !reason.trim()}
              loading={action.isPending}
              onClick={() =>
                action.mutate({
                  path: `/native-gates/${id}/release`,
                  body: { review_id: gate.review_id, reason },
                })
              }
            >
              仅放行当前审批点
            </Button>
          </Space>
        </>
      )}
    </div>
  );
}
