import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import {
  NativeSupervisor,
  explicitFailure,
} from "../dist/native-supervision.js";

test("explicit shell/test failures survive a successful pipeline without matching ordinary prose", () => {
  const calls = [{ type: "tool/call", data: { callId: "test", name: "bash" } }];
  const result = (text) => ({
    type: "tool/result",
    data: {
      message: {
        toolCallId: "test",
        isError: false,
        content: [{ type: "text", text }],
      },
    },
  });
  for (const output of [
    " Test Files  1 failed (1)\n Tests 7 failed | 40 passed (47)",
    "\u001b[31m Tests  3 failed | 46 passed (49)\u001b[0m",
    "[exit code: 2]",
    "# fail 2",
    "FAILED (failures=1, errors=2)",
  ])
    assert.equal(explicitFailure(result(output), calls), true, output);
  for (const output of [
    "Tests 0 failed | 47 passed",
    "# fail 0",
    "[exit code: 0]",
    "Error handling is implemented",
    "expect(output).toContain('Tests 3 failed')",
  ])
    assert.equal(explicitFailure(result(output), calls), false, output);
  assert.equal(explicitFailure(result("Tests 3 failed"), []), false);
  assert.equal(
    explicitFailure(result("Tests 3 failed"), [
      { type: "tool/call", data: { callId: "test", name: "read" } },
    ]),
    false,
  );
});

export const user = (value = "实现斗地主") => ({
  id: randomUUID(),
  role: "user",
  source: { kind: "user" },
  content: [{ type: "text", text: value }],
});
export async function until(fn) {
  for (let i = 0; i < 400; i++) {
    if (fn()) return;
    await new Promise((r) => setTimeout(r, 5));
  }
  throw new Error("condition timeout");
}
export function transport() {
  const gates = new Map();
  const reports = [];
  const calls = [];
  return {
    gates,
    reports,
    calls,
    async call(b) {
      calls.push(b);
      if (b.action === "enable") return { enabled: true };
      if (b.action === "state") {
        reports.push(b);
        return {};
      }
      if (b.action === "open" && !gates.has(b.gate_id))
        gates.set(b.gate_id, {
          id: b.gate_id,
          phase: b.packet.phase,
          status: b.ready && !b.reason ? "waiting" : "blocked",
          ready: b.ready,
          reason: b.reason || "",
          packet: b.packet,
        });
      const gate = gates.get(b.gate_id);
      if (!gate) throw new Error("missing gate");
      if (b.action === "cancel") gate.status = "cancelled";
      if (b.action === "ack") gate.status = "consumed";
      if (b.action === "hold") gate.status = "blocked";
      return structuredClone(gate);
    },
    decide(gate, decision) {
      gate.status = "waiting";
      gate.review = {
        id: randomUUID(),
        execution_done: true,
        result: {
          decision,
          summary: decision,
          instruction: "继续",
          pause_proof: { pause_verified: true },
        },
      };
    },
  };
}
async function fixture(t) {
  const dir = await mkdtemp(join(tmpdir(), "native-plugin-"));
  const rows = [];
  const handlers = {};
  let guard;
  let restricted = false;
  const agent = {
    id: "session-unit",
    status: "running",
    session: { header: { cwd: dir }, snapshotEvents: () => rows },
    inbox: { nextStep: [], nextTurn: [] },
    ctx: {
      tools: {
        restrict() {
          restricted = true;
          return () => {
            restricted = false;
          };
        },
      },
    },
    steer(m) {
      this.inbox.nextStep.push(m);
    },
  };
  let jobs = [];
  const ctx = {
    on(n, f) {
      handlers[n] = f;
      return () => {};
    },
    effect() {},
    tools: {
      guard(f) {
        guard = f;
        return () => {};
      },
    },
    agents: { list: () => [agent] },
    jobs: { list: () => jobs },
  };
  const tr = transport();
  const supervisor = new NativeSupervisor(ctx, tr, dir, 3);
  supervisor.install();
  const abort = new AbortController();
  t.after(async () => {
    abort.abort();
    supervisor.dispose();
    await new Promise((r) => setTimeout(r, 30));
    await rm(dir, { recursive: true, force: true });
  });
  const pre = (messages = []) =>
    handlers["agent/pre-step"](
      { agent, messages, turn: 1, step: 1, signal: abort.signal },
      async () => ({ kind: "enter", messages }),
    );
  const end = () =>
    handlers["agent/turn-stopping"]({ agent, turn: 1, signal: abort.signal });
  return {
    dir,
    ctx,
    agent,
    rows,
    tr,
    supervisor,
    abort,
    pre,
    end,
    guard: (target = agent) => guard({ agent: target }),
    restricted: () => restricted,
    jobs: (value) => {
      jobs = value;
    },
  };
}

test("plan gates every tool, approve develops, revise repairs, done releases the turn", async (t) => {
  const f = await fixture(t);
  const decision = await f.pre([user()]);
  assert.match(decision.messages.at(-1).content[0].text, /只输出/);
  assert.ok(f.guard());
  assert.ok(f.restricted());
  let wait = f.end();
  await until(() => f.tr.gates.size === 1);
  const plan = [...f.tr.gates.values()][0];
  assert.equal(plan.phase, "plan");
  assert.ok(f.guard());
  f.tr.decide(plan, "approve");
  await wait;
  assert.equal(f.guard(), undefined);
  assert.equal(f.restricted(), false);
  assert.equal(f.agent.inbox.nextStep.length, 1);
  await f.pre(f.agent.inbox.nextStep.splice(0));
  wait = f.end();
  await until(() => f.tr.gates.size === 2);
  f.tr.decide([...f.tr.gates.values()][1], "revise");
  await wait;
  assert.equal(f.guard(), undefined);
  await f.pre(f.agent.inbox.nextStep.splice(0));
  wait = f.end();
  await until(() => f.tr.gates.size === 3);
  f.tr.decide([...f.tr.gates.values()][2], "done");
  await wait;
  assert.equal(f.tr.reports.at(-1).state, "verified");
  assert.equal(f.agent.inbox.nextStep.length, 0);
});

test("new human input resets approval; plugin recovery text does not", async (t) => {
  const f = await fixture(t);
  await f.pre([user()]);
  const wait = f.end();
  await until(() => f.tr.gates.size);
  f.tr.decide([...f.tr.gates.values()][0], "approve");
  await wait;
  await f.pre(f.agent.inbox.nextStep.splice(0));
  assert.equal(f.guard(), undefined);
  await f.pre([user("增加联网模式")]);
  assert.ok(f.guard());
});

test("three explicit tool failures trigger one checkpoint before the next step", async (t) => {
  const f = await fixture(t);
  await f.pre([user()]);
  const plan = f.end();
  await until(() => f.tr.gates.size);
  f.tr.decide([...f.tr.gates.values()][0], "approve");
  await plan;
  f.rows.push({
    seq: 0,
    type: "tool/call",
    data: { callId: "masked-test", name: "bash" },
  });
  for (let i = 1; i <= 5; i++)
    f.rows.push({
      seq: i,
      type: "tool/result",
      data: {
        message: {
          isError: i === 5,
          toolCallId: "masked-test",
          content: [
            {
              type: "text",
              text:
                i % 2 === 1 ? " Test Files  1 failed (1)" : "Tests 47 passed",
            },
          ],
        },
      },
    });
  const step = f.pre();
  await until(() => f.tr.gates.size === 2);
  const gate = [...f.tr.gates.values()][1];
  assert.equal(gate.phase, "checkpoint");
  assert.ok(f.guard());
  f.tr.decide(gate, "approve");
  await step;
  await f.pre();
  assert.equal(f.tr.gates.size, 2);
});

test("review packet retains the complete final report and test matrix within the size limit", async (t) => {
  const f = await fixture(t);
  await f.pre([user("检查报告")]);
  const final =
    "报告开始\n" +
    "具体改动和证据。".repeat(800) +
    "\n最终测试矩阵：233 passed，类型检查和构建退出码 0。";
  f.rows.push({
    seq: 1,
    type: "assistant/message",
    data: { message: { content: [{ type: "text", text: final }] } },
  });
  const pending = f.end();
  await until(() => f.tr.gates.size === 1);
  const gate = [...f.tr.gates.values()][0];
  assert.ok(gate.packet.summary.includes(final));
  assert.ok(gate.packet.summary.length <= 20000);
  f.tr.decide(gate, "approve");
  await pending;
});

test("cancel during review prevents late results from injecting a recovery message", async (t) => {
  const f = await fixture(t);
  await f.pre([user()]);
  const wait = f.end();
  const rejection = assert.rejects(wait);
  await until(() => f.tr.gates.size);
  f.abort.abort();
  await rejection;
  const gate = [...f.tr.gates.values()][0];
  assert.equal(gate.status, "cancelled");
  f.tr.decide(gate, "approve");
  await new Promise((r) => setTimeout(r, 15));
  assert.equal(f.agent.inbox.nextStep.length, 0);
});

test("a running background job holds review; manual release remains single-gate", async (t) => {
  const f = await fixture(t);
  await f.pre([user()]);
  f.jobs([{ id: "job1", status: "running" }]);
  const wait = f.end();
  await until(() => f.tr.gates.size);
  const gate = [...f.tr.gates.values()][0];
  assert.equal(gate.status, "blocked");
  assert.ok(f.guard());
  f.jobs([]);
  gate.status = "released";
  gate.override = { reason: "本次人工检查通过" };
  await wait;
  assert.equal(f.guard(), undefined);
  const acceptance = f.end();
  await until(() => f.tr.gates.size === 2);
  const final = [...f.tr.gates.values()][1];
  final.status = "released";
  final.override = { reason: "用户确认" };
  await acceptance;
  assert.equal(f.tr.reports.at(-1).state, "manual_released");
});

test("connection failures reuse the gate ID and never permit tool execution", async (t) => {
  const f = await fixture(t);
  await f.pre([user()]);
  const original = f.tr.call;
  let failures = 3;
  f.tr.call = async (b) => {
    if (b.action === "open" && failures-- > 0) throw new Error("offline");
    return original(b);
  };
  const wait = f.end();
  await until(() => f.tr.gates.size);
  assert.ok(f.guard());
  f.tr.decide([...f.tr.gates.values()][0], "approve");
  await wait;
  assert.equal(f.tr.gates.size, 1);
});

test("review response acknowledgement loss does not duplicate recovery injection", async (t) => {
  const f = await fixture(t);
  await f.pre([user()]);
  const original = f.tr.call;
  let lost = false;
  f.tr.call = async (b) => {
    const r = await original(b);
    if (b.action === "ack" && !lost) {
      lost = true;
      throw new Error("lost acknowledgement");
    }
    return r;
  };
  const wait = f.end();
  await until(() => f.tr.gates.size);
  f.tr.decide([...f.tr.gates.values()][0], "approve");
  await wait;
  assert.equal(f.agent.inbox.nextStep.length, 1);
  assert.equal(f.tr.calls.filter((c) => c.action === "ack").length, 2);
});

test("after three repairs a further revise holds the gate for human intervention", async (t) => {
  const f = await fixture(t);
  await f.pre([user()]);
  for (let i = 0; i < 3; i++) {
    const wait = f.end();
    await until(() => f.tr.gates.size === i + 1);
    f.tr.decide([...f.tr.gates.values()][i], "revise");
    await wait;
    await f.pre(f.agent.inbox.nextStep.splice(0));
  }
  const wait = f.end();
  await until(() => f.tr.gates.size === 4);
  const gate = [...f.tr.gates.values()][3];
  f.tr.decide(gate, "revise");
  await until(() => gate.status === "blocked");
  assert.ok(f.guard());
  gate.override = { reason: "人工处理后放行" };
  gate.status = "released";
  await wait;
  assert.equal(f.guard(), undefined);
});

test("restoring a consumed gate uses the persisted message ID and does not inject twice", async (t) => {
  const f = await fixture(t);
  await f.pre([user()]);
  const { readFile, readdir, writeFile } = await import("node:fs/promises");
  let diskBeforeClear;
  const original = f.tr.call;
  f.tr.call = async (b) => {
    if (b.action === "ack") {
      const filename = (await readdir(f.dir)).find((n) => n.endsWith(".json"));
      diskBeforeClear = [
        filename,
        await readFile(join(f.dir, filename), "utf8"),
      ];
    }
    return original(b);
  };
  const wait = f.end();
  await until(() => f.tr.gates.size);
  f.tr.decide([...f.tr.gates.values()][0], "approve");
  await wait;
  const message = f.agent.inbox.nextStep[0];
  assert.ok(message);
  f.supervisor.dispose();
  await writeFile(join(f.dir, diskBeforeClear[0]), diskBeforeClear[1]);
  const handlers = {};
  const ctx = {
    on(n, fn) {
      handlers[n] = fn;
      return () => {};
    },
    effect() {},
    tools: {
      guard() {
        return () => {};
      },
    },
    agents: { list: () => [f.agent] },
    jobs: { list: () => [] },
  };
  const restored = new NativeSupervisor(ctx, f.tr, f.dir, 3);
  restored.install();
  try {
    await handlers["agent/pre-step"](
      {
        agent: f.agent,
        messages: [],
        turn: 1,
        step: 2,
        signal: f.abort.signal,
      },
      async () => ({ kind: "enter", messages: [] }),
    );
    assert.equal(f.agent.inbox.nextStep.length, 1);
    assert.equal(f.agent.inbox.nextStep[0].id, message.id);
  } finally {
    restored.dispose();
  }
});

test("concurrent main sessions have independent approvals and child work holds only its parent", async (t) => {
  const f = await fixture(t);
  const other = {
    ...f.agent,
    id: "other-main",
    inbox: { nextStep: [], nextTurn: [] },
  };
  const childAgent = {
    ...f.agent,
    id: "child",
    session: {
      ...f.agent.session,
      header: {
        origin: "subagent",
        delegationDepth: 1,
        parentSession: f.agent.id,
      },
    },
  };
  const ctx = f.ctx;
  ctx.agents.list = () => [f.agent, other, childAgent];
  await f.pre([user()]);
  const request = user("第二个任务");
  await f.supervisor.preStep(
    {
      agent: other,
      messages: [request],
      turn: 1,
      step: 1,
      signal: f.abort.signal,
    },
    async () => ({ kind: "enter", messages: [request] }),
  );
  const parentWait = f.end();
  const otherWait = f.supervisor.turnStopping({
    agent: other,
    turn: 1,
    signal: f.abort.signal,
  });
  await until(() => f.tr.gates.size === 2);
  const parentGate = [...f.tr.gates.values()].find(
    (g) => g.packet.session_id === f.agent.id,
  );
  const otherGate = [...f.tr.gates.values()].find(
    (g) => g.packet.session_id === other.id,
  );
  assert.equal(parentGate.status, "blocked");
  f.tr.decide(otherGate, "approve");
  await otherWait;
  assert.equal(f.guard(other), undefined);
  assert.ok(f.guard());
  childAgent.status = "idle";
  f.tr.decide(parentGate, "approve");
  await parentWait;
  assert.equal(f.tr.gates.size, 2);
});

test("new user steering supersedes an unresolved gate without consuming its late result", async (t) => {
  const f = await fixture(t);
  await f.pre([user()]);
  const wait = f.end();
  await until(() => f.tr.gates.size);
  const gate = [...f.tr.gates.values()][0];
  f.agent.inbox.nextStep.push(user("改做五子棋"));
  await wait;
  assert.equal(gate.status, "cancelled");
  assert.ok(f.guard());
  const admitted = await f.pre(f.agent.inbox.nextStep.splice(0));
  assert.match(admitted.messages.at(-1).content[0].text, /只输出/);
  assert.equal(f.tr.calls.filter((c) => c.action === "ack").length, 0);
});
