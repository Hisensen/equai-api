"""运营看板：任务流水的排序与筛选，以及每日聚合的时区边界。

按天聚合必须落在北京时间的日历天上。服务器跑在 UTC，如果直接按 UTC 分组，
每天早上 8 点前的任务会被算进前一天，日报就会对不上。
"""
from __future__ import annotations

import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone

import test_local_transfer  # noqa: F401  先设置隔离的数据库与存储，再导入应用
from fastapi.testclient import TestClient

from database import connect, init_db, public_id
from main import app

BEIJING = timezone(timedelta(hours=8))


def _seed(rows: list[dict]) -> str:
    """按给定的时间与状态造任务，返回商户 ID。"""
    merchant_id, now = public_id("mrc"), time.time()
    init_db()
    with connect() as db:
        db.execute(
            """INSERT INTO merchants(id,username,password_hash,recovery_hash,status,balance_milli,
            reserved_milli,consecutive_failures,max_concurrency,created_at,updated_at,activated_at)
            VALUES(?,?,?,?,'active',50000,0,0,4,?,?,?)""",
            (merchant_id, f"board_{merchant_id[-6:]}", "x", "x", now, now, now),
        )
        for row in rows:
            task_id = public_id("vid")
            created = row["created_at"]
            db.execute(
                """INSERT INTO merchant_tasks(id,merchant_id,kind,quality,duration,ratio,title,status,
                progress,message,payload,input_objects,price_milli,failure_price_milli,created_at,
                queued_at,started_at,finished_at,provider_billing)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, merchant_id, row.get("kind", "reference"), row.get("quality", "720p"), 15, "9:16",
                 "board", row.get("status", "completed"), 100, "", json.dumps(row.get("payload", {})), "[]",
                 1500, 100, created, created, created + 20, created + 200, row.get("cost", 0.5)),
            )
            if row.get("charged_milli"):
                db.execute(
                    """INSERT INTO point_ledger(id,merchant_id,task_id,event_type,available_delta_milli,
                    reserved_delta_milli,charged_milli,available_after_milli,reserved_after_milli,
                    note,actor,created_at) VALUES(?,?,?,'task_charge',0,0,?,0,0,'','system',?)""",
                    (public_id("led"), merchant_id, task_id, row["charged_milli"], created + 200),
                )
    return merchant_id


class AdminBoardTest(unittest.TestCase):
    def setUp(self) -> None:
        init_db()                                  # TestClient 不触发启动钩子，先自己建表
        self.client = TestClient(app)
        self.client.post("/api/admin/login", json={"password": os.environ["H3_ADMIN_PASSWORD"]},
                         headers={"X-Real-IP": "board-admin"}).raise_for_status()
        with connect() as db:                      # 每个用例从干净的任务表开始
            db.execute("DELETE FROM point_ledger")
            db.execute("DELETE FROM merchant_tasks")

    def test_tasks_come_back_newest_first(self) -> None:
        now = time.time()
        _seed([{"created_at": now - 3600}, {"created_at": now - 60}, {"created_at": now - 7200}])
        tasks = self.client.get("/api/admin/tasks").json()["tasks"]
        stamps = [task["created_at"] for task in tasks]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_filters_narrow_the_stream(self) -> None:
        now = time.time()
        _seed([
            {"created_at": now - 60, "status": "failed", "kind": "text"},
            {"created_at": now - 120, "status": "completed", "kind": "reference"},
            {"created_at": now - 180, "status": "completed", "kind": "reference",
             "payload": {"_tool": "remix"}},
        ])
        self.assertEqual(self.client.get("/api/admin/tasks?status=failed").json()["total"], 1)
        self.assertEqual(self.client.get("/api/admin/tasks?kind=reference").json()["total"], 2)
        tools = {t["tool"] for t in self.client.get("/api/admin/tasks?kind=reference").json()["tasks"]}
        self.assertEqual(tools, {"video", "remix"})
        self.assertEqual(self.client.get("/api/admin/tasks?merchant=不存在的商户").json()["total"], 0)

    def test_paging_walks_the_whole_stream_without_repeats(self) -> None:
        now = time.time()
        _seed([{"created_at": now - index * 60} for index in range(7)])
        first = self.client.get("/api/admin/tasks?limit=3&offset=0").json()
        second = self.client.get("/api/admin/tasks?limit=3&offset=3").json()
        self.assertEqual(first["total"], 7)
        self.assertEqual(len(first["tasks"]), 3)
        self.assertFalse({t["id"] for t in first["tasks"]} & {t["id"] for t in second["tasks"]})

    def test_since_days_excludes_older_tasks(self) -> None:
        now = time.time()
        _seed([{"created_at": now - 60}, {"created_at": now - 5 * 86400}])
        self.assertEqual(self.client.get("/api/admin/tasks?since_days=1").json()["total"], 1)
        self.assertEqual(self.client.get("/api/admin/tasks?since_days=30").json()["total"], 2)

    def test_daily_rows_group_on_beijing_days(self) -> None:
        # 北京时间某天 00:30 与 23:30 必须落在同一天；同一天 UTC 下会被拆成两天。
        day = datetime.now(BEIJING).replace(hour=0, minute=30, second=0, microsecond=0) - timedelta(days=1)
        _seed([{"created_at": day.timestamp()},
               {"created_at": (day + timedelta(hours=23)).timestamp()}])
        rows = self.client.get("/api/admin/stats/daily?days=7").json()["days"]
        matched = [row for row in rows if row["day"] == day.strftime("%Y-%m-%d")]
        self.assertEqual(len(matched), 1, rows)
        self.assertEqual(matched[0]["total"], 2)

    def test_daily_counts_costs_and_revenue(self) -> None:
        now = time.time()
        _seed([
            {"created_at": now - 300, "status": "completed", "cost": 0.25, "charged_milli": 1500},
            {"created_at": now - 600, "status": "failed", "cost": 0.10, "charged_milli": 100},
            {"created_at": now - 900, "status": "running", "cost": 0},
        ])
        today = datetime.now(BEIJING).strftime("%Y-%m-%d")
        row = next(r for r in self.client.get("/api/admin/stats/daily").json()["days"] if r["day"] == today)
        self.assertEqual((row["total"], row["completed"], row["failed"], row["active"]), (3, 1, 1, 1))
        self.assertAlmostEqual(row["cost"], 0.35, places=4)
        self.assertAlmostEqual(row["revenue"], 1.6, places=3)     # 1500 + 100 毫积分

    def test_the_board_is_closed_to_anonymous_visitors(self) -> None:
        anonymous = TestClient(app)
        self.assertEqual(anonymous.get("/api/admin/tasks").status_code, 401)
        self.assertEqual(anonymous.get("/api/admin/stats/daily").status_code, 401)


if __name__ == "__main__":
    unittest.main()
