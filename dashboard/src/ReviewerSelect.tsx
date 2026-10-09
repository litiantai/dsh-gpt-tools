import { ErrorNotice, serializeError } from './errors';
import React from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Button, Select, Space } from "antd";
import type { DefaultOptionType } from "antd/es/select";
import { ReloadOutlined } from "@ant-design/icons";
import { api } from "./api";
import { time } from "./components";
import type { ReviewerSelection, ModelCatalog } from "./types";

export const reviewerNames: Record<string, string> = {
  codex: "Codex/GPT",
  claude: "Claude CLI",
  harness: "DeepSeek Harness",
};
export function ReviewerSelect({
  value,
  onChange,
  connection,
  allowedProviders,
}: {
  value?: ReviewerSelection;
  onChange?: (value: ReviewerSelection) => void;
  connection: () => Record<string, unknown>;
  allowedProviders?: string[];
}) {
  const provider = value?.provider || "codex";
  const catalog = useQuery<ModelCatalog>({
    queryKey: ["reviewer-models", provider],
    queryFn: () => api(`/reviewers/${provider}/models`),
    staleTime: 300000,
    retry: false,
  });
  const [refreshing, setRefreshing] = React.useState(false);
  const [refreshed, setRefreshed] = React.useState<{
    provider: string;
    data: ModelCatalog;
  } | null>(null);
  const [error, setError] = React.useState("");
  const data = refreshed?.provider === provider ? refreshed.data : catalog.data;
  const key = (id: string, route?: string) => JSON.stringify([route || "", id]);
  const selected = value?.model
    ? key(value.model, value.model_provider)
    : undefined;
  const models = data?.models || [];
  const options = models.map((m) => ({
    value: key(m.id, m.model_provider),
    label: `${m.provider_name ? `${m.provider_name} / ` : ""}${m.name} (${m.id})`,
  }));
  const missing = selected && !options.some((o) => o.value === selected);
  if (missing)
    options.unshift({
      value: selected,
      label: `${value?.model} · 已保存，目录未确认`,
    });
  const choices = options.map((o) => ({
    ...o,
    disabled: !!missing && o.value === selected,
  }));
  const groups = new Map<string, typeof choices>();
  for (const option of choices) {
    const model = models.find(
      (m) => key(m.id, m.model_provider) === option.value,
    );
    const group =
      model?.provider_name || model?.model_provider || "已保存的选择";
    groups.set(group, [...(groups.get(group) || []), option]);
  }
  return (
    <div className="reviewer-select">
      <Space.Compact block>
        <Select
          aria-label="审查器"
          value={provider}
          style={{ width: 180 }}
          options={Object.entries(reviewerNames).filter(([key])=>!allowedProviders || allowedProviders.includes(key)).map(([value, label]) => ({
            value,
            label,
          }))}
          onChange={(provider) => {
            setError("");
            onChange?.({ provider, model: "" });
          }}
        />
        <Select<string, DefaultOptionType>
          aria-label="审查模型"
          showSearch
          optionFilterProp="label"
          value={selected}
          style={{ flex: 1, minWidth: 0 }}
          placeholder="选择工具提供的模型"
          loading={catalog.isFetching || refreshing}
          options={
            provider === "harness"
              ? Array.from(groups, ([label, options]) => ({ label, options }))
              : choices
          }
          onChange={(key) => {
            const [model_provider, model] = JSON.parse(key);
            onChange?.({
              ...value,
              provider,
              model,
              ...(model_provider ? { model_provider } : {}),
            });
          }}
        />
        <Button
          aria-label="刷新模型"
          title="刷新模型列表"
          icon={<ReloadOutlined />}
          loading={refreshing}
          onClick={async () => {
            setRefreshing(true);
            setError("");
            try {
              setRefreshed({
                provider,
                data: await api(
                  `/reviewers/${provider}/models/refresh`,
                  connection(),
                ),
              });
            } catch (e) {
              setError((e as Error).message);
            } finally {
              setRefreshing(false);
            }
          }}
        />
      </Space.Compact>
      <small className="muted">
        {data?.source || reviewerNames[provider]} ·{" "}
        {data?.fetched_at
          ? `更新于 ${time(data.fetched_at)}`
          : "尚未获取模型目录"}
        {data?.stale ? " · 缓存结果" : ""}
      </small>
      {(error || data?.error || catalog.error) && (
        <Alert
          type="warning"
          showIcon
          message={<ErrorNotice value={error || data?.error || catalog.error}/>}
        />
      )}
    </div>
  );
}
