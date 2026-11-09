(() => {
  "use strict";

  // ───────── constants (collector/retention.py · rules.json 과 동일 값 유지) ─────────
  const CFG = window.PA_CONFIG || {};
  const ROOT = "mail";
  const RETAIN_DAYS = 180;
  const WARN_DAYS = 7;
  const BLOCK_RULE = { days: 30, min: 3, maxRate: 0.1 };
  const DEFAULT_CATS = ["식비", "장보기", "쇼핑", "교통", "여행/숙박", "구독", "통신", "공구/EDC", "의료", "공과금", "기타"];
  const DAY_MS = 86400000;

  // ───────── helpers ─────────
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const won = (n) => Math.round(Number(n) || 0).toLocaleString("ko-KR");
  const fx = (n) => (Number(n) || 0).toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
  const fmtKST = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul" });
  const todayKST = () => fmtKST.format(new Date());
  const addDays = (iso, n) => { const d = new Date(iso + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const addMonths = (ym, n) => { const [y, m] = ym.split("-").map(Number); return new Date(Date.UTC(y, m - 1 + n, 1)).toISOString().slice(0, 7); };
  const WD = ["일", "월", "화", "수", "목", "금", "토"];
  const wd = (iso) => WD[new Date(iso + "T00:00:00Z").getUTCDay()];
  const dayDiff = (a, b) => Math.round((new Date(b + "T00:00:00Z") - new Date(a + "T00:00:00Z")) / DAY_MS);
  const isHttps = (u) => typeof u === "string" && /^https:\/\//i.test(u);
  const isMailto = (u) => typeof u === "string" && /^mailto:/i.test(u);
  const isDate = (s) => /^\d{4}-\d{2}-\d{2}$/.test(s || "");
  const isMonth = (s) => /^\d{4}-\d{2}$/.test(s || "");
  const lsGet = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } };
  const obj = (v) => (v && typeof v === "object" ? v : {});
  const KEY_RE = /^[A-Za-z0-9_-]{1,64}$/;

  // ───────── retention (collector/retention.py 와 동일 규칙) ─────────
  const pathToRid = (p) => p.replace(/^\/+|\/+$/g, "").split("/").join("~");
  const PATH_RE = /^(daily|spend|reservations|adSenders)\/[A-Za-z0-9_\-/]{1,190}$/;
  const expiresOf = (iso) => new Date(new Date(iso).getTime() + RETAIN_DAYS * DAY_MS);
  function daysLeft(iso, now = Date.now()) {
    const t = new Date(iso).getTime();
    if (!Number.isFinite(t)) return 0;
    return Math.max(0, Math.ceil((t + RETAIN_DAYS * DAY_MS - now) / DAY_MS));
  }
  const isWarn = (e) => e && daysLeft(e.actionAt) <= WARN_DAYS;

  // ───────── spend dedupe ─────────
  // 같은 결제가 여러 계정·여러 알림으로 중복 수신 → 하나만 집계.
  // 동일 판정: 같은 날짜·통화·금액 + 시각 차 ≤ 10분 (시각 없으면 가맹점명 동일). notDup/excluded/hidden 제외
  const DUP_WINDOW_MIN = 10;
  const normMerchant = (s) => String(s || "").toLowerCase().replace(/\(주\)|주식회사|㈜|\(유\)/g, "").replace(/[^0-9a-zㄱ-ㆎ가-힣]/g, "");
  const toMin = (t) => { const m = /^(\d{1,2}):(\d{2})/.exec(t || ""); return m ? Number(m[1]) * 60 + Number(m[2]) : null; };
  const amtKey = (x) => Math.round((Number(x.amount) || 0) * 100);
  function sameTxn(a, b) {
    const ta = toMin(a.time), tb = toMin(b.time);
    if (ta !== null && tb !== null) return Math.abs(ta - tb) <= DUP_WINDOW_MIN;
    const ma = normMerchant(a.merchant), mb = normMerchant(b.merchant);
    return !!ma && ma === mb;
  }
  function dedupeSpend(o) {
    const all = Object.entries(obj(o))
      .filter(([, v]) => v && typeof v === "object" && !v.hidden)
      .map(([key, v]) => ({ key, ...v, dups: [], dupOf: null }));
    const buckets = {};
    all.forEach((x) => {
      if (x.excluded || x.notDup || !x.date) return;
      const b = `${x.date}|${x.currency || "KRW"}|${amtKey(x)}`;
      (buckets[b] = buckets[b] || []).push(x);
    });
    Object.values(buckets).forEach((items) => {
      if (items.length < 2) return;
      items.sort((a, b) => (a.time || "99:99").localeCompare(b.time || "99:99") || a.key.localeCompare(b.key));
      const clusters = [];
      items.forEach((x) => {
        const c = clusters.find((cl) => cl.some((y) => sameTxn(x, y)));
        if (c) c.push(x); else clusters.push([x]);
      });
      clusters.forEach((cl) => {
        if (cl.length < 2) return;
        const primary = cl.find((y) => y.manual) || cl[0];
        cl.forEach((y) => { if (y !== primary) { y.dupOf = primary.key; primary.dups.push(y.key); } });
      });
    });
    return all;
  }
  const activeKRW = (list) => list.filter((x) => !x.excluded && !x.dupOf && (x.currency || "KRW") === "KRW");

  let toastTimer;
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 2800);
  }

  // ───────── state ─────────
  const TABS = ["daily", "spend", "res", "block", "vault"];
  const S = {
    tab: TABS.includes(location.hash.slice(1)) ? location.hash.slice(1) : lsGet("md.tab", "daily"),
    day: todayKST(),
    month: todayKST().slice(0, 7),
    dayFilter: "all",
    resFilter: "upcoming",
    vaultFilter: "all",
    showDup: false,
    lastRun: null,
    accounts: {},
    dailyRange: {},
    spend: {},
    spendPrev: {},
    reservations: {},
    adSenders: {},
    retention: {},
    refs: [],
    dayRef: null,
    spendRefs: [],
  };
  if (!TABS.includes(S.tab)) S.tab = "daily";

  // ───────── boot ─────────
  const cfgOk = CFG.firebase && CFG.firebase.apiKey && !String(CFG.firebase.apiKey).includes("REPLACE_ME");
  if (isHttps(CFG.actionsUrl)) $("#runNow").href = CFG.actionsUrl; else $("#runNow").hidden = true;

  if (!cfgOk || typeof firebase === "undefined") {
    $("#runInfo").textContent = "설정 필요";
    const v = $("#loginView");
    v.hidden = false;
    v.innerHTML = `<h2>설정 필요</h2><p class="muted">${typeof firebase === "undefined"
      ? "Firebase SDK 로드 실패 (네트워크 확인 후 새로고침)"
      : "config.js 에 Firebase 웹 앱 구성값을 입력하세요."}</p>`;
    return;
  }

  firebase.initializeApp(CFG.firebase);
  const auth = firebase.auth();
  const db = firebase.database();
  auth.setPersistence(firebase.auth.Auth.Persistence.LOCAL).catch(() => {});
  const ref = (p = "") => db.ref(p ? `${ROOT}/${p}` : ROOT);

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
  }

  // ───────── write helpers ─────────
  async function write(updates, okMsg) {
    try {
      await ref().update(updates);
      if (okMsg) toast(okMsg);
      return true;
    } catch (e) {
      toast(e && e.code === "PERMISSION_DENIED" ? "권한 없음 — 보안 규칙의 UID를 확인하세요." : `저장 실패: ${e.message || e}`);
      return false;
    }
  }

  function retentionEntry(kind, path, label, at) {
    return { kind, path, label: String(label || path).slice(0, 80), actionAt: at, expiresAt: expiresOf(at).toISOString(), warnedAt: null };
  }

  function hideItem(path, label) {
    if (!PATH_RE.test(path)) { toast("잘못된 경로"); return Promise.resolve(false); }
    const at = new Date().toISOString();
    return write({
      [`${path}/hidden`]: true,
      [`${path}/hiddenAt`]: at,
      [`retention/${pathToRid(path)}`]: retentionEntry("hidden", path, label, at),
    }, `숨김 — ${RETAIN_DAYS}일 후 자동 삭제 (보관함에서 복원 가능)`);
  }

  function restoreEntry(rid) {
    const e = S.retention[rid];
    if (!e || !PATH_RE.test(e.path || "")) return Promise.resolve(false);
    const u = { [`retention/${rid}`]: null };
    if (e.path.startsWith("adSenders/")) {
      u[`${e.path}/status`] = "pending";
      u[`${e.path}/blockedAt`] = null;
      u[`${e.path}/hiddenAt`] = null;
      u[`${e.path}/unsub`] = null;
    } else {
      u[`${e.path}/hidden`] = null;
      u[`${e.path}/hiddenAt`] = null;
    }
    const msg = e.kind === "blocked"
      ? "차단 해제 — 이미 휴지통으로 옮긴 메일은 메일함 휴지통에서 직접 복구하세요."
      : "복원 완료";
    return write(u, msg);
  }

  function extendEntry(rid) {
    const e = S.retention[rid];
    if (!e || !PATH_RE.test(e.path || "")) return Promise.resolve(false);
    const at = new Date().toISOString();
    const field = e.kind === "blocked" ? "blockedAt" : "hiddenAt";
    return write({
      [`retention/${rid}/actionAt`]: at,
      [`retention/${rid}/expiresAt`]: expiresOf(at).toISOString(),
      [`retention/${rid}/warnedAt`]: null,
      [`${e.path}/${field}`]: at,
    }, `${RETAIN_DAYS}일 연장 — 만료 ${fmtKST.format(expiresOf(at))}`);
  }

  function blockSender(key) {
    const s = S.adSenders[key];
    if (!s || !KEY_RE.test(key)) return Promise.resolve(false);
    const at = new Date().toISOString();
    const path = `adSenders/${key}`;
    return write({
      [`${path}/status`]: "blocked",
      [`${path}/blockedAt`]: at,
      [`retention/${pathToRid(path)}`]: retentionEntry("blocked", path, s.name || s.sender, at),
    });
  }

  function dismissSender(key) {
    const s = S.adSenders[key];
    if (!s || !KEY_RE.test(key)) return Promise.resolve(false);
    const at = new Date().toISOString();
    const path = `adSenders/${key}`;
    return write({
      [`${path}/status`]: "dismissed",
      [`${path}/hiddenAt`]: at,
      [`retention/${pathToRid(path)}`]: retentionEntry("hidden", path, s.name || s.sender, at),
    }, "유지 — 차단 요청 목록에서 숨김 (보관함에서 복원 가능)");
  }

  // ───────── auth ─────────
  const authErrMap = {
    "auth/invalid-credential": "이메일 또는 비밀번호가 올바르지 않습니다.",
    "auth/invalid-email": "이메일 형식이 올바르지 않습니다.",
    "auth/wrong-password": "비밀번호가 올바르지 않습니다.",
    "auth/user-not-found": "등록되지 않은 계정입니다.",
    "auth/too-many-requests": "시도 횟수 초과. 잠시 후 다시 시도하세요.",
    "auth/unauthorized-domain": "Firebase Auth 승인 도메인에 현재 도메인을 추가하세요.",
    "auth/network-request-failed": "네트워크 오류",
    "auth/popup-closed-by-user": "로그인 창이 닫혔습니다.",
    "auth/popup-blocked": "팝업이 차단되었습니다.",
    "auth/provider-already-linked": "이미 비밀번호가 연결되어 있습니다.",
    "auth/operation-not-allowed": "Firebase 콘솔 → Authentication → 이메일/비밀번호 로그인을 사용 설정하세요.",
    "auth/weak-password": "비밀번호가 너무 약합니다.",
    "auth/email-already-in-use": "같은 이메일의 별도 계정이 존재합니다. Firebase 콘솔 Users에서 삭제 후 재시도하세요.",
    "auth/credential-already-in-use": "같은 이메일의 별도 계정이 존재합니다. Firebase 콘솔 Users에서 삭제 후 재시도하세요.",
  };
  const authMsg = (e) => authErrMap[e && e.code] || (e && e.message) || String(e);

  const isStandalone = window.navigator.standalone === true || window.matchMedia("(display-mode: standalone)").matches;
  if (isStandalone) {
    $("#googleLogin").hidden = true;
    $("#loginDivider").hidden = true;
    $("#standaloneHint").hidden = false;
  }
  const hasPassword = (u) => !!u && u.providerData.some((p) => p.providerId === "password");
  const updatePwBtn = (u) => { $("#pwLinkBtn").hidden = !u || isStandalone || hasPassword(u); };

  $("#googleLogin").addEventListener("click", async () => {
    $("#loginErr").textContent = "";
    const provider = new firebase.auth.GoogleAuthProvider();
    provider.setCustomParameters({ prompt: "select_account" });
    try {
      await auth.signInWithPopup(provider);
    } catch (e) {
      if (e.code === "auth/popup-blocked" || e.code === "auth/operation-not-supported-in-this-environment") {
        try { await auth.signInWithRedirect(provider); } catch (e2) { $("#loginErr").textContent = authMsg(e2); }
      } else {
        $("#loginErr").textContent = authMsg(e);
      }
    }
  });

  $("#emailLogin").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    $("#loginErr").textContent = "";
    const email = $("#loginEmail").value.trim();
    const pw = $("#loginPw").value;
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) { $("#loginErr").textContent = "이메일 형식이 올바르지 않습니다."; return; }
    if (pw.length < 6) { $("#loginErr").textContent = "비밀번호는 6자 이상입니다."; return; }
    try { await auth.signInWithEmailAndPassword(email, pw); } catch (e) { $("#loginErr").textContent = authMsg(e); }
  });

  $("#pwLinkBtn").addEventListener("click", async () => {
    const user = auth.currentUser;
    if (!user) return;
    if (hasPassword(user)) { toast("이미 비밀번호가 연결되어 있습니다."); return; }
    if (!user.email) { toast("계정 이메일을 확인할 수 없습니다."); return; }
    const pw = window.prompt(`${user.email}\n홈 화면 앱용 비밀번호 (8자 이상)`);
    if (pw === null) return;
    if (pw.length < 8) { toast("8자 이상 입력하세요."); return; }
    const pw2 = window.prompt("비밀번호 확인");
    if (pw2 === null) return;
    if (pw !== pw2) { toast("비밀번호가 일치하지 않습니다."); return; }
    const cred = firebase.auth.EmailAuthProvider.credential(user.email, pw);
    const done = () => { toast("연결 완료 — 홈 화면 앱에서 이메일로 로그인하세요."); updatePwBtn(auth.currentUser); };
    try {
      await user.linkWithCredential(cred);
      done();
    } catch (e) {
      if (e.code !== "auth/requires-recent-login") { toast(authMsg(e)); return; }
      try {
        const provider = new firebase.auth.GoogleAuthProvider();
        provider.setCustomParameters({ login_hint: user.email });
        await user.reauthenticateWithPopup(provider);
        await user.linkWithCredential(cred);
        done();
      } catch (e2) { toast(authMsg(e2)); }
    }
  });

  $("#logoutBtn").addEventListener("click", () => auth.signOut().catch((e) => toast(authMsg(e))));
  $("#copyUid").addEventListener("click", async () => {
    const uid = $("#myUid").textContent;
    try { await navigator.clipboard.writeText(uid); toast("UID 복사됨"); }
    catch {
      const r = document.createRange(); r.selectNodeContents($("#myUid"));
      const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(r);
      toast("길게 눌러 복사하세요");
    }
  });

  function showOnly(id) {
    ["loginView", "deniedView"].forEach((v) => { $("#" + v).hidden = v !== id; });
    $$(".tab").forEach((t) => { t.hidden = true; });
    $("#tabbar").hidden = id !== null;
    if (id !== null) $("#expBanner").hidden = true;
  }

  function detach() {
    S.refs.forEach((r) => r.off());
    S.refs = [];
    if (S.dayRef) { S.dayRef.off(); S.dayRef = null; }
    S.spendRefs.forEach((r) => r.off());
    S.spendRefs = [];
  }

  auth.getRedirectResult().catch((e) => { $("#loginErr").textContent = authMsg(e); });

  auth.onAuthStateChanged(async (user) => {
    detach();
    $("#logoutBtn").hidden = !user;
    updatePwBtn(user);
    if (!user) {
      showOnly("loginView");
      $("#runInfo").textContent = "로그인 필요";
      $("#runDot").className = "dot";
      return;
    }
    try {
      await ref("meta").limitToFirst(1).once("value");
    } catch (e) {
      if (e && e.code === "PERMISSION_DENIED") {
        $("#myUid").textContent = user.uid;
        showOnly("deniedView");
        $("#runInfo").textContent = "권한 없음";
        return;
      }
      toast(`연결 오류: ${e.message || e}`);
    }
    showOnly(null);
    attach();
    switchTab(S.tab);
  });

  // ───────── subscriptions ─────────
  function sub(path, fn, query) {
    const r = query ? query(ref(path)) : ref(path);
    r.on("value", (snap) => fn(snap.val()), (e) => toast(`${path} 읽기 실패: ${e.code || e.message}`));
    S.refs.push(r);
  }

  function attach() {
    sub("meta/lastRun", (v) => { S.lastRun = v; renderRun(); });
    sub("accounts", (v) => { S.accounts = obj(v); renderRun(); renderDaily(); });
    sub("reservations", (v) => { S.reservations = obj(v); renderRes(); renderDaily(); });
    sub("adSenders", (v) => { S.adSenders = obj(v); renderBlock(); });
    sub("retention", (v) => { S.retention = obj(v); renderVault(); renderBanner(); });
    attachDay();
    attachSpend();
  }

  function attachDay() {
    if (S.dayRef) S.dayRef.off();
    const start = addDays(S.day, -13);
    S.dayRef = ref("daily").orderByKey().startAt(start).endAt(S.day);
    S.dayRef.on("value", (snap) => { S.dailyRange = obj(snap.val()); renderDaily(); },
      (e) => toast(`일별 읽기 실패: ${e.code || e.message}`));
  }

  function attachSpend() {
    S.spendRefs.forEach((r) => r.off());
    const cur = ref(`spend/${S.month}`);
    const prev = ref(`spend/${addMonths(S.month, -1)}`);
    cur.on("value", (s) => { S.spend = obj(s.val()); renderSpend(); }, (e) => toast(`지출 읽기 실패: ${e.code}`));
    prev.on("value", (s) => { S.spendPrev = obj(s.val()); renderSpend(); }, () => {});
    S.spendRefs = [cur, prev];
  }

  // ───────── tabs ─────────
  function switchTab(tab) {
    if (!TABS.includes(tab)) tab = "daily";
    S.tab = tab;
    lsSet("md.tab", tab);
    if (location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
    $$(".tab").forEach((t) => { t.hidden = t.id !== `tab-${tab}`; });
    $$("#tabbar button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    window.scrollTo({ top: 0 });
    renderBanner();
  }
  $("#tabbar").addEventListener("click", (e) => { const b = e.target.closest("button[data-tab]"); if (b) switchTab(b.dataset.tab); });
  window.addEventListener("hashchange", () => { const t = location.hash.slice(1); if (TABS.includes(t) && !$("#tabbar").hidden) switchTab(t); });
  $("#expGo").addEventListener("click", () => { S.vaultFilter = "all"; switchTab("vault"); renderVault(); });

  // ───────── header / banner ─────────
  function renderRun() {
    const r = S.lastRun;
    const dot = $("#runDot");
    if (!r || !r.startedAt) { $("#runInfo").textContent = "수집 기록 없음"; dot.className = "dot"; return; }
    const ageH = (Date.now() - new Date(r.finishedAt || r.startedAt).getTime()) / 3600000;
    const bad = Object.entries(obj(r.accounts)).filter(([, a]) => a && a.ok === false).map(([k]) => k);
    dot.className = "dot " + (bad.length ? "bad" : ageH > 30 ? "stale" : "ok");
    const t = new Date(r.finishedAt || r.startedAt);
    const when = `${fmtKST.format(t).slice(5)} ${t.toLocaleTimeString("ko-KR", { timeZone: "Asia/Seoul", hour: "2-digit", minute: "2-digit", hour12: false })}`;
    $("#runInfo").textContent = bad.length ? `${when} 수집 · 실패 ${bad.join(", ")}` : `${when} 수집${ageH > 30 ? " · 지연" : ""}`;
  }

  function renderBanner() {
    const warn = Object.values(S.retention).filter((e) => e && PATH_RE.test(e.path || "") && isWarn(e));
    const n = warn.length;
    const b = $("#vaultBadge");
    b.hidden = !n; b.textContent = n;
    const banner = $("#expBanner");
    banner.hidden = !n || $("#tabbar").hidden || S.tab === "vault";
    if (n) {
      const soon = Math.min(...warn.map((e) => daysLeft(e.actionAt)));
      $("#expText").textContent = `숨김·차단 ${n}건이 ${soon === 0 ? "오늘" : `D-${soon}`} 이내 자동 삭제됩니다`;
    }
  }

  // ───────── 일별 정리 ─────────
  $("#dayPick").addEventListener("change", (e) => { if (isDate(e.target.value)) { S.day = e.target.value; attachDay(); } });
  $$("[data-day]").forEach((b) => b.addEventListener("click", () => {
    const n = addDays(S.day, Number(b.dataset.day));
    if (n > todayKST()) return;
    S.day = n; attachDay();
  }));
  $("#dayFilter").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-f]"); if (!b) return;
    S.dayFilter = b.dataset.f;
    $$("#dayFilter button").forEach((x) => x.classList.toggle("on", x === b));
    renderDaily();
  });

  function accLabel(aid) { return (S.accounts[aid] && S.accounts[aid].label) || aid; }

  function renderDaily() {
    $("#dayPick").value = S.day;
    $("#dayPick").max = todayKST();
    const day = obj(S.dailyRange[S.day]);
    const accs = Object.keys(day).sort();
    let total = 0, unread = 0, ads = 0, spend = 0, resCnt = 0;
    const items = [];
    accs.forEach((aid) => {
      const a = obj(day[aid]);
      total += a.total || 0; unread += a.unread || 0; ads += a.ads || 0; spend += a.spendKRW || 0; resCnt += a.reservations || 0;
      Object.entries(obj(a.items)).forEach(([k, it]) => {
        if (!it || it.hidden) return;
        items.push({ ...it, key: k, aid, path: `daily/${S.day}/${aid}/items/${k}` });
      });
    });
    $("#dayKpis").innerHTML = [
      ["수신", total, `${accs.length}개 계정`],
      ["미열람", unread, total ? `${Math.round((unread / total) * 100)}%` : "-"],
      ["광고", ads, total ? `${Math.round((ads / total) * 100)}%` : "-"],
      ["지출", `₩${won(spend)}`, `예약 ${resCnt}건`],
    ].map(([k, v, d]) => `<div class="kpi"><div class="k">${k}</div><div class="v">${esc(v)}</div><div class="d">${esc(d)}</div></div>`).join("");

    const f = S.dayFilter;
    const shown = items.filter((x) => f === "all" ? x.tag !== "ad"
      : f === "unread" ? x.unread && x.tag !== "ad"
        : f === "key" ? x.tag === "spend" || x.tag === "res"
          : x.tag === "ad");
    shown.sort((a, b) => (b.time || "").localeCompare(a.time || ""));
    const TAG = { spend: "지출", res: "예약", ad: "광고", mail: "" };
    const adNote = f === "all" && ads ? `<p class="muted small">광고 ${ads}건은 '광고' 필터에서 확인</p>` : "";
    $("#dayList").innerHTML = (shown.length ? `<ul class="list">${shown.map((x) => `
      <li class="${x.unread ? "unread" : ""}">
        <span class="s">${TAG[x.tag] ? `<span class="tag t-${x.tag}">${TAG[x.tag]}</span>` : ""}${esc(x.subject || "(제목 없음)")}</span>
        <span class="r">${esc(x.time || "")}</span>
        <span class="f">${esc(x.from)} · ${esc(accLabel(x.aid))}</span>
        <button class="linkish" type="button" data-hide="${esc(x.path)}" data-label="${esc(x.subject || "")}">숨기기</button>
      </li>`).join("")}</ul>` : `<ul class="list"><li class="empty">${total ? "해당 메일 없음" : "수집 데이터 없음"}</li></ul>`) + adNote;

    // 14일 추이
    const days = Array.from({ length: 14 }, (_, i) => addDays(S.day, i - 13));
    const vals = days.map((d) => {
      const o = obj(S.dailyRange[d]);
      let t = 0, a = 0;
      Object.values(o).forEach((x) => { t += (x && x.total) || 0; a += (x && x.ads) || 0; });
      return { d, n: t - a, a };
    });
    const max = Math.max(1, ...vals.map((v) => v.n + v.a));
    $("#dayTrend").innerHTML = vals.map((v) => `
      <div class="col${v.d === S.day ? " sel" : ""}" data-d="${v.d}" title="${v.d} 일반 ${v.n} · 광고 ${v.a}">
        <i class="a" style="height:${(v.n / max) * 100}%"></i><i class="b" style="height:${(v.a / max) * 100}%"></i>
        <span>${Number(v.d.slice(8))}</span>
      </div>`).join("");
  }
  $("#dayList").addEventListener("click", (e) => {
    const b = e.target.closest("[data-hide]"); if (!b) return;
    hideItem(b.dataset.hide, b.dataset.label);
  });
  $("#dayTrend").addEventListener("click", (e) => {
    const c = e.target.closest(".col[data-d]"); if (!c) return;
    S.day = c.dataset.d; attachDay();
  });

  // ───────── 월별 지출 ─────────
  $("#monthPick").addEventListener("change", (e) => { if (isMonth(e.target.value)) { S.month = e.target.value; attachSpend(); } });
  $$("[data-month]").forEach((b) => b.addEventListener("click", () => {
    const n = addMonths(S.month, Number(b.dataset.month));
    if (n > todayKST().slice(0, 7)) return;
    S.month = n; attachSpend();
  }));
  $("#dupToggle").addEventListener("click", () => { S.showDup = !S.showDup; renderSpend(); });

  function renderSpend() {
    $("#monthPick").value = S.month;
    $("#monthPick").max = todayKST().slice(0, 7);
    const list = dedupeSpend(S.spend);
    const act = activeKRW(list);
    const sum = act.reduce((s, x) => s + (Number(x.amount) || 0), 0);
    const prevSum = activeKRW(dedupeSpend(S.spendPrev)).reduce((s, x) => s + (Number(x.amount) || 0), 0);
    const fxList = list.filter((x) => !x.excluded && !x.dupOf && (x.currency || "KRW") !== "KRW");
    const fxText = Object.entries(fxList.reduce((m, x) => { m[x.currency] = (m[x.currency] || 0) + (Number(x.amount) || 0); return m; }, {}))
      .map(([c, v]) => `${c} ${fx(v)}`).join(" · ") || "-";
    const delta = prevSum ? Math.round(((sum - prevSum) / Math.abs(prevSum)) * 100) : null;
    const isCur = S.month === todayKST().slice(0, 7);
    const daysIn = isCur ? Number(todayKST().slice(8)) : new Date(Date.UTC(+S.month.slice(0, 4), +S.month.slice(5), 0)).getUTCDate();
    $("#spendKpis").innerHTML = [
      ["합계", `₩${won(sum)}`, delta === null ? "전월 데이터 없음" : `전월 대비 ${delta > 0 ? "+" : ""}${delta}%`, delta === null ? "" : delta > 0 ? "up" : "down"],
      ["건수", act.length, `중복 병합 ${list.filter((x) => x.dupOf).length}건`, ""],
      ["일평균", `₩${won(sum / Math.max(1, daysIn))}`, `${daysIn}일 기준`, ""],
      ["외화", fxText, "합계 미포함", ""],
    ].map(([k, v, d, c]) => `<div class="kpi"><div class="k">${k}</div><div class="v">${esc(v)}</div><div class="d ${c}">${esc(d)}</div></div>`).join("");

    const cats = {};
    act.forEach((x) => { const c = x.category || "기타"; cats[c] = (cats[c] || 0) + (Number(x.amount) || 0); });
    const ordered = Object.entries(cats).sort((a, b) => b[1] - a[1]);
    const cmax = Math.max(1, ...ordered.map(([, v]) => Math.abs(v)));
    $("#spendCats").innerHTML = ordered.length ? ordered.map(([c, v]) => `
      <div class="bar"><span class="n">${esc(c)}</span><span class="track"><span class="fill" style="width:${(Math.abs(v) / cmax) * 100}%"></span></span>
      <span class="v">₩${won(v)}<small>${sum ? Math.round((v / sum) * 100) : 0}%</small></span></div>`).join("")
      : `<p class="muted small" style="text-align:center">이 달 지출 없음</p>`;

    const dupCnt = list.filter((x) => x.dupOf).length;
    const dt = $("#dupToggle");
    dt.hidden = !dupCnt;
    dt.textContent = S.showDup ? "중복 숨기기" : `중복 ${dupCnt}건 보기`;

    const visible = list.filter((x) => S.showDup || !x.dupOf);
    const byDate = {};
    visible.forEach((x) => { (byDate[x.date || "-"] = byDate[x.date || "-"] || []).push(x); });
    $("#spendList").innerHTML = Object.keys(byDate).sort().reverse().map((d) => {
      const rows = byDate[d].sort((a, b) => (b.time || "").localeCompare(a.time || ""));
      const daySum = activeKRW(rows).reduce((s, x) => s + (Number(x.amount) || 0), 0);
      return `<div class="dgroup"><div class="dhead"><span>${esc(d)}${isDate(d) ? ` (${wd(d)})` : ""}</span><span class="num">₩${won(daySum)}</span></div>
        ${rows.map((x) => {
          const cur = x.currency || "KRW";
          const amt = cur === "KRW" ? `₩${won(x.amount)}` : `${cur} ${fx(x.amount)}`;
          const chips = [x.dupOf ? `<span class="chip dup">중복</span>` : "", x.dups.length ? `<span class="chip merged">+${x.dups.length} 병합</span>` : "",
            x.manual ? `<span class="chip manual">수정</span>` : ""].join("");
          return `<div class="item${x.excluded || x.dupOf ? " excluded" : ""}" data-sp="${esc(x.key)}" role="button" tabindex="0">
            <span class="s">${esc(x.merchant || x.from || "-")}${chips}</span>
            <span class="amt${Number(x.amount) < 0 ? " neg" : ""}">${esc(amt)}</span>
            <span class="f">${esc(x.category || "기타")} · ${esc(x.time || "")} · ${esc(accLabel(x.account))}</span>
          </div>`;
        }).join("")}</div>`;
    }).join("") || `<p class="muted small" style="text-align:center">항목 없음</p>`;
  }

  let spKey = null;
  const catOptions = () => {
    const extra = Object.values(S.spend).map((x) => x && x.category).filter((c) => c && !DEFAULT_CATS.includes(c));
    return [...DEFAULT_CATS, ...new Set(extra)];
  };
  function openSpend(key) {
    const x = S.spend[key];
    if (!x || !KEY_RE.test(key)) return;
    spKey = key;
    $("#spTitle").textContent = x.merchant || "지출 항목";
    $("#spMeta").textContent = `${x.date || ""} ${x.time || ""} · ${x.subject || ""}`;
    $("#spCat").innerHTML = catOptions().map((c) => `<option${c === (x.category || "기타") ? " selected" : ""}>${esc(c)}</option>`).join("");
    $("#spExclude").checked = !!x.excluded;
    $("#spNotDup").checked = !!x.notDup;
    if (typeof $("#spendDialog").showModal === "function") $("#spendDialog").showModal();
  }
  $("#spendList").addEventListener("click", (e) => { const it = e.target.closest("[data-sp]"); if (it) openSpend(it.dataset.sp); });
  $("#spendList").addEventListener("keydown", (e) => {
    if (e.key !== "Enter" && e.key !== " ") return;
    const it = e.target.closest("[data-sp]"); if (it) { e.preventDefault(); openSpend(it.dataset.sp); }
  });
  $("#spendDialog").addEventListener("close", () => {
    if ($("#spendDialog").returnValue !== "ok" || !spKey) return;
    const p = `spend/${S.month}/${spKey}`;
    write({
      [`${p}/category`]: $("#spCat").value,
      [`${p}/excluded`]: $("#spExclude").checked || null,
      [`${p}/notDup`]: $("#spNotDup").checked || null,
      [`${p}/manual`]: true,
    }, "저장됨");
  });
  $("#spHide").addEventListener("click", () => {
    if (!spKey) return;
    const x = S.spend[spKey] || {};
    $("#spendDialog").close("cancel");
    hideItem(`spend/${S.month}/${spKey}`, `${x.merchant || "지출"} ₩${won(x.amount)}`);
  });

  // ───────── 예약 일자별 ─────────
  $("#resFilter").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-f]"); if (!b) return;
    S.resFilter = b.dataset.f;
    $$("#resFilter button").forEach((x) => x.classList.toggle("on", x === b));
    renderRes();
  });

  function renderRes() {
    const today = todayKST();
    const all = Object.entries(S.reservations)
      .filter(([k, v]) => v && typeof v === "object" && !v.hidden && isDate(v.eventDate) && KEY_RE.test(k))
      .map(([key, v]) => ({ key, ...v }));
    const f = S.resFilter;
    const list = all.filter((x) => f === "all" || (f === "upcoming" ? x.eventDate >= today : x.eventDate < today));
    list.sort((a, b) => (f === "past" ? -1 : 1) * (a.eventDate.localeCompare(b.eventDate) || (a.eventTime || "").localeCompare(b.eventTime || "")));
    const groups = [];
    list.forEach((x) => {
      const g = groups[groups.length - 1];
      if (g && g.d === x.eventDate) g.items.push(x); else groups.push({ d: x.eventDate, items: [x] });
    });
    $("#resList").innerHTML = groups.length ? groups.map((g) => {
      const dd = dayDiff(today, g.d);
      const tag = dd === 0 ? `<span class="dday today">오늘</span>` : dd > 0 ? `<span class="dday">D-${dd}</span>` : `<span class="dday">${-dd}일 전</span>`;
      return `<div class="rday"><div class="rdate"><div class="mm">${g.d.slice(0, 7)} (${wd(g.d)})</div><div class="dd">${Number(g.d.slice(8))}</div>${tag}</div>
        <div>${g.items.map((x) => `<div class="ritem${x.status === "canceled" ? " canceled" : ""}">
          <div class="s">${x.eventTime ? `<span class="num">${esc(x.eventTime)}</span> ` : ""}${esc(x.title || x.subject || "(제목 없음)")}</div>
          <div class="f">${esc(x.vendor || "")}${x.place ? ` · ${esc(x.place)}` : ""} · ${esc(accLabel(x.account))}${x.status === "canceled" ? " · 취소됨" : ""}</div>
          <div class="acts"><button class="linkish" type="button" data-hide="reservations/${esc(x.key)}" data-label="${esc(x.title || "")}">숨기기</button></div>
        </div>`).join("")}</div></div>`;
    }).join("") : `<p class="muted small" style="text-align:center">${f === "upcoming" ? "예정된 예약 없음" : "예약 없음"}</p>`;
  }
  $("#resList").addEventListener("click", (e) => { const b = e.target.closest("[data-hide]"); if (b) hideItem(b.dataset.hide, b.dataset.label); });

  // ───────── 광고 차단 요청 ─────────
  $("#blockRule").textContent = `기준: 최근 ${BLOCK_RULE.days}일 광고성 메일 ${BLOCK_RULE.min}통 이상 · 열람률 ${BLOCK_RULE.maxRate * 100}% 이하 발신자. ` +
    `[차단] → 다음 수집 때 원클릭 수신거부(지원 시) + 해당 발신자 메일 휴지통 이동, 이후 신규 메일도 매일 휴지통 이동. ` +
    `[유지] → 목록에서 숨김. 둘 다 ${RETAIN_DAYS}일 후 자동 삭제되며 차단 항목은 삭제 시 차단도 해제됩니다.`;

  function methodBadge(s) {
    if (s.oneClick && isHttps(s.unsubHttp)) return `<span class="method auto">자동 수신거부</span>`;
    if (isHttps(s.unsubHttp) || isMailto(s.unsubMailto)) return `<span class="method manual">수동 수신거부</span>`;
    return `<span class="method none">휴지통만</span>`;
  }

  function renderBlock() {
    const all = Object.entries(S.adSenders).filter(([k, v]) => v && typeof v === "object" && KEY_RE.test(k)).map(([key, v]) => ({ key, ...v }));
    const pending = all.filter((x) => x.status === "pending").sort((a, b) => (b.total || 0) - (a.total || 0));
    const active = all.filter((x) => x.status === "blocked").sort((a, b) => String(b.blockedAt || "").localeCompare(String(a.blockedAt || "")));
    $("#blockCount").textContent = pending.length;
    $("#blockAll").hidden = pending.length < 2;
    const badge = $("#blockBadge"); badge.hidden = !pending.length; badge.textContent = pending.length;

    $("#blockPending").innerHTML = pending.length ? pending.map((x) => {
      const rate = Math.round((Number(x.rate) || 0) * 100);
      return `<div class="bcard">
        <div class="top"><div style="min-width:0"><div class="nm">${esc(x.name || x.sender)}</div><div class="em">${esc(x.sender)}</div></div>${methodBadge(x)}</div>
        <div class="ls">최근: ${esc(x.lastSubject || "")}</div>
        <div class="meter" aria-label="열람률"><i style="width:${Math.min(100, rate)}%"></i></div>
        <div class="stats"><span>${BLOCK_RULE.days}일 ${x.total || 0}통</span><span>열람 ${x.read || 0}통 (${rate}%)</span><span>${esc(accLabel(x.account))}</span></div>
        <div class="acts"><button class="btn small" type="button" data-keep="${esc(x.key)}">유지</button><button class="btn small danger" type="button" data-block="${esc(x.key)}">차단</button></div>
      </div>`;
    }).join("") : `<p class="muted small" style="text-align:center">차단 요청 없음</p>`;

    $("#blockActive").innerHTML = active.length ? active.map((x) => {
      const rid = pathToRid(`adSenders/${x.key}`);
      const left = daysLeft(x.blockedAt);
      const u = obj(x.unsub);
      const unsubTxt = !u.method ? "다음 수집 때 처리" : u.ok ? "수신거부 완료" : u.method === "oneclick" ? `수신거부 실패(${esc(u.code)})` : "수동 수신거부 필요";
      const manual = !u.ok && (isHttps(x.unsubHttp) || isMailto(x.unsubMailto))
        ? ` · <a href="${esc(isHttps(x.unsubHttp) ? x.unsubHttp : x.unsubMailto)}" target="_blank" rel="noopener noreferrer">수신거부 링크</a>` : "";
      return `<div class="bcard">
        <div class="top"><div style="min-width:0"><div class="nm">${esc(x.name || x.sender)}</div><div class="em">${esc(x.sender)}</div></div><span class="state">${left <= WARN_DAYS ? `<span class="warn-t">D-${left}</span>` : `D-${left}`}</span></div>
        <div class="stats"><span>${unsubTxt}${manual}</span><span>휴지통 ${x.trashed || 0}건</span><span>${esc(accLabel(x.account))}</span></div>
        <div class="acts"><button class="btn small" type="button" data-restore="${esc(rid)}">차단 해제</button><button class="btn small" type="button" data-extend="${esc(rid)}">${RETAIN_DAYS}일 연장</button></div>
      </div>`;
    }).join("") : `<p class="muted small" style="text-align:center">차단 중인 발신자 없음</p>`;
  }

  $("#tab-block").addEventListener("click", async (e) => {
    const t = e.target.closest("button"); if (!t) return;
    if (t.dataset.block) { t.disabled = true; if (await blockSender(t.dataset.block)) toast("차단 등록 — 다음 수집 때 휴지통 이동"); t.disabled = false; }
    else if (t.dataset.keep) dismissSender(t.dataset.keep);
    else if (t.dataset.restore) restoreEntry(t.dataset.restore);
    else if (t.dataset.extend) extendEntry(t.dataset.extend);
    else if (t.id === "blockAll") {
      const keys = Object.entries(S.adSenders).filter(([k, v]) => v && v.status === "pending" && KEY_RE.test(k)).map(([k]) => k);
      if (!keys.length || !window.confirm(`${keys.length}개 발신자를 모두 차단할까요?`)) return;
      const at = new Date().toISOString();
      const u = {};
      keys.forEach((k) => {
        const p = `adSenders/${k}`;
        u[`${p}/status`] = "blocked";
        u[`${p}/blockedAt`] = at;
        u[`retention/${pathToRid(p)}`] = retentionEntry("blocked", p, S.adSenders[k].name || S.adSenders[k].sender, at);
      });
      write(u, `${keys.length}개 차단 등록`);
    }
  });

  // ───────── 보관함 ─────────
  $("#vaultRule").textContent = `숨김·차단 항목은 처리 시점부터 ${RETAIN_DAYS}일 보관 후 승인 없이 자동 삭제됩니다. ` +
    `만료 ${WARN_DAYS}일 전부터 상단 경고 + 경고 메일(1회). 메일 원본의 영구 삭제는 각 메일함 휴지통 규칙을 따릅니다 ` +
    `(Gmail 휴지통 30일, 네이버는 메일 환경설정의 휴지통 비우기 설정).`;
  $("#vaultFilter").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-f]"); if (!b) return;
    S.vaultFilter = b.dataset.f;
    $$("#vaultFilter button").forEach((x) => x.classList.toggle("on", x === b));
    renderVault();
  });

  const KIND_OF_PATH = (p) => p.startsWith("spend/") ? "지출" : p.startsWith("reservations/") ? "예약" : p.startsWith("daily/") ? "메일" : "발신자";

  function renderVault() {
    $$("#vaultFilter button").forEach((x) => x.classList.toggle("on", x.dataset.f === S.vaultFilter));
    const list = Object.entries(S.retention)
      .filter(([, e]) => e && PATH_RE.test(e.path || "") && Number.isFinite(new Date(e.actionAt).getTime()))
      .filter(([, e]) => S.vaultFilter === "all" || e.kind === S.vaultFilter)
      .map(([rid, e]) => ({ rid, ...e, left: daysLeft(e.actionAt) }))
      .sort((a, b) => a.left - b.left);
    $("#vaultList").innerHTML = list.length ? list.map((x) => {
      const warn = x.left <= WARN_DAYS;
      return `<div class="vitem${warn ? " warn" : ""}">
        <div class="vtop"><span class="tag">${x.kind === "blocked" ? "차단" : "숨김"}·${KIND_OF_PATH(x.path)}</span>
          <span class="vleft${warn ? " warn-t" : ""}">${x.left === 0 ? "오늘 삭제" : `D-${x.left}`}</span></div>
        <div class="s">${esc(x.label || x.path)}</div>
        <div class="f">처리 ${esc(fmtKST.format(new Date(x.actionAt)))} · 삭제 예정 ${esc(fmtKST.format(expiresOf(x.actionAt)))}${x.warnedAt ? " · 경고 메일 발송됨" : ""}</div>
        <div class="acts"><button class="btn small" type="button" data-restore="${esc(x.rid)}">${x.kind === "blocked" ? "차단 해제" : "복원"}</button>
          <button class="btn small" type="button" data-extend="${esc(x.rid)}">${RETAIN_DAYS}일 연장</button></div>
      </div>`;
    }).join("") : `<p class="muted small" style="text-align:center">보관 중인 항목 없음</p>`;
  }
  $("#vaultList").addEventListener("click", (e) => {
    const t = e.target.closest("button"); if (!t) return;
    if (t.dataset.restore) restoreEntry(t.dataset.restore);
    else if (t.dataset.extend) extendEntry(t.dataset.extend);
  });

  // 날짜가 바뀌면(자정 경과, 백그라운드 복귀) D-day 재계산
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState !== "visible" || $("#tabbar").hidden) return;
    renderBanner(); renderVault(); renderBlock(); renderRes();
  });
})();
