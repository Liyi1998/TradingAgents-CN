"""Optional CPU-only OpenAI embedding endpoint. Install fastembed separately.

Reads a private JSON config path from argv. No chat models, remote inference or
request-text logging. Long text is pooled across 400-character chunks, not
silently truncated to the transformer's token window.
"""
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading

import numpy as np
from fastembed import TextEmbedding


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    os.nice(10)
    model = TextEmbedding(config["model"], cache_dir=config["cache_dir"], threads=2,
                          local_files_only=True)
    list(model.embed(["就绪"]))
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_json(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == "/health":
                return self.send_json(200, {"ready": True, "model": config["model"], "dimension": 384})
            self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/embeddings":
                return self.send_json(404, {"error": "not found"})
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + config["token"]):
                return self.send_json(401, {"error": "unauthorized"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 300000:
                    raise ValueError()
                request = json.loads(self.rfile.read(size))
                texts = request["input"]
                if isinstance(texts, str):
                    texts = [texts]
                if (request["model"] != config["model"] or request.get("dimensions", 384) != 384
                        or not isinstance(texts, list) or not 1 <= len(texts) <= 32
                        or not all(isinstance(t, str) and t.strip() for t in texts)
                        or sum(map(len, texts)) > 24000):
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                return self.send_json(400, {"error": "invalid model, dimension or input (max 24000 characters)"})
            if not lock.acquire(timeout=2):
                return self.send_json(503, {"error": "embedding worker busy; optional enrichment may be skipped"})
            try:
                chunks, spans = [], []
                for text in texts:
                    start = len(chunks)
                    chunks.extend(text[i:i + 400] for i in range(0, len(text), 400))
                    spans.append((start, len(chunks)))
                vectors = list(model.embed(chunks, batch_size=8))
                rows = []
                for index, (start, end) in enumerate(spans):
                    vector = np.mean(vectors[start:end], axis=0)
                    vector = vector / max(float(np.linalg.norm(vector)), 1e-12)
                    rows.append({"object": "embedding", "index": index, "embedding": vector.tolist()})
                self.send_json(200, {"object": "list", "model": config["model"], "data": rows,
                                     "usage": {"prompt_tokens": 0, "total_tokens": 0}})
            except Exception:
                self.send_json(503, {"error": "local embedding unavailable"})
            finally:
                lock.release()

    server = ThreadingHTTPServer((config.get("bind", "127.0.0.1"), config["port"]), Handler)
    server.daemon_threads = True
    print("local embedding ready", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
