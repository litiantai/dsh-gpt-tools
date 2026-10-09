import { defineConfig } from "@playwright/test";
import { execFileSync } from "node:child_process";
const port = process.env.DSH_E2E_PORT || execFileSync("python3", ["-c", "import socket; s=socket.socket(); s.bind(('127.0.0.1',0)); print(s.getsockname()[1]); s.close()"], { encoding: "utf8" }).trim();
process.env.DSH_E2E_PORT = port;
const origin = `http://127.0.0.1:${port}`;
export default defineConfig({
  testDir: "./tests/browser",
  workers: 1,
  timeout: 30000,
  use: {
    baseURL: origin,
    headless: true,
    viewport: { width: 1440, height: 1050 },
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "python3 -B tests/serve_fixture.py",
    url: origin,
    reuseExistingServer: false,
    gracefulShutdown: { signal: "SIGTERM", timeout: 10000 },
    timeout: 20000,
  },
  outputDir: ".playwright/results",
  reporter: "list",
});
