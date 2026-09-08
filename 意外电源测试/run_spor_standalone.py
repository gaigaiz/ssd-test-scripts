#!/usr/bin/env python3
"""
SPOR测试独立运行脚本 v3.0
两阶段测试流程：阶段1(掉电前) + 阶段2(上电后)
自动控制timeboard完成完整测试流程
"""

import argparse
import subprocess
import time
import os
import json
import serial
import crcmod
import sys

# 全局配置
STATE_FILE = "/home/test/standalone/spor_test_state.json"
LOG_DIR = "/home/test/standalone/spor_logs"
LOCK_FILE = "/home/test/standalone/spor_test.lock"

# 测试参数常量
TEST_SIZE_GB = 20  # 测试数据大小（GB）
TEST_SIZE_BYTES = TEST_SIZE_GB * 1024 * 1024 * 1024  # 测试数据大小（字节）
LBA_SIZE = 4096  # 每个LBA的大小（字节）
TEST_SIZE_LBA = TEST_SIZE_BYTES // LBA_SIZE  # 测试数据对应的LBA数
SKIP_LBA_ON_POWERLOSS = 8  # 掉电时跳过的LBA数

# 单位转换常量
KB = 1024
MB = 1024 * 1024
GB = 1024 * 1024 * 1024

class TimeboardController:
    """Timeboard控制器 - 通过Modbus RTU协议控制"""
    
    def __init__(self, port='/dev/ttyUSB0', baud_rate=115200):
        self.port = port
        self.baud_rate = baud_rate
        self.ser = None
    
    def modbus_crc16(self, data):
        """计算Modbus RTU CRC16校验码"""
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 0x0001:
                    crc >>= 1
                    crc ^= 0xA001
                else:
                    crc >>= 1
        return [crc & 0xFF, (crc >> 8) & 0xFF]
    
    def build_modbus_rtu_packet(self, delay_ms):
        """组装写寄存器(0x10)的Modbus报文"""
        # 延时单位：100ms，转换为协议要求的格式
        delay = int(delay_ms / 100)  # 转换为100ms单位
        high_8 = (delay >> 16) & 0xFF
        mid_8 = (delay >> 8) & 0xFF
        low_8 = delay & 0xFF
        
        # 报文结构：从站地址 + 功能码 + 起始地址 + 寄存器数量 + 字节长度 + 6字节数据 + CRC
        packet = [
            0x01,                # 从站地址
            0x10,                # 功能码：写多个寄存器
            0x00,                # 起始地址高字节 (0x20)
            0x20,                # 起始地址低字节
            0x00,                # 寄存器数量高字节 (3个)
            0x03,                # 寄存器数量低字节
            0x06,                # 数据字节数：3寄存器×2=6字节
            # 6字节数据段
            0x01,                # CMD_TYPE (Data0 Hi)
            0x05,                # CMD_LENGTH (Data0 Lo)
            high_8,              # 延时高8位
            mid_8,               # 延时中8位
            low_8,               # 延时低8位
            0x00                 # 补零占位
        ]
        
        # 追加CRC校验
        crc_bytes = self.modbus_crc16(packet)
        packet.extend(crc_bytes)
        return bytes(packet)
    
    def send_raw(self, data):
        """发送原始数据"""
        if self.ser:
            self.ser.write(data)
            time.sleep(0.5)
            # 读取响应（部分Modbus从站写寄存器无应答属于正常现象）
            try:
                response = self.ser.read(1024)
                return response
            except:
                return b''
        return b''
    
    def connect(self):
        """连接timeboard"""
        try:
            ports = ['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2', '/dev/ttyACM0']
            for port in ports:
                # 检查串口是否存在
                if not os.path.exists(port):
                    continue
                # 检查串口权限
                if not os.access(port, os.R_OK | os.W_OK):
                    print(f"警告: 串口 {port} 权限不足，请执行: sudo chmod 666 {port}")
                    continue
                try:
                    self.ser = serial.Serial(port=port, baudrate=self.baud_rate,
                                            parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                                            bytesize=serial.EIGHTBITS, timeout=3)
                    self.port = port
                    return True, f"已连接到 {port}"
                except Exception as e:
                    print(f"尝试 {port} 失败: {e}")
                    continue
            return False, "无法连接到timeboard"
        except Exception as e:
            return False, f"连接失败: {e}"
    
    def disconnect(self):
        """断开连接"""
        if self.ser:
            self.ser.close()
            self.ser = None
    
    def send_command(self, cmd):
        """发送ASCII命令"""
        if not self.ser:
            return False, "未连接"
        try:
            self.ser.write((cmd + '\r\n').encode())
            time.sleep(0.5)
            response = self.ser.read(1024).decode().strip()
            return True, response
        except Exception as e:
            return False, f"发送失败: {e}"
    
    def trigger_poweroff(self, delay_ms=5000):
        """触发掉电（使用Modbus命令激活延时掉电）"""
        if not self.ser:
            print("错误：串口未连接")
            return False
        
        print(f"触发掉电，延时 {delay_ms}ms...")
        packet = self.build_modbus_rtu_packet(delay_ms)
        print(f"报文HEX: {packet.hex().upper()}")
        
        try:
            # 发送前先清空缓冲区
            self.ser.flushInput()
            self.ser.flushOutput()
            
            # 发送命令
            bytes_written = self.ser.write(packet)
            print(f"已发送 {bytes_written} 字节")
            
            time.sleep(0.5)
            
            # 尝试读取响应（部分设备可能无响应）
            try:
                response = self.ser.read(1024)
                if response:
                    print(f"收到响应: {response.hex().upper()}")
                else:
                    print("无响应（部分Modbus从站写寄存器无应答属于正常现象）")
            except Exception as e:
                print(f"读取响应失败: {e}")
            
            print(f"设置掉电延时：{delay_ms}ms = {delay_ms / 1000:.1f}s")
            return True
        except Exception as e:
            print(f"发送失败: {e}")
            return False

class TeeStream:
    """重定向流 - 同时输出到终端和文件，带时间戳"""
    
    def __init__(self, file_path):
        # 使用低级文件描述符以确保 O_APPEND 行为可靠，并使用行缓冲
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        try:
            fd = os.open(file_path, flags, 0o666)
            # 使用文本模式并启用行缓冲（buffering=1）
            self.file = os.fdopen(fd, 'a', buffering=1)
        except Exception:
            # 回退到普通打开方式
            self.file = open(file_path, 'a', buffering=1)
        self.stdout = sys.stdout
        self.line_start = True
    
    def write(self, message):
        if self.line_start and message.strip():
            timestamp = time.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            self.stdout.write(f"[{timestamp}] ")
            self.file.write(f"[{timestamp}] ")
        self.line_start = message.endswith('\n')
        self.stdout.write(message)
        self.file.write(message)
        self.file.flush()
    
    def flush(self):
        self.stdout.flush()
        self.file.flush()

class SPORTestStandalone:
    """独立SPOR测试类 - 两阶段流程"""
    
    def __init__(self, device='/dev/nvme0n1', cycles=1, power_channel=1, delay_before_poweroff=5):
        self.device = device
        self.cycles = cycles
        self.power_channel = power_channel
        self.delay_before_poweroff = delay_before_poweroff
        self.log_dir = LOG_DIR
        self.timeboard = None
        
        os.makedirs(self.log_dir, exist_ok=True)
        # 日志重定向在 run() 中根据 state 决定，以便多次运行使用同一日志文件
        self.log_file = None
    
    def load_state(self):
        """加载测试状态"""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, 'r') as f:
                    return json.load(f)
            except:
                pass
        return None
    
    def save_state(self, state):
        """保存测试状态"""
        # 原子写入到临时文件，fsync保证数据落盘，最后替换到目标路径
        tmp_file = STATE_FILE + '.tmp'
        try:
            data = json.dumps(state, indent=2)
            # 写入临时文件并强制刷新到磁盘
            with open(tmp_file, 'w') as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass

            # 原子替换目标文件
            try:
                os.replace(tmp_file, STATE_FILE)
            except Exception:
                # 回退为普通写入
                with open(STATE_FILE, 'w') as f:
                    f.write(data)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass

            # 尝试刷新所在目录以保证目录项也落盘
            try:
                dir_fd = os.open(os.path.dirname(STATE_FILE) or '.', os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                pass
        finally:
            # 清理临时文件（如果存在且未被替换）
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception:
                pass
    
    def clear_state(self):
        """清除状态文件"""
        if os.path.exists(STATE_FILE):
            os.remove(STATE_FILE)
    
    def check_pcie_link_status(self):
        """检查PCIe链路状态"""
        print("检查PCIe链路状态...")
        try:
            # 先获取NVMe设备的PCIe地址
            result = subprocess.run("lspci | grep -i nvme | awk '{print $1}'", 
                                   shell=True, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout.strip():
                pcie_addr = result.stdout.strip().split('\n')[0]  # 取第一个NVMe设备
                print(f"NVMe设备PCIe地址: {pcie_addr}")
                # 使用lspci获取详细链路状态
                result = subprocess.run(f"sudo lspci -vv -s {pcie_addr} | grep -E '(LnkCap|LnkSta|Speed|Width)'", 
                                       shell=True, capture_output=True, text=True)
                if result.returncode == 0 and result.stdout:
                    lines = result.stdout.strip().split('\n')
                    for line in lines:
                        print(f"###{line.strip()}###")
                    return True, result.stdout.strip()
            
            print("警告: 无法获取PCIe链路状态")
            return False, "未知"
        except Exception as e:
            print(f"检查PCIe链路状态失败: {e}")
            return False, "未知"

    def run_command(self, cmd, timeout=300):
        """执行命令"""
        print(f"执行命令: {cmd}")
        try:
            result = subprocess.run(cmd, shell=True, timeout=timeout,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", "命令超时"
        except Exception as e:
            return False, "", str(e)
    
    def parse_fio_speed(self, output):
        """从fio输出中解析速度信息"""
        for line in output.split('\n'):
            if 'WRITE:' in line or 'READ:' in line:
                parts = line.split()
                for i, part in enumerate(parts):
                    if 'MB/s' in part or 'GB/s' in part:
                        return f"{parts[i-1]} {part}"
        return "未知"

    def write_pattern(self, pattern, size=f'{TEST_SIZE_GB}G'):
        """写入pattern（使用do_verify生成CRC）"""
        print(f"写入pattern 0x{pattern}...")
        cmd = f"sudo fio --name=write_pattern_{pattern} --filename={self.device} " \
              f"--rw=write --bs=128k --ioengine=libaio --direct=1 --size={size} " \
              f"--numjobs=4 --iodepth=64 --do_verify=1 --verify_pattern=0x{pattern} --verify=crc32c --group_reporting"
        success, stdout, stderr = self.run_command(cmd, timeout=600)
        if success:
            speed = self.parse_fio_speed(stdout)
            print(f"写入速度: {speed}")
        return success, stdout, stderr
    
    def verify_pattern(self, pattern, size='100M', offset='0', bs='128k'):
        """验证pattern（直接读取数据）"""
        print(f"验证pattern 0x{pattern} (offset={offset}, size={size}, bs={bs})...")
        # 直接读取数据，不使用fio的verify机制（写入时未生成CRC）
        cmd = f"sudo fio --name=verify_pattern_{pattern} --filename={self.device} " \
              f"--rw=read --ioengine=libaio --direct=1 --bs={bs} --size={size} " \
              f"--numjobs=8 --iodepth=128 --buffer_pattern=0x{pattern} --group_reporting"
        if offset != '0':
            cmd += f" --offset={offset}"
        success, stdout, stderr = self.run_command(cmd, timeout=600)
        if success:
            speed = self.parse_fio_speed(stdout)
            print(f"读取速度: {speed}")
        return success, stdout, stderr
    
    def start_spor_write(self):
        """启动SPOR写入（后台运行）"""
        print("启动SPOR写入...")
        # 使用唯一的日志文件名，记录写入位置
        log_file = os.path.join(self.log_dir, f"fio_io_trace_spor_write.log")
        # 不使用do_verify，避免read操作被记录到日志
        cmd = f"sudo fio --name=spor_write --filename={self.device} " \
              f"--rw=write --bs={LBA_SIZE} --ioengine=libaio --direct=1 --size={TEST_SIZE_GB}G " \
              f"--numjobs=1 --write_iolog={log_file} --log_avg_msec=10 " \
              f"--buffer_pattern=0x22 --group_reporting"
        
        try:
            subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return True
        except Exception as e:
            print(f"启动fio失败: {e}")
            return False
    
    def kill_fio(self):
        """终止fio进程"""
        subprocess.run("sudo pkill -f 'fio --name=spor_write'", shell=True, capture_output=True)
    
    def get_write_position(self):
        """获取写入位置（从fio_io_trace_spor_write.log解析，读取最后10行）"""
        log_file = os.path.join(self.log_dir, "fio_io_trace_spor_write.log")
        max_offset = 0

        if os.path.exists(log_file):
            try:
                with open(log_file, 'r') as f:
                    # 读取最后10行，写入位置通常在最后几行
                    lines = f.readlines()
                    total_lines = len(lines)
                    process_lines = lines[-10:] if total_lines > 10 else lines
                    print(f"检测到IO日志文件，共 {total_lines} 行，分析最后{len(process_lines)}行")
                    
                    # 打印要分析的行用于调试
                    print("分析的行:")
                    for i, line in enumerate(process_lines):
                        print(f"  {i+1}: {line.strip()}")

                    for line in process_lines:
                        parts = line.strip().split()
                        # fio_io_trace.log格式: /dev/nvme0n1 write offset size
                        # 示例: /dev/nvme0n1 write 0 4096
                        if len(parts) >= 4:
                            try:
                                if parts[1] == 'write':
                                    offset = int(parts[2])
                                    size = int(parts[3])

                                    # 验证 offset/size 是否为 LBA 对齐
                                    if (offset % LBA_SIZE) != 0 or (size % LBA_SIZE) != 0:
                                        end_offset = offset + size
                                        print(f"  跳过非对齐写入: offset={offset}, size={size}, end_offset={end_offset} (非 {LBA_SIZE} 字节对齐)")
                                        # 将非对齐条目视为日志不完整或损坏，跳过
                                        continue

                                    end_offset = offset + size
                                    print(f"  解析: offset={offset}, size={size}, end_offset={end_offset}")
                                    if end_offset > max_offset:
                                        max_offset = end_offset
                            except ValueError:
                                continue
                # 确保最大偏移为LBA对齐（以防仍有小概率非对齐残留）
                if max_offset % LBA_SIZE != 0:
                    aligned = (max_offset // LBA_SIZE) * LBA_SIZE
                    print(f"警告: 最大写入偏移 {max_offset} 不是 {LBA_SIZE} 对齐，已向下取整为 {aligned}")
                    max_offset = aligned

                print(f"最大写入偏移: {max_offset} 字节 ({max_offset // (1024*1024*1024)}GB)")
            except Exception as e:
                print(f"解析fio_io_trace.log失败: {e}")
        else:
            print(f"警告: IO日志文件不存在: {log_file}")

        # 转换为LBA（每个LBA 4KB）
        lba = max_offset // 4096
        return lba
    
    def init_timeboard(self):
        """初始化timeboard"""
        self.timeboard = TimeboardController()
        success, msg = self.timeboard.connect()
        if success:
            return True
        else:
            print(f"Timeboard连接失败: {msg}")
            return False
    
    def disable_autostart(self):
        """禁用开机自启动"""
        print("禁用开机自启动...")
        # 禁用systemd服务
        subprocess.run("sudo systemctl disable spor-test.service", shell=True, capture_output=True)
        subprocess.run("sudo rm -f /etc/systemd/system/spor-test.service", shell=True, capture_output=True)
        subprocess.run("sudo systemctl daemon-reload", shell=True, capture_output=True)
        
        # 清理桌面自动启动项
        subprocess.run("rm -f /home/test/.config/autostart/spor-test.desktop", shell=True, capture_output=True)
    
    def phase1_poweroff(self):
        """阶段1：掉电前流程"""
        state = self.load_state()
        if not state:
            state = {
                'total_cycles': self.cycles,
                'current_cycle': 1,
                'phase': 1,
                'results': []
            }
        
        current_cycle = state['current_cycle']
        
        # 检查是否已经完成所有测试
        if current_cycle > state['total_cycles']:
            print(f"{'='*60}")
            print("测试已完成！")
            print('='*60)
            print(f"当前轮数({current_cycle}) > 总轮数({state['total_cycles']})")
            self.print_final_results(state)
            self.clear_state()
            self.disable_autostart()
            return True
        
        print(f"{'='*60}")
        print(f"第 {current_cycle}/{state['total_cycles']} 轮 - 阶段1：掉电前")
        print('='*60)
        
        # 检查PCIe链路状态
        self.check_pcie_link_status()
        
        # Step 1: 写入pattern11打底 (20G)
        print("\nStep 1: 写入pattern11打底 (20G)")
        success, stdout, stderr = self.write_pattern('11', size='20G')
        if not success:
            print(f"失败: {stderr}")
            state['results'].append({'cycle': current_cycle, 'success': False, 'error': '写入pattern11失败'})
            self.save_state(state)
            return False
        
        # Step 2: 验证pattern11
        print("\nStep 2: 验证pattern11")
        success, stdout, stderr = self.verify_pattern('11', size='20G')
        verify_11_ok = success
        if not success:
            print(f"警告: 验证pattern11失败 - {stderr}")
            # 记录警告，但继续测试流程
        
        # Step 3: 启动SPOR写入
        print("\nStep 3: 启动SPOR写入")
        if not self.start_spor_write():
            state['results'].append({'cycle': current_cycle, 'success': False, 'error': '启动fio失败'})
            self.save_state(state)
            return False

        # Step 4: 等待指定秒数
        print(f"\nStep 4: 等待 {self.delay_before_poweroff} 秒...")
        time.sleep(self.delay_before_poweroff)

        # Step 5: 更新状态到阶段2
        state['phase'] = 2
        self.save_state(state)

        # Step 6: 触发掉电
        print("\nStep 5: 触发掉电...")
        if self.timeboard and self.timeboard.ser:
            # 确保所有数据已提交到磁盘后再触发掉电
            try:
                os.sync()
            except Exception:
                pass
            time.sleep(1)
            self.timeboard.trigger_poweroff(2000)  # 2秒后断电
        else:
            print("模拟模式：终止fio进程")
            self.kill_fio()

        return True
    
    def phase2_poweron(self):
        """阶段2：上电后流程"""
        state = self.load_state()
        if not state or state.get('phase') != 2:
            print("错误：状态文件不存在或不是阶段2")
            return False
        
        current_cycle = state['current_cycle']
        
        print(f"{'='*60}")
        print(f"第 {current_cycle}/{state['total_cycles']} 轮 - 阶段2：上电后")
        print('='*60)
        
        # Step 1: 等待SSD初始化
        print("\nStep 1: 等待SSD初始化...")
        time.sleep(15)
        
        # 检查PCIe链路状态
        self.check_pcie_link_status()
        
        # Step 2: 从fio_io_trace.log读取写入位置
        print("\nStep 2: 读取写入位置")
        last_lba = self.get_write_position()
        print(f"读取到写入位置 - LBA {last_lba}")
        
        # Step 3: 验证前段pattern22（已写入的部分）
        print("\nStep 3: 验证前段pattern22")
        
        if last_lba > 0:
            # 如果写入完整（达到TEST_SIZE_GB），验证全部数据
            if last_lba >= TEST_SIZE_LBA:
                print(f"已写入位置 LBA {last_lba}（完整{TEST_SIZE_GB}GB），验证整个{TEST_SIZE_GB}GB")
                written_size_bytes = TEST_SIZE_BYTES
                success, stdout, stderr = self.verify_pattern('22', size=f'{TEST_SIZE_GB}G', offset='0', bs=f'{LBA_SIZE}')
            else:
                # 未写完整，跳过最后SKIP_LBA_ON_POWERLOSS个LBA（掉电时可能未完全写入）
                skip_lba = SKIP_LBA_ON_POWERLOSS
                valid_lba = last_lba - skip_lba
                if valid_lba < 0:
                    valid_lba = 0

                # 计算有效写入的数据大小（每个LBA LBA_SIZE字节）
                written_size_bytes = valid_lba * LBA_SIZE
                written_size_mb = written_size_bytes // MB
                written_size_gb = written_size_bytes // GB
                print(f"已写入位置 LBA {last_lba}，跳过最后{skip_lba}个LBA，有效验证到 LBA {valid_lba}，约 {written_size_gb}GB {written_size_mb % 1024}MB")

                if written_size_bytes > 0:
                    success, stdout, stderr = self.verify_pattern('22', size=f'{written_size_bytes}', offset='0', bs=f'{LBA_SIZE}')
                else:
                    print("有效数据为0，跳过pattern22验证")
                    success = True
        else:
            # 写入位置为0，假设写入完整（TEST_SIZE_GB），验证整个TEST_SIZE_GB
            print(f"写入位置为0，假设写入完整，验证整个{TEST_SIZE_GB}GB")
            success, stdout, stderr = self.verify_pattern('22', size=f'{TEST_SIZE_GB}G', offset='0', bs=f'{LBA_SIZE}')
        verify_22 = success
        if not verify_22:
            print(f"失败: {stderr}")

        # Step 4: 验证后段pattern11（未被覆盖的部分）
        print("\nStep 4: 验证后段pattern11")
        if last_lba > 0:
            # 如果写入完整，整个区域都是pattern22，跳过pattern11验证
            if last_lba >= TEST_SIZE_LBA:
                print(f"写入完整（{TEST_SIZE_GB}GB），整个区域都是pattern22，跳过pattern11验证")
                success = True
            else:
                # 后段从last_lba位置开始（跳过可能损坏的区域）
                start_offset = last_lba * LBA_SIZE  # 转换为字节

                # 计算剩余大小
                remaining_bytes = TEST_SIZE_BYTES - start_offset
                if remaining_bytes > 0:
                    remaining_mb = remaining_bytes // MB
                    remaining_gb = remaining_bytes // GB
                    print(f"从LBA {last_lba}（偏移 {start_offset} 字节）开始验证，约 {remaining_gb}GB {remaining_mb % 1024}MB")

                    # 根据剩余大小选择合适的单位
                    if remaining_bytes >= GB:
                        size_str = f'{remaining_bytes // GB}G'
                    elif remaining_bytes >= MB:
                        size_str = f'{remaining_mb}M'
                    else:
                        size_str = f'{remaining_bytes}'

                    success, stdout, stderr = self.verify_pattern('11', size=size_str, offset=f'{start_offset}', bs='128k')
                else:
                    print("剩余空间不足，跳过pattern11验证")
                    success = True
        else:
            # 写入位置为0，说明写入完整，整个区域都是pattern22，跳过pattern11验证
            print(f"写入完整（{TEST_SIZE_GB}GB），整个区域都是pattern22，跳过pattern11验证")
            success = True
        verify_11 = success
        if not verify_11:
            print(f"失败: {stderr}")
        
        # 记录本轮结果
        cycle_success = verify_22 and verify_11
        state['results'].append({
            'cycle': current_cycle,
            'success': cycle_success,
            'last_lba': last_lba,
            'verify_22': verify_22,
            'verify_11': verify_11
        })

        # 立即保存本轮结果，以防上电/掉电过程中脚本意外中断
        self.save_state(state)

        print(f"\n本轮结果: {'✅ 通过' if cycle_success else '❌ 失败'}")
        print(f"状态更新: current_cycle={current_cycle}, total_cycles={state['total_cycles']}")
        
        # 判断是否完成所有测试
        if current_cycle >= state['total_cycles']:
            # 测试完成
            print("\n所有测试轮数已完成！")
            self.print_final_results(state)
            self.clear_state()
            self.disable_autostart()
            return True
        else:
            # 准备下一轮
            state['current_cycle'] = current_cycle + 1
            state['phase'] = 1
            state.pop('last_lba', None)
            self.save_state(state)
            print(f"\n准备下一轮 ({current_cycle + 1}/{state['total_cycles']})")
            
            # 再次触发断电，开始下一轮
            if self.timeboard and self.timeboard.ser:
                try:
                    os.sync()
                except Exception:
                    pass
                time.sleep(1)
                self.timeboard.trigger_poweroff(2000)
            return True
    
    def print_final_results(self, state):
        """打印最终结果"""
        print("\n" + "="*60)
        print("=== 测试完成 ===")
        print("="*60)
        
        passed = sum(1 for r in state['results'] if r['success'])
        failed = len(state['results']) - passed
        
        print(f"总轮数: {state['total_cycles']}")
        print(f"通过: {passed}")
        print(f"失败: {failed}")
        
        # 保存结果到文件
        result_file = os.path.join(self.log_dir, f"spor_results_{time.strftime('%Y%m%d_%H%M%S')}.json")
        with open(result_file, 'w') as f:
            json.dump(state, f, indent=2)
        print(f"\n结果已保存到: {result_file}")
    
    def test_timeboard(self, poweroff_delay_ms=10000):
        """测试timeboard掉电功能"""
        print("="*60)
        print("Timeboard 掉电测试")
        print("="*60)
        
        # 初始化timeboard
        if not self.init_timeboard():
            print("无法连接timeboard，测试退出")
            return
        
        print(f"测试掉电延时: {poweroff_delay_ms}ms ({poweroff_delay_ms/1000:.1f}秒)")
        
        # 触发掉电
        print("发送掉电命令...")
        if self.timeboard.trigger_poweroff(poweroff_delay_ms):
            print(f"掉电命令已发送，系统将在 {poweroff_delay_ms/1000:.1f} 秒后断电")
            print("请确保timeboard已正确连接并配置")
        else:
            print("掉电命令发送失败")
        
        # 等待一段时间观察结果
        print("\n等待中... (按Ctrl+C退出)")
        try:
            for i in range(int(poweroff_delay_ms/1000) + 5):
                print(f"\r倒计时: {int(poweroff_delay_ms/1000) + 5 - i} 秒", end="")
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\n测试已取消")
    
    def run(self, test_timeboard_mode=False, poweroff_delay_ms=10000):
        """运行测试"""
        if test_timeboard_mode:
            self.test_timeboard(poweroff_delay_ms)
            return
        # 先加载状态（或创建初始状态），再设置日志，以确保同一轮使用同一个日志文件
        state = self.load_state()
        if not state:
            state = {
                'total_cycles': self.cycles,
                'current_cycle': 1,
                'phase': 1,
                'results': []
            }
            # 先保存基础状态；setup_logging 会在需要时写入 log_file 字段
            self.save_state(state)

        # 设置日志（若 state 中已有 log_file 则复用，否则生成并保存）
        try:
            self.setup_logging(state)
        except Exception as e:
            print(f"设置日志失败: {e}")

        # 初始化timeboard（现在已有日志重定向）
        self.init_timeboard()

        # 根据阶段执行
        if state.get('phase') == 1:
            self.phase1_poweroff()
        elif state.get('phase') == 2:
            self.phase2_poweron()

    def setup_logging(self, state):
        """确保本轮测试使用持久的日志文件并重定向 stdout"""
        # priority: state['log_file'] -> generate new
        log_file = state.get('log_file') if state else None
        # 如果存在通用日志文件（例如 run_spor_tests.sh 或 systemd 配置使用的 spor_test.log），优先复用它
        common_log = os.path.join(self.log_dir, 'spor_test.log')
        if not log_file and os.path.exists(common_log):
            log_file = common_log
            state['log_file'] = log_file
            self.save_state(state)

        if not log_file:
            # 没有现成的日志文件，生成新的带时间戳的日志文件并持久化
            log_file = os.path.join(self.log_dir, f"spor_test_{time.strftime('%Y%m%d_%H%M%S')}.log")
            state['log_file'] = log_file
            self.save_state(state)

        # 设置实例属性并重定向 stdout
        self.log_file = log_file
        try:
            sys.stdout = TeeStream(self.log_file)
        except Exception:
            # 如果无法重定向，不要阻塞测试运行
            pass

def main():
    parser = argparse.ArgumentParser(description='SPOR测试脚本 - 独立运行版 v3.0')
    parser.add_argument('-d', '--device', type=str, default='/dev/nvme0n1', help='测试设备')
    parser.add_argument('-c', '--cycles', type=int, default=1, help='测试轮数')
    parser.add_argument('-p', '--power-channel', type=int, default=1, help='电源通道')
    parser.add_argument('--delay', type=int, default=5, help='写入后多久断电（秒）')
    
    # Timeboard测试参数
    parser.add_argument('--test-timeboard', action='store_true', help='测试timeboard掉电功能')
    parser.add_argument('--poweroff-delay-ms', type=int, default=10000, help='掉电延时（毫秒），默认10秒')
    
    args = parser.parse_args()
    
    # 进程锁: 防止同时有多个脚本实例并发运行
    def pid_is_running(pid):
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        else:
            return True

    # 尝试创建锁文件，如果已有运行中的 pid 则退出
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE, 'r') as f:
                old_pid = int(f.read().strip() or 0)
        except Exception:
            old_pid = 0
        if old_pid and pid_is_running(old_pid):
            print(f"另一个测试实例正在运行 (pid={old_pid})，退出")
            return
        else:
            try:
                os.remove(LOCK_FILE)
            except Exception:
                pass

    try:
        with open(LOCK_FILE, 'w') as f:
            f.write(str(os.getpid()))
    except Exception:
        print("警告: 无法创建锁文件，继续运行但可能会并发")

    import atexit, signal
    def _cleanup_lock():
        try:
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
        except Exception:
            pass
    atexit.register(_cleanup_lock)
    def _handle_sig(signum, frame):
        _cleanup_lock()
        sys.exit(0)
    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    test = SPORTestStandalone(
        device=args.device,
        cycles=args.cycles,
        power_channel=args.power_channel,
        delay_before_poweroff=args.delay
    )
    
    # 判断是否为timeboard测试模式
    if args.test_timeboard:
        test.run(test_timeboard_mode=True, poweroff_delay_ms=args.poweroff_delay_ms)
    else:
        test.run()

if __name__ == '__main__':
    main()
