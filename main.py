   # Copyright (C) 2026 CastBase Team. All rights reserved.
   # This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, version 3.
   # This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY.
   # import sys
import os
import time
import json
import urllib.parse
import socket
import platform
import math
import requests

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, 
                               QHBoxLayout, QPushButton, QLineEdit, QLabel, 
                               QStackedWidget, QGridLayout, QFrame, QProgressBar,
                               QInputDialog, QSizePolicy, QDialog, QTextEdit, 
                               QDialogButtonBox, QComboBox)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont, QIcon, QPixmap, QImage

import numpy as np
import NDIlib as ndi
import av

# 跨平台路径处理
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOGO_PATH = os.path.join(BASE_DIR, "logo.png")

# ==========================================
# 线程 1：鉴权请求 (2.0 增强版)
# ==========================================
class AuthThread(QThread):
    finished = Signal(bool, dict, str)
    def __init__(self, server_url, ticket):
        super().__init__()
        self.server_url = server_url.rstrip('/')
        self.ticket = ticket

    def run(self):
        try:
            url = f"{self.server_url}/api/ndi/auth"
            response = requests.post(url, json={"ticket": self.ticket}, timeout=5)
            data = response.json()
            if response.status_code == 200 and data.get("success"):
                self.finished.emit(True, data, "")
            else:
                self.finished.emit(False, {}, data.get("error", "鉴权失败"))
        except Exception as e:
            self.finished.emit(False, {}, f"网络异常: {str(e)}")

# ==========================================
# 线程 2：不死鸟视频引擎 (带硬解与自动重连)
# ==========================================
class VideoEngineThread(QThread):
    frame_ready = Signal(QImage)
    stats_update = Signal(dict)
    vu_update = Signal(int, int) 
    status_update = Signal(str)

    def __init__(self, stream_url, ndi_name):
        super().__init__()
        self.stream_url = stream_url
        self.ndi_name = ndi_name  # 记录原始字符串
        self.running = True
        self.extra_options = {}
        self.last_ndi_ts = 0 # 【新增】用于平滑时间戳算法

        # 【核心修复 1：彻底消灭乱码】
        # 直接使用 SendCreate 的构造函数传参，这是最稳妥的内存传递方式
        self._ndi_desc = ndi.SendCreate(p_ndi_name=self.ndi_name)
        # 如果您的库版本不支持在构造函数传参，请保留下面这两行，否则可以注释掉
        if hasattr(self._ndi_desc, 'ndi_name'):
            self._ndi_desc.ndi_name = self.ndi_name

    def run(self):
        # 创建发送实例
        ndi_send = ndi.send_create(self._ndi_desc)
        
        while self.running:
            self.status_update.emit("🟡 正在尝试拉流...")
            try:
                container = av.open(self.stream_url, options=self.extra_options, timeout=5)
                self.status_update.emit("🟢 信号已接入")
            except Exception as e:
                print(f"❌ 连接失败 (尝试 {self.stream_url}): {e}")
                self.status_update.emit("🔴 无信号，3秒后重连...")
                for _ in range(30):
                    if not self.running: 
                        break
                    time.sleep(0.1)
                continue

            video_stream = next((s for s in container.streams if s.type == 'video'), None)
            audio_stream = next((s for s in container.streams if s.type == 'audio'), None)
            
            # 【修复 Bug】动态获取源流帧率，防止 vMix 因帧率不匹配导致持续丢帧
            fps_n, fps_d = 30000, 1001 # 默认值
            if video_stream and video_stream.average_rate:
                fps_n = video_stream.average_rate.numerator
                fps_d = video_stream.average_rate.denominator

            resampler = av.AudioResampler(format='fltp', layout='stereo', rate=48000) if audio_stream else None

            video_frame_ndi = ndi.VideoFrameV2()
            fps_calc_time = time.time()
            frame_count, bytes_count = 0, 0

            try:
                for packet in container.demux(video=0, audio=0):
                    if not self.running: break
                    if packet.size: bytes_count += packet.size

                    for frame in packet.decode():
                        # --- 视频处理：RGBA 模式 ---
                        if isinstance(frame, av.VideoFrame):
                            # 【核心修复 2：切换为 rgba，每像素 4 字节】
                            rgba_frame = frame.reformat(format='rgba')
                            img = rgba_frame.to_ndarray()
                            h, w, _ = img.shape
                            
                            # 强制内存连续
                            video_frame_ndi.data = np.ascontiguousarray(img)
                            video_frame_ndi.xres = w
                            video_frame_ndi.yres = h
                            # 使用 RGBA 标识，这在 OBS 中兼容性最好
                            video_frame_ndi.FourCC = ndi.FOURCC_VIDEO_TYPE_RGBA
                            # 【关键：Stride 必须是 w * 4】
                            video_frame_ndi.line_stride_in_bytes = w * 4
                            
                            video_frame_ndi.picture_aspect_ratio = float(w) / float(h)
                            # 【修复】使用真实的帧率元数据
                            video_frame_ndi.frame_rate_N = fps_n
                            video_frame_ndi.frame_rate_D = fps_d
                            
                            # 【核心修复 3：平滑时间戳算法】解决 vMix 丢帧问题
                            # 不再使用抖动的系统时间，而是生成数学上完美的等间隔时间戳
                            now_ns = int(time.time() * 10000000)
                            if self.last_ndi_ts == 0 or abs(now_ns - self.last_ndi_ts) > 5000000:
                                # 如果是首帧，或时间漂移超过 500ms (网络卡顿)，则重置为当前时间
                                self.last_ndi_ts = now_ns
                            else:
                                # 否则，严格按照帧间隔递增 (1秒 = 10,000,000 单位)
                                # 公式：步长 = 1e7 * 分母 / 分子
                                step = int(10000000 * fps_d / fps_n)
                                self.last_ndi_ts += step
                            
                            video_frame_ndi.timestamp = self.last_ndi_ts
                            
                            ndi.send_send_video_v2(ndi_send, video_frame_ndi)

                            # UI 渲染
                            frame_count += 1
                            small = rgba_frame.reformat(width=320, height=180, format='rgb24').to_ndarray()
                            qimg = QImage(small.data, 320, 180, 320 * 3, QImage.Format_RGB888).copy()
                            self.frame_ready.emit(qimg)

                            now = time.time()
                            if now - fps_calc_time >= 1.0:
                                kbps = (bytes_count * 8) / 1000 / (now - fps_calc_time)
                                conns = ndi.send_get_no_connections(ndi_send, 0)
                                self.stats_update.emit({
                                    "fps": round(frame_count/(now-fps_calc_time), 1),
                                    "width": w, "height": h, "kbps": int(kbps), "ndi_conns": conns
                                })
                                frame_count, bytes_count, fps_calc_time = 0, 0, now

                        # --- 音频处理 ---
                        elif isinstance(frame, av.AudioFrame) and resampler:
                            resampled = resampler.resample(frame)
                            for f in resampled:
                                audio_arr = np.ascontiguousarray(f.to_ndarray(), dtype=np.float32)
                                if len(audio_arr) >= 2:
                                    rms_l = np.sqrt(np.mean(audio_arr[0]**2))
                                    rms_r = np.sqrt(np.mean(audio_arr[1]**2))
                                    # 将物理电平转为 0-100 的 UI 刻度
                                    vu_l = min(100, max(0, int(20 * math.log10(rms_l + 1e-6) + 100)))
                                    vu_r = min(100, max(0, int(20 * math.log10(rms_r + 1e-6) + 100)))
                                    self.vu_update.emit(vu_l, vu_r)
                                af_ndi = ndi.AudioFrameV2()
                                af_ndi.sample_rate, af_ndi.no_channels, af_ndi.no_samples = f.rate, 2, f.samples
                                af_ndi.data = audio_arr
                                af_ndi.channel_stride_in_bytes = audio_arr.strides[0]
                                ndi.send_send_audio_v2(ndi_send, af_ndi)

            except Exception as e:
                print(f"解析异常: {e}")
            
            container.close()
            if not self.running: break
            time.sleep(1)

        ndi.send_destroy(ndi_send)

    def stop(self):
        self.running = False

# ==========================================
# 组件：16:9 强制比例容器 (新增)
# ==========================================
class AspectRatioContainer(QWidget):
    def __init__(self, child):
        super().__init__()
        self.child = child
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignCenter)
        layout.addWidget(child)
        
    def resizeEvent(self, event):
        w = event.size().width()
        h = event.size().height()
        # 核心算法：无论网格怎么被外力拉扯，内部窗口死咬 16:9 不放
        if w * 9 > h * 16:
            self.child.setFixedSize(int(h * 16 / 9), h)
        else:
            self.child.setFixedSize(w, int(w * 9 / 16))
        super().resizeEvent(event)

# ==========================================
# 组件：ChannelWidget (UI 样式修复版)
# ==========================================
class ChannelWidget(QFrame):
    def __init__(self, channel_id, parent):
        super().__init__()
        self.channel_id, self.parent_win = channel_id, parent
        self.video_thread = None
        self._dying_thread = None # 初始化占位
        self.current_ticket = None
        
        # 【修复：字体与边框样式】
        self.setStyleSheet("""
            QFrame { 
                background-color: #FFFFFF; 
                border-radius: 12px; 
                border: 1px solid #E5E5EA; 
            }
            QLabel { 
                color: #1D1D1F; 
                border: none; 
            }
            QLineEdit, QComboBox { 
                border: 1px solid #D1D1D6; 
                border-radius: 6px; 
                padding: 8px; 
                background-color: #F5F5F7;
                color: #1D1D1F;
            }
            QPushButton { 
                background-color: #0066CC; 
                color: white; 
                border-radius: 6px; 
                padding: 8px; 
                font-weight: bold; 
                border: none;
            }
            QPushButton#stopBtn { background-color: #FF3B30; }
            
            /* 【修复：音量柱样式】 */
            QProgressBar { 
                border: 1px solid #E5E5EA;
                border-radius: 4px; 
                background-color: #F5F5F7; 
                width: 8px; 
            }
            QProgressBar::chunk { 
                border-radius: 4px; 
                background-color: #34C759; 
            }
        """)

        layout = QVBoxLayout(self)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack)
        self.init_ui()

    def init_ui(self):
        # --- 输入页 ---
        self.in_p = QWidget()
        l1 = QVBoxLayout(self.in_p)
        
        self.combo_proto = QComboBox()
        self.combo_proto.addItems(["RTMP (默认)", "SRT (低延迟)"])
        self.combo_proto.setFixedWidth(160)
        
        self.t_in = QLineEdit()
        self.t_in.setPlaceholderText("6位授权码")
        self.t_in.setAlignment(Qt.AlignCenter)
        self.t_in.setFixedWidth(160) # 优化：收窄输入框

        self.btn = QPushButton(f"接入通道 {self.channel_id}")
        self.btn.clicked.connect(self.start_auth)
        self.btn.setFixedWidth(160) # 优化：收窄按钮

        self.err = QLabel("")
        self.err.setStyleSheet("color: #FF3B30; font-size: 11px;")
        
        l1.addStretch()
        l1.addWidget(self.combo_proto, 0, Qt.AlignCenter)
        l1.addWidget(self.t_in, 0, Qt.AlignCenter)
        l1.addWidget(self.btn, 0, Qt.AlignCenter)
        l1.addWidget(self.err, 0, Qt.AlignCenter)
        l1.addStretch()
        
        # --- 监看页 ---
        self.mon_p = QWidget()
        l2 = QVBoxLayout(self.mon_p)
        
        # 顶部标题栏
        h1 = QHBoxLayout()
        self.title = QLabel("🔴 就绪")
        self.title.setFont(QFont("Arial", 12, QFont.Bold))
        stop = QPushButton("断开"); stop.setObjectName("stopBtn"); stop.setFixedSize(60, 30)
        stop.clicked.connect(self.disconnect_stream)
        h1.addWidget(self.title); h1.addStretch(); h1.addWidget(stop)
        
        # --- 中间画面与音量柱层 ---
        h2 = QHBoxLayout()
        h2.setSpacing(10) # 画面与音量柱的间距
        
        self.v_l = QLabel("等待信号...")
        self.v_l.setStyleSheet("background: black; color: white; border-radius: 8px;")
        self.v_l.setAlignment(Qt.AlignCenter)
        self.video_wrapper = AspectRatioContainer(self.v_l)
        
        # 【关键修复】：改用 QHBoxLayout 让左右声道并排显示
        vu_layout = QHBoxLayout()
        vu_layout.setSpacing(4) # 两根音量柱之间的间距
        
        self.v_l_bar = QProgressBar(); self.v_l_bar.setOrientation(Qt.Vertical)
        self.v_r_bar = QProgressBar(); self.v_r_bar.setOrientation(Qt.Vertical)
        
        # 强制固定宽度，防止音量柱变成“大方块”
        for bar in [self.v_l_bar, self.v_r_bar]:
            bar.setTextVisible(False)
            bar.setRange(0, 100)
            bar.setFixedWidth(8) # 锁定宽度为 8 像素，保持精致感
            vu_layout.addWidget(bar)
        
        h2.addWidget(self.video_wrapper, stretch=1)
        h2.addLayout(vu_layout) # 加入并排的音量柱
        
        # 底部状态栏
        self.stat = QLabel("状态: 待机")
        self.stat.setStyleSheet("color: #8E8E93; font-size: 11px;")
        
        l2.addLayout(h1)
        l2.addLayout(h2, stretch=1)
        l2.addWidget(self.stat)

        self.stack.addWidget(self.in_p)
        self.stack.addWidget(self.mon_p)

    def start_auth(self):
        tk = self.t_in.text().strip()
        if len(tk) != 6: return
        if not self.parent_win.check_ticket(tk, self.channel_id):
            self.err.setText("❌ 该码已在其他通道运行")
            return
        self.btn.setEnabled(False)
        self.auth = AuthThread(self.parent_win.config["server_url"], tk)
        self.auth.finished.connect(self.on_auth)
        self.auth.start()

    def on_auth(self, ok, data, err):
        self.btn.setEnabled(True)
        if not ok: 
            self.err.setText(f"❌ {err}")
            return
        
        self.current_ticket = self.t_in.text().strip()
        self.parent_win.use_ticket(self.current_ticket, self.channel_id)
        
        sk = data.get("stream_key", "Unknown")
        ndi_n = f"CastBase_CH{self.channel_id}_{sk[:6]}"
        
        self.title.setText(f"🟢 {sk[:8]}...")
        self.stack.setCurrentIndex(1)
        
        # 动态解析 IP
        srv_url = self.parent_win.config.get("server_url", "http://127.0.0.1")
        parsed = urllib.parse.urlparse(srv_url)
        host = parsed.hostname or "127.0.0.1"
        
        # 构建 URL (支持 RTMP/SRT 切换)
        extra_opts = {}
        if self.combo_proto.currentIndex() == 1:
            # SRT 模式 (SRS 6.0 标准)
            # 【优化】SRS 标准格式：latency=300(ms) 放入 streamid，移除 URL 后缀参数
            raw_sid = f"#!::r=live/{sk},latency=300,m=request,ticket={self.current_ticket}"
            enc_sid = urllib.parse.quote(raw_sid)
            url = f"srt://{host}:10080?streamid={enc_sid}"
        else:
            # RTMP 模式
            url = f"rtmp://{host}:1935/live/{sk}?ticket={self.current_ticket}"
            # 【优化】RTMP 不需要手动传 rw_timeout，av.open(timeout=5) 会自动处理 TCP 超时
            extra_opts = {}
        
        if self.video_thread:
            self.video_thread.stop()
            
        self.video_thread = VideoEngineThread(url, ndi_n)
        self.video_thread.extra_options = extra_opts # 【关键】注入参数字典
        
        # 【关键：重新连接信号】
        self.video_thread.frame_ready.connect(
            lambda img: self.v_l.setPixmap(QPixmap.fromImage(img).scaled(
                self.v_l.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        )
        self.video_thread.vu_update.connect(
            lambda l, r: (self.v_l_bar.setValue(l), self.v_r_bar.setValue(r))
        )
        self.video_thread.stats_update.connect(
            lambda s: self.stat.setText(
                f"🟢 {s['width']}x{s['height']} | {s['fps']} FPS | 📡 {s['kbps']} kbps | 🔗 NDI连接: {s['ndi_conns']}"
            )
        )
        self.video_thread.status_update.connect(self.stat.setText)
        self.video_thread.start()

    def disconnect_stream(self):
        if self.video_thread:
            self.video_thread.stop()
            # 【关键修复 3】：把正在停机的老线程暂时交给 _dying_thread 引用
            # 这样主线可以立刻往下走，而老线程能在后台安全地自我销毁，不会引发崩溃
            self._dying_thread = self.video_thread 
            self.video_thread = None
            
        self.parent_win.release_ticket(self.current_ticket)
        self.stack.setCurrentIndex(0)
        self.err.setText("")

# ==========================================
# 组件：设置与关于面板 (含 NDI 免责协议)
# ==========================================
class SettingsDialog(QDialog):
    def __init__(self, current_url, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ 设置与关于")
        self.setFixedSize(450, 350)
        
        self.setStyleSheet("""
            QDialog { background-color: #FFFFFF; }
            QLabel { color: #1D1D1F; font-weight: bold; }
            QLineEdit { border: 1px solid #D1D1D6; border-radius: 6px; padding: 8px; background-color: #F5F5F7; color: #1D1D1F;}
            QTextEdit { border: 1px solid #E5E5EA; border-radius: 8px; padding: 10px; background-color: #FAFAFA; color: #666666; font-size: 12px; }
            QPushButton { background-color: #0066CC; color: white; border-radius: 6px; padding: 6px 15px; font-weight: bold; }
            QPushButton:hover { background-color: #005BB5; }
        """)

        layout = QVBoxLayout(self)
        layout.setSpacing(15)
        
        # 1. API 地址设置区
        h_layout = QHBoxLayout()
        h_layout.addWidget(QLabel("CastBase 服务器地址:"))
        self.url_input = QLineEdit(current_url)
        h_layout.addWidget(self.url_input, stretch=1)
        layout.addLayout(h_layout)
        
        # 2. NDI 免责与版权声明区
        layout.addWidget(QLabel("NDI® 授权与免责声明:"))
        self.disclaimer = QTextEdit()
        self.disclaimer.setReadOnly(True) # 设为只读，防止用户修改协议
        
        # 官方标准的双语协议
        agreement_text = (
            "NDI® is a registered trademark of Vizrt NDI AB.\n"
            "本软件(CastBase Multiview)集成了 NDI® (Network Device Interface) 技术。\n\n"
            "【免责声明】\n"
            "1. 本软件按“原样”提供。开发者不对因使用本软件或 NDI® 协议造成的任何直接、间接、偶然的利润损失、直播事故或数据丢失承担法律责任。\n"
            "2. NDI® 视频流的传输稳定性和极致画质，极度依赖于您所处的局域网(LAN)交换机吞吐能力及网线质量(推荐千兆及以上环境)。\n"
            "3. 本应用仅作为视频流接收与路由节点，不修改源画面数据。使用本软件即表示您知晓并遵守 Vizrt NDI AB 的最终用户许可协议(EULA)。"
        )
        self.disclaimer.setPlainText(agreement_text)
        layout.addWidget(self.disclaimer)
        
        # 3. 底部按钮区 (保存 / 取消)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        # 将默认的英文改成中文
        self.buttons.button(QDialogButtonBox.Save).setText("保存设置")
        self.buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self.buttons.button(QDialogButtonBox.Cancel).setStyleSheet("background-color: #E5E5EA; color: #1D1D1F;")
        
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def get_url(self):
        return self.url_input.text().strip()
    
# ==========================================
# 主窗口：CastBase Receiver V2
# ==========================================
class CastBaseReceiverV2(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("CastBase Receiver 2.0 - Multiview")
        if os.path.exists(LOGO_PATH): self.setWindowIcon(QIcon(LOGO_PATH))
        self.resize(1024, 680)
        self.config = self.load_cfg()
        self.active_tickets = {} # {ticket: channel_id}

        cw = QWidget(); self.setCentralWidget(cw)
        main_l = QVBoxLayout(cw)
        
        # Header
        head = QHBoxLayout()
        logo = QLabel()
        if os.path.exists(LOGO_PATH): logo.setPixmap(QPixmap(LOGO_PATH).scaled(32, 32, Qt.KeepAspectRatio))
        title = QLabel("CastBase Multiview")
        title.setFont(QFont("Arial", 20, QFont.Bold))
        set_btn = QPushButton("⚙️ 设置"); set_btn.clicked.connect(self.open_set)
        head.addWidget(logo); head.addWidget(title); head.addStretch(); head.addWidget(set_btn)
        
        grid = QGridLayout()
        for i in range(4):
            ch = ChannelWidget(i+1, self)
            grid.addWidget(ch, i // 2, i % 2)

        main_l.addLayout(head); main_l.addLayout(grid, stretch=1)

    def closeEvent(self, event):
        """【修复】窗口关闭时，强制清理所有后台线程，防止 Abort trap 崩溃"""
        print("正在安全关闭所有通道...")
        for ch in self.findChildren(ChannelWidget):
            ch.disconnect_stream()
            # 如果有正在后台安乐死的线程，必须等待它彻底断气，否则 NDI 销毁时会炸
            if ch._dying_thread and ch._dying_thread.isRunning():
                ch._dying_thread.wait()
        event.accept()

    def check_ticket(self, tk, c_id):
        return tk not in self.active_tickets or self.active_tickets[tk] == c_id

    def use_ticket(self, tk, c_id): self.active_tickets[tk] = c_id

    def release_ticket(self, tk):
        if tk in self.active_tickets: del self.active_tickets[tk]

    def load_cfg(self):
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, 'r') as f: return json.load(f)
        return {"server_url": "http://127.0.0.1"}

    def open_set(self):
        # 呼出高级设置面板
        dialog = SettingsDialog(self.config["server_url"], self)
        
        # 如果用户点击了“保存设置”
        if dialog.exec() == QDialog.Accepted:
            new_url = dialog.get_url()
            if new_url and new_url != self.config["server_url"]: 
                self.config["server_url"] = new_url
                # 写入配置文件
                with open(CONFIG_FILE, 'w') as f: 
                    json.dump(self.config, f)

if __name__ == "__main__":
    # 【核心修复 1】：NDI 引擎必须在整个软件启动时初始化 1 次，绝不能在线程里反复初始化！
    if not ndi.initialize():
        print("❌ 致命错误：NDI 全局底座初始化失败！")
        sys.exit(1)
        
    app = QApplication(sys.argv)
    w = CastBaseReceiverV2()
    w.show()
    
    # 捕获软件退出事件
    ret = app.exec()
    
    # 只有当用户关闭了整个软件，才允许彻底销毁 NDI 底座
    ndi.destroy()
    sys.exit(ret)