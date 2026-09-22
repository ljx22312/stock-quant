// 全局状态与 API
const API = '/api';  // 同源本地数据服务（nginx 反代到 127.0.0.1:8791）
const store = {
  dir: [],           // stocks 集合目录
  snaps: {},         // symbol -> snapshot
  page: 'watch',     // 当前页面
  tab: 'all',        // watchlist tab
  kind: 'stock',     // index page tab
  favs: new Set(JSON.parse(localStorage.getItem('stockdesk_favs') || '[]')),
  detail: null,      // 详情页 symbol
  chartRange: 250,
  chart: null,       // echarts 实例
  chartReq: 0,       // 图表请求序号（竞态守卫）
  chartType: 'daily', // daily | hour | tick
  profileChart: null,
  profileReq: 0,     // 量能分布请求序号
  profiles: {},      // symbol -> 量能分布缓存
  signalsList: [],   // 信号页列表缓存（点击定位用）
  markSignal: null,  // 详情页待标记的信号
  signalHist: null,  // symbol -> 该标的信号历史（缓存）
  showAllSignals: false, // 是否显示全部历史信号标记
  market: null,      // 大盘温度（/api/market，云版无此端点则为 null）
  marketAt: 0,       // 大盘温度上次拉取时间
  etf: { list: [], at: 0, cat: 'all' },  // ETF 页：目录+行情缓存、当前类别
  etfQuotes: {},     // symbol -> ETF 实时行情（snapshot 同形，供详情页头部复用）
  valsnap: null,     // {sym, data} 详情页收盘估值/资金快照
  valMetric: 'pe_ttm', // 估值历史当前指标
  valReq: 0, flowReq: 0, // 估值/资金流请求序号（竞态守卫）
  valChart: null, flowChart: null,
};
const saveFavs = () => localStorage.setItem('stockdesk_favs', JSON.stringify([...store.favs]));

async function api(path) {
  const r = await fetch(API + path);
  let j;
  try { j = await r.json(); } catch { throw new Error(`接口响应异常 (HTTP ${r.status})`); }
  if (j.error) throw new Error(j.error);
  return j.data;
}

const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const fmtQuoteTime = (qt) => {
  if (!qt) return '-';
  const c = qt.indexOf(':'); // 新浪格式 '2026-08-3116:14:55'
  if (c >= 0) return qt.slice(c - 2, c + 3);
  return qt.slice(8, 10) + ':' + qt.slice(10, 12); // 腾讯格式 '20260831161455'
};

const fmtPct = (v) => (v == null ? '-' : (v >= 0 ? '+' : '') + v.toFixed(2) + '%');
const cls = (v) => (v == null ? '' : v > 0 ? 'up' : v < 0 ? 'down' : 'flat');
const fmtVol = (h) => h >= 1e4 ? (h / 1e4).toFixed(1) + '万手' : h + '手';
const fmtAmt = (w) => w >= 1e4 ? (w / 1e4).toFixed(2) + '亿' : w + '万';
const fmtMv = (v) => v && v > 0 ? v.toFixed(1) + '亿' : '-';
const fmtPe = (v) => v && v > 0 ? v.toFixed(1) : '-';
const ruleText = (r) => r.startsWith('daily:') ? '日线' : r.endsWith('_hist') ? '历史回放' : '盘中';
// 主力净流入（元）→ 万/亿显示
const fmtMainNet = (y) => Math.abs(y) >= 1e8 ? (y / 1e8).toFixed(2) + '亿'
  : Math.abs(y) >= 1e4 ? (y / 1e4).toFixed(0) + '万' : String(Math.round(y));

// 详情页行情头部（每个术语带 ⓘ 解释入口；PB/量比/主力净流入来自收盘估值快照，云版无端点时显示 -）
function quoteGridHtml(q) {
  const v = store.valsnap && store.valsnap.sym === q.symbol ? store.valsnap.data : null;
  const mainNet = v && v.main_net != null
    ? `<span class="${cls(v.main_net)}">${fmtMainNet(v.main_net)}</span>${v.main_ratio != null ? ' <span style="font-size:11px;color:#8a8f98">' + v.main_ratio.toFixed(1) + '%</span>' : ''}`
    : '-';
  return `
    <div class="q-price ${cls(q.pct_chg)}">${q.price.toFixed(2)} <span class="pct-pill ${cls(q.pct_chg) || 'flat'}">${fmtPct(q.pct_chg)}</span> <i class="help-ico" data-help="pct">ⓘ</i></div>
    <div class="q-grid">
      <div class="q-item"><span>今开<i class="help-ico" data-help="open">ⓘ</i></span>${q.open.toFixed(2)}</div>
      <div class="q-item"><span>昨收<i class="help-ico" data-help="prev_close">ⓘ</i></span>${q.prev_close.toFixed(2)}</div>
      <div class="q-item"><span>最高<i class="help-ico" data-help="high">ⓘ</i></span>${q.high.toFixed(2)}</div>
      <div class="q-item"><span>最低<i class="help-ico" data-help="low">ⓘ</i></span>${q.low.toFixed(2)}</div>
      <div class="q-item"><span>成交量<i class="help-ico" data-help="volume">ⓘ</i></span>${fmtVol(q.volume_hand)}</div>
      <div class="q-item"><span>成交额<i class="help-ico" data-help="amount">ⓘ</i></span>${fmtAmt(q.amount_wan)}</div>
      <div class="q-item"><span>换手率<i class="help-ico" data-help="turnover">ⓘ</i></span>${q.turnover_rate}%</div>
      <div class="q-item"><span>更新<i class="help-ico" data-help="time">ⓘ</i></span>${fmtQuoteTime(q.quote_time)}</div>
      <div class="q-item"><span>总市值<i class="help-ico" data-help="total_mv">ⓘ</i></span>${fmtMv(q.total_mv)}</div>
      <div class="q-item"><span>流通市值<i class="help-ico" data-help="circ_mv">ⓘ</i></span>${fmtMv(q.circ_mv)}</div>
      <div class="q-item"><span>市盈率<i class="help-ico" data-help="pe_ttm">ⓘ</i></span>${fmtPe(q.pe_ttm)}</div>
      <div class="q-item"><span>市净率<i class="help-ico" data-help="pb">ⓘ</i></span>${v && v.pb != null ? v.pb.toFixed(2) : '-'}</div>
      <div class="q-item"><span>量比<i class="help-ico" data-help="vol_ratio">ⓘ</i></span>${v && v.vol_ratio != null ? v.vol_ratio.toFixed(2) : '-'}</div>
      <div class="q-item"><span>主力净流入<i class="help-ico" data-help="main_net">ⓘ</i></span>${mainNet}</div>
    </div>`;
}

function findItem(sym) {
  const d = store.dir.find(d => d.symbol === sym);
  if (d) return d;
  const e = store.etfQuotes[sym];
  return e ? { symbol: sym, name: e.name, type: 'etf' } : undefined;
}
function snap(sym) { return store.snaps[sym] || store.etfQuotes[sym]; }

async function refreshSnapshots() {
  const list = await api('/quote');
  store.snaps = Object.fromEntries(list.map(q => [q.symbol, q]));
}

// ---------- 大盘温度（全市场涨跌/涨停聚合；云版无 /api/market 端点时静默隐藏） ----------
async function loadMarketTemp() {
  try {
    store.market = await api('/market');
    store.marketAt = Date.now();
  } catch { store.market = null; }
  if (store.page === 'watch') renderWatch();
}

function marketTempHtml(m) {
  if (!m || !m.date) return '';
  const up = m.up || 0, down = m.down || 0;
  const ratio = up / Math.max(1, up + down);
  const upW = Math.round(ratio * 1000) / 10;
  // 情绪词由上涨家数占比机械推导：>=70% 火热 / >=55% 偏暖 / 45~55% 中性 / >=30% 偏冷 / 其余冰点
  const [vd, vc] = ratio >= 0.7 ? ['火热', 'warm'] : ratio >= 0.55 ? ['偏暖', 'warm']
    : ratio >= 0.45 ? ['中性', 'neutral'] : ratio >= 0.3 ? ['偏冷', 'cold'] : ['冰点', 'cold'];
  const stat = (label, val, c) => `<span>${label}<b class="${c || ''}">${val}</b></span>`;
  return `<div class="market-temp" data-help="market">
    <div class="mt-head"><span class="mt-title">大盘温度</span><i class="help-ico" data-help="market">ⓘ</i><span class="mt-verdict ${vc}">${vd}</span><span class="mt-date">${m.date.slice(4, 6)}-${m.date.slice(6, 8)} 收盘</span></div>
    <div class="mt-bar-wrap"><span class="mt-side up">上涨 <b>${up}</b></span><div class="mt-bar"><i style="width:${upW}%"></i></div><span class="mt-side down">下跌 <b>${down}</b></span></div>
    <div class="mt-stats">
      ${stat('涨停', m.limit_up ? m.limit_up + (m.max_lbc > 1 ? '·' + m.max_lbc + '板' : '') : 0, 'up')}
      ${stat('跌停', m.limit_down != null ? m.limit_down : '—', 'down')}
      ${stat('炸板率', m.zha_ban_rate != null ? m.zha_ban_rate + '%' : '—')}
      ${stat('中位涨幅', m.median_pct != null ? fmtPct(m.median_pct) : '—', m.median_pct > 0 ? 'up' : m.median_pct < 0 ? 'down' : '')}
    </div>
  </div>`;
}

// ---------- 页面：自选 ----------
function renderWatch() {
  if (!store.market && Date.now() - store.marketAt > 60000) loadMarketTemp();
  const groups = { stock: [], index: [] };
  for (const d of store.dir) (groups[d.type] || []).push(d);
  const favItems = [...store.favs].map(findItem).filter(Boolean);
  const tab = store.tab;
  let items = [];
  if (tab === 'stock') items = groups.stock;
  else if (tab === 'index') items = groups.index;
  else if (tab === 'fav') items = favItems;
  else items = [...groups.stock, ...groups.index];

  const tabs = [['all', '全部'], ['stock', '个股'], ['index', '指数'], ['fav', `自选(${favItems.length})`]];
  const tabHtml = tabs.map(([k, t]) => `<span class="tab ${k === tab ? 'active' : ''}" data-tab="${k}">${t}</span>`).join('');

  const listHtml = items.length ? items.map(it => {
    const q = snap(it.symbol) || {};
    return `<div class="card" data-sym="${it.symbol}">
      <div><div class="name">${esc(it.name)}</div><div class="sub">${esc(it.symbol)}${it.type === 'index' ? ' · 指数' : ''}</div></div>
      <div class="num price ${cls(q.pct_chg)}">${q.price != null ? q.price.toFixed(2) : '-'}</div>
      <div class="num"><span class="pct-pill ${cls(q.pct_chg) || 'flat'}">${fmtPct(q.pct_chg)}</span></div>
    </div>`;
  }).join('') : '<div class="empty">自选为空，点卡片进入详情后收藏</div>';

  document.getElementById('content').innerHTML = marketTempHtml(store.market) + `<div class="tabs">${tabHtml}</div>${listHtml}`;
  document.querySelectorAll('#content .tab').forEach(el =>
    el.onclick = () => { store.tab = el.dataset.tab; renderWatch(); });
  document.querySelectorAll('#content .card').forEach(el =>
    el.onclick = () => showDetail(el.dataset.sym));
}

// ---------- 页面：指数 ----------
function renderIndex() {
  const all = store.dir.filter(d => d.type === 'index' || d.type === 'industry');
  const sh = all.filter(d => d.symbol.startsWith('sh'));
  const sz = all.filter(d => d.symbol.startsWith('sz'));
  const ind = all.filter(d => d.type === 'industry');
  const list = store.kind === 'industry' ? ind : (store.kind === 'stock' ? sh : sz);
  const tabs = [['stock', '沪市指数'], ['index', '深市指数'], ['industry', '行业<i class="help-ico" data-help="industry_sw">ⓘ</i>']];
  const tabHtml = tabs.map(([k, t]) => `<span class="tab ${k === store.kind ? 'active' : ''}" data-kind="${k}">${t}</span>`).join('');
  const listHtml = list.map(it => {
    const q = snap(it.symbol) || {};
    return `<div class="card" data-sym="${it.symbol}">
      <div><div class="name">${esc(it.name)}</div><div class="sub">${esc(it.symbol)}</div></div>
      <div class="num price ${cls(q.pct_chg)}">${q.price != null ? q.price.toFixed(2) : '-'}</div>
      <div class="num"><span class="pct-pill ${cls(q.pct_chg) || 'flat'}">${fmtPct(q.pct_chg)}</span></div>
    </div>`;
  }).join('');
  document.getElementById('content').innerHTML = `<div class="tabs">${tabHtml}</div>${listHtml}`;
  document.querySelectorAll('#content .tab').forEach(el =>
    el.onclick = () => { store.kind = el.dataset.kind; renderIndex(); });
  document.querySelectorAll('#content .card').forEach(el =>
    el.onclick = () => showDetail(el.dataset.sym));
}

// ---------- 页面：ETF（精选场内基金，腾讯实时行情；日线=本地 tdx+增量合并） ----------
const ETF_CATS = [['all', '全部'], ['broad', '宽基'], ['cross', '跨境'], ['sector', '行业'], ['cmdty', '商品'], ['bond', '债券']];
async function renderEtf() {
  const el = document.getElementById('content');
  if (!store.etf.list.length || Date.now() - store.etf.at > 60000) {
    el.innerHTML = '<div class="empty">加载中…</div>';
    try {
      store.etf.list = await api('/etf');
      store.etf.at = Date.now();
      store.etfQuotes = Object.fromEntries(store.etf.list.map(it => [it.symbol, it]));
    } catch (e) {
      el.innerHTML = `<div class="empty">ETF 数据加载失败: ${e.message}</div>`;
      return;
    }
    if (store.page !== 'etf') return; // 已切走
  }
  const cat = store.etf.cat;
  const items = cat === 'all' ? store.etf.list : store.etf.list.filter(i => i.cat === cat);
  const tabHtml = ETF_CATS.map(([k, t]) =>
    `<span class="tab ${k === cat ? 'active' : ''}" data-cat="${k}">${t}</span>`).join('');
  const stale = store.etf.list.some(i => i.stale);
  const listHtml = items.map(it => `
    <div class="card" data-sym="${it.symbol}">
      <div><div class="name">${esc(it.name)}</div><div class="sub">${esc(it.symbol)}${it.cat_name ? ' · ' + esc(it.cat_name) : ''}</div></div>
      <div class="num price ${cls(it.pct_chg)}">${it.price != null ? it.price.toFixed(3) : '-'}</div>
      <div class="num"><span class="pct-pill ${cls(it.pct_chg) || 'flat'}">${fmtPct(it.pct_chg)}</span><br>
      <span style="font-size:11px;color:#8a8f98">${it.amount_wan != null ? fmtAmt(it.amount_wan) : '-'}</span></div>
    </div>`).join('');
  el.innerHTML = (stale ? '<div class="empty" style="padding:6px">行情源暂不可达，显示为缓存数据</div>' : '') +
    `<div class="tabs">${tabHtml}</div>` + (listHtml || '<div class="empty">该类别暂无标的</div>');
  document.querySelectorAll('#content .tab').forEach(el =>
    el.onclick = () => { store.etf.cat = el.dataset.cat; renderEtf(); });
  document.querySelectorAll('#content .card').forEach(el =>
    el.onclick = () => showDetail(el.dataset.sym));
}

// ---------- 页面：信号 ----------
async function renderSignals() {
  document.getElementById('content').innerHTML = '<div class="empty">加载中…</div>';
  const all = await api('/signals?limit=200');
  const realtime = all.filter(s => !s.hist);
  store.signalsList = realtime;
  const html = realtime.length ? realtime.map(s => {
    const t = new Date(s.ts * 1000);
    const ts = `${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')} ${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
    const rc = s.rule.startsWith('daily:') ? 'daily' : 'rt';
    return `<div class="sig ${rc}" data-sid="${s.id ?? s.ts}" data-sym="${esc(s.symbol)}"><div class="sig-meta">${ruleText(s.rule)} · ${ts} · ${esc(s.symbol)}</div><div class="sig-msg">${esc(s.message)}</div></div>`;
  }).join('') : '<div class="empty">暂无信号</div>';
  document.getElementById('content').innerHTML = `<div class="chart-help"><span class="chip" data-help="signal">信号是什么 ⓘ</span></div>` + html;
  document.querySelectorAll('#content .sig').forEach(el => {
    el.onclick = () => {
      const s = store.signalsList.find(x => String(x.id ?? x.ts) === el.dataset.sid);
      if (s) showDetail(s.symbol, s);
    };
  });
}

// ---------- 页面：我的 ----------
function renderMe() {
  const validFavs = [...store.favs].filter(sym => findItem(sym));
  const favHtml = validFavs.map(sym => {
    const it = findItem(sym);
    return `<div class="fav-row"><span>${esc(it.name)} <span class="sub">${esc(sym)}</span></span><span class="fav-rm" data-sym="${sym}">移除</span></div>`;
  }).join('') || '<div class="empty">暂无自选</div>';
  document.getElementById('content').innerHTML = `
    <div class="sec-title">我的自选（${validFavs.length}）</div>
    <div class="card" style="display:block">${favHtml}</div>
    <div class="sec-title">同步状态 <i class="help-ico" data-help="sync">ⓘ</i></div>
    <div class="card" style="display:block;padding:10px 14px">
      <div class="fav-row"><span>日线同步</span><span class="sub">交易日 15:45</span></div>
      <div class="fav-row"><span>快照同步</span><span class="sub">盘中每 5 分钟</span></div>
      <div class="fav-row"><span>信号同步</span><span class="sub">每小时</span></div>
    </div>`;
  document.querySelectorAll('.fav-rm').forEach(el =>
    el.onclick = () => { store.favs.delete(el.dataset.sym); saveFavs(); renderMe(); });
}

// ---------- 页面：宏观 ----------
const MACRO_CAT = {
  money: ['m2_yoy', 'm1_yoy'],
  infl: ['cpi_yoy', 'ppi_yoy'],
  growth: ['pmi_mfg', 'ip_yoy'],
  credit: ['social_financing'],
  rate: ['lpr_1y', 'shibor_3m', 'cn_gov10y', 'cn_ts10y2y', 'us_gov10y', 'us_ts10y2y'],
  funds: ['margin_sh', 'margin_sz'],
  overseas: ['fred_VIXCLS', 'fred_WALCL', 'fred_RRPONTSYD', 'fred_DTWEXBGS', 'fred_DFF'],
  emotion: ['qvix_300', 'qvix_1000'],
};
const MACRO_CAT_NAME = { money: '货币', infl: '通胀', growth: '增长', credit: '信用', rate: '利率', funds: '资金面', overseas: '海外', emotion: '情绪' };
const fmtMacroNum = (v) => {
  if (v == null) return '-';
  if (Math.abs(v) >= 10000) return Math.round(v).toLocaleString();
  if (Math.abs(v) >= 100) return Math.round(v * 100) / 100;
  return String(Math.round(v * 100) / 100);
};
const macroChg = (it) => {
  if (it.prev == null) return null;
  const d = it.latest - it.prev;
  return { d, txt: (d >= 0 ? '+' : '') + fmtMacroNum(Math.abs(d) < 0.005 ? Math.round(d * 1000) / 1000 : d) + (it.unit || '') };
};
const macroSparkOpt = (it) => ({
  grid: { left: 0, right: 0, top: 2, bottom: 0 },
  xAxis: { type: 'category', show: false, boundaryGap: false, data: it.series.map(p => p[0]) },
  yAxis: { type: 'value', show: false, scale: true },
  series: [{
    type: 'line', data: it.series.map(p => p[1]), showSymbol: false, smooth: true,
    lineStyle: { width: 1.5, color: '#4d8fd1' },
    areaStyle: { color: 'rgba(77,143,209,.14)' },
  }],
  animation: false,
});
let macroCharts = [];
async function renderMacro() {
  document.getElementById('content').innerHTML = '<div class="empty">加载中…</div>';
  const list = await api('/macro');
  store.macro = list;
  const used = new Set(Object.values(MACRO_CAT).flat());
  const byCat = [];
  for (const [cat, ids] of Object.entries(MACRO_CAT)) {
    const items = ids.map(id => list.find(x => x.id === id)).filter(Boolean);
    if (items.length) byCat.push([cat, items]);
  }
  const rest = list.filter(x => !used.has(x.id));
  if (rest.length) byCat.push(['other', rest]);
  let html = `<div class="chart-help"><span class="chip" data-help="macro">宏观指标是什么 ⓘ</span></div>`;
  for (const [cat, items] of byCat) {
    html += `<div class="sec-title">${MACRO_CAT_NAME[cat] || '其他'}</div><div class="macro-grid">`;
    for (const it of items) {
      const chg = macroChg(it);
      const d = chg ? chg.d : 0;
      html += `<div class="mcard" data-id="${esc(it.id)}">
        <div class="mname"><span class="mname-txt">${esc(it.name)}</span><i class="help-ico" data-help="${esc(it.id)}">ⓘ</i></div>
        <div class="mval">${fmtMacroNum(it.latest)}<span class="munit">${esc(it.unit)}</span></div>
        <div class="mrow"><span class="mchg ${cls(d)}">${chg ? '较上次 ' + esc(chg.txt) : '—'}</span><span class="mdate">${esc((it.series[it.series.length - 1] || [''])[0])}</span></div>
        <div class="mspark" data-id="${esc(it.id)}"></div>
      </div>`;
    }
    html += `</div>`;
  }
  document.getElementById('content').innerHTML = html;
  macroCharts.forEach(c => c.dispose());
  macroCharts = [];
  document.querySelectorAll('#content .mcard').forEach(el => {
    el.onclick = () => openMacroDetail(el.dataset.id);
  });
  document.querySelectorAll('#content .mspark').forEach(el => {
    const it = store.macro.find(x => x.id === el.dataset.id);
    if (!it) return;
    const ch = echarts.init(el);
    ch.setOption(macroSparkOpt(it));
    macroCharts.push(ch);
  });
}
async function openMacroDetail(id) {
  const it = store.macro.find(x => x.id === id) || (await api('/macro?id=' + encodeURIComponent(id)))[0];
  if (!it) return;
  store.macroSel = it;
  document.getElementById('md-name').textContent = it.name;
  document.getElementById('md-help').dataset.help = it.id;
  const chg = macroChg(it);
  document.getElementById('md-val').innerHTML =
    `<span class="md-num">${fmtMacroNum(it.latest)}<span class="munit">${esc(it.unit)}</span></span>` +
    `<span class="mchg ${cls(chg ? chg.d : 0)}">${chg ? '较上次 ' + esc(chg.txt) : '—'}</span>`;
  document.getElementById('md-note').textContent =
    `数据源：统计局 / 央行 / 交易所 / FRED 等，序列约近 10 年。点左上角 ‹ 返回。`;
  document.getElementById('macro-detail').style.display = 'block';
  document.body.style.overflow = 'hidden';
  const box = document.getElementById('md-chart');
  // 窄屏下按视口高度自适应，先定高再 init
  const h = Math.max(280, Math.min(window.innerHeight - 200, 420));
  box.style.height = h + 'px';
  if (store.macroDetailChart) store.macroDetailChart.dispose();
  store.macroDetailChart = echarts.init(box);
  store.macroDetailChart.setOption({
    backgroundColor: 'transparent',
    grid: { left: 44, right: 12, top: 16, bottom: 28 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#222834', borderColor: '#3a4250', textStyle: { color: '#d8dce3' },
      formatter: (ps) => {
        const p = ps[0];
        return `${p.axisValue}<br/><span style="color:#4d8fd1">${esc(it.name)}</span>：${fmtMacroNum(p.value)}${esc(it.unit)}`;
      },
    },
    xAxis: {
      type: 'category', boundaryGap: false,
      data: it.series.map(p => p[0]),
      axisLine: { lineStyle: { color: '#3a4250' } },
      axisLabel: { color: '#8a8f98', fontSize: 11, hideOverlap: true },
    },
    yAxis: {
      type: 'value', scale: true,
      splitLine: { lineStyle: { color: '#262c36' } },
      axisLabel: { color: '#8a8f98', fontSize: 11 },
    },
    series: [{
      type: 'line', data: it.series.map(p => p[1]), showSymbol: false, smooth: true,
      lineStyle: { width: 2, color: '#4d8fd1' },
      areaStyle: { color: 'rgba(77,143,209,.12)' },
    }],
  });
  // 窄屏下重新计算高度
  store.macroDetailChart.resize();
}
function closeMacroDetail() {
  document.getElementById('macro-detail').style.display = 'none';
  document.body.style.overflow = '';
}

// ---------- 详情页（K线） ----------
async function showDetail(sym, markSignal = null) {
  const it = findItem(sym) || { symbol: sym, name: sym, type: 'stock' };
  const isEtf = it.type === 'etf';
  if (isEtf && (store.chartType === 'hour' || store.chartType === 'tick')) store.chartType = 'daily';
  // ETF 无小时线/分时数据，隐藏对应切换按钮（切回个股时恢复）
  document.querySelectorAll('#ctype button').forEach(b => {
    b.style.display = (isEtf && (b.dataset.type === 'hour' || b.dataset.type === 'tick')) ? 'none' : '';
  });
  store.detail = sym;
  store.markSignal = markSignal;
  if (markSignal) {
    // 自动切到信号对应的图类型，让标记落在正确的时间粒度上
    store.chartType = markSignal.rule.startsWith('daily:') ? 'daily' : 'hour';
  }
  document.getElementById('detail').style.display = 'block';
  document.getElementById('d-name').textContent = it.name;
  document.getElementById('d-sym').textContent = sym;
  updateFavBtn();
  switchChartType(store.chartType);
  loadProfile(sym);
  loadValsnap(sym);
  loadValHist(sym);
  loadFundFlow(sym);
}

// 收盘估值/资金快照（市净率/量比/主力净流入三格）；云版无端点时静默，格子显示 -
async function loadValsnap(sym) {
  if (!store.valsnap || store.valsnap.sym !== sym) {
    try {
      store.valsnap = { sym, data: await api('/valsnap?symbol=' + encodeURIComponent(sym)) };
    } catch { store.valsnap = { sym, data: null }; }
  }
  if (store.detail === sym && snap(sym)) showDetailQuoteOnly();
}

function updateFavBtn() {
  const on = store.favs.has(store.detail);
  document.getElementById('d-fav').textContent = on ? '★ 已自选' : '☆ 加自选';
  document.getElementById('d-fav').style.color = on ? '#f5c242' : '#8a8f98';
}

// 日线聚合为周线/月线（OHLCV 合成，date 取该周期最后一天）
function aggregateBars(bars, period) {
  if (period === 'daily') return bars;
  const keyOf = (d) => {
    const [y, m, dd] = d.split('-').map(Number);
    if (period === 'month') return `${y}-${String(m).padStart(2, '0')}`;
    // 周：ISO 周（周一为一周开始）
    const dt = new Date(y, m - 1, dd);
    const day = (dt.getDay() + 6) % 7; // 0=周一
    const monday = new Date(dt); monday.setDate(dd - day);
    return `${monday.getFullYear()}-${String(monday.getMonth() + 1).padStart(2, '0')}-${String(monday.getDate()).padStart(2, '0')}`;
  };
  const groups = new Map();
  for (const b of bars) {
    const k = keyOf(b.date);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(b);
  }
  const out = [];
  for (const [, g] of groups) {
    out.push({
      date: g[g.length - 1].date,
      open: g[0].open,
      close: g[g.length - 1].close,
      high: Math.max(...g.map(x => x.high)),
      low: Math.min(...g.map(x => x.low)),
      volume: g.reduce((s, x) => s + x.volume, 0),
    });
  }
  return out;
}

async function loadChart(sym, limit) {
  const req = ++store.chartReq;
  store.chartRange = limit;
  document.querySelectorAll('#ranges button').forEach(b => b.classList.toggle('active', +b.dataset.limit === limit));
  let bars;
  const type = store.chartType;
  const isEtf = (findItem(sym) || {}).type === 'etf';
  if (type === 'tick') bars = await api(`/tick?symbol=${sym}`);
  else if (type === 'hour') bars = await api(`/hour?symbol=${sym}&limit=${limit}`);
  else if (type === 'week' || type === 'month') {
    // 周/月：多拉日线再聚合（周线 limit*5 天，月线 limit*22 天）
    const days = type === 'week' ? limit * 5 : limit * 22;
    const dailyPath = isEtf ? '/etfdaily' : '/daily';
    const raw = await api(`${dailyPath}?symbol=${sym}&limit=${Math.min(days, 2000)}`);
    bars = aggregateBars(raw, type);
  }
  else bars = await api(`${isEtf ? '/etfdaily' : '/daily'}?symbol=${sym}&limit=${limit}`);
  if (req !== store.chartReq) return; // 已被更新的请求/关闭取代
  const sigs = await ensureSignalHist(sym);
  if (req !== store.chartReq) return;
  if (store.chartType === 'tick') renderTick(bars);
  else renderCandle(bars, sigs);
  // 最新行情头部
  const q = snap(sym);
  document.getElementById('d-quote').innerHTML = q ? quoteGridHtml(q) : '';
}

// 小时线时间戳 202608311400 -> 08-31 14:00；日线原样（YYYY-MM-DD）
const fmtX = (t) => store.chartType === 'hour'
  ? `${t.slice(4, 6)}-${t.slice(6, 8)} ${t.slice(8, 10)}:${t.slice(10, 12)}`
  : t;

// 信号定位：日线信号 -> YYYY-MM-DD；盘中信号 -> 对应那根60分钟K线的时间戳
// 腾讯小时线的时间戳是该小时的结束时刻：09:30-10:29→1030, 10:30-11:29→1130, 13:00-13:59→1400, 14:00-15:00→1500
function sigKey(s) {
  const d = new Date(s.ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  const ymd = `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  if (s.rule.startsWith('daily:')) return ymd;
  const hm = `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}`;
  const hh = d.getHours(), mm = d.getMinutes();
  let end;
  if (hh < 10 || (hh === 10 && mm < 30)) end = '1030';
  else if (hh < 11 || (hh === 11 && mm < 30)) end = '1130';
  else if (hh < 14) end = '1400';
  else end = '1500';
  return hm + end;
}
// 键按当前图类型对齐：日线/周/月模式只要日期，小时模式要完整时间戳
const alignKey = (k) => (store.chartType === 'hour' ? k : (k.length > 10 ? `${k.slice(0, 4)}-${k.slice(4, 6)}-${k.slice(6, 8)}` : k));

async function ensureSignalHist(sym) {
  if (store.signalHist && store.signalHist.sym === sym) return store.signalHist.list;
  try {
    const list = await api(`/signals?symbol=${sym}&limit=500`);
    store.signalHist = { sym, list };
  } catch { store.signalHist = { sym, list: [] }; }
  return store.signalHist.list;
}

const RANGES = {
  daily: { limits: [60, 120, 250, 500, 1000], labels: ['3月', '半年', '1年', '2年', '4年'] },
  hour: { limits: [20, 40, 80, 160, 320], labels: ['1周', '2周', '1月', '2月', '4月'] },
  week: { limits: [26, 52, 104, 208, 400], labels: ['半年', '1年', '2年', '4年', '8年'] },
  month: { limits: [12, 24, 48, 96, 144], labels: ['1年', '2年', '4年', '8年', '12年'] },
};

function switchChartType(type) {
  store.chartType = type;
  document.querySelectorAll('#ctype button').forEach(b => b.classList.toggle('active', b.dataset.type === type));
  const isTick = type === 'tick';
  document.getElementById('ranges').style.display = isTick ? 'none' : 'flex';
  const chips = document.querySelector('#detail .chart-help');
  if (chips) chips.style.display = isTick ? 'none' : 'flex';
  updateSignalToggle();
  if (!isTick) {
    const r = RANGES[type];
    const btns = document.querySelectorAll('#ranges button');
    btns.forEach((b, i) => { b.dataset.limit = r.limits[i]; b.textContent = r.labels[i]; });
    store.chartRange = r.limits.includes(store.chartRange) ? store.chartRange : r.limits[2];
    btns.forEach(b => b.classList.toggle('active', +b.dataset.limit === store.chartRange));
  }
  loadChart(store.detail, store.chartRange);
}

function updateSignalToggle() {
  const wrap = document.getElementById('sig-toggle-wrap');
  if (!wrap) return;
  wrap.style.display = store.chartType === 'tick' ? 'none' : 'inline-flex';
  const btn = document.getElementById('sig-toggle');
  btn.textContent = store.showAllSignals ? '历史信号：开' : '历史信号';
  btn.classList.toggle('on', store.showAllSignals);
}

function renderCandle(bars, sigs = []) {
  const el = document.getElementById('chart');
  el.innerHTML = '';
  if (!bars.length) { el.innerHTML = '<div class="empty">暂无数据</div>'; return; }
  if (typeof echarts === 'undefined') { el.innerHTML = '<div class="empty">图表库加载失败</div>'; return; }
  if (store.chart) { store.chart.dispose(); }
  store.chart = echarts.init(el);
  const dates = bars.map(b => b.date);
  const kdata = bars.map(b => [b.open, b.close, b.low, b.high]);
  const vols = bars.map(b => b.volume);
  const ma = (n) => bars.map((_, i) => i < n - 1 ? null : +(bars.slice(i - n + 1, i + 1).reduce((s, x) => s + x.close, 0) / n).toFixed(3));
  const mas = [['MA5', ma(5)], ['MA20', ma(20)], ['MA60', ma(60)]]
    .filter(([, v]) => v.some(x => x != null)); // 数据不足的均线不画（如 60 根时 MA60）

  // 信号标记：把信号定位到对应那根K线（y 用该根K线最高价，点落在 K 线上方）
  const isHour = store.chartType === 'hour';
  const dateIdx = new Map(dates.map((d, i) => [d, i]));
  // 周/月模式：把信号日期映射到所属周期（该周期最后一根K线的日期）
  const periodOf = (day) => {
    for (let i = dates.length - 1; i >= 0; i--) {
      if (dates[i] <= day) return dates[i];
    }
    return null;
  };
  const marks = [];
  const seen = new Set();
  for (const s of sigs) {
    const isDailyRule = s.rule.startsWith('daily:');
    // 小时模式只画盘中信号；日/周/月模式只画日线信号
    if (isHour ? isDailyRule : !isDailyRule) continue;
    if (!store.showAllSignals && (!store.markSignal || s.id !== store.markSignal.id)) continue;
    let key = alignKey(sigKey(s));
    if (store.chartType === 'week' || store.chartType === 'month') {
      key = periodOf(key);
      if (!key) continue;
    }
    if (seen.has(key)) continue;
    seen.add(key);
    if (dateIdx.has(key)) {
      const i = dateIdx.get(key);
      marks.push({
        name: s.message,
        value: [key, bars[i].high],
      });
    }
  }
  // y 轴顶部留 12% 空间，避免信号标记与K线重叠
  const hi = Math.max(...bars.map(b => b.high), ...marks.map(m => m.value[1]));
  const lo = Math.min(...bars.map(b => b.low));
  const yTop = hi + (hi - lo) * 0.12;

  store.chart.setOption({
    backgroundColor: 'transparent',
    animation: false,
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#222834', borderColor: '#262c36',
      textStyle: { color: '#d8dce3', fontSize: 12 },
      axisPointer: { type: 'cross', label: { backgroundColor: '#4d8fd1' } },
    },
    legend: { data: mas.map(([n]) => n), textStyle: { color: '#8a8f98' }, top: 0 },
    grid: [
      { left: 50, right: 10, top: 30, height: '55%' },
      { left: 50, right: 10, top: '72%', height: '18%' },
    ],
    xAxis: [
      { type: 'category', data: dates, gridIndex: 0, axisLabel: { color: '#8a8f98', formatter: fmtX } },
      { type: 'category', data: dates, gridIndex: 1, axisLabel: { show: false } },
    ],
    yAxis: [
      { scale: true, max: yTop, gridIndex: 0, splitLine: { lineStyle: { color: '#262c36' } }, axisLabel: { color: '#8a8f98' } },
      { scale: true, gridIndex: 1, splitLine: { show: false }, axisLabel: { show: false } },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1] },
      { type: 'slider', xAxisIndex: [0, 1], bottom: 4, height: 18, borderColor: '#262c36', textStyle: { color: '#8a8f98' } },
    ],
    series: [
      {
        name: 'K线', type: 'candlestick', data: kdata, xAxisIndex: 0, yAxisIndex: 0,
        itemStyle: { color: '#e05555', color0: '#3fbf7f', borderColor: '#e05555', borderColor0: '#3fbf7f' },
      },
      ...mas.map(([n, v]) => ({ name: n, type: 'line', data: v, smooth: true, showSymbol: false, lineStyle: { width: 1, color: { MA5: '#f5c242', MA20: '#4d8fd1', MA60: '#b06fd1' }[n] } })),
      {
        name: '信号', type: 'scatter', xAxisIndex: 0, yAxisIndex: 0,
        symbol: 'triangle', symbolSize: 14, data: marks,
        itemStyle: { color: '#e05555', borderColor: '#fff', borderWidth: 1 },
        label: {
          show: true, position: 'top', fontSize: 10, color: '#fff',
          backgroundColor: '#e05555', borderRadius: 3, padding: [2, 5],
          formatter: '信号',
        },
        tooltip: { formatter: (p) => p.name },
        z: 10,
      },
      {
        name: '成交量', type: 'bar', data: vols, xAxisIndex: 1, yAxisIndex: 1,
        itemStyle: {
          // 与 K 线红绿一致：收>=开为红(涨)，否则绿(跌)
          color: (p) => {
            const b = bars[p.dataIndex];
            return b && b.close >= b.open ? '#e05555' : '#3fbf7f';
          },
        },
      },
    ],
  });
  // 点信号标记：弹出信号详情浮层
  store.chart.off('click');
  store.chart.on('click', (p) => {
    if (p.seriesName === '信号' && p.name) showSigTip(p.name, p.event.event);
  });
}

// 信号详情浮层（点红色三角弹出）
function showSigTip(text, evt) {
  let tip = document.getElementById('sig-tip');
  if (!tip) {
    tip = document.createElement('div');
    tip.id = 'sig-tip';
    document.getElementById('detail').appendChild(tip);
  }
  tip.innerHTML = `<div class="sig-tip-msg">${esc(text)}</div><span class="sig-tip-x">✕</span>`;
  tip.style.display = 'block';
  tip.querySelector('.sig-tip-x').onclick = () => { tip.style.display = 'none'; };
}

function renderTick(bars) {
  const el = document.getElementById('chart');
  el.innerHTML = '';
  if (!bars.length) { el.innerHTML = '<div class="empty">暂无分时数据</div>'; return; }
  if (typeof echarts === 'undefined') { el.innerHTML = '<div class="empty">图表库加载失败</div>'; return; }
  if (store.chart) { store.chart.dispose(); }
  store.chart = echarts.init(el);
  const times = bars.map(b => b.date.slice(8, 10) + ':' + b.date.slice(10, 12));
  const prices = bars.map(b => b.price);
  const q = snap(store.detail);
  const prevClose = q ? q.prev_close : null;
  const avgPrice = q && q.avg_price > 0 ? q.avg_price : null;
  const markLines = [];
  if (prevClose) markLines.push({
    yAxis: prevClose,
    label: { show: true, formatter: '昨收', color: '#8a8f98' },
    lineStyle: { color: '#8a8f98', type: 'dashed' },
  });
  if (avgPrice) markLines.push({
    yAxis: avgPrice,
    label: { show: true, formatter: '均价', color: '#f5c242' },
    lineStyle: { color: '#f5c242', type: 'dashed' },
  });
  store.chart.setOption({
    backgroundColor: 'transparent',
    animation: false,
    tooltip: { trigger: 'axis' },
    grid: { left: 50, right: 12, top: 20, bottom: 24 },
    xAxis: { type: 'category', data: times, axisLabel: { color: '#8a8f98' } },
    yAxis: { scale: true, splitLine: { lineStyle: { color: '#262c36' } }, axisLabel: { color: '#8a8f98' } },
    series: [{
      name: '价格', type: 'line', data: prices, smooth: true, showSymbol: false,
      lineStyle: { width: 2, color: '#4d8fd1' },
      areaStyle: { color: 'rgba(77,143,209,.12)' },
      markLine: markLines.length ? {
        silent: true, symbol: 'none',
        data: markLines,
      } : undefined,
    }],
  });
}

async function loadProfile(sym) {
  const req = ++store.profileReq;
  let rows;
  if (store.profiles[sym]) rows = store.profiles[sym];
  else rows = await api(`/profile?symbol=${sym}`);
  if (req !== store.profileReq) return;
  store.profiles[sym] = rows;
  const el = document.getElementById('profile-chart');
  el.innerHTML = '';
  if (!rows.length) { el.innerHTML = '<div class="empty">暂无量能数据</div>'; return; }
  if (typeof echarts === 'undefined') { el.innerHTML = '<div class="empty">图表库加载失败</div>'; return; }
  if (store.profileChart) { store.profileChart.dispose(); }
  store.profileChart = echarts.init(el);
  store.profileChart.setOption({
    backgroundColor: 'transparent',
    animation: false,
    tooltip: { trigger: 'axis' },
    grid: { left: 52, right: 10, top: 10, bottom: 22 },
    xAxis: { type: 'category', data: rows.map(r => r.hm), axisLabel: { color: '#8a8f98', interval: 5 } },
    yAxis: { type: 'value', splitLine: { lineStyle: { color: '#262c36' } }, axisLabel: { color: '#8a8f98', formatter: (v) => v >= 1e4 ? (v / 1e4).toFixed(0) + '万' : v } },
    series: [{ type: 'bar', data: rows.map(r => r.avg_vol), itemStyle: { color: '#4d8fd1' }, barMaxWidth: 10 }],
  });
}

function hideDetail() {
  store.chartReq++; // 使在途的 loadChart 结果作废
  store.profileReq++;
  store.valReq++;
  store.flowReq++;
  store.markSignal = null;
  store.showAllSignals = false;
  document.getElementById('detail').style.display = 'none';
  document.getElementById('val-sec').style.display = 'none';
  document.getElementById('flow-sec').style.display = 'none';
  const tip = document.getElementById('sig-tip');
  if (tip) tip.style.display = 'none';
  if (store.chart) { store.chart.dispose(); store.chart = null; }
  if (store.profileChart) { store.profileChart.dispose(); store.profileChart = null; }
  if (store.valChart) { store.valChart.dispose(); store.valChart = null; }
  if (store.flowChart) { store.flowChart.dispose(); store.flowChart = null; }
}

// ---------- 估值历史 / 主力资金流（本机独有端点，云版无此端点时区块自动隐藏） ----------
const VAL_NAME = {
  pe_ttm: '市盈率TTM', pe_static: '市盈率(静)', pb: '市净率', ps_ttm: '市销率',
  pcf_ocf_ttm: '市现率', peg: 'PEG', div_yield: '股息率', total_mv: '总市值', circ_mv: '流通市值',
};
const valFmt = (metric) => (v) => {
  if (v == null || isNaN(v)) return '-';
  if (metric === 'total_mv' || metric === 'circ_mv') return (v / 1e8).toFixed(0) + '亿';
  if (metric === 'div_yield') return v.toFixed(2) + '%';
  return v.toFixed(2);
};

async function loadValHist(sym, metric) {
  const req = ++store.valReq;
  store.valMetric = metric || store.valMetric || 'pe_ttm';
  document.getElementById('val-metric-help').dataset.help = store.valMetric;
  let data = null;
  try { data = await api(`/valuation?symbol=${encodeURIComponent(sym)}&metric=${store.valMetric}`); }
  catch { /* 云版无端点 */ }
  if (req !== store.valReq) return;
  renderValHist(sym, data);
}

function renderValHist(sym, data) {
  const sec = document.getElementById('val-sec');
  const ok = data && data.points && data.points.length && store.detail === sym;
  sec.style.display = ok ? 'block' : 'none';
  if (!ok) return;
  const since = data.start.slice(0, 4);
  const fmt = valFmt(data.metric);
  document.getElementById('val-stat').textContent =
    `当前 ${fmt(data.stats.latest)} · ${since} 年以来第 ${data.stats.pctile}% 分位（${fmt(data.stats.min)} ~ ${fmt(data.stats.max)}）`;
  const el = document.getElementById('val-chart');
  el.style.height = '190px';
  if (store.valChart) store.valChart.dispose();
  store.valChart = echarts.init(el);
  store.valChart.setOption({
    backgroundColor: 'transparent',
    animation: false,
    grid: { left: 60, right: 12, top: 12, bottom: 26 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#222834', borderColor: '#3a4250', textStyle: { color: '#d8dce3', fontSize: 12 },
      formatter: (ps) => {
        const p = ps[0];
        return `${p.axisValue}<br/><span style="color:#4d8fd1">${VAL_NAME[data.metric]}</span>：${fmt(p.value)}`;
      },
    },
    xAxis: {
      type: 'category', boundaryGap: false,
      data: data.points.map(p => p[0]),
      axisLine: { lineStyle: { color: '#3a4250' } },
      axisLabel: { color: '#8a8f98', fontSize: 11, hideOverlap: true },
    },
    yAxis: {
      type: 'value', scale: true,
      splitLine: { lineStyle: { color: '#262c36' } },
      axisLabel: { color: '#8a8f98', fontSize: 11, formatter: fmt },
    },
    dataZoom: [{ type: 'inside' }],
    series: [{
      type: 'line', data: data.points.map(p => p[1]), showSymbol: false,
      lineStyle: { width: 1.5, color: '#4d8fd1' },
      areaStyle: { color: 'rgba(77,143,209,.12)' },
    }],
  });
}

async function loadFundFlow(sym) {
  const req = ++store.flowReq;
  let data = null;
  try { data = await api(`/fundflow?symbol=${encodeURIComponent(sym)}`); }
  catch { /* 云版无端点 */ }
  if (req !== store.flowReq) return;
  renderFundFlow(sym, data);
}

function renderFundFlow(sym, data) {
  const sec = document.getElementById('flow-sec');
  const rows = data && data.rows && data.rows.length && store.detail === sym ? data.rows : null;
  sec.style.display = rows ? 'block' : 'none';
  if (!rows) return;
  const fmtYi = (v) => Math.abs(v) >= 1e8 ? (v / 1e8).toFixed(1) + '亿'
    : Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(0) + '万' : String(Math.round(v));
  let cum = 0;
  const cumArr = rows.map(r => (cum += (r.main_net || 0)));
  const el = document.getElementById('flow-chart');
  el.style.height = '210px';
  if (store.flowChart) store.flowChart.dispose();
  store.flowChart = echarts.init(el);
  store.flowChart.setOption({
    backgroundColor: 'transparent',
    animation: false,
    legend: { data: ['当日净流入', '累计'], textStyle: { color: '#8a8f98' }, top: 0 },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#222834', borderColor: '#3a4250', textStyle: { color: '#d8dce3', fontSize: 12 },
      formatter: (ps) => {
        const r = rows[Math.min(ps[0].dataIndex, rows.length - 1)];
        const pct = r.main_ratio != null ? `（占比 ${(r.main_ratio * 100).toFixed(1)}%）` : '';
        return `${r.date}<br/>主力净流入 <span style="color:${r.main_net >= 0 ? '#e05555' : '#3fbf7f'}">${fmtYi(r.main_net)}</span>${pct}<br/>累计 ${fmtYi(cumArr[ps[0].dataIndex])}`;
      },
    },
    grid: { left: 60, right: 60, top: 26, bottom: 26 },
    xAxis: {
      type: 'category', data: rows.map(r => r.date),
      axisLine: { lineStyle: { color: '#3a4250' } },
      axisLabel: { color: '#8a8f98', fontSize: 11, hideOverlap: true },
    },
    yAxis: [
      { type: 'value', scale: true, splitLine: { lineStyle: { color: '#262c36' } }, axisLabel: { color: '#8a8f98', fontSize: 11, formatter: fmtYi } },
      { type: 'value', scale: true, splitLine: { show: false }, axisLabel: { color: '#8a8f98', fontSize: 11, formatter: fmtYi } },
    ],
    series: [
      { name: '当日净流入', type: 'bar', data: rows.map(r => r.main_net), yAxisIndex: 0,
        itemStyle: { color: (p) => p.value >= 0 ? '#e05555' : '#3fbf7f' } },
      { name: '累计', type: 'line', data: cumArr, yAxisIndex: 1, showSymbol: false,
        lineStyle: { width: 1.5, color: '#f5c242' } },
    ],
  });
}

// ---------- 导航 ----------
const pages = { watch: renderWatch, index: renderIndex, etf: renderEtf, macro: renderMacro, signals: renderSignals, me: renderMe };
function switchPage(p) {
  store.page = p;
  hideDetail();
  if (p !== 'macro') {
    macroCharts.forEach(c => c.dispose());
    macroCharts = [];
  }
  document.querySelectorAll('#nav span').forEach(el => el.classList.toggle('active', el.dataset.page === p));
  pages[p]();
}

async function boot() {
  store.dir = await api('/stocks');
  await refreshSnapshots();
  switchPage('watch');
  setInterval(async () => {
    await refreshSnapshots().catch(() => {});
    if (document.getElementById('detail').style.display === 'block') {
      if (store.etfQuotes[store.detail]) {  // ETF 详情：单独刷新该只行情
        api('/etf?symbols=' + store.detail)
          .then(l => { if (l[0] && !l[0].stale) { store.etfQuotes[store.detail] = l[0]; loadChartHeaderOnly(); } })
          .catch(() => {});
      }
      const q = snap(store.detail); if (q) loadChartHeaderOnly();
    } else if (store.page === 'etf') {
      store.etf.at = 0; renderEtf();
    } else if (store.page === 'watch' || store.page === 'index') {
      pages[store.page]();
    }
  }, 60000);
}
function loadChartHeaderOnly() {
  const q = snap(store.detail);
  if (q && store.detail) showDetailQuoteOnly();
}
function showDetailQuoteOnly() {
  const q = snap(store.detail);
  document.getElementById('d-quote').innerHTML = q ? quoteGridHtml(q) : '';
}

document.querySelectorAll('#nav span').forEach(el => el.onclick = () => switchPage(el.dataset.page));
document.getElementById('d-close').onclick = hideDetail;
document.getElementById('md-close').onclick = closeMacroDetail;
document.getElementById('macro-detail').onclick = (e) => { if (e.target.id === 'macro-detail') closeMacroDetail(); };
document.getElementById('d-fav').onclick = () => {
  if (!store.detail) return;
  store.favs.has(store.detail) ? store.favs.delete(store.detail) : store.favs.add(store.detail);
  saveFavs(); updateFavBtn();
};
document.querySelectorAll('#ranges button').forEach(b => b.onclick = () => loadChart(store.detail, +b.dataset.limit));
document.querySelectorAll('#ctype button').forEach(b => b.onclick = () => switchChartType(b.dataset.type));
document.getElementById('sig-toggle').onclick = () => {
  store.showAllSignals = !store.showAllSignals;
  updateSignalToggle();
  loadChart(store.detail, store.chartRange);
};
document.getElementById('val-metric').onchange = (e) => loadValHist(store.detail, e.target.value);

boot().catch(e => {
  document.getElementById('content').innerHTML = `<div class="empty">加载失败: ${e.message}</div>`;
});

// ---------- 新手帮助（ⓘ → 底部解释面板 + 可复制的 AI 提问提示词） ----------
const HELP = {
  open: {
    title: '今开',
    what: '今天早上 9:30 开盘时，第一笔成交的价格。',
    when: '想确认今天开盘是高开还是低开（比昨收高就是高开，比昨收低就是低开），就看它。',
    prompt: '用大白话给我讲讲股票的"今开"是什么意思？什么是高开、低开？',
  },
  prev_close: {
    title: '昨收',
    what: '上一个交易日收盘时的价格，也就是今天涨跌的"起跑线"——今天的涨跌幅，都是拿它当基准算出来的。',
    when: '想知道今天到底涨了还是跌了、涨了多少，先看昨收。',
    prompt: '请用大白话解释股票的"昨收价"是什么意思，为什么涨跌幅要拿昨收当基准？',
  },
  high: {
    title: '最高',
    what: '今天一整天里，价格最高到过多少。',
    when: '想知道这只股票今天冲高到什么位置、有没有到过高位，就看它。',
    prompt: '股票行情里的"最高价"是什么意思？怎么用它辅助判断卖点？',
  },
  low: {
    title: '最低',
    what: '今天一整天里，价格最低到过多少。',
    when: '想知道今天跌到什么位置、哪里有人愿意接盘，就看它。',
    prompt: '股票行情里的"最低价"是什么意思？怎么用它辅助判断支撑位？',
  },
  volume: {
    title: '成交量',
    what: '今天一共成交了多少手（1手 = 100股）。买卖越热闹，这个数字越大。',
    when: '价格在涨、成交量也在放大，说明这个涨比较实在；如果涨的时候没人交易（缩量），就要多留个心眼。',
    prompt: '用大白话讲讲股票的"成交量"是什么意思？"放量"和"缩量"分别说明什么？',
  },
  amount: {
    title: '成交额',
    what: '今天所有买卖加起来，一共花了多少钱（这里以万元为单位）。',
    when: '想知道这只股票有多受资金关注、规模大不大，就看它。',
    prompt: '股票的"成交额"是什么意思？它和"成交量"有什么区别？',
  },
  turnover: {
    title: '换手率',
    what: '今天换手买卖的股票，占流通股总数的比例。换手率越高，说明买卖越频繁、越热闹。',
    when: '换手率突然变得很高，往往说明多空分歧大，或者有大资金在进出。',
    prompt: '用大白话解释"换手率"是什么？换手率高和低分别代表什么？',
  },
  time: {
    title: '更新时间',
    what: '这一行行情数据是几点更新的。',
    when: '先看它再下结论：如果还是早上的时间，说明数据还没刷新，别拿旧数据做决定。',
    prompt: '行情页面上的"更新时间"是什么意思？为什么看行情前要先看更新时间？',
  },
  pct: {
    title: '涨跌幅',
    what: '现在的价格跟昨天收盘价相比，涨了或跌了百分之几。红色带 + 号是涨，绿色带 - 号是跌。',
    when: '想快速知道这只股票今天表现怎么样，第一眼看它就行。',
    prompt: '股票里的"涨跌幅"是怎么算出来的？红色和绿色分别代表什么？',
  },
  kline: {
    title: 'K线',
    what: '把每天的开盘价、收盘价、最高价、最低价，画成一根柱子。红色柱子 = 今天涨了，绿色柱子 = 今天跌了；柱子上下那两根细线，是当天到过的最高价和最低价。',
    when: '看一只股票一天、或一段时间里的价格走势，就靠它。',
    prompt: '我是新手，请用最简单的话讲讲K线图怎么看？一根红柱子和一根绿柱子分别代表什么？',
  },
  redgreen: {
    title: '红涨绿跌',
    what: '中国股市的规矩：红色代表上涨，绿色代表下跌，跟国外正好相反。所以这里的红不是"危险"，是"涨了"。',
    when: '看任何行情颜色之前，先记住这一条，就不会把红当坏消息了。',
    prompt: '为什么中国股市是红涨绿跌、跟国外相反？用大白话给我解释清楚。',
  },
  ma5: {
    title: 'MA5 均线',
    what: '把最近 5 天的收盘价算平均，再把每天的这个平均价连成一条线。因为只算 5 天，它对价格变化反应最快，代表短期走势。',
    when: '想看最近几天的短期走势，就看它；价格在 MA5 上方，说明短期偏强。',
    prompt: '用大白话讲讲 MA5 均线是什么？它代表什么时间段，有什么用处？',
  },
  ma20: {
    title: 'MA20 均线',
    what: '把最近 20 天（约一个月）的收盘价算平均，连成一条线，代表一个月左右的走势，很多人把它叫作"生命线"。',
    when: '想判断中期趋势是向上还是向下，看它最常用。',
    prompt: '用大白话解释 MA20 均线（20日均线）是什么？为什么很多人把它叫"生命线"？',
  },
  ma60: {
    title: 'MA60 均线',
    what: '把最近 60 天（约一个季度）的收盘价算平均，连成一条线，代表更长时间的大方向。',
    when: '想判断大方向、这只股票整体是走强还是走弱，就看它；价格站上 MA60，通常说明中期走强。',
    prompt: '用大白话解释 MA60 均线是什么意思？价格在 MA60 上方和下方分别说明什么？',
  },
  volbar: {
    title: '成交量柱',
    what: 'K线图下方那一根根竖条，每天一根，竖条越高代表当天成交量越大，颜色跟当天涨跌一致（红涨绿跌）。',
    when: '配合 K 线一起看：涨的时候竖条在长高，说明这个涨更可信。',
    prompt: 'K线图下面那些竖条是什么？怎么配合上面的K线一起看？',
  },
  hour: {
    title: '小时线',
    what: '把一天 4 小时交易分成 4 根 60 分钟的柱子，每根代表一小时内的开盘、收盘、最高、最低。比日线更细，能看出一天里价格是怎么走的。注意：小时线数据目前只保留最近约 4 个月。',
    when: '想复盘某天盘中怎么波动、看一天内哪个时段走强走弱，就切到小时线。',
    prompt: '用大白话讲讲股票的"小时线"（60分钟K线）和日线有什么区别？什么时候适合看小时线？',
  },
  tick: {
    title: '今日分时',
    what: '把今天从开盘到现在的价格，按时间连成一条线。横轴是时间，纵轴是价格；那条灰色虚线是昨收价——线在虚线上方，说明今天暂时是涨的。',
    when: '想知道今天盘中涨跌的整个过程、现在处在全天哪个位置，看分时最直观。',
    prompt: '用大白话解释股票"分时图"怎么从零看起？上面那条灰色虚线（昨收）代表什么？',
  },
  profile: {
    title: '日内量能分布',
    what: '把过去 10 天每天的成交量，按 5 分钟一个时段拆开，算出每个时段（比如 09:35、10:00）的平均成交量，画成一根根柱子。柱子越高，说明这个时段平时买卖越活跃。',
    when: '配合"放量"判断：现在这个时段的实际成交量明显高于对应柱子，说明此刻比平时活跃；明显低于，说明清淡。',
    prompt: '用大白话解释"同时段量能分布"是什么意思？怎么用它判断股票放量、缩量？',
  },
  total_mv: {
    title: '总市值',
    what: '这家公司所有的股票加起来，按现在的价格算，一共值多少钱。数值越大，公司块头越大。',
    when: '想快速知道这家公司是大公司还是小公司，就看它。千亿以上一般算大盘股。',
    prompt: '用大白话解释"总市值"是什么意思？大盘股和小盘股有什么区别？',
  },
  circ_mv: {
    title: '流通市值',
    what: '这家公司里，可以在市场上自由买卖的那部分股票，按现在的价格算值多少钱。有些股票被大股东锁定不能卖，所以流通市值通常比总市值小一点。',
    when: '想知道真正在市场上交易、能影响价格的盘子有多大，看它。',
    prompt: '用大白话解释"流通市值"和"总市值"有什么区别？',
  },
  pe_ttm: {
    title: '市盈率（PE）',
    what: '现在的股价，相当于公司一年盈利的多少倍。比如 PE=11，意思是按现在的盈利水平，大约 11 年能赚回买股的钱。一般来说，PE 低说明相对便宜，PE 高说明相对贵（但也要看行业）。',
    when: '想粗略判断这只股票"贵不贵"，就看它。和同行业公司比、和它自己的历史比更有意义。',
    prompt: '用大白话解释"市盈率（PE）"是什么意思？PE高好还是低好？怎么看一只股票贵不贵？',
  },
  week: {
    title: '周线',
    what: '把一周的 5 根日线合成一根柱子：周一的开盘价、周五的收盘价、这一周的最高最低价。比日线看得更远，能过滤掉单日的小波动。',
    when: '想看几个月到几年的中期趋势，不被每天的涨跌干扰，就切到周线。',
    prompt: '用大白话解释股票的"周线"和日线有什么区别？什么时候适合看周线？',
  },
  month: {
    title: '月线',
    what: '把一个月的日线合成一根柱子。一根柱子代表一个月的开、收、高、低。是看长期大趋势用的。',
    when: '想判断这只股票几年来的大方向是向上还是向下，就切到月线。',
    prompt: '用大白话解释股票的"月线"是什么？怎么用月线判断长期趋势？',
  },
  avgline: {
    title: '均价线',
    what: '今天所有成交的平均价格。把今天每笔买卖的钱加起来除以总股数得到。它代表"今天大家平均的买入成本"。',
    when: '分时图里，价格在均价线上方，说明今天买的人总体是赚的（强势）；在下方，说明总体是亏的（弱势）。',
    prompt: '用大白话解释分时图里的"均价线"是什么意思？价格在均价线上方和下方分别说明什么？',
  },
  watch: {
    title: '自选',
    what: '你自己收藏的股票列表。在详情页点"加自选"，就能把常看的股票收进来，下次打开直接看。',
    when: '把每天都要盯的股票加进自选，就不用每次重新找。',
    prompt: '股票软件里的"自选股"是什么意思？一般怎么用？',
  },
  index: {
    title: '指数',
    what: '指数不是一只股票，而是一篮子股票的综合表现。比如上证指数代表整个沪市大盘，沪深300代表规模最大的 300 家公司。',
    when: '想快速知道整个市场今天好不好、大环境怎么样，就看指数。',
    prompt: '用大白话解释什么是股票"指数"？宽基指数、规模指数、基准指数都是什么意思？',
  },
  etf: {
    title: 'ETF',
    what: 'ETF 是"一篮子证券"的基金，像股票一样在场内买卖。这里精选了 49 只代表性品种：宽基（沪深300、A500）、行业（半导体、创新药）、跨境（纳指、恒生科技）、商品（黄金、原油）与债券/货币，覆盖大多数常见投资方向。',
    when: '不想选个股、只想跟某个方向（大盘/行业/黄金/海外）的涨跌，先来这里看对应 ETF 的走势和成交热度。',
    prompt: '用大白话讲讲什么是 ETF？买 ETF 和买股票有什么区别？宽基、行业、跨境 ETF 分别适合什么人？',
  },
  signal: {
    title: '信号',
    what: '系统按照设定好的规则，自动盯盘算出来的提醒。比如价格突破了某个位置、成交量突然放大等，发现值得留意的动静就会记录下来。',
    when: '每天花一分钟扫一眼信号页，可以帮你发现平时没注意到的机会或风险。',
    prompt: '股票里的"交易信号"一般指什么？常见的金叉、死叉、放量是什么意思？',
  },
  me: {
    title: '我的',
    what: '你自己的专区：这里能看到你收藏的股票，以及数据同步的状态（每天几点更新）。',
    when: '想整理自选、确认数据是否正常更新，就到这里来。',
    prompt: '帮我看看我的自选和同步状态页面应该怎么用？',
  },
  macro: {
    title: '宏观指标',
    what: '这些是影响整个市场的"大环境"数据，不是某一只股票。比如：货币供应（M1/M2）代表市场里有多少钱，CPI/PPI 代表物价贵不贵，PMI 代表工厂生意好不好，两融余额代表大家借钱炒股的热度。卡片上的小曲线是最近 10 年的走势，红色数字是比上一次数据涨了，绿色是跌了。',
    when: '想知道现在整个市场处在一个什么样的环境（钱多不多、经济热不热、海外紧不紧），就来看这个页面。大环境会间接影响所有股票。',
    prompt: '用大白话给我讲讲解读宏观指标的基本方法：M1/M2、CPI、PPI、PMI、社融、两融余额这些分别代表什么？怎么看它们现在高不高、对股市有什么影响？',
  },
  market: {
    title: '大盘温度',
    what: '整个市场当天的整体表现：多少家上涨、多少家下跌、涨停/跌停各多少家、涨停后炸板（封不住板）的比例、以及全部股票涨跌幅的中间值。它说的是"整体环境"，不是某一只股票。',
    when: '想判断今天是普涨还是普跌、市场情绪热不热，就扫一眼这里。涨停多且炸板率低说明情绪偏热；下跌家数远多于上涨说明整体走弱，个股操作要更谨慎。',
    prompt: '用大白话解释怎么通过上涨下跌家数、涨停数、炸板率、涨跌幅中位数来判断当天A股的整体情绪？',
  },
  pb: {
    title: '市净率（PB）',
    what: '股价相当于公司每股净资产的多少倍。净资产可以粗略理解为公司的"家底"（资产减去负债）。PB=1 表示股价正好等于家底价。',
    when: '看银行、地产、钢铁这类重资产行业时 PB 特别有用。PB 低不一定是便宜，要结合盈利能力一起看。',
    prompt: '用大白话解释"市净率（PB）"是什么？PB 和市盈率 PE 有什么区别，分别适合看什么类型的公司？',
  },
  vol_ratio: {
    title: '量比',
    what: '今天的成交量和最近几天同时段的平均成交量比。量比大于 1 说明今天比平时热闹（放量），小于 1 说明比平时清淡（缩量）。',
    when: '价格在涨、量比也明显大于 1，说明这个涨有真金白银支撑；价格涨但量比很小，就要多留个心眼。',
    prompt: '股票里的"量比"是什么意思？量比高和低分别说明什么？怎么用它判断放量缩量？',
  },
  main_net: {
    title: '主力净流入',
    what: '大单资金（一般代表机构、大户）当天买入减去卖出的差额。正数代表主力整体在买进，负数代表在卖出；旁边的百分比是它占当天成交额的比例。',
    when: '想知道"大资金"今天是在买还是在卖，看它最直观。但注意：主力资金也会骗线，别只凭这一个指标做决定。',
    prompt: '用大白话解释股票的"主力净流入"是什么？主力净流入为正就一定会涨吗？看这个指标要注意什么坑？',
  },
  val_hist: {
    title: '估值历史',
    what: '把这只股票 2018 年以来的市盈率、市净率、股息率等估值指标画成曲线。旁边的"分位"是说：当前值在这段历史里从低到高排在第几——第 20% 分位意思是比历史上 80% 的时间都便宜。',
    when: '判断现在相对自己的历史是贵还是便宜：低分位往往是布局区，高分位要警惕。注意：便宜不等于会涨，先看公司基本面有没有变坏，周期股的低分位经常是陷阱。',
    prompt: '市盈率的历史分位数怎么用？低分位的股票一定值得买吗？请用大白话讲讲估值百分位的用法和常见的坑。',
  },
  fund_hist: {
    title: '主力资金流',
    what: '近 10 个月每天大单资金净买入（红柱）或净卖出（绿柱）的金额，黄线是这段时间的累计净流入。柱子和黄线一起看：黄线持续向上说明主力整体在吸筹，向下说明在撤。',
    when: '想确认一段上涨或下跌有没有大资金参与时看它。注意：资金流只是从成交结构反推的参考，主力可以对倒造假，务必结合价格位置和基本面一起判断。',
    prompt: '用大白话解释股票的"主力资金流"怎么看？单日净流入和累计净流入分别说明什么？这个指标有什么局限？',
  },
  // ---------- 宏观指标逐个注释（key = 指标 id） ----------
  m2_yoy: {
    title: 'M2 同比',
    what: 'M2 是市场上"钱"的总量（现金＋活期＋定期存款都算），同比是跟去年同月比多了百分之几。钱变多了，其中一部分就可能流进股市。',
    when: 'M2 增速持续走高，说明央行在放水、资金面宽松，对股市是偏暖的大环境；持续走低则相反。它变化慢，看趋势不看单月。',
    prompt: '用大白话解释 M2 是什么？M2 同比增速高低对股市分别意味着什么？',
  },
  m1_yoy: {
    title: 'M1 同比',
    what: 'M1 是"活钱"——现金加企业活期存款，代表随时能花出去的钱。M1 增速高，说明企业手里活钱多、经营活跃；M1 和 M2 的差距（剪刀差）能看经济活力。',
    when: 'M1 增速明显回升，往往领先于经济和股市回暖；M1 长期低迷说明钱趴在账上不动，经济偏冷。',
    prompt: '用大白话解释 M1 和 M2 有什么区别？什么是 M1-M2 剪刀差，它对经济和股市有什么预示作用？',
  },
  cpi_yoy: {
    title: 'CPI 同比',
    what: 'CPI 是老百姓消费物价的涨幅，同比是跟去年同月比。2% 左右最舒服：太高是通胀（钱贬值），负数说明物价在跌（通缩，需求不足）。',
    when: 'CPI 温和上行说明需求回暖，利好消费类公司；CPI 长期为负说明消费疲软，政策往往会出手刺激，那时要盯政策方向。',
    prompt: '用大白话解释 CPI 同比是什么？CPI 太高、太低、负增长分别说明经济什么问题，对股市有什么影响？',
  },
  ppi_yoy: {
    title: 'PPI 同比',
    what: 'PPI 是工厂出厂价的涨幅，反映工业品价格。PPI 上行说明工厂产品卖得上价，周期类行业（钢铁、煤炭、有色）利润往往改善。',
    when: '做周期股必看：PPI 从负转正、持续上行时，上游资源类公司业绩通常跟着好转；PPI 持续为负说明工业品卖不动。',
    prompt: '用大白话解释 PPI 是什么？PPI 和 CPI 有什么区别？PPI 上行为什么利好周期股？',
  },
  pmi_mfg: {
    title: '制造业 PMI',
    what: 'PMI 是每个月对工厂采购经理的问卷调查，50 是荣枯线：高于 50 说明制造业在扩张，低于 50 在收缩。它是每月最早公布的经济数据。',
    when: '想抢先看经济冷暖就看它：PMI 连续站上 50 并走高，股市通常有支撑；跌破 50 且持续下行要警惕。',
    prompt: '用大白话解释制造业 PMI 是什么？50 这条荣枯线为什么重要？PMI 和股市走势有什么关系？',
  },
  ip_yoy: {
    title: '工业增加值同比',
    what: '工业增加值是工厂实际产出的增长（剔除了价格因素），代表实体经济的"产量"热度。',
    when: '它跟 PMI 互相印证：两个都强说明经济真的在回暖；PMI 强而工业增加值弱，说明回暖可能不扎实。',
    prompt: '用大白话解释工业增加值同比是什么？它和 PMI 有什么区别？怎么用它判断经济真实热度？',
  },
  social_financing: {
    title: '社融增量',
    what: '社融是全市场一个月新增的"借钱总量"——企业贷款、发债、政府发债等都算。它代表实体经济从金融体系拿到了多少钱。',
    when: '社融超预期说明企业和政府愿意加杠杆投资，对股市偏利好；持续低迷说明大家不愿借钱扩张，经济偏冷。单月波动大，看趋势。',
    prompt: '用大白话解释社会融资规模（社融）是什么？社融超预期或低于预期对股市分别意味着什么？',
  },
  lpr_1y: {
    title: '1年 LPR',
    what: 'LPR 是银行贷款的基准利率，1 年期主要影响企业短期贷款（房贷利率看 5 年期）。LPR 降 = 借钱变便宜 = 降息。',
    when: 'LPR 下调利好对利率敏感的板块（地产链）；长期不降甚至收紧时，高估值成长股容易受压。',
    prompt: '用大白话解释 LPR 是什么？LPR 下调对贷款、存款和股市分别有什么影响？',
  },
  shibor_3m: {
    title: 'Shibor 3个月',
    what: 'Shibor 是银行之间互相借钱的利率，3 个月期最常用。它是银行间资金松紧的温度计：利率低说明银行不缺钱。',
    when: 'Shibor 快速上行说明市场缺钱（钱紧），股市容易承压；持续低位说明流动性宽裕，对股市友好。',
    prompt: '用大白话解释 Shibor 是什么？Shibor 升高或降低分别说明市场资金什么情况，对股市有什么影响？',
  },
  cn_gov10y: {
    title: '中国国债10年收益率',
    what: '10 年期国债收益率是中国最安全资产的回报率，相当于国内"无风险利率"，是所有资产定价的地基。',
    when: '收益率下行，债券变贵、股票的相对吸引力上升（尤其高股息股）；快速上行说明资金成本上升，对高估值成长股不利。',
    prompt: '用大白话解释 10 年期国债收益率为什么被称为无风险利率？它上升或下降对股市分别有什么影响？',
  },
  cn_ts10y2y: {
    title: '中债期限利差（10年-2年）',
    what: '10 年期和 2 年期国债收益率的差。正常情况下长期利率更高（差为正）；利差收窄甚至倒挂，往往预示经济预期转弱。',
    when: '利差持续走阔说明市场预期未来增长和通胀回升，偏利好；利差快速收窄要留意经济降温信号。',
    prompt: '用大白话解释国债期限利差（10年减2年）是什么？利差倒挂为什么被视为经济衰退信号？',
  },
  us_gov10y: {
    title: '美国国债10年收益率',
    what: '10 年期美债收益率是全球资产定价的"锚"。它走高会吸引全球资金回流美元资产，新兴市场（包括 A 股）容易承压。',
    when: '美债收益率快速上行时，A 股尤其成长股、外资重仓股容易跌；见顶回落则是全球风险资产松口气的信号。',
    prompt: '用大白话解释 10 年期美债收益率为什么影响全球股市？它上行为什么对 A 股成长股不利？',
  },
  us_ts10y2y: {
    title: '美债期限利差（10年-2年）',
    what: '美国 10 年期和 2 年期国债收益率的差。美国历史上多次倒挂（短期利率高于长期）后出现经济衰退，是最受关注的衰退预警指标之一。',
    when: '倒挂加深说明市场预期美国要衰退或降息，全球股市波动会加大；倒挂解除往往发生在降息周期开启前后。',
    prompt: '用大白话解释美债利率倒挂是什么？为什么它被认为是美国经济衰退的预警信号？对全球股市有什么影响？',
  },
  margin_sh: {
    title: '沪市两融余额',
    what: '两融余额是股民向券商借钱买股票（融资）还没还的总金额，这里统计沪市。它代表市场里"杠杆资金"的规模，是情绪的放大器。',
    when: '两融余额持续攀升说明投资者敢加杠杆、情绪热；快速下降说明在被迫去杠杆，往往伴随下跌。它跟随行情是常态，与指数背离时才值得警惕。',
    prompt: '用大白话解释两融余额是什么？两融余额上升或下降分别反映市场什么情绪？怎么用它辅助判断行情？',
  },
  margin_sz: {
    title: '深市两融余额',
    what: '和沪市两融余额一样，是深市股民借钱买股票还没还的总金额。深市成长股、小盘股多，它更能反映激进资金的情绪。',
    when: '看法和沪市一致：持续攀升是情绪热，快速下降要警惕去杠杆踩踏。两个市场结合起来看全市场杠杆水平。',
    prompt: '用大白话解释两融余额是什么？深市两融和沪市两融反映的情绪有什么不同？',
  },
  fred_VIXCLS: {
    title: 'VIX 恐慌指数',
    what: 'VIX 是美股标普 500 期权隐含的预期波动率，俗称恐慌指数。平时 10~20，超过 30 说明市场恐慌，极端时（如 2020 年 3 月）能到 80 以上。',
    when: 'VIX 飙升说明海外出事了，A 股开盘容易跟跌；VIX 长期低位说明海外风平浪静。做隔夜持仓决策前扫一眼。',
    prompt: '用大白话解释 VIX 恐慌指数是什么？VIX 飙升对 A 股有什么影响？',
  },
  fred_WALCL: {
    title: '美联储总资产',
    what: '美联储总资产是它"印了多少钱"的直接体现：扩表（上升）= 向市场投放美元，缩表（下降）= 回收美元。',
    when: '扩表期全球流动性宽松，股票等风险资产普遍受益；缩表期资金回流美国，A 股的外资流入会变弱。',
    prompt: '用大白话解释美联储扩表和缩表是什么？它们对全球股市和 A 股分别有什么影响？',
  },
  fred_RRPONTSYD: {
    title: '隔夜逆回购用量',
    what: '美国货币基金等机构把闲钱隔夜存回美联储的规模。用量大说明美元体系里"闲钱"多但暂时不愿进市场，相当于流动性的蓄水池。',
    when: '用量持续下降说明蓄水池在放水（资金流向市场），边际利好风险资产；异常飙升说明资金避险情绪浓。这个指标偏专业，了解即可。',
    prompt: '用大白话解释美联储隔夜逆回购（ON RRP）是什么？它的用量变化对美元流动性意味着什么？',
  },
  fred_DTWEXBGS: {
    title: '美元广义指数',
    what: '美元对一篮子主要货币的综合汇率指数，衡量美元整体强不强。美元走强，人民币相对承压，外资流入 A 股会变慢。',
    when: '美元指数快速上行期，A 股（尤其外资重仓股）和人民币汇率通常承压；美元走弱期，新兴市场资产普遍受益。',
    prompt: '用大白话解释美元指数是什么？美元走强或走弱对人民币和 A 股分别有什么影响？',
  },
  fred_DFF: {
    title: '联邦基金利率',
    what: '美国的基准利率，美联储加息降息说的就是它。它决定全球美元的"价格"，是所有海外资产的总开关。',
    when: '加息周期全球资金回流美国，A 股承压；降息周期资金外溢，新兴市场受益。它变化不频繁，但每次变化都是大事。',
    prompt: '用大白话解释联邦基金利率是什么？美联储加息和降息分别如何传导到 A 股？',
  },
  qvix_300: {
    title: 'QVIX 沪深300',
    what: 'QVIX 是从沪深300 相关期权价格反推出的市场预期波动率，相当于 A 股版的"恐慌指数"。数值越高，说明市场预期未来波动越大。',
    when: 'QVIX 突然飙升说明期权玩家在买保护、预期大跌，短线要谨慎；长期低位说明市场自满，反而要留意变盘。',
    prompt: '用大白话解释 QVIX（中国波指）是什么？它和美股 VIX 有什么区别？怎么用它判断 A 股短期风险？',
  },
  qvix_1000: {
    title: 'QVIX 中证1000',
    what: '从中证1000 相关期权反推的预期波动率，反映中小盘股的恐慌程度。用法和 QVIX300 一样，只是标的偏向小盘股。',
    when: '做小盘、题材股时看它更准：QVIX1000 飙升说明小盘股波动加剧，追高要慎重。',
    prompt: '用大白话解释中证1000 的 QVIX 是什么？为什么做中小盘股票要关注它？',
  },
  // ---------- 估值历史下拉指标 ----------
  pe_static: {
    title: '市盈率（静）',
    what: '静态市盈率 = 现在的股价 ÷ 上一年度的每股收益。它用的是"已经翻篇"的旧业绩，不像 TTM（滚动四个季度）那样贴近最新情况。',
    when: '年报刚发布时它和 TTM 差不多；年中看它会偏旧。两个 PE 差距大，说明公司最近业绩变化大。',
    prompt: '用大白话解释静态市盈率和 TTM 市盈率有什么区别？看估值时该用哪个？',
  },
  ps_ttm: {
    title: '市销率（PS）',
    what: '市销率 = 总市值 ÷ 一年的营业收入。它不看利润只看收入，适合还没盈利或利润波动大的公司（成长期公司、周期低谷的公司）。',
    when: '公司亏损、PE 算不出来时用 PS 兜底比较；但收入高不等于赚钱，PS 要和毛利率、净利率一起看。',
    prompt: '用大白话解释市销率（PS）是什么？什么样的公司适合用 PS 估值？只用 PS 估值有什么坑？',
  },
  pcf_ocf_ttm: {
    title: '市现率（PCF）',
    what: '市现率 = 总市值 ÷ 一年的经营现金流。利润可以被会计手法调节，现金流很难造假，所以它比 PE 更"实"。',
    when: '怀疑一家公司利润有水分时用它验证：PE 低但市现率很高，说明账面利润没变成真金白银，要小心。',
    prompt: '用大白话解释市现率（PCF）是什么？为什么说现金流比利润更难造假？怎么用市现率验证利润质量？',
  },
  peg: {
    title: 'PEG',
    what: 'PEG = 市盈率 ÷ 盈利增速（%）。PE 30 倍但增速 30%，PEG=1，说明贵得有道理；PE 低但增速为负，PEG 反而难看。一般 PEG 小于 1 算相对便宜。',
    when: '看成长股时比单看 PE 更公平。注意：它高度依赖对未来增速的估计，增速预测错了 PEG 就没意义。',
    prompt: '用大白话解释 PEG 估值法是什么？PEG 小于 1 就一定能买吗？用 PEG 估值要注意什么？',
  },
  div_yield: {
    title: '股息率 TTM',
    what: '股息率 = 最近一年每股分红 ÷ 现在的股价。股价 10 元、一年分红 0.5 元，股息率就是 5%，相当于买这只股票的"利息"。',
    when: '熊市里高股息股（银行、煤炭等）抗跌性往往更好，因为有分红托底；但要看分红能不能持续，业绩下滑的公司分红可能缩水。',
    prompt: '用大白话解释股息率是什么？高股息股票有什么优缺点？什么是"股息率陷阱"？',
  },
  // ---------- 其他小项 ----------
  sync: {
    title: '数据同步时间',
    what: '这个网站的数据更新节奏：日线行情每个交易日 15:45 收盘后更新；盘中快照每 5 分钟抓一次；信号每小时扫一轮。',
    when: '收盘前看到的日线还是昨天的，别拿它当今天的结果；盘中看实时涨跌以快照为准。',
    prompt: '股票软件里的日线数据为什么通常收盘后才更新？盘中数据和收盘数据有什么区别？',
  },
  industry_sw: {
    title: '行业指数（申万一级）',
    what: '这里列的是申万一级行业指数，共 31 个，覆盖 A 股全部行业分类。每个指数代表一个行业所有股票打包后的整体表现，比如"银行"指数就是所有银行股的综合走势。',
    when: '想判断市场热点在哪个行业、手里的股票是跟着行业走还是独自异动，就来这里对照。行业涨而个股跌，说明是个股自己的问题。',
    prompt: '用大白话解释申万一级行业分类是什么？怎么通过行业指数判断市场热点轮动？',
  },
  newbie: {
    title: '新手入门：一张K线图怎么看',
    what: '一共四步：① 记住"红涨绿跌"，红 = 涨、绿 = 跌；② 图里每根柱子代表一天，柱子的高低是当天的价格范围；③ 图里那几条彩色细线是"均线"（MA5、MA20、MA60），代表最近 5 天、20 天、60 天的平均价格走势，时间越长的线方向越稳；④ 图下方一根根竖条是每天的成交量，条越高当天买卖越热闹。',
    when: '第一次打开这个页面，或者想跟家人解释这张图，就先看这一条。',
    prompt: '我是完全不懂股票的新手，请用最通俗的话，一步步教我怎么看懂一张K线图，包括红绿颜色、柱子、彩色均线和下面的成交量。',
  },
};

function openHelp(key) {
  const h = HELP[key];
  if (!h) return;
  document.getElementById('help-title').textContent = h.title;
  document.getElementById('help-what').textContent = h.what;
  document.getElementById('help-when').textContent = h.when;
  document.getElementById('help-prompt').textContent = h.prompt;
  document.getElementById('help-copy').textContent = '复制提示词';
  document.getElementById('help').classList.add('show');
  document.body.style.overflow = 'hidden'; // 打开面板时锁住背景滚动
}

function closeHelp() {
  document.getElementById('help').classList.remove('show');
  document.body.style.overflow = '';
}

function fallbackCopy(text, done) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand('copy'); done(); } catch (e) { /* 忽略 */ }
  document.body.removeChild(ta);
}

function copyPrompt() {
  const t = document.getElementById('help-prompt').textContent;
  const done = () => {
    const b = document.getElementById('help-copy');
    b.textContent = '已复制 ✓';
    setTimeout(() => { b.textContent = '复制提示词'; }, 1500);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(t).then(done).catch(() => fallbackCopy(t, done));
  } else {
    fallbackCopy(t, done);
  }
}

// 捕获阶段拦截：点 ⓘ / 术语打开解释，且不触发卡片、页签的点击
document.addEventListener('click', (e) => {
  const t = e.target.closest('[data-help]');
  if (t) { e.stopPropagation(); openHelp(t.dataset.help); return; }
  if (e.target.closest('#help-mask') || e.target.closest('#help-close')) closeHelp();
}, true);

document.getElementById('help-copy').onclick = copyPrompt;
document.getElementById('help-fab').onclick = () => openHelp('newbie');
