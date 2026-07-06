# Google Classroom 提出レポート LLM採点システム

Google Classroom の提出レポート(PDF / Googleドキュメント / Word)を、ローカルGPU上の
VLM(vLLM + Qwen-VL)でマルチモーダル採点する半自動システム。
0〜2点は自動確定、3点は「候補」としてTAが目視確定する。**採点処理はすべてDockerで
実行し、ホスト環境を汚さない**(GPUを使うのは vLLM コンテナのみ)。

## 何をするか

- 提出物を取得 → PDF化 → ページ画像化 → VLMで2回採点 → 集計
- **2段階のハイブリッド採点**で「3点候補の見逃しゼロ」と「候補の絞り込み」を両立
- 遅延・形式違反・未提出を自動仕分け、根拠(evidence)付きで出力
- 成績の書き戻しは「採点API + ブラウザのユーザースクリプト」でログイン済みブラウザから入力

## アーキテクチャ

```
                 ┌── フェーズ1: 一次採点 (Qwen2.5-VL-7B) ── 甘め=見逃さない粗い網
 提出物 ─ fetch ─┤
                 └── フェーズ2: 審判 (Qwen3-VL-8B) ── judge再採点 + ペアワイズ比較で候補を絞る
                                                          │
                                    report(集計CSV/サマリ)┤
                                                          └─ 採点API → ブラウザで成績簿に下書き点入力
```

- **一次採点**は意図的に甘く、人間が3点にする答案を取りこぼさない(高recall)
- **審判フェーズ**が判別力の高いモデルで 0/1/2 の確定点を高精度化し、3点候補を絞り込む
- 詳しい検証根拠は `docs/calibration-policy.md`(ローカル、gitignore)

---

## セットアップ

### 1. 取得と設定ファイル

```bash
git clone git@github.com:kake256/class_room_app.git
cd class_room_app
cp config.example.yaml config.yaml     # 実値を記入(このファイルは gitignore)
```

`config.yaml` の主な項目:

| 項目 | 説明 |
|---|---|
| `classroom.course_id` | 対象コースID(Classroom URLの `/c/` の後ろ。Base64形式でも可) |
| `assignments` | courseWorkId → 課題キー の対応(下記) |
| `vllm.model` | 一次採点モデル(既定 Qwen2.5-VL-7B) |
| `pairwise.model` | 審判モデル(既定 Qwen3-VL-8B-FP8) |
| `pairwise.anchor_student_id` | ペアワイズ比較の基準答案(2点相当)。**課題ごとに選び直す** |
| `pairwise.crit_prune` | 審判の観点合計がこの値未満の候補を降格(既定2.0) |
| `late_penalty` | 遅延減点(既定1) |
| `api.token` | 設定すると採点APIが `X-API-Key` 必須になる |

### 2. Classroom API 認証(初回1回のみ)

1. Google Cloud Console で OAuth クライアント(デスクトップ)を作成し、
   `credentials.json` をプロジェクト直下に置く
2. `touch token.json` してから `run.sh` の任意コマンドを実行すると認証URLが出る
3. ブラウザで開いて許可 → `token.json` に保存(以後は自動更新)

スコープ: `classroom.coursework.students`(下書き点書き込みに必要) /
`rosters.readonly` / `drive.readonly`

**VSCode Remote-SSH の場合**: 出てきた認証URLを Ctrl+クリックで手元ブラウザで許可。
VSCodeがポート8765を自動転送する(届かなければ「ポート」タブで8765を手動追加)。

**素のSSH の場合**: 手元PCで `ssh -L 8765:localhost:8765 <user>@<host>` してから認証URLを開く。
`run.sh` が接続形態を検知して手順を表示する。

### 3. 課題の登録

新しい課題を採点する前に、`./run.sh list` で courseWorkId を確認し、
`config.yaml` の `assignments:` に「courseWorkId → 課題キー」を追加する:

```yaml
assignments:
  "<courseWorkId>": rf        # 例
```

課題キーと対応するルーブリック(`grader/rubric.py` の `ASSIGNMENT_SPECS`):

| キー | 内容 | ルーブリック |
|---|---|---|
| `rf` / `svm` / `adaboost` | 実験レポート(パラメータ変更→識別境界/識別率) | EXPERIMENT(定量評価・実験方法・考察) |
| `kansou1` / `tokubetsu0511` | 講義の感想・まとめ | KANSOU(まとめの具体性・理解・感想) |
| `sukina` | 好きな手法とその理由 | EFFORT(取り組み量+概念理解の軽い確認) |

新しい種類の課題は `ASSIGNMENT_SPECS` に課題文とルーブリック種別を1エントリ追加する。

---

## 採点の実行(フルフロー)

vLLMサーバはモデルを切り替えて2フェーズで使う(すべてDockerでGPUを使うのはこれのみ):

```bash
# フェーズ1: 一次採点
./run.sh serve q25-7b                   # 一次採点モデルのvLLMを起動
./run.sh run <courseWorkId>             # fetch → render → 2回採点
#   感想文寄りの課題を甘めに採点したいとき:
docker compose run --rm grader run --coursework <courseWorkId> --lenient

# フェーズ2: 審判(0/1/2の確定点向上 + 3点候補の絞り込み)
./run.sh serve q3-8b                    # 審判モデルに切り替え
./run.sh refine <courseWorkId>          # judge再採点 + ペアワイズ比較
#   基準答案(anchor)をコマンドで指定する場合:
./run.sh refine <courseWorkId> <anchor_student_id>

# 集計
./run.sh report <courseWorkId>          # サマリ表示 + data/report/<cw>.csv
```

`report` の出力 CSV / サマリ:

- `category`: `auto_0`/`auto_1`/`auto_2`(自動確定)、`candidate_3`(3点候補、TA確認)、
  `review`(要確認)、`not_submitted`(未提出)
- `tier`: 候補内の格付け。`strong`(最有力、両順ペアワイズ勝ち)→ `borderline` の順で確認
- `judge_score` / `evidence` / `flags` も出力

### 採点姿勢の切り替え

`--lenient`(甘め)/ `--strict`(厳しめ、既定)を `run`/`verify`/`grade`/`watch` に付けられる。
甘めは「明らかな不足がなければ加点・迷ったら高い方」。EFFORT ルーブリック(`sukina`)は
「ある程度書けていて概念をある程度理解していれば3点」を内蔵(lenient不要)。

### 提出期間中の自動採点(任意)

```bash
./run.sh serve q25-7b
./run.sh watch <courseWorkId> 3600      # 1時間ごとに取得→採点(処理済みskip、再提出は自動再採点)
```

---

## 成績の書き戻し(下書き点の入力)

Classroom API は「課題を作成したプロジェクト」以外からの成績書き込みを拒否する
(UI作成課題は `ProjectPermissionDenied`)。そのため書き戻しは **2通り**:

- **方式A**: 採点API + ブラウザのユーザースクリプト … UI作成課題でも可(推奨・実運用済み)。
  `./run.sh api-up` でAPIを起動し、`browser/classroom-grader.user.js` のパネルから
  **未返却の全員にシステム点(1〜3点)を下書き一括入力**。「下書き全削除」で入れ直しも可能。
  返却済みの生徒には触れない(state判定)。外部NWからは SSH転送 /
  Cloudflare Tunnel(手元PCに導入不要) / Tailscale で到達
- **方式B**: `./run.sh push-grades <cw>` … このツール/API経由で作成した課題のみ直接書き込み

運用の流れ: 全員分を下書き入力 → 成績簿で判定のズレだけ手直し → 「返却」で確定。
再分析後は「下書き全削除」→ 再入力。サーバー構成・アクセス経路(localhost/SSH/
Cloudflare/Tailscale)・ユーザースクリプトの詳細は
**[docs/grade-writeback.md](docs/grade-writeback.md)** を参照。

---

## 過去課題での傾向検証(verify)

人間の確定成績と採点結果を突き合わせて、一致率・甘辛傾向を確認する:

```bash
./run.sh verify <courseWorkId>                       # Classroomの確定成績(assignedGrade)と比較
./run.sh verify <courseWorkId> samples/past.csv      # 正解CSV(student_id/name/email + human_score)
```

完全一致率・±1以内一致率・平均差(甘い/辛い)・不一致答案一覧を表示し、
`data/report/<cw>_verify.csv` に保存する。truth CSV はコンテナから見える
`samples/` か `data/` に置く。

---

## 採点ポリシー(要点)

- 1答案につき独立2回採点。一致→採用、不一致→低い方 + `inconsistent` でレビュー行き
- 0/1/2点は自動確定(警告フラグがあればレビュー行き)、3点は自動確定せず候補提示
- 審判フェーズ: judge が 0/1/2 を高精度化、ペアワイズで候補を降格(両順負け/tieのみ)、
  judge観点合計が閾値未満の候補も降格。人間3点の見逃しゼロを維持
- `late` は report 側で −1(3点満点は減点保留 `late_waiver_candidate`)
- 再提出は Drive の modifiedTime / PDFハッシュで検知して自動再採点
- ローカル運用のため匿名化しない。名簿APIから実名を取得しCSV・サマリに表示する

---

## コマンド一覧(run.sh)

```
./run.sh test                             ユニットテスト
./run.sh list                             課題一覧(courseWorkId確認)
./run.sh serve {q25-7b|q3-8b|stop|status} vLLMサーバのモデル切り替え
./run.sh run <cw> [--lenient]             一次採点(fetch→render→grade)
./run.sh refine <cw> [anchorId]           審判フェーズ(judge再採点+ペアワイズ)
./run.sh report <cw>                       集計CSV+サマリ
./run.sh verify <cw> [truth.csv]          過去課題で人間の成績と傾向比較
./run.sh calibrate [dir] [truth.csv]      ローカルPDFでキャリブレーション
./run.sh watch <cw> [間隔秒]               提出を定期取得して自動採点
./run.sh push-grades-dry <cw>             下書き点書き込みの対象確認
./run.sh push-grades <cw>                 下書き点を書き込み(API作成課題のみ)
./run.sh api-up / api-down                採点API(localhost:8800)の起動/停止
./run.sh build                            Dockerイメージのビルド
```

---

## ディレクトリ

```
grader/            採点ロジック(fetch/render/grade/pairwise/report/push/api …)
browser/           成績簿入力ユーザースクリプト
scripts/           vllm-server.sh(モデルプロファイル切り替え)
tests/             ユニットテスト
config.example.yaml 設定テンプレート(実値の config.yaml は gitignore)
docker-compose.tailscale.yml  Tailscale経由でAPIをHTTPS公開するオーバーレイ
data/
  raw/ pdf/ pages/ results/ meta/ report/ calibration/   各段階の中間成果物(冪等・部分再実行可)
```

`data/`・`config.yaml`・`credentials.json`・`token.json`・`.env`・学生氏名を含む
CSVやドキュメントは **gitignore 済み**(個人情報をリポジトリに含めない)。
