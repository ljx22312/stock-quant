/**
 * Kimi API key 多级故障转移代理。
 *
 * 为什么需要它：
 *   Kimi 订阅 key 有"5 小时窗口用量上限"，用满后该窗口内所有请求返回
 *   403 access_terminated_error。pi 的 provider apiKey 是启动时固定的，
 *   而网页 AI 的 pi 会话是长驻的，换 key 需要重启会话；放在代理层则对上层透明。
 *
 * key 链（按顺序尝试，逐个降级）：
 *   KIMI_API_KEY（主） → KIMI_API_KEY_BACKUP（备用） → KIMI_API_KEY_BACKUP2（二级备用）
 *
 * 行为：
 *   1. 优先用第一个"未在冷却期"的 key；某个 key 出现额度/鉴权类错误时，
 *      给它打上冷却标记并立刻用下一个 key 重发本次请求；
 *   2. 冷却期结束后自动重试更高优先级的 key，成功即切回 —— 即"有额度就用优先级高的"；
 *   3. 全部 key 都不可用时，不再向上层抛错，而是返回一条正常的模型回复
 *      （KEYPROXY_EXHAUSTED_MESSAGE，默认"目前 AI 模型额度已用尽，请稍后再询问。"），
 *      网页端会像普通回答一样展示这句话。
 *
 * 流式安全：2xx 响应直接透传 body（SSE 不缓冲）；只有错误响应才读 body 做判断。
 * 无第三方依赖，Node 内置 http/fetch。
 */
'use strict';

const http = require('http');
const { Readable } = require('stream');
const fs = require('fs');
const path = require('path');

// ---- 配置（可被 systemd EnvironmentFile 覆盖） ----
const PORT = parseInt(process.env.KEYPROXY_PORT || '8799', 10);
const HOST = process.env.KEYPROXY_HOST || '127.0.0.1';
const UPSTREAM = (process.env.KIMI_UPSTREAM || 'https://api.kimi.com/coding/v1').replace(/\/+$/, '');
// 单个 key 的冷却时长：到期自动重试（额度窗口重置后尽快切回），默认 60s
const COOLDOWN_MS = parseInt(process.env.KEYPROXY_COOLDOWN_MS || '60000', 10);
const EXHAUSTED_MESSAGE =
  process.env.KEYPROXY_EXHAUSTED_MESSAGE || '目前 AI 模型额度已用尽，请稍后再询问。';

function loadDotEnv(file) {
  try {
    for (const line of fs.readFileSync(file, 'utf8').split('\n')) {
      const m = line.match(/^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\s*$/);
      if (m && process.env[m[1]] === undefined) process.env[m[1]] = m[2].replace(/^["']|["']$/g, '');
    }
  } catch {}
}
// 本地直接运行（非 systemd）时也能读到仓库 .env
loadDotEnv(path.join(__dirname, '.env'));

// 有序 key 链（没配置的层级自动跳过）
const KEYS = [
  { name: 'primary', label: '主 key', key: process.env.KIMI_API_KEY || '' },
  { name: 'backup', label: '备用 key', key: process.env.KIMI_API_KEY_BACKUP || '' },
  { name: 'backup2', label: '二级备用 key', key: process.env.KIMI_API_KEY_BACKUP2 || '' },
].filter((k) => k.key);

const cooldownUntil = new Map(); // key name -> 冷却截止时间戳

function remainingMs(name) {
  return Math.max(0, (cooldownUntil.get(name) || 0) - Date.now());
}

// 候选 key：优先用未冷却的；全部冷却时探测第一个（额度窗口可能已重置）
function candidates() {
  const fresh = KEYS.filter((k) => remainingMs(k.name) === 0);
  return fresh.length ? fresh : KEYS.slice(0, 1);
}

// 额度/鉴权类错误识别：触发即换下一个 key 重试
function isKeyFailure(status, bodyText) {
  if (![401, 402, 403, 429].includes(status)) return false;
  const t = String(bodyText || '').toLowerCase();
  return (
    t.includes('access_terminated_error') ||
    t.includes('usage limit') ||
    t.includes('quota') ||
    t.includes('insufficient') ||
    t.includes('balance') ||
    t.includes('exceeded') ||
    t.includes('额度') ||
    t.includes('余额') ||
    t.includes('欠费') ||
    status === 401
  );
}

function log(msg) {
  console.log(`[keyproxy ${new Date().toISOString()}] ${msg}`);
}

// 逐跳/需重写的头不转发；authorization 由本代理按当前 key 注入
const SKIP_REQ_HEADERS = new Set([
  'host', 'authorization', 'content-length', 'connection', 'transfer-encoding', 'accept-encoding',
]);

async function forward(reqPath, method, rawHeaders, bodyBuf, key) {
  const h = {};
  for (const [k, v] of Object.entries(rawHeaders || {})) {
    if (SKIP_REQ_HEADERS.has(String(k).toLowerCase())) continue;
    h[k] = v;
  }
  h['authorization'] = 'Bearer ' + key;
  // pi 的 provider baseUrl 是 <代理>/v1，而上游本身就含 /v1，
  // 归一化避免拼成 /v1/v1/...（404 resource_not_found_error）
  const p = UPSTREAM.endsWith('/v1') && reqPath.startsWith('/v1/') ? reqPath.slice(3) : reqPath;
  return fetch(UPSTREAM + p, {
    method,
    headers: h,
    body: method === 'GET' || method === 'HEAD' ? undefined : bodyBuf,
  });
}

function pipeUpstream(resp, res) {
  res.statusCode = resp.status;
  resp.headers.forEach((v, k) => {
    const lk = k.toLowerCase();
    if (['content-length', 'content-encoding', 'connection', 'transfer-encoding'].includes(lk)) return;
    res.setHeader(k, v);
  });
  if (resp.body) {
    Readable.fromWeb(resp.body).pipe(res);
  } else {
    res.end();
  }
}

// 所有 key 都不可用时，返回一条"正常"的模型回复，让网页端直接展示给用户
function exhaustedReply(res, reqJson) {
  const model = (reqJson && reqJson.model) || 'k3';
  const id = 'chatcmpl-keyproxy-' + Date.now();
  const created = Math.floor(Date.now() / 1000);
  log(`全部 key 不可用 → 返回友好提示："${EXHAUSTED_MESSAGE}"`);

  if (reqJson && reqJson.stream) {
    res.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8', 'cache-control': 'no-cache' });
    const chunk = (delta, finish) =>
      'data: ' + JSON.stringify({
        id, object: 'chat.completion.chunk', created, model,
        choices: [{ index: 0, delta, finish_reason: finish }],
      }) + '\n\n';
    res.write(chunk({ role: 'assistant', content: '' }, null));
    res.write(chunk({ content: EXHAUSTED_MESSAGE }, null));
    res.write(chunk({}, 'stop'));
    res.write('data: [DONE]\n\n');
    return res.end();
  }

  res.writeHead(200, { 'content-type': 'application/json; charset=utf-8' });
  return res.end(JSON.stringify({
    id, object: 'chat.completion', created, model,
    choices: [{ index: 0, message: { role: 'assistant', content: EXHAUSTED_MESSAGE }, finish_reason: 'stop' }],
    usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
  }));
}

const server = http.createServer(async (req, res) => {
  const reqPath = req.url || '/';

  // 健康检查：暴露 key 链与各自冷却状态，便于运维确认
  if (reqPath === '/health' || reqPath === '/keyproxy/health') {
    const active = candidates()[0];
    res.writeHead(200, { 'content-type': 'application/json' });
    return res.end(JSON.stringify({
      ok: true,
      active: active ? active.name : null,
      cooldownMs: COOLDOWN_MS,
      exhaustedMessage: EXHAUSTED_MESSAGE,
      keys: KEYS.map((k) => ({
        name: k.name,
        label: k.label,
        hasKey: !!k.key,
        coolingRemainingMs: remainingMs(k.name),
      })),
    }));
  }

  // 收集请求体（OpenAI 请求体为 JSON，体积小）
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const bodyBuf = Buffer.concat(chunks);
  let reqJson = null;
  try { reqJson = JSON.parse(bodyBuf.toString('utf8')); } catch {}

  if (!KEYS.length) {
    res.writeHead(500, { 'content-type': 'application/json' });
    return res.end(JSON.stringify({ error: { message: 'keyproxy: 未配置任何 KIMI_API_KEY' } }));
  }

  const list = candidates();
  const lastKeyIdx = list.length - 1;

  for (let i = 0; i < list.length; i++) {
    const { name, label, key } = list[i];
    const wasCooling = remainingMs(name) > 0;
    let resp;
    try {
      resp = await forward(reqPath, req.method, req.headers, bodyBuf, key);
    } catch (e) {
      log(`${label}(${name}) 请求异常: ${e.message}`);
      if (i === lastKeyIdx) break;
      continue;
    }

    if (resp.ok) {
      if (wasCooling) {
        log(`${label}(${name}) 已恢复，切回该 key`);
        cooldownUntil.delete(name);
      }
      if (i > 0) log(`使用 ${label}(${name}) 转发成功: ${req.method} ${reqPath}`);
      return pipeUpstream(resp, res);
    }

    const errText = await resp.text().catch(() => '');
    if (isKeyFailure(resp.status, errText)) {
      cooldownUntil.set(name, Date.now() + COOLDOWN_MS);
      log(`${label}(${name}) 额度/鉴权失败(HTTP ${resp.status}) → 冷却 ${Math.round(COOLDOWN_MS / 1000)}s` +
          (i < lastKeyIdx ? `，改用 ${list[i + 1].label}` : '，已无下一级 key'));
      continue;
    }

    // 非 key 问题的错误（参数错误等）：原样透出，不降级
    res.statusCode = resp.status;
    res.setHeader('content-type', resp.headers.get('content-type') || 'application/json');
    return res.end(errText);
  }

  return exhaustedReply(res, reqJson);
});

server.listen(PORT, HOST, () => {
  log(`启动监听 http://${HOST}:${PORT} → ${UPSTREAM}；key 链=[${KEYS.map((k) => k.name).join(' → ') || '空'}]，单 key 冷却=${Math.round(COOLDOWN_MS / 1000)}s`);
});
