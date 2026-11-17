#!/usr/bin/env python3
"""Linux Metrics HTTP Exporter.

聚合多个采集器（host、gpu 等）并提供 Prometheus 指标抓取端点。

    python3 metrics_exporter.py --port 9100

端点：
    /metrics  Prometheus 文本格式
    /health   健康检查
    /         简单索引页
"""
from __future__ import annotations

import http.server
import sys
from typing import Iterable
from urllib.parse import urlparse

from gpu_collector import GpuCollector, PYNVML_AVAILABLE, PYNVML_IMPORT_ERROR
from host_collector import HostCollector


_INDEX_HTML = '''<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Linux Metrics Exporter</title></head>
<body><h1>Linux Metrics Exporter</h1>
<p><a href="/metrics">/metrics</a> - Prometheus 格式指标</p>
<p><a href="/health">/health</a> - 健康检查</p>
</body></html>'''


class MetricsServer(http.server.ThreadingHTTPServer):
    """带采集器列表的 HTTP 服务器。

    采集器挂在**服务器实例**上（而不是 handler 的类属性），
    避免用可变类属性做全局状态——多实例或测试场景下会互相污染。

    必须用多线程版（ThreadingHTTPServer）：单线程的 TCPServer 会被一个慢连接
    （Prometheus 抓取超时后仍在发数据）阻塞后续所有抓取。
    ThreadingHTTPServer 自带 daemon_threads，且 allow_reuse_address 默认为真，
    重启不会被端口占用卡住。
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler_cls, collectors: Iterable):
        super().__init__(address, handler_cls)
        self.collectors = tuple(collectors)


class MetricsHandler(http.server.BaseHTTPRequestHandler):
    """Prometheus 指标 HTTP 处理器。"""

    def do_GET(self) -> None:
        """按路径分派到对应的响应方法。"""
        path = urlparse(self.path).path
        if path == '/metrics':
            self._respond_metrics()
        elif path == '/health':
            self._respond_health()
        elif path == '/':
            self._respond_index()
        else:
            self._respond_not_found()

    def _respond_metrics(self) -> None:
        # 每个采集器忽略自己不支持的 kwargs；空串过滤掉
        parts = [c.to_prometheus().rstrip() for c in self.server.collectors]
        body = '\n'.join(p for p in parts if p) + '\n'
        self._write(200, 'text/plain; version=0.0.4; charset=utf-8', body)

    def _respond_health(self) -> None:
        self._write(200, 'text/plain; charset=utf-8', 'OK')

    def _respond_index(self) -> None:
        # Content-Type 必须带 charset=utf-8：否则浏览器按本地编码（如 GBK）
        # 解读 UTF-8 字节，页面里的中文全成乱码。页面里的 <meta charset>
        # 只是兜底（文件被另存后本地打开时也靠它）。
        self._write(200, 'text/html; charset=utf-8', _INDEX_HTML)

    def _respond_not_found(self) -> None:
        self._write(404, 'text/plain', 'Not Found\n')

    def handle_one_request(self) -> None:
        """统一收口客户端断连。

        Prometheus 抓取超时或主动取消时，可能在响应完全发出前就断开 socket。
        基类除了 `_write` 外，`send_error` 等路径也会写 socket，三者可能抛出
        BrokenPipeError / ConnectionResetError / ConnectionAbortedError——
        它们同属 ConnectionError。连接已断则无响应可发，标记关闭即可，
        不应让异常冒泡到 socketserver.handle_error 刷满日志。
        """
        try:
            super().handle_one_request()
        except ConnectionError:
            self.close_connection = True

    def _write(self, status: int, content_type: str, body: str) -> None:
        if isinstance(body, str):
            body = body.encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            self.close_connection = True

    def log_message(self, format: str, *args) -> None:
        """自定义日志格式（单次 write，避免多线程下日志行交错）。

        立即 flush：stdout 重定向到文件时是块缓冲的，不 flush 的话日志会
        攒成 4KB 一批才落盘，排障时看不到最近的抓取记录。
        """
        try:
            line = f'[{self.log_date_time_string()}] {format % args}\n'
            sys.stdout.write(line)
            sys.stdout.flush()
        except Exception:
            pass


class MetricsExporter:
    """指标导出器 HTTP 服务器。"""

    def __init__(self, port: int = 9100, collectors: Iterable | None = None):
        self.port = port
        self.collectors = list(collectors) if collectors is not None \
            else [HostCollector(), GpuCollector()]
        self.server = None

    def _warn_gpu_unavailable(self) -> None:
        """GPU 采集不可用时给出一次性告警。

        这不是致命错误——主机侧指标照常上报——但如果不提示，运维看到
        `DCGM_FI_DEV_*` 一条都没有时无从判断是「这台机器没 GPU」还是
        「采集器装坏了」。
        """
        if PYNVML_AVAILABLE:
            return
        reason = f"：{PYNVML_IMPORT_ERROR}" if PYNVML_IMPORT_ERROR else ""
        print(
            f'[WARN] pynvml 不可用{reason}，GPU 指标（DCGM_FI_DEV_*）将全部缺失；'
            f'主机侧 node_* 指标不受影响。'
        )
        print('[WARN] 如需 GPU 指标，请执行: pip install nvidia-ml-py')

    def start(self) -> None:
        """启动 HTTP 服务器（阻塞直到 stop() 或 Ctrl-C）。"""
        self._warn_gpu_unavailable()
        self.server = MetricsServer(("", self.port), MetricsHandler, self.collectors)
        print(f'Metrics exporter started on http://:{self.port}/metrics')
        print(f'Prometheus 抓取地址: http://localhost:{self.port}/metrics')
        self.server.serve_forever()

    def stop(self) -> None:
        """停止 HTTP 服务器。"""
        if self.server:
            self.server.shutdown()
            print('Metrics exporter stopped')


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='Linux Metrics Exporter')
    parser.add_argument('--port', type=int, default=9100, help='HTTP 端口 (默认: 9100)')
    args = parser.parse_args()

    exporter = MetricsExporter(port=args.port)
    try:
        exporter.start()
    except KeyboardInterrupt:
        exporter.stop()
