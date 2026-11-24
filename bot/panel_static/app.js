"use strict";
// Homelab Admin: one page, no libraries. Talks to the bot's JSON API (bot/web.py) with the login cookie.
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const GB = 1024 ** 3;
  const MOVING = ["downloading", "importing", "queued"];

  // --- icons (simple line icons, 24×24) ------------------------------------------------------
  const ICONS = {
    home: '<path d="M3 11l9-8 9 8"/><path d="M5 10v10h14V10"/>',
    inbox: '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/>',
    download: '<path d="M12 3v12"/><path d="M7 10l5 5 5-5"/><path d="M5 21h14"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>',
    calendar: '<rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/>',
    follow: '<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
    cube: '<path d="M21 16V8a2 2 0 0 0-1-1.7l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.7l7 4a2 2 0 0 0 2 0l7-4a2 2 0 0 0 1-1.7z"/><path d="M3.3 7L12 12l8.7-5M12 22V12"/>',
    menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    x: '<path d="M18 6L6 18M6 6l12 12"/>',
    check: '<path d="M20 6L9 17l-5-5"/>',
    alert: '<path d="M10.3 3.9L1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z"/><path d="M12 9v4M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
    refresh: '<path d="M21 12a9 9 0 1 1-3-6.7L21 8"/><path d="M21 3v5h-5"/>',
    play: '<path d="M6 4l14 8-14 8z"/>',
    pause: '<path d="M7 4h3v16H7zM14 4h3v16h-3z"/>',
    stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    trash: '<path d="M3 6h18M8 6V4h8v2M19 6l-1 14H6L5 6"/>',
    save: '<path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2z"/><path d="M17 21v-8H7v8M7 3v5h8"/>',
    terminal: '<path d="M4 17l6-6-6-6M12 19h8"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    film: '<rect x="2" y="3" width="20" height="18" rx="2"/><path d="M7 3v18M17 3v18M2 8h5M2 16h5M17 8h5M17 16h5"/>',
    tv: '<rect x="2" y="7" width="20" height="14" rx="2"/><path d="M17 2l-5 5-5-5"/>',
    disk: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5v14c0 1.7 4 3 9 3s9-1.3 9-3V5"/><path d="M3 12c0 1.7 4 3 9 3s9-1.3 9-3"/>',
    users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    external: '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6M15 3h6v6M10 14L21 3"/>',
    key: '<circle cx="7.5" cy="15.5" r="5.5"/><path d="M21 2l-9.6 9.6M15.5 7.5l3 3L22 7l-3-3"/>',
  };

  function icon(name, cls = "i") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", cls);
    svg.setAttribute("aria-hidden", "true");
    svg.innerHTML = ICONS[name] || ""; // fixed strings from above, never data
    return svg;
  }

  // --- DOM helpers ------------------------------------------------------------------------------
  const PROPS = new Set(["value", "checked", "disabled", "selected", "open", "hidden", "indeterminate"]);

  function h(tag, props, ...children) {
    const el = document.createElement(tag);
    for (const [key, value] of Object.entries(props || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key === "style") Object.assign(el.style, value);
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else if (PROPS.has(key)) el[key] = value;
      else el.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false || child === "") continue;
      el.append(child instanceof Node ? child : String(child));
    }
    return el;
  }

  // Bot messages use a little Discord markup: **bold**, `code`, "-# " small lines and \-escaped characters.
  function rich(text, cls = "rich") {
    const box = h("div", { class: cls });
    const lines = String(text ?? "").split("\n");
    lines.forEach((raw, index) => {
      let line = raw;
      let target = box;
      if (line.startsWith("-# ")) {
        target = h("span", { class: "sub" });
        line = line.slice(3);
      }
      const escaped = [];
      const safe = line.replace(/\\([^\w\s])/g, (_, c) => {
        escaped.push(c);
        return `${escaped.length - 1}`;
      });
      const restore = (s) => s.replace(/(\d+)/g, (_, n) => escaped[Number(n)]);
      const pattern = /\*\*(.+?)\*\*|`([^`]+)`/g;
      let last = 0;
      let match;
      while ((match = pattern.exec(safe))) {
        if (match.index > last) target.append(restore(safe.slice(last, match.index)));
        target.append(match[1] !== undefined ? h("strong", null, restore(match[1])) : h("code", null, restore(match[2])));
        last = pattern.lastIndex;
      }
      if (last < safe.length) target.append(restore(safe.slice(last)));
      if (target !== box) box.append(target);
      if (index < lines.length - 1) box.append("\n");
    });
    return box;
  }

  // --- formatting -------------------------------------------------------------------------------
  function size(bytes) {
    if (bytes === null || bytes === undefined) return "?";
    if (bytes <= 0) return "0 GB";
    const gb = bytes / GB;
    if (gb >= 10) return `${Math.round(gb)} GB`;
    if (gb >= 1) return `${gb.toFixed(1)} GB`;
    const mb = bytes / 1024 ** 2;
    return `${mb >= 10 ? Math.round(mb) : mb.toFixed(1)} MB`;
  }
  function speed(bytes) {
    if (!bytes) return "";
    return bytes >= 1024 ** 2 ? `${(bytes / 1024 ** 2).toFixed(1)} MB/s` : `${Math.round(bytes / 1024)} KB/s`;
  }
  function etaText(seconds) {
    if (!seconds) return "";
    const minutes = seconds / 60;
    if (minutes < 1) return "gleich fertig";
    if (minutes < 60) return `noch ${Math.round(minutes)} Min.`;
    const hours = minutes / 60;
    if (hours < 24) return `noch ${Math.max(1, Math.round(hours))} Std.`;
    const days = Math.round(hours / 24);
    return `noch ${days} ${days === 1 ? "Tag" : "Tage"}`;
  }
  function dateTime(value) {
    const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return date.toLocaleString("de-CH", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function ago(seconds) {
    if (!seconds) return "";
    const minutes = (Date.now() / 1000 - seconds) / 60;
    if (minutes < 60) return `vor ${Math.max(1, Math.round(minutes))} Min.`;
    if (minutes < 60 * 24) return `vor ${Math.round(minutes / 60)} Std.`;
    const days = Math.round(minutes / 60 / 24);
    return `vor ${days} ${days === 1 ? "Tag" : "Tagen"}`;
  }
  const count = (n, one, many) => `${n} ${n === 1 ? one : many}`;
  const kindPill = (kind) => h("span", { class: "pill" }, kind === "movie" ? "Film" : "Serie");

  function bar(percent, color = "", thin = false) {
    const fill = h("span");
    fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    return h("div", { class: `bar ${color} ${thin ? "thin" : ""}`, role: "progressbar", "aria-valuenow": Math.round(percent), "aria-valuemin": 0, "aria-valuemax": 100 }, fill);
  }
  function poster(url, large = false) {
    const cls = `poster${large ? " large" : ""}`;
    return url ? h("img", { class: cls, src: url, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : h("div", { class: cls }, icon("film"));
  }
  function banner(kind, text, iconName = "alert") {
    return h("div", { class: `banner ${kind}`, role: kind === "bad" ? "alert" : null }, icon(iconName), rich(text));
  }
  function empty(iconName, title, text, action) {
    return h("div", { class: "empty" }, icon(iconName), h("strong", null, title), text ? h("span", null, text) : null, action || null);
  }
  function loadingRow(text = "Lädt …") {
    return h("div", { class: "loading" }, h("div", { class: "spinner" }), text);
  }
  function section(title, ...content) {
    return h("section", { class: "section" }, h("div", { class: "section-head" }, h("h2", null, title)), ...content);
  }

  // --- API ------------------------------------------------------------------------------------------
  async function api(path, options = {}) {
    const method = options.method || (options.body !== undefined ? "POST" : "GET");
    const init = { method, headers: {}, credentials: "same-origin" };
    if (method !== "GET") {
      init.headers["X-Panel"] = "1";
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body ?? {});
    }
    let response;
    try {
      response = await fetch(path, init);
    } catch {
      throw new Error("Keine Verbindung zum Bot. Läuft er noch?");
    }
    let data = null;
    try {
      data = await response.json();
    } catch {
      data = null;
    }
    if (response.status === 401 && path !== "/api/login") {
      showLogin();
      throw new Error("Bitte zuerst anmelden.");
    }
    if (!response.ok) throw new Error((data && data.error) || `Fehler ${response.status}`);
    return data;
  }

  function toast(text, kind = "ok") {
    if (!text) return;
    const box = h("div", { class: `toast ${kind}`, role: kind === "bad" ? "alert" : "status" }, icon(kind === "bad" ? "alert" : "check"), rich(text));
    box.addEventListener("click", () => box.remove());
    $("#toasts").append(box);
    setTimeout(() => box.remove(), Math.min(16000, 4500 + String(text).length * 35));
  }

  // Runs a button's action: busy while it runs, the answer as a toast, errors too.
  async function run(button, action, after) {
    if (button) {
      button.classList.add("busy");
      button.disabled = true;
    }
    try {
      const result = await action();
      if (result && result.message) toast(result.message);
      if (after) await after(result);
      return result;
    } catch (error) {
      toast(error.message, "bad");
      return null;
    } finally {
      if (button && button.isConnected) {
        button.classList.remove("busy");
        button.disabled = false;
      }
    }
  }

  // --- modal ---------------------------------------------------------------------------------------
  const modal = $("#modal");
  const modalBody = $("#modal-body");
  let onModalClose = null;

  function openModal({ title, body, foot, wide = false, onClose = null }) {
    const previous = onModalClose;
    onModalClose = null;
    if (previous) previous();
    const parts = [
      h("div", { class: "modal-head" }, typeof title === "string" ? h("h2", null, title) : title,
        h("button", { class: "icon-btn", type: "button", "aria-label": "Schliessen", onclick: closeModal }, icon("x"))),
      h("div", { class: "modal-main" }, body),
    ];
    if (foot) parts.push(h("div", { class: "modal-foot" }, foot));
    modalBody.replaceChildren(...parts);
    modal.classList.toggle("wide", wide);
    onModalClose = onClose;
    if (!modal.open) modal.showModal();
  }
  function closeModal() {
    if (modal.open) modal.close();
  }
  modal.addEventListener("close", () => {
    const callback = onModalClose;
    onModalClose = null;
    modalBody.replaceChildren();
    if (callback) callback();
  });
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal();
  });

  function confirmBox(title, text, okLabel = "Ja", danger = true) {
    return new Promise((resolve) => {
      let answered = false;
      const ok = h("button", { class: `btn ${danger ? "danger solid" : "primary"}`, type: "button", onclick: () => { answered = true; closeModal(); resolve(true); } }, okLabel);
      openModal({
        title,
        body: rich(text),
        foot: [h("div", { class: "grow" }), h("button", { class: "btn ghost", type: "button", onclick: closeModal }, "Abbrechen"), ok],
        onClose: () => { if (!answered) resolve(false); },
      });
      ok.focus();
    });
  }

  // --- pages and navigation --------------------------------------------------------------------
  const page = $("#page");
  let session = null;
  let timer = null;
  let renderToken = 0;

  const PAGES = [
    { id: "uebersicht", label: "Übersicht", icon: "home", render: renderOverview },
    { id: "erledigen", label: "Zu erledigen", icon: "inbox", render: renderInbox, badge: true },
    { id: "downloads", label: "Downloads", icon: "download", render: renderDownloads, need: "qbit" },
    { id: "suchen", label: "Suchen & holen", icon: "search", render: renderSearch },
    { id: "kalender", label: "Kalender", icon: "calendar", render: renderCalendar, need: "tv" },
    { id: "verfolgt", label: "Verfolgt", icon: "follow", render: renderFollows, need: "tv" },
    { id: "minecraft", label: "Minecraft", icon: "cube", render: renderMinecraft, need: "mc" },
  ];
  const visiblePages = () => PAGES.filter((p) => !p.need || (session && session[p.need]));

  function buildNav() {
    const links = visiblePages().map((p) => h("a", { class: "nav-link", href: `#/${p.id}`, "data-page": p.id }, icon(p.icon), h("span", null, p.label),
      p.badge ? h("span", { class: "badge", id: "inbox-badge", hidden: true }) : null));
    $("#nav-links").replaceChildren(...links);
    $("#user-name").textContent = session.user;
  }

  function setBadge(counts) {
    const badge = $("#inbox-badge");
    if (!badge || !counts) return;
    const total = Object.values(counts).reduce((a, b) => a + b, 0);
    badge.textContent = total;
    badge.hidden = total === 0;
  }

  function stopTimer() {
    if (timer) clearInterval(timer);
    timer = null;
  }
  function every(ms, fn) {
    stopTimer();
    timer = setInterval(() => {
      if (!document.hidden && !modal.open) fn();
    }, ms);
  }

  function route() {
    if (!session) return;
    const wanted = location.hash.replace(/^#\/?/, "") || "uebersicht";
    const target = visiblePages().find((p) => p.id === wanted) || visiblePages()[0];
    for (const link of document.querySelectorAll(".nav-link")) link.classList.toggle("active", link.dataset.page === target.id);
    closeMenu();
    stopTimer();
    const token = ++renderToken;
    document.title = `${target.label} · Homelab Admin`;
    page.replaceChildren();
    page.focus({ preventScroll: true });
    window.scrollTo(0, 0);
    target.render(token);
  }
  window.addEventListener("hashchange", route);

  function head(title, subtitle, ...actions) {
    return h("div", { class: "page-head" }, h("div", null, h("h1", null, title), subtitle ? h("p", { class: "muted" }, subtitle) : null),
      actions.length ? h("div", { class: "row" }, actions) : null);
  }
  function refreshButton(fn) {
    return h("button", { class: "btn ghost small", type: "button", onclick: (e) => run(e.currentTarget, fn) }, icon("refresh"), "Aktualisieren");
  }
  // Loads a page part; the first time errors show in place, on refreshes quietly as a toast.
  async function load(token, area, fn, quiet = false) {
    try {
      await fn();
    } catch (error) {
      if (token !== renderToken) return;
      if (quiet) return;
      area.replaceChildren(banner("bad", error.message), h("button", { class: "btn", type: "button", onclick: () => { area.replaceChildren(loadingRow()); load(token, area, fn); } }, icon("refresh"), "Nochmals versuchen"));
    }
  }

  // --- Übersicht -------------------------------------------------------------------------------
  const STATE_LABEL = {
    downloading: "lädt", importing: "wird übernommen", queued: "wartet", paused: "pausiert",
    stalled: "hängt, keine Quellen", blocked: "Übernahme blockiert", failed: "fehlgeschlagen", warning: "Problem",
  };

  function mediaCard(c) {
    const p = c.progress;
    const body = h("div", { class: "item-body" },
      h("div", { class: "item-title" }, h("h3", null, c.name), kindPill(c.kind),
        c.pending ? h("span", { class: "pill amber" }, "wartet auf Freigabe") : null,
        !c.request ? h("span", { class: "pill violet" }, "ohne Anfrage") : null));
    if (p) {
      const moving = p.downloads.some((d) => MOVING.includes(d.state));
      const color = p.percent >= 100 ? "green" : moving ? "" : "amber";
      body.append(h("div", { class: "progress-row" }, bar(p.percent, color), h("strong", null, `${p.percent} %`)));
      const parts = [];
      if (p.size) parts.push(`${size(p.loaded)} von ${size(p.size)}`);
      if (p.speed) parts.push(speed(p.speed));
      if (p.eta_text) parts.push(p.eta_text);
      if (c.kind === "tv" && p.total) parts.push(`${p.have} von ${p.total} Folgen da`);
      if (p.upcoming) parts.push(`${p.upcoming} kommen noch`);
      if (parts.length) body.append(h("div", { class: "item-meta" }, parts.map((x) => h("span", null, x))));
      const stuck = p.downloads.filter((d) => !MOVING.includes(d.state));
      if (stuck.length) body.append(h("div", { class: "item-meta" }, h("span", { class: "pill amber" }, `${count(stuck.length, "Download", "Downloads")} pausiert oder hängen`)));
      if (p.missing.length) {
        body.append(h("div", { class: "small muted" }, `Fehlt noch: ${p.missing.map((m) => `Staffel ${m.season}, Folge ${m.text}`).join("; ")} · ${p.searching ? "Sonarr sucht gerade" : "wartet auf ein passendes Release"}`));
      } else if (c.kind === "movie" && !p.have && !p.downloads.length) {
        body.append(h("div", { class: "small muted" }, p.searching ? "Radarr sucht gerade" : "Radarr wartet auf ein passendes Release"));
      }
      if (p.downloads.length) {
        body.append(h("details", { class: "more" }, h("summary", null, `${count(p.downloads.length, "Download", "Downloads")} anzeigen`),
          h("div", { class: "dl-list" }, p.downloads.map((d) => h("div", { class: "dl", title: d.message || null },
            h("span", { class: "mono" }, d.what || "Film"),
            bar(d.percent, d.state === "downloading" ? "" : d.state === "importing" ? "green" : "amber", true),
            h("span", { class: "small muted nowrap" }, d.state === "downloading" ? [`${d.percent} %`, d.speed ? ` · ${speed(d.speed)}` : "", d.eta_text ? ` · ${d.eta_text}` : ""].join("") : STATE_LABEL[d.state] || d.state))))));
      }
    } else if (c.state_text) {
      body.append(rich(c.state_text, "rich muted small"));
    }
    if (c.who) body.append(h("div", { class: "item-meta" }, `Angefragt von ${c.who}`));
    return h("div", { class: "item" }, poster(c.poster), body);
  }

  function calendarList(days) {
    const STATE = { have: ["green", "da"], loading: ["blue", "lädt"], searching: ["amber", "wird gesucht"], waiting: ["", "wird geholt"] };
    return h("div", { class: "days" }, days.map((day) => h("div", { class: "day" },
      h("h3", { class: day.label.startsWith("Heute") ? "today" : null }, day.label),
      h("div", { class: "day-items" }, day.items.map((item) => {
        const [color, label] = STATE[item.state] || ["", item.state];
        return h("div", { class: "cal" }, h("span", { class: "time" }, item.time || "–"), icon(item.kind === "movie" ? "film" : "tv"),
          h("div", { class: "cal-title" }, h("strong", null, item.title), " ", h("span", null, `· ${item.what}${item.episode_title ? ` · ${item.episode_title}` : ""}`)),
          h("span", { class: `pill ${color}` }, label));
      })))));
  }

  async function renderOverview(token) {
    const content = h("div", null, h("div", { class: "tiles" }, [0, 1, 2, 3].map(() => h("div", { class: "skeleton" }))));
    const refresh = async (quiet = false) => {
      const [data, mc, calendar] = await Promise.all([
        api("/api/overview"),
        session.mc ? api("/api/mc/status").catch((e) => ({ error: e.message })) : Promise.resolve(null),
        session.tv ? api("/api/calendar?days=3").catch(() => null) : Promise.resolve(null),
      ]);
      if (token !== renderToken) return;
      setBadge(data.inbox);
      const brake = data.brake;
      const freeClass = data.free === null ? "" : brake.enabled && data.free < brake.limit ? "bad" : data.free < data.min_free ? "warn" : "good";
      const inbox = data.inbox;
      const todo = inbox.approvals + inbox.watched + inbox.follows + inbox.friends;
      const todoParts = [inbox.approvals && count(inbox.approvals, "Freigabe", "Freigaben"), inbox.watched && `${inbox.watched}× geschaut`,
        inbox.follows && count(inbox.follows, "Verfolgen-Wunsch", "Verfolgen-Wünsche"), inbox.friends && count(inbox.friends, "Zugang", "Zugänge")].filter(Boolean);
      const tiles = [
        h("div", { class: `tile ${freeClass}` }, h("span", { class: "tile-label" }, icon("disk"), "Speicher"),
          h("span", { class: "tile-value" }, data.free === null ? "?" : `${size(data.free)} frei`),
          h("span", { class: "tile-sub" }, `Neue Anfragen ab ${size(data.min_free)} frei`)),
        h(session.qbit ? "a" : "div", { class: "tile", href: session.qbit ? "#/downloads" : null }, h("span", { class: "tile-label" }, icon("download"), "Downloads"),
          h("span", { class: "tile-value" }, data.active ? `${data.active} aktiv` : "Ruhe"),
          h("span", { class: "tile-sub" }, data.speed ? speed(data.speed) : "gerade lädt nichts")),
        h("a", { class: `tile ${todo ? "warn" : ""}`, href: "#/erledigen" }, h("span", { class: "tile-label" }, icon("inbox"), "Zu erledigen"),
          h("span", { class: "tile-value" }, todo ? String(todo) : "Nichts"),
          h("span", { class: "tile-sub" }, todoParts.join(" · ") || "alles erledigt")),
      ];
      if (session.mc) {
        const running = mc && !mc.error && mc.running;
        tiles.push(h("a", { class: `tile ${running ? "good" : ""}`, href: "#/minecraft" }, h("span", { class: "tile-label" }, icon("cube"), "Minecraft"),
          h("span", { class: "tile-value" }, mc && mc.error ? "?" : running ? mc.running : "Aus"),
          h("span", { class: "tile-sub" }, mc && mc.error ? "Bot nicht erreichbar" : running ? (mc.ready ? (mc.players ? `${mc.players.online}/${mc.players.max} online` : "bereit") : "startet …") : "kein Server an")));
      }
      const parts = [h("div", { class: "tiles" }, tiles)];
      if (brake.active) parts.push(h("div", { class: "section" }, banner("bad", `**Notbremse aktiv:** Die Downloads sind angehalten, bis wieder ${size(brake.resume_at)} frei sind. Platz schaffen geht unter Suchen & holen (nur Bibliothek).`)));
      for (const problem of data.problems) parts.push(banner("warn", problem));
      parts.push(section(`Läuft gerade${data.cards.length ? ` (${data.cards.length})` : ""}`,
        data.cards.length ? h("div", { class: "items" }, data.cards.map(mediaCard))
          : empty("check", "Gerade lädt nichts", "Alles Angefragte ist schon da.", h("a", { class: "btn", href: "#/suchen" }, icon("search"), "Etwas suchen"))));
      if (calendar && calendar.days.length) parts.push(section("Demnächst", calendarList(calendar.days)));
      content.replaceChildren(...parts);
    };
    page.append(head("Übersicht", `Hallo ${session.user}. Alles aus Discord an einem Ort.`, refreshButton(() => refresh())), content);
    await load(token, content, refresh);
    every(15000, () => load(token, content, () => refresh(true), true));
  }

  // --- Zu erledigen ------------------------------------------------------------------------------
  async function renderInbox(token) {
    const content = h("div", null, loadingRow());
    const refresh = async () => {
      const data = await api("/api/inbox");
      if (token !== renderToken) return;
      setBadge({ a: data.approvals.length, b: data.watched.length, c: data.follows.length, d: data.friends.length });
      const parts = [];
      const act = (path, id, action, button, ask) => async () => {
        if (ask && !(await confirmBox(ask[0], ask[1], ask[2]))) return;
        await run(button, () => api(path, { body: { id, action } }), refresh);
      };
      if (data.approvals.length) {
        parts.push(section("Anfragen freigeben", h("div", { class: "items" }, data.approvals.map((a) => {
          const yes = h("button", { class: "btn primary small", type: "button" }, icon("check"), "Freigeben");
          const no = h("button", { class: "btn small", type: "button" }, "Ablehnen");
          yes.onclick = act("/api/inbox/approval", a.id, "approve", yes);
          no.onclick = act("/api/inbox/approval", a.id, "decline", no);
          return h("div", { class: "item" }, poster(a.poster), h("div", { class: "item-body" },
            h("div", { class: "item-title" }, h("h3", null, a.name), kindPill(a.kind)),
            h("div", { class: "item-meta" }, a.seasons.length ? h("span", null, `Staffel ${a.seasons.join(", ")}`) : null, a.who ? h("span", null, `von ${a.who}`) : null),
            h("div", { class: "item-actions" }, yes, no)));
        }))));
      }
      if (data.watched.length) {
        parts.push(section("Geschaut. Löschen?", h("div", { class: "items" }, data.watched.map((q) => {
          const del = h("button", { class: "btn danger small", type: "button" }, icon("trash"), "Löschen");
          const keep = h("button", { class: "btn small", type: "button" }, "Behalten");
          const what = q.kind === "movie" ? `den Film ${q.label}` : `${count(q.episodes.length, "Folge", "Folgen")} von ${q.label}`;
          del.onclick = act("/api/inbox/watched", q.id, "delete", del, ["Löschen?", `**${what}** löschen? Die Dateien und Torrents werden entfernt.`, "Löschen"]);
          keep.onclick = act("/api/inbox/watched", q.id, "keep", keep);
          return h("div", { class: "item simple" }, h("div", { class: "item-body" },
            h("div", { class: "item-title" }, h("h3", null, q.label), kindPill(q.kind)),
            q.episodes.length ? h("div", { class: "chips" }, q.episodes.map((e) => h("span", { class: "pill" }, e))) : null,
            h("div", { class: "item-meta" }, q.viewers.length ? h("span", null, `geschaut von ${q.viewers.join(", ")}`) : null,
              q.size ? h("span", null, `≈ ${size(q.size)}`) : null, q.created ? h("span", null, ago(q.created)) : null),
            h("div", { class: "item-actions" }, del, keep)));
        }))));
      }
      if (data.follows.length) {
        parts.push(section("Wünsche: Serie verfolgen", h("div", { class: "items" }, data.follows.map((f) => {
          const yes = h("button", { class: "btn primary small", type: "button" }, icon("check"), "Freigeben");
          const no = h("button", { class: "btn small", type: "button" }, "Ablehnen");
          yes.onclick = act("/api/inbox/follow", f.id, "ok", yes);
          no.onclick = act("/api/inbox/follow", f.id, "no", no);
          return h("div", { class: "item simple" }, h("div", { class: "item-body" },
            h("div", { class: "item-title" }, h("h3", null, f.label)),
            h("div", { class: "item-meta" }, h("span", null, `${f.user} möchte sie verfolgen`), h("span", null, f.start ? `ab ${f.start}` : "ab jetzt"),
              f.new_seasons ? h("span", null, "mit neuen Staffeln") : null, f.created ? h("span", null, ago(f.created)) : null),
            h("div", { class: "item-actions" }, yes, no)));
        }))));
      }
      if (data.friends.length) {
        parts.push(section("Jellyfin-Zugänge", h("div", { class: "items" }, data.friends.map((f) => {
          const yes = h("button", { class: "btn primary small", type: "button" }, icon("check"), "Freigeben");
          const no = h("button", { class: "btn small", type: "button" }, "Ablehnen");
          yes.onclick = act("/api/inbox/friend", f.id, "approve", yes);
          no.onclick = act("/api/inbox/friend", f.id, "decline", no);
          return h("div", { class: "item simple" }, h("div", { class: "item-body" },
            h("div", { class: "item-title" }, h("h3", null, f.user), h("span", { class: "pill" }, f.existing ? "wieder freischalten" : "neuer Zugang")),
            h("div", { class: "item-meta" }, h("span", null, `Benutzername in Jellyfin: ${f.jellyfin_name}`), f.created ? h("span", null, ago(f.created)) : null),
            h("div", { class: "item-actions" }, yes, no)));
        }))));
      }
      content.replaceChildren(...(parts.length ? parts : [empty("check", "Nichts zu erledigen", "Neue Anfragen, „Geschaut“-Fragen und Wünsche erscheinen hier und weiterhin in Discord.")]));
    };
    page.append(head("Zu erledigen", "Alles, was auf deine Entscheidung wartet. Discord wird mitgeführt.", refreshButton(refresh)), content);
    await load(token, content, refresh);
    every(30000, () => load(token, content, refresh, true));
  }

  // --- Downloads --------------------------------------------------------------------------------
  let torrentFilter = "all";
  const TORRENT_FILTERS = [
    ["all", "Alle", () => true],
    ["active", "Laden", (t) => ["downloading", "stalled", "queued"].includes(t.state)],
    ["paused", "Pausiert", (t) => t.state === "paused"],
    ["done", "Fertig", (t) => ["seeding", "done"].includes(t.state)],
    ["problems", "Probleme", (t) => ["error", "other", "stalled"].includes(t.state)],
  ];
  const TORRENT_STATE = {
    downloading: ["blue", "lädt"], stalled: ["amber", "hängt"], queued: ["", "wartet"], paused: ["amber", "pausiert"],
    seeding: ["green", "fertig, teilt"], done: ["green", "fertig"], error: ["red", "Fehler"], other: ["", "?"],
  };

  async function renderDownloads(token) {
    const content = h("div", null, loadingRow());
    let torrents = [];
    const draw = (data) => {
      const filter = TORRENT_FILTERS.find((f) => f[0] === torrentFilter) || TORRENT_FILTERS[0];
      const shown = data.torrents.filter(filter[2]);
      const running = data.torrents.filter((t) => ["downloading", "stalled", "queued"].includes(t.state));
      const paused = data.torrents.filter((t) => t.state === "paused");
      const parts = [];
      if (data.brake.active) parts.push(banner("bad", `**Notbremse aktiv:** Laufende Downloads sind angehalten, bis wieder ${size(data.brake.resume_at)} frei sind (jetzt ${size(data.free)}).`));
      else if (data.brake.enabled) parts.push(banner("info", `Frei: **${size(data.free)}**. Unter ${size(data.brake.limit)} hält die Notbremse alle Downloads an.`, "info"));
      const pauseAll = h("button", { class: "btn small", type: "button", disabled: !running.length }, icon("pause"), "Alle laufenden anhalten");
      const resumeAll = h("button", { class: "btn small", type: "button", disabled: !paused.length }, icon("play"), "Alle pausierten fortsetzen");
      pauseAll.onclick = () => run(pauseAll, () => api("/api/downloads/pause", { body: { hashes: running.map((t) => t.hash) } }), refresh);
      resumeAll.onclick = () => run(resumeAll, () => api("/api/downloads/resume", { body: { hashes: paused.map((t) => t.hash) } }), refresh);
      parts.push(h("div", { class: "row between" },
        h("div", { class: "filters" }, TORRENT_FILTERS.map(([id, label, test]) => h("button", {
          class: `filter ${id === torrentFilter ? "active" : ""}`, type: "button", onclick: () => { torrentFilter = id; draw(data); },
        }, `${label} (${data.torrents.filter(test).length})`))),
        h("div", { class: "row" }, pauseAll, resumeAll)));
      parts.push(h("div", { class: "torrents section" }, shown.length ? shown.map((t) => {
        const [color, label] = TORRENT_STATE[t.state] || ["", t.raw_state];
        let action = null;
        if (["downloading", "stalled", "queued"].includes(t.state)) {
          action = h("button", { class: "btn small", type: "button", title: "Anhalten" }, icon("pause"));
          action.onclick = () => run(action, () => api("/api/downloads/pause", { body: { hashes: [t.hash] } }), refresh);
        } else if (t.state === "paused") {
          action = h("button", { class: "btn small", type: "button", title: "Fortsetzen" }, icon("play"));
          action.onclick = () => run(action, () => api("/api/downloads/resume", { body: { hashes: [t.hash] } }), refresh);
        }
        const info = [`${t.percent.toFixed(t.percent >= 100 ? 0 : 1)} %`];
        if (t.speed) info.push(speed(t.speed));
        if (t.eta && t.state === "downloading") info.push(etaText(t.eta));
        return h("div", { class: "torrent" },
          h("div", null, h("div", { class: "torrent-name" }, t.name),
            h("div", { class: "item-meta" }, h("span", { class: `pill ${color}` }, label), t.category ? h("span", null, t.category) : null,
              h("span", null, size(t.size)), t.state !== "done" && t.state !== "seeding" ? h("span", null, `${t.seeds} Quellen`) : null,
              t.added ? h("span", null, ago(t.added)) : null)),
          h("div", { class: "torrent-progress" }, bar(t.percent, t.percent >= 100 ? "green" : color === "amber" ? "amber" : "", true), h("span", null, info.join(" · "))),
          h("div", null, action));
      }) : empty("download", "Hier ist nichts", torrentFilter === "all" ? "qBittorrent hat gerade keine Torrents." : "Kein Torrent passt zu diesem Filter.")));
      content.replaceChildren(...parts);
    };
    const refresh = async () => {
      const data = await api("/api/downloads");
      if (token !== renderToken) return;
      torrents = data;
      draw(data);
    };
    page.append(head("Downloads", "Alle Torrents in qBittorrent, live.", refreshButton(refresh)), content);
    await load(token, content, refresh);
    every(5000, () => load(token, content, refresh, true));
    return torrents;
  }

  // --- Suchen & holen ---------------------------------------------------------------------------
  const searchState = { query: "", kind: "", library: false, results: null };
  const STATUS_PILL = { 2: ["amber", "wartet"], 3: ["blue", "wird geladen"], 4: ["amber", "teilweise da"], 5: ["green", "da"], 7: ["", "gelöscht"] };

  async function renderSearch(token) {
    const input = h("input", { type: "search", placeholder: "Film oder Serie, z.B. Dune oder Dexter", value: searchState.query, "aria-label": "Suchen", autocomplete: "off" });
    const results = h("div");
    const kinds = h("div", { class: "segmented", role: "group", "aria-label": "Art" });
    const library = h("input", { type: "checkbox", checked: searchState.library });
    let debounce = null;

    const drawKinds = () => kinds.replaceChildren(...[["", "Alle"], ["movie", "Filme"], ["tv", "Serien"]].map(([id, label]) => h("button", {
      type: "button", class: id === searchState.kind ? "active" : "", onclick: () => { searchState.kind = id; drawKinds(); go(); },
    }, label)));
    const draw = () => {
      const list = searchState.results;
      if (list === null) {
        results.replaceChildren(empty("search", "Suchen", searchState.library ? "Was in der Bibliothek ist, zum Löschen." : "Anfragen, einzelne Folgen holen, verfolgen oder löschen: alles aus dem Treffer heraus."));
        return;
      }
      if (!list.length) {
        results.replaceChildren(empty("search", "Nichts gefunden", "Anders schreiben oder den Originaltitel versuchen."));
        return;
      }
      results.replaceChildren(h("div", { class: "results" }, list.map((t) => {
        const status = STATUS_PILL[t.status];
        return h("button", { class: "result", type: "button", onclick: () => openTitle(t.kind, t.tmdb_id) },
          h("div", { class: "result-poster" }, icon(t.kind === "movie" ? "film" : "tv"), t.poster ? h("img", { src: t.poster, alt: "", loading: "lazy", referrerpolicy: "no-referrer" }) : null,
            status ? h("span", { class: `pill ${status[0]}` }, status[1]) : null),
          h("div", null, h("div", { class: "result-title" }, t.name), h("div", { class: "small muted" }, [t.kind === "movie" ? "Film" : "Serie", t.year].filter(Boolean).join(" · "))));
      })));
    };
    const go = async () => {
      const query = input.value.trim();
      searchState.query = query;
      searchState.library = library.checked;
      if (query.length < 2) {
        searchState.results = null;
        draw();
        return;
      }
      results.replaceChildren(loadingRow("Sucht …"));
      const params = new URLSearchParams({ q: query });
      if (searchState.kind) params.set("kind", searchState.kind);
      if (searchState.library) params.set("library", "1");
      try {
        const data = await api(`/api/search?${params}`);
        if (token !== renderToken || input.value.trim() !== query) return;
        searchState.results = data.results;
        draw();
      } catch (error) {
        results.replaceChildren(banner("bad", error.message));
      }
    };
    input.addEventListener("input", () => {
      clearTimeout(debounce);
      debounce = setTimeout(go, 450);
    });
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        clearTimeout(debounce);
        go();
      }
    });
    library.addEventListener("change", go);
    drawKinds();
    page.append(head("Suchen & holen", "Klick auf einen Treffer: anfragen, einzelne Folgen, verfolgen oder löschen."),
      h("div", { class: "searchbar" }, input, kinds, h("label", { class: "check" }, library, "Nur Bibliothek")), results);
    draw();
    input.focus();
  }

  function estimate(t, seasons) {
    let hours;
    let what;
    if (t.kind === "movie") {
      if (!t.runtime) return null;
      hours = t.runtime / 60;
      what = `${t.runtime} Min.`;
    } else {
      const episodes = t.seasons.filter((s) => seasons.includes(s.number)).reduce((sum, s) => sum + s.episodes, 0);
      if (!episodes) return null;
      hours = (episodes * t.episode_minutes) / 60;
      what = `${episodes} Folgen à ${t.episode_minutes} Min.`;
    }
    const typical = hours * t.rate.typical * GB;
    const low = hours * t.rate.low * GB;
    const high = hours * t.rate.high * GB;
    let text = `≈ **${size(typical)}** (${size(low)}–${size(high)}) für ${what}`;
    let warn = false;
    if (t.free !== null) {
      text += ` · frei ${size(t.free)}`;
      if (t.free - typical < t.min_free) {
        warn = true;
        text += `\nKönnte knapp werden: danach wären etwa ${size(Math.max(t.free - typical, 0))} frei, unter ${size(t.min_free)} sind Anfragen gesperrt.`;
      }
    }
    return { text, warn };
  }

  async function openTitle(kind, tmdbId) {
    openModal({ title: "Lädt …", body: loadingRow() });
    let t;
    try {
      t = await api(`/api/title?kind=${kind}&id=${tmdbId}`);
    } catch (error) {
      openModal({ title: "Fehler", body: banner("bad", error.message) });
      return;
    }
    const parts = [];
    const info = h("div", { class: "stack" });
    const meta = [kindPill(t.kind), t.year ? h("span", { class: "pill" }, t.year) : null];
    if (t.runtime) meta.push(h("span", { class: "pill" }, t.kind === "movie" ? `${Math.floor(t.runtime / 60)} Std. ${t.runtime % 60} Min.` : `${t.runtime} Min. pro Folge`));
    if (t.followed) meta.push(h("span", { class: "pill blue" }, "wird verfolgt"));
    info.append(h("div", { class: "row" }, meta), rich(t.status_text), t.overview ? h("p", { class: "overview" }, t.overview) : null);
    parts.push(h("div", { class: "detail" }, poster(t.poster, true), info));

    // Request
    const chosen = new Set(t.requestable_seasons);
    const estimateBox = h("div", { class: "small" });
    const drawEstimate = () => {
      const result = estimate(t, [...chosen]);
      estimateBox.replaceChildren(result ? rich(result.text, `rich ${result.warn ? "" : "muted"}`) : "");
      if (result && result.warn) estimateBox.firstChild.style.color = "var(--amber)";
    };
    if (t.requestable) {
      const box = h("div", { class: "box" }, h("h3", null, "Anfragen"));
      if (t.kind === "tv" && t.seasons.length) {
        const STATE = { 2: "angefragt", 3: "wird geladen", 4: "teilweise da", 5: "schon da", 6: "gesperrt" };
        box.append(h("div", { class: "seasons" }, t.seasons.map((s) => {
          const check = h("input", { type: "checkbox", checked: s.requestable, disabled: !s.requestable });
          check.addEventListener("change", () => {
            if (check.checked) chosen.add(s.number); else chosen.delete(s.number);
            drawEstimate();
            requestButton.disabled = !chosen.size;
          });
          return h("label", { class: `season ${s.requestable ? "" : "off"}` }, check, h("span", null, `Staffel ${s.number}`, h("small", null, s.requestable ? `${s.episodes} Folgen` : STATE[s.status] || "")));
        })));
      }
      let profile = null;
      if (t.profiles.length) {
        profile = h("select", { "aria-label": "Qualität" }, h("option", { value: "" }, "Standard-Qualität"), t.profiles.map((p) => h("option", { value: p.id }, p.name)));
        box.append(h("label", null, "Qualität", profile));
      }
      const requestButton = h("button", { class: "btn primary", type: "button" }, icon("download"), "Anfragen");
      requestButton.onclick = () => run(requestButton, () => api("/api/request", {
        body: { kind: t.kind, tmdb_id: t.tmdb_id, seasons: t.kind === "tv" ? [...chosen] : null, profile_id: profile && profile.value ? Number(profile.value) : null },
      }), () => closeModal());
      box.append(estimateBox, h("div", { class: "row" }, requestButton));
      parts.push(box);
      drawEstimate();
    }

    // Episodes and following (shows in Sonarr)
    if (t.kind === "tv" && t.sonarr) {
      const pick = h("button", { class: "btn", type: "button" }, icon("list"), "Einzelne Folgen wählen");
      pick.onclick = () => openPicker(t.tmdb_id);
      const start = h("input", { placeholder: "z.B. S02E04, leer = nur was neu kommt", maxlength: 20 });
      const seasonsToo = h("input", { type: "checkbox", checked: true });
      const follow = h("button", { class: "btn", type: "button" }, icon("follow"), "Verfolgen");
      const result = h("div");
      follow.onclick = () => run(follow, async () => {
        const answer = await api("/api/follows/add", { body: { tmdb_id: t.tmdb_id, start: start.value, new_seasons: seasonsToo.checked } });
        result.replaceChildren(rich(answer.message, "rich result-text"));
        return null;
      });
      parts.push(h("div", { class: "box" }, h("h3", null, "Folgen holen"),
        h("p", { class: "small muted" }, "Staffeln aufklappen und genau die Folgen anhaken, die du willst. Die Serie kommt dafür ohne Suche in Sonarr."),
        h("div", { class: "row" }, pick)));
      parts.push(h("div", { class: "box" }, h("h3", null, "Verfolgen"),
        h("p", { class: "small muted" }, "Jede neue Folge kommt von selbst, sobald sie erscheint. Mit „ab“ auch ältere Folgen ab dieser."),
        h("div", { class: "row" }, h("label", { class: "grow" }, "Ab Folge", start)),
        h("label", { class: "check" }, seasonsToo, "Auch neue Staffeln holen"),
        h("div", { class: "row" }, follow), result));
    }

    // Delete
    if (t.in_library) {
      const del = h("button", { class: "btn danger", type: "button" }, icon("trash"), "Aus der Bibliothek löschen");
      del.onclick = async () => {
        if (!(await confirmBox("Löschen?", `**${t.label}** löschen? Die Dateien kommen von der Platte, die Torrents aus qBittorrent. Das lässt sich nicht rückgängig machen.`, "Löschen"))) return;
        const answer = await run(null, () => api("/api/delete", { body: { kind: t.kind, tmdb_id: t.tmdb_id } }));
        if (answer && answer.deleted && searchState.query) {
          searchState.results = (searchState.results || []).filter((r) => !(r.kind === t.kind && r.tmdb_id === t.tmdb_id) || !searchState.library);
          if (location.hash.includes("suchen")) route();
        }
      };
      parts.push(h("div", { class: "box" }, h("h3", null, "Löschen"), h("p", { class: "small muted" }, "Wie /löschen: Dateien, Torrents und der Eintrag in Seerr."), h("div", { class: "row" }, del)));
    }
    const links = t.seerr_url ? h("a", { class: "btn ghost small", href: t.seerr_url, target: "_blank", rel: "noopener noreferrer" }, icon("external"), "In Seerr öffnen") : null;
    openModal({ title: t.label, body: h("div", { class: "stack" }, parts), foot: links ? [links] : null });
  }

  // --- episode picker ------------------------------------------------------------------------------
  async function openPicker(tmdbId) {
    openModal({ title: "Folgen wählen", body: loadingRow("Sonarr lädt die Folgen. Neue Serien brauchen dafür bis zu einer Minute …"), wide: true });
    let data;
    try {
      data = await api("/api/episodes/open", { body: { tmdb_id: tmdbId } });
    } catch (error) {
      openModal({ title: "Folgen wählen", body: banner("bad", error.message) });
      return;
    }
    const selected = new Set();
    let season = data.first_season;
    let fetched = false;
    const tabs = h("div", { class: "tabs", role: "tablist" });
    const list = h("div", { class: "episodes" });
    const summary = h("div", { class: "picker-sum" });
    const fetchButton = h("button", { class: "btn primary", type: "button" }, icon("download"), "Holen");
    const choosable = (e) => e.state === "missing" || e.state === "upcoming";
    const ofSeason = () => data.episodes.filter((e) => e.season === season);

    const drawTabs = () => tabs.replaceChildren(...data.seasons.map((s) => h("button", {
      class: `tab ${s.number === season ? "active" : ""}`, type: "button", role: "tab", "aria-selected": String(s.number === season),
      onclick: () => { season = s.number; drawTabs(); drawList(); },
    }, h("span", null, s.number === 0 ? "Specials" : `Staffel ${s.number}`, s.open ? h("span", { class: "dot", title: "Es fehlen noch Folgen" }) : null),
    h("small", null, `${s.have}/${s.total} da`))));
    const drawList = () => {
      list.replaceChildren(...ofSeason().map((e) => {
        const can = choosable(e);
        const check = h("input", { type: "checkbox", checked: e.state === "have" || selected.has(e.id), disabled: !can, "aria-label": e.label });
        const row = h("label", { class: `episode ${can ? "" : "off"} ${selected.has(e.id) ? "selected" : ""}` }, check,
          h("span", { class: "episode-name" }, e.label), h("span", { class: `state ${e.state}` }, e.state_text));
        check.addEventListener("change", () => {
          if (check.checked) selected.add(e.id); else selected.delete(e.id);
          row.classList.toggle("selected", check.checked);
          drawSummary();
        });
        return row;
      }));
    };
    const drawSummary = () => {
      const n = selected.size;
      const total = n * data.per_episode;
      const parts = [n ? h("strong", null, `${count(n, "Folge", "Folgen")} ≈ ${size(total)}`) : h("strong", null, "Noch nichts ausgewählt")];
      if (data.free !== null) {
        const after = data.free - total;
        parts.push(h("div", { class: after < data.min_free && n ? "" : "muted" }, `Frei ${size(data.free)}${n ? `, danach ≈ ${size(Math.max(after, 0))}` : ""}`));
        if (n && after < data.min_free) parts[1].style.color = "var(--amber)";
      }
      summary.replaceChildren(...parts);
      fetchButton.disabled = !n;
      fetchButton.lastChild.textContent = n ? `Holen (${n})` : "Holen";
    };
    const whole = h("button", { class: "btn small", type: "button" }, "Ganze Staffel");
    whole.onclick = () => { ofSeason().filter(choosable).forEach((e) => selected.add(e.id)); drawList(); drawSummary(); };
    const clear = h("button", { class: "btn ghost small", type: "button" }, "Staffel abwählen");
    clear.onclick = () => { ofSeason().forEach((e) => selected.delete(e.id)); drawList(); drawSummary(); };
    fetchButton.onclick = () => run(fetchButton, async () => {
      const answer = await api("/api/episodes/fetch", { body: { series_id: data.series_id, episode_ids: [...selected] } });
      fetched = true;
      openModal({
        title: data.label,
        body: rich(answer.message, "rich result-text"),
        foot: [h("div", { class: "grow" }), h("a", { class: "btn", href: "#/uebersicht", onclick: closeModal }, "Zur Übersicht"), h("button", { class: "btn primary", type: "button", onclick: closeModal }, "Fertig")],
      });
      return null;
    });

    const title = h("div", { class: "row grow" }, data.poster ? h("img", { class: "poster", src: data.poster, alt: "", referrerpolicy: "no-referrer" }) : null,
      h("div", { class: "grow" }, h("h2", null, data.label), h("div", { class: "small muted" }, data.added ? "Neu in Sonarr, nur zum Auswählen: ohne Holen wird sie wieder entfernt." : "Haken setzen und holen. Graue Folgen sind schon da oder laden.")));
    title.firstChild && title.firstChild.tagName === "IMG" && (title.firstChild.style.width = "44px");
    drawTabs();
    drawList();
    drawSummary();
    openModal({
      title,
      wide: true,
      body: h("div", { class: "stack" }, tabs, h("div", { class: "row" }, whole, clear), list),
      foot: [summary, h("button", { class: "btn ghost", type: "button", onclick: closeModal }, "Abbrechen"), fetchButton],
      onClose: () => {
        if (!fetched && data.added) api("/api/episodes/close", { body: { series_id: data.series_id } }).then((r) => toast(r && r.message)).catch(() => {});
      },
    });
  }

  // --- Kalender -----------------------------------------------------------------------------------
  let calendarDays = 14;
  async function renderCalendar(token) {
    const content = h("div", null, loadingRow());
    const range = h("div", { class: "segmented", role: "group", "aria-label": "Zeitraum" });
    const refresh = async () => {
      const data = await api(`/api/calendar?days=${calendarDays}`);
      if (token !== renderToken) return;
      content.replaceChildren(data.days.length ? calendarList(data.days) : empty("calendar", "Nichts geplant", "In dieser Zeit kommt nichts, was Sonarr oder Radarr holen."));
    };
    const drawRange = () => range.replaceChildren(...[7, 14, 31].map((n) => h("button", {
      type: "button", class: n === calendarDays ? "active" : "", onclick: () => { calendarDays = n; drawRange(); content.replaceChildren(loadingRow()); load(token, content, refresh); },
    }, `${n} Tage`)));
    drawRange();
    page.append(head("Kalender", "Was in den nächsten Tagen erscheint und automatisch geholt wird. Uhrzeiten in Ortszeit.", range), content);
    await load(token, content, refresh);
  }

  // --- Verfolgt -----------------------------------------------------------------------------------
  async function renderFollows(token) {
    const content = h("div", null, loadingRow());
    const refresh = async () => {
      const data = await api("/api/follows");
      if (token !== renderToken) return;
      if (!data.follows.length) {
        content.replaceChildren(empty("follow", "Keine Serie wird verfolgt", "Eine Serie suchen und dort „Verfolgen“ wählen.", h("a", { class: "btn", href: "#/suchen" }, icon("search"), "Serie suchen")));
        return;
      }
      content.replaceChildren(h("div", { class: "items" }, data.follows.map((f) => {
        const stop = h("button", { class: "btn danger small", type: "button" }, icon("stop"), "Beenden");
        stop.onclick = async () => {
          if (!(await confirmBox("Verfolgen beenden?", `**${f.label}** nicht mehr verfolgen? Angekündigte Folgen werden dann nicht mehr geholt. Was schon da ist, bleibt.`, "Beenden"))) return;
          await run(stop, () => api("/api/follows/stop", { body: { id: f.id } }), refresh);
        };
        return h("div", { class: "item simple" }, h("div", { class: "item-body" },
          h("div", { class: "item-title" }, h("h3", null, f.label), h("span", { class: "pill" }, f.start ? `ab ${f.start}` : "nur Neues"),
            f.new_seasons ? h("span", { class: "pill blue" }, "mit neuen Staffeln") : null),
          h("div", { class: "item-meta" }, f.next ? h("span", null, `Nächste Folge: ${f.next.when} · ${f.next.code}${f.next.title && f.next.title.toUpperCase() !== "TBA" ? ` · ${f.next.title}` : ""}`) : h("span", null, "Keine neue Folge angekündigt"),
            h("span", null, `von ${f.user}`)),
          h("div", { class: "item-actions" }, stop)));
      })));
    };
    const add = h("a", { class: "btn primary small", href: "#/suchen", onclick: () => { searchState.kind = "tv"; } }, icon("plus"), "Serie verfolgen");
    page.append(head("Verfolgt", "Serien, deren neue Folgen von selbst kommen.", add), content);
    await load(token, content, refresh);
  }

  // --- Minecraft ----------------------------------------------------------------------------------
  function memoryLimit(text) {
    const match = /^(\d+(?:\.\d+)?)([kmg])?/i.exec(text || "");
    if (!match) return null;
    const unit = (match[2] || "m").toLowerCase();
    return Number(match[1]) * (unit === "g" ? 1024 : unit === "k" ? 1 / 1024 : 1);
  }

  async function renderMinecraft(token) {
    const statusArea = h("div", null, loadingRow());
    const whitelistChips = h("div", { class: "chips" });
    const consoleOut = h("div", { class: "console", "aria-live": "polite" }, h("span", { class: "faint" }, "Ausgabe der Befehle erscheint hier."));
    const consoleInput = h("input", { placeholder: "Befehl, z.B. time set day oder op Name", autocomplete: "off", "aria-label": "Konsolenbefehl" });
    const consoleButton = h("button", { class: "btn", type: "submit" }, icon("terminal"), "Senden");
    const logOut = h("div", { class: "console" }, h("span", { class: "faint" }, "Server wählen und Log laden."));
    const logServer = h("select", { "aria-label": "Server" });
    const logLines = h("select", { "aria-label": "Zeilen" }, [100, 300, 1000].map((n) => h("option", { value: n, selected: n === 300 }, `${n} Zeilen`)));
    let status = null;

    const action = (button, path, body, after) => run(button, () => api(`/api/mc/${path}`, { body }), async (r) => { if (after) await after(r); await refresh(); });

    const draw = () => {
      const s = status;
      const parts = [];
      if (s.running) {
        const memory = memoryLimit(s.mem_limit);
        const stop = h("button", { class: "btn danger", type: "button" }, icon("stop"), "Stoppen");
        stop.onclick = async () => {
          const online = s.players ? s.players.online : 0;
          const text = online ? `Auf **${s.running}** sind ${count(online, "Spieler", "Spieler")} online (${s.players.names.join(", ")}). Trotzdem stoppen? Die Welt wird gespeichert.` : `**${s.running}** stoppen? Die Welt wird gespeichert, danach macht der Bot ein Backup.`;
          if (!(await confirmBox("Server stoppen?", text, online ? "Trotzdem stoppen" : "Stoppen"))) return;
          await action(stop, "stop", { force: online > 0 });
        };
        const backup = h("button", { class: "btn", type: "button" }, icon("save"), "Backup jetzt");
        backup.onclick = () => { toast("Backup läuft, das kann ein paar Minuten dauern …"); action(backup, "backup", { name: s.running }); };
        const server = s.servers.find((x) => x.name === s.running);
        parts.push(h("div", { class: `mc-hero ${s.ready ? "" : "starting"}` },
          h("div", null,
            h("div", { class: "row" }, h("h2", null, s.running), h("span", { class: `pill ${s.ready ? "green" : "amber"}` }, s.ready ? "bereit" : "startet …"), server ? h("span", { class: "pill" }, server.label) : null),
            h("div", { class: "mc-stats" },
              h("div", { class: "mc-stat" }, h("span", null, "Spieler"), h("strong", null, s.players ? `${s.players.online} / ${s.players.max}` : "–"),
                s.players && s.players.names.length ? h("small", { class: "muted" }, s.players.names.join(", ")) : null),
              h("div", { class: "mc-stat" }, h("span", null, "RAM"), h("strong", null, s.memory_mb !== null ? `${(s.memory_mb / 1024).toFixed(1)} GB` : "–"),
                memory && s.memory_mb !== null ? bar((100 * s.memory_mb) / memory, s.memory_mb / memory > 0.9 ? "red" : "green", true) : null),
              s.address ? h("div", { class: "mc-stat" }, h("span", null, "Adresse"), h("strong", { class: "mono" }, s.address), s.lan_address ? h("small", { class: "muted mono" }, `zuhause ${s.lan_address}`) : null) : null)),
          h("div", { class: "row" }, backup, stop)));
      } else {
        parts.push(h("div", { class: "mc-hero off" }, h("div", null, h("h2", null, "Kein Server an"),
          h("p", { class: "muted" }, `Starte unten einen Server. Er stoppt sich nach ${s.autostop_minutes} Minuten ohne Spieler selbst und macht dann ein Backup.`))));
      }
      parts.push(section("Server", s.servers.length ? h("div", { class: "servers" }, s.servers.map((x) => {
        const running = x.name === s.running;
        const start = h("button", { class: "btn primary small", type: "button", disabled: !!s.running }, icon("play"), "Starten");
        start.onclick = () => action(start, "start", { name: x.name });
        const backup = h("button", { class: "btn small", type: "button" }, icon("save"), "Backup");
        backup.onclick = () => { toast("Backup läuft, das kann ein paar Minuten dauern …"); action(backup, "backup", { name: x.name }); };
        const logs = h("button", { class: "btn ghost small", type: "button" }, icon("terminal"), "Log");
        logs.onclick = () => { logServer.value = x.name; loadLogs(); logOut.scrollIntoView({ behavior: "smooth", block: "center" }); };
        const del = h("button", { class: "btn danger small", type: "button", disabled: running }, icon("trash"));
        del.title = "Server löschen";
        del.onclick = async () => {
          if (!(await confirmBox("Server löschen?", `**${x.name}** löschen? Die Welt kommt in den Papierkorb-Ordner auf dem Server und lässt sich von dort noch retten.`, "Löschen"))) return;
          await action(del, "servers/delete", { name: x.name });
        };
        const newest = x.backups[0];
        return h("div", { class: `server ${running ? "running" : ""}` },
          h("div", { class: "row between" }, h("h3", null, x.name), running ? h("span", { class: "pill green" }, "läuft") : x.last ? h("span", { class: "pill" }, "zuletzt gespielt") : null),
          h("div", { class: "small muted" }, x.label),
          h("div", { class: "small muted" }, newest ? `${count(x.backups.length, "Backup", "Backups")}, neuestes ${dateTime(newest.time)} (${size(newest.size)})` : "Noch kein Backup"),
          h("div", { class: "item-actions" }, start, backup, logs, del));
      })) : empty("cube", "Noch kein Server", "Unten einen neuen Server anlegen.")));
      statusArea.replaceChildren(...parts);
      whitelistChips.replaceChildren(...(s.whitelist.length ? s.whitelist.map((name) => {
        const remove = h("button", { type: "button", "aria-label": `${name} entfernen`, title: "Entfernen" }, icon("x"));
        remove.onclick = async () => {
          if (!(await confirmBox("Von der Whitelist nehmen?", `**${name}** darf dann auf keinem Server mehr mitspielen.`, "Entfernen"))) return;
          await action(remove, "whitelist/remove", { player: name });
        };
        return h("span", { class: "chip" }, name, remove);
      }) : [h("span", { class: "muted" }, "Noch niemand.")]));
      const chosen = logServer.value;
      logServer.replaceChildren(...s.servers.map((x) => h("option", { value: x.name, selected: x.name === (chosen || s.running || (s.servers.find((y) => y.last) || {}).name) }, x.name)));
      consoleInput.disabled = consoleButton.disabled = !s.ready;
      consoleInput.placeholder = s.ready ? "Befehl, z.B. time set day oder op Name" : "Nur wenn ein Server bereit ist";
    };
    const refresh = async () => {
      const data = await api("/api/mc/status");
      if (token !== renderToken) return;
      status = data;
      draw();
    };
    async function loadLogs() {
      if (!logServer.value) return;
      logOut.replaceChildren(loadingRow());
      try {
        const data = await api(`/api/mc/logs?name=${encodeURIComponent(logServer.value)}&lines=${logLines.value}`);
        logOut.textContent = data.text || "(leer)";
        logOut.scrollTop = logOut.scrollHeight;
      } catch (error) {
        logOut.replaceChildren(h("span", { class: "err" }, error.message));
      }
    }

    // whitelist form
    const player = h("input", { placeholder: "Minecraft-Name", autocomplete: "off", maxlength: 16, "aria-label": "Minecraft-Name" });
    const addPlayer = h("button", { class: "btn", type: "submit" }, icon("plus"), "Hinzufügen");
    const whitelistForm = h("form", { class: "row" }, h("div", { class: "grow" }, player), addPlayer);
    whitelistForm.addEventListener("submit", (e) => {
      e.preventDefault();
      if (!player.value.trim()) return;
      action(addPlayer, "whitelist/add", { player: player.value.trim() }, () => { player.value = ""; });
    });

    // console
    const consoleForm = h("form", { class: "row" }, h("div", { class: "grow" }, consoleInput), consoleButton);
    let firstCommand = true;
    consoleForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      const command = consoleInput.value.trim();
      if (!command) return;
      if (firstCommand) { consoleOut.replaceChildren(); firstCommand = false; }
      consoleOut.append(h("div", { class: "cmd" }, `> ${command}`));
      consoleInput.value = "";
      try {
        const answer = await api("/api/mc/console", { body: { command } });
        consoleOut.append(h("div", null, answer.output || "(keine Ausgabe)"));
      } catch (error) {
        consoleOut.append(h("div", { class: "err" }, error.message));
      }
      consoleOut.scrollTop = consoleOut.scrollHeight;
    });

    // new server
    const newName = h("input", { placeholder: "z.B. survival", maxlength: 20, autocomplete: "off" });
    const newType = h("select", null, h("option", { value: "paper" }, "Paper (empfohlen)"), h("option", { value: "vanilla" }, "Vanilla"));
    const versionList = h("datalist", { id: "mc-versions" });
    const newVersion = h("input", { placeholder: "leer = neueste", list: "mc-versions", autocomplete: "off" });
    const loadVersions = async () => {
      try {
        const data = await api(`/api/mc/versions?type=${newType.value}&q=${encodeURIComponent(newVersion.value)}`);
        versionList.replaceChildren(...data.versions.map((v) => h("option", { value: v.value }, v.label)));
      } catch {
        versionList.replaceChildren();
      }
    };
    newType.addEventListener("change", loadVersions);
    newVersion.addEventListener("focus", loadVersions, { once: true });
    const create = h("button", { class: "btn primary", type: "submit" }, icon("plus"), "Anlegen");
    const createForm = h("form", { class: "stack" },
      h("div", { class: "cols" }, h("label", null, "Name", newName), h("label", null, "Typ", newType)),
      h("label", null, "Version", newVersion, versionList),
      h("p", { class: "small muted" }, "Die Version bleibt danach fest, damit eine Welt nie ungefragt auf eine neue Version wechselt."),
      h("div", { class: "row" }, create));
    createForm.addEventListener("submit", (e) => {
      e.preventDefault();
      action(create, "servers/create", { name: newName.value.trim(), type: newType.value, version: newVersion.value.trim() }, () => { newName.value = ""; newVersion.value = ""; });
    });
    const logButton = h("button", { class: "btn small", type: "button", onclick: loadLogs }, icon("refresh"), "Laden");

    page.append(head("Minecraft", "Server starten und stoppen, Whitelist, Backups, Konsole und Logs.", refreshButton(refresh)),
      statusArea,
      h("div", { class: "cols section" },
        h("div", { class: "card stack" }, h("h2", null, "Whitelist"), h("p", { class: "small muted" }, "Gilt für alle Server. Der Name wird bei Mojang geprüft."), whitelistChips, whitelistForm),
        h("div", { class: "card stack" }, h("h2", null, "Konsole"), h("p", { class: "small muted" }, "Befehle an den laufenden Server, ohne Schrägstrich."), consoleOut, consoleForm)),
      h("div", { class: "card stack section" }, h("div", { class: "row between" }, h("h2", null, "Log"), h("div", { class: "row" }, logServer, logLines, logButton)), logOut),
      h("details", { class: "card section" }, h("summary", null, h("strong", null, "Neuen Server anlegen")), h("div", { class: "section" }, createForm)));
    await load(token, statusArea, refresh);
    every(10000, () => load(token, statusArea, refresh, true));
  }

  // --- login and start ----------------------------------------------------------------------------
  function closeMenu() {
    $("#nav").classList.remove("open");
    $("#scrim").hidden = true;
    $("#menu-button").setAttribute("aria-expanded", "false");
  }
  function showLogin() {
    session = null;
    stopTimer();
    closeModal();
    $("#shell").hidden = true;
    $("#login").hidden = false;
    document.title = "Anmelden · Homelab Admin";
    setTimeout(() => $("#login-form").elements.namedItem("name").focus(), 0);
  }
  async function boot() {
    try {
      session = await api("/api/session");
    } catch (error) {
      if (session === null && !$("#login").hidden) return; // the login form is showing
      showLogin();
      return;
    }
    $("#login").hidden = true;
    $("#shell").hidden = false;
    buildNav();
    route();
  }

  $("#menu-button").append(icon("menu"));
  $("#menu-button").addEventListener("click", () => {
    const open = !$("#nav").classList.contains("open");
    $("#nav").classList.toggle("open", open);
    $("#scrim").hidden = !open;
    $("#menu-button").setAttribute("aria-expanded", String(open));
  });
  $("#scrim").addEventListener("click", closeMenu);
  $("#logout").addEventListener("click", async () => {
    try {
      await api("/api/logout", { body: {} });
    } catch {
      // logged out anyway
    }
    showLogin();
  });
  $("#login-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button");
    const error = $("#login-error");
    error.hidden = true;
    button.classList.add("busy");
    button.disabled = true;
    try {
      const fields = form.elements;
      await api("/api/login", { body: { name: fields.namedItem("name").value.trim(), password: fields.namedItem("password").value } });
      fields.namedItem("password").value = "";
      await boot();
    } catch (problem) {
      error.textContent = problem.message;
      error.hidden = false;
    } finally {
      button.classList.remove("busy");
      button.disabled = false;
    }
  });

  boot();
})();
