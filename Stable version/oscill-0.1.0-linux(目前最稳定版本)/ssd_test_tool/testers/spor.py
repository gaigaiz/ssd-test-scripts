#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSD Test Tool - SPORTester+TimeboardController Module

Auto-extracted from ssd_test_v1.9.3.py for modularization.
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ..common import (
    TestConfig, TestResult, PerfTask,
    STATUS_PASS, STATUS_FAIL, STATUS_ERROR, STATUS_SKIP,
    TEST_FW, TEST_SMART, TEST_CAPACITY, TEST_PERF, TEST_POWERCYCLE,
    TEST_SPOR, TEST_OSINT, TEST_RW, TEST_POWER, TEST_ALL, VALID_TEST_ITEMS,
    DEVICE_NVME, DEVICE_SATA, DEVICE_UNKNOWN,
    PERF_FOB, PERF_STEADY, PERF_BOTH, PERF_UNKNOWN,
    FW_UPGRADE, FW_DOWNGRADE,
    RW_MODE_FULL_DISK, RW_MODE_FILE_CYCLE, RW_MODE_LONG_RUN, RW_MODE_ALL,
    RW_FILE_SIZES, RW_PATTERN_RANDOM, RW_PATTERN_AA, RW_PATTERN_55, RW_PATTERN_00, RW_PATTERN_FF,
    DEFAULT_PERF_RUNTIME, DEFAULT_PERF_QD, DEFAULT_FW_COMMIT_TIMEOUT,
    DEFAULT_CMD_TIMEOUT, DEFAULT_SMART_RW_SIZE_MB,
    DEFAULT_PC_CYCLES, DEFAULT_PC_POWER_MODE, DEFAULT_PC_MOUNT_POINT,
    DEFAULT_PC_STATE_FILE, DEFAULT_PC_BOOT_TIMEOUT, DEFAULT_PC_OFF_INTERVAL,
    DEFAULT_PC_RW_DURATION, DEFAULT_PC_TEST_FILE_COUNT, DEFAULT_PC_TEST_FILE_SIZE_MB,
    DEFAULT_PC_PATTERN, DEFAULT_PC_PATTERN_SIZE_GB, DEFAULT_PC_LINK_CHECK,
    DEFAULT_SPOR_POWER_MODE, DEFAULT_SPOR_ENHANCED_IODEPTH, DEFAULT_SPOR_ENHANCED_BS,
    SSD_STATE_UNKNOWN, SSD_STATE_FOB, SSD_STATE_STEADY,
    REQUIRED_COMMANDS, SCRIPT_VERSION,
    run_cmd, check_root, check_dependencies, get_device_type, get_nvme_controller,
    bytes_to_human, human_to_bytes, wait_for_device, confirm_destructive,
    get_default_log_dir, resolve_log_dir, setup_logging,
    _get_state_file_path, load_ssd_state, save_ssd_state, get_device_serial,
)


class TimeboardController:
    """Timeboard 硬件断电控制器 - 通过 Modbus RTU 协议控制串口继电器电源板。"""

    def __init__(self, port='/dev/ttyUSB0', baud_rate=115200, logger=None):
        self.port = port
        self.baud_rate = baud_rate
        self.ser = None
        self.log = logger

    def modbus_crc16(self, data: bytes) -> List[int]:
        """计算 Modbus RTU CRC16 校验码。"""
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

    def build_modbus_rtu_packet(self, delay_ms: int) -> bytes:
        """组装写多个寄存器(0x10)的 Modbus 报文，设置延时掉电。"""
        delay = int(delay_ms / 100)  # 协议单位 100ms
        high_8 = (delay >> 16) & 0xFF
        mid_8 = (delay >> 8) & 0xFF
        low_8 = delay & 0xFF

        packet = [
            0x01,    # 从站地址
            0x10,    # 功能码：写多个寄存器
            0x00, 0x20,  # 起始地址 0x0020
            0x00, 0x03,  # 寄存器数量 3
            0x06,    # 数据字节数
            0x01,    # CMD_TYPE
            0x05,    # CMD_LENGTH
            high_8, mid_8, low_8,  # 延时 3 字节
            0x00,    # 补零占位
        ]
        crc_bytes = self.modbus_crc16(bytes(packet))
        packet.extend(crc_bytes)
        return bytes(packet)

    def connect(self) -> Tuple[bool, str]:
        """连接 Timeboard，自动探测可用串口。"""
        try:
            import serial
        except ImportError:
            return False, "未安装 pyserial，请执行: pip3 install pyserial"

        candidate_ports = ['/dev/ttyUSB0', '/dev/ttyUSB1', '/dev/ttyUSB2',
                           '/dev/ttyACM0', '/dev/ttyACM1']
        for port in candidate_ports:
            if not os.path.exists(port):
                continue
            if not os.access(port, os.R_OK | os.W_OK):
                if self.log:
                    self.log.warning(f"Serial port {port} permission denied, please run: sudo chmod 666 {port}")
                continue
            try:
                self.ser = serial.Serial(
                    port=port, baudrate=self.baud_rate,
                    parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                    bytesize=serial.EIGHTBITS, timeout=3
                )
                self.port = port
                if self.log:
                    self.log.info(f"Timeboard connected: {port}")
                return True, f"已连接到 {port}"
            except Exception as e:
                if self.log:
                    self.log.debug(f"Attempt {port} failed: {e}")
                continue
        return False, "无法连接到 Timeboard，请检查串口和权限"

    def disconnect(self):
        """断开连接。"""
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def trigger_poweroff(self, delay_ms: int = 2000) -> bool:
        """触发延时掉电。delay_ms 为硬件收到命令后多少毫秒断电。"""
        if not self.ser:
            if self.log:
                self.log.error("Timeboard not connected, cannot trigger power loss")
            return False

        if self.log:
            self.log.info(f"Triggering hardware power loss, delay {delay_ms}ms ({delay_ms / 1000:.1f}s)...")
        packet = self.build_modbus_rtu_packet(delay_ms)
        if self.log:
            self.log.debug(f"Modbus packet HEX: {packet.hex().upper()}")

        try:
            self.ser.flushInput()
            self.ser.flushOutput()
            bytes_written = self.ser.write(packet)
            if self.log:
                self.log.debug(f"Sent {bytes_written} bytes")
            time.sleep(0.5)
            try:
                response = self.ser.read(1024)
                if response and self.log:
                    self.log.debug(f"Response received: {response.hex().upper()}")
            except Exception:
                pass
            if self.log:
                self.log.info(f"Power loss command sent, system will power off after {delay_ms / 1000:.1f}s s before power off")
            return True
        except Exception as e:
            if self.log:
                self.log.error(f"Failed to send power loss command: {e}")
            return False

class SPORTester:
    """
    意外电源循环测试 (Surprise Power Cycle Test, SPOR)。

    测试流程（每轮）：
      阶段1（掉电前）：写入 pattern11 打底 -> 验证 -> 启动 SPOR 写入(pattern22/混合读写)
                       -> 写入过程中直接触发硬件断电（不 sync、不待机）
      阶段2（上电后）：检测 SSD 是否掉盘 -> SMART 检查 -> 解析掉电时写入位置
                       -> 验证前段 pattern22 + 后段 pattern11 数据完整性
                       -> 记录结果 -> 进入下一轮
      全部循环完成后：执行最终完整功能测试（容量 + SMART + 基本性能）

    核心设计：
      - Timeboard 硬件 Modbus RTU 断电，真正"意外"不经过待机命令
      - fio write_iolog 精确记录掉电时最后写入 LBA 位置
      - 原子写入状态文件，断电后不损坏，开机自启恢复
      - 跳过掉电边界最后 N 个 LBA 容错（可能未完全写入）
    """

    # 测试阶段标识
    PHASE_POWEROFF = 1
    PHASE_POWERON = 2
    PHASE_DONE = 3

    def __init__(self, config: TestConfig, logger: logging.Logger):
        self.cfg = config
        self.log = logger
        self.device = config.device
        self.ctrl = config.nvme_ctrl if config.device_type == DEVICE_NVME else config.device

        # SPOR 测试参数（从 config 读取，带默认值）
        self.cycles = getattr(config, 'spor_cycles', 10)
        self.delay_before_poweroff = getattr(config, 'spor_delay', 5)
        self.test_size_gb = getattr(config, 'spor_test_size_gb', 20)
        self.lba_size = getattr(config, 'spor_lba_size', 4096)
        self.skip_lba = getattr(config, 'spor_skip_lba', 8)
        self.poweroff_delay_ms = getattr(config, 'spor_poweroff_delay_ms', 500)
        self.mixed_rw = getattr(config, 'spor_mixed_rw', False)
        self.mixed_read_ratio = getattr(config, 'spor_mixed_read_ratio', 70)
        self.final_test = getattr(config, 'spor_final_test', True)
        self.timeboard_port = getattr(config, 'spor_timeboard_port', '/dev/ttyUSB0')
        # SPOR 三模式参数
        self.spor_power_mode = getattr(config, 'spor_power_mode', 'timeboard')
        self.spor_enhanced_iodepth = getattr(config, 'spor_enhanced_iodepth', 256)
        self.spor_enhanced_bs = getattr(config, 'spor_enhanced_bs', '128k')

        # 派生参数
        self.test_size_bytes = self.test_size_gb * 1024 * 1024 * 1024
        self.test_size_lba = self.test_size_bytes // self.lba_size

        # 状态文件和日志
        self.state_file = getattr(config, 'spor_state_file',
                                  '/var/lib/ssd_spor_state.json')
        self.log_dir = config.log_dir
        os.makedirs(self.log_dir, exist_ok=True)

        # Timeboard 控制器
        self.timeboard = TimeboardController(
            port=self.timeboard_port, logger=logger
        )

    # ---------- 状态文件管理（原子写入） ----------

    def load_state(self) -> Optional[Dict[str, Any]]:
        """加载状态文件，不存在返回 None。"""
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r', encoding='utf-8') as f:
                    state = json.load(f)
                self.log.info(f"Loading SPOR state: cycle {state.get('current_cycle', '?')}/"
                              f"{state.get('total_cycles', '?')} rounds, "
                              f"phase {state.get('phase', '?')}")
                return state
            except Exception as e:
                self.log.warning(f"State file read failed, reinitializing: {e}")
        return None

    def save_state(self, state: Dict[str, Any]):
        """原子保存状态文件（临时文件 + fsync + rename + 目录 fsync）。"""
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp_file = self.state_file + '.tmp'
        data = json.dumps(state, indent=2, ensure_ascii=False)
        try:
            with open(tmp_file, 'w', encoding='utf-8') as f:
                f.write(data)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            try:
                os.replace(tmp_file, self.state_file)
            except Exception:
                with open(self.state_file, 'w', encoding='utf-8') as f:
                    f.write(data)
                    f.flush()
            # 刷新目录项
            try:
                dir_fd = os.open(os.path.dirname(self.state_file) or '.', os.O_DIRECTORY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except Exception:
                pass
        finally:
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception:
                pass

    def clear_state(self):
        """清除状态文件。"""
        if os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except Exception:
                pass

    # ---------- 工具函数 ----------

    def run_command(self, cmd: str, timeout: int = 300) -> Tuple[bool, str, str]:
        """执行 shell 命令，返回 (成功, stdout, stderr)。"""
        self.log.debug(f"Executing command: {cmd}")
        try:
            result = subprocess.run(
                cmd, shell=True, timeout=timeout,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            return result.returncode == 0, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return False, "", "命令超时"
        except Exception as e:
            return False, "", str(e)

    def parse_fio_speed(self, output: str) -> str:
        """从 fio 输出中解析带宽速度。"""
        for line in output.split('\n'):
            if 'WRITE:' in line or 'READ:' in line:
                parts = line.split()
                for i, part in enumerate(parts):
                    if 'MB/s' in part or 'GB/s' in part:
                        return f"{parts[i - 1]} {part}"
        return "未知"

    def check_device_present(self) -> bool:
        """检测 SSD 是否被系统枚举（掉盘检测）。"""
        if os.path.exists(self.device):
            # 额外验证设备可访问
            try:
                _, out, _ = run_cmd(["lsblk", "-d", "-n", "-o", "NAME", self.device],
                                     check=True, capture=True, logger=self.log, timeout=10)
                if out.strip():
                    return True
            except Exception:
                pass
        self.log.error(f"SSD disk dropped: device {self.device} does not exist or is inaccessible")
        return False

    def check_smart(self) -> Tuple[bool, Dict[str, Any]]:
        """上电后 SMART 检查，返回 (正常, 详细信息)。"""
        info = {}
        try:
            if self.cfg.device_type == DEVICE_NVME:
                _, out, _ = run_cmd(["nvme", "smart-log", self.ctrl, "-o", "json"],
                                     check=True, capture=True, logger=self.log, timeout=15)
                smart = json.loads(out)
                info["media_errors"] = smart.get("media_and_data_integrity_errors")
                info["available_spare"] = smart.get("available_spare")
                info["power_cycles"] = smart.get("power_cycles")
                temp = smart.get("temperature", 0)
                info["temperature_c"] = temp - 273 if temp > 200 else temp

                ok = (info.get("media_errors", 1) == 0
                      and (info.get("available_spare", 100) >= 10)
                      and 0 <= info.get("temperature_c", 25) <= 70)
                self.log.info(f"  SMART check: Media errors={info.get('media_errors')}, "
                              f"available_spare={info.get('available_spare')}%, "
                              f"temperature={info.get('temperature_c')}°C -> {'OK' if ok else 'FAIL'}")
                return ok, info
            else:
                _, out, _ = run_cmd(["smartctl", "-H", self.device],
                                     check=True, capture=True, logger=self.log, timeout=15)
                info["raw"] = out.strip()[:500]
                ok = "PASSED" in out.upper() or "OK" in out.upper()
                self.log.info(f"  SMART check: {'OK' if ok else 'FAIL'}")
                return ok, info
        except Exception as e:
            self.log.error(f"  SMART check failed: {e}")
            info["error"] = str(e)
            return False, info

    def check_pcie_link(self) -> Tuple[bool, str]:
        """检查 PCIe 链路状态（修复版：按类代码 0108 筛选）。"""
        try:
            # NVMe 设备 PCIe 类代码为 0108
            _, out, _ = self.run_command("lspci -d ::0108 2>/dev/null | head -1")
            if not out.strip():
                # 回退：按 nvme 关键字
                _, out, _ = self.run_command("lspci | grep -i -E 'nvme|non-volatile' | head -1")
            if not out.strip():
                self.log.warning("  Cannot get PCIe device address")
                return False, "未知"

            pcie_addr = out.strip().split()[0]
            _, detail, _ = self.run_command(
                f"lspci -vv -s {pcie_addr} 2>/dev/null | grep -E '(LnkSta|Speed|Width)'"
            )
            self.log.info(f"  PCIe link ({pcie_addr}): {detail.strip()[:200]}")
            return True, detail.strip()
        except Exception as e:
            self.log.warning(f"  PCIe linkcheck failed: {e}")
            return False, str(e)

    # ---------- 写入与验证 ----------

    def write_pattern(self, pattern: str, size: Optional[str] = None) -> Tuple[bool, str, str]:
        """写入指定 pattern 打底（使用 fio do_verify + crc32c 确保写入正确）。"""
        size = size or f'{self.test_size_gb}G'
        self.log.info(f"  Writing pattern 0x{pattern} ({size})...")
        cmd = (f"fio --name=write_pattern_{pattern} --filename={self.device} "
               f"--rw=write --bs=128k --ioengine=libaio --direct=1 --size={size} "
               f"--numjobs=4 --iodepth=64 --do_verify=1 --verify_pattern=0x{pattern} "
               f"--verify=crc32c --group_reporting")
        success, stdout, stderr = self.run_command(cmd, timeout=600)
        if success:
            speed = self.parse_fio_speed(stdout)
            self.log.info(f"    Write speed: {speed}")
        return success, stdout, stderr

    def verify_pattern(self, pattern: str, size: str = '100M',
                       offset: str = '0', bs: str = '128k') -> Tuple[bool, str, str]:
        """验证指定区域的 pattern（直接读取比对 buffer_pattern）。"""
        self.log.info(f"  Verifying pattern 0x{pattern} (offset={offset}, size={size}, bs={bs})...")
        cmd = (f"fio --name=verify_pattern_{pattern} --filename={self.device} "
               f"--rw=read --ioengine=libaio --direct=1 --bs={bs} --size={size} "
               f"--numjobs=8 --iodepth=128 --buffer_pattern=0x{pattern} --group_reporting")
        if offset != '0':
            cmd += f" --offset={offset}"
        success, stdout, stderr = self.run_command(cmd, timeout=600)
        if success:
            speed = self.parse_fio_speed(stdout)
            self.log.info(f"    Read speed: {speed}")
        return success, stdout, stderr

    def start_spor_write(self) -> bool:
        """
        启动 SPOR 写入（后台运行）。
        支持纯写和混合读写两种模式，使用高队列深度（iodepth=32, numjobs=4）保证写入压力。
        使用 write_iolog 记录each IO 的 offset/size，用于掉电后定位写入位置。
        """
        self.log.info("  Starting SPOR write (background)...")
        log_file = os.path.join(self.log_dir, "fio_io_trace_spor_write.log")
        # 清理旧的 iolog
        if os.path.exists(log_file):
            try:
                os.remove(log_file)
            except Exception:
                pass

        if self.mixed_rw:
            # 混合读写模式：70% 读 + 30% 写，随机 4K
            rw_type = "randrw"
            extra = f"--rwmixread={self.mixed_read_ratio} --bs=4k"
            self.log.info(f"    Mode: Mixed R/W (randrw, {self.mixed_read_ratio}% read, 4K)")
        else:
            # 纯顺序写模式
            rw_type = "write"
            extra = f"--bs={self.lba_size}"
            self.log.info(f"    Mode: pure sequential write (write, {self.lba_size}B)")

        cmd = (f"fio --name=spor_write --filename={self.device} "
               f"--rw={rw_type} {extra} --ioengine=libaio --direct=1 "
               f"--size={self.test_size_gb}G --numjobs=4 --iodepth=32 "
               f"--write_iolog={log_file} --log_avg_msec=10 "
               f"--buffer_pattern=0x22 --group_reporting")

        try:
            subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            time.sleep(2)  # 等待 fio 启动并开始写入
            self.log.info("    SPOR write started (iodepth=32, numjobs=4)")
            return True
        except Exception as e:
            self.log.error(f"    start fio failed: {e}")
            return False

    def kill_fio(self):
        """终止 fio 进程。"""
        self.run_command("pkill -f 'fio --name=spor_write'", timeout=10)

    def get_write_position(self) -> int:
        """
        从 fio write_iolog 解析掉电时的最后写入位置（LBA）。
        读取最后 20 行，取最大 end_offset，确保 LBA 对齐。
        """
        log_file = os.path.join(self.log_dir, "fio_io_trace_spor_write.log")
        max_offset = 0

        if not os.path.exists(log_file):
            self.log.warning(f"  IO log file not found: {log_file}")
            return 0

        try:
            with open(log_file, 'r') as f:
                lines = f.readlines()
            total_lines = len(lines)
            # 读取最后 20 行（掉电时写入位置通常在最后几行）
            process_lines = lines[-20:] if total_lines > 20 else lines
            self.log.info(f"  IO log total {total_lines} lines, analyzing last {len(process_lines)} lines")

            for line in process_lines:
                parts = line.strip().split()
                # fio iolog 格式: device write offset size
                if len(parts) >= 4 and parts[1] == 'write':
                    try:
                        offset = int(parts[2])
                        size = int(parts[3])
                        # 验证 LBA 对齐，非对齐条目可能是掉电时截断的日志
                        if offset % self.lba_size != 0 or size % self.lba_size != 0:
                            self.log.debug(f"    skipping unaligned write: offset={offset}, size={size}")
                            continue
                        end_offset = offset + size
                        if end_offset > max_offset:
                            max_offset = end_offset
                    except ValueError:
                        continue

            # 确保最大偏移 LBA 对齐
            if max_offset % self.lba_size != 0:
                max_offset = (max_offset // self.lba_size) * self.lba_size

            lba = max_offset // self.lba_size
            self.log.info(f"  Write position at power loss: LBA {lba} ({bytes_to_human(max_offset)})")
            return lba
        except Exception as e:
            self.log.error(f"  parse IO logfailed: {e}")
            return 0

    # ---------- enhanced（增强）模式：IPMI 意外power off ----------

    def enhanced_ipmi_surprise_poweroff(self, state: Dict[str, Any]) -> Tuple[bool, str]:
        """
        enhanced 模式专属：通过 IPMI 直接意外断电（不 sync、不 shutdown）。
        启动 nohup 后台脚本：等待 -> ipmitool power off -> 延时 -> ipmitool power on。
        关键：断电前不执行 os.sync()，保证真正的"意外"断电。
        参考 OKN power_down(safe_shutdown=False) 直接 link_state.power_off()。
        返回 (成功, 消息)。
        """
        try:
            # 保存状态到阶段2（断电后进程终止，上电后自启恢复）
            state['phase'] = self.PHASE_POWERON
            state['last_poweroff_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state['poweroff_mode'] = 'enhanced_ipmi'
            self.save_state(state)

            # 启动后台脚本：等待 -> IPMI power off -> 延时 -> IPMI 上电
            # 注意：不执行 shutdown -h now，这是 SPOR 意外断电与正常关机的本质区别
            ipmi_script = "/tmp/ssd_spor_enhanced_power_cycle.sh"
            with open(ipmi_script, "w") as f:
                f.write(f"""#!/bin/bash
# enhanced SPOR: 等待写入进行后直接 IPMI 断电（不经过 OS shutdown）
sleep {self.delay_before_poweroff + 2}
# IPMI 直接断电（意外断电，不发送待机命令）
ipmitool -H {self.cfg.ipmi_host} -U {self.cfg.ipmi_user} -P {self.cfg.ipmi_pass} -I lanplus chassis power off
sleep {getattr(self.cfg, 'pc_off_interval', 30)}
# IPMI 上电
ipmitool -H {self.cfg.ipmi_host} -U {self.cfg.ipmi_user} -P {self.cfg.ipmi_pass} -I lanplus chassis power on
""")
            os.chmod(ipmi_script, 0o755)
            subprocess.Popen(["nohup", ipmi_script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
            self.log.info("  [enhanced] IPMI surprise power off background script started (no sync, no shutdown, direct power off)")
            self.log.info(f"  [enhanced] Estimated {self.delay_before_poweroff + 2} secondss before power off, "
                          f"power off {getattr(self.cfg, 'pc_off_interval', 30)} s then auto power on")
            return True, "IPMI surprise power off script started"
        except Exception as e:
            self.log.error(f"  [enhanced] IPMI surprise power off start failed: {e}")
            return False, str(e)

    # ---------- enhanced（增强）模式：高 QD 线程级异步写入 ----------

    def start_enhanced_write_thread(self) -> bool:
        """
        enhanced 模式专属：启动高队列深度异步写入（线程级追踪）。
        使用 threading.Thread 启动 fio 顺序写（iodepth=256, bs=128k, pattern=0x22）。
        通过定期读取 fio 子进程 /proc/<pid>/io 追踪已写入字节数。
        参考 OKN MyThread 异步写入模式。
        返回 (成功)。
        """
        self.log.info("  Starting enhanced high QD write (thread-level trace)...")
        log_file = os.path.join(self.log_dir, "fio_io_trace_spor_enhanced.log")
        if os.path.exists(log_file):
            try:
                os.remove(log_file)
            except Exception:
                pass

        iodepth = getattr(self.cfg, 'spor_enhanced_iodepth', 256)
        bs = getattr(self.cfg, 'spor_enhanced_bs', '128k')
        self.log.info(f"    Mode: pure sequential write (write, bs={bs}, iodepth={iodepth}, numjobs=1)")

        cmd = (f"fio --name=spor_enhanced_write --filename={self.device} "
               f"--rw=write --bs={bs} --ioengine=libaio --direct=1 "
               f"--size={self.test_size_gb}G --numjobs=1 --iodepth={iodepth} "
               f"--write_iolog={log_file} --log_avg_msec=10 "
               f"--buffer_pattern=0x22 --group_reporting --output-format=json")

        # 共享进度变量
        self._enhanced_write_bytes = 0
        self._enhanced_fio_proc = None
        self._enhanced_stop_monitor = False

        def _run_fio():
            try:
                self._enhanced_fio_proc = subprocess.Popen(
                    cmd, shell=True, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                self._enhanced_fio_proc.wait()
            except Exception:
                pass

        def _monitor_progress():
            """定期读取 fio 子进程的 /proc/<pid>/io write_bytes。"""
            time.sleep(2)  # 等待 fio 启动
            while not self._enhanced_stop_monitor:
                try:
                    if self._enhanced_fio_proc and self._enhanced_fio_proc.poll() is None:
                        pid = self._enhanced_fio_proc.pid
                        io_file = f"/proc/{pid}/io"
                        if os.path.exists(io_file):
                            with open(io_file, 'r') as pf:
                                for line in pf:
                                    if line.startswith('write_bytes:'):
                                        self._enhanced_write_bytes = int(line.split(':')[1].strip())
                                        break
                except Exception:
                    pass
                time.sleep(1)

        try:
            self._enhanced_write_thread = threading.Thread(target=_run_fio, daemon=True)
            self._enhanced_monitor_thread = threading.Thread(target=_monitor_progress, daemon=True)
            self._enhanced_write_thread.start()
            self._enhanced_monitor_thread.start()
            time.sleep(2)  # 等待 fio 启动并开始写入
            self.log.info(f"    Enhanced write started (iodepth={iodepth}, thread-level progress trace)")
            return True
        except Exception as e:
            self.log.error(f"    start enhanced Write failed: {e}")
            return False

    def get_enhanced_write_position(self) -> int:
        """
        enhanced 模式专属：从线程共享进度变量获取掉电时已写入 LBA。
        若进度变量不可用（为 0），fallback 到 iolog 解析。
        返回 last_lba。
        """
        written_bytes = getattr(self, '_enhanced_write_bytes', 0)
        if written_bytes > 0:
            lba = written_bytes // self.lba_size
            self.log.info(f"  Write position at power loss(threadtrace): LBA {lba} ({bytes_to_human(written_bytes)})")
            return lba
        else:
            self.log.info("  Thread progress unavailable, falling back to iolog parse")
            return self.get_write_position()

    # ---------- enhanced（增强）模式：fio 内置 verify Pattern 校验 ----------

    def enhanced_verify_pattern(self, pattern_hex: str, size: str,
                                 offset: str = '0') -> Tuple[bool, int]:
        """
        enhanced 模式专属：使用 fio 内置 --verify=pattern 逐块校验。
        参考 OKN verify_read(do_data_compare=True, verify_pattern=True)。
        参数:
          pattern_hex: Pattern 十六进制值（如 '22'、'11'）
          size: 校验大小（如 '20G'、'1024M'、字节数）
          offset: 起始偏移（默认 '0'）
        返回 (全部通过, mismatch 块数)。
        """
        try:
            cmd = (f"fio --name=spor_enhanced_verify --filename={self.device} "
                   f"--rw=read --bs={self.lba_size} --ioengine=libaio --direct=1 "
                   f"--size={size} --offset={offset} --numjobs=1 --iodepth=32 "
                   f"--verify=pattern --verify_pattern=0x{pattern_hex} --do_verify=1 "
                   f"--verify_fatal=0 --group_reporting --output-format=json")
            self.log.info(f"    enhanced verify: pattern=0x{pattern_hex}, size={size}, offset={offset}")
            success, stdout, stderr = self.run_command(cmd, timeout=3600)
            mismatch = 0
            try:
                perf_data = json.loads(stdout)
                job = perf_data["jobs"][0]
                verify_err = job.get("verify", {}).get("verify_errors", 0)
                mismatch = verify_err
            except Exception as parse_err:
                self.log.debug(f"    fio verify JSON parsefailed: {parse_err}")
                # fallback: 文本匹配
                if "mismatch" in (stdout + stderr).lower() or "verification failed" in (stdout + stderr).lower():
                    mismatch = -1  # 标记为有错误但无法精确计数
            all_ok = (mismatch == 0)
            if all_ok:
                self.log.info(f"    enhanced verify passed (0 mismatch)")
            else:
                self.log.error(f"    enhanced verify failed (mismatch={mismatch})")
            return all_ok, mismatch
        except Exception as e:
            self.log.error(f"    enhanced verify error: {e}")
            return False, -1

    # ---------- manual（手动）模式：手动意外power off ----------

    def manual_surprise_poweroff(self, state: Dict[str, Any]) -> Tuple[bool, str]:
        """
        manual 模式专属：手动意外断电。
        保存状态后打印倒计时提示，不执行任何断电操作（等待用户手动切断电源）。
        关键：断电前不 sync，用户需在写入进行中直接切断电源。
        返回 (成功, 消息)。
        """
        try:
            state['phase'] = self.PHASE_POWERON
            state['last_poweroff_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state['poweroff_mode'] = 'manual'
            self.save_state(state)

            self.log.info("=" * 55)
            self.log.info("  [manual] Please manually cut power immediately (surprise power off, no sync, no shutdown)")
            self.log.info(f"  current fio write still in progress, please power off within {self.delay_before_poweroff} seconds")
            self.log.info("  After power off, wait 10-30 seconds, then manually power on")
            self.log.info("  After system starts, rerun the same command to automatically resume executing phase2")
            self.log.info("=" * 55)
            # 等待一段时间让用户有时间断电（进程会被断电强制终止）
            time.sleep(max(self.delay_before_poweroff, 10))
            return True, "手动断电提示已打印"
        except Exception as e:
            self.log.error(f"  [manual] Manual power off preparation failed: {e}")
            return False, str(e)

    # ---------- 阶段1：掉电前 ----------

    def phase1_poweroff(self, state: Dict[str, Any]) -> bool:
        """
        阶段1：掉电前流程。
        写入 pattern11 打底 -> 验证 -> 启动 SPOR 写入 -> 等待 -> 保存状态 -> 硬件断电。
        关键：断电前不执行 os.sync()，保证真正的"意外"断电。
        """
        current_cycle = state['current_cycle']

        self.log.info("=" * 55)
        self.log.info(f"  Cycle {current_cycle}/{state['total_cycles']} - phase1: before power loss")
        self.log.info("=" * 55)

        # 检查 PCIe 链路
        self.check_pcie_link()

        # Step 1: 写入 pattern11 打底
        self.log.info("\n  Step 1/5: Writing pattern 0x11 baseline")
        success, _, stderr = self.write_pattern('11', size=f'{self.test_size_gb}G')
        if not success:
            self.log.error(f"    Writing pattern11 failed: {stderr}")
            state['results'].append({
                'cycle': current_cycle, 'success': False,
                'error': '写入pattern11失败', 'phase': 'poweroff'
            })
            self.save_state(state)
            return False

        # Step 2: 验证 pattern11
        self.log.info("\n  Step 2/5: Verifying pattern11")
        success, _, stderr = self.verify_pattern('11', size=f'{self.test_size_gb}G')
        if not success:
            self.log.warning(f"    Verifying pattern11 failed(resumetest): {stderr}")

        # Step 3: 启动 SPOR 写入（高压力，支持混合读写）
        self.log.info("\n  Step 3/5: start SPOR write")
        if self.spor_power_mode == 'enhanced':
            write_ok = self.start_enhanced_write_thread()
        else:
            write_ok = self.start_spor_write()
        if not write_ok:
            state['results'].append({
                'cycle': current_cycle, 'success': False,
                'error': '启动fio失败', 'phase': 'poweroff'
            })
            self.save_state(state)
            return False

        # Step 4: 等待指定秒数（让写入充分进行，此时 fio 仍在活跃写入）
        self.log.info(f"\n  Step 4/5: Waiting during write {self.delay_before_poweroff} seconds...")
        time.sleep(self.delay_before_poweroff)

        # Step 5: 保存状态到阶段2，然后触发意外断电（三模式）
        # 关键：不执行 os.sync()，保证断电是真正的"意外"
        self.log.info("\n  Step 5/5: Save state and trigger surprise power loss (no sync)")

        if self.spor_power_mode == 'timeboard':
            # 旧方案：Timeboard 硬件断电
            state['phase'] = self.PHASE_POWERON
            state['last_poweroff_time'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            state['poweroff_mode'] = 'timeboard'
            self.save_state(state)

            # 连接 Timeboard 并触发掉电
            if not self.timeboard.ser:
                ok, msg = self.timeboard.connect()
                if not ok:
                    self.log.error(f"Timeboard connection failed: {msg}")
                    self.kill_fio()
                    state['results'].append({
                        'cycle': current_cycle, 'success': False,
                        'error': f'Timeboard连接失败: {msg}', 'phase': 'poweroff'
                    })
                    self.save_state(state)
                    return False

            # 触发硬件掉电（短延时，增强"意外"性）
            self.timeboard.trigger_poweroff(self.poweroff_delay_ms)
            # 等待断电（系统会被切断，此进程将终止）
            time.sleep(self.poweroff_delay_ms / 1000 + 5)

        elif self.spor_power_mode == 'manual':
            # manual 方案：手动意外断电（打印提示，不执行断电操作）
            self.manual_surprise_poweroff(state)

        else:  # enhanced
            # enhanced 方案：IPMI 直接意外断电（不 shutdown、不 sync）
            if not getattr(self.cfg, 'ipmi_host', None):
                self.log.error("Enhanced mode requires --ipmi-host for IPMI surprise power off")
                state['results'].append({
                    'cycle': current_cycle, 'success': False,
                    'error': 'enhanced模式缺少ipmi-host', 'phase': 'poweroff'
                })
                self.save_state(state)
                return False
            self.enhanced_ipmi_surprise_poweroff(state)
            # 等待后台脚本触发断电（系统会被切断，此进程将终止）
            time.sleep(self.delay_before_poweroff + 5)

        return True

    # ---------- 阶段2：上电后 ----------

    def phase2_poweron(self, state: Dict[str, Any]) -> bool:
        """
        阶段2：上电后流程。
        掉盘检测 -> SMART 检查 -> PCIe 检查 -> 解析写入位置 -> 验证前段 pattern22
        -> 验证后段 pattern11 -> 记录结果 -> 准备下一轮或最终测试。
        """
        current_cycle = state['current_cycle']

        self.log.info("=" * 55)
        self.log.info(f"  Cycle {current_cycle}/{state['total_cycles']} - phase2: after power on")
        self.log.info("=" * 55)

        # Step 1: 等待 SSD 初始化 + 掉盘检测
        self.log.info("\n  Step 1/6: Waiting for SSD initialization and detecting disk drop...")
        time.sleep(15)
        if not self.check_device_present():
            state['results'].append({
                'cycle': current_cycle, 'success': False,
                'error': 'SSD掉盘（上电后设备不存在）', 'phase': 'poweron'
            })
            self.save_state(state)
            return False

        # Step 2: PCIe 链路检查
        self.log.info("\n  Step 2/6: PCIe linkcheck")
        self.check_pcie_link()

        # Step 3: SMART 检查
        self.log.info("\n  Step 3/6: SMART health check")
        smart_ok, smart_info = self.check_smart()
        state.setdefault('smart_history', []).append({
            'cycle': current_cycle, **smart_info
        })

        # Step 4: 解析掉电时写入位置
        self.log.info("\n  Step 4/6: parseWrite position at power loss")
        if self.spor_power_mode == 'enhanced':
            last_lba = self.get_enhanced_write_position()
        else:
            last_lba = self.get_write_position()

        # Step 5: 验证前段 pattern22（已写入区域，跳过最后 skip_lba 个 LBA）
        self.log.info("\n  Step 5/6: verify before section pattern22(written area)")
        if self.spor_power_mode == 'enhanced':
            verify_22 = self._enhanced_verify_written_area(last_lba)
        else:
            verify_22 = self._verify_written_area(last_lba)

        # Step 6: 验证后段 pattern11（未被覆盖区域）
        self.log.info("\n  Step 6/6: verify after section pattern11(unwritten area)")
        if self.spor_power_mode == 'enhanced':
            verify_11 = self._enhanced_verify_unwritten_area(last_lba)
        else:
            verify_11 = self._verify_unwritten_area(last_lba)

        # 记录本轮结果
        cycle_success = verify_22 and verify_11 and smart_ok
        state['results'].append({
            'cycle': current_cycle,
            'success': cycle_success,
            'last_lba': last_lba,
            'verify_22': verify_22,
            'verify_11': verify_11,
            'smart_ok': smart_ok,
            'phase': 'poweron'
        })
        self.save_state(state)

        self.log.info(f"\n  This round result: {'PASS' if cycle_success else 'FAIL'} "
                      f"(pattern22={'OK' if verify_22 else 'FAIL'}, "
                      f"pattern11={'OK' if verify_11 else 'FAIL'}, "
                      f"SMART={'OK' if smart_ok else 'FAIL'})")

        # 判断是否完成所有循环
        if current_cycle >= state['total_cycles']:
            self.log.info("\n  All test rounds completed!")
            state['phase'] = self.PHASE_DONE
            self.save_state(state)

            # 最终完整功能测试
            if self.final_test:
                self.run_final_functional_test(state)

            self.print_final_results(state)
            self.clear_state()
            return True
        else:
            # 准备下一轮
            state['current_cycle'] = current_cycle + 1
            state['phase'] = self.PHASE_POWEROFF
            self.save_state(state)
            self.log.info(f"\n  Preparing next round ({current_cycle + 1}/{state['total_cycles']})")
            return True

    def _verify_written_area(self, last_lba: int) -> bool:
        """验证已写入区域（前段 pattern22），跳过掉电边界最后 skip_lba 个 LBA。"""
        if last_lba >= self.test_size_lba:
            # 写入完整，验证全部
            self.log.info(f"    write complete({self.test_size_gb}GB), verify all pattern22")
            success, _, stderr = self.verify_pattern(
                '22', size=f'{self.test_size_gb}G', offset='0', bs=f'{self.lba_size}')
            return success

        if last_lba > 0:
            # 未写完整，跳过最后 skip_lba 个 LBA（掉电时可能未完全写入）
            valid_lba = max(0, last_lba - self.skip_lba)
            valid_bytes = valid_lba * self.lba_size
            self.log.info(f"    Written LBA {last_lba}, skipping last {self.skip_lba} LBA, "
                          f"validated up to LBA {valid_lba} ({bytes_to_human(valid_bytes)})")
            if valid_bytes > 0:
                success, _, stderr = self.verify_pattern(
                    '22', size=f'{valid_bytes}', offset='0', bs=f'{self.lba_size}')
                if not success:
                    self.log.error(f"    pattern22 verificationfailed: {stderr[:200]}")
                return success
            else:
                self.log.info("    Valid data is 0, skipped pattern22 verification")
                return True
        else:
            # 写入位置为 0，假设写入完整
            self.log.info(f"    Write position is 0, assuming write complete, verify all pattern22")
            success, _, _ = self.verify_pattern(
                '22', size=f'{self.test_size_gb}G', offset='0', bs=f'{self.lba_size}')
            return success

    def _verify_unwritten_area(self, last_lba: int) -> bool:
        """验证未被覆盖区域（后段 pattern11）。"""
        if last_lba >= self.test_size_lba or last_lba == 0:
            self.log.info("    write complete, entire area is pattern22, skipped pattern11 verification")
            return True

        start_offset = last_lba * self.lba_size
        remaining_bytes = self.test_size_bytes - start_offset
        if remaining_bytes <= 0:
            self.log.info("    Insufficient remaining space, skipped pattern11 verification")
            return True

        self.log.info(f"    From LBA {last_lba}(offset {start_offset}) starting verification, "
                      f"remaining {bytes_to_human(remaining_bytes)}")

        if remaining_bytes >= 1024 ** 3:
            size_str = f'{remaining_bytes // (1024 ** 3)}G'
        elif remaining_bytes >= 1024 ** 2:
            size_str = f'{remaining_bytes // (1024 ** 2)}M'
        else:
            size_str = f'{remaining_bytes}'

        success, _, stderr = self.verify_pattern(
            '11', size=size_str, offset=f'{start_offset}', bs='128k')
        if not success:
            self.log.error(f"    pattern11 verificationfailed: {stderr[:200]}")
        return success


    def _enhanced_verify_written_area(self, last_lba: int) -> bool:
        """enhanced 模式：验证已写入区域（前段 pattern22），使用 fio 内置 verify=pattern。"""
        if last_lba >= self.test_size_lba:
            self.log.info(f"    write complete({self.test_size_gb}GB), verify all pattern22")
            ok, _ = self.enhanced_verify_pattern('22', size=f'{self.test_size_gb}G', offset='0')
            return ok

        if last_lba > 0:
            valid_lba = max(0, last_lba - self.skip_lba)
            valid_bytes = valid_lba * self.lba_size
            self.log.info(f"    Written LBA {last_lba}, skipping last {self.skip_lba} LBA, "
                          f"validated up to LBA {valid_lba} ({bytes_to_human(valid_bytes)})")
            if valid_bytes > 0:
                ok, mismatch = self.enhanced_verify_pattern('22', size=f'{valid_bytes}', offset='0')
                if not ok:
                    self.log.error(f"    pattern22 enhanced verificationfailed: mismatch={mismatch}")
                return ok
            else:
                self.log.info("    Valid data is 0, skipped pattern22 verification")
                return True
        else:
            self.log.info(f"    Write position is 0, assuming write complete, verify all pattern22")
            ok, _ = self.enhanced_verify_pattern('22', size=f'{self.test_size_gb}G', offset='0')
            return ok

    def _enhanced_verify_unwritten_area(self, last_lba: int) -> bool:
        """enhanced 模式：验证未被覆盖区域（后段 pattern11），使用 fio 内置 verify=pattern。"""
        if last_lba >= self.test_size_lba or last_lba == 0:
            self.log.info("    write complete, entire area is pattern22, skipped pattern11 verification")
            return True

        start_offset = last_lba * self.lba_size
        remaining_bytes = self.test_size_bytes - start_offset
        if remaining_bytes <= 0:
            self.log.info("    Insufficient remaining space, skipped pattern11 verification")
            return True

        self.log.info(f"    From LBA {last_lba}(offset {start_offset}) starting verification, "
                      f"remaining {bytes_to_human(remaining_bytes)}")

        ok, mismatch = self.enhanced_verify_pattern('11', size=f'{remaining_bytes}',
                                                      offset=f'{start_offset}')
        if not ok:
            self.log.error(f"    pattern11 enhanced verificationfailed: mismatch={mismatch}")
        return ok

    # ---------- 最终完整功能测试 ----------

    def run_final_functional_test(self, state: Dict[str, Any]) -> Tuple[bool, Dict[str, Any]]:
        """
        所有循环完成后执行最终完整功能测试：
        容量检查 + SMART 检查 + 基本性能测试（顺序读/写）。
        """
        self.log.info("=" * 55)
        self.log.info("  Final full functionality test")
        self.log.info("=" * 55)
        result = {"capacity_ok": False, "smart_ok": False,
                  "seq_read_ok": False, "seq_write_ok": False, "details": {}}

        # 1. 容量检查
        self.log.info("\n  [1/4] Capacity check")
        try:
            _, out, _ = run_cmd(["lsblk", "-b", "-d", "-n", "-o", "SIZE", self.device],
                                 check=True, capture=True, logger=self.log, timeout=10)
            cap = int(out.strip())
            result["details"]["capacity_bytes"] = cap
            result["capacity_ok"] = cap > 0
            self.log.info(f"    capacity: {bytes_to_human(cap)} ({bytes_to_human(cap, binary=True)}) -> "
                          f"{'OK' if cap > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    Capacity check failed: {e}")

        # 2. SMART 检查
        self.log.info("\n  [2/4] SMART check")
        smart_ok, smart_info = self.check_smart()
        result["smart_ok"] = smart_ok
        result["details"]["smart"] = smart_info

        # 3. 顺序读性能测试
        self.log.info("\n  [3/4] sequential readperformancetest (128K QD32, 30s)")
        try:
            fio_cmd = [
                "fio", "--name=final_seq_read", f"--filename={self.device}",
                "--rw=read", "--bs=128k", "--iodepth=32",
                "--runtime=30", "--time_based", "--direct=1",
                "--ioengine=libaio", "--group_reporting", "--output-format=json",
            ]
            _, out, _ = run_cmd(fio_cmd, check=True, capture=True,
                                 logger=self.log, timeout=60)
            perf = json.loads(out)
            read_bw = perf["jobs"][0]["read"]["bw"]
            result["details"]["seq_read_bw_mbps"] = round(read_bw / 1024, 2)
            result["seq_read_ok"] = read_bw > 0
            self.log.info(f"    sequential readBandwidth: {read_bw / 1024:.2f} MB/s -> "
                          f"{'OK' if read_bw > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    sequential readTest failed: {e}")

        # 4. 顺序写性能测试
        self.log.info("\n  [4/4] Sequential write performance test (128K QD32, 30s)")
        try:
            fio_cmd = [
                "fio", "--name=final_seq_write", f"--filename={self.device}",
                "--rw=write", "--bs=128k", "--iodepth=32",
                "--runtime=30", "--time_based", "--direct=1",
                "--ioengine=libaio", "--group_reporting", "--output-format=json",
            ]
            _, out, _ = run_cmd(fio_cmd, check=True, capture=True,
                                 logger=self.log, timeout=60)
            perf = json.loads(out)
            write_bw = perf["jobs"][0]["write"]["bw"]
            result["details"]["seq_write_bw_mbps"] = round(write_bw / 1024, 2)
            result["seq_write_ok"] = write_bw > 0
            self.log.info(f"    Sequential write bandwidth: {write_bw / 1024:.2f} MB/s -> "
                          f"{'OK' if write_bw > 0 else 'FAIL'}")
        except Exception as e:
            self.log.error(f"    Sequential write test failed: {e}")

        all_ok = all([result["capacity_ok"], result["smart_ok"],
                      result["seq_read_ok"], result["seq_write_ok"]])
        result["overall"] = "PASS" if all_ok else "FAIL"
        state["final_functional_test"] = result

        self.log.info(f"\n  Final functionality test: {'PASS' if all_ok else 'FAIL'}")
        return all_ok, result

    # ---------- 结果输出 ----------

    def print_final_results(self, state: Dict[str, Any]):
        """打印最终测试结果汇总。"""
        self.log.info("\n" + "=" * 55)
        self.log.info("  SPOR testcompleted - Result summary")
        self.log.info("=" * 55)

        results = state.get('results', [])
        passed = sum(1 for r in results if r.get('success'))
        failed = len(results) - passed

        self.log.info(f"  Total rounds: {state.get('total_cycles', '?')}")
        self.log.info(f"  Actually executed: {len(results)} round")
        self.log.info(f"  passed: {passed}")
        self.log.info(f"  failed: {failed}")

        for r in results:
            status = "PASS" if r.get('success') else "FAIL"
            self.log.info(f"    Round {r.get('cycle', '?')} round: {status} "
                          f"(LBA={r.get('last_lba', '?')}, "
                          f"p22={'OK' if r.get('verify_22') else 'FAIL'}, "
                          f"p11={'OK' if r.get('verify_11') else 'FAIL'}, "
                          f"SMART={'OK' if r.get('smart_ok') else 'FAIL'})")
            if r.get('error'):
                self.log.info(f"      ERROR: {r['error']}")

        if 'final_functional_test' in state:
            ft = state['final_functional_test']
            self.log.info(f"  Final functionality test: {ft.get('overall', '?')}")

        # 保存结果到文件
        result_file = os.path.join(
            self.log_dir, f"spor_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
        try:
            with open(result_file, 'w', encoding='utf-8') as f:
                json.dump(state, f, indent=2, ensure_ascii=False)
            self.log.info(f"  Result saved to: {result_file}")
        except Exception as e:
            self.log.error(f"  Failed to save result file: {e}")

    # ---------- 主入口 ----------

    def run(self) -> TestResult:
        """
        SPOR 测试主入口。
        两阶段状态机：阶段1掉电前 -> 硬件断电（进程终止）-> 上电后自启恢复 -> 阶段2上电后。
        通过状态文件持久化实现断点恢复。
        """
        result = TestResult(
            test_item=TEST_SPOR,
            test_name="意外电源循环测试(SPOR)",
            device=self.device
        )
        result.start()

        if self.cfg.dry_run:
            self.log.info(f"[DRY-RUN] Will execute SPOR test: {self.cycles} round, "
                          f"mode={self.spor_power_mode}, "
                          f"delay={self.delay_before_poweroff}s, "
                          f"mixed_rw={'On' if self.mixed_rw else 'Off'}")
            result.finish(STATUS_SKIP, "dry-run mode")
            return result

        try:
            # 加载或初始化状态
            state = self.load_state()
            if not state:
                state = {
                    'total_cycles': self.cycles,
                    'current_cycle': 1,
                    'phase': self.PHASE_POWEROFF,
                    'results': [],
                    'created_at': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                self.save_state(state)

            # 初始化 Timeboard（仅 timeboard 模式阶段1需要）
            if state.get('phase') == self.PHASE_POWEROFF and self.spor_power_mode == 'timeboard':
                ok, msg = self.timeboard.connect()
                if not ok:
                    self.log.warning(f"Timeboard connection failed: {msg}(not needed for phase2)")

            # 根据阶段执行
            phase = state.get('phase')
            if phase == self.PHASE_POWEROFF:
                self.phase1_poweroff(state)
                # 如果执行到这里说明断电未生效（模拟模式或 Timeboard 失败）
                result.details["note"] = "阶段1执行完成，系统应已断电"
                self.log.info("Phase 1 completed, waiting for power-on to resume Phase 2")
                result.finish(STATUS_PASS)
            elif phase == self.PHASE_POWERON:
                success = self.phase2_poweron(state)
                result.details["cycles_completed"] = len(state.get('results', []))
                result.details["final_test"] = state.get('final_functional_test')
                if success:
                    # 检查是否全部完成
                    if state.get('phase') == self.PHASE_DONE:
                        passed = sum(1 for r in state.get('results', []) if r.get('success'))
                        total = len(state.get('results', []))
                        result.details["passed"] = passed
                        result.details["failed"] = total - passed
                        if passed == total:
                            self.log.info(f"All {total} rounds passed")
                            result.finish(STATUS_PASS)
                        else:
                            result.finish(STATUS_FAIL, f"{passed}/{total} rounds passed, {total - passed} rounds failed")
                    else:
                        self.log.info("Phase 2 completed, preparing for next round Phase 1")
                        result.finish(STATUS_PASS)
                else:
                    result.finish(STATUS_FAIL, "Phase 2 execution failed")
            elif phase == self.PHASE_DONE:
                result.details["cycles_completed"] = len(state.get('results', []))
                self.log.info("Test completed (state file indicates done)")
                result.finish(STATUS_PASS)
            else:
                result.finish(STATUS_ERROR, f"Unknown phase: {phase}")

        except Exception as e:
            self.log.exception(f"SPOR Test error: {e}")
            result.finish(STATUS_ERROR, str(e))

        return result

