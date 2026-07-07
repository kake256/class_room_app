// ==UserScript==
// @name         Classroom Grader (0/2点=自動返却・1/3点=下書き)
// @namespace    classroom-grading-automation
// @version      3.1
// @description  採点APIから点数を取得。0/2点は下書き入力+自動返却(下書きのみも可)、1/3点ほかは下書きのみ。全削除も可
// @match        https://classroom.google.com/*
// @updateURL    https://raw.githubusercontent.com/kake256/class_room_app/main/browser/classroom-grader.user.js
// @downloadURL  https://raw.githubusercontent.com/kake256/class_room_app/main/browser/classroom-grader.user.js
// @grant        none
// ==/UserScript==

/*
 * 「生徒の提出物」ページ(.../submissions/...)で使う。
 *
 * 区分(APIの category 列で判定):
 *   - 自動返却組 = auto_0(0点) / auto_2(2点) … 確信度が高い層。下書き入力→その生徒だけ
 *     選択→「返却」まで自動で行う(生徒に成績が公開される・取り消し不可)
 *   - 下書き組   = auto_1(1点) / candidate_3(3点候補) … 教員が目視確認する層。下書き入力のみ
 *   - review     … ゲート不通過/不一致/エラー等。数値点が信頼できないため下書きのみ(返却しない)
 *
 * 動作:
 *   - 0/2点 入力+返却: 自動返却組を下書き入力→チェック選択→返却(完全自動)
 *   - 0/2点 下書き入力: 自動返却組を下書き入力のみ(返却しない・確認してから別途返却したい時)
 *   - 1/3点 下書き入力: 下書き組を未返却の全員に下書き入力(返却しない)
 *   - 下書き全削除: 未返却の生徒の下書き点をまとめて消す(再分析後のやり直し用)
 *
 * 安全設計:
 *   - 返却済み(RETURNED)の生徒には入力も削除も返却も一切触れない(APIの state で判定)
 *   - 返却は「自動返却組(auto_0/auto_2)」だけ。1/3点・review・未対象には触れない
 *   - 返却前ガード: 既にチェック済みの欄があれば中止 / 対象を全員選択できた時だけ返却実行
 *   - 入力: 既に値がある欄は上書きしない(スキップ)
 *   - クリックで開いた入力欄(activeElement)だけを操作(別セルへの誤書き込み防止)
 *   - 各操作後に結果を検証。失敗したら中断
 *
 * !! 返却UIのDOMは未実測の推定値。初回は必ずプレビュー→少人数の課題で動作確認し、
 *    動かなければ SEL.rowCheckbox / SEL.returnButton / SEL.dialog をライブDOMに合わせて調整する。
 */

(function () {
  "use strict";

  const DEFAULT_API_BASE = "http://localhost:8800";
  const apiBase = () => localStorage.getItem("cga_api_base") || DEFAULT_API_BASE;
  const apiToken = () => localStorage.getItem("cga_api_token") || "";
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // 区分の割り当て
  const RETURN_CATS = new Set(["auto_0", "auto_2"]);            // 下書き+自動返却
  // auto_3: 感想文系課題の文章量ボーナスで自動確認済みとなった3点(judge僅差+長文)。
  // 判定自体は人間が確定していないため、3点は下書きのみに留め自動返却はしない(安全側)
  const DRAFT_CATS = new Set(["auto_1", "candidate_3", "auto_3", "review"]); // 下書きのみ
  const ALL_CATS = new Set([...RETURN_CATS, ...DRAFT_CATS]);   // 削除・プレビュー対象

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

  async function fetchGrades(cw) {
    const headers = apiToken() ? { "X-API-Key": apiToken() } : {};
    const res = await fetch(`${apiBase()}/grades/${cw}`, { headers });
    if (!res.ok) throw new Error(`API ${res.status}: ${await res.text()}`);
    return (await res.json()).grades;
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

  // 未返却(TURNED_IN)で点数があり、指定カテゴリに属する生徒を {氏名 -> {score,name,category}} で返す
  function buildTargets(grades, cats) {
    const remaining = new Map();
    grades.forEach((g) => {
      const c = String(g.category);
      const score = g.score_after_late ?? g.content_score;
      const ok = cats.has(c) &&
        score !== null && score !== undefined && g.state === "TURNED_IN";
      if (ok) remaining.set(normName(g.name), { score, name: g.name, category: c });
    });
    return remaining;
  }

  // 下書き入力(preview時は色付けのみ)。緑=自動返却組 / 青=下書きのみ組
  async function inputDrafts(targets, { preview }) {
    return sweep(targets, SEL.addButton, async (btn, t, name) => {
      const color = RETURN_CATS.has(t.category) ? "#4caf50" : "#4a90d9";
      if (preview) {
        btn.style.outline = "2px solid " + color;
        btn.title = `→ ${t.score}点 (${t.category})`;
        return "done";
      }
      const res = await setGrade(btn, t.score, name);
      if (res === "skip") { btn.style.outline = "2px solid #999"; return "skip"; }
      if (res) { btn.style.outline = "2px solid " + color; return "done"; }
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

  async function runPreview(cw) {
    const targets = buildTargets(await fetchGrades(cw), ALL_CATS);
    log(`プレビュー対象: ${targets.size}件`);
    await inputDrafts(targets, { preview: true });
    log("プレビュー: 緑=0/2点(自動返却) / 青=1/3点ほか(下書きのみ)");
  }

  // 下書きのみ投入(返却しない)。cats で対象カテゴリを切替
  async function runDraftInput(cw, cats, label) {
    const targets = buildTargets(await fetchGrades(cw), cats);
    log(`下書き入力対象(${label}・未返却): ${targets.size}件`);
    const r = await inputDrafts(targets, { preview: false });
    log(`下書き入力完了: 確定${r.done}` +
      `${r.skip ? " / スキップ(既存)" + r.skip : ""} / 未照合${r.left} / 失敗${r.fail}`);
    if (r.left > 0) log("※未照合=下書き入力済み or 画面外(自動スクロールで解消)");
  }

  async function runAutoReturn(cw) {
    const targets = buildTargets(await fetchGrades(cw), RETURN_CATS);
    log(`自動返却対象(0/2点・未返却): ${targets.size}件`);
    if (targets.size === 0) { log("対象なし。終了"); return; }

    const names = [...targets.values()].map((t) => t.name);
    const ok = confirm(
      `【自動返却】${targets.size}人(0点/2点)を下書き入力し、その生徒だけを選択して「返却」します。\n` +
      `→ 生徒に成績が公開されます(取り消せません)。\n` +
      `例: ${names.slice(0, 6).join("、")}${names.length > 6 ? " ほか" : ""}\n\n続行しますか?`);
    if (!ok) { log("中止しました"); return; }

    // ① 下書き入力
    log("① 下書き入力中…");
    const ir = await inputDrafts(new Map(targets), { preview: false });
    if (ir.fail) { log("入力に失敗。返却を中止(下書きは保持)"); return; }
    log(`① 完了: 入力${ir.done} / 既存${ir.skip} / 未照合${ir.left}`);

    // ガード: 既にチェックが入っている欄があれば誤返却防止で中止
    const preChecked = [...document.querySelectorAll(SEL.rowCheckbox)].filter(isChecked);
    if (preChecked.length) {
      log(`!! 既に${preChecked.length}件チェック済み。手動で解除してから再実行(誤返却防止)`);
      return;
    }

    // ② 対象の生徒だけ選択
    log("② 対象の生徒だけ選択中…");
    const sr = await selectCheckboxes(new Map(targets));
    if (sr.fail) { log("選択に失敗。返却を中止(何も返却していません)"); return; }
    if (sr.left > 0) {
      log(`!! ${sr.left}人を選択できず。返却を中止(全員選択できた時だけ返却します)`);
      log("   → SEL.rowCheckbox のセレクタをライブDOMに合わせて調整してください");
      return;
    }
    log(`② 完了: ${sr.done + sr.skip}人を選択`);

    // ③ 返却
    if (!confirm(`選択した ${targets.size}人 を返却します。最終確認・よろしいですか?`)) {
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
    log(`✔ 自動返却 完了: ${targets.size}人に返却を実行。画面で「返却済み」を目視確認してください`);
  }

  async function runDeleteAll(cw) {
    const remaining = buildTargets(await fetchGrades(cw), ALL_CATS);
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
      "font:12px sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.2);width:300px";

    const title = document.createElement("b");
    title.textContent = "Classroom Grader v3";
    p.appendChild(title);

    const apiInput = field(p, "API:", "cga-api", "215px", "https://xxx.trycloudflare.com");
    const tokenInput = field(p, "token:", "cga-token", "205px", "api.tokenと同じ値");
    const cwInput = field(p, "courseWorkId:", "cga-cw", "150px", "");

    const btnRow = document.createElement("div");
    btnRow.style.marginTop = "8px";
    const previewBtn = document.createElement("button");
    previewBtn.textContent = "プレビュー";
    const draftBtn = document.createElement("button");
    draftBtn.textContent = "1/3点 下書き入力";
    draftBtn.style.cssText = "color:#1a56a0;margin-left:6px";
    const returnBtn = document.createElement("button");
    returnBtn.textContent = "0/2点 入力+返却";
    returnBtn.style.cssText = "color:#fff;background:#2e7d32;margin-left:6px";
    btnRow.appendChild(previewBtn);
    btnRow.appendChild(draftBtn);
    btnRow.appendChild(returnBtn);
    p.appendChild(btnRow);

    const btnRow2 = document.createElement("div");
    btnRow2.style.marginTop = "6px";
    const draft02Btn = document.createElement("button");
    draft02Btn.textContent = "0/2点 下書き入力";
    draft02Btn.style.cssText = "color:#2e7d32";
    const delBtn = document.createElement("button");
    delBtn.textContent = "下書き全削除";
    delBtn.style.cssText = "color:#fff;background:#b00;margin-left:6px";
    btnRow2.appendChild(draft02Btn);
    btnRow2.appendChild(delBtn);
    p.appendChild(btnRow2);

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
      runPreview(cw()).catch((e) => log("ERROR: " + e.message));
    draftBtn.onclick = () => {
      if (confirm("1/3点ほか(auto_1/candidate_3/auto_3/review)を未返却の全員に下書き入力します(返却しません)。続行しますか?"))
        runDraftInput(cw(), DRAFT_CATS, "1/3点ほか").catch((e) => log("ERROR: " + e.message));
    };
    draft02Btn.onclick = () => {
      if (confirm("0/2点(auto_0/auto_2)を未返却の全員に下書き入力します(返却しません)。続行しますか?"))
        runDraftInput(cw(), RETURN_CATS, "0/2点").catch((e) => log("ERROR: " + e.message));
    };
    returnBtn.onclick = () =>
      runAutoReturn(cw()).catch((e) => log("ERROR: " + e.message));
    delBtn.onclick = () => {
      if (confirm("未返却の生徒の下書き点をすべて削除します(返却済みには触れません)。よろしいですか?") &&
        confirm("本当に削除しますか? この操作で下書きが空に戻ります。"))
        runDeleteAll(cw()).catch((e) => log("ERROR: " + e.message));
    };
  }

  const iv = setInterval(() => { if (document.body) { buildPanel(); clearInterval(iv); } }, 1000);
})();
