(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.CgaSelectors = api;
})(globalThis, function () {
  "use strict";
  // Classroom DOM changes should be handled only in this module.
  const SELECTORS = Object.freeze({
    emptyGradeButton: '[role="button"][aria-label*="成績を追加"], [role="button"][aria-label*="Add grade"]',
    anyGradeButton: '[role="button"][aria-label*="成績を追加"], [role="button"][aria-label*="Add grade"], [role="button"][aria-label*="さんの成績"], [role="button"][aria-label*="grade for" i]',
    editInput: 'input[aria-label="成績を編集"], input[aria-label="Edit grade"]',
    studentLinks: 'a[href*="/submissions/"]'
  });

  function normalizeStudentName(value) {
    return String(value || "").replace(/AR\d{5}/ig, "").replace(/[\s　]/g, "").trim();
  }

  function studentNameForGradeButton(button) {
    const label = String(button?.getAttribute?.("aria-label") || "");
    return normalizeStudentName(label
      .replace(/さんの成績.*$/i, "")
      .replace(/(?:add|edit)?\s*grade\s+for\s*/i, "")
      .replace(/(?:'s)?\s*grade.*$/i, ""));
  }

  function studentIdForGradeButton(button) {
    let node = button;
    for (let depth = 0; node && depth < 32; depth += 1, node = node.parentElement) {
      const ids = new Set();
      for (const link of node.querySelectorAll?.(SELECTORS.studentLinks) || []) {
        const match = new URL(link.href, location.href).pathname.match(/\/submissions\/([^/?#]+)/);
        if (match) ids.add(CgaCore.normalizeClassroomId(decodeURIComponent(match[1])));
      }
      // Never guess once an ancestor spans multiple students.
      if (ids.size === 1) return ids.values().next().value;
      if (ids.size > 1) return null;
    }
    return null;
  }

  function gradeButtons(root = document) {
    return [...new Set(root.querySelectorAll(SELECTORS.anyGradeButton))];
  }

  function gradeButtonForStudent(studentId, studentName = "", root = document) {
    const buttons = gradeButtons(root);
    const idMatches = buttons.filter(
      (button) => studentIdForGradeButton(button) === String(studentId));
    if (idMatches.length === 1) return idMatches[0];
    if (idMatches.length > 1) return null;
    const wanted = normalizeStudentName(studentName);
    if (!wanted) return null;
    const nameMatches = buttons.filter(
      (button) => studentNameForGradeButton(button) === wanted);
    return nameMatches.length === 1 ? nameMatches[0] : null;
  }

  function pendingStudentForGradeButton(button, pending) {
    const id = studentIdForGradeButton(button);
    if (id && pending.has(id)) return id;
    const name = studentNameForGradeButton(button);
    if (!name) return null;
    const matches = [...pending.entries()].filter(([, item]) =>
      normalizeStudentName(item.studentName) === name);
    return matches.length === 1 ? matches[0][0] : null;
  }

  function isVisibleEditInput(input, view = globalThis) {
    if (!input?.matches?.(SELECTORS.editInput) || input.hidden || input.disabled
        || input.getAttribute?.("aria-hidden") === "true") return false;
    const style = view.getComputedStyle?.(input);
    if (style && (style.display === "none" || style.visibility === "hidden"
        || style.visibility === "collapse")) return false;
    const rects = input.getClientRects?.();
    return !rects || rects.length > 0;
  }

  function activeEditInput(root = document, view = globalThis) {
    const active = root.activeElement;
    if (isVisibleEditInput(active, view)) return active;
    return [...root.querySelectorAll(SELECTORS.editInput)].find(
      (input) => isVisibleEditInput(input, view)) || null;
  }

  return { SELECTORS, normalizeStudentName, studentNameForGradeButton,
    studentIdForGradeButton, gradeButtons, gradeButtonForStudent,
    pendingStudentForGradeButton, isVisibleEditInput, activeEditInput };
});
