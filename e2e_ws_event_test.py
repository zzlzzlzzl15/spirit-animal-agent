#!/usr/bin/env python3
"""WS-level E2E: verify the full app event stream (deltas / tool events / complete)."""
import asyncio
import json
import sys

import websockets

QUERY = "帮我查看一下今日的财经新闻"


async def main() -> int:
    async with websockets.connect("ws://127.0.0.1:9877") as ws:
        await ws.send(json.dumps({
            "type": "command",
            "action": "chat",
            "id": "req-e2e-1",
            "data": {"message": QUERY},
        }))
        print(f"[SENT] chat: {QUERY}", flush=True)

        events = {"stream_delta": 0, "tool_start": 0, "tool_complete": 0}
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=300)
            msg = json.loads(raw)
            if msg.get("type") == "event":
                ev = msg.get("event") or (msg.get("data") or {}).get("event") or "?"
                data = msg.get("data") or {}
                if ev == "stream_delta":
                    events["stream_delta"] += 1
                    txt = data.get("text", "")
                    print(f"[EVENT] stream_delta #{events['stream_delta']}: {txt[:70]!r}", flush=True)
                elif ev in ("tool_start", "tool_complete"):
                    events[ev] += 1
                    print(f"[EVENT] {ev}: {json.dumps(data, ensure_ascii=False)[:160]}", flush=True)
                elif ev == "chat_complete":
                    print(f"[EVENT] chat_complete: {json.dumps(data, ensure_ascii=False)[:300]}", flush=True)
                elif ev == "init":
                    continue
                else:
                    print(f"[EVENT] {ev}: {json.dumps(data, ensure_ascii=False)[:120]}", flush=True)
            elif msg.get("type") == "response":
                payload = msg.get("data") or {}
                print(f"[RESPONSE] id={msg.get('id')}", flush=True)
                print(f"  tool_calls: {len(payload.get('tool_calls') or [])}", flush=True)
                for tc in payload.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    print(f"    - {fn.get('name')}({str(fn.get('arguments'))[:80]})", flush=True)
                resp = payload.get("response") or ""
                print(f"  response ({len(resp)} chars): {resp[:300]}", flush=True)
                break

        print("=" * 60, flush=True)
        print(f"SUMMARY: {events}", flush=True)
        ok = events["tool_start"] > 0 and events["tool_complete"] > 0
        print("VERDICT:", "PASS - tool events + loop verified" if ok else "FAIL - missing tool events", flush=True)
        return 0 if ok else 1


sys.exit(asyncio.run(main()))
