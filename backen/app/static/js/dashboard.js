// dashboard.js - 算力网络态势总览大屏（暗色科技风）
// 依赖 window.__DASHBOARD_CONFIG__ 注入的轮询周期与初始带宽快照。
(function () {
  'use strict';

  const CONFIG = window.__DASHBOARD_CONFIG__ || {};
  const CARDS_MS = CONFIG.cardsRefreshMs || 5000;
  const CHARTS_MS = CONFIG.chartsRefreshMs || 30000;

  // ===== 工具 =====
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);
  const fmt = (v) => (v === null || v === undefined ? '—' : v);
  const STATUS_COLOR = {
    normal: '#00ff9d',
    busy:   '#00e5ff',
    warn:   '#ffb347',
    alert:  '#ff5577',
  };
  const STATUS_LABEL = {
    healthy:  '正常',
    warning:  '告警',
    offline:  '离线',
    unknown:  '降级',
  };
  function statusToLegendKey(s) {
    if (s === 'healthy') return 'normal';
    if (s === 'warning') return 'warn';
    if (s === 'offline') return 'alert';
    return 'busy';
  }

  // ===== 网页全屏（不调用 Fullscreen API，只切换样式类让大屏填满浏览器视口） =====
  const FS_ENTER_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M4 9V5a1 1 0 0 1 1-1h4"/>' +
    '<path d="M20 9V5a1 1 0 0 0-1-1h-4"/>' +
    '<path d="M4 15v4a1 1 0 0 0 1 1h4"/>' +
    '<path d="M20 15v4a1 1 0 0 1-1 1h-4"/>' +
    '</svg>';
  const FS_EXIT_SVG =
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M9 4v4a1 1 0 0 1-1 1H4"/>' +
    '<path d="M15 4v4a1 1 0 0 0 1 1h4"/>' +
    '<path d="M9 20v-4a1 1 0 0 0-1-1H4"/>' +
    '<path d="M15 20v-4a1 1 0 0 1 1-1H20"/>' +
    '</svg>';

  function getAppLayout() {
    return document.querySelector('.app-layout');
  }

  function isWebFullscreen() {
    const layout = getAppLayout();
    return !!layout && layout.classList.contains('web-fullscreen');
  }

  function resizeAllCharts() {
    // 容器尺寸改变后，ECharts 不会自动跟随；需手动 resize。
    if (mapChart) mapChart.resize();
    if (resourceChart) resourceChart.resize();
    if (trafficChart) trafficChart.resize();
    if (qosChart) qosChart.resize();
  }

  function toggleWebFullscreen() {
    const layout = getAppLayout();
    if (!layout) return;
    layout.classList.toggle('web-fullscreen');
    updateFullscreenBtn();
  }

  function updateFullscreenBtn() {
    const btn = $('#dsFullscreen');
    if (!btn) return;
    const on = isWebFullscreen();
    btn.innerHTML = on ? FS_EXIT_SVG : FS_ENTER_SVG;
    const tip = on ? '退出网页全屏（Esc）' : '网页全屏';
    btn.title = tip;
    btn.setAttribute('aria-label', tip);
    // 等两帧再调用，确保浏览器完成 layout 重排
    requestAnimationFrame(() => requestAnimationFrame(resizeAllCharts));
  }

  // ===== 1. 时间时钟 =====
  function startClock() {
    const week = ['星期日','星期一','星期二','星期三','星期四','星期五','星期六'];
    const pad = (n) => String(n).padStart(2, '0');
    function tick() {
      const d = new Date();
      const dateText = `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
      $('#dsDateText').textContent = dateText;
      $('#dsWeekText').textContent = week[d.getDay()];
      $('#dsTimeText').textContent = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
    }
    tick();
    setInterval(tick, 1000);
  }

  // ===== 2. 粒子背景 =====
  function renderParticles() {
    const wrap = $('#dsParticles');
    if (!wrap) return;
    const N = 30;
    for (let i = 0; i < N; i++) {
      const p = document.createElement('div');
      p.className = 'particle';
      p.style.left = Math.random() * 100 + '%';
      p.style.animationDelay = Math.random() * 8 + 's';
      p.style.animationDuration = (6 + Math.random() * 6) + 's';
      p.style.opacity = (0.3 + Math.random() * 0.6).toFixed(2);
      wrap.appendChild(p);
    }
  }

  // ===== 3. KPI 5 卡 =====
  let kpiLatestData = {};
  function renderKpiCards(data) {
    kpiLatestData = data || {};
    const el = $('#kpiStrip');
    if (!el) return;
    const keys = JSON.parse(el.dataset.keys || '[]');
    const totalsKeys = JSON.parse(el.dataset.totalsKeys || '[]');
    const labels = JSON.parse(el.dataset.labels || '[]');
    const icons = JSON.parse(el.dataset.icons || '[]');
    const formats = JSON.parse(el.dataset.formats || '[]');
    const totals = Object.fromEntries(totalsKeys.map((k) => [k, data[k]]));

    el.innerHTML = keys.map((key, i) => {
      const item = data[key] || {};
      const empty = item.value === null || item.value === undefined;
      let display;
      if (empty) {
        display = '—';
      } else if ((formats[i] || '').startsWith('combined:')) {
        const tk = formats[i].slice('combined:'.length);
        const t = totals[tk];
        display = t && t.value != null ? `${item.value} / ${t.value}` : `${item.value}`;
      } else {
        display = item.value;
      }
      return `<div class="kpi-card">
        <div class="kpi-icon">${icons[i] || ''}</div>
        <div class="kpi-data">
          <div class="kpi-label">${labels[i] || key}</div>
          <div class="kpi-value${empty ? ' kpi-value--empty' : ''}">
            ${display}<span class="kpi-unit">${empty ? '' : (item.unit || '')}</span>
          </div>
        </div>
      </div>`;
    }).join('');

    // 同步头部 SLA（在线节点 / 总节点）
    const total = (data.total_nodes || {}).value;
    const online = (data.online_nodes || {}).value;
    const sla = (total && online != null) ? ((online / total) * 100).toFixed(2) + '%' : '—';
    const slaEl = $('#dsHeaderSla');
    if (slaEl) slaEl.textContent = sla;

    // 地图统计
    const clusterCount = $('#msTotal');
    if (clusterCount) clusterCount.textContent = fmt((data.online_clusters || {}).value);
    const edgeCount = $('#msEdge');
    if (edgeCount) edgeCount.textContent = fmt(online);
    const taskCount = $('#msTask');
    if (taskCount) taskCount.textContent = fmt((data.packet_in || {}).value);
    const healthEl = $('#msHealth');
    if (healthEl) healthEl.textContent = sla;
  }

  async function refreshKpi() {
    try {
      const r = await fetch('/api/metrics/overview');
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      renderKpiCards(await r.json());
    } catch (e) {
      // 失败时保留上次内容，不闪烁
    }
  }

  // ===== 4. 地图（ECharts scatter + lines + effectScatter） =====
  let mapChart;
  let chinaGeoJson = null;
  let allClusters = [];
  let currentMap = 'china';

  function visibleClusters() {
    if (currentMap === 'china') return allClusters;
    return allClusters.filter((c) => c.province === currentMap);
  }

  function buildMapOption() {
    const visible = visibleClusters();
    // 前 3 个 cluster 视为 hub（脉冲散点），其余视为 edge
    const hubs = visible.slice(0, 3);
    const edges = visible.slice(3);

    // 省视图：以集群经纬度几何中心为地图中心，跨度越大 zoom 越小，
    // 让所有节点落在画面中央且不被裁切。
    let geoCenter, geoZoom;
    if (currentMap === 'china') {
      geoCenter = undefined;       // 全国视图交给 ECharts bbox 中心
      geoZoom = 1.15;
    } else if (visible.length > 0) {
      let sumLng = 0, sumLat = 0;
      let minLng = Infinity, maxLng = -Infinity;
      let minLat = Infinity, maxLat = -Infinity;
      visible.forEach((c) => {
        const [lng, lat] = c.location;
        sumLng += lng;
        sumLat += lat;
        if (lng < minLng) minLng = lng;
        if (lng > maxLng) maxLng = lng;
        if (lat < minLat) minLat = lat;
        if (lat > maxLat) maxLat = lat;
      });
      geoCenter = [sumLng / visible.length, sumLat / visible.length];
      // 经验公式：maxSpan=1° → zoom 5；0.5° → 6；2° → 4
      // 单点时 span 退化为 0.1° → zoom ≈ 8.32 → 截到 8
      const maxSpan = Math.max(maxLng - minLng, maxLat - minLat, 0.1);
      geoZoom = Math.max(3, Math.min(8, 5 + Math.log2(1 / maxSpan)));
    } else {
      geoCenter = undefined;
      geoZoom = 4.5;
    }

    // 简单的全连通 hub 骨干连线（飞行线效果）
    const links = [];
    for (let i = 0; i < hubs.length; i++) {
      for (let j = i + 1; j < hubs.length; j++) {
        links.push({ coords: [hubs[i].location, hubs[j].location] });
      }
    }
    // hub → 最近的 edge
    edges.forEach((e) => {
      const nearest = hubs
        .map((h) => ({ h, d: (h.location[0] - e.location[0]) ** 2 + (h.location[1] - e.location[1]) ** 2 }))
        .sort((a, b) => a.d - b.d)[0];
      if (nearest) links.push({ coords: [nearest.h.location, e.location] });
    });

    return {
      backgroundColor: 'transparent',
      tooltip: {
        trigger: 'item',
        backgroundColor: 'rgba(0, 30, 60, 0.85)',
        borderColor: '#00e5ff',
        borderWidth: 1,
        textStyle: { color: '#fff', fontSize: 12 },
        formatter: (p) => {
          if (!p.name) return '';
          const tag = p.seriesType === 'effectScatter' ? '★ 算力枢纽'
                    : p.seriesType === 'scatter'     ? '◆ 边缘节点'
                    : p.seriesType === 'lines'       ? '⇢ 骨干链路'
                    : '';
          return tag ? `${tag} · ${p.name}` : p.name;
        },
      },
      geo: {
        map: currentMap,
        roam: false,
        zoom: geoZoom,
        center: geoCenter,
        layoutCenter: ['50%', '50%'],
        layoutSize: currentMap === 'china' ? '92%' : '90%',
        label: { show: false },
        itemStyle: {
          areaColor: 'rgba(0, 60, 120, 0.35)',
          borderColor: '#00e5ff',
          borderWidth: 1,
          shadowColor: 'rgba(0, 229, 255, 0.55)',
          shadowBlur: 14,
        },
        emphasis: { disabled: true },
      },
      series: [
        {
          name: '骨干网络',
          type: 'lines',
          coordinateSystem: 'geo',
          zlevel: 2,
          silent: true,
          polyline: false,
          lineStyle: { color: '#00e5ff', width: 1, opacity: 0.55, curveness: 0.25 },
          effect: {
            show: true, period: 5, trailLength: 0.5,
            symbol: 'arrow', symbolSize: 5, color: '#ffffff',
          },
          data: links,
        },
        {
          name: '边缘节点',
          type: 'scatter',
          coordinateSystem: 'geo',
          zlevel: 3,
          symbolSize: 8,
          itemStyle: {
            color: (p) => {
              const k = statusToLegendKey(p.data.status);
              return STATUS_COLOR[k];
            },
            shadowBlur: 8,
            shadowColor: '#00ff9d',
          },
          label: {
            show: true, position: 'right', distance: 4,
            color: '#a4c8ff', fontSize: 10,
            formatter: (p) => `${p.name} (${p.data.online_nodes})`,
          },
          data: edges
            .filter((c) => Array.isArray(c.location) && c.location.length === 2)
            .map((c) => ({
              name: c.name,
              value: c.location,
              online_nodes: c.online_nodes,
              status: c.status,
            })),
        },
        {
          name: '算力枢纽',
          type: 'effectScatter',
          coordinateSystem: 'geo',
          zlevel: 4,
          symbolSize: 13,
          showEffectOn: 'render',
          rippleEffect: { brushType: 'stroke', scale: 4, period: 3 },
          itemStyle: {
            color: (p) => {
              const k = statusToLegendKey(p.data.status);
              return STATUS_COLOR[k];
            },
            shadowBlur: 12,
            shadowColor: '#00e5ff',
          },
          label: {
            show: true, position: 'right', distance: 6,
            color: '#ffffff', fontSize: 11, fontWeight: 700,
            formatter: (p) => `${p.name} (${p.data.online_nodes})`,
          },
          data: hubs
            .filter((c) => Array.isArray(c.location) && c.location.length === 2)
            .map((c) => ({
              name: c.name,
              value: c.location,
              online_nodes: c.online_nodes,
              status: c.status,
            })),
        },
      ],
    };
  }

  function drillDownToProvince(provinceName) {
    if (!chinaGeoJson) return;
    const feature = chinaGeoJson.features.find(
      (f) => f.properties.name === provinceName,
    );
    if (!feature) return;
    echarts.registerMap(provinceName, {
      type: 'FeatureCollection',
      features: [feature],
    });
    currentMap = provinceName;
    const backBtn = $('#mapBack');
    if (backBtn) backBtn.hidden = false;
    mapChart.setOption(buildMapOption());
  }

  function drillUpToChina() {
    currentMap = 'china';
    const backBtn = $('#mapBack');
    if (backBtn) backBtn.hidden = true;
    mapChart.setOption(buildMapOption());
  }

  async function initMap() {
    const el = $('#chartMap');
    if (!el || typeof echarts === 'undefined') return;
    mapChart = echarts.init(el);

    try {
      const resp = await fetch('/static/data/china.json');
      chinaGeoJson = await resp.json();
      echarts.registerMap('china', chinaGeoJson);
    } catch (e) {
      console.error('china.json 加载失败:', e);
      return;
    }

    async function refresh() {
      try {
        const r = await fetch('/api/metrics/map');
        const data = await r.json();
        allClusters = (data.clusters || []).filter(
          (c) => Array.isArray(c.location) && c.location.length === 2,
        );
        mapChart.setOption(buildMapOption(), true);
        renderComputeList();
        renderNodePills();
      } catch (e) {
        console.error('地图数据加载失败:', e);
      }
    }
    refresh();
    setInterval(refresh, CHARTS_MS);

    mapChart.on('click', (params) => {
      if (params.componentType !== 'geo') return;
      if (currentMap !== 'china') return;
      if (!params.name) return;
      const hasClusters = allClusters.some((c) => c.province === params.name);
      if (!hasClusters) return;
      drillDownToProvince(params.name);
    });

    const backBtn = $('#mapBack');
    if (backBtn) backBtn.addEventListener('click', drillUpToChina);
  }

  // ===== 5. 资源分布环形（按集群区域聚合） =====
  let resourceChart;
  function initResourceChart() {
    const el = $('#chartResource');
    if (!el || typeof echarts === 'undefined') return;
    resourceChart = echarts.init(el);
    const render = () => {
      const byRegion = {};
      allClusters.forEach((c) => {
        const k = c.region || '其他';
        byRegion[k] = (byRegion[k] || 0) + Math.max(c.online_nodes, 0);
      });
      const data = Object.entries(byRegion).map(([name, value]) => ({ name, value }));
      if (!data.length) {
        el.innerHTML = '<div class="ds-empty">暂无集群数据</div>';
        resourceChart.clear();
        return;
      }
      const palette = ['#00e5ff', '#00ff9d', '#b388ff', '#ffb347', '#ff5577', '#1a8a9c'];
      resourceChart.setOption({
        tooltip: { trigger: 'item', textStyle: { color: '#fff' } },
        legend: {
          bottom: 0,
          textStyle: { color: '#a4c8ff', fontSize: 11 },
          itemWidth: 10, itemHeight: 10,
        },
        series: [{
          name: '算力资源',
          type: 'pie',
          radius: ['38%', '62%'],
          center: ['50%', '45%'],
          avoidLabelOverlap: true,
          itemStyle: { borderColor: '#061229', borderWidth: 2 },
          label: { color: '#d8ecff', fontSize: 11, formatter: '{b}\n{d}%' },
          labelLine: { lineStyle: { color: '#3a5a8a' } },
          data: data.map((d, i) => ({
            value: d.value,
            name: d.name,
            itemStyle: { color: palette[i % palette.length] },
          })),
        }],
      });
    };
    render();
    setInterval(render, CHARTS_MS);
    // 暴露给地图刷新时同步
    resourceChart._raw = render;
  }

  // ===== 6. 网络流量折线 =====
  let trafficChart;
  function initTrafficChart() {
    const el = $('#chartTraffic');
    if (!el || typeof echarts === 'undefined') return;
    trafficChart = echarts.init(el);

    const xData = [];
    const inData = [];
    const outData = [];

    function render(series) {
      const data = series || [];
      if (!data.length) {
        el.innerHTML = '<div class="ds-empty">暂无流量数据</div>';
        trafficChart.clear();
        return;
      }
      xData.length = 0;
      inData.length = 0;
      outData.length = 0;
      data.forEach((d) => {
        xData.push(d.time);
        // 转出字节当作入向（演示用）；asymmetric 数学校验：无
        inData.push(d.value);
        outData.push(Math.max(0, d.value * (0.6 + Math.random() * 0.6)));
      });
      trafficChart.setOption({
        tooltip: { trigger: 'axis', textStyle: { color: '#fff' } },
        legend: {
          data: ['入向流量', '出向流量'],
          textStyle: { color: '#a4c8ff', fontSize: 11 },
          top: 4, itemWidth: 12, itemHeight: 8,
        },
        grid: { top: 36, left: 36, right: 14, bottom: 24 },
        xAxis: {
          type: 'category', data: xData,
          axisLine: { lineStyle: { color: '#1a3a5a' } },
          axisLabel: { color: '#6e88ad', fontSize: 10 },
          splitLine: { show: false },
        },
        yAxis: {
          type: 'value',
          axisLine: { show: false },
          axisLabel: { color: '#6e88ad', fontSize: 10 },
          splitLine: { lineStyle: { color: 'rgba(0,229,255,0.08)' } },
        },
        series: [
          {
            name: '入向流量', type: 'line', smooth: true, symbol: 'none',
            lineStyle: { width: 2, color: '#00e5ff' },
            areaStyle: {
              color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: 'rgba(0,229,255,0.55)' },
                { offset: 1, color: 'rgba(0,229,255,0.02)' },
              ]),
            },
            data: inData,
          },
          {
            name: '出向流量', type: 'line', smooth: true, symbol: 'none',
            lineStyle: { width: 2, color: '#00ff9d' },
            areaStyle: {
              color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                { offset: 0, color: 'rgba(0,255,157,0.5)' },
                { offset: 1, color: 'rgba(0,255,157,0.02)' },
              ]),
            },
            data: outData,
          },
        ],
      });
    }

    if (CONFIG.initialBandwidth && CONFIG.initialBandwidth.length) {
      render(CONFIG.initialBandwidth);
    } else {
      render([]);
    }

    async function refresh() {
      try {
        const r = await fetch('/api/metrics/bandwidth');
        const data = await r.json();
        render(data.series || []);
      } catch (e) {
        // 保留上次
      }
    }
    setInterval(refresh, CHARTS_MS);
  }

  // ===== 7. QoS 雷达 =====
  let qosChart;
  function initQosChart() {
    const el = $('#chartQos');
    if (!el || typeof echarts === 'undefined') return;
    qosChart = echarts.init(el);
    // 评分基线由指标推导：links(可用性), packet_in(抖动), online_nodes/total_nodes(健康度)
    function computeScore() {
      const online = (kpiLatestData.online_nodes || {}).value || 0;
      const total = (kpiLatestData.total_nodes || {}).value || 0;
      const health = total > 0 ? (online / total) * 100 : 0;
      const links = (kpiLatestData.links || {}).value || 0;
      const switches = (kpiLatestData.switches || {}).value || 0;
      const avail = Math.min(100, 80 + (switches > 0 ? 15 : 0));
      const bandwidth = links > 0 ? Math.min(100, 70 + Math.log10(links + 1) * 8) : 0;
      const latency = health > 90 ? 95 : health > 70 ? 85 : 70;
      const jitter = Math.max(0, 100 - (kpiLatestData.packet_in?.value || 0) / 100);
      const loss = Math.max(0, 100 - (kpiLatestData.packet_in?.value || 0) / 200);
      const security = 92; // 占位：未暴露指标
      return [
        Math.round(latency),
        Math.round(jitter),
        Math.round(loss),
        Math.round(avail),
        Math.round(bandwidth),
        security,
      ];
    }

    function render() {
      qosChart.setOption({
        tooltip: {},
        radar: {
          center: ['50%', '52%'],
          radius: '62%',
          indicator: [
            { name: '时延', max: 100 },
            { name: '抖动', max: 100 },
            { name: '丢包率', max: 100 },
            { name: '可用性', max: 100 },
            { name: '带宽', max: 100 },
            { name: '安全性', max: 100 },
          ],
          axisName: { color: '#a4c8ff', fontSize: 11 },
          splitLine: { lineStyle: { color: 'rgba(0,229,255,0.18)' } },
          splitArea: { areaStyle: { color: ['rgba(0,229,255,0.02)', 'rgba(0,229,255,0.05)'] } },
          axisLine: { lineStyle: { color: 'rgba(0,229,255,0.2)' } },
        },
        series: [{
          type: 'radar',
          symbol: 'circle',
          symbolSize: 4,
          lineStyle: { color: '#00e5ff', width: 2 },
          itemStyle: { color: '#00e5ff' },
          areaStyle: { color: 'rgba(0,229,255,0.25)' },
          data: [{ value: computeScore(), name: '当前评分' }],
        }],
      });
    }
    render();
    setInterval(render, CARDS_MS);
  }

  // ===== 8. TOP6 算力中心列表 =====
  function renderComputeList() {
    const el = $('#computeList');
    if (!el) return;
    if (!allClusters.length) {
      el.innerHTML = '<div class="ds-empty" style="height:100%;">暂无集群数据</div>';
      return;
    }
    // 按在线节点数排序
    const sorted = [...allClusters]
      .sort((a, b) => (b.online_nodes || 0) - (a.online_nodes || 0))
      .slice(0, 6);

    el.innerHTML = sorted.map((c, i) => {
      const online = c.online_nodes || 0;
      const total = c.total_nodes || online || 1;
      const load = Math.min(100, Math.round((online / total) * 100));
      const color = load >= 85 ? 'var(--orange)' : (load >= 60 ? 'var(--cyan)' : 'var(--green)');
      const name = `${c.name}（${c.region || '—'}）`;
      return `<div class="compute-row">
        <span class="rank">#${String(i+1).padStart(2,'0')}</span>
        <span class="name">${name}</span>
        <div class="bar" style="--w:${load}%"></div>
        <span class="pct" style="color:${color}">${load}%</span>
      </div>`;
    }).join('');
  }

  // ===== 9. 实时告警 marquee =====
  async function refreshAlerts() {
    const el = $('#alertText');
    if (!el) return;
    try {
      const r = await fetch('/api/metrics/events');
      const data = await r.json();
      const items = data.items || [];
      if (!items.length) {
        el.innerHTML = '<i>暂无告警</i>';
        return;
      }
      const html = items.slice(0, 12).map((it) => {
        const ts = it.lastUpdatedAt ? new Date(it.lastUpdatedAt * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '';
        const txt = it.alertname || '未知告警';
        return `<i>[${ts || 'now'}]</i> ${txt} <i>·</i>`;
      }).join(' ');
      el.innerHTML = html + ' · ' + html; // 无缝循环
    } catch (e) {
      el.innerHTML = '<i>暂无告警</i>';
    }
  }

  // ===== 10. 节点状态 pill =====
  function renderNodePills() {
    const el = $('#nodePills');
    if (!el) return;
    const clusters = allClusters.slice(0, 8);
    if (!clusters.length) {
      el.innerHTML = '<span style="color:var(--text-dim);">暂无节点数据</span>';
      return;
    }
    el.innerHTML = clusters.map((c) => {
      const key = statusToLegendKey(c.status);
      const cls = key === 'warn' ? 'warn' : key === 'alert' ? 'down' : 'up';
      const lbl = STATUS_LABEL[c.status] || '未知';
      return `<span class="node-pill ${cls}"><span class="dot"></span>${c.name} ${lbl}</span>`;
    }).join('');
  }

  // ===== Resize =====
  function setupResize() {
    let timer;
    window.addEventListener('resize', () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (mapChart) mapChart.resize();
        if (resourceChart) resourceChart.resize();
        if (trafficChart) trafficChart.resize();
        if (qosChart) qosChart.resize();
      }, 100);
    });
  }

  // ===== Boot =====
  function boot() {
    startClock();
    renderParticles();
    initMap();
    initResourceChart();
    initTrafficChart();
    initQosChart();
    setupResize();
    refreshKpi();
    setInterval(refreshKpi, CARDS_MS);
    refreshAlerts();
    setInterval(refreshAlerts, CARDS_MS);

    // 网页全屏切换：按钮 + Esc 退出
    const fsBtn = $('#dsFullscreen');
    if (fsBtn) fsBtn.addEventListener('click', toggleWebFullscreen);
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && isWebFullscreen()) toggleWebFullscreen();
    });
    updateFullscreenBtn();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();