// health.js —— 浏览器侧时延测量（分段分解版）
//
// 主指标 = **TCP 握手 RTT**（connectEnd - connectStart）——这是浏览器能拿到的
// 最接近 ping 的量：一次 TCP 三次握手的往返时间，不含 DNS、不含 HTTP 往返。
// 另外给出 DNS / 首字节(TTFB) / 总耗时（用户感知的打开耗时）作参照。
//
// 时延由**运行浏览器的这台设备**实测：服务器端测不到「客户端 → 其它服务器」这条
// 路径（那是客户端的网络出口），只能由浏览器自己发请求、自己计时。
//
// 关键限制：JavaScript 不能建裸 TCP 连接、不能发 ICMP，浏览器只能发 HTTP 请求。
// 采集器提供轻量的 GET /health（返回 OK），用它当探测目标——下载几百 KB 的
// /metrics 会反过来干扰探测结果。
(function () {
  'use strict';

  const $ = (s) => document.querySelector(s);
  const body = $('#hbBody');
  const SAMPLES = parseInt($('.hb-page').dataset.samples, 10) || 3;

  let runToken = 0;          // 渲染竞态守卫：只有最新一次 run 允许写表格

  function esc(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function fmt(v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return '—';
    return Number(v).toFixed(digits === undefined ? 2 : digits) + ' ms';
  }

  function median(arr) {
    const v = arr.filter((x) => x !== null && x !== undefined && isFinite(x));
    if (!v.length) return null;
    const s = [...v].sort((a, b) => a - b);
    const m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }

  // 取两时刻之差。Chrome 对跨源且响应没有 Timing-Allow-Origin 的资源会把
  // 分段字段清零（可能是 0，也可能只清零了其中一个）——不做这层校验就会算出
  // 「DNS 613ms > 总耗时 505ms」这种自相矛盾的假值。
  function segDiff(start, end) {
    if (!(start > 0) || !(end > start)) return null;
    return end - start;
  }

  // 取某个 URL 的全部 Resource Timing 条目（按请求顺序）
  function entriesFor(baseUrl) {
    try {
      return performance.getEntriesByType('resource')
        .filter((e) => e.name.split('?')[0] === baseUrl);
    } catch (err) {
      return [];
    }
  }

  // 单个目标的浏览器侧探测
  async function probe(t) {
    const times = [];
    let error = null;
    for (let i = 0; i < SAMPLES; i++) {
      // 加随机串绕开缓存；采集器用 urlparse(self.path).path 路由，忽略查询串
      const url = t.url + (t.url.indexOf('?') >= 0 ? '&' : '?') + '_t=' + Date.now() + '_' + i;
      const t0 = performance.now();
      try {
        await fetch(url, {
          mode: 'no-cors',
          cache: 'no-store',
          referrerPolicy: 'no-referrer',
        });
        times.push(performance.now() - t0);
      } catch (e) {
        error = (e && e.message) ? e.message : String(e);
        break;
      }
    }
    return {
      target: t,
      times: times,
      latency: times.length ? median(times) : null,
      min: times.length ? Math.min.apply(null, times) : null,
      max: times.length ? Math.max.apply(null, times) : null,
      error: error,
      dns: null, tcp: null, ttfb: null, tcpSamples: null,
    };
  }

  // 把 Resource Timing 的分段耗时合并进结果。
  //
  // 条目是在响应处理完之后才入表的，必须等探测全部结束后统一读（探测过程中
  // 立刻读会读到空数组）。条目按请求顺序与 samples 一一对应。
  function applyTimings(rows) {
    rows.forEach((r) => {
      const entries = entriesFor(r.target.url);
      const dns = [], tcp = [], ttfb = [];
      entries.forEach((e, i) => {
        const d = segDiff(e.domainLookupStart, e.domainLookupEnd);
        const t = segDiff(e.connectStart, e.connectEnd);
        const f = segDiff(e.requestStart, e.responseStart);
        // 自洽校验：各段之和不应超过该样本的总耗时。不满足说明这组分段不可信
        // （多半是浏览器清零字段造成的），整个样本的分段丢弃。
        const total = r.times[i];
        const sum = [d, t, f].filter((x) => x !== null).reduce((a, b) => a + b, 0);
        if (total !== undefined && sum > total * 1.05) return;
        if (d !== null) dns.push(d);
        if (t !== null) tcp.push(t);
        if (f !== null) ttfb.push(f);
      });
      r.dns = median(dns);
      r.tcp = median(tcp);
      r.ttfb = median(ttfb);
      r.tcpSamples = tcp;          // 握手 RTT 的各次采样（算抖动用）
    });
  }

  async function loadTargets() {
    const r = await fetch('/health/targets');
    const d = await r.json();
    return d.targets || [];
  }

  function render(rows) {
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="9" class="hb-empty">暂无目标服务器</td></tr>';
      return;
    }
    body.innerHTML = rows.map((r) => {
      const t = r.target;
      const cls = r.error ? 'hb-bad' : '';
      // 采样波动：主指标（握手 RTT）的极差；拿不到握手就退回总耗时
      const base = (r.tcp !== null) ? r.tcpSamples : r.times;
      const jitter = (base && base.length > 1)
        ? Math.max.apply(null, base) - Math.min.apply(null, base) : null;
      const status = r.error
        ? esc(r.error)
        : (r.latency === null ? '—' : `可达 · ${r.times.length} 次采样`);
      return `<tr>
        <td>${esc(t.hostname || '—')}</td>
        <td>${esc(t.cluster || '—')}</td>
        <td class="hb-mono">${esc(t.instance)}</td>
        <td class="${cls}">${fmt(r.tcp)}</td>
        <td>${fmt(r.dns)}</td>
        <td>${fmt(r.ttfb)}</td>
        <td>${fmt(r.latency)}</td>
        <td>${fmt(jitter)}</td>
        <td class="${cls}">${status}</td>
      </tr>`;
    }).join('');

    // 汇总卡：以 TCP 握手 RTT 为准
    const ok = rows.filter((r) => !r.error && r.latency !== null);
    const hands = ok.map((r) => r.tcp).filter((x) => x !== null);
    const totals = ok.map((r) => r.latency);
    $('#hbCount').textContent = rows.length;
    $('#hbCliOk').textContent = ok.length + '/' + rows.length;
    $('#hbCliAvg').textContent = hands.length
      ? fmt(hands.reduce((a, b) => a + b, 0) / hands.length) : '—';
    $('#hbCliMax').textContent = hands.length ? fmt(Math.max.apply(null, hands)) : '—';

    // 拿不到分段数据时给出可执行的提示（采集器未部署 Timing-Allow-Origin）
    const hint = $('#hbHint');
    if (hint) {
      const withTcp = rows.filter((r) => r.tcp !== null).length;
      const missing = rows.filter((r) => !r.error && r.tcp === null).length;
      if (rows.length && withTcp === 0) {
        hint.hidden = false;
        hint.textContent = '未拿到 TCP 握手耗时：采集器的 /health 需要返回 Timing-Allow-Origin 头，'
          + '请更新采集器后重试（python3 deploy-tool/deploy.py deploy --all --restart-tasks）。'
          + '在更新之前只能给出「总耗时」。';
      } else if (missing > 0) {
        hint.hidden = false;
        hint.textContent = missing + ' 个目标没有握手耗时（对应采集器尚未更新到带 '
          + 'Timing-Allow-Origin 头的版本），这些目标只给出「总耗时」。';
      } else {
        hint.hidden = true;
      }
    }
  }

  async function runProbe() {
    const token = ++runToken;          // 本次 run 的令牌
    const btn = $('#hbRun');
    btn.disabled = true;
    btn.textContent = '测量中…';
    try {
      const targets = await loadTargets();
      if (token !== runToken) return;   // 已被更新的 run 取代
      if (!targets.length) {
        render([]);
        return;
      }

      // 逐个探测：串行，避免并发把带宽/连接队列搅乱
      const rows = [];
      for (const t of targets) {
        const r = await probe(t);
        r.tcpSamples = null;
        rows.push(r);
        if (token !== runToken) return;
        render(rows);   // 逐行刷新，看得到进度
      }

      // Resource Timing 的条目是在响应处理完之后才入表的，最后统一读一遍。
      await new Promise((r) => setTimeout(r, 300));
      if (token !== runToken) return;
      applyTimings(rows);
      render(rows);
    } catch (e) {
      if (token !== runToken) return;
      body.innerHTML = `<tr><td colspan="9" class="hb-empty hb-bad">加载失败：${esc(e.message || e)}</td></tr>`;
    } finally {
      if (token === runToken) {
        btn.disabled = false;
        btn.textContent = '开始测量';
      }
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    $('#hbRun').addEventListener('click', runProbe);
  });
})();
