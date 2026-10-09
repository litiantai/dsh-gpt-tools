import { ErrorNotice } from '../errors';
import { unpack } from '../RecordDetails';
import {
  ApartmentOutlined,
  ArrowRightOutlined,
  CheckCircleOutlined,
  ClockCircleOutlined,
  FileSearchOutlined,
  PlayCircleOutlined,
  ReloadOutlined,
  SettingOutlined,
  StopOutlined,
  ThunderboltOutlined,
} from "@ant-design/icons";
import {
  Alert,
  Badge,
  Button,
  Card,
  Empty,
  Progress,
  Space,
  Table,
} from "antd";
import { useNavigate } from "react-router-dom";
import type { Overview, Review } from "../types";

import {
  Connection,
  Heading,
  QueryState,
  Status,
  labels,
  time,
  useAction,
  useData,
} from "../components";
export default function OverviewPage() {
  const query = useData<Overview>("/overview");
  const reviews = useData<Review[]>("/reviews");
  const action = useAction();
  const navigate = useNavigate();
  const data = query.data;
  return (
    <>
      <Heading
        eyebrow="WORKSPACE OVERVIEW"
        title="总览"
        description="掌握会话进展，在需要的时候介入。"
        actions={
          <Button
            icon={<ReloadOutlined />}
            onClick={() => {
              void query.refetch();
              void reviews.refetch();
            }}
          >
            刷新状态
          </Button>
        }
      />
      <QueryState query={query}>
        {data && (
          <>
            {data.service.running && !data.service.compatible && (
              <Alert
                className="spaced-bottom"
                type="warning"
                showIcon
                message="检测到旧版监工服务"
                description="历史记录仍可查看。请通过旧版 CLI 停止服务，再从这里启动新版监工，启用审查控制。"
              />
            )}
            <section className="service-banner">
              <div>
                <div className="banner-tag">
                  <span
                    className={
                      data.service.running ? "live-dot" : "offline-dot"
                    }
                  />
                  {data.service.running ? "监工服务运行中" : "监工服务已停止"}
                </div>
                <h2>
                  {data.service.running
                    ? "每一次交接，都有据可查。"
                    : "准备好开始下一次审查。"}
                </h2>
                <p>
                  {data.service.running
                    ? "自动跟进交接请求，保留审查依据，并在需要时交由你处理。"
                    : "启动监工后，DeepSeek 的交接请求会进入审查中心。"}
                </p>
                <Space wrap>
                  <Button
                    type="primary"
                    icon={
                      data.service.running ? (
                        <StopOutlined />
                      ) : (
                        <PlayCircleOutlined />
                      )
                    }
                    loading={action.isPending}
                    disabled={data.service.running && !data.service.compatible}
                    aria-label={data.service.running ? "停止监工" : "启动监工"}
                    onClick={() =>
                      action.mutate({
                        path: data.service.running
                          ? "/service/stop"
                          : "/service/start",
                      })
                    }
                  >
                    {data.service.running ? "停止监工" : "启动监工"}
                  </Button>
                  <Button type="text" onClick={() => navigate("/reviews")}>
                    进入审查中心 <ArrowRightOutlined />
                  </Button>
                </Space>
              </div>
              <div className="flow-art" aria-hidden="true">
                <div className="flow-node">
                  DS<span>DeepSeek</span>
                </div>
                <div className="flow-connector">
                  <span />
                  <small>交接 · 审查 · 反馈</small>
                </div>
                <div className="flow-node gpt">
                  <CheckCircleOutlined />
                  <span>审查员</span>
                </div>
              </div>
            </section>
            <div className="metrics">
              {[
                {
                  label: "已发现会话",
                  value: data.sessions,
                  foot: "来自本机会话目录",
                  icon: <ApartmentOutlined />,
                },
                {
                  label: "进行中的审查",
                  value: (data.counts.running || 0) + (data.counts.queued || 0),
                  foot: "包含等待执行的审查",
                  icon: <ThunderboltOutlined />,
                },
                {
                  label: "待人工审批",
                  value: data.counts.awaiting_human || 0,
                  foot: "等待你的判断与指令",
                  icon: <ClockCircleOutlined />,
                },
                {
                  label: "今日审查额度",
                  value: `${data.quota.used} / ${data.quota.limit}`,
                  foot: `${data.quota.day} · 每日计数`,
                  icon: <FileSearchOutlined />,
                },
              ].map((item, index) => (
                <button
                  className="metric"
                  key={item.label}
                  onClick={() =>
                    navigate(
                      index === 0
                        ? "/sessions"
                        : index === 3
                          ? "/settings"
                          : "/reviews" +
                            (index === 2 ? "?status=awaiting_human" : ""),
                    )
                  }
                >
                  <div>
                    <span>{item.label}</span>
                    <span className={`metric-icon tone-${index}`}>
                      {item.icon}
                    </span>
                  </div>
                  <strong>{item.value}</strong>
                  <small>{item.foot}</small>
                </button>
              ))}
            </div>
            <div className="overview-grid">
              <Card
                className="panel"
                title="最近审查"
                extra={
                  <Button type="link" onClick={() => navigate("/reviews")}>
                    查看全部 <ArrowRightOutlined />
                  </Button>
                }
              >
                <QueryState query={reviews}>
                  <Table
                    pagination={false}
                    size="middle"
                    rowKey="id"
                    dataSource={reviews.data?.slice(0, 6)}
                    scroll={{ x: 500 }}
                    locale={{
                      emptyText: (
                        <Empty description="还没有审查记录">
                          <span className="muted">
                            交接请求或手动审查会显示在这里
                          </span>
                        </Empty>
                      ),
                    }}
                    columns={[
                      {
                        title: "审查任务",
                        render: (_, r) => (
                          <button
                            className="text-link"
                            onClick={() => navigate(`/reviews?id=${r.id}`)}
                          >
                            {labels[r.packet.phase]}
                            <small>{r.session_id.slice(0, 24)}…</small>
                          </button>
                        ),
                      },
                      {
                        title: "状态",
                        render: (_, r) => <Status value={r.status} />,
                      },
                      {
                        title: "时间",
                        render: (_, r) => (
                          <span className="muted">{time(r.created)}</span>
                        ),
                      },
                    ]}
                  />
                </QueryState>
              </Card>
              <Card className="panel" title="运行环境">
                <div className="environment-row">
                  <span>监工服务</span>
                  <Badge
                    status={data.service.running ? "success" : "default"}
                    text={data.service.running ? "运行中" : "已停止"}
                  />
                </div>
                <div className="environment-row">
                  <span>会话连接</span>
                  <Connection value={data.connector} />
                </div>
                <div className="environment-row">
                  <span>运行方式</span>
                  <strong>本机 · 单用户</strong>
                </div>
                <div className="quota-block">
                  <div>
                    <span>今日额度使用</span>
                    <strong>
                      {data.quota.used} / {data.quota.limit}
                    </strong>
                  </div>
                  <Progress
                    percent={Math.min(
                      100,
                      Math.round((data.quota.used / data.quota.limit) * 100),
                    )}
                    showInfo={false}
                    strokeColor="var(--primary)"
                  />
                  <p>
                    只有启动模型审查才会消耗额度。
                    <br />
                    看板刷新不会调用模型。
                  </p>
                </div>
                <Button
                  block
                  onClick={() => navigate("/settings")}
                  icon={<SettingOutlined />}
                >
                  管理配置
                </Button>
              </Card>
            </div>
            {data.errors.length > 0 && (
              <Card className="panel exceptions" title="近期需关注">
                <div className="exception-list">
                  {data.errors.map((item) => (
                    <div key={item.id}>
                      <span className="exception-mark">!</span>
                      <div>
                        <ErrorNotice value={unpack(item.result)}/>
                        <small>{time(item.updated)}</small>
                      </div>
                      <Button
                        onClick={() => navigate(`/reviews?id=${item.id}`)}
                      >
                        查看详情
                      </Button>
                    </div>
                  ))}
                </div>
              </Card>
            )}
          </>
        )}
      </QueryState>
    </>
  );
}
