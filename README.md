# Google Classroom 提出レポート LLM採点システム

Google Classroom の提出レポート(PDF/Googleドキュメント/Word)を、ローカルGPU上の
VLM(vLLM + Qwen2.5-VL)でマルチモーダル採点する。0〜2点は自動確定、3点は
「3点候補」としてTAに提示する半自動運用。**すべてDockerで実行し、ホスト環境を汚さない。**

## 前提

- ホスト側で vLLM を起動しておく(GPUを使うのはvLLMのみ。本ツールはCPUのみ):

```bash
vllm serve Qwen/Qwen2.5-VL-32B-Instruct-AWQ \
  --max-model-len 16384 \
  --limit-mm-per-prompt image=8
```

- `config.yaml` の `vllm.base_url` は Docker 内から見える
  `http://host.docker.internal:8000/v1` を設定済み
- `classroom.course_id` に対象コースIDを設定する

## かんたん実行(run.sh)

```bash
./run.sh test                          # ユニットテスト
./run.sh list                          # 課題一覧(courseWorkId確認)
./run.sh verify <courseWorkId>         # 過去課題で傾向検証(Classroomの確定成績と比較)
./run.sh verify <courseWorkId> samples/past_scores.csv   # 正解CSV指定
./run.sh calibrate                     # samples/ のPDFでキャリブレーション
./run.sh watch <courseWorkId>          # 提出を1時間ごとに自動取得・採点
./run.sh run <courseWorkId>            # 締切時の最終バッチ
./run.sh report <courseWorkId>         # 集計CSV+サマリ
```

token.json が空のときは自動でOAuth認証モード(ポート公開)になり、
表示されるURLをブラウザで開くだけで認証できる。

過去課題での検証(`verify`)は、提出物を取得→2回採点→人間の確定成績
(Classroomの assignedGrade、なければ `--truth` CSV: student_id/name/email +
human_score 列)と突き合わせ、完全一致率・±1以内一致率・平均差(甘い/辛い)・
不一致答案の一覧を表示する。CSVは `data/report/<cw>_verify.csv` に保存。
truth CSV はコンテナから見える `samples/` か `data/` に置くこと。

## 使い方(docker compose 直接)

```bash
docker compose build

# ユニットテスト
docker compose run --rm test

# 1) キャリブレーション(最初のマイルストーン)
#    samples/ にPDFを置き、samples/truth.csv (filename,human_score) を用意
docker compose run --rm grader calibrate --dir samples --truth samples/truth.csv
docker compose run --rm grader calibrate --dir samples --truth samples/truth.csv \
  --model Qwen/Qwen2.5-VL-7B-Instruct   # 7Bとの比較

# 2) 提出期間中: watchモード(1時間ごとに Classroom から自動取得→render→grade、
#    処理済みスキップ、再提出は自動再採点)。初回はOAuth認証のため --service-ports を付ける
docker compose run --rm --service-ports grader watch --coursework <courseWorkId> --interval 3600

# 3) 締切(講義開始)時: 最終バッチ
docker compose run --rm grader run --coursework <courseWorkId>

# 4) 講義中: サマリ+CSV
docker compose run --rm grader report --coursework <courseWorkId>
```

## Classroom API 認証(fetch/run/watch に必要)

1. Google Cloud Console で OAuth クライアント(デスクトップ)を作成し
   `credentials.json` をプロジェクト直下に置く
2. `touch token.json` してから run.sh で実行すると認証URLが表示される。
   ブラウザで開いて許可すると `http://localhost:8765` にリダイレクトされ、
   token.json に保存される(以後は自動更新)
   (スコープ: coursework.students.readonly / rosters.readonly / drive.readonly)

   **VSCode Remote-SSH の場合(推奨)**: ターミナルに出る認証URLを
   Ctrl+クリックして手元のブラウザで許可するだけ。VSCodeがポート8765を
   自動転送するためリダイレクトはそのまま届く(届かない場合はパネルの
   「ポート」タブで 8765 を手動追加)

   **素のSSHの場合**: 手元のPCでトンネルを張ってから認証URLを開く:

   ```bash
   ssh -L 8765:localhost:8765 <user>@<リモートホスト>
   ```

   run.sh が接続形態を自動検知して該当する手順を表示する。
   認証は初回の1回だけで、以後 watch/verify などは完全自動
3. 成績の書き戻し: Classroom API は「課題を作成したプロジェクト」以外からの
   書き込みを拒否する(UI作成課題は `ProjectPermissionDenied`)。このため
   下記の「採点API + ブラウザ拡張」方式で、ログイン済みブラウザから下書き点を入力する

## 採点API + 成績簿入力(下書き点の書き戻し)

Classroom APIの書き込み制約を、ログイン済みの自分のブラウザ経由で回避する構成:

```
[GPUマシン] 採点API (localhost:8800)  ──JSON──►  [自分のブラウザ]
   report結果をJSONで返す                    成績簿タブで動くユーザースクリプトが
                                             APIから点数を取得して下書き点を入力
```

1. API起動: `docker compose up -d api`(localhostのみ公開)。ブラウザからの経路:
   - 同一LAN/SSH: `-L 8800:localhost:8800`(VSCodeのポート転送でも可)→ `http://localhost:8800`
   - VPN不可の外部NW(Tailscale): ホストに何も入れずDockerで完結:
     ```bash
     cp .env.example .env    # TS_AUTHKEY を記入(管理コンソールでMagicDNS+HTTPS証明書も有効化)
     docker compose -f docker-compose.yml -f docker-compose.tailscale.yml up -d tailscale api-ts
     ```
     → `https://classroom-grader.<tailnet>.ts.net`(HTTPS化でmixed-content回避、
     Tailnet内=鍵認証のみ到達)。api-tsは結果配信専用(採点は ./run.sh 側で実施)
   - `GET /grades/{courseWorkId}` … report結果をJSONで返す
   - `POST /jobs {coursework_id, phase}` … 採点を非同期起動(run/refine/report/full)
   - `GET /jobs/{id}` … 進捗。`config.yaml` の `api.token` で X-API-Key 必須にできる
2. `browser/classroom-grader.user.js` を Tampermonkey 等に登録
3. 対象課題の成績ページを開き、右下パネルで courseWorkId を入れて
   「プレビュー」(色付けのみ)→確認→「入力実行」
   - 下書き点(draftGrade)のみ入力。返却ボタンには触れない
   - 既に点数があるセルはスキップ、入力後に読み戻し検証、失敗時は中断
   - 成績簿DOMは変わりやすいので、動かない場合はスクリプト内 SELECTORS を調整

## 採点ポリシー(要点)

- 1答案につき独立2回採点。一致→採用、不一致→低い方+`inconsistent`でレビュー行き
- 0/1/2点は自動確定(警告フラグがあればレビュー行き)、3点は `candidate_3` としてTAが目視確定
- `late` は report 側で −1(3点満点は減点保留 `late_waiver_candidate`)
- 再提出は Drive の modifiedTime / PDFハッシュで検知して自動再採点
- ローカル運用のため匿名化はしない。名簿APIから実名を取得し、
  CSV・サマリに氏名を表示する(`data/` は共有・コミット禁止)

## ディレクトリ

```
data/
  raw/ pdf/ pages/ results/ meta/ report/ calibration/
```

各段階の中間成果物を保存し、部分再実行できる(冪等)。
