#!/bin/bash
# SPOR测试独立运行脚本 v3.0
# 两阶段测试流程：阶段1(掉电前) + 阶段2(上电后)
# 自动配置开机自启动，完成后自动禁用

# sudo ./run_spor_tests.sh -d /dev/nvme0n1 -c 2   指定设备 运行 2轮

VERSION="3.0"

# 脚本目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# 默认参数
DEVICE="/dev/nvme0n1"
CYCLES=1
DELAY=5

# 状态文件和日志目录
STATE_FILE="/home/test/standalone/spor_test_state.json"
LOG_DIR="/home/test/standalone/spor_logs"
SERVICE_FILE="/etc/systemd/system/spor-test.service"

# 解析参数
while [[ $# -gt 0 ]]; do
    case $1 in
        -d|--device)
            DEVICE="$2"
            shift 2
            ;;
        -c|--cycles|--cycle)
            CYCLES="$2"
            shift 2
            ;;
        --delay)
            DELAY="$2"
            shift 2
            ;;
        -h|--help)
            echo "SPOR测试脚本 v$VERSION"
            echo ""
            echo "用法: $0 [选项]"
            echo ""
            echo "选项:"
            echo "  -d, --device       指定测试设备 (默认: /dev/nvme0n1)"
            echo "  -c, --cycles       测试轮数 (默认: 1)"
            echo "  --delay            写入后多久断电，单位秒 (默认: 5)"
            echo "  -h, --help         显示帮助信息"
            echo ""
            echo "示例:"
            echo "  $0 --cycles 10                    # 运行10轮测试"
            echo "  $0 -d /dev/nvme0n1 -c 5           # 指定设备运行5轮"
            echo "  $0 -c 10 --delay 10               # 设置10秒延时"
            exit 0
            ;;
        *)
            echo "未知选项: $1"
            exit 1
            ;;
    esac
done

# 检查是否以root权限运行
if [[ $EUID -ne 0 ]]; then
    echo "请以root权限运行此脚本"
    echo "使用: sudo $0 $*"
    exit 1
fi

# 检查fio是否安装
if ! command -v fio &> /dev/null; then
    echo "错误: fio未安装，请先安装fio"
    echo "sudo apt-get install fio"
    exit 1
fi

# 安装Python依赖
echo "安装Python依赖..."
pip3 install pyserial crcmod -q

# 设置串口权限
echo "设置串口权限..."
usermod -aG dialout test
chmod 666 /dev/ttyUSB0 2>/dev/null || true

# 设置目录权限
echo "设置目录权限..."
mkdir -p "$LOG_DIR"
mkdir -p "$SCRIPT_DIR"
chown -R test:test "$LOG_DIR"
chown -R test:test "$SCRIPT_DIR"

# 确保日志文件可写
touch "$LOG_DIR/spor_test.log" 2>/dev/null || true
chown test:test "$LOG_DIR/spor_test.log" 2>/dev/null || true
chmod 666 "$LOG_DIR/spor_test.log" 2>/dev/null || true

# 确保状态文件可写
touch "$STATE_FILE" 2>/dev/null || true
chown test:test "$STATE_FILE" 2>/dev/null || true
chmod 666 "$STATE_FILE" 2>/dev/null || true

# 配置test用户免密码sudo
echo "配置sudo权限..."
echo "test ALL=(ALL) NOPASSWD: ALL" | tee /etc/sudoers.d/test_spor > /dev/null
chmod 0440 /etc/sudoers.d/test_spor

# 检查设备是否存在
if [[ ! -b "$DEVICE" ]]; then
    echo "错误: 设备 $DEVICE 不存在"
    exit 1
fi

# 创建目录
mkdir -p "$LOG_DIR"

echo "========================================"
echo "SPOR独立测试脚本 v$VERSION"
echo "两阶段流程：阶段1(掉电前) + 阶段2(上电后)"
echo "========================================"
echo "测试设备: $DEVICE"
echo "测试轮数: $CYCLES"
echo "断电延时: ${DELAY}秒"
echo "========================================"

# 创建初始状态文件
echo "创建初始状态文件..."
cat > "$STATE_FILE" << EOF
{
    "total_cycles": $CYCLES,
    "current_cycle": 1,
    "phase": 1,
    "results": []
}
EOF

# 检测可用的终端程序
TERMINAL_CMD=""
if which gnome-terminal > /dev/null; then
    TERMINAL_CMD="gnome-terminal --"
elif which xterm > /dev/null; then
    TERMINAL_CMD="xterm -e"
elif which xfce4-terminal > /dev/null; then
    TERMINAL_CMD="xfce4-terminal --execute"
elif which konsole > /dev/null; then
    TERMINAL_CMD="konsole -e"
else
    TERMINAL_CMD="xterm -e"
fi

echo "检测到终端: $TERMINAL_CMD"

# 创建桌面自动启动项（用户登录后自动打开终端）
echo "创建桌面自动启动项..."
mkdir -p /home/test/.config/autostart
[ "Desktop Entry"]
cat > /home/test/.config/autostart/spor-test.desktop << EOF
[Desktop Entry]
Type=Application
Name=SPOR Test
Comment=Start SPOR test on login
Exec=$TERMINAL_CMD /bin/bash -c 'cd $SCRIPT_DIR && /usr/bin/python3 $SCRIPT_DIR/run_spor_standalone.py --device $DEVICE --cycles $CYCLES --delay $DELAY 2>&1; exec bash'
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
EOF
chown test:test /home/test/.config/autostart/spor-test.desktop

# 不使用 systemd 服务，改用桌面自启（.desktop）启动终端窗口运行脚本
echo "使用桌面自启 (.desktop) 启动脚本，不创建 systemd 服务。"
echo "如果你想用 systemd，可手动创建/启用服务。"

echo ""
echo "开始第一次测试运行（在桌面终端窗口中）..."
# 根据是否有本地 X DISPLAY 决定运行方式：
# - 无 DISPLAY（例如通过 SSH 远程运行） -> 直接在当前终端运行
# - 有 DISPLAY（本地桌面） -> 在桌面终端中打开运行窗口
if [[ -z "$DISPLAY" ]]; then
    echo "未检测到 DISPLAY，直接在当前终端运行脚本（远程模式）。输出追加到 $LOG_DIR/spor_test.log"
    /usr/bin/python3 "$SCRIPT_DIR/run_spor_standalone.py" --device "$DEVICE" --cycles "$CYCLES" --delay "$DELAY" 2>&1 | tee -a "$LOG_DIR/spor_test.log"
else
    # 如果有 DISPLAY，则尝试以 test 用户在桌面终端打开窗口
    # 使用 test 用户的 XAUTHORITY（若存在），否则直接调用
    XAUTH="/home/test/.Xauthority"
        if [[ -f "$XAUTH" ]]; then
            echo "在桌面终端中以 test 用户打开脚本窗口（使用 XAUTHORITY）..."
            sudo -u test DISPLAY="$DISPLAY" XAUTHORITY="$XAUTH" $TERMINAL_CMD /bin/bash -c "cd \"$SCRIPT_DIR\" && /usr/bin/python3 \"$SCRIPT_DIR/run_spor_standalone.py\" --device \"$DEVICE\" --cycles \"$CYCLES\" --delay \"$DELAY\" 2>&1; exec bash"
        else
            echo "在桌面终端中打开脚本窗口（以 test 用户），但未找到 XAUTHORITY：尝试直接打开终端。"
            sudo -u test $TERMINAL_CMD /bin/bash -c "cd \"$SCRIPT_DIR\" && /usr/bin/python3 \"$SCRIPT_DIR/run_spor_standalone.py\" --device \"$DEVICE\" --cycles \"$CYCLES\" --delay \"$DELAY\" 2>&1; exec bash"
        fi
fi

echo ""
echo "测试脚本已启动！"
echo "后续上电后会自动继续测试，完成后自动禁用自启动"
echo "日志目录: $LOG_DIR"
echo "状态文件: $STATE_FILE"
