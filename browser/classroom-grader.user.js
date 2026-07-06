// ==UserScript==
// @name         Classroom Grader (下書き点入力)
// @namespace    classroom-grading-automation
// @version      1.2
// @description  採点APIから点数を取得し、Classroom成績簿に下書き点を入力する
// @match        https://classroom.google.com/*
// @updateURL    https://raw.githubusercontent.com/kake256/class_room_app/main/browser/classroom-grader.user.js
// @downloadURL  https://raw.githubusercontent.com/kake256/class_room_app/main/browser/classroom-grader.user.js
// @grant        none
// ==/UserScript==

/*
 * 「生徒の提出物」ページ(.../submissions/...)で使う。
 * 点数欄は <span role="button" aria-label="氏名 さんの成績を追加"> で、
 * クリックすると <input aria-label="成績を編集"> が現れる方式に対応。
 *
 * 安全設計:
 *   - 未採点(「成績を追加」)の欄のみ対象。既に点数がある生徒(「成績を編集」)には触れない
 *   - 「返却」ボタンには一切触れない
 *   - 入力後に、その生徒の「成績を追加」ボタンが消えたか(=確定したか)を検証。ダメなら中断
 */

(function () {
  "use strict";

  const DEFAULT_API_BASE = "http://localhost:8800";
  const apiBase = () => localStorage.getItem("cga_api_base") || DEFAULT_API_BASE;
  const apiToken = () => localStorage.getItem("cga_api_token") || "";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // ---- ページDOM依存(壊れたらここを調整) ----
  const SEL = {
    // 未採点の点数欄(氏名入り)。「成績を追加」を含む role=button
    addButton: '[role="button"][aria-label*="成績を追加"]',
    // クリック後に現れる点数入力欄
    editInput: 'input[aria-label="成績を編集"]',
  };

  // 氏名正規化(照合用): 学籍番号プレフィックスと空白を除去
  function normName(s) {
    return (s || "").replace(/AR\d{5}/i, "").replace(/[\s　]/g, "").trim();
  }

  // aria-label「氏名 さんの成績を追加」から氏名部分を取り出して正規化
  function nameFromLabel(label) {
    return normName((label || "").replace(/さんの成績を(追加|編集).*$/, ""));
  }

  async function fetchGrades(cw) {
    const headers = apiToken() ? { "X-API-Key": apiToken() } : {};
    const res = await fetch(`${apiBase()}/grades/${cw}`, { headers });
    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
    return (await res.json()).grades;
  }

  // 未採点の点数ボタンを {name -> ボタン} で集める
  function collectAddButtons() {
    const map = new Map();
    document.querySelectorAll(SEL.addButton).forEach((btn) => {
      const name = nameFromLabel(btn.getAttribute("aria-label"));
      if (name) map.set(name, btn);
    });
    return map;
  }

  function findButton(map, apiName) {
    const t = normName(apiName);
    if (map.has(t)) return map.get(t);
    for (const [name, btn] of map) {
      if (name.includes(t) || t.includes(name)) return btn;
    }
    return null;
  }

  // <input> に値を設定してReactに通知
  function setInputValue(input, value) {
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype, "value").set;
    setter.call(input, String(value));
    input.dispatchEvent(new Event("input", { bubbles: true }));
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }

  // 1人分: ボタンをクリック→現れたinputに入力→Enterで確定→確定検証
  async function setGrade(btn, score, name) {
    btn.click();
    let input = null;
    for (let i = 0; i < 30; i++) {
      input = document.querySelector(SEL.editInput);
      if (input) break;
      await sleep(50);
    }
    if (!input) return false;
    setInputValue(input, score);
    await sleep(120);
    ["keydown", "keyup"].forEach((type) =>
      input.dispatchEvent(new KeyboardEvent(type, {
        key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true,
      })));
    if (input.blur) input.blur();
    await sleep(250);
    // 検証: この生徒の「成績を追加」ボタンが消えていれば確定成功
    const stillAdd = [...document.querySelectorAll(SEL.addButton)]
      .some((b) => nameFromLabel(b.getAttribute("aria-label")) === name);
    return !stillAdd;
  }

  async function run(cw, { preview, includeCandidates }) {
    const grades = await fetchGrades(cw);
    const map = collectAddButtons();
    log(`未採点の点数欄: ${map.size}件 / API: ${grades.length}件` +
        (includeCandidates ? "(3点候補も入力)" : "(3点候補は保留)"));

    // 既定: auto_*(低い点 0/1/2)のみ入力。candidate_3(3点)は保留=TAが確認。
    // review は人間判断待ちなので入れない。チェック時のみ candidate_3 も入力。
    const targets = grades.filter((g) => {
      const c = String(g.category);
      if (c.startsWith("auto_")) return true;
      if (c === "candidate_3" && includeCandidates) return true;
      return false;
    });

    let done = 0, miss = 0, fail = 0;
    for (const g of targets) {
      const btn = findButton(map, g.name);
      if (!btn) { miss++; continue; }   // 既に採点済み or 氏名不一致
      const score = g.score_after_late ?? g.content_score;

      if (preview) {
        btn.style.outline = "2px solid #4a90d9";
        btn.title = `→ ${score}点 (${g.category})`;
        done++;
        continue;
      }
      const ok = await setGrade(btn, score, findKeyName(map, btn));
      btn.style.outline = ok ? "2px solid #4caf50" : "2px solid #e53935";
      if (ok) { done++; } else {
        fail++;
        log(`!! 確定できず: ${g.name}。中断します(既入力分は保持)。`);
        break;
      }
      await sleep(200);
    }
    log(`${preview ? "プレビュー" : "入力"}完了: 対象${targets.length} / ` +
        `${preview ? "予定" : "確定"}${done} / 未照合(採点済み含む)${miss} / 失敗${fail}`);
    if (map.size === 0) log("※未採点の点数欄が0件。全員採点済みか、SEL.addButtonを要調整。");
  }

  function findKeyName(map, btn) {
    for (const [name, b] of map) if (b === btn) return name;
    return nameFromLabel(btn.getAttribute("aria-label"));
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
      "font:12px sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.2);width:280px";

    const title = document.createElement("b");
    title.textContent = "Classroom Grader";
    p.appendChild(title);

    const apiInput = field(p, "API:", "cga-api", "210px", "https://xxx.trycloudflare.com");
    const tokenInput = field(p, "token:", "cga-token", "200px", "api.tokenと同じ値");
    const cwInput = field(p, "courseWorkId:", "cga-cw", "150px", "");

    // 3点候補も入力するかのチェックbox(既定OFF=保留)
    const optRow = document.createElement("label");
    optRow.style.cssText = "display:block;margin-top:6px";
    const cand = document.createElement("input");
    cand.type = "checkbox";
    cand.id = "cga-cand";
    optRow.appendChild(cand);
    optRow.appendChild(document.createTextNode(" 3点候補も入力する(既定は保留)"));
    p.appendChild(optRow);

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
    const opts = (preview) => ({ preview, includeCandidates: cand.checked });
    previewBtn.onclick = () =>
      run(cw(), opts(true)).catch((e) => log("ERROR: " + e.message));
    runBtn.onclick = () => {
      const msg = cand.checked
        ? "未採点の下書き点(3点候補含む)を入力します。続行しますか?"
        : "未採点のうち低い点(0/1/2)のみ入力します(3点候補は保留)。続行しますか?";
      if (confirm(msg))
        run(cw(), opts(false)).catch((e) => log("ERROR: " + e.message));
    };
  }

  const iv = setInterval(() => { if (document.body) { buildPanel(); clearInterval(iv); } }, 1000);
})();
