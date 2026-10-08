import troTHU.runtime_context as ctx
import json
ctx.bootstrap_config()
print("After bootstrap:", json.dumps(ctx.CONFIG.get('integrations')))
