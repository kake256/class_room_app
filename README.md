# Google Classroom 提出レポート LLM採点システム

Google Classroom の提出レポート(PDF / Googleドキュメント / Word)を、ローカルGPU上の
VLM(vLLM + Qwen-VL)でマルチモーダル採点する半自動システム。
全答案についてAIが採点案を作り、教員がWeb UIで確認・修正してからClassroomへ下書き入力する。**採点処理はすべてDockerで
実行し、ホスト環境を汚さない**(GPUを使うのは vLLM コンテナのみ)。

> **現在の標準運用:** **Qwen2.5-VL単独の1段階採点＋全答案の人間確認**。
> report内部の旧category名にかかわらず、AI出力はすべて「採点案」である。
> Web UIで教員が全答案を確認・修正した後、確認済みだけの短期バッチをMV3拡張でClassroomの
> 空欄へ下書き入力する。自動確定・自動返却は行わず、最終確定と返却は教員がClassroomで行う。
> Qwen3-VLによる審判フェーズ(`refine`)は**診断・比較用**として単独実行のみ残し、一括採点
> (`full`)には含めない。詳細は [docs/teacher-review-workflow.md](docs/teacher-review-workflow.md) を参照。

## 何をするか

- 提出物を取得 → PDF化 → ページ画像化 → VLMで2回採点 → 集計
- 遅延・形式違反・未提出を自動仕分け、根拠(evidence)付きで出力
- 課題種別ごとの採点基準を3種のテンプレートから一括適用し、課題ごとに個別調整できる
- 確定点だけを使ったコースランキングをWeb UIとGoogle Sheetsへ出力
- Codex / Claude CodeからMCPで課題・お知らせの下書き作成、採点、ランキング参照ができる
- 成績の下書き入力は「採点API + 専用Chrome/Edge MV3拡張」でログイン済みブラウザから行う

## アーキテクチャ

```
 提出物 ─ fetch ─ 採点 (Qwen2.5-VL-7B、独立2回) ─ report(集計CSV/サマリ)
                                                       │
                        Web UIで教員が全答案を確認・修正 ┤
                                                       └─ 採点API → ブラウザで成績簿に下書き点入力

 [診断用・標準運用外] refine: 審判 (Qwen3-VL-8B) ── judge再採点 + ペアワイズ比較
```

- 標準は**Qwen2.5単独の1段階採点**で、全答案を教員が確認する。一括採点`full`は
  `run` → `report` だけを実行する
- 審判フェーズ`refine`はモデル比較・診断のために残してあり、`full`からは呼ばれない。
  ベンチマークではQwen3の精度優位が図表を含む答案に集中する一方、復号が遅く
  常用に見合わないと判断した
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
| `classroom.course_id` | CLI単独利用時の既定コースID（Web UIは担当コースから選択） |
| `assignments` | courseWorkId → 課題キー の対応(下記) |
| `vllm.model` | 一次採点モデル(既定 Qwen2.5-VL-7B) |
| `pairwise.model` | 審判モデル(既定 Qwen3-VL-8B-FP8) |
| `pairwise.anchor_student_id` | ペアワイズ比較の基準答案(2点相当)。**課題ごとに選び直す** |
| `pairwise.crit_prune` | 審判の観点合計がこの値未満の候補を降格(既定2.0) |
| `length_bonus` | 感想文系課題(KANSOU/EFFORT)の3点候補を自動確認する仕組み(下記) |
| `late_penalty` | 遅延減点(既定1) |
| `web_auth.allowed_emails / allowed_domains` | Web UIへのGoogleログイン許可リスト |
| `web_auth.session_ttl_seconds` | Webログインの有効期間（既定12時間） |
| `integration.token` | legacyユーザースクリプト互換用。現行MV3拡張は短期バッチのペアリングコードを使う |

### 2. Classroom API 認証（Web UIから接続）

管理者が一度だけGoogle Cloud ConsoleでClassroom APIとDrive APIを有効化し、OAuthクライアントを
作成して、ダウンロードしたJSONをプロジェクト直下の`credentials.json`へ配置する。その後の
利用者操作はWeb UIで完結する。

1. `docker compose up -d api`でWeb UIを起動する
2. `http://localhost:8800/ui/`を開く（遠隔サーバーの場合は8800番をSSH転送する）
3. 「Googleでログイン」を押す
4. Googleの同意画面で許可する。完了すると担当コース一覧が出る
5. 担当コースを選び、そのコースの課題だけを表示・採点する

認証URLのコピーや8765番ポートの転送は不要。Web UIのトークンは
`data/oauth_tokens/`にGoogle identity `sub`のSHA-256ハッシュ名で利用者ごとに分離し、
`0600`で原子的に保存する。メールアドレスやraw `sub`はファイル名・ジョブ・APIに保存しない。
`classroom.token_file` (`token.json`)はCLI単独利用の後方互換として残り、Webログインでは上書きしない。

既存のデスクトップアプリ（`installed`）型OAuthクライアントは、既定のloopback URL
`http://localhost:8800/oauth2callback`を利用できる。Webアプリ（`web`）型クライアントを使う場合は、
Google Cloud Consoleの「承認済みのリダイレクトURI」にこのURL（または
`classroom.oauth_redirect_uri`の設定値）を**完全一致**で登録する。`redirect_uri_mismatch`が出た場合は、
スキーム、ホスト、ポート、パス、末尾スラッシュまで一致しているか確認する。

スコープ: `openid` / `userinfo.email`（Web UIログインの本人確認） /
`classroom.courses.readonly`（教師の担当コース一覧） /
`classroom.coursework.students`(下書き点書き込みに必要) /
`rosters.readonly` / `drive.readonly` /
`spreadsheets`(ランキングのGoogle Sheets出力) /
`classroom.announcements`(MCPからのお知らせ下書き作成・公開)

後半2つはWeb OAuth（`grader/google_auth.py` の `OAUTH_SCOPES`）だけに追加し、CLI用の
`SCOPES`と共有`token.json`には追加しない。既存トークンにはこれらが無いため、UIに案内が
出た場合は「Google権限を再接続」から一度だけ再同意する。Google Cloud Console側でも
Sheets APIの有効化と、同意画面へのスコープ登録が必要である。

担当コースはClassroom API `courses.list` を `teacherId="me"` および
`courseStates=["ACTIVE"]` でページング取得する。サーバーは課題一覧取得と採点ジョブ開始のたびに
選択コースがこの一覧内にあることを再確認し、ブラウザの`localStorage`だけを信頼しない。

GoogleのID tokenは公式ライブラリでクライアントID・発行元・有効期限を検証し、確認済みメールだけを
受け付ける。`web_auth.allowed_emails`または`allowed_domains`を設定すると一致する利用者だけがログイン
できる。両方空の場合はOAuthクライアント側で許可されたGoogleユーザーを許可するため、共有環境では
allowlistを設定する。Webセッションは署名済みHttpOnly Cookieで保持し、変更操作にはCSRF tokenを使う。
本番では`CGA_SESSION_SECRET`を設定し、HTTPS公開時は`secure_cookie: true`にする。

既存の共有`token.json`だけを使っていた利用者は、利用者別トークンへの移行と
`classroom.courses.readonly`スコープ追加のため、Web UIで一度再ログインする必要がある。

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
| `sukina_kansou` | 好きな手法とその理由(課題文は`sukina`と同じ) | KANSOU(感想文想定で採点したい場合) |

新しい種類の課題は `ASSIGNMENT_SPECS` に課題文とルーブリック種別を1エントリ追加する。

---

## 採点の実行(フルフロー)

標準運用はQwen2.5単独の1段階採点である(DockerでGPUを使うのはvLLMコンテナのみ):

```bash
./run.sh serve q25-7b                   # 採点モデルのvLLMを起動
./run.sh run <courseWorkId>             # fetch → render → 2回採点
#   感想文寄りの課題を甘めに採点したいとき:
docker compose run --rm grader run --coursework <courseWorkId> --lenient
./run.sh report <courseWorkId>          # サマリ表示 + data/report/<cw>.csv
```

この後はWeb UIで全答案を確認・修正する。Web UIの「AI採点案を作成」(`full`)は
`run` → `report` を順に実行する。

<details>
<summary>診断用: 審判フェーズ(refine)を単独で実行する</summary>

標準運用では使わない。モデル比較や採点傾向の調査のときだけ実行する。

```bash
./run.sh serve q3-8b                    # 審判モデルに切り替え
./run.sh refine <courseWorkId>          # judge再採点 + ペアワイズ比較
./run.sh refine <courseWorkId> <anchor_student_id>   # 基準答案を指定する場合
./run.sh serve q25-7b                   # 終了後は標準モデルへ戻す
```
</details>

`report` の出力 CSV / サマリ:

- `category`: `auto_0`/`auto_1`/`auto_2`/`auto_3`/`candidate_3`は旧形式との読取互換用の
  内部分類であり、Web UIではすべて教員確認が必要なAI採点案として扱う。ほかに`review`(要確認)、
  `not_submitted`(未提出)がある
- `tier`: candidate_3 内の格付け。`strong`(最有力、両順ペアワイズ勝ち)→ `borderline` の順で確認
- `judge_score` / `evidence` / `flags` も出力

#### 3点候補の自動確認(`length_bonus`、感想文系課題のみ)

一次採点・審判とも3点で一致した候補(`candidate_3`)のうち、確信度が十分高いものは
`auto_3` として自動確認済み扱いにし、TAの目視確認対象から外す:

- 審判の観点合計(`crit_min_sum`、2回の低い方)が満点(`max_crit_sum`、既定3.0)に達していれば無条件で確認済み
- 満点未満・僅差(`min_crit_sum`、既定2.5)でも、本文が長い(`min_chars`文字以上)、または
  講義の技術用語(`grader/rubric.py` の `COURSE_KEYWORDS`)に複数言及している(`min_keyword_hits`語以上)なら
  「具体性」の裏付けとして確認済みにする

`config.yaml` の `length_bonus.rubric_keys`(既定 `["KANSOU", "EFFORT"]`)で対象課題種別を限定する。
**実験課題(EXPERIMENT)には現状適用しない**: 演習レポートは内容の質に関わらず全員が長文・
専門用語だらけになりやすく、同じ閾値では判別力を持たないため(実測確認済み)。
`auto_3` は内部集計上の「高信頼な3点案」を表すだけで、成績の自動確定・自動返却対象にはしない。
確定理由は `judge_full_marks_confirmed` / `length_bonus_confirmed` /
`keyword_bonus_confirmed` の flags で区別する。

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

現行MVPでは、AI採点案をWeb UIで教員が確認・修正し、確認済み答案だけの短期バッチを作る。
専用Chrome/Edge MV3拡張`extension/`がClassroom提出物ページの**空欄だけ**へ下書き点を入力する。
最終確定・返却はClassroom上で教員が行い、システムは自動返却、削除、既存点上書きを行わない。

1. 「AI採点案を作成」を押す。
2. 全答案の提案点を確認・修正し、確認済みにする。
3. 下書き対象をプレビューし、明示的に短期バッチを作る。
4. Classroom提出物ページで拡張へペアリングコードを渡し、空欄へ入力する。
5. Classroom上で教員が確認し、最終確定・返却する。

Tampermonkey版`browser/classroom-grader.user.js`と`push-grades`はlegacy互換用で、現行運用では
非推奨。詳しい安全条件と拡張の導入は
**[docs/grade-writeback.md](docs/grade-writeback.md)**、固定HTTPS URLから利用する場合のgatewayと
251 outbound agentは**[docs/outbound-gateway.md](docs/outbound-gateway.md)**を参照する。

## Web UI（採点の実行・監視）

APIコンテナは採点Web UIも配信する。実行ホスト251上で起動し、250または手元PCから
SSHポート転送でアクセスする。

```bash
# 251上で実行（250から ssh kake-251 経由で発行）
docker compose up -d api

# 手元側で必要に応じて転送
ssh -L 8800:localhost:8800 kake-251
```

ブラウザで `http://localhost:8800/ui/` を開く。Web UIでは以下を行える。

固定HTTPS URLが必要な場合は、Linuxホスト上の既存APIをそのまま公開できる
`docker-compose.funnel.yml`を使用する。起動、`tailscale funnel --bg 8800`、状態確認、解除、
Google OAuthと公開時の安全設定は
**[docs/tailscale-funnel.md](docs/tailscale-funnel.md)**を参照する。既存の
`docker-compose.tailscale.yml`はvLLMへ到達できないlegacy構成で、新規運用では使用しない。

画面は**採点 / MCP / ジョブ / 拡張機能**のタブに分かれている。課題の取得と採点基準の
一括適用は「採点」タブ最上部に集約され、課題一覧は既定で折りたたまれている。
実行条件を満たさない操作（採点基準未設定での採点開始、AI採点案が無い状態での結果確認など）は
ボタンが無効化される。

- GoogleログインとClassroom OAuth接続（一度の同意操作）
- 教師として参加中のACTIVEコース一覧と、選択コースの課題一覧
- ルーブリック未登録課題の警告（自動推定せず採点開始を無効化）
- 採点基準テンプレート3種（感想／調査系／演習系）の一括適用と、課題ごとの個別調整
- 課題ごとの教師備考、0/1/2/3点条件、Classroom実点mapping、遅延減点の設定
- コース内で採点基準テンプレートを保存・適用・名前変更・削除し、課題満点へ非線形mappingを換算
- 集計前の提出・人間採点・システム採点・未処理件数の確認
- `run`（採点）、`report`（集計）、`full`（`run`→`report`）の起動と姿勢の切り替え
- 「ジョブ」タブでの待ち行列・永続ジョブ状態・ログの確認と、待機中ジョブの取り消し
- category別集計、要確認答案、根拠・flagsの確認とCSV取得
- コースランキング（**ランキング / 各回の最高点者 / 各回の0点＆未提出者 / 各回の詳細**の4タブ）
- 答案ダイアログでの内容確認（最高点者・0点者の答案をページ送りで確認）
- 明示確認後のGoogle Sheets出力（URL/ID、シート名、出力範囲を画面で指定）

### コースランキングの算出

**Classroomで確定した点（`assignedGrade`）とWeb UIで教員が確認した点だけ**を集計する。
AI採点案は含めない。ランキング更新時はClassroomから確定点を取得してから集計する。

- 順位は課題ごとの得点率の平均（`得点 / 満点`）で決まり、同点は同順位（competition ranking）
- **未提出は −1/3 のペナルティ**（`grader/ranking.py` `MISSING_PENALTY_RATE`）。
  提出したうえでの0点（0.0）より不利になる
- 未確定の課題は分母から除外する（まだ採点していない回で不利にならない）
- 提出数・最高点回数・未提出数を各行に表示する
- 集計結果は既定180秒キャッシュする（`ranking.cache_ttl_seconds`）。
  「更新」ボタンでキャッシュを無視して再取得できる

Google Sheets出力ではWeb OAuthだけにSheets書込scopeを追加する。従来のWebログイントークンには
このscopeがないため、UIに案内が表示された場合は「Google権限を再接続」から一度だけ再同意する。
CLI用のOAuth scopeと共有`token.json`は変更しない。Sheets APIはGoogle Cloud Consoleで別途有効化し、
出力先スプレッドシートをログイン中のGoogleアカウントへ共有しておく。
お知らせ投稿用の`classroom.announcements`も同じ再接続で同意する。

採点基準ダイアログでは、現在のフォーム内容をコース専用テンプレートとして保存できる。
テンプレート適用はフォームへ反映するだけで、課題設定を自動保存しない。適用直後は必ず未確認へ戻るため、
0〜3点条件と換算点を確認し、「採点に使用する」をチェックして保存するまで採点を開始できない。
専用MV3拡張はWeb UIの「専用Chrome/Edge拡張をダウンロード」からZIPで取得できる。
ZIPには固定allowlistの拡張ファイルだけが入り、設定値、token、学生データ、テストは含まれない。

一括採点では、APIと分離したallowlist型`model-controller`が採点モデル(`q25-7b` / `q3-8b` /
`minicpm-v45`のallowlist)を管理する。標準運用ではQwen2.5のまま切り替えない。
Web UIで全答案を確認・修正して短期バッチを作った後、専用MV3拡張がClassroomの空欄へ
下書き入力する。最終確定・返却はClassroom上で教員が行う。

## MCP（Codex / Claude Code連携）

APIコンテナは`/mcp`にstateless Streamable HTTPのMCP endpointを配信する。Web UIと同じ
Google利用者・教師コース認可を使い、**34 tool**を提供する。詳細と接続手順は
**[docs/mcp.md](docs/mcp.md)** を参照する。

できること:

- コース・課題一覧、準備状況、採点結果、ランキング、各回の最高点者の参照
- 採点基準の設定（`confirm=false`は検証preview、`confirm=true`だけが保存）
- 答案の準備・取得と採点案の一括保存
- **課題の下書き作成・公開**（`preview` → `create ..._draft` → `publish`）
- **お知らせの下書き作成・公開**（同じ3段階。本文のみ、全学生向け）
- ランキングのGoogle Sheets出力

Classroomへ直接書き込むのは、明示確認済みの課題・お知らせの下書き作成・公開と、
同じMCP利用者が本システムで作成した課題の空欄`draftGrade`入力だけである。
**成績確定・返却・提出取消は提供しない。** 書き込み系toolは`confirm=true`が無い限り拒否し、
作成系は`idempotency_key`で重複作成を防ぐ。

お知らせは課題と同じ手順だが、Announcements APIには`associatedWithDeveloper`が無いため、
**「同じMCP利用者が本システムから作成した」作成履歴だけを所有の根拠**とし、記録の無い
お知らせは本文が完全一致しても公開しない。添付・リンク素材と個別配信、予約投稿、
投稿後の編集・削除は提供しない。

---

採点モードは採点姿勢（厳密／甘め）を切り替える。EXPERIMENT/KANSOU/EFFORT等の
ルーブリック種別は`assignments`設定から課題ごとに自動選択され、採点モードとは別概念である。

ジョブ状態は251上の `data/jobs/` に保存される。API再起動時に実行中だったジョブは
`interrupted` と表示される。同時実行は単一vLLMを保護するため1件に制限される。
実行中に新しく作成されたジョブは永続FIFOで待機する。安全なプロセス停止と部分成果物の扱いが
未定義のため、現在取り消せるのは`queued`だけである。

Webジョブの新規データは
`data/courses/<course_id>/courseworks/<courseWorkId>/`下の`settings/meta/raw/pdf/pages/results/report.csv`に分離する。
従来パスは移動・削除せず、`config.yaml`の既定コースに限って読み取りfallbackする。
人間の`draftGrade`/`assignedGrade`は0点も含めて保護し、拡張による下書き入力対象から除外する。

`run`/`refine`開始直前にGPU上のモデルIDと応答を再確認する。一括採点`full`では、APIと
分離したallowlist型`model-controller`へ切替要求を送り、採点(`run`)と集計(`report`)を順に実行する。
APIコンテナへDocker socketは渡さない。

コンテナは既定でUID/GID `1000:1000`として動く。実行ホストが異なる場合は `.env` に
`CGA_UID=<id -uの値>` と `CGA_GID=<id -gの値>` を設定し、`data/`へ書き込めるユーザーに合わせる。

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
- 観点合計(0〜3)は**四捨五入**(0.5は必ず切り上げ)で整数化する(`grader/grade.py`
  `clamp_total`)。Python組み込みの `round()` は銀行丸め(`round(2.5)==2`)になり
  ルーブリックの指示と食い違うため使わない
- 0/1/2/3点のすべてをAI採点案として提示し、教員確認後だけ下書き対象にする。`auto_*`や
  `candidate_3`は採点アルゴリズム内部と旧report互換の分類であり、成績確定を意味しない
- レビュー行きの判定: ゲート不通過・2回不一致・形式違反・切り捨て・エラーは常にレビュー行き。
  審判フェーズ未実施の答案は一次採点自身が書いた flags(判断に迷った旨の自由記述)も
  レビュー行きの根拠にするが、審判フェーズ実施済みなら judge との突き合わせ(下記)に委ねる
  (自由記述flagsだけで機械的にレビュー行きにしない)
- 審判フェーズ(`refine`、診断用): judge が 0/1/2 を高精度化(judge観点合計を優先)、ペアワイズで候補を降格
  (両順負け/tieのみ)、judge観点合計が閾値未満の候補も降格。人間3点の見逃しゼロを維持。
  ペアワイズ比較の評価軸は課題種別(EXPERIMENT/KANSOU/EFFORT)ごとに切り替える
  (`grader/pairwise.py` `PAIRWISE_PROMPTS`)。実験課題向けの軸(定量評価・考察の深さ)を
  感想文課題にそのまま使うと比較が噛み合わず誤判定するため
- `late` は report 側で −1(3点満点は減点保留 `late_waiver_candidate`)
- 再提出は Drive の modifiedTime / PDFハッシュで検知して自動再採点
- ローカル運用のため匿名化しない。名簿APIから実名を取得しCSV・サマリに表示する

---

## コマンド一覧(run.sh)

```
./run.sh test                             ユニットテスト
./run.sh list                             課題一覧(courseWorkId確認)
./run.sh serve {q25-7b|q3-8b|minicpm-v45|stop|status}  vLLMサーバのモデル切り替え
./run.sh run <cw> [--lenient]             一次採点(fetch→render→grade)
./run.sh refine <cw> [anchorId]           審判フェーズ(診断用。標準運用では使わない)
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
grader/            採点ロジック(fetch/render/grade/report/api/mcp_server/ranking/settings_presets …)
grader/web/        Web UI(index.html / app.js / style.css)
extension/         現行のClassroom下書き入力用Chrome/Edge MV3拡張
browser/           legacy互換用ユーザースクリプト（現行運用では非推奨）
gateway/           固定HTTPS URL用gatewayと251 outbound agent
scripts/           vllm-server.sh、model-controller.py
tests/             ユニットテスト
config.example.yaml 設定テンプレート(実値の config.yaml は gitignore)
docker-compose.tailscale.yml  api-ts/netns方式のlegacyオーバーレイ（新規運用では非推奨）
docker-compose.funnel.yml     既存APIをTailscale Funnelで公開するLinux用override
data/
  raw/ pdf/ pages/ results/ meta/ report/ calibration/   各段階の中間成果物(冪等・部分再実行可)
```

`data/`・`config.yaml`・`credentials.json`・`token.json`・`.env`・学生氏名を含む
CSVやドキュメントは **gitignore 済み**(個人情報をリポジトリに含めない)。
