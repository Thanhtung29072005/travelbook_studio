"""Optional Telegram delivery with a durable queue, separate from agent execution."""
import hashlib
import json
from pathlib import Path
import threading
import time

import httpx

NAMES = {'pm':'PM — lập yêu cầu', 'researcher':'Nghiên cứu', 'planner':'Lập lịch trình',
    'budget':'Ngân sách', 'content_editor':'Biên tập nội dung', 'image_curator':'Chọn ảnh địa điểm',
    'illustrator':'Vẽ minh họa', 'map_agent':'Bản đồ và biểu đồ', 'reviewer':'Kiểm duyệt',
    'layout_editor':'Dàn trang PDF', 'pm_finalize':'PM — xác nhận hoàn tất'}


class DeliveryError(Exception):
    def __init__(self, code, retry_after=None):
        super().__init__(code)
        self.retry_after = retry_after


class TelegramNotifications:
    def __init__(self, db, settings, *, transport=None):
        self.db, self.settings, self.transport = db, settings, transport
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        self._thread = None

    @property
    def enabled(self):
        return bool(self.settings.telegram_enabled and self.settings.telegram_bot_token.get_secret_value().strip()
                    and self.settings.telegram_chat_id.strip())

    def _schema(self):
        with self.db.connect() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS telegram_deliveries(
                id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                chat_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, locked_until REAL NOT NULL DEFAULT 0)''')

    def start(self):
        if not self.enabled:
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._schema()
            self._stop.clear()
            self._thread = threading.Thread(target=self._worker, name='telegram-delivery', daemon=True)
            self._thread.start()

    def close(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _queue(self, project_id, key, kind, payload):
        if not self.enabled:
            return
        # Notification errors never change the outcome of an agent or project.
        try:
            self._schema()
            destination = f'{self.settings.telegram_chat_id}:{self.settings.telegram_message_thread_id}'
            identity = hashlib.sha256(f'{project_id}:{destination}:{key}'.encode()).hexdigest()
            payload['thread_id'] = self.settings.telegram_message_thread_id
            with self.db.connect() as connection:
                connection.execute('INSERT OR IGNORE INTO telegram_deliveries(id,project_id,chat_id,kind,payload) VALUES(?,?,?,?,?)',
                    (identity,project_id,self.settings.telegram_chat_id.strip(),kind,json.dumps(payload,ensure_ascii=False)))
            self.start()
            self._wake.set()
        except Exception:
            self._event(project_id, 'telegram_failed', 'Không lưu được thông báo Telegram', {})

    def _event(self, pid, kind, message, payload):
        try:
            self.db.add_event(pid,'system',kind,message,payload)
        except Exception:
            pass

    def agent_completed(self, state, node, result):
        if not self.enabled or node not in NAMES:
            return
        pid = state['project_id']
        try:
            row = self.db.get_project(pid)
            title = (row['title'] if row else pid)[:180]
            revision = state.get('revision_count',0)
            review = result.get('review')
            verdict = getattr(review,'verdict',None) if not isinstance(review,dict) else review.get('verdict')
            detail = '\nKết quả kiểm duyệt: ' + ('cần sửa lại.' if verdict=='REVISE' else 'đạt.') if verdict else ''
            label = f'\nVòng sửa: {revision}' if revision else ''
            text = f'✅ {NAMES[node]} đã hoàn thành\nDự án: {title}\nMã: {pid}{label}{detail}'
            self._queue(pid,f"agent:{state.get('execution_count',0)}:{revision}:{node}",'message',{'text':text})
        except Exception:
            self._event(pid,'telegram_failed','Không chuẩn bị được thông báo agent',{'node':node})

    def project_completed(self, project_id, state):
        if not self.enabled:
            return
        try:
            pdf = state.get('final_pdf')
            path = getattr(pdf,'path',None) if not isinstance(pdf,dict) else pdf.get('path')
            row = self.db.get_project(project_id)
            title = (row['title'] if row else project_id)[:180]
            review = state.get('review')
            verdict = review.get('verdict') if isinstance(review,dict) else getattr(review,'verdict',None)
            note = '\nLưu ý: bản PDF còn vấn đề kiểm duyệt, cần xem lại.' if verdict=='REVISE' else ''
            key = f"pdf:{state.get('execution_count',0)}:{state.get('revision_count',0)}:{path}"
            self._queue(project_id,key,'pdf',{'path':path,'caption':f'📘 Cẩm nang đã hoàn thành\nDự án: {title}\nMã: {project_id}{note}'})
        except Exception:
            self._event(project_id,'telegram_failed','Không chuẩn bị được PDF gửi Telegram',{})

    def _post(self, method, *, data, files=None):
        token = self.settings.telegram_bot_token.get_secret_value().strip()
        # Never include exception text or request URLs in logs: URL contains the bot token.
        try:
            with httpx.Client(timeout=self.settings.telegram_timeout_seconds, transport=self.transport,
                              follow_redirects=False) as client:
                response = client.post(f'https://api.telegram.org/bot{token}/{method}',data=data,files=files)
                body = response.json()
            if not response.is_success or not body.get('ok'):
                retry_after = body.get('parameters',{}).get('retry_after') if isinstance(body,dict) else None
                raise DeliveryError(f'Telegram HTTP {response.status_code}',retry_after)
        except DeliveryError:
            raise
        except Exception:
            raise DeliveryError('Telegram network/response error') from None

    def _send(self, item):
        payload = json.loads(item['payload'])
        data = {'chat_id':item['chat_id']}
        if payload.get('thread_id'):
            data['message_thread_id'] = str(payload['thread_id'])
        if item['kind']=='message':
            self._post('sendMessage',data={**data,'text':payload['text']})
        else:
            path = Path(payload.get('path') or '').resolve()
            root = self.settings.artifacts_dir.resolve()
            if not path.is_relative_to(root) or not path.is_file() or path.suffix.lower()!='.pdf':
                raise DeliveryError('PDF missing or outside project artifacts')
            if path.stat().st_size > 50_000_000:
                raise DeliveryError('PDF exceeds Telegram upload limit (50 MB)')
            with path.open('rb') as stream:
                if stream.read(5)!=b'%PDF-':
                    raise DeliveryError('Invalid PDF file')
                stream.seek(0)
                self._post('sendDocument',data={**data,'caption':payload['caption']},
                           files={'document':(path.name,stream,'application/pdf')})

    def deliver_one(self):
        """Claim before networking so parallel workers cannot send the same queued item."""
        now = time.time()
        with self.db.connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute("SELECT * FROM telegram_deliveries WHERE status='pending' AND chat_id=? AND next_attempt<=? AND locked_until<=? ORDER BY rowid LIMIT 1",(self.settings.telegram_chat_id.strip(),now,now)).fetchone()
            if row is None:
                return False
            item = dict(row)
            connection.execute('UPDATE telegram_deliveries SET locked_until=? WHERE id=?',
                               (now+self.settings.telegram_timeout_seconds+60,item['id']))
        try:
            self._send(item)
        except Exception as error:
            attempt = item['attempts']+1
            try:
                retry_after = int(getattr(error,'retry_after',None) or 0)
            except (TypeError,ValueError):
                retry_after = 0
            delay = min(3600,max(2**attempt,retry_after))
            status = 'failed' if attempt>=3 else 'pending'
            with self.db.connect() as connection:
                connection.execute('UPDATE telegram_deliveries SET status=?,attempts=?,next_attempt=?,locked_until=0 WHERE id=?',
                    (status,attempt,time.time()+delay,item['id']))
            self._event(item['project_id'],'telegram_failed','Gửi Telegram chưa thành công; '+('đã hết 3 lần thử' if status=='failed' else 'sẽ thử lại'),
                        {'kind':item['kind'],'attempt':attempt})
        else:
            with self.db.connect() as connection:
                connection.execute("UPDATE telegram_deliveries SET status='sent',locked_until=0 WHERE id=?",(item['id'],))
            self._event(item['project_id'],'telegram_sent','Đã gửi '+('PDF' if item['kind']=='pdf' else 'thông báo agent')+' lên Telegram',{'kind':item['kind']})
        return True

    def _worker(self):
        while not self._stop.is_set():
            try:
                if self.deliver_one():
                    continue
            except Exception:
                pass
            self._wake.wait(2)
            self._wake.clear()
