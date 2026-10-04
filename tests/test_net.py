import threading
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ctransfer import net


def test_post_file_sends_policy_fields_then_the_file(tmp_path):
    received = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            msg = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {self.headers['Content-Type']}\r\n\r\n".encode() + body)
            received["parts"] = [(p.get_param("name", header="content-disposition"), p.get_payload(decode=True))
                                 for p in msg.iter_parts()]
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    path = tmp_path / "b.tgz"
    path.write_bytes(bytes(range(256)) * 5000)
    try:
        net.post_file({"url": f"http://127.0.0.1:{server.server_port}/", "fields": {"key": "bundles/t/push.tgz", "policy": "cG9s"}}, path)
    finally:
        server.shutdown()
    # S3 requires the file to be the last field, after the policy it is checked against.
    assert received["parts"] == [("key", b"bundles/t/push.tgz"), ("policy", b"cG9s"), ("file", path.read_bytes())]
