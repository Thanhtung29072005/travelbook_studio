"""Read Telegram chat IDs or send an explicitly requested test; never print the bot token."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

import httpx
from backend.app.config import Settings
from backend.app.services.telegram import TelegramNotifications, DeliveryError


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    action=parser.add_mutually_exclusive_group(required=True)
    action.add_argument('--list-chats',action='store_true',help='List chats after you send /start or a message to your bot')
    action.add_argument('--test',action='store_true',help='Send a test to TELEGRAM_CHAT_ID')
    args=parser.parse_args()
    settings=Settings()
    token=settings.telegram_bot_token.get_secret_value().strip()
    if not token:
        parser.exit(1,'Thieu TELEGRAM_BOT_TOKEN trong .env.\n')
    try:
        if args.list_chats:
            with httpx.Client(timeout=settings.telegram_timeout_seconds) as client:
                response=client.get(f'https://api.telegram.org/bot{token}/getUpdates',params={'timeout':0,'limit':100})
                body=response.json()
            if not response.is_success or not body.get('ok'):
                parser.exit(1,f'Telegram HTTP {response.status_code}: kiem tra token; getUpdates khong dung duoc neu bot dang co webhook.\n')
            chats={}
            for update in body.get('result',[]):
                message=update.get('message') or update.get('channel_post') or update.get('my_chat_member') or {}
                chat=message.get('chat',{})
                if 'id' in chat:
                    chats[chat['id']]={'chat_id':chat['id'],'type':chat.get('type'),
                        'name':chat.get('title') or chat.get('first_name') or chat.get('username'),
                        'topic_id':message.get('message_thread_id')}
            print(json.dumps(list(chats.values()),ensure_ascii=True,indent=2))
            if not chats:
                print('Hay nhan /start cho bot hoac them bot vao nhom va gui /start@ten_bot, roi chay lai.')
        else:
            if not settings.telegram_chat_id.strip():
                parser.exit(1,'Thieu TELEGRAM_CHAT_ID trong .env.\n')
            data={'chat_id':settings.telegram_chat_id,'text':'✅ TravelBook đã kết nối Telegram. Bạn sẽ nhận thông báo hoàn thành agent và PDF cuối.'}
            if settings.telegram_message_thread_id:
                data['message_thread_id']=str(settings.telegram_message_thread_id)
            TelegramNotifications(None,settings)._post('sendMessage',data=data)
            print('Da gui thong bao thu thanh cong.')
    except (httpx.HTTPError,ValueError,DeliveryError):
        parser.exit(1,'Khong ket noi duoc Telegram. Kiem tra mang, token, chat ID va quyen gui tin cua bot.\n')


if __name__=='__main__':
    main()
