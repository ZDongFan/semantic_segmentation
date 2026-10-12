# -*- coding: utf-8 -*-
"""不引入第三方依赖的内部服务客户端，网络调用由项目页放到工作线程。"""

import json
import configparser
from pathlib import Path
from contextlib import closing
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request


class ProjectClientError(RuntimeError):
    """内部服务拒绝请求或连接失败。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止内部请求被重定向到其他端点。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ProjectClient:
    """只连接显式配置的内部服务。"""

    def __init__(self, base_url):
        parsed = urllib.parse.urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("请输入有效的内部 HTTP(S) 服务地址。")
        self.base_url = base_url.rstrip("/")
        self.opener = urllib.request.build_opener(_NoRedirect())

    @classmethod
    def from_config(cls):
        """从插件部署配置读取地址，界面不暴露后端配置或凭据。"""
        path = Path(os.environ.get("LCC_PLUGIN_CONFIG", Path(__file__).with_name("project_service.ini")))
        config = configparser.ConfigParser(interpolation=None)
        if not config.read(str(path), encoding="utf-8"):
            raise ProjectClientError("项目服务配置缺失，请联系管理员。")
        return cls(config.get("service", "url"))

    def request_on_exit(self, path, upload, timeout_ms=5000):
        """退出时用 Qt 流式上传；单次总期限到达即取消，不重试。"""
        from qgis.PyQt.QtCore import QEventLoop, QFile, QIODevice, QTimer, QUrl, Qt
        from qgis.PyQt.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply

        manager = QNetworkAccessManager()
        request = QNetworkRequest(QUrl(self.base_url + path))
        request.setAttribute(QNetworkRequest.RedirectPolicyAttribute,
                             QNetworkRequest.ManualRedirectPolicy)
        request.setHeader(QNetworkRequest.ContentTypeHeader, "application/geopackage+sqlite3")
        stream = QFile(upload)
        if not stream.open(QIODevice.ReadOnly):
            raise ProjectClientError("无法打开退出保存快照。")
        request.setHeader(QNetworkRequest.ContentLengthHeader, stream.size())
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.setTimerType(Qt.PreciseTimer)
        expired = False
        reply = None

        def timeout():
            nonlocal expired
            expired = True
            if reply is not None:
                reply.abort()
            loop.quit()

        try:
            timer.timeout.connect(timeout)
            timer.start(timeout_ms)
            reply = manager.put(request, stream)
            reply.finished.connect(loop.quit)
            if not reply.isFinished():
                loop.exec_()
            if expired or not reply.isFinished():
                raise ProjectClientError("退出保存超时或被取消，未确认保存成功。")
            status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
            body = bytes(reply.readAll())
            if status is None or reply.error() != QNetworkReply.NoError or not 200 <= int(status) < 300:
                try:
                    detail = json.loads(body).get("detail", "请求被拒绝")
                except (ValueError, AttributeError):
                    detail = "连接失败或传输中断"
                raise ProjectClientError("退出保存未确认成功：{} {}".format(status or "", detail), status)
            try:
                result = json.loads(body)
                if not isinstance(result, dict) or not result.get("draft_id"):
                    raise ValueError()
                return result
            except ValueError:
                raise ProjectClientError("退出保存响应无效，未确认保存成功。") from None
        finally:
            timer.stop()
            if reply is not None:
                if not reply.isFinished():
                    reply.abort()
                reply.close()
                reply.deleteLater()
            stream.close()
            manager.deleteLater()

    def request(self, method, path, payload=None, upload=None, download=None):
        """文件流式传输；只在当前线程使用连接，不把凭据写入日志。"""
        headers = {}
        stream = None
        data = None
        try:
            if upload:
                stream = open(upload, "rb")
                data = stream
                headers.update({"Content-Type": "application/geopackage+sqlite3",
                                "Content-Length": str(os.path.getsize(upload))})
            elif payload is not None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                headers["Content-Type"] = "application/json"
            request = urllib.request.Request(self.base_url + path, data=data,
                                             headers=headers, method=method)
            with self.opener.open(request, timeout=120) as response:
                if download:
                    with open(download, "wb") as destination:
                        while True:
                            block = response.read(1024 * 1024)
                            if not block:
                                break
                            destination.write(block)
                    # HTTP 头名称不区分大小写，保留 HTTPMessage 的查询语义。
                    return response.headers
                return json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read(65536)).get("detail", "请求被拒绝")
            except (ValueError, AttributeError):
                message = "请求被拒绝"
            raise ProjectClientError("内部服务 {}: {}".format(exc.code, message), exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProjectClientError("无法连接内部服务或传输中断；草稿保留，可重试。") from None
        finally:
            if stream:
                stream.close()


def snapshot_gpkg(source, destination):
    """使用 SQLite 在线备份合并 WAL，避免直接复制未检查点的草稿。"""
    from pathlib import Path
    with closing(sqlite3.connect(Path(source).resolve().as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
