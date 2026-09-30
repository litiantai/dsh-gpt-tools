/** Connect the local supervisor to Harness without a browser or stored login cookies. */
import { readFile, mkdir, rename, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";

export const name = "dsh-supervisor-connector";
export const inject = ["apiProxy"];
type Rpc<T> = {
  rpcId: string;
  result: { ok: true; value: T } | { ok: false; error: { message: string } };
};
type Request<T> = { rpcId: string; payload: T };
export interface Api {
  sessions: {
    prompt(
      request: Request<{
        sessionId: string;
        mode: "steer";
        content: { type: "text"; text: string }[];
        clientTimeZone: string;
      }>,
    ): Promise<Rpc<{ accepted: true }>>;
    cancel(
      request: Request<{ sessionId: string }>,
    ): Promise<Rpc<{ accepted: true }>>;
    history(
      request: Request<{ sessionId: string; maxMessages: number }>,
    ): Promise<
      Rpc<{
        events: {
          event: {
            type: string;
            data?: { message?: { source?: { rpcId?: string } } };
          };
        }[];
      }>
    >;
  };
}
interface Context {
  apiProxy: Api;
  effect(callback: () => () => void, label?: string): void;
}
export interface Command {
  id: string;
  session_id: string;
  kind: "steer" | "stop";
  text: string;
}
export interface Report {
  id: string;
  status: "accepted" | "consumed" | "failed" | "unknown";
  detail?: string;
}
interface RecordEntry {
  command: Command;
  report: Report;
}

/** Bound waiting without retrying a possibly admitted side effect. */
async function deadline<T>(
  operation: Promise<T>,
  milliseconds = 5000,
): Promise<T> {
  let timer: ReturnType<typeof setTimeout>;
  try {
    return await Promise.race([
      operation,
      new Promise<never>((_, reject) => {
        timer = setTimeout(
          () => reject(new Error("Harness 响应超时，送达待核实")),
          milliseconds,
        );
      }),
    ]);
  } finally {
    clearTimeout(timer!);
  }
}

/** Exact RPC-id correlation: repeated text alone cannot prove consumption. */
export async function consumed(api: Api, command: Command): Promise<boolean> {
  const response = await deadline(
    api.sessions.history({
      rpcId: randomUUID(),
      payload: { sessionId: command.session_id, maxMessages: 100 },
    }),
    2000,
  );
  return (
    response.result.ok &&
    response.result.value.events.some(
      ({ event }) =>
        event.type === "user/message" &&
        event.data?.message?.source?.rpcId === command.id,
    )
  );
}

export async function dispatch(
  api: Api,
  command: Command,
  timeout = 5000,
): Promise<Report> {
  try {
    const response = await deadline(
      command.kind === "stop"
        ? api.sessions.cancel({
            rpcId: command.id,
            payload: { sessionId: command.session_id },
          })
        : api.sessions.prompt({
            rpcId: command.id,
            payload: {
              sessionId: command.session_id,
              mode: "steer",
              content: [{ type: "text", text: command.text }],
              clientTimeZone: "Asia/Shanghai",
            },
          }),
      timeout,
    );
    return response.result.ok
      ? { id: command.id, status: "accepted" }
      : {
          id: command.id,
          status: "failed",
          detail: response.result.error.message,
        };
  } catch (error) {
    return {
      id: command.id,
      status: "unknown",
      detail: error instanceof Error ? error.message : String(error),
    };
  }
}

export function apply(
  ctx: Context,
  config: { stateDir?: string; home?: string; origin?: string } = {},
): void {
  const home = resolve(
    config.home || process.env.DSH_HOME || join(homedir(), ".dsh"),
  );
  const state = resolve(
    config.stateDir ||
      process.env.DSH_SUPERVISOR_STATE ||
      join(home, "supervisor"),
  );
  const origin = config.origin || "http://127.0.0.1:13084";
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(origin))
    throw new Error("Supervisor must use a loopback HTTP address");
  const ledgerPath = join(state, "connector-ledger.json");
  let closed = false;
  let busy = false;
  let loaded = false;
  let ledger: Record<string, RecordEntry> = {};
  const save = async () => {
    await mkdir(state, { recursive: true, mode: 0o700 });
    const tmp = `${ledgerPath}.${process.pid}.tmp`;
    await writeFile(tmp, JSON.stringify(ledger), { mode: 0o600 });
    await rename(tmp, ledgerPath);
  };
  const poll = async () => {
    if (closed || busy) return;
    busy = true;
    try {
      if (!loaded) {
        try {
          ledger = JSON.parse(await readFile(ledgerPath, "utf8"));
        } catch (error) {
          if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
        }
        loaded = true;
      }
      // Retain outcomes across acknowledgement loss and process restarts. Never resend unknown input.
      await Promise.all(
        Object.values(ledger)
          .filter(
            (entry) =>
              entry.command.kind === "steer" &&
              ["accepted", "unknown"].includes(entry.report.status),
          )
          .slice(-20)
          .map(async (entry) => {
            try {
              if (await consumed(ctx.apiProxy, entry.command))
                entry.report = { id: entry.command.id, status: "consumed" };
            } catch {
              /* Keep ambiguous delivery visible and continue the heartbeat. */
            }
          }),
      );
      await save();
      const key = (await readFile(join(state, "connector.key"), "utf8")).trim();
      const reports = Object.values(ledger)
        .slice(-40)
        .map((entry) => entry.report);
      const response = await fetch(`${origin}/api/connector/poll`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${key}`,
        },
        body: JSON.stringify({ home, reports }),
        signal: AbortSignal.timeout(5000),
      });
      if (!response.ok) throw new Error(`Dashboard HTTP ${response.status}`);
      const result = (await response.json()) as {
        commands: Command[];
        acknowledged: string[];
      };
      for (const id of result.acknowledged || []) delete ledger[id];
      await save();
      for (const command of result.commands) {
        if (closed) break;
        if (ledger[command.id]) continue;
        ledger[command.id] = {
          command,
          report: {
            id: command.id,
            status: "unknown",
            detail: "已领取，正在核实送达",
          },
        };
        await save();
        ledger[command.id].report = await dispatch(ctx.apiProxy, command);
        await save();
      }
    } catch {
      // Offline dashboard is expected; reconnect without logging credentials or dispatching twice.
    } finally {
      busy = false;
    }
  };
  const timer = setInterval(() => {
    void poll();
  }, 1000);
  void poll();
  ctx.effect(
    () => () => {
      closed = true;
      clearInterval(timer);
    },
    "supervisor connector",
  );
}
