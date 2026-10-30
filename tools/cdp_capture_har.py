#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CDP 抓包 -> 标准 HAR 1.2（含 request body，不脱敏）。

用户指令(2026-10-30)：凭证/指纹/body 全部原样落盘，不脱敏。
response 只记 status/headers，不记 body。
实时落盘：每 2 秒刷新一次 HAR，进程被 kill 也最多丢 2 秒。
"""
import json
import os
import sys
import threading
import time
import urllib.request
from urllib.parse import urlparse, parse_qs

import websocket

OUT = sys.argv[1] if len(sys.argv) > 1 else "capture.har"


def headers_to_har(h):
    return [{"name": k, "value": v} for k, v in (h or {}).items()]


def main():
    targets = json.load(urllib.request.urlopen("http://127.0.0.1:9222/json"))
    page = next((t for t in targets if t.get("type") == "page"), None)
    if page is None:
        print("NO_PAGE_TARGET", file=sys.stderr, flush=True)
        sys.exit(1)
    ws_url = page["webSocketDebuggerUrl"]
    print(f"ATTACH {page.get('url','')}", file=sys.stderr, flush=True)

    entries = []
    by_id = {}
    lock = threading.Lock()
    dirty = threading.Event()

    def save_har(final=False):
        har = {
            "log": {
                "version": "1.2",
                "creator": {"name": "cdp_capture", "version": "1.0"},
                "pages": [],
                "entries": entries,
            }
        }
        os.makedirs(os.path.dirname(os.path.abspath(OUT)) or ".", exist_ok=True)
        tmp = OUT + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(har, f, ensure_ascii=False, indent=2)
        os.replace(tmp, OUT)
        print(f"HAR_SAVED entries={len(entries)}" + (" (final)" if final else ""),
              file=sys.stderr, flush=True)

    def flusher():
        while True:
            dirty.wait(timeout=2.0)
            with lock:
                if dirty.is_set():
                    dirty.clear()
                    try:
                        save_har()
                    except Exception as e:
                        print(f"SAVE_ERR {e}", file=sys.stderr, flush=True)

    threading.Thread(target=flusher, daemon=True).start()

    def on_open(ws):
        ws.send(json.dumps({
            "id": 1, "method": "Network.enable",
            "params": {
                "maxTotalBufferSize": 100000000,
                "maxResourceBufferSize": 50000000,
                "maxPostDataSize": 10 * 1024 * 1024,
            },
        }))
        print("CAPTURE_STARTED", file=sys.stderr, flush=True)

    def on_message(ws, message):
        try:
            msg = json.loads(message)
        except Exception:
            return
        m = msg.get("method")
        p = msg.get("params", {})
        if m == "Network.requestWillBeSent":
            req = p.get("request", {})
            rid = p.get("requestId")
            hdrs = req.get("headers", {})
            ctype = hdrs.get("content-type") or hdrs.get("Content-Type") or ""
            post_data = None
            pd = req.get("postData")
            if pd:
                text = pd if isinstance(pd, str) else (pd.get("text") if isinstance(pd, dict) else "")
                post_data = {"mimeType": ctype, "text": text}
            parsed = urlparse(req.get("url", ""))
            qs = [{"name": k, "value": v[0] if isinstance(v, list) and len(v) == 1 else v}
                  for k, v in parse_qs(parsed.query, keep_blank_values=True).items()]
            entry = {
                "startedDateTime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "time": 0,
                "request": {
                    "method": req.get("method"),
                    "url": req.get("url"),
                    "httpVersion": req.get("httpVersion") or "HTTP/1.1",
                    "headers": headers_to_har(hdrs),
                    "queryString": qs,
                    "cookies": [],
                    "headersSize": -1,
                    "bodySize": -1,
                    "postData": post_data,
                },
                "response": {},
                "cache": {},
                "timings": {"send": 0, "wait": 0, "receive": 0},
            }
            with lock:
                entries.append(entry)
                by_id[rid] = entry
                dirty.set()
            print(f"REQ {req.get('method')} {str(req.get('url'))[:100]}", file=sys.stderr, flush=True)
        elif m == "Network.responseReceived":
            resp = p.get("response", {})
            rid = p.get("requestId")
            with lock:
                entry = by_id.get(rid)
                if entry:
                    entry["response"] = {
                        "status": resp.get("status"),
                        "statusText": resp.get("statusText"),
                        "httpVersion": resp.get("httpVersion") or "HTTP/1.1",
                        "headers": headers_to_har(resp.get("headers", {})),
                        "cookies": [],
                        "content": {"size": 0, "mimeType": resp.get("mimeType", "")},
                        "redirectURL": resp.get("redirectURL") or "",
                        "headersSize": -1,
                        "bodySize": -1,
                    }
                    dirty.set()

    def on_close(ws, code, msg):
        with lock:
            dirty.clear()
            try:
                save_har(final=True)
            except Exception as e:
                print(f"SAVE_ERR {e}", file=sys.stderr, flush=True)

    ws = websocket.WebSocketApp(ws_url, on_open=on_open, on_message=on_message, on_close=on_close)
    ws.run_forever()


if __name__ == "__main__":
    main()
