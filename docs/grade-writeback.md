# 成績の書き戻し(採点API + 成績簿入力)

採点結果(下書き点)を Google Classroom に入力するためのサーバー構成と手順。
概要は [README](../README.md) を参照。

Classroom API は「課題を作成したプロジェクト」以外からの成績書き込みを拒否する
(教師がUIで作成した課題は `ProjectPermissionDenied`)。そのため書き戻しは **2通り**。

- **方式A**: 採点API + ブラウザのユーザースクリプト … UI作成課題でも可(推奨)
- **方式B**: `push-grades` … このツール/API経由で作成した課題のみ

---

## 方式A: 採点API + ブラウザのユーザースクリプト

ログイン済みの自分のブラウザ経由で入力するため、API制約も認証も回避できる。

```
[GPUマシン] 採点API  ──JSON(GET /grades)──►  [自分のブラウザ]
   report結果を配信                       成績簿タブのユーザースクリプトが
                                          点数を取得して下書き点を入力
```

### 1) 採点API を起動

```bash
./run.sh api-up                          # localhost:8800 で起動(= docker compose up -d api)
./run.sh api-down                        # 停止
```

エンドポイント:

| メソッド / パス | 内容 |
|---|---|
| `GET /health` | 稼働確認 |
| `GET /grades/{courseWorkId}` | report結果をJSONで返す(要 report 実行済み) |
| `POST /jobs {coursework_id, phase}` | 採点を非同期起動(phase: run/refine/report/full) |
| `GET /jobs/{id}` | ジョブ進捗 |

`config.yaml` の `api.token` を設定すると全エンドポイントで `X-API-Key` ヘッダ必須になる
(ユーザースクリプトのパネルの token 欄に同じ値を入れる)。CORSは
`classroom.google.com` からのアクセスのみ許可。

### 2) ブラウザからAPIへ届く経路を用意(いずれか)

| 経路 | 手順 | ユーザースクリプトのAPI欄 | 手元PCへの導入 |
|---|---|---|---|
| 同一LAN / SSH転送 | `ssh -L 8800:localhost:8800 …`(VSCodeのポート転送でも可) | `http://localhost:8800` | 不要 |
| 外部NW(Cloudflare Tunnel) | 下記(推奨) | `https://<ランダム>.trycloudflare.com` | **不要** |
| 外部NW(Tailscale) | 後述 | `https://classroom-grader.<tailnet>.ts.net` | 要Tailscaleクライアント |

> **なぜHTTPSが要るか**: Classroom成績簿は https ページ。そこから `http://<IP>:8800` を
> 直接叩くとブラウザに mixed-content でブロックされる。`http://localhost` だけは例外的に
> 許可されるためSSH転送はhttpでよいが、Tailscale等のIP/ホスト名経由ではHTTPS化が必要。

#### SSH転送(同一LAN / VPN内)

- **VSCode Remote-SSH**: 下部「ポート」タブで `8800` を転送に追加(OAuthの8765と同じ操作)
- **素のSSH**: 手元PCで `ssh -L 8800:localhost:8800 <user>@<host>`
- 確認: 手元ブラウザで `http://localhost:8800/health` が `{"status":"ok"}` を返せばOK

#### Cloudflare Tunnel(手元PCに何も入れない・推奨)

公開HTTPS URLを発行する方式。手元PCにクライアント導入が不要で、ブラウザでURLを開くだけ。
既定は **Quick Tunnel**(Cloudflareアカウント・ドメイン不要)。

**公開URLは誰でも到達しうるため、必ずAPIトークンで保護する**:

```bash
# 1) config.yaml の api.token に十分長いランダム文字列を設定
openssl rand -hex 24                      # 生成例。出力を config.yaml の api.token: に貼る

# 2) api + cloudflared を起動
docker compose -f docker-compose.yml -f docker-compose.cloudflare.yml up -d api cloudflared

# 3) 発行された公開URLを確認
docker compose -f docker-compose.yml -f docker-compose.cloudflare.yml logs cloudflared | grep trycloudflare
#   → https://<ランダム語>.trycloudflare.com
```

ユーザースクリプトのパネルで **API=その公開URL**、**token=`api.token`と同じ値** を入力する。

- HTTPSなので mixed-content にならない。CORSは `classroom.google.com` のみ許可
- `X-API-Key` トークンで保護(トークン無しのアクセスは401)
- **Quick TunnelのURLは起動ごとに変わる**。安定URL + Googleログイン認証にしたい場合は
  Cloudflareにドメインを追加し、Zero Trustで名前付きトンネル+Accessを構成する
  (`docker-compose.cloudflare.yml` のコメント参照)。

#### Tailscale(要クライアント導入)

VPNを張れない外部ネットワークから使う場合。WireGuardベースの私設ネットワークで、
`tailscale serve` によりAPIを **Tailnet内のみ・HTTPSで** 公開する。

準備(初回のみ):

1. Tailscale管理コンソールで **MagicDNS** と **HTTPS証明書** を有効化
2. 認証キーを発行(Settings → Keys、**reusable** 推奨)
3. `.env` に記入:

   ```bash
   cp .env.example .env
   # TS_AUTHKEY=tskey-auth-xxxxxxxxxxxx
   ```

起動:

```bash
docker compose -f docker-compose.yml -f docker-compose.tailscale.yml up -d tailscale api-ts
# 公開URLの確認
docker compose -f docker-compose.yml -f docker-compose.tailscale.yml \
  exec tailscale tailscale serve status
# 疎通確認(netns共有先のAPIに届くか)
docker compose -f docker-compose.yml -f docker-compose.tailscale.yml \
  exec tailscale wget -qO- http://127.0.0.1:8800/health
```

→ ユーザースクリプトのAPI欄に `https://classroom-grader.<tailnet>.ts.net` を入力。

構成と注意点:

- **tailscale** コンテナ: userspaceモード(NET_ADMIN/TUN等の特権不要)。ノード鍵は
  `data/tailscale` ボリュームに永続化(再起動しても再認証不要)
- **api-ts** コンテナ: tailscale の netns を共有し、`tailscale serve` が `127.0.0.1:8800`
  をHTTPS公開。**結果配信(GET /grades)専用**
- netns共有のため api-ts はホストの vLLM に届かない → `POST /jobs` の採点起動は不可。
  **採点は通常どおり `./run.sh serve/run/refine/report` で実施**し、api-ts は配信のみ
- netns共有のため、**tailscale コンテナを再起動したら api-ts も再起動**すること:
  `docker compose -f … -f docker-compose.tailscale.yml restart api-ts`
  (正規の TS_AUTHKEY + 状態ボリュームなら tailscale は認証後に安定し再起動不要)
- Tailnet内=鍵認証のデバイスからのみ到達。学生データも WireGuard で暗号化されて流れる

### 3) ユーザースクリプトを登録して実行

1. Tampermonkey 等に `browser/classroom-grader.user.js` を登録
2. 対象課題の成績ページ(生徒×課題の一覧)を開く
3. 右下パネルで **API接続先**(上表のURL)・**token**(任意)・**courseWorkId** を入力
   (API接続先・token はブラウザの localStorage に保存される)
4. **「プレビュー」** … 入力予定を青・既存点をグレーで色付け(**入力しない**)
5. 内容を確認して **「入力実行」** … 下書き点(draftGrade)のみ入力

安全設計:

- 下書き点(draftGrade)のみ入力。**「返却」ボタンには一切触れない**
- 既に点数があるセルはスキップ(上書きしない)
- 入力後に値を読み戻して検証。ズレたら中断
- `auto_*` と `candidate_3` が対象。`review` は人間判断待ちのため入力しない

> **DOM調整について**: Classroom成績簿のHTML構造は変わりやすい。動かない場合は
> スクリプト冒頭の `SELECTORS`(生徒行 `studentRow` / 氏名 `studentName` /
> 点数入力欄 `gradeInput`)を、開発者ツールで見た実際のDOMに合わせて調整する。
> 氏名照合は `normName`(学籍番号プレフィックスと空白を除去して部分一致)で行う。

---

## 方式B: push-grades(API作成課題のみ)

課題をこのツール/API経由で作成した場合は、サーバーを介さず直接書き込める:

```bash
./run.sh push-grades-dry <courseWorkId>              # 対象確認(dry-run、書き込まない)
./run.sh push-grades <courseWorkId>                  # auto_0/1/2 の下書き点を書き込み
docker compose run --rm grader push-grades \
  --coursework <cw> --include-candidates             # 3点候補にも基準値を入れる
```

- 下書き点(draftGrade)のみ・既に点数がある提出はスキップ・返却はしない
- 100点満点課題は 0→70 / 1→75 / 2→80 / 3→85 に自動変換(`grader/push.py` の `SCORE_MAP_100`)
- UI作成課題では `ProjectPermissionDenied` になる(その場合は方式A)
