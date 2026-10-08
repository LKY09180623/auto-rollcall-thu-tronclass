import os
file_path = '/home/ubuntu/troTHU/number_runtime.py'
with open(file_path, 'r', encoding='utf-8') as f:
    code = f.read()

target = """                    elif classification.status == ctx.NumberAttemptStatus.UNAUTHORIZED:
                        ctx.log(event='tron_http_error', path=ctx.number_log_path(rcid), counter=try_code, status='number_unauthorized', url=str(resp.url), http_status=resp.status, rollcall_id=rcid, rollcall_type='number', message='數字點名期間登入狀態失效。', payload_excerpt=body[:300])
                        if fatal_error is None and found_code == 'NA':
                            fatal_error = ctx.UnauthorizedError(classification.message or '數字點名期間登入狀態失效。')
                            stop_event.set()
                        return 'fatal'"""

replacement = """                    elif classification.status == ctx.NumberAttemptStatus.UNAUTHORIZED:
                        if session == swarm_sessions[0]:
                            ctx.log(event='tron_http_error', path=ctx.number_log_path(rcid), counter=try_code, status='number_unauthorized', url=str(resp.url), http_status=resp.status, rollcall_id=rcid, rollcall_type='number', message='數字點名期間登入狀態失效。', payload_excerpt=body[:300])
                            if fatal_error is None and found_code == 'NA':
                                fatal_error = ctx.UnauthorizedError(classification.message or '數字點名期間登入狀態失效。')
                                stop_event.set()
                            return 'fatal'
                        else:
                            # Sub-account got UNAUTHORIZED. Hand it over to the main account!
                            return await try_number_code(swarm_sessions[0], try_code, method=method)"""

new_code = code.replace(target, replacement)
with open(file_path, 'w', encoding='utf-8') as f:
    f.write(new_code)
