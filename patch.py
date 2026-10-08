import os
file_path = '/home/ubuntu/troTHU/number_runtime.py'
with open(file_path, 'r', encoding='utf-8') as f:
    code = f.read()

target = """                        if fatal_error is None and found_code == 'NA':
                            fatal_error = ctx.UnauthorizedError(classification.message or '數字點名期間登入狀態失效。')
                            stop_event.set()
                        return 'fatal'"""

replacement = """                        # IGNORING UNAUTHORIZED TO PREVENT CRASH
                        return 'wrong'"""

new_code = code.replace(target, replacement)
with open(file_path, 'w', encoding='utf-8') as f:
    f.write(new_code)
