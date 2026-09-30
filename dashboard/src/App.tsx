import {
  ApartmentOutlined,
  ArrowRightOutlined,
  DashboardOutlined,
  FileSearchOutlined,
  HistoryOutlined,
  SettingOutlined,
} from "@ant-design/icons";
import {
  NavLink,
  Navigate,
  Route,
  Routes,
  useNavigate,
} from "react-router-dom";
import type { Overview } from "./types";

import { Connection, useData } from "./components";
import EventsPage from "./pages/EventsPage";
import OverviewPage from "./pages/OverviewPage";
import ReviewsPage from "./pages/ReviewsPage";
import SessionsPage from "./pages/SessionsPage";
import SettingsPage from "./pages/SettingsPage";
export default function Shell() {
  const overview = useData<Overview>("/overview");
  const navigate = useNavigate();
  const nav = [
    ["/", "总览", <DashboardOutlined />],
    ["/sessions", "会话管理", <ApartmentOutlined />],
    ["/reviews", "审查中心", <FileSearchOutlined />],
    ["/events", "操作日志", <HistoryOutlined />],
    ["/settings", "设置", <SettingOutlined />],
  ] as const;
  return (
    <div className="workspace">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">
            <ApartmentOutlined />
          </div>
          <div>
            <strong>监工工作台</strong>
            <small>DEEPSEEK ↔ GPT</small>
          </div>
        </div>
        <div className="nav-label">工作空间</div>
        <nav>
          {nav.map(([to, label, icon]) => (
            <NavLink key={to} to={to} end={to === "/"}>
              {icon}
              <span>{label}</span>
              {to === "/reviews" && !!overview.data?.counts.awaiting_human && (
                <b>{overview.data.counts.awaiting_human}</b>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-note">
          <span className="live-dot" /> 本机工作空间
          <p>
            所有操作与审查记录
            <br />
            保存在你的电脑上。
          </p>
        </div>
        <div className="sidebar-footer">
          SUPERVISOR <span>v1.0</span>
        </div>
      </aside>
      <div className="main">
        <header className="topbar">
          <span className="breadcrumb">
            工作空间 <span>/</span> 监工管理
          </span>
          <div className="topbar-right">
            <Connection value={overview.data?.connector} />
            <span className="divider" />
            <span>Asia/Shanghai</span>
            <span className="avatar">本</span>
          </div>
        </header>
        <main>
          <Routes>
            <Route path="/" element={<OverviewPage />} />
            <Route path="/sessions" element={<SessionsPage />} />
            <Route path="/reviews" element={<ReviewsPage />} />
            <Route path="/events" element={<EventsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
        <footer className="page-footer">
          <span>DeepSeek ↔ GPT · 本地监工管理</span>
          <button onClick={() => navigate("/settings")}>
            连接与配置 <ArrowRightOutlined />
          </button>
        </footer>
      </div>
    </div>
  );
}
