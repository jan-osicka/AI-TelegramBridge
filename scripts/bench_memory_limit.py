"""Benchmark fast-chat latency vs. memory size in the system prompt (run against the local llama server).
Set MEMORY_DIR (notes folder) and LOCAL_MODEL (model name); the server URL is URL below.
Usage: python scripts/bench_memory_limit.py [limit ...]   (default 0 8000 16000 32000 full)"""
import json, os, statistics, sys, time, urllib.request, uuid
from pathlib import Path
from telegram_bridge import chat as chatmod

URL = "http://127.0.0.1:8081/v1/chat/completions"
BRAIN = Path(os.environ.get("MEMORY_DIR", Path.home() / ".telegram-bridge" / "memory"))
PERSONA = "Your name is Qwen. Reply concisely."


def run(system, user="Ahoj, jak se mas?", max_tokens=16):
    body = {"model": os.environ.get("LOCAL_MODEL", "local-model"), "max_tokens": max_tokens, "stream": True,
            "chat_template_kwargs": {"enable_thinking": False},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json"})
    t0 = time.perf_counter(); ttft = None
    with urllib.request.urlopen(req, timeout=300) as r:
        for line in r:
            if line.startswith(b"data:") and b"[DONE]" not in line and ttft is None:
                d = json.loads(line[5:])
                if (d.get("choices") or [{}])[0].get("delta", {}).get("content"):
                    ttft = time.perf_counter() - t0
    return ttft or 0.0, time.perf_counter() - t0


def main():
    full = len(chatmod.brain_summary(BRAIN, 10**9))
    print(f"full memory: {full} chars (~{full // 3} tokens)")
    args = [int(a) if a != "full" else full for a in sys.argv[1:]] or [0, 8000, 16000, 32000, full]
    for lim in args:
        brain = chatmod.brain_summary(BRAIN, lim) if lim else ""
        if lim > full:  # pad with repeated brain to simulate a bigger brain
            brain = (brain + "\n\n") * (lim // max(full, 1) + 1)
            brain = brain[:lim]
        res = {}
        for kind in ("cold", "warm"):
            runs = []
            for _ in range(3):
                nonce = f"[{uuid.uuid4()}]\n" if kind == "cold" else ""
                runs.append(run(nonce + PERSONA + "\n\n" + brain))
            res[kind] = tuple(statistics.median(x[i] for x in runs) for i in (0, 1))
        print(f"limit={lim:>6} chars  cold ttft/total={res['cold'][0]:.2f}/{res['cold'][1]:.2f}s  "
              f"warm ttft/total={res['warm'][0]:.2f}/{res['warm'][1]:.2f}s", flush=True)


main()
