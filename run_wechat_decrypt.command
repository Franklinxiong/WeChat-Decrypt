#!/bin/bash
# ============================================================
# 微信聊天记录解密引导（本机私有版）
# 用途：引导你完成「重签微信 -> 提取 passphrase -> 解密本机库」，
#       解密结果写入 ~/WeChatData/wechat_decrypted（仓库之外），
#       之后由 run_wechat_analysis.command 构建分析库并启动 UI。
# 注意：需要 sudo 与微信内操作，无法全自动；仅在本人 Mac 上对本人账号有效。
# ============================================================

cd "$(dirname "$0")" || exit 1
X=$(pwd)
TOOL="$X/tools/wcdb-key-tool"
WX=/Applications/WeChat.app
DATA_DIR="$HOME/WeChatData"
DECRYPTED_DIR="$DATA_DIR/wechat_decrypted"

echo "=============================================="
echo " 微信聊天记录解密引导（本机私有）"
echo "=============================================="

# 0) 工具检查
if [ ! -f "$TOOL/wcdb_key_tool_macos.py" ]; then
  echo "[错误] 未找到 $TOOL/wcdb_key_tool_macos.py"
  read -r -p "按回车退出..." _; exit 1
fi

# 1) 确认微信已退出
if pgrep -x WeChat >/dev/null 2>&1; then
  echo "[1/5] 微信仍在运行，先退出..."
  osascript -e 'quit app "WeChat"' 2>/dev/null
  sleep 2
fi
pgrep -x WeChat >/dev/null 2>&1 && osascript -e 'tell application "WeChat" to quit' >/dev/null 2>&1
sleep 1
echo "  · 微信已退出。"

# 2) 重签微信（去 Hardened Runtime）
echo "[2/5] 重签微信为 ad-hoc（需输入管理员密码，弹系统授权框）..."
sudo codesign --force --deep --sign - "$WX"
# shellcheck disable=SC2181
if [ $? -ne 0 ]; then
  echo "[错误] 重签失败。请确认系统授权框已输入密码。"
  read -r -p "按回车退出..." _; exit 1
fi
echo "  · 重签完成（已去 Hardened Runtime）。"

# 3) 重启微信并登录
echo "[3/5] 正在启动微信，请确保完成登录（先不要退出登录）..."
open "$WX"
sleep 6
echo "  · 已启动微信。"

# 4) 运行提取+解密
echo "[4/5] 运行 wcdb-key-tool 提取 passphrase 并解密（需再次输入管理员密码）..."
echo "  · 脚本提示时，请在微信内操作：设置 -> 退出登录 -> 重新登录（用于触发断点）"
sudo python3 "$TOOL/wcdb_key_tool_macos.py" extract --decrypt
# shellcheck disable=SC2181
if [ $? -ne 0 ]; then
  echo "[提示] 提取/解密未成功完成，请查看上方输出。"
  read -r -p "按回车退出..." _; exit 1
fi

# 5) 收集产物到仓库之外
echo "[5/5] 收集解密产物到 $DECRYPTED_DIR ..."
mkdir -p "$DECRYPTED_DIR"
# wcdb-key-tool 各子命令默认输出位置可能不同，这里尽量汇总所有明文 db
find "$HOME/Library/Containers/com.tencent.xinWeChat" \
  -name "*.db" -type f -newer "$WX" 2>/dev/null | head -200 | while read -r f; do
  cp -n "$f" "$DECRYPTED_DIR/" 2>/dev/null
done

# 校验
if ls "$DECRYPTED_DIR"/*.db >/dev/null 2>&1; then
  echo "  · 已在 $DECRYPTED_DIR 找到 $(ls "$DECRYPTED_DIR"/*.db | wc -l | tr -d ' ') 个库。"
  echo "    下一步：双击 run_wechat_analysis.command 构建分析库并启动 UI。"
else
  echo "  · 未找到明文库。请把微信的底层 db 手动复制到 $DECRYPTED_DIR/ 后，再运行 run_wechat_analysis.command。"
  echo "    查找提示：wcdb-key-tool 的输出位置请查看 $TOOL/README.md"
fi

echo "=============================================="
echo " 完成。"
echo "=============================================="
read -r -p "按回车退出..." _
