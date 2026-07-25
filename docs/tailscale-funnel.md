# Tailscale FunnelでWeb UIを公開する

`docker-compose.funnel.yml`は、Linuxホストの`127.0.0.1:8800`で動く既存の`api`を
Tailscale Funnelへ公開する最小overrideである。`tailscale-funnel`コンテナはhost networkを使うため、
APIは従来どおりホスト経由でvLLMへ到達できる。

既存の`docker-compose.tailscale.yml`は`api-ts`がTailscaleのnetwork namespaceを共有するlegacy構成で、
ホストのvLLMへ到達できない。新規運用では併用しない。

## 事前設定

1. Tailscale管理画面で、対象tailnetのHTTPSとFunnel利用を許可する。
2. Funnelを許可したタグ付きauth keyを`.env`の`TS_AUTHKEY`へ設定する。値をGit、コマンド履歴、
   チャット、ログへ記録しない。
3. `config.yaml`で`web_auth.secure_cookie: true`、`web_auth.require_explicit_roles: true`を設定し、
   `roles`へ利用者を明示登録する。十分長い`CGA_SESSION_SECRET`も`.env`等から設定する。

FunnelはWeb UIをインターネットへ公開する。`allowed_emails`/`allowed_domains`だけに依存せず、
明示roleとGoogleログインの両方でアクセスを制限する。

## 起動とFunnel有効化

```bash
docker compose -f docker-compose.yml -f docker-compose.funnel.yml up -d api tailscale-funnel
docker compose -f docker-compose.yml -f docker-compose.funnel.yml \
  exec tailscale-funnel tailscale funnel --bg 8800
```

公開URLは通常`https://classroom-grader.<tailnet>.ts.net`となる。Google Cloud ConsoleのWeb OAuth
clientへ、次のredirect URIを完全一致で登録し、`classroom.oauth_redirect_uri`にも同じ値を設定する。

```text
https://classroom-grader.<tailnet>.ts.net/oauth2callback
```

OAuth clientはWeb application型を使う。公開URLやredirect URIが異なる状態でログインを試さない。

## 状態確認

```bash
docker compose -f docker-compose.yml -f docker-compose.funnel.yml \
  exec tailscale-funnel tailscale status
docker compose -f docker-compose.yml -f docker-compose.funnel.yml \
  exec tailscale-funnel tailscale funnel status
```

状態確認の出力を共有するときは、tailnet名、端末名、IP、利用者情報を必要に応じて伏せる。

## Funnel設定の解除

```bash
docker compose -f docker-compose.yml -f docker-compose.funnel.yml \
  exec tailscale-funnel tailscale funnel reset
```

`reset`はFunnel/Serve設定を解除する。`data/tailscale-funnel`にはノード状態が残るため、削除や再認証は
別の管理操作として扱う。通常の停止は次で行う。

```bash
docker compose -f docker-compose.yml -f docker-compose.funnel.yml stop tailscale-funnel
```

実運用前に少人数・非本番課題でGoogleログイン、role拒否、OAuth callback、採点ジョブ、拡張の接続を
確認する。Funnelは通信経路を公開するだけであり、Classroomへの自動返却を許可しない。
