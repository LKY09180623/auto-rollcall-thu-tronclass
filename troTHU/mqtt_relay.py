import paho.mqtt.client as mqtt
import asyncio
import json
import uuid

try:
    import troTHU.runtime_context as ctx
except ImportError:
    import runtime_context as ctx

MQTT_BROKER = "broker.emqx.io"
MQTT_PORT = 1883

_client = None
_loop = None

def on_connect(client, userdata, flags, rc):
    channel = ctx.CONFIG.get("mqtt_channel")
    if channel and rc == 0:
        topic = f"tronclass/qr_relay/{channel}"
        client.subscribe(topic)
        ctx.log_print(f"✅ 已連線至雲端廣播站，正在監聽專屬頻道: {channel}")
    elif rc != 0:
        ctx.log_print(f"⚠️ MQTT 連線失敗，回傳碼: {rc}")

async def _execute_mqtt_qr(payload: str):
    try:
        result = await ctx.qr_fanout_result(payload)
        if result.get("status") == "no_matches":
            profiles = list(ctx.list_profiles(ctx.CONFIG))
            if not profiles:
                profiles = [ctx.get_active_profile(ctx.CONFIG)]
            original = ctx.get_active_profile(ctx.CONFIG).name
            try:
                for prof in profiles:
                    ctx.switch_profile(ctx.CONFIG, prof.name)
                    ctx.log_print(f"為帳號 {prof.name} 直接執行 QR 簽到...")
                    connector = ctx.create_http_connector()
                    timeout = ctx.create_http_client_timeout()
                    session_kwargs = {"connector": connector, "headers": {"User-Agent": ctx.random_ua()}}
                    if timeout is not None:
                        session_kwargs["timeout"] = timeout
                    async with ctx.aiohttp.ClientSession(**session_kwargs) as session:
                        login_res = await ctx.login(session)
                        if login_res.ok:
                            confirmed = await ctx.submit_qr_payload(session, payload)
                            if not confirmed:
                                ctx.log_print(f"[{prof.name}] QR 已送出但尚未確認出席，請先查看官方點名紀錄。")
                        else:
                            ctx.log_print(f"[{prof.name}] 登入失敗 ({login_res.status})，無法完成 QR 簽到。")
            finally:
                ctx.switch_profile(ctx.CONFIG, original)
        elif not result.get("ok"):
            ctx.log_print("QR 處理未全部確認成功，請查看各帳號點名紀錄。")
    except Exception as e:
        ctx.log_print(f"處理 MQTT 廣播簽到時發生錯誤: {e}")

def on_message(client, userdata, msg):
    payload = msg.payload.decode('utf-8')
    ctx.log_print(f"📡 收到雲端廣播 QR: {payload[:30]}...")
    
    # Try to validate json
    try:
        data = json.loads(payload)
        if "rollcallId" not in data and "rollcall_id" not in data:
            ctx.log_print("⚠️ 收到的廣播格式不正確，略過。")
            return
    except json.JSONDecodeError:
        ctx.log_print("⚠️ 收到的廣播不是合法的 JSON 格式，略過。")
        return

    if _loop and not _loop.is_closed():
        ctx.log_print("🚀 正在執行 QR 代點名...")
        asyncio.run_coroutine_threadsafe(
            _execute_mqtt_qr(payload),
            _loop
        )

def start_mqtt_relay(loop: asyncio.AbstractEventLoop):
    global _client, _loop
    _loop = loop
    
    channel = ctx.CONFIG.get("mqtt_channel")
    if not channel:
        return
        
    _client = mqtt.Client(client_id="tron_bot_" + uuid.uuid4().hex)
    _client.on_connect = on_connect
    _client.on_message = on_message
    
    try:
        ctx.log_print(f"啟動 MQTT 雲端廣播接收器 (頻道: {channel})...")
        _client.connect(MQTT_BROKER, MQTT_PORT, 60)
        _client.loop_start() # Starts a background thread
    except Exception as e:
        ctx.log_print(f"⚠️ 無法連線至 MQTT Broker: {e}")

def stop_mqtt_relay():
    if _client:
        _client.loop_stop()
        _client.disconnect()
