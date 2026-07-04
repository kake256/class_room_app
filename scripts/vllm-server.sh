#!/usr/bin/env bash
# vLLMサーバのプロファイル切り替え(ホスト側で実行、すべてDockerコンテナ)。
# 使い方: scripts/vllm-server.sh {q25-7b|q3-8b|stop|status}
#   q25-7b : 一次採点用 Qwen2.5-VL-7B (vLLM v0.8.4)
#   q3-8b  : 審判用 Qwen3-VL-8B FP8 (vLLM v0.11.0、要CUDA回避策)
set -euo pipefail

CACHE=/home/qwen/.cache/huggingface
NAME=vllm

wait_ready() {
  echo "起動待ち(モデルロード数分)..."
  for _ in $(seq 1 120); do
    if curl -s -m 3 http://localhost:8000/v1/models >/dev/null 2>&1; then
      echo "ready: $(curl -s http://localhost:8000/v1/models | grep -o '"id":"[^"]*"' | head -1)"
      return 0
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' $NAME 2>/dev/null)" != "true" ]; then
      echo "起動失敗:"; docker logs $NAME 2>&1 | grep -iE 'ValueError|OutOfMemory|RuntimeError' | tail -3
      return 1
    fi
    sleep 10
  done
  echo "timeout"; return 1
}

case "${1:-}" in
  q25-7b)
    docker rm -f $NAME >/dev/null 2>&1 || true
    docker run -d --name $NAME --gpus all \
      --tmpfs /usr/local/cuda/compat \
      -v $CACHE:/root/.cache/huggingface \
      -p 8000:8000 vllm/vllm-openai:v0.8.4 \
      --model Qwen/Qwen2.5-VL-7B-Instruct \
      --max-model-len 16384 \
      --gpu-memory-utilization 0.82 \
      --max-num-seqs 2 \
      --limit-mm-per-prompt image=10 \
      --mm-processor-kwargs '{"max_pixels": 802816}' >/dev/null
    wait_ready ;;
  q3-8b)
    # util 0.90はViT活性化でOOMすることがある(2026-07-03実測)ため0.85
    docker rm -f $NAME >/dev/null 2>&1 || true
    docker run -d --name $NAME --gpus all \
      -e NVIDIA_DISABLE_REQUIRE=1 \
      -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
      --tmpfs /usr/local/cuda/compat \
      -v $CACHE:/root/.cache/huggingface \
      -p 8000:8000 vllm/vllm-openai:v0.11.0 \
      --model Qwen/Qwen3-VL-8B-Instruct-FP8 \
      --max-model-len 24576 \
      --gpu-memory-utilization 0.85 \
      --max-num-seqs 2 \
      --limit-mm-per-prompt '{"image": 10}' >/dev/null
    wait_ready ;;
  stop)
    docker rm -f $NAME >/dev/null 2>&1 || true; echo stopped ;;
  status)
    docker ps --filter name=$NAME --format '{{.Names}} {{.Status}}'
    curl -s -m 3 http://localhost:8000/v1/models 2>/dev/null | grep -o '"id":"[^"]*"' | head -1 || echo "(API応答なし)" ;;
  *)
    echo "usage: $0 {q25-7b|q3-8b|stop|status}"; exit 1 ;;
esac
