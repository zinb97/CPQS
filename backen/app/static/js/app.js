// app.js - 通用页面交互：侧边栏折叠 + 系统时钟
(function () {
  'use strict';

  // 侧边栏折叠
  const toggle = document.getElementById('sidebarToggle');
  const sidebar = document.getElementById('sidebar');
  if (toggle && sidebar) {
    toggle.addEventListener('click', () => {
      sidebar.classList.toggle('sidebar--collapsed');
    });
  }

  // 系统时钟（仅 client-side，避免 SSR 与客户端时区不一致）
  function updateClock() {
    document.querySelectorAll('[data-clock]').forEach((el) => {
      el.textContent = new Date().toLocaleString('zh-CN', {
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      });
    });
  }
  updateClock();
  setInterval(updateClock, 1000);
})();