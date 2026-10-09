/** Actual installed Harness loop and tool runtime; no paid model or user session. */
import test from "node:test";
import assert from "node:assert/strict";
import { access, mkdtemp, rm } from "node:fs/promises";
import { homedir, tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { NativeSupervisor } from "../dist/native-supervision.js";
const base =
  process.env.DSH_RUNTIME_NODE_MODULES ||
  join(
    homedir(),
    "Library/Application Support/com.thsoctop.desktop/dependencies/dsh/node_modules",
  );
let available = true;
try {
  await access(join(base, "@deepseek-ai/dsh-agent-loop/lib/index.js"));
} catch {
  available = false;
}
const load = (name) =>
  import(pathToFileURL(join(base, "@deepseek-ai", name, "lib/index.js")).href);
async function until(fn) {
  for (let i = 0; i < 800; i++) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 5));
  }
  throw new Error("Harness did not reach gate");
}
function textResponse(text) {
  return [
    { type: "block-start", index: 0, blockType: "text" },
    { type: "text-delta", index: 0, text },
    { type: "block-end", index: 0, block: { type: "text", text } },
    { type: "finish", reason: { kind: "stop" } },
  ];
}
function toolResponse() {
  return [
    { type: "block-start", index: 0, blockType: "tool-call" },
    {
      type: "block-end",
      index: 0,
      block: {
        type: "tool-call",
        id: "write-test",
        name: "touch",
        arguments: "{}",
      },
    },
    { type: "finish", reason: { kind: "tool-calls" } },
  ];
}

test(
  "installed Harness enforces plan pause, runs tools only after approval, and waits before turn/end",
  { skip: !available, timeout: 15000 },
  async () => {
    const [
      { Context },
      llm,
      sessions,
      prompt,
      tools,
      agents,
      loop,
      projection,
    ] = await Promise.all(
      [
        "cordis",
        "dsh-llm",
        "dsh-session",
        "dsh-system-prompt",
        "dsh-tools",
        "dsh-agent",
        "dsh-agent-loop",
        "dsh-session-projection",
      ].map(load),
    );
    const ctx = new Context();
    for (const plugin of [
      llm.default,
      sessions.default,
      projection.default,
      prompt.default,
      tools.default,
      agents.default,
    ])
      await ctx.plugin(plugin);
    await ctx.plugin(loop.default, { agents: [] });
    let calls = 0;
    let executions = 0;
    class Script extends llm.LlmAdapter {
      async resolveModel(provider, model) {
        return { provider, id: model, name: model };
      }
      async *stream() {
        const chunks = [
          textResponse("方案：实现功能并测试"),
          toolResponse(),
          textResponse("完成，请验收"),
          toolResponse(),
          textResponse("已返修"),
        ][calls++];
        if (!chunks) throw new Error("script exhausted");
        yield* chunks;
      }
    }
    ctx.llm.registerAdapter(["mock"], new Script());
    ctx.tools.register(
      tools.defineContentToolFixture({
        name: "touch",
        description: "Count a real tool execution",
        parameters: {},
        execute: async () => {
          executions++;
          return [{ type: "text", text: "ok" }];
        },
      }),
    );
    const dir = await mkdtemp(join(tmpdir(), "supervisor-harness-"));
    const gates = new Map();
    const reports = [];
    const tr = {
      async call(b) {
        if (b.action === "enable") return {};
        if (b.action === "state") {
          reports.push(b);
          return {};
        }
        if (b.action === "open" && !gates.has(b.gate_id))
          gates.set(b.gate_id, {
            id: b.gate_id,
            phase: b.packet.phase,
            status: "waiting",
            ready: 1,
            reason: "",
          });
        const gate = gates.get(b.gate_id);
        if (b.action === "cancel") gate.status = "cancelled";
        if (b.action === "ack") gate.status = "consumed";
        return structuredClone(gate);
      },
    };
    // Only the job inventory is empty; event dispatch, admission, tool guard and inbox are real.
    const nativeCtx = {
      on: ctx.on.bind(ctx),
      effect: ctx.effect.bind(ctx),
      tools: ctx.tools,
      agents: ctx.agents,
      jobs: { list: () => [] },
    };
    const supervisor = new NativeSupervisor(nativeCtx, tr, dir, 5);
    supervisor.install();
    const agent = await ctx.agentLoop.create(
      sessions.SessionId("native-integration"),
      { provider: "mock", model: "test" },
    );
    const decide = (gate, decision) => {
      gate.review = {
        id: gate.id,
        execution_done: true,
        result: {
          decision,
          instruction: "按审查结果继续",
          summary: decision,
          pause_proof: { pause_verified: true },
        },
      };
    };
    try {
      agent.followup(
        llm.createUserMessage({
          content: [{ type: "text", text: "实现功能" }],
          source: { kind: "user" },
        }),
      );
      await until(() => gates.size === 1);
      assert.equal(calls, 1);
      assert.equal(executions, 0);
      const seq = agent.session.snapshotEvents().length;
      await new Promise((r) => setTimeout(r, 40));
      assert.equal(agent.session.snapshotEvents().length, seq);
      decide([...gates.values()][0], "approve");
      await until(() => gates.size === 2);
      assert.equal(executions, 1);
      assert.equal(calls, 3);
      assert.equal(
        agent.session.snapshotEvents().filter((e) => e.type === "turn/end")
          .length,
        0,
      );
      decide([...gates.values()][1], "revise");
      await until(() => gates.size === 3);
      assert.equal(executions, 2);
      decide([...gates.values()][2], "done");
      await agent.whenIdle();
      assert.equal(reports.at(-1).state, "verified");
      assert.equal(
        agent.session.snapshotEvents().filter((e) => e.type === "turn/end")
          .length,
        1,
      );
    } finally {
      supervisor.dispose();
      await rm(dir, { recursive: true, force: true });
    }
  },
);
