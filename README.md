# emailbot — QQ 邮箱求职助手 Bot

每 30 分钟轮询一次 QQ 邮箱, 用 LLM(DeepSeek) 识别求职/招聘相关邮件并私聊推送要点;
邮件中带时间的日程(测评/笔试/AI Coding/AI 面试/面试)自动写入日程表;
时间窗口过长或不明确时会私聊询问你想安排在什么时候;
每天 9:00 推送当天日程; 日程开始前 30 分钟提醒;
也可以直接在 QQ 私聊里用自然语言添加/查询日程。

## 架构

```
QQ邮箱 --IMAP(30min轮询)--> NoneBot2(Python) --DeepSeek--> 邮件分类/时间提取
                                |
NapCatQQ(小号) <--OneBot v11 反向WS--> 私聊通知/询问/命令
                                |
                          SQLite(data/emailbot.db): 邮件账本 / 日程 / 待答问题
```

## 前置条件

1. **Python ≥ 3.11**
2. **QQ 邮箱开启 IMAP**: 网页版 QQ 邮箱 → 设置 → 账户 → 开启 "IMAP/SMTP 服务" → 生成**授权码**(不是 QQ 密码)
3. **DeepSeek API Key**: https://platform.deepseek.com 申请
4. **NapCatQQ**: https://napneko.github.io 下载安装, 登录一个 **QQ 小号**(通知会由小号私聊发给你的主号), 并与主号**互加好友**

## 安装与配置

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    Linux/Mac: source .venv/bin/activate
pip install -e .

cp .env.example .env   # 然后编辑 .env 填入: 主号QQ / 邮箱 / 授权码 / DeepSeek key / ONEBOT_ACCESS_TOKEN(随机长字符串)
```

## NapCat 端配置(关键)

1. 启动 NapCat 并登录小号, 从控制台日志找到 WebUI 地址(形如 `http://127.0.0.1:6099/webui?token=xxxxx`)
2. WebUI → **网络配置 → 新建 → WebSocket 客户端**:
   - URL: `ws://127.0.0.1:8080/onebot/v11/ws`
   - Token: 与 `.env` 中 `ONEBOT_ACCESS_TOKEN` **完全一致**
   - 消息格式: `array`;  `reportSelfMessage`: **关**
3. **先启动本 bot, 再启用 NapCat 该网络配置**(顺序反了会连不上, 重启 NapCat 即可)

## 启动

```bash
python bot.py
```

## 使用

主号给 Bot 小号发私聊:

| 消息 | 作用 |
|---|---|
| `帮助` | 功能说明 |
| `今日` | 今天日程 |
| `日程` | 未来 7 天日程 |
| `取消` | 跳过当前正在询问你的问题 |
| `明天下午3点字节跳动一面` | 自然语言加日程 |
| `周五 19:00-21:00 笔试` | 带时间段的日程 |

收到含时间窗口(如"链接48小时内有效")或时间不明的日程邮件时, bot 会问你打算安排在什么时候,
直接回复如 `明晚7点` 即可, 回复 `取消` 跳过。

## FAQ

- **NapCat 连不上 / 403**: Token 两端不一致; 或 NapCat 先于 bot 启动 —— 重启 NapCat 或重新启用该网络配置。
- **收不到私聊**: 小号与主号没互加好友; 或小号被风控(新号先在手机正常登录养几天)。
- **IMAP 登录失败**: 密码栏填的是**授权码**不是 QQ 密码; 确认已在网页版开启 IMAP/SMTP。
- **LLM 报错 401**: `DEEPSEEK_API_KEY` 无效或欠费。
- **改配置后**: 重启 `python bot.py` 生效。

## 开发

```bash
pip install -e ".[dev]"
```

代码结构: `plugins/` 只放 nonebot 耦合层(命令/私聊入口/定时任务), 业务逻辑在
`pipeline.py`(邮件流水线)、`askq.py`(询问状态机)、`events.py`(日程)等纯 Python 模块, 可脱离框架单测。
