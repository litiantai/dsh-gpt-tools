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
    await page.goto("/");
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
    await page.goto("/");
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
    await page.goto("/");
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
      .getByRole("button", { name: /阶段检查 · 看板集成测试会话/ })
      .click();
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
    await page.goto("/");
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
