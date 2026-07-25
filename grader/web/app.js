"use strict";
const $=id=>document.getElementById(id); let rows=[]; let selectedCourse="";let selectedCourseId="";let courses=[]; let pollTimer; let notice="";let modelInfo={model:null,role:"none"};let session={logged_in:false,csrf_token:null};
async function api(path,options={}){options.credentials="same-origin";options.headers={...(options.headers||{})};const method=(options.method||"GET").toUpperCase();if(!["GET","HEAD","OPTIONS"].includes(method)&&session.csrf_token)options.headers["X-CSRF-Token"]=session.csrf_token;const r=await fetch(path,options);if(!r.ok){let d;try{d=await r.json()}catch{d={detail:r.statusText}}throw new Error(d.detail||`HTTP ${r.status}`)}return r}
const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n};
function cards(el,data){el.replaceChildren();Object.entries(data).forEach(([k,v])=>{const c=node("div",undefined,"card");c.append(node("strong",k),node("div",v));el.append(c)})}

// 旧reportのcategoryは読めるが、UIではすべてAI提案として扱い自動確定しない。
const CAT_LABELS={auto_0:"AI提案 0点",auto_1:"AI提案 1点",auto_2:"AI提案 2点",auto_3:"AI提案 3点",candidate_3:"AI提案 3点",review:"AI要再確認",not_submitted:"未提出"};
const PHASE_LABELS={prepare:"答案準備",run:"一次採点",refine:"再チェック",report:"集計",full:"AI採点案を作成"};
const STEP_LABELS={"model:run":"一次モデルを準備中","model:refine":"審判モデルを準備中",prepare:"答案取得中",run:"一次採点中",refine:"再チェック中",report:"集計中"};
const ROLE_LABELS={primary:"一次採点用",judge:"審判用",unknown:"不明なモデル",none:"停止中"};
const isHeld=r=>String(r.flags||"").includes("demoted_from_3")&&String(r.category||"").startsWith("auto_");

// 疑問答案の優先表示。数値が大きいほど先に確認する。自己申告の確信度だけに
// 頼らず、独立照合(evidence_verification_status)とモデル間不一致を重く見る。
const RISK_LABELS={
  evidence_not_verified:"根拠が本文に見つからない",
  evidence_not_verified_visual:"根拠照合が未実施(画像)",
  evidence_too_short:"根拠が短すぎる",
  visual_review_required:"図表の確認が必要",
  large_model_disagreement:"モデル間で2段階以上の差",
  ocr_quality_low:"OCR品質が低い",
  item_parse_recovered:"出力形式の異常から復旧",
  low_confidence:"確信度が低い",
  score_rubric_mismatch:"点数と基準レベルの不一致",
  boundary:"隣接レベルとの境界",
  has_visual_material:"図表あり",
  batch_retry_used:"バッチ再試行あり",
  q25_visual_max_score:"画像答案の最高点提案（過大評価傾向あり・優先確認）",
};
const STATE_LABELS={
  ready_for_human_review:"確認可",
  model_review_pending:"再確認待ち",
  model_review_unresolved:"再確認でも未解決",
  model_review_failed:"再確認に失敗",
  primary_saved:"一次のみ",
};
// 実質的な警告だけを重み付けする。has_visual_material と
// evidence_not_verified_visual は画像答案なら常に立つため、リスクスコア・
// 「疑問答案のみ表示」・Qwen3ルーティングのいずれにも使わない(情報バッジのみ)。
const RISK_WEIGHTS={
  evidence_not_verified:40,large_model_disagreement:35,visual_review_required:30,
  evidence_too_short:25,ocr_quality_low:18,
  item_parse_recovered:15,low_confidence:12,score_rubric_mismatch:6,
  boundary:4,batch_retry_used:2,
  // 実測(画像答案92件): P(人間点<最高点|Qwen2.5最高点)=0.75、再現率0.74。
  // テキスト答案では0.14しかないため画像答案限定の複合条件とする。
  q25_visual_max_score:20,
};
// スコアに寄与しない情報バッジ(表示のみ)
const INFO_BADGES=new Set(["has_visual_material","evidence_not_verified_visual"]);
// Qwen2.5が画像答案へ最高点を提案し、まだ人間が確認していない答案。
// 確認順の優先度・バッジ・件数表示にのみ使う。automatic_eligible、
// proposal_state、Qwen3ルーティング、点数の自動減点には影響させない。
const TOP_INTERNAL_SCORE=3;
// 必ず一次採点(primary_result)から判定する。統合後の最終点(content_score)や
// review_scoreはreview_wins適用後の値であり、Qwen2.5のバイアス検出には使えない。
// primary_resultが無い行(旧systemパイプライン等)ではフラグを立てない。
const isQ25VisualMaxScore=r=>{
  const primary=r.primary_result;
  if(!primary)return false;
  return /Qwen2\.5/.test(String(primary.model||""))
    && Number(primary.internal_score)===TOP_INTERNAL_SCORE
    && r.teacher_status!=="confirmed"
    && [...(r.review_reasons||[]),...(r.remaining_validation_reasons||[])].includes("has_visual_material");
};
// 飽和判定: 未確認の画像答案に対しフラグ率が高すぎる、または非フラグ群が
// 小さすぎる場合、このシグナルは行の選別能力を持たない(全件フラグに退化)。
// 実測例: 距離計算課題では24件中23件(95.8%)にフラグが立ち、非フラグ群はn=1。
// その場合は行単位のリスク重みを0にし、課題単位の警告と情報バッジだけ残す。
// これは表示制御のみで、自動減点・自動ルーティング・automatic_eligible・
// proposal_stateには影響させない。
// 原則: 未確認画像答案が十分あり(>=10)、かつフラグ率が0.90以上のときだけ飽和。
// 非フラグ件数だけで判定すると小規模課題を過剰に無効化するため、必ずフラグ率
// 条件と組み合わせる(0.80以上 かつ 非フラグ3件未満)。
const FLAG_SATURATION_RATE=0.90;
const MIN_VISUAL_FOR_SATURATION=10;
const SECONDARY_SATURATION_RATE=0.80;
const SECONDARY_MIN_UNFLAGGED=3;
let visualMaxSaturated=false;
function evaluateVisualMaxSaturation(){
  const pending=rows.filter(r=>r.teacher_status!=="confirmed"
    && [...(r.review_reasons||[]),...(r.remaining_validation_reasons||[])]
        .includes("has_visual_material"));
  const flagged=pending.filter(isQ25VisualMaxScore).length;
  const unflagged=pending.length-flagged;
  const rate=pending.length?flagged/pending.length:0;
  const primary=pending.length>=MIN_VISUAL_FOR_SATURATION&&rate>=FLAG_SATURATION_RATE;
  const secondary=rate>=SECONDARY_SATURATION_RATE&&unflagged<SECONDARY_MIN_UNFLAGGED;
  visualMaxSaturated=pending.length>0&&(primary||secondary);
  return {pending:pending.length,flagged,unflagged,rate:Number(rate.toFixed(4)),
          saturated:visualMaxSaturated,by:primary?"rate_and_volume":(secondary?"few_unflagged":null)};
}
const allSignals=r=>{
  const seen=new Set();
  for(const name of [...(r.remaining_validation_reasons||[]),...(r.review_reasons||[])])seen.add(name);
  if(r.evidence_verification_status==="not_found")seen.add("evidence_not_verified");
  if(r.evidence_verification_status==="not_run_visual")seen.add("evidence_not_verified_visual");
  if(r.evidence_verification_status==="too_short")seen.add("evidence_too_short");
  if(Math.abs(Number(r.score_delta)||0)>=2)seen.add("large_model_disagreement");
  if(isQ25VisualMaxScore(r))seen.add("q25_visual_max_score");
  return seen;
};
// 実質的な警告のみ(スコアと絞り込みに使う)
const riskReasons=r=>[...allSignals(r)].filter(name=>!INFO_BADGES.has(name))
  .sort((a,b)=>(RISK_WEIGHTS[b]||0)-(RISK_WEIGHTS[a]||0));
// 情報バッジのみ(表示専用)
const infoBadges=r=>[...allSignals(r)].filter(name=>INFO_BADGES.has(name));
function riskScore(r){
  if(r.teacher_status==="confirmed")return -1;  // 教員が確認済みなら優先度なし
  let score=riskReasons(r).reduce((total,name)=>{
    // 飽和時はq25_visual_max_scoreを行の優先度へ加算しない(表示は残す)
    if(name==="q25_visual_max_score"&&visualMaxSaturated)return total;
    return total+(RISK_WEIGHTS[name]||0);
  },0);
  if(r.proposal_state==="model_review_unresolved")score+=30;
  if(r.proposal_state==="model_review_failed")score+=25;
  if(r.proposal_state==="model_review_pending")score+=10;
  if(r.automatic_eligible===false&&r.proposal_state)score+=5;
  return score;
}
const RISK_THRESHOLD=12;

// タブ切り替え。採点パネルは2ブロックに分かれているため両方を制御する。
const TAB_PANELS={grading:["panel-grading","panel-grading-2","panel-grading-3"],
                  mcp:["panel-mcp"],jobs:["panel-jobs"],extension:["panel-extension"]};
function selectTab(name){
  for(const [key,ids] of Object.entries(TAB_PANELS)){
    const active=key===name;
    const tab=$(`tab-${key}`);
    if(tab){tab.setAttribute("aria-selected",String(active));tab.classList.toggle("active",active);}
    for(const id of ids){const panel=$(id);if(panel)panel.hidden=!active;}
  }
  try{localStorage.setItem("cga-active-tab",name)}catch{}
}

function renderSession(){const badge=$("login-badge"),login=$("google-login"),reconnect=$("google-reconnect"),logout=$("logout"),sheets=$("sheets-scope-warning"),rankingExport=$("ranking-export");if(session.logged_in){badge.textContent="ログイン済み";badge.className="badge connected";$("login-label").textContent="Google Classroom接続済み";$("login-email").textContent=`${session.email||""} / 権限: ${session.role||"grader"}`;login.hidden=true;reconnect.hidden=Boolean(session.classroom_oauth?.sheets_scope_granted);reconnect.disabled=false;logout.hidden=false;sheets.textContent=session.classroom_oauth?.sheets_scope_granted?"Google Sheets出力権限: 接続済み":"Google Sheetsへ出力するには「Google権限を再接続」で追加権限への同意が必要です。";rankingExport.disabled=session.role==="viewer"||!session.classroom_oauth?.sheets_scope_granted;$("app-content").hidden=false}else{badge.textContent="未ログイン";badge.className="badge warning";$("login-label").textContent="Googleログインが必要です";$("login-email").textContent="";login.hidden=false;login.disabled=!session.classroom_oauth?.credentials_valid;reconnect.hidden=true;logout.hidden=true;sheets.textContent="";rankingExport.disabled=true;$("app-content").hidden=true}}

async function connectGoogle(){const buttons=[$("google-login"),$("google-reconnect")];buttons.forEach(button=>button.disabled=true);$("message").textContent="Google認証を開始しています…";try{const d=await api("/api/v1/auth/google/start",{method:"POST"}).then(r=>r.json());const popup=window.open(d.authorization_url,"classroom-oauth","popup,width=560,height=720");if(!popup)window.location.assign(d.authorization_url);else $("message").textContent="Googleの画面でアクセスを許可してください。"}catch(e){$("message").textContent=e.message;buttons.forEach(button=>button.disabled=false)}}

async function logout(){try{await api("/api/v1/auth/logout",{method:"POST"});session={logged_in:false,csrf_token:null};$("message").textContent="ログアウトしました。";renderSession()}catch(e){$("message").textContent=e.message}}

function handleOAuthReturn(){const q=new URLSearchParams(location.search),result=q.get("oauth");if(!result)return;if(window.opener&&window.opener!==window){window.opener.postMessage({type:"classroom-oauth",result,message:q.get("message")||""},location.origin);window.close();return}notice=result==="success"?"Google Classroomに接続しました。":(q.get("message")||"Google認証に失敗しました。");history.replaceState({},"",location.pathname)}

async function refresh(){$("message").textContent="確認中…";try{session=await api("/api/v1/auth/session").then(r=>r.json());renderSession();if(!session.logged_in){$("message").textContent=notice||(session.classroom_oauth?.credentials_valid?"Googleでログインしてください。":"OAuthクライアント設定を管理者へ確認してください。");notice="";return}const [s,j,m]=await Promise.all([api("/api/v1/status").then(r=>r.json()),api("/api/v1/jobs").then(r=>r.json()),api("/api/v1/model").then(r=>r.json()).catch(()=>({model:null,role:"none"}))]);modelInfo=m;cards($("status"),{"API":s.status,"GPUのモデル":`${ROLE_LABELS[m.role]||m.role}${m.model?` (${m.model.split("/").pop()})`:""}`,"実行中ジョブ":s.active_jobs.length});renderJobs(j.jobs);await loadMcpTokens();await loadDevices();await loadCourses();if(selectedCourseId)await loadRanking();$("message").textContent=notice;notice=""}catch(e){$("message").textContent=e.message}}

async function loadCourses(){try{const d=await api("/api/v1/courses").then(r=>r.json());courses=d.courses||[];const sel=$("course-select");sel.replaceChildren(new Option("担当コースを選択してください",""),...courses.map(c=>new Option(`${c.name}${c.section?` (${c.section})`:""}`,c.id)));const saved=localStorage.getItem("cga-selected-course");if(saved&&courses.some(c=>c.id===saved))sel.value=saved;else if(courses.length===1)sel.value=courses[0].id;selectedCourseId=sel.value;if(selectedCourseId)await loadCourseworks();else{$("courses").replaceChildren();$("course-error").textContent=courses.length?"担当コースを選択してください。":"教師として参加中のACTIVEコースがありません。"}}catch(e){$("course-error").textContent=e.message}}

// 一番上のクイック操作へ課題一覧を反映する(課題カードを探さず取得・採点できる)
let courseworkIndex=[];
function renderQuickCourseworkOptions(){
  const select=$("quick-coursework");
  if(!select)return;
  const previous=select.value;
  select.replaceChildren();
  if(!courseworkIndex.length){
    select.append(node("option","コースを選択してください"));
    select.firstChild.value="";
    return;
  }
  const placeholder=node("option","課題を選択");placeholder.value="";select.append(placeholder);
  for(const c of courseworkIndex){
    const option=node("option",`${c.title||c.id}${c.configured?"":"（採点基準 未設定）"}`);
    option.value=c.id;
    select.append(option);
  }
  if(previous&&courseworkIndex.some(c=>c.id===previous))select.value=previous;
  syncQuickButtons();
}
// 実行できない操作はボタン自体を無効化し、理由をtitleで示す
function syncQuickButtons(){
  const prepare=$("quick-prepare"),full=$("quick-full"),presetApply=$("preset-apply");
  if(!prepare||!full)return;
  const viewer=session.role==="viewer";
  const id=$("quick-coursework")?.value||"";
  const target=courseworkIndex.find(c=>c.id===id);
  prepare.disabled=viewer||!target;
  prepare.title=viewer?"閲覧のみの権限です。":(target?"":"課題を選択してください。");
  const blocked=!target||!target.configured;
  full.disabled=viewer||blocked;
  full.title=viewer?"閲覧のみの権限です。"
    :(!target?"課題を選択してください。"
      :(!target.configured?"採点基準を確認済みにするまで採点を開始できません。":""));
  if(presetApply){
    const applyAll=$("preset-all")?.checked;
    const hasTarget=applyAll?courseworkIndex.some(c=>!c.configured):Boolean(target);
    presetApply.disabled=viewer||!$("preset-select")?.value||!hasTarget;
    presetApply.title=viewer?"閲覧のみの権限です。"
      :(!$("preset-select")?.value?"課題タイプを選択してください。"
        :(!hasTarget?(applyAll?"未設定の課題がありません。":"課題を選択してください。"):""));
  }
}

async function runQuickJob(phase){
  const courseworkId=$("quick-coursework").value;
  const message=$("quick-message");
  if(!courseworkId){message.textContent="課題を選択してください。";return}
  const target=courseworkIndex.find(c=>c.id===courseworkId);
  if(!target){message.textContent="課題が見つかりません。「課題一覧を再取得」を押してください。";return}
  if(phase==="full"&&!target.configured){
    message.textContent="採点基準を確認済みにするまで採点を開始できません。課題一覧で設定してください。";
    return;
  }
  message.textContent=`${PHASE_LABELS[phase]}を開始しています…`;
  try{await startJob(target,phase);message.textContent=`${PHASE_LABELS[phase]}を開始しました。進捗は「ジョブ」で確認できます。`}
  catch(e){message.textContent=e.message}
}

// 実運用で確定した基準をもとにした既定の採点基準プリセット
async function loadPresets(){
  const select=$("preset-select");
  if(!select)return;
  try{
    const d=await api("/api/v1/settings-presets").then(r=>r.json());
    presetCatalog=d.presets;
    renderDialogPresetOptions();
    select.replaceChildren();
    const placeholder=node("option","課題タイプを選択");placeholder.value="";select.append(placeholder);
    for(const preset of d.presets){
      const option=node("option",preset.label);
      option.value=preset.id;option.title=preset.description;
      select.append(option);
    }
  }catch(e){$("preset-message").textContent=e.message}
}

async function applyPreset(){
  const message=$("preset-message");
  const presetId=$("preset-select").value;
  if(!selectedCourseId){message.textContent="担当コースを選択してください。";return}
  if(!presetId){message.textContent="課題タイプを選択してください。";return}
  const applyAll=$("preset-all").checked;
  const targets=applyAll
    ? courseworkIndex.filter(c=>!c.configured).map(c=>c.id)
    : ($("quick-coursework").value?[$("quick-coursework").value]:[]);
  if(!targets.length){
    message.textContent=applyAll?"未設定の課題がありません。":"課題を選択してください。";
    return;
  }
  if(!confirm(`${targets.length}件の課題へ既定の採点基準を適用します。\n`
      +"適用しただけでは確認済みになりません。内容を確認して保存してください。"))return;
  message.textContent="適用しています…";
  try{
    const d=await api(`/api/v1/courses/${encodeURIComponent(selectedCourseId)}/settings-presets/apply`,
      {method:"POST",headers:{"Content-Type":"application/json"},
       body:JSON.stringify({preset_id:presetId,coursework_ids:targets,overwrite_confirmed:false})})
      .then(r=>r.json());
    const skippedNote=d.skipped.length?` / 対象外 ${d.skipped.length}件（確認済みなど）`:"";
    message.textContent=`適用 ${d.applied.length}件${skippedNote}。内容を確認し、確認チェックを付けて保存してください。`;
    await loadCourseworks();
  }catch(e){message.textContent=e.message}
}

async function loadCourseworks(){selectedCourseId=$("course-select").value;$("courses").replaceChildren();if(!selectedCourseId){courseworkIndex=[];renderQuickCourseworkOptions();syncQuickButtons();if($("courses-summary")){const total=d.courseworks.length,unset=courseworkIndex.filter(c=>!c.configured).length;$("courses-summary").textContent=`課題一覧（${total}件`+(unset?`／採点基準 未設定 ${unset}件`:"")+"）"}return}localStorage.setItem("cga-selected-course",selectedCourseId);const current=courses.find(c=>c.id===selectedCourseId);$("selected-course").textContent=`選択中: ${current?.name||selectedCourseId}`;try{const courseId=selectedCourseId;const d=await api(`/api/v1/courses/${encodeURIComponent(courseId)}/overview`).then(r=>r.json());$("course-error").textContent="";const root=$("courses");courseworkIndex=d.courseworks.map(c=>({id:c.id,title:c.title,configured:Boolean(c.configured)&&!c.overview_errors?.some(e=>e.code==="settings_unavailable")}));renderQuickCourseworkOptions();for(const c of d.courseworks){const ready=c.readiness;const settingsError=c.overview_errors?.some(e=>e.code==="settings_unavailable");const readinessError=c.overview_errors?.some(e=>e.code==="readiness_unavailable");const configured=Boolean(c.configured)&&!settingsError;const box=node("article",undefined,"course");box.append(node("strong",c.title||c.id),node("div",`ID: ${c.id} / 配点: ${c.max_points??"-"} / 採点基準: ${c.settings?.confirmed?"教師設定済み":(c.settings?"未確認":(c.assignment_key||"未登録"))}`),node("div",readinessError||!ready?"準備状況を取得できません。":`対象 ${ready.total} / 人間採点 ${ready.human_graded} / AI処理済み ${ready.system_graded}`));if(settingsError)box.append(node("div","採点基準を読み取れません。管理者へ確認してください。","error"));else if(!configured)box.append(node("div","採点基準を設定し、確認チェックを付けて保存するまで採点は開始できません。","error"));const acts=node("div",undefined,"course-actions");const settingsButton=node("button","採点基準を設定");settingsButton.disabled=session.role==="viewer";settingsButton.addEventListener("click",()=>openSettings(c,c.max_points));acts.append(settingsButton);const full=node("button",PHASE_LABELS.full);full.disabled=session.role==="viewer"||!configured;if(!configured)full.title="採点基準を確認済みにするまで採点を開始できません。";full.addEventListener("click",()=>startJob(c,"full"));acts.append(full);const graded=Number(ready?.system_graded||0)>0;const transfer=node("button","Classroomへ下書き入力");transfer.disabled=session.role==="viewer"||!graded;if(!graded)transfer.title="AI採点案がまだありません。先に「AI採点案を作成」を実行してください。";const transferMessage=node("p","","hint coursework-transfer-message");transferMessage.setAttribute("role","status");transfer.addEventListener("click",()=>transferDraftFor(courseId,c.id,transfer,transferMessage));acts.append(transfer);const view=node("button","結果を確認");view.disabled=!graded;if(!graded)view.title="AI採点案がまだありません。先に「AI採点案を作成」を実行してください。";view.addEventListener("click",()=>loadResults(c.id,c.title));acts.append(view);box.append(acts,transferMessage);root.append(box)}}catch(e){$("course-error").textContent=e.message}}

let settingsCoursework=null;let settingsMaxPoints=null;
function readSettingsForm(){const body={notes:$("settings-notes").value,levels:{},score_mapping:{},late_penalty:Number($("late-penalty").value),confirmed:$("settings-confirmed").checked};for(let i=0;i<4;i++){body.levels[i]=$(`level-${i}`).value;body.score_mapping[i]=Number($(`map-${i}`).value)}return body}
function fillSettingsForm(s){$("settings-notes").value=s.notes||"";for(let i=0;i<4;i++){$(`level-${i}`).value=s.levels?.[i]||"";$(`map-${i}`).value=s.score_mapping?.[i]??""}$("late-penalty").value=s.late_penalty??0;$("settings-confirmed").checked=Boolean(s.confirmed)}
async function openSettings(c,maxPoints){settingsCoursework=c;settingsMaxPoints=maxPoints;if($("dialog-preset-message")){$("dialog-preset-message").textContent="";$("dialog-preset-select").value=""}const s=c.settings||{};$("settings-title").textContent=`採点基準 — ${c.title||c.id} (満点 ${maxPoints??"-"})`;fillSettingsForm(s);$("settings-error").textContent="";$("template-message").textContent="";$("settings-dialog").showModal();await loadTemplates()}
async function saveSettings(e){e.preventDefault();const body=readSettingsForm();try{await api(`/api/v1/courses/${selectedCourseId}/courseworks/${settingsCoursework.id}/settings`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});$("settings-dialog").close();await loadCourseworks()}catch(err){$("settings-error").textContent=err.message}}

function updateTemplateButtons(){const selected=Boolean($("template-select").value),editable=session.role!=="viewer";$("template-apply").disabled=!selected||!editable;$("template-rename").disabled=!selected||!editable;$("template-delete").disabled=!selected||!editable;$("template-create").disabled=!editable}
// 個別課題の設定ダイアログでも既定の採点基準を選べるようにする。
// 保存済みテンプレート(コース内で作成したもの)とは別枠で提示する。
let presetCatalog=[];
function renderDialogPresetOptions(){
  const select=$("dialog-preset-select");
  if(!select)return;
  select.replaceChildren();
  const placeholder=node("option","課題タイプを選択");placeholder.value="";select.append(placeholder);
  for(const preset of presetCatalog){
    const option=node("option",preset.label);
    option.value=preset.id;option.title=preset.description;
    select.append(option);
  }
}
async function loadDialogPreset(){
  const message=$("dialog-preset-message");
  const presetId=$("dialog-preset-select").value;
  if(!presetId){message.textContent="課題タイプを選択してください。";return}
  try{
    const points=Number(settingsMaxPoints)||10;
    const d=await api(`/api/v1/settings-presets/${encodeURIComponent(presetId)}`
      +`?max_points=${encodeURIComponent(points)}`).then(r=>r.json());
    // 確認チェックは引き継がない(内容を確認してから教員が付ける)
    fillSettingsForm({...d.settings,confirmed:false});
    message.textContent="フォームへ読み込みました。内容を確認・調整し、「確認済み」を付けて保存してください。";
  }catch(e){message.textContent=e.message}
}

async function loadTemplates(preferred=""){const select=$("template-select");try{const d=await api(`/api/v1/courses/${encodeURIComponent(selectedCourseId)}/settings-templates`).then(r=>r.json());select.replaceChildren(new Option("テンプレートを選択",""),...d.templates.map(t=>new Option(t.name,t.id)));if(preferred&&d.templates.some(t=>t.id===preferred))select.value=preferred;$("template-message").textContent=d.templates.length?`${d.templates.length}件のコース用テンプレートがあります。`:"このコースのテンプレートはまだありません。"}catch(e){$("template-message").textContent=e.message}updateTemplateButtons()}
async function createTemplate(){const name=prompt("新しいテンプレート名を入力してください");if(!name)return;try{const d=await api(`/api/v1/courses/${encodeURIComponent(selectedCourseId)}/settings-templates`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,coursework_id:settingsCoursework.id,settings:readSettingsForm()})}).then(r=>r.json());await loadTemplates(d.template.id);$("template-message").textContent="現在のフォーム内容をテンプレートへ保存しました。"}catch(e){$("template-message").textContent=e.message}}
async function applyTemplate(){const id=$("template-select").value;if(!id)return;try{const d=await api(`/api/v1/courses/${encodeURIComponent(selectedCourseId)}/settings-templates/${encodeURIComponent(id)}/apply`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({coursework_id:settingsCoursework.id})}).then(r=>r.json());fillSettingsForm(d.settings);$("settings-confirmed").checked=false;$("template-message").textContent=`満点${settingsMaxPoints}点へ換算してフォームに反映しました。内容を確認し、確認チェックを付けて保存してください。`}catch(e){$("template-message").textContent=e.message}}
async function renameTemplate(){const id=$("template-select").value;if(!id)return;const current=$("template-select").selectedOptions[0]?.textContent||"";const name=prompt("新しいテンプレート名を入力してください",current);if(!name)return;try{await api(`/api/v1/courses/${encodeURIComponent(selectedCourseId)}/settings-templates/${encodeURIComponent(id)}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})});await loadTemplates(id);$("template-message").textContent="テンプレート名を変更しました。"}catch(e){$("template-message").textContent=e.message}}
async function deleteTemplate(){const id=$("template-select").value;if(!id)return;const name=$("template-select").selectedOptions[0]?.textContent||"選択中のテンプレート";if(!confirm(`「${name}」を削除しますか?`))return;try{await api(`/api/v1/courses/${encodeURIComponent(selectedCourseId)}/settings-templates/${encodeURIComponent(id)}`,{method:"DELETE"});await loadTemplates();$("template-message").textContent="テンプレートを削除しました。"}catch(e){$("template-message").textContent=e.message}}

// ジョブ起動前にGPUのモデルが前提を満たすか確認する
function modelWarning(phase){if(phase==="run"&&modelInfo.role!=="primary")return `一次採点用モデルがGPUに載っていません(現在: ${ROLE_LABELS[modelInfo.role]||"?"})。\nサーバーで bash scripts/vllm-server.sh q25-7b を実行してから再試行してください。\nこのまま開始すると失敗します。`;if(phase==="refine"&&modelInfo.role!=="judge")return `審判用モデルがGPUに載っていません(現在: ${ROLE_LABELS[modelInfo.role]||"?"})。\nサーバーで bash scripts/vllm-server.sh q3-8b を実行してから再試行してください。\nこのまま開始すると失敗します。`;return null}

async function startJob(c,phase){if(!selectedCourseId)return;const stance=$("grading-mode").value;const body={course_id:selectedCourseId,coursework_id:c.id,phase,stance,force:false};
if(phase==="refine"){const a=prompt("基準答案(2点相当)のstudent_idを入力してください");if(!a)return;body.anchor=a}
const warn=modelWarning(phase);if(warn){alert(`【開始できません】${warn}`);return}
if(phase==="report"){if(c.readiness?.missing>0){if(!confirm(`未処理が${c.readiness.missing}件あります。部分集計であることを理解して続行しますか?`))return;body.allow_partial=true}if(c.readiness?.report_exists){if(!confirm("既存の集計CSVを上書きしますか?"))return;body.overwrite_report=true}}
const stanceLabel={auto:"自動(課題タイプに合わせる)",lenient:"甘め",strict:"厳しめ"}[stance];
if(confirm(`「${c.title||c.id}」で【${PHASE_LABELS[phase]}】を開始しますか?\n採点の厳しさ: ${stanceLabel}`)){try{await api("/api/v1/jobs",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});await refresh();startPolling()}catch(e){alert(e.message)}}}

function renderJobs(jobs){const root=$("jobs");root.replaceChildren();jobs.slice(0,20).forEach(j=>{const box=node("article",undefined,"job");const st={queued:"待機中",running:"実行中",succeeded:"完了",failed:"失敗",blocked:"安全ガードで停止",canceled:"取り消し",interrupted:"中断"}[j.status]||j.status;const step=j.status==="queued"?`待ち順 ${j.position||"-"}`:(STEP_LABELS[j.current_step]||j.current_step||"待機");const done=(j.completed_steps||[]).map(x=>PHASE_LABELS[x]||x).join(" → ");box.append(node("strong",`${PHASE_LABELS[j.phase]||j.phase} — ${st}`),node("div",`コース ${j.course_id||"-"} / 課題 ${j.coursework_id} / ${step} / ${j.created_at}`));if(done)box.append(node("div",`完了工程: ${done}`));if(j.status==="queued"&&session.role!=="viewer"){const cancel=node("button","待機ジョブを取り消す");cancel.addEventListener("click",()=>cancelJob(j.id));box.append(cancel)}if(j.error)box.append(node("div",j.error,"error"));if(j.log){const p=node("pre",j.log);box.append(p)}root.append(box)})}
async function cancelJob(id){if(!confirm("待機中のジョブを取り消しますか?"))return;try{await api(`/api/v1/jobs/${id}/cancel`,{method:"POST"});await refresh()}catch(e){alert(e.message)}}

function startPolling(){clearInterval(pollTimer);pollTimer=setInterval(async()=>{try{const j=await api("/api/v1/jobs").then(r=>r.json());renderJobs(j.jobs);if(!j.jobs.some(x=>["queued","running"].includes(x.status))){clearInterval(pollTimer);if(selectedCourse)loadResults(selectedCourse)}}catch(e){clearInterval(pollTimer)}},2000)}

async function loadResults(id,title=""){try{const d=await api(`/api/v1/courses/${encodeURIComponent(selectedCourseId)}/courseworks/${id}/results`).then(r=>r.json());selectedCourse=id;rows=d.grades;$("results-section").hidden=false;$("draft-transfer").disabled=session.role==="viewer";const current=courses.find(c=>c.id===selectedCourseId);$("results-title").textContent=`AI採点結果 — ${current?.name||selectedCourseId} / ${title||id}`;evaluateVisualMaxSaturation();cards($("counts"),{"優先確認":rows.filter(r=>riskScore(r)>=RISK_THRESHOLD).length,"画像答案の最高点提案":rows.filter(isQ25VisualMaxScore).length,"自動で下書き対象":rows.filter(r=>r.automatic_eligible&&r.teacher_status!=="confirmed").length,"教員確認済み":rows.filter(r=>r.teacher_status==="confirmed").length,"合計":rows.length});renderVisualMaxWarning();renderRows();$("results-section").scrollIntoView({behavior:"smooth"})}catch(e){alert(e.message)}}

async function downloadCsv(e){e.preventDefault();if(!selectedCourse||!selectedCourseId)return;try{const r=await api(`/api/v1/courses/${encodeURIComponent(selectedCourseId)}/courseworks/${selectedCourse}/report.csv`);const blob=await r.blob(),url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=`${selectedCourse}.csv`;a.click();URL.revokeObjectURL(url)}catch(err){alert(err.message)}}

// 連続実行で見出しが二重に描画されるのを防ぐ。DOMの書き換えはawaitの後に
// まとめて行い、最新のリクエストの結果だけを反映する。
let rankingRequestId=0;
async function loadRanking(forceRefresh=false){
  const message=$("ranking-message");
  const requestId=++rankingRequestId;
  if(!selectedCourseId){
    $("ranking-wrap").hidden=true;$("ranking-detail").hidden=true;
    $("ranking-head").replaceChildren();$("ranking-rows").replaceChildren();
    message.textContent="担当コースを選択してください。";return;
  }
  message.textContent="確定済みの点数を取得して集計しています…";
  try{
    const url=`/api/v1/courses/${encodeURIComponent(selectedCourseId)}/ranking`
      +(forceRefresh?"?refresh=true":"");
    const d=await api(url).then(r=>r.json());
    if(requestId!==rankingRequestId)return;  // 新しい要求が来ていれば破棄する
    const head=$("ranking-head"),body=$("ranking-rows");
    const detailHead=$("ranking-detail-head"),detailBody=$("ranking-detail-rows");
    head.replaceChildren();body.replaceChildren();
    detailHead.replaceChildren();detailBody.replaceChildren();
    if(!d.rows.length){
      $("ranking-wrap").hidden=true;$("ranking-detail").hidden=true;
      lastRanking=null;renderZeroAndMissing();
      message.textContent="確定済み成績はまだありません。Classroomで点数を確定するか、"
        +"採点結果画面で「点数を修正」して確認済みにすると集計されます。";
      return;
    }
    // 1) 集計表: 平均点と提出状況を見せる(課題ごとの点数は内訳へ)
    const summaryHead=node("tr");
    for(const [label,hint] of [["順位",""],["氏名",""],
        ["課題の平均点","満点で正規化した平均。未提出は3点満点中−1点と同じ比重で減点"],
        ["最高点回数","各課題で受講者中の最高点だった回数"],
        ["提出数","未提出と分かっている課題を除いた数"],
        ["未提出数","未提出と分かっている課題数"],
        ["未確定数","提出済みだが点数が確定していない課題数（平均点の母数に含めない）"],
        ["確定点合計","確定した実点の合計（参考）"]]){
      const th=node("th",label);if(hint)th.title=hint;summaryHead.append(th);
    }
    head.append(summaryHead);
    for(const row of d.rows){
      const line=node("tr");
      line.append(node("td",row.rank),node("td",row.name),
        node("td",`${(row.average_rate*100).toFixed(1)}%`),
        node("td",row.top_score_count),node("td",row.submitted_count),
        node("td",row.not_submitted_count),node("td",row.unconfirmed_count),
        node("td",row.total));
      body.append(line);
    }
    $("ranking-wrap").hidden=false;
    // 2) 内訳表: 課題ごとの点数は折りたたみへ分離する
    const detailRow=node("tr");
    for(const label of ["順位","氏名"])detailRow.append(node("th",label));
    for(const c of d.courseworks){
      const label=c.max_points?`${c.title||c.coursework_id} / ${c.max_points}点`
        :(c.title||c.coursework_id);
      const th=node("th",label,"coursework-col");
      th.title=label;
      detailRow.append(th);
    }
    detailHead.append(detailRow);
    for(const row of d.rows){
      const line=node("tr");
      line.append(node("td",row.rank),node("td",row.name));
      for(const c of d.courseworks){
        const score=row.scores[c.coursework_id];
        const missing=row.not_submitted?.[c.coursework_id];
        line.append(node("td",score??(missing?"−1":"—"),missing?"missing-cell":""));
      }
      detailBody.append(line);
    }
    $("ranking-detail").hidden=false;
    lastRanking=d;
    renderZeroAndMissing();
    const cacheNote=d.cached
      ?`（${Math.round(d.cache_age_seconds)}秒前の集計を再利用。最新にするには「ランキングを更新」）`:"";
    message.textContent=`${d.rows.length}名 / ${d.courseworks.length}課題`
      +`（同点は最高点回数で判定）${cacheNote}`;
  }catch(e){
    if(requestId===rankingRequestId)message.textContent=e.message;
  }
}

// ランキング表示の切り替え(ランキング / 最高点者 / 0点・未提出 / 各回の詳細)。
const RANKING_SUBPANELS={ranking:"subpanel-ranking",top:"subpanel-top",
                         zero:"subpanel-zero",breakdown:"subpanel-breakdown"};
let topScorersLoaded=false;
function selectRankingSubtab(name){
  if(!RANKING_SUBPANELS[name])name="ranking";
  for(const [key,panelId] of Object.entries(RANKING_SUBPANELS)){
    const active=key===name;
    const tab=$(`subtab-${key}`),panel=$(panelId);
    if(tab){tab.setAttribute("aria-selected",String(active));tab.classList.toggle("active",active);}
    if(panel)panel.hidden=!active;
  }
  try{localStorage.setItem("cga-ranking-subtab",name)}catch{}
  // 最高点者は開いたときに初回だけ取得する(無駄な集計を避ける)
  if(name==="top"&&!topScorersLoaded&&selectedCourseId)loadTopScorers();
}

// 0点・未提出者はランキング取得結果から組み立てる(追加のAPI呼び出しをしない)。
let lastRanking=null;
function renderZeroAndMissing(){
  const message=$("zero-message"),root=$("zero-list");
  root.replaceChildren();
  if(!lastRanking){message.textContent="ランキングを更新すると表示されます。";return}
  const {courseworks,rows}=lastRanking;
  let flagged=0;
  for(const c of courseworks){
    const zero=[],missing=[];
    for(const row of rows){
      if(row.not_submitted?.[c.coursework_id])missing.push(row);
      else if(row.scores[c.coursework_id]===0)zero.push(row);
    }
    if(!zero.length&&!missing.length)continue;
    flagged++;
    const box=node("article",undefined,"course");
    const cap=c.max_points?` / ${c.max_points}点`:"";
    box.append(node("strong",`${c.title||c.coursework_id}${cap}`));
    box.append(node("div",`0点 ${zero.length}名 / 未提出 ${missing.length}名`));
    if(zero.length){
      const line=node("div",undefined,"course-actions");
      line.append(node("span","0点：","zero-label"));
      for(const row of zero){
        const button=node("button",row.name);
        button.title="答案を開いて内容を確認します";
        button.addEventListener("click",()=>openAnswer(
          c.coursework_id,c.title||c.coursework_id,
          {student_id:row.student_id,name:row.name,score:0}));
        line.append(button);
      }
      box.append(line);
    }
    if(missing.length){
      const line=node("div",undefined,"course-actions");
      line.append(node("span","未提出：","zero-label"));
      // 未提出は答案が無いので名前だけを出す
      for(const row of missing)line.append(node("span",row.name,"missing-name"));
      box.append(line);
    }
    root.append(box);
  }
  message.textContent=flagged
    ?`${flagged}課題で0点または未提出があります。`
    :"0点・未提出の学生はいません。";
}

// 各課題の最高点取得者。ランキングと同じキャッシュを使う。
async function loadTopScorers(forceRefresh=false){
  const message=$("top-scorers-message"),root=$("top-scorers");
  root.replaceChildren();
  if(!selectedCourseId){message.textContent="担当コースを選択してください。";return}
  message.textContent="最高点取得者を集計しています…";
  try{
    const url=`/api/v1/courses/${encodeURIComponent(selectedCourseId)}/top-scorers`
      +(forceRefresh?"?refresh=true":"");
    const d=await api(url).then(r=>r.json());
    const withScores=d.courseworks.filter(c=>c.top_score!==null&&c.scorers.length);
    if(!withScores.length){
      message.textContent="確定済みの点がまだないため、最高点を判定できません。";
      return;
    }
    for(const entry of withScores){
      const box=node("article",undefined,"course");
      const cap=entry.max_points?` / ${entry.max_points}点`:"";
      box.append(node("strong",`${entry.title}${cap}`));
      box.append(node("div",`最高点 ${entry.top_score}（${entry.scorer_count}名）`));
      const list=node("div",undefined,"course-actions");
      for(const scorer of entry.scorers){
        const button=node("button",`${scorer.name}（${scorer.score}）`);
        button.title="答案を開いて内容を確認します";
        button.addEventListener("click",
          ()=>openAnswer(entry.coursework_id,entry.title,scorer));
        list.append(button);
      }
      box.append(list);
      root.append(box);
    }
    const cacheNote=d.cached?`（${Math.round(d.cache_age_seconds)}秒前の集計を再利用）`:"";
    message.textContent=`${withScores.length}課題${cacheNote}`;
    topScorersLoaded=true;
  }catch(e){message.textContent=e.message}
}

// 最高点答案の内容確認。テキストはそのまま、画像はページ送りで表示する。
let answerContext=null;
async function openAnswer(courseworkId,title,scorer,page=1){
  const dialog=$("answer-dialog"),body=$("answer-body");
  answerContext={courseworkId,title,scorer,page};
  $("answer-title").textContent=`${title} — ${scorer.name}（${scorer.score}点）`;
  $("answer-meta").textContent="読み込んでいます…";
  body.replaceChildren();
  if(!dialog.open)dialog.showModal();
  try{
    const d=await api(`/api/v1/courses/${encodeURIComponent(selectedCourseId)}`
      +`/courseworks/${encodeURIComponent(courseworkId)}`
      +`/submissions/${encodeURIComponent(scorer.student_id)}/answer?page=${page}`)
      .then(r=>r.json());
    if(d.content_mode==="text"){
      const pre=node("pre",d.answer_text||"(本文がありません)");
      body.append(pre);
      $("answer-meta").textContent=`テキスト答案（${d.page_count??"?"}ページ）`;
      $("answer-prev").hidden=true;$("answer-next").hidden=true;
    }else{
      const img=node("img");
      img.src=`data:${d.image.mime_type};base64,${d.image.data_base64}`;
      img.alt="答案ページ";img.className="answer-image";
      body.append(img);
      $("answer-meta").textContent=`画像答案 ${d.page}/${d.available_pages}ページ`;
      $("answer-prev").hidden=false;$("answer-next").hidden=false;
      $("answer-prev").disabled=d.page<=1;
      $("answer-next").disabled=d.page>=d.available_pages;
      answerContext.available=d.available_pages;
    }
  }catch(e){$("answer-meta").textContent=e.message}
}
function stepAnswer(delta){
  if(!answerContext)return;
  const next=answerContext.page+delta;
  if(next<1||(answerContext.available&&next>answerContext.available))return;
  openAnswer(answerContext.courseworkId,answerContext.title,answerContext.scorer,next);
}

async function exportRanking(){if(!selectedCourseId)return;const spreadsheet=$("ranking-spreadsheet").value.trim(),sheet=$("ranking-sheet").value.trim(),range=$("ranking-range").value.trim();if(!spreadsheet){$("ranking-message").textContent="スプレッドシートURLまたはIDを入力してください。";return}if(!confirm(`選択コースの確定済みランキングをGoogle Sheetsへ出力しますか?\nシート: ${sheet}\n範囲: ${range}`))return;localStorage.setItem("cga-ranking-spreadsheet",spreadsheet);localStorage.setItem("cga-ranking-sheet",sheet);localStorage.setItem("cga-ranking-range",range);$("ranking-message").textContent="Google Sheetsへ出力しています…";try{const d=await api(`/api/v1/courses/${encodeURIComponent(selectedCourseId)}/ranking/sheets`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({spreadsheet,sheet_name:sheet,range})}).then(r=>r.json());$("ranking-message").textContent=`Google Sheetsへ${d.updated_cells}セルを出力しました。`}catch(e){$("ranking-message").textContent=e.message}}

// 確認開始時刻。一覧を描画した時点ではなく、教員がその行をアクティブ化
// (点数欄へフォーカス、または行をクリック)した時点で開始する。全行へ一括
// 設定すると「一覧を開いてからの経過時間」を測ることになり、実際の確認時間に
// ならない。同時に進行するタイマーは常に1件だけとする。
// 送信するのは点数と開始時刻のみ。答案本文・学生名は送らない。
const reviewOpenedAt=new Map();
let activeReviewStudentId=null;
function activateReview(studentId){
  if(activeReviewStudentId===studentId)return;
  // 直前の行の計測は破棄する(実際に確認したのは新しい行)
  if(activeReviewStudentId!==null)reviewOpenedAt.delete(activeReviewStudentId);
  activeReviewStudentId=studentId;
  reviewOpenedAt.set(studentId,Math.floor(Date.now()/1000));
}
async function saveReview(r,confirmed){const input=$(`score-${r.student_id}`),score=Number(input.value);const startedAt=reviewOpenedAt.get(r.student_id)??null;try{await api(`/api/v1/courses/${selectedCourseId}/courseworks/${selectedCourse}/reviews/${r.student_id}`,{method:"PUT",headers:{"Content-Type":"application/json"},body:JSON.stringify({score,confirmed,review_started_at:startedAt})});reviewOpenedAt.delete(r.student_id);if(activeReviewStudentId===r.student_id)activeReviewStudentId=null;await loadResults(selectedCourse)}catch(e){alert(e.message)}}
function renderVisualMaxWarning(){const el=$("visual-max-warning");if(!el)return;
  const stat=evaluateVisualMaxSaturation();
  const n=rows.filter(isQ25VisualMaxScore).length;
  el.hidden=n===0;
  if(!n)return;
  if(stat.saturated){
    // 選別能力がないため、順序ではなく課題単位の注意として伝える。
    el.textContent=`この課題では未確認の画像答案${stat.pending}件のうち${stat.flagged}件`
      +`(${Math.round(stat.rate*100)}%)でQwen2.5が最高点を提案しており、`
      +`このフラグでは優先順位を付けられません。画像答案全体を過大評価の可能性がある`
      +`ものとして確認してください。`;
  }else{
    el.textContent=`この課題ではQwen2.5が画像答案へ最高点を提案した答案が${n}件あります。`
      +`過大評価の傾向が確認されているため、これらを優先して確認してください。`;
  }}
function renderRows(){const q=$("search").value.toLowerCase();const riskOnly=$("risk-only")?.checked;const out=rows.filter(r=>!q||`${r.name} ${r.student_id}`.toLowerCase().includes(q)).map(r=>({row:r,score:riskScore(r),reasons:riskReasons(r)})).filter(x=>!riskOnly||x.score>=RISK_THRESHOLD).sort((a,b)=>b.score-a.score||String(a.row.name||a.row.student_id).localeCompare(String(b.row.name||b.row.student_id)));const body=$("result-rows");body.replaceChildren();out.forEach(({row:r,score,reasons})=>{const high=score>=RISK_THRESHOLD;const tr=node("tr",undefined,r.teacher_status==="confirmed"?"confirmed":(high?"risk":"proposal"));tr.append(node("td",score<0?"—":(high?`要確認 ${score}`:String(score))));tr.append(node("td",r.name||r.student_id),node("td",`${r.proposal_score??"-"} (${CAT_LABELS[r.category]||"AI提案"})`));const scoreCell=node("td"),input=node("input");input.type="number";input.step="0.1";input.min="0";input.value=r.teacher_score??"";input.id=`score-${r.student_id}`;input.addEventListener("focus",()=>activateReview(r.student_id));tr.addEventListener("click",()=>activateReview(r.student_id));scoreCell.append(input);const CONFIRMED_BY={web_review:"教員修正済み",classroom_assigned_grade:"Classroom確定済み"};const stateLabel=r.teacher_status==="confirmed"?(CONFIRMED_BY[r.teacher_confirmed_by]||"教員確認済み"):(r.automatic_eligible?"自動対象":`対象外（修正可）${r.proposal_state?" / "+(STATE_LABELS[r.proposal_state]||r.proposal_state):""}`);tr.append(scoreCell,node("td",stateLabel));const action=node("td"),save=node("button",r.teacher_status==="confirmed"?"修正を保存":"点数を修正");save.disabled=session.role==="viewer";save.addEventListener("click",()=>saveReview(r,true));action.append(save);const detailCell=node("td");if(reasons.length)detailCell.append(node("div",reasons.map(name=>RISK_LABELS[name]||name).join(" / "),"risk-reasons"));const badges=infoBadges(r);if(badges.length)detailCell.append(node("div",badges.map(name=>RISK_LABELS[name]||name).join(" / "),"info-badges"));const delta=Number(r.score_delta)||0;const detail=[r.reason,r.evidence,r.confidence!=null?`confidence ${r.confidence}`:"",delta?`一次${r.primary_result?.internal_score??"?"}→再確認${r.review_result?.internal_score??"?"}`:"",r.model?`model ${r.model}`:""].filter(Boolean).join(" / ");detailCell.append(node("div",detail));tr.append(action,detailCell);body.append(tr)})}

async function previewDraft(){if(!selectedCourse)return;try{const d=await api(`/api/v1/courses/${selectedCourseId}/courseworks/${selectedCourse}/draft-batches/preview`).then(r=>r.json());$("draft-message").textContent=`下書き対象 ${d.eligible_count}件 / 除外 ${d.skipped_count}件。対象全件のバッチを作成できます。`;return d}catch(e){alert(e.message)}}
const EXTENSION_MIN_VERSION="0.5.4";
function versionAtLeast(value,minimum){const current=String(value||"").split(".").map(Number),required=minimum.split(".").map(Number);if(current.some(n=>!Number.isInteger(n)||n<0))return false;for(let i=0;i<3;i++){if((current[i]||0)!==(required[i]||0))return (current[i]||0)>(required[i]||0)}return true}
function extensionBridgeReady(){return new Promise((resolve,reject)=>{const requestId=crypto.randomUUID();const timeout=setTimeout(()=>{window.removeEventListener("message",receive);reject(new Error("Chrome拡張機能v0.5.4以上が応答しません。上のZIPを再ダウンロードし、chrome://extensions で拡張を更新・再読み込みしてください。"))},1800);function receive(event){if(event.source!==window||event.origin!==location.origin||event.data?.type!=="CGA_EXTENSION_BRIDGE_READY_V1"||event.data.requestId!==requestId)return;clearTimeout(timeout);window.removeEventListener("message",receive);if(!versionAtLeast(event.data.version,EXTENSION_MIN_VERSION)||!event.data.capabilities?.includes("local_queue_v1")){reject(new Error(`Chrome拡張機能v${EXTENSION_MIN_VERSION}以上が必要です。ZIPから更新・再読み込みしてください（検出: ${event.data.version||"旧版"}）。`));return}resolve(event.data)}window.addEventListener("message",receive);window.postMessage({type:"CGA_EXTENSION_BRIDGE_PROBE_V1",requestId},location.origin)})}
async function sendTransferToExtension(payload){await extensionBridgeReady();return new Promise((resolve,reject)=>{const requestId=crypto.randomUUID();const timeout=setTimeout(()=>{window.removeEventListener("message",receive);reject(new Error("拡張機能への転送がタイムアウトしました。chrome://extensions でv0.5.4を再読み込みしてください。"))},2500);function receive(event){if(event.source!==window||event.origin!==location.origin||event.data?.type!=="CGA_EXTENSION_TRANSFER_RESULT_V1"||event.data.requestId!==requestId)return;clearTimeout(timeout);window.removeEventListener("message",receive);if(event.data.ok&&versionAtLeast(event.data.version,EXTENSION_MIN_VERSION))resolve(event.data);else reject(new Error(event.data.error||"拡張機能v0.5.4へ転送できませんでした。"))}window.addEventListener("message",receive);window.postMessage({type:"CGA_EXTENSION_TRANSFER_V1",requestId,payload},location.origin)})}
async function transferDraftFor(courseId,courseworkId,button,message){if(!courseId||!courseworkId||button.disabled)return;const original=button.textContent;button.disabled=true;button.textContent="準備中…";message.textContent="安全条件を再確認して転送データを準備しています…";try{const payload=await api(`/api/v1/courses/${encodeURIComponent(courseId)}/courseworks/${encodeURIComponent(courseworkId)}/extension-transfer`,{method:"POST"}).then(r=>r.json());message.textContent=`転送対象${payload.items.length}件を確認しました。Chrome拡張機能v0.5.4へ転送しています…`;const result=await sendTransferToExtension(payload);message.textContent=`成功: ${result.count}件を拡張機能v${result.version}へ転送しました。対象課題のClassroom採点画面で「準備済み${result.count}件を空欄へ入力」を押してください。`}catch(e){message.textContent=`転送できません: ${e.message}`}finally{button.textContent=original;button.disabled=session.role==="viewer"}}
async function transferDraft(){if(!selectedCourse||!selectedCourseId)return;await transferDraftFor(selectedCourseId,selectedCourse,$("draft-transfer"),$("transfer-message"))}
async function createDraft(){const p=await previewDraft();if(!p?.eligible_count||!confirm(`${p.eligible_count}件の一回利用バッチを作成しますか?`))return;try{const d=await api("/api/v1/draft-batches",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({course_id:selectedCourseId,coursework_id:selectedCourse})}).then(r=>r.json());$("draft-message").textContent=`下書きバッチを作成しました（${d.count}件）。接続済み端末では「最新の下書きを取得して入力」を押してください。旧式互換用の短期下書きコード: ${d.pairing_code}`}catch(e){alert(e.message)}}

function clearMcpToken(){$("mcp-token").value="";$("mcp-token").type="password";$("mcp-once").hidden=true}
function renderMcpTokens(tokens){const root=$("mcp-tokens");root.replaceChildren();for(const token of tokens){const row=node("article",undefined,"job");row.append(node("strong",`token ${token.id}`),node("div",`権限 ${token.role} / 作成 ${token.created_at} / 期限 ${token.expires_at} / 最終利用 ${token.last_used_at||"未使用"}`));const revoke=node("button","失効");revoke.addEventListener("click",()=>revokeMcpToken(token.id));row.append(revoke);root.append(row)}if(!tokens.length)root.append(node("p","発行済みtokenはありません。","hint"))}
async function loadMcpTokens(){const section=$("mcp-section");if(session.role==="viewer"){section.hidden=true;return}section.hidden=false;try{const d=await api("/api/v1/mcp/tokens").then(r=>r.json());renderMcpTokens(d.tokens)}catch(e){$("mcp-message").textContent=e.message}}
function renderDevices(devices){const root=$("extension-devices");root.replaceChildren();for(const device of devices){const row=node("article",undefined,"job");row.append(node("strong",device.label||device.id),node("div",`接続作成 ${new Date(device.created_at*1000).toLocaleString()} / 期限 ${new Date(device.expires_at*1000).toLocaleString()} / 最終利用 ${device.last_used_at?new Date(device.last_used_at*1000).toLocaleString():"未使用"}`));const revoke=node("button","失効");revoke.addEventListener("click",()=>revokeDevice(device.id));row.append(revoke);root.append(row)}if(!devices.length)root.append(node("p","接続済み端末はありません。手順3で初回接続コードを発行してください。","hint"))}
async function loadDevices(){if(session.role==="viewer")return;try{const d=await api("/api/v1/extension/devices").then(r=>r.json());renderDevices(d.devices)}catch(e){$("device-message").textContent=e.message}}
async function createDevicePairing(){const label=$("device-label").value,expires_days=Number($("device-days").value);if(!confirm(`端末「${label}」の初回接続コードを発行しますか?`))return;try{const d=await api("/api/v1/extension/devices/pairing-codes",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({label,expires_days,confirm:true})}).then(r=>r.json());$("device-code").value=d.pairing_code;$("device-code-wrap").hidden=false;$("device-message").textContent="初回接続コードを発行しました。一度だけClassroom採点画面へ入力してください。接続後は再入力不要です。";await loadDevices()}catch(e){$("device-message").textContent=e.message}}
async function copyDevicePairingCode(){const value=$("device-code").value;if(!value)return;try{await navigator.clipboard.writeText(value);$("device-message").textContent="初回接続コードをコピーしました。Classroom採点画面へ一度入力してください。"}catch(e){$("device-message").textContent="コピーできませんでした。コードを選択して手動でコピーしてください。"}}
async function revokeDevice(id){if(!confirm("この端末を失効しますか?"))return;try{await api(`/api/v1/extension/devices/${encodeURIComponent(id)}`,{method:"DELETE"});await loadDevices()}catch(e){$("device-message").textContent=e.message}}
async function createMcpToken(){clearMcpToken();const days=Number($("mcp-days").value);try{const d=await api("/api/v1/mcp/tokens",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({expires_days:days})}).then(r=>r.json());$("mcp-token").value=d.token;$("mcp-once").hidden=false;$("mcp-message").textContent="tokenを発行しました。この画面を離れると再表示できません。";await loadMcpTokens()}catch(e){$("mcp-message").textContent=e.message}}
async function revokeMcpToken(id){if(!confirm("このMCP tokenを失効しますか?"))return;try{await api(`/api/v1/mcp/tokens/${encodeURIComponent(id)}`,{method:"DELETE"});await loadMcpTokens();$("mcp-message").textContent="tokenを失効しました。"}catch(e){$("mcp-message").textContent=e.message}}
async function copyMcpToken(){const value=$("mcp-token").value;if(!value)return;await navigator.clipboard.writeText(value);$("mcp-message").textContent="クリップボードへコピーしました。コピー後は画面から消してください。"}
const mcpUrl=`${location.origin}/mcp`;
$("mcp-url").textContent=mcpUrl;
$("mcp-codex-example").textContent=`# ~/.codex/config.toml\n[mcp_servers.classroom_grader]\nurl = "${mcpUrl}"\n# 初回接続時にブラウザでGoogleログイン`;
$("mcp-claude-example").textContent=`Claude Desktop → Settings → Connectors → Add custom connector\nURL: ${mcpUrl}\n追加後、ブラウザでGoogleログイン`;
$("mcp-token-codex-example").textContent=`# OAuth非対応クライアントだけで使用\n[mcp_servers.classroom_grader]\nurl = "${mcpUrl}"\nbearer_token_env_var = "CGA_MCP_TOKEN"`;
$("mcp-token-claude-example").textContent=`claude mcp add --transport http classroom-grader ${mcpUrl} --header "Authorization: Bearer \${CGA_MCP_TOKEN}"`;
$("mcp-create").addEventListener("click",createMcpToken);$("mcp-copy").addEventListener("click",copyMcpToken);$("mcp-clear").addEventListener("click",clearMcpToken);$("mcp-reveal").addEventListener("click",()=>{$("mcp-token").type=$("mcp-token").type==="password"?"text":"password"});$("device-pair").addEventListener("click",createDevicePairing);$("device-code-copy").addEventListener("click",copyDevicePairingCode);
$("settings-form").addEventListener("submit",saveSettings);
$("template-select").addEventListener("change",updateTemplateButtons);$("template-create").addEventListener("click",createTemplate);$("template-apply").addEventListener("click",applyTemplate);$("template-rename").addEventListener("click",renameTemplate);$("template-delete").addEventListener("click",deleteTemplate);
$("ranking-spreadsheet").value=localStorage.getItem("cga-ranking-spreadsheet")||"";$("ranking-sheet").value=localStorage.getItem("cga-ranking-sheet")||"ランキング";$("ranking-range").value=localStorage.getItem("cga-ranking-range")||"A1:Z1000";
$("google-login").addEventListener("click",connectGoogle);$("google-reconnect").addEventListener("click",connectGoogle);$("logout").addEventListener("click",logout);$("course-select").addEventListener("change",async()=>{topScorersLoaded=false;await loadCourseworks();await loadRanking();if(!$("subpanel-top").hidden)await loadTopScorers()});$("ranking-refresh").addEventListener("click",()=>loadRanking(true));$("top-scorers-refresh").addEventListener("click",()=>loadTopScorers(true));$("subtab-ranking").addEventListener("click",()=>selectRankingSubtab("ranking"));$("subtab-top").addEventListener("click",()=>selectRankingSubtab("top"));$("subtab-zero").addEventListener("click",()=>selectRankingSubtab("zero"));$("subtab-breakdown").addEventListener("click",()=>selectRankingSubtab("breakdown"));$("answer-close").addEventListener("click",()=>$("answer-dialog").close());$("answer-prev").addEventListener("click",()=>stepAnswer(-1));$("answer-next").addEventListener("click",()=>stepAnswer(1));$("ranking-export").addEventListener("click",exportRanking);$("tab-grading").addEventListener("click",()=>selectTab("grading"));$("tab-mcp").addEventListener("click",()=>selectTab("mcp"));$("tab-extension").addEventListener("click",()=>selectTab("extension"));$("tab-jobs").addEventListener("click",()=>selectTab("jobs"));$("preset-apply").addEventListener("click",applyPreset);$("dialog-preset-load").addEventListener("click",loadDialogPreset);$("quick-coursework").addEventListener("change",syncQuickButtons);$("preset-select").addEventListener("change",syncQuickButtons);$("preset-all").addEventListener("change",syncQuickButtons);$("courses-refresh").addEventListener("click",loadCourseworks);$("quick-prepare").addEventListener("click",()=>runQuickJob("prepare"));$("quick-full").addEventListener("click",()=>runQuickJob("full"));$("search").addEventListener("input",renderRows);$("risk-only").addEventListener("change",renderRows);$("draft-preview").addEventListener("click",previewDraft);$("draft-transfer").addEventListener("click",transferDraft);$("draft-create").addEventListener("click",createDraft);$("csv").addEventListener("click",downloadCsv);window.addEventListener("message",e=>{if(e.origin!==location.origin||e.data?.type!=="classroom-oauth")return;notice=e.data.result==="success"?"Google Classroomに接続しました。":(e.data.message||"Google認証に失敗しました。");refresh()});selectTab(localStorage.getItem("cga-active-tab")||"grading");selectRankingSubtab(localStorage.getItem("cga-ranking-subtab")||"ranking");handleOAuthReturn();refresh();loadPresets().then(syncQuickButtons);
