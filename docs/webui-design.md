# Web UI 化の検討メモ

> 初期検討を残しつつ、運用に関する記述は現行MVPへ更新している。正本は`README.md`、
> `docs/model-controller.md`、`docs/teacher-review-workflow.md`、`docs/grade-writeback.md`とする。

## 目的

Google Classroom の課題選択から一次採点、審判、レポート確認までを、CLIに不慣れなTAでも
ブラウザから安全に実行できるようにする。LLMの判断だけで成績を返却する仕組みにはせず、
人間確認と既存のClassroom書き戻し導線を維持する。

## 実行環境の絶対条件

開発ホストと実行ホストを明確に分離する。

| ホスト | 役割 |
|---|---|
| `qwen@192.168.112.250` | コード編集、文書作成、git操作のみ。ソースコードの正本 |
| SSH alias `kake-251` (`kake@192.168.112.251`) | テスト、Docker、API/Web UI、vLLM、採点ジョブ、データ保存 |

251にはCodexや常駐エージェントを置かない。判断、コマンド発行、監視はすべて250側から
`ssh kake-251 '...'` で行う。Web UI/APIは251上のDockerで起動し、利用者のブラウザから
SSHポート転送などを経由してアクセスする。

`data/` は提出物、採点結果、ジョブログを含めて251だけに存在させる。250へ同期・取得しない。
結果確認や集計も251上で実行し、標準出力のテキストだけをSSH越しに確認する。ユーザーが
個別ファイルの取得を明示した場合を除き、PDF、画像、CSV、JSONを250へ持ち帰らない。

250から251へコードを同期するときは `rsync` を使用し、少なくとも次を守る。

- `data/` とキャッシュ、仮想環境、一時ファイルを除外する。
- `--delete` を使用しない。
- 実行・テストの直前に毎回同期する。
- 250をsource of truthとし、251ではコードを編集しない。
- `.env`、トークン、認証情報の内容を標準出力やログへ表示しない。

250で許可する実行は、`git diff`、`rg`などの読み取り、ファイル編集、git操作、および
`python -m py_compile`／`node --check`程度の静的構文チェックだけとする。Pythonプログラム、
pytest、Docker、サーバー、依存インストールはすべて251で実行する。

## 現状と課題

現状は `run.sh` と `python -m grader` が処理の正本で、FastAPIには結果取得と簡易ジョブ起動がある。
Web UIの土台は存在するが、運用画面としては次が不足している。

- 課題一覧、登録済みルーブリック、アンカー答案を一画面で確認できない。
- ジョブ状態はプロセスメモリだけにあり、再起動で消える。同じ課題の競合実行も防げない。
- stdout末尾4,000文字しか保持せず、フェーズ別進捗や学生単位の進捗が分からない。
- 主導線の`full`は、APIから分離したallowlist型model-controllerで一次モデルと審判モデルを
  切り替え、`run → refine → report`を一気通貫で実行する。個別`run/refine/report`は診断用の
  互換経路として残し、個別実行時は従来どおり手動で準備したモデルを開始直前に確認する。
- APIトークン未設定でもジョブ起動できる。外部公開時の誤設定を検知できない。
- 結果JSON／CSVの破損や途中更新に備えた原子的書き込みがない。
- 旧category名と教員確認状態は分離済みで、UIでは全categoryをAI採点案として扱う。
- 精度評価の根拠と、最近追加されたlength bonusの回帰テストが不足している。
- Classroomの返却DOMは未検証であり、Web UIから不可逆操作まで一体化するのは危険である。

## 推奨スコープ

### MVPで実装する

1. ダッシュボード
   - API、設定、Classroom認証、一次／審判モデルの疎通状態
   - 最近のジョブと課題別の最新レポート
2. 課題一覧
   - Classroomから課題ID、タイトル、締切、配点を取得
   - `assignments` の課題キー／ルーブリック登録状況を表示
3. 採点ウィザード
   - 課題とstrict/lenientを確認
   - 主導線の`full`でモデル切替、一次採点、審判、集計を連続実行
   - 個別フェーズは診断用互換経路として維持
4. ジョブ監視
   - queued/running/succeeded/failed/cancelled
   - 現在フェーズ、開始・終了時刻、ログ、エラー
   - 課題単位で書き込みジョブを1つに制限
5. 結果画面
   - category別集計、candidate_3/review優先表示、学生一覧、根拠、flags
   - 氏名／区分による検索・絞り込み
   - CSVダウンロード
6. 安全性
   - Web UIと拡張からClassroom返却は行わない
   - 全categoryを教員確認が必要なAI採点案として扱う
   - 確認済みかつ人間採点のない答案だけを、専用MV3拡張で空欄へ下書き入力する
   - pushはMVP対象外、またはdry-runだけに限定
   - force実行は確認ダイアログ必須

### MVP後に検討する

- model-controllerの運用監視、再開手順、タイムアウト値の実測調整。
- WebSocket/SSEによる学生単位のリアルタイム進捗。MVPは2秒程度のポーリングで十分。
- 複数TAが同じ答案を同時確認する場合の競合制御。
- 実Classroom DOMでのMV3拡張検証とselector更新手順の確立。
- 精度ダッシュボード。匿名化した固定評価セットでモデル／課題別の指標を表示する。

## アーキテクチャ

新しいフロントエンドビルド基盤は導入せず、FastAPIから静的HTML/CSS/JavaScriptを配信する。
現在の小規模・単一運用者用途では、React等を加えるより依存と運用負荷を抑えられる。

```text
Browser
  ├─ SSH port forward等 → 251上の GET /ui/  静的UI
  └─ /api/v1/*                JSON API
        │
        ├─ JobService         排他・状態・永続化
        ├─ grading functions  fetch/grade/refine/reportを直接呼ぶ
        └─ data/jobs/*.json   ジョブと監査情報
```

既存の `/health`, `/grades/*`, `/jobs` は互換性のため残し、新UIはバージョン付きAPIを使う。
ジョブ実行でCLI subprocessを恒久的な境界にせず、サービス層から既存Python関数を呼ぶ。ただし
MVPの小さな差分を優先する場合、最初はsubprocessを残してもよい。その場合も状態管理と排他は
サービス層に分離し、後から実行方式を交換できるようにする。

## API案

- `GET /api/v1/status`: 設定、認証ファイル、モデル疎通、実行中ジョブ
- `GET /api/v1/courseworks`: Classroom課題一覧と登録状況
- `GET /api/v1/courseworks/{id}/results`: レポートJSON
- `GET /api/v1/courseworks/{id}/report.csv`: CSVダウンロード
- `POST /api/v1/jobs`: `phase`, `coursework_id`, `lenient`, `force`, `anchor`
- `GET /api/v1/jobs`: ジョブ一覧
- `GET /api/v1/jobs/{id}`: 状態、フェーズ、ログ

POSTは既存の `X-API-Key` を必須にする。token未設定時に許可するのはloopbackからのアクセスだけとし、
プロキシ公開を想定する構成では起動時警告を出す。coursework ID、phase、anchorは厳格に検証する。

## ジョブ状態と排他

ジョブは `data/jobs/<job-id>.json` に原子的に保存する。最低限のフィールドは以下。

```json
{
  "id": "...",
  "coursework_id": "...",
  "phase": "run",
  "status": "queued",
  "current_step": "fetch",
  "created_at": "...",
  "started_at": null,
  "finished_at": null,
  "options": {"lenient": false, "force": false},
  "log": [],
  "error": null
}
```

同一courseworkに対する `run/refine/report` は同時に1件だけ許可し、競合時はHTTP 409を返す。
異なる課題の並列処理も、単一vLLMを共有する現状では既定1ジョブとする。API再起動時に
`running` のまま残ったジョブは `interrupted` に遷移させる。

ジョブの実行と通常の状態表示は251上のWeb UIが担える。ただし、Web UI自身やDocker、vLLMが
停止した場合に251上で自己復旧するエージェントは存在しない。そのため起動、モデル切り替え、
長時間ジョブの健全性確認は250からSSHで実施する。バックグラウンド処理を開始した場合は、
プロセスとログを定期的にポーリングし、投げっぱなしにしない。

## UI画面

- `/ui/`: 状態カード、課題一覧、最近のジョブ
- `/ui/courseworks/{id}`: 実行設定、フェーズボタン、最新集計
- `/ui/jobs/{id}`: 状態、ログ、再実行導線
- 結果表: `review` → `candidate_3` → `auto_3` → `auto_0/1/2` → `not_submitted` の順

色だけに依存せず、区分名とアイコン／テキストを併用する。学生氏名とevidenceを扱うため、
外部CDN、解析タグ、外部フォントを使わない。

## 判定状態と自動返却方針

`auto_3` は「システム確認済みの3点」と定義し、既存ユーザースクリプトの
「確定のみ入力」で自動返却する。judge満点による確定と、length/keyword bonusによる確定の
どちらも `auto_3` であり、返却可否は同じとする。確定理由はflagsで区別してUIに表示する。

- `auto_0/1/2/3`: 確定組。既存ユーザースクリプトで自動返却可能
- `candidate_3`: 3点候補。人間確認と手動返却が必要
- `review`: 要確認。人間確認と手動返却が必要
- `not_submitted`: 返却対象外

Web UIはClassroom DOMを操作せず、専用MV3拡張へ短命・一回利用の下書きバッチだけを渡す。
`category`と教員確認・下書き可否は次のように分離する。

- `grading_category`: auto_0/1/2/3, candidate_3, review, not_submitted
- `requires_human_review`: bool
- `draft_writeback_allowed`: bool
- `automatic_return_allowed`: 常にfalse（自動返却は実装しない）

CSV読取互換のため既存categoryを維持するが、UI上ではすべて「AI採点案」と表示する。
教員確認済みで、返却済み・人間採点・既存点がない答案だけを下書き対象にする。

## テストと完了条件

- API: 認証、入力検証、404、409、ジョブ永続化、再起動復旧をテストする。
- JobService: 状態遷移と同一課題排他を単体テストする。
- Report: auto_3、judge優先、pairwise降格、lateの組合せを回帰テストする。
- UI: 外部依存なしでロードでき、APIエラー、空データ、実行中、失敗状態を表示できる。
- 250からコードを`data/`除外・`--delete`なしで251へ同期する。
- 251上で `docker compose run --rm test` が成功する。
- 251上で `docker compose up -d api` 後、SSH越しに`/ui/`と既存`/health` `/grades/*`を確認する。
- 実ClassroomやvLLMを使わず、モックでジョブを最後まで検証できる。
- テスト、ログ確認、結果集計は251上で行い、250には標準出力以外の実行データを保存しない。

## 実装順序

1. JobService、永続化、排他、APIテスト
2. v1 status/courseworks/results API
3. 静的Web UIと課題・結果画面
4. ジョブ実行画面とポーリング
5. report判定の回帰テストと文書更新
6. Docker上の統合確認
