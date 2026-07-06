// ==UserScript==
// @name         Classroom Grader (下書き点入力)
// @namespace    classroom-grading-automation
// @version      1.1
// @description  採点APIから点数を取得し、Classroom成績簿に下書き点を入力する
// @match        https://classroom.google.com/*
// @updateURL    https://raw.githubusercontent.com/kake256/class_room_app/main/browser/classroom-grader.user.js
// @downloadURL  https://raw.githubusercontent.com/kake256/class_room_app/main/browser/classroom-grader.user.js
// @grant        none
// ==/UserScript==

/*
 * 使い方:
 *   1. GPUマシンで採点API + トンネル(Cloudflare/Tailscale/SSH転送)を用意
 *   2. 対象課題の「生徒の提出物」ページを開く
 *   3. 右下パネルで API接続先・token・courseWorkId を入れて「プレビュー」→確認→「入力実行」
 *
 * 安全設計:
 *   - 下書き点(draftGrade)の入力欄のみ操作。「返却」ボタンには一切触れない
 *   - 既に点数が入っているセルはスキップ(上書きしない)
 *   - 入力後に値を読み戻して検証。ズレたら中断
 *
 * 注意:
 *   - Classroomは Trusted Types を使うため innerHTML を使わずDOM APIでUIを構築している
 *   - 成績簿DOMは変わりやすい。動かない場合は下の SELECTORS を実際のページに合わせて調整
 */

(function () {
  "use strict";

  const DEFAULT_API_BASE = "http://localhost:8800";
  const apiBase = () => localStorage.getItem("cga_api_base") || DEFAULT_API_BASE;
  const apiToken = () => localStorage.getItem("cga_api_token") || "";

  // ---- ページDOMに依存する部分(壊れたらここを調整) ----
  const SELECTORS = {
    studentRow: '[role="row"], [role="listitem"]',
    studentName: '[data-student-name], [aria-label]',
    gradeInput: 'input[type="text"], input[aria-label*="点"], input[aria-label*="grade" i]',
  };

  function normName(s) {
    return (s || "")
      .replace(/AR\d{5}/i, "")
      .replace(/[\s　]/g, "")
      .trim();
  }

  async function fetchGrades(cw) {
    const headers = apiToken() ? { "X-API-Key": apiToken() } : {};
    const res = await fetch(`${apiBase()}/grades/${cw}`, { headers });
    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
    return (await res.json()).grades;
  }

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

  function matchRow(rows, grade) {
    const target = normName(grade.name);
    return rows.find((r) => r.name && (r.name === target ||
      r.name.includes(target) || target.includes(r.name)));
  }

  function setInputValue(input, value) {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value").set;
    setter.call(input, String(value));
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
    input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
  }

  async function run(cw, { preview }) {
    const grades = await fetchGrades(cw);
    const rows = collectRows();
    log(`成績簿の行: ${rows.length}件 / API: ${grades.length}件`);

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
      await new Promise((r) => setTimeout(r, 300));
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

  function log(msg) {
    const el = document.getElementById("cga-log");
    if (el) el.textContent = msg + "\n" + el.textContent;
    console.log("[ClassroomGrader]", msg);
  }

  // ---- 操作パネル(Trusted Types対応: innerHTMLを使わずDOM APIで構築) ----
  function field(parent, labelText, id, width, ph) {
    const row = document.createElement("div");
    row.style.marginTop = "4px";
    row.appendChild(document.createTextNode(labelText + " "));
    const inp = document.createElement("input");
    inp.id = id;
    inp.style.width = width;
    if (ph) inp.placeholder = ph;
    row.appendChild(inp);
    parent.appendChild(row);
    return inp;
  }

  function buildPanel() {
    if (document.getElementById("cga-panel")) return;
    const p = document.createElement("div");
    p.id = "cga-panel";
    p.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:99999;" +
      "background:#fff;border:1px solid #ccc;border-radius:8px;padding:10px;" +
      "font:12px sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.2);width:280px";

    const title = document.createElement("b");
    title.textContent = "Classroom Grader";
    p.appendChild(title);

    const apiInput = field(p, "API:", "cga-api", "210px", "https://xxx.trycloudflare.com");
    const tokenInput = field(p, "token:", "cga-token", "200px", "api.tokenと同じ値");
    const cwInput = field(p, "courseWorkId:", "cga-cw", "150px", "");

    const btnRow = document.createElement("div");
    btnRow.style.marginTop = "6px";
    const previewBtn = document.createElement("button");
    previewBtn.textContent = "プレビュー";
    const runBtn = document.createElement("button");
    runBtn.textContent = "入力実行";
    runBtn.style.color = "#b00";
    runBtn.style.marginLeft = "6px";
    btnRow.appendChild(previewBtn);
    btnRow.appendChild(runBtn);
    p.appendChild(btnRow);

    const logEl = document.createElement("pre");
    logEl.id = "cga-log";
    logEl.style.cssText = "max-height:140px;overflow:auto;margin:6px 0 0;" +
      "white-space:pre-wrap;color:#333";
    p.appendChild(logEl);

    document.body.appendChild(p);

    apiInput.value = apiBase();
    tokenInput.value = apiToken();
    apiInput.onchange = () => localStorage.setItem("cga_api_base", apiInput.value.trim());
    tokenInput.onchange = () => localStorage.setItem("cga_api_token", tokenInput.value.trim());

    const cw = () => cwInput.value.trim();
    previewBtn.onclick = () =>
      run(cw(), { preview: true }).catch((e) => log("ERROR: " + e.message));
    runBtn.onclick = () => {
      if (confirm("下書き点を入力します(返却はしません)。続行しますか?"))
        run(cw(), { preview: false }).catch((e) => log("ERROR: " + e.message));
    };
  }

  const iv = setInterval(() => { if (document.body) { buildPanel(); clearInterval(iv); } }, 1000);
})();
