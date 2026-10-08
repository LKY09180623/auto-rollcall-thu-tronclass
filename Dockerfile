FROM python:3.10-slim

# 設定工作目錄
WORKDIR /app

# 安裝依賴套件
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install paho-mqtt

# 複製執行所需檔案
COPY troTHU ./troTHU
COPY scanner_app ./scanner_app
COPY scripts ./scripts
COPY probe_daemon.py .
COPY render_start.py .
COPY config.advanced.example.yaml ./config.advanced.yaml

# 設定環境變數
ENV PYTHONUNBUFFERED=1

# 啟動程序管理與健康檢查入口
CMD ["python", "render_start.py"]
