<div align="center">

# 🔓 WeChat-Decrypt

**macOS 微信 4.x 聊天记录解密 · 分析 · 可视化**

在自己电脑上，把**你自己的**微信聊天记录解密为明文，一键构建成分析库，用本地 Web 界面查看真实的聊天统计与正文。

![macOS][badge-macos]
![Python][badge-python]
![License][badge-license]
![PR][badge-pr]

[✨ 特性](#-特性) · [🚀 快速开始](#-快速开始) · [⚙️ 原理](#️-工作原理) · [🛡️ 安全与隐私](#️-安全与隐私) · [❓ FAQ](#-faq)

</div>

---

## 💡 这是什么

微信 4.x 的聊天数据库使用 **SQLCipher4（AES-256-CBC + HMAC-SHA512）** 加密，直接打开只会得到乱码。
本项目提供一条完整链路：

1. **解密** —— 从自己电脑运行的微信进程里，合法取出本账号的数据库密钥，把加密库还原成明文 SQLite；
2. **构建** —— 解析明文库（含 zstd 压缩正文、发送者前缀、昵称/群名映射），聚合成统一的 `analysis.db`；
3. **分析** —— 用 Streamlit 本地界面查看消息量、会话、时间趋势、发送者排行，以及**聊天正文原文**。

> 本项目**只在你自己的电脑上解密你自己的账号**，不做批量、不碰服务器通信、不修改微信行为。

---

## ✨ 特性

- 🗝️ **真正解密**：支持微信 4.1.10+（新版本只留 passphrase，老工具全部失效，本工具用 LLDB 断点 + PBKDF2 派生解决）
- 📦 **仓库零数据**：代码与数据彻底分离——不放真实数据，也不放示例数据集，数据一律由你自备、存放在仓库之外
- 🧩 **正文还原**：自动解压 zstd 消息、剥离发送者前缀，文本/表情/图片/视频/语音/群系统消息都能看懂
- 📊 **可视化分析**：会话 Top、时间热力（小时/星期）、发送者排行、消息明细浏览、CSV 导出
- 🖥️ **一键启动**：macOS 双击 `.command` 即可完成「构建分析库 → 打开界面」

---

## 🗂️ 目录结构

```
WeChat-Decrypt/
├── README.md
├── requirements.txt            # 依赖（streamlit / pandas / plotly / zstandard）
├── config.json                 # 数据路径配置（指向仓库之外）
├── app.py                      # Streamlit UI 入口
├── analysis/                   # 数据加载与统计模块
│   ├── __init__.py
│   ├── loader.py               # sqlite -> DataFrame
│   ├── stats.py                # 统计聚合
│   └── export.py               # CSV 导出
├── scripts/
│   └── build_analysis.py       # 明文库 -> analysis.db（正文解压/发送者/昵称群名映射）
├── tools/
│   └── wcdb-key-tool/          # 解密工具（上游 MIT 项目，提取 passphrase + 逐库解密）
├── run_wechat_analysis.command # 双击：构建分析库 + 启动 UI
└── run_wechat_decrypt.command  # 双击：解密引导（本机私有，需 sudo）
```

---

## 🗄️ 数据放哪？（仓库零数据的关键约定）

参照常见开源仓库的做法：**代码入库，数据不入库**。所有数据统一存放在**仓库之外**的 `~/WeChatData/`：

```
~/WeChatData/
├── wechat_decrypted/    # 你本机微信解密后的明文库（第 1 步生成）
└── analysis.db          # 构建出的统一分析库（第 2 步生成）
```

| 路径 | 内容 | 谁生成 |
|---|---|---|
| `~/WeChatData/wechat_decrypted/` | 你本机微信解密后的明文数据库 | `run_wechat_decrypt.command` 或手动 |
| `~/WeChatData/analysis.db` | 构建出的统一分析库 | `scripts/build_analysis.py`（或启动脚本自动构建） |

如需换位置，修改 `config.json` 中的 `db_path`，或用环境变量 `WE_CHAT_DB` 覆盖。

---

## 🚀 快速开始

### 环境要求

| 项目 | 要求 |
|---|---|
| 系统 | macOS 12+（微信 4.1.10+，实测 15.x 通过） |
| Python | 3.9+（含 pip，自带 venv） |
| 命令行工具 | `xcode-select --install`（提供 `lldb`） |
| 微信 | 已登录的桌面版微信 |

### 第一步：解密（一次性，需 sudo 授权）

> 解密依赖你本机微信的登录态，仅对你自己电脑上的本人账号有意义。

**方式 A：一键引导**（推荐）

双击 `run_wechat_decrypt.command`，脚本会依序引导你完成：
1. 退出微信
2. ad-hoc 重签微信（去掉 Hardened Runtime，否则无法读取进程内存）——会弹出系统授权框让你输入管理员密码
3. 重启微信并登录
4. 运行解密工具，并提示你在微信内「设置 → 退出登录 → 重新登录」以触发密钥计算断点
5. 自动把明文库收集到 `~/WeChatData/wechat_decrypted/`

**方式 B：手动命令**

```bash
# 1. 退出微信
osascript -e 'quit app "WeChat"'

# 2. 重签微信为 ad-hoc（去掉 Hardened Runtime，需输入管理员密码）
sudo codesign --force --deep --sign - /Applications/WeChat.app

# 3. 重新启动微信并完成登录
open /Applications/WeChat.app

# 4. 提取 passphrase 并解密（首次会提示你在微信里退出登录再重新登录，触发断点）
cd tools/wcdb-key-tool
sudo python3 wcdb_key_tool_macos.py extract --decrypt
```

> ✅ 抓到的 passphrase 会缓存在 `~/.wcdb-key-tool/wechat-passphrase.json`（权限 600），之后不用重复这一步。
> 📁 把解密出的明文 `.db` 统一放到 `~/WeChatData/wechat_decrypted/` 供下一步使用。

### 第二步：构建分析库

```bash
# 推荐：直接双击 run_wechat_analysis.command，会自动完成构建并打开界面
python3 scripts/build_analysis.py
# 读取 ~/WeChatData/wechat_decrypted，生成 ~/WeChatData/analysis.db
```

### 第三步：启动分析界面

```bash
# 方式一：双击 run_wechat_analysis.command
# 方式二：命令行
streamlit run app.py      # 默认读取 ~/WeChatData/analysis.db
```

浏览器会自动打开 `http://localhost:8501`。

---

## ⚙️ 工作原理

### 解密：为什么老的"内存扫描"失效了

| 版本 | 密钥形态 | 提取方式 |
|---|---|---|
| 微信 4.0.x | 内存中明文缓存 `raw key + salt` | 直接进程内存扫描 `x'<hex>'` 模式 |
| 微信 4.1.10+ | 内存中只留 **passphrase** | LLDB 断点 `CCKeyDerivationPBKDF` + PBKDF2 派生 |

新版本微信把真正的密钥派生推迟到登录时计算、用完即弃，只留一个 passphrase。macOS 版微信的密钥派生调用的是苹果系统库 `CommonCrypto` 的 `CCKeyDerivationPBKDF`——这是公开的系统符号，**无需逆向微信二进制**。用 LLDB 断在该函数上，再利用「退出登录 → 重新登录」触发一次新的计算，从寄存器读出 32 字节 passphrase；随后对每个数据库文件按其 16 字节 salt 做 `PBKDF2-HMAC-SHA512`（256,000 轮）派生专属 AES-256 密钥，并先做 **HMAC 校验**确认密钥正确，再逐页 AES-256-CBC 解密为标准 SQLite。

### 分析：从解密库到可视化

```
明文 message_*.db / session.db / contact.db
        │  build_analysis.py
        │  ① 会话 wxid -> MD5 -> Msg_<hash> 表映射
        │  ② message_content：明文 或 zstd(魔数 28 b5 2f fd) 解压
        │  ③ 剥离 "发送者wxid:\n" 前缀 → 还原正文
        │  ④ 关联 contact/session → 昵称/群名/发送者 display_name
        ▼
     analysis.db （sessions / contacts / senders / messages / meta 五张表）
        │  app.py（Streamlit）
        ▼
     Web 界面：总览 / 会话 / 时间趋势 / 发送者 / 明细 / 导出
```

---

## 🧩 配置

优先级：**环境变量 `WE_CHAT_DB` > `config.json` 的 `db_path` > 默认 `~/WeChatData/analysis.db`**。

```bash
export WE_CHAT_DB=/absolute/path/to/analysis.db
streamlit run app.py
```

`config.json` 示例：

```json
{
  "db_path": "/Users/you/WeChatData/analysis.db",
  "decrypted_dir": "/Users/you/WeChatData/wechat_decrypted"
}
```

---

## 📖 UI 功能一览

| 页签 | 内容 |
|---|---|
| 总览 | 消息数 / 会话数 / 发送者数 / 时间跨度，按天趋势，类型分布 |
| 会话分析 | Top 会话（真实群名/备注）、会话概览表 |
| 时间趋势 | 按小时分布、按星期分布 |
| 发送者 | 消息量排行（真实昵称）、发送明细表 |
| 明细浏览 | 按会话 / 发送者筛选，看**聊天正文原文** |
| 导出 | 各数据表一键导出 CSV |

---

## 🛡️ 安全与隐私

- 🔒 **仅本机**：解密产物与分析库含你真实的昵称、群名、聊天正文，务必只保存在本机、**切勿对外分发**，也**不要提交进 Git 仓库**
- 🗑️ **仓库干净**：本项目不携带任何数据与示例，`.gitignore` 已屏蔽虚拟环境等，可安全开源
- 🐛 **不影响微信**：解密工具只在密钥计算瞬间附加调试器读取一次参数，随即 detach，不修改微信行为、不接触网络协议
- ⚠️ **重签说明**：为读取微信进程内存，需将微信临时重签为 ad-hoc；微信后续自动更新会恢复官方签名，不影响正常使用
- 🔑 **密钥安全**：passphrase 仅存本机 `~/.wcdb-key-tool/`（权限 600），请勿外泄

---

## ❓ FAQ

**Q：会封号吗？**
A：不会。只在密钥计算一瞬间读取一次寄存器/内存，不修改微信行为、不接触微信服务器通信。

**Q：微信更新后还能用吗？**
A：macOS 断的是系统函数，与微信自身版本无关，理论更稳定；但微信更新恢复官方签名后，需重新执行一次重签步骤。

**Q：为什么需要 sudo？**
A：调试器附加其他进程需要 `task_for_pid` 权限，macOS 下需管理员授权。

**Q：`analysis.db` 不存在怎么办？**
A：确保已完成第 1 步解密并生成 `~/WeChatData/wechat_decrypted/`，再运行 `python3 scripts/build_analysis.py`，或直接双击 `run_wechat_analysis.command`。

**Q：端口被占用？**
A：`streamlit run app.py --server.port 8600`。

**Q：Windows / Linux 能用吗？**
A：解密工具支持三平台（Linux 已验证、Windows 为实验性）；但本仓库的构建脚本与 UI 以 macOS 路径约定编写，跨平台使用需自行适配路径。

---

## 🧰 技术栈

| 层 | 技术 |
|---|---|
| 解密 | CommonCrypto / LLDB / PBKDF2-HMAC-SHA512 / SQLCipher4 |
| 解析 | Python 3.9+ / SQLite / zstandard |
| 分析展示 | Streamlit / Pandas / Plotly |

---

## 📄 许可证与致谢

- 仓库代码：MIT License
- 解密工具 `tools/wcdb-key-tool/` 为上游 [wcdb-key-tool](https://github.com/) 项目（MIT 许可），README 内注明 contributor 与致谢，见 `tools/wcdb-key-tool/README.md`

## 🤝 贡献

欢迎 Issue 与 PR：正文解析优化、更多图表、跨平台适配、Windows 解密验证等。请确保贡献内容**不含任何真实聊天数据**。

[badge-macos]: https://img.shields.io/badge/platform-macOS-333333?style=flat-square&logo=apple&logoColor=white
[badge-python]: https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white
[badge-license]: https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square
[badge-pr]: https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat-square
*（内容由AI生成，仅供参考）*
