"""The in-page site probe (JavaScript) — kept in its own module so the analyzer in
``siteprobe.py`` stays readable. ``SITE_PROBE_JS`` is ONE self-contained expression: run it
with the agent-browser plugin's ``browser_eval`` on a page opened with ``browser_open``. It
returns a compact JSON **string** (``JSON.stringify``), hard-capped at ``__CAP__`` characters,
describing the rendered page for two jobs:

* style usage — computed colors / type / radii / spacing / shadows / z-index / transitions,
  each with counts, rendered-area weight and 1-3 example selectors; text-vs-background color
  pairs (for contrast); ``:root`` custom properties (and whether ``--<prefix>-*`` are present);
  the DS's class usage (``.<prefix>-*``);
* structure — landmarks, forms, and REPEATED UI PATTERNS: visible elements clustered by a
  structural signature (tag + role + normalized class stems + child shape), each with a count,
  an inferred kind, a trimmed representative ``outerHTML``, size stats, observed states,
  variant features and the accessible role/name.

It walks at most ``MAX_NODES`` visible elements within ``MAX_MS`` and never throws: an error
inside a section lands in ``notes`` and the rest still returns. ``__DS_PREFIX__`` is replaced
by the DS's class/var prefix (``pl``) before use — see ``siteprobe.probe_script``.
"""

SITE_PROBE_SOURCE = r"""(() => {
const T0 = performance.now(), MAX_NODES = 5000, MAX_MS = 3500, CAP = +"__CAP__", PFX = "__DS_PREFIX__";
const out = { probe: "ds-site-probe", v: 1, url: String(location.href).slice(0, 2000), title: String(document.title || "").slice(0, 120),
  viewport: { w: innerWidth || 1280, h: innerHeight || 800 }, readyState: document.readyState, colorScheme: "",
  walked: 0, visible: 0, ms: 0, truncated: [], notes: [] };
const root = document.documentElement, body = document.body, SIDES = ["Top", "Right", "Bottom", "Left"];
const attr = (e, a) => e.getAttribute(a), qs = (e, q) => e.querySelector(q), low = (x) => String(x || "").toLowerCase(), R = Math.round;
if (!body) { out.notes.push("no <body>"); return JSON.stringify(out); }
const vw = out.viewport.w, vh = out.viewport.h;
out.docHeight = R(Math.max(root.scrollHeight || 0, body.scrollHeight || 0, vh));
const MAX_Y = vh * 12;
try { out.colorScheme = getComputedStyle(root).colorScheme || ""; } catch (e) {}
const clear = (v) => !v || v === "transparent" || /rgba\([^)]*,\s*0\)$/.test(v) || /\/\s*0\)$/.test(v);
const alphaOf = (v) => { let m = /rgba\([^,]+,[^,]+,[^,]+,\s*([\d.]+)\)/.exec(v) || /\/\s*([\d.]+%?)\s*\)$/.exec(v);
  if (!m) return 1; const a = m[1].endsWith("%") ? parseFloat(m[1]) / 100 : parseFloat(m[1]); return isNaN(a) ? 1 : a; };
const kebab = (s) => s.replace(/([a-z0-9])([A-Z])/g, "$1-$2").toLowerCase();
const clsOf = (el) => { const c = el.getAttribute && attr(el, "class"); return c ? c.trim().split(/\s+/).slice(0, 40) : []; };
const selMemo = new WeakMap();
const part = (e) => { let s = low(e.tagName);
  if (e.id && e.id.length < 32 && !/\d{3,}|[:.]/.test(e.id)) return s + "#" + e.id;
  const c = clsOf(e).filter((x) => !/[:\[\]\/!@%()]/.test(x)).slice(0, 2).map((x) => x.slice(0, 40));
  return c.length ? s + "." + c.join(".") : s; };
const selOf = (el) => { let s = selMemo.get(el); if (s) return s; const p = el.parentElement;
  s = (p && p !== body && p !== root ? part(p) + " > " : "") + part(el); if (s.length > 72) s = s.slice(0, 71) + "…"; selMemo.set(el, s); return s; };
const bump = (map, key, w, el, extra, nex) => { let r = map.get(key);
  if (!r) { r = { v: key, n: 0, area: 0, ex: [] }; if (extra) Object.assign(r, extra); map.set(key, r); }
  r.n++; r.area += w; if (el && r.ex.length < (nex || 1)) { const s = selOf(el); if (!r.ex.includes(s)) r.ex.push(s); } return r; };
const colors = new Map(), fam = new Map(), sizes = new Map(), weights = new Map(), lhs = new Map(), radii = new Map(),
  space = new Map(), shadows = new Map(), zs = new Map(), trans = new Map(), pairs = new Map(), dsCls = new Map();
const addColor = (v, prop, w, el) => { if (clear(v)) return; const r = bump(colors, v, w, el, { props: {} }, 2); r.props[prop] = (r.props[prop] || 0) + 1; };
const bgMemo = new WeakMap();
const layers = (el) => { if (!el || el.nodeType !== 1) return ["canvas"]; if (bgMemo.has(el)) return bgMemo.get(el);
  let res; try { const s = getComputedStyle(el);
    if (s.backgroundImage && s.backgroundImage !== "none" && !/^url\(["']?data:image\/svg/.test(s.backgroundImage)) res = ["image"];
    else if (!clear(s.backgroundColor)) res = alphaOf(s.backgroundColor) >= 0.999 ? [s.backgroundColor] : [s.backgroundColor].concat(layers(el.parentElement)).slice(0, 4);
    else res = layers(el.parentElement);
  } catch (e) { res = ["canvas"]; }
  bgMemo.set(el, res); return res; };
// Compact tables (the script travels on a command line; see SITE_PROBE_JS below).
// "a=b" maps a → b; a bare "a" maps a → a.
const table = (spec) => { const o = {}; for (const e of spec.split(" ")) { const [k, v] = e.split("="); o[k] = v || k; } return o; };
const ROLEK = table("button link tab tablist=tabs dialog=modal alertdialog=modal alert status=toast tooltip menu menubar=menu menuitem=menu-item " +
  "menuitemcheckbox=menu-item menuitemradio=menu-item switch=toggle checkbox radio progressbar=progress slider combobox=select listbox=select " +
  "option row=table-row table grid=table searchbox=input textbox=input navigation=nav banner=header contentinfo=footer separator=divider " +
  "tabpanel=tab-panel listitem=list-row article=card search heading img=image");
const TAG_KIND = table("TD=table-cell TH=table-cell THEAD=table-part TBODY=table-part SELECT=select TEXTAREA=textarea DIALOG=modal " +
  "DETAILS=accordion SUMMARY=accordion TABLE=table TR=table-row PROGRESS=progress METER=progress HR=divider NAV=nav FORM=form KBD=kbd " +
  "PRE=code-block LABEL=label H1=heading H2=heading H3=heading H4=heading H5=heading H6=heading BLOCKQUOTE=quote FIGURE=figure");
// kind=alternatives (regex source), in priority order.
const HINTS = ("carousel=carousel|marquee|swiper|slick-slider|splide|embla|slideshow toast=toast|snackbar|notification tooltip=tooltip|popover-tip " +
  "modal=modal|dialog|lightbox breadcrumb=breadcrumbs? pagination=pagination|pager|paginator avatar badge=badge|chip|pill|tag|label-pill|lozenge " +
  "tab=tabs?|tab-item|tab-button|segmented accordion=accordion|collapsible|disclosure|faq-item toggle=switch|toggle hero=hero|jumbotron|masthead|splash " +
  "card=card|tile alert=alert|callout|notice|banner|announcement menu=dropdown|menu|popover nav-item=nav-?item|nav-?link|menu-?item button=btn|button|cta " +
  "skeleton=skeleton|shimmer spinner=spinner|loader|loading steps=stepper|steps? stat=stat|metric|kpi kbd=kbd|keycap|shortcut divider=divider|separator|rule " +
  "input=input|text-?field|search-?box select=select|combobox").split(" ").map((e) => e.split("="));
const HINT_RE = HINTS.map(([k, p]) => [k, new RegExp("(^|[\\s_-])(" + (p || k) + ")(?=[\\s_-]|$)")]);
const STATE_CLS = /^(is-|has-)?(active|selected|current|open|opened|closed|disabled|expanded|collapsed|checked|focus|focused|hover|visible|hidden|show|shown|loading|error|invalid|valid)$/;
const UTIL_CLS = /^-?([mp][trblxyse]?|[wh]|min|max|size|gap|space|text|bg|border|rounded|shadow|flex|grid|col|row|items|justify|self|content|place|font|leading|tracking|opacity|z|top|left|right|bottom|inset|overflow|cursor|transition|duration|ease|delay|ring|outline|fill|stroke|order|basis|grow|shrink|block|inline|absolute|relative|fixed|sticky|static|sr|truncate|underline|uppercase|lowercase|capitalize|italic|antialiased|container|animate|aspect|object|pointer|select|whitespace|break|list|decoration|divide|backdrop|blur|filter|transform|translate|rotate|scale|origin|visible|invisible|table|float|clear|isolate|from|via|to|hidden|group|peer|prose|line|dark|sm|md|lg|x?l|2xl)(-|$)/;
const stemOf = (c) => {
  if (new RegExp("^" + PFX + "-[a-z]").test(c)) return c.toLowerCase().replace(/--.*$/, "");
  if (/[:\[\]\/!@%()#.]/.test(c) || UTIL_CLS.test(c)) return "";
  if (/^(sc-|css-|jsx-|svelte-|emotion-|chakra-|mui|ant-|tw-|framer-|astro-)/i.test(c)) return "";
  let s = c.replace(/(__|--|_|-)[A-Za-z0-9]{5,}$/, (m) => /\d/.test(m) ? "" : m);
  s = s.replace(/^_?(?:[A-Za-z0-9]{1,8}_)+(?=[A-Za-z])/, (m) => (/\d/.test(m) || /.[A-Z]/.test(m.replace(/^_/, ""))) ? "" : m);
  if (/\d/.test(s) && s.length >= 5 && !/[-_]/.test(s)) return "";
  if (/^[a-zA-Z]{5,8}$/.test(s) && /[a-z][A-Z]/.test(s) && /[A-Z].*[A-Z]/.test(s)) return "";
  s = kebab(s).replace(/--[a-z0-9-]+$/, "").replace(/\d+/g, "");
  if (s.length < 2 || STATE_CLS.test(s)) return "";
  return s.slice(0, 32); };
const modsOf = (cls) => { const m = []; for (const c of cls) { const k = kebab(c);
  const b = /--([a-z][a-z0-9-]*)$/.exec(k); if (b) m.push(b[1]); else if (STATE_CLS.test(k)) m.push(k); } return m; };
const textOf = (el) => { let t = ""; for (const c of el.childNodes) if (c.nodeType === 3) t += c.textContent; return t.replace(/\s+/g, " ").trim(); };
const nameOf = (el) => { const al = attr(el, "aria-label"); if (al) return al.trim();
  const lb = attr(el, "aria-labelledby"); if (lb) { const t = lb.split(/\s+/).map((i) => { const x = document.getElementById(i); return x ? x.textContent : ""; }).join(" ").trim(); if (t) return t; }
  if (el.labels && el.labels.length) return el.labels[0].textContent.replace(/\s+/g, " ").trim();
  const alt = attr(el, "alt"); if (alt) return alt;
  let t = (el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
  if (!t) { const im = el.querySelector && qs(el, "img[alt],svg[aria-label],[aria-label]"); if (im) t = attr(im, "alt") || attr(im, "aria-label") || ""; }
  return (t || attr(el, "title") || attr(el, "placeholder") || attr(el, "value") || "").slice(0, 60); };
const INTER = { A: 1, BUTTON: 1, INPUT: 1, SELECT: 1, TEXTAREA: 1, SUMMARY: 1, DETAILS: 1, DIALOG: 1 };
const INTERACTIVE_KIND = /^(button|icon-button|link-button|link|nav-item|tab|toggle|checkbox|radio|input|select|textarea|slider|menu-item|accordion|pagination-item|clickable)$/;
const STRUCT = /^(LI|TR|ARTICLE|FIGURE|TABLE|FORM|NAV|HEADER|FOOTER|ASIDE|UL|OL|PROGRESS|METER|HR|KBD|PRE|H1|H2|H3|H4|H5|H6|LABEL|BLOCKQUOTE|IMG)$/;
const ATTRS = /^(id|class|role|type|name|href|alt|placeholder|title|for|disabled|checked|value|tabindex|aria-[a-z]+|data-(state|variant|size|tone|kind|color))$/;
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const snip = (el, cap) => { const ser = (e, d) => {
    if (e.nodeType === 3) { const t = e.textContent.replace(/\s+/g, " ").trim(); return t ? esc(t.length > 40 ? t.slice(0, 37) + "…" : t) : ""; }
    if (e.nodeType !== 1) return ""; const tag = low(e.tagName);
    if (/^(script|style|noscript|template|link|meta)$/.test(tag)) return "";
    let a = ""; for (const at of Array.from(e.attributes)) { if (!ATTRS.test(at.name)) continue; let v = at.value;
      if (at.name === "href") v = v.replace(/^https?:\/\/[^/]+/, "").slice(0, 40);
      if (at.name === "class") v = v.trim().split(/\s+/).slice(0, 3).map((x) => x.length > 30 ? x.slice(0, 29) + "…" : x).join(" ");
      if (v.length > 60) v = v.slice(0, 57) + "…"; a += " " + at.name + (v === "" ? "" : '="' + v.replace(/"/g, "&quot;") + '"'); }
    if (tag === "svg") { const lab = attr(e, "aria-label"); return lab ? '<svg aria-label="' + lab.slice(0, 30).replace(/"/g, "&quot;") + '">…</svg>' : "<svg>…</svg>"; }
    if (/^(img|input|br|hr|source)$/.test(tag)) return "<" + tag + a + ">";
    if (d >= 3) return "<" + tag + a + ">…</" + tag + ">";
    let inner = "", k = 0; for (const c of Array.from(e.childNodes)) { if (k >= 5) { inner += "…"; break; } const s = ser(c, d + 1); if (s) { inner += s; k++; } }
    return "<" + tag + a + ">" + inner + "</" + tag + ">"; };
  let s = ""; try { s = ser(el, 0); } catch (e) {} return s.length > cap ? s.slice(0, cap - 1) + "…" : s; };
const shapeOf = (el) => { const kids = []; for (const c of Array.from(el.children).slice(0, 24)) {
    const t = low(c.tagName); if (/^(script|style|template|noscript|link|meta)$/.test(t)) continue;
    const k = c instanceof SVGElement ? "svg" : t; if (kids[kids.length - 1] !== k && kids[kids.length - 1] !== k + "+") kids.push(k); else kids[kids.length - 1] = k + "+"; }
  return kids.slice(0, 6).join(",") + (textOf(el) ? (kids.length ? ",#t" : "#t") : ""); };
const hintText = (cls) => cls.filter((x) => !/[:\[\]\/!@%()]/.test(x) && !(UTIL_CLS.test(x) && x.includes("-")))
  .map((x) => { const i = x.lastIndexOf("__"); return kebab(i >= 0 ? x.slice(i + 2) : x); }).join(" ");
const switchy = (el) => attr(el, "role") === "switch" || el.hasAttribute("aria-checked") || !!(el.querySelector && qs(el, "input[type=checkbox]"));
const boxy = (e) => { if (!e || e.nodeType !== 1) return false; const s = getComputedStyle(e);
  return (!clear(s.backgroundColor) || (parseFloat(s.borderTopWidth) || 0) > 0) && (parseFloat(s.paddingLeft) || 0) >= 8; };
const nextPrev = (el) => { const p = el.parentElement && (el.parentElement.parentElement || el.parentElement);
  return !!(p && qs(p, 'button[aria-label*="next" i],button[aria-label*="prev" i],[class*="arrow" i] button,button[class*="next" i]')); };
const kindOf = (el, cs, r, cls, txt, box) => {
  const tag = el.tagName, role = low(attr(el, "role")), c = hintText(cls);
  const iconOnly = !txt && !(el.innerText || "").trim() && !!qs(el, "svg,img,i,[class*=icon]");
  const inNav = !!el.closest("nav,[role=navigation]") && !el.closest("footer,[role=contentinfo]"); const lbl = low(attr(el, "aria-label"));
  if (/breadcrumb/.test(lbl)) return "breadcrumb"; if (/pagination|pager/.test(lbl)) return "pagination";
  if (role === "region" && /carousel|slides/.test((attr(el, "aria-roledescription") || "") + " " + lbl) || HINT_RE[0][1].test(c)) return "carousel";
  if (role && ROLEK[role]) { const k = ROLEK[role]; if (k === "button" && iconOnly) return "icon-button"; if (k === "link" && inNav) return "nav-item"; return k; }
  // A horizontally overflowing track (scrollable, or clipped with prev/next arrows) is a carousel, whatever its tag.
  if (el.children.length >= 3 && el.scrollWidth > el.clientWidth + 16 && (/(auto|scroll)/.test(cs.overflowX) || (/hidden|clip/.test(cs.overflowX) && nextPrev(el)))) return "carousel";
  if (tag === "BUTTON" || (tag === "INPUT" && /^(submit|button|reset)$/i.test(el.type))) {
    if (/(^|[\s_-])(tab|tabs|segmented)(?=[\s_-]|$)/.test(c)) return "tab"; if (/(^|[\s_-])(switch|toggle)(?=[\s_-]|$)/.test(c) && switchy(el)) return "toggle";
    if (el.hasAttribute("aria-expanded") && el.closest("[class*=accordion],[class*=faq],details")) return "accordion";
    if (/(^|[\s_-])(nav-?item|nav-?link|menu-?item)(?=[\s_-]|$)/.test(c)) return "nav-item";
    return iconOnly ? "icon-button" : "button"; }
  if (tag === "A" && el.closest("[class*=breadcrumb],[aria-label*=readcrumb]")) return "breadcrumb-item";
  if (tag === "A" && el.closest("[class*=pagination],[aria-label*=agination],[class*=pager]")) return "pagination-item";
  if (tag === "A") { const only = el.children.length === 1 ? el.children[0] : null;
    const btnish = r.height <= 64 && ((box && parseFloat(cs.paddingLeft) >= 8) || (only && !/^(IMG|PICTURE|SVG|svg)$/.test(only.tagName) && boxy(only) && (el.innerText || "").trim().length <= 40));
    if (/(^|[\s_-])(btn|button|cta)(?=[\s_-]|$)/.test(c) || btnish) return "link-button";
    if (/(^|[\s_-])(tab|tabs)(?=[\s_-]|$)/.test(c)) return "tab";
    if (r.width > 160 && r.height > 110 && qs(el, "img,h2,h3,h4,picture")) return "card";
    if (/(^|[\s_-])(badge|chip|pill|tag)(?=[\s_-]|$)/.test(c)) return "badge";
    return inNav ? "nav-item" : "link"; }
  if (tag === "INPUT") { const t = low(el.type || "text");
    if (t === "checkbox") return /switch|toggle/.test(c) ? "toggle" : "checkbox"; if (t === "radio") return "radio";
    if (t === "range") return "slider"; if (t === "file") return "file-input"; return t === "search" ? "search" : "input"; }
  const TAGK = TAG_KIND;
  if (tag === "NAV" && /breadcrumb/.test(c)) return "breadcrumb"; if ((tag === "NAV" || tag === "UL") && /pagination|pager/.test(c)) return "pagination";
  if (TAGK[tag]) return TAGK[tag];
  if (tag === "HEADER") return el.parentElement === body || r.top + scrollY < 10 ? "header" : "card-header";
  if (tag === "FOOTER") return r.width >= vw * 0.6 && r.top + scrollY + r.height >= out.docHeight * 0.6 && !el.closest("article,[class*=card]") ? "footer" : "card-footer";
  if (tag === "IMG") { const rad = parseFloat(cs.borderTopLeftRadius) || 0; return (Math.abs(r.width - r.height) < 3 && r.width <= 96 && rad >= r.width * 0.3) || /avatar/.test(c) ? "avatar" : "image"; }
  if (tag === "UL" || tag === "OL") return /breadcrumb/.test(c) ? "breadcrumb" : /pagination|pager/.test(c) ? "pagination" : "list";
  if (tag === "LI") { if (/breadcrumb/.test(c) || el.closest("[class*=breadcrumb],[aria-label*=readcrumb]")) return "breadcrumb-item";
    if (el.closest("[class*=pagination],[aria-label*=agination]")) return "pagination-item"; return inNav ? "nav-item" : "list-row"; }
  for (const [k, re] of HINT_RE) if (re.test(c)) { if (k === "button" && !box) continue;
    if (k === "toggle" && !switchy(el)) continue;
    if (k === "hero") { if ((heroEl && heroEl.contains(el)) || tag === "MAIN" || tag === "BODY" || r.height > vh * 1.6) continue; heroEl = el; } return k; }
  if (tag === "ARTICLE") return "card";
  const fixed = cs.position === "fixed" || cs.position === "sticky";
  if (fixed && r.width > vw * 0.4 && r.height > vh * 0.4 && box) return "modal";
  if (fixed && r.width < 480 && r.height < 200 && box && r.top > vh * 0.5) return "toast";
  const shortText = txt || (el.children.length <= 2 ? String(el.textContent || "").replace(/\s+/g, " ").trim() : "");
  if (box && shortText && shortText.length <= 24 && r.height <= 34 && r.width <= 220 && (parseFloat(cs.borderTopLeftRadius) || 0) > 0 && el.children.length <= 2) return "badge";
  if (Math.abs(r.width - r.height) < 3 && r.width <= 64 && (parseFloat(cs.borderTopLeftRadius) || 0) >= r.width * 0.4 && (qs(el, "img") || (txt && txt.length <= 3))) return "avatar";
  if (r.top + scrollY < vh && r.width >= vw * 0.75 && r.height >= vh * 0.35 && r.height <= vh * 1.6 && tag !== "MAIN" && qs(el, "h1")) {
    if (heroEl && heroEl.contains(el)) return box ? "container" : "generic"; heroEl = el; return "hero"; }
  if (cs.display.includes("flex") && cs.flexDirection.startsWith("row") && el.children.length >= 2 && r.height <= 160) {
    const f = el.children[0]; if (f && (f.tagName === "IMG" || f instanceof SVGElement || /avatar|icon|thumb/.test(String(f.className)))) return "media-object"; }
  if (box && r.width >= 120 && r.width <= 720 && r.height >= 70 && r.height <= 900 && el.children.length >= 2 && qs(el, "h2,h3,h4,h5,img,picture,p")) return "card";
  if (cs.cursor === "pointer" && !(el.parentElement && getComputedStyle(el.parentElement).cursor === "pointer") && !el.closest("a,button,label,summary,[role]")) return "clickable";
  return box ? "container" : "generic"; };
let heroEl = null; const clusters = new Map(); const landmarks = [];
const pushNum = (arr, v) => { if (arr.length < 60) arr.push(v); };
const topSet = (o, k) => { if (k === undefined || k === null || k === "") return; o[k] = (o[k] || 0) + 1; };
const visit = (el, cs, r, top) => {
  const tag = el.tagName, cls = clsOf(el), txt = textOf(el), fold = top < vh ? 1 : 0;
  const area = Math.min(r.width, vw) * Math.min(r.height, vh);
  const bg = cs.backgroundColor, hasBg = !clear(bg);
  let hasBorder = false; const perim = (r.width + r.height) * 2;
  if (hasBg) addColor(bg, "background", area, el);
  const seenB = {}; for (const sd of SIDES) { const w = parseFloat(cs["border" + sd + "Width"]) || 0, col = cs["border" + sd + "Color"];
    if (w > 0 && cs["border" + sd + "Style"] !== "none" && !clear(col)) { hasBorder = true; if (!seenB[col]) { seenB[col] = 1; addColor(col, "border", perim / 4, el); } } }
  if (cs.outlineStyle !== "none" && (parseFloat(cs.outlineWidth) || 0) > 0) addColor(cs.outlineColor, "outline", perim, el);
  if (el instanceof SVGElement) { const shape = el.querySelector && qs(el, "path,circle,rect,polygon,line,use"); const s2 = shape ? getComputedStyle(shape) : cs;
    for (const [p, v] of [["fill", s2.fill], ["stroke", s2.stroke]]) if (v && v !== "none" && !/^url/.test(v)) addColor(v, p, area, el); return null; }
  if (txt) { const fs = parseFloat(cs.fontSize) || 16, fw = parseInt(cs.fontWeight) || 400, ta = Math.min(area, txt.length * fs * fs * 0.55);
    addColor(cs.color, "color", ta, el);
    bump(fam, cs.fontFamily.slice(0, 64), ta, el); bump(sizes, cs.fontSize, ta, el); bump(weights, String(fw), ta, el); bump(lhs, cs.lineHeight, ta, el);
    const L = layers(el), large = fs >= 24 || (fs >= 18.66 && fw >= 700); const pk = cs.color + "|" + L.join(">");
    const pr = bump(pairs, pk, ta, el, { fg: cs.color, bg: L, fs: fs, small: 0, large: 0 }, 2); pr.fs = Math.min(pr.fs, fs); if (large) pr.large++; else pr.small++;
    if (!pr.text) pr.text = txt.slice(0, 40); }
  const shadow = cs.boxShadow && cs.boxShadow !== "none";
  const rad = cs.borderRadius; const box = hasBg || hasBorder || shadow;
  if (rad && rad !== "0px" && (box || tag === "IMG" || tag === "INPUT" || tag === "BUTTON")) bump(radii, rad, 1, el);
  if (shadow) bump(shadows, cs.boxShadow.slice(0, 160), 1, el);
  if (cs.position !== "static" && cs.zIndex !== "auto") bump(zs, cs.zIndex, 1, el);
  const td = cs.transitionDuration; if (td && !/^0s(,\s*0s)*$/.test(td)) bump(trans, (cs.transitionProperty + " " + td + " " + cs.transitionTimingFunction).slice(0, 80), 1, el);
  for (const [p, props] of ["padding", "margin"].map((q) => [q, SIDES.map((d) => q + d)])) {
    const seen = {}; for (const k of props) { const v = cs[k]; const px = parseFloat(v); if (!v || !px || seen[v] || Math.abs(px) > (p === "margin" ? 96 : 128)) continue; seen[v] = 1;
      const s = bump(space, v, 1, el, { props: {} }); s.props[p] = (s.props[p] || 0) + 1; } }
  if (/flex|grid/.test(cs.display)) for (const g of [cs.rowGap, cs.columnGap]) { const px = parseFloat(g); if (g && px && px <= 128) { const s = bump(space, g, 1, el, { props: {} }); s.props.gap = (s.props.gap || 0) + 1; break; } }
  for (const c of cls) if (c.startsWith(PFX + "-") && /^[a-z]/.test(c.slice(PFX.length + 1))) dsCls.set(c, (dsCls.get(c) || 0) + 1);
  const role = low(attr(el, "role"));
  if (/^(HEADER|NAV|MAIN|ASIDE|FOOTER)$/.test(tag) || /^(banner|navigation|main|complementary|contentinfo|search|region|form)$/.test(role)) {
    if (landmarks.length < 24 && (tag !== "HEADER" && tag !== "FOOTER" || !el.closest("article,section,[class*=card]")))
      landmarks.push({ tag: low(tag), role: role || undefined, label: (attr(el, "aria-label") || "").slice(0, 40) || undefined, sel: selOf(el),
        rect: [R(r.left), R(top), R(r.width), R(r.height)], links: el.querySelectorAll("a").length }); }
  const tabbable = el.hasAttribute("tabindex") && attr(el, "tabindex") !== "-1";
  const hinted = cls.length && HINT_RE.some(([k, re]) => re.test(hintText(cls)));
  const pointer = cs.cursor === "pointer" && !INTER[tag];
  const scroller = /(auto|scroll|hidden|clip)/.test(cs.overflowX) && el.children.length >= 3 && el.scrollWidth > el.clientWidth + 16;
  if (!(INTER[tag] || role || tabbable || STRUCT.test(tag) || box || hinted || pointer || scroller)) return null;
  if (tag === "IMG" && !(r.width <= 96 && Math.abs(r.width - r.height) < 3)) return null;
  const kind = kindOf(el, cs, r, cls, txt, box);
  if (kind === "generic") return null;
  const stems = Array.from(new Set(cls.map(stemOf).filter(Boolean))).sort().slice(0, 3);
  const type = tag === "INPUT" ? (el.type || "text") : "";
  // Items of different widgets (two tab bars, header vs sidebar nav) are different patterns: key them by their container.
  const owner = /^(tab|nav-item|menu-item|breadcrumb-item|pagination-item)$/.test(kind) && el.parentElement
    ? part(el.parentElement.closest("[role=tablist],[role=menu],[role=menubar],nav,ul,ol") || el.parentElement) : "";
  const sig = [kind, low(tag) + (type ? "[" + type + "]" : ""), role, stems.join("."), shapeOf(el), owner].join("|");
  let cl = clusters.get(sig);
  if (!cl) { cl = { sig, kinds: {}, n: 0, fold: 0, top: 1e9, w: [], h: [], el, area: 0, stems, states: {}, bg: {}, fg: {}, hs: {}, fs: {}, rad: {}, bd: {}, mods: {}, icon: 0, img: 0, href: 0, named: 0, unnamed: 0, names: [], role: role || "", interactive: 0 };
    clusters.set(sig, cl); }
  cl.n++; cl.kinds[kind] = (cl.kinds[kind] || 0) + 1; cl.fold += fold; cl.top = Math.min(cl.top, R(top)); pushNum(cl.w, R(r.width)); pushNum(cl.h, R(r.height)); cl.area += area;
  if (hasBg) topSet(cl.bg, bg); topSet(cl.fg, cs.color); topSet(cl.hs, String(R(r.height))); topSet(cl.fs, cs.fontSize); if (rad !== "0px") topSet(cl.rad, rad); if (hasBorder) topSet(cl.bd, cs.borderTopColor);
  for (const m of modsOf(cls)) topSet(cl.mods, m);
  for (const a of "aria-expanded aria-selected aria-current aria-pressed aria-checked aria-disabled data-state aria-invalid".split(" ")) { const v = attr(el, a); if (v !== null) topSet(cl.states, a + "=" + v.slice(0, 16)); }
  if (el.disabled || el.hasAttribute("disabled")) topSet(cl.states, "disabled");
  if (qs(el, "svg,i[class*=icon],[class*=icon]")) cl.icon++; if (qs(el, "img,picture")) cl.img++; if (attr(el, "href")) cl.href++;
  if (INTERACTIVE_KIND.test(kind) || INTER[tag]) { cl.interactive++; const nm = nameOf(el); if (nm) { cl.named++; if (cl.names.length < 4 && !cl.names.includes(nm)) cl.names.push(nm); } else cl.unnamed++; }
  if (area > (cl.bestArea || 0) && cl.n <= 20) { cl.bestArea = area; cl.el = el; }
  return null; };
const SKIP = /^(SCRIPT|STYLE|META|LINK|NOSCRIPT|TEMPLATE|HEAD|TITLE|BR|WBR|SOURCE|TRACK|PARAM|OBJECT|EMBED|IFRAME)$/;
try { const stack = [body]; while (stack.length) {
    const el = stack.pop();
    if (out.walked >= MAX_NODES || performance.now() - T0 > MAX_MS) { out.truncated.push(out.walked >= MAX_NODES ? "walk:node-cap" : "walk:time-cap"); break; }
    if (SKIP.test(el.tagName)) continue; out.walked++;
    let cs; try { cs = getComputedStyle(el); } catch (e) { continue; }
    if (cs.display === "none" || cs.visibility === "hidden" || cs.opacity === "0") continue;
    const r = el.getBoundingClientRect(); const top = r.top + scrollY;
    const kids = () => { if (el instanceof SVGElement) return; const ch = el.children; for (let i = ch.length - 1; i >= 0; i--) stack.push(ch[i]);
      if (el.shadowRoot) for (const c of Array.from(el.shadowRoot.children).reverse()) stack.push(c); };
    if (r.width < 2 || r.height < 2) { if (!/hidden|clip/.test(cs.overflow)) kids(); continue; }
    if (r.right < 0 || r.left > vw * 1.25 || top > MAX_Y || r.bottom + scrollY < 0) continue;
    out.visible++; if (el !== body) visit(el, cs, r, top); kids(); }
} catch (e) { out.notes.push("walk: " + String(e).slice(0, 120)); }
const vars = { count: 0, ds: 0, crossOrigin: 0, sample: {} };
try { const names = new Set(); const walkR = (rules) => { for (const rule of Array.from(rules)) { try {
      if (rule.cssRules) walkR(rule.cssRules);
      if (rule.style && /(^|,)\s*(:root|html|body)\b/.test(rule.selectorText || "")) for (const p of Array.from(rule.style)) if (p.startsWith("--") && names.size < 3000) names.add(p);
    } catch (e) {} } };
  for (const sh of Array.from(document.styleSheets)) { try { walkR(sh.cssRules); } catch (e) { vars.crossOrigin++; } }
  for (const p of Array.from(root.style)) if (p.startsWith("--")) names.add(p);
  const rs = getComputedStyle(root); vars.count = names.size; const dsp = "--" + PFX + "-";
  const ordered = Array.from(names).sort((a, b) => (b.startsWith(dsp) - a.startsWith(dsp)) || a.localeCompare(b));
  let others = 0; for (const p of ordered) { const isDs = p.startsWith(dsp); if (isDs) vars.ds++;
    if (!isDs && others >= 60) continue; const v = rs.getPropertyValue(p).trim(); if (!v || v.length > 140) continue;
    if (Object.keys(vars.sample).length >= 260) break; vars.sample[p] = v; if (!isDs) others++; }
} catch (e) { out.notes.push("vars: " + String(e).slice(0, 120)); }
const forms = [];
try { for (const f of Array.from(document.querySelectorAll("form,[role=form]")).slice(0, 8)) { const r = f.getBoundingClientRect(); if (r.width < 2 || r.height < 2) continue;
    const controls = []; for (const c of Array.from(f.querySelectorAll("input,select,textarea,button,[role=switch],[role=combobox],[role=checkbox]")).slice(0, 16)) {
      if (c.type === "hidden") continue; const cr = c.getBoundingClientRect(); if (cr.width < 2 && cr.height < 2 && c.type !== "checkbox" && c.type !== "radio") continue;
      controls.push({ tag: low(c.tagName), type: c.type || attr(c, "role") || undefined, name: (attr(c, "name") || "").slice(0, 30) || undefined,
        label: nameOf(c).slice(0, 40) || undefined, required: c.required || undefined, sel: selOf(c) }); }
    forms.push({ sel: selOf(f), method: low(attr(f, "method") || "get"), action: (attr(f, "action") || "").replace(/^https?:\/\/[^/]+/, "").slice(0, 60), controls }); }
} catch (e) { out.notes.push("forms: " + String(e).slice(0, 120)); }
const top = (map, k, by) => Array.from(map.values()).sort((a, b) => (b[by || "area"] - a[by || "area"]) || (b.n - a.n)).slice(0, k)
  .map((r) => { const o = Object.assign({}, r); if (by === "n") delete o.area; else o.area = R(o.area); return o; });
const rgbOf = (v) => { const m = /^rgba?\(([\d.]+),\s*([\d.]+),\s*([\d.]+)(?:,\s*([\d.]+))?\)$/.exec(v || ""); return m ? [+m[1], +m[2], +m[3], m[4] === undefined ? 1 : +m[4]] : null; };
const over = (top, bot) => [0, 1, 2].map((i) => top[i] * top[3] + bot[i] * (1 - top[3])).concat([1]);
const lum = (c) => { const f = (x) => { x /= 255; return x <= 0.04045 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); }; return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
const ratioOf = (p) => { if (p.bg[0] === "image") return null; let bg = /dark/.test(out.colorScheme) ? [18, 18, 18, 1] : [255, 255, 255, 1];
  for (const l of p.bg.slice().reverse()) { if (l === "canvas") continue; const c = rgbOf(l); if (!c) return null; bg = over(c, bg); }
  const fg0 = rgbOf(p.fg); if (!fg0) return null; const fg = over(fg0, bg); const a = lum(fg), b = lum(bg); return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05); };
const med = (a) => { if (!a.length) return 0; const s = a.slice().sort((x, y) => x - y); return s[Math.floor(s.length / 2)]; };
const topKeys = (o, k) => Object.entries(o).sort((a, b) => b[1] - a[1]).slice(0, k || 5);
const STRONG = /^(carousel|hero|modal|table|tabs|breadcrumb|pagination|nav|header|footer|form|accordion|select|toggle|input|search|textarea|steps|toast|alert|tooltip|progress|slider|menu|code-block|file-input)$/;
const clOut = [];
for (const cl of clusters.values()) { const kind = topKeys(cl.kinds, 1)[0][0];
  if (cl.n < 2 && !STRONG.test(kind)) continue; if (/^(container|image|label|list|figure|tab-panel|card-header|card-footer)$/.test(kind) && cl.n < 3) continue;
  const fold = cl.fold / cl.n, mw = med(cl.w), mh = med(cl.h);
  const rank = Math.log2(1 + cl.n) * (1 + 2 * fold) * (INTERACTIVE_KIND.test(kind) ? 1.5 : 1) * (/^(generic|container|clickable)$/.test(kind) ? 0.5 : 1) * Math.min(3, 1 + Math.sqrt(mw * mh) / 200);
  clOut.push({ rank, cl, kind, fold, mw, mh }); }
clOut.sort((a, b) => b.rank - a.rank);
let clusterList = clOut.slice(0, 40).map(({ rank, cl, kind, mw, mh }) => { const o = { kind, n: cl.n, sig: cl.sig, rank: R(rank * 10) / 10,
    fold: cl.fold, top: cl.top, w: [Math.min.apply(null, cl.w), mw, Math.max.apply(null, cl.w)], h: [Math.min.apply(null, cl.h), mh, Math.max.apply(null, cl.h)],
    sel: selOf(cl.el), html: snip(cl.el, 600), stems: cl.stems };
  if (Object.keys(cl.kinds).length > 1) o.kinds = cl.kinds; if (cl.role) o.role = cl.role;
  const vr = { bg: topKeys(cl.bg), fg: topKeys(cl.fg, 3), h: topKeys(cl.hs), fs: topKeys(cl.fs, 4), radius: topKeys(cl.rad, 3), border: topKeys(cl.bd, 3), mods: topKeys(cl.mods, 8) };
  for (const k of Object.keys(vr)) if (!vr[k].length) delete vr[k]; o.variants = vr;
  if (cl.icon) o.icon = cl.icon; if (cl.img) o.img = cl.img; if (cl.href) o.href = cl.href; if (Object.keys(cl.states).length) o.states = cl.states;
  if (cl.interactive) o.a11y = { named: cl.named, unnamed: cl.unnamed, names: cl.names };
  return o; });
const colorsOut = top(colors, 32);
out.styles = { colors: colorsOut, fonts: { families: top(fam, 6), sizes: top(sizes, 18), weights: top(weights, 8), lineHeights: top(lhs, 10) },
  radii: top(radii, 16, "n"), spacing: top(space, 24, "n"), shadows: top(shadows, 10, "n"), zIndex: top(zs, 12, "n"), transitions: top(trans, 8, "n"), pairs: (() => { const all = top(pairs, 400).map((p) => { delete p.v; const r = ratioOf(p); if (r) p.cr = R(r * 100) / 100; return p; });
    const keep = all.slice(0, 12); const worst = all.slice(12).filter((p) => p.cr && p.cr < 4.5).sort((a, b) => a.cr - b.cr).slice(0, 10);
    return keep.concat(worst); })() };
out.vars = vars; out.dsClasses = { prefix: PFX, total: Array.from(dsCls.values()).reduce((a, b) => a + b, 0), top: Array.from(dsCls.entries()).sort((a, b) => b[1] - a[1]).slice(0, 40) };
out.landmarks = landmarks; out.forms = forms; out.clusters = clusterList;
out.ms = R(performance.now() - T0);
let s = JSON.stringify(out);
// Over the cap: trim in rounds — fewer examples, shorter markup, then every list shorter.
const lists = () => [out.clusters, out.landmarks, out.forms].concat(Object.values(out.styles).filter(Array.isArray), Object.values(out.styles.fonts));
for (let i = 0; s.length > CAP && i < 8; i++) {
  out.truncated.push("shrink:" + i);
  out.clusters.forEach((c) => { c.html = c.html.slice(0, Math.max(160, 600 - 80 * (i + 1))); if (i > 3) { delete c.stems; c.sig = c.sig.slice(0, 90); } });
  for (const l of lists()) { l.forEach((r) => { if (r.ex) r.ex = r.ex.slice(0, i > 4 ? 0 : 1); }); if (i > 1) l.length = Math.min(l.length, Math.max(4, Math.ceil(l.length * 0.8))); }
  if (i === 2) { const keep = {}; for (const [k, v] of Object.entries(out.vars.sample)) if (k.startsWith("--" + PFX + "-") && Object.keys(keep).length < 150) keep[k] = v; out.vars.sample = keep; }
  if (i === 6) out.vars.sample = {};
  s = JSON.stringify(out); }
while (s.length > CAP && out.clusters.length) { out.clusters.pop(); s = JSON.stringify(out); }
if (s.length > CAP) { out.styles = { colors: out.styles.colors.slice(0, 20) }; out.truncated.push("hard-cap"); s = JSON.stringify(out); }
if (typeof s !== "string" || s.length > CAP) s = JSON.stringify({ probe: "ds-site-probe", v: 1, url: out.url.slice(0, 300), truncated: ["hard-cap:dropped"],
  notes: ["over the size cap"], styles: {}, clusters: [] });
return s;
})()"""


# ── generated by scripts/build_probe.py from SITE_PROBE_SOURCE — never edit by hand ──
# The minified form is what ships: browser_eval passes the script on the agent-browser command
# line, and Windows' CreateProcess caps a whole command line at 32,767 characters.
SOURCE_SHA256 = "3f16c6c512f583e58cd2ef60bd41b872ecd03167126d668fc64553c4bad4a58e"
SITE_PROBE_JS = '(()=>{const at=performance.now(),ct=5e3,Dt=3500,q=+\'__CAP__\',L=\'__DS_PREFIX__\',a={probe:\'ds-site-probe\',v:1,url:String(location.href).slice(0,2e3),title:String(document.title||\'\').slice(0,120),viewport:{w:innerWidth||1280,h:innerHeight||800},readyState:document.readyState,colorScheme:\'\',walked:0,visible:0,ms:0,truncated:[],notes:[]},B=document.documentElement,_=document.body,lt=[\'Top\',\'Right\',\'Bottom\',\'Left\'],h=(t,e)=>t.getAttribute(e),k=(t,e)=>t.querySelector(e),v=t=>String(t||\'\').toLowerCase(),w=Math.round;if(!_)return a.notes.push(\'no <body>\'),JSON.stringify(a);const D=a.viewport.w,S=a.viewport.h;a.docHeight=w(Math.max(B.scrollHeight||0,_.scrollHeight||0,S));const Ft=S*12;try{a.colorScheme=getComputedStyle(B).colorScheme||\'\'}catch{}const F=t=>!t||t===\'transparent\'||/rgba\\([^)]*,\\s*0\\)$/.test(t)||/\\/\\s*0\\)$/.test(t),jt=t=>{let e=/rgba\\([^,]+,[^,]+,[^,]+,\\s*([\\d.]+)\\)/.exec(t)||/\\/\\s*([\\d.]+%?)\\s*\\)$/.exec(t);if(!e)return 1;const n=e[1].endsWith(\'%\')?parseFloat(e[1])/100:parseFloat(e[1]);return isNaN(n)?1:n},Y=t=>t.replace(/([a-z0-9])([A-Z])/g,\'$1-$2\').toLowerCase(),dt=t=>{const e=t.getAttribute&&h(t,\'class\');return e?e.trim().split(/\\s+/).slice(0,40):[]},ft=new WeakMap,X=t=>{let e=v(t.tagName);if(t.id&&t.id.length<32&&!/\\d{3,}|[:.]/.test(t.id))return e+\'#\'+t.id;const n=dt(t).filter(r=>!/[:\\[\\]\\/!@%()]/.test(r)).slice(0,2).map(r=>r.slice(0,40));return n.length?e+\'.\'+n.join(\'.\'):e},j=t=>{let e=ft.get(t);if(e)return e;const n=t.parentElement;return e=(n&&n!==_&&n!==B?X(n)+\' > \':\'\')+X(t),e.length>72&&(e=e.slice(0,71)+\'…\'),ft.set(t,e),e},y=(t,e,n,r,s,i)=>{let o=t.get(e);if(o||(o={v:e,n:0,area:0,ex:[]},s&&Object.assign(o,s),t.set(e,o)),o.n++,o.area+=n,r&&o.ex.length<(i||1)){const c=j(r);o.ex.includes(c)||o.ex.push(c)}return o},ut=new Map,ht=new Map,pt=new Map,gt=new Map,mt=new Map,bt=new Map,Z=new Map,wt=new Map,yt=new Map,kt=new Map,vt=new Map,K=new Map,G=(t,e,n,r)=>{if(F(t))return;const s=y(ut,t,n,r,{props:{}},2);s.props[e]=(s.props[e]||0)+1},J=new WeakMap,Q=t=>{if(!t||t.nodeType!==1)return[\'canvas\'];if(J.has(t))return J.get(t);let e;try{const n=getComputedStyle(t);n.backgroundImage&&n.backgroundImage!==\'none\'&&!/^url\\(["\']?data:image\\/svg/.test(n.backgroundImage)?e=[\'image\']:F(n.backgroundColor)?e=Q(t.parentElement):e=jt(n.backgroundColor)>=.999?[n.backgroundColor]:[n.backgroundColor].concat(Q(t.parentElement)).slice(0,4)}catch{e=[\'canvas\']}return J.set(t,e),e},At=t=>{const e={};for(const n of t.split(\' \')){const[r,s]=n.split(\'=\');e[r]=s||r}return e},Et=At(\'button link tab tablist=tabs dialog=modal alertdialog=modal alert status=toast tooltip menu menubar=menu menuitem=menu-item menuitemcheckbox=menu-item menuitemradio=menu-item switch=toggle checkbox radio progressbar=progress slider combobox=select listbox=select option row=table-row table grid=table searchbox=input textbox=input navigation=nav banner=header contentinfo=footer separator=divider tabpanel=tab-panel listitem=list-row article=card search heading img=image\'),Gt=At(\'TD=table-cell TH=table-cell THEAD=table-part TBODY=table-part SELECT=select TEXTAREA=textarea DIALOG=modal DETAILS=accordion SUMMARY=accordion TABLE=table TR=table-row PROGRESS=progress METER=progress HR=divider NAV=nav FORM=form KBD=kbd PRE=code-block LABEL=label H1=heading H2=heading H3=heading H4=heading H5=heading H6=heading BLOCKQUOTE=quote FIGURE=figure\'),tt=\'carousel=carousel|marquee|swiper|slick-slider|splide|embla|slideshow toast=toast|snackbar|notification tooltip=tooltip|popover-tip modal=modal|dialog|lightbox breadcrumb=breadcrumbs? pagination=pagination|pager|paginator avatar badge=badge|chip|pill|tag|label-pill|lozenge tab=tabs?|tab-item|tab-button|segmented accordion=accordion|collapsible|disclosure|faq-item toggle=switch|toggle hero=hero|jumbotron|masthead|splash card=card|tile alert=alert|callout|notice|banner|announcement menu=dropdown|menu|popover nav-item=nav-?item|nav-?link|menu-?item button=btn|button|cta skeleton=skeleton|shimmer spinner=spinner|loader|loading steps=stepper|steps? stat=stat|metric|kpi kbd=kbd|keycap|shortcut divider=divider|separator|rule input=input|text-?field|search-?box select=select|combobox\'.split(\' \').map(t=>t.split(\'=\')).map(([t,e])=>[t,new RegExp(\'(^|[\\\\s_-])(\'+(e||t)+\')(?=[\\\\s_-]|$)\')]),St=/^(is-|has-)?(active|selected|current|open|opened|closed|disabled|expanded|collapsed|checked|focus|focused|hover|visible|hidden|show|shown|loading|error|invalid|valid)$/,Tt=/^-?([mp][trblxyse]?|[wh]|min|max|size|gap|space|text|bg|border|rounded|shadow|flex|grid|col|row|items|justify|self|content|place|font|leading|tracking|opacity|z|top|left|right|bottom|inset|overflow|cursor|transition|duration|ease|delay|ring|outline|fill|stroke|order|basis|grow|shrink|block|inline|absolute|relative|fixed|sticky|static|sr|truncate|underline|uppercase|lowercase|capitalize|italic|antialiased|container|animate|aspect|object|pointer|select|whitespace|break|list|decoration|divide|backdrop|blur|filter|transform|translate|rotate|scale|origin|visible|invisible|table|float|clear|isolate|from|via|to|hidden|group|peer|prose|line|dark|sm|md|lg|x?l|2xl)(-|$)/,Pt=t=>{if(new RegExp(\'^\'+L+\'-[a-z]\').test(t))return t.toLowerCase().replace(/--.*$/,\'\');if(/[:\\[\\]\\/!@%()#.]/.test(t)||Tt.test(t)||/^(sc-|css-|jsx-|svelte-|emotion-|chakra-|mui|ant-|tw-|framer-|astro-)/i.test(t))return\'\';let e=t.replace(/(__|--|_|-)[A-Za-z0-9]{5,}$/,n=>/\\d/.test(n)?\'\':n);return e=e.replace(/^_?(?:[A-Za-z0-9]{1,8}_)+(?=[A-Za-z])/,n=>/\\d/.test(n)||/.[A-Z]/.test(n.replace(/^_/,\'\'))?\'\':n),/\\d/.test(e)&&e.length>=5&&!/[-_]/.test(e)||/^[a-zA-Z]{5,8}$/.test(e)&&/[a-z][A-Z]/.test(e)&&/[A-Z].*[A-Z]/.test(e)||(e=Y(e).replace(/--[a-z0-9-]+$/,\'\').replace(/\\d+/g,\'\'),e.length<2||St.test(e))?\'\':e.slice(0,32)},Ut=t=>{const e=[];for(const n of t){const r=Y(n),s=/--([a-z][a-z0-9-]*)$/.exec(r);s?e.push(s[1]):St.test(r)&&e.push(r)}return e},xt=t=>{let e=\'\';for(const n of t.childNodes)n.nodeType===3&&(e+=n.textContent);return e.replace(/\\s+/g,\' \').trim()},Mt=t=>{const e=h(t,\'aria-label\');if(e)return e.trim();const n=h(t,\'aria-labelledby\');if(n){const i=n.split(/\\s+/).map(o=>{const c=document.getElementById(o);return c?c.textContent:\'\'}).join(\' \').trim();if(i)return i}if(t.labels&&t.labels.length)return t.labels[0].textContent.replace(/\\s+/g,\' \').trim();const r=h(t,\'alt\');if(r)return r;let s=(t.innerText||t.textContent||\'\').replace(/\\s+/g,\' \').trim();if(!s){const i=t.querySelector&&k(t,\'img[alt],svg[aria-label],[aria-label]\');i&&(s=h(i,\'alt\')||h(i,\'aria-label\')||\'\')}return(s||h(t,\'title\')||h(t,\'placeholder\')||h(t,\'value\')||\'\').slice(0,60)},et={A:1,BUTTON:1,INPUT:1,SELECT:1,TEXTAREA:1,SUMMARY:1,DETAILS:1,DIALOG:1},Ot=/^(button|icon-button|link-button|link|nav-item|tab|toggle|checkbox|radio|input|select|textarea|slider|menu-item|accordion|pagination-item|clickable)$/,Wt=/^(LI|TR|ARTICLE|FIGURE|TABLE|FORM|NAV|HEADER|FOOTER|ASIDE|UL|OL|PROGRESS|METER|HR|KBD|PRE|H1|H2|H3|H4|H5|H6|LABEL|BLOCKQUOTE|IMG)$/,qt=/^(id|class|role|type|name|href|alt|placeholder|title|for|disabled|checked|value|tabindex|aria-[a-z]+|data-(state|variant|size|tone|kind|color))$/,Kt=t=>t.replace(/&/g,\'&amp;\').replace(/</g,\'&lt;\').replace(/>/g,\'&gt;\'),Vt=(t,e)=>{const n=(s,i)=>{if(s.nodeType===3){const p=s.textContent.replace(/\\s+/g,\' \').trim();return p?Kt(p.length>40?p.slice(0,37)+\'…\':p):\'\'}if(s.nodeType!==1)return\'\';const o=v(s.tagName);if(/^(script|style|noscript|template|link|meta)$/.test(o))return\'\';let c=\'\';for(const p of Array.from(s.attributes)){if(!qt.test(p.name))continue;let m=p.value;p.name===\'href\'&&(m=m.replace(/^https?:\\/\\/[^/]+/,\'\').slice(0,40)),p.name===\'class\'&&(m=m.trim().split(/\\s+/).slice(0,3).map(O=>O.length>30?O.slice(0,29)+\'…\':O).join(\' \')),m.length>60&&(m=m.slice(0,57)+\'…\'),c+=\' \'+p.name+(m===\'\'?\'\':\'="\'+m.replace(/"/g,\'&quot;\')+\'"\')}if(o===\'svg\'){const p=h(s,\'aria-label\');return p?\'<svg aria-label="\'+p.slice(0,30).replace(/"/g,\'&quot;\')+\'">…</svg>\':\'<svg>…</svg>\'}if(/^(img|input|br|hr|source)$/.test(o))return\'<\'+o+c+\'>\';if(i>=3)return\'<\'+o+c+\'>…</\'+o+\'>\';let d=\'\',M=0;for(const p of Array.from(s.childNodes)){if(M>=5){d+=\'…\';break}const m=n(p,i+1);m&&(d+=m,M++)}return\'<\'+o+c+\'>\'+d+\'</\'+o+\'>\'};let r=\'\';try{r=n(t,0)}catch{}return r.length>e?r.slice(0,e-1)+\'…\':r},Yt=t=>{const e=[];for(const n of Array.from(t.children).slice(0,24)){const r=v(n.tagName);if(/^(script|style|template|noscript|link|meta)$/.test(r))continue;const s=n instanceof SVGElement?\'svg\':r;e[e.length-1]!==s&&e[e.length-1]!==s+\'+\'?e.push(s):e[e.length-1]=s+\'+\'}return e.slice(0,6).join(\',\')+(xt(t)?e.length?\',#t\':\'#t\':\'\')},Rt=t=>t.filter(e=>!/[:\\[\\]\\/!@%()]/.test(e)&&!(Tt.test(e)&&e.includes(\'-\'))).map(e=>{const n=e.lastIndexOf(\'__\');return Y(n>=0?e.slice(n+2):e)}).join(\' \'),It=t=>h(t,\'role\')===\'switch\'||t.hasAttribute(\'aria-checked\')||!!(t.querySelector&&k(t,\'input[type=checkbox]\')),Xt=t=>{if(!t||t.nodeType!==1)return!1;const e=getComputedStyle(t);return(!F(e.backgroundColor)||(parseFloat(e.borderTopWidth)||0)>0)&&(parseFloat(e.paddingLeft)||0)>=8},Zt=t=>{const e=t.parentElement&&(t.parentElement.parentElement||t.parentElement);return!!(e&&k(e,\'button[aria-label*="next" i],button[aria-label*="prev" i],[class*="arrow" i] button,button[class*="next" i]\'))},Jt=(t,e,n,r,s,i)=>{const o=t.tagName,c=v(h(t,\'role\')),d=Rt(r),M=!s&&!(t.innerText||\'\').trim()&&!!k(t,\'svg,img,i,[class*=icon]\'),p=!!t.closest(\'nav,[role=navigation]\')&&!t.closest(\'footer,[role=contentinfo]\'),m=v(h(t,\'aria-label\'));if(/breadcrumb/.test(m))return\'breadcrumb\';if(/pagination|pager/.test(m))return\'pagination\';if(c===\'region\'&&/carousel|slides/.test((h(t,\'aria-roledescription\')||\'\')+\' \'+m)||tt[0][1].test(d))return\'carousel\';if(c&&Et[c]){const u=Et[c];return u===\'button\'&&M?\'icon-button\':u===\'link\'&&p?\'nav-item\':u}if(t.children.length>=3&&t.scrollWidth>t.clientWidth+16&&(/(auto|scroll)/.test(e.overflowX)||/hidden|clip/.test(e.overflowX)&&Zt(t)))return\'carousel\';if(o===\'BUTTON\'||o===\'INPUT\'&&/^(submit|button|reset)$/i.test(t.type))return/(^|[\\s_-])(tab|tabs|segmented)(?=[\\s_-]|$)/.test(d)?\'tab\':/(^|[\\s_-])(switch|toggle)(?=[\\s_-]|$)/.test(d)&&It(t)?\'toggle\':t.hasAttribute(\'aria-expanded\')&&t.closest(\'[class*=accordion],[class*=faq],details\')?\'accordion\':/(^|[\\s_-])(nav-?item|nav-?link|menu-?item)(?=[\\s_-]|$)/.test(d)?\'nav-item\':M?\'icon-button\':\'button\';if(o===\'A\'&&t.closest(\'[class*=breadcrumb],[aria-label*=readcrumb]\'))return\'breadcrumb-item\';if(o===\'A\'&&t.closest(\'[class*=pagination],[aria-label*=agination],[class*=pager]\'))return\'pagination-item\';if(o===\'A\'){const u=t.children.length===1?t.children[0]:null,C=n.height<=64&&(i&&parseFloat(e.paddingLeft)>=8||u&&!/^(IMG|PICTURE|SVG|svg)$/.test(u.tagName)&&Xt(u)&&(t.innerText||\'\').trim().length<=40);return/(^|[\\s_-])(btn|button|cta)(?=[\\s_-]|$)/.test(d)||C?\'link-button\':/(^|[\\s_-])(tab|tabs)(?=[\\s_-]|$)/.test(d)?\'tab\':n.width>160&&n.height>110&&k(t,\'img,h2,h3,h4,picture\')?\'card\':/(^|[\\s_-])(badge|chip|pill|tag)(?=[\\s_-]|$)/.test(d)?\'badge\':p?\'nav-item\':\'link\'}if(o===\'INPUT\'){const u=v(t.type||\'text\');return u===\'checkbox\'?/switch|toggle/.test(d)?\'toggle\':\'checkbox\':u===\'radio\'?\'radio\':u===\'range\'?\'slider\':u===\'file\'?\'file-input\':u===\'search\'?\'search\':\'input\'}const O=Gt;if(o===\'NAV\'&&/breadcrumb/.test(d))return\'breadcrumb\';if((o===\'NAV\'||o===\'UL\')&&/pagination|pager/.test(d))return\'pagination\';if(O[o])return O[o];if(o===\'HEADER\')return t.parentElement===_||n.top+scrollY<10?\'header\':\'card-header\';if(o===\'FOOTER\')return n.width>=D*.6&&n.top+scrollY+n.height>=a.docHeight*.6&&!t.closest(\'article,[class*=card]\')?\'footer\':\'card-footer\';if(o===\'IMG\'){const u=parseFloat(e.borderTopLeftRadius)||0;return Math.abs(n.width-n.height)<3&&n.width<=96&&u>=n.width*.3||/avatar/.test(d)?\'avatar\':\'image\'}if(o===\'UL\'||o===\'OL\')return/breadcrumb/.test(d)?\'breadcrumb\':/pagination|pager/.test(d)?\'pagination\':\'list\';if(o===\'LI\')return/breadcrumb/.test(d)||t.closest(\'[class*=breadcrumb],[aria-label*=readcrumb]\')?\'breadcrumb-item\':t.closest(\'[class*=pagination],[aria-label*=agination]\')?\'pagination-item\':p?\'nav-item\':\'list-row\';for(const[u,C]of tt)if(C.test(d)){if(u===\'button\'&&!i||u===\'toggle\'&&!It(t))continue;if(u===\'hero\'){if($&&$.contains(t)||o===\'MAIN\'||o===\'BODY\'||n.height>S*1.6)continue;$=t}return u}if(o===\'ARTICLE\')return\'card\';const P=e.position===\'fixed\'||e.position===\'sticky\';if(P&&n.width>D*.4&&n.height>S*.4&&i)return\'modal\';if(P&&n.width<480&&n.height<200&&i&&n.top>S*.5)return\'toast\';const U=s||(t.children.length<=2?String(t.textContent||\'\').replace(/\\s+/g,\' \').trim():\'\');if(i&&U&&U.length<=24&&n.height<=34&&n.width<=220&&(parseFloat(e.borderTopLeftRadius)||0)>0&&t.children.length<=2)return\'badge\';if(Math.abs(n.width-n.height)<3&&n.width<=64&&(parseFloat(e.borderTopLeftRadius)||0)>=n.width*.4&&(k(t,\'img\')||s&&s.length<=3))return\'avatar\';if(n.top+scrollY<S&&n.width>=D*.75&&n.height>=S*.35&&n.height<=S*1.6&&o!==\'MAIN\'&&k(t,\'h1\'))return $&&$.contains(t)?i?\'container\':\'generic\':($=t,\'hero\');if(e.display.includes(\'flex\')&&e.flexDirection.startsWith(\'row\')&&t.children.length>=2&&n.height<=160){const u=t.children[0];if(u&&(u.tagName===\'IMG\'||u instanceof SVGElement||/avatar|icon|thumb/.test(String(u.className))))return\'media-object\'}return i&&n.width>=120&&n.width<=720&&n.height>=70&&n.height<=900&&t.children.length>=2&&k(t,\'h2,h3,h4,h5,img,picture,p\')?\'card\':e.cursor===\'pointer\'&&!(t.parentElement&&getComputedStyle(t.parentElement).cursor===\'pointer\')&&!t.closest(\'a,button,label,summary,[role]\')?\'clickable\':i?\'container\':\'generic\'};let $=null;const nt=new Map,rt=[],Ct=(t,e)=>{t.length<60&&t.push(e)},x=(t,e)=>{e==null||e===\'\'||(t[e]=(t[e]||0)+1)},Qt=(t,e,n,r)=>{const s=t.tagName,i=dt(t),o=xt(t),c=r<S?1:0,d=Math.min(n.width,D)*Math.min(n.height,S),M=e.backgroundColor,p=!F(M);let m=!1;const O=(n.width+n.height)*2;p&&G(M,\'background\',d,t);const P={};for(const l of lt){const b=parseFloat(e[\'border\'+l+\'Width\'])||0,g=e[\'border\'+l+\'Color\'];b>0&&e[\'border\'+l+\'Style\']!==\'none\'&&!F(g)&&(m=!0,P[g]||(P[g]=1,G(g,\'border\',O/4,t)))}if(e.outlineStyle!==\'none\'&&(parseFloat(e.outlineWidth)||0)>0&&G(e.outlineColor,\'outline\',O,t),t instanceof SVGElement){const l=t.querySelector&&k(t,\'path,circle,rect,polygon,line,use\'),b=l?getComputedStyle(l):e;for(const[g,E]of[[\'fill\',b.fill],[\'stroke\',b.stroke]])E&&E!==\'none\'&&!/^url/.test(E)&&G(E,g,d,t);return null}if(o){const l=parseFloat(e.fontSize)||16,b=parseInt(e.fontWeight)||400,g=Math.min(d,o.length*l*l*.55);G(e.color,\'color\',g,t),y(ht,e.fontFamily.slice(0,64),g,t),y(pt,e.fontSize,g,t),y(gt,String(b),g,t),y(mt,e.lineHeight,g,t);const E=Q(t),N=l>=24||l>=18.66&&b>=700,V=e.color+\'|\'+E.join(\'>\'),R=y(vt,V,g,t,{fg:e.color,bg:E,fs:l,small:0,large:0},2);R.fs=Math.min(R.fs,l),N?R.large++:R.small++,R.text||(R.text=o.slice(0,40))}const U=e.boxShadow&&e.boxShadow!==\'none\',u=e.borderRadius,C=p||m||U;u&&u!==\'0px\'&&(C||s===\'IMG\'||s===\'INPUT\'||s===\'BUTTON\')&&y(bt,u,1,t),U&&y(wt,e.boxShadow.slice(0,160),1,t),e.position!==\'static\'&&e.zIndex!==\'auto\'&&y(yt,e.zIndex,1,t);const ot=e.transitionDuration;ot&&!/^0s(,\\s*0s)*$/.test(ot)&&y(kt,(e.transitionProperty+\' \'+ot+\' \'+e.transitionTimingFunction).slice(0,80),1,t);for(const[l,b]of[\'padding\',\'margin\'].map(g=>[g,lt.map(E=>g+E)])){const g={};for(const E of b){const N=e[E],V=parseFloat(N);if(!N||!V||g[N]||Math.abs(V)>(l===\'margin\'?96:128))continue;g[N]=1;const R=y(Z,N,1,t,{props:{}});R.props[l]=(R.props[l]||0)+1}}if(/flex|grid/.test(e.display))for(const l of[e.rowGap,e.columnGap]){const b=parseFloat(l);if(l&&b&&b<=128){const g=y(Z,l,1,t,{props:{}});g.props.gap=(g.props.gap||0)+1;break}}for(const l of i)l.startsWith(L+\'-\')&&/^[a-z]/.test(l.slice(L.length+1))&&K.set(l,(K.get(l)||0)+1);const W=v(h(t,\'role\'));(/^(HEADER|NAV|MAIN|ASIDE|FOOTER)$/.test(s)||/^(banner|navigation|main|complementary|contentinfo|search|region|form)$/.test(W))&&rt.length<24&&(s!==\'HEADER\'&&s!==\'FOOTER\'||!t.closest(\'article,section,[class*=card]\'))&&rt.push({tag:v(s),role:W||void 0,label:(h(t,\'aria-label\')||\'\').slice(0,40)||void 0,sel:j(t),rect:[w(n.left),w(r),w(n.width),w(n.height)],links:t.querySelectorAll(\'a\').length});const ie=t.hasAttribute(\'tabindex\')&&h(t,\'tabindex\')!==\'-1\',ae=i.length&&tt.some(([l,b])=>b.test(Rt(i))),ce=e.cursor===\'pointer\'&&!et[s],le=/(auto|scroll|hidden|clip)/.test(e.overflowX)&&t.children.length>=3&&t.scrollWidth>t.clientWidth+16;if(!(et[s]||W||ie||Wt.test(s)||C||ae||ce||le)||s===\'IMG\'&&!(n.width<=96&&Math.abs(n.width-n.height)<3))return null;const z=Jt(t,e,n,i,o,C);if(z===\'generic\')return null;const zt=Array.from(new Set(i.map(Pt).filter(Boolean))).sort().slice(0,3),Bt=s===\'INPUT\'?t.type||\'text\':\'\',de=/^(tab|nav-item|menu-item|breadcrumb-item|pagination-item)$/.test(z)&&t.parentElement?X(t.parentElement.closest(\'[role=tablist],[role=menu],[role=menubar],nav,ul,ol\')||t.parentElement):\'\',it=[z,v(s)+(Bt?\'[\'+Bt+\']\':\'\'),W,zt.join(\'.\'),Yt(t),de].join(\'|\');let f=nt.get(it);f||(f={sig:it,kinds:{},n:0,fold:0,top:1e9,w:[],h:[],el:t,area:0,stems:zt,states:{},bg:{},fg:{},hs:{},fs:{},rad:{},bd:{},mods:{},icon:0,img:0,href:0,named:0,unnamed:0,names:[],role:W||\'\',interactive:0},nt.set(it,f)),f.n++,f.kinds[z]=(f.kinds[z]||0)+1,f.fold+=c,f.top=Math.min(f.top,w(r)),Ct(f.w,w(n.width)),Ct(f.h,w(n.height)),f.area+=d,p&&x(f.bg,M),x(f.fg,e.color),x(f.hs,String(w(n.height))),x(f.fs,e.fontSize),u!==\'0px\'&&x(f.rad,u),m&&x(f.bd,e.borderTopColor);for(const l of Ut(i))x(f.mods,l);for(const l of\'aria-expanded aria-selected aria-current aria-pressed aria-checked aria-disabled data-state aria-invalid\'.split(\' \')){const b=h(t,l);b!==null&&x(f.states,l+\'=\'+b.slice(0,16))}if((t.disabled||t.hasAttribute(\'disabled\'))&&x(f.states,\'disabled\'),k(t,\'svg,i[class*=icon],[class*=icon]\')&&f.icon++,k(t,\'img,picture\')&&f.img++,h(t,\'href\')&&f.href++,Ot.test(z)||et[s]){f.interactive++;const l=Mt(t);l?(f.named++,f.names.length<4&&!f.names.includes(l)&&f.names.push(l)):f.unnamed++}return d>(f.bestArea||0)&&f.n<=20&&(f.bestArea=d,f.el=t),null},te=/^(SCRIPT|STYLE|META|LINK|NOSCRIPT|TEMPLATE|HEAD|TITLE|BR|WBR|SOURCE|TRACK|PARAM|OBJECT|EMBED|IFRAME)$/;try{const t=[_];for(;t.length;){const e=t.pop();if(a.walked>=ct||performance.now()-at>Dt){a.truncated.push(a.walked>=ct?\'walk:node-cap\':\'walk:time-cap\');break}if(te.test(e.tagName))continue;a.walked++;let n;try{n=getComputedStyle(e)}catch{continue}if(n.display===\'none\'||n.visibility===\'hidden\'||n.opacity===\'0\')continue;const r=e.getBoundingClientRect(),s=r.top+scrollY,i=()=>{if(e instanceof SVGElement)return;const o=e.children;for(let c=o.length-1;c>=0;c--)t.push(o[c]);if(e.shadowRoot)for(const c of Array.from(e.shadowRoot.children).reverse())t.push(c)};if(r.width<2||r.height<2){/hidden|clip/.test(n.overflow)||i();continue}r.right<0||r.left>D*1.25||s>Ft||r.bottom+scrollY<0||(a.visible++,e!==_&&Qt(e,n,r,s),i())}}catch(t){a.notes.push(\'walk: \'+String(t).slice(0,120))}const H={count:0,ds:0,crossOrigin:0,sample:{}};try{const t=new Set,e=o=>{for(const c of Array.from(o))try{if(c.cssRules&&e(c.cssRules),c.style&&/(^|,)\\s*(:root|html|body)\\b/.test(c.selectorText||\'\'))for(const d of Array.from(c.style))d.startsWith(\'--\')&&t.size<3e3&&t.add(d)}catch{}};for(const o of Array.from(document.styleSheets))try{e(o.cssRules)}catch{H.crossOrigin++}for(const o of Array.from(B.style))o.startsWith(\'--\')&&t.add(o);const n=getComputedStyle(B);H.count=t.size;const r=\'--\'+L+\'-\',s=Array.from(t).sort((o,c)=>c.startsWith(r)-o.startsWith(r)||o.localeCompare(c));let i=0;for(const o of s){const c=o.startsWith(r);if(c&&H.ds++,!c&&i>=60)continue;const d=n.getPropertyValue(o).trim();if(!(!d||d.length>140)){if(Object.keys(H.sample).length>=260)break;H.sample[o]=d,c||i++}}}catch(t){a.notes.push(\'vars: \'+String(t).slice(0,120))}const Nt=[];try{for(const t of Array.from(document.querySelectorAll(\'form,[role=form]\')).slice(0,8)){const e=t.getBoundingClientRect();if(e.width<2||e.height<2)continue;const n=[];for(const r of Array.from(t.querySelectorAll(\'input,select,textarea,button,[role=switch],[role=combobox],[role=checkbox]\')).slice(0,16)){if(r.type===\'hidden\')continue;const s=r.getBoundingClientRect();s.width<2&&s.height<2&&r.type!==\'checkbox\'&&r.type!==\'radio\'||n.push({tag:v(r.tagName),type:r.type||h(r,\'role\')||void 0,name:(h(r,\'name\')||\'\').slice(0,30)||void 0,label:Mt(r).slice(0,40)||void 0,required:r.required||void 0,sel:j(r)})}Nt.push({sel:j(t),method:v(h(t,\'method\')||\'get\'),action:(h(t,\'action\')||\'\').replace(/^https?:\\/\\/[^/]+/,\'\').slice(0,60),controls:n})}}catch(t){a.notes.push(\'forms: \'+String(t).slice(0,120))}const A=(t,e,n)=>Array.from(t.values()).sort((r,s)=>s[n||\'area\']-r[n||\'area\']||s.n-r.n).slice(0,e).map(r=>{const s=Object.assign({},r);return n===\'n\'?delete s.area:s.area=w(s.area),s}),Lt=t=>{const e=/^rgba?\\(([\\d.]+),\\s*([\\d.]+),\\s*([\\d.]+)(?:,\\s*([\\d.]+))?\\)$/.exec(t||\'\');return e?[+e[1],+e[2],+e[3],e[4]===void 0?1:+e[4]]:null},_t=(t,e)=>[0,1,2].map(n=>t[n]*t[3]+e[n]*(1-t[3])).concat([1]),$t=t=>{const e=n=>(n/=255,n<=.04045?n/12.92:Math.pow((n+.055)/1.055,2.4));return .2126*e(t[0])+.7152*e(t[1])+.0722*e(t[2])},ee=t=>{if(t.bg[0]===\'image\')return null;let e=/dark/.test(a.colorScheme)?[18,18,18,1]:[255,255,255,1];for(const o of t.bg.slice().reverse()){if(o===\'canvas\')continue;const c=Lt(o);if(!c)return null;e=_t(c,e)}const n=Lt(t.fg);if(!n)return null;const r=_t(n,e),s=$t(r),i=$t(e);return(Math.max(s,i)+.05)/(Math.min(s,i)+.05)},Ht=t=>{if(!t.length)return 0;const e=t.slice().sort((n,r)=>n-r);return e[Math.floor(e.length/2)]},I=(t,e)=>Object.entries(t).sort((n,r)=>r[1]-n[1]).slice(0,e||5),ne=/^(carousel|hero|modal|table|tabs|breadcrumb|pagination|nav|header|footer|form|accordion|select|toggle|input|search|textarea|steps|toast|alert|tooltip|progress|slider|menu|code-block|file-input)$/,st=[];for(const t of nt.values()){const e=I(t.kinds,1)[0][0];if(t.n<2&&!ne.test(e)||/^(container|image|label|list|figure|tab-panel|card-header|card-footer)$/.test(e)&&t.n<3)continue;const n=t.fold/t.n,r=Ht(t.w),s=Ht(t.h),i=Math.log2(1+t.n)*(1+2*n)*(Ot.test(e)?1.5:1)*(/^(generic|container|clickable)$/.test(e)?.5:1)*Math.min(3,1+Math.sqrt(r*s)/200);st.push({rank:i,cl:t,kind:e,fold:n,mw:r,mh:s})}st.sort((t,e)=>e.rank-t.rank);let re=st.slice(0,40).map(({rank:t,cl:e,kind:n,mw:r,mh:s})=>{const i={kind:n,n:e.n,sig:e.sig,rank:w(t*10)/10,fold:e.fold,top:e.top,w:[Math.min.apply(null,e.w),r,Math.max.apply(null,e.w)],h:[Math.min.apply(null,e.h),s,Math.max.apply(null,e.h)],sel:j(e.el),html:Vt(e.el,600),stems:e.stems};Object.keys(e.kinds).length>1&&(i.kinds=e.kinds),e.role&&(i.role=e.role);const o={bg:I(e.bg),fg:I(e.fg,3),h:I(e.hs),fs:I(e.fs,4),radius:I(e.rad,3),border:I(e.bd,3),mods:I(e.mods,8)};for(const c of Object.keys(o))o[c].length||delete o[c];return i.variants=o,e.icon&&(i.icon=e.icon),e.img&&(i.img=e.img),e.href&&(i.href=e.href),Object.keys(e.states).length&&(i.states=e.states),e.interactive&&(i.a11y={named:e.named,unnamed:e.unnamed,names:e.names}),i});const se=A(ut,32);a.styles={colors:se,fonts:{families:A(ht,6),sizes:A(pt,18),weights:A(gt,8),lineHeights:A(mt,10)},radii:A(bt,16,\'n\'),spacing:A(Z,24,\'n\'),shadows:A(wt,10,\'n\'),zIndex:A(yt,12,\'n\'),transitions:A(kt,8,\'n\'),pairs:(()=>{const t=A(vt,400).map(r=>{delete r.v;const s=ee(r);return s&&(r.cr=w(s*100)/100),r}),e=t.slice(0,12),n=t.slice(12).filter(r=>r.cr&&r.cr<4.5).sort((r,s)=>r.cr-s.cr).slice(0,10);return e.concat(n)})()},a.vars=H,a.dsClasses={prefix:L,total:Array.from(K.values()).reduce((t,e)=>t+e,0),top:Array.from(K.entries()).sort((t,e)=>e[1]-t[1]).slice(0,40)},a.landmarks=rt,a.forms=Nt,a.clusters=re,a.ms=w(performance.now()-at);let T=JSON.stringify(a);const oe=()=>[a.clusters,a.landmarks,a.forms].concat(Object.values(a.styles).filter(Array.isArray),Object.values(a.styles.fonts));for(let t=0;T.length>q&&t<8;t++){a.truncated.push(\'shrink:\'+t),a.clusters.forEach(e=>{e.html=e.html.slice(0,Math.max(160,600-80*(t+1))),t>3&&(delete e.stems,e.sig=e.sig.slice(0,90))});for(const e of oe())e.forEach(n=>{n.ex&&(n.ex=n.ex.slice(0,t>4?0:1))}),t>1&&(e.length=Math.min(e.length,Math.max(4,Math.ceil(e.length*.8))));if(t===2){const e={};for(const[n,r]of Object.entries(a.vars.sample))n.startsWith(\'--\'+L+\'-\')&&Object.keys(e).length<150&&(e[n]=r);a.vars.sample=e}t===6&&(a.vars.sample={}),T=JSON.stringify(a)}for(;T.length>q&&a.clusters.length;)a.clusters.pop(),T=JSON.stringify(a);return T.length>q&&(a.styles={colors:a.styles.colors.slice(0,20)},a.truncated.push(\'hard-cap\'),T=JSON.stringify(a)),(typeof T!=\'string\'||T.length>q)&&(T=JSON.stringify({probe:\'ds-site-probe\',v:1,url:a.url.slice(0,300),truncated:[\'hard-cap:dropped\'],notes:[\'over the size cap\'],styles:{},clusters:[]})),T})()'
