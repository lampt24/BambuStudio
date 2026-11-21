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
    ai_base_url: '', ai_key: 'sk-very-secret-key', ai_model: '', vision_model: '', webhook_url: '',
  }, null, 2), { mode: 0o600 });
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

    console.log('4a. Tabs switch views and follow the URL hash');
    const tab = async (name) => {
      await page.click(`.nav-item[data-tab="${name}"]`);
      await page.waitForFunction((n) => !document.querySelector(`.view[data-view="${n}"]`).hidden, name);
    };
    for (const name of ['alerts', 'ai', 'log', 'printers']) {
      await tab(name);
      assert.strictEqual(await page.getAttribute(`.nav-item[data-tab="${name}"]`, 'aria-current'), 'page');
      assert.strictEqual(await page.locator('.view:not([hidden])').count(), 1);
    }
    await page.goto(BASE + '/#log');
    await page.waitForSelector('#events .event');
    assert.ok(await page.isHidden('.view[data-view="printers"]'));
    await tab('printers');
    assert.strictEqual((await page.textContent('#alerts-count')).trim(), '1');

    console.log('4b. Alerts are attributed per printer');
    const second = page.locator('.printer').nth(1);
    assert.strictEqual((await second.locator('.alert-count').textContent()).trim(), '1');
    assert.ok(await first.locator('.alert-count').isHidden());
    await tab('alerts');
    assert.strictEqual(await page.locator('#alert-printers .chip').count(), 3); // all + 2 printers
    await page.locator('#alert-printers .chip', { hasText: 'A1 trái' }).click();
    assert.strictEqual(await page.locator('#alerts .alert').count(), 0);
    await page.locator('#alert-printers .chip', { hasText: 'A1 phải' }).click();
    assert.strictEqual(await page.locator('#alerts .alert').count(), 1);
    await page.locator('#alert-printers .chip', { hasText: 'Tất cả' }).click();

    console.log('4c. Activity log lists events with printer filter');
    await tab('log');
    await page.waitForSelector('#events .event');
    assert.strictEqual(await page.textContent('#events .day'), 'Hôm nay');
    assert.ok((await page.textContent('#events')).includes('Phát hiện máy in mới'));
    await page.selectOption('#ev-printer', 'a1-02');
    await page.waitForFunction(() => [...document.querySelectorAll('#events .event')].every((e) => !e.textContent.includes('A1 trái')));
    await page.selectOption('#ev-printer', '');

    console.log('4d. Detail dialog shows that printer\'s alerts and activity');
    await tab('printers');
    await second.click();
    await page.waitForFunction(() => document.querySelectorAll('#detail-alerts .alert').length === 1);
    await page.waitForSelector('#detail-events .event');
    await page.keyboard.press('Escape');

    console.log('4e. Printer controls follow state and round-trip through the bridge');
    await first.click();
    await page.waitForFunction(() => document.getElementById('detail').open);
    assert.ok(await page.isEnabled('#detail [data-cmd="pause"]'));
    assert.ok(await page.isDisabled('#detail [data-cmd="resume"]'));
    assert.ok(await page.isEnabled('#detail [data-cmd="speed"][data-level="3"]'));
    await page.click('#detail [data-cmd="pause"]');
    let polled = [];
    for (let i = 0; i < 20 && !polled.length; i++) {
      await new Promise((r) => setTimeout(r, 300));
      const res = await fetch(BASE + '/api/device/commands/poll', {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${DEVICE}` },
        body: JSON.stringify({ device_id: 'redmi' }) });
      polled = (await res.json()).commands;
    }
    assert.strictEqual(polled.length, 1);
    assert.strictEqual(polled[0].command, 'pause');
    assert.strictEqual(await post('/api/device/commands/result', { id: polled[0].id, ok: true, reason: 'success' }), 200);
    await page.waitForFunction(() => document.getElementById('ctl-status').textContent.includes('đã thực hiện'), null, { timeout: 10000 });
    // Light: confirmed "on" must flip the button even while the server still holds the old state
    assert.strictEqual(await page.getAttribute('#ctl-light', 'aria-label'), 'Bật đèn');
    await page.click('#ctl-light');
    polled = [];
    for (let i = 0; i < 20 && !polled.length; i++) {
      await new Promise((r) => setTimeout(r, 300));
      const res = await fetch(BASE + '/api/device/commands/poll', {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${DEVICE}` },
        body: JSON.stringify({ device_id: 'redmi' }) });
      polled = (await res.json()).commands;
    }
    assert.deepStrictEqual([polled[0].command, polled[0].params.mode], ['light', 'on']);
    assert.strictEqual(await post('/api/device/commands/result', { id: polled[0].id, ok: true, reason: 'success' }), 200);
    await page.waitForFunction(() => document.getElementById('ctl-light').getAttribute('aria-label') === 'Tắt đèn', null, { timeout: 10000 });
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await new Promise((r) => setTimeout(r, 1500));
    assert.strictEqual(await page.getAttribute('#ctl-light', 'aria-label'), 'Tắt đèn');
    page.once('dialog', (d) => d.dismiss());
    await page.click('#detail [data-cmd="stop"]');   // dismissed confirm: nothing queued
    await new Promise((r) => setTimeout(r, 500));
    const none = await (await fetch(BASE + '/api/device/commands/poll', {
      method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${DEVICE}` },
      body: JSON.stringify({ device_id: 'redmi' }) })).json();
    assert.deepStrictEqual(none.commands, []);
    await page.keyboard.press('Escape');

    console.log('4f. Live view: card button starts streaming, closing stops it');
    const devPoll = async () => (await (await fetch(BASE + '/api/device/commands/poll', {
      method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${DEVICE}` },
      body: JSON.stringify({ device_id: 'redmi' }) })).json());
    await first.locator('.live-btn').click();
    await page.waitForFunction(() => document.getElementById('detail').open);
    let live = [];
    for (let i = 0; i < 20 && !live.includes('a1-01'); i++) {
      await new Promise((r) => setTimeout(r, 200));
      live = (await devPoll()).live;
    }
    assert.ok(live.includes('a1-01'));
    for (let i = 0; i < 3; i++) {
      const r = await fetch(BASE + '/api/device/live/frame', {
        method: 'POST', headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${DEVICE}` },
        body: JSON.stringify({ printer_id: 'a1-01', image_base64: JPEG_B64 }) });
      assert.strictEqual((await r.json()).accepted, true);
      await new Promise((r2) => setTimeout(r2, 400));
    }
    await page.waitForFunction(() => document.getElementById('live-state').textContent.includes('TRỰC TIẾP'));
    assert.ok((await page.getAttribute('#detail-img', 'src')).startsWith('blob:'));
    assert.strictEqual(await page.getAttribute('#btn-live', 'aria-label'), 'Dừng xem');
    assert.strictEqual(await page.getAttribute('#btn-live', 'aria-pressed'), 'true');
    await tab('log');   // leaving the printers tab stops the stream
    await new Promise((r) => setTimeout(r, 500));
    assert.deepStrictEqual((await devPoll()).live, []);
    assert.strictEqual(await page.getAttribute('#btn-live', 'aria-pressed'), 'false');
    await tab('printers');

    console.log('5. Ack removes the alert from the open list');
    await tab('alerts');
    const before = await page.locator('#alerts .alert').count();
    assert.ok(before >= 1);
    await page.locator('#alerts .alert .ack-btn').first().click();
    await page.waitForFunction((n) => document.querySelectorAll('#alerts .alert').length === n - 1, before);
    await page.waitForFunction(() => document.getElementById('events').textContent.includes('Đã xử lý cảnh báo'));
    assert.ok(await page.evaluate(() => document.querySelector('.printer:nth-child(2) .alert-count').hidden));
    assert.ok(await page.isHidden('#alerts-count'));

    console.log('6. Desktop: details docked on the right, selection follows cards and sidebar');
    await tab('printers');
    await first.click();
    assert.ok(await page.evaluate(() => document.getElementById('detail').open && document.getElementById('detail').classList.contains('docked')));
    assert.ok(!(await page.evaluate(() => document.getElementById('detail').matches(':modal'))));
    assert.ok((await page.textContent('#detail-metrics')).includes('42%'));
    assert.ok(await first.evaluate((c) => c.classList.contains('selected')));
    const pBox = await page.locator('.printer-col').boundingBox();
    const dBox = await page.locator('#detail').boundingBox();
    assert.ok(dBox.x > pBox.x + pBox.width - 1, 'detail should sit to the right of the list');
    await page.click('.nav-printer[data-printer="a1-02"]');
    await page.waitForFunction(() => document.getElementById('detail-title').textContent === 'A1 phải');
    assert.ok(await second.evaluate((c) => c.classList.contains('selected')));
    assert.strictEqual((await page.textContent('#nav-printers-count')).trim(), '2');

    console.log('7. CSP header present and no console errors');
    const res = await fetch(BASE + '/');
    assert.ok((res.headers.get('content-security-policy') || '').includes("script-src 'self'"));
    // The single 401 comes from the deliberate wrong-token attempt in step 2
    assert.deepStrictEqual(errors.filter((e) => !e.includes('status of 401')), []);

    console.log('8. Settings: load, change, save, secrets never sent to the page');
    await page.click('#btn-settings');
    await page.waitForFunction(() => document.getElementById('settings').open);
    assert.ok(!(await page.content()).includes('sk-very-secret-key'));
    assert.strictEqual(await page.getAttribute('[name=ai_key]', 'placeholder'), 'Đã lưu, để trống nếu giữ nguyên');
    await page.fill('[name=telegram_chat_id]', '123456');
    await page.selectOption('[name=snapshot_interval_seconds]', '300');
    await page.check('[name=capture_when_idle]');
    await page.click('#settings-save');
    await page.waitForFunction(() => document.getElementById('settings-status').textContent === 'Đã lưu');
    const saved = await (await fetch(BASE + '/api/settings', { headers: { Authorization: `Bearer ${MANAGER}` } })).json();
    assert.strictEqual(saved.settings.telegram_chat_id, '123456');
    assert.strictEqual(saved.settings.snapshot_interval_seconds, 300);
    assert.strictEqual(saved.settings.capture_when_idle, true);
    assert.strictEqual(saved.settings.ai_key_set, true);
    const onDisk = JSON.parse(fs.readFileSync(cfgPath, 'utf8'));
    assert.strictEqual(onDisk.ai_key, 'sk-very-secret-key');
    assert.strictEqual(onDisk.telegram_chat_id, '123456');
    await page.click('#settings [data-action="cleanup"]');
    await page.waitForFunction(() => document.querySelector('[data-result="cleanup"]').dataset.state === 'ok');
    await page.click('#settings .dialog-foot [data-close]');

    console.log('8b. Theme switch overrides the OS and persists');
    await page.emulateMedia({ colorScheme: 'dark' });
    const bg = () => page.evaluate(() => getComputedStyle(document.body).backgroundColor);
    const darkBg = await bg();
    await page.click('#btn-theme');
    await page.click('[data-theme-choice="light"]');
    assert.strictEqual(await page.getAttribute('html', 'data-theme'), 'light');
    assert.notStrictEqual(await bg(), darkBg);
    await page.reload();
    await page.waitForSelector('.printer');
    assert.strictEqual(await page.getAttribute('html', 'data-theme'), 'light');
    await page.click('#btn-theme');
    await page.click('[data-theme-choice="dark"]');
    assert.strictEqual(await bg(), darkBg);
    await page.click('#btn-theme');
    await page.click('[data-theme-choice="auto"]');
    assert.strictEqual(await page.getAttribute('html', 'data-theme'), null);
    assert.strictEqual(await bg(), darkBg); // OS is dark

    console.log('9. Phone: bottom tab bar, no horizontal overflow');
    await page.setViewportSize({ width: 360, height: 780 });
    assert.ok(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)));
    const navBox = await page.locator('.nav').boundingBox();
    assert.ok(Math.abs(navBox.y + navBox.height - 780) < 2, `tab bar should sit at the bottom, got y=${navBox.y}`);
    await tab('log');
    assert.ok(!(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)));
    await tab('printers');
    assert.ok(!(await page.evaluate(() => document.getElementById('detail').open)), 'docked panel closes on narrow screens');
    await first.click();
    assert.ok(await page.evaluate(() => document.getElementById('detail').matches(':modal')));
    await page.keyboard.press('Escape');
    assert.ok(!(await page.evaluate(() => document.getElementById('detail').open)));
    await page.click('#btn-settings');
    await page.waitForFunction(() => document.getElementById('settings').open);
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
