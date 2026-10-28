"""Select a calibrated drift and an independent manual-mask version."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile

HERE = Path(__file__).resolve().parent
DEFAULT_EDITOR = Path.home() / 'project/crafts_tod_manual_mask_v2/crafts_tod_native_web.py'
DEFAULT_OUTPUT = Path.home() / 'project/rfi_runs/calibration_vulpecula_perseus_20260905'
DEFAULT_AUTO_ROOT = Path('/data71/common_storage/zhouyunfan/test_codex20260722/oldpipeline/test_all/'
                         'rfi_vulpecula_perseus_20261014/products')


def parser():
    p = argparse.ArgumentParser(description=__doc__, epilog=(
        'Examples: run_rfi_calibration.sh --list; '
        'run_rfi_calibration.sh 320 v1 0001; '
        'run_rfi_calibration.sh Dec+2654_22_09 v2 0011 --port 8993. '
        'The version labels manual masks, not the calibrated FITS.'))
    p.add_argument('scan', nargs='?', help='idrift, full scan name, or unique Dec/Dc prefix')
    p.add_argument('version', nargs='?', default='v1', help='independent mask version: v1, v2, v3, ...')
    p.add_argument('nfile', nargs='?', default='0001')
    p.add_argument('--port', type=int, default=8992)
    p.add_argument('--list', action='store_true', help='show audited drifts without starting a service')
    p.add_argument('--dry-run', action='store_true', help='check selection and print command; no writes')
    p.add_argument('--check', action='store_true', help='also read and validate the selected 19 FITS axes; no writes')
    p.add_argument('--prepare-only', action='store_true', help='prepare provenance/backup without starting the Web service')
    p.add_argument('--auto', choices=['none', 'required'], default='required',
                   help='none: manual-only; required: require both auto-mask sets in this campaign')
    p.add_argument('--auto-root', type=Path, default=DEFAULT_AUTO_ROOT,
                   help='products root containing project/scan subdirectories')
    p.add_argument('--output-root', type=Path, default=DEFAULT_OUTPUT)
    p.add_argument('--editor', type=Path, default=DEFAULT_EDITOR)
    return p


def select_scan(rows, selector):
    exact = [r for r in rows if selector in {str(r['idrift']), r['scan'], r['source'], r['relative_dir']}]
    matches = exact or [r for r in rows if r['scan'].lower().startswith(selector.lower())]
    if len(matches) != 1:
        detail = '\n'.join(f"  {r['idrift']}  {r['relative_dir']}" for r in matches)
        raise ValueError(('没有匹配的扫描。请用 --list。' if not matches else '扫描名称不唯一，请选择 idrift：\n'+detail))
    return matches[0]


def show_list(manifest):
    print('idrift  nfile范围   T文件数  扫描（含观测日期）')
    for r in sorted(manifest['scans'], key=lambda r: (r['project'], r['scan'])):
        marker = '  [HFC需复核]' if r['hfc_io_error_count'] else ''
        if r['blocked_serials']:
            marker += ' [暂不载入:'+','.join(r['blocked_serials'])+']'
        print(f"{r['idrift']:>6}  {r['serials'][0]}–{r['serials'][-1]}  {r['count']:>7}  {r['relative_dir']}{marker}")
    print('\n启动: bash ~/project/run_rfi_calibration.sh <idrift或完整扫描名> [v1|v2|v3] [0001]')
    print('默认 v1 + 两类自动 mask；同一版本载入已有人工标记，新版本从空白人工标记开始。')
    print('旧人工版本首次启用自动层前完整备份；不会清空已有标记或自动改为 v2。')
    print('HFC需复核不等于RFI；列表其余扫描也未做绝对定标的科学验收。')


def check_contract(destination, contract):
    """Allow only the explicit manual-only -> automatic-layer migration."""
    if destination.is_symlink() or destination.resolve() != destination:
        raise ValueError(f'保存目录不能通过软链接指向其他版本：{destination}')
    record = destination / 'run.json'
    if record.exists():
        if record.is_symlink():
            raise ValueError('run.json 不能是软链接。')
        previous = json.loads(record.read_text())
        keys = ['campaign_root', 'input_dir', 'mask_dir', 'mask_version', 'idrift', 'excluded_serials']
        if any(previous.get(k) != contract[k] for k in keys):
            raise ValueError('此输出版本的来源与当前参数不同；请使用新版本。')
        old_mode, mode = previous.get('automatic_mode'), contract['automatic_mode']
        if old_mode == 'none' and mode == 'required':
            return 'upgrade', previous
        if old_mode != mode or (mode == 'required' and previous.get('automatic_dirs') != contract['automatic_dirs']):
            raise ValueError('此版本的自动层配置与当前参数不同；请使用新版本。')
        return 'existing', previous
    if destination.exists() and any(p.name != '.session.lock' for p in destination.iterdir()):
        raise ValueError('目标目录已有未登记来源的内容；请保留原目录并改用新版本。')
    return 'new', None


def file_sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def backup_manual_version(destination, backup_parent):
    """Independent, byte-identical snapshot under the held session lock."""
    files = sorted(p for p in destination.iterdir() if p.name != '.session.lock')
    if any(p.is_symlink() or not p.is_file() for p in files):
        raise ValueError('旧版本含软链接或子目录，未修改现场；请先核对该版本。')
    backup_parent.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path(tempfile.mkdtemp(prefix=f'{destination.name}_before_auto_{stamp}_', dir=backup_parent))
    hashes = {}
    for path in files:
        before = file_sha256(path)
        shutil.copy2(path, backup / path.name)
        if file_sha256(backup / path.name) != before or file_sha256(path) != before:
            raise ValueError(f'备份时文件发生变化，未更新配置；检查 {backup}')
        hashes[path.name] = before
    if {p.name for p in destination.iterdir() if p.name != '.session.lock'} != set(hashes):
        raise ValueError(f'备份时目录发生变化，未更新配置；检查 {backup}')
    with (backup / 'backup_manifest.json').open('x') as stream:
        json.dump({'source': str(destination), 'created_utc': stamp, 'complete': True,
                   'files_sha256': hashes}, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    return backup


def prepare_run_record(destination, contract, backup_parent):
    # Recheck after acquiring the session lock; dry-run observations may be stale.
    action, previous = check_contract(destination, contract)
    if action == 'existing':
        return
    record = dict(previous or {}, **contract)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if action == 'upgrade':
        backup = backup_manual_version(destination, backup_parent)
        print(f'旧版完整备份（已逐文件校验）: {backup}', flush=True)
        record.update(automatic_enabled_utc=now, before_auto_backup=str(backup))
    else:
        record['created_utc'] = now
    with tempfile.NamedTemporaryFile(mode='w', dir=destination, prefix='.run-', suffix='.tmp', delete=False) as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    temporary.replace(destination / 'run.json')


def main():
    args = parser().parse_args()
    manifest = json.loads((HERE / 'scans.json').read_text())
    if args.list or args.scan is None:
        show_list(manifest)
        return
    if not re.fullmatch(r'v[1-9][0-9]*', args.version):
        raise ValueError('版本必须为 v1、v2、v3 等；版本对应独立的 manual mask 保存目录。')
    if not re.fullmatch(r'[0-9]{1,4}', args.nfile) or int(args.nfile) < 1:
        raise ValueError('nfile 必须是正整数，例如 1 或 0001。')
    if not 1 <= args.port <= 65535:
        raise ValueError('端口必须在 1–65535 之间。')
    serial = f'{int(args.nfile):04d}'
    row = select_scan(manifest['scans'], args.scan)
    if serial in row['blocked_serials']:
        raise ValueError(f"nfile={serial} 的19-beam轴/结构不兼容；原文件保留，暂不载入。见 CALIBRATION_AUDIT.md。")
    if serial not in row['serials']:
        raise ValueError(f"该扫描没有 nfile={serial}；范围 {row['serials'][0]}–{row['serials'][-1]}。")
    if not row['generation_complete']:
        raise ValueError('该扫描的生成检查未通过，请先查看检查报告。')
    source = Path(manifest['data_root']) / row['relative_dir']
    output_root = args.output_root.expanduser().resolve()
    destination = output_root / row['project'] / row['scan'] / 'T' / ('masks_'+args.version)
    editor = args.editor.expanduser().resolve()
    if not source.is_dir() or not editor.is_file():
        raise ValueError(f'输入目录或 Web 程序不存在：{source}；{editor}')

    # Only use this campaign. Never borrow masks from the older five-stripe run.
    if args.auto == 'required':
        auto_scan = args.auto_root.expanduser().resolve() / row['relative_dir']
        auto_dirs = [auto_scan/'one_channel_rfi_v2_products', auto_scan/'satellite_rfi_1380_products']
    else:
        disabled = output_root / '.manual_only_no_auto_masks'
        if disabled.exists():
            raise ValueError(f'人工模式占位路径必须不存在，以免误读自动 mask：{disabled}')
        auto_dirs = [disabled/'one_channel', disabled/'sat1380']

    sys.path.insert(0, str(editor.parent))
    from crafts_tod_mask import discover_groups, validate_group
    groups = discover_groups(source)
    actual = {g.serial: g for g in groups}
    if len(actual) != len(groups) or set(actual) != set(row['all_serials']):
        raise ValueError('当前输入条目与检查清单不同，请重新核对该扫描。')
    expected_beams = tuple(f'M{i:02d}' for i in range(1,20))
    if any(tuple(sorted(g.beams)) != expected_beams for g in groups if g.serial in row['serials']):
        raise ValueError('有 nfile 缺少或重复 beam，不能作为完整的19-beam组启动。')
    if args.auto == 'required':
        suffixes = ['_1channel_rfi_v2_mask.npz', '_satellite_rfi_1380_mask.npz']
        missing = [str(d/(f.path.stem+suf)) for g in groups if g.serial in row['serials'] for f in g.files
                   for d,suf in zip(auto_dirs,suffixes)
                   if not (d/(f.path.stem+suf)).is_file() or (d/(f.path.stem+suf)).stat().st_size == 0]
        if missing:
            raise ValueError(f'缺少或为空的自动 mask 共 {len(missing)} 个；拒绝静默跳过。首个：{missing[0]}')

    print(f"扫描: idrift={row['idrift']}  {row['relative_dir']}")
    print(f"起始 nfile: {serial}；可用范围: {row['serials'][0]}–{row['serials'][-1]}")
    input_view = (output_root/'_input_views'/manifest['audit_tag']/row['project']/row['scan']/'eligible_T'
                  if row['blocked_serials'] else source)
    print(f'输入源: {source}\n保存: {destination}\n标记版本: {args.version}')
    if row['blocked_serials']:
        print('暂不载入不兼容组: '+', '.join(row['blocked_serials'])+'；使用单独软链接视图，原数据不变。')
    print('自动 RFI: '+('不加载；仅人工标记，界面自动层应为0/19' if args.auto=='none' else '要求本扫描的两类产品齐全'))
    if args.auto == 'required':
        print(f'OneChannel: {auto_dirs[0]}\nSAT1380: {auto_dirs[1]}')
        print('网页默认勾选“使用自动 mask”：显示数据和平均谱排除自动标记样本；已有人工标记继续生效。')
        print('保存仍为人工 mask；两类自动 mask 独立保留，不写入或改变原始 FITS。')
    existing = len(list(destination.glob('*_manual_mask.json'))) if destination.exists() else 0
    print(f'此版本已有 {existing} 个 nfile 的 JSON（文件存在不代表人工复核完成）。')
    if row['hfc_io_error_count']:
        print(f"提示：该扫描有 {row['hfc_io_error_count']} 个 HFC 输入读取问题；请区分定标异常与 RFI。")
        print('详见同目录 CALIBRATION_AUDIT.md；此入口不修改或修复定标数据。')
    interior_missing=row['missing_hfc_counts'].get('interior_or_no_beam',0)
    if interior_missing:
        print(f'提示：该扫描另有 {interior_missing} 个序列内部缺失的 HFC 模式，建议先复核定标输入。')
    if row.get('affected_serials'):
        print('此前定位的关联 nfile: '+', '.join(row['affected_serials'])+'（见报告中的 beam 和文件名）')
    print(f'浏览器: http://127.0.0.1:{args.port}')
    print(f'Mac 连接: ssh -L {args.port}:127.0.0.1:{args.port} CRAFTS')

    contract = {'campaign_root':manifest['data_root'],'input_dir':str(source),
                'mask_dir':str(destination),'mask_version':args.version,'idrift':row['idrift'],
                'automatic_mode':args.auto,'editor':str(editor),'audit_utc':manifest['audit_utc'],
                'input_view':str(input_view),'excluded_serials':row['blocked_serials'],
                'automatic_dirs':dict(zip(['one_channel','sat1380'], map(str, auto_dirs)))}
    action, previous = check_contract(destination, contract)
    if action == 'upgrade':
        print('此版本首次启用自动层：启动前将完整备份旧版本，仅更新 run.json；人工 JSON/NPZ 保持原样。')

    command = [sys.executable, '-B', str(editor), actual[serial].key,
               '--input-dir',str(input_view),'--mask-dir',str(destination),
               '--one-channel-dir',str(auto_dirs[0]),'--sat1380-dir',str(auto_dirs[1]),
               '--address','127.0.0.1','--port',str(args.port)]
    if args.auto == 'required':
        command.append('--require-all-auto-masks')
    if args.check:
        freq, mjd, shape = validate_group(actual[serial])
        print(f'19 beams 轴检查通过：FLUX={shape}，freq={freq[0]:.6f}–{freq[-1]:.6f} MHz。')
    sys.path.insert(0, str(HERE))
    from session_control import resolve_occupied_port
    requested = f"{row['relative_dir']} / {args.version} / nfile {serial} / 保存 {destination}"
    if args.dry_run or args.check:
        print('命令: '+shlex.join(command))
        resolve_occupied_port(args.port, editor, requested, read_only=True)
        print('只读检查结束；没有创建输出目录或启动服务。')
        return

    if not args.prepare_only and not resolve_occupied_port(args.port, editor, requested):
        return
    if input_view != source:
        wanted = {f.path.name:f.path for g in groups if g.serial in row['serials'] for f in g.files}
        input_view.mkdir(parents=True, exist_ok=True)
        for path in input_view.iterdir():
            if path.name not in wanted or not path.is_symlink() or path.resolve()!=wanted[path.name].resolve():
                raise ValueError(f'输入视图存在不匹配的内容，保留现场并退出：{path}')
        for name,target in wanted.items():
            link=input_view/name
            if not link.is_symlink():
                link.symlink_to(target)
    destination.mkdir(parents=True, exist_ok=True)
    if (destination/'.session.lock').is_symlink():
        raise ValueError('会话锁不能是软链接。')
    lock = (destination/'.session.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise ValueError('该扫描的此版本已有会话运行，请连接已有会话或选其他版本。') from exc
    os.set_inheritable(lock.fileno(), True)
    prepare_run_record(destination, contract, output_root/'_backups'/row['project']/row['scan'])
    if args.prepare_only:
        print('目录与配置准备完成；未启动服务、未保存或修改人工标记。')
        return
    sys.stdout.flush()
    os.execv(sys.executable, command)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, KeyError) as error:
        print(f'错误：{error}', file=sys.stderr)
        sys.exit(2)
