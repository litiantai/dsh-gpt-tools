import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App as AntApp, ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./style.css";

import Shell from "./App";
const client = new QueryClient({
  defaultOptions: {
    queries: {
      retry: 1,
      refetchInterval: () => (document.hidden ? 15000 : 3000),
      refetchIntervalInBackground: true,
      staleTime: 1500,
    },
    mutations: { retry: false },
  },
});
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      button={{ autoInsertSpace: false }}
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#4566ed",
          colorBgLayout: "#f5f7fb",
          colorText: "#202a41",
          colorTextSecondary: "#7a8497",
          borderRadius: 10,
          fontFamily:
            'Inter, -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif',
        },
        components: {
          Table: {
            headerBg: "#f9fafc",
            headerColor: "#7a8497",
            cellPaddingBlock: 18,
          },
          Button: { controlHeight: 38 },
          Card: { headerFontSize: 15 },
        },
      }}
    >
      <AntApp>
        <QueryClientProvider client={client}>
          <BrowserRouter>
            <Shell />
          </BrowserRouter>
        </QueryClientProvider>
      </AntApp>
    </ConfigProvider>
  </React.StrictMode>,
);
