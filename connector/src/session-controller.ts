/** Connector entry for Harness hosts exposing the sessionController service. */
import { apply as connect, type Api } from "./index.js";

export const name = "dsh-supervisor-connector";
export const inject = ["sessionController"];

type Prompt = Parameters<Api["sessions"]["prompt"]>[0]["payload"];
type History = Extract<
  Awaited<ReturnType<Api["sessions"]["history"]>>["result"],
  { ok: true }
>["value"];

/** Host methods used without activating sessions during receipt inspection. */
export interface SessionController {
  prompt(
    request: Prompt & { requestId: string },
    signal: AbortSignal,
  ): Promise<{ accepted: true }>;
  cancel(request: { sessionId: string }): { accepted: true };
  inspect(
    sessionId: string,
    signal?: AbortSignal,
  ): Promise<{
    events: History["events"][number]["event"][];
  }>;
}

/** Preserve operation IDs and raw event receipts across the newer Host API.
 * @param controller Host session command and inspection service.
 * @returns Adapter consumed by the connector's existing delivery ledger.
 */
export function adaptController(controller: SessionController): Api {
  return {
    sessions: {
      async prompt({ rpcId, payload }) {
        const value = await controller.prompt(
          { ...payload, requestId: rpcId },
          AbortSignal.timeout(5000),
        );
        return { rpcId, result: { ok: true, value } };
      },
      async cancel({ rpcId, payload }) {
        const value = controller.cancel(payload);
        return { rpcId, result: { ok: true, value } };
      },
      async history({ rpcId, payload }) {
        const snapshot = await controller.inspect(
          payload.sessionId,
          AbortSignal.timeout(2000),
        );
        return {
          rpcId,
          result: {
            ok: true,
            value: { events: snapshot.events.map((event) => ({ event })) },
          },
        };
      },
    },
  };
}

/** Start the shared heartbeat and delivery loop after the Host service is ready.
 * @param ctx Cordis context providing the current session controller.
 * @param config Local supervisor paths and origin.
 */
export function apply(
  ctx: {
    sessionController: SessionController;
    effect(callback: () => () => void, label?: string): void;
  },
  config: Parameters<typeof connect>[1] = {},
): void {
  connect(
    {
      apiProxy: adaptController(ctx.sessionController),
      effect: ctx.effect.bind(ctx),
    },
    config,
  );
}
