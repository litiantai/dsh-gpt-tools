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
  useLocation,
} from "react-router-dom";
import type { Overview } from "./types";
import { useEffect, useState } from 'react';
import { projectSection, projectSections } from './projectNavigation';

import { Connection, useData } from "./components";
import EventsPage from "./pages/EventsPage";
import OverviewPage from "./pages/OverviewPage";
import ReviewsPage from "./pages/ReviewsPage";
import SessionsPage from "./pages/SessionsPage";
import SettingsPage from "./pages/SettingsPage";
import AutopilotPage from "./pages/AutopilotPage";
import PlatformUpdate from './PlatformUpdate';
export default function Shell() {
  const overview = useData<Overview>("/overview");
  const navigate = useNavigate();
  const location = useLocation();
  const pureChat = location.pathname === '/autopilot' && projectSection(new URLSearchParams(location.search)) === 'intelligence';
  const projectView = location.pathname === '/autopilot' || location.pathname === '/';
  const [projectUrl,setProjectUrl]=useState(()=>sessionStorage.getItem('last-project-url') || '/autopilot');
  useEffect(()=>{
    if(location.pathname==='/autopilot') {
      const url=location.pathname+location.search;
      setProjectUrl(url);
      sessionStorage.setItem('last-project-url',url);
    }
  },[location.pathname,location.search]);
  const nav = [
    ["/supervisor", "监工总览", <DashboardOutlined />],
    ["/sessions", "全部会话", <ApartmentOutlined />],
    ["/reviews", "全部审查", <FileSearchOutlined />],
    ["/events", "平台日志", <HistoryOutlined />],
    ["/settings", "平台设置", <SettingOutlined />],
  ] as const;
  return (
    <div className={`workspace ${projectView ? 'project-workspace' : ''} ${pureChat ? 'pure-chat-workspace' : ''}`}>
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">
            <ApartmentOutlined />
          </div>
          <div>
            <strong>研发控制台</strong>
            <small>DEEPSEEK ↔ GPT</small>
          </div>
        </div>
        <div className="nav-label">项目工作区</div>
        <nav aria-label="项目导航">
          <NavLink to={projectUrl} aria-label="持续研发"><ApartmentOutlined/><span>持续研发</span></NavLink>
        </nav>
        <div className="nav-label platform-nav-label">平台管理 · 全部项目</div>
        <nav aria-label="平台管理">
          {nav.map(([to, label, icon]) => (
            <NavLink key={to} to={to} aria-label={label} end>
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
            {projectView ? '项目工作区' : '平台管理'} <span>/</span> {projectView ? projectSections[projectSection(new URLSearchParams(location.search))].label : nav.find(([path])=>path===location.pathname)?.[1] || '监工管理'}
          </span>
          <div className="topbar-right">
            <Connection value={overview.data?.connector} />
            <span className="divider" />
            <span>Asia/Shanghai</span>
            <span className="avatar">本</span>
          </div>
        </header>
        <main>
          <PlatformUpdate />
          <Routes>
            <Route path="/autopilot" element={<AutopilotPage />} />
            <Route path="/" element={<Navigate to="/autopilot" replace />} />
            <Route path="/supervisor" element={<OverviewPage />} />
            <Route path="/sessions" element={<SessionsPage />} />
            <Route path="/reviews" element={<ReviewsPage />} />
            <Route path="/events" element={<EventsPage />} />
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </main>
        <footer className="page-footer">
          <span>持续研发控制中心 · 数据保存在本机</span>
          <button onClick={() => navigate("/settings")}>
            平台连接与配置 <ArrowRightOutlined />
          </button>
        </footer>
      </div>
    </div>
  );
}
