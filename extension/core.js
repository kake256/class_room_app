(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CgaCore = api;
})(globalThis, function () {
  "use strict";

  function normalizeClassroomId(value) {
    const raw = String(value || "");
    if (!raw || !/^[A-Za-z0-9_-]+$/.test(raw)) return raw;
    try {
      const base64 = raw.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - raw.length % 4) % 4);
      const decoded = typeof atob === "function"
        ? atob(base64)
        : Buffer.from(base64, "base64").toString("binary");
      return /^\d+$/.test(decoded) ? decoded : raw;
    } catch (_error) {
      return raw;
    }
  }

  function parseClassroomContext(url) {
    const u = new URL(url);
    if (u.origin !== "https://classroom.google.com") return null;
    const match = u.pathname.match(/\/c\/([^/]+)\/a\/([^/]+)\/submissions(?:\/|$)/);
    return match ? {
      courseId: normalizeClassroomId(match[1]),
      courseworkId: normalizeClassroomId(match[2])
    } : null;
  }

  function validateServerUrl(value) {
    const u = new URL(String(value || "").trim());
    if (u.username || u.password || u.search || u.hash) throw new Error("サーバーURLに認証情報・クエリ・フラグメントは指定できません。");
    const local = ["localhost", "127.0.0.1", "[::1]"].includes(u.hostname);
    if (u.protocol !== "https:" && !(u.protocol === "http:" && local)) throw new Error("HTTPS（localhostのみHTTP可）のURLを指定してください。");
    return u.origin;
  }

  function hostPermissionPattern(serverUrl) {
    const u = new URL(validateServerUrl(serverUrl));
    // Chrome match patterns do not include a port; host permission covers that host's ports.
    return `${u.protocol}//${u.hostname}/*`;
  }

  function validateBatch(batch, context) {
    if (!batch || typeof batch !== "object") throw new Error("バッチ応答が不正です。");
    if (String(batch.course_id) !== context.courseId || String(batch.coursework_id) !== context.courseworkId) {
      throw new Error("バッチと表示中のコースまたは課題が一致しません。");
    }
    const max = Number(batch.max_points);
    if (!Number.isFinite(max) || max <= 0) throw new Error("満点が不正です。");
    if (!Array.isArray(batch.items) || batch.items.length > 500) throw new Error("入力項目が不正です。");
    const seen = new Set();
    const entries = batch.items.map((item) => {
      const studentId = String(item.student_id || "").trim();
      const score = Number(item.score);
      if (!studentId || seen.has(studentId)) throw new Error("学生IDが空または重複しています。");
      if (!Number.isFinite(score) || score < 0 || score > max) throw new Error("0点以上、満点以下の有限値だけ入力できます。");
      seen.add(studentId);
      const studentName = String(item.student_name || "").trim();
      if (studentName.length > 200 || /[\u0000-\u001f\u007f]/.test(studentName)) throw new Error("学生名が不正です。");
      return { studentId, studentName, score };
    });
    return { batchId: String(batch.batch_id || ""), maxPoints: max, entries };
  }

  function validateAutomaticJob(job, context) {
    if (!job || typeof job !== "object" || Array.isArray(job)) throw new Error("自動入力ジョブ応答が不正です。");
    if (!/^[0-9a-f]{12}$/.test(String(job.job_id || ""))) throw new Error("自動入力ジョブIDが不正です。");
    if (String(job.course_id) !== context.courseId || String(job.coursework_id) !== context.courseworkId) {
      throw new Error("自動入力ジョブと表示中のコースまたは課題が一致しません。");
    }
    const validated = validateBatch({
      batch_id: job.job_id, course_id: job.course_id, coursework_id: job.coursework_id,
      max_points: job.max_points, items: job.items
    }, context);
    const expiresAt = Number(job.expires_at);
    if (!Number.isInteger(expiresAt) || expiresAt <= 0) throw new Error("自動入力ジョブの有効期限が不正です。");
    return { jobId: validated.batchId, maxPoints: validated.maxPoints,
      entries: validated.entries, expiresAt };
  }

  function validateAutomaticResults(results) {
    if (!Array.isArray(results) || results.length > 500) throw new Error("自動入力の結果件数が不正です。");
    const seen = new Set();
    return results.map((item) => {
      const studentId = String(item?.student_id || "");
      const outcome = String(item?.outcome || "");
      if (!/^[A-Za-z0-9_-]{1,256}$/.test(studentId) || seen.has(studentId)) throw new Error("自動入力の学生参照が不正です。");
      if (!["filled", "existing", "failed"].includes(outcome)) throw new Error("自動入力の結果が不正です。");
      seen.add(studentId);
      return { student_id: studentId, outcome };
    });
  }

  const TRANSFER_TYPE = "CGA_EXTENSION_TRANSFER_V1";
  const TRANSFER_TTL_MS = 30 * 60 * 1000;

  function validateTransferPayload(payload) {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("転送データが不正です。");
    const expected = ["course_id", "coursework_id", "items", "max_points"];
    if (Object.keys(payload).sort().join("|") !== expected.join("|")) throw new Error("転送項目が不正です。");
    const courseId = String(payload.course_id || "");
    const courseworkId = String(payload.coursework_id || "");
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(courseId) || !/^[A-Za-z0-9_-]{1,128}$/.test(courseworkId)) throw new Error("課題IDが不正です。");
    const maxPoints = Number(payload.max_points);
    if (!Number.isFinite(maxPoints) || maxPoints <= 0) throw new Error("満点が不正です。");
    if (!Array.isArray(payload.items) || payload.items.length < 1 || payload.items.length > 500) throw new Error("転送件数が不正です。");
    const seen = new Set();
    const items = payload.items.map((item) => {
      if (!item || typeof item !== "object") throw new Error("転送項目が不正です。");
      const keys = Object.keys(item).sort().join("|");
      if (keys !== "score|student_id" && keys !== "score|student_id|student_name") throw new Error("転送項目が不正です。");
      const studentId = String(item.student_id || "");
      const studentName = String(item.student_name || "").trim();
      const score = Number(item.score);
      if (!/^[A-Za-z0-9_-]{1,256}$/.test(studentId) || seen.has(studentId)) throw new Error("学生参照が不正です。");
      if (studentName.length > 200 || /[\u0000-\u001f\u007f]/.test(studentName)) throw new Error("学生名が不正です。");
      if (!Number.isFinite(score) || score < 0 || score > maxPoints) throw new Error("点数が不正です。");
      seen.add(studentId);
      return { student_id: studentId, student_name: studentName, score };
    });
    return { course_id: courseId, coursework_id: courseworkId, max_points: maxPoints, items };
  }

  function validateTransferEnvelope(envelope, expectedOrigin) {
    if (!envelope?.sourceIsWindow || envelope.origin !== expectedOrigin || envelope.data?.type !== TRANSFER_TYPE) throw new Error("転送元が不正です。");
    if (!/^[A-Za-z0-9_-]{16,80}$/.test(String(envelope.data.requestId || ""))) throw new Error("転送要求が不正です。");
    return { requestId: envelope.data.requestId, payload: validateTransferPayload(envelope.data.payload) };
  }

  function queueKey(courseId, courseworkId) { return `${courseId}:${courseworkId}`; }
  function purgeQueues(queues, now) {
    return Object.fromEntries(Object.entries(queues || {}).filter(([, value]) => value?.expires_at > now));
  }
  function putQueue(queues, payload, now) {
    const safe = validateTransferPayload(payload);
    const next = purgeQueues(queues, now);
    next[queueKey(safe.course_id, safe.coursework_id)] = { ...safe, created_at: now, expires_at: now + TRANSFER_TTL_MS };
    return next;
  }
  function getQueue(queues, courseId, courseworkId, now) {
    return purgeQueues(queues, now)[queueKey(courseId, courseworkId)] || null;
  }
  function updateQueue(queues, courseId, courseworkId, completedIds, now) {
    const next = purgeQueues(queues, now);
    const key = queueKey(courseId, courseworkId);
    const queue = next[key];
    if (!queue) return { queues: next, remaining: 0, found: false };
    if (!Array.isArray(completedIds) || completedIds.length > 500) throw new Error("完了件数が不正です。");
    const completed = new Set(completedIds.map(String));
    queue.items = queue.items.filter((item) => !completed.has(item.student_id));
    const remaining = queue.items.length;
    if (!remaining) delete next[key];
    return { queues: next, remaining, found: true };
  }

  function applyLocalGradeOutcome(state, studentId, outcome) {
    if (!state?.pending?.has(studentId)) return false;
    if (outcome === "filled") {
      state.filled.add(studentId);
      state.completed.add(studentId);
      state.pending.delete(studentId);
      return true;
    }
    if (outcome === "existing") {
      state.existing.add(studentId);
      state.completed.add(studentId);
      state.pending.delete(studentId);
      return true;
    }
    return false;
  }

  return { normalizeClassroomId, parseClassroomContext, validateServerUrl, hostPermissionPattern,
    validateBatch, validateAutomaticJob, validateAutomaticResults,
    validateTransferPayload, validateTransferEnvelope, purgeQueues, putQueue,
    getQueue, updateQueue, applyLocalGradeOutcome, TRANSFER_TYPE, TRANSFER_TTL_MS };
});
