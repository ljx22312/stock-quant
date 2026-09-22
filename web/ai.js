/* StockDesk AI 对话框 v2 —— IIFE 封装，避免与 app.js 顶层声明冲突 */
(() => {
/* StockDesk AI 对话框 v2 —— 同源中转（匿名写请求/读回复，本机 worker 出站处理） */

const AI_DB = '';   // 同源：nginx 把 /collections/* 反代到本地数据服务

const ai = {
  // session_id：一次持久对话（localStorage），agent 靠它续上下文
  sid: localStorage.getItem('stockdesk_ai_sid') || (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()),
  skill: localStorage.getItem('stockdesk_ai_skill') || '',
  activeReq: null,       // 当前进行中的请求 id
  activeTimer: null,     // 轮询定时器
  msgs: [],              // {role, text}
};

localStorage.setItem('stockdesk_ai_sid', ai.sid);
localStorage.removeItem('stockdesk_ai_mode'); // 模式/模型选择已移除，清理旧用户残留
localStorage.removeItem('stockdesk_ai_model');

async function aiDb(path, opts = {}) {
  const r = await fetch(AI_DB + path, {
    ...opts,
    headers: { 'Content-Type': 'application/json', ...opts.headers },
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.message || `HTTP ${r.status}`);
  return j;
}

function aiToast(msg, isErr) {
  const d = document.createElement('div');
  d.className = 'ai-toast' + (isErr ? ' ai-toast-err' : '');
  d.textContent = msg;
  document.body.appendChild(d);
  setTimeout(() => d.remove(), 4000);
}

function openAi() {
  document.getElementById('ai').classList.add('show');
  document.body.style.overflow = 'hidden';
  renderAi();
}
function closeAi() {
  document.getElementById('ai').classList.remove('show');
  document.body.style.overflow = '';
  if (ai.activeTimer) { clearInterval(ai.activeTimer); ai.activeTimer = null; }
}

function renderAi() {
  const box = document.getElementById('ai-msgs');
  box.innerHTML = ai.msgs.length ? '' : `<div class="ai-empty">
    我是你的股票研究助理：可查真实行情数据、写代码算指标/做回测，支持多轮对话记忆。<br>
    直接开始提问 ↓
  </div>`;
  for (const m of ai.msgs) box.appendChild(msgEl(m));
  box.scrollTop = box.scrollHeight;
}

function msgEl(m) {
  const d = document.createElement('div');
  d.className = 'ai-msg ' + m.role;
  // 思考过程：流式时默认展开，结束后自动收起；用户手动开合过则以手动状态为准
  const thinkOpen = m.thinkManual !== undefined ? m.thinkManual : !!m.pending;
  const think = m.thinking ? `<details class="ai-thinking"${thinkOpen ? ' open' : ''}><summary>🤔 思考过程</summary><div class="ai-think-body">${esc(m.thinking)}</div></details>` : '';
  d.innerHTML = `${think}<div class="ai-bubble">${linkify(esc(m.text)).replace(/\n/g, '<br>')}${m.pending ? '<span class="ai-typing">…</span>' : ''}</div>`;
  return d;
}
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const linkify = s => s.replace(/(https?:\/\/[^\s<]+)/g, '<a href="$1" target="_blank" rel="noopener">$1</a>');

// ---- 一键复制当前对话 ----
function conversationText() {
  const head = `StockDesk AI 对话记录（${new Date().toLocaleString('zh-CN')}）\n`;
  const body = ai.msgs.map((m) => {
    const who = m.role === 'user' ? '【我】' : '【AI】';
    let s = who + '\n' + (m.text || '（无内容）');
    if (m.thinking) s += '\n\n[思考过程]\n' + m.thinking;
    return s;
  }).join('\n\n────────\n\n');
  return head + '\n' + body;
}

// 非安全上下文（http 域名）下 navigator.clipboard 不可用，退回 execCommand
function legacyCopy(text) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '-1000px';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, ta.value.length);
  let ok = false;
  try { ok = document.execCommand('copy'); } finally { ta.remove(); }
  if (!ok) throw new Error('浏览器拒绝复制');
}

async function copyAll() {
  if (!ai.msgs.length) { aiToast('当前对话为空'); return; }
  const text = conversationText();
  const n = ai.msgs.length;
  try {
    if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      legacyCopy(text);
    }
    aiToast(`已复制全部对话（${n} 条）`);
  } catch (e) {
    try { legacyCopy(text); aiToast(`已复制全部对话（${n} 条）`); }
    catch (e2) { aiToast('复制失败：' + (e2.message || e.message), true); }
  }
}

function bindAi() {
  document.getElementById('ai-fab').onclick = openAi;
  document.getElementById('ai-close').onclick = closeAi;
  document.getElementById('ai-mask').onclick = closeAi;
  document.getElementById('ai-send').onclick = sendMsg;
  document.getElementById('ai-input').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
  });
  document.getElementById('ai-skill').onchange = e => { ai.skill = e.target.value; localStorage.setItem('stockdesk_ai_skill', ai.skill); };
  document.getElementById('ai-stop').onclick = stopCurrent;
  document.getElementById('ai-clear').onclick = clearChat;
  document.getElementById('ai-copy').onclick = copyAll;
  // 记录用户对思考块的手动开合（toggle 事件不冒泡，用捕获监听）
  document.getElementById('ai-msgs').addEventListener('toggle', e => {
    if (!e.target.classList || !e.target.classList.contains('ai-thinking')) return;
    const box = document.getElementById('ai-msgs');
    const idx = [...box.children].indexOf(e.target.closest('.ai-msg'));
    if (idx >= 0 && ai.msgs[idx]) ai.msgs[idx].thinkManual = e.target.open;
  }, true);
  document.getElementById('ai-input').placeholder = '问行情、查数据、写代码算指标/回测…';
}

async function sendMsg() {
  if (ai.activeReq) { aiToast('上一条还在处理中…'); return; }
  const inp = document.getElementById('ai-input');
  const text = inp.value.trim();
  if (!text) return;
  inp.value = '';
  ai.msgs.push({ role: 'user', text });
  renderAi();
  // 写请求
  try {
    const j = await aiDb('/collections/ai_requests/documents', {
      method: 'POST',
      body: JSON.stringify({
        data: [{
          mode: 'agent', question: text, session_id: ai.sid, status: 'pending',
          model: 'kimi/k3', skill: ai.skill,
          created_at: { $date: { $numberLong: String(Date.now()) } },
        }],
      }),
    });
    ai.activeReq = j.insertedIds[0];
    ai.msgs.push({ role: 'assistant', text: '', thinking: '', pending: true });
    setBusy(true);
    renderAi();
    pollReply();
  } catch (e) {
    aiToast('提交失败: ' + e.message, true);
  }
}

// 请求处理中切换 发送/停止 按钮
function setBusy(busy) {
  const send = document.getElementById('ai-send');
  const stop = document.getElementById('ai-stop');
  send.style.display = busy ? 'none' : '';
  stop.style.display = busy ? '' : 'none';
}

// 用户点击停止：写一条 stop 请求，worker 轮询到后 abort 当前生成
async function stopCurrent() {
  if (!ai.activeReq) return;
  try {
    await aiDb('/collections/ai_requests/documents', {
      method: 'POST',
      body: JSON.stringify({
        data: [{
          mode: 'stop', target_id: ai.activeReq, status: 'pending',
          created_at: { $date: { $numberLong: String(Date.now()) } },
        }],
      }),
    });
    aiToast('已发送停止指令，正在停止…');
  } catch (e) {
    aiToast('停止指令发送失败: ' + e.message, true);
  }
}

function pollReply() {
  if (!ai.activeReq) return;
  if (ai.activeTimer) clearInterval(ai.activeTimer);
  const box = document.getElementById('ai-msgs');
  let tries = 0;
  const started = Date.now();
  const MAX_TRIES = 150;          // 4s 间隔约 10 分钟，防 agent 掉线后无限空轮询
  const tick = async () => {
    if (!document.hidden) {
      try {
        const q = encodeURIComponent(JSON.stringify({ request_id: ai.activeReq }));
        const j = await aiDb(`/collections/ai_replies/documents?query=${q}&limit=1`);
        const rep = j.list && j.list[0];
        if (rep) {
          const idx = ai.msgs.findIndex(m => m.pending);
          if (idx >= 0) {
            ai.msgs[idx].text = rep.text || '';
            ai.msgs[idx].thinking = rep.thinking || '';
            ai.msgs[idx].pending = !rep.done;
            // 替换当前节点
            const node = box.children[idx];
            if (node) node.outerHTML = msgEl(ai.msgs[idx]).outerHTML;
            box.scrollTop = box.scrollHeight;
          }
          if (rep.done) {
            clearInterval(ai.activeTimer);
            ai.activeTimer = null;
            ai.activeReq = null;
            setBusy(false);
            renderAi();
            return;
          }
        }
      } catch (e) {
        // 网络抖动忽略，下次轮询重试
      }
    }
    tries += 1;
    if (tries >= MAX_TRIES || Date.now() - started > 15 * 60 * 1000) {
      // 回复丢失/agent 掉线时停止空转，避免每 2 秒一次的无效读取持续一整天
      clearInterval(ai.activeTimer);
      ai.activeTimer = null;
      const idx = ai.msgs.findIndex(m => m.pending);
      if (idx >= 0) ai.msgs[idx].pending = false;
      setBusy(false);
      aiToast('回复超时，可重发消息继续对话', true);
      renderAi();
    }
  };
  ai.activeTimer = setInterval(tick, 4000);
}

async function clearChat() {
  if (!confirm('清空当前对话？Agent 的会话记忆也会清除。')) return;
  if (ai.activeTimer) { clearInterval(ai.activeTimer); ai.activeTimer = null; }
  ai.sid = crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random();
  localStorage.setItem('stockdesk_ai_sid', ai.sid);
  ai.msgs = [];
  ai.activeReq = null;
  setBusy(false);
  renderAi();
}

document.addEventListener('DOMContentLoaded', () => {
  bindAi();
});

})();
