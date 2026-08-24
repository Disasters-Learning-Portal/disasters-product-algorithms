# `jupyterlab-bxplorer` Bootstrap CSS leak — diagnosis + unposted report drafts

> ## ⚠ CORRECTION (2026-08-11) — read before using either draft
>
> **This is NOT the cause of 2i2c-org/infrastructure#8770** ("chevron icons everywhere").
> That bug is `maap-dps-jupyter-extension` + `maap_algorithms_jupyter_extension`, which ship
> `overflow: scroll !important` — `scroll` rather than `auto` forces a scrollbar onto elements
> with nothing to scroll, so Lab's few-px-tall chrome paints only the stepper arrows. The
> working fix was documented in that issue's comments on 2026-07-20 (`jupyter labextension
> uninstall …`); an early WebFetch of the issue reported "no comments are present" when there
> were six, and this file was written on that false premise.
>
> **What survives:** the Bootstrap finding below is real and verified from the wheel, and
> removing bxplorer (PR #94) stands on its own merits. Only the *attribution* was wrong.
>
> **Before posting:** Draft A is still accurate — it never mentions #8770 as caused by
> bxplorer, so it can go as-is. **Draft B asserts the wrong root cause and must be rewritten
> or dropped.** See `.clinerules.md` rule 39.

**Status: NOT POSTED.** Both drafts below are held deliberately. Nothing has been filed with
Navteca and nothing has been added to the 2i2c issue. Post only on an explicit decision to do so.

**Why it's kept:** the extension was removed from `image/environment.yml` on 2026-08-11, which
fixes the Disasters Hub but leaves the upstream defect in place. If we ever want the S3-browser
panel back, or another hub hits the same symptom, this is the finished write-up — no need to
re-derive it from the wheel.

Full engineering rationale lives in `.clinerules.md` rule 39. This file is the *reportable*
version: same facts, written for an audience outside the repo.

---

## Summary of the defect

`jupyterlab-bxplorer` imports Bootstrap 5's full dist stylesheet into JupyterLab's **global**
stylesheet, so Bootstrap's Reboot applies to the entire Lab application rather than the
BXplorer panel.

Verified against `jupyterlab_bxplorer-0.2.30-py3-none-any.whl`:

| Fact | Evidence |
|---|---|
| Two unscoped import sites | `style/index.css` → `@import '../node_modules/bootstrap/dist/css/bootstrap.min.css';` and `src/components/DownloadComponent.tsx` → `import "../../node_modules/bootstrap/dist/css/bootstrap.min.css"` |
| Loaded eagerly, not on panel open | The 7.5 MB `vendors-…style-loader…injectStylesIntoStyleTag….js` chunk is in `remoteEntry`'s initial load graph; `lib_index_js` has **zero** `__webpack_require__.e` dynamic imports |
| Injected globally | webpack `style-loader` appends it to `document.head` at Lab startup |
| Reboot is present verbatim | `*,::after,::before{box-sizing:border-box}` and `body{margin:0;font-family:var(--bs-body-font-family);font-size:var(--bs-body-font-size);line-height:var(--bs-body-line-height);…}`, plus 7641 `--bs-*` declarations and bare-element rules on `h1-h6, hr, p, ul, a, button, svg, table, code, pre` |

**Failure chain:** Lab's `body` is `--jp-ui-font-size1` ≈ 13px → Reboot overrides it to 16px/1.5
at *equal specificity* → Lab UI text grows ~23% → every fixed-height piece of Lab chrome
overflows its `overflow: auto` box → Chrome paints real scrollbars → each box is a few px short,
leaving no room for a track or thumb, so **only the stepper arrows render**. Those "chevrons"
are scrollbar buttons, not icons. Chrome-only: Firefox and macOS overlay scrollbars have no
stepper arrows.

**Intermittency (inferred, not read from source):** Lab core CSS and federated-extension CSS are
both injected as runtime `<style>` tags, so equal-specificity `body{}` rules are decided by which
tag is appended last, and a 7.5 MB federated chunk's arrival order varies with cache state and
network jitter.

**One-line diagnostic**, DevTools console on an affected tab:

```js
getComputedStyle(document.body).fontSize   // "16px" = Bootstrap won (broken); "13px" = fine
```

---

## Draft A — new issue on `Navteca/jupyterlab-bxplorer`

> **Title:** Bootstrap's dist CSS is imported unscoped and breaks the host JupyterLab UI

````markdown
`jupyterlab-bxplorer` imports Bootstrap 5's full dist stylesheet into JupyterLab's *global*
stylesheet, so Bootstrap's Reboot applies to the entire Lab application, not just the BXplorer
panel.

Two import sites:

    style/index.css                       @import '../node_modules/bootstrap/dist/css/bootstrap.min.css';
    src/components/DownloadComponent.tsx  import "../../node_modules/bootstrap/dist/css/bootstrap.min.css"

In `jupyterlab_bxplorer-0.2.30-py3-none-any.whl` this ends up in a 7.5 MB chunk
(`vendors-…style-loader…injectStylesIntoStyleTag….js`) that `remoteEntry` pulls into the
extension's initial load graph — `lib_index_js` has zero `__webpack_require__.e` calls — so
`style-loader` injects it into `document.head` at Lab startup on every page load, whether or not
the panel is opened. The injected CSS includes Reboot verbatim on bare selectors:

```css
*,::after,::before{box-sizing:border-box}
body{margin:0;font-family:var(--bs-body-font-family);font-size:var(--bs-body-font-size);/*1rem*/
     line-height:var(--bs-body-line-height);color:…;background-color:…}
```

plus 7641 `--bs-*` declarations and bare-element rules on `h1-h6, hr, p, ul, a, button, svg,
table, code, pre`.

**Effect:** JupyterLab's `body` is `--jp-ui-font-size1` ≈ 13px; Reboot overrides it to 16px/1.5 at
equal specificity. Lab's UI text grows ~23%, every fixed-height piece of Lab chrome overflows its
`overflow: auto` box, and Chrome paints scrollbars with no room for a track or thumb — so all
that renders is stepper arrows, on the menu bar, every toolbar button, the breadcrumb, the tab
bar. The menu bar is also clipped. Chrome-specific, since Firefox and macOS overlay scrollbars
have no stepper arrows. Screenshot attached.

Check on any affected tab: `getComputedStyle(document.body).fontSize` → `16px`.

**Suggested fixes:** drop Bootstrap (the package already ships MUI 7 and Syncfusion EJ2), or
scope it — wrap the panel in a class and build Bootstrap with a prefix / `@scope`, or move to
MUI's `ScopedCssBaseline`. A JupyterLab extension's CSS should never set `body` or `*`.

We've removed the extension from our hub image for now and would happily re-add it once the CSS
is scoped.
````

**Before posting:** attach the operator screenshot showing the arrows across the menu bar, file
browser toolbar, breadcrumb, and tab bar. Re-check the version number — the diagnosis above is
pinned to 0.2.30 and should be re-verified against whatever is current at posting time (see
"Re-verifying" below).

---

## Draft B — comment on [2i2c-org/infrastructure#8770](https://github.com/2i2c-org/infrastructure/issues/8770)

The issue ("Weird UI issue with chevron icons everywhere", opened 2026-07-20) has the symptom and
a screenshot but no root cause and no comments.

```markdown
Root-caused this. It's not 2i2c infra — it's `jupyterlab-bxplorer`, which we had pip-installed in
the custom image.

It `@import`s Bootstrap 5's full dist CSS unscoped into Lab's global stylesheet
(`style/index.css` + `src/components/DownloadComponent.tsx`), loaded eagerly at Lab startup via
`remoteEntry`. Bootstrap's Reboot then overrides `body{font-size}` from Lab's 13px to 16px at
equal specificity, every fixed-height piece of Lab chrome overflows, and Chrome paints scrollbars
too small to show a track or thumb — so you get nothing but the stepper arrows. Those "chevrons"
are scrollbar buttons. Chrome-only because Firefox/macOS overlay scrollbars have no stepper
arrows.

The intermittency is a cascade race: Lab core CSS and federated-extension CSS are both injected
as runtime `<style>` tags, so which `body{}` rule wins depends on which tag is appended last, and
a 7.5 MB federated chunk's arrival order varies with cache state. Diagnostic on an affected tab:
`getComputedStyle(document.body).fontSize` → `16px` = broken, `13px` = fine.

Fixed on our side by removing the extension from the image. Filed upstream at
Navteca/jupyterlab-bxplorer. Safe to close.
```

**Before posting:** the last line claims an upstream issue exists. Either file Draft A first and
link it, or cut that sentence.

---

## Re-verifying against a newer release

The whole diagnosis can be re-derived from the published wheel without installing anything:

```python
import json, urllib.request, io, zipfile, re

j = json.load(urllib.request.urlopen("https://pypi.org/pypi/jupyterlab-bxplorer/json"))
url = [f for f in j["urls"] if f["filename"].endswith(".whl")][0]["url"]
z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(url).read()))
print("version:", j["info"]["version"])

for n in z.namelist():
    if not n.endswith(".js") or n.endswith(".map") or "labextension/static" not in n:
        continue
    t = z.read(n).decode("utf8", "replace")
    if "--bs-body-font-family" in t:            # Bootstrap present
        print("BOOTSTRAP:", n.split("/")[-1])
        for m in re.finditer(r"\*,::after,::before\{|(?<![-\w.])body\{margin:0", t):
            print("   unscoped reset @", m.start(), "->", t[m.start():m.start()+120])
```

If a future release shows no hits, the leak is fixed and the extension can be reconsidered —
re-add it to `image/environment.yml`, rebuild the dev image, and run the hub check in
`.clinerules.md` rule 39 (hard-reload Chrome several times; `getComputedStyle(document.body).fontSize`
must be `13px` on every load, since the bug is cascade-order dependent and one clean load isn't proof).
