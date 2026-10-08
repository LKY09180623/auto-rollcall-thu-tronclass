import troTHU.runtime_context as ctx
import json
advanced = ctx.load_advanced_config()
text = open(ctx.CONFIG_PATH, 'r', encoding='utf-8').read()
simple = ctx.parse_simple_config_text(text)
merged = ctx.merge_simple_and_advanced_config(simple, advanced)
print("Before normalize:", json.dumps(merged.get('integrations')))
normalized = ctx.normalize_config(merged)
print("After normalize:", json.dumps(normalized.get('integrations')))
