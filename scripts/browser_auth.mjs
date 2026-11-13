import crypto from 'node:crypto';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

const LOCK_FILE = '.browser-auth.lock';
const DEFAULT_TIMEOUT_MS = 5 * 60_000;
const SCOPE_SEGMENT = /^[a-z0-9][a-z0-9._-]*$/;

export class AuthRuntimeError extends Error {
  constructor(code, safeMessage) {
    super(safeMessage);
    this.name = 'AuthRuntimeError';
    this.code = code;
    this.safeMessage = safeMessage;
  }
}

export function safeErrorMessage(error) {
  if (error instanceof AuthRuntimeError) {
    return error.safeMessage;
  }
  return '浏览器认证流程发生未预期错误；未输出底层异常以避免泄露认证信息。';
}

export function resolveStateRoot({
  platform = process.platform,
  environment = process.env,
  homeDirectory = os.homedir(),
} = {}) {
  if (platform === 'darwin') {
    return path.join(homeDirectory, 'Library', 'Application Support', 'pe-browser-auth');
  }
  if (platform === 'linux') {
    const xdgStateHome = environment.XDG_STATE_HOME?.trim();
    return path.join(xdgStateHome || path.join(homeDirectory, '.local', 'state'), 'pe-browser-auth');
  }
  throw new AuthRuntimeError('UNSUPPORTED_PLATFORM', 'Browser Auth Runtime 首版仅支持 macOS 与 Linux。');
}

function validateScope(scope) {
  const segments = [scope?.platform, scope?.environment, scope?.account];
  if (segments.some((segment) => !SCOPE_SEGMENT.test(segment || ''))) {
    throw new AuthRuntimeError('INVALID_SCOPE', 'Browser Profile scope 非法。');
  }
  return segments;
}

export function resolveProfileDirectory(scope, options = {}) {
  const segments = validateScope(scope);
  const stateRoot = options.stateRoot || resolveStateRoot(options);
  return path.join(stateRoot, 'profiles', ...segments);
}

export function isAllowedOrigin(rawUrl, allowedOrigins) {
  try {
    return allowedOrigins.includes(new URL(rawUrl).origin);
  } catch {
    return false;
  }
}

async function ensurePrivateDirectory(directory) {
  await fs.mkdir(directory, { recursive: true, mode: 0o700 });
  await fs.chmod(directory, 0o700);
}

function defaultPidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error?.code === 'EPERM';
  }
}

async function readLock(lockPath) {
  try {
    const [raw, stat] = await Promise.all([
      fs.readFile(lockPath, 'utf8'),
      fs.stat(lockPath),
    ]);
    const parsed = JSON.parse(raw);
    return { pid: Number(parsed.pid), startedAt: parsed.startedAt, mtimeMs: stat.mtimeMs };
  } catch {
    try {
      const stat = await fs.stat(lockPath);
      return { pid: NaN, startedAt: null, mtimeMs: stat.mtimeMs };
    } catch {
      return null;
    }
  }
}

export async function acquireProfileLock(profileDirectory, {
  pid = process.pid,
  now = () => Date.now(),
  isPidAlive = defaultPidAlive,
} = {}) {
  await ensurePrivateDirectory(profileDirectory);
  const lockPath = path.join(profileDirectory, LOCK_FILE);
  const candidatePath = path.join(profileDirectory, `.${LOCK_FILE}.${pid}.${crypto.randomUUID()}.candidate`);

  await fs.writeFile(
    candidatePath,
    JSON.stringify({ pid, startedAt: new Date(now()).toISOString() }) + '\n',
    { flag: 'wx', mode: 0o600 },
  );

  try {
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        await fs.link(candidatePath, lockPath);

        let released = false;
        return {
          lockPath,
          async release() {
            if (released) return;
            released = true;
            const current = await readLock(lockPath);
            if (current?.pid === pid) {
              await fs.unlink(lockPath).catch((error) => {
                if (error?.code !== 'ENOENT') throw error;
              });
            }
          },
        };
      } catch (error) {
        if (error?.code !== 'EEXIST') throw error;
        const current = await readLock(lockPath);
        if (!Number.isInteger(current?.pid) || current.pid <= 0) {
          throw new AuthRuntimeError(
            'PROFILE_LOCKED',
            'Browser Profile 锁内容无效；为避免误删活跃锁，未自动回收。',
          );
        }
        if (isPidAlive(current.pid)) {
          throw new AuthRuntimeError('PROFILE_LOCKED', '该 Browser Profile 正被另一个登录流程使用。');
        }
        await fs.unlink(lockPath).catch((unlinkError) => {
          if (unlinkError?.code !== 'ENOENT') throw unlinkError;
        });
      }
    }
  } finally {
    await fs.unlink(candidatePath).catch((error) => {
      if (error?.code !== 'ENOENT') throw error;
    });
  }
  throw new AuthRuntimeError('PROFILE_LOCKED', '无法获取 Browser Profile 锁。');
}

export async function ensureTargetPage(context, loginUrl, allowedPageOrigins) {
  if (!isAllowedOrigin(loginUrl, allowedPageOrigins)) {
    throw new AuthRuntimeError('ORIGIN_DENIED', '登录页 Origin 不在 Browser Auth Runtime 白名单中。');
  }

  const existing = context.pages().find((page) => isAllowedOrigin(page.url(), allowedPageOrigins));
  const page = existing || await context.newPage();
  if (page.url() !== loginUrl) {
    await page.goto(loginUrl, { waitUntil: 'domcontentloaded' });
  }
  await page.bringToFront();
  return page;
}

async function defaultLauncher(profileDirectory, options) {
  let chromium;
  try {
    ({ chromium } = await import('playwright-core'));
  } catch {
    throw new AuthRuntimeError(
      'DEPENDENCY_MISSING',
      '缺少 playwright-core。请在 feishu-doc 根目录执行 npm ci。',
    );
  }
  try {
    return await chromium.launchPersistentContext(profileDirectory, options);
  } catch {
    throw new AuthRuntimeError(
      'BROWSER_UNAVAILABLE',
      '无法启动系统 Chrome；请检查 macOS /Applications/Google Chrome.app 或 Linux google-chrome 是否存在且可执行。',
    );
  }
}

export async function runBrowserAuth({
  scope,
  loginUrl,
  allowedPageOrigins,
  onContextReady,
  authenticate,
  timeoutMs = DEFAULT_TIMEOUT_MS,
  stateRoot,
  launcher = defaultLauncher,
  launchOptions = {},
}) {
  const profileDirectory = resolveProfileDirectory(scope, { stateRoot });
  const lock = await acquireProfileLock(profileDirectory);
  let context;
  try {
    context = await launcher(profileDirectory, {
      channel: 'chrome',
      headless: false,
      ...launchOptions,
    });
    if (onContextReady) await onContextReady(context);
    const page = await ensureTargetPage(context, loginUrl, allowedPageOrigins);
    return await authenticate({ context, page, timeoutMs, profileDirectory });
  } catch (error) {
    if (error instanceof AuthRuntimeError) throw error;
    throw new AuthRuntimeError('AUTH_FLOW_FAILED', '浏览器认证流程失败；旧凭据未被修改。');
  } finally {
    if (context) await context.close().catch(() => {});
    await lock.release();
  }
}
