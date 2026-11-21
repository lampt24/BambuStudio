#!/usr/bin/env node
/* Dashboard integration test: real PrintPilot server + headless browser.
 *
 *   PLAYWRIGHT_MODULE=/path/to/node_modules/playwright \
 *   PLAYWRIGHT_CHROMIUM=/path/to/chrome-headless-shell \
 *   node services/printpilot/web/test_dashboard.js
 *
 * Seeds two printers, a snapshot and an XSS-shaped alert through the device
 * API, then checks login, rendering, escaping, ack, detail dialog, CSP and
 * mobile layout.
 */
'use strict';

const assert = require('assert');
const { spawn } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const playwright = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const PORT = 18000 + Math.floor(Math.random() * 900);
const BASE = `http://127.0.0.1:${PORT}`;
const MANAGER = 'test-manager-token-0123456789';
const DEVICE = 'test-device-token-9876543210';
// 1x1 white JPEG
const JPEG_B64 = '/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=';

async function post(pathname, body, token = DEVICE) {
  const res = await fetch(BASE + pathname, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  return res.status;
}

async function waitForServer() {
  for (let i = 0; i < 50; i++) {
    try { if ((await fetch(`${BASE}/health`)).ok) return; } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error('server did not start');
}

async function main() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'pp-dash-'));
  const cfgPath = path.join(dir, 'server.json');
  fs.writeFileSync(cfgPath, JSON.stringify({
    host: '127.0.0.1', port: PORT, data_dir: dir,
    manager_token: MANAGER, device_token: DEVICE,
    ai_base_url: '', ai_key: '', ai_model: '', vision_model: '', webhook_url: '',
  }));
  const server = spawn('python3', [path.join(__dirname, '..', 'server.py'), cfgPath], { stdio: 'ignore' });
  let browser;
  try {
    await waitForServer();

    // Seed data
    assert.strictEqual(await post('/api/heartbeat', {
      device_id: 'redmi', printer_id: 'a1-01', status: 'printing', printer_name: 'A1 trái', printer_model: 'A1',
      telemetry: { progress: 42, nozzle_temp: 220, bed_temp: 60, current_layer: 10, total_layers: 100, remaining_min: 75, job_name: 'vase.3mf' },
    }), 200);
    assert.strictEqual(await post('/api/heartbeat', {
      device_id: 'redmi', printer_id: 'a1-02', status: 'idle', printer_name: 'A1 phải', printer_model: 'A1',
      error: '<img src=x onerror="window.__xss=1">',
    }), 200);
    assert.strictEqual(await post('/api/snapshots', {
      device_id: 'redmi', printer_id: 'a1-01', captured_at: new Date().toISOString().replace(/\.\d+Z$/, 'Z'),
      image_base64: JPEG_B64, status: 'printing', telemetry: { progress: 42 },
    }), 201);

    browser = await playwright.chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM || undefined });
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on('pageerror', (e) => errors.push(e.message));
    page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });

    await page.goto(BASE);
    console.log('1. Login dialog shown without credentials');
    assert.ok(await page.evaluate(() => document.getElementById('login').open));

    console.log('2. Wrong token rejected, right token accepted');
    await page.fill('#login-token', 'wrong');
    await page.click('#login-submit');
    await page.waitForSelector('#login-error:not([hidden])');
    await page.fill('#login-token', MANAGER);
    await page.click('#login-submit');
    await page.waitForSelector('.printer');
    assert.ok(!(await page.evaluate(() => document.getElementById('login').open)));

    console.log('3. Two printer cards with telemetry');
    assert.strictEqual(await page.locator('.printer').count(), 2);
    const first = page.locator('.printer').first();
    assert.ok((await first.textContent()).includes('A1 trái'));
    assert.ok((await first.textContent()).includes('42%'));
    assert.ok((await first.textContent()).includes('còn 1h15'));
    await page.waitForFunction(() => document.querySelector('.printer img')?.src.startsWith('blob:'));

    console.log('4. Server text is escaped');
    assert.strictEqual(await page.evaluate(() => window.__xss), undefined);
    assert.ok((await page.textContent('#alerts')).includes('<img src=x'));

    console.log('5. Ack removes the alert from the open list');
    const before = await page.locator('#alerts .alert').count();
    assert.ok(before >= 1);
    await page.locator('#alerts .alert button').first().click();
    await page.waitForFunction((n) => document.querySelectorAll('#alerts .alert').length === n - 1, before);

    console.log('6. Detail dialog opens and closes');
    await first.click();
    assert.ok(await page.evaluate(() => document.getElementById('detail').open));
    assert.ok((await page.textContent('#detail-metrics')).includes('42%'));
    await page.keyboard.press('Escape');
    assert.ok(!(await page.evaluate(() => document.getElementById('detail').open)));

    console.log('7. CSP header present and no console errors');
    const res = await fetch(BASE + '/');
    assert.ok((res.headers.get('content-security-policy') || '').includes("script-src 'self'"));
    // The single 401 comes from the deliberate wrong-token attempt in step 2
    assert.deepStrictEqual(errors.filter((e) => !e.includes('status of 401')), []);

    console.log('8. No horizontal overflow on phone width');
    await page.setViewportSize({ width: 360, height: 780 });
    assert.ok(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)));

    console.log('\nOK: dashboard integration test passed');
  } finally {
    if (browser) await browser.close();
    server.kill();
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

main().catch((err) => {
  console.error('FAILED:', err);
  process.exit(1);
});
