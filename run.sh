#!/usr/bin/env bash
# 採点システムの簡易実行スクリプト(すべてDocker内で実行)
set -euo pipefail
cd "$(dirname "$0")"

usage() {
  cat <<'EOF'
使い方: ./run.sh <コマンド> [引数]

  ./run.sh test                             ユニットテスト
  ./run.sh list                             コースの課題一覧(courseWorkId確認)
  ./run.sh verify <courseWorkId> [truth.csv]
                                            過去課題を取得・採点し人間の成績と傾向比較
                                            (truth.csv省略時はClassroomの確定成績を使用)
  ./run.sh calibrate [dir] [truth.csv]      ローカルPDFでキャリブレーション
                                            (既定: samples samples/truth.csv)
  ./run.sh watch <courseWorkId> [間隔秒]     提出を定期的に自動取得して採点(既定3600秒)
  ./run.sh run <courseWorkId>               fetch→採点を1回実行(締切時の最終バッチ)
  ./run.sh serve {q25-7b|q3-8b|stop|status} vLLMサーバのモデル切り替え
  ./run.sh refine <courseWorkId> [anchorId]  審判フェーズ(judge再採点+ペアワイズ絞り込み)
                                            ※実行前に ./run.sh serve q3-8b
  ./run.sh report <courseWorkId>            集計CSV+講義中用サマリ表示
  ./run.sh push-grades-dry <courseWorkId>   下書き点書き込みの対象確認(dry-run)
  ./run.sh push-grades <courseWorkId>       auto_0/1/2の下書き点をClassroomへ書き込み
  ./run.sh api-up / api-down                採点API(localhost:8800)の起動/停止
EOF
  exit 1
}

[ $# -ge 1 ] || usage
cmd=$1; shift || true

# 初回のみOAuth用ポートを公開する(token.jsonが空なら認証が必要)
PORTS=""
if [ ! -s token.json ]; then
  PORTS="--service-ports"
  echo "[run.sh] token.json が未作成のため初回OAuth認証が走ります。"
  if [ "${TERM_PROGRAM:-}" = "vscode" ]; then
    cat <<'EOF'
[run.sh] VSCode Remote-SSH を検知しました。手順:

  1. この後に表示される認証URL(https://accounts.google.com/...)を
     Ctrl+クリック(手元のブラウザで開く)して許可する
  2. VSCodeがポート8765を自動転送するため、リダイレクトはそのまま届きます
     (届かない場合: パネルの「ポート」タブで 8765 を手動追加)
  3. token.json に保存されて完了(次回以降この手順は不要)

EOF
  elif [ -n "${SSH_CONNECTION:-}" ]; then
    host=$(hostname)
    cat <<EOF
[run.sh] SSH接続中のため、手元のPCでポートフォワーディングが必要です:

  1. 手元のPCで別ターミナルを開き、次を実行:
       ssh -L 8765:localhost:8765 ${USER}@${host}
  2. この後に表示される認証URL(https://accounts.google.com/...)を
     手元のPCのブラウザで開いて許可する
  3. リダイレクト(http://localhost:8765/...)がトンネル経由でここに届き、
     token.json に保存されて完了(次回以降この手順は不要)

EOF
  else
    echo "[run.sh] 表示されるURLをブラウザで開いて許可してください。"
  fi
fi

dc() { docker compose run --rm $PORTS grader "$@"; }

case "$cmd" in
  test)      docker compose run --rm test ;;
  list)      dc list ;;
  verify)
    [ $# -ge 1 ] || usage
    cw=$1
    if [ $# -ge 2 ]; then dc verify --coursework "$cw" --truth "$2"
    else dc verify --coursework "$cw"; fi ;;
  calibrate) dc calibrate --dir "${1:-samples}" --truth "${2:-samples/truth.csv}" ;;
  watch)
    [ $# -ge 1 ] || usage
    dc watch --coursework "$1" --interval "${2:-3600}" ;;
  run)
    [ $# -ge 1 ] || usage
    cw=$1; shift
    dc run --coursework "$cw" "$@" ;;   # 追加引数(--lenient等)をそのまま渡す
  serve)
    [ $# -ge 1 ] || usage
    bash "$(dirname "$0")/scripts/vllm-server.sh" "$1" ;;
  refine)
    [ $# -ge 1 ] || usage
    if [ $# -ge 2 ]; then dc refine --coursework "$1" --anchor "$2"
    else dc refine --coursework "$1"; fi ;;
  report)
    [ $# -ge 1 ] || usage
    dc report --coursework "$1" ;;
  push-grades)
    [ $# -ge 1 ] || usage
    dc push-grades --coursework "$@" ;;
  push-grades-dry)
    [ $# -ge 1 ] || usage
    dc push-grades --dry-run --coursework "$1" ;;
  api-up)    docker compose up -d api && echo "採点API: http://localhost:8800" ;;
  api-down)  docker compose stop api ;;
  build)     docker compose build ;;
  *)         usage ;;
esac
