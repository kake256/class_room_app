// ==UserScript==
// @name         Classroom Grader (下書き点入力)
// @namespace    classroom-grading-automation
// @version      2.0
// @description  採点APIから点数を取得し、Classroom成績簿に下書き点を入力/全削除する
// @match        https://classroom.google.com/*
// @updateURL    https://raw.githubusercontent.com/kake256/class_room_app/main/browser/classroom-grader.user.js
// @downloadURL  https://raw.githubusercontent.com/kake256/class_room_app/main/browser/classroom-grader.user.js
// @grant        none
// ==/UserScript==

/*
 * 「生徒の提出物」ページ(.../submissions/...)で使う。
 *
 * 動作:
 *   - 入力実行: システム採点(1点/2点/3点)を未返却(TURNED_IN)の全員に下書き入力。
 *     判定ズレは下書きを見ながらClassroom上で手直しする運用
 *   - 下書き全削除: 未返却の生徒の下書き点をまとめて消す(再分析後のやり直し用)
 *
 * 安全設計:
 *   - 返却済み(RETURNED)の生徒には入力も削除も一切触れない(APIのstateで判定)
 *   - 「返却」ボタンには一切触れない
 *   - 入力: 既に値がある欄は上書きしない(スキップ)
 *   - クリックで開いた入力欄(activeElement)だけを操作(別セルへの誤書き込み防止)
 *   - 各操作後に結果を検証。失敗したら中断
 */

(function () {
  "use strict";

  const DEFAULT_API_BASE = "http://localhost:8800";
  const apiBase = () => localStorage.getItem("cga_api_base") || DEFAULT_API_BASE;
  const apiToken = () => localStorage.getItem("cga_api_token") || "";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---- ページDOM依存(壊れたらここを調整) ----
  const SEL = {
    // 未採点の点数欄(氏名入り)
    addButton: '[role="button"][aria-label*="成績を追加"]',
    // 点数欄全般(未採点・下書きあり両方。氏名+「さんの成績」を含む)
    anyGradeButton: '[role="button"][aria-label*="さんの成績"]',
    // クリック後に現れる点数入力欄
    editInput: 'input[aria-label="成績を編集"]',
  };

  // 氏名正規化(照合用): 学籍番号プレフィックスと空白を除去
  function normName(s) {
    return (s || "").replace(/AR\d{5}/i, "").replace(/[\s　]/g, "").trim();
  }

  // aria-label「氏名 さんの成績を追加/編集/…」から氏名部分を取り出して正規化
  function nameFromLabel(label) {
    return normName((label || "").replace(/さんの成績.*$/, ""));
  }

  async function fetchGrades(cw) {
    const headers = apiToken() ? { "X-API-Key": apiToken() } : {};
    const res = await fetch(`${apiBase()}/grades/${cw}`, { headers });
    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
    return (await res.json()).grades;
  }

  // セレクタに合う点数ボタンを {正規化氏名 -> ボタン} で集める
  function collectButtons(selector) {
    const map = new Map();
    document.querySelectorAll(selector).forEach((btn) => {
      const name = nameFromLabel(btn.getAttribute("aria-label"));
      if (name) map.set(name, btn);
    });
    return map;
  }

  // <input> に値を設定してReactに通知
  function setInputValue(input, value) {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value").set;
    setter.call(input, String(value));
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // ボタンをクリックして、そのクリックで開いた入力欄(activeElement)を返す
  async function openCell(btn) {
    const stale = document.querySelector(SEL.editInput);
    if (stale) { stale.blur(); await sleep(150); }
    btn.click();
    for (let i = 0; i < 30; i++) {
      const ae = document.activeElement;
      if (ae && ae.matches && ae.matches(SEL.editInput)) return ae;
      await sleep(50);
    }
    return null;
  }

  function pressEnter(input) {
    ["keydown", "keyup"].forEach((type) =>
      input.dispatchEvent(new KeyboardEvent(type, {
        key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true,
      })));
    if (input.blur) input.blur();
  }

  // 1人分入力: 空欄のみに書き込み、確定を検証
  async function setGrade(btn, score, name) {
    const input = await openCell(btn);
    if (!input) return false;
    const initial = input.getAttribute("data-initial-value");
    if (initial && initial.trim() !== "") { input.blur(); return "skip"; }  // 既存値は守る
    setInputValue(input, score);
    await sleep(120);
    pressEnter(input);
    await sleep(250);
    const stillAdd = [...document.querySelectorAll(SEL.addButton)]
      .some((b) => nameFromLabel(b.getAttribute("aria-label")) === name);
    return !stillAdd;
  }

  // 1人分削除: 値がある欄を空にして確定(「成績を追加」に戻れば成功)
  async function clearGrade(btn, name) {
    const input = await openCell(btn);
    if (!input) return false;
    const initial = input.getAttribute("data-initial-value");
    if (!initial || initial.trim() === "") { input.blur(); return "skip"; }  // 元々空
    setInputValue(input, "");
    await sleep(120);
    pressEnter(input);
    await sleep(300);
    const backToAdd = [...document.querySelectorAll(SEL.addButton)]
      .some((b) => nameFromLabel(b.getAttribute("aria-label")) === name);
    return backToAdd;
  }

  // 表示中の行を処理→最後の点数欄をscrollIntoViewで送る→繰り返し(仮想スクロール対応)
  async function sweep(remaining, selector, handler) {
    let done = 0, skip = 0, fail = 0, stagnant = 0, lastSig = "";
    for (let pass = 0; pass < 120 && remaining.size > 0 && !fail && stagnant < 3; pass++) {
      let processed = 0;
      for (const [name, btn] of collectButtons(selector)) {
        if (!remaining.has(name)) continue;
        const t = remaining.get(name);
        const res = await handler(btn, t, name);
        if (res === "fail") { fail++; break; }
        if (res === "skip") skip++;
        else if (res === "done") done++;
        remaining.delete(name);
        processed++;
      }
      if (fail) break;
      const btns = document.querySelectorAll(selector);
      const sig = btns.length + "|" +
        (btns.length ? btns[btns.length - 1].getAttribute("aria-label") : "");
      if (btns.length) btns[btns.length - 1].scrollIntoView({ block: "center" });
      await sleep(500);
      if (processed === 0 && sig === lastSig) stagnant++; else stagnant = 0;
      lastSig = sig;
    }
    return { done, skip, fail, left: remaining.size };
  }

  // 対象: 未返却(TURNED_IN)で点数のある生徒(auto_* / candidate_3 / review すべて)
  function buildTargets(grades) {
    const remaining = new Map();
    grades.forEach((g) => {
      const c = String(g.category);
      const score = g.score_after_late ?? g.content_score;
      const ok = (c.startsWith("auto_") || c === "candidate_3" || c === "review") &&
        score !== null && score !== undefined && g.state === "TURNED_IN";
      if (ok) remaining.set(normName(g.name), { score, name: g.name });
    });
    return remaining;
  }

  async function runInput(cw, { preview }) {
    const remaining = buildTargets(await fetchGrades(cw));
    log(`入力対象(未返却・全カテゴリ): ${remaining.size}件`);
    const r = await sweep(remaining, SEL.addButton, async (btn, t, name) => {
      if (preview) {
        btn.style.outline = "2px solid #4a90d9";
        btn.title = `→ ${t.score}点`;
        return "done";
      }
      const res = await setGrade(btn, t.score, name);
      if (res === "skip") { btn.style.outline = "2px solid #999"; return "skip"; }
      if (res) { btn.style.outline = "2px solid #4caf50"; return "done"; }
      btn.style.outline = "2px solid #e53935";
      log(`!! 確定できず: ${t.name}。中断(既入力分は保持)`);
      return "fail";
    });
    log(`${preview ? "プレビュー" : "入力"}完了: ${preview ? "予定" : "確定"}${r.done}` +
        `${r.skip ? " / スキップ(既存)" + r.skip : ""} / 未照合${r.left} / 失敗${r.fail}`);
    if (r.left > 0) log("※未照合=下書き入力済み(点数欄が「追加」でない)or 画面外");
  }

  async function runDeleteAll(cw) {
    const remaining = buildTargets(await fetchGrades(cw));
    log(`削除対象(未返却のみ): 最大${remaining.size}件`);
    const r = await sweep(remaining, SEL.anyGradeButton, async (btn, t, name) => {
      const res = await clearGrade(btn, name);
      if (res === "skip") { btn.style.outline = "2px dashed #999"; return "skip"; }
      if (res) { btn.style.outline = "2px solid #ff9800"; return "done"; }
      btn.style.outline = "2px solid #e53935";
      log(`!! 削除できず: ${t.name}。中断`);
      return "fail";
    });
    log(`全削除完了: 削除${r.done} / 元々空${r.skip} / 未照合${r.left} / 失敗${r.fail}`);
  }

  function log(msg) {
    const el = document.getElementById("cga-log");
    if (el) el.textContent = msg + "\n" + el.textContent;
    console.log("[ClassroomGrader]", msg);
  }

  // ---- 操作パネル(Trusted Types対応: DOM APIで構築) ----
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
      "font:12px sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.2);width:290px";

    const title = document.createElement("b");
    title.textContent = "Classroom Grader v2";
    p.appendChild(title);

    const apiInput = field(p, "API:", "cga-api", "215px", "https://xxx.trycloudflare.com");
    const tokenInput = field(p, "token:", "cga-token", "205px", "api.tokenと同じ値");
    const cwInput = field(p, "courseWorkId:", "cga-cw", "150px", "");

    const btnRow = document.createElement("div");
    btnRow.style.marginTop = "8px";
    const previewBtn = document.createElement("button");
    previewBtn.textContent = "プレビュー";
    const runBtn = document.createElement("button");
    runBtn.textContent = "入力実行";
    runBtn.style.cssText = "color:#b00;margin-left:6px";
    const delBtn = document.createElement("button");
    delBtn.textContent = "下書き全削除";
    delBtn.style.cssText = "color:#fff;background:#b00;margin-left:6px";
    btnRow.appendChild(previewBtn);
    btnRow.appendChild(runBtn);
    btnRow.appendChild(delBtn);
    p.appendChild(btnRow);

    const logEl = document.createElement("pre");
    logEl.id = "cga-log";
    logEl.style.cssText = "max-height:150px;overflow:auto;margin:6px 0 0;" +
      "white-space:pre-wrap;color:#333";
    p.appendChild(logEl);

    document.body.appendChild(p);

    apiInput.value = apiBase();
    tokenInput.value = apiToken();
    apiInput.onchange = () => localStorage.setItem("cga_api_base", apiInput.value.trim());
    tokenInput.onchange = () => localStorage.setItem("cga_api_token", tokenInput.value.trim());

    const cw = () => cwInput.value.trim();
    previewBtn.onclick = () =>
      runInput(cw(), { preview: true }).catch((e) => log("ERROR: " + e.message));
    runBtn.onclick = () => {
      if (confirm("未返却の全員にシステム点(1〜3点)を下書き入力します(返却はしません)。続行しますか?"))
        runInput(cw(), { preview: false }).catch((e) => log("ERROR: " + e.message));
    };
    delBtn.onclick = () => {
      if (confirm("未返却の生徒の下書き点をすべて削除します(返却済みには触れません)。よろしいですか?") &&
          confirm("本当に削除しますか? この操作で下書きが空に戻ります。"))
        runDeleteAll(cw()).catch((e) => log("ERROR: " + e.message));
    };
  }

  const iv = setInterval(() => { if (document.body) { buildPanel(); clearInterval(iv); } }, 1000);
})();
