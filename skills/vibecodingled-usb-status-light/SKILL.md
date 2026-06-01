---
name: vibecodingled-usb-status-light
description: Install, detect, configure, and validate the VibeCodingLed ATtiny85 USB AI status light with LED and buzzer reminders. Use when a customer plugs in the VibeCodingLed USB product and asks an AI tool to set it up, install required runtime dependencies, verify USB HID communication, configure AI coding thread monitoring, explain the LED/buzzer meanings, or troubleshoot VID/PID, driver, HID, hook, or status-sync problems.
---

# VibeCodingLed USB 状态灯

## 客户目标

客户只需要做两件事：

1. 把 VibeCodingLed 插到电脑 USB 口。
2. 对 AI 工具说：“读取 VibeCodingLed skill，帮我自动安装并监控 AI 编程状态。”

不要要求客户手动编辑 Hook、JSON、线程 ID、驱动 INF、PlatformIO 配置或 Python 脚本。你作为 AI 代理负责完成检测、安装、配置、验证和解释。

## 必须保持的架构

- 通信方式固定为 USB HID，不改 BLE、Wi-Fi、MQTT、串口或云服务。
- 运行态 USB 设备：`VID_16C0&PID_05DF`，Product 通常为 `01AI VibeCodingLed`。
- Micronucleus bootloader：`VID_16D0&PID_0753`。这是固件上传模式，不是正常客户使用模式。
- 板载 LED：ATtiny85 `PB1`，Digispark/Arduino `P1`，固件常量 `LED_PIN = 1`。
- 蜂鸣器：ATtiny85 `PB0`，Digispark/Arduino `P0`，固件常量 `BUZZER_PIN = 0`。
- 蜂鸣器默认 PWM duty 为 `8`。客户说“响一点”时执行 `scripts\vibe_led.py buzzer louder`；客户说“轻一点”时执行 `scripts\vibe_led.py buzzer quieter`；客户说“恢复默认音量”时执行 `scripts\vibe_led.py buzzer default`。
- USB 通信占用 `PB3/P3` 和 `PB4/P4`，不要把 LED、蜂鸣器或其它外设改到这两个管脚。

## LED 和蜂鸣器状态语义

| AI 总状态 | 下发状态 | LED 效果 | 蜂鸣器效果 | 客户含义 |
| --- | --- | --- | --- | --- |
| 没有需要监控或确认的任务 | `off` | 灯灭 | 静音 | 当前没有任务需要关注 |
| 一个或多个线程都在运行 | `running` | 亮 0.5 秒、灭 0.5 秒 | 静音 | AI 正在工作 |
| 部分线程停止且仍有线程运行 | `partial` | 亮 1.5 秒、灭 0.5 秒 | 每 2 秒短鸣一次，共 3 次 | 有线程完成、失败或中断，同时还有其它线程正在运行 |
| 所有已追踪线程都停止 | `done` | 常亮 | 每 4 秒三连短鸣一轮，共 3 轮 | 所有任务都结束，需要客户查看最终结果 |

完成和异常不区分灯效。`done` 是锁存提醒：所有已追踪线程都停止后保持常亮并三连短鸣 3 轮，直到客户说“我看完了，继续监控”并触发 `thread ack`。`partial/done` 才使用蜂鸣提醒；`off/running` 保持静音。

同一状态被后台重复下发时，蜂鸣器不应重新开始计数；只有输出状态真正变化时才重新触发 3 次/3 轮提醒。

蜂鸣器音量设置保存在 `~/.vibecodingled/settings.json`。`scripts\vibe_led.py` 在下发 `set/thread` 状态时会自动把保存的 `buzzer_pwm_duty` 写入 HID payload 的 `Flags` 字节；固件收到 `Flags=0` 时使用默认 duty `8`。不要让客户手动编辑设置文件。

## 自动安装与检测流程

从本 skill 目录执行命令。优先使用 `scripts/vibe_led.py`，不要重新发明 HID 协议。运行时状态文件默认写入用户目录 `~/.vibecodingled/`，不要写入 skill 交付包目录。

1. 确认 Python 可用：

```powershell
py -3 --version
```

如果 `py` 不存在，尝试 `python --version`。如果都不存在，安装 Python 3 后继续。

2. 安装运行态依赖：

```powershell
py -3 -m pip install --user hidapi
```

3. 检测设备：

```powershell
py -3 scripts\vibe_led.py list
```

预期看到 `16C0:05DF 01AI VibeCodingLed`。如果只看到 `16D0:0753`，等待 5 秒或让客户重新插拔一次；仍然只出现 bootloader 时，产品可能未进入运行态固件。

4. 做四态自检：

```powershell
py -3 scripts\vibe_led.py set off
py -3 scripts\vibe_led.py set running
py -3 scripts\vibe_led.py set partial
py -3 scripts\vibe_led.py set done
py -3 scripts\vibe_led.py status
```

让客户肉眼和听觉确认：灭、亮 0.5 秒灭 0.5 秒、亮 1.5 秒灭 0.5 秒、常亮都正确，`partial/done` 下蜂鸣器会短鸣，并确认 `status` 返回 `error=ok`。最后不要强制关灯；按客户当前监控状态决定是否恢复 `running`、`partial`、`done` 或 `off`。

做完任何直接硬件测试后，必须恢复当前 AI 工具的正常监控状态。Codex 场景下执行 `python scripts\install_codex_monitor.py`，等待几秒后再执行 `python scripts\vibe_led.py status` 和 `python scripts\vibe_led.py thread list`，确认设备回到当前真实线程状态，例如 `state=running error=ok`。

## 蜂鸣器音量调整

默认音量较轻，PWM duty 为 `8`。客户可以用自然语言要求调整，不要要求客户手动输入数字：

| 客户说法 | 执行动作 |
| --- | --- |
| “声音响一点”“蜂鸣器大一点” | `python scripts\vibe_led.py buzzer louder` |
| “声音轻一点”“蜂鸣器小一点” | `python scripts\vibe_led.py buzzer quieter` |
| “恢复默认音量” | `python scripts\vibe_led.py buzzer default` |
| “设置成 16” | `python scripts\vibe_led.py buzzer set 16`，仅在客户明确给出数字时使用 |

可用档位按 `2/4/8/16/24/32/48/64/96/128` 逐级调整。`buzzer get` 可查看当前设置。调整命令会立即保存设置，并按当前线程聚合状态重新下发一次状态；如果当前没有提醒状态，新的音量会在下一次 `partial/done` 提醒时生效。

## 驱动处理规则

- 正常客户使用只需要运行态 HID；Windows/macOS/Linux 通常不需要为 `16C0:05DF` 单独装驱动。
- 不要让客户安装 PlatformIO、Micronucleus 或 Zadig，除非产品进入 bootloader 且明确需要重新刷固件。
- Windows 若显示 `USB\VID_16D0&PID_0753` 且错误码 `28`，这是 bootloader 驱动缺失，不是运行态 HID 缺失。先重新插拔并等待运行态；只有固件维护场景才安装 Digispark/Micronucleus 驱动。
- 如果 `scripts/vibe_led.py list` 找不到设备，先检查 USB 口、数据线、集线器、省电设置，再检查设备管理器中的 VID/PID。

## 配置 AI 线程监控

优先使用当前 AI 工具提供的正式 Hook、自动化、任务列表或线程列表 API。Codex 单独安装时必须可用：当前实现读取 Codex 本机会话事件文件中的 `task_started`、`task_complete`、`turn_aborted`，这比旧的 `task_count` 数量推断可靠。不要把产品解释成 Codex-only；Codex 是必须支持的下限，Cursor 和其它工具仍可通过 Hook 或快照接入。

通用状态网关命令：

```powershell
py -3 scripts\vibe_led.py thread sync --source <tool-name> <active-thread-id-1> <active-thread-id-2>
py -3 scripts\vibe_led.py thread ack --source <tool-name>
py -3 scripts\vibe_led.py thread clear <thread-id>
py -3 scripts\vibe_led.py thread reset
```

推荐规则：

- 定时或事件触发读取当前 active AI 线程快照。
- 把 active 线程 ID 传给 `thread sync --source <tool-name> ...`。
- 同一 `source` 上一轮 running、本轮消失的线程会标记为 stopped；如果还有其它 running 线程，聚合为 `partial`，LED 亮 1.5 秒、灭 0.5 秒，蜂鸣器每 2 秒短鸣一次，共 3 次。
- 如果没有任何 running 线程，但存在 stopped 记录，聚合为 `done`，LED 常亮，蜂鸣器每 4 秒三连短鸣一轮，共 3 轮，直到客户确认。
- 客户说“我看完了”“知道了”“清掉已结束提醒”“继续监控剩余任务”时，优先执行 `thread ack --source <tool-name>`。它只清除 stopped 提醒，保留 running 线程，让 LED 和蜂鸣器回到当前剩余任务状态。
- 只有客户明确说“全部关掉”“停止监控”“清空所有状态”时，才执行 `thread reset`。

如果 AI 工具有事件 Hook：

| Hook 事件 | 调用 |
| --- | --- |
| 线程/会话开始 | `thread start <id> --source <tool-name>` |
| 线程/会话结束或失败 | `thread stop <id> --source <tool-name> --reason completed/problem/stopped` |
| 客户已查看停止提醒 | `thread ack --source <tool-name>` |
| 会话列表快照 | `thread sync --source <tool-name> <active ids...>` |

如果 AI 工具没有 Hook，但能列出活跃线程，使用 5-15 秒一次的快照同步。如果既没有 Hook 也不能列出活跃线程，要如实说明“无法保证自动监控”，不要假装已经配置成功。

## Codex 自动监控

Codex 是必须单独支持的平台。当前环境中，`codex_app.list_threads` 这类线程列表工具不能被普通本机后台脚本调用，因此默认使用随 skill 提供的本机会话事件监控：

```powershell
python scripts\codex_session_monitor.py --once --reset-monitor-state
```

确认能看到 `codex_session ... active_count=<数字>`、`aggregate=running/partial/done/off`、`status: state=... error=ok` 后，安装并启动后台监控：

```powershell
python scripts\install_codex_monitor.py
```

该命令会：

- 停止旧的 `codex_task_count_monitor.py` 进程，避免旧数量推断和新事件监控互相覆盖。
- 启动 `codex_session_monitor.py --interval 1` 后台进程。
- 把 PID 写入 `~/.vibecodingled/codex_session_monitor.pid`。
- 在 Windows 启动目录写入隐藏启动项 `VibeCodingLed-Codex-Monitor.vbs`，下次登录后由 VBS 隐藏调用安装器，再由安装器启动监控并刷新 PID。
- Windows 后台进程和子进程必须使用隐藏窗口方式启动，不能让客户看到反复一闪而过的控制台窗口。

如需临时手动启动，可使用：

```powershell
$script = (Resolve-Path 'scripts\codex_session_monitor.py').Path
$proc = Start-Process -FilePath 'python' -ArgumentList @('-u', $script, '--interval', '1', '--reset-monitor-state') -WindowStyle Hidden -PassThru
New-Item -ItemType Directory -Force "$env:USERPROFILE\.vibecodingled" | Out-Null
Set-Content "$env:USERPROFILE\.vibecodingled\codex_session_monitor.pid" $proc.Id -Encoding ASCII
```

Codex 事件规则：

| Codex 事件 | LED 行为 |
| --- | --- |
| `task_started` | `thread start codex:<thread-id> --source codex`，灯闪烁，蜂鸣器静音 |
| `task_complete` | `thread stop ... --reason completed`；如果还有其它 running，灯亮 1.5 秒、灭 0.5 秒并短鸣 3 次；如果没有 running，灯常亮并三连短鸣 3 轮 |
| `turn_aborted` | `thread stop ... --reason problem`；如果还有其它 running，灯亮 1.5 秒、灭 0.5 秒并短鸣 3 次；如果没有 running，灯常亮并三连短鸣 3 轮 |
| 客户确认已查看 | `thread ack --source codex`；无剩余 running 时灯灭并静音，有剩余 running 时回到当前运行态 |

该监控读取 `~/.codex/state_5.sqlite` 中最近用户可见 Codex 会话的 `rollout_path`，再增量跟踪对应 `rollout-*.jsonl`。默认只纳入 `thread_source=user` 或 `source=vscode` 的会话，避免内部 `exec` 自动任务残留误判为客户正在运行的对话；首次启动只取最近 24 小时内仍未闭合的任务作为当前 running。旧的 `codex_task_count_monitor.py` 仅保留为兼容 fallback，不再作为默认安装路径。

## Cursor 自动监控

安装 Cursor Hooks：

```powershell
python scripts\install_cursor_hooks.py
```

脚本会把 `vibe_led.py` 和 `cursor_hook.py` 复制到：

```text
%USERPROFILE%\.cursor\hooks\vibecodingled
```

并合并或创建：

```text
%USERPROFILE%\.cursor\hooks.json
```

当前 Hook 映射：

| Cursor Hook | LED 网关动作 |
| --- | --- |
| `beforeSubmitPrompt` | `thread start cursor-agent --source cursor` |
| `preToolUse` | `thread start cursor-agent --source cursor` |
| `stop` | `thread stop cursor-agent --source cursor --reason completed/problem/stopped` |
| `sessionEnd` | `thread clear cursor-agent` |

安装后重启 Cursor，让 hooks.json 生效。

## 验收标准

配置完成后必须向客户说明以下结果：

- 是否识别到运行态 `16C0:05DF`。
- `off/running/partial/done` 四态是否都已验证，包括 LED 和蜂鸣器效果。
- 监控源是什么，例如 `codex`、`cursor`、`claude`。
- 当前线程聚合状态是什么。
- 当前蜂鸣器音量 duty 是多少；默认应为 `8`，除非客户要求调大或调小。
- 后台监控是否已启动；如是 Codex，说明 PID 文件位置 `~/.vibecodingled/codex_session_monitor.pid`。
- Cursor hooks 是否已经写入 `~/.cursor/hooks.json`；如写入，提示重启 Cursor 生效。
- 客户以后只需插上产品并让 AI 加载本 skill；无需手动改配置。

## 详细硬件资料

需要解释原理图、接线、使用说明或故障排查时，读取 `references/hardware-and-usage.md`。


