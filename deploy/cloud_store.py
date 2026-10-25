"""云端 CL1 月度快照与 24 小时请求去重，独立于本地统计库。"""

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from deploy.cloud_contract import cl1_metrics, cl1_request, timestamp


class SnapshotConflict(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__('同一请求 ID 的正文发生变化' if code == 'REQUEST_CONFLICT' else '旧快照不能覆盖新快照')


class SnapshotStore:
    """每次连接一个事务：快照和回执一同提交，提交失败不确认接受。"""

    def __init__(self, database, *, clock=None):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with closing(self._connect()) as connection, connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS snapshots (
                    device_id TEXT NOT NULL, instance_id TEXT NOT NULL, month TEXT NOT NULL,
                    collected_at REAL NOT NULL, snapshot TEXT NOT NULL,
                    PRIMARY KEY (device_id, instance_id, month)
                );
                CREATE TABLE IF NOT EXISTS requests (
                    request_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                    expires_at REAL NOT NULL, receipt TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requests_expiration ON requests(expires_at);
            ''')

    def _connect(self):
        connection = sqlite3.connect(self.database, timeout=15)
        connection.execute('PRAGMA synchronous=FULL')
        return connection

    def accept(self, payload):
        cl1_request(payload)
        canonical = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        now = self.clock()
        collected = timestamp(payload['collected_at'], 'collected_at').timestamp()
        key = (payload['device_id'], payload['instance_id'], payload['month'])
        with closing(self._connect()) as connection, connection:
            # 在读去重记录之前取得写锁，防止并发请求重复确认或覆盖较新快照。
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('DELETE FROM requests WHERE expires_at <= ?', (now.timestamp(),))
            previous = connection.execute('SELECT digest, receipt FROM requests WHERE request_id = ?',
                                          (payload['request_id'],)).fetchone()
            if previous:
                if previous[0] != digest:
                    raise SnapshotConflict('REQUEST_CONFLICT')
                return {**json.loads(previous[1]), 'duplicate': True}
            latest = connection.execute(
                'SELECT collected_at FROM snapshots WHERE device_id = ? AND instance_id = ? AND month = ?', key
            ).fetchone()
            if latest and collected < latest[0]:
                raise SnapshotConflict('STALE_SNAPSHOT')
            snapshot = {**{key: value for key, value in payload.items() if key != 'request_id'}, **cl1_metrics(payload)}
            receipt = {'schema_version': 2, 'request_id': payload['request_id'], 'accepted': True, 'duplicate': False,
                       'received_at': now.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z'),
                       'snapshot': snapshot}
            connection.execute('INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?, ?, ?)',
                               (*key, collected, json.dumps(snapshot, ensure_ascii=False)))
            connection.execute('INSERT INTO requests VALUES (?, ?, ?, ?)',
                               (payload['request_id'], digest, now.timestamp() + 24 * 3600,
                                json.dumps(receipt, ensure_ascii=False)))
        return receipt
