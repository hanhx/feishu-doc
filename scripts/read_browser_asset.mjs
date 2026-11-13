import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { text as readText } from 'node:stream/consumers';
import { AuthRuntimeError, runBrowserAuth, safeErrorMessage } from './browser_auth.mjs';
import { visibleBitableRange } from './bitable_grid.mjs';

const ID = /^[a-zA-Z0-9_-]+$/;
const CELL_RANGE = /^(\d{1,5}),(\d{1,5}):(\d{1,5}),(\d{1,5})$/;
const PREVIEW = {
  image: 'img',
  whiteboard: '.whiteboard-canvas-container:has(canvas:visible)',
  sheet: 'canvas:visible, [role="grid"]:visible, table:visible',
  bitable: 'canvas:visible, [role="grid"]:visible, table:visible',
};
const DOWNLOAD = /^(下载(?:附件|文件)?|Download(?: attachment| file)?)$/i;
const PERMISSION = /^(申请(?:阅读|访问)?权限|无权访问|暂无访问权限|Request access|Access denied)$/i;
const LOGIN_HOST = /^(?:accounts|passport|login|auth)\.feishu\.cn$/i;
const COPY_FALLBACK_CODES = new Set([
  'AUTO_RANGE_UNAVAILABLE', 'AUTO_RANGE_MISMATCH', 'COPY_UNAVAILABLE', 'INVALID_RANGE',
]);

function failure(code, message) {
  return new AuthRuntimeError(code, message);
}

async function waitForDocument(page, url, timeoutMs, detectLogin = false) {
  const target = current => current.origin === url.origin &&
    current.pathname.replace(/\/$/, '') === url.pathname.replace(/\/$/, '');
  const documentReady = async () => {
    await page.waitForURL(target, { timeout: timeoutMs });
    const content = page.locator('.docx-page-block').first();
    const denied = page.getByText(PERMISSION).first();
    await content.or(denied).first().waitFor({ timeout: 20_000 });
    if (!(await content.isVisible()) && await denied.isVisible()) {
      throw failure('DOCUMENT_DENIED', '当前飞书账号没有文档查看权限。');
    }
  };
  try {
    if (!detectLogin) return await documentReady();
    const login = page.waitForURL(current => LOGIN_HOST.test(current.hostname) ||
      (current.origin === url.origin && /^\/(?:login|passport|accounts)(?:\/|$)/i.test(current.pathname)),
    { timeout: timeoutMs }).then(() => {
      throw failure('BROWSER_LOGIN_REQUIRED', '飞书浏览器登录已失效。');
    });
    return await Promise.race([documentReady(), login]);
  } catch (error) {
    if (error instanceof AuthRuntimeError) throw error;
    if (await page.getByText(PERMISSION).first().isVisible()) {
      throw failure('DOCUMENT_DENIED', '当前飞书账号没有文档查看权限。');
    }
    throw failure('DOCUMENT_UNAVAILABLE', '飞书文档未打开。请检查专用浏览器登录状态及文档权限。');
  }
}

export async function locateBlock(page, url, blockId, timeoutMs) {
  await waitForDocument(page, url, timeoutMs);
  return findBlockOnPage(page, blockId, false);
}

async function findBlockOnPage(page, blockId, retryFromTop) {
  const block = page.locator(`[data-record-id="${blockId}"], [data-block-id="${blockId}"]`);
  if (!(await block.count())) {
    const viewport = page.viewportSize();
    if (viewport) await page.mouse.move(Math.floor(viewport.width / 2), Math.floor(viewport.height / 2));
    for (let pass = 0; pass < (retryFromTop ? 2 : 1); pass += 1) {
      if (pass) await page.mouse.wheel(0, -30 * 650);
      for (let attempt = 0; attempt < 30; attempt += 1) {
        await page.mouse.wheel(0, 650);
        try {
          await block.waitFor({ timeout: 500 });
          break;
        } catch {
          // Long documents mount offscreen blocks as the page scrolls.
        }
      }
      if (await block.count()) break;
    }
  }
  if (await block.count() !== 1) {
    throw failure('BLOCK_UNAVAILABLE', '页面没有找到唯一的指定块，未读取其他块。');
  }
  await block.scrollIntoViewIfNeeded({ timeout: 10_000 });
  return block;
}

async function capturePreview(block, type, output) {
  const ready = block.locator(PREVIEW[type]).filter({ visible: true }).first();
  const denied = block.getByText(PERMISSION).filter({ visible: true }).first();
  await ready.or(denied).first().waitFor({ timeout: 25_000 });
  if (await denied.isVisible()) {
    throw failure('ASSET_DENIED', '当前飞书账号没有此块的查看权限。');
  }
  if (type === 'image') {
    await ready.evaluate(async (image) => image.decode(), null, { timeout: 10_000 });
  }
  const target = type === 'sheet' || type === 'bitable' ? block : ready;
  const bytes = await target.screenshot({ type: 'png', timeout: 12_000 });
  await fs.writeFile(output, bytes, { flag: 'wx', mode: 0o600 });
  return 'visible-preview';
}

async function downloadExcel(page, block, output) {
  await block.hover({ timeout: 8_000 });
  const downloadControl = block.getByRole('button', { name: DOWNLOAD })
    .or(block.getByRole('link', { name: DOWNLOAD }))
    .or(block.getByTitle(DOWNLOAD)).filter({ visible: true });
  if (await downloadControl.count() !== 1 || !(await downloadControl.isVisible())) {
    throw failure('DOWNLOAD_UNAVAILABLE', '指定附件块未显示可用的下载入口。请确认页面允许下载该附件。');
  }
  const downloadPromise = page.waitForEvent('download', { timeout: 20_000 }).catch(() => null);
  await downloadControl.click({ timeout: 8_000 });
  const download = await downloadPromise;
  if (!download) throw failure('DOWNLOAD_UNAVAILABLE', '飞书页面没有返回附件下载。');
  if (path.extname(download.suggestedFilename()).toLowerCase() !== path.extname(output).toLowerCase()) {
    await download.cancel().catch(() => {});
    throw failure('WRONG_FILE_TYPE', '下载文件的扩展名与所选 Excel 类型不一致。');
  }

  const temporary = await fs.mkdtemp(path.join(os.tmpdir(), 'feishu-asset-'));
  try {
    const downloaded = path.join(temporary, 'workbook');
    await download.saveAs(downloaded);
    await fs.chmod(downloaded, 0o600);
    // Exclusive copy keeps concurrent commands from overwriting an output.
    await fs.copyFile(downloaded, output, fs.constants.COPYFILE_EXCL);
  } finally {
    await fs.rm(temporary, { recursive: true, force: true });
  }
  return 'download';
}

async function copyBitable(page, block, origin, coordinates, output) {
  const grid = block.locator('canvas[role="faster"]:visible');
  await grid.waitFor({ timeout: 25_000 });
  await grid.scrollIntoViewIfNeeded();
  const box = await grid.boundingBox();
  if (!box) throw failure('INVALID_RANGE', '指定表格当前不可见。');
  let expected;
  if (!coordinates) {
    expected = await grid.evaluate(visibleBitableRange);
    if (!expected) throw failure('AUTO_RANGE_UNAVAILABLE', '无法可靠识别可见数据单元格。请确认表格处于网格视图。');
    coordinates = expected.coordinates;
  }
  const [x1, y1, x2, y2] = coordinates;
  const points = [{ x: box.x + x1, y: box.y + y1 }, { x: box.x + x2, y: box.y + y2 }];
  const valid = [x1, x2].every(x => x < box.width) && [y1, y2].every(y => y < box.height) &&
    await block.evaluate((element, points) => points.every(({ x, y }) => element.contains(document.elementFromPoint(x, y))), points);
  if (!valid) throw failure('INVALID_RANGE', '拖选范围超出当前可操作的表格区域。');

  await page.context().grantPermissions(['clipboard-read', 'clipboard-write'], { origin });
  const original = await page.evaluateHandle(async () => {
    const items = await navigator.clipboard.read();
    return Promise.all(items.map(async (item) => Object.fromEntries(await Promise.all(
      item.types.map(async (type) => [type, await item.getType(type)]),
    ))));
  });
  const marker = `feishu-copy-${crypto.randomUUID()}`;
  let copied;
  let clipboardSet = false;
  try {
    await page.evaluate((text) => navigator.clipboard.writeText(text), marker);
    clipboardSet = true;
    await page.mouse.move(points[0].x, points[0].y);
    await page.mouse.down();
    try {
      await page.mouse.move(points[1].x, points[1].y, { steps: 12 });
    } finally {
      await page.mouse.up();
    }
    await page.keyboard.press(process.platform === 'darwin' ? 'Meta+c' : 'Control+c');
    await page.waitForFunction((value) => navigator.clipboard.readText().then(text => text !== value), marker,
      { timeout: 5000 }).catch(() => {});
    copied = await page.evaluate(async () => {
      const text = await navigator.clipboard.readText();
      const items = await navigator.clipboard.read();
      const item = items.find(value => value.types.includes('text/html'));
      const html = item ? await (await item.getType('text/html')).text() : '';
      const parsed = new DOMParser().parseFromString(html, 'text/html');
      const table = parsed.querySelector('table[data-byte-bitable]');
      return { text, columnsByRow: Array.from(table?.rows ?? [], row => row.cells.length) };
    });
    if (!copied.columnsByRow.some(Boolean) || copied.text === marker) {
      throw failure('COPY_UNAVAILABLE', '飞书未返回多维表格单元格。请检查复制权限和拖选位置。');
    }
    if (expected && (copied.columnsByRow.length !== expected.rowCount ||
        copied.columnsByRow.some(count => count !== expected.columnCount))) {
      const actualColumns = Math.max(...copied.columnsByRow);
      throw failure('AUTO_RANGE_MISMATCH', `可见网格识别为 ${expected.rowCount} 行 × ${expected.columnCount} 列，飞书复制返回 ${copied.columnsByRow.length} 行 × ${actualColumns} 列。请核对当前视图后重试。`);
    }
  } finally {
    try {
      if (clipboardSet) {
        await page.evaluate(async ({ items, marker, copiedText }) => {
          const current = await navigator.clipboard.readText();
          if (current !== marker && current !== copiedText) return;
          if (items.every(item => Object.values(item).every(blob => blob.size === 0))) {
            await navigator.clipboard.writeText('');
          } else {
            await navigator.clipboard.write(items.map(item => new ClipboardItem(item)));
          }
        }, { items: original, marker, copiedText: copied?.text });
      }
    } catch {
      throw failure('CLIPBOARD_RESTORE_FAILED', '无法恢复原剪贴板。请检查浏览器状态后重试。');
    } finally {
      await original.dispose();
    }
  }
  const temporary = output ? null : await fs.mkdtemp(path.join(os.tmpdir(), 'feishu-bitable-'));
  const outputPath = output || path.join(temporary, 'table.tsv');
  try {
    await fs.writeFile(outputPath, copied.text, { flag: 'wx', mode: 0o600 });
  } catch (error) {
    if (temporary) await fs.rmdir(temporary).catch(() => {});
    throw error;
  }
  return { mode: 'clipboard', path: outputPath, rowCount: copied.columnsByRow.length,
    columnCount: Math.max(...copied.columnsByRow) };
}

function documentUrl(rawUrl) {
  const url = new URL(rawUrl);
  if (url.protocol !== 'https:' || !/^[a-z0-9-]+\.feishu\.cn$/.test(url.host) || url.username || url.password ||
      url.search || url.hash || !/^\/(?:docx|wiki)\/[a-zA-Z0-9_-]+\/?$/.test(url.pathname)) {
    throw failure('INVALID_ARGUMENTS', '飞书文档地址无效。');
  }
  return url;
}

function assetJob({ blockId, type, output, cellRange, fallbackOutput }) {
  const isCopy = type === 'bitable' && (!output || /\.tsv$/i.test(output));
  const coordinates = cellRange?.match(CELL_RANGE)?.slice(1).map(Number);
  if (typeof blockId !== 'string' || !ID.test(blockId) || !(type === 'excel' || Object.hasOwn(PREVIEW, type)) ||
      (output ? !path.isAbsolute(output) : !isCopy) || (cellRange && (!isCopy || !coordinates)) ||
      !(type === 'excel' ? /\.xlsx?$/i.test(output) : isCopy || /\.png$/i.test(output)) ||
      (fallbackOutput && (!isCopy || !path.isAbsolute(fallbackOutput) || !/\.png$/i.test(fallbackOutput)))) {
    throw failure('INVALID_ARGUMENTS', '飞书资源块或输出路径无效。');
  }
  return { blockId, type, output, coordinates, isCopy, fallbackOutput };
}

async function requireNewOutput(output) {
  if (!output) return;
  try {
    await fs.lstat(output);
  } catch (error) {
    if (error.code === 'ENOENT') return;
    throw error;
  }
  throw failure('OUTPUT_EXISTS', '输出文件已存在。');
}

async function inDocument(url, acceptDownloads, run, read) {
  const open = headless => run({
    scope: { platform: 'feishu-doc', environment: url.hostname.split('.')[0], account: 'default' },
    loginUrl: url.href,
    allowedPageOrigins: [url.origin],
    launchOptions: { acceptDownloads, headless },
    authenticate: async ({ page, timeoutMs }) => {
      await waitForDocument(page, url, headless ? 25_000 : timeoutMs, headless);
      return read(page);
    },
  });
  try {
    return await open(true);
  } catch (error) {
    if (error.code !== 'BROWSER_LOGIN_REQUIRED') throw error;
    process.stderr.write('请在飞书专用 Chrome 窗口完成登录，命令随后会继续读取。\n');
    return open(false);
  }
}

async function readAssetOnPage(page, url, job, retryFromTop = false) {
  const { blockId, type, output, isCopy, coordinates, fallbackOutput } = job;
  try {
    const block = await findBlockOnPage(page, blockId, retryFromTop);
    if (isCopy) {
      try {
        const result = await copyBitable(page, block, url.origin, coordinates, output);
        return { blockId, type, ...result };
      } catch (error) {
        if (!fallbackOutput || (error instanceof AuthRuntimeError && !COPY_FALLBACK_CODES.has(error.code)) ||
            (error.code && !(error instanceof AuthRuntimeError))) throw error;
        const fallbackReason = error instanceof AuthRuntimeError ? error.code : 'COPY_UNAVAILABLE';
        const mode = await capturePreview(block, type, fallbackOutput);
        return { blockId, type, mode, path: fallbackOutput, fallbackReason };
      }
    }
    const mode = type === 'excel' ? await downloadExcel(page, block, output) : await capturePreview(block, type, output);
    return { blockId, type, mode, path: output };
  } catch (error) {
    if (error instanceof AuthRuntimeError) throw error;
    if (error.code === 'EEXIST') throw failure('OUTPUT_EXISTS', '输出文件已存在。');
    throw failure('ASSET_UNAVAILABLE', '指定块未完成渲染或无法保存，请检查页面是否可以查看此块。');
  }
}

export async function readBrowserAsset(rawUrl, blockId, type, output, cellRange, run = runBrowserAuth) {
  const url = documentUrl(rawUrl);
  const job = assetJob({ blockId, type, output, cellRange });
  await requireNewOutput(output);
  return inDocument(url, type === 'excel', run, page => readAssetOnPage(page, url, job));
}

export async function readBrowserAssets(rawUrl, requests, run = runBrowserAuth) {
  const url = documentUrl(rawUrl);
  const jobs = requests.map(assetJob);
  if (!jobs.length) return [];
  return inDocument(url, jobs.some(job => job.type === 'excel'), run, async page => {
    const results = [];
    for (const job of jobs) {
      try {
        await requireNewOutput(job.output);
        results.push(await readAssetOnPage(page, url, job, true));
      } catch (error) {
        results.push({ blockId: job.blockId, type: job.type,
          error: { code: error.code || 'ASSET_UNAVAILABLE', message: safeErrorMessage(error) } });
      }
    }
    return results;
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    const [modeOrUrl, ...args] = process.argv.slice(2);
    const result = modeOrUrl === '--batch'
      ? await readBrowserAssets(args[0], JSON.parse(await readText(process.stdin)))
      : await readBrowserAsset(modeOrUrl, ...args);
    console.log(JSON.stringify(result));
  } catch (error) {
    console.error(`❌ ${safeErrorMessage(error)}`);
    process.exitCode = 1;
  }
}
