import troTHU.runtime_context as ctx
import json
print(json.dumps(ctx.normalize_config(ctx.load_advanced_config()).get("integrations")))
