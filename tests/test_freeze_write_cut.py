"""冻写切断（freeze-write cut）失败测。

口径：甲路径——冻结源入库整笔锁行拒插、记审计、正式零增。
本文件先落红测，再改落库；不得只改提示。
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app as appmod
from app import app, init_db

FROZEN_SOURCE = "物流源"      # 种子数据 output_mode=冻结
WRITABLE_SOURCE = "订单源"    # 种子数据 output_mode=可写


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "DB_PATH", tmp_path / "freeze-cut.db")
    appmod.SESSIONS.clear()
    init_db()
    yield


def login(client: TestClient, username: str = "admin", password: str = "123456") -> dict:
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["token"]}


def db_count(sql: str, args: tuple = ()) -> int:
    with appmod.connect() as c:
        return c.execute(sql, args).fetchone()["c"]


def snapshot(client: TestClient, headers: dict) -> dict:
    arrivals = client.get("/api/arrivals", headers=headers).json()["items"]
    sources = client.get("/api/sources", headers=headers).json()["items"]
    catchups = client.get("/api/catchups", headers=headers).json()["items"]
    return {
        "arrivals": len(arrivals),
        "sources": {s["name"]: s["output_mode"] for s in sources},
        "catchups": len(catchups),
    }


def arrival_payload(lag_minutes: int = 5) -> dict:
    """物流源 SLA=30；lag=5 本应算出“正常/可写”。

    冻结徽标仍在时必须先切断，不得借这笔记账把源改回可写。
    """
    from datetime import datetime, timedelta

    start = datetime(2026, 11, 16, 10, 0, 0)
    return {
        "source": FROZEN_SOURCE,
        "source_clock": start.isoformat(),
        "ingest_clock": (start + timedelta(minutes=lag_minutes)).isoformat(),
    }


def test_frozen_ingest_refused_and_formal_zero_increase():
    """冻结源入库：拒写、审计留痕、正式零增，且不是只改提示。"""
    with TestClient(app) as client:
        headers = login(client)
        before = snapshot(client, headers)
        before_rows = db_count("SELECT COUNT(*) c FROM arrivals WHERE source=?", (FROZEN_SOURCE,))
        before_watermark = client.get("/api/watermark", headers=headers).json()

        r = client.post("/api/arrivals", headers=headers, json=arrival_payload())

        # 插入成功即红：必须先看到拒绝，而不是 200。
        assert r.status_code == 423, r.text
        body = r.json()
        assert body["ok"] is False
        assert body["code"] == "FREEZE_REFUSED"
        assert body["inserted"] is False
        assert body["audit_action"] == "拒写"

        after = snapshot(client, headers)
        assert after["arrivals"] == before["arrivals"], "到达卡片不得新增"
        assert after["sources"][FROZEN_SOURCE] == "冻结", "源目录牌必须仍冻结"
        assert after["catchups"] == before["catchups"], "任务附页不得新增"
        assert db_count("SELECT COUNT(*) c FROM arrivals WHERE source=?", (FROZEN_SOURCE,)) == before_rows

        audit = client.get("/api/freeze-audit", headers=headers).json()["items"]
        assert len(audit) == 1
        assert audit[0]["source"] == FROZEN_SOURCE
        assert audit[0]["action"] == "拒写"
        assert audit[0]["mode"] == "冻结"

        after_watermark = client.get("/api/watermark", headers=headers).json()
        assert after_watermark["arrivals"] == before_watermark["arrivals"], "水位读数不得上涨"
        assert after_watermark["freeze_refused"] == before_watermark["freeze_refused"] + 1
        assert after_watermark["freeze_contention"] == before_watermark["freeze_contention"]


def test_five_surfaces_refuse_together():
    """源目录牌、到达卡片、审计列表、任务附页、水位读数五处同拒。"""
    with TestClient(app) as client:
        headers = login(client)
        before = snapshot(client, headers)
        before_watermark = client.get("/api/watermark", headers=headers).json()

        r = client.post("/api/arrivals", headers=headers, json=arrival_payload(lag_minutes=120))
        assert r.status_code == 423, r.text

        # 1 源目录牌
        sources = client.get("/api/sources", headers=headers).json()["items"]
        frozen = next(s for s in sources if s["name"] == FROZEN_SOURCE)
        assert frozen["output_mode"] == "冻结"

        # 2 到达卡片
        arrivals = client.get("/api/arrivals", headers=headers).json()["items"]
        assert len(arrivals) == before["arrivals"]
        assert not any(a["source"] == FROZEN_SOURCE and a["id"] > 2 for a in arrivals)

        # 3 审计列表（拒写有痕，但不是到达流水）
        audit = client.get("/api/freeze-audit", headers=headers).json()["items"]
        assert [x["action"] for x in audit] == ["拒写"]

        # 4 任务附页
        tasks = client.get("/api/tasks", headers=headers).json()["items"]
        task = next(t for t in tasks if t["source"] == FROZEN_SOURCE)
        assert task["freeze_cut"] is True
        assert task["ingest_allowed"] is False

        # 5 水位读数
        after_watermark = client.get("/api/watermark", headers=headers).json()
        assert after_watermark["arrivals"] == before_watermark["arrivals"]
        assert after_watermark["sources"] == before_watermark["sources"]
        assert after_watermark["catchups"] == before_watermark["catchups"]


def test_two_seats_racing_frozen_ingest_record_contention(monkeypatch):
    """调度席与采集席同时打冻结源入库：后交只记冻写争用，正式仍零增。"""
    # 让首笔冻结锁行驻留足够久，双线程抢入必有一笔争用留痕。
    monkeypatch.setattr(appmod, "FREEZE_LOCK_HOLD_SECONDS", 1.0)

    with TestClient(app) as client:
        dispatcher = login(client, "admin", "123456")
        collector = login(client, "viewer", "view123")
        before = snapshot(client, dispatcher)
        barrier = threading.Barrier(2)

        def seat_post(headers: dict):
            seat_client = TestClient(app)
            barrier.wait(timeout=5)
            r = seat_client.post("/api/arrivals", headers=headers, json=arrival_payload())
            return r.status_code, r.json()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(seat_post, dispatcher), pool.submit(seat_post, collector)]
            results = [f.result(timeout=15) for f in futures]

        assert [status for status, _ in results] == [423, 423], results
        codes = {body["code"] for _, body in results}
        assert codes == {"FREEZE_REFUSED", "FREEZE_CONTENTION"}, results

        after = snapshot(client, dispatcher)
        assert after["arrivals"] == before["arrivals"], "双席抢入也不得新增到达"
        assert after["sources"][FROZEN_SOURCE] == "冻结"

        audit = client.get("/api/freeze-audit", headers=dispatcher).json()["items"]
        actions = sorted(x["action"] for x in audit)
        assert actions == ["冻写争用", "拒写"], "缺冻写争用留痕即红"

        watermark = client.get("/api/watermark", headers=dispatcher).json()
        assert watermark["freeze_refused"] == 1
        assert watermark["freeze_contention"] == 1
        assert watermark["arrivals"] == before["arrivals"]


def test_collection_ingest_still_accepted_for_writable_source():
    """采集入库可验收：非冻结源照常入库。"""
    with TestClient(app) as client:
        headers = login(client)
        before = client.get("/api/arrivals", headers=headers).json()["items"]
        r = client.post(
            "/api/arrivals",
            headers=headers,
            json={
                "source": WRITABLE_SOURCE,
                "source_clock": "2026-11-16T10:00:00",
                "ingest_clock": "2026-11-16T10:05:00",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["inserted"] is True
        after = client.get("/api/arrivals", headers=headers).json()["items"]
        assert len(after) == len(before) + 1
        assert client.get("/api/freeze-audit", headers=headers).json()["items"] == []


def test_catchup_unfreezes_then_ingest_is_allowed():
    """补齐解冻后，同一源可正常入库；冻写切断不是永久拒绝。"""
    with TestClient(app) as client:
        headers = login(client)
        assert client.post(f"/api/catchup/{FROZEN_SOURCE}", headers=headers).status_code == 200
        r = client.post("/api/arrivals", headers=headers, json=arrival_payload())
        assert r.status_code == 200, r.text
        assert r.json()["inserted"] is True
        assert r.json()["output_mode"] == "可写"
        sources = client.get("/api/sources", headers=headers).json()["items"]
        assert next(s for s in sources if s["name"] == FROZEN_SOURCE)["output_mode"] == "可写"


def test_card_deviation_blocks_ingest_and_leaves_mark(monkeypatch, tmp_path):
    """偏离冻写切断卡即驳回：留「冻写偏了」，任何源都不落库。"""
    card = tmp_path / "冻写切断卡.md"
    card.write_text("改口：甲乙口径并行\n", encoding="utf-8")
    seal = tmp_path / "seal.json"
    seal.write_text(
        '{"status":"钉死","paths":["甲","乙"],"sha256":"deadbeef"}', encoding="utf-8"
    )
    monkeypatch.setattr(appmod, "CARD_DOC", card)
    monkeypatch.setattr(appmod, "CARD_SEAL", seal)

    with TestClient(app) as client:
        headers = login(client)
        before = client.get("/api/arrivals", headers=headers).json()["items"]
        r = client.post(
            "/api/arrivals",
            headers=headers,
            json={
                "source": WRITABLE_SOURCE,
                "source_clock": "2026-11-16T10:00:00",
                "ingest_clock": "2026-11-16T10:05:00",
            },
        )
        assert r.status_code == 423, r.text
        assert r.json()["code"] == "FREEZE_CARD_DEVIATION"
        assert r.json()["inserted"] is False
        after = client.get("/api/arrivals", headers=headers).json()["items"]
        assert len(after) == len(before), "偏离切断卡时不得落库"
        audit = client.get("/api/freeze-audit", headers=headers).json()["items"]
        assert [x["action"] for x in audit] == ["冻写偏了"]


def test_card_seal_matches_pinned_ledger():
    """冻结当天钉死的专册仍在档，且只认甲路径。"""
    import json

    seal = json.loads(appmod.CARD_SEAL.read_text(encoding="utf-8"))
    assert seal["status"] == "钉死"
    assert seal["paths"] == ["甲"], "甲乙口径不得混用"
    assert appmod.card_deviation() is None, "切断专册被改口即红"
