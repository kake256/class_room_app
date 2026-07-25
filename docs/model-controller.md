# モデル切替コントローラー

Web UIの「一括採点」は`run → refine → report`を順番に実行し、各採点工程の前に
`model-controller`へモデル切替を依頼する。APIコンテナにはDocker socketを渡さず、socketは
専用controllerコンテナだけへmountする。

controllerは固定プロファイル`q25-7b`と`q3-8b`だけを受け付け、Bearer secretがない要求、
任意コマンド、任意引数を拒否する。同時切替は409で拒否し、必要モデルがすでにhealthyなら再起動しない。

## 設定

秘密値はリポジトリ直下の`.env.model-controller`一箇所だけに保存する。APIとcontrollerの両serviceが
同じ`env_file`を読む。composeの`environment`へ同名変数を記述すると空のshell展開値で上書きし得るため、
`CGA_MODEL_CONTROLLER_SECRET`を`environment`へ重ねない。

```dotenv
CGA_MODEL_CONTROLLER_SECRET=<十分長いランダム値>
```

このファイルはGitへ追加せず、所有者だけが読める`0600`にする。secretをコマンド引数、compose本文、
ログ、チャットへ記録しない。ファイル未配置またはsecret未設定ではcontrollerが`/switch`を401で拒否し、
一括採点は安全に停止する。

vLLMコンテナへmountするHugging Face cacheはDocker daemonが動く251ホスト上の絶対パスである。
通常の`.env`または起動shellで、実際のパスを明示する。

```dotenv
CGA_HOST_HF_HOME=/absolute/host/path/to/huggingface
```

この値はsecretではないが、空値や相対パスではモデル切替を拒否する。controller内の`HOME`をcache sourceに
使うとDocker daemon側の別パスとして解釈されるため使用しない。

## コンテナ構成

`Dockerfile.model-controller`はDocker CLI、bash、curl、Pythonとpin済みのFastAPI/uvicornだけを含む。
composeでは次の境界を維持する。

- `model-controller`はdefault networkの`8810`で待つが、`ports`を持たずhost/LANへ公開しない。
- APIは`http://model-controller:8810`へ接続し、controllerのhealthy後に起動する。
- `/var/run/docker.sock`はrootで動くcontrollerだけへmountし、APIには絶対にmountしない。
- controllerからhost vLLMの確認先は`http://host.docker.internal:8000`とする。
- `scripts/vllm-server.sh`は`VLLM_BASE_URL`で確認先を切り替え、Docker daemonへ固定2プロファイルだけを渡す。

## 検証と起動

secretファイルを生成する前でも構造とimageは検証できる。

```bash
CGA_HOST_HF_HOME=/absolute/host/path/to/huggingface docker compose config --quiet
docker compose build model-controller
```

管理者が`.env.model-controller`とcache pathを準備した後、計画した保守時間に起動する。

```bash
docker compose up -d model-controller api
docker compose ps model-controller api
docker compose exec model-controller curl -fsS http://127.0.0.1:8810/health
```

このhealth endpointはcontrollerプロセスの生存確認であり、GPUモデルを起動しない。実際の`/switch`は
モデルロードを伴い最大1500秒かかるため、テストやデプロイ確認で無断実行しない。
