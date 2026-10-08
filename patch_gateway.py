import os

path = '/app/troTHU/discord_gateway.py'
with open(path, 'r') as f:
    content = f.read()

content = content.replace(
    'interaction_id = _clean_text(payload.get("id"))',
    'print("DISCORD_PAYLOAD:", payload, flush=True)\n    interaction_id = _clean_text(payload.get("id"))'
)

with open(path, 'w') as f:
    f.write(content)
