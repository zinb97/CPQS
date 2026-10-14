#!/usr/bin/env python3
"""
Linux Metrics HTTP Exporter
聚合多个采集器（host、gpu 等）并提供 Prometheus 指标抓取端点
"""

import http.server
from urllib.parse import urlparse

from host_collector import HostCollector
from gpu_collector import GpuCollector


_INDEX_HTML = '''<!DOCTYPE html>
<html><head><title>Linux Metrics Exporter</title></head>
<body><h1>Linux Metrics Exporter</h1>
<p><a href="/metrics">/metrics</a> - Prometheus 格式指标</p>
<p><a href="/health">/health</a> - 健康检查</p>
</body></html>'''


class MetricsHandler(http.server.BaseHTTPRequestHandler):
    """Prometheus 指标 HTTP 处理器"""

    collectors = ()

    @classmethod
    def set_collectors(cls, collectors):
        cls.collectors = tuple(collectors)

    def do_GET(self):
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

    def _respond_metrics(self):
        # 每个采集器忽略自己不支持的 kwargs；空串过滤掉
        parts = [c.to_prometheus().rstrip() for c in self.collectors]
        body = '\n'.join(p for p in parts if p) + '\n'
        self._write(200, 'text/plain; version=0.0.4; charset=utf-8', body)

    def _respond_health(self):
        self._write(200, 'text/plain', 'OK')

    def _respond_index(self):
        self._write(200, 'text/html', _INDEX_HTML)

    def _respond_not_found(self):
        self._write(404, 'text/plain', 'Not Found\n')

    def handle_one_request(self):
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

    def _write(self, status, content_type, body):
        if isinstance(body, str):
            body = body.encode('utf-8')
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            self.close_connection = True

    def log_message(self, format, *args):
        """自定义日志格式"""
        try:
            print(f'[{self.log_date_time_string()}] {format % args}')
        except ConnectionError:
            self.close_connection = True


class MetricsExporter:
    """指标导出器 HTTP 服务器"""

    def __init__(self, port=9100):
        self.port = port
        self.collectors = [HostCollector(), GpuCollector()]
        self.server = None

    def start(self):
        """启动 HTTP 服务器"""
        MetricsHandler.set_collectors(self.collectors)
        # 必须用多线程版：TCPServer 是单线程的，一个慢连接（Prometheus 抓取
        # 超时后仍在发数据）会阻塞后续所有抓取。ThreadingHTTPServer 自带
        # daemon_threads，且 allow_reuse_address 默认为真，重启不会被端口占用卡住。
        self.server = http.server.ThreadingHTTPServer(("", self.port), MetricsHandler)
        print(f'Metrics exporter started on http://:{self.port}/metrics')
        print(f'Prometheus 抓取地址: http://localhost:{self.port}/metrics')
        self.server.serve_forever()

    def stop(self):
        """停止 HTTP 服务器"""
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
