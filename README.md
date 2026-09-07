<div align="center">

# 📊 WeChat Data Insights

**微信聊天记录 · 本地统计与分析**

在自己电脑上，把微信产生的本地聊天记录整理成结构化数据，用本地 Web 界面回顾、统计与导出。数据全程留在本机，不进任何云端。

![macOS][badge-macos]
![Python][badge-python]
![License][badge-license]

[✨ 特性](#-特性) · [🚀 快速开始](#-快速开始) · [🗄️ 数据放哪](#️-数据放哪) · [🛡️ 安全与隐私](#️-安全与隐私) · [❓ 常见问题](#-常见问题)

</div>

---

## 💡 这是什么

微信 4.x 会把本地聊天记录以**私有格式**存储，普通文件阅读器无法直接读取，也不方便统计。
本项目提供一条本地处理链路：

1. **整理** —— 一次性把本机微信账号的本地记录整理为可读的统一格式；
2. **构建** —— 解析成结构化的 SQLite 分析库（自动还原昵称、群名、正文）；
3. **分析** —— 用 Streamlit 本地界面查看消息量、会话、时间趋势、发送者排行，以及聊天正文原文。

> 全程**只在你自己的电脑上、处理你自己的账号数据**，不碰服务器通信、不修改微信行为、数据不出本机。

---

## ✨ 特性

- 🖥️ **完全本地**：数据与处理全部在本机完成，不上传任何云端
- 📦 **仓库零数据**：代码与数据彻底分离——不放真实数据，也不放示例数据，数据一律由你自备、存放在仓库之外
- 🧩 **正文还原**：自动处理多种编码格式的正文，识别文本/表情/图片/视频/语音/群系统消息
- 📊 **可视化分析**：会话 Top、时间热力（小时/星期）、发送者排行、消息明细浏览、CSV 导出
- 🖱️ **一键启动**：macOS 双击 `.command` 即可完成「构建分析库 → 打开界面」

---

## 🗂️ 目录结构

```
WeChat-Decrypt/
├── README.md
├── requirements.txt            # 依赖（streamlit / pandas / plotly / zstandard / jieba 等）
├── config.json                 # 数据路径配置（指向仓库之外）
├── app.py                      # 本地 Web 界面入口（14 个页签）
├── analysis/                   # 数据加载与统计模块
│   ├── __init__.py
│   ├── loader.py               # sqlite -> DataFrame
│   ├── stats.py                # 统计聚合（含词云/情感/双人关系/年度报告等）
│   ├── distill.py              # 聊天人蒸馏：会话 -> 角色人设 SKILL.md
│   └── export.py               # CSV 导出
├── scripts/
│   └── build_analysis.py       # 原始库 -> analysis.db（正文解析/昵称群名映射）
├── tools/
│   └── wcdb-key-tool/          # 本机数据整理工具（上游 MIT 项目）
├── run_wechat_analysis.command # 双击：构建分析库 + 启动界面
└── run_wechat_decrypt.command  # 双击：一次性整理本地记录（需系统授权）
```

---

## 🚀 快速开始

### 环境要求

| 项目 | 要求 |
|---|---|
| 系统 | macOS 12+（实测 15.x 通过） |
| Python | 3.9+（含 pip，自带 venv） |
| 开发者工具 | `xcode-select --install` |
| 微信 | 已登录的桌面版微信 |

### 第一步：整理本地记录（一次性）

直接双击 `run_wechat_decrypt.command`，脚本会自动引导：
1. 退出微信
2. 读取本机微信数据目录（期间会弹出系统授权框，输入管理员密码即可）
3. 重新启动并登录微信
4. 触发一次本账号的数据刷新，脚本在本地把记录整理为统一格式
5. 输出到 `~/WeChatData/wechat_decrypted/`

> 整理出的内容只包含你本机、本人账号的本地记录，仅供你自己使用。

### 第二步：构建分析库

```bash
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

## 🗄️ 数据放哪？（仓库零数据的关键约定）

参照常见开源仓库的做法：**代码入库，数据不入库**。所有数据统一存放在**仓库之外**的 `~/WeChatData/`：

```
~/WeChatData/
├── wechat_decrypted/    # 第一步整理出的统一格式记录
└── analysis.db          # 第二步构建出的分析库
```

| 路径 | 内容 | 谁生成 |
|---|---|---|
| `~/WeChatData/wechat_decrypted/` | 整理后的本地记录 | 第一步脚本 |
| `~/WeChatData/analysis.db` | 构建出的结构化分析库 | `scripts/build_analysis.py` |

如需换位置，修改 `config.json` 中的 `db_path`，或用环境变量 `WE_CHAT_DB` 覆盖。

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
  "db_path": "~/WeChatData/analysis.db",
  "decrypted_dir": "~/WeChatData/wechat_decrypted"
}
```

---

## 📖 界面功能一览

| 页签 | 内容 |
|---|---|
| 总览 | 消息数 / 会话数 / 发送者数 / 时间跨度，按天趋势，类型分布 |
| 会话分析 | Top 会话（昵称/群名）、会话概览表 |
| 行为洞察 | 发送行为聚合：频率、时段偏好、互动习惯 |
| 时间趋势 | 按小时分布、按星期分布 |
| 发送者 | 消息量排行（昵称）、发送明细表 |
| 明细浏览 | 按会话 / 发送者筛选，查看正文 |
| 导出 | 各数据表一键导出 CSV |
| 年度报告 | 分章节深色卡片：总消息/字符/活跃天数/最佳好友/峰值日/连续/熬夜/关键词/收发比 |
| 词云 | 全站 / 单会话 / 单发送者词云，支持蒙版形状，高频词 TopN |
| 日历热力图 | GitHub 风格全年活跃方格，支持年份选择 |
| 情感分析 | 文本消息逐月/逐周情感曲线、正负向占比、最甜/最丧月份 |
| 个性关键词 | 每个发送者相对全站的 TF-IDF 独特词 + 常发高频词 |
| 双人关系 | 双向往复节奏、应答耗时分布、月度轨迹、共同短语、长连击 |
| 人设蒸馏 | 选定会话生成可植入 agent 平台的角色人设 SKILL（见下文「蒸馏」） |

---

## 🛡️ 安全与隐私

- 🔒 **仅本机**：整理产物与分析库含你真实昵称、群名、聊天正文，务必只保存在本机、切勿对外分发，也不要提交进 Git 仓库
- 🗑️ **仓库干净**：本项目不携带任何数据与示例，可放心管理代码
- 🐛 **不改动微信**：只读处理本机本地记录，不修改微信行为、不产生任何网络请求
- 🔑 **授权安全**：管理员授权仅用于读取本机应用数据目录，不涉及任何远程服务

---

## ❓ 常见问题

**Q：会影响我的微信账号吗？**
A：不会。只读取本机已有的本地记录，不修改微信行为，也不向任何服务器发送数据。

**Q：为什么需要管理员密码？**
A：macOS 对应用数据目录设有访问保护，读取时需要系统级授权。

**Q：`analysis.db` 不存在怎么办？**
A：确认第一步已完成并生成 `~/WeChatData/wechat_decrypted/`，再运行 `python3 scripts/build_analysis.py`，或直接双击 `run_wechat_analysis.command`。

**Q：端口被占用？**
A：`streamlit run app.py --server.port 8600`。

**Q：其它平台可以用吗？**
A：本仓库的构建脚本与界面以 macOS 路径约定编写，跨平台使用需自行适配。

---

## 🧰 技术栈

| 层 | 技术 |
|---|---|
| 解析 | Python 3.9+ / SQLite / zstandard |
| 分析展示 | Streamlit / Pandas / Plotly |
| 文本分析 | jieba（分词）/ SnowNLP（情感）/ 手写 TF-IDF |
| 词云渲染 | wordcloud / matplotlib |

---

## 🧬 蒸馏：从聊天会话生成角色人设 SKILL

「人设蒸馏」页签提供一项周边能力：选定某个聊天会话（好友单聊 / 群聊 / 自己）后，从该会话文本中提取角色特征，生成一份 agent 平台可识别的标准 `SKILL.md` 角色人设文档——即把一个人的说话风格"蒸馏"成可供 AI 模仿的指令。

- **纯本地规则**：分词 / 词频 / 语气词 / 标点习惯 / 句长分布 / 活跃时段 / TF-IDF 独特词全部本地计算，**不依赖任何 LLM / API Key**，可离线运行
- **产物形态**：标准 SKILL 文件夹（`SKILL.md` + `assets/`），拷入 agent 平台的 skills 目录即可被识别调用
- **输出位置**：默认写至仓库外 `~/WeChatData/skills/<会话>/`，不进 Git（蒸馏产物含真实聊天文本摘录，遵循"数据不出库"约定）
- **轻量脱敏**：风格例句中的姓名 / 手机号 / 微信号等自动打码

## 📝 更新日志

### 2026-09-07 · v0.2 增强分析 + 人设蒸馏

- **新增 7 个页签**：年度报告、词云、日历热力图、情感分析、个性关键词（TF-IDF）、双人关系、人设蒸馏，连同既有页签共 14 个
- **私聊方向判定修复**：基于 `real_sender_id` + Name2Id 反查精确判定私聊收发方向，私聊对方消息数由旧逻辑的 1285 条提升至 169135 条；群聊方向与旧逻辑 100% 一致
- **pair_stats 修复**：修正双人关系统计把全库私聊本人消息误算入对话者会话内本人消息的问题，修复后与 SQL 直查逐条吻合
- **全新依赖**：`jieba` / `wordcloud` / `matplotlib` / `snownlp`
- **构建脚本**：新增 `--self-wxid` 参数（默认从账号目录自动推导本机 wxid）

---

## 📄 许可证与致谢

- 仓库代码：MIT License
- `tools/wcdb-key-tool/` 为上游开源项目（MIT 许可），README 内注明 contributor 与致谢，见 `tools/wcdb-key-tool/README.md`

## 🤝 贡献

欢迎 Issue 与 PR：正文解析优化、更多图表、跨平台适配等。请确保贡献内容**不含任何真实聊天数据**。

[badge-macos]: https://img.shields.io/badge/platform-macOS-333333?style=flat-square&logo=apple&logoColor=white
[badge-python]: https://img.shields.io/badge/python-3.9%2B-3776AB?style=flat-square&logo=python&logoColor=white
[badge-license]: https://img.shields.io/badge/license-MIT-blue.svg?style=flat-square
