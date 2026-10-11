// dashboard.js - Dashboard 实时刷新：指标卡轮询 + ECharts 图表 + 列表
(function () {
  'use strict';

  const CONFIG = window.__DASHBOARD_CONFIG__ || {};
  const CARDS_MS = CONFIG.cardsRefreshMs || 5000;
  const CHARTS_MS = CONFIG.chartsRefreshMs || 30000;

  // 指标卡图标（与 components.css 配色对应）
  const ICONS = {
    nodes:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><circle cx="19" cy="5" r="2"/><circle cx="5" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/></svg>',
    server:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="6" rx="1"/><rect x="2" y="11" width="20" height="6" rx="1"/></svg>',
    cpu:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/></svg>',
    memory:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="6" width="20" height="12" rx="1"/><line x1="6" y1="10" x2="6" y2="14"/><line x1="10" y1="10" x2="10" y2="14"/><line x1="14" y1="10" x2="14" y2="14"/><line x1="18" y1="10" x2="18" y2="14"/></svg>',
    switch:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="4" width="20" height="6" rx="1"/><rect x="2" y="14" width="20" height="6" rx="1"/></svg>',
    link:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>',
    host:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/></svg>',
    packet:
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>',
  };

  function fmt(v) {
    return v === null || v === undefined ? '—' : v;
  }

  // 渲染单张指标卡的值文本
  // format:
  //   "plain"（默认） — 原样输出 item.value
  //   "combined:KEY"  — 输出 `${item.value} / ${data[KEY].value}`（用于"在线/总节点数"）
  function renderMetricValue(item, format, data) {
    if (item.value === null || item.value === undefined) return '—';
    if (format && format.startsWith('combined:')) {
      const secondary = (data[format.slice('combined:'.length)] || {}).value;
      if (secondary === null || secondary === undefined) return '—';
      return `${item.value} / ${secondary}`;
    }
    return item.value;
  }

  // ===== 1. 指标卡轮询 =====
  function setupMetricCards() {
    document.querySelectorAll('.panel__metrics[data-endpoint]').forEach((el) => {
      const endpoint = el.dataset.endpoint;
      let keys, labels, icons, colors, formats;
      try {
        keys = JSON.parse(el.dataset.keys || '[]');
        labels = JSON.parse(el.dataset.labels || '[]');
        icons = JSON.parse(el.dataset.icons || '[]');
        colors = JSON.parse(el.dataset.colors || '[]');
        formats = JSON.parse(el.dataset.formats || '[]');
      } catch (e) {
        return;
      }

      async function refresh() {
        try {
          const r = await fetch(endpoint);
          if (!r.ok) throw new Error(`HTTP ${r.status}`);
          const data = await r.json();
          el.innerHTML = keys
            .map((key, i) => {
              const item = data[key] || {};
              const empty = item.value === null || item.value === undefined;
              return `<div class="metric-card">
                <div class="metric-card__header">
                  <span class="metric-card__label">${labels[i] || key}</span>
                  <div class="metric-card__icon metric-card__icon--${colors[i] || 'blue'}">${ICONS[icons[i]] || ''}</div>
                </div>
                <div class="metric-card__value${empty ? ' metric-card__value--empty' : ''}">
                  ${renderMetricValue(item, formats[i], data)}<span class="metric-card__unit">${item.unit || ''}</span>
                </div>
              </div>`;
            })
            .join('');
        } catch (e) {
          el.innerHTML = '<div class="loading">数据加载失败</div>';
        }
      }
      refresh();
      setInterval(refresh, CARDS_MS);
    });
  }

  // ===== 2. 中国地图（ECharts scatter）— 支持点击省份下钻 =====
  let geoMap;
  let chinaGeoJson = null;   // 缓存 china.json 完整 GeoJSON（用于省下钻）
  let allClusters = [];       // 缓存 /map 返回的完整集群列表
  let currentMap = 'china';   // 当前显示层级：'china' 或省份全称（如 "湖南省"）

  // 当前视图可见的集群：中国视图显示全部；省视图按 province 过滤
  function visibleClusters() {
    if (currentMap === 'china') return allClusters;
    return allClusters.filter((c) => c.province === currentMap);
  }

  function renderMap() {
    const visible = visibleClusters();
    const geoBase = {
      roam: false,
      itemStyle: { areaColor: '#e2e8f0', borderColor: '#94a3b8', borderWidth: 1 },
      emphasis: { itemStyle: { areaColor: '#cbd5e1' } },
      label: { show: false },
    };

    let geoConfig;
    if (currentMap === 'china') {
      geoConfig = { ...geoBase, map: 'china', center: [105, 36], zoom: 1.2 };
    } else {
      // 省视图：用省的中心 + 高 zoom
      const feature = chinaGeoJson.features.find(
        (f) => f.properties.name === currentMap,
      );
      const center = (feature && feature.properties.center) || [105, 36];
      geoConfig = { ...geoBase, map: currentMap, center, zoom: 4.5 };
    }

    geoMap.setOption({
      tooltip: { trigger: 'item' },
      geo: geoConfig,
      series: [
        {
          type: 'scatter',
          coordinateSystem: 'geo',
          data: visible
            .filter((c) => Array.isArray(c.location) && c.location.length === 2)
            .map((c) => ({
              name: c.name,
              value: [c.location[0], c.location[1], c.online_nodes],
              status: c.status,
            })),
          symbolSize: 25,
          itemStyle: {
            color: (p) =>
              p.data.status === 'healthy'
                ? '#10b981'
                : p.data.status === 'warning'
                  ? '#f59e0b'
                  : '#94a3b8',
            shadowBlur: 15,
          },
          emphasis: { scale: 1.2 },
          label: {
            show: true,
            position: 'bottom',
            formatter: '{b}',
            fontSize: 11,
            color: '#1e293b',
          },
        },
      ],
    });
  }

  function drillDownToProvince(provinceName) {
    if (!chinaGeoJson) return;
    const feature = chinaGeoJson.features.find(
      (f) => f.properties.name === provinceName,
    );
    if (!feature) return;
    // ECharts 子地图注册：把单省 feature 包成 FeatureCollection
    echarts.registerMap(provinceName, {
      type: 'FeatureCollection',
      features: [feature],
    });
    currentMap = provinceName;
    const backBtn = document.getElementById('mapBack');
    if (backBtn) backBtn.hidden = false;
    renderMap();
  }

  function drillUpToChina() {
    currentMap = 'china';
    const backBtn = document.getElementById('mapBack');
    if (backBtn) backBtn.hidden = true;
    renderMap();
  }

  async function initMap() {
    const el = document.getElementById('geoMap');
    if (!el || typeof echarts === 'undefined') return;
    geoMap = echarts.init(el);

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
        const resp = await fetch(el.dataset.endpoint);
        const data = await resp.json();
        allClusters = data.clusters || [];
        renderMap();
      } catch (e) {
        console.error('地图数据加载失败:', e);
      }
    }
    refresh();
    setInterval(refresh, CHARTS_MS);

    // 点击 geo 区域下钻（仅中国层级、且该省有集群时触发）
    geoMap.on('click', (params) => {
      if (params.componentType !== 'geo') return;
      if (currentMap !== 'china') return;
      if (!params.name) return;
      const hasClusters = allClusters.some((c) => c.province === params.name);
      if (!hasClusters) return;
      drillDownToProvince(params.name);
    });

    // 返回全国按钮
    const backBtn = document.getElementById('mapBack');
    if (backBtn) backBtn.addEventListener('click', drillUpToChina);
  }

  // ===== 3. 带宽趋势（24h） =====
  let bandwidthChart;
  async function initBandwidth() {
    const el = document.getElementById('bandwidthTrend');
    if (!el || typeof echarts === 'undefined') return;
    bandwidthChart = echarts.init(el);

    function render(series) {
      bandwidthChart.setOption({
        grid: { left: 36, right: 8, top: 5, bottom: 18 },
        xAxis: {
          type: 'category',
          data: series.map((d) => d.time),
          axisLine: { lineStyle: { color: '#e2e8f0' } },
          axisLabel: { fontSize: 9, color: '#94a3b8', show: false },
        },
        yAxis: {
          type: 'value',
          axisLine: { show: false },
          axisTick: { show: false },
          axisLabel: { fontSize: 9, color: '#94a3b8' },
          splitLine: { lineStyle: { color: '#e2e8f0' } },
        },
        tooltip: { trigger: 'axis', formatter: '{b}<br/>{c} Mbps' },
        series: [
          {
            type: 'line',
            smooth: true,
            showSymbol: false,
            data: series.map((d) => d.value),
            areaStyle: {
              color: {
                type: 'linear',
                x: 0,
                y: 0,
                x2: 0,
                y2: 1,
                colorStops: [
                  { offset: 0, color: 'rgba(139, 92, 246, 0.3)' },
                  { offset: 1, color: 'rgba(139, 92, 246, 0)' },
                ],
              },
            },
            lineStyle: { color: '#8b5cf6', width: 2 },
            itemStyle: { color: '#8b5cf6' },
          },
        ],
      });
    }

    if (CONFIG.initialBandwidth && CONFIG.initialBandwidth.length) {
      render(CONFIG.initialBandwidth);
    }

    async function refresh() {
      try {
        const r = await fetch(el.dataset.endpoint);
        const data = await r.json();
        render(data.series || []);
      } catch (e) {
        console.error('带宽趋势加载失败:', e);
      }
    }
    refresh();
    setInterval(refresh, CHARTS_MS);
  }

  // ===== 4. 切片环形图 =====
  let sliceChart;
  async function initSliceChart() {
    const el = document.getElementById('sliceChart');
    if (!el || typeof echarts === 'undefined') return;
    sliceChart = echarts.init(el);
    const PALETTE = ['#0ea5e9', '#8b5cf6', '#f59e0b', '#10b981', '#ef4444'];

    function renderEmpty() {
      el.innerHTML =
        '<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--color-text-muted);font-size:11px;">暂无切片数据</div>';
      sliceChart.clear();
    }

    async function refresh() {
      try {
        const r = await fetch(el.dataset.endpoint);
        const data = await r.json();
        const items = data.items || [];
        if (!items.length) {
          renderEmpty();
          return;
        }
        sliceChart.setOption({
          tooltip: { trigger: 'item', formatter: '{b}: {c} ({d}%)' },
          series: [
            {
              type: 'pie',
              radius: ['45%', '70%'],
              center: ['50%', '50%'],
              avoidLabelOverlap: false,
              itemStyle: { borderRadius: 4, borderColor: '#fff', borderWidth: 2 },
              label: { show: false },
              emphasis: { label: { show: false } },
              data: items.map((s, i) => ({
                value: s.cpu_used || 0,
                name: s.name || `slice-${i}`,
                itemStyle: { color: PALETTE[i % PALETTE.length] },
              })),
            },
          ],
        });
      } catch (e) {
        renderEmpty();
      }
    }
    refresh();
    setInterval(refresh, CHARTS_MS);
  }

  // ===== 5. 链路质量列表 =====
  async function initLinkQuality() {
    const el = document.getElementById('linkQuality');
    if (!el) return;

    function renderEmpty() {
      el.innerHTML =
        '<li style="color:var(--color-text-muted);text-align:center;padding:8px 0;">暂无数据</li>';
    }

    async function refresh() {
      try {
        const r = await fetch(el.dataset.endpoint);
        const data = await r.json();
        const items = data.items || [];
        if (!items.length) {
          renderEmpty();
          return;
        }
        el.innerHTML = items
          .map(
            (l) => `
            <li>
              <span>${l.source} → ${l.target}</span>
              <span class="list-item__primary">${l.latency}ms</span>
            </li>`,
          )
          .join('');
      } catch (e) {
        renderEmpty();
      }
    }
    refresh();
    setInterval(refresh, CHARTS_MS);
  }

  // ===== 6. QoS 策略列表 =====
  async function initPolicies() {
    const el = document.getElementById('qosOverview');
    if (!el) return;

    function renderEmpty() {
      el.innerHTML =
        '<li style="color:var(--color-text-muted);text-align:center;padding:8px 0;">暂无数据</li>';
    }

    async function refresh() {
      try {
        const r = await fetch(el.dataset.endpoint);
        const data = await r.json();
        const items = data.items || [];
        if (!items.length) {
          renderEmpty();
          return;
        }
        el.innerHTML = items
          .slice(0, 4)
          .map(
            (p) => `
            <li>
              <span>${p.name}</span>
              <span class="status status--${p.status === 'active' ? 'healthy' : 'offline'}">${p.status === 'active' ? '生效' : '未生效'}</span>
            </li>`,
          )
          .join('');
      } catch (e) {
        renderEmpty();
      }
    }
    refresh();
    setInterval(refresh, CHARTS_MS);
  }

  // ===== Resize =====
  function setupResize() {
    let timer;
    window.addEventListener('resize', () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (geoMap) geoMap.resize();
        if (bandwidthChart) bandwidthChart.resize();
        if (sliceChart) sliceChart.resize();
      }, 100);
    });
  }

  // ===== Boot =====
  function boot() {
    setupMetricCards();
    initMap();
    initBandwidth();
    initSliceChart();
    initLinkQuality();
    initPolicies();
    setupResize();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot);
  } else {
    boot();
  }
})();