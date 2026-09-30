import test from "node:test";
import assert from "node:assert/strict";
import { adaptController } from "../dist/session-controller.js";
import { dispatch, consumed } from "../dist/index.js";

const command = {
  id: "operation-modern",
  session_id: "session-modern",
  kind: "steer",
  text: "检查边界",
};

test("controller receives the durable request ID, target, steer mode and cancellation signal", async () => {
  let received;
  const api = adaptController({
    async prompt(request, signal) {
      received = request;
      assert.ok(signal instanceof AbortSignal);
      return { accepted: true };
    },
  });
  assert.equal((await dispatch(api, command)).status, "accepted");
  assert.deepEqual(received, {
    requestId: command.id,
    sessionId: command.session_id,
    mode: "steer",
    content: [{ type: "text", text: command.text }],
    clientTimeZone: "Asia/Shanghai",
  });
});

test("controller cancel retains the exact target and never invokes prompt", async () => {
  let target;
  const api = adaptController({
    cancel(request) {
      target = request.sessionId;
      return { accepted: true };
    },
  });
  assert.equal(
    (await dispatch(api, { ...command, kind: "stop" })).status,
    "accepted",
  );
  assert.equal(target, command.session_id);
});

test("read-only inspection correlates consumption by the original operation ID", async () => {
  let rpcId = "other-operation";
  const api = adaptController({
    async inspect(sessionId, signal) {
      assert.equal(sessionId, command.session_id);
      assert.ok(signal instanceof AbortSignal);
      return {
        events: [
          { type: "user/message", data: { message: { source: { rpcId } } } },
        ],
      };
    },
  });
  assert.equal(await consumed(api, command), false);
  rpcId = command.id;
  assert.equal(await consumed(api, command), true);
});

test("controller exception remains unknown and is never retried", async () => {
  let calls = 0;
  const api = adaptController({
    async prompt() {
      calls++;
      throw new Error("admission interrupted");
    },
  });
  assert.equal((await dispatch(api, command)).status, "unknown");
  assert.equal(calls, 1);
});
