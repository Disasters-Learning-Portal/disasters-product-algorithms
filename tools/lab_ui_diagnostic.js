/*
 * JupyterLab UI diagnostic — captures why the Lab chrome is showing scrollbar
 * arrows / clipped menus, as a file you can attach to a bug report.
 *
 * HOW TO RUN
 *   1. Open the broken Lab tab in Chrome.
 *   2. DevTools (F12) -> Console.
 *   3. Paste this whole file, press Enter.
 *   4. It prints a summary and downloads `lab-ui-diagnostic.json`.
 *      (`copy(window.__labDiag)` also puts the JSON on your clipboard.)
 *
 * Captures: browser zoom + devicePixelRatio, the computed <body> typography vs
 * JupyterLab's own --jp-* variables, every stylesheet carrying a global
 * `*` / `html` / `body` rule, and per-element overflow state for each piece of
 * Lab chrome that is showing arrows.
 *
 * See .clinerules.md rule 39 and docs/HUB_DEPLOYMENT.md.
 */
(() => {
  const round = (n) => Math.round(n * 1000) / 1000;
  const r = { capturedAt: new Date().toISOString() };

  // --- browser / display state -------------------------------------------
  // Chrome zoom is sticky per-domain per-user, so it is a prime suspect for a
  // bug that "only some people" see on "only some days".
  r.browser = {
    userAgent: navigator.userAgent,
    platform: navigator.platform,
    devicePixelRatio: round(devicePixelRatio),
    innerSize: [innerWidth, innerHeight],
    outerSize: [outerWidth, outerHeight],
    // Rough zoom estimate. 1 means 100%. Unreliable with a sidebar/devtools
    // docked, so treat it as a hint, not proof — confirm via the Chrome menu.
    zoomEstimate: round(outerWidth / innerWidth),
    visualViewportScale: window.visualViewport ? round(visualViewport.scale) : null,
    forcedColors: matchMedia("(forced-colors: active)").matches,
    prefersReducedMotion: matchMedia("(prefers-reduced-motion: reduce)").matches,
    colorScheme: matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light",
  };

  // --- typography: did anything override Lab's baseline? ------------------
  const bodyCS = getComputedStyle(document.body);
  const rootCS = getComputedStyle(document.documentElement);
  r.typography = {
    bodyFontSize: bodyCS.fontSize, // expect ~13px; 16px means something reset it
    bodyFontFamily: bodyCS.fontFamily,
    bodyLineHeight: bodyCS.lineHeight,
    bodyBoxSizing: bodyCS.boxSizing,
    htmlFontSize: rootCS.fontSize,
    jpUiFontSize1: rootCS.getPropertyValue("--jp-ui-font-size1").trim(),
    jpUiFontFamily: rootCS.getPropertyValue("--jp-ui-font-family").trim(),
    bodyColorScheme: bodyCS.colorScheme,
    bodyScrollbarWidth: bodyCS.scrollbarWidth,
    bodyScrollbarColor: bodyCS.scrollbarColor,
  };

  // --- stylesheets carrying globally-scoped rules -------------------------
  const GLOBAL_SEL = /(^|,)\s*(\*|html|body)\s*(,|\{|$)/;
  r.globalStyleSheets = [];
  r.unreadableStyleSheets = [];
  [...document.styleSheets].forEach((sheet, i) => {
    let rules;
    try {
      rules = [...sheet.cssRules];
    } catch (e) {
      r.unreadableStyleSheets.push({ index: i, href: sheet.href, reason: String(e.name) });
      return;
    }
    const globals = [];
    const walk = (list) => {
      for (const rule of list) {
        if (rule.cssRules) { walk(rule.cssRules); continue; }
        if (rule.selectorText && GLOBAL_SEL.test(rule.selectorText)) {
          globals.push(rule.selectorText + " { " + (rule.style?.cssText || "").slice(0, 160) + " }");
        }
      }
    };
    walk(rules);
    if (globals.length) {
      const node = sheet.ownerNode;
      r.globalStyleSheets.push({
        index: i,
        href: sheet.href,
        ownerTag: node?.tagName,
        ownerId: node?.id || null,
        // style-loader tags carry no href — this is how you spot an extension.
        injectedAtRuntime: !sheet.href && node?.tagName === "STYLE",
        totalRules: rules.length,
        globalRuleCount: globals.length,
        globalRules: globals.slice(0, 12),
      });
    }
  });

  // --- per-element overflow state for the chrome showing arrows -----------
  const TARGETS = {
    menubar: "#jp-MainMenu, .lm-MenuBar",
    topPanel: "#jp-top-panel",
    tabbar: ".lm-TabBar",
    sidebarToolbar: ".jp-FileBrowser .jp-Toolbar, .jp-SideBar .jp-Toolbar",
    breadcrumbs: ".jp-FileBrowser-crumbs, .jp-BreadCrumbs",
    dirListingHeader: ".jp-DirListing-header",
    toolbarButton: ".jp-Toolbar-item",
  };
  r.elements = {};
  for (const [name, selector] of Object.entries(TARGETS)) {
    const el = document.querySelector(selector);
    if (!el) { r.elements[name] = { found: false, selector }; continue; }
    const cs = getComputedStyle(el);
    r.elements[name] = {
      found: true,
      selector,
      overflowX: cs.overflowX,
      overflowY: cs.overflowY,
      fontSize: cs.fontSize,
      boxSizing: cs.boxSizing,
      client: [el.clientWidth, el.clientHeight],
      scroll: [el.scrollWidth, el.scrollHeight],
      // true => content genuinely exceeds the box (a sizing problem)
      // false + visible arrows => scrollbars are being FORCED (overflow: scroll,
      // an OS/browser "always show scrollbars" setting, or a UA stylesheet)
      overflowing: el.scrollWidth > el.clientWidth || el.scrollHeight > el.clientHeight,
    };
  }

  // How wide is a scrollbar here? 0 => overlay scrollbars; >0 => classic.
  const probe = document.createElement("div");
  probe.style.cssText = "position:absolute;top:-9999px;width:100px;height:100px;overflow:scroll";
  document.body.appendChild(probe);
  r.scrollbarGutterPx = probe.offsetWidth - probe.clientWidth;
  probe.remove();

  // --- installed extensions (best effort) ---------------------------------
  try {
    r.labExtensions = [...document.querySelectorAll("script[src]")]
      .map((s) => s.src)
      .filter((s) => s.includes("labextensions"))
      .map((s) => s.split("labextensions/")[1]?.split("/static/")[0])
      .filter((v, i, a) => v && a.indexOf(v) === i);
  } catch (e) {
    r.labExtensions = "unavailable: " + String(e);
  }

  window.__labDiag = r;

  // --- summary + download -------------------------------------------------
  console.log("%cJupyterLab UI diagnostic", "font-weight:bold;font-size:14px");
  console.log("zoom estimate:", r.browser.zoomEstimate, "(1 = 100%)  dpr:", r.browser.devicePixelRatio);
  console.log("body font-size:", r.typography.bodyFontSize, " --jp-ui-font-size1:", r.typography.jpUiFontSize1);
  console.log("scrollbar gutter:", r.scrollbarGutterPx + "px", r.scrollbarGutterPx === 0 ? "(overlay)" : "(classic)");
  console.log("stylesheets with global * / html / body rules:", r.globalStyleSheets.length);
  console.table(
    Object.entries(r.elements)
      .filter(([, v]) => v.found)
      .map(([k, v]) => ({ element: k, overflowX: v.overflowX, overflowY: v.overflowY, client: v.client.join("x"), scroll: v.scroll.join("x"), overflowing: v.overflowing }))
  );

  const blob = new Blob([JSON.stringify(r, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "lab-ui-diagnostic.json";
  a.click();
  URL.revokeObjectURL(a.href);

  console.log("Saved lab-ui-diagnostic.json — also on `window.__labDiag`, `copy(window.__labDiag)` to clipboard.");
  return r;
})();
