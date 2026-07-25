# Claude Code向け実装プロンプト

> **履歴資料:** この初期実装プロンプトはすでに実行済みで、現行仕様書ではない。
> 現行運用は`README.md`、`docs/model-controller.md`、`docs/teacher-review-workflow.md`、
> `docs/grade-writeback.md`を参照する。以下の要求も現行の安全方針へ更新している。

以下をClaude Codeへ、このリポジトリのルートで渡してください。

---

`CLASSROOM-GRADING-AUTOMATION` に、採点処理を安全に実行・監視するWeb UIのMVPを実装してください。

最初に `CLAUDE.md`、`README.md`、`docs/webui-design.md`、`grader/api.py`、
`grader/__main__.py`、`grader/pipeline.py`、`grader/pairwise.py`、`grader/report.py`、
`tests/` を読み、既存設計とテストを壊さない小さな差分に分けて進めてください。

## 実行環境に関する絶対条件

この作業は2台構成です。251にはClaude CodeやCodexを導入できず、SSH経由でしか操作できません。

- 250: `qwen@192.168.112.250`、現在の環境。コード編集、文書、git操作だけを行うsource of truth。
- 251: SSH alias `kake-251` (`kake@192.168.112.251`)。テスト、Docker、Web UI/API、
  vLLM、採点、長時間処理、`data/`の保存を行う計算資源。

以下を厳守してください。

1. 250ではPythonプログラム、pytest、Docker、サーバー、依存インストールを実行しない。
   `python -m py_compile`、`node --check`程度の静的構文チェックだけは許可する。
2. SSH接続には既存aliasだけを使う。最初に
   `ssh -o BatchMode=yes kake-251 'hostname; whoami'` で接続を確認する。新しい鍵は作らない。
3. 実行前に毎回、250から251へコードをrsyncする。`data/`、キャッシュ、仮想環境、出力、
   socket、lock、tmpを除外し、`--delete`を絶対に使わない。251側のコードは直接編集しない。
4. `data/`は251にのみ存在させる。250へ作成、同期、pullしない。結果確認は251上で集計し、
   SSH標準出力のテキストだけを受け取る。ユーザーが明示した個別ファイル以外は持ち帰らない。
5. 長時間ジョブをバックグラウンド起動したら、250からSSHでプロセスとログを定期確認する。
   251に自律的な監視主体はいないため、投げっぱなしにしない。
6. `.env`、APIキー、token、credentialsの内容をcat、ログ、報告へ出さない。

同期は次の形を基準にし、プロジェクトの実パス
`/home/qwen/classroom-grading-automation/` →
`kake-251:/home/kake/classroom-grading-automation/` を使用してください。

```bash
rsync -aHv --partial --update --info=progress2 \
  --exclude='data/' \
  --exclude='__pycache__/' --exclude='.pytest_cache/' \
  --exclude='.mypy_cache/' --exclude='.ruff_cache/' \
  --exclude='node_modules/' --exclude='.venv/' --exclude='venv/' \
  --exclude='.hf_cache/' --exclude='Output/' --exclude='outputs/' \
  --exclude='*.sock' --exclude='*.lock' --exclude='*.tmp' \
  /home/qwen/classroom-grading-automation/ \
  kake-251:/home/kake/classroom-grading-automation/
```

`data/`を除外できていること、コマンドに`--delete`がないことを実行前に再確認してください。

## ゴール

ブラウザから次を実行できる状態にしてください。

1. API・設定・Classroom接続に必要なファイルの状態を確認する。
2. Google Classroomの課題一覧を表示し、課題ID、タイトル、締切、配点、ルーブリック登録状況を確認する。
3. 課題ごとに一次採点 `run`、審判 `refine`、集計 `report` を起動する。
4. strict/lenient、force、refineのanchorを画面で指定・確認する。
5. ジョブのqueued/running/succeeded/failed/interrupted、現在フェーズ、ログを確認する。
6. 最新結果をcategory別に集計し、学生、点数、tier、judge_score、flags、evidenceを確認する。
7. 結果をCSVとしてダウンロードする。

## 制約

- Web UIと拡張からClassroomの「返却」は実行しない。全categoryをAI採点案として教員が確認し、
  専用MV3拡張はClassroomの空欄へ下書き入力だけを行う。
- `auto_*`と`candidate_3`は旧report互換の内部分類であり、確定・自動返却を意味しない。
- `push-grades` もMVPでは実行対象にしない。
- 学生データを外部へ送信しない。CDN、外部フォント、解析タグを使わない。
- 新しいNode/Reactビルド環境は導入しない。FastAPI配下の静的HTML/CSS/vanilla JSで実装する。
- 既存の `/health`、`/grades/{coursework_id}`、`POST /jobs`、`GET /jobs/{id}` と
  `browser/classroom-grader.user.js` の互換性を維持する。
- 認証情報、設定実値、学生データをコミットしない。
- 主導線の`full`はallowlist型model-controllerでモデルを切り替える。個別`run/refine`は
  手動準備したモデルを確認して実行する診断用互換経路として残す。
- 実データや実Classroomへ書き込むテストは行わない。
- Web UI/APIと永続ジョブデータは251だけで動かす。250へ実行時データを生成しない。

## バックエンド要件

`grader/api.py` を肥大化させず、ジョブ管理を例えば `grader/jobs.py` のサービスとして分離してください。

- `data/jobs/<job_id>.json` にジョブ状態を永続化する。
- 一時ファイルへ書いてからrenameする原子的書き込みにする。
- 起動時に残っている `running` ジョブを `interrupted` にする。
- 同じcourseworkの書き込み系ジョブを同時実行させず、競合時はHTTP 409を返す。
- 単一vLLM前提で、全体の同時採点ジョブ数も既定1にする。
- 状態遷移は少なくとも queued → running → succeeded/failed を持つ。
- created_at、started_at、finished_at、current_step、options、末尾ログ、errorを保持する。
- coursework ID、phase、anchorを検証し、任意コマンドを組み立てられないようにする。
- 既存CLIをsubprocessで呼ぶ実装を維持してもよいが、実行と状態管理を分離し、将来Python関数の
  直接呼び出しへ交換できる構造にする。

追加APIは `/api/v1` 配下にしてください。

- `GET /api/v1/status`
- `GET /api/v1/courseworks`
- `GET /api/v1/courseworks/{coursework_id}/results`
- `GET /api/v1/courseworks/{coursework_id}/report.csv`
- `POST /api/v1/jobs`
- `GET /api/v1/jobs`
- `GET /api/v1/jobs/{job_id}`

POSTには既存の `X-API-Key` 認証を適用してください。GETにも学生情報が含まれるため同じ認証を
適用してください。既存エンドポイントの認証挙動は維持します。

`GET /api/v1/courseworks` は既存 `list_courseworks()` のprint出力に依存せずデータを返せるよう、
必要なら取得処理と表示処理を分離してください。Classroom接続失敗は500の生スタックではなく、
UIで説明できるエラーJSONにしてください。

## フロントエンド要件

FastAPIから `/ui/` にUIを配信し、必要な静的ファイルも同一アプリから提供してください。

- ダッシュボードに状態、課題一覧、最近のジョブを表示する。
- 課題行から詳細を開き、run/refine/reportを実行できる。
- refineではanchorが空なら送信できない。
- forceは危険操作として確認ダイアログを表示する。
- ジョブ詳細を約2秒間隔でポーリングし、終了後に結果を再取得する。
- 結果表はreview、candidate_3を優先し、category/tier/nameで絞り込める。
- 全categoryを「AI採点案」と表示し、教員確認済みだけを下書きバッチ対象にする。
  auto_3の提案理由はflagsから表示できるようにする。
- API tokenは入力可能にしてもよいが、localStorageへ平文保存する場合はその旨を明示する。
  可能ならsessionStorageを既定にする。
- loading、空データ、401、409、接続失敗、ジョブ失敗を日本語で表示する。
- 色だけで状態を表さず、キーボード操作と基本的なアクセシビリティを確保する。
- Trusted Typesを考慮し、学生由来の文字列を`innerHTML`へ入れず`textContent`を使う。

## 採点ロジックの回帰テスト

Web UI追加と同時に、現状不足している以下のテストを追加してください。

- length bonus無効時はcandidate_3のまま。
- 対象外ルーブリックではauto_3にならない。
- judge満点、文字数条件、キーワード条件それぞれのauto_3判定。
- judge優先でauto_0/1/2のcontent_scoreとcategoryが一致する。
- pairwise demoteとlate penaltyを組み合わせても整合する。
- categoryにかかわらず、未確認答案が下書きバッチへ混入しない。
- 人間採点、返却済み、既存draft/assigned gradeが下書き対象に混入しない。

API／ジョブには以下をテストしてください。

- token認証
- 不正phaseと不正ID
- 同一課題の409排他
- ジョブ永続化とAPI再起動相当のinterrupted復旧
- 成功、subprocess失敗、例外時の状態遷移
- CSVダウンロードの404と正常系
- Classroom APIをモックした課題一覧

## 文書と設定

- READMEにWeb UIの起動方法、URL、操作手順、モデル切り替えが手動であることを追記する。
- `docs/webui-design.md` は実装と違う箇所が生じたら更新する。
- Docker Composeの既存 `api` サービスでUIも表示できるようにする。
- UID/GID固定問題はWeb UI実装と独立した小さな変更で安全に直せる場合のみ対応し、無理に混ぜない。
- 依存を追加する場合は理由を説明し、最小限にする。

## 完了条件

次をすべて確認し、結果を報告してください。

1. 250で変更後、指定の除外付きrsyncで251へ同期する。`data/`を送らず、`--delete`を使わない。
2. 251上で `docker compose run --rm test` が成功する。
3. 251上で `docker compose up -d api` 後、250からSSH越しに `/ui/` と `/health` を確認する。
4. モックまたはテスト用設定で、ジョブ作成→状態遷移→結果表示を251上で確認できる。
5. 既存 `/grades/*` とユーザースクリプト向けJSONの互換性が維持される。
6. 検証中に実Classroomへの書き込みや返却を行っていない。実装上も自動返却経路はなく、
   専用MV3拡張は確認済み答案の空欄への下書き入力だけを行う。
7. 250に`data/`やテスト生成物を持ち帰っていない。
8. 最終報告に、250でのdiff概要、251へpushした範囲、251での実行・監視・結果を含める。
   機密情報や大量の学生データは含めない。

作業開始時に実装方針を簡潔に示し、その後はバックエンド状態管理、API、UI、テスト、文書の順に
小さく実装してください。既存の未コミット変更があれば上書きせず、競合する場合は止めて報告してください。

---
