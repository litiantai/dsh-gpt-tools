import test from "node:test";
import assert from "node:assert/strict";
import { dispatch, consumed } from "../dist/index.js";
const command = {
  id: "unique-operation",
  session_id: "session-test",
  kind: "steer",
  text: "请检查边界条件",
};
test("steer uses exact session, operation id and timezone", async () => {
  let received;
  const api = {
    sessions: {
      prompt: async (request) => {
        received = request;
        return { result: { ok: true, value: { accepted: true } } };
      },
    },
  };
  assert.equal((await dispatch(api, command)).status, "accepted");
  assert.equal(received.rpcId, command.id);
  assert.equal(received.payload.mode, "steer");
  assert.equal(received.payload.sessionId, command.session_id);
  assert.equal(received.payload.clientTimeZone, "Asia/Shanghai");
});
test("stop calls cancel, not prompt", async () => {
  let session;
  const api = {
    sessions: {
      cancel: async (request) => {
        session = request.payload.sessionId;
        return { result: { ok: true, value: { accepted: true } } };
      },
    },
  };
  assert.equal(
    (await dispatch(api, { ...command, kind: "stop" })).status,
    "accepted",
  );
  assert.equal(session, command.session_id);
});
test("ambiguous transport failure is never reported as failed or accepted", async () => {
  const api = {
    sessions: {
      prompt: async () => {
        throw new Error("connection lost");
      },
    },
  };
  assert.equal((await dispatch(api, command)).status, "unknown");
});
test("explicit host rejection is failed", async () => {
  const api = {
    sessions: {
      prompt: async () => ({
        result: { ok: false, error: { message: "agent-busy" } },
      }),
    },
  };
  assert.deepEqual(await dispatch(api, command), {
    id: command.id,
    status: "failed",
    detail: "agent-busy",
  });
});
test("consumption requires exact source rpcId on a user message", async () => {
  let event = {
    type: "user/message",
    data: {
      message: { source: { rpcId: "another-request" }, content: command.text },
    },
  };
  const api = {
    sessions: {
      history: async () => ({
        result: { ok: true, value: { events: [{ event }] } },
      }),
    },
  };
  assert.equal(await consumed(api, command), false);
  event = {
    type: "user/message",
    data: { message: { source: { rpcId: command.id } } },
  };
  assert.equal(await consumed(api, command), true);
});

test("hung host call becomes unknown without resending", async () => {
  let calls = 0;
  const api = {
    sessions: {
      prompt: () => {
        calls++;
        return new Promise(() => {});
      },
    },
  };
  assert.equal((await dispatch(api, command, 20)).status, "unknown");
  assert.equal(calls, 1);
});
