# Classroomへの下書き点入力

現在のMVPは、AI採点案を教員が確認した後、専用Chrome/Edge拡張がClassroomの空欄へ
下書き点だけを入力する。自動確定、自動返却、既存点の上書き、下書き削除は行わない。

## 現行フロー

1. Web UIへGoogleログインする。
2. 課題を選び「AI採点案を作成」を押す。提出物取得、一次採点、モデル切替、再チェック、
   集計が順に進む。
3. Web UIで全答案の提案点を確認し、必要なら修正して確認済みにする。
4. 「下書き対象をプレビュー」で対象と除外件数を確認する。
5. 明示確認後に短期・一回利用のバッチを作成し、ペアリングコードを得る。
6. Chrome/Edgeへ`extension/`をload-unpackedし、Classroomの対象提出物ページを開く。
7. 拡張へWeb UIのserver URLとペアリングコードを入力し、「取得して空欄へ入力」を押す。
8. Classroom上で結果を確認し、教員が最終確定・返却する。

Web UIはGoogleセッションCookieとCSRFで保護され、長期固定APIキーを要求しない。バッチの
pairing codeとaccess tokenは短命で、サーバーにはハッシュだけを保持する。

## バッチの安全条件

バッチには次をすべて満たす答案だけが入る。

- Web UIで教員が確認済み
- 未提出・RETURNEDではない
- 人間採点、既存draft grade、既存assigned gradeがない
- 0以上かつ課題満点以下

拡張側でも次を強制する。

- URLのcourse IDとcourseWork IDがバッチと一致する
- student IDで一意に対応できる
- 成績欄が空である
- 入力後の表示値が期待点と一致する
- 自動返却、送信、削除、チェックボックス操作、既存点上書きを実装しない
- 学生名、ID、答案、点数、capabilityをconsoleへ記録しない

詳細は[`extension/README.md`](../extension/README.md)を参照する。ClassroomのDOM変更時は
`extension/selectors.js`だけを調整し、実課題では少人数の検証後に利用する。

## 接続経路

同一端末・SSH転送では`http://localhost:8800`を利用できる。外部から固定URLで利用する場合は、
クラウドの小型gatewayと251から外向き接続するagentを使う。gatewayは要求を永続保存せず、
Google session、CSRF、RBACをそのまま中継する。導入、TLS、secret、provider URL、OAuth redirect URIの
更新手順は[`docs/outbound-gateway.md`](outbound-gateway.md)を参照する。

## Google Classroom APIの制約

Classroom UIで作成した課題は、別Google Cloudプロジェクトからのgrade書き込みが
`ProjectPermissionDenied`になる場合がある。このため現行MVPはログイン済みClassroom画面で動く
拡張を使う。APIや拡張が学生への返却を自動実行することはない。

## Legacy: Tampermonkey

`browser/classroom-grader.user.js`は過去運用との互換確認用に残しているが、現行MVPでは非推奨である。
旧スクリプトには確定組、自動返却、下書き削除など現行方針に含まれない機能があるため、通常運用で
インストール・実行しない。新規利用者は`extension/`だけを使用する。

`push-grades`もAPI作成課題向けのlegacy CLIであり、現行Web UIフローでは使用しない。
