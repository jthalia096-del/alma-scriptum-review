FROM python:3.11-slim

ENV FORCE_REBUILD=2
ENV PYTHONUNBUFFERED=1
ENV QTWEBENGINE_DISABLE_SANDBOX=1
ENV QTWEBENGINE_CHROMIUM_FLAGS="--no-sandbox --disable-gpu"
ENV QT_QPA_PLATFORM=offscreen
ENV QT_QUICK_BACKEND=software
ENV QT_OPENGL=software
ENV LIBGL_ALWAYS_SOFTWARE=1
ENV MESA_LOADER_DRIVER_OVERRIDE=llvmpipe

RUN apt-get update && apt-get install -y \
    calibre \
    xvfb \
    tesseract-ocr \
    tesseract-ocr-eng \
    fonts-dejavu-core \
    libegl1 \
    libgl1 \
    libopengl0 \
    libxcb-cursor0 \
    libxkbcommon-x11-0 \
    libnss3 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
