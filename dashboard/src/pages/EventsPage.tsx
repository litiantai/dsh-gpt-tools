import { RecordValue } from '../RecordDetails';
import { ReloadOutlined, SearchOutlined } from "@ant-design/icons";
import { Button, Card, Input, Table } from "antd";
import React from "react";
import { useNavigate } from "react-router-dom";
import type { Event } from "../types";

import { Heading, QueryState, eventLabels, time, useData } from "../components";
export default function EventsPage() {
  const query = useData<Event[]>("/events");
  const [search, setSearch] = React.useState("");
  const navigate = useNavigate();
  return (
    <>
      <Heading
        eyebrow="ACTIVITY LOG"
        title="操作日志"
        description="追溯服务事件、审查流转和人工操作。"
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
            placeholder="搜索事件、会话或详情"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            allowClear
          />
          <span className="muted">最近 500 条记录</span>
        </div>
        <QueryState query={query}>
          <Table
            rowKey="id"
            pagination={{ pageSize: 15, showSizeChanger: false }}
            scroll={{ x: 800 }}
            dataSource={query.data?.filter((e) =>
              (
                eventLabels[e.kind] +
                e.kind +
                e.session_id +
                JSON.stringify(e.detail)
              )
                .toLowerCase()
                .includes(search.toLowerCase()),
            )}
            expandable={{
              expandedRowRender: (e) => (
                <RecordValue value={e.detail}/>
              ),
            }}
            columns={[
              {
                title: "时间",
                width: 180,
                render: (_, e) => (
                  <span className="muted nowrap">{time(e.at)}</span>
                ),
              },
              {
                title: "事件",
                render: (_, e) => (
                  <strong>{eventLabels[e.kind] || e.kind}</strong>
                ),
              },
              {
                title: "关联会话",
                render: (_, e) =>
                  e.session_id ? (
                    <button
                      className="text-link"
                      onClick={() => navigate(`/sessions?id=${e.session_id}`)}
                    >
                      {e.session_id}
                    </button>
                  ) : (
                    "—"
                  ),
              },
              {
                title: "审查",
                width: 100,
                render: (_, e) =>
                  e.review_id ? (
                    <Button
                      size="small"
                      onClick={() => navigate(`/reviews?id=${e.review_id}`)}
                    >
                      查看
                    </Button>
                  ) : (
                    "—"
                  ),
              },
            ]}
          />
        </QueryState>
      </Card>
    </>
  );
}
