// ==UserScript==
// @name         Classroom Grader (下書き点入力)
// @namespace    classroom-grading-automation
// @version      1.0
// @description  採点API(localhost:8800)から点数を取得し、Classroom成績簿に下書き点を入力する
// @match        https://classroom.google.com/*
// @grant        none
// ==/UserScript==

/*
 * 使い方:
 *   1. GPUマシンで採点API を起動: docker compose up -d api
 *      (SSHごしなら手元PCへ -L 8800:localhost:8800 でポート転送)
 *   2. Tampermonkey等でこのスクリプトを登録
 *   3. 対象課題の「成績」ページ(生徒×課題の一覧)を開く
 *   4. 右下のパネルで courseWorkId を入れて「プレビュー」→内容確認→「入力実行」
 *
 * 安全設計:
 *   - 下書き点(draftGrade)の入力欄のみ操作。「返却」ボタンには一切触れない
 *   - 既に点数が入っているセルはスキップ(上書きしない)
 *   - 入力後に値を読み戻して検証。ズレたら中断
 *   - まずプレビュー(色付けのみ、入力しない)で対象を確認してから実行する
 *
 * 注意: Classroomの成績簿DOMは変わりやすい。動かない場合は下の SELECTORS を
 *       実際のページに合わせて調整すること(ブラウザの検証ツールで確認)。
 */

(function () {
  "use strict";

  const API_BASE = "http://localhost:8800";

  // ---- ページDOMに依存する部分(壊れたらここを調整) ----
  const SELECTORS = {
    // 生徒1人分の行。Classroom成績簿は行に aria-label で氏名が入ることが多い
    studentRow: '[role="row"], [role="listitem"]',
    // 行内の氏名テキスト
    studentName: '[data-student-name], [aria-label]',
    // 行内の点数入力欄(下書き点)
    gradeInput: 'input[type="text"], input[aria-label*="点"], input[aria-label*="grade" i]',
  };

  // ---- 氏名の正規化(照合用): 学籍番号プレフィックスと空白を除去 ----
  function normName(s) {
    return (s || "")
      .replace(/AR\d{5}/i, "")
      .replace(/[\s　]/g, "")
      .trim();
  }

  // ---- API取得 ----
  async function fetchGrades(cw) {
    const res = await fetch(`${API_BASE}/grades/${cw}`);
    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
    return (await res.json()).grades;
  }

  // ---- 成績簿の行を{名前 -> 入力欄}で集める ----
  function collectRows() {
    const rows = [];
    document.querySelectorAll(SELECTORS.studentRow).forEach((row) => {
      const nameEl = row.querySelector(SELECTORS.studentName);
      const input = row.querySelector(SELECTORS.gradeInput);
      if (!nameEl || !input) return;
      const name = nameEl.getAttribute("aria-label") || nameEl.textContent;
      if (name) rows.push({ name: normName(name), input, row });
    });
    return rows;
  }

  // ---- 1件だけ照合(名前の部分一致) ----
  function matchRow(rows, grade) {
    const target = normName(grade.name);
    return rows.find((r) => r.name && (r.name === target ||
      r.name.includes(target) || target.includes(r.name)));
  }

  // ---- 入力欄に値を設定してReactに通知 ----
  function setInputValue(input, value) {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value").set;
    setter.call(input, String(value));
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  }

  // ---- メイン処理 ----
  async function run(cw, { preview }) {
    const grades = await fetchGrades(cw);
    const rows = collectRows();
    log(`成績簿の行: ${rows.length}件 / API: ${grades.length}件`);

    // 自動確定(auto_*)と、下書き基準を入れる候補(candidate_3)を対象にする。
    // reviewは人間判断待ちなので入力しない。
    const targets = grades.filter((g) =>
      String(g.category).startsWith("auto_") || g.category === "candidate_3");

    let done = 0, skip = 0, miss = 0, fail = 0;
    for (const g of targets) {
      const row = matchRow(rows, g);
      if (!row) { miss++; continue; }
      const score = g.score_after_late ?? g.content_score;

      if (row.input.value && row.input.value.trim() !== "") {
        row.row.style.outline = "2px solid #999";  // 既存点あり=グレー
        skip++;
        continue;
      }
      if (preview) {
        row.row.style.outline = "2px solid #4a90d9";  // 入力予定=青
        row.row.title = `→ ${score}点 (${g.category})`;
        done++;
        continue;
      }
      setInputValue(row.input, score);
      await new Promise((r) => setTimeout(r, 300));  // 反映待ち
      if (String(row.input.value).trim() === String(score)) {
        row.row.style.outline = "2px solid #4caf50";  // 成功=緑
        done++;
      } else {
        row.row.style.outline = "2px solid #e53935";  // 失敗=赤
        fail++;
        log(`!! 検証NG: ${g.name} 期待=${score} 実際=${row.input.value}。中断します。`);
        break;
      }
    }
    log(`${preview ? "プレビュー" : "入力"}完了: 対象${targets.length} / ` +
        `${preview ? "予定" : "入力"}${done} / スキップ(既存)${skip} / ` +
        `未照合${miss} / 失敗${fail}`);
    if (miss > 0) log("※未照合は氏名の表記ゆれの可能性。SELECTORSかnormNameを調整。");
  }

  // ---- 操作パネル ----
  function log(msg) {
    const el = document.getElementById("cga-log");
    if (el) el.textContent = msg + "\n" + el.textContent;
    console.log("[ClassroomGrader]", msg);
  }

  function buildPanel() {
    if (document.getElementById("cga-panel")) return;
    const p = document.createElement("div");
    p.id = "cga-panel";
    p.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:99999;" +
      "background:#fff;border:1px solid #ccc;border-radius:8px;padding:10px;" +
      "font:12px sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.2);width:280px";
    p.innerHTML =
      '<b>Classroom Grader</b><br>' +
      'courseWorkId: <input id="cga-cw" style="width:150px">' +
      '<div style="margin-top:6px">' +
      '<button id="cga-preview">プレビュー</button> ' +
      '<button id="cga-run" style="color:#b00">入力実行</button></div>' +
      '<pre id="cga-log" style="max-height:140px;overflow:auto;margin:6px 0 0;' +
      'white-space:pre-wrap;color:#333"></pre>';
    document.body.appendChild(p);
    const cw = () => document.getElementById("cga-cw").value.trim();
    document.getElementById("cga-preview").onclick = () =>
      run(cw(), { preview: true }).catch((e) => log("ERROR: " + e.message));
    document.getElementById("cga-run").onclick = () => {
      if (confirm("下書き点を入力します(返却はしません)。続行しますか?"))
        run(cw(), { preview: false }).catch((e) => log("ERROR: " + e.message));
    };
  }

  const iv = setInterval(() => { if (document.body) { buildPanel(); clearInterval(iv); } }, 1000);
})();
