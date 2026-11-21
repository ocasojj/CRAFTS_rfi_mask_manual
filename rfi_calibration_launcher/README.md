# 定标批次的一键标记入口

Activate the Python environment before starting the launcher. The wrapper uses
`python3`, or the interpreter selected by `CRAFTS_PYTHON`. The editor defaults
to the copy in this checkout. Set `CRAFTS_AUTO_ROOT` (or `--auto-root`) for
automatic products and `CRAFTS_OUTPUT_ROOT` (or `--output-root`) for manual
outputs. The default output directory is `rfi_runs` in the working directory.

## 本地配置

将经过核对的扫描清单放在本目录的 `scans.json`。格式见 `scans.example.json`；模板不是实际核验结果，默认 `generation_complete=false`，不能直接启动标记。已有服务器的 `scans.json` 和 `CALIBRATION_AUDIT.md` 应原样保留；它们不纳入 Git。

清单需要指定：

- `data_root`：定标 FITS 根目录；`relative_dir`：该条带相对目录。
- `all_serials`：输入目录中的全部 nfile；`serials`：可供 Web 加载的 nfile。
- `blocked_serials`：因轴或结构不兼容而暂不加载的编号。入口为其余编号建立软链接视图，不改动 FITS。
- Optional `short_tail_records` maps eligible nfile IDs to shortened beam IDs and their observed record counts. For a listed nfile, `--check` verifies the timestamp prefix against the longest beam.
- `generation_complete` 及审计字段：按真实检查结果填写；示例中的数量和零值均是占位值。

## 启动与预检

```bash
bash run_rfi_calibration.sh --list
bash run_rfi_calibration.sh <idrift或完整条带名> v1 0001 --port 8992
bash run_rfi_calibration.sh <idrift> v1 0001 --dry-run
bash run_rfi_calibration.sh <idrift> v1 0001 --check
```

省略版本和编号时仍为 `v1 0001`。同版本续读已有人工标记；新版本从空白开始。`--output-root`、`--editor` 和 `--auto-root` 可覆盖部署路径。

默认加载两类自动 mask，目录结构为：

```text
<auto-root>/<project>/<scan>/one_channel_rfi_v2_products/
<auto-root>/<project>/<scan>/satellite_rfi_1380_products/
```

入口检查两类产品是否齐全，Web 加载时检查形状及频率/时间轴。旧人工版本首次启用自动层前会完整备份并校验旧文件，然后仅更新 `run.json`；不会清空人工标记。网页保存仍只导出人工 JSON/NPZ。

`--dry-run` 和 `--check` 完全只读；后者额外检查所选组的 FITS 轴。`--prepare-only` 准备目录、来源记录及必要备份，但不启动服务。`--auto none` 是显式人工模式；同版本不能静默切换自动层来源。

## 端口已有会话

脚本识别本用户的 RFI 进程，显示当前条带、nfile、保存目录和未保存状态，再提供：

```text
1) 继续使用旧会话
2) 安全重启，按本次命令指定的条带 / 版本 / nfile 启动
0) 取消，保留旧会话（默认）
```

有未保存修改、正在加载/保存/切换、进程不匹配或状态无法验证时，不会结束旧服务。关闭由旧服务在状态锁内再次核对后执行；不使用强制 kill。非交互运行不会自动选择重启，通过 SSH 直接执行时需要分配交互终端。

升级前已经运行的旧进程不具备安全关闭接口：先在网页保存，再在原终端正常退出一次。之后启动的新会话支持上述菜单。控制凭据仅保存在本用户的 `~/.cache/crafts-rfi/sessions/`，不会通过网页状态接口返回。

Mac 本地建立转发后打开对应网页：

```bash
ssh -N -L 8992:127.0.0.1:8992 YOUR_SSH_HOST
```

同一扫描、同一 mask 版本仍只允许一个会话；换端口不会绕过版本锁。

## 已核验的历史人工标记

`legacy_import.json` 记录一次性导入的来源、nfile 清单和复制校验信息。启动时会提示曾承接多少个旧人工 nfile，随后按当前版本目录正常载入；不会在每次启动时重新拷贝或覆盖后续标注。独立版本（如 v2）不会自动继承 v1 的导入结果。

历史人工结果复用前必须核对观测日期、频率/MJD/shape 和各 beam 的 mask 内容。导入使用独立副本，保留原始 JSON/NPZ 及其历史来源记录；当前 Web 加载时重新绑定当前 FITS 路径与当前批次自动层，下一次人工保存时正常更新来源元数据。旧自动 mask 不作为当前批次的替代品。

若存在 `legacy_import_pending.json`，入口拒绝启动，以免把尚未完整发布的导入当成完成状态。该文件应由成功完成校验的导入过程移除，不能靠手动删除来跳过未完成的导入。

## 验证

在仓库根目录运行：

```bash
node --test test_web_zoom_ui.cjs
python -B test_native_navigation.py
python -B rfi_calibration_launcher/test_launcher.py
python -B rfi_calibration_launcher/test_session_control.py
```

测试使用临时文件或模拟会话；不需要正式 FITS，也不会结束真实标注进程。
