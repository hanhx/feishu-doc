import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { chromium } from 'playwright-core';
import { importDrawio } from '../scripts/import_drawio.mjs';

const url = 'https://fixture.feishu.cn/docx/sample';
const body = `<!doctype html><style>canvas{border:1px solid black} .zone-container{min-height:24px} .menu-item-main-box{width:max-content}</style>
  <main class="docx-page-block">
    <div class="page-block-children"><div class="root-render-unit-container">
    <div class="render-unit-wrapper">
    <div class="block docx-heading1-block" data-record-id="section">
      <div class="zone-container" contenteditable="true"><div class="ace-line">架构方案</div></div>
    </div>
    </div><div class="render-unit-wrapper">
    <div class="block docx-text-block" data-record-id="next">
      <div class="zone-container" contenteditable="true"><div class="ace-line">原段落</div></div>
    </div>
    </div><div class="render-unit-wrapper">
    <div class="block docx-whiteboard-block" data-record-id="existing"><canvas></canvas></div>
    </div><div class="render-unit-wrapper">
    <div class="block docx-text-block isEmpty" data-record-id="end">
      <div class="zone-container" contenteditable="true"><div class="ace-line"><br></div></div>
    </div>
    </div></div></div>
  </main>
  <div class="menu-item-main-box" hidden><button>画板</button></div>
  <div class="screenshot-container" hidden>
    <span class="_topbar-title-example">画板</span>
    <div class="_user-tool-container-example">
      <button id="more"><svg data-icon="MoreBoldOutlined" width="10" height="10"></svg></button>
    </div>
    <button id="close">退出</button>
  </div>
  <script>
    const menu = document.querySelector('.menu-item-main-box');
    const editor = document.querySelector('.screenshot-container');
    const section = document.querySelector('[data-record-id="section"]');
    // Simulate a section well below the fold and a canvas whose first open
    // gesture is ignored while its handlers initialize.
    section.style.marginTop = '2200px';
    const wrap = block => {
      const wrapper = document.createElement('div');
      wrapper.className = 'render-unit-wrapper';
      wrapper.append(block);
      return wrapper;
    };
    const imported = sessionStorage.getItem('imported');
    const makeBoard = () => {
      const board = document.createElement('div');
      board.className = 'block docx-whiteboard-block';
      board.dataset.recordId = 'newboard';
      board.innerHTML = '<div class="whiteboard-canvas-container"><canvas width="260" height="100"></canvas></div>';
      return board;
    };
    const draw = board => {
      const ctx = board.querySelector('canvas').getContext('2d');
      ctx.fillStyle = '#6c8ebf';
      ctx.fillRect(10, 15, 90, 40);
      ctx.fillRect(140, 15, 90, 40);
      ctx.fillStyle = '#000';
      ctx.fillRect(100, 35, 40, 2);
    };
    if (imported) {
      const board = makeBoard();
      const paragraph = document.querySelector('[data-record-id="end"]');
      if (imported === 'end') paragraph.replaceWith(board);
      else section.parentElement.after(wrap(board));
      setTimeout(() => draw(board), 350);
    }
    section.addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      const paragraph = document.createElement('div');
      paragraph.className = 'block docx-text-block isEmpty';
      paragraph.dataset.recordId = 'newparagraph';
      paragraph.innerHTML = '<div class="zone-container" contenteditable="true"><div class="ace-line"><br></div></div>';
      section.parentElement.after(wrap(paragraph));
      paragraph.querySelector('.zone-container').focus();
      getSelection().collapse(paragraph.querySelector('.ace-line'), 0);
    });
    document.addEventListener('input', event => {
      if (event.target.innerText.trim() === '/hb') menu.hidden = false;
    });
    menu.querySelector('button').addEventListener('click', () => {
      const insertionBlock = [...document.querySelectorAll('.docx-text-block')].find(
        block => block.innerText.trim() === '/hb');
      const position = insertionBlock.dataset.recordId === 'end' ? 'end' : 'section';
      const board = makeBoard();
      insertionBlock.replaceWith(board);
      menu.hidden = true;
      const canvas = board.querySelector('.whiteboard-canvas-container');
      const open = () => {
        canvas.classList.add('whiteboard-block_container__selected');
        editor.hidden = false;
      };
      if (position === 'section') {
        sessionStorage.setItem('created', String(Number(sessionStorage.getItem('created') || 0) + 1));
        canvas.addEventListener('dblclick', () => {
          const attempts = Number(sessionStorage.getItem('opens') || 0) + 1;
          sessionStorage.setItem('opens', String(attempts));
          if (attempts === 2) {
            open();
            return;
          }
          const wrapper = board.parentElement;
          board.remove();
          const restore = () => {
            if (section.getBoundingClientRect().top < 0 || section.getBoundingClientRect().bottom > innerHeight) return;
            wrapper.append(board);
            window.removeEventListener('scroll', restore);
          };
          window.scrollTo(0, 0);
          requestAnimationFrame(() => window.addEventListener('scroll', restore));
        });
      } else {
        open();
      }
      document.querySelector('#more').addEventListener('click', () => {
        const chooser = document.createElement('input');
        chooser.type = 'file';
        chooser.accept = '.drawio';
        chooser.hidden = true;
        const dialog = document.createElement('section');
        dialog.setAttribute('role', 'dialog');
        const upload = document.createElement('div');
        upload.className = 'ud__upload';
        upload.textContent = '导入';
        upload.append(chooser);
        dialog.append(upload);
        editor.append(dialog);
        chooser.addEventListener('change', async () => {
          const file = chooser.files[0];
          if (file.name !== 'sample.drawio' || !(await file.text()).includes('<mxfile')) return;
          draw(board);
          sessionStorage.setItem('imported', position);
          const toast = document.createElement('div');
          toast.textContent = '图形已导入';
          editor.append(toast);
        });
      });
      document.querySelector('#close').addEventListener('click', () => { editor.hidden = true; });
    });
  </script>`;

test('imports into a new board at the requested position and captures it after reload', async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'feishu-drawio-test-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  t.after(() => browser.close());
  const source = path.join(directory, 'sample.drawio');
  await fs.writeFile(source, '<mxfile><diagram/></mxfile>');
  for (const request of [
    { anchor: 'section', heading: '架构方案', nextBlock: 'next', reuseEmpty: false },
    { anchor: 'end', nextBlock: '', reuseEmpty: true },
  ]) {
    const page = await browser.newPage();
    page.on('pageerror', error => t.diagnostic(error.message));
    try {
      await page.route(url, route => route.fulfill({ contentType: 'text/html; charset=utf-8', body }));
      const run = async ({ scope, loginUrl, allowedPageOrigins, authenticate }) => {
        assert.deepEqual(scope, { platform: 'feishu-doc', environment: 'fixture', account: 'default' });
        assert.deepEqual(allowedPageOrigins, [new URL(url).origin]);
        await page.goto(loginUrl);
        return authenticate({ page, timeoutMs: 2000 });
      };
      const output = path.join(directory, request.anchor + '.png');
      const options = { ...request, existingBoards: ['existing'] };
      const result = await importDrawio(url, source, output, options, run).catch(async error => {
        const order = await page.locator('.block[data-record-id]').evaluateAll(
          nodes => nodes.map(node => [node.dataset.recordId, node.innerText.trim()]));
        throw new Error(`${request.anchor}: ${JSON.stringify(order)} ${error.message}`, { cause: error });
      });
      assert.deepEqual(result, { status: 'imported', blockId: 'newboard', path: output });
      const expected = request.reuseEmpty
        ? ['section', 'next', 'existing', 'newboard']
        : ['section', 'newboard', 'next', 'existing', 'end'];
      assert.deepEqual(await page.locator('.block[data-record-id]').evaluateAll(
        nodes => nodes.map(node => node.dataset.recordId)), expected);
      if (request.anchor === 'section') {
        assert.deepEqual(await page.evaluate(() => [
          sessionStorage.getItem('created'), sessionStorage.getItem('opens'),
        ]), ['1', '2']);
      }
      const pixels = await page.locator('[data-record-id=newboard] canvas').evaluate(canvas =>
        [...canvas.getContext('2d').getImageData(15, 25, 1, 1).data]);
      assert.deepEqual(pixels, [108, 142, 191, 255]);
      assert.deepEqual([...await fs.readFile(output)].slice(0, 8), [137, 80, 78, 71, 13, 10, 26, 10]);
      await assert.rejects(importDrawio(url, source, output, options, run), { code: 'OUTPUT_EXISTS' });
    } finally {
      await page.close();
    }
  }
  const page = await browser.newPage();
  try {
    // The board exists but its wrapper is absent from the rendered block order.
    const missingPosition = body.replace('insertionBlock.replaceWith(board);',
      "insertionBlock.replaceWith(board); board.parentElement.classList.remove('render-unit-wrapper');");
    await page.route(url, route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: missingPosition }));
    const output = path.join(directory, 'pending.png');
    const result = await importDrawio(url, source, output, {
      anchor: 'section', heading: '架构方案', nextBlock: 'next', existingBoards: ['existing'],
    }, async ({ loginUrl, authenticate }) => {
      await page.goto(loginUrl);
      return authenticate({ page, timeoutMs: 2000 });
    });
    assert.deepEqual(result, { status: 'locate_saved_board', paragraphId: 'newparagraph' });
    assert.equal(await page.locator('[data-record-id="newboard"]').count(), 1);
    assert.deepEqual(await page.evaluate(() => [
      sessionStorage.getItem('created'), sessionStorage.getItem('imported'),
    ]), ['1', null]);
    await assert.rejects(fs.access(output), { code: 'ENOENT' });
  } finally {
    await page.close();
  }
});

test('replaces only the target board and resumes an empty board without clearing it', async (t) => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'feishu-drawio-replace-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  t.after(() => browser.close());
  const source = path.join(directory, 'replace.drawio');
  const bytes = '<mxGraphModel><root><mxCell vertex="1"/></root></mxGraphModel>';
  await fs.writeFile(source, bytes);
  const request = { anchor: 'board', replace: true, sourceDigest: createHash('sha256').update(bytes).digest('hex') };
  const page = await browser.newPage();
  let reject = true;
  await page.route(url, route => route.fulfill({ contentType: 'text/html; charset=utf-8', body: `
    <style>[hidden]{display:none!important}</style>
    <main class="docx-page-block">
      <div class="docx-whiteboard-block" data-record-id="other">保留</div>
      <div class="docx-whiteboard-block" data-record-id="board">
        <div class="whiteboard-canvas-container" style="width:300px;height:200px"><canvas width="300" height="200"></canvas></div>
      </div>
    </main>
    <div class="screenshot-container" hidden>
      <span class="_topbar-title-fixture">画板</span>
      <div class="_user-tool-container-fixture">
        <button id="menu"><svg data-icon="MoreBoldOutlined" width="10" height="10"></svg></button>
      </div>
      <div role="dialog" hidden><div class="ud__upload">导入<input type="file" accept=".drawio" hidden></div></div>
      <div class="whiteboard-canvas-container">
        <div class="whiteboad-x-canvasInputEventTarget" tabindex="0"><canvas width="300" height="200"></canvas></div>
      </div>
      <button id="selected" hidden><svg data-icon="FilterOutlined" width="10" height="10"></svg></button>
      <button id="undo"><svg data-icon="UndoOutlined" width="10" height="10"></svg></button>
      <button id="close">退出</button><span id="toast"></span>
    </div>
    <script>
      const board = document.querySelector('[data-record-id="board"]');
      const inline = board.querySelector('canvas');
      const editor = document.querySelector('.screenshot-container');
      const surface = editor.querySelector('[tabindex]');
      const dialog = editor.querySelector('[role=dialog]');
      const selected = document.querySelector('#selected');
      const render = (canvas, color) => {
        const context = canvas.getContext('2d');
        context.clearRect(0, 0, 300, 200);
        if (color) {
          context.fillStyle = color;
          context.fillRect(35, 35, 140, 100);
        }
      };
      const previous = sessionStorage.getItem('color') ?? '#335577';
      render(inline, previous);
      inline.ondblclick = () => {
        inline.parentElement.classList.add('whiteboard-block_container__selected');
        render(surface.querySelector('canvas'), previous);
        editor.hidden = false;
      };
      document.querySelector('#menu').onclick = () => { dialog.hidden = !dialog.hidden; };
      surface.onkeydown = event => {
        if ((event.ctrlKey || event.metaKey) && event.key === 'a') {
          event.preventDefault();
          selected.hidden = false;
        }
        if (event.key === 'Delete' && !selected.hidden) {
          render(surface.querySelector('canvas'), '');
          selected.hidden = true;
          sessionStorage.setItem('deleted', 'true');
        }
      };
      document.querySelector('#undo').onclick = () => {
        render(surface.querySelector('canvas'), previous);
        sessionStorage.setItem('restored', 'true');
      };
      dialog.querySelector('input').onchange = () => {
        dialog.hidden = true;
        if (${reject}) {
          document.querySelector('#toast').textContent = '导入失败';
          return;
        }
        sessionStorage.setItem('color', '#993311');
        document.querySelector('#toast').textContent = '图形已导入';
      };
      document.querySelector('#close').onclick = () => { editor.hidden = true; };
    </script>` }));
  const run = async ({ loginUrl, authenticate }) => {
    await page.goto(loginUrl);
    return authenticate({ page, timeoutMs: 2000 });
  };
  const output = path.join(directory, 'updated.png');
  await assert.rejects(importDrawio(url, source, output, request, run), /已撤销删除/);
  assert.equal(await page.evaluate(() => sessionStorage.getItem('restored')), 'true');
  await assert.rejects(fs.access(output), { code: 'ENOENT' });

  reject = false;
  assert.deepEqual(await importDrawio(url, source, output, request, run),
    { status: 'replaced', blockId: 'board', path: output });
  assert.deepEqual(await page.locator('[data-record-id]').evaluateAll(
    nodes => nodes.map(node => node.dataset.recordId)), ['other', 'board']);
  const pixels = await page.locator('[data-record-id="board"] canvas').evaluate(canvas =>
    [...canvas.getContext('2d').getImageData(40, 40, 1, 1).data]);
  assert.deepEqual(pixels, [153, 51, 17, 255]);

  const resume = { anchor: 'board', resume: true, sourceDigest: request.sourceDigest };
  const recovered = path.join(directory, 'recovered.png');
  await page.evaluate(() => sessionStorage.removeItem('deleted'));
  await assert.rejects(importDrawio(url, source, recovered, resume, run), /未确认画板为空/);
  assert.equal(await page.evaluate(() => sessionStorage.getItem('deleted')), null);
  await assert.rejects(fs.access(recovered), { code: 'ENOENT' });
  await page.evaluate(() => sessionStorage.setItem('color', ''));
  assert.deepEqual(await importDrawio(url, source, recovered, resume, run),
    { status: 'imported', blockId: 'board', path: recovered });
  assert.equal(await page.evaluate(() => sessionStorage.getItem('deleted')), null);
});
