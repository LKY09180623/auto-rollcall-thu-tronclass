import troTHU.runtime_context as ctx
import json
print(json.dumps(ctx.CONFIG.get('integrations')))
