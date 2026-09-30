import { LinkOutlined, ReloadOutlined } from "@ant-design/icons";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Form,
  Input,
  InputNumber,
} from "antd";
import React from "react";
import type { Settings } from "../types";

import {
  Connection,
  Heading,
  QueryState,
  time,
  useAction,
  useData,
} from "../components";
export default function SettingsPage() {
  const query = useData<Settings>("/settings");
  const [form] = Form.useForm();
  const action = useAction();
  const [initialized, setInitialized] = React.useState(false);
  const data = query.data;
  React.useEffect(() => {
    if (data && !initialized) {
      form.setFieldsValue(data);
      setInitialized(true);
    }
  }, [data, form, initialized]);
  return (
    <>
      <Heading
        eyebrow="WORKSPACE SETTINGS"
        title="设置"
        description="配置后续审查与本机服务连接。"
      />
      <QueryState query={query}>
        {data && (
          <div className="settings-grid">
            <Card title="审查与服务配置" className="panel">
              <Alert
                type="info"
                showIcon
                message="模型、超时和额度对后续审查生效。更改会话目录或端口前，需先停止监工。"
                className="spaced-bottom"
              />
              <Form
                form={form}
                layout="vertical"
                onFinish={(values) =>
                  action.mutate({
                    path: "/settings",
                    method: "PUT",
                    body: values,
                  })
                }
              >
                <div className="form-grid">
                  <Form.Item
                    name="model"
                    label="GPT 审查模型"
                    rules={[{ required: true, whitespace: true }]}
                  >
                    <Input placeholder="模型名称" />
                  </Form.Item>
                  <Form.Item
                    name="codex_bin"
                    label="Codex CLI"
                    rules={[{ required: true, whitespace: true }]}
                  >
                    <Input />
                  </Form.Item>
                  <Form.Item
                    name="review_timeout"
                    label="单次模型审查超时（秒）"
                    rules={[{ required: true }]}
                  >
                    <InputNumber
                      min={1}
                      max={450}
                      precision={0}
                      className="full"
                    />
                  </Form.Item>
                  <Form.Item
                    name="max_per_day"
                    label="每日审查额度（次）"
                    rules={[{ required: true }]}
                  >
                    <InputNumber
                      min={1}
                      max={10000}
                      precision={0}
                      className="full"
                    />
                  </Form.Item>
                </div>
                <Form.Item
                  name="home"
                  label="Harness 会话目录"
                  rules={[{ required: true, whitespace: true }]}
                >
                  <Input />
                </Form.Item>
                <Form.Item
                  name="history_dir"
                  label="历史审查目录"
                  rules={[{ required: true, whitespace: true }]}
                >
                  <Input />
                </Form.Item>
                <Form.Item
                  name="bridge_port"
                  label="桥接服务端口"
                  rules={[{ required: true }]}
                >
                  <InputNumber min={1024} max={65535} precision={0} />
                </Form.Item>
                <div className="form-footer">
                  <Button
                    type="primary"
                    htmlType="submit"
                    loading={action.isPending}
                  >
                    保存配置
                  </Button>
                  <span className="muted">不会清空已有额度和审查记录</span>
                </div>
              </Form>
            </Card>
            <div>
              <Card title="Harness 连接插件" className="panel">
                <div className="connection-icon">
                  <LinkOutlined />
                </div>
                <h3>
                  <Connection value={data.connector} />
                </h3>
                <p className="muted">
                  插件在 Harness 宿主内发送指令，无需保持浏览器页面打开。
                </p>
                <Descriptions
                  column={1}
                  size="small"
                  items={[
                    {
                      key: "home",
                      label: "插件目录",
                      children: (
                        <span className="break">
                          {data.connector.home || "尚未连接"}
                        </span>
                      ),
                    },
                    {
                      key: "seen",
                      label: "最近心跳",
                      children: time(data.connector.last_seen),
                    },
                  ]}
                />
                <Button
                  block
                  className="spaced"
                  icon={<ReloadOutlined />}
                  onClick={() => query.refetch()}
                >
                  检测连接
                </Button>
                {!data.connector.online && (
                  <Alert
                    className="spaced"
                    type="warning"
                    message="插件未连接"
                    description="按项目 README 安装 connector 插件，并使其指向同一状态目录。"
                  />
                )}
                {data.connector.online && !data.connector.home_matches && (
                  <Alert
                    className="spaced"
                    type="warning"
                    message="目录不匹配"
                    description="请停止监工后，将左侧会话目录设置为插件报告的目录。"
                  />
                )}
              </Card>
              <Card title="本地数据" className="panel spaced">
                <span className="muted">管理状态与操作记录</span>
                <p className="path-label">{data.state_dir}</p>
                <p className="muted">
                  时间显示：Asia/Shanghai
                  <br />
                  管理服务：{window.location.host}
                </p>
              </Card>
            </div>
          </div>
        )}
      </QueryState>
    </>
  );
}
