"""Community stock reports.

Neighbors can say "this store has it / is low / is out / is closed". Reports
expire after REPORT_TTL_S and override the (older) inventory snapshot in
engine.stock_of(). In AWS the reports live in DynamoDB with a TTL attribute;
locally they live in memory (optionally mirrored to a JSON file).
"""
import json
import os
import threading
import time

REPORT_TTL_S = 6 * 3600
VALID_STATUS = {"in_stock", "low", "out", "closed"}


class LocalReportStore:
    def __init__(self, path=None):
        self._lock = threading.Lock()
        self._path = path
        self._data = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    for row in json.load(f):
                        self._data[(row["store_id"], row["item_id"])] = {"status": row["status"], "ts": row["ts"]}
            except (OSError, ValueError, KeyError):
                self._data = {}

    def put(self, store_id, item_id, status, ts=None):
        ts = int(ts or time.time())
        with self._lock:
            self._data[(store_id, item_id)] = {"status": status, "ts": ts}
            self._flush()

    def all_recent(self, now_ts=None):
        now_ts = now_ts or time.time()
        with self._lock:
            return {k: dict(v) for k, v in self._data.items() if now_ts - v["ts"] <= REPORT_TTL_S}

    def _flush(self):
        if not self._path:
            return
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            rows = [{"store_id": k[0], "item_id": k[1], **v} for k, v in self._data.items()]
            with open(self._path, "w", encoding="utf-8") as f:
                json.dump(rows, f)
        except OSError:
            pass  # reports are best-effort


class DynamoReportStore:
    CACHE_S = 10

    def __init__(self, table_name):
        import boto3  # imported lazily so local mode needs no AWS SDK
        self._table = boto3.resource("dynamodb").Table(table_name)
        self._cache = (0.0, {})

    def put(self, store_id, item_id, status, ts=None):
        ts = int(ts or time.time())
        self._table.put_item(Item={
            "store_id": store_id, "item_id": item_id, "status": status,
            "ts": ts, "ttl": ts + REPORT_TTL_S,
        })
        self._cache = (0.0, {})

    def all_recent(self, now_ts=None):
        now_ts = now_ts or time.time()
        fetched_at, data = self._cache
        if now_ts - fetched_at < self.CACHE_S and data:
            return data
        from boto3.dynamodb.conditions import Attr
        cutoff = int(now_ts - REPORT_TTL_S)
        kwargs = {"FilterExpression": Attr("ts").gt(cutoff)}
        out = {}
        while True:
            page = self._table.scan(**kwargs)
            for row in page.get("Items", []):
                out[(row["store_id"], row["item_id"])] = {"status": row["status"], "ts": int(row["ts"])}
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        self._cache = (now_ts, out)
        return out


_store = None


def get_store():
    global _store
    if _store is None:
        table = os.environ.get("REPORTS_TABLE")
        _store = DynamoReportStore(table) if table else LocalReportStore(os.environ.get("REPORTS_FILE") or None)
    return _store


def set_store(s):
    """Test hook."""
    global _store
    _store = s
