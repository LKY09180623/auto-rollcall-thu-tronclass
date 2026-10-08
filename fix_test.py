path = 'tests/test_providers.py'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()
print(f'Total lines: {len(lines)}')
for i in range(545, 570):
    print(f'{i+1}: {repr(lines[i][:100])}')
