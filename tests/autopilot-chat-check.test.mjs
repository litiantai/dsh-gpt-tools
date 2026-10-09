import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from '@playwright/test';
import { acceptanceFailure, waitForResearchReply } from '../scripts/autopilot-chat-check.mjs';

let browser;
before(async () => { browser = await chromium.launch({headless:true}); });
after(async () => { await browser?.close(); });
const title = '<aside>研究会话已就绪</aside><nav>研究会话已就绪</nav>';

test('session titles and user echoes cannot satisfy assistant acceptance', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent(title + '<div data-chat-flow-kind="user-message">研究会话已就绪</div>');
    await assert.rejects(waitForResearchReply(page,{timeout:60,interval:10}), /验收时限/);
    await page.setContent(title + '<div data-chat-flow-kind="assistant-step"><p>研究会话已就绪</p></div>');
    await waitForResearchReply(page,{timeout:100,interval:10});
  } finally { await page.close(); }
});

test('quota failure terminates immediately with non-retryable blocked receipt', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent(title + '<div data-chat-flow-kind="turn-error">This turn failed Insufficient Balance (request_id: fixture) QUOTA</div>');
    const start = Date.now();
    await assert.rejects(waitForResearchReply(page), error => {
      const result = acceptanceFailure(error);
      assert.equal(result.status, 'blocked');
      assert.equal(result.error_code, 'MODEL_BALANCE_INSUFFICIENT');
      assert.equal(result.retryable, false);
      return true;
    });
    assert.ok(Date.now()-start < 2000);
  } finally { await page.close(); }
});

test('hidden answers cannot pass and ordinary product failures remain failures', async () => {
  const page = await browser.newPage();
  try {
    await page.setContent('<div hidden data-chat-flow-kind="assistant-step">研究会话已就绪</div>');
    await assert.rejects(waitForResearchReply(page,{timeout:60,interval:10}));
    assert.equal(acceptanceFailure(new Error('Chart reported rendering failure')).status,'fail');
    assert.equal(acceptanceFailure(new Error('strict mode violation')).status,'blocked');
  } finally { await page.close(); }
});
