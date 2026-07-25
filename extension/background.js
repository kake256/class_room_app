"use strict";
importScripts("core.js");

const DEFAULT_SERVER_URL = "https://classroom-grader-1.tail80e540.ts.net";
const WEB_UI_ORIGIN = "https://classroom-grader-1.tail80e540.ts.net";
const QUEUE_STORAGE_KEY = "localDraftQueuesV1";
const queueStorage = chrome.storage.session || chrome.storage.local;

async function loadQueues() {
  const stored = await queueStorage.get({ [QUEUE_STORAGE_KEY]: {} });
  return CgaCore.purgeQueues(stored[QUEUE_STORAGE_KEY], Date.now());
}
async function saveQueues(queues) {
  await queueStorage.set({ [QUEUE_STORAGE_KEY]: queues });
}
async function purgeStoredQueues() { await saveQueues(await loadQueues()); }
chrome.runtime.onStartup.addListener(purgeStoredQueues);
chrome.runtime.onInstalled.addListener(purgeStoredQueues);

chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());

async function settings() {
  const data = await chrome.storage.sync.get({ serverUrl: DEFAULT_SERVER_URL });
  return { serverUrl: CgaCore.validateServerUrl(data.serverUrl) };
}

async function apiFetch(path, options = {}) {
  const { serverUrl } = await settings();
  const allowed = await chrome.permissions.contains({ origins: [CgaCore.hostPermissionPattern(serverUrl)] });
  if (!allowed) throw new Error("設定画面でサーバーへの接続を許可してください。");
  const response = await fetch(`${serverUrl}${path}`, {
    ...options,
    cache: "no-store",
    credentials: "omit",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) }
  });
  if (!response.ok) {
    const error = new Error(`サーバー要求に失敗しました（HTTP ${response.status}）。`);
    error.status = response.status;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message?.type === "storeLocalQueue") {
      const senderUrl = new URL(sender.url || "about:blank");
      if (senderUrl.origin !== WEB_UI_ORIGIN || !senderUrl.pathname.startsWith("/ui/")) throw new Error("転送元を確認できません。");
      const payload = CgaCore.validateTransferPayload(message.payload);
      const queues = CgaCore.putQueue(await loadQueues(), payload, Date.now());
      await saveQueues(queues);
      sendResponse({ ok: true, count: payload.items.length });
      return;
    }
    if (message?.type === "getLocalQueue") {
      const context = CgaCore.parseClassroomContext(sender.url || "");
      if (!context) throw new Error("送信元のClassroom課題を確認できません。");
      const queues = await loadQueues();
      await saveQueues(queues);
      const queue = CgaCore.getQueue(queues, context.courseId, context.courseworkId, Date.now());
      sendResponse({ ok: true, queue });
      return;
    }
    if (message?.type === "updateLocalQueue") {
      const context = CgaCore.parseClassroomContext(sender.url || "");
      if (!context) throw new Error("送信元のClassroom課題を確認できません。");
      const result = CgaCore.updateQueue(
        await loadQueues(), context.courseId, context.courseworkId,
        message.completedStudentIds, Date.now());
      await saveQueues(result.queues);
      sendResponse({ ok: true, remaining: result.remaining, complete: result.found && result.remaining === 0 });
      return;
    }
    if (message?.type === "deviceStatus") {
      const local = await chrome.storage.local.get({ deviceToken: "" });
      sendResponse({ ok: true, paired: /^cgd_[A-Za-z0-9_-]{20,}$/.test(local.deviceToken) });
      return;
    }
    if (message?.type === "verifiedDeviceStatus") {
      const local = await chrome.storage.local.get({ deviceToken: "" });
      if (!/^cgd_[A-Za-z0-9_-]{20,256}$/.test(local.deviceToken)) {
        sendResponse({ ok: true, localToken: false, valid: false });
        return;
      }
      try {
        const result = await apiFetch("/api/v1/extension/devices/status", {
          headers: { Authorization: `Bearer ${local.deviceToken}` }
        });
        sendResponse({ ok: true, localToken: true, valid: result?.valid === true });
      } catch (error) {
        if (error.status !== 404) throw error;
        sendResponse({ ok: true, localToken: true, valid: false });
      }
      return;
    }
    if (message?.type === "pollDraftInputJob") {
      const context = CgaCore.parseClassroomContext(sender.url || "");
      if (!context) throw new Error("送信元のClassroom課題を確認できません。");
      const local = await chrome.storage.local.get({ deviceToken: "" });
      if (!/^cgd_[A-Za-z0-9_-]{20,256}$/.test(local.deviceToken)) {
        sendResponse({ ok: true, paired: false, job: null });
        return;
      }
      const query = new URLSearchParams({ course_id: context.courseId, coursework_id: context.courseworkId });
      try {
        const raw = await apiFetch(`/api/v1/extension/draft-input-jobs/pending?${query}`, {
          headers: { Authorization: `Bearer ${local.deviceToken}` }
        });
        sendResponse({ ok: true, paired: true, job: CgaCore.validateAutomaticJob(raw, context) });
      } catch (error) {
        if (error.status !== 404) throw error;
        sendResponse({ ok: true, paired: true, job: null });
      }
      return;
    }
    if (message?.type === "reportDraftInputJob") {
      const context = CgaCore.parseClassroomContext(sender.url || "");
      if (!context) throw new Error("送信元のClassroom課題を確認できません。");
      const jobId = String(message.jobId || "");
      if (!/^[0-9a-f]{12}$/.test(jobId)) throw new Error("自動入力ジョブIDが不正です。");
      const results = CgaCore.validateAutomaticResults(message.results);
      const local = await chrome.storage.local.get({ deviceToken: "" });
      if (!/^cgd_[A-Za-z0-9_-]{20,256}$/.test(local.deviceToken)) throw new Error("端末接続が必要です。");
      const progress = await apiFetch(`/api/v1/extension/draft-input-jobs/${jobId}/progress`, {
        method: "POST", headers: { Authorization: `Bearer ${local.deviceToken}` },
        body: JSON.stringify({ results })
      });
      sendResponse({ ok: true, progress });
      return;
    }
    if (message?.type === "unpairDevice") {
      await chrome.storage.local.remove("deviceToken");
      sendResponse({ ok: true });
      return;
    }
    if (message?.type === "claimBatch") {
      const code = String(message.pairingCode || "").trim();
      if (!/^[A-Za-z0-9-]{6,64}$/.test(code)) throw new Error("初回接続コード（端末ペアリング）の形式が不正です。");
      const context = message.context;
      const senderContext = CgaCore.parseClassroomContext(sender.url || "");
      if (!senderContext || senderContext.courseId !== context?.courseId || senderContext.courseworkId !== context?.courseworkId) {
        throw new Error("送信元のClassroom課題を確認できません。");
      }
      let claim;
      let device = null;
      try {
        device = await apiFetch("/api/v1/extension/devices/claim", {
          method: "POST", body: JSON.stringify({ pairing_code: code })
        });
      } catch (error) {
        if (error.status !== 404) throw error;
      }
      if (device) {
        if (!/^cgd_[A-Za-z0-9_-]{20,256}$/.test(String(device.device_token || ""))) {
          throw new Error("端末token応答が不正です。");
        }
        await chrome.storage.local.set({ deviceToken: device.device_token });
        claim = await apiFetch("/api/v1/extension/device-batches/claim", {
          method: "POST", headers: { Authorization: `Bearer ${device.device_token}` },
          body: JSON.stringify({ course_id: context.courseId, coursework_id: context.courseworkId })
        });
      } else {
        // Legacy: old per-batch pairing code remains supported.
        claim = await apiFetch("/api/v1/extension/pairings/claim", {
          method: "POST", body: JSON.stringify({ pairing_code: code, course_id: context.courseId, coursework_id: context.courseworkId })
        });
      }
      if (!/^[A-Za-z0-9_-]{8,256}$/.test(String(claim.batch_id || "")) ||
          !/^[A-Za-z0-9_-]{20,512}$/.test(String(claim.access_token || ""))) {
        throw new Error("サーバーのcapability応答が不正です。");
      }
      const id = encodeURIComponent(claim.batch_id);
      const batch = await apiFetch(`/api/v1/extension/batches/${id}`, {
        headers: { Authorization: `Bearer ${claim.access_token}` }
      });
      sendResponse({ ok: true, batch, receipt: { batchId: claim.batch_id, token: claim.access_token } });
      return;
    }
    if (message?.type === "claimLatestBatch") {
      const context = message.context;
      const senderContext = CgaCore.parseClassroomContext(sender.url || "");
      if (!senderContext || senderContext.courseId !== context?.courseId || senderContext.courseworkId !== context?.courseworkId) {
        throw new Error("送信元のClassroom課題を確認できません。");
      }
      const local = await chrome.storage.local.get({ deviceToken: "" });
      if (!/^cgd_[A-Za-z0-9_-]{20,256}$/.test(local.deviceToken)) throw new Error("初回接続コードで端末を接続してください。");
      const claim = await apiFetch("/api/v1/extension/device-batches/claim", {
        method: "POST", headers: { Authorization: `Bearer ${local.deviceToken}` },
        body: JSON.stringify({ course_id: context.courseId, coursework_id: context.courseworkId })
      });
      const id = encodeURIComponent(claim.batch_id);
      const batch = await apiFetch(`/api/v1/extension/batches/${id}`, {
        headers: { Authorization: `Bearer ${claim.access_token}` }
      });
      sendResponse({ ok: true, batch, receipt: { batchId: claim.batch_id, token: claim.access_token } });
      return;
    }
    if (message?.type === "consumeBatch") {
      if (!CgaCore.parseClassroomContext(sender.url || "")) throw new Error("送信元を確認できません。");
      if (!/^[A-Za-z0-9_-]{8,256}$/.test(String(message.receipt?.batchId || "")) ||
          !/^[A-Za-z0-9_-]{20,512}$/.test(String(message.receipt?.token || ""))) {
        throw new Error("バッチcapabilityが不正です。");
      }
      const id = encodeURIComponent(message.receipt.batchId);
      await apiFetch(`/api/v1/extension/batches/${id}/consume`, {
        method: "POST",
        headers: { Authorization: `Bearer ${message.receipt.token}` },
        body: JSON.stringify({ attempted: message.summary.attempted, filled: message.summary.filled, skipped: message.summary.skipped, failed: message.summary.failed })
      });
      sendResponse({ ok: true });
      return;
    }
    throw new Error("不明な要求です。");
  })().catch((error) => sendResponse({ ok: false, error: error.message }));
  return true;
});
