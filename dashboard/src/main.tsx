import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import DashboardTheme from "./theme";
import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import "./style.css";
import "./project-flow.css";

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
    <QueryClientProvider client={client}>
      <BrowserRouter>
        <DashboardTheme><Shell /></DashboardTheme>
      </BrowserRouter>
    </QueryClientProvider>
  </React.StrictMode>,
);
