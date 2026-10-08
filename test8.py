import asyncio
import troTHU.runtime_context as ctx
from troTHU.bot_handlers import create_bot_runtime
import json

async def test():
    ctx.bootstrap_config()
    runtime = create_bot_runtime(ctx.CONFIG, base_dir=ctx.BASE_DIR)
    
    class MockCommand:
        action = "qr-submit"
        profile = "A113280040"
        payload = {"payload": "7777"}
        source_user_id = "862506419508477953"
        adapter = None

    try:
        res = await runtime.handlers.qr_submit(profile="A113280040", payload="7777", command=MockCommand())
        print("OK handler res:", res)
    except Exception as e:
        import traceback
        traceback.print_exc()

asyncio.run(test())
