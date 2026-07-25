// ==UserScript==
// @name         Classroom Grader (確定=入力+返却・全員=下書きのみ)
// @namespace    classroom-grading-automation
// @version      5.0
// @description  コース別採点APIから保護済み実点数を取得し、人間採点を上書きせず入力
// @match        https://classroom.google.com/*
// @updateURL    https://raw.githubusercontent.com/kake256/class_room_app/main/browser/classroom-grader.user.js
// @downloadURL  https://raw.githubusercontent.com/kake256/class_room_app/main/browser/classroom-grader.user.js
// @grant        none
// ==/UserScript==

/*
 * 「生徒の提出物」ページ(.../submissions/...)で使う。
 *
 * 区分(APIの category 列で判定):
 *   - 確定組 = auto_0(0点) / auto_1(1点) / auto_2(2点) / auto_3(3点・システム確認済み)
 *     … 要確認(要レビュー)ではない層。下書き入力→その生徒だけ選択→「返却」まで自動
 *   - 要確認組 = candidate_3(3点候補・要確認) / review(要レビュー)
 *     … 教員が目視確認する層。下書き入力のみ(返却しない)
 *
 * 動作:
 *   - 確定のみ入力: 確定組を下書き入力→チェック選択→返却(完全自動)
 *   - 全て下書き: 全員(確定組+要確認組)を未返却の全員に下書き入力(返却しない)
 *   - 下書き全削除: 未返却の生徒の下書き点をまとめて消す(再分析後のやり直し用)
 *
 * 安全設計:
 *   - 返却済み(RETURNED)の生徒には入力も削除も返却も一切触れない(APIの state で判定)
 *   - 返却は「確定組(auto_0/1/2/3)」だけ。candidate_3・reviewには返却しない
 *   - 返却前ガード: 既にチェック済みの欄があれば中止 / 対象を全員選択できた時だけ返却実行
 *   - 入力: 既に値がある欄は上書きしない(スキップ)
 *   - クリックで開いた入力欄(activeElement)だけを操作(別セルへの誤書き込み防止)
 *   - 各操作後に結果を検証。失敗したら中断
 *
 * !! 返却UIのDOMは未実測の推定値。初回は必ず少人数の課題で動作確認し、
 *    動かなければ SEL.rowCheckbox / SEL.returnButton / SEL.dialog をライブDOMに合わせて調整する。
 */

(function () {
  "use strict";

  const DEFAULT_API_BASE = "http://localhost:8800";
  const apiBase = () => localStorage.getItem("cga_api_base") || DEFAULT_API_BASE;
  const apiToken = () => localStorage.getItem("cga_api_token") || "";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // 区分の割り当て
  const RETURN_CATS = new Set(["auto_0", "auto_1", "auto_2", "auto_3"]); // 下書き+自動返却(確定組)
  const REVIEW_CATS = new Set(["candidate_3", "review"]);                // 下書きのみ(要確認組)
  const ALL_CATS = new Set([...RETURN_CATS, ...REVIEW_CATS]);           // 全員(下書き/削除の対象)

  // ---- ページDOM依存(壊れたらここを調整) ----
  const SEL = {
    // 未採点の点数欄(氏名入り)
    addButton: '[role="button"][aria-label*="成績を追加"]',
    // 点数欄全般(未採点・下書きあり両方。氏名+「さんの成績」を含む)
    anyGradeButton: '[role="button"][aria-label*="さんの成績"]',
    // クリック後に現れる点数入力欄
    editInput: 'input[aria-label="成績を編集"]',
    // !! 以下は返却UI用・未実測の推定。ライブDOMで要調整 --------------------
    // 生徒行の選択チェックボックス(氏名を aria-label に含む想定)
    rowCheckbox: '[role="checkbox"][aria-label*="さん"]',
    // 選択後に有効化される「返却」ボタン
    returnButton: '[role="button"][aria-label*="返却"]',
    // 返却確認ダイアログ
    dialog: '[role="dialog"], [role="alertdialog"]',
  };

  // 氏名正規化(照合用): 学籍番号プレフィックスと空白を除去
  function normName(s) {
    return (s || "").replace(/AR\d{5}/i, "").replace(/[\s　]/g, "").trim();
  }

  // aria-label「氏名 さんの成績を追加/編集/…」から氏名部分を取り出して正規化
  function nameFromLabel(label) {
    return normName((label || "").replace(/さんの成績.*$/, ""));
  }

  // チェックボックスの aria-label(「氏名 さんを選択」等の想定)から氏名を取り出す
  function nameFromCheckbox(label) {
    return normName((label || "").replace(/(さん).*$/, "").replace(/(を選択|を選ぶ|選択|の提出物).*$/, ""));
  }

  function isChecked(el) {
    return el.getAttribute("aria-checked") === "true" || el.checked === true;
  }

  // APIから採点結果一式を取得。{grades, max_points, ...} を返す
  async function fetchGrades(course, cw) {
    const headers = apiToken() ? { "X-API-Key": apiToken() } : {};
    const res = await fetch(`${apiBase()}/grades/${course}/${cw}`, { headers });
    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
    return await res.json();
  }

  // セレクタに合う要素を {正規化氏名 -> 要素} で集める(nameFn で氏名抽出方法を切替)
  function collectByName(selector, nameFn) {
    const map = new Map();
    document.querySelectorAll(selector).forEach((el) => {
      const name = nameFn(el.getAttribute("aria-label"));
      if (name) map.set(name, el);
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

  // 表示中の行を処理→最後の要素をscrollIntoViewで送る→繰り返し(仮想スクロール対応)
  async function sweep(remaining, selector, handler, nameFn = nameFromLabel) {
    let done = 0, skip = 0, fail = 0, stagnant = 0, lastSig = "";
    for (let pass = 0; pass < 120 && remaining.size > 0 && !fail && stagnant < 3; pass++) {
      let processed = 0;
      for (const [name, el] of collectByName(selector, nameFn)) {
        if (!remaining.has(name)) continue;
        const t = remaining.get(name);
        const res = await handler(el, t, name);
        if (res === "fail") { fail++; break; }
        if (res === "skip") skip++;
        else if (res === "done") done++;
        remaining.delete(name);
        processed++;
      }
      if (fail) break;
      const els = document.querySelectorAll(selector);
      const sig = els.length + "|" +
        (els.length ? els[els.length - 1].getAttribute("aria-label") : "");
      if (els.length) els[els.length - 1].scrollIntoView({ block: "center" });
      await sleep(500);
      if (processed === 0 && sig === lastSig) stagnant++; else stagnant = 0;
      lastSig = sig;
    }
    return { done, skip, fail, left: remaining.size };
  }

  // 未返却(TURNED_IN)で点数があり、指定カテゴリに属する生徒を {氏名 -> {score,name,category,held}} で返す
  // held=true: demoted_from_3(3点評価からの降格)フラグ付き。下書きは入れるが自動返却しない
  // maxPoints: 課題の満点。3以外(100点/5点等)は3点スケールから換算して入力する
  function buildTargets(grades, cats, maxPoints) {
    const remaining = new Map();
    const scale = maxPoints && maxPoints !== 3;
    grades.forEach((g) => {
      const c = String(g.category);
      const absolute = g.mapped_score;
      const raw = absolute ?? g.score_after_late ?? g.content_score;
      const ok = cats.has(c) &&
        raw !== null && raw !== undefined && g.state === "TURNED_IN";
      const score = absolute !== null && absolute !== undefined ? absolute :
        (scale ? Math.round((raw / 3) * maxPoints) : raw);
      const held = String(g.flags || "").includes("demoted_from_3");
      if (ok) remaining.set(normName(g.name), { score, name: g.name, category: c, held });
    });
    return remaining;
  }

  // 満点が3以外なら換算中である旨をログに出す
  function logScaleInfo(maxPoints) {
    if (maxPoints && maxPoints !== 3) {
      log(`この課題は${maxPoints}点満点: 3点スケールから換算して入力します(例: 2点→${Math.round(2 / 3 * maxPoints)}点)`);
    }
  }

  // 下書き入力
  async function inputDrafts(targets) {
    return sweep(targets, SEL.addButton, async (btn, t, name) => {
      const res = await setGrade(btn, t.score, name);
      if (res === "skip") { btn.style.outline = "2px solid #999"; return "skip"; }
      if (res) { btn.style.outline = "2px solid #4caf50"; return "done"; }
      btn.style.outline = "2px solid #e53935";
      log(`!! 確定できず: ${t.name}。中断(既入力分は保持)`);
      return "fail";
    });
  }

  // 対象の生徒だけチェックを入れる(仮想スクロール対応)
  async function selectCheckboxes(targets) {
    return sweep(targets, SEL.rowCheckbox, async (cb, t) => {
      if (isChecked(cb)) { cb.style.outline = "2px solid #4caf50"; return "skip"; }
      cb.click();
      await sleep(120);
      if (isChecked(cb)) { cb.style.outline = "2px solid #4caf50"; return "done"; }
      cb.style.outline = "2px solid #e53935";
      log(`!! 選択できず: ${t.name}`);
      return "fail";
    }, nameFromCheckbox);
  }

  // 「返却」ボタン→確認ダイアログの「返却」を押す。ダイアログが閉じれば成功
  async function clickReturn() {
    const btn = document.querySelector(SEL.returnButton) ||
      [...document.querySelectorAll('[role="button"],button')].find((b) => {
        const a = (b.getAttribute("aria-label") || "") + " " + (b.textContent || "");
        return /返却/.test(a) && b.offsetParent !== null &&
          b.getAttribute("aria-disabled") !== "true";
      });
    if (!btn) return false;
    btn.click();
    await sleep(700);
    const dlg = document.querySelector(SEL.dialog);
    if (dlg) {
      const confirmBtn = [...dlg.querySelectorAll('[role="button"],button')]
        .find((b) => /返却/.test(b.textContent || "") && b.getAttribute("aria-disabled") !== "true");
      if (!confirmBtn) return false;
      confirmBtn.click();
      await sleep(900);
    }
    return !document.querySelector(SEL.dialog);
  }

  // 下書きのみ投入(返却しない)。対象は全員(確定組+要確認組)
  async function runDraftAll(course, cw) {
    const data = await fetchGrades(course, cw);
    logScaleInfo(data.max_points);
    const targets = buildTargets(data.grades, ALL_CATS, data.max_points);
    log(`下書き入力対象(全員・未返却): ${targets.size}件`);
    const r = await inputDrafts(targets);
    log(`下書き入力完了: 確定${r.done}` +
      `${r.skip ? " / スキップ(既存)" + r.skip : ""} / 未照合${r.left} / 失敗${r.fail}`);
    if (r.left > 0) log("※未照合=下書き入力済み or 画面外(自動スクロールで解消)");
  }

  // 確定組(auto_0/1/2/3)のみ下書き入力→選択→返却(完全自動)
  // demoted_from_3フラグ付き(held)は下書きのみ入れ、返却対象から外す(満点取り逃がしの抜き取り確認用)
  async function runConfirmedReturn(course, cw) {
    const data = await fetchGrades(course, cw);
    logScaleInfo(data.max_points);
    const targets = buildTargets(data.grades, RETURN_CATS, data.max_points);
    const heldTargets = new Map([...targets].filter(([, t]) => t.held));
    const returnTargets = new Map([...targets].filter(([, t]) => !t.held));
    log(`確定対象(要確認以外・未返却): ${targets.size}件` +
      (heldTargets.size ? `(うち${heldTargets.size}件はdemoted_from_3のため下書きのみ・返却保留)` : ""));
    if (targets.size === 0) { log("対象なし。終了"); return; }

    const names = [...returnTargets.values()].map((t) => t.name);
    const ok = confirm(
      `【確定のみ入力】${targets.size}人(要確認以外)を下書き入力し、うち${returnTargets.size}人を選択して「返却」します。\n` +
      (heldTargets.size ? `※${heldTargets.size}人は3点評価からの降格(demoted_from_3)のため下書きのみ入れ、返却しません。\n` : "") +
      `→ 生徒に成績が公開されます(取り消せません)。\n` +
      `例: ${names.slice(0, 6).join("、")}${names.length > 6 ? " ほか" : ""}\n\n続行しますか?`);
    if (!ok) { log("中止しました"); return; }

    // ① 下書き入力(保留分も含めて全員に入れる)
    log("① 下書き入力中…");
    const ir = await inputDrafts(new Map(targets));
    if (ir.fail) { log("入力に失敗。返却を中止(下書きは保持)"); return; }
    log(`① 完了: 入力${ir.done} / 既存${ir.skip} / 未照合${ir.left}`);
    if (heldTargets.size) {
      log(`返却保留(要抜き取り確認): ${[...heldTargets.values()].map((t) => t.name).join("、")}`);
    }
    if (returnTargets.size === 0) { log("返却対象なし(全員保留)。下書きのみで終了"); return; }

    // ガード: 既にチェックが入っている欄があれば誤返却防止で中止
    const preChecked = [...document.querySelectorAll(SEL.rowCheckbox)].filter(isChecked);
    if (preChecked.length) {
      log(`!! 既に${preChecked.length}件チェック済み。手動で解除してから再実行(誤返却防止)`);
      return;
    }

    // ② 対象の生徒だけ選択(返却保留分は選択しない)
    log("② 対象の生徒だけ選択中…");
    const sr = await selectCheckboxes(new Map(returnTargets));
    if (sr.fail) { log("選択に失敗。返却を中止(何も返却していません)"); return; }
    if (sr.left > 0) {
      log(`!! ${sr.left}人を選択できず。返却を中止(全員選択できた時だけ返却します)`);
      log("   → SEL.rowCheckbox のセレクタをライブDOMに合わせて調整してください");
      return;
    }
    log(`② 完了: ${sr.done + sr.skip}人を選択`);

    // ③ 返却
    if (!confirm(`選択した ${returnTargets.size}人 を返却します。最終確認・よろしいですか?`)) {
      log("返却を中止(選択は残っています。手動で解除してください)");
      return;
    }
    log("③ 「返却」を実行中…");
    const returned = await clickReturn();
    if (!returned) {
      log("!! 返却ボタン/ダイアログを操作できず。返却されていません。");
      log("   → SEL.returnButton / SEL.dialog をライブDOMに合わせて調整してください");
      return;
    }
    log(`✔ 確定完了: ${returnTargets.size}人に返却を実行。画面で「返却済み」を目視確認してください` +
      (heldTargets.size ? ` / ${heldTargets.size}人は下書きのみ(返却保留)` : ""));
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
      "font:12px sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.2);width:300px";

    const title = document.createElement("b");
    title.textContent = "Classroom Grader v5";
    p.appendChild(title);

    const apiInput = field(p, "API:", "cga-api", "215px", "https://xxx.trycloudflare.com");
    const tokenInput = field(p, "token:", "cga-token", "205px", "integration.tokenと同じ値");
    const courseInput = field(p, "courseId:", "cga-course", "150px", "");
    const cwInput = field(p, "courseWorkId:", "cga-cw", "150px", "");

    const btnRow = document.createElement("div");
    btnRow.style.marginTop = "8px";
    const draftAllBtn = document.createElement("button");
    draftAllBtn.textContent = "全て下書き";
    draftAllBtn.style.cssText = "color:#1a56a0";
    const confirmBtn = document.createElement("button");
    confirmBtn.textContent = "確定のみ入力";
    confirmBtn.style.cssText = "color:#fff;background:#2e7d32;margin-left:6px";
    btnRow.appendChild(draftAllBtn);
    btnRow.appendChild(confirmBtn);
    p.appendChild(btnRow);

    const safety = document.createElement("p");
    safety.textContent = "人間が入力した下書きを保護するため、一括削除機能は無効です。";
    p.appendChild(safety);

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

    const course = () => courseInput.value.trim();
    const cw = () => cwInput.value.trim();
    draftAllBtn.onclick = () => {
      if (confirm("未返却の全員(要確認含む)に下書き入力します(返却しません)。続行しますか?"))
        runDraftAll(course(), cw()).catch((e) => log("ERROR: " + e.message));
    };
    confirmBtn.onclick = () =>
      runConfirmedReturn(course(), cw()).catch((e) => log("ERROR: " + e.message));
  }

  const iv = setInterval(() => { if (document.body) { buildPanel(); clearInterval(iv); } }, 1000);
})();
