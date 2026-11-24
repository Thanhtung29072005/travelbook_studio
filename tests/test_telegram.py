import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from backend.app.config import Settings
from backend.app.db import Database
from backend.app.services.telegram import TelegramNotifications


class TelegramTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)
        self.db=Database(self.root/'test.db')
        self.db.init()
        self.db.create_project('telegram-test','Du lịch Huế','Test')
        self.settings=Settings(_env_file=None,data_dir=self.root,telegram_enabled=True,
            telegram_bot_token='test-bot-token',telegram_chat_id='-100123',telegram_message_thread_id=42)
        self.requests=[]
        def handler(request):
            self.requests.append(request)
            return httpx.Response(200,json={'ok':True,'result':{'message_id':1}})
        self.service=TelegramNotifications(self.db,self.settings,transport=httpx.MockTransport(handler))
        # Deterministic manual drain instead of background threads in unit tests.
        self.start=patch.object(self.service,'start')
        self.start.start()
        self.addCleanup(self.start.stop)
        self.state={'project_id':'telegram-test','execution_count':0,'revision_count':0}

    def rows(self):
        with self.db.connect() as connection:
            return [dict(row) for row in connection.execute('SELECT * FROM telegram_deliveries ORDER BY rowid')]

    def test_completion_order_deduplication_and_revision(self):
        self.service.agent_completed(self.state,'illustrator',{})
        self.service.agent_completed(self.state,'map_agent',{})
        self.service.agent_completed(self.state,'illustrator',{})
        self.service.agent_completed({**self.state,'revision_count':1},'illustrator',{})
        self.assertEqual(len(self.rows()),3)
        self.assertTrue(self.service.deliver_one())
        self.assertTrue(self.service.deliver_one())
        self.assertTrue(self.service.deliver_one())
        self.assertFalse(self.service.deliver_one())
        from urllib.parse import parse_qs
        messages=[parse_qs(request.content.decode()) for request in self.requests]
        self.assertIn('Vẽ minh họa',messages[0]['text'][0])
        self.assertIn('Bản đồ',messages[1]['text'][0])
        self.assertIn('Vòng sửa: 1',messages[2]['text'][0])
        self.assertEqual(messages[0]['message_thread_id'],['42'])

    def test_final_pdf_is_uploaded_as_document(self):
        path=self.settings.artifacts_dir/'telegram-test'/'travelbook.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'%PDF-1.4\nunit-test')
        state={**self.state,'final_pdf':{'path':str(path)},'review':{'verdict':'REVISE'}}
        self.service.project_completed('telegram-test',state)
        self.service.project_completed('telegram-test',state)
        self.assertEqual(len(self.rows()),1)
        self.service.deliver_one()
        request=self.requests[0]
        self.assertTrue(request.url.path.endswith('/sendDocument'))
        self.assertIn(b'filename="travelbook.pdf"',request.content)
        self.assertIn(b'%PDF-1.4',request.content)
        self.assertIn('còn vấn đề kiểm duyệt'.encode(),request.content)

    def test_failed_delivery_does_not_raise_or_leak_bot_token(self):
        self.service.transport=httpx.MockTransport(lambda request:httpx.Response(401,json={'ok':False}))
        self.service.agent_completed(self.state,'planner',{})
        for _ in range(3):
            with self.db.connect() as connection:
                connection.execute('UPDATE telegram_deliveries SET next_attempt=0')
            self.service.deliver_one()
        self.assertEqual(self.rows()[0]['status'],'failed')
        self.assertEqual(self.db.get_project('telegram-test')['status'],'queued')
        self.assertNotIn('test-bot-token',json.dumps(self.db.get_events('telegram-test')))

    def test_pdf_outside_artifacts_is_never_uploaded(self):
        path=self.root/'private.pdf'
        path.write_bytes(b'%PDF-1.4')
        self.service.project_completed('telegram-test',{**self.state,'final_pdf':{'path':str(path)}})
        self.service.deliver_one()
        self.assertEqual(self.requests,[])

    def test_disabled_notifications_make_no_network_or_queue(self):
        self.settings.telegram_enabled=False
        self.service.agent_completed(self.state,'pm',{})
        self.service.project_completed('telegram-test',self.state)
        self.assertEqual(self.requests,[])
        with self.db.connect() as connection:
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='telegram_deliveries'").fetchone())

    def test_pending_queue_survives_recreating_sender(self):
        self.service.agent_completed(self.state,'researcher',{})
        other=TelegramNotifications(self.db,self.settings,transport=self.service.transport)
        other.deliver_one()
        self.assertEqual(self.rows()[0]['status'],'sent')
        self.assertEqual(len(self.requests),1)
