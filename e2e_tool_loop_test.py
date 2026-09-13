#!/usr/bin/env python3
"""E2E test: full tool-call loop (LLM -> tool -> loop -> summary).

Replicates the exact agent construction of _launcher.py, then sends the
real user query and prints every stage: deltas, tool events, iterations.
"""
import logging
import sys

sys.path.insert(0, r"e:\个人文件夹\agent学习\hermes\spirit-agent-main")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)

from spirit.agent.agent import AgentConfig, SpiritAgent
from spirit.config import load_config

raw = load_config()
llm_cfg = raw.get("llm", {})
config = AgentConfig(
    model=llm_cfg.get("model", ""),
    api_key=llm_cfg.get("api_key", ""),
    base_url=llm_cfg.get("base_url", ""),
    provider=llm_cfg.get("provider", "auto"),
    platform="cli",
)
stream_cfg = raw.get("streaming", {}) or {}
config.streaming_enabled = bool(stream_cfg.get("enabled", False))
print(f"[CFG] model={config.model} streaming={config.streaming_enabled}", flush=True)

agent = SpiritAgent(config)

delta_count = {"v": 0}


def cb(text):
    delta_count["v"] += 1
    print(f"[DELTA #{delta_count['v']}] {text!r}", flush=True)


def on_start(name, args):
    print(f"[TOOL_START] {name} args={str(args)[:120]}", flush=True)


def on_complete(name, result):
    print(f"[TOOL_COMPLETE] {name} -> {str(result)[:150]}", flush=True)


agent.on_tool_start = on_start
agent.on_tool_complete = on_complete

result = agent.chat("帮我查看一下今日的财经新闻", stream_callback=cb)

print("=" * 70, flush=True)
print(f"ITERATIONS: {result['iterations']}", flush=True)
print(f"DELTA_COUNT: {delta_count['v']}", flush=True)
print(f"TOOL_CALLS: {len(result['tool_calls'])}", flush=True)
for tc in result["tool_calls"]:
    fn = tc.get("function", {})
    print(f"  - {fn.get('name')}({str(fn.get('arguments'))[:100]})", flush=True)
print(f"RESPONSE:\n{result['response'][:800]}", flush=True)
