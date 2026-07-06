// ==UserScript==
// @name         Classroom Grader (下書き点入力)
// @namespace    classroom-grading-automation
// @version      1.5
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

  // スクロール可能なリスト容器を探す(仮想スクロール対応)
  function scrollContainer() {
    const btn = document.querySelector(SEL.addButton);
    let el = btn ? btn.parentElement : null;
    while (el && el !== document.body) {
      const oy = getComputedStyle(el).overflowY;
      if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight + 20) return el;
      el = el.parentElement;
    }
    return document.scrollingElement || document.documentElement;
  }

  // 表示中ボタン名 bname に対応する残りターゲットのキーを返す(完全一致→部分一致)
  function takeMatch(remaining, bname) {
    if (remaining.has(bname)) return bname;
    for (const key of remaining.keys()) {
      if (key && (key.includes(bname) || bname.includes(key))) return key;
    }
    return null;
  }

  async function run(cw, { preview, includeCandidates, includeReview }) {
    const grades = await fetchGrades(cw);
    const inc = [];
    if (includeCandidates) inc.push("3点候補");
    if (includeReview) inc.push("review");

    // 既定: auto_*(低い点 0/1/2)のみ。チェック時のみ candidate_3 / review も含める。
    const targets = grades.filter((g) => {
      const c = String(g.category);
      if (c.startsWith("auto_")) return true;
      if (c === "candidate_3" && includeCandidates) return true;
      if (c === "review" && includeReview) return true;
      return false;
    });
    // 残り: normName -> {score, name}
    const remaining = new Map();
    targets.forEach((g) => remaining.set(normName(g.name),
      { score: g.score_after_late ?? g.content_score, name: g.name }));
    log(`API対象: ${targets.length}件` + (inc.length ? `(低い点+${inc.join("+")})` : "(低い点のみ)"));

    // 仮想スクロール対策: リストを上から下までスクロールしながら、表示された行を順次処理
    const cont = scrollContainer();
    if (cont) { cont.scrollTop = 0; await sleep(400); }
    let done = 0, fail = 0, stagnant = 0;
    for (let pass = 0; pass < 400 && remaining.size > 0 && !fail; pass++) {
      for (const [bname, btn] of collectAddButtons()) {
        const key = takeMatch(remaining, bname);
        if (!key) continue;
        const t = remaining.get(key);
        if (preview) {
          btn.style.outline = "2px solid #4a90d9";
          btn.title = `→ ${t.score}点`;
          remaining.delete(key); done++;
        } else {
          const ok = await setGrade(btn, t.score, bname);
          btn.style.outline = ok ? "2px solid #4caf50" : "2px solid #e53935";
          if (ok) { remaining.delete(key); done++; }
          else { fail++; log(`!! 確定できず: ${t.name}。中断(既入力分は保持)。`); break; }
        }
      }
      if (fail || !cont) break;
      const before = cont.scrollTop;
      cont.scrollTop = before + Math.max(200, cont.clientHeight * 0.8);
      await sleep(450);
      if (cont.scrollTop <= before + 2) { if (++stagnant >= 2) break; } else stagnant = 0;
    }
    log(`${preview ? "プレビュー" : "入力"}完了: ${preview ? "予定" : "確定"}${done} / ` +
        `未照合${remaining.size} / 失敗${fail}`);
    if (remaining.size > 0) {
      const names = [...remaining.values()].slice(0, 8).map((v) => v.name).join(", ");
      log(`未照合(採点済み or 表示外): ${names}${remaining.size > 8 ? " ほか" : ""}`);
    }
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

    // 入力対象を広げるチェックbox(既定OFF=低い点のみ)
    function checkbox(labelText, id) {
      const row = document.createElement("label");
      row.style.cssText = "display:block;margin-top:6px";
      const box = document.createElement("input");
      box.type = "checkbox";
      box.id = id;
      row.appendChild(box);
      row.appendChild(document.createTextNode(" " + labelText));
      p.appendChild(row);
      return box;
    }
    const cand = checkbox("3点候補も入力する(既定は保留)", "cga-cand");
    const rev = checkbox("review(要確認)も入力する", "cga-rev");

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
    const opts = (preview) => ({
      preview, includeCandidates: cand.checked, includeReview: rev.checked,
    });
    previewBtn.onclick = () =>
      run(cw(), opts(true)).catch((e) => log("ERROR: " + e.message));
    runBtn.onclick = () => {
      const extra = [];
      if (cand.checked) extra.push("3点候補");
      if (rev.checked) extra.push("review");
      const msg = extra.length
        ? `未採点の下書き点(低い点 + ${extra.join("+")})を入力します。続行しますか?`
        : "未採点のうち低い点(0/1/2)のみ入力します(3点候補・reviewは保留)。続行しますか?";
      if (confirm(msg))
        run(cw(), opts(false)).catch((e) => log("ERROR: " + e.message));
    };
  }

  const iv = setInterval(() => { if (document.body) { buildPanel(); clearInterval(iv); } }, 1000);
})();
