#!/usr/bin/env bash
# vLLMサーバのプロファイル切り替え(ホスト側で実行、すべてDockerコンテナ)。
# 使い方: scripts/vllm-server.sh {q25-7b|q3-8b|minicpm-v45|stop|status}
#   q25-7b     : 一次採点用 Qwen2.5-VL-7B (vLLM v0.8.4)
#   q3-8b      : 審判用 Qwen3-VL-8B FP8 (vLLM v0.11.0、要CUDA回避策)
#   minicpm-v45: 比較検証用 MiniCPM-V-4.5 BF16 (vLLM v0.11.0)
set -euo pipefail

# controllerコンテナではDocker daemonが解釈するホスト絶対パスを明示する。
# ホストから直接使う従来経路ではHF_HOME/HOMEへfallbackする。
if [ "${CGA_HOST_HF_HOME+x}" = x ]; then
  if [ -z "${CGA_HOST_HF_HOME}" ] || [[ "${CGA_HOST_HF_HOME}" != /* ]]; then
    echo "CGA_HOST_HF_HOMEにホストの絶対パスを設定してください" >&2
    exit 1
  fi
  CACHE=${CGA_HOST_HF_HOME}
else
  CACHE=${HF_HOME:-${HOME}/.cache/huggingface}
  mkdir -p "$CACHE"
fi
VLLM_BASE_URL=${VLLM_BASE_URL:-http://localhost:8000}
VLLM_BASE_URL=${VLLM_BASE_URL%/}
NAME=vllm

wait_ready() {
  echo "起動待ち(モデルロード数分)..."
  for _ in $(seq 1 120); do
    if curl -s -m 3 "$VLLM_BASE_URL/v1/models" >/dev/null 2>&1; then
      echo "ready: $(curl -s "$VLLM_BASE_URL/v1/models" | grep -o '"id":"[^"]*"' | head -1)"
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
      -v "$CACHE:/root/.cache/huggingface" \
      -p 8000:8000 vllm/vllm-openai:v0.8.4 \
      --model Qwen/Qwen2.5-VL-7B-Instruct \
      --max-model-len 16384 \
      --gpu-memory-utilization 0.82 \
      --max-num-seqs 4 \
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
      -v "$CACHE:/root/.cache/huggingface" \
      -p 8000:8000 vllm/vllm-openai:v0.11.0 \
      --model Qwen/Qwen3-VL-8B-Instruct-FP8 \
      --max-model-len 24576 \
      --gpu-memory-utilization 0.85 \
      --max-num-seqs 4 \
      --limit-mm-per-prompt '{"image": 10}' \
      --mm-processor-kwargs '{"max_pixels": 802816}' \
      --mm-processor-cache-gb 0 >/dev/null
      # --mm-processor-cache-gb 0: 長時間のrefineで multimodal/cache.py の
      # mm_hash AssertionError によりエンジンが死ぬ事象への対策(真因はMM前処理
      # キャッシュ。2026-07-19確認)。prefix-cachingは無関係だったため再有効化し、
      # ルーブリック共通プレフィックスの再計算を省いて高速化(2026-07-20)。
      # max-num-seqs 2→4: 待ち行列解消(クライアント並列とサーバー処理数を一致させる)
    wait_ready ;;
  minicpm-v45)
    # BF16優先(精度比較条件を揃えるため量子化へは無断で変更しない)。
    # OOM時のフォールバック順: 1) max-num-seqs 4→2  2) max-model-len 16384→12288
    # (量子化版へ切り替える場合は理由と形式を別途記録すること)
    # HF_HUB_DISABLE_XET=1 / HF_HUB_ENABLE_HF_TRANSFER=0: Xet(CAS)転送と
    # hf_transferはいずれも接続不安定時に例外でエンジンを落とす
    # (2026-07-24実測、両方vllm-openaiイメージの既定で有効)。素の
    # 再開可能HTTPレンジ取得は同条件で正常に再開できたため両方無効化。
    docker rm -f $NAME >/dev/null 2>&1 || true
    docker run -d --name $NAME --gpus all \
      -e HF_HUB_DISABLE_XET=1 \
      -e HF_HUB_ENABLE_HF_TRANSFER=0 \
      --tmpfs /usr/local/cuda/compat \
      -v "$CACHE:/root/.cache/huggingface" \
      -p 8000:8000 vllm/vllm-openai:v0.11.0 \
      --model openbmb/MiniCPM-V-4_5 \
      --trust-remote-code \
      --max-model-len 16384 \
      --gpu-memory-utilization 0.85 \
      --max-num-seqs 4 \
      --limit-mm-per-prompt '{"image": 10}' \
      --mm-processor-cache-gb 0 >/dev/null
      # --mm-processor-cache-gb 0: q3-8bと同様、長時間運用でのmm_hash
      # AssertionError再発を避けるため安全側(2026-07-19の対策を踏襲)
    wait_ready ;;
  stop)
    docker rm -f $NAME >/dev/null 2>&1 || true; echo stopped ;;
  status)
    docker ps --filter name=$NAME --format '{{.Names}} {{.Status}}'
    curl -s -m 3 "$VLLM_BASE_URL/v1/models" 2>/dev/null | grep -o '"id":"[^"]*"' | head -1 || echo "(API応答なし)" ;;
  health)
    # エンジンの生存確認: /v1/models応答だけではエンジンクラッシュ
    # (例: mm_hashキャッシュのAssertionError)を検知できないため、
    # 実際にcompletionを1件流して確認する。採点ジョブ投入前に呼ぶこと。
    MODEL=$(curl -s -m 5 "$VLLM_BASE_URL/v1/models" 2>/dev/null \
      | grep -o '"id":"[^"]*"' | head -1 | cut -d'"' -f4)
    if [ -z "$MODEL" ]; then echo "unhealthy: APIが応答しません"; exit 1; fi
    if timeout 90 curl -s -X POST "$VLLM_BASE_URL/v1/chat/completions" \
        -H "Content-Type: application/json" \
        -d "{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"ping\"}],\"max_tokens\":3}" \
        -o /dev/null -w "%{http_code}" | grep -q 200; then
      echo "healthy: $MODEL"
    else
      echo "unhealthy: completionがタイムアウトまたは失敗(エンジンクラッシュの疑い)"
      echo "  対処: $0 q25-7b または $0 q3-8b で再起動"
      exit 1
    fi ;;
  *)
    echo "usage: $0 {q25-7b|q3-8b|minicpm-v45|stop|status|health}"; exit 1 ;;
esac
