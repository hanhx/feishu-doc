import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright-core';
import { readBrowserAsset, readBrowserAssets } from '../scripts/read_browser_asset.mjs';

const docUrl = 'https://fixture.feishu.cn/docx/sample';

test('browser reads the specified blocks and uses the visible Excel download', async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'feishu-assets-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  t.after(() => browser.close());
  const context = await browser.newContext({ acceptDownloads: true });
  t.after(() => context.close());
  const page = await context.newPage();
  const expectedRequests = [];
  await page.route('https://fixture.feishu.cn/**', async (route) => {
    const url = new URL(route.request().url());
    expectedRequests.push(url.pathname);
    if (url.pathname === '/workbook') {
      await route.fulfill({
        status: 200,
        headers: { 'content-disposition': 'attachment; filename="fixture.xlsx"' },
        body: 'fixture workbook bytes',
      });
      return;
    }
    await route.fulfill({ contentType: 'text/html; charset=utf-8', body: `<!doctype html>
      <main class="docx-page-block">
        <div data-record-id="picture"><img width="10" height="10" src="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10'%3E%3Crect width='10' height='10' fill='blue'/%3E%3C/svg%3E"></div>
        <div data-record-id="board"><div class="whiteboard-canvas-container"><canvas width="40" height="20"></canvas></div></div>
        <div data-record-id="table" style="position:relative;width:100px;height:72px">
          <canvas role="faster" width="100" height="72"></canvas>
          <div class="faster-dom-over-layer" style="position:absolute;inset:0;pointer-events:none">
            <div style="position:absolute;left:12px;top:18px;width:88px;height:54px"></div>
            <div style="position:absolute;left:0;top:18px;width:12px;height:54px"></div>
          </div>
        </div>
        <div data-record-id="excel"><a href="/workbook">下载</a><a href="/other" hidden>下载</a></div>
        <div data-record-id="denied">申请权限</div>
      </main>
      <script>
        const grid = document.querySelector('canvas[role=faster]');
        const ctx = grid.getContext('2d');
        ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 100, 66);
        ctx.fillStyle = '#ccc';
        for (const y of [0, 17, 33, 49, 65]) ctx.fillRect(0, y, y === 33 || y === 49 ? 82 : 100, 1);
        ctx.fillRect(45, 0, 1, 50);
        ctx.fillRect(82, 0, 1, 66);
        ctx.fillRect(99, 0, 1, 66);
        ctx.fillStyle = '#000';
        ctx.fillRect(5, 53, 1, 7); ctx.fillRect(2, 56, 7, 1);
        ctx.fillRect(90, 4, 1, 7); ctx.fillRect(87, 7, 7, 1);
        let selected = false;
        grid.addEventListener('mousedown', (event) => {
          grid.dataset.start = [event.offsetX, event.offsetY].join(',');
        });
        grid.addEventListener('mouseup', (event) => {
          grid.dataset.end = [event.offsetX, event.offsetY].join(',');
          selected = event.offsetX < 90;
        });
        document.addEventListener('copy', (event) => {
          event.preventDefault();
          event.clipboardData.setData('text/plain', selected ? 'first\\t\\nsecond\\tvalue' : 'document text');
          if (selected) event.clipboardData.setData('text/html', '<table data-byte-bitable="grid"><tr><td>first</td><td></td></tr><tr><td>second</td><td>value</td></tr></table>');
        });
      </script>` });
  });
  const run = async ({ scope, loginUrl, allowedPageOrigins, authenticate }) => {
    assert.deepEqual(scope, { platform: 'feishu-doc', environment: 'fixture', account: 'default' });
    assert.deepEqual(allowedPageOrigins, ['https://fixture.feishu.cn']);
    await page.goto(loginUrl);
    return authenticate({ page, timeoutMs: 1000 });
  };

  for (const [block, type] of [['picture', 'image'], ['board', 'whiteboard'], ['table', 'bitable'], ['table', 'sheet']]) {
    const target = path.join(directory, `${type}.png`);
    const result = await readBrowserAsset(docUrl, block, type, target, undefined, run);
    assert.equal(result.mode, 'visible-preview');
    const bytes = await fs.readFile(target);
    assert.deepEqual([...bytes.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
    await assert.rejects(readBrowserAsset(docUrl, block, type, target, undefined, run), { code: 'OUTPUT_EXISTS' });
  }

  const excel = path.join(directory, 'workbook.xlsx');
  assert.equal((await readBrowserAsset(docUrl, 'excel', 'excel', excel, undefined, run)).mode, 'download');
  assert.equal(await fs.readFile(excel, 'utf8'), 'fixture workbook bytes');
  await assert.rejects(readBrowserAsset(docUrl, 'denied', 'bitable', path.join(directory, 'denied.png'), undefined, run), { code: 'ASSET_DENIED' });
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(docUrl).origin });
  const originalClipboard = await page.evaluate(() => navigator.clipboard.readText());
  try {
    await page.evaluate(() => navigator.clipboard.write([new ClipboardItem({
      'text/plain': new Blob(['original'], { type: 'text/plain' }),
      'text/html': new Blob(['<b>original</b>'], { type: 'text/html' }),
    })]));
    const table = path.join(directory, 'table.tsv');
    assert.equal((await readBrowserAsset(docUrl, 'table', 'bitable', table, '10,10:80,50', run)).mode, 'clipboard');
    assert.equal(await fs.readFile(table, 'utf8'), 'first\t\nsecond\tvalue');
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'original');
    assert.ok(await page.evaluate(async () => (await navigator.clipboard.read())[0].types.includes('text/html')));
    const automatic = await readBrowserAsset(docUrl, 'table', 'bitable', '', undefined, run);
    t.after(() => fs.rm(path.dirname(automatic.path), { recursive: true, force: true }));
    assert.equal(automatic.rowCount, 2);
    assert.equal(automatic.columnCount, 2);
    assert.equal(await fs.readFile(automatic.path, 'utf8'), 'first\t\nsecond\tvalue');
    const grid = page.locator('canvas[role=faster]');
    assert.equal(await grid.getAttribute('data-start'), '28,25');
    assert.equal(await grid.getAttribute('data-end'), '63,41');
    await assert.rejects(readBrowserAsset(docUrl, 'table', 'bitable', path.join(directory, 'invalid.tsv'), '10,10:101,50', run), { code: 'INVALID_RANGE' });
    const wrong = path.join(directory, 'wrong.tsv');
    await assert.rejects(readBrowserAsset(docUrl, 'table', 'bitable', wrong, '10,10:95,50', run), { code: 'COPY_UNAVAILABLE' });
    await assert.rejects(fs.access(wrong), { code: 'ENOENT' });
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), 'original');
  } finally {
    await page.evaluate(text => navigator.clipboard.writeText(text), originalClipboard);
  }

  let sessions = 0;
  const navigationsBeforeBatch = expectedRequests.filter(request => request === '/docx/sample').length;
  const preview = path.join(directory, 'batch-table.png');
  const tableOutput = path.join(directory, 'batch-table.tsv');
  const batchJobs = [
    { blockId: 'picture', type: 'image', output: path.join(directory, 'batch-image.png') },
    { blockId: 'denied', type: 'bitable', output: path.join(directory, 'batch-denied.png') },
    { blockId: 'table', type: 'bitable', output: tableOutput, cellRange: '10,10:95,50', fallbackOutput: preview },
    { blockId: 'board', type: 'whiteboard', output: path.join(directory, 'batch-board.png') },
  ];
  await page.evaluate(() => navigator.clipboard.writeText(''));
  let batch;
  try {
    batch = await readBrowserAssets(docUrl, batchJobs, async options => {
      sessions += 1;
      assert.equal(options.launchOptions.headless, true);
      return run(options);
    });
    assert.equal(await page.evaluate(() => navigator.clipboard.readText()), '');
  } finally {
    await page.evaluate(text => navigator.clipboard.writeText(text), originalClipboard);
  }
  assert.equal(sessions, 1);
  assert.equal(expectedRequests.filter(request => request === '/docx/sample').length - navigationsBeforeBatch, 1);
  assert.deepEqual(batch.map(result => result.mode || result.error.code), [
    'visible-preview', 'ASSET_DENIED', 'visible-preview', 'visible-preview',
  ]);
  assert.equal(batch[2].fallbackReason, 'COPY_UNAVAILABLE');
  await fs.access(preview);
  await assert.rejects(fs.access(tableOutput), { code: 'ENOENT' });
  assert.equal(expectedRequests.filter((request) => request === '/workbook').length, 1);
  assert.ok(expectedRequests.every((request) => ['/docx/sample', '/workbook'].includes(request)));
});

test('a Feishu login redirect reopens visibly and resumes the same batch', async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'feishu-assets-login-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  t.after(() => browser.close());
  const page = await browser.newPage();
  const loginUrl = 'https://accounts.feishu.cn/accounts/page/login';
  const launches = [];
  let authorized = false;
  await page.route('https://fixture.feishu.cn/**', route => authorized
    ? route.fulfill({ contentType: 'text/html', body: `<main class="docx-page-block"><div data-record-id="board"><div class="whiteboard-canvas-container"><canvas></canvas></div></div></main>` })
    : route.fulfill({ status: 302, headers: { location: loginUrl } }));
  await page.route('https://accounts.feishu.cn/**', route => route.fulfill({ body: 'Login page' }));

  const results = await readBrowserAssets(docUrl, [
    { blockId: 'board', type: 'whiteboard', output: path.join(directory, 'board.png') },
  ], async ({ launchOptions, authenticate }) => {
    launches.push(launchOptions.headless);
    await page.goto(docUrl);
    assert.equal(new URL(page.url()).hostname, new URL(loginUrl).hostname);
    if (launchOptions.headless) return authenticate({ page, timeoutMs: 500 });
    const reading = authenticate({ page, timeoutMs: 5000 });
    authorized = true;
    await page.goto(docUrl);
    return reading;
  });
  assert.deepEqual(launches, [true, false]);
  assert.equal(results[0].mode, 'visible-preview');
});
