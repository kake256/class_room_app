"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const { normalizeClassroomId, parseClassroomContext, validateServerUrl, hostPermissionPattern,
  validateBatch, validateAutomaticJob, validateAutomaticResults,
  validateTransferPayload, validateTransferEnvelope, putQueue, getQueue,
  updateQueue, applyLocalGradeOutcome, TRANSFER_TTL_MS } = require("../core.js");

test("base64urlのASCII数字だけをIDへ復号する", () => {
  assert.equal(normalizeClassroomId("MTIzNDU2"), "123456");
  assert.equal(normalizeClassroomId("course"), "course");
  assert.equal(normalizeClassroomId("bad%value"), "bad%value");
});

test("submissions URLからcontextを抽出する", () => {
  assert.deepEqual(parseClassroomContext("https://classroom.google.com/c/course/a/work/submissions/by-status/and-sort-name/all"), { courseId: "course", courseworkId: "work" });
  assert.equal(parseClassroomContext("https://classroom.google.com/c/course"), null);
  assert.deepEqual(parseClassroomContext("https://classroom.google.com/c/MjAwMDAwMDAwMDAx/a/MTAwMDAwMDAwMDAx/submissions"), { courseId: "200000000001", courseworkId: "100000000001" });
});
test("server URLはHTTPSまたはlocalhost HTTPに限定する", () => {
  assert.equal(validateServerUrl("https://grader.example/a"), "https://grader.example");
  assert.equal(validateServerUrl("http://localhost:8800"), "http://localhost:8800");
  assert.throws(() => validateServerUrl("http://grader.example"));
  assert.equal(hostPermissionPattern("http://localhost:8800"), "http://localhost/*");
});
test("batchの文脈と各点数を検証する", () => {
  const context = { courseId: "c", courseworkId: "w" };
  const entry = validateBatch({ batch_id: "b", course_id: "c", coursework_id: "w", max_points: 10, items: [{ student_id: "s", student_name: "Student", score: 8 }] }, context).entries[0];
  assert.equal(entry.score, 8);
  assert.equal(entry.studentName, "Student");
  assert.throws(() => validateBatch({ course_id: "x", coursework_id: "w", max_points: 10, items: [] }, context));
  assert.throws(() => validateBatch({ course_id: "c", coursework_id: "w", max_points: 10, items: [{ student_id: "s", score: 11 }] }, context));
});

test("自動ジョブは課題文脈・ID・点数・報告結果を固定検証する", () => {
  const context = { courseId: "c", courseworkId: "w" };
  const raw = { job_id: "abcdef123456", course_id: "c", coursework_id: "w",
    max_points: 10, expires_at: 2_000_000_000,
    items: [{ student_id: "s1", score: 8 }] };
  assert.equal(validateAutomaticJob(raw, context).jobId, "abcdef123456");
  assert.throws(() => validateAutomaticJob({ ...raw, coursework_id: "wrong" }, context));
  assert.throws(() => validateAutomaticJob({ ...raw, job_id: "../unsafe" }, context));
  assert.deepEqual(validateAutomaticResults([
    { student_id: "s1", outcome: "filled" },
    { student_id: "s2", outcome: "existing" },
    { student_id: "s3", outcome: "failed" },
  ]).map((item) => item.outcome), ["filled", "existing", "failed"]);
  assert.throws(() => validateAutomaticResults([{ student_id: "s1", outcome: "skipped" }]));
  assert.throws(() => validateAutomaticResults([
    { student_id: "s1", outcome: "filled" }, { student_id: "s1", outcome: "existing" }]));
});

test("WebUI転送payloadとoriginを固定検証する", () => {
  const payload = { course_id: "c", coursework_id: "w", max_points: 10,
    items: [{ student_id: "s1", student_name: "A", score: 0 }, { student_id: "s2", student_name: "B", score: 10 }] };
  assert.equal(validateTransferPayload(payload).items.length, 2);
  assert.throws(() => validateTransferPayload({ ...payload, items: [{ student_id: "s", score: 11 }] }));
  assert.throws(() => validateTransferPayload({ ...payload, reason: "must not pass" }));
  assert.throws(() => validateTransferPayload({ ...payload, items: Array(501).fill({ student_id: "x", score: 1 }) }));
  const data = { type: "CGA_EXTENSION_TRANSFER_V1", requestId: "request_123456789",
    payload };
  assert.equal(validateTransferEnvelope({ sourceIsWindow: true,
    origin: "https://classroom-grader-1.tail80e540.ts.net", data },
  "https://classroom-grader-1.tail80e540.ts.net").payload.course_id, "c");
  assert.throws(() => validateTransferEnvelope({ sourceIsWindow: false,
    origin: "https://classroom-grader-1.tail80e540.ts.net", data },
  "https://classroom-grader-1.tail80e540.ts.net"));
  assert.throws(() => validateTransferEnvelope({ sourceIsWindow: true,
    origin: "https://evil.example", data },
  "https://classroom-grader-1.tail80e540.ts.net"));
});

test("local queueは課題分離・TTL・部分成功・0件保持・全件消去を守る", () => {
  const now = 1_000_000;
  const payload = { course_id: "c", coursework_id: "w", max_points: 10,
    items: [{ student_id: "s1", score: 1 }, { student_id: "s2", score: 2 }] };
  let queues = putQueue({}, payload, now);
  assert.equal(getQueue(queues, "c", "wrong", now), null);
  assert.equal(getQueue(queues, "c", "w", now).items.length, 2);
  let result = updateQueue(queues, "c", "w", [], now + 1);
  assert.equal(result.remaining, 2);
  result = updateQueue(result.queues, "c", "w", ["s1"], now + 2);
  assert.equal(result.remaining, 1);
  result = updateQueue(result.queues, "c", "w", ["s2"], now + 3);
  assert.equal(result.remaining, 0);
  queues = putQueue({}, payload, now);
  assert.equal(getQueue(queues, "c", "w", now + TRANSFER_TTL_MS + 1), null);
});

test("既存点と入力成功はlocal queueのterminal完了として除外する", () => {
  const state = { pending: new Map([["existing", 7], ["filled", 8], ["failed", 9]]),
    completed: new Set(), filled: new Set(), existing: new Set() };
  assert.equal(applyLocalGradeOutcome(state, "existing", "existing"), true);
  assert.equal(applyLocalGradeOutcome(state, "filled", "filled"), true);
  assert.equal(applyLocalGradeOutcome(state, "failed", "failed"), false);
  assert.deepEqual([...state.pending.keys()], ["failed"]);
  assert.deepEqual([...state.completed], ["existing", "filled"]);
  assert.deepEqual([...state.existing], ["existing"]);
  assert.deepEqual([...state.filled], ["filled"]);
});
