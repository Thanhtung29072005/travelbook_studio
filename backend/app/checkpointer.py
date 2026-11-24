"""SQLite implementation of LangGraph's public BaseCheckpointSaver protocol.

Stores complete typed checkpoints and per-task pending writes in the project DB.
No pickle fallback; deleting a project cascades to its checkpoints.
"""
from threading import RLock
from uuid import uuid4

from langgraph.checkpoint.base import BaseCheckpointSaver, CheckpointTuple, WRITES_IDX_MAP, get_checkpoint_metadata
from .checkpoint_codec import StateSerializer


class SQLiteCheckpointer(BaseCheckpointSaver):
    def __init__(self, db):
        super().__init__(serde=StateSerializer())
        self.db, self._lock, self._ready = db, RLock(), False

    def setup(self):
        with self._lock:
            if self._ready: return
            with self.db.connect() as connection:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS graph_checkpoints (
                        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                        thread_id TEXT NOT NULL,
                        namespace TEXT NOT NULL, checkpoint_id TEXT NOT NULL, parent_id TEXT,
                        checkpoint_type TEXT NOT NULL, checkpoint BLOB NOT NULL,
                        metadata_type TEXT NOT NULL, metadata BLOB NOT NULL,
                        PRIMARY KEY(thread_id, namespace, checkpoint_id));
                    CREATE TABLE IF NOT EXISTS graph_writes (
                        thread_id TEXT NOT NULL, namespace TEXT NOT NULL, checkpoint_id TEXT NOT NULL,
                        task_id TEXT NOT NULL, idx INTEGER NOT NULL, channel TEXT NOT NULL,
                        value_type TEXT NOT NULL, value BLOB NOT NULL, task_path TEXT NOT NULL,
                        PRIMARY KEY(thread_id, namespace, checkpoint_id, task_id, idx),
                        FOREIGN KEY(thread_id, namespace, checkpoint_id)
                          REFERENCES graph_checkpoints(thread_id, namespace, checkpoint_id) ON DELETE CASCADE);
                    CREATE TABLE IF NOT EXISTS graph_threads (
                        project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
                        thread_id TEXT NOT NULL);
                """)
            self._ready = True

    def thread_for(self, project_id):
        self.setup()
        with self.db.connect() as connection:
            row = connection.execute("SELECT thread_id FROM graph_threads WHERE project_id=?", (project_id,)).fetchone()
        return row[0] if row else project_id

    def fork_thread(self, project_id):
        self.setup()
        thread_id = project_id+":run:"+uuid4().hex
        with self.db.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO graph_threads VALUES(?,?)", (project_id, thread_id))
        return thread_id

    @staticmethod
    def _config(thread, namespace, checkpoint_id):
        return {"configurable": {"thread_id": thread, "checkpoint_ns": namespace, "checkpoint_id": checkpoint_id}}

    def get_tuple(self, config):
        self.setup()
        values = config["configurable"]
        thread, namespace = values["thread_id"], values.get("checkpoint_ns", "")
        query = "SELECT * FROM graph_checkpoints WHERE thread_id=? AND namespace=?"
        params = [thread, namespace]
        if values.get("checkpoint_id"):
            query += " AND checkpoint_id=?"; params.append(values["checkpoint_id"])
        with self.db.connect() as connection:
            row = connection.execute(query+" ORDER BY checkpoint_id DESC LIMIT 1", params).fetchone()
            if row is None: return None
            writes = connection.execute("SELECT task_id,channel,value_type,value FROM graph_writes WHERE thread_id=? AND namespace=? AND checkpoint_id=? ORDER BY task_id,idx",
                (thread, namespace, row["checkpoint_id"])).fetchall()
        return CheckpointTuple(config=self._config(thread, namespace, row["checkpoint_id"]),
            checkpoint=self.serde.loads_typed((row["checkpoint_type"], row["checkpoint"])),
            metadata=self.serde.loads_typed((row["metadata_type"], row["metadata"])),
            parent_config=self._config(thread, namespace, row["parent_id"]) if row["parent_id"] else None,
            pending_writes=[(item["task_id"], item["channel"], self.serde.loads_typed((item["value_type"], item["value"]))) for item in writes])

    def put(self, config, checkpoint, metadata, new_versions):
        self.setup()
        values = config["configurable"]
        thread, namespace = values["thread_id"], values.get("checkpoint_ns", "")
        c_type, c_blob = self.serde.dumps_typed(checkpoint)
        m_type, m_blob = self.serde.dumps_typed(get_checkpoint_metadata(config, metadata))
        with self.db.connect() as connection:
            connection.execute("INSERT INTO graph_checkpoints VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(thread_id,namespace,checkpoint_id) DO UPDATE SET parent_id=excluded.parent_id,checkpoint_type=excluded.checkpoint_type,checkpoint=excluded.checkpoint,metadata_type=excluded.metadata_type,metadata=excluded.metadata",
                (thread.split(":run:",1)[0], thread, namespace, checkpoint["id"], values.get("checkpoint_id"), c_type, c_blob, m_type, m_blob))
        return self._config(thread, namespace, checkpoint["id"])

    def put_writes(self, config, writes, task_id, task_path=""):
        self.setup()
        values = config["configurable"]
        rows = []
        for index, (channel, value) in enumerate(writes):
            value_type, blob = self.serde.dumps_typed(value)
            rows.append((values["thread_id"], values.get("checkpoint_ns", ""), values["checkpoint_id"], task_id,
                         WRITES_IDX_MAP.get(channel, index), channel, value_type, blob, task_path))
        with self.db.connect() as connection:
            for row in rows:
                clause = "DO UPDATE SET channel=excluded.channel,value_type=excluded.value_type,value=excluded.value,task_path=excluded.task_path" if row[4] < 0 else "DO NOTHING"
                connection.execute("INSERT INTO graph_writes VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(thread_id,namespace,checkpoint_id,task_id,idx) "+clause, row)

    def list(self, config, *, filter=None, before=None, limit=None):
        self.setup()
        query, params = "SELECT thread_id,namespace,checkpoint_id FROM graph_checkpoints WHERE 1=1", []
        if config:
            values = config["configurable"]
            query += " AND thread_id=? AND namespace=?"; params.extend([values["thread_id"], values.get("checkpoint_ns", "")])
        if before:
            query += " AND checkpoint_id<?"; params.append(before["configurable"]["checkpoint_id"])
        with self.db.connect() as connection:
            rows = connection.execute(query+" ORDER BY checkpoint_id DESC", params).fetchall()
        count = 0
        for row in rows:
            item = self.get_tuple(self._config(*row))
            if item is None or (filter and any(item.metadata.get(key) != value for key,value in filter.items())): continue
            if limit is not None and count >= limit: break
            count += 1
            yield item

    def delete_thread(self, thread_id):
        self.setup()
        with self.db.connect() as connection:
            connection.execute("DELETE FROM graph_checkpoints WHERE thread_id=?", (thread_id,))
