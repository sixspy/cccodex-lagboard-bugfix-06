"""复现：冻结徽标仍亮，却继续收报文入库。

跑法（仓库根目录）：
    python scripts/repro_freeze_ingest.py

修复前：POST /api/arrivals 返回 200，正式到达 +1，源态被改回「可写」。
修复后：返回 423 FREEZE_REFUSED，正式到达零增，审计留「拒写」。
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def main() -> int:
    app.DB_PATH = Path(tempfile.mkdtemp()) / "repro.db"
    app.init_db()
    client = TestClient(app.app)
    token = client.post("/api/login", json={"username": "admin", "password": "123456"}).json()["token"]
    headers = {"Authorization": "Bearer " + token}

    frozen = next(s for s in client.get("/api/sources", headers=headers).json()["items"] if s["name"] == "物流源")
    before = client.get("/api/arrivals", headers=headers).json()["items"]
    print(f"复现前 物流源.output_mode = {frozen['output_mode']}，arrivals = {len(before)}")

    resp = client.post("/api/arrivals", headers=headers, json={
        "source": "物流源",
        "source_clock": "2026-11-16T10:00:00",
        "ingest_clock": "2026-11-16T10:05:00",
    })
    after = client.get("/api/arrivals", headers=headers).json()["items"]
    audit = client.get("/api/freeze-audit", headers=headers).json()["items"]
    print(f"POST /api/arrivals -> {resp.status_code} {resp.json()}")
    print(f"复现后 arrivals = {len(after)}，审计 = {[x['action'] for x in audit]}")

    if resp.status_code == 200 and len(after) == len(before) + 1:
        print("复现成功：冻结徽标下仍插入了正式到达行（缺陷在）")
        return 1
    if resp.status_code == 423 and len(after) == len(before) and [x["action"] for x in audit] == ["拒写"]:
        print("复现失败（已切断）：冻结源被拒写、审计留痕、正式零增")
        return 0
    print("结果不在预期口径内")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
