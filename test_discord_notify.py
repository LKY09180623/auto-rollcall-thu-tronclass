import asyncio, yaml
import sys
sys.path.append('/home/ubuntu/auto-rollcall-thu-tronclass-QR')
from troTHU.notification_delivery import build_notification_requests, send_notification_request

async def test():
    config = yaml.safe_load(open('/home/ubuntu/config.yaml'))
    reqs = build_notification_requests(config, '這是一條測試通知！\n如果你看到這條訊息，代表 Discord 通知連線已經成功建立。', 'Test success!')
    for r in reqs:
        await send_notification_request(r)

if __name__ == '__main__':
    asyncio.run(test())
