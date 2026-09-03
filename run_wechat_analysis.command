#!/bin/bash
# 微信聊天记录分析工具 - 一键启动脚本（macOS 双击运行）
# 仓库不含数据。数据统一存放于 ~/WeChatData/（项目之外）：
#   ~/WeChatData/wechat_decrypted  解密后的明文库（run_wechat_decrypt.command 生成）
#   ~/WeChatData/analysis.db       构建出的分析库（本脚本自动构建后供 UI 读取）
set -e
cd "$(dirname "$0")"

DATA_DIR="$HOME/WeChatData"
ANALYSIS_DB="$DATA_DIR/analysis.db"
DECRYPTED_DIR="$DATA_DIR/wechat_decrypted"

echo "=============================================="
echo " 微信聊天记录分析工具 正在启动..."
echo "=============================================="

if [ ! -d ".venv" ]; then
  echo "[1/4] 首次运行，正在创建虚拟环境 .venv ..."
  python3 -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[2/4] 检查并安装依赖（静默，仅首次较慢）..."
pip install -q --disable-pip-version-check -r requirements.txt

echo "[3/4] 检查分析库..."
if [ -f "$ANALYSIS_DB" ]; then
  echo "  · 已有 $ANALYSIS_DB"
elif [ -d "$DECRYPTED_DIR" ]; then
  echo "  · 检测到解密库 $DECRYPTED_DIR，正在构建 $ANALYSIS_DB ..."
  python3 scripts/build_analysis.py
else
  echo "  · 未找到解密库 $DECRYPTED_DIR，也缺分析库 $ANALYSIS_DB。"
  echo "  · 请先运行 run_wechat_decrypt.command 解密出 ~/WeChatData/wechat_decrypted，再重试。"
  read -r -p "按回车退出..." _
  exit 1
fi

echo "[4/4] 启动分析界面（Streamlit）..."
echo "启动完成后浏览器会自动打开 http://localhost:8501"
echo "按 Ctrl+C 可退出。"
exec streamlit run app.py
