"""Identify an occupied RFI port and offer an authenticated, idle-only restart."""
import errno
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request


def port_available(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
            return True
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                return False
            raise


def process_identity(pid):
    proc = Path('/proc') / str(pid)
    fields = (proc/'stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': pid, 'uid': proc.stat().st_uid, 'start_ticks': fields[19], 'state': fields[0]}


def same_process(first, second):
    return all(first.get(k) == second.get(k) for k in ('pid', 'uid', 'start_ticks'))


def option_value(argv, option):
    value = None
    for index, arg in enumerate(argv):
        if arg == option and index+1 < len(argv):
            value = argv[index+1]
        elif arg.startswith(option+'='):
            value = arg.split('=', 1)[1]
    return value


def request_json(port, path, payload=None, token=None):
    headers = {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers['Content-Type'] = 'application/json'
    if token:
        headers['X-RFI-Control-Token'] = token
    request = urllib.request.Request(f'http://127.0.0.1:{port}{path}', data=data, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=4) as response:
            raw = response.read(4*1024*1024+1)
            if len(raw) > 4*1024*1024:
                raise ValueError('会话状态响应过大，未进行任何操作。')
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        try:
            detail = json.loads(error.read(65536)).get('error', str(error.code))
        except (ValueError, AttributeError):
            detail = str(error.code)
        raise ValueError(f'会话拒绝请求：{detail}') from error


def inspect_session(port, editor):
    result = {'port': port, 'recognized': False, 'reason': ''}
    try:
        status = subprocess.run(['ss', '-H', '-ltnp', f'sport = :{port}'],
                                capture_output=True, text=True, timeout=5, check=True)
        pids = set(map(int, re.findall(r'pid=(\d+)', status.stdout)))
        if len(pids) != 1:
            raise ValueError('无法唯一确认监听进程，可能是其他用户的服务或端口状态已变化。')
        pid = pids.pop()
        identity = process_identity(pid)
        result['identity'] = identity
        if identity['uid'] != os.getuid():
            raise ValueError('监听进程属于其他用户。')
        proc = Path('/proc') / str(pid)
        argv = [a.decode(errors='surrogateescape') for a in (proc/'cmdline').read_bytes().split(b'\0') if a]
        if not Path(os.readlink(proc/'exe')).name.startswith('python'):
            raise ValueError('监听进程不是已知的 Python RFI 程序。')
        script_index = 1
        while script_index < len(argv) and argv[script_index] in {'-B', '-u', '-O', '-OO'}:
            script_index += 1
        cwd = Path(os.readlink(proc/'cwd'))
        if script_index >= len(argv) or (cwd/argv[script_index]).resolve() != Path(editor).resolve():
            raise ValueError('监听进程不是此入口使用的 RFI Web 程序。')
        arguments = argv[script_index+1:]
        if (option_value(arguments, '--port') or '8992') != str(port):
            raise ValueError('进程参数与监听端口不一致。')
        if not same_process(identity, process_identity(pid)):
            raise ValueError('检查期间进程发生变化。')
        result.update(recognized=True, editor=str(Path(editor).resolve()))
        for name in ('input-dir', 'mask-dir'):
            value = option_value(arguments, '--'+name)
            result[name.replace('-', '_')] = str((cwd/value).resolve()) if value else '未在命令行指定'
        try:
            state = request_json(port, '/api/state')
            if (not isinstance(state, dict) or state.get('version') != 'native-web-v1'
                    or not isinstance(state.get('group'), dict)
                    or any(type(state.get(k)) is not bool for k in ('dirty', 'busy', 'ready'))
                    or not all(isinstance(state['group'].get(k), str) for k in ('scan', 'serial', 'key'))):
                raise ValueError('返回内容不是可验证的 RFI 会话状态。')
            result['state'] = state
        except (OSError, ValueError) as error:
            result['state_error'] = str(error)
    except (OSError, ValueError, subprocess.SubprocessError, IndexError) as error:
        result['reason'] = str(error)
    return result


def control_record(session):
    state = session.get('state', {})
    capability = state.get('session', {})
    if not isinstance(capability, dict) or capability.get('protocol') != 1:
        raise ValueError('旧版会话没有安全关闭接口；本次升级请先保存，再在原终端 Ctrl+C 正常退出一次。')
    path = Path.home()/'.cache/crafts-rfi/sessions'/f"{session['port']}-{session['identity']['pid']}.json"
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('控制文件的所有者或权限不符合要求。')
    if info.st_size > 65536:
        raise ValueError('控制文件异常。')
    record = json.loads(path.read_text())
    if (record.get('protocol') != 1 or record.get('pid') != session['identity']['pid']
            or record.get('pid') != capability.get('pid')
            or record.get('instance') != capability.get('instance')
            or record.get('port') != session['port'] or record.get('editor') != session['editor']
            or not isinstance(record.get('token'), str) or len(record['token']) < 32):
        raise ValueError('控制文件与当前进程实例不一致，保留旧会话。')
    return record


def restart_block_reason(session):
    if not session.get('recognized'):
        return session.get('reason') or '无法确认进程归属。'
    state = session.get('state')
    if not state:
        return '无法读取未保存状态；'+session.get('state_error', '')
    if state['dirty']:
        return '存在未保存的人工标记，请先在原网页保存。'
    if state['busy'] or state.get('phase') not in {'ready', 'error'}:
        return '会话正在加载、保存、切换或关闭，请等待完成。'
    try:
        control_record(session)
    except (OSError, ValueError, TypeError, KeyError) as error:
        return str(error)
    return ''


def describe_session(session):
    print(f"\n端口 {session['port']} 已被占用。")
    if not session['recognized']:
        print('无法确认是本用户的 RFI 会话：'+session['reason'])
        return
    print(f"已识别本用户的 RFI 会话，PID {session['identity']['pid']}")
    state = session.get('state')
    if state:
        group = state['group']
        print(f"当前条带: {group['scan']}；nfile: {group['serial']}")
        print(f"未保存修改: {'有' if state['dirty'] else '无'}；状态: {state.get('phase', '未知')}")
    else:
        print('当前条带 / 未保存状态: 无法读取，禁止重启。')
    print(f"旧会话输入: {session['input_dir']}\n旧会话保存: {session['mask_dir']}")
    reason = restart_block_reason(session)
    if reason:
        print('安全重启暂不可用：'+reason)


def shutdown_session(session, timeout=20):
    """No signals/force-kill: only the matching server may approve its shutdown."""
    reason = restart_block_reason(session)
    if reason:
        raise ValueError(reason)
    identity = session['identity']
    if not same_process(identity, process_identity(identity['pid'])):
        raise ValueError('进程已经变化，未请求关闭。')
    record = control_record(session)
    state = session['state']
    payload = {'pid': identity['pid'], 'instance': record['instance'],
               'group_key': state['group']['key'], 'revision': state['revision']}
    try:
        response = request_json(session['port'], '/api/control/shutdown', payload, record['token'])
        if (response.get('stopping') is not True or response.get('pid') != identity['pid']
                or response.get('instance') != record['instance']):
            raise ValueError('关闭确认内容不匹配；未启动新程序。')
    except OSError:
        # A lost reply is not consent to signal a PID. Only observe whether the
        # original instance actually exits; never retry against a new owner.
        print('未收到完整关闭响应，正在检查旧进程是否已退出…', flush=True)
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            current = process_identity(identity['pid'])
            alive = same_process(identity, current) and current['state'] != 'Z'
        except FileNotFoundError:
            alive = False
        if not alive:
            if port_available(session['port']):
                print('旧会话已安全退出，端口已释放。', flush=True)
                return
            raise ValueError('旧进程已退出，但端口被其他进程占用；未启动或结束任何新进程。')
        time.sleep(0.2)
    raise ValueError('旧进程尚未完全退出；未强制结束，也未启动第二个程序。请稍后重试。')


def resolve_occupied_port(port, editor, requested, *, read_only=False):
    """Return True only when a new server should start; False means reuse/cancel."""
    if port_available(port):
        return True
    session = inspect_session(port, editor)
    describe_session(session)
    print('本次请求启动: '+requested)
    if read_only:
        print('只读检查：没有弹出选择或结束任何进程。')
        return False
    if not session['recognized']:
        raise ValueError('保留现有服务。请使用其他端口，不能自动结束这个进程。')
    if not sys.stdin.isatty():
        raise ValueError('需要在交互终端选择继续或重启；非交互运行不会自动结束旧会话。')
    while True:
        print('\n1) 继续使用旧会话（不切换条带、不改标记）')
        print('2) 安全重启（停止旧会话，按上方本次请求的条带 / 版本 / nfile 启动）')
        print('0) 取消（保留旧会话）')
        try:
            choice = input('请选择 [1/2/0，默认 0]: ').strip()
        except (EOFError, KeyboardInterrupt):
            print('\n已取消，旧会话保持不变。')
            return False
        if choice in {'', '0'}:
            print('已取消，旧会话保持不变。')
            return False
        if choice not in {'1', '2'}:
            print('请输入 1、2 或 0。')
            continue
        fresh = inspect_session(port, editor)
        if not fresh['recognized'] or not same_process(session['identity'], fresh.get('identity', {})):
            raise ValueError('端口对应进程已变化；未进行操作，请重新运行脚本。')
        if choice == '1':
            print(f'继续使用旧会话：http://127.0.0.1:{port}')
            print(f'Mac 本地如需重建转发：ssh -N -L {port}:127.0.0.1:{port} CRAFTS')
            print('本次请求没有另行启动；请在现有网页中继续。')
            return False
        reason = restart_block_reason(fresh)
        old_state, new_state = session.get('state', {}), fresh.get('state', {})
        changed = any(old_state.get(k) != new_state.get(k) for k in ('group', 'revision', 'session'))
        if reason or changed:
            print('保留旧会话：'+(reason or '条目或编辑状态在选择期间发生变化，请重新确认。'))
            session = fresh
            describe_session(session)
            continue
        print('正在请求旧会话再次检查状态并关闭…', flush=True)
        shutdown_session(fresh)
        return True
