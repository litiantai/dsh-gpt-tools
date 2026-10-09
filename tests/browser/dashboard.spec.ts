import { test, expect } from "@playwright/test";
import { readFile, readdir } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import { join } from "node:path";
const meta = async () =>
  JSON.parse(await readFile(".dashboard/e2e.json", "utf8"));

test.afterEach(async ({ page }, info) => {
  if (info.status !== info.expectedStatus) {
    try {
      const config = await meta();
      const results = [];
      for (const id of await readdir(join(config.state, "reviews"))) {
        const read = async (name) =>
          readFile(join(config.state, "reviews", id, name), "utf8").catch(
            () => "",
          );
        results.push({
          id,
          result: await read("result.json"),
          stderr: await read("stderr.log"),
        });
      }
      await info.attach("review-diagnostics", {
        body: JSON.stringify(results, null, 2),
        contentType: "application/json",
      });
    } catch {}
  }
});

test.describe.serial("live dashboard with isolated local services", () => {
  test("five pages load without browser errors; desktop and narrow layouts", async ({
    page,
  }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto("/supervisor");
    await expect(
      page.getByRole("heading", { name: "总览", exact: true }),
    ).toBeVisible();
    await expect(page.getByText("已发现会话")).toBeVisible();
    await page.screenshot({
      path: ".playwright/overview-desktop.png",
      fullPage: true,
    });
    for (const [href, title] of [
      ["/sessions", "会话管理"],
      ["/reviews", "审查中心"],
      ["/events", "操作日志"],
      ["/settings", "设置"],
    ]) {
      await page.locator(`nav a[href="${href}"]`).click();
      await expect(
        page.getByRole("heading", { name: title, exact: true }),
      ).toBeVisible();
    }
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/supervisor");
    await expect(
      page.getByRole("heading", { name: "总览", exact: true }),
    ).toBeVisible();
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
    await expect(page.getByText("今日额度使用")).toBeVisible();
    await page.screenshot({
      path: ".playwright/overview-mobile.png",
      fullPage: true,
    });
    expect(errors).toEqual([]);
  });
  test("start service and complete an observation review", async ({ page }) => {
    await page.goto("/supervisor");
    await page.getByRole("button", { name: "启动监工", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "停止监工", exact: true }),
    ).toBeVisible({ timeout: 10000 });
    await page.goto("/reviews");
    await page.getByRole("button", { name: "发起观察审查" }).click();
    await page.getByLabel("会话", { exact: true }).click();
    await page.getByText("看板集成测试会话", { exact: true }).click();
    await page.getByLabel("审查目标与摘要").fill("核对 source.txt 测试文件");
    await page.getByLabel("审查范围（相对路径，每行一个）").fill("source.txt");
    await page.getByRole("button", { name: "开始审查", exact: true }).click();
    await expect(page.getByText("已完成", { exact: true })).toBeVisible({
      timeout: 15000,
    });
    await page
      .getByRole("button", { name: /^展开会话 看板集成测试会话/ })
      .click();
    await page.getByRole("button", { name: /^阶段检查/ }).click();
    await expect(page.getByText("观察审查：未验证会话暂停")).toBeVisible();
  });
  test("manual decision releases a real blocking HTTP handoff", async ({
    page,
  }) => {
    const config = await meta();
    await page.goto("/sessions");
    await page.getByRole("switch").click();
    await expect(page.getByRole("switch")).toHaveAttribute(
      "aria-checked",
      "true",
    );
    const key = await readFile(join(config.state, "bridge.key"), "utf8");
    const id = randomUUID();
    const handoff = fetch(`http://127.0.0.1:${config.bridge_port}/handoff`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${key}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        session_id: config.session,
        cwd: config.project,
        request_id: id,
        phase: "plan",
        scope: ["source.txt"],
        summary: "人工确认方案测试",
      }),
    }).then((r) => r.json());
    await page.goto(`/reviews?id=${id}`);
    await expect(
      page.getByText("提交人工结论", { exact: true }).first(),
    ).toBeVisible({ timeout: 15000 });
    await page.getByRole("combobox", { name: "人工结论", exact: true }).click();
    await page
      .locator(".ant-select-item-option-content")
      .filter({ hasText: "批准" })
      .click();
    await page
      .getByRole("textbox", { name: "审批指令", exact: true })
      .fill("方案通过，请继续执行约定的测试。");
    await page
      .getByRole("button", { name: "提交人工结论", exact: true })
      .click();
    const result = await handoff;
    expect(result.decision).toBe("approve");
    expect(result.pause_proof.pause_verified).toBe(true);
    await expect(page.getByText("人工审批", { exact: true })).toBeVisible();
  });
  test("plugin delivers steer once, verifies consumption, and stops a session", async ({
    page,
  }) => {
    const config = await meta();
    await page.goto("/sessions");
    await page.getByRole("button", { name: "插话", exact: true }).click();
    await page
      .getByRole("textbox", { name: "人工指令", exact: true })
      .fill("集成测试：核对错误边界");
    await page.getByRole("button", { name: "发送指令", exact: true }).click();
    await expect(page.getByText("会话已消费", { exact: true })).toBeVisible({
      timeout: 15000,
    });
    const records = (
      await readFile(join(config.state, "sent-messages.jsonl"), "utf8")
    )
      .trim()
      .split("\n");
    expect(records).toHaveLength(1);
    expect(JSON.parse(records[0]).payload.mode).toBe("steer");
    await page.keyboard.press("Escape");
    await page
      .getByRole("button", { name: "停止 看板集成测试会话", exact: true })
      .click();
    await page
      .getByRole("button", { name: "停止当前轮次", exact: true })
      .click();
    await expect
      .poll(async () => {
        try {
          return JSON.parse(
            await readFile(join(config.state, "stopped.json"), "utf8"),
          ).payload.sessionId;
        } catch {
          return "";
        }
      })
      .toBe(config.session);
  });
  test("save configuration and stop service while dashboard stays available", async ({
    page,
  }) => {
    await page.goto("/settings");
    await page.getByLabel("每日审查额度（次）").fill("15");
    await page.getByRole("button", { name: "保存配置", exact: true }).click();
    await expect(page.getByText("操作已提交")).toBeVisible();
    await page.reload();
    await expect(page.getByLabel("每日审查额度（次）")).toHaveValue("15");
    await page.goto("/supervisor");
    await page.getByRole("button", { name: "停止监工", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "启动监工", exact: true }),
    ).toBeVisible({ timeout: 10000 });
    await page.goto("/reviews");
    await expect(
      page.getByRole("heading", { name: "审查中心", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByText("已完成", { exact: true }).first(),
    ).toBeVisible();
  });
});

test("native blocked gate requires a reason and records a single explicit release", async ({
  page,
}) => {
  const config = await meta();
  const key = (
    await readFile(join(config.state, "connector.key"), "utf8")
  ).trim();
  const gateId = randomUUID();
  const owner = randomUUID();
  const native = async (body: Record<string, unknown>) => {
    const r = await fetch("http://127.0.0.1:13085/api/connector/native", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${key}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        home: config.home,
        owner,
        gate_id: gateId,
        ...body,
      }),
    });
    expect(r.ok).toBeTruthy();
    return r.json();
  };
  await native({
    action: "open",
    ready: true,
    reason: "模拟服务故障，等待人工处理",
    packet: {
      session_id: config.session,
      phase: "acceptance",
      summary: "独立插件监管测试",
      pause_seq: 10000,
    },
  });
  await native({
    action: "state",
    session_id: config.session,
    state: "blocked",
  });
  await page.goto(`/sessions?id=${config.session}`);
  await expect(page.getByText("监管已阻塞", { exact: true })).toBeVisible();
  const release = page.getByRole("button", { name: "仅放行当前审批点" });
  await expect(release).toBeDisabled();
  await page.getByLabel("人工放行原因").fill("人工已确认本次测试结果");
  await expect(release).toBeEnabled();
  await page.screenshot({
    path: ".playwright/native-gate-blocked.png",
    fullPage: true,
    animations: "disabled",
  });
  await release.click();
  await expect(
    page.getByText("人工放行，未通过审查员验收", { exact: true }),
  ).toBeVisible();
  await native({ action: "ack" });
  await native({
    action: "state",
    session_id: config.session,
    state: "manual_released",
  });
});

test("reviews group by session ID and preserve history filters and detail links", async ({
  page,
}) => {
  const sessions = ["session-a", "session-b"].map((id) => ({
    id,
    title: "同名小游戏",
    cwd: "/tmp/games",
    status: "idle",
    subagent: false,
  }));
  const review = (
    id: string,
    session_id: string,
    created: number,
    status: string,
    phase: string,
  ) => ({
    id,
    session_id,
    created,
    updated: created,
    status,
    mode: "native_handoff",
    packet: { phase, summary: `记录 ${id}`, cwd: "/tmp/games", scope: ["."] },
    result: {
      decision:
        status === "blocked"
          ? "blocked"
          : phase === "acceptance"
            ? "done"
            : "approve",
      summary: "核验结果",
      checks: [],
      issues: [],
    },
  });
  const records = [
    review("a-plan", "session-a", 100, "completed", "plan"),
    review("a-failed", "session-a", 200, "blocked", "acceptance"),
    review("a-done", "session-a", 300, "completed", "acceptance"),
    review("b-plan", "session-b", 150, "completed", "plan"),
    review("missing-session", "session-missing", 50, "completed", "checkpoint"),
  ];
  await page.route("**/api/sessions", (route) =>
    route.fulfill({ json: sessions }),
  );
  await page.route("**/api/reviews", (route) =>
    route.fulfill({ json: records }),
  );
  await page.route("**/api/reviews/*", (route) =>
    route.fulfill({
      json: records.find((r) => route.request().url().endsWith(`/${r.id}`)),
    }),
  );
  await page.goto("/reviews");
  const toggle = page.getByRole("button", {
    name: "展开会话 同名小游戏（session-a）",
    exact: true,
  });
  await expect(page.getByRole("button", { name: /^展开会话/ })).toHaveCount(3);
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(page.getByText("记录 a-failed", { exact: true })).toBeHidden();
  await expect(page.locator(".ant-table-row").first()).toContainText(
    "session-a",
  );
  await toggle.click();
  const history = page.locator(".ant-table-expanded-row");
  await expect(history.getByRole("button", { name: /记录/ })).toHaveCount(3);
  await expect(history.locator(".ant-table-row").first()).toContainText(
    "记录 a-done",
  );
  await expect(history.getByText("插件强制监管")).toHaveCount(3);
  await history.getByRole("button", { name: /记录 a-failed/ }).click();
  await expect(page).toHaveURL(/id=a-failed/);
  await expect(
    page.locator(".ant-drawer").getByText("a-failed", { exact: true }),
  ).toBeVisible();
  await page.keyboard.press("Escape");
  await page
    .getByRole("button", {
      name: "收起会话 同名小游戏（session-a）",
      exact: true,
    })
    .click();
  await expect(page.getByText("记录 a-failed", { exact: true })).toBeHidden();
  await page.getByPlaceholder("搜索摘要、会话或审查 ID").fill("同名小游戏");
  await expect(page.getByRole("button", { name: /^展开会话/ })).toHaveCount(2);
  await page.getByPlaceholder("搜索摘要、会话或审查 ID").fill("a-failed");
  await expect(page.getByRole("button", { name: /^展开会话/ })).toHaveCount(1);
  await expect(page.getByText("1 / 3 条匹配")).toBeVisible();
  await expect(page.getByText("验收通过", { exact: true })).toBeVisible();
  await page.goto("/reviews?status=blocked&session=session-a");
  await expect(page.getByText("1 / 3 条匹配")).toBeVisible();
  await expect(page.getByText("验收通过", { exact: true })).toBeVisible();
  await toggle.click();
  await expect(history.getByRole("button", { name: /记录/ })).toHaveCount(1);
  await expect(
    history.getByRole("button", { name: /记录 a-failed/ }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.goto("/reviews?id=b-plan");
  await expect(
    page.locator(".ant-drawer").getByText("b-plan", { exact: true }),
  ).toBeVisible();
});

test("settings use live model dropdowns and preserve unified and stage selections", async ({
  page,
}) => {
  const catalog = (provider: string) => ({
    provider,
    models: [
      {
        id: `${provider}-one`,
        name: "模型一",
        ...(provider === "harness"
          ? { model_provider: "route-a", provider_name: "提供商甲" }
          : {}),
      },
      {
        id: `${provider}-two`,
        name: "模型二",
        ...(provider === "harness"
          ? { model_provider: "route-b", provider_name: "提供商乙" }
          : {}),
      },
    ],
    source: provider,
    fetched_at: 1791001000,
    stale: false,
    error: null,
  });
  await page.route("**/api/reviewers/*/models", (route) =>
    route.fulfill({ json: catalog(route.request().url().split("/").at(-2)!) }),
  );
  await page.route("**/api/reviewers/*/models/refresh", (route) =>
    route.fulfill({
      json: {
        ...catalog("claude"),
        stale: true,
        error: "模拟刷新失败，保留缓存",
      },
    }),
  );
  await page.goto("/settings");
  await page
    .getByRole("combobox", { name: "审查器", exact: true })
    .press("Enter");
  await page
    .locator(".ant-select-item-option-content")
    .getByText("Claude CLI", { exact: true })
    .click();
  await page.getByRole("combobox", { name: "审查模型", exact: true }).click();
  await page.getByText("模型一 (claude-one)", { exact: true }).click();
  await page.getByRole("radio", { name: "按阶段配置" }).check();
  await expect(
    page.getByRole("combobox", { name: "审查器", exact: true }),
  ).toHaveCount(3);
  await expect(
    page.getByText("模型一 (claude-one)", { exact: true }),
  ).toHaveCount(3);
  await page
    .getByRole("combobox", { name: "审查模型", exact: true })
    .nth(2)
    .press("Enter");
  await page.getByText("模型二 (claude-two)", { exact: true }).click();
  await page.getByRole("button", { name: "保存配置", exact: true }).click();
  await expect(page.getByText("操作已提交")).toBeVisible();
  await page.reload();
  await expect(page.getByRole("radio", { name: "按阶段配置" })).toBeChecked();
  await expect(
    page.getByText("模型二 (claude-two)", { exact: true }),
  ).toBeVisible();
  await page.getByRole("radio", { name: "统一配置" }).check();
  await expect(
    page.getByText("模型一 (claude-one)", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "刷新模型", exact: true }).click();
  await expect(page.getByText("模拟刷新失败，保留缓存")).toBeVisible();
  await page.getByRole("radio", { name: "按阶段配置" }).check();
  await expect(
    page.getByText("模型二 (claude-two)", { exact: true }),
  ).toBeVisible();
});
