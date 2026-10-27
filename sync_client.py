"""
Amazon Trend Hunter - Synchronization Client & Offline Queue
Connects trend_hunter_gui.pyw to Affiliate Landing Page Builder API
"""
import os
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
import urllib.request
import urllib.error

QUEUE_DB_PATH = Path(__file__).resolve().parent / "sync_queue.db"


class SyncClient:
    def __init__(self, api_base_url="http://127.0.0.1:8000", api_key=None):
        self.api_base_url = api_base_url.rstrip("/")
        self.api_key = api_key or os.getenv("API_SECRET_KEY", "amazon_trend_secret_token_2026")
        self._init_queue_db()

    def _init_queue_db(self):
        conn = sqlite3.connect(str(QUEUE_DB_PATH))
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS pending_sync (
            asin TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            last_attempt TEXT,
            error_message TEXT,
            created_at TEXT NOT NULL
        )
        """)
        conn.commit()
        conn.close()

    def check_health(self) -> dict:
        url = f"{self.api_base_url}/api/health"
        req = urllib.request.Request(url, headers={"X-API-Key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            return {"status": "offline", "error": str(e)}
        return {"status": "offline"}

    def _format_product_payload(self, p: dict, associate_tag: str) -> dict:
        """Handles both flat dictionaries and canonical nested schema"""
        now = datetime.now(timezone.utc).isoformat()
        asin = p.get("asin", "").strip().upper()

        # Support nested schema or flat fallback
        title = p.get("naming", {}).get("clean_title") or p.get("title", f"Product {asin}")
        category = p.get("site_category") or p.get("category", "General")
        
        comm = p.get("commercial", {})
        price = float(comm.get("price") if "price" in comm else p.get("price", 0.0))
        rating = float(comm.get("rating") if "rating" in comm else p.get("rating", 0.0))
        reviews = int(comm.get("review_count") if "review_count" in comm else p.get("reviews", 0))
        prime = bool(comm.get("prime_eligible") if "prime_eligible" in comm else p.get("prime", True))
        badge = str(comm.get("badge") if "badge" in comm else p.get("badge", "None"))

        editorial = p.get("editorial", {})
        hook = editorial.get("hero_hook") or p.get("hook", "")
        target_audience = editorial.get("target_audience") or p.get("target_audience", "")

        return {
            "marketplace": "amazon.com",
            "asin": asin,
            "title": str(title).strip(),
            "category": str(category),
            "price": price,
            "currency": "USD",
            "rating": rating,
            "review_count": reviews,
            "prime_eligible": prime,
            "badge": badge,
            "associate_tag": associate_tag or "prismeia-tw-20",
            "associate_commission_rate": float(p.get("comm_rate", 0.04)),
            "creator_eligible": bool(p.get("creator_eligible", False)),
            "creator_bonus_rate": float(p.get("creator_bonus", 0.0)),
            "marketing_hook": str(hook),
            "target_audience": str(target_audience),
            "source_type": "desktop_sync",
            "source_verified_at": now,
            "last_synced_at": now
        }

    def send_product_async(self, product_dict: dict, associate_tag: str, callback=None):
        """Runs network sync in background thread to avoid Tkinter UI freezing"""
        def _worker():
            res = self.send_product_sync(product_dict, associate_tag)
            if callback:
                callback(res)
        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

    def send_product_sync(self, product_dict: dict, associate_tag: str) -> dict:
        asin = product_dict.get("asin", "").strip().upper()
        payload = self._format_product_payload(product_dict, associate_tag)
        json_data = json.dumps(payload).encode("utf-8")

        url = f"{self.api_base_url}/api/products/sync"
        req = urllib.request.Request(
            url,
            data=json_data,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": self.api_key
            },
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=4) as response:
                if response.status in [200, 201]:
                    self._remove_from_queue(asin)
                    return {
                        "success": True,
                        "status": "synchronized",
                        "asin": asin,
                        "message": "Product synchronized successfully to Landing Page Builder!"
                    }
                else:
                    err_msg = f"Server returned unexpected HTTP status {response.status}"
                    self._enqueue_offline(asin, payload, err_msg)
                    return {
                        "success": False,
                        "status": "queued_offline",
                        "asin": asin,
                        "message": err_msg
                    }
        except Exception as ex:
            self._enqueue_offline(asin, payload, str(ex))
            return {
                "success": False,
                "status": "queued_offline",
                "asin": asin,
                "message": f"Builder API is offline. Product safely queued locally. (Error: {str(ex)})"
            }

    def _enqueue_offline(self, asin: str, payload: dict, error_msg: str):
        conn = sqlite3.connect(str(QUEUE_DB_PATH))
        cursor = conn.cursor()
        now = datetime.now(timezone.utc).isoformat()
        cursor.execute("""
        INSERT INTO pending_sync (asin, payload_json, attempts, last_attempt, error_message, created_at)
        VALUES (?, ?, 1, ?, ?, ?)
        ON CONFLICT(asin) DO UPDATE SET
            payload_json = excluded.payload_json,
            attempts = attempts + 1,
            last_attempt = excluded.last_attempt,
            error_message = excluded.error_message
        """, (asin, json.dumps(payload), now, error_msg, now))
        conn.commit()
        conn.close()

    def _remove_from_queue(self, asin: str):
        conn = sqlite3.connect(str(QUEUE_DB_PATH))
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_sync WHERE asin = ?", (asin,))
        conn.commit()
        conn.close()

    def get_queue_count(self) -> int:
        conn = sqlite3.connect(str(QUEUE_DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM pending_sync")
        count = cursor.fetchone()[0]
        conn.close()
        return count

    def flush_pending_queue(self) -> dict:
        conn = sqlite3.connect(str(QUEUE_DB_PATH))
        cursor = conn.cursor()
        cursor.execute("SELECT asin, payload_json, attempts FROM pending_sync")
        rows = cursor.fetchall()
        conn.close()

        if not rows:
            return {"synced": 0, "remaining": 0}

        synced_count = 0
        now = datetime.now(timezone.utc).isoformat()

        for asin, payload_json, attempts in rows:
            try:
                req = urllib.request.Request(
                    f"{self.api_base_url}/api/products/sync",
                    data=payload_json.encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "X-API-Key": self.api_key
                    },
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=4) as response:
                    if response.status in [200, 201]:
                        self._remove_from_queue(asin)
                        synced_count += 1
                    else:
                        self._update_queue_failure(asin, attempts + 1, f"HTTP {response.status}", now)
            except Exception as ex:
                self._update_queue_failure(asin, attempts + 1, str(ex), now)

        return {"synced": synced_count, "remaining": self.get_queue_count()}

    def _update_queue_failure(self, asin: str, attempts: int, error_msg: str, timestamp: str):
        conn = sqlite3.connect(str(QUEUE_DB_PATH))
        cursor = conn.cursor()
        cursor.execute("""
        UPDATE pending_sync 
        SET attempts = ?, last_attempt = ?, error_message = ?
        WHERE asin = ?
        """, (attempts, timestamp, error_msg, asin))
        conn.commit()
        conn.close()

    def get_associated_landing_page(self, asin: str) -> dict:
        url = f"{self.api_base_url}/api/products/{asin.strip().upper()}"
        req = urllib.request.Request(url, headers={"X-API-Key": self.api_key})
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    return {
                        "has_lander": bool(data.get("landing_page_slug")),
                        "slug": data.get("landing_page_slug"),
                        "status": data.get("landing_page_status"),
                        "live_url": data.get("landing_page_live_url"),
                        "lander_id": data.get("landing_page_id")
                    }
        except Exception:
            pass
        return {"has_lander": False}
