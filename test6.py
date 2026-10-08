import troTHU.runtime_context as ctx
import json
print("BEFORE bootstrap:", json.dumps(ctx.CONFIG.get('integrations')))
ctx.bootstrap_config()
print("AFTER bootstrap:", json.dumps(ctx.CONFIG.get('integrations')))
