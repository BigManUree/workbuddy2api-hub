# 部署增强脚本（deploy-extras）

在上游 workbuddy2api-hub 之上的本地增强：**`auto` 模型自动调度代理**，以及部署/运维用的小工具。所有密钥均通过环境变量或 `.env` 文件提供，**本仓库不含任何真实密钥**。

## 解决什么问题

上游按「账号 + 模型」计频（code 6004），单模型超限后冷却数小时；网关本身只会轮换身份产品（cli/workbuddy/vscode），**不会自动换模型**。本代理让客户端模型名填 `auto`，请求某个模型被限流时自动顺延到池中的下一个模型，全程对客户端透明。

## 目录

| 路径 | 说明 |
|---|---|
| `failover/wb2api-model-failover.py` | 本地故障转移代理，默认监听 `127.0.0.1:8799` → 网关 |
| `failover/.env.example` | 配置模板，复制为 `.env` 后填写 |
| `failover/wb2api-failover.vbs` | Windows 开机静默自启（pythonw，无黑框） |
| `ssh/wb2api-ssh.py` | paramiko 密码 SSH 小助手（凭据走环境变量） |
| `server/docker-compose.yml` | 服务器实际使用的 compose（宿主 7863 → 容器 8788） |

## 使用方法

### 1. 安装依赖

```bash
pip install requests
```

### 2. 配置

```bash
cd failover
cp .env.example .env
# 编辑 .env，填 WB2API_TARGET（网关地址）和 WB2API_KEY（accounts/settings.json 的 launcher_key）
```

### 3. 启动

```bash
python wb2api-model-failover.py
# Windows 静默运行：pythonw wb2api-model-failover.py
```

启动时会打印调度池，例如 `pool (15 models): glm-5.3, deepseek-v4-pro, ...`。

### 4. 客户端接入

- Base URL：`http://127.0.0.1:8799`
- API Key：与网关相同
- 模型名：`auto`（也支持指定具体模型；该模型被限流时从它开始顺延）

Claude Code 的 `~/.claude/settings.json` 示例：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:8799",
    "ANTHROPIC_API_KEY": "wb-xxxx",
    "ANTHROPIC_MODEL": "auto",
    "CLAUDE_CODE_DISABLE_UNKNOWN_MODEL_WINDOW_ENFORCEMENT": "1"
  }
}
```

### 5. 开机自启（Windows）

把 `failover/wb2api-failover.vbs` 复制到
`%AppData%\Microsoft\Windows\Start Menu\Programs\Startup`，
并按需修改其中的脚本路径。

可选：再注册一个计划任务看门狗，代理意外退出后最多 5 分钟自动拉回
（脚本自带单实例保护，重复运行会直接退出，注册看门狗是安全的）：

```powershell
$action = New-ScheduledTaskAction -Execute 'wscript.exe' -Argument '"C:\...\wb2api-failover.vbs"'
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
Register-ScheduledTask -TaskName 'WB2apiFailoverWatchdog' -Action $action -Trigger $trigger -Settings $settings -Force
```

## 工作机制与踩坑记录

1. **调度池动态生成**：启动时 GET 网关 `/v1/models`，`PRIORITY_MODELS`（能力优先）排前，其余模型自动追加；上游新增模型重启代理即自动入池。拉取失败则用内置 `FALLBACK_CHAIN`（不退出进程）。
2. **限流不是 HTTP 429**：网关对"已知冷却"返回 **HTTP 200 + 错误信封** `{"type":"error","error":{"type":"rate_limit_error","message":"... retry in Ns ... frequency limit"}}`。代理同时识别 429 与该错误信封才会降级。
3. **流式与非流式按 Content-Type 分流**：`text/event-stream` 逐块实时透传（不能缓冲，否则破坏流式并超时）；JSON 响应缓冲后检查内容。
4. **冷却缓存**：从 `retry in Ns` 解析到期时间存入内存，`auto` 时直接跳过冷却中的模型；全部冷却时才向客户端返回最后的限流错误。
5. 测试推理模型时 `max_tokens` 不要设太小，否则额度全用于思考、content 为空（stop_reason=max_tokens）。
6. **pythonw 无控制台陷阱**：pythonw 下 `sys.stdout/stderr` 为 `None`，任何 `print`（包括异常 traceback）都会直接让线程/进程静默死亡。脚本启动时会把 stdio 重定向到脚本旁的 `wb2api-failover.log`（超 1MB 自动轮转），运行日志也写入该文件——代理异常时先看这个日志。
7. **单实例保证**：Windows 的 SO_REUSEADDR 允许两个进程绑定同一端口（连接随机落到其一）。脚本启动时先探测端口，且以 `allow_reuse_address=False` 绑定，重复启动要么探针命中退出、要么绑定失败退出，不会叠加实例。

## 安全建议

- 云安全组中 22 与网关端口不要对 `0.0.0.0/0` 开放，收敛到自己的出口 IP（`x.x.x.x/32`）。
- 面板默认密码 `admin`，登录后立即修改。
- Watchtower 已停止维护（内置 Docker API 1.25 与新版 Docker 不兼容），更新请手动 `docker compose pull && docker compose up -d`。
