# AI採点結果とClassroom下書き入力

MVPではAIの点数を自動確定・自動返却しない。運用順序は固定する。

1. Web UIの「AI採点案を作成」または外部AI連携で採点結果を作成する。
2. 現在の採点基準で正常に作成され、安全条件を満たすAI採点結果は自動的に下書き対象になる。必要な答案だけWeb UIで点数を修正する。
3. 「下書き対象をプレビュー」で対象件数と除外件数を確認する。
4. 短期・一回利用のバッチを作り、MV3拡張へペアリングコードを渡す。
5. 初回だけ端末ペアリングコードを拡張へ入力する。以後はClassroom提出物ページの「最新の下書きを取得して入力」を1回押し、空欄だけへ下書き点を入力する。
6. 教員がClassroom上で点数を確認し、最終確定・返却する。

バッチからは未提出、RETURNED、人間採点、既存draft/assigned grade、無効・古い採点結果、0未満、満点超過を
除外する。拡張も空欄以外を上書きせず、自動返却、送信、削除を行わない。旧reportの
`auto_*`/`candidate_3` categoryは読取互換のため残るが、UIではすべてAI提案として扱う。

教員確認は利用者Google `sub`のハッシュ、course ID、courseWork ID、student IDの組で保存し、
別利用者・別コース・別課題へ混ざらない。バッチ本体はAPIメモリだけに短時間保持し、pairing codeと
access tokenはハッシュだけを保存する。gatewayには答案・採点結果を永続保存しない。
端末tokenもサーバにはSHA-256 hashだけをowner別0600 JSONで保存し、raw tokenは拡張の`chrome.storage.local`だけに置く。端末はWeb UIから失効でき、待機バッチは同じowner/course/courseWorkだけが原子的に一度取得できる。
拡張設定画面の「local token削除」はブラウザ内だけの解除であり、サーバー側端末の失効ではない。完全な失効はWeb UIの端末一覧から行う。現在、未claimの端末pairing codeとready下書きバッチはAPIプロセスメモリにあるため、API再起動時に失われる。端末tokenのhashと採点案は永続化されるので、再起動後はpairing codeまたは下書きバッチだけを再発行する。
現在の採点基準fingerprintで正常に作成されたローカル採点結果または外部MCP採点案は、答案ごとのWeb承認なしで下書きプレビュー対象になる。Web UIで点数を修正した場合はその値を優先する。人間採点、既存draftGrade、RETURNED、未提出、エラー、古いfingerprintは常に除外する。最終確認と返却はClassroom上で教師が行う。
