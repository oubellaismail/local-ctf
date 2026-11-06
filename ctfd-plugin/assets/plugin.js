/**
 * CTFd Controller Plugin - client-side challenge instance management
 *
 * Config injected by __init__.py (after_request) as window.CTF_CONTROLLER:
 *   { team_id, is_admin }
 *
 * All controller API calls go through the CTFd proxy (/controller-proxy/...)
 * so no cluster-internal URLs or API keys are exposed to the browser.
 */

(function () {

  // ─── Flag submission intercept — GOAL! overlay ───────────────────────────
  // Must run before any auth check so it catches all flag submissions.

  // ── GOAL! overlay — video stays permanently in DOM to avoid re-init lag ─────
  // Moving a <video> element in the DOM forces the browser to re-initialize its
  // renderer, causing a blank flash. We keep both the video and the text layer
  // fixed in place at all times and just toggle their visibility.

  var GOAL_CLIP_URL = "/plugins/controller-plugin/assets/goal-clip.mp4";

  // Video layer — fixed, always in DOM, invisible until triggered
  var _goalVid = document.createElement("video");
  _goalVid.id = "ctf-goal-vid";
  _goalVid.src = GOAL_CLIP_URL;
  _goalVid.preload = "auto";
  _goalVid.muted = true;
  _goalVid.playsInline = true;
  _goalVid.setAttribute("playsinline", "");
  document.body.appendChild(_goalVid);

  // Text/overlay layer — fixed, always in DOM, invisible until triggered
  var _goalOverlay = document.createElement("div");
  _goalOverlay.id = "ctf-goal-overlay";
  _goalOverlay.innerHTML =
    '<div class="ctf-goal-inner">' +
      '<div class="ctf-goal-lines"></div>' +
      '<div class="ctf-goal-text">G O A L !</div>' +
      '<div class="ctf-goal-sub">FLAG CAPTURED</div>' +
    '</div>';
  document.body.appendChild(_goalOverlay);

  var _goalActive = false;
  var _goalDismissTimer = null;

  function showGoalOverlay() {
    if (_goalActive) return;
    _goalActive = true;

    _goalVid.currentTime = 0;
    _goalVid.play();

    _goalVid.classList.add("ctf-goal-visible");
    _goalOverlay.classList.remove("ctf-goal-fade");
    _goalOverlay.classList.add("ctf-goal-visible");

    function dismiss() {
      if (!_goalActive) return;
      _goalOverlay.classList.add("ctf-goal-fade");
      setTimeout(function () {
        _goalOverlay.classList.remove("ctf-goal-visible", "ctf-goal-fade");
        _goalVid.classList.remove("ctf-goal-visible");
        _goalVid.pause();
        _goalActive = false;
      }, 500);
    }

    _goalOverlay.onclick = dismiss;
    _goalVid.addEventListener("ended", dismiss, { once: true });
    if (_goalDismissTimer) clearTimeout(_goalDismissTimer);
    _goalDismissTimer = setTimeout(dismiss, 4500);
  }

  // ── MutationObserver — watches for CTFd success alert in the modal ──────────
  // The alert lives inside <template x-if="response"> so Alpine.js INSERTS a
  // brand new <div class="alert alert-success"> into the DOM on flag submit.
  // We must watch childList (new nodes), not just attribute changes.
  (function observeFlagSuccess() {
    var lastTrigger = 0;

    function checkForSuccess(el) {
      if (!el || !el.classList) return false;
      if (el.classList.contains("alert-success") || el.classList.contains("alert-info")) {
        var now = Date.now();
        if (now - lastTrigger < 2000) return false;
        lastTrigger = now;
        showGoalOverlay();
        return true;
      }
      return false;
    }

    function onMutation(mutations) {
      for (var i = 0; i < mutations.length; i++) {
        var m = mutations[i];
        // Primary path: Alpine x-if inserts a new element node
        if (m.type === "childList") {
          for (var j = 0; j < m.addedNodes.length; j++) {
            var node = m.addedNodes[j];
            if (node.nodeType !== 1) continue;
            if (checkForSuccess(node)) return;
            if (node.querySelectorAll) {
              var hits = node.querySelectorAll(".alert-success, .alert-info");
              for (var k = 0; k < hits.length; k++) {
                if (checkForSuccess(hits[k])) return;
              }
            }
          }
        }
        // Fallback: attribute mutation on existing element
        if (m.type === "attributes" && checkForSuccess(m.target)) return;
      }
    }

    var observer = new MutationObserver(onMutation);
    observer.observe(document.body, {
      subtree: true,
      childList: true,       // catch Alpine x-if insertions
      attributes: true,
      attributeFilter: ["class"]
    });
  })();

  // ─── Logo injection (runs for ALL visitors, including unauthenticated) ─────

  var LOGO_URL = "/plugins/controller-plugin/assets/logo-dark.png";

  function injectBackground() {
    if (document.getElementById("ctf-bg")) return;
    var el = document.createElement("div");
    el.id = "ctf-bg";
    document.body.insertBefore(el, document.body.firstChild);
  }

  function injectLogo() {
    var brand = document.querySelector(".navbar-brand");
    if (!brand) return;
    if (document.getElementById("ctf-brand-logo")) return;
    var img = document.createElement("img");
    img.id  = "ctf-brand-logo";
    img.className = "ctf-logo";
    img.src = LOGO_URL;
    img.alt = "Infodays CTF";
    img.width = 32;
    img.height = 32;
    // Remove the text next to the logo — logo only
    brand.textContent = "";
    brand.appendChild(img);
  }

  function forceDarkMode() {
    // Always enforce dark mode — toggle button hidden via CSS (.theme-switch)
    document.documentElement.setAttribute("data-bs-theme", "dark");
    // If anything tries to flip the theme, snap it back
    new MutationObserver(function() {
      if (document.documentElement.getAttribute("data-bs-theme") !== "dark") {
        document.documentElement.setAttribute("data-bs-theme", "dark");
      }
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-bs-theme"] });
  }

  function injectRulesLink() {
    var navList = document.querySelector(".navbar-nav");
    if (!navList) return;
    // Don't duplicate
    if (navList.querySelector('a[href="/rules"]')) return;
    var li = document.createElement("li");
    li.className = "nav-item";
    var a = document.createElement("a");
    a.className = "nav-link";
    a.href = "/rules";
    a.textContent = "Rules";
    li.appendChild(a);
    // Insert after the first nav item (usually Challenges)
    var firstItem = navList.querySelector(".nav-item");
    if (firstItem && firstItem.nextSibling) {
      navList.insertBefore(li, firstItem.nextSibling);
    } else {
      navList.appendChild(li);
    }
  }

  function initLogo() {
    forceDarkMode();
    injectBackground();
    injectLogo();
    injectRulesLink();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", initLogo);
  } else {
    initLogo();
  }

  // ─── Controller UI (requires authenticated team) ──────────────────────────

  var cfg = window.CTF_CONTROLLER;
  if (!cfg) return;

  var IS_ADMIN = cfg.is_admin === true;
  // Admins have no team — default to team 1 for testing; normal users must have a team
  var TEAM_ID;
  if (cfg.team_id && cfg.team_id !== null) {
    TEAM_ID = String(cfg.team_id);
  } else if (IS_ADMIN) {
    TEAM_ID = "1";
  } else {
    return; // not logged in or no team
  }

  var SERVICE_CATEGORIES = ["web", "pwn", "misc", "crypto", "forensics", "reversing", "osint", "stego"];

  // Challenge manifest ID map: { ctfd_numeric_id_string: manifest_id }
  // Fetched once from /controller-plugin/challenge-map (player-accessible).
  var challengeManifestMap = {};
  (async function loadChallengeMap() {
    try {
      var resp = await ctfdFetch("/controller-plugin/challenge-map");
      if (resp.ok) {
        challengeManifestMap = await resp.json();
      }
    } catch (e) { /* non-fatal, fall back to name slug */ }
  })();

  // Global instance state — tracks all running instances for board indicators
  var allInstanceStatuses = {}; // { challenge_id: { status, url } }
  var boardPollTimer = null;

  // Per-challenge state (reset when modal opens)
  var currentChallengeId = null;
  var currentStatus = null;
  var refreshTimer = null;

  // Multi-flag state (reset when modal opens)
  var currentNumericId = null;
  var isMultiFlag = false;
  var progressPollTimer = null;

  // ─── CTFd API helpers ───────────────────────────────────────────────────────

  function getCsrfToken() {
    // CTFd 3.8.2 stores CSRF nonce in window.init.csrfNonce
    return (window.init && window.init.csrfNonce) ? window.init.csrfNonce : "";
  }

  function ctfdFetch(path, opts) {
    var defaults = {
      headers: { "Content-Type": "application/json", "CSRF-Token": getCsrfToken() },
      credentials: "same-origin",
    };
    if (opts) {
      defaults.method = opts.method || "GET";
      if (opts.body) defaults.body = opts.body;
    }
    return fetch(path, defaults);
  }

  // Fetch challenge details (id, category) from CTFd API
  async function fetchChallengeInfo(challengeId) {
    try {
      var resp = await ctfdFetch("/api/v1/challenges/" + challengeId);
      if (!resp.ok) return null;
      var data = await resp.json();
      return data.data || null;
    } catch (e) {
      return null;
    }
  }

  // ─── Controller proxy helpers (all requests go through CTFd) ──────────────

  async function fetchStatus() {
    try {
      var resp = await ctfdFetch("/controller-proxy/status/" + TEAM_ID);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      var data = await resp.json();
      return (data.challenges || []).find(function (c) {
        return c.challenge_id === currentChallengeId;
      }) || null;
    } catch (err) {
      console.error("[ctf-controller] status fetch failed:", err);
      return undefined; // undefined = error, null = not found
    }
  }

  // ─── UI rendering ───────────────────────────────────────────────────────────

  function getOrCreateContainer() {
    var existing = document.getElementById("ctf-controller-ui");
    if (existing) return existing;

    var container = document.createElement("div");
    container.id = "ctf-controller-ui";
    container.className = "ctf-controller-ui";
    container.setAttribute("role", "status");
    container.setAttribute("aria-live", "polite");

    // Insert AFTER challenge description, BEFORE the flag submit row
    // Flow: read challenge → start instance → get URL → submit flag
    var submitRow = document.querySelector(".submit-row");
    var descEl    = document.querySelector(".challenge-desc");
    var modalBody = document.querySelector(".modal-body");

    if (submitRow) {
      submitRow.parentNode.insertBefore(container, submitRow);
    } else if (descEl) {
      descEl.parentNode.insertBefore(container, descEl.nextSibling);
    } else if (modalBody) {
      modalBody.appendChild(container);
    }
    return container;
  }

  function renderUI(challenge) {
    var container = getOrCreateContainer();
    if (!container) return;

    if (challenge === undefined) {
      renderUnknown(container);
      return;
    }

    // null means challenge is not managed by the controller (static/file
    // download challenges). Show a download button pointing to the plugin's
    // per-team zip endpoint.
    // For multi_flag static bundles: progress bar is already shown above —
    // append the download button below it without clearing the container.
    if (challenge === null) {
      if (!currentChallengeId) return;

      if (isMultiFlag) {
        // Avoid duplicating the button on repeated polls
        if (!container.querySelector(".ctf-controller-row")) {
          var dlRow = document.createElement("div");
          dlRow.className = "ctf-controller-row";
          var dlBtn = document.createElement("a");
          dlBtn.href = "/download/" + currentChallengeId;
          dlBtn.className = "btn btn-sm btn-primary w-100";
          dlBtn.textContent = "\u2B07 Download Challenge";
          dlBtn.setAttribute("download", "");
          dlRow.appendChild(dlBtn);
          container.appendChild(dlRow);
        }
        return;
      }

      container.innerHTML = "";
      var dlRow = document.createElement("div");
      dlRow.className = "ctf-controller-row";
      var dlBtn = document.createElement("a");
      dlBtn.href = "/download/" + currentChallengeId;
      dlBtn.className = "btn btn-sm btn-primary w-100";
      dlBtn.textContent = "\u2B07 Download Challenge";
      dlBtn.setAttribute("download", "");
      dlRow.appendChild(dlBtn);
      container.appendChild(dlRow);
      return;
    }

    var status = challenge.status || "fresh";
    currentStatus = status;

    // Preserve multi-flag progress bar across re-renders
    var savedProgress = container.querySelector(".ctf-mf-progress");
    container.innerHTML = "";
    if (savedProgress) container.insertBefore(savedProgress, container.firstChild);

    if (status === "fresh" || status === "suspended") {
      // ── Idle state: full-width Start button ───────────────
      var idleRow = document.createElement("div");
      idleRow.className = "ctf-action-row";

      var badge = document.createElement("span");
      badge.className = "ctf-badge " + (status === "fresh" ? "ctf-badge--fresh" : "ctf-badge--suspended");
      badge.textContent = status === "fresh" ? "Not Started" : "Stopped";

      var startBtn = document.createElement("button");
      startBtn.className = "btn btn-sm btn-primary ctf-start-btn";
      startBtn.innerHTML = "<span class='ctf-btn-icon'>\u25B6</span> Start Instance";
      startBtn.onclick = handleStart;

      idleRow.appendChild(badge);
      idleRow.appendChild(startBtn);
      container.appendChild(idleRow);

    } else if (status === "running") {
      // ── Running state: URL hero block + quiet action buttons ──

      if (challenge && challenge.url) {
        // URL is the hero — big, prominent, easy to click/copy
        var urlBlock = document.createElement("div");
        urlBlock.className = "ctf-url-block";

        if (challenge.url.startsWith("nc ")) {
          var ncLabel = document.createElement("span");
          ncLabel.className = "ctf-url-label";
          ncLabel.textContent = "CONNECT";
          var code = document.createElement("code");
          code.className = "ctf-nc-command";
          code.textContent = challenge.url;
          urlBlock.appendChild(ncLabel);
          urlBlock.appendChild(code);
        } else {
          var httpLabel = document.createElement("span");
          httpLabel.className = "ctf-url-label";
          httpLabel.textContent = "OPEN";
          var urlLink = document.createElement("a");
          urlLink.href = challenge.url;
          urlLink.target = "_blank";
          urlLink.rel = "noopener noreferrer";
          urlLink.className = "ctf-instance-url";
          urlLink.textContent = challenge.url;
          urlLink.addEventListener("click", function (e) {
            e.preventDefault();
            e.stopPropagation();
            window.open(challenge.url, "_blank", "noopener,noreferrer");
          });
          urlBlock.appendChild(httpLabel);
          urlBlock.appendChild(urlLink);
        }
        container.appendChild(urlBlock);
      }

      // Extra ports (e.g. Modbus for ait-baha-nuclear)
      if (challenge && challenge.extra_ports && Object.keys(challenge.extra_ports).length > 0) {
        var extraBlock = document.createElement("div");
        extraBlock.className = "ctf-extra-ports";
        Object.keys(challenge.extra_ports).forEach(function (portName) {
          var row = document.createElement("div");
          row.className = "ctf-extra-port-row";
          var label = document.createElement("span");
          label.className = "ctf-url-label";
          label.textContent = portName.toUpperCase();
          var code = document.createElement("code");
          code.className = "ctf-nc-command";
          code.textContent = challenge.extra_ports[portName];
          row.appendChild(label);
          row.appendChild(code);
          extraBlock.appendChild(row);
        });
        container.appendChild(extraBlock);
      }

      // Action row: status dot + quiet Stop + Restart
      var actionRow = document.createElement("div");
      actionRow.className = "ctf-action-row ctf-action-row--running";

      var runningDot = document.createElement("span");
      runningDot.className = "ctf-running-dot";
      var runningLabel = document.createElement("span");
      runningLabel.className = "ctf-running-label";
      runningLabel.textContent = "Instance running";

      var stopBtn = document.createElement("button");
      stopBtn.className = "btn btn-sm ctf-stop-btn";
      stopBtn.innerHTML = "\u23F9 Stop";
      stopBtn.onclick = handleStop;

      var restartBtn = document.createElement("button");
      restartBtn.className = "btn btn-sm ctf-restart-btn";
      restartBtn.innerHTML = "\u21BA Restart";
      restartBtn.onclick = handleRestart;

      actionRow.appendChild(runningDot);
      actionRow.appendChild(runningLabel);
      actionRow.appendChild(stopBtn);
      actionRow.appendChild(restartBtn);
      container.appendChild(actionRow);

    } else {
      renderUnknown(container);
    }
  }

  function renderUnknown(container) {
    if (!container) container = getOrCreateContainer();
    if (!container) return;

    var savedProgress = container.querySelector(".ctf-mf-progress");
    container.innerHTML = "";
    if (savedProgress) container.appendChild(savedProgress);

    var badge = document.createElement("span");
    badge.textContent = "Error";
    badge.className = "ctf-badge ctf-badge--error";
    container.appendChild(badge);

    var btn = document.createElement("button");
    btn.className = "btn btn-sm btn-secondary";
    btn.disabled = true;
    btn.textContent = "Unavailable";
    container.appendChild(btn);

    var retryLink = document.createElement("a");
    retryLink.href = "#";
    retryLink.textContent = "Retry";
    retryLink.className = "ctf-retry-link";
    retryLink.onclick = function (e) {
      e.preventDefault();
      updateUI();
    };
    container.appendChild(retryLink);
  }

  function showInlineWarning(message) {
    var container = document.getElementById("ctf-controller-ui");
    if (!container) return;

    // Remove any existing warning
    var old = container.querySelector(".ctf-inline-warning");
    if (old) old.remove();

    var warning = document.createElement("div");
    warning.className = "ctf-inline-warning";
    warning.setAttribute("role", "alert");

    var icon = document.createElement("span");
    icon.className = "ctf-warning-icon";
    icon.textContent = "\u26A0";
    icon.setAttribute("aria-hidden", "true");

    var text = document.createElement("span");
    text.textContent = message;

    var dismiss = document.createElement("button");
    dismiss.className = "ctf-warning-dismiss";
    dismiss.setAttribute("aria-label", "Dismiss warning");
    dismiss.textContent = "\u2715";
    dismiss.onclick = function () { warning.remove(); };

    warning.appendChild(icon);
    warning.appendChild(text);
    warning.appendChild(dismiss);

    // Insert at the top of the container (before the row)
    container.insertBefore(warning, container.firstChild);
  }

  function setButtonLoading(loading) {
    var btn = document.querySelector("#ctf-controller-ui button");
    if (!btn) return;
    btn.disabled = loading;
    if (loading) {
      btn.textContent = currentStatus === "running" ? "Stopping\u2026" : "Starting\u2026";
    }
  }

  // ─── Button handlers ────────────────────────────────────────────────────────

  // Render a "pod is starting, waiting for readiness" state
  function renderStarting() {
    var container = getOrCreateContainer();
    if (!container) return;
    var savedProgress = container.querySelector(".ctf-mf-progress");
    container.innerHTML = "";
    if (savedProgress) container.insertBefore(savedProgress, container.firstChild);

    var row = document.createElement("div");
    row.className = "ctf-controller-row";

    var badge = document.createElement("span");
    badge.className = "ctf-badge ctf-badge--starting";
    badge.textContent = "Starting\u2026";
    row.appendChild(badge);

    var note = document.createElement("span");
    note.className = "ctf-starting-note";
    note.textContent = "Waiting for pod to be ready\u2026";
    row.appendChild(note);

    container.appendChild(row);
  }

  async function handleStart() {
    setButtonLoading(true);
    try {
      var resp = await ctfdFetch("/controller-proxy/activate", {
        method: "POST",
        body: JSON.stringify({ team_id: TEAM_ID, challenge_id: currentChallengeId }),
      });
      var data = await resp.json();

      if (resp.status === 429) {
        showInlineWarning(data.detail || "Instance limit reached — you can run up to 4 at a time. Stop an active instance first.");
        setButtonLoading(false);
        return;
      }
      if (!resp.ok) {
        renderUnknown();
        setButtonLoading(false);
        return;
      }

      // Pod is scaling up — don't show URL yet, wait for readiness
      renderStarting();
      allInstanceStatuses[currentChallengeId] = { status: "running", url: null };
      updateBoardIndicators();

      // Poll every 4s until controller confirms pod is ready (url available)
      var waited = 0;
      var MAX_WAIT = 120000; // 2 min timeout
      var INTERVAL = 4000;

      stopPolling(); // pause normal 15s poll while we fast-poll
      var readyTimer = setInterval(async function () {
        waited += INTERVAL;
        var challenge = await fetchStatus();
        if (challenge && challenge.status === "running" && challenge.url) {
          clearInterval(readyTimer);
          renderUI(challenge);
          allInstanceStatuses[currentChallengeId] = challenge;
          updateBoardIndicators();
          startPolling(); // resume normal polling
        } else if (waited >= MAX_WAIT) {
          clearInterval(readyTimer);
          showInlineWarning("Pod is taking too long to start. Try refreshing.");
          startPolling();
        }
      }, INTERVAL);

    } catch (err) {
      console.error("[ctf-controller] start failed:", err);
      renderUnknown();
      setButtonLoading(false);
    }
  }

  async function handleStop() {
    setButtonLoading(true);
    try {
      var resp = await ctfdFetch("/controller-proxy/deactivate", {
        method: "POST",
        body: JSON.stringify({ team_id: TEAM_ID, challenge_id: currentChallengeId }),
      });

      if (!resp.ok) {
        renderUnknown();
        setButtonLoading(false);
        return;
      }

      renderUI({
        challenge_id: currentChallengeId,
        status: "suspended",
        url: null,
      });
      setButtonLoading(false);
      // Update board indicators immediately
      allInstanceStatuses[currentChallengeId] = { status: "suspended", url: null };
      updateBoardIndicators();
    } catch (err) {
      console.error("[ctf-controller] stop failed:", err);
      renderUnknown();
      setButtonLoading(false);
    }
  }

  async function handleRestart() {
    // Disable both buttons immediately
    document.querySelectorAll("#ctf-controller-ui button").forEach(function (b) { b.disabled = true; });
    try {
      // Deactivate first
      var stopResp = await ctfdFetch("/controller-proxy/deactivate", {
        method: "POST",
        body: JSON.stringify({ team_id: TEAM_ID, challenge_id: currentChallengeId }),
      });
      if (!stopResp.ok) { renderUnknown(); return; }

      // Brief pause so K8s scales down before we scale back up
      await new Promise(function (r) { setTimeout(r, 1500); });

      // Reactivate
      var startResp = await ctfdFetch("/controller-proxy/activate", {
        method: "POST",
        body: JSON.stringify({ team_id: TEAM_ID, challenge_id: currentChallengeId }),
      });
      if (!startResp.ok) { renderUnknown(); return; }

      // Wait for readiness (same logic as handleStart)
      renderStarting();
      allInstanceStatuses[currentChallengeId] = { status: "running", url: null };
      updateBoardIndicators();

      stopPolling();
      var waited = 0;
      var readyTimer = setInterval(async function () {
        waited += 4000;
        var challenge = await fetchStatus();
        if (challenge && challenge.status === "running" && challenge.url) {
          clearInterval(readyTimer);
          renderUI(challenge);
          allInstanceStatuses[currentChallengeId] = challenge;
          updateBoardIndicators();
          startPolling();
        } else if (waited >= 120000) {
          clearInterval(readyTimer);
          showInlineWarning("Pod is taking too long to restart. Try refreshing.");
          startPolling();
        }
      }, 4000);
    } catch (err) {
      console.error("[ctf-controller] restart failed:", err);
      renderUnknown();
    }
  }

  // ─── Multi-flag progress ─────────────────────────────────────────────────────

  async function fetchMultiFlagProgress(numericId) {
    try {
      var resp = await ctfdFetch("/api/plugin/multi-flag-progress/" + numericId);
      if (!resp.ok) return null;
      return await resp.json();
    } catch (e) {
      return null;
    }
  }

  function renderMultiFlagProgress(progressData) {
    if (!progressData) return;
    var container = getOrCreateContainer();
    if (!container) return;

    var old = container.querySelector(".ctf-mf-progress");
    if (old) old.remove();

    var total = progressData.total || 0;
    var found = progressData.found || 0;
    var pts   = progressData.points_earned || 0;
    var complete = progressData.complete || false;
    var pct = total > 0 ? Math.round(found / total * 100) : 0;

    var bar = document.createElement("div");
    bar.className = "ctf-mf-progress";

    var labelRow = document.createElement("div");
    labelRow.className = "ctf-mf-label-row";

    var badge = document.createElement("span");
    badge.className = "ctf-badge " + (complete ? "ctf-badge--running" : "ctf-badge--muted");
    badge.textContent = complete ? "\u2713 Complete" : (found + "/" + total + " flags");
    labelRow.appendChild(badge);

    var ptsSpan = document.createElement("span");
    ptsSpan.className = "ctf-mf-pts";
    ptsSpan.textContent = pts + " pts earned";
    labelRow.appendChild(ptsSpan);

    bar.appendChild(labelRow);

    var track = document.createElement("div");
    track.className = "ctf-mf-bar-track";
    var fill = document.createElement("div");
    fill.className = "ctf-mf-bar-fill" + (complete ? " ctf-mf-bar-complete" : "");
    fill.style.width = pct + "%";
    track.appendChild(fill);
    bar.appendChild(track);

    // Insert at the top of the container (before the service Start/Stop row)
    container.insertBefore(bar, container.firstChild);
  }

  function startProgressPolling(numericId) {
    if (progressPollTimer) clearInterval(progressPollTimer);
    progressPollTimer = setInterval(async function () {
      var progress = await fetchMultiFlagProgress(numericId);
      renderMultiFlagProgress(progress);
    }, 10000);
  }

  function stopProgressPolling() {
    if (progressPollTimer) clearInterval(progressPollTimer);
    progressPollTimer = null;
  }

  // ─── Status polling ─────────────────────────────────────────────────────────

  async function updateUI() {
    var challenge = await fetchStatus();
    renderUI(challenge);
  }

  function startPolling() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(updateUI, 15000);
  }

  function stopPolling() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = null;
  }

  // ─── Challenge modal hooking (CTFd 3.8.2 — Bootstrap 5 modal events) ───────

  var openingId = null;

  async function onChallengeOpen(numericId) {
    numericId = String(numericId);
    if (openingId === numericId) return;
    openingId = numericId;

    stopPolling();
    stopProgressPolling();
    currentChallengeId = null;
    currentNumericId = null;
    currentStatus = null;
    isMultiFlag = false;

    var old = document.getElementById("ctf-controller-ui");
    if (old) old.remove();

    var info = await fetchChallengeInfo(numericId);
    if (openingId !== numericId) return;
    openingId = null;
    if (!info) {
      console.warn("[ctf-controller] fetchChallengeInfo returned null for", numericId);
      return;
    }

    var category = (info.category || "").toLowerCase();
    if (!SERVICE_CATEGORIES.includes(category)) {
      console.log("[ctf-controller] skipping non-service category:", category);
      return;
    }

    currentNumericId = numericId;
    isMultiFlag = info.type === "multi_flag";

    // Derive manifest ID: 1) challenge-map (player-accessible), 2) name slug fallback.
    currentChallengeId = (challengeManifestMap[String(numericId)] || "").replace(/-\d+$/, "") || null;

    if (!currentChallengeId && info.name) {
      // Normalize unicode (ï→i, é→e, etc.) before slugifying so accented
      // challenge names like "Aït Baha Nuclear" resolve to the correct id.
      currentChallengeId = info.name
        .normalize("NFD")
        .replace(/[\u0300-\u036f]/g, "")
        .toLowerCase()
        .replace(/\s+/g, "-")
        .replace(/[^a-z0-9-]/g, "");
    }
    if (!currentChallengeId) return;

    console.log("[ctf-controller] challenge opened:", currentChallengeId, "type:", info.type);

    // Multi-flag challenges: show progress bar + service UI (if applicable)
    if (isMultiFlag) {
      var progress = await fetchMultiFlagProgress(numericId);
      // Create container so progress bar has somewhere to live
      getOrCreateContainer();
      renderMultiFlagProgress(progress);
      startProgressPolling(numericId);
    }

    // Fetch controller status to determine if challenge has a running service.
    // For static (file-only) challenges, the controller returns null → renderUI(null)
    // shows a download button (single-flag) or nothing (multi_flag, which already
    // shows a progress bar above).
    updateUI();
    startPolling();
  }

  function onModalClose() {
    stopPolling();
    stopProgressPolling();
    openingId = null;
    currentChallengeId = null;
    currentNumericId = null;
    isMultiFlag = false;
    var ui = document.getElementById("ctf-controller-ui");
    if (ui) ui.remove();
  }

  function watchChallengeModal() {
    // Use Bootstrap 5 native modal events on #challenge-window.
    // "shown.bs.modal" fires after the modal is fully visible and x-html content is rendered.
    // "hidden.bs.modal" fires when the modal closes.
    // These events bubble, so we listen on document for safety.

    document.addEventListener("shown.bs.modal", function (e) {
      var modal = e.target;
      if (modal.id !== "challenge-window") return;

      console.log("[ctf-controller] modal shown event fired");

      // Find challenge ID from the hidden input rendered by x-html
      var cidEl = modal.querySelector("#challenge-id");
      var cid = cidEl ? cidEl.value : null;

      if (!cid) {
        // Fallback: parse from URL hash (#ChallengeName-ID)
        var hash = decodeURIComponent(window.location.hash.slice(1));
        var m = hash.match(/-(\d+)$/);
        cid = m ? m[1] : null;
      }

      if (cid) {
        console.log("[ctf-controller] detected challenge id:", cid);
        onChallengeOpen(cid);
      }
    });

    document.addEventListener("hidden.bs.modal", function (e) {
      if (e.target.id === "challenge-window") {
        onModalClose();
      }
    });

    // Handle direct deep-link: page loaded with hash already set.
    // ChallengeBoard.init() in challenges.js auto-opens the modal if hash exists,
    // which will fire shown.bs.modal — no extra handling needed.
  }

  // ─── Board-level instance indicators ────────────────────────────────────────

  function slugify(name) {
    return name.toLowerCase().replace(/\s+/g, "-").replace(/[^a-z0-9-]/g, "");
  }

  function findChallengeButtons() {
    // CTFd renders challenge cards as .challenge-button elements with the challenge name as text
    return document.querySelectorAll(".challenge-button");
  }

  function updateBoardIndicators() {
    var buttons = findChallengeButtons();
    buttons.forEach(function (btn) {
      var name = (btn.textContent || "").trim();
      // Challenge buttons may contain points text — extract just the name
      // CTFd format: "Challenge Name\n100" — take first line
      var firstLine = name.split("\n")[0].trim();
      var slug = slugify(firstLine);

      // Remove all instance status classes
      btn.classList.remove("ctf-instance-active", "ctf-instance-stopped");

      var instance = allInstanceStatuses[slug];
      if (instance && instance.status === "running") {
        btn.classList.add("ctf-instance-active");
      } else if (instance && instance.status === "suspended") {
        btn.classList.add("ctf-instance-stopped");
      }
    });
  }

  async function fetchAllStatuses() {
    try {
      var resp = await ctfdFetch("/controller-proxy/status/" + TEAM_ID);
      if (!resp.ok) return;
      var data = await resp.json();
      allInstanceStatuses = {};
      (data.challenges || []).forEach(function (ch) {
        allInstanceStatuses[ch.challenge_id] = ch;
      });
      updateBoardIndicators();
    } catch (err) {
      console.error("[ctf-controller] board status fetch failed:", err);
    }
  }

  function startBoardPolling() {
    // CTFd renders challenge buttons dynamically via Alpine.js / AJAX —
    // they don't exist at DOMContentLoaded. Use MutationObserver to detect
    // when .challenge-button elements appear, then start polling.
    var alreadyStarted = false;

    function beginPolling() {
      if (alreadyStarted) return;
      alreadyStarted = true;
      console.log("[ctf-controller] challenge buttons detected, starting board polling");
      fetchAllStatuses();
      if (boardPollTimer) clearInterval(boardPollTimer);
      boardPollTimer = setInterval(fetchAllStatuses, 20000);
    }

    // Check immediately in case buttons are already rendered
    if (document.querySelector(".challenge-button")) {
      beginPolling();
      return;
    }

    // Watch for buttons to appear
    var observer = new MutationObserver(function () {
      if (document.querySelector(".challenge-button")) {
        observer.disconnect();
        beginPolling();
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });

    // Safety timeout — stop watching after 30s if never found (not on challenges page)
    setTimeout(function () { observer.disconnect(); }, 30000);
  }

  // ─── Bootstrap ──────────────────────────────────────────────────────────────

  function init() {
    console.log("[ctf-controller] Plugin loaded, team_id=" + TEAM_ID + ", admin=" + IS_ADMIN);
    watchChallengeModal();
    startBoardPolling();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
