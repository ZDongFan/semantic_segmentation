"""内部客户端文件快照和凭据传输边界。"""

import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from land_cover_classification.project_client import ProjectClient, ProjectClientError, snapshot_gpkg


def test_snapshot_includes_wal(tmp_path):
    source, target = tmp_path / "source.gpkg", tmp_path / "snapshot.gpkg"
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE data(value TEXT)")
        db.execute("INSERT INTO data VALUES ('编辑结果')")
        db.commit()
        snapshot_gpkg(source, target)
        with sqlite3.connect(target) as snapshot:
            assert snapshot.execute("SELECT value FROM data").fetchone()[0] == "编辑结果"


def test_stream_upload_download_and_redirect(tmp_path):
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_PUT(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.headers.get("X-API-Key"), body))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"draft_id":"test"}')

        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/secret")
                self.end_headers()
            else:
                self.send_response(200)
                self.send_header("x-draft-id", "lowercase-header-id")
                self.end_headers()
                self.wfile.write(b"gpkg-data")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = ProjectClient("http://127.0.0.1:" + str(server.server_port))
        path = tmp_path / "draft.gpkg"
        path.write_bytes(b"test-body")
        assert client.request("PUT", "/draft", upload=path)["draft_id"] == "test"
        assert received == [(None, b"test-body")]
        headers = client.request("GET", "/file", download=tmp_path / "download.gpkg")
        assert headers["X-Draft-ID"] == "lowercase-header-id"
        assert (tmp_path / "download.gpkg").read_bytes() == b"gpkg-data"
        with pytest.raises(ProjectClientError) as error:
            client.request("GET", "/redirect")
        assert error.value.status == 302
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_reject_credential_url():
    with pytest.raises(ValueError):
        ProjectClient("http://user:password@host")
