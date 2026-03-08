# CastBase Multiview NDI Client

🚀 **专为广电与直播团队打造的高性能、多通道流媒体 NDI 接收网关。**

CastBase Multiview 是一款基于 Python 与 C++ 底层引擎开发的轻量级多画面监看与路由软件。它能够稳定拉取 RTMP/SRT 直播流，并将其无损转化为标准 NDI® 信号广播至局域网，完美解决跨语言内存管理与第三方导播软件（如 vMix、OBS）的兼容性痛点。

## ✨ 核心特性

* **双引擎拉流**：支持传统 RTMP 协议与新一代极低延迟、抗弱网的 SRT 协议。
* **广电级 NDI 广播**：
    * 采用 `rgb24/rgba` 内存对齐，彻底解决 Mac/Windows 环境下 OBS 拉取 NDI 黑屏、花屏问题。
    * 强锁指针内存，根治底层 C++ 库导致的 NDI 通道名称乱码。
    * 自动注入微秒级时间戳 (Timestamp)，确保极佳的音画同步。
* **优雅的交互 UI**：基于 PySide6 打造的苹果风深色/浅色自适应界面，包含 16:9 强制自适应容器与实时高保真 VU 音量柱。
* **极简异步架构**：彻底剥离 UI 线程与网络解码线程，断开重连达到“毫秒级”响应，绝不卡死主界面。

## 🛠️ 技术栈

* **GUI 框架**: PySide6
* **音视频解码**: PyAV (FFmpeg 核心)
* **矩阵运算**: NumPy
* **NDI 底层**: ndi-python (需配合系统原生 NDI Tools)

## 📦 如何运行与打包

### 1. 环境准备
确保您的机器已安装 Python 3.9+，并前往 NDI 官网下载安装对应的 **NDI Tools** (包含底层动态链接库)。

```bash
# 安装依赖
pip install -r requirements.txt

开发者运行
python main.py

一键打包 (Mac / Windows)
本项目全面支持 PyInstaller 打包为独立的 .app 或 .exe
pyinstaller --noconfirm --windowed --name "CastBase Multiview" --icon="logo.ico" --collect-all numpy --collect-all av --hidden-import PySide6 --hidden-import NDIlib main.py