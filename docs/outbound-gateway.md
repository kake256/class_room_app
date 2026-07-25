# 251 outbound gateway（任意構成）

`gateway/` は、251へ受信ポートを開けずに固定HTTPS URLから採点Web UIを使うための小型中継です。クラウドgatewayは要求と応答をメモリ内で一時保持し、251のagentがHTTPS long-pollで取りに行きます。学生データ、要求、応答、Cookie、認証情報をディスクへ保存せず、アプリケーションもそれらをログへ出しません。

利用者にgateway専用tokenは要求しません。ブラウザのGoogle session Cookie、CSRF token、ローカルAPIのRBACを変更せずに中継します。クラウドとagentの間だけ独立したagent secretで認証します。

## 中継範囲と安全制限

gatewayとagentの両方が同じmethod/path allowlistを検査します。対象は次だけです。

- `GET /`, `/ui/`, `/ui/static/<file>`, `/ui/classroom-grader.user.js`
- Google login用のsession/start/logoutと`GET /oauth2callback`
- courses/courseworks/settings/readiness/results/report API
- jobs、teacher reviews、draft-batches、extension API

許可methodはendpointごとの`GET`/`POST`/`PUT`だけです。`/internal/*`、任意path、path traversal、管理用の未登録endpointは中継しません。request headerは`Cookie`、`Content-Type`、`X-CSRF-Token`、`Authorization`だけ、response headerは`Content-Type`、`Cache-Control`、`Set-Cookie`、`Location`だけです。

既定ではrequest body 256 KiB、response 2 MiB、保留要求64件までです。agent未接続/上限到達は503、ローカル応答待ち超過は504、size超過は413/502です。設定可能な上限:

- `CGA_GATEWAY_REQUEST_TIMEOUT_SECONDS`（既定30、最大120）
- `CGA_GATEWAY_LONG_POLL_SECONDS`（既定20、最大30）
- `CGA_GATEWAY_MAX_REQUEST_BYTES`（既定256 KiB、最大1 MiB）
- `CGA_GATEWAY_MAX_RESPONSE_BYTES`（既定2 MiB、最大5 MiB）
- `CGA_GATEWAY_MAX_PENDING`（既定64、最大256）

## secret

client用secretはありません。agentには十分長いランダム値を使い、クラウドには平文ではなくSHA-256 digestだけを設定します。比較はconstant-timeです。

```bash
python -c 'import hashlib,getpass; print(hashlib.sha256(getpass.getpass().encode()).hexdigest())'
```

クラウドgatewayの必須環境変数は`CGA_GATEWAY_AGENT_TOKEN_SHA256`です。`CGA_GATEWAY_REQUIRE_TLS=true`は既定で、平文transportを拒否します。

## provider URLと起動

リポジトリルートで`docker build -f gateway/Dockerfile .`としてimageを作成します。gatewayはTLS終端後に`PORT`で待ち受けます。Cloud Run URLは通常`https://SERVICE-PROJECT.REGION.run.app`、Renderは`https://SERVICE.onrender.com`、Fly.ioは`https://APP.fly.dev`形式です。必ずprovider管理画面に表示された実際のHTTPS URLを使用してください。

ブローカーは永続化しないため、MVPは単一instance（Cloud Runなら最大instance数1）にします。再起動時の処理中要求は失敗し、ブラウザが再試行します。Docker起動は`--no-access-log`です。provider側のrequest logも無効化するか保存期間を最小化してください。実データを扱う前にproviderのIAM、ingress、ログ、リージョン、組織のデータ取扱規程も確認します。

251では平文agent tokenを権限制限した環境変数/secret managerから渡し、agentを常駐化します。値をコマンド履歴へ直接書かないでください。

```bash
export CGA_GATEWAY_URL='https://SERVICE-PROJECT.REGION.run.app'
export CGA_GATEWAY_AGENT_TOKEN='agentの平文token'
export CGA_LOCAL_API_URL='http://127.0.0.1:8800'
python -m gateway.agent
```

Google Cloud OAuth clientの承認済みredirect URIと`classroom.oauth_redirect_uri`には、gatewayの固定URLを使った`https://SERVICE-PROJECT.REGION.run.app/oauth2callback`を完全一致で設定します。session cookieをgateway originへ設定するため、ローカルURLをredirect URIに残したまま運用しないでください。`web_auth.secure_cookie: true`も有効にします。

gateway-agent間はTLS必須で、agentはHTTP URLを拒否します。gatewayのTLS検査はproviderの信頼済みproxyが設定する`X-Forwarded-Proto`を前提とするため、コンテナをproxyなしで直接公開しません。

## テスト

実データや実deployなしで実行できます。

```bash
pytest -q gateway/tests
```
