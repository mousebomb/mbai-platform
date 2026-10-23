"""查任务状态：python scripts/job-status.py <job_id>"""

import sys

import httpx

job_id = sys.argv[1]
j = httpx.get(f"http://127.0.0.1:8000/v1/jobs/{job_id}", timeout=30).json()
print(j["state"], j.get("error") or "")
if j.get("started_at") and j.get("finished_at"):
    print(f"耗时 {j['finished_at'] - j['started_at']:.1f}s")
for a in j["artifacts"]:
    print(a["path"])
