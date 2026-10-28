(() => {
  "use strict";

  const CFG = window.PA_CONFIG || {};
  const DEFAULT_CATS = ["식비", "장보기", "쇼핑", "교통", "여행/숙박", "구독", "통신", "공구/EDC", "의료", "공과금", "기타"];
  const BLOCK_RULE = { days: 30, min: 3, maxRate: 0.1 }; // collector/rules.json 과 동일하게 유지

  // ───────── helpers ─────────
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const won = (n) => Math.round(Number(n) || 0).toLocaleString("ko-KR");
  const usd = (n) => (Number(n) || 0).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const fmtKST = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Seoul" });
  const todayKST = () => fmtKST.format(new Date());
  const addDays = (iso, n) => { const d = new Date(iso + "T00:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
  const addMonths = (ym, n) => { const [y, m] = ym.split("-").map(Number); const d = new Date(Date.UTC(y, m - 1 + n, 1)); return d.toISOString().slice(0, 7); };
  const WD = ["일", "월", "화", "수", "목", "금", "토"];
  const wd = (iso) => WD[new Date(iso + "T00:00:00Z").getUTCDay()];
  const dayDiff = (a, b) => Math.round((new Date(b + "T00:00:00Z") - new Date(a + "T00:00:00Z")) / 86400000);
  const isHttps = (u) => typeof u === "string" && /^https:\/\//i.test(u);
  const lsGet = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch { return d; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch { /* storage unavailable */ } };

  let toastTimer;
  function toast(msg) {
    const t = $("#toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
  }

  // ───────── state ─────────
  const S = {
    tab: "today",
    day: todayKST(),
    month: todayKST().slice(0, 7),
    resFilter: "upcoming",
    lastRun: null,
    accounts: {},
    daily: {},
    candidates: {},
    reservations: {},
    spend: {},
    spendPrev: {},
    refs: [],
    spendRefs: [],
  };

  // ───────── boot ─────────
  const cfgOk = CFG.firebase && CFG.firebase.apiKey && !String(CFG.firebase.apiKey).includes("REPLACE_ME");
  if (CFG.actionsUrl) $("#runNow").href = CFG.actionsUrl; else $("#runNow").hidden = true;

  if (!cfgOk || typeof firebase === "undefined") {
    $("#runInfo").textContent = "설정 필요";
    const v = $("#loginView");
    v.hidden = false;
    v.innerHTML = `<h2>설정 필요</h2><p class="muted">${typeof firebase === "undefined"
      ? "Firebase SDK 로드 실패 (네트워크 확인)"
      : "config.js 에 Firebase 웹 앱 구성값을 입력하세요."}</p>`;
    return;
  }

  firebase.initializeApp(CFG.firebase);
  const auth = firebase.auth();
  const db = firebase.database();
  auth.setPersistence(firebase.auth.Auth.Persistence.LOCAL).catch(() => {});

  if ("serviceWorker" in navigator) {
    window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
  }

  // ───────── auth ─────────
  const authErr = (e) => {
    const map = {
      "auth/invalid-credential": "이메일 또는 비밀번호가 올바르지 않습니다.",
      "auth/wrong-password": "비밀번호가 올바르지 않습니다.",
      "auth/user-not-found": "등록되지 않은 계정입니다.",
      "auth/too-many-requests": "시도 횟수 초과. 잠시 후 다시 시도하세요.",
      "auth/unauthorized-domain": "Firebase Auth 승인 도메인에 현재 도메인을 추가하세요.",
      "auth/network-request-failed": "네트워크 오류",
    };
    $("#loginErr").textContent = map[e.code] || e.message || String(e);
  };

  $("#googleLogin").addEventListener("click", async () => {
    $("#loginErr").textContent = "";
    const provider = new firebase.auth.GoogleAuthProvider();
    provider.setCustomParameters({ prompt: "select_account" });
    try {
      await auth.signInWithPopup(provider);
    } catch (e) {
      if (["auth/popup-blocked", "auth/operation-not-supported-in-this-environment", "auth/cancelled-popup-request"].includes(e.code)) {
        auth.signInWithRedirect(provider).catch(authErr);
      } else if (e.code !== "auth/popup-closed-by-user") {
        authErr(e);
      }
    }
  });
  auth.getRedirectResult().catch(authErr);

  $("#emailLogin").addEventListener("submit", (ev) => {
    ev.preventDefault();
    $("#loginErr").textContent = "";
    auth.signInWithEmailAndPassword($("#loginEmail").value.trim(), $("#loginPw").value).catch(authErr);
  });

  $("#logoutBtn").addEventListener("click", () => auth.signOut());
  $("#copyUid").addEventListener("click", async () => {
    try { await navigator.clipboard.writeText($("#myUid").textContent); toast("UID 복사됨"); }
    catch { toast("복사 실패 — 직접 선택해 복사하세요"); }
  });

  function showOnly(id) {
    ["#loginView", "#deniedView"].forEach((s) => ($(s).hidden = s !== id));
    $$(".tab").forEach((t) => (t.hidden = true));
    $("#tabbar").hidden = id !== null;
  }

  auth.onAuthStateChanged((user) => {
    detachAll();
    $("#logoutBtn").hidden = !user;
    if (!user) {
      $("#runInfo").textContent = "로그인 필요";
      showOnly("#loginView");
      return;
    }
    db.ref("pa/meta").once("value")
      .then(() => { showOnly(null); attach(); switchTab(initialTab()); })
      .catch((e) => {
        $("#runInfo").textContent = "권한 없음";
        $("#myUid").textContent = user.uid;
        showOnly("#deniedView");
        console.warn(e);
      });
  });

  // ───────── data subscriptions ─────────
  function on(ref, cb) {
    const h = ref.on("value", (snap) => cb(snap.val() || {}), (e) => toast(`읽기 오류: ${e.code || e.message}`));
    S.refs.push(() => ref.off("value", h));
  }

  function detachAll() {
    S.refs.forEach((f) => f()); S.refs = [];
    S.spendRefs.forEach((f) => f()); S.spendRefs = [];
  }

  function attach() {
    on(db.ref("pa/meta/lastRun"), (v) => { S.lastRun = v; renderRun(); renderToday(); });
    on(db.ref("pa/accounts"), (v) => { S.accounts = v; renderToday(); });
    on(db.ref("pa/daily").orderByKey().limitToLast(60), (v) => { S.daily = { ...S.daily, ...v }; renderToday(); });
    on(db.ref("pa/blockCandidates"), (v) => { S.candidates = v; renderBlock(); });
    on(db.ref("pa/reservations"), (v) => { S.reservations = v; renderRes(); renderToday(); });
    attachSpend();
  }

  function attachSpend() {
    S.spendRefs.forEach((f) => f()); S.spendRefs = [];
    S.spend = {}; S.spendPrev = {};
    const cur = db.ref(`pa/spend/${S.month}`);
    const prev = db.ref(`pa/spend/${addMonths(S.month, -1)}`);
    const h1 = cur.on("value", (s) => { S.spend = s.val() || {}; renderSpend(); }, (e) => toast(e.code || e.message));
    const h2 = prev.on("value", (s) => { S.spendPrev = s.val() || {}; renderSpend(); }, () => {});
    S.spendRefs.push(() => cur.off("value", h1), () => prev.off("value", h2));
  }

  async function ensureDay(day) {
    if (S.daily[day] !== undefined) return;
    try {
      const snap = await db.ref(`pa/daily/${day}`).once("value");
      S.daily[day] = snap.val() || null;
    } catch { S.daily[day] = null; }
    renderToday();
  }

  async function write(updates, okMsg) {
    try { await db.ref("pa").update(updates); if (okMsg) toast(okMsg); }
    catch (e) { toast(`저장 실패: ${e.code || e.message}`); }
  }

  // ───────── tabs ─────────
  function initialTab() {
    const h = location.hash.replace("#", "");
    return ["today", "spend", "res", "block"].includes(h) ? h : lsGet("pa.tab", "today");
  }
  function switchTab(tab) {
    S.tab = tab;
    lsSet("pa.tab", tab);
    if (location.hash !== "#" + tab) history.replaceState(null, "", "#" + tab);
    $$(".tab").forEach((t) => (t.hidden = t.id !== `tab-${tab}`));
    $$("#tabbar button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
    window.scrollTo({ top: 0 });
  }
  $$("#tabbar button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
  window.addEventListener("hashchange", () => {
    const h = location.hash.replace("#", "");
    if (!$("#tabbar").hidden && ["today", "spend", "res", "block"].includes(h) && h !== S.tab) switchTab(h);
  });

  // ───────── header / run status ─────────
  function renderRun() {
    const r = S.lastRun;
    const dot = $("#runDot");
    dot.className = "dot";
    if (!r || !r.finishedAt) { $("#runInfo").textContent = "아직 수집 기록 없음"; return; }
    const fin = new Date(r.finishedAt);
    const ageH = (Date.now() - fin.getTime()) / 3.6e6;
    const accs = r.accounts || {};
    const failed = Object.entries(accs).filter(([, v]) => v && v.ok === false).map(([k]) => label(k));
    dot.classList.add(failed.length ? "bad" : ageH > 26 ? "stale" : "ok");
    const t = new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(fin);
    $("#runInfo").textContent = failed.length ? `수집 ${t} · 실패: ${failed.join(", ")}` : `수집 ${t} · ${Object.keys(accs).length}계정 정상${ageH > 26 ? " · 지연" : ""}`;
  }
  const label = (id) => (S.accounts[id] && S.accounts[id].label) || id;

  // ───────── TODAY ─────────
  $("#dayPick").addEventListener("change", (e) => { if (e.target.value) { S.day = e.target.value; ensureDay(S.day); renderToday(); } });
  $$("[data-day]").forEach((b) => b.addEventListener("click", () => { S.day = addDays(S.day, Number(b.dataset.day)); ensureDay(S.day); renderToday(); }));

  function dayTotals(d) {
    const out = { total: 0, unread: 0, bulk: 0, spend: 0, reservations: 0 };
    Object.values(d || {}).forEach((a) => { if (!a) return; for (const k in out) out[k] += Number(a[k]) || 0; });
    return out;
  }

  function renderToday() {
    if (!$("#dayPick")) return;
    $("#dayPick").value = S.day;
    const d = S.daily[S.day] || {};
    const t = dayTotals(d);
    const bulkPct = t.total ? Math.round((t.bulk / t.total) * 100) : 0;
    const isToday = S.day === todayKST();
    $("#dayKpis").innerHTML = [
      kpi("수신", t.total, `일반 ${t.total - t.bulk}${isToday ? " · 집계 중" : ""}`),
      kpi("미열람", t.unread, `${wd(S.day)}요일`),
      kpi("광고", t.bulk, `${bulkPct}%`),
      kpi("지출", `₩${won(t.spend)}`, `예약 ${t.reservations}건`),
    ].join("");

    const ids = Array.from(new Set([...Object.keys(S.accounts), ...Object.keys(d)])).sort();
    const runAcc = (S.lastRun && S.lastRun.accounts) || {};
    $("#dayAccounts").innerHTML = ids.length ? ids.map((id) => {
      const a = d[id] || {};
      const err = runAcc[id] && runAcc[id].ok === false ? `<span class="err-chip" title="${esc(runAcc[id].error)}">수집 실패</span>` : "";
      return `<div class="acc"><div class="t"><span>${esc(label(id))}</span>${err}</div>
        <div class="m"><div>수신<b>${a.total || 0}</b></div><div>미열람<b>${a.unread || 0}</b></div>
        <div>광고<b>${a.bulk || 0}</b></div><div>지출<b>${won(a.spend)}</b></div></div></div>`;
    }).join("") : `<div class="acc muted">계정 데이터 없음</div>`;

    const imp = [];
    for (const [id, a] of Object.entries(d)) (a && a.important || []).forEach((m) => imp.push({ ...m, acc: id }));
    imp.sort((x, y) => (y.time || "").localeCompare(x.time || ""));
    $("#dayImportant").innerHTML = imp.length ? imp.map((m) =>
      `<li><span class="s">${esc(m.subject)}</span><span class="r">${esc(m.time)}</span>
       <span class="f"><span class="tag">${esc(label(m.acc))}</span>${esc(m.from)}</span><span></span></li>`).join("")
      : `<li><span class="empty">미열람 주요 메일 없음</span></li>`;

    const end = addDays(S.day, 7);
    const up = Object.entries(S.reservations)
      .map(([k, v]) => ({ k, ...v }))
      .filter((r) => !r.hidden && r.status !== "canceled" && r.eventDate >= S.day && r.eventDate <= end)
      .sort((a, b) => (a.eventDate + (a.eventTime || "")).localeCompare(b.eventDate + (b.eventTime || "")));
    $("#dayUpcoming").innerHTML = up.length ? up.map((r) => {
      const dd = dayDiff(todayKST(), r.eventDate);
      return `<li><span class="s">${esc(r.title)}</span><span class="r">${dd === 0 ? "오늘" : dd > 0 ? "D-" + dd : "D+" + -dd}</span>
        <span class="f">${esc(r.eventDate.slice(5).replace("-", "/"))}(${wd(r.eventDate)}) ${esc(r.eventTime || "")} · ${esc(r.vendor || "")}${r.place ? " · " + esc(r.place) : ""}</span><span></span></li>`;
    }).join("") : `<li><span class="empty">7일 내 예약 없음</span></li>`;

    const days = Array.from({ length: 14 }, (_, i) => addDays(S.day, i - 13));
    const vals = days.map((x) => dayTotals(S.daily[x]));
    const max = Math.max(1, ...vals.map((v) => v.total));
    $("#dayTrend").innerHTML = days.map((x, i) => {
      const v = vals[i];
      const a = ((v.total - v.bulk) / max) * 100, b = (v.bulk / max) * 100;
      return `<div class="col${x === S.day ? " sel" : ""}" data-go="${x}" title="${x} 수신 ${v.total} / 광고 ${v.bulk}">
        <i class="a" style="height:${a}%"></i><i class="b" style="height:${b}%"></i>
        <span>${i % 2 === 1 || x === S.day ? Number(x.slice(8)) : ""}</span></div>`;
    }).join("");
    $$("#dayTrend [data-go]").forEach((c) => c.addEventListener("click", () => { S.day = c.dataset.go; renderToday(); }));
  }

  function kpi(k, v, d, cls = "") {
    return `<div class="kpi"><div class="k">${esc(k)}</div><div class="v">${esc(v)}</div><div class="d ${cls}">${esc(d)}</div></div>`;
  }

  // ───────── SPEND ─────────
  $("#monthPick").addEventListener("change", (e) => { if (e.target.value) { S.month = e.target.value; attachSpend(); renderSpend(); } });
  $$("[data-month]").forEach((b) => b.addEventListener("click", () => { S.month = addMonths(S.month, Number(b.dataset.month)); attachSpend(); renderSpend(); }));

  const activeKRW = (obj) => Object.entries(obj || {}).map(([key, v]) => ({ key, ...v }))
    .filter((x) => !x.excluded && (x.currency || "KRW") === "KRW");

  function renderSpend() {
    $("#monthPick").value = S.month;
    const all = Object.entries(S.spend).map(([key, v]) => ({ key, ...v }));
    const act = activeKRW(S.spend);
    const total = act.reduce((s, x) => s + (Number(x.amount) || 0), 0);
    const prevTotal = activeKRW(S.spendPrev).reduce((s, x) => s + (Number(x.amount) || 0), 0);
    const usdTotal = all.filter((x) => !x.excluded && x.currency === "USD").reduce((s, x) => s + (Number(x.amount) || 0), 0);
    const curMonth = todayKST().slice(0, 7);
    const [y, m] = S.month.split("-").map(Number);
    const daysIn = new Date(Date.UTC(y, m, 0)).getUTCDate();
    const elapsed = S.month === curMonth ? Number(todayKST().slice(8)) : S.month < curMonth ? daysIn : 0;
    let deltaTxt = "전월 데이터 없음", deltaCls = "";
    if (prevTotal > 0) {
      const p = ((total - prevTotal) / prevTotal) * 100;
      deltaTxt = `${p >= 0 ? "▲" : "▼"} ${Math.abs(p).toFixed(1)}% (전월 ₩${won(prevTotal)})`;
      deltaCls = p >= 0 ? "up" : "down";
    }
    $("#spendKpis").innerHTML = [
      kpi("월 합계", `₩${won(total)}`, deltaTxt, deltaCls),
      kpi("건수", act.length, `제외 ${all.filter((x) => x.excluded).length}건`),
      kpi("일평균", elapsed ? `₩${won(total / elapsed)}` : "-", elapsed ? `${elapsed}일 기준` : "미래 월"),
      kpi("해외(USD)", usdTotal ? `$${usd(usdTotal)}` : "-", "원화 합계 미포함"),
    ].join("");

    const byCat = {};
    act.forEach((x) => { const c = x.category || "기타"; byCat[c] = (byCat[c] || 0) + (Number(x.amount) || 0); });
    const cats = Object.entries(byCat).sort((a, b) => b[1] - a[1]);
    const maxCat = Math.max(1, ...cats.map((c) => c[1]));
    $("#spendCats").innerHTML = cats.length ? cats.map(([c, v]) =>
      `<div class="bar"><span class="n">${esc(c)}</span><span class="track"><span class="fill" style="width:${Math.max(0, (v / maxCat) * 100)}%"></span></span>
       <span class="v">₩${won(v)}<small>${total > 0 ? Math.round((v / total) * 100) : 0}%</small></span></div>`).join("")
      : `<p class="muted small" style="padding:10px 0;margin:0">이 달 지출 데이터 없음</p>`;

    const dupKey = (x) => `${x.date}|${Math.abs(Number(x.amount) || 0)}`;
    const dupCount = {};
    act.forEach((x) => (dupCount[dupKey(x)] = (dupCount[dupKey(x)] || 0) + 1));
    const byDate = {};
    all.forEach((x) => (byDate[x.date] = byDate[x.date] || []).push(x));
    const dates = Object.keys(byDate).sort().reverse();
    $("#spendList").innerHTML = dates.map((dte) => {
      const items = byDate[dte].sort((a, b) => (b.time || "").localeCompare(a.time || ""));
      const sum = items.filter((x) => !x.excluded && (x.currency || "KRW") === "KRW").reduce((s, x) => s + (Number(x.amount) || 0), 0);
      return `<div class="dgroup"><div class="dhead"><span>${dte.slice(5).replace("-", "/")} (${wd(dte)})</span><span class="num">₩${won(sum)}</span></div>
        ${items.map((x) => {
          const amt = Number(x.amount) || 0;
          const cur = x.currency === "USD" ? `$${usd(amt)}` : `₩${won(amt)}`;
          const dup = !x.excluded && dupCount[dupKey(x)] > 1 ? `<span class="chip dup">중복 의심</span>` : "";
          const man = x.manual ? `<span class="chip manual">수정</span>` : "";
          return `<div class="item${x.excluded ? " excluded" : ""}" data-k="${esc(x.key)}">
            <span class="s">${esc(x.merchant || x.from || "-")}${dup}${man}</span><span class="amt num${amt < 0 ? " neg" : ""}">${cur}</span>
            <span class="f"><span class="tag">${esc(x.category || "기타")}</span>${esc(x.time || "")} · ${esc(label(x.account))} · ${esc(x.subject || "")}</span><span></span></div>`;
        }).join("")}</div>`;
    }).join("");
    $$("#spendList .item").forEach((el) => el.addEventListener("click", () => openCat(el.dataset.k)));
  }

  let editKey = null;
  function openCat(key) {
    const x = S.spend[key];
    if (!x) return;
    editKey = key;
    const cats = Array.from(new Set([...DEFAULT_CATS, ...Object.values(S.spend).map((v) => v.category).filter(Boolean)]));
    $("#catTitle").textContent = `${x.merchant || x.from} · ${x.currency === "USD" ? "$" + usd(x.amount) : "₩" + won(x.amount)}`;
    $("#catSelect").innerHTML = cats.map((c) => `<option ${c === (x.category || "기타") ? "selected" : ""}>${esc(c)}</option>`).join("");
    $("#catExclude").checked = !!x.excluded;
    const dlg = $("#catDialog");
    if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", "");
  }
  $("#catDialog").addEventListener("close", () => {
    const dlg = $("#catDialog");
    if (dlg.returnValue !== "ok" || !editKey) return;
    const base = `spend/${S.month}/${editKey}`;
    write({ [`${base}/category`]: $("#catSelect").value, [`${base}/manual`]: true, [`${base}/excluded`]: $("#catExclude").checked }, "저장됨");
    editKey = null;
  });

  // ───────── RESERVATIONS ─────────
  $$("#resFilter button").forEach((b) => b.addEventListener("click", () => {
    S.resFilter = b.dataset.f;
    $$("#resFilter button").forEach((x) => x.classList.toggle("on", x === b));
    renderRes();
  }));

  function renderRes() {
    const today = todayKST();
    let list = Object.entries(S.reservations).map(([k, v]) => ({ k, ...v })).filter((r) => !r.hidden && r.eventDate);
    if (S.resFilter === "upcoming") list = list.filter((r) => r.eventDate >= today);
    if (S.resFilter === "past") list = list.filter((r) => r.eventDate < today);
    const keyOf = (r) => r.eventDate + (r.eventTime || "99:99");
    list.sort((a, b) => S.resFilter === "past" ? keyOf(b).localeCompare(keyOf(a)) : keyOf(a).localeCompare(keyOf(b)));
    const groups = {};
    list.forEach((r) => (groups[r.eventDate] = groups[r.eventDate] || []).push(r));
    const ks = Object.keys(groups);
    $("#resList").innerHTML = ks.length ? ks.map((d) => {
      const dd = dayDiff(today, d);
      const ddTxt = dd === 0 ? "오늘" : dd > 0 ? `D-${dd}` : `D+${-dd}`;
      return `<div class="rday"><div class="rdate"><div class="dd">${Number(d.slice(8))}</div>
        <div class="mm">${Number(d.slice(5, 7))}월 ${wd(d)}</div><span class="dday${dd === 0 ? " today" : ""}">${ddTxt}</span></div>
        <div>${groups[d].map((r) => `<div class="ritem${r.status === "canceled" ? " canceled" : ""}">
          <div class="s">${r.eventTime ? `<span class="num">${esc(r.eventTime)}</span> ` : ""}${esc(r.title)}</div>
          <div class="f">${esc(r.vendor || "")}${r.place ? " · " + esc(r.place) : ""} · ${esc(label(r.account))}${r.status === "canceled" ? " · 취소됨" : ""}</div>
          <div class="acts"><button class="linkish" data-hide="${esc(r.k)}">숨기기</button></div></div>`).join("")}</div></div>`;
    }).join("") : `<p class="muted" style="text-align:center;padding:30px 0">${S.resFilter === "past" ? "지난 예약 없음" : "예정된 예약 없음"}</p>`;
    $$("#resList [data-hide]").forEach((b) => b.addEventListener("click", () =>
      write({ [`reservations/${b.dataset.hide}/hidden`]: true }, "숨김 처리")));
  }

  // ───────── BLOCK ─────────
  $("#blockRule").textContent = `최근 ${BLOCK_RULE.days}일 광고성 메일 ${BLOCK_RULE.min}통 이상 · 열람률 ${BLOCK_RULE.maxRate * 100}% 이하 발신자. 승인하면 다음 수집 때 수신거부(원클릭 지원 시 자동) + 기존 메일 휴지통 이동, 이후 신규 메일도 자동 이동합니다.`;

  function methodBadge(c) {
    if (c.oneClick) return `<span class="method auto">원클릭 수신거부</span>`;
    if (c.unsubHttp || c.unsubMailto) return `<span class="method manual">수동 수신거부</span>`;
    return `<span class="method none">휴지통 이동만</span>`;
  }

  function renderBlock() {
    const all = Object.entries(S.candidates).map(([k, v]) => ({ k, ...v }));
    const pending = all.filter((c) => c.status === "pending" || c.status === "approved")
      .sort((a, b) => (a.status === b.status ? (b.total || 0) - (a.total || 0) : a.status === "pending" ? -1 : 1));
    const done = all.filter((c) => c.status === "done").sort((a, b) => ((b.result && b.result.at) || "").localeCompare((a.result && a.result.at) || ""));
    const nPending = all.filter((c) => c.status === "pending").length;
    $("#blockCount").textContent = nPending;
    $("#blockBadge").hidden = !nPending;
    $("#blockBadge").textContent = nPending;
    $("#approveAll").hidden = nPending < 2;

    $("#blockPending").innerHTML = pending.length ? pending.map((c) => {
      const rate = Math.round((c.rate || 0) * 100);
      const acts = c.status === "approved"
        ? `<span class="state">승인됨 · 다음 수집 시 처리</span><button class="btn small" data-act="undo" data-k="${esc(c.k)}">승인 취소</button>`
        : `<button class="btn" data-act="keep" data-k="${esc(c.k)}">유지</button><button class="btn primary" data-act="approve" data-k="${esc(c.k)}">차단 승인</button>`;
      return `<div class="bcard"><div class="top"><div style="min-width:0"><div class="nm">${esc(c.name || c.sender)}</div>
        <div class="em">${esc(c.sender)} · ${esc(label(c.account))}</div></div>${methodBadge(c)}</div>
        <div class="meter"><i style="width:${rate}%"></i></div>
        <div class="stats"><span class="num">${BLOCK_RULE.days}일 ${c.total || 0}통</span><span class="num">열람 ${c.read || 0}통 (${rate}%)</span></div>
        <div class="ls">최근: ${esc(c.lastSubject || "")}</div><div class="acts">${acts}</div></div>`;
    }).join("") : `<p class="muted small" style="text-align:center;padding:18px 0">차단 요청 없음</p>`;

    $("#blockDone").innerHTML = done.length ? done.map((c) => {
      const r = c.result || {};
      let how = "휴지통 자동 이동";
      let link = "";
      if (r.method === "oneclick") how = r.ok ? "원클릭 수신거부 완료" : `원클릭 실패(${esc(r.code)}) · 휴지통 자동 이동`;
      if (r.method === "manual_link" && isHttps(c.unsubHttp)) link = `<a class="btn small" href="${esc(c.unsubHttp)}" target="_blank" rel="noopener noreferrer">수신거부 페이지 열기</a>`;
      if (r.method === "manual_mailto" && c.unsubMailto) link = `<a class="btn small" href="${esc(c.unsubMailto)}">수신거부 메일 작성</a>`;
      return `<div class="bcard"><div class="top"><div style="min-width:0"><div class="nm">${esc(c.name || c.sender)}</div>
        <div class="em">${esc(c.sender)} · ${esc(label(c.account))}</div></div><span class="method none">${esc((r.at || "").slice(5, 10).replace("-", "/"))}</span></div>
        <div class="stats"><span>${how}</span><span class="num">기존 ${r.trashed || 0}통 이동</span></div>
        <div class="acts">${link || "<span></span>"}<button class="btn small" data-act="unblock" data-k="${esc(c.k)}">차단 해제</button></div></div>`;
    }).join("") : `<p class="muted small" style="text-align:center;padding:18px 0">처리 이력 없음</p>`;

    $$("#tab-block [data-act]").forEach((b) => b.addEventListener("click", () => blockAction(b.dataset.act, b.dataset.k)));
  }

  function blockAction(act, k) {
    const c = S.candidates[k];
    if (!c) return;
    const now = new Date().toISOString();
    if (act === "approve") write({ [`blockCandidates/${k}/status`]: "approved", [`blockCandidates/${k}/approvedAt`]: now }, "승인 — 다음 수집 때 처리");
    if (act === "undo") write({ [`blockCandidates/${k}/status`]: "pending" }, "승인 취소");
    if (act === "keep") write({
      [`blockCandidates/${k}/status`]: "rejected",
      [`allowlist/${k}`]: { sender: c.sender, account: c.account, at: now },
    }, "유지 — 다시 제안하지 않음");
    if (act === "unblock") {
      if (!confirm(`${c.sender} 차단을 해제할까요?\n(이미 수신거부된 구독은 복구되지 않습니다)`)) return;
      write({
        [`blocklist/${k}`]: null,
        [`blockCandidates/${k}/status`]: "rejected",
        [`allowlist/${k}`]: { sender: c.sender, account: c.account, at: now },
      }, "차단 해제");
    }
  }

  $("#approveAll").addEventListener("click", () => {
    const ks = Object.entries(S.candidates).filter(([, v]) => v.status === "pending").map(([k]) => k);
    if (!ks.length || !confirm(`대기 중 ${ks.length}개 발신자를 모두 차단 승인할까요?`)) return;
    const now = new Date().toISOString();
    const up = {};
    ks.forEach((k) => { up[`blockCandidates/${k}/status`] = "approved"; up[`blockCandidates/${k}/approvedAt`] = now; });
    write(up, `${ks.length}건 승인`);
  });
})();
