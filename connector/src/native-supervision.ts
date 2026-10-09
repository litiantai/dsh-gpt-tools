/** Plugin-owned, fail-closed review gates on the native Harness lifecycle. */
import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

export interface Message {
  id: string;
  role: "user";
  source: { kind: string; [key: string]: unknown };
  content: { type: string; text?: string }[];
}
export interface Event {
  seq: number;
  type: string;
  data: any;
}
export interface Agent {
  id: string;
  status: string;
  session: {
    header: {
      cwd?: string;
      origin?: string;
      delegationDepth?: number;
      parentSession?: string;
    };
    snapshotEvents(): readonly Event[];
  };
  inbox: { nextStep: readonly Message[]; nextTurn: readonly Message[] };
  ctx: { tools: { restrict(filter: { allow: string[] }): () => void } };
  steer(message: Message): void;
}
export interface NativeContext {
  on(event: string, listener: (...args: any[]) => any): () => void;
  effect(callback: () => () => void, label?: string): void;
  tools: {
    guard(fn: (exec: { agent?: Agent }) => string | undefined): () => void;
  };
  agents: { list(): Agent[] };
  jobs: { list(owner: string): { id: string; status: string }[] };
}
export interface Config {
  home?: string;
  stateDir?: string;
  origin?: string;
}
type Phase = "plan" | "checkpoint" | "acceptance";
type Stage =
  | "planning"
  | "developing"
  | "waiting"
  | "blocked"
  | "verified"
  | "manual_released"
  | "cancelled";
interface Verdict {
  decision: "approve" | "revise" | "done" | "blocked";
  instruction: string;
  summary: string;
  pause_proof?: { pause_verified: boolean };
}
interface GateReply {
  id: string;
  status: string;
  reason: string;
  ready: number;
  override?: { reason: string };
  review?: {
    id: string;
    execution_done: boolean;
    result?: Verdict;
    reviewer?: { provider: string; model: string };
  };
}
interface Gate {
  id: string;
  phase: Phase;
  packet: {
    session_id: string;
    phase: Phase;
    summary: string;
    pause_seq: number;
  };
  hold?: string;
  resolution?: {
    next: Stage;
    message?: Message;
    manual: boolean;
    reviewId?: string;
  };
}
interface State {
  stage: Stage;
  seen: string[];
  task: string;
  revisions: Record<Phase, number>;
  lastFailure: number;
  lastGateId?: string;
  gate?: Gate;
}
export interface Transport {
  call(body: Record<string, unknown>, signal?: AbortSignal): Promise<any>;
}
const note = (text: string): Message => ({
  id: randomUUID(),
  role: "user",
  source: { kind: "supervisor-native", form: "instruction" },
  content: [{ type: "text", text }],
});
const text = (m: { content?: { type: string; text?: string }[] }) =>
  (m.content || [])
    .filter((b) => b.type === "text")
    .map((b) => b.text || "")
    .join("\n");

/** Recognize explicit failures, including test runners hidden by shell pipelines. */
export function explicitFailure(
  event: Event,
  events: readonly Event[],
): boolean {
  const message = event.data.message || event.data;
  if (message.isError === true) return true;
  const callId = message.toolCallId || message.source?.callId;
  const call = events.find(
    (e) => e.type === "tool/call" && e.data.callId === callId,
  );
  if (!callId || call?.data.name !== "bash") return false;
  const output = text(message).replace(/\u001b\[[0-9;]*m/g, "");
  // These are result summaries, not arbitrary mentions of "error" in source or prose.
  return (
    /^\[exit code: -?[1-9]\d*\]\s*$/m.test(output) ||
    /^\s*(?:Test Files|Tests)\s+[1-9]\d* failed\b/m.test(output) ||
    /^\s*# fail [1-9]\d*\s*$/m.test(output) ||
    /^FAILED \((?:failures|errors)=[1-9]\d*(?:, (?:failures|errors)=\d+)*\)\s*$/m.test(
      output,
    )
  );
}
const child = (a: Agent) =>
  a.session.header.origin === "subagent" ||
  (a.session.header.delegationDepth || 0) > 0;
const planning =
  "插件强制监管已启用。现在只输出本次任务的具体实施方案、授权范围和验收标准，说明已有改动；不要调用任何工具或声称完成。方案将自动交给审查员，获批后插件会通知你继续。";

export function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(signal.reason);
    const cancel = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", cancel);
      resolve();
    }, ms);
    signal.addEventListener("abort", cancel, { once: true });
  });
}

export class HttpTransport implements Transport {
  readonly home: string;
  readonly stateDir: string;
  readonly origin: string;
  constructor(config: Config) {
    this.home = resolve(
      config.home || process.env.DSH_HOME || join(homedir(), ".dsh"),
    );
    this.stateDir = resolve(
      config.stateDir ||
        process.env.DSH_SUPERVISOR_STATE ||
        join(this.home, "supervisor"),
    );
    this.origin = config.origin || "http://127.0.0.1:13084";
    if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(this.origin))
      throw new Error("Supervisor must use a loopback HTTP address");
  }
  async call(
    body: Record<string, unknown>,
    signal?: AbortSignal,
  ): Promise<any> {
    const key = (
      await readFile(join(this.stateDir, "connector.key"), "utf8")
    ).trim();
    const response = await fetch(`${this.origin}/api/connector/native`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${key}`,
      },
      body: JSON.stringify({ ...body, home: this.home }),
      signal: signal
        ? AbortSignal.any([signal, AbortSignal.timeout(8000)])
        : AbortSignal.timeout(8000),
    });
    const result = (await response.json()) as { error?: string };
    if (!response.ok)
      throw new Error(result.error || `HTTP ${response.status}`);
    return result;
  }
}

export class NativeSupervisor {
  private readonly owner = randomUUID();
  private readonly stop = new AbortController();
  private readonly states = new Map<string, State>();
  private readonly loads = new Map<string, Promise<State>>();
  private readonly restrictions = new Map<string, () => void>();
  private readonly writes = new Map<string, Promise<void>>();
  private readonly disposers: (() => void)[] = [];
  constructor(
    private readonly ctx: NativeContext,
    private readonly transport: Transport,
    private readonly dir: string,
    private readonly pollMs = 1000,
  ) {}

  install(): void {
    // Monotonic guards cannot be overridden by another pre-execute listener.
    this.disposers.push(
      this.ctx.tools.guard((exec) => {
        if (!exec.agent) return;
        const root = this.root(exec.agent);
        if (this.states.get(root.id)?.stage !== "developing")
          return "监管尚未批准执行工具，请先输出方案或等待审查";
      }),
    );
    this.disposers.push(
      this.ctx.on("agent/pre-step", (p: any, next: any) =>
        this.preStep(p, next),
      ),
    );
    this.disposers.push(
      this.ctx.on("agent/turn-stopping", (p: any) => this.turnStopping(p)),
    );
    this.disposers.push(
      this.ctx.on("agent/disposed", ({ agent }: { agent: Agent }) => {
        this.restrictions.get(agent.id)?.();
        this.restrictions.delete(agent.id);
      }),
    );
    this.disposers.push(
      this.ctx.on(
        "agent/status",
        ({ agent, status }: { agent: Agent; status: string }) => {
          if (status !== "idle" || child(agent)) return;
          const state = this.states.get(agent.id);
          const last = [...agent.session.snapshotEvents()]
            .reverse()
            .find((e) => e.type === "turn/end");
          if (
            state &&
            ["aborted", "error", "blocked"].includes(last?.data.reason?.kind)
          ) {
            state.stage = "cancelled";
            void this.save(agent, state)
              .then(() => this.report(agent, state, "当前轮次已停止"))
              .catch(() => {});
          }
        },
      ),
    );
    // Start the bridge once per activation. Deliberate later service stops stay stopped.
    void this.enable();
    this.ctx.effect(() => () => this.dispose(), "native supervisor gates");
  }

  private async enable(): Promise<void> {
    while (!this.stop.signal.aborted) {
      try {
        await this.transport.call({ action: "enable" }, this.stop.signal);
        return;
      } catch {
        try {
          await pause(this.pollMs, this.stop.signal);
        } catch {
          return;
        }
      }
    }
  }
  private root(agent: Agent): Agent {
    const seen = new Set<string>();
    while (
      child(agent) &&
      agent.session.header.parentSession &&
      !seen.has(agent.id)
    ) {
      seen.add(agent.id);
      const parent = this.ctx.agents
        .list()
        .find((a) => a.id === agent.session.header.parentSession);
      if (!parent) break;
      agent = parent;
    }
    return agent;
  }
  private path(id: string): string {
    return join(
      this.dir,
      createHash("sha256").update(id).digest("hex") + ".json",
    );
  }
  private async state(agent: Agent): Promise<State> {
    let pending = this.loads.get(agent.id);
    if (!pending) {
      pending = (async () => {
        let s: State;
        try {
          s = JSON.parse(await readFile(this.path(agent.id), "utf8"));
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
          s = {
            stage: "planning",
            seen: [],
            task: "",
            revisions: { plan: 0, checkpoint: 0, acceptance: 0 },
            lastFailure: 0,
          };
        }
        if (!s.task)
          s.task = agent.session
            .snapshotEvents()
            .filter(
              (e) =>
                e.type === "user/message" &&
                (e.data.source?.kind === "user" ||
                  e.data.message?.source?.kind === "user"),
            )
            .slice(-5)
            .map((e) => text(e.data.message || e.data))
            .join("\n")
            .slice(-5000);
        // No durable pending gate means a resumed runtime must get a fresh plan approval.
        if (!s.gate) s.stage = "planning";
        this.states.set(agent.id, s);
        return s;
      })();
      this.loads.set(agent.id, pending);
    }
    return pending;
  }
  private async save(agent: Agent, state: State): Promise<void> {
    const data = JSON.stringify(state);
    const old = this.writes.get(agent.id) || Promise.resolve();
    const work = old
      .catch(() => {})
      .then(async () => {
        await mkdir(this.dir, { recursive: true, mode: 0o700 });
        const tmp = `${this.path(agent.id)}.${randomUUID()}.tmp`;
        await writeFile(tmp, data, { mode: 0o600 });
        await rename(tmp, this.path(agent.id));
      });
    this.writes.set(agent.id, work);
    await work;
  }
  private restrict(agent: Agent, state: State): void {
    if (state.stage === "developing") {
      this.restrictions.get(agent.id)?.();
      this.restrictions.delete(agent.id);
    } else if (!this.restrictions.has(agent.id))
      this.restrictions.set(agent.id, agent.ctx.tools.restrict({ allow: [] }));
  }
  private async report(agent: Agent, state: State, reason = ""): Promise<void> {
    try {
      await this.transport.call({
        action: "state",
        session_id: agent.id,
        state: state.stage,
        reason,
        gate_id: state.gate?.id || state.lastGateId,
      });
    } catch {
      /* Local gate remains closed while offline. */
    }
  }
  private quiescent(agent: Agent): boolean {
    try {
      const family = this.ctx.agents
        .list()
        .filter((a) => a.id === agent.id || this.root(a).id === agent.id);
      if (!family.some((a) => a.id === agent.id)) family.push(agent);
      return family.every(
        (a) =>
          (a.id === agent.id ||
            (a.status === "idle" &&
              !a.inbox.nextStep.length &&
              !a.inbox.nextTurn.length)) &&
          this.ctx.jobs
            .list(a.id)
            .every((j) => ["completed", "failed", "killed"].includes(j.status)),
      );
    } catch {
      return false;
    }
  }
  private currentSeq(agent: Agent): number {
    return agent.session
      .snapshotEvents()
      .reduce((n, e) => Math.max(n, e.seq), 0);
  }
  private summary(agent: Agent, state: State, phase: Phase): string {
    const rows = agent.session.snapshotEvents();
    const latest = [...rows]
      .reverse()
      .find(
        (e) => e.type === "assistant/message" && text(e.data.message || e.data),
      );
    const report = latest ? text(latest.data.message || latest.data) : "";
    const completeReport =
      report.length <= 10000
        ? report
        : report.slice(0, 6000) +
          "\n[中间内容已截断；请读取会话日志核对全文]\n" +
          report.slice(-3900);
    const messages = rows
      .filter(
        (e) =>
          e !== latest &&
          ["user/message", "assistant/message", "tool/result"].includes(e.type),
      )
      .slice(-45)
      .map((e) => `${e.type}: ${text(e.data.message || e.data).slice(0, 1600)}`)
      .join("\n");
    return `阶段：${phase}\n用户任务：${state.task.slice(-4000)}\n审查当前任务及其授权改动，不把无关历史改动算入。方案阶段只审核方案；验收核对实际文件与测试证据。必要时主动读取工作区和会话日志。\n最近上下文（节选）：\n${messages.slice(-5000)}\n最新方案或验收报告：\n${completeReport}`;
  }
  private async reset(
    agent: Agent,
    state: State,
    messages: Message[],
    signal: AbortSignal,
  ): Promise<void> {
    if (state.gate) {
      // A restart may still have an old owner's lease or an unacknowledged open.
      while (true) {
        signal.throwIfAborted();
        try {
          const gate = (await this.transport.call(
            { action: "cancel", gate_id: state.gate.id, owner: this.owner },
            signal,
          )) as GateReply;
          if (!gate.review || gate.review.execution_done) break;
        } catch (error) {
          signal.throwIfAborted();
          state.stage = "blocked";
          await this.report(
            agent,
            state,
            error instanceof Error ? error.message : "正在核对上一次审查",
          );
        }
        await pause(this.pollMs, signal);
      }
      state.gate = undefined;
    }
    state.stage = "planning";
    state.lastGateId = undefined;
    state.revisions = { plan: 0, checkpoint: 0, acceptance: 0 };
    state.lastFailure = this.currentSeq(agent);
    state.task = (state.task + "\n" + messages.map(text).join("\n")).slice(
      -5000,
    );
    state.seen = [...state.seen, ...messages.map((m) => m.id)].slice(-100);
    await this.save(agent, state);
  }
  private async cancelGate(id: string): Promise<void> {
    try {
      await this.transport.call({
        action: "cancel",
        gate_id: id,
        owner: this.owner,
      });
    } catch {
      /* Retried by disposal cleanup / pending-state recovery. */
    }
  }
  async preStep(
    p: {
      agent: Agent;
      messages: Message[];
      turn: number;
      step: number;
      signal: AbortSignal;
    },
    next: () => Promise<any>,
  ): Promise<any> {
    const { agent } = p;
    if (child(agent)) return next();
    const state = await this.state(agent);
    const signal = AbortSignal.any([p.signal, this.stop.signal]);
    signal.throwIfAborted();
    // Resolve the downstream message batch first, preserving other native policies.
    const decision = await next();
    if (decision.kind !== "enter") return decision;
    const fresh = (decision.messages as Message[]).filter(
      (m) => m.source?.kind === "user" && !state.seen.includes(m.id),
    );
    if (fresh.length) await this.reset(agent, state, fresh, signal);
    if (state.stage === "cancelled") await this.reset(agent, state, [], signal);
    if (state.gate) await this.waitGate(agent, state, signal);
    // A cancelled/failed approval never admits a model step.
    signal.throwIfAborted();
    if (state.stage === "developing") {
      const events = agent.session.snapshotEvents();
      const results = events
        .filter((e) => e.type === "tool/result" && e.seq > state.lastFailure)
        .slice(-5);
      if (results.filter((e) => explicitFailure(e, events)).length >= 3) {
        state.lastFailure = results[results.length - 1].seq;
        await this.openGate(agent, state, "checkpoint", signal);
      }
    }
    this.restrict(agent, state);
    await this.report(agent, state);
    if (state.stage === "planning")
      return { ...decision, messages: [...decision.messages, note(planning)] };
    return decision;
  }
  async turnStopping(p: {
    agent: Agent;
    turn: number;
    signal: AbortSignal;
  }): Promise<void> {
    const { agent } = p;
    if (child(agent)) return;
    const state = await this.state(agent);
    const signal = AbortSignal.any([p.signal, this.stop.signal]);
    signal.throwIfAborted();
    if (state.gate) {
      await this.waitGate(agent, state, signal);
      return;
    }
    await this.openGate(
      agent,
      state,
      state.stage === "planning" ? "plan" : "acceptance",
      signal,
    );
  }
  private async openGate(
    agent: Agent,
    state: State,
    phase: Phase,
    signal: AbortSignal,
  ): Promise<void> {
    state.gate = {
      id: randomUUID(),
      phase,
      packet: {
        session_id: agent.id,
        phase,
        summary: this.summary(agent, state, phase),
        pause_seq: this.currentSeq(agent),
      },
      hold:
        state.revisions[phase] > 3
          ? "同一阶段连续返修已达 3 次，请人工处理后重试或放行"
          : undefined,
    };
    state.stage = "waiting";
    this.restrict(agent, state);
    await this.save(agent, state);
    await this.waitGate(agent, state, signal);
  }
  private hasNewInput(agent: Agent, state: State): boolean {
    return agent.inbox.nextStep.some(
      (m) => m.source?.kind === "user" && !state.seen.includes(m.id),
    );
  }

  private async supersede(
    agent: Agent,
    state: State,
    signal: AbortSignal,
  ): Promise<void> {
    const gate = state.gate!;
    // An in-flight reviewer must settle before another model step is admitted.
    while (true) {
      signal.throwIfAborted();
      try {
        const cancelled = (await this.transport.call(
          { action: "cancel", gate_id: gate.id, owner: this.owner },
          signal,
        )) as GateReply;
        if (!cancelled.review || cancelled.review.execution_done) break;
      } catch {
        signal.throwIfAborted();
      }
      await pause(this.pollMs, signal);
    }
    state.gate = undefined;
    state.stage = "planning";
    state.lastGateId = undefined;
    state.revisions = { plan: 0, checkpoint: 0, acceptance: 0 };
    this.restrict(agent, state);
    await this.save(agent, state);
    await this.report(agent, state, "收到新指令，重新审批方案");
  }

  private async waitGate(
    agent: Agent,
    state: State,
    signal: AbortSignal,
  ): Promise<void> {
    const gate = state.gate!;
    let opened = false;
    let delay = false;
    try {
      while (true) {
        signal.throwIfAborted();
        if (delay) await pause(this.pollMs, signal);
        delay = true;
        if (opened && this.hasNewInput(agent, state)) {
          await this.supersede(agent, state, signal);
          return;
        }
        const ready = this.quiescent(agent);
        let reply: GateReply;
        try {
          reply = await this.transport.call(
            {
              action: opened ? "poll" : "open",
              gate_id: gate.id,
              owner: this.owner,
              packet: gate.packet,
              ready,
              reason:
                gate.hold ||
                (!ready ? "等待后台任务及子代理结束后自动审查" : ""),
            },
            signal,
          );
          opened = true;
        } catch (error) {
          signal.throwIfAborted();
          state.stage = "blocked";
          await this.report(
            agent,
            state,
            error instanceof Error ? error.message : String(error),
          );
          continue;
        }
        if (this.hasNewInput(agent, state)) continue;
        if (reply.status === "cancelled")
          throw new Error("监管审批点已取消，当前轮次不能继续");
        const result = reply.review?.result;
        const released = !!reply.override;
        if (
          !ready ||
          reply.status === "blocked" ||
          (!released && (!result || result.decision === "blocked")) ||
          (reply.review && !reply.review.execution_done)
        ) {
          state.stage =
            reply.status === "blocked" || !ready ? "blocked" : "waiting";
          await this.report(
            agent,
            state,
            !ready ? "等待后台任务及子代理结束后自动审查" : reply.reason,
          );
          continue;
        }
        if (!released && !result?.pause_proof?.pause_verified) {
          state.stage = "blocked";
          await this.report(agent, state, "暂停证据未通过");
          continue;
        }
        if (
          !released &&
          result?.decision === "revise" &&
          state.revisions[gate.phase] >= 3 &&
          gate.resolution?.reviewId !== reply.review?.id
        ) {
          await this.transport.call(
            { action: "hold", gate_id: gate.id, owner: this.owner },
            signal,
          );
          state.revisions[gate.phase] = 0; // A later explicit retry starts a new bounded cycle.
          await this.save(agent, state);
          continue;
        }
        if (gate.resolution && gate.resolution.reviewId !== reply.review?.id)
          gate.resolution = undefined;
        // Persist one exact recovery message before injecting it. Replay checks its identity.
        if (!gate.resolution) {
          if (result?.decision === "revise" && !released)
            state.revisions[gate.phase]++;
          else state.revisions[gate.phase] = 0;
          const revise = !released && result?.decision === "revise";
          const next: Stage =
            revise && gate.phase === "plan"
              ? "planning"
              : gate.phase === "acceptance" && !revise
                ? released
                  ? "manual_released"
                  : "verified"
                : "developing";
          const reviewer = reply.review?.reviewer;
          const reviewerLabel = reviewer
            ? `${({ codex: "Codex/GPT", claude: "Claude CLI", harness: "DeepSeek Harness" } as Record<string, string>)[reviewer.provider] || reviewer.provider} / ${reviewer.model}`
            : "审查员";
          const instruction = released
            ? `人工仅放行本审批点：${reply.override!.reason}。这不代表 审查员验收通过。`
            : `${reviewerLabel}：${result!.instruction}`;
          gate.resolution = {
            next,
            manual: released,
            reviewId: reply.review?.id,
            message:
              next === "verified" || next === "manual_released"
                ? undefined
                : note(
                    `监管审查 ${gate.id}：${instruction}\n${next === "planning" ? planning : "继续执行获批任务，完成后自动进入 审查员验收。"}`,
                  ),
          };
          await this.save(agent, state);
        }
        signal.throwIfAborted();
        try {
          await this.transport.call(
            { action: "ack", gate_id: gate.id, owner: this.owner },
            signal,
          );
        } catch {
          signal.throwIfAborted();
          continue;
        }
        signal.throwIfAborted();
        const resolution = gate.resolution;
        if (
          resolution.message &&
          !this.hasMessage(agent, resolution.message.id)
        )
          agent.steer(resolution.message);
        state.stage = resolution.next;
        state.lastGateId = gate.id;
        state.gate = undefined;
        this.restrict(agent, state);
        await this.save(agent, state);
        await this.report(
          agent,
          state,
          resolution.manual ? "人工放行，未通过审查员验收" : "",
        );
        return;
      }
    } catch (error) {
      await this.cancelGate(gate.id);
      state.stage = "cancelled";
      await this.save(agent, state);
      await this.report(agent, state, "任务取消或监管等待已中断");
      throw error;
    }
  }
  private hasMessage(agent: Agent, id: string): boolean {
    return (
      [...agent.inbox.nextStep, ...agent.inbox.nextTurn].some(
        (m) => m.id === id,
      ) ||
      agent.session
        .snapshotEvents()
        .some(
          (e) =>
            e.type === "user/message" &&
            (e.data.id === id || e.data.message?.id === id),
        )
    );
  }
  dispose(): void {
    this.stop.abort(new Error("监管插件已卸载，审批等待取消"));
    for (const dispose of this.disposers) dispose();
    for (const lift of this.restrictions.values()) lift();
    this.restrictions.clear();
    for (const state of this.states.values())
      if (state.gate) void this.cancelGate(state.gate.id);
  }
}

export function installNative(
  ctx: NativeContext,
  config: Config,
): NativeSupervisor {
  const transport = new HttpTransport(config);
  const supervisor = new NativeSupervisor(
    ctx,
    transport,
    join(transport.stateDir, "native-connector"),
  );
  supervisor.install();
  return supervisor;
}
