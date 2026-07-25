FROM python:3.11-slim

# Word→PDF変換用 LibreOffice(writerのみ)+ 日本語フォント
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY grader/ grader/
COPY scripts/ scripts/
# 公開ZIPへ含めるMV3拡張の固定ファイルだけをimageへ配置する。
COPY extension/manifest.json extension/background.js extension/content.js extension/bridge.js \
     extension/core.js extension/selectors.js extension/options.html \
     extension/options.js extension/README.md extension/
COPY tests/ tests/
# 既定として example を config.yaml として同梱。実運用は compose の volume で
# 本物の config.yaml を上書きマウントする(config.yaml は gitignore 対象)
COPY config.example.yaml config.yaml

ENTRYPOINT ["python", "-m", "grader"]
