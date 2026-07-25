"use strict";
(() => {
  const context = CgaCore.parseClassroomContext(location.href);
  if (!context) return;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  const host = document.createElement("div");
  host.id = "cga-extension-root";
  const shadow = host.attachShadow({ mode: "closed" });
  shadow.innerHTML = `<style>
    :host{all:initial} .box{position:fixed;right:18px;bottom:18px;z-index:2147483647;width:300px;padding:14px;background:#fff;border:1px solid #bbb;border-radius:10px;box-shadow:0 3px 16px #0004;font:14px sans-serif;color:#202124}
    input,button{box-sizing:border-box;width:100%;margin-top:8px;padding:8px} button{cursor:pointer} button:disabled{cursor:wait;opacity:.6}.status{margin-top:8px;white-space:pre-wrap}.warn{color:#b3261e;font-size:12px}
  </style><section class="box" aria-label="採点下書き入力">
    <strong>採点下書き入力</strong><div class="warn">空欄だけに入力します。返却・送信・既存点の上書きは行いません。</div>
    <div id="ready" class="status">転送済みキューを確認しています…</div>
    <details id="legacy"><summary>旧方式の接続コードを使用</summary>
      <label id="pair-label">初回接続コード（端末ペアリング）<input id="code" autocomplete="one-time-code" spellcheck="false"></label>
    </details>
    <button id="fill" type="button">旧方式の下書きを取得して入力</button>
    <button id="clear" type="button" disabled>この画面で入力した下書きを削除</button>
    <div id="status" class="status" role="status"></div>
  </section>`;
  document.documentElement.append(host);
  const code = shadow.getElementById("code");
  const button = shadow.getElementById("fill");
  const clearButton = shadow.getElementById("clear");
  const status = shadow.getElementById("status");
  const pairLabel = shadow.getElementById("pair-label");
  const ready = shadow.getElementById("ready");
  const legacy = shadow.getElementById("legacy");
  let paired = false;
  let retryLegacy = null;
  let automaticRunning = false;
  const undoRecords = new Map();

  function recordFilled(studentId, studentName, score) {
    undoRecords.set(studentId, { studentName, score: Number(score) });
    clearButton.disabled = false;
    clearButton.textContent = `この画面で入力した${undoRecords.size}件を削除`;
  }

  function forgetFilled(studentId) {
    undoRecords.delete(studentId);
    clearButton.disabled = undoRecords.size === 0;
    clearButton.textContent = undoRecords.size
      ? `この画面で入力した${undoRecords.size}件を削除`
      : "この画面で入力した下書きを削除";
  }

  function message(payload) {
    return new Promise((resolve, reject) => chrome.runtime.sendMessage(payload, (reply) => {
      if (chrome.runtime.lastError) reject(new Error("拡張機能との通信に失敗しました。"));
      else if (!reply?.ok) reject(new Error(reply?.error || "要求に失敗しました。"));
      else resolve(reply);
    }));
  }

  function setNativeValue(input, value) {
    Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value").set.call(input, String(value));
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  async function openGradeInput(gradeButton) {
    const stale = CgaSelectors.activeEditInput(document, window);
    if (stale) return null;
    gradeButton.click();
    for (let i = 0; i < 30; i += 1) {
      const input = document.activeElement;
      if (CgaSelectors.isVisibleEditInput(input, window)) return input;
      await sleep(50);
    }
    return null;
  }

  async function fillOne(gradeButton, studentId, studentName, score) {
    if (!gradeButton.matches(CgaSelectors.SELECTORS.emptyGradeButton)
        || CgaSelectors.gradeButtonForStudent(studentId, studentName, document) !== gradeButton) return "failed";
    const input = await openGradeInput(gradeButton);
    if (!input) return "failed";
    const initial = input.getAttribute("data-initial-value") ?? input.value;
    if (String(initial).trim() !== "") { input.blur(); return "skipped"; }
    setNativeValue(input, score);
    if (!Number.isFinite(Number(input.value)) || Number(input.value) !== Number(score)) {
      input.blur();
      return "failed";
    }
    await sleep(100);
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
    input.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", bubbles: true }));
    input.blur();
    for (let i = 0; i < 20; i += 1) {
      await sleep(50);
      const current = CgaSelectors.gradeButtonForStudent(studentId, studentName, document);
      if (current && !current.matches(CgaSelectors.SELECTORS.emptyGradeButton)) return "filled";
    }
    return "failed";
  }

  async function clearOne(gradeButton, studentId, studentName, expectedScore) {
    if (gradeButton.matches(CgaSelectors.SELECTORS.emptyGradeButton)
        || CgaSelectors.gradeButtonForStudent(studentId, studentName, document) !== gradeButton) return "changed";
    const input = await openGradeInput(gradeButton);
    if (!input) return "failed";
    const initial = input.getAttribute("data-initial-value") ?? input.value;
    if (!Number.isFinite(Number(initial)) || Number(initial) !== Number(expectedScore)) {
      input.blur();
      return "changed";
    }
    setNativeValue(input, "");
    if (String(input.value) !== "") { input.blur(); return "failed"; }
    await sleep(100);
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", code: "Enter", bubbles: true }));
    input.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", code: "Enter", bubbles: true }));
    input.blur();
    for (let i = 0; i < 20; i += 1) {
      await sleep(50);
      const current = CgaSelectors.gradeButtonForStudent(studentId, studentName, document);
      if (current?.matches(CgaSelectors.SELECTORS.emptyGradeButton)) return "cleared";
    }
    return "failed";
  }

  async function clearFilled() {
    if (!undoRecords.size || automaticRunning || button.disabled) return;
    if (!confirm(`この画面で拡張機能が入力した最大${undoRecords.size}件を削除します。\n現在値が入力時と一致する下書きだけが対象です。続行しますか？`)) return;
    button.disabled = true;
    clearButton.disabled = true;
    let cleared = 0, changed = 0, failed = 0, unseen = 0, stagnant = 0, previousSignature = "";
    try {
      for (let pass = 0; pass < 120 && undoRecords.size && !failed && stagnant < 3; pass += 1) {
        let processed = 0;
        const gradeButtons = CgaSelectors.gradeButtons(document);
        for (const [studentId, item] of [...undoRecords]) {
          const gradeButton = CgaSelectors.gradeButtonForStudent(
            studentId, item.studentName, document);
          if (!gradeButton) continue;
          const outcome = await clearOne(
            gradeButton, studentId, item.studentName, item.score);
          processed += 1;
          if (outcome === "cleared") { cleared += 1; forgetFilled(studentId); }
          else if (outcome === "changed") { changed += 1; forgetFilled(studentId); }
          else { failed += 1; break; }
        }
        if (!undoRecords.size || failed) break;
        const signature = gradeButtons.map((item) => item.getAttribute("aria-label") || "").join("|");
        gradeButtons.at(-1)?.scrollIntoView({ block: "center" });
        await sleep(400);
        stagnant = processed === 0 && signature === previousSignature ? stagnant + 1 : 0;
        previousSignature = signature;
      }
      unseen = undoRecords.size;
      status.textContent = `削除 ${cleared} / 変更済みのため保護 ${changed} / 未表示・未照合 ${unseen} / 失敗 ${failed}`;
    } finally {
      button.disabled = false;
      clearButton.disabled = undoRecords.size === 0;
    }
  }

  async function runLocal(queue) {
    const pending = new Map(queue.items.map((item) => [item.student_id,
      { score: item.score, studentName: item.student_name || "" }]));
    const completed = new Set(), filled = new Set(), matched = new Set(), existing = new Set();
    let detected = 0, failures = 0, stagnant = 0, previousSignature = "";
    for (let pass = 0; pass < 120 && pending.size && !failures && stagnant < 3; pass += 1) {
      let processed = 0;
      const gradeButtons = CgaSelectors.gradeButtons(document);
      detected = Math.max(detected, gradeButtons.length);
      for (const gradeButton of gradeButtons) {
        const studentId = CgaSelectors.pendingStudentForGradeButton(gradeButton, pending);
        if (!studentId || !pending.has(studentId)) continue;
        matched.add(studentId);
        if (!gradeButton.matches(CgaSelectors.SELECTORS.emptyGradeButton)) {
          CgaCore.applyLocalGradeOutcome(
            { pending, completed, filled, existing }, studentId, "existing");
          processed += 1;
          continue;
        }
        const item = pending.get(studentId);
        const result = await fillOne(gradeButton, studentId, item.studentName, item.score);
        processed += 1;
        if (result === "filled") {
          recordFilled(studentId, item.studentName, item.score);
          CgaCore.applyLocalGradeOutcome(
            { pending, completed, filled, existing }, studentId, "filled");
        }
        else if (result === "failed") { failures += 1; break; }
      }
      const signature = gradeButtons.map((item) => item.getAttribute("aria-label") || "").join("|");
      gradeButtons.at(-1)?.scrollIntoView({ block: "center" });
      await sleep(400);
      stagnant = processed === 0 && signature === previousSignature ? stagnant + 1 : 0;
      previousSignature = signature;
    }
    let remaining = queue.items.length;
    if (completed.size) {
      const updated = await message({ type: "updateLocalQueue", completedStudentIds: [...completed] });
      remaining = updated.remaining;
    }
    const unseen = pending.size;
    status.textContent = `検出 ${detected} / 照合 ${matched.size} / 入力 ${filled.size} / 既存点 ${existing.size} / 未表示・未照合 ${unseen} / 失敗 ${failures}\n${remaining ? `残り${remaining}件を保持しました。再試行できます。` : "全件完了し、転送キューを消去しました。"}`;
    ready.textContent = remaining ? `準備済み ${remaining}件` : "転送済みキューはありません。";
    button.textContent = remaining ? `準備済み${remaining}件を空欄へ入力` : "旧方式の下書きを取得して入力";
    legacy.hidden = remaining > 0;
  }

  async function runAutomatic(job) {
    const pending = new Map(job.entries.map((entry) => [entry.studentId,
      { score: entry.score, studentName: entry.studentName || "" }]));
    const matched = new Set();
    const results = [];
    let detected = 0;
    for (let pass = 0; pass < 12 && pending.size; pass += 1) {
      let processed = 0;
      const gradeButtons = CgaSelectors.gradeButtons(document);
      detected = Math.max(detected, gradeButtons.length);
      for (const gradeButton of gradeButtons) {
        const studentId = CgaSelectors.pendingStudentForGradeButton(gradeButton, pending);
        if (!studentId || !pending.has(studentId)) continue;
        matched.add(studentId);
        let outcome = "existing";
        if (gradeButton.matches(CgaSelectors.SELECTORS.emptyGradeButton)) {
          const item = pending.get(studentId);
          outcome = await fillOne(gradeButton, studentId, item.studentName, item.score);
          if (outcome === "filled") recordFilled(studentId, item.studentName, item.score);
          if (outcome === "skipped") outcome = "existing";
        }
        results.push({ student_id: studentId, outcome });
        pending.delete(studentId);
        processed += 1;
      }
      if (!pending.size) break;
      gradeButtons.at(-1)?.scrollIntoView({ block: "center" });
      await sleep(400);
      if (!processed && pass >= 2) break;
    }
    const reply = await message({ type: "reportDraftInputJob", jobId: job.jobId, results });
    const filled = results.filter((item) => item.outcome === "filled").length;
    const existing = results.filter((item) => item.outcome === "existing").length;
    const failed = results.filter((item) => item.outcome === "failed").length;
    status.textContent = `自動ジョブ: 検出 ${detected} / 照合 ${matched.size} / 入力 ${filled} / 既存点 ${existing} / 未表示・未照合 ${pending.size} / 失敗 ${failed}\n状態: ${reply.progress.status}`;
  }

  async function pollAutomatic() {
    if (automaticRunning || button.disabled || document.hidden) return;
    const current = CgaCore.parseClassroomContext(location.href);
    if (!current) return;
    if (CgaSelectors.activeEditInput(document, window)) return;
    automaticRunning = true;
    try {
      const reply = await message({ type: "pollDraftInputJob" });
      paired = Boolean(reply.paired);
      pairLabel.hidden = paired;
      if (reply.job) await runAutomatic(reply.job);
    } catch (error) {
      status.textContent = `自動ジョブ停止: ${error.message}`;
    } finally {
      automaticRunning = false;
    }
  }

  async function runLegacy() {
      const current = CgaCore.parseClassroomContext(location.href);
      if (!current) throw new Error("表示中のClassroom課題を確認できません。");
      const reply = retryLegacy || (paired
        ? await message({ type: "claimLatestBatch", context: current })
        : await message({ type: "claimBatch", pairingCode: code.value, context: current }));
      paired = true;
      pairLabel.hidden = true;
      code.value = "";
      const batch = CgaCore.validateBatch(reply.batch, current);
      const pending = new Map(batch.entries.map((entry) => [entry.studentId,
        { score: entry.score, studentName: entry.studentName || "" }]));
      const summary = { attempted: batch.entries.length, filled: 0, skipped: 0, failed: 0 };
      let stagnant = 0;
      let previousSignature = "";
      for (let pass = 0; pass < 120 && pending.size && !summary.failed && stagnant < 3; pass += 1) {
        let processed = 0;
        const gradeButtons = CgaSelectors.gradeButtons(document);
        for (const gradeButton of gradeButtons) {
          const studentId = CgaSelectors.pendingStudentForGradeButton(gradeButton, pending);
          if (!studentId || !pending.has(studentId)) continue;
          let result = "skipped";
          if (gradeButton.matches(CgaSelectors.SELECTORS.emptyGradeButton)) {
            const item = pending.get(studentId);
            result = await fillOne(gradeButton, studentId, item.studentName, item.score);
            if (result === "filled") recordFilled(studentId, item.studentName, item.score);
          }
          summary[result] += 1;
          pending.delete(studentId);
          processed += 1;
          if (result === "failed") break;
        }
        const signature = gradeButtons.map((item) => item.getAttribute("aria-label") || "").join("|");
        gradeButtons.at(-1)?.scrollIntoView({ block: "center" });
        await sleep(400);
        stagnant = processed === 0 && signature === previousSignature ? stagnant + 1 : 0;
        previousSignature = signature;
      }
      summary.skipped += pending.size;
      if (summary.filled > 0) {
        await message({ type: "consumeBatch", receipt: reply.receipt, summary });
        retryLegacy = null;
        status.textContent = `旧方式完了: 入力 ${summary.filled} / 空欄なし・未表示 ${summary.skipped} / 失敗 ${summary.failed}`;
      } else {
        retryLegacy = reply;
        status.textContent = `旧方式: 入力0件のためバッチを消費せず保持しました。空欄と表示課題を確認して再試行してください（未表示 ${summary.skipped} / 失敗 ${summary.failed}）。`;
      }
  }

  async function run() {
    if (automaticRunning) {
      status.textContent = "自動ジョブを処理中です。完了後に再実行してください。";
      return;
    }
    button.disabled = true;
    status.textContent = "準備済みキューを確認しています…";
    try {
      const current = CgaCore.parseClassroomContext(location.href);
      if (!current) throw new Error("表示中のClassroom課題を確認できません。");
      if (CgaSelectors.activeEditInput(document, window)) throw new Error("編集中の成績欄を閉じてから再実行してください。");
      const local = await message({ type: "getLocalQueue" });
      if (local.queue) await runLocal(local.queue);
      else await runLegacy();
    } catch (error) {
      status.textContent = `停止: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  }
  button.addEventListener("click", run);
  clearButton.addEventListener("click", clearFilled);
  message({ type: "getLocalQueue" }).then((reply) => {
    if (reply.queue) {
      ready.textContent = `準備済み ${reply.queue.items.length}件`;
      button.textContent = `準備済み${reply.queue.items.length}件を空欄へ入力`;
      legacy.hidden = true;
    } else {
      ready.textContent = "転送済みキューはありません。旧方式も利用できます。";
      button.textContent = "旧方式の下書きを取得して入力";
    }
  }).catch(() => { ready.textContent = "転送済みキューを確認できませんでした。"; });
  message({ type: "deviceStatus" }).then((reply) => {
    paired = Boolean(reply.paired);
    pairLabel.hidden = paired;
  }).catch(() => {});
  setTimeout(pollAutomatic, 1000);
  setInterval(pollAutomatic, 5000);
})();
