"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
global.location = { href: "https://classroom.google.com/c/c/a/w/submissions" };
global.CgaCore = { normalizeClassroomId: (value) => value };
const { SELECTORS, normalizeStudentName, studentNameForGradeButton,
  studentIdForGradeButton, gradeButtons, gradeButtonForStudent,
  pendingStudentForGradeButton, isVisibleEditInput, activeEditInput } = require("../selectors.js");

function node(links = [], parentElement = null) {
  return { parentElement, getAttribute: () => "", querySelectorAll: () => links.map((id) => ({
    href: `https://classroom.google.com/c/c/a/w/submissions/${id}`
  })) };
}

test("深い行コンテナまで探索するが複数学生IDの祖先では誤照合しない", () => {
  const row = node(["student-one"]);
  let child = row;
  for (let index = 0; index < 12; index += 1) child = node([], child);
  assert.equal(studentIdForGradeButton(child), "student-one");
  const ambiguous = node(["student-one", "student-two"]);
  child = node([], ambiguous);
  assert.equal(studentIdForGradeButton(child), null);
});

test("空欄と既存点を列挙し重複要素を除く", () => {
  assert.match(SELECTORS.anyGradeButton, /成績を追加/);
  assert.match(SELECTORS.anyGradeButton, /Add grade/);
  assert.match(SELECTORS.anyGradeButton, /さんの成績/);
  assert.match(SELECTORS.anyGradeButton, /grade for/);
  const empty = node(["student-one"]), existing = node(["student-two"]);
  const root = { querySelectorAll: () => [empty, existing, empty] };
  assert.deepEqual(gradeButtons(root), [empty, existing]);
  assert.equal(gradeButtonForStudent("student-one", "", root), empty);
  const ambiguous = { querySelectorAll: () => [empty, node(["student-one"])] };
  assert.equal(gradeButtonForStudent("student-one", "", ambiguous), null);
});

test("IDリンクがない場合だけTampermonkey互換の氏名で一意照合する", () => {
  function named(label) {
    return { parentElement: null, getAttribute: (key) => key === "aria-label" ? label : null,
      querySelectorAll: () => [] };
  }
  const taro = named("AR12345 テスト 太郎 さんの成績を追加");
  const hanako = named("テスト花子 さんの成績");
  const root = { querySelectorAll: () => [taro, hanako] };
  assert.equal(normalizeStudentName("AR12345 テスト　太郎"), "テスト太郎");
  assert.equal(studentNameForGradeButton(taro), "テスト太郎");
  assert.equal(gradeButtonForStudent("111", "テスト 太郎", root), taro);
  const pending = new Map([["111", { studentName: "テスト 太郎" }],
    ["222", { studentName: "テスト花子" }]]);
  assert.equal(pendingStudentForGradeButton(taro, pending), "111");
  const duplicate = new Map([["111", { studentName: "同姓同名" }],
    ["222", { studentName: "同姓 同名" }]]);
  assert.equal(pendingStudentForGradeButton(named("同姓同名 さんの成績を追加"), duplicate), null);
});

function editInput({ hidden = false, disabled = false, ariaHidden = "false",
  display = "block", visibility = "visible", rects = 1 } = {}) {
  return {
    hidden, disabled, matches: () => true,
    getAttribute: (name) => name === "aria-hidden" ? ariaHidden : null,
    getClientRects: () => Array(rects).fill({}),
    styleForTest: { display, visibility }
  };
}

test("不可視persistent inputを無視し可視編集inputだけを停止対象にする", () => {
  const view = { getComputedStyle: (input) => input.styleForTest };
  const hidden = editInput({ hidden: true });
  const ariaHidden = editInput({ ariaHidden: "true" });
  const displayNone = editInput({ display: "none" });
  const noRect = editInput({ rects: 0 });
  const visible = editInput();
  for (const input of [hidden, ariaHidden, displayNone, noRect]) {
    assert.equal(isVisibleEditInput(input, view), false);
  }
  assert.equal(isVisibleEditInput(visible, view), true);
  const root = { activeElement: hidden, querySelectorAll: () => [hidden, noRect, visible] };
  assert.equal(activeEditInput(root, view), visible);
  assert.equal(activeEditInput({ activeElement: hidden, querySelectorAll: () => [hidden] }, view), null);
});
