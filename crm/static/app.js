"use strict";
/* Small behaviours only. Every screen works without JavaScript except the audio player,
   which cannot exist without it. No framework, no build step. */

// --- filters submit themselves, so nobody hunts for a "apply" button ---------
document.querySelectorAll("form.auto select, form.auto input[type=date]").forEach((node) =>
  node.addEventListener("change", () => node.form.submit()),
);

// --- the recording player ----------------------------------------------------
(function player() {
  const audio = document.getElementById("recording");
  if (!audio) return;

  const playButton = document.getElementById("play");
  const readout = document.getElementById("at");
  const ruler = document.getElementById("ruler");
  const played = ruler && ruler.querySelector(".played");
  const totalLabel = document.getElementById("total");
  const fallback = Number(audio.dataset.duration || 0);

  const clock = (value) => {
    const total = Math.max(0, Math.round(value || 0));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
  };
  const length = () => (audio.duration && isFinite(audio.duration) ? audio.duration : fallback);

  function marks() {
    if (!ruler) return;
    ruler.querySelectorAll("button, .tick").forEach((node) => node.remove());
    const total = length();
    if (!total) return;
    const step = total > 120 ? 30 : 10;
    for (let second = step; second < total; second += step) {
      const tick = document.createElement("div");
      tick.className = "tick";
      tick.style.left = `${(second / total) * 100}%`;
      ruler.append(tick);
    }
    document.querySelectorAll("[data-at]").forEach((source, index) => {
      const at = Number(source.dataset.at) / 1000;
      if (!at) return;
      const mark = document.createElement("button");
      mark.type = "button";
      mark.textContent = String(index + 1);
      mark.title = `${clock(at)} — ${source.dataset.question || ""}`;
      mark.style.left = `${Math.min(100, (at / total) * 100)}%`;
      mark.addEventListener("click", (event) => { event.stopPropagation(); seek(at); });
      ruler.append(mark);
    });
    if (totalLabel) totalLabel.textContent = clock(total);
  }

  function seek(seconds) {
    audio.currentTime = Math.max(0, seconds);
    audio.play().catch(() => {});
  }

  if (playButton) {
    playButton.addEventListener("click", () => {
      if (audio.paused) audio.play().catch(() => {}); else audio.pause();
    });
  }
  audio.addEventListener("play", () => { if (playButton) playButton.textContent = "❙❙"; });
  audio.addEventListener("pause", () => { if (playButton) playButton.textContent = "▶"; });
  audio.addEventListener("loadedmetadata", marks);
  audio.addEventListener("timeupdate", () => {
    const total = length();
    if (played) played.style.width = total ? `${(audio.currentTime / total) * 100}%` : "0";
    if (readout) readout.textContent = clock(audio.currentTime);
  });
  audio.addEventListener("error", () => {
    const box = document.getElementById("player");
    if (box) box.innerHTML = `<div class="empty">${box.dataset.missing || ""}</div>`;
  });
  if (ruler) {
    ruler.addEventListener("click", (event) => {
      const total = length();
      if (!total) return;
      const box = ruler.getBoundingClientRect();
      seek(((event.clientX - box.left) / box.width) * total);
    });
  }
  document.querySelectorAll("button[data-at]").forEach((button) =>
    button.addEventListener("click", () => seek(Number(button.dataset.at) / 1000)),
  );
  const speed = document.getElementById("speed");
  if (speed) {
    speed.addEventListener("click", () => {
      const steps = [1, 1.5, 2];
      const next = steps[(steps.indexOf(audio.playbackRate) + 1) % steps.length];
      audio.playbackRate = next;
      speed.textContent = `${next}×`;
    });
  }
  document.addEventListener("keydown", (event) => {
    if (["INPUT", "SELECT", "TEXTAREA"].includes(document.activeElement?.tagName)) return;
    if (event.code === "Space") { event.preventDefault(); playButton?.click(); }
    if (event.code === "ArrowLeft") seek(audio.currentTime - 5);
    if (event.code === "ArrowRight") seek(audio.currentTime + 5);
  });
  marks();
})();

// --- "/" focuses the search box, the way every console does ------------------
document.addEventListener("keydown", (event) => {
  if (event.key !== "/" || ["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement?.tagName)) return;
  const search = document.querySelector("form.search input");
  if (search) { event.preventDefault(); search.focus(); }
});


// --- the dashboard keeps itself up to date -----------------------------------
// Numbers change while you watch them: a supervisor should not have to reload a page
// to see that a call is happening right now.
(function liveDashboard() {
  const box = document.getElementById("figures");
  if (!box) return;

  const EVERY_MS = 10000;
  const updatedAt = document.getElementById("updated");
  const state = document.getElementById("bridge-state");
  const bridgeText = document.getElementById("bridge-text");
  const unhandledRow = document.getElementById("unhandled-row");
  const strings = document.body.dataset;

  /* Zero means "nothing answered yet", which the server-rendered page shows as a dash.
     Printing "0 ms" here made a quiet morning read as if answers took no time at all. */
  const asMs = (value) =>
    !value ? "—" : value >= 1000 ? `${(value / 1000).toFixed(1)} s` : `${Math.round(value)} ms`;

  /* Counts from the old number to the new one, so a change is noticed instead of
     silently replacing a digit. Long jumps are still short in time. */
  function countTo(node, from, to, format) {
    const started = performance.now();
    const duration = Math.min(600, 200 + Math.abs(to - from) * 8);
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced || from === to) {
      node.textContent = format(to);
      return;
    }
    function frame(now) {
      const share = Math.min(1, (now - started) / duration);
      const eased = 1 - Math.pow(1 - share, 3);
      node.textContent = format(Math.round(from + (to - from) * eased));
      if (share < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function apply(figures) {
    document.querySelectorAll("[data-figure]").forEach((node) => {
      const key = node.dataset.figure;
      if (!(key in figures)) return;
      const format = node.dataset.format === "ms" ? asMs : String;
      // "1.5 s" is 1500, not 15: read the number the way it was written.
      const shown = String(node.textContent).trim();
      const before = shown.endsWith("s")
        ? Math.round(parseFloat(shown) * 1000) || 0
        : parseInt(shown.replace(/[^\d-]/g, ""), 10) || 0;
      const after = Number(figures[key]) || 0;
      if (format(after) === node.textContent) return;
      countTo(node, before, after, format);
      const tile = node.closest(".tile") || node;
      tile.classList.remove("updating");
      void tile.offsetWidth;            // restart the flash even on a repeated change
      tile.classList.add("updating");
    });
    if (unhandledRow) unhandledRow.classList.toggle("hidden", !figures.unhandled_calls);
  }

  async function refresh() {
    try {
      const response = await fetch("/api/dashboard", { headers: { Accept: "application/json" } });
      if (response.status === 401) {
        window.location.href = "/login?next=/";   // the session ended while the tab was open
        return;
      }
      if (!response.ok) return;
      const data = await response.json();
      apply(data.figures);
      if (state) {
        const pulse = state.querySelector(".pulse");
        pulse.className = `pulse${data.bridge.up ? (data.bridge.active_calls ? " busy" : "") : " down"}`;
        if (bridgeText) {
          bridgeText.textContent = data.bridge.up
            ? (data.bridge.active_calls ? `${data.bridge.active_calls} · ` : "") + strings.serviceOk
            : strings.serviceDown;
        }
      }
      if (updatedAt) updatedAt.textContent = `${strings.updated} ${data.at}`;
    } catch {
      /* a refresh that did not happen is not worth showing anybody */
    }
  }

  refresh();
  setInterval(refresh, EVERY_MS);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) refresh();    // back to the tab: show the truth immediately
  });
})();
