from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Iterator


DATA_DIR = Path(os.getenv("H3_DATA_DIR", "/data"))
DB = DATA_DIR / "tasks.sqlite3"

DEFAULT_MAX_CONCURRENCY = 4
MAX_MERCHANT_CONCURRENCY = 5
# 2K is intentionally configurable so pricing can be changed without a code
# release.  The default follows the existing linear 1/2/3 pt tiers.
PRICE_MILLI = {
    "720p": int(os.getenv("H3_PRICE_720P_MILLI", "1000")),
    "1080p": int(os.getenv("H3_PRICE_1080P_MILLI", "2000")),
    "2k": int(os.getenv("H3_PRICE_2K_MILLI", "3000")),
}
# Runtime failures never bill. Keep the legacy failure_price_milli column for
# existing rows, but never bill from it. User cancellation has separate rules.
FAILURE_PRICE_MILLI = {"720p": 0, "1080p": 0, "2k": 0}
INPUT_RETENTION_SECONDS = int(os.getenv("H3_INPUT_RETENTION_SECONDS", "86400"))
CONSECUTIVE_FAILURE_LIMIT = int(os.getenv("H3_CONSECUTIVE_FAILURE_LIMIT", "10"))


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB, timeout=30, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


@contextlib.contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    db = connect()
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS merchants (
              id TEXT PRIMARY KEY,
              username TEXT NOT NULL UNIQUE COLLATE NOCASE,
              password_hash TEXT NOT NULL,
              recovery_hash TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending',
              balance_milli INTEGER NOT NULL DEFAULT 0 CHECK(balance_milli >= 0),
              reserved_milli INTEGER NOT NULL DEFAULT 0 CHECK(reserved_milli >= 0),
              consecutive_failures INTEGER NOT NULL DEFAULT 0,
              max_concurrency INTEGER NOT NULL DEFAULT 4,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              activated_at REAL
            );

            CREATE TABLE IF NOT EXISTS sessions (
              token_hash TEXT PRIMARY KEY,
              principal_type TEXT NOT NULL,
              merchant_id TEXT,
              created_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              last_seen_at REAL NOT NULL,
              FOREIGN KEY(merchant_id) REFERENCES merchants(id)
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

            CREATE TABLE IF NOT EXISTS api_keys (
              id TEXT PRIMARY KEY,
              merchant_id TEXT NOT NULL,
              name TEXT NOT NULL,
              prefix TEXT NOT NULL,
              key_hash TEXT NOT NULL UNIQUE,
              status TEXT NOT NULL DEFAULT 'active',
              created_at REAL NOT NULL,
              last_used_at REAL,
              revoked_at REAL,
              FOREIGN KEY(merchant_id) REFERENCES merchants(id)
            );
            CREATE INDEX IF NOT EXISTS idx_api_keys_merchant ON api_keys(merchant_id);

            CREATE TABLE IF NOT EXISTS merchant_tasks (
              id TEXT PRIMARY KEY,
              merchant_id TEXT NOT NULL,
              kind TEXT NOT NULL,
              quality TEXT NOT NULL,
              duration INTEGER NOT NULL,
              ratio TEXT NOT NULL,
              title TEXT NOT NULL,
              status TEXT NOT NULL,
              progress INTEGER NOT NULL DEFAULT 0,
              message TEXT NOT NULL DEFAULT '',
              payload TEXT NOT NULL,
              input_objects TEXT NOT NULL DEFAULT '[]',
              artifacts TEXT NOT NULL DEFAULT '[]',
              remote_id TEXT,
              provider_started INTEGER NOT NULL DEFAULT 0,
              provider_billing REAL NOT NULL DEFAULT 0,
              price_milli INTEGER NOT NULL,
              failure_price_milli INTEGER NOT NULL,
              billing_status TEXT NOT NULL DEFAULT 'reserved',
              error TEXT NOT NULL DEFAULT '',
              idempotency_key TEXT,
              request_hash TEXT,
              created_at REAL NOT NULL,
              queued_at REAL NOT NULL,
              started_at REAL,
              finished_at REAL,
              input_expires_at REAL,
              output_expires_at REAL,
              cancel_requested INTEGER NOT NULL DEFAULT 0,
              portal_hidden_at REAL,
              portal_note TEXT NOT NULL DEFAULT '',
              FOREIGN KEY(merchant_id) REFERENCES merchants(id),
              UNIQUE(merchant_id, idempotency_key)
            );
            CREATE INDEX IF NOT EXISTS idx_merchant_tasks_status ON merchant_tasks(status, queued_at);
            CREATE INDEX IF NOT EXISTS idx_merchant_tasks_owner ON merchant_tasks(merchant_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS assets (
              id TEXT PRIMARY KEY,
              merchant_id TEXT NOT NULL,
              task_id TEXT NOT NULL,
              role TEXT NOT NULL,
              bucket TEXT NOT NULL,
              object_key TEXT NOT NULL UNIQUE,
              filename TEXT NOT NULL,
              content_type TEXT NOT NULL,
              size_bytes INTEGER,
              created_at REAL NOT NULL,
              expires_at REAL,
              purged_at REAL,
              FOREIGN KEY(merchant_id) REFERENCES merchants(id),
              FOREIGN KEY(task_id) REFERENCES merchant_tasks(id)
            );
            CREATE INDEX IF NOT EXISTS idx_assets_expiry ON assets(expires_at, purged_at);

            CREATE TABLE IF NOT EXISTS point_ledger (
              id TEXT PRIMARY KEY,
              merchant_id TEXT NOT NULL,
              task_id TEXT,
              event_type TEXT NOT NULL,
              available_delta_milli INTEGER NOT NULL,
              reserved_delta_milli INTEGER NOT NULL,
              charged_milli INTEGER NOT NULL DEFAULT 0,
              available_after_milli INTEGER NOT NULL,
              reserved_after_milli INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              actor TEXT NOT NULL DEFAULT 'system',
              created_at REAL NOT NULL,
              FOREIGN KEY(merchant_id) REFERENCES merchants(id),
              FOREIGN KEY(task_id) REFERENCES merchant_tasks(id)
            );
            CREATE INDEX IF NOT EXISTS idx_ledger_owner ON point_ledger(merchant_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS admin_audit (
              id TEXT PRIMARY KEY,
              action TEXT NOT NULL,
              merchant_id TEXT,
              detail TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS prompt_service_settings (
              id INTEGER PRIMARY KEY CHECK(id = 1),
              enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
              base_url TEXT NOT NULL DEFAULT 'https://api.deepseek.com',
              api_key_ciphertext TEXT NOT NULL DEFAULT '',
              key_hint TEXT NOT NULL DEFAULT '',
              updated_at REAL NOT NULL
            );

            INSERT OR IGNORE INTO prompt_service_settings
              (id,enabled,base_url,api_key_ciphertext,key_hint,updated_at)
            VALUES
              (1,0,'https://api.deepseek.com','','',0);

            -- Hypit-style durable editing projects.  Existing video tasks remain
            -- the execution primitive; these tables retain the editable project
            -- boundary, its analysis/timeline, and each reproducible Run.
            CREATE TABLE IF NOT EXISTS video_projects (
              id TEXT PRIMARY KEY,
              merchant_id TEXT NOT NULL,
              title TEXT NOT NULL,
              brief TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'draft',
              source_task_id TEXT,
              analysis_json TEXT NOT NULL DEFAULT '{}',
              timeline_json TEXT NOT NULL DEFAULT '{}',
              treatment_json TEXT NOT NULL DEFAULT '{}',
              current_run_id TEXT,
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              FOREIGN KEY(merchant_id) REFERENCES merchants(id),
              FOREIGN KEY(source_task_id) REFERENCES merchant_tasks(id)
            );
            CREATE INDEX IF NOT EXISTS idx_video_projects_owner
              ON video_projects(merchant_id, updated_at DESC);

            CREATE TABLE IF NOT EXISTS video_project_runs (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              task_id TEXT NOT NULL,
              run_number INTEGER NOT NULL,
              input_fingerprint TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'queued',
              selected_nodes_json TEXT NOT NULL DEFAULT '[]',
              result_json TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL,
              finished_at REAL,
              FOREIGN KEY(project_id) REFERENCES video_projects(id),
              FOREIGN KEY(task_id) REFERENCES merchant_tasks(id),
              UNIQUE(project_id, run_number)
            );
            CREATE INDEX IF NOT EXISTS idx_video_project_runs_project
              ON video_project_runs(project_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS video_project_nodes (
              id TEXT PRIMARY KEY,
              project_id TEXT NOT NULL,
              run_id TEXT,
              node_type TEXT NOT NULL,
              node_key TEXT NOT NULL,
              source_start REAL,
              source_end REAL,
              dependency_fingerprint TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'planned',
              output_json TEXT NOT NULL DEFAULT '{}',
              created_at REAL NOT NULL,
              updated_at REAL NOT NULL,
              FOREIGN KEY(project_id) REFERENCES video_projects(id),
              FOREIGN KEY(run_id) REFERENCES video_project_runs(id),
              UNIQUE(project_id, node_key)
            );
            CREATE INDEX IF NOT EXISTS idx_video_project_nodes_run
              ON video_project_nodes(project_id, run_id, node_type);

            -- Reusable reenactment templates: the reference understanding, Script and
            -- packaging of a finished task plus its pictures and voices, kept for a
            -- limited time so new prompts can adapt them instead of starting over.
            CREATE TABLE IF NOT EXISTS video_templates (
              id TEXT PRIMARY KEY,
              merchant_id TEXT NOT NULL,
              name TEXT NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              source_task_id TEXT NOT NULL,
              project_id TEXT NOT NULL,
              snapshot_json TEXT NOT NULL,
              uses INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL,
              expires_at REAL NOT NULL,
              FOREIGN KEY(merchant_id) REFERENCES merchants(id)
            );
            CREATE INDEX IF NOT EXISTS idx_video_templates_owner
              ON video_templates(merchant_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS schema_migrations (
              name TEXT PRIMARY KEY,
              applied_at REAL NOT NULL
            );
            """
        )
        db.executescript(
            f"""
            CREATE TRIGGER IF NOT EXISTS cap_merchant_concurrency_after_insert
            AFTER INSERT ON merchants
            WHEN NEW.max_concurrency > {MAX_MERCHANT_CONCURRENCY}
            BEGIN
              UPDATE merchants SET max_concurrency={MAX_MERCHANT_CONCURRENCY}
              WHERE id=NEW.id;
            END;
            CREATE TRIGGER IF NOT EXISTS cap_merchant_concurrency_after_update
            AFTER UPDATE OF max_concurrency ON merchants
            WHEN NEW.max_concurrency > {MAX_MERCHANT_CONCURRENCY}
            BEGIN
              UPDATE merchants SET max_concurrency={MAX_MERCHANT_CONCURRENCY}
              WHERE id=NEW.id;
            END;
            """
        )
        db.execute(
            "UPDATE merchants SET max_concurrency=? WHERE max_concurrency>?",
            (MAX_MERCHANT_CONCURRENCY, MAX_MERCHANT_CONCURRENCY),
        )
        migration = "merchant-default-concurrency-4"
        if not db.execute(
            "SELECT 1 FROM schema_migrations WHERE name=?", (migration,)
        ).fetchone():
            db.execute("UPDATE merchants SET max_concurrency=4 WHERE max_concurrency=2")
            db.execute(
                "INSERT INTO schema_migrations(name,applied_at) VALUES(?,?)",
                (migration, time.time()),
            )
        task_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(merchant_tasks)")
        }
        if "portal_hidden_at" not in task_columns:
            db.execute("ALTER TABLE merchant_tasks ADD COLUMN portal_hidden_at REAL")
        if "portal_note" not in task_columns:
            db.execute(
                "ALTER TABLE merchant_tasks ADD COLUMN portal_note TEXT NOT NULL DEFAULT ''"
            )
        db.execute(
            """CREATE INDEX IF NOT EXISTS idx_merchant_tasks_portal_visible
            ON merchant_tasks(merchant_id, portal_hidden_at, created_at DESC)"""
        )


def public_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def points(milli: int | float) -> float:
    return round(float(milli) / 1000, 3)


def merchant_view(row: sqlite3.Row | dict) -> dict:
    value = dict(row)
    return {
        "id": value["id"],
        "username": value["username"],
        "status": value["status"],
        "balance": points(value["balance_milli"]),
        "reserved": points(value["reserved_milli"]),
        "available": points(value["balance_milli"]),
        "consecutive_failures": value["consecutive_failures"],
        "max_concurrency": value["max_concurrency"],
        "created_at": value["created_at"],
        "activated_at": value["activated_at"],
    }


def _ledger(
    db: sqlite3.Connection,
    merchant_id: str,
    event_type: str,
    *,
    available_delta: int,
    reserved_delta: int,
    charged: int = 0,
    task_id: str | None = None,
    note: str = "",
    actor: str = "system",
) -> None:
    account = db.execute(
        "SELECT balance_milli, reserved_milli FROM merchants WHERE id=?", (merchant_id,)
    ).fetchone()
    db.execute(
        """INSERT INTO point_ledger
        (id,merchant_id,task_id,event_type,available_delta_milli,reserved_delta_milli,
         charged_milli,available_after_milli,reserved_after_milli,note,actor,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            public_id("led"), merchant_id, task_id, event_type, available_delta,
            reserved_delta, charged, account["balance_milli"], account["reserved_milli"],
            note[:500], actor[:120], time.time(),
        ),
    )


def refresh_key_state(db: sqlite3.Connection, merchant_id: str) -> None:
    account = db.execute(
        "SELECT status,balance_milli FROM merchants WHERE id=?", (merchant_id,)
    ).fetchone()
    if not account:
        return
    enabled = account["status"] == "active" and account["balance_milli"] >= PRICE_MILLI["720p"]
    db.execute(
        """UPDATE api_keys SET status=?
        WHERE merchant_id=? AND status IN ('active','suspended_balance','suspended_account')""",
        ("active" if enabled else ("suspended_balance" if account["status"] == "active" else "suspended_account"), merchant_id),
    )


def reserve_task(db: sqlite3.Connection, merchant_id: str, task_id: str, quality: str,
                 *, amount_milli: int | None = None) -> None:
    amount = PRICE_MILLI[quality] if amount_milli is None else amount_milli
    if amount < 0:
        raise ValueError("任务费用不能为负数")
    account = db.execute("SELECT * FROM merchants WHERE id=?", (merchant_id,)).fetchone()
    if not account or account["status"] != "active":
        raise PermissionError("账户尚未启用或已暂停")
    if account["balance_milli"] < amount:
        raise ValueError("积分不足")
    # The scheduler enforces per-merchant concurrency across all task types.
    # New requests stay queued until a slot becomes available.
    db.execute(
        "UPDATE merchants SET balance_milli=balance_milli-?,reserved_milli=reserved_milli+?,updated_at=? WHERE id=?",
        (amount, amount, time.time(), merchant_id),
    )
    _ledger(
        db, merchant_id, "task_reserve", available_delta=-amount,
        reserved_delta=amount, task_id=task_id, note=f"冻结 {quality} 任务费用",
    )
    refresh_key_state(db, merchant_id)


def settle_task(
    db: sqlite3.Connection,
    task_id: str,
    outcome: str,
    *,
    note: str = "",
) -> bool:
    task = db.execute("SELECT * FROM merchant_tasks WHERE id=?", (task_id,)).fetchone()
    if not task or task["billing_status"] != "reserved":
        return False
    full = int(task["price_milli"])
    if outcome in {"success", "cancel_after_start"}:
        charged = full
        refund = 0
        event = "task_success" if outcome == "success" else "task_cancel_after_start"
    elif outcome == "failure_after_start":
        charged = 0
        refund = full
        event = "task_failure"
    elif outcome in {"dispatch_failure", "cancel_before_start"}:
        charged = 0
        refund = full
        event = "task_release"
    else:
        raise ValueError(f"未知结算结果: {outcome}")
    account = db.execute(
        "SELECT reserved_milli FROM merchants WHERE id=?", (task["merchant_id"],)
    ).fetchone()
    if not account:
        raise RuntimeError(f"任务 {task_id} 对应的商户不存在")
    if int(account["reserved_milli"]) < full:
        raise RuntimeError(
            f"任务 {task_id} 冻结积分账实不一致: "
            f"账户冻结 {account['reserved_milli']}，任务需要结算 {full}"
        )
    db.execute(
        """UPDATE merchants SET balance_milli=balance_milli+?,
        reserved_milli=reserved_milli-?,updated_at=? WHERE id=?""",
        (refund, full, time.time(), task["merchant_id"]),
    )
    db.execute(
        "UPDATE merchant_tasks SET billing_status=? WHERE id=?",
        ("captured" if charged else "released", task_id),
    )
    _ledger(
        db, task["merchant_id"], event, available_delta=refund,
        reserved_delta=-full, charged=charged, task_id=task_id, note=note,
    )
    if outcome == "success":
        db.execute(
            "UPDATE merchants SET consecutive_failures=0 WHERE id=?", (task["merchant_id"],)
        )
    elif outcome == "failure_after_start":
        db.execute(
            "UPDATE merchants SET consecutive_failures=consecutive_failures+1 WHERE id=?",
            (task["merchant_id"],),
        )
        failures = db.execute(
            "SELECT consecutive_failures FROM merchants WHERE id=?", (task["merchant_id"],)
        ).fetchone()["consecutive_failures"]
        if failures >= CONSECUTIVE_FAILURE_LIMIT:
            db.execute(
                "UPDATE merchants SET status='suspended_failures',updated_at=? WHERE id=?",
                (time.time(), task["merchant_id"]),
            )
            queued = db.execute(
                "SELECT id FROM merchant_tasks WHERE merchant_id=? AND status='queued'",
                (task["merchant_id"],),
            ).fetchall()
            for pending in queued:
                expires = time.time() + INPUT_RETENTION_SECONDS
                db.execute(
                    "UPDATE merchant_tasks SET status='cancelled',message='账户连续失败，排队任务已释放',finished_at=?,input_expires_at=? WHERE id=?",
                    (time.time(), expires, pending["id"]),
                )
                db.execute("UPDATE assets SET expires_at=? WHERE task_id=? AND role='input'", (expires, pending["id"]))
                settle_task(db, pending["id"], "cancel_before_start", note="账户连续失败自动暂停")
    refresh_key_state(db, task["merchant_id"])
    return True


def audit(db: sqlite3.Connection, action: str, merchant_id: str | None, detail: dict) -> None:
    db.execute(
        "INSERT INTO admin_audit(id,action,merchant_id,detail,created_at) VALUES(?,?,?,?,?)",
        (public_id("aud"), action, merchant_id, json.dumps(detail, ensure_ascii=False), time.time()),
    )
