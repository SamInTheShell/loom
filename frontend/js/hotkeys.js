/* hotkeys.js — hold Ctrl to reveal what the keyboard can do, right where
 * it can do it. Any element carrying data-hotkey gets a small chip pinned
 * to its corner while Ctrl is held. Chips are drawn only for elements
 * actually visible right now, so the reveal is context-aware by
 * construction: a chat tab shows chat keys, the library shows its own.
 *
 * The chips are pure display (pointer-events: none) and sit above every
 * other layer; the actual key handling lives with each surface (main.js
 * for globals, util.js for menus/dialogs, the tabs for their widgets). */
"use strict";

const HOTKEY_REVEAL_DELAY = 250;   // ms of held Ctrl before chips appear

/* tag an element with the combo its chip should show; returns the node so
 * call sites can wrap element creation */
function setHotkey(node, combo) {
  if (node) node.setAttribute("data-hotkey", combo);
  return node;
}

function installHotkeyReveal() {
  let layer = null, timer = null, ticker = null;

  const hide = () => {
    clearTimeout(timer); timer = null;
    clearInterval(ticker); ticker = null;
    layer?.remove(); layer = null;
  };

  const draw = () => {
    if (!layer) {
      layer = el("div", { class: "hotkey-layer" });
      document.body.append(layer);
    }
    layer.replaceChildren();
    const placed = [];   // chips never overlap: collide → slide away
    const collides = (l, t, w, h) => placed.some((p) =>
      l < p.l + p.w + 2 && p.l < l + w + 2
      && t < p.t + p.h + 2 && p.t < t + h + 2);
    for (const n of document.querySelectorAll("[data-hotkey]")) {
      if (n.closest(".hidden")) continue;
      const r = n.getBoundingClientRect();
      if (!r.width || !r.height) continue;                    // display:none
      if (r.bottom < 0 || r.top > window.innerHeight
          || r.right < 0 || r.left > window.innerWidth) continue;
      const chip = el("span", { class: "kbd-chip", text: n.dataset.hotkey });
      layer.append(chip);
      const cr = chip.getBoundingClientRect();
      // centered on the element, floating just above it (below when the
      // element hugs the top edge, e.g. the top bar)
      const left = Math.max(2, Math.min(r.left + r.width / 2 - cr.width / 2,
        window.innerWidth - cr.width - 4));
      let top = r.top - cr.height - 3;
      let dir = -1;                       // collide → slide further away
      if (top < 2) { top = r.bottom + 3; dir = 1; }
      let guard = 0;
      while (collides(left, top, cr.width, cr.height) && guard++ < 40) {
        top += dir * (cr.height + 3);
      }
      top = Math.max(2, Math.min(top, window.innerHeight - cr.height - 2));
      placed.push({ l: left, t: top, w: cr.width, h: cr.height });
      chip.style.left = left + "px";
      chip.style.top = top + "px";
    }
  };

  document.addEventListener("keydown", (e) => {
    if (e.key !== "Control" || e.repeat || timer || layer) return;
    timer = setTimeout(() => {
      draw();
      // the view can change while Ctrl stays held (Ctrl+L, menus opening,
      // permission cards resolving) — keep the chips honest
      ticker = setInterval(draw, 350);
    }, HOTKEY_REVEAL_DELAY);
  }, true);
  document.addEventListener("keyup", (e) => {
    if (e.key === "Control") hide();
  }, true);
  window.addEventListener("blur", hide);
  document.addEventListener("mousedown", hide, true);
}
