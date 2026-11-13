import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { AuthRuntimeError, runBrowserAuth, safeErrorMessage } from './browser_auth.mjs';
import { locateBlock } from './read_browser_asset.mjs';

const ID = /^[a-zA-Z0-9_-]+$/;
const BOARD = '.docx-whiteboard-block[data-record-id]';
const EDITOR = 'div.screenshot-container:has([class*="_topbar-title"]:visible)';
const CANVAS = '.whiteboard-canvas-container:has(canvas:visible)';
const SURFACE = '.whiteboad-x-canvasInputEventTarget[tabindex="0"]:visible';
const SELECTED_BOARD = '.whiteboard-block_container__selected';
const BOARD_MORE = '[class*="_user-tool-container-"] button:has(svg[data-icon="MoreBoldOutlined"]):visible';
const EDITABLE = '.zone-container[contenteditable=true]';
const ROOT_BLOCKS = '.page-block-children > .root-render-unit-container > .render-unit-wrapper > .block[data-record-id]';
const TIMEOUT = 10_000;
const OPEN_BOARD_ATTEMPTS = 2;

function fail(code, message) {
  throw new AuthRuntimeError(code, message);
}

function positionInDocument(element, selector) {
  const blocks = [...element.closest('.docx-page-block').querySelectorAll(selector)];
  const index = blocks.indexOf(element);
  return index < 0 ? null : [blocks[index - 1]?.dataset.recordId ?? '', blocks[index + 1]?.dataset.recordId ?? ''];
}

async function assertDocument(page, url) {
  const current = new URL(page.url());
  if (current.origin !== url.origin || current.pathname.replace(/\/$/, '') !== url.pathname.replace(/\/$/, '')) {
    fail('DOCUMENT_CHANGED', '浏览器已离开指定飞书文档。');
  }
}

async function focusEnd(block, requireEmpty) {
  const editor = block.locator(`${EDITABLE}:visible`);
  await editor.click({ position: { x: 10, y: 8 }, timeout: TIMEOUT });
  const valid = await editor.evaluate((element, requireEmpty) => {
    if (requireEmpty && element.innerText.replace(/\u200b/g, '').trim()) return false;
    const selection = getSelection();
    if (!selection?.anchorNode || !element.contains(selection.anchorNode)) return false;
    const line = element.querySelector('.ace-line:last-child') ?? element;
    const range = document.createRange();
    range.selectNodeContents(line);
    range.collapse(false);
    selection.removeAllRanges();
    selection.addRange(range);
    return selection.isCollapsed && element.contains(selection.anchorNode);
  }, requireEmpty);
  if (!valid) fail('EDIT_POSITION_UNAVAILABLE', '无法把光标定位到指定块的末尾。');
}

async function insertionParagraph(page, anchor, request) {
  const position = await anchor.evaluate(positionInDocument, ROOT_BLOCKS);
  if (!position || position[1] !== request.nextBlock) {
    fail('POSITION_CHANGED', '插入位置附近的文档内容已变化，请重新核对。');
  }
  if (request.reuseEmpty) {
    return { paragraph: anchor, previous: position[0] };
  }

  // A heading becomes a following text block when Enter is pressed at its end.
  // A text block supports the same action when appending to the document.
  const editor = anchor.locator(`${EDITABLE}:visible`);
  if (!(await editor.count())) {
    fail('EDIT_POSITION_UNAVAILABLE', '目标块没有可用的文本编辑入口。追加时可在文末保留一个空段落。');
  }
  await focusEnd(anchor, false);
  if (request.heading && (await editor.innerText()).replace(/\u200b/g, '').trim() !== request.heading) {
    fail('POSITION_CHANGED', '页面章节标题与目标标题不一致，请重新核对。');
  }
  await page.keyboard.press('Enter');
  const newId = await page.waitForFunction(({ anchor, nextBlock }) => {
    const selection = getSelection();
    const node = selection?.anchorNode;
    const element = node instanceof Element ? node : node?.parentElement;
    const block = element?.closest('.docx-text-block[data-record-id]');
    if (!block || block.dataset.recordId === anchor || block.dataset.recordId === nextBlock) return false;
    const editor = block.querySelector('.zone-container[contenteditable=true]');
    if (!editor || editor.innerText.replace(/\u200b/g, '').trim()) return false;
    return block.dataset.recordId;
  }, request, { timeout: TIMEOUT }).then(handle => handle.jsonValue());
  if (!ID.test(newId)) fail('EDIT_POSITION_UNAVAILABLE', '新段落未返回有效块 ID。');
  const target = page.locator(`.docx-page-block .docx-text-block[data-record-id="${newId}"]`);
  const inserted = await target.evaluate(positionInDocument, ROOT_BLOCKS);
  if (!inserted || inserted[0] !== request.anchor || inserted[1] !== request.nextBlock) {
    fail('POSITION_CHANGED', '新段落位置与指定章节不一致。');
  }
  return { paragraph: target, previous: inserted[0] };
}

async function addBoard(page, url, anchor, request) {
  const { paragraph, previous } = await insertionParagraph(page, anchor, request);
  const paragraphId = await paragraph.getAttribute('data-record-id');
  await focusEnd(paragraph, true);
  await assertDocument(page, url);
  await page.keyboard.insertText('/hb');
  const menu = page.locator('.menu-item-main-box:visible')
    .filter({ has: page.getByText(/^(画板|Whiteboard)$/) });
  try {
    await menu.click({ timeout: TIMEOUT });
  } catch {
    // Only erase our exact command from the verified empty insertion line.
    const editor = paragraph.locator(EDITABLE);
    const text = await editor.innerText({ timeout: 1000 }).catch(() => '');
    if (text.replace(/\u200b/g, '').trim() === '/hb') {
      await page.keyboard.press('Escape');
      await focusEnd(paragraph, false);
      for (let i = 0; i < 3; i += 1) await page.keyboard.press('Backspace');
    }
    fail('BOARD_MENU_UNAVAILABLE', '编辑器未能通过画板菜单插入画板。');
  }

  const newBoardId = await page.waitForFunction(({ rootBlocks, selector, existingBoards, previous, nextBlock, paragraphId }) => {
    const root = document.querySelector('.docx-page-block');
    const blocks = [...(root?.querySelectorAll(rootBlocks) ?? [])];
    const matches = blocks.filter((element, index) => {
      if (!element.matches(selector) || existingBoards.includes(element.dataset.recordId)) return false;
      let before = blocks[index - 1]?.dataset.recordId ?? '';
      let after = blocks[index + 1]?.dataset.recordId ?? '';
      if (before === paragraphId) before = blocks[index - 2]?.dataset.recordId ?? '';
      if (after === paragraphId) after = blocks[index + 2]?.dataset.recordId ?? '';
      return before === previous && (after === nextBlock || (!nextBlock && blocks[index + 1]?.matches('.isEmpty')));
    });
    return matches.length === 1 && matches[0].dataset.recordId;
  }, { rootBlocks: ROOT_BLOCKS, selector: BOARD, existingBoards: request.existingBoards, previous,
    nextBlock: request.nextBlock, paragraphId }, { timeout: TIMEOUT }).then(handle => handle.jsonValue(), error => {
    if (error.name !== 'TimeoutError') throw error;
    return null;
  });
  if (!newBoardId) return { status: 'locate_saved_board', paragraphId };
  if (!ID.test(newBoardId)) fail('BOARD_UNAVAILABLE', '新增画板未返回有效块 ID。');
  return newBoardId;
}

async function openBoard(page, url, board, anchor, resume) {
  const editor = page.locator(EDITOR);
  const selected = board.locator(SELECTED_BOARD);
  const canvas = board.locator(CANVAS).first();
  let failureMessage;

  // The new block can exist while Feishu is still mounting or scrolling its
  // canvas. Retry only the interaction with this block, before any upload.
  for (let attempt = 0; attempt < OPEN_BOARD_ATTEMPTS; attempt += 1) {
    await assertDocument(page, url);
    if (!(await editor.count())) {
      failureMessage = '未等到目标画板的画布渲染。';
      try {
        const scrollTarget = await board.count() ? board : anchor;
        await scrollTarget.evaluate(element => {
          element.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
        }, undefined, { timeout: TIMEOUT });
        const box = await canvas.boundingBox({ timeout: TIMEOUT });
        if (!box) continue;
        await assertDocument(page, url);
        if (!(await editor.count())) {
          failureMessage = '目标画布未能接收双击。';
          // A saved empty board may only open from its visible placeholder.
          const position = resume || attempt > 0
            ? { x: box.width / 2, y: box.height / 2 }
            : { x: Math.min(box.width / 3, 100), y: Math.min(box.height / 3, 100) };
          await canvas.dblclick({ position, timeout: TIMEOUT });
          failureMessage = '双击后未等到画板编辑器出现。';
        }
        await editor.waitFor({ timeout: TIMEOUT });
      } catch (error) {
        if (error.name !== 'TimeoutError') throw error;
        if (!(await editor.count())) continue;
      }
    }
    // An automatically opened editor must also belong to the requested block.
    await selected.waitFor({ state: 'attached', timeout: TIMEOUT }).catch(error => {
      if (error.name !== 'TimeoutError') throw error;
      fail('BOARD_SELECTION_CHANGED', '打开的画板编辑器未关联到目标画板。');
    });
    await assertBoard(page, url, board, editor);
    return editor;
  }
  fail('BOARD_OPEN_TIMEOUT', failureMessage);
}

async function openDrawioChooser(page, editor) {
  await editor.locator(BOARD_MORE).click({ timeout: TIMEOUT });
  const chooser = page.locator('[role=dialog]:visible .ud__upload input[type=file][accept=".drawio"]');
  await chooser.waitFor({ state: 'attached', timeout: TIMEOUT });
  return chooser;
}

async function assertBoard(page, url, board, editor) {
  await assertDocument(page, url);
  if (await board.locator(SELECTED_BOARD).count() !== 1 || await editor.count() !== 1) {
    fail('BOARD_SELECTION_CHANGED', '画板选择已变化。');
  }
}

async function uploadDrawio(page, url, board, editor, source, beforeSubmit = () => {}) {
  const chooser = await openDrawioChooser(page, editor);
  await assertBoard(page, url, board, editor);
  beforeSubmit();
  await chooser.setInputFiles(source, { timeout: TIMEOUT });
  const success = page.getByText(/^(图形已导入|Diagrams? imported(?: successfully)?)[。.]?$/).filter({ visible: true });
  const rejection = page.getByText(/^(图形导入失败|导入失败|Diagram import failed|Import failed)[。.]?$/).filter({ visible: true });
  await success.or(rejection).first().waitFor({ timeout: 25_000 });
  if (!(await success.count()) && await rejection.count()) {
    fail('DRAWIO_IMPORT_REJECTED', '飞书明确返回导入失败。');
  }
  if (!(await success.count()) || await rejection.count()) {
    fail('DRAWIO_IMPORT_UNVERIFIED', '飞书的导入结果无法确认。');
  }
  await editor.getByRole('button', { name: /^(退出|Exit|Close)$/ }).click({ timeout: TIMEOUT });
  await editor.waitFor({ state: 'hidden', timeout: TIMEOUT });
}

// Read rendered canvas layers. The watermark sits in the DOM above them.
function canvasState({ element, empty }) {
  let readable = 0;
  let unreadable = 0;
  let marks = 0;
  for (const canvas of element.querySelectorAll('canvas')) {
    if (!canvas.width || !canvas.height) continue;
    const sample = document.createElement('canvas');
    sample.width = 192;
    sample.height = 96;
    const context = sample.getContext('2d', { willReadFrequently: true });
    context.drawImage(canvas, 0, 0, sample.width, sample.height);
    try {
      const { data } = context.getImageData(0, 0, sample.width, sample.height);
      readable += 1;
      for (let index = 0; index < data.length; index += 4) {
        if (data[index + 3] > 90 && Math.min(data[index], data[index + 1], data[index + 2]) < 175) {
          marks += 1;
        }
      }
    } catch {
      unreadable += 1;
    }
  }
  return empty ? readable > 0 && unreadable === 0 && marks === 0 : marks > 6;
}

async function waitForCanvas(page, container, empty) {
  const element = await container.elementHandle({ timeout: TIMEOUT });
  try {
    const result = await page.waitForFunction(canvasState, { element, empty }, { timeout: TIMEOUT, polling: 100 });
    await result.dispose();
  } finally {
    await element.dispose();
  }
}

async function clearBoard(page, url, board, editor) {
  // Verify that Feishu exposes the import control before touching existing shapes.
  const chooser = await openDrawioChooser(page, editor);
  await editor.locator(BOARD_MORE).click({ timeout: TIMEOUT });
  await chooser.waitFor({ state: 'hidden', timeout: TIMEOUT });
  const surface = editor.locator(SURFACE);
  const canvas = editor.locator(CANVAS).filter({ has: page.locator(SURFACE) });
  const undo = editor.locator('button:has(svg[data-icon="UndoOutlined"]):visible');
  await waitForCanvas(page, canvas, false).catch(() => {
    fail('BOARD_CONTENT_UNVERIFIED', '未确认画板上有可见旧图，未执行删除。');
  });
  await surface.focus({ timeout: TIMEOUT });
  if (!(await surface.evaluate(el => document.activeElement === el))) {
    fail('BOARD_FOCUS_UNAVAILABLE', '画板没有获取键盘焦点。');
  }
  await page.keyboard.press('ControlOrMeta+a');
  await editor.locator('button:has(svg[data-icon="FilterOutlined"]):visible').waitFor({ timeout: TIMEOUT });
  await assertBoard(page, url, board, editor);
  if (!(await surface.evaluate(el => document.activeElement === el))) {
    fail('BOARD_FOCUS_UNAVAILABLE', '删除前画板已失去键盘焦点。');
  }
  await page.keyboard.press('Delete');
  try {
    await waitForCanvas(page, canvas, true);
  } catch {
    fail('BOARD_CLEAR_UNVERIFIED', '已提交删除，但未能确认画板已清空。请在该画板中检查并按需撤销。');
  }
  return async () => {
    await assertBoard(page, url, board, editor);
    const element = await canvas.elementHandle({ timeout: TIMEOUT });
    try {
      if (!(await page.evaluate(canvasState, { element, empty: true }))) return false;
    } finally {
      await element.dispose();
    }
    await undo.click({ timeout: TIMEOUT });
    await waitForCanvas(page, canvas, false);
    return true;
  };
}

export async function captureSavedBoard(page, board, output) {
  const preview = board.locator(CANVAS).first();
  await preview.waitFor({ timeout: TIMEOUT });
  await waitForCanvas(page, preview, false).catch(() => {
    fail('BOARD_PREVIEW_UNVERIFIED', '已收到导入成功提示，回读画板时未能确认图形已渲染。');
  });
  const screenshot = await preview.screenshot({ type: 'png', timeout: TIMEOUT });
  await fs.writeFile(output, screenshot, { flag: 'wx', mode: 0o600 });
}

export async function importDrawio(rawUrl, source, output, request, run = runBrowserAuth) {
  const url = new URL(rawUrl);
  const existingBoard = request.replace || request.resume;
  if (url.protocol !== 'https:' || !/^[a-z0-9-]+\.feishu\.cn$/.test(url.host) || url.username || url.password ||
      url.search || url.hash || !/^\/(?:docx|wiki)\/[a-zA-Z0-9_-]+\/?$/.test(url.pathname) ||
      !path.isAbsolute(source) || !/\.drawio$/i.test(source) || !path.isAbsolute(output) ||
      !/\.png$/i.test(output) || !ID.test(request.anchor) ||
      (request.replace && request.resume) ||
      (existingBoard ? !/^[a-f0-9]{64}$/.test(request.sourceDigest) :
        ((request.nextBlock && !ID.test(request.nextBlock)) || !Array.isArray(request.existingBoards)))) {
    fail('INVALID_ARGUMENTS', '飞书文档、插入位置、源文件或截图路径无效。');
  }
  try {
    await fs.lstat(output);
    fail('OUTPUT_EXISTS', '截图文件已存在。');
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  const file = { name: path.basename(source), mimeType: 'application/xml', buffer: await fs.readFile(source) };
  if (request.sourceDigest && createHash('sha256').update(file.buffer).digest('hex') !== request.sourceDigest) {
    fail('SOURCE_CHANGED', '源文件在校验后发生变化，请重新检查。');
  }
  return run({
    scope: { platform: 'feishu-doc', environment: url.hostname.split('.')[0], account: 'default' },
    loginUrl: url.href,
    allowedPageOrigins: [url.origin],
    authenticate: async ({ page, timeoutMs }) => {
      let stage = '定位章节';
      let blockId;
      try {
        const anchor = await locateBlock(page, url, request.anchor, timeoutMs);
        await assertDocument(page, url);
        if (await page.locator(EDITOR).count()) {
          fail('BOARD_ALREADY_OPEN', '请退出当前打开的画板后再导入。');
        }
        if (existingBoard) {
          if (!(await anchor.evaluate((element, selector) => element.matches(selector), BOARD))) {
            fail('BOARD_UNAVAILABLE', '目标块不是画板。');
          }
          blockId = request.anchor;
        } else {
          stage = '新建画板';
          const created = await addBoard(page, url, anchor, request);
          if (typeof created !== 'string') return created;
          blockId = created;
        }
        const board = page.locator(`${BOARD}[data-record-id="${blockId}"]`);
        stage = '打开画板';
        const editor = await openBoard(page, url, board, anchor, request.resume);
        let restore;
        if (request.replace) {
          stage = '清空旧图';
          restore = await clearBoard(page, url, board, editor);
        } else if (request.resume) {
          stage = '核对空白画板';
          const surface = editor.locator(SURFACE);
          const canvas = editor.locator(CANVAS).filter({ has: page.locator(SURFACE) });
          await waitForCanvas(page, canvas, true).catch(error => {
            if (error.name !== 'TimeoutError') throw error;
            fail('BOARD_NOT_EMPTY', '未确认画板为空，未提交导入。');
          });
          await assertBoard(page, url, board, editor);
        }
        stage = '导入 draw.io';
        let submitted = false;
        try {
          await uploadDrawio(page, url, board, editor, file, () => { submitted = true; });
        } catch (error) {
          if (restore && (!submitted || error.code === 'DRAWIO_IMPORT_REJECTED')) {
            const restored = await restore().catch(() => false);
            if (restored) fail('BOARD_RESTORED', '新版尚未提交，已撤销删除并看到画板恢复。');
          }
          throw error;
        }
        stage = '截图核对';
        await page.reload({ waitUntil: 'domcontentloaded', timeout: TIMEOUT });
        const savedBoard = await locateBlock(page, url, blockId, timeoutMs);
        await captureSavedBoard(page, savedBoard, output);
        return { status: request.replace ? 'replaced' : 'imported', blockId, path: output };
      } catch (error) {
        if (stage === '定位章节' && error instanceof AuthRuntimeError) throw error;
        const location = blockId ? `画板 ${blockId}。` : '';
        const detail = error instanceof AuthRuntimeError ? error.safeMessage : '';
        fail('IMPORT_INCOMPLETE', `${stage}阶段未完成。${location}${detail}请先核对文档当前状态再决定是否重试。`);
      }
    },
  });
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    const input = [];
    for await (const chunk of process.stdin) input.push(chunk);
    const result = await importDrawio(...process.argv.slice(2), JSON.parse(Buffer.concat(input)));
    console.log(JSON.stringify(result));
  } catch (error) {
    console.error(`❌ ${safeErrorMessage(error)}`);
    process.exitCode = 1;
  }
}
