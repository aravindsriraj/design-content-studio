#!/usr/bin/env node
/**
 * capture.mjs: read a website's brand evidence with a real browser (runs in the capture sandbox,
 * which has internet access and no credentials; never in the design sandbox).
 *
 *   node /opt/studio/capture.mjs --url https://example.com --out /tmp/capture [--pages 3]
 *
 * Writes <out>/capture.json, desktop and mobile screenshots per page, and the files it could
 * download: logo candidates (SVG/PNG/ICO) and web fonts (woff2/woff/ttf/otf) under <out>/assets/.
 * Everything is evidence for a person-reviewed draft; nothing here decides the brand.
 * Exit: 0 written · 2 could not run (bad arguments, browser or the home page failed).
 */
import { createRequire } from "node:module";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const require = createRequire(join(process.env.DESIGN_AGENT_RENDER_DIR || "/opt/design-agent/render", "package.json"));
const puppeteer = require("puppeteer-core");
const BROWSER = process.env.DESIGN_AGENT_BROWSER || "/usr/local/bin/chrome-headless-shell";
const MAX_ASSET = 5 * 1024 * 1024;
const FONT_EXT = /\.(woff2?|ttf|otf)(\?|#|$)/i;

function flags(argv) {
  const out = { pages: 3 };
  for (let i = 0; i < argv.length; i += 2) out[argv[i].replace(/^--/, "")] = argv[i + 1];
  if (!out.url || !out.out) throw new Error("usage: --url <https://…> --out <dir> [--pages N]");
  if (!/^https?:\/\//i.test(out.url)) throw new Error("url must be http(s)");
  out.pages = Math.max(1, Math.min(5, Number(out.pages) || 3));
  return out;
}

// Runs in the page: computed styles, custom properties, colour weights, logos, copy, links.
function readPage() {
  const css = (el, props) => {
    if (!el) return null;
    const s = getComputedStyle(el);
    return Object.fromEntries(props.map((p) => [p, s.getPropertyValue(p)]));
  };
  const TYPE = ["font-family", "font-size", "font-weight", "line-height", "letter-spacing", "text-transform", "color"];
  const BOX = ["background-color", "color", "border-radius", "border-color", "border-width", "padding", "font-family", "font-weight", "font-size", "text-transform"];
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none" && Number(s.opacity) > 0.05;
  };
  const pick = (sel) => [...document.querySelectorAll(sel)].find(visible) || null;

  const vars = {};
  const walk = (rules) => {
    for (const rule of rules || []) {
      if (Object.keys(vars).length >= 5000) return;
      if (rule.cssRules && !rule.selectorText) walk(rule.cssRules); // @media, @layer, @supports
      if (rule.selectorText && /(^|,)\s*(:root|html|body)\b/.test(rule.selectorText)) {
        for (const name of rule.style) if (name.startsWith("--")) vars[name] = rule.style.getPropertyValue(name).trim();
      }
    }
  };
  for (const sheet of document.styleSheets) {
    try { walk(sheet.cssRules); } catch { /* cross-origin sheet */ }
  }
  const rootStyle = getComputedStyle(document.documentElement);
  for (const name of rootStyle) {
    if (name.startsWith("--") && Object.keys(vars).length < 5000) vars[name] = rootStyle.getPropertyValue(name).trim();
  }
  // Inline SVG logos often paint with var(--…) or classes; bake computed paint in so the file stands alone.
  const standalone = (svg) => {
    const copy = svg.cloneNode(true);
    const src = [svg, ...svg.querySelectorAll("*")], dst = [copy, ...copy.querySelectorAll("*")];
    src.forEach((el, i) => {
      const s = getComputedStyle(el);
      for (const p of ["fill", "stroke", "opacity", "fill-opacity", "stroke-width"]) {
        const v = s.getPropertyValue(p);
        if (v) dst[i].setAttribute(p, v);
      }
    });
    if (!copy.getAttribute("xmlns")) copy.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    return copy.outerHTML;
  };

  // Colour by visible area (backgrounds) and by text mass (ink), the evidence for roles.
  const bg = {}, ink = {}, accents = {};
  const add = (map, key, w) => { if (key && !/rgba\(0, 0, 0, 0\)|transparent/.test(key)) map[key] = (map[key] || 0) + w; };
  for (const el of document.querySelectorAll("body *")) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    add(bg, s.backgroundColor, Math.min(r.width * r.height, 4e6));
    const own = [...el.childNodes].filter((n) => n.nodeType === 3).map((n) => n.textContent.trim()).join("");
    if (own) add(ink, s.color, own.length * parseFloat(s.fontSize));
    if (el.matches("a, button, [role=button], [class*=btn], [class*=button]")) add(accents, s.backgroundColor, 1), add(accents, s.color, 1);
  }
  add(bg, getComputedStyle(document.body).backgroundColor, innerWidth * innerHeight);
  const top = (m, n) => Object.entries(m).sort((a, b) => b[1] - a[1]).slice(0, n).map(([color, weight]) => ({ color, weight: Math.round(weight) }));

  const abs = (u) => { try { return new URL(u, location.href).href; } catch { return null; } };
  const logos = [];
  const header = document.querySelector("header, [role=banner], nav") || document.body;
  for (const el of header.querySelectorAll("svg, img")) {
    if (!visible(el)) continue;
    const r = el.getBoundingClientRect();
    if (r.top > 200 || r.width > 600 || (r.width < 20 && r.height < 20)) continue;
    const label = (el.getAttribute("alt") || el.getAttribute("aria-label") || el.closest("a")?.getAttribute("aria-label") || "").slice(0, 80);
    if (el.tagName === "IMG") logos.push({ kind: "header-img", src: abs(el.currentSrc || el.src), label, box: [r.width, r.height].map(Math.round) });
    else logos.push({ kind: "header-svg", svg: standalone(el).slice(0, 200000), label, box: [r.width, r.height].map(Math.round) });
    if (logos.length >= 4) break;
  }
  for (const l of document.querySelectorAll("link[rel*=icon], link[rel=apple-touch-icon], link[rel=mask-icon]")) logos.push({ kind: l.rel, src: abs(l.href) });
  const og = document.querySelector("meta[property='og:image']")?.content;
  if (og) logos.push({ kind: "og:image", src: abs(og) });

  const text = (sel, n) => [...document.querySelectorAll(sel)].filter(visible).map((e) => e.innerText.trim().replace(/\s+/g, " ")).filter(Boolean).slice(0, n);
  const links = [...document.querySelectorAll("a[href]")].map((a) => ({ text: a.innerText.trim().slice(0, 60), href: abs(a.getAttribute("href")) }))
    .filter((l) => l.href && new URL(l.href).origin === location.origin && !l.href.includes("#"));

  return {
    url: location.href,
    title: document.title,
    description: document.querySelector("meta[name=description]")?.content || "",
    theme_color: document.querySelector("meta[name=theme-color]")?.content || "",
    custom_properties: Object.fromEntries(Object.entries(vars)
      .sort(([a], [b]) => Number(/brand|primary|accent|color|colour|font|radius/i.test(b)) - Number(/brand|primary|accent|color|colour|font|radius/i.test(a)))
      .slice(0, 300)),
    custom_properties_total: Object.keys(vars).length,
    type: { body: css(document.body, TYPE), h1: css(pick("h1"), TYPE), h2: css(pick("h2"), TYPE), h3: css(pick("h3"), TYPE), p: css(pick("main p, p"), TYPE), a: css(pick("main a, a"), TYPE), nav: css(pick("nav a"), TYPE) },
    components: { button: css(pick("button, [role=button], a[class*=btn], a[class*=button]"), BOX), input: css(pick("input[type=text], input[type=email], input:not([type])"), BOX), card: css(pick("[class*=card]"), BOX) },
    colors: { backgrounds: top(bg, 10), text: top(ink, 8), interactive: top(accents, 8) },
    fonts_loaded: [...new Set([...document.fonts].filter((f) => f.status === "loaded").map((f) => `${f.family.replace(/"/g, "")} ${f.weight} ${f.style}`))],
    logos,
    copy: { h1: text("h1", 3), h2: text("h2", 8), buttons: text("button, a[class*=btn], a[class*=button]", 10), paragraphs: text("main p, p", 8).map((p) => p.slice(0, 400)) },
    links: links.slice(0, 80),
  };
}

const PRIORITY = /about|company|story|brand|press|media|design|product|pricing|features/i;

async function main() {
  const f = flags(process.argv.slice(2));
  const assets = join(f.out, "assets");
  mkdirSync(assets, { recursive: true });
  const browser = await puppeteer.launch({ executablePath: BROWSER, headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars"] });
  const fonts = new Map();
  const result = { started: new Date().toISOString(), requested: f.url, pages: [], fonts: [], assets: [], errors: [] };
  try {
    const visit = async (url, i) => {
      const page = await browser.newPage();
      page.on("response", async (res) => {
        const u = res.url();
        if (!FONT_EXT.test(u) || fonts.has(u) || fonts.size >= 24) return;
        try {
          const buf = await res.buffer();
          if (buf.length <= MAX_ASSET) {
            const name = `font-${fonts.size + 1}-${decodeURIComponent(u.split("/").pop().split(/[?#]/)[0]).replace(/[^\w.-]/g, "_")}`;
            fonts.set(u, name);
            writeFileSync(join(assets, name), buf);
          }
        } catch { /* redirects and opaque responses carry no body */ }
      });
      await page.setViewport({ width: 1440, height: 900 });
      await page.goto(url, { waitUntil: "networkidle2", timeout: 45000 });
      await new Promise((r) => setTimeout(r, 1200));
      const data = await page.evaluate(readPage);
      const shot = `page-${i}-desktop.png`;
      await page.screenshot({ path: join(f.out, shot), clip: { x: 0, y: 0, width: 1440, height: Math.min(await page.evaluate(() => document.documentElement.scrollHeight), 3600) } });
      await page.setViewport({ width: 390, height: 844, isMobile: true, deviceScaleFactor: 2 });
      await new Promise((r) => setTimeout(r, 600));
      const mobile = `page-${i}-mobile.png`;
      await page.screenshot({ path: join(f.out, mobile) });
      await page.close();
      return { ...data, screenshots: [shot, mobile] };
    };
    const home = await visit(f.url, 1);
    result.pages.push(home);
    const next = [...new Map(home.links.filter((l) => PRIORITY.test(l.href + " " + l.text)).map((l) => [l.href.replace(/\/$/, ""), l])).values()]
      .filter((l) => l.href.replace(/\/$/, "") !== home.url.replace(/\/$/, ""))
      .slice(0, f.pages - 1);
    for (const [i, l] of next.entries()) {
      try { result.pages.push(await visit(l.href, i + 2)); } catch (e) { result.errors.push(`${l.href}: ${e.message.slice(0, 200)}`); }
    }
    // Logo files: inline SVGs are written as-is; linked ones are downloaded by the browser.
    const page = await browser.newPage();
    let n = 0;
    for (const logo of home.logos) {
      n += 1;
      if (logo.svg) {
        const name = `logo-${n}.svg`;
        writeFileSync(join(assets, name), logo.svg);
        delete logo.svg;
        logo.file = `assets/${name}`;
        continue;
      }
      try {
        const res = await page.goto(logo.src, { timeout: 20000 });
        const buf = await res.buffer();
        const type = (res.headers()["content-type"] || "").split(";")[0];
        const ext = { "image/svg+xml": "svg", "image/png": "png", "image/jpeg": "jpg", "image/webp": "webp", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico" }[type];
        if (ext && buf.length <= MAX_ASSET) {
          const name = `logo-${n}.${ext}`;
          writeFileSync(join(assets, name), buf);
          logo.file = `assets/${name}`;
        }
      } catch (e) { result.errors.push(`logo ${logo.src}: ${e.message.slice(0, 120)}`); }
    }
    result.fonts = [...fonts.entries()].map(([url, file]) => ({ url, file: `assets/${file}` }));
  } finally {
    await browser.close();
  }
  writeFileSync(join(f.out, "capture.json"), JSON.stringify(result, null, 1));
  console.log(`__CAPTURE_OK__ ${result.pages.length} page(s), ${result.fonts.length} font file(s), ${result.pages[0].logos.filter((l) => l.file).length} logo file(s)`);
}

main().catch((e) => {
  console.error(`capture: ${e.message}`);
  process.exit(2);
});
