# Classroom Grading Draft Filler (MV3)

## 推奨セットアップ（0.5.4）

1. Web UIの「Chrome拡張機能セットアップ」でZIPをダウンロードして展開する。
2. Chromeで`chrome://extensions`を開き、デベロッパーモードを有効にする。
3. 「パッケージ化されていない拡張機能を読み込む」を押し、`manifest.json`がある解凍フォルダを選ぶ。
4. Claude/CodexからMCPの`create_classroom_draft_input_job(..., confirm=true)`を実行する。
5. Classroomの同じ課題の`.../submissions/...`採点画面を開く。ペアリング済み拡張が
   5秒間隔で該当ジョブを取得し、空欄だけへ自動入力する。

0.4.0のWeb UI「拡張機能へ転送」方式も引き続き利用できます。その場合は画面の
「準備済みN件を空欄へ入力」を押します。

Web UI bridgeは公開originと固定message schemaを検証し、課題ID、満点、学生ID、表示名、点数だけを
`chrome.storage.session`へ30分間保存します。答案・理由は転送しません。表示名はClassroom DOMに学生IDがない場合のTampermonkey互換照合にだけ使用します。1課題1queue、最大500件で、
成功した学生IDだけをqueueから除きます。0件入力・途中失敗・未表示がある場合は残件を保持します。
ページを再読み込みしても同じブラウザセッション内で再開でき、全件成功時だけqueueを消去します。

公開サーバー`https://classroom-grader-1.tail80e540.ts.net`が既定値です。通常はURL入力が不要です。
既に保存済みのURLは維持されます。管理者が接続先を変更する場合は拡張の設定画面へoriginだけを入力し、
`/ui`や`/mcp`を付けません。

初回接続コードで端末を一度ペアリングします。自動ジョブはMCPでの`confirm=true`を実行許可とし、
拡張側の追加クリックを要求しません。既存利用者は0.5.4のZIPで拡張を更新してください。

0.5.4ではClassroomの空欄と既存点を同じ行走査で検出します。学生IDを優先し、IDがDOMにない場合はTampermonkey版と同じ氏名照合へ安全にフォールバックします。既存点は上書きせず完了扱いにし、
非表示の永続inputは編集中と誤判定しません。入力後は対象学生の行が空欄でなくなったことを確認できた
場合だけ成功として扱います。

自動ジョブでは表示中のcourse/courseWorkと完全一致するジョブだけを取得します。既存値の上書き、返却、送信、コメント、チェックボックス操作は実装していません。下書き削除は同じページ読み込み中に拡張機能自身が入力成功した項目だけをメモリで保持し、現在値が入力時の点数と一致する場合だけ空欄へ戻します。ページ更新後、人が変更した点数、既存点は削除対象になりません。既存点は`existing`として完了扱いにします。学生名・ID・答案・点数をconsoleへ記録せず、画面とMCP進捗には件数だけを表示します。ClassroomのDOM依存は `selectors.js` に隔離しています。

## バックエンドAPI

端末raw tokenは`chrome.storage.local`にだけ保存し、`sync`へは保存しません。設定画面の再接続はブラウザ内のlocal tokenだけを消し、サーバー側端末を失効しません。完全に失効する場合はWeb UIの端末一覧で「失効」を押します。全応答に `Cache-Control: no-store` を付け、サーバーはdevice/batch tokenのhashだけを保存します。CORS許可は不要です（拡張service workerが通信）。

1. `POST /api/v1/extension/devices/claim`
   - body: `{pairing_code}`
   - 初回接続コードを一度だけ使用し、端末tokenを返す。端末tokenはlocal storageだけへ保存する。
2. `POST /api/v1/extension/device-batches/claim`
   - 端末tokenと表示中の`course_id` / `coursework_id`で最新の待機バッチをclaimする。
3. `POST /api/v1/extension/pairings/claim`（旧式fallback）
   - body: `{pairing_code, course_id, coursework_id}`
   - 旧式の短期下書きコードを原子的に使用済みにし、`{batch_id, access_token, expires_at}` を返す。
4. `GET /api/v1/extension/batches/{batch_id}`
   - `Authorization: Bearer <access_token>`
   - `{batch_id, course_id, coursework_id, expires_at, max_points, items:[{student_id, score}]}` を返す。点数は確定済みの有限値のみ。
5. `POST /api/v1/extension/batches/{batch_id}/consume`
   - 同じBearer token、bodyはPIIなしの `{attempted, filled, skipped, failed}`。
   - batchを原子的に使用済みにし、`{consumed:true}`を返す。使用済み・無効なcapabilityは`404`。
6. `GET /api/v1/extension/draft-input-jobs/pending?course_id=...&coursework_id=...`
   - 端末tokenと表示中の課題が一致し、採点基準fingerprintが現在も同一の待機ジョブだけを返す。
7. `POST /api/v1/extension/draft-input-jobs/{job_id}/progress`
   - `filled` / `existing` / `failed`を報告する。未表示項目はpendingのまま保持し、再開できる。

バッチ作成はGoogleログイン利用者に束縛する。claimはcourse/courseWork文脈一致を必須とし、
claim後は短命Bearer capabilityだけでfetch/consumeする。TTL超過・文脈不一致は情報を漏らさない
`404`、レート制限は`429`とする。APIはClassroom操作を行わない。

## 開発確認

```sh
node --test tests/*.test.js
node --check background.js && node --check content.js && node --check bridge.js && node --check options.js
```
