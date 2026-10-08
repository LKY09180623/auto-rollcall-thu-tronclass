import troTHU.runtime_context as ctx
import json
ctx.bootstrap_config()
print(json.dumps(ctx.CONFIG.get('accounts'), indent=2))
