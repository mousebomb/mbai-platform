"""Layout JSON 端到端验证：把版式对象当 prompt 提交，确认文字按 bbox 落位。

用法：
    uv run python scripts\\test-layout.py
    uv run python scripts\\test-layout.py http://<本机IP>:8000

约定（与 capabilities 里的 prompt 绑定一致）：
    bbox 坐标以画布中心为原点、像素为单位，顺序 yxyx，坐标空间 abs_centralized
"""

import json
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=30) as c:
        caps = c.get("/v1/capabilities").json()
        if not caps:
            print("没有注册任何能力变体，先跑 uv run mbai doctor")
            return 1
        for v in caps:
            print(f"{v['capability']}/{v['variant']} <- {v['model_id']}")

        # 三行文字，各自给死 bbox，用来肉眼核对落位
        layout = {
            "summary": {
                "caption": "A minimal typographic jazz poster, deep navy background, "
                "thin gold rules, quiet editorial mood"
            },
            "items": [
                {"label": "text", "text": "NOCTURNE", "bbox": [-470, -420, -330, 420]},
                {"label": "text", "text": "JAZZ NIGHT", "bbox": [-250, -300, -190, 300]},
                {"label": "text", "text": "SATURDAY 8 PM", "bbox": [400, -420, 450, 420]},
            ],
            "bbox_coord_space": "abs_centralized",
            "bbox_format": "yxyx",
        }

        job = c.post(
            "/v1/jobs",
            json={
                "capability": "image.txt2img",
                "variant": "moss-image-sft",
                "params": {"prompt": layout, "width": 1024, "height": 1024, "steps": 25},
            },
        ).json()
        print(f"任务 {job['id']} 入队")

        while True:
            job = c.get(f"/v1/jobs/{job['id']}").json()
            if job["state"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(3)

        cost = f"{job['finished_at'] - job['started_at']:.1f}s" if job["finished_at"] else "-"
        print(f"状态 {job['state']}  耗时 {cost}")
        if job["state"] != "succeeded":
            print(f"错误: {job.get('error')}")
            return 1

        for a in job["artifacts"]:
            name = a["path"].split("\\")[-1]
            print(f"产物 {a['path']}  {a['width']}x{a['height']}")
            print(f"      {BASE}/v1/jobs/{job['id']}/file/{name}")
        # 打出来方便确认「对象确实序列化成了 Layout JSON，而不是被当成普通字符串」
        print("实际提交的 prompt:", json.dumps(job["params"]["prompt"], ensure_ascii=False)[:160], "...")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
