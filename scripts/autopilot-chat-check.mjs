// Use Harness's semantic chat nodes; sidebar titles and user echoes are not replies.
export function acceptanceFailure(error) {
  const reason = String(error);
  const balance = /Insufficient Balance|余额不足|MODEL_BALANCE_INSUFFICIENT/i.test(reason);
  const infrastructure = /AUTOPILOT_BLOCKED|TimeoutError|strict mode violation/.test(reason);
  return {
    status: balance || infrastructure ? 'blocked' : 'fail',
    reason: balance ? `模型服务余额不足（QUOTA），恢复模型账户余额后重试；${reason}` : reason,
    ...(balance ? { failure_kind: 'agent_execution', error_code: 'MODEL_BALANCE_INSUFFICIENT', retryable: false } :
      infrastructure ? { failure_kind: 'acceptance_infrastructure' } : {}),
  };
}

export async function waitForResearchReply(page, { timeout = 120000, interval = 250 } = {}) {
  const reply = page.locator('[data-chat-flow-kind="assistant-step"]:visible')
    .getByText('研究会话已就绪', { exact: true }).first();
  const failure = page.locator('[data-chat-flow-kind="turn-error"]:visible').last();
  const deadline = Date.now() + timeout;
  do {
    if (await failure.isVisible()) {
      const detail = await failure.innerText();
      throw new Error(`AUTOPILOT_BLOCKED: 研究会话执行失败：${detail}`);
    }
    if (await reply.isVisible()) return;
    await new Promise(resolve => setTimeout(resolve, interval));
  } while (Date.now() < deadline);
  throw new Error('AUTOPILOT_BLOCKED: 研究会话未在验收时限内返回目标回复；需核对模型服务及会话证据');
}
