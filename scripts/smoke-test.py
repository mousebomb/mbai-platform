"""端到端冒烟测试：健康检查 → 提交任务 → 轮询 → 打印产物与耗时。"""

import sys
import time

import httpx

BASE = "http://127.0.0.1:8000"


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=30) as c:
        health = c.get("/v1/health").json()
        print(f"引擎: {health['engine']['ok']}  变体数: {health['variants']}")
        if not health["engine"]["ok"]:
            print(f"引擎不可用: {health['engine'].get('error')}")
            return 1
        if health["registry_errors"]:
            print(f"注册表错误: {health['registry_errors']}")
            return 1

        req = {
            "capability": "image.txt2img",
            "variant": sys.argv[1] if len(sys.argv) > 1 else "moss-image-sft",
            "params": {
                "prompt": "一只戴墨镜的柴犬坐在赛博朋克霓虹街头，电影感，浅景深",
                "width": 1024,
                "height": 1024,
                "steps": 25,
            },
        }
        started = time.time()
        job = c.post("/v1/jobs", json=req).json()
        print(f"任务 {job['id']} 已入队")

        while True:
            job = c.get(f"/v1/jobs/{job['id']}").json()
            if job["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(2)

        elapsed = time.time() - started
        print(f"状态 {job['state']}  总耗时 {elapsed:.1f}s")
        if job["state"] != "succeeded":
            print(f"错误: {job['error']}")
            return 1
        for a in job["artifacts"]:
            print(f"产物: {a['path']} {a['width']}x{a['height']}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
