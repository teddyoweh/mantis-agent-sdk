"""The single-page dashboard served by ``mantis serve``.

One self-contained HTML document — inline CSS + vanilla JS, no external assets,
no build step, works offline. ``__TOKEN__`` is substituted server-side with the
LAN access token (empty string in loopback mode). Kept as a module constant so
it ships in the wheel with the package.
"""

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mantis · dashboard</title>
<link rel="icon" type="image/svg+xml" href="/mantis.svg">
<style>
  /* ==========================================================================
     mantis serve — an instrument panel for a local agent runtime.
     Neutral surfaces, one accent. Elevation is a hairline border and a one-step
     background, never a shadow. Sans for the UI, mono only for the things that
     are literally text on this machine: ids, paths, model names, code.
     Tokens first; everything below reads them.
     ========================================================================== */
  :root {
    --bg: #fafafa; --panel: #ffffff; --panel-2: #f3f4f6; --fill: #eceef1; --hover: #f5f6f8;
    --line: rgba(0,0,0,.08); --line-2: rgba(0,0,0,.16);
    --ink: #111111; --ink-2: #4b5058; --ink-3: #7d8290;
    --accent: #2f8f3a; --accent-ink: #ffffff; --accent-soft: rgba(47,143,58,.12); --accent-line: rgba(47,143,58,.45);
    --ok: #2f8f3a; --warn: #c27a10; --bad: #d23f31; --info: #2f6fdd;
    --ok-soft: rgba(47,143,58,.12); --warn-soft: rgba(194,122,16,.13); --bad-soft: rgba(210,63,49,.12);
    --info-soft: rgba(47,111,221,.12); --tool-soft: rgba(0,0,0,.06);
    --user: #2f6fdd; --tool: #6b7280; --err: #d23f31; --caution: #c27a10; --caution-soft: rgba(194,122,16,.13);
    --radius: 10px; --r-sm: 7px;
    --sans: -apple-system, BlinkMacSystemFont, Inter, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    --t: 140ms cubic-bezier(.2,.7,.2,1);
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0a0b0d; --panel: #111316; --panel-2: #16181c; --fill: #1c1f24; --hover: #15171b;
      --line: rgba(255,255,255,.08); --line-2: rgba(255,255,255,.16);
      --ink: #ededed; --ink-2: #9a9ea6; --ink-3: #6e7380;
      --accent: #58c467; --accent-ink: #08130a; --accent-soft: rgba(88,196,103,.14); --accent-line: rgba(88,196,103,.5);
      --ok: #58c467; --warn: #e0a24a; --bad: #ee6a5e; --info: #6ea2ff;
      --ok-soft: rgba(88,196,103,.14); --warn-soft: rgba(224,162,74,.15); --bad-soft: rgba(238,106,94,.14);
      --info-soft: rgba(110,162,255,.14); --tool-soft: rgba(255,255,255,.06);
      --user: #6ea2ff; --tool: #9a9ea6; --err: #ee6a5e; --caution: #e0a24a; --caution-soft: rgba(224,162,74,.15);
      color-scheme: dark;
    }
  }
  :root[data-theme="dark"] {
    --bg: #0a0b0d; --panel: #111316; --panel-2: #16181c; --fill: #1c1f24; --hover: #15171b;
    --line: rgba(255,255,255,.08); --line-2: rgba(255,255,255,.16);
    --ink: #ededed; --ink-2: #9a9ea6; --ink-3: #6e7380;
    --accent: #58c467; --accent-ink: #08130a; --accent-soft: rgba(88,196,103,.14); --accent-line: rgba(88,196,103,.5);
    --ok: #58c467; --warn: #e0a24a; --bad: #ee6a5e; --info: #6ea2ff;
    --ok-soft: rgba(88,196,103,.14); --warn-soft: rgba(224,162,74,.15); --bad-soft: rgba(238,106,94,.14);
    --info-soft: rgba(110,162,255,.14); --tool-soft: rgba(255,255,255,.06);
    --user: #6ea2ff; --tool: #9a9ea6; --err: #ee6a5e; --caution: #e0a24a; --caution-soft: rgba(224,162,74,.15);
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: var(--sans); background: var(--bg); color: var(--ink);
    font-size: 13px; line-height: 1.5; -webkit-font-smoothing: antialiased;
    display: grid; grid-template-rows: 48px 1fr; height: 100vh; overflow: hidden;
  }
  a { color: var(--accent); text-decoration: none; }
  :focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent); border-radius: 6px; }
  ::selection { background: var(--accent-soft); }
  @media (prefers-reduced-motion: reduce) {
    * { animation-duration: .001ms !important; transition-duration: .001ms !important; }
  }

  /* ---- shell: one slim top bar ---- */
  #top { display: flex; align-items: center; gap: 14px; padding: 0 16px; background: var(--panel);
    border-bottom: 1px solid var(--line); min-width: 0; }
  .brand { display: flex; align-items: center; gap: 8px; flex: none; padding-right: 6px; }
  .brand img { width: 22px; height: 22px; display: block; }
  .brand span { font-weight: 700; font-size: 13.5px; letter-spacing: -.02em; }
  #nav { display: flex; align-items: stretch; gap: 2px; height: 48px; min-width: 0; overflow-x: auto;
    scrollbar-width: none; }
  #nav::-webkit-scrollbar { display: none; }
  #nav button { position: relative; display: inline-flex; align-items: center; gap: 6px; font: inherit;
    font-size: 13px; font-weight: 500; padding: 0 10px; border: 0; background: transparent;
    color: var(--ink-2); cursor: pointer; white-space: nowrap; transition: color var(--t); }
  #nav button .k { font-family: var(--mono); font-size: 10px; color: var(--ink-3); opacity: 0;
    transition: opacity var(--t); }
  #nav button:hover { color: var(--ink); }
  #nav button:hover .k { opacity: 1; }
  #nav button.on { color: var(--ink); font-weight: 600; }
  #nav button.on::after { content: ""; position: absolute; left: 8px; right: 8px; bottom: -1px; height: 2px;
    background: var(--accent); border-radius: 2px 2px 0 0; }
  .topr { margin-left: auto; display: flex; align-items: center; gap: 8px; flex: none; min-width: 0; }
  .railfoot { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--ink-2);
    min-width: 0; max-width: 34vw; }
  .rf-v { font-family: var(--mono); font-size: 12px; font-weight: 600; color: var(--ink);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rf-s { color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .rf-c, .rf-l, .kbd { display: none; }
  .tb { display: inline-flex; align-items: center; gap: 7px; height: 30px; padding: 0 10px; font: inherit;
    font-size: 12.5px; color: var(--ink-2); background: var(--panel-2); border: 1px solid var(--line);
    border-radius: 8px; cursor: pointer; white-space: nowrap; transition: border-color var(--t), color var(--t), background var(--t); }
  .tb:hover { border-color: var(--line-2); color: var(--ink); background: var(--hover); }
  .tb kbd { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); border: 1px solid var(--line);
    border-radius: 4px; padding: 0 4px; line-height: 1.5; }
  .tb.icon { width: 30px; padding: 0; justify-content: center; font-size: 14px; }
  .lan { font-family: var(--mono); font-size: 10.5px; font-weight: 600; letter-spacing: .04em;
    text-transform: uppercase; padding: 3px 8px; border-radius: 6px; border: 1px solid var(--line);
    color: var(--ink-3); white-space: nowrap; }
  .lan.on { color: var(--warn); border-color: var(--warn); background: var(--warn-soft); }
  .live { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); flex: none;
    animation: pulse 2.6s ease-in-out infinite; }
  .live.off { background: var(--ink-3); animation: none; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }

  main { overflow: hidden; min-height: 0; }
  .view { display: none; height: 100%; }
  .view.on { display: block; }
  .scroll { overflow-y: auto; height: 100%; }
  .page { max-width: 1120px; margin: 0 auto; padding: 26px 24px 70px; }
  .page.wide { max-width: 1360px; }

  /* ---- page furniture ---- */
  .page-h { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
  .page-t { font-size: 18px; font-weight: 600; letter-spacing: -.02em; margin: 0; display: flex;
    align-items: center; gap: 10px; }
  .count { font-family: var(--mono); font-size: 11px; font-weight: 600; color: var(--ink-2);
    background: var(--fill); padding: 1px 7px; border-radius: 6px; font-variant-numeric: tabular-nums; }
  .page-d { color: var(--ink-2); font-size: 13px; line-height: 1.55; max-width: 72ch; margin: 0 0 18px; }
  .page-d code, .mono { font-family: var(--mono); font-size: 11.5px; color: var(--ink-2);
    background: var(--fill); padding: 1px 5px; border-radius: 4px; }
  .page-a { margin-left: auto; display: flex; gap: 8px; align-items: center; flex: none; }
  .sec { margin-top: 26px; }
  .sec-t { font-family: var(--mono); font-size: 10px; font-weight: 600; letter-spacing: .1em;
    text-transform: uppercase; color: var(--ink-3); margin: 0 0 10px; display: flex; align-items: center; gap: 10px; }
  .sec-t .fp { font-weight: 400; letter-spacing: 0; text-transform: none; color: var(--ink-3); flex: none;
    max-width: 46%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sec-t::after { content: ""; flex: 1; height: 1px; background: var(--line); }

  /* THE SIGNATURE — the signal path: each node is a live count of what the
     agent is wired to right now. The only decorative element on the page. */
  .path { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; font-family: var(--mono);
    font-size: 12px; padding: 11px 14px; margin-bottom: 22px; border-radius: var(--radius);
    background: var(--panel); border: 1px solid var(--line); }
  .path .n { display: inline-flex; align-items: center; gap: 6px; color: var(--ink); padding: 3px 8px;
    border-radius: 6px; background: var(--accent-soft); transition: background var(--t), color var(--t); }
  .path .n b { font-weight: 700; color: var(--accent); }
  .path .n.dim { background: var(--fill); color: var(--ink-2); }
  .path .n.dim b { color: var(--ink); }
  .path .n.warn { background: var(--warn-soft); color: var(--warn); }
  .path .n.warn b { color: var(--warn); }
  .path .n.bad { background: var(--bad-soft); color: var(--bad); }
  .path .arw { color: var(--accent); opacity: .55; letter-spacing: .1em; font-size: 10px; }
  .path .n.clk { cursor: pointer; }
  .path .n.clk:hover { background: var(--accent-soft); color: var(--accent); }

  /* ---- cards & lists: hairline, one-step bg, accent when selected ---- */
  .card, .card2, .fam, .dpc, .setup, .trace, .ctxbox, .hero, .host, .selfhost-card, .comp, details.layer,
  .cfg, .list, .mtable, .browse { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); }
  .card { padding: 15px 16px; display: flex; flex-direction: column; gap: 10px; }
  .card2 { padding: 14px 16px 16px; }
  .card2 h3 { font-family: var(--mono); font-size: 10px; font-weight: 600; letter-spacing: .1em;
    text-transform: uppercase; color: var(--ink-3); margin: 0 0 3px; }
  .card2 .note2 { font-size: 12px; color: var(--ink-3); margin-bottom: 12px; }
  .card2 .note2 b, .note2 b { color: var(--ink-2); font-family: var(--mono); font-weight: 600; }
  .note2 { font-size: 12px; color: var(--ink-3); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
  .card .head { display: flex; align-items: center; gap: 8px; }
  .card .name { font-weight: 600; font-size: 13.5px; }
  .card .url { font-family: var(--mono); font-size: 11px; color: var(--ink-3); white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; }
  .card .note { font-size: 12px; color: var(--ink-3); line-height: 1.45; }
  .card.cur, .fam.cur, .dpc.cur { border-color: var(--accent-line); }
  .card.flash, .lrow.flash, .msg.flash { box-shadow: 0 0 0 2px var(--accent); }
  .list { padding: 4px; }
  .lrow { border-radius: var(--r-sm); }
  .lrow + .lrow { margin-top: 2px; }
  .lrow-top { display: flex; align-items: center; gap: 10px; padding: 11px 12px 11px 12px; cursor: pointer;
    border-radius: var(--r-sm); border: 1px solid transparent; transition: background var(--t), border-color var(--t); }
  .lrow-top:hover, .lrow.open .lrow-top { background: var(--hover); border-color: var(--line); }
  .lrow .nm { font-weight: 600; font-size: 13px; flex: none; }
  .lrow .sub { color: var(--ink-3); font-size: 12px; font-family: var(--mono); flex: 1; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .lrow .sub.sans { font-family: var(--sans); color: var(--ink-2); }
  .lrow .acts { display: flex; gap: 6px; flex: none; opacity: .7; transition: opacity var(--t); }
  .lrow:hover .acts, .lrow.open .acts { opacity: 1; }
  .lrow .caret { color: var(--ink-3); font-size: 8px; width: 9px; flex: none; transition: transform var(--t); }
  .lrow.open .caret { transform: rotate(90deg); }
  .lbody { display: none; padding: 6px 14px 16px 32px; }
  .lrow.open .lbody { display: block; animation: drawer .16s ease-out; }
  @keyframes drawer { from { opacity: 0; transform: translateY(-3px); } to { opacity: 1; transform: none; } }

  /* dots · tags · chips · buttons — one vocabulary */
  .dot2 { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--ink-3); }
  .dot2.ok { background: var(--ok); } .dot2.bad { background: var(--bad); } .dot2.warn { background: var(--warn); }
  .dot2.run { background: var(--ok); animation: pulse 1.6s ease-in-out infinite; }
  .dot2.pend { background: var(--warn); }
  .t2 { font-size: 10px; font-weight: 600; letter-spacing: .03em; text-transform: uppercase; padding: 2px 6px;
    border-radius: 5px; background: var(--fill); color: var(--ink-2); flex: none; white-space: nowrap; }
  .t2.acc { background: var(--ok-soft); color: var(--ok); }
  .t2.vio { background: var(--info-soft); color: var(--info); }
  .t2.blu { background: var(--info-soft); color: var(--info); }
  .t2.amb { background: var(--warn-soft); color: var(--warn); }
  .t2.red { background: var(--bad-soft); color: var(--bad); }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { font-family: var(--mono); font-size: 11px; padding: 3px 8px; border-radius: 6px;
    background: var(--fill); color: var(--ink-2); border: 1px solid transparent; }
  .chip.cur { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
  .chip.more { color: var(--ink-3); }
  .chip.clk { cursor: pointer; transition: border-color var(--t), color var(--t); }
  .chip.clk:hover { border-color: var(--accent-line); color: var(--accent); }
  .recent { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; }
  .pill { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--ink-2);
    background: var(--fill); border-radius: 6px; padding: 2px 7px; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .pill b { font-weight: 600; color: var(--ink); }
  .pill.acc { background: var(--ok-soft); color: var(--ok); } .pill.acc b { color: var(--ok); }
  .pill.mono { font-family: var(--mono); }
  .b { font: inherit; font-size: 12.5px; font-weight: 500; padding: 6px 12px; border-radius: 7px;
    border: 1px solid var(--line); cursor: pointer; white-space: nowrap; line-height: 1.2;
    background: var(--panel); color: var(--ink-2); transition: background var(--t), color var(--t), border-color var(--t); }
  .b:hover { border-color: var(--line-2); color: var(--ink); background: var(--hover); }
  .b.pri { background: var(--accent); color: var(--accent-ink); border-color: transparent; font-weight: 600; }
  .b.pri:hover { filter: brightness(1.07); background: var(--accent); color: var(--accent-ink); }
  .b.gho { background: transparent; border-color: transparent; }
  .b.gho:hover { background: var(--fill); border-color: transparent; color: var(--ink); }
  .b.dan:hover { color: var(--bad); border-color: var(--bad); background: var(--bad-soft); }
  .b.dan.pri, .b.armed { color: #fff; background: var(--bad); border-color: transparent; }
  .b:disabled { opacity: .5; cursor: default; filter: none; }
  .b.on { background: var(--accent-soft); color: var(--accent); border-color: var(--accent-line); }
  .btn { font: inherit; font-size: 12.5px; font-weight: 600; padding: 7px 14px; border: 0; border-radius: 7px;
    background: var(--accent); color: var(--accent-ink); cursor: pointer; white-space: nowrap; }
  .btn:hover { filter: brightness(1.07); }
  .btn:disabled { opacity: .5; cursor: default; }
  .btn.big { padding: 9px 16px; font-size: 13px; text-decoration: none; display: inline-block; }
  .a-link { color: var(--accent); font-size: 12.5px; }
  .a-link:hover { text-decoration: underline; }
  .guide-link { font: inherit; font-size: 12px; color: var(--accent); cursor: pointer; background: none;
    border: 0; padding: 0; text-align: left; }
  .guide-link:hover { text-decoration: underline; }
  .actions { display: flex; gap: 14px; }
  .actions button { background: none; border: 0; padding: 0; font: inherit; font-size: 12px; cursor: pointer;
    color: var(--ink-2); }
  .actions button:hover { color: var(--ink); }
  .actions button.danger:hover { color: var(--bad); }

  /* inputs */
  input.in, textarea.in, select.in { font: inherit; font-size: 12.5px; padding: 7px 10px; border: 1px solid var(--line);
    border-radius: 7px; background: var(--panel); color: var(--ink); min-width: 0; flex: 1;
    transition: border-color var(--t); }
  input.in[type=password], .mono-in { font-family: var(--mono); }
  input.in:hover, textarea.in:hover, select.in:hover { border-color: var(--line-2); }
  input.in:focus, textarea.in:focus, select.in:focus { outline: none; border-color: var(--accent);
    box-shadow: 0 0 0 2px var(--accent-soft); }
  input.in::placeholder, textarea.in::placeholder { color: var(--ink-3); }
  select.in { cursor: pointer; flex: none; }
  input.in.search { width: 100%; margin-bottom: 12px; }
  .find { position: relative; margin-bottom: 12px; }
  .find input { width: 100%; font: inherit; font-size: 13px; padding: 9px 12px 9px 32px; border: 1px solid var(--line);
    border-radius: 8px; background: var(--panel); color: var(--ink); transition: border-color var(--t); }
  .find input:hover { border-color: var(--line-2); }
  .find input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
  .find::before { content: "⌕"; position: absolute; left: 11px; top: 50%; transform: translateY(-50%);
    color: var(--ink-3); font-size: 14px; pointer-events: none; }
  label.chk { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--ink-2);
    cursor: pointer; white-space: nowrap; }
  .enable { display: flex; gap: 8px; }
  .filters { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
  .fchips { display: flex; gap: 3px; flex: none; background: var(--panel-2); border: 1px solid var(--line);
    border-radius: 8px; padding: 2px; }
  .fchip { font: inherit; font-size: 12px; padding: 5px 10px; border: 0; border-radius: 6px; background: transparent;
    color: var(--ink-2); cursor: pointer; white-space: nowrap; transition: background var(--t), color var(--t); }
  .fchip:hover { color: var(--ink); }
  .fchip { border: 1px solid transparent; }
  .fchip.on { background: var(--panel); color: var(--ink); font-weight: 600; border-color: var(--line); }

  /* empty states & skeletons */
  .zero { border: 1px dashed var(--line-2); border-radius: var(--radius); padding: 28px 22px; text-align: center; }
  .zero .zt { font-weight: 600; font-size: 13.5px; margin-bottom: 4px; }
  .zero .zd { font-size: 12.5px; color: var(--ink-3); max-width: 54ch; margin: 0 auto; line-height: 1.55; }
  .empty { color: var(--ink-3); padding: 36px 20px; text-align: center; font-size: 13px; }
  .sk { border-radius: 8px; background: linear-gradient(90deg, var(--fill) 25%, var(--panel-2) 50%, var(--fill) 75%);
    background-size: 200% 100%; animation: shimmer 1.2s linear infinite; height: 14px; margin: 8px 0; }
  .sk.card { height: 84px; border: 0; margin: 0; }
  .skgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; margin-top: 12px; }
  @keyframes shimmer { from { background-position: 200% 0; } to { background-position: -200% 0; } }

  /* key/value detail, code boxes, probes, banners */
  .kvs { display: grid; grid-template-columns: 104px 1fr; gap: 4px 14px; align-items: baseline; font-size: 12.5px;
    margin: 10px 0 0; }
  .kvs dt { color: var(--ink-3); font-size: 10.5px; text-transform: uppercase; letter-spacing: .04em; font-weight: 600;
    padding-top: 2px; }
  .kvs dd { margin: 0; font-family: var(--mono); font-size: 12px; word-break: break-word; }
  .kvs dd.wrap { white-space: pre-wrap; font-family: var(--sans); }
  .secret { color: var(--ink-3); letter-spacing: .12em; }
  .jsonbox { margin-top: 12px; }
  .jsonbox pre { margin: 0; background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px;
    padding: 11px 12px; font-family: var(--mono); font-size: 11.5px; line-height: 1.55; overflow: auto;
    max-height: 320px; white-space: pre; }
  .jsonbox .jh { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
  .jsonbox .jt { font-size: 10.5px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--ink-3); }
  .probe { margin-top: 12px; border-radius: 8px; padding: 11px 13px; font-size: 12.5px; background: var(--panel-2);
    border: 1px solid var(--line); border-left: 3px solid var(--ink-3); }
  .probe.ok { border-left-color: var(--ok); }
  .probe.bad { border-left-color: var(--bad); }
  .probe .ph2 { display: flex; align-items: center; gap: 8px; font-weight: 600; }
  .probe .pe, .pe { font-family: var(--mono); font-size: 11.5px; color: var(--bad); margin-top: 6px; word-break: break-word; }
  .toolgrid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .toolgrid .tk { font-family: var(--mono); font-size: 11px; background: var(--fill); color: var(--ink-2);
    padding: 3px 8px; border-radius: 6px; }
  .banner { display: flex; align-items: center; gap: 12px; padding: 11px 14px; border-radius: 9px; margin-bottom: 16px;
    font-size: 13px; background: var(--warn-soft); color: var(--warn); border: 1px solid transparent; }
  .banner b { font-weight: 600; }
  .banner code { font-family: var(--mono); font-size: 12px; }
  .banner .sp { flex: 1; }
  .comp { padding: 14px 16px; margin-bottom: 16px; display: none; }
  .comp.on { display: block; }
  .comp .r { display: flex; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
  .comp .r > * { flex: 1; min-width: 150px; }
  .comp .r > .fit { flex: none; min-width: 0; }
  .comp textarea.in { width: 100%; min-height: 118px; resize: vertical; line-height: 1.55; font-family: var(--mono); }
  .comp .foot { display: flex; align-items: center; gap: 12px; margin-top: 10px; }
  .comp .hint { font-size: 11.5px; color: var(--ink-3); flex: 1; line-height: 1.5; }
  .comp .hint code { font-family: var(--mono); background: var(--fill); padding: 1px 5px; border-radius: 4px; }
  .mark2 { width: 22px; height: 22px; border-radius: 6px; flex: none; display: inline-flex; align-items: center;
    justify-content: center; background: var(--fill); color: var(--ink); font-family: var(--mono); font-size: 11px;
    font-weight: 700; overflow: hidden; }
  .mark2 svg { width: 14px; height: 14px; display: block; }
  .refresh { display: inline-flex; align-items: center; gap: 6px; font-family: var(--mono); font-size: 10.5px;
    color: var(--ink-3); }

  /* ---- modal sheet ---- */
  #modal { position: fixed; inset: 0; background: rgba(0,0,0,.5); display: none; align-items: center;
    justify-content: center; padding: 20px; z-index: 30; backdrop-filter: blur(2px); }
  #modal.on { display: flex; }
  .sheet { position: relative; background: var(--panel); border: 1px solid var(--line-2); border-radius: 14px;
    max-width: 560px; width: 100%; max-height: 86vh; overflow-y: auto; padding: 22px 24px; animation: rise .16s ease-out; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .sheet.wide { max-width: 800px; }
  .sheet h3 { margin: 0 0 3px; font-size: 16px; font-weight: 600; letter-spacing: -.01em; }
  .sheet h4 { margin: 16px 0 6px; font-size: 10.5px; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-3); }
  .sheet .sub { color: var(--ink-3); font-size: 12px; font-family: var(--mono); margin-bottom: 14px; word-break: break-all; }
  .sheet ol { margin: 0; padding-left: 20px; }
  .sheet ol li { margin: 7px 0; font-size: 13px; line-height: 1.5; }
  .sheet .free { font-size: 12.5px; color: var(--ink-2); background: var(--panel-2); border: 1px solid var(--line);
    border-radius: 8px; padding: 10px 12px; margin: 14px 0; }
  .sheet .cta { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 16px; }
  .sheet .notes { list-style: none; padding: 0; margin: 12px 0 0; }
  .sheet .notes li { font-size: 12.5px; color: var(--ink-2); padding: 4px 0 4px 16px; position: relative; }
  .sheet .notes li::before { content: "·"; position: absolute; left: 4px; color: var(--accent); }
  .rt, .plat { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 11px 13px; margin: 8px 0; }
  .rt .rn, .plat-n { font-weight: 600; font-size: 13px; }
  .rt .rnote, .rnote { color: var(--ink-3); font-size: 12px; margin: 2px 0; }
  .rt code { display: block; font-family: var(--mono); font-size: 11.5px; background: var(--fill); padding: 7px 9px;
    border-radius: 6px; overflow-x: auto; white-space: pre; margin: 6px 0 2px; }
  .skill-box { background: var(--accent-soft); border-radius: 9px; padding: 13px 15px; margin: 16px 0; }
  .skill-t { font-weight: 600; font-size: 13px; margin-bottom: 4px; }
  .skill-b { font-size: 12.5px; color: var(--ink-2); line-height: 1.5; margin-bottom: 10px; }
  .plat-top { display: flex; align-items: center; gap: 9px; }
  .plat-k { font-size: 10px; color: var(--ink-3); background: var(--fill); padding: 2px 8px; border-radius: 6px; }
  .plat-links { display: flex; gap: 16px; margin-top: 8px; }
  .sheet .x { position: absolute; top: 12px; right: 14px; background: none; border: 0; font-size: 18px;
    color: var(--ink-3); cursor: pointer; line-height: 1; }
  .sheet .x:hover { color: var(--ink); }
  #toast { position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%) translateY(6px); background: var(--ink);
    color: var(--bg); padding: 10px 16px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity var(--t), transform var(--t);
    pointer-events: none; z-index: 40; max-width: 80vw; }
  #toast.on { opacity: 1; transform: translateX(-50%); }
  #toast.err { background: var(--bad); color: #fff; }

  /* ---- command palette (⌘K) ---- */
  #palette { position: fixed; inset: 0; background: rgba(0,0,0,.45); display: none; align-items: flex-start;
    justify-content: center; padding: 12vh 16px 0; z-index: 35; backdrop-filter: blur(2px); }
  #palette.on { display: flex; }
  .pal { width: 100%; max-width: 620px; background: var(--panel); border: 1px solid var(--line-2); border-radius: 12px;
    overflow: hidden; animation: rise .14s ease-out; }
  .pal input { width: 100%; font: inherit; font-size: 15px; padding: 14px 16px; border: 0; background: transparent;
    color: var(--ink); border-bottom: 1px solid var(--line); }
  .pal input:focus { outline: none; box-shadow: none; }
  .pal-list { max-height: 52vh; overflow-y: auto; padding: 6px; }
  .pal-g { font-family: var(--mono); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-3);
    padding: 8px 10px 4px; }
  .pal-i { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 7px; cursor: pointer;
    font-size: 13px; }
  .pal-i.on, .pal-i:hover { background: var(--accent-soft); }
  .pal-i .pk { font-family: var(--mono); font-size: 10px; color: var(--ink-3); background: var(--fill); padding: 1px 6px;
    border-radius: 4px; flex: none; }
  .pal-i .pt { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pal-i .ps { color: var(--ink-3); font-size: 11.5px; margin-left: auto; white-space: nowrap; font-family: var(--mono); }
  .pal-f { display: flex; gap: 14px; padding: 8px 14px; border-top: 1px solid var(--line); font-family: var(--mono);
    font-size: 10.5px; color: var(--ink-3); }
  .pal-f b { color: var(--ink-2); background: var(--fill); padding: 0 5px; border-radius: 4px; font-weight: 600; }
  .pal-none { padding: 22px; text-align: center; color: var(--ink-3); font-size: 12.5px; }

  /* ==========================================================================
     SESSIONS — three columns; projects and sessions are cards you can scan.
     ========================================================================== */
  #sessions.on { display: grid; grid-template-columns: 280px 320px minmax(0,1fr); height: 100%; }
  .col { overflow-y: auto; height: 100%; min-width: 0; border-right: 1px solid var(--line); background: var(--bg); }
  .col:last-child { border-right: 0; }
  .col-head { position: sticky; top: 0; z-index: 2; background: var(--bg); padding: 14px 14px 8px;
    font-family: var(--mono); font-size: 10px; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-3); }
  .colfind { padding: 0 12px 8px; position: sticky; top: 34px; background: var(--bg); z-index: 2; }
  .colfind input { width: 100%; font: inherit; font-size: 12.5px; padding: 7px 10px; border: 1px solid var(--line);
    border-radius: 7px; background: var(--panel); color: var(--ink); }
  .colfind input:focus { outline: none; border-color: var(--accent); box-shadow: 0 0 0 2px var(--accent-soft); }
  .cards { display: flex; flex-direction: column; gap: 8px; padding: 0 12px 16px; }
  .pcard { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 12px 13px;
    cursor: pointer; transition: border-color var(--t), background var(--t); min-width: 0; }
  .pcard:hover { border-color: var(--line-2); background: var(--hover); }
  .pcard.on { border-color: var(--accent); background: var(--accent-soft); }
  .pcard .t { font-weight: 600; font-size: 13px; line-height: 1.35; overflow: hidden; text-overflow: ellipsis;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; word-break: break-word; }
  .pcard .s { color: var(--ink-3); font-size: 11px; margin-top: 3px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; font-family: var(--mono); }
  .pcard .s.sans { font-family: var(--sans); color: var(--ink-2); font-size: 12px; }
  .pcard .m { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
  .row { display: none; }
  .path.old { display: none; }

  /* transcript */
  #transcript { padding: 20px 26px 60px; max-width: 920px; margin: 0 auto; }
  .conv-head { margin-bottom: 14px; }
  .conv-head h2 { font-size: 16px; font-weight: 600; margin: 0 0 3px; letter-spacing: -.01em; }
  .conv-head .sub { color: var(--ink-3); font-size: 12px; font-family: var(--mono); }
  .msg { display: grid; grid-template-columns: 84px minmax(0,1fr); gap: 8px 12px; padding: 12px 0;
    border-top: 1px solid var(--line); }
  .msg:first-of-type { border-top: 0; }
  .who { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; font-size: 11px; color: var(--ink-3); }
  .rc { font-size: 10px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; padding: 2px 7px;
    border-radius: 5px; background: var(--fill); color: var(--ink-2); }
  .msg.user .rc { background: var(--info-soft); color: var(--user); }
  .msg.assistant .rc { background: var(--ok-soft); color: var(--ok); }
  .msg.system .rc { background: var(--warn-soft); color: var(--warn); }
  .who .ts, .who .tc { font-size: 10.5px; color: var(--ink-3); font-family: var(--mono); }
  .mbody { min-width: 0; font-size: 13.5px; line-height: 1.6; }
  .text { white-space: pre-wrap; word-wrap: break-word; }
  .md p { margin: 0 0 8px; } .md p:last-child { margin: 0; }
  .md ul, .md ol { margin: 4px 0 8px; padding-left: 22px; }
  .md li { margin: 2px 0; }
  .md h1, .md h2, .md h3, .md h4 { font-size: 13.5px; font-weight: 600; margin: 10px 0 4px; }
  .md code { font-family: var(--mono); font-size: 12px; background: var(--fill); padding: 1px 5px; border-radius: 4px; }
  .md pre { margin: 6px 0 8px; background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 12px; overflow-x: auto; }
  .md pre code { background: none; padding: 0; font-size: 12px; line-height: 1.5; white-space: pre; }
  .md blockquote { margin: 4px 0 8px; padding-left: 12px; border-left: 2px solid var(--line-2); color: var(--ink-2); }
  .thinking { border-left: 2px solid var(--line-2); padding: 2px 0 2px 12px; color: var(--ink-2); font-style: italic;
    white-space: pre-wrap; margin: 6px 0; font-size: 12.5px; }
  .ctxtog { font: inherit; font-size: 11px; color: var(--ink-3); background: var(--fill); border: 0; border-radius: 5px;
    padding: 2px 8px; cursor: pointer; margin: 4px 0; }
  .ctxtog:hover { color: var(--ink); }
  .ctxbody { display: none; margin: 6px 0 4px; }
  .ctxbody.on { display: block; }
  .ctxbody pre { margin: 0; font-family: var(--mono); font-size: 11.5px; white-space: pre-wrap; word-break: break-word;
    color: var(--ink-3); background: var(--panel-2); border: 1px dashed var(--line-2); border-radius: 8px;
    padding: 10px 12px; max-height: 300px; overflow: auto; }
  .tcall { border: 1px solid var(--line); border-radius: 8px; margin: 6px 0; overflow: hidden; background: var(--panel); }
  .tcall .th { display: flex; align-items: center; gap: 8px; padding: 7px 11px; font-family: var(--mono); font-size: 12px;
    cursor: pointer; user-select: none; color: var(--ink-2); transition: background var(--t); }
  .tcall .th:hover { background: var(--hover); }
  .tcall .th .tn { color: var(--ink); font-weight: 600; }
  .tcall .th .ta { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .tcall .th .sz { margin-left: auto; color: var(--ink-3); font-size: 11px; white-space: nowrap; }
  .tcall .th .tg { font-size: 9px; color: var(--ink-3); transition: transform var(--t); }
  .tcall.open .th .tg { transform: rotate(90deg); }
  .tcall.err .th .tn { color: var(--bad); }
  .tcall .tb2 { display: none; border-top: 1px solid var(--line); }
  .tcall.open .tb2 { display: block; }
  .tcall .tl2 { font-family: var(--mono); font-size: 10px; letter-spacing: .08em; text-transform: uppercase;
    color: var(--ink-3); padding: 7px 11px 0; }
  .tcall pre { margin: 0; padding: 6px 11px 10px; overflow-x: auto; font-family: var(--mono); font-size: 11.5px;
    white-space: pre-wrap; word-break: break-word; max-height: 340px; color: var(--ink-2); }
  .tcall.err pre.res { color: var(--bad); }
  .block { border: 1px solid var(--line); border-radius: 8px; margin: 6px 0; background: var(--panel); overflow: hidden; }
  .block .bh { padding: 6px 11px; font-family: var(--mono); font-size: 12px; display: flex; gap: 8px; align-items: center; }
  .block pre { margin: 0; padding: 8px 11px; overflow-x: auto; font-family: var(--mono); font-size: 11.5px;
    white-space: pre-wrap; word-break: break-word; max-height: 340px; }
  .badge { font-size: 10px; padding: 1px 6px; border-radius: 5px; background: var(--fill); color: var(--ink-3);
    font-family: var(--mono); }
  .compact { border-radius: 8px; background: var(--warn-soft); color: var(--warn); padding: 8px 12px; font-size: 12px;
    font-family: var(--mono); }
  .conv-stats { margin: 0 0 12px; }
  .ctxbox { padding: 12px 14px 8px; margin-bottom: 16px; }
  .ctxbox .ch { display: flex; align-items: baseline; gap: 10px; font-family: var(--mono); font-size: 10px;
    letter-spacing: .1em; text-transform: uppercase; color: var(--ink-3); font-weight: 600; }
  .ctxbox .ch span:last-child { margin-left: auto; text-transform: none; letter-spacing: 0; font-weight: 400; }
  .ctxsvg { width: 100%; height: auto; display: block; margin-top: 6px; }
  .ctxsvg .bar { fill: var(--accent); opacity: .55; transition: opacity var(--t); }
  .ctxsvg .bar:hover, .ctxsvg .bar.on { opacity: 1; }
  .ctxsvg .cap { stroke: var(--bad); stroke-width: 1; stroke-dasharray: 3 3; }
  .ctxsvg .cost { fill: none; stroke: var(--info); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
  .ctxsvg text { fill: var(--ink-3); font-family: var(--mono); font-size: 9px; }

  /* ==========================================================================
     OVERVIEW — readouts, the trace, the punchcard, the spectrum, the ledgers.
     ========================================================================== */
  .lcd { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 22px; font-size: 12px; color: var(--ink-3);
    margin: 0 0 18px; }
  .lcd i { font-style: normal; color: var(--ink); font-weight: 600; font-size: 15px; letter-spacing: -.02em;
    font-variant-numeric: tabular-nums; margin-right: 5px; font-family: var(--mono); }
  .lcd .hot i { color: var(--accent); }
  .lcd.tight { margin: 4px 0 10px; gap: 0 18px; }
  .lcd .dim i { color: var(--ink-2); }
  .trace { position: relative; padding: 14px 16px 8px; margin-bottom: 12px; }
  .trace-h { display: flex; align-items: baseline; gap: 12px; margin-bottom: 4px; }
  .trace-t { font-family: var(--mono); font-size: 10px; font-weight: 600; letter-spacing: .1em; text-transform: uppercase;
    color: var(--ink-3); }
  .trace-pk { margin-left: auto; font-family: var(--mono); font-size: 11.5px; color: var(--ink-2); }
  .trace-pk b { color: var(--ink); }
  .trace svg { display: block; width: 100%; height: auto; }
  .trace .env { fill: var(--accent); opacity: .1; }
  .trace .sig { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round;
    vector-effect: non-scaling-stroke; }
  .trace .raw { fill: none; stroke: var(--accent); stroke-width: 1; opacity: .3; vector-effect: non-scaling-stroke; }
  .trace .pk { stroke: var(--ink-3); stroke-width: 1; stroke-dasharray: 2 3; }
  .trace .pkd { fill: var(--accent); }
  .trace .base { stroke: var(--line-2); stroke-width: 1; }
  .trace text { fill: var(--ink-3); font-family: var(--mono); font-size: 9px; }
  @media (prefers-reduced-motion: no-preference) {
    .trace .sig { animation: draw 1.15s cubic-bezier(.22,.7,.2,1) forwards; }
    .trace .env, .trace .raw { animation: fadein .8s .35s both ease-out; }
    @keyframes draw { to { stroke-dashoffset: 0; } }
    @keyframes fadein { from { opacity: 0; } }
  }
  .duo { display: grid; grid-template-columns: 1.15fr 1fr; gap: 12px; }
  @media (max-width: 980px) { .duo { grid-template-columns: 1fr; } }
  .punch { width: 100%; height: auto; display: block; }
  .punch .cell { fill: var(--accent); }
  .punch text { fill: var(--ink-3); font-family: var(--mono); font-size: 8.5px; }
  .spec { display: flex; height: 10px; border-radius: 5px; overflow: hidden; gap: 2px; margin-bottom: 14px; }
  .spec i { display: block; background: var(--accent); }
  .tl { display: grid; grid-template-columns: 1fr auto auto; gap: 3px 12px; align-items: baseline; font-size: 12px; }
  .tl .tn { color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); }
  .tl .tc { color: var(--ink-3); font-variant-numeric: tabular-nums; }
  .tl .tp { color: var(--ink-2); font-variant-numeric: tabular-nums; text-align: right; min-width: 34px; }
  .tl .sw { width: 8px; height: 8px; border-radius: 2px; background: var(--accent); display: inline-block; margin-right: 8px; }
  .plist { display: flex; flex-direction: column; gap: 4px; margin-top: 2px; }
  .prow { position: relative; display: flex; align-items: center; gap: 10px; padding: 9px 12px; border-radius: 8px;
    font-size: 12.5px; overflow: hidden; border: 1px solid var(--line); background: var(--panel); }
  .prow .fillbar { position: absolute; left: 0; top: 0; bottom: 0; background: var(--accent); opacity: .08; }
  .prow .pn { position: relative; font-weight: 600; }
  .prow .pp { position: relative; color: var(--ink-3); font-size: 11px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; font-family: var(--mono); }
  .prow .pv { position: relative; margin-left: auto; color: var(--ink-2); white-space: nowrap; font-variant-numeric: tabular-nums;
    font-family: var(--mono); font-size: 11.5px; }
  .prow .pf { position: relative; } .prow .pf .mark2 { width: 18px; height: 18px; border-radius: 5px; }
  .prow .pf .mark2 svg { width: 11px; height: 11px; }
  .prow .t2 { position: relative; }
  .fam-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; margin-bottom: 12px; }
  .fam { padding: 12px 13px 11px; display: flex; flex-direction: column; gap: 6px; min-width: 0; position: relative;
    cursor: pointer; transition: border-color var(--t), background var(--t); }
  .fam:hover { border-color: var(--line-2); background: var(--hover); }
  .fam .fh { display: flex; align-items: center; gap: 8px; }
  .fam .mark2 { width: 26px; height: 26px; } .fam .mark2 svg { width: 16px; height: 16px; }
  .fam .fn { font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fam .fa { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--ink-2); white-space: nowrap; overflow: hidden; }
  .fam .fm { font-family: var(--mono); font-size: 11.5px; color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fam .fm.none { color: var(--ink-3); font-style: italic; font-family: var(--sans); }
  .fam .ff { display: flex; align-items: center; gap: 6px; margin-top: auto; }
  .fam .ff .b { padding: 4px 9px; font-size: 11.5px; }
  .fam .ff .pr { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); display: inline-flex; align-items: center;
    gap: 5px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
  .fam .ff .pr.ok { color: var(--ok); } .fam .ff .pr.bad { color: var(--bad); }
  .fam .sub2 { font-size: 10.5px; color: var(--ink-3); font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .spend-h { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; flex-wrap: wrap; }
  .spend-h h3 { margin: 0; }
  .spend-h .fchips { margin-left: auto; }
  .bars { width: 100%; height: auto; display: block; margin: 6px 0 4px; }
  .bars .est { fill: url(#hatch); }
  .bars .rec { fill: var(--accent); }
  .bars .hl { fill: var(--accent); opacity: .18; }
  .bars text { fill: var(--ink-3); font-family: var(--mono); font-size: 9px; }
  .bars .base { stroke: var(--line-2); stroke-width: 1; }
  .leg { display: flex; gap: 14px; font-size: 11px; color: var(--ink-3); margin-bottom: 8px; flex-wrap: wrap; }
  .leg i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; vertical-align: -1px; margin-right: 5px;
    background: var(--accent); }
  .leg i.est { background: repeating-linear-gradient(135deg, var(--accent) 0 2px, transparent 2px 4px); }
  .act { display: flex; flex-direction: column; gap: 4px; }
  .arow { display: grid; grid-template-columns: 14px 78px minmax(0,1fr) 78px 124px 112px; gap: 10px; align-items: center;
    padding: 9px 12px; border-radius: 8px; font-size: 12px; cursor: pointer; border: 1px solid var(--line);
    background: var(--panel); transition: border-color var(--t), background var(--t); }
  .arow:hover { border-color: var(--line-2); background: var(--hover); }
  .arow .ad { font-size: 12.5px; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .arow .ad small { color: var(--ink-3); font-size: 11px; margin-left: 6px; }
  .arow .ak { color: var(--ink-2); font-size: 10px; text-transform: uppercase; letter-spacing: .04em; font-weight: 600;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .arow .ae, .arow .at { color: var(--ink-2); text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap;
    font-size: 11.5px; font-family: var(--mono); }
  .arow .ag { color: var(--ink-3); text-align: right; font-size: 11px; white-space: nowrap; }
  .arow:hover .ag { color: var(--accent); }
  .run-ph { margin: 14px 0 6px; font-family: var(--mono); font-size: 10px; font-weight: 600; letter-spacing: .1em;
    text-transform: uppercase; color: var(--ink-3); display: flex; gap: 10px; align-items: center; }
  .run-ag { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin: 6px 0; font-size: 12.5px; }
  .run-ag .rh { display: flex; align-items: center; gap: 8px; font-size: 12px; }
  .run-ag .rh b { font-weight: 600; }
  .run-ag .rh .sp { flex: 1; }
  .run-ag .rs { color: var(--ink-2); margin-top: 5px; white-space: pre-wrap; word-break: break-word; max-height: 160px;
    overflow: auto; font-size: 12px; }
  .run-ag .re { color: var(--bad); font-family: var(--mono); font-size: 11.5px; margin-top: 5px; }
  .log { background: var(--panel-2); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; font-family: var(--mono);
    font-size: 11px; line-height: 1.55; max-height: 220px; overflow: auto; white-space: pre-wrap; word-break: break-word;
    color: var(--ink-2); }

  /* ==========================================================================
     MODELS — a comparison table, local models, provider setup, self-host.
     ========================================================================== */
  .hero { padding: 18px 20px; margin-bottom: 20px; }
  .hero-t { font-size: 18px; font-weight: 600; letter-spacing: -.02em; margin: 0 0 4px; }
  .hero-s { font-size: 12.5px; color: var(--ink-2); line-height: 1.5; max-width: 62ch; }
  .hero-lbl { font-size: 10px; text-transform: uppercase; letter-spacing: .08em; color: var(--ink-3); font-weight: 600; }
  .hero-m { font-family: var(--mono); font-weight: 600; font-size: 14px; }
  .host { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 14px 16px; margin-bottom: 20px; }
  .host .kbadge { font-size: 10.5px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; padding: 3px 9px; border-radius: 6px; }
  .kbadge.selfhost { background: var(--info-soft); color: var(--info); }
  .kbadge.provider { background: var(--ok-soft); color: var(--ok); }
  .kbadge.local { background: var(--info-soft); color: var(--info); }
  .kbadge.default { background: var(--fill); color: var(--ink-2); }
  .host .hm { font-family: var(--mono); font-weight: 600; font-size: 14px; }
  .host .hb, .hero .hb { font-family: var(--mono); font-size: 12px; color: var(--ink-3); margin-left: auto; word-break: break-all; }
  .browse-head { display: flex; align-items: baseline; gap: 11px; margin-bottom: 10px; }
  .cnt2 { font-size: 11.5px; color: var(--ink-2); background: var(--fill); padding: 1px 8px; border-radius: 6px; }
  .browse { overflow: hidden auto; max-height: 340px; margin-bottom: 22px; }
  .brow { display: flex; align-items: center; gap: 12px; padding: 8px 12px; cursor: pointer; border-radius: 7px; margin: 1px 4px; }
  .brow:hover { background: var(--hover); }
  .brow.cur { background: var(--accent-soft); }
  .brow .bm { font-family: var(--mono); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .brow .bp { font-size: 11.5px; color: var(--ink-3); white-space: nowrap; }
  .brow .bx { font-size: 11px; color: var(--ink-3); white-space: nowrap; min-width: 62px; text-align: right; }
  .browse-empty { padding: 14px 15px; font-size: 12.5px; color: var(--ink-3); font-style: italic; }
  .selfhost-card { gap: 10px; margin-bottom: 20px; }
  .selfhost-card .fields, .selfhost .fields { display: flex; flex-direction: column; gap: 8px; }
  .selfhost-card .fields .r, .selfhost .fields .r { display: flex; gap: 8px; }
  .selfhost { border: 1px solid var(--accent-line); border-radius: var(--radius); padding: 15px 17px; margin-bottom: 20px;
    background: var(--accent-soft); }
  .selfhost h3 { margin: 0 0 4px; font-size: 14px; }
  .keyline { display: flex; align-items: center; gap: 8px; font-family: var(--mono); font-size: 12px; }
  .keyline .env { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .keyline .val { font-weight: 600; }
  .src { font-size: 9px; text-transform: uppercase; letter-spacing: .05em; font-weight: 600; padding: 2px 6px; border-radius: 4px; }
  .src.env { background: var(--info-soft); color: var(--info); }
  .src.saved { background: var(--ok-soft); color: var(--ok); }
  .setup { padding: 13px 15px 14px; margin-bottom: 10px; }
  .setup-h { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .setup-n { font-weight: 600; font-size: 13px; }
  .setup-s { font-size: 12.5px; color: var(--ink-2); }
  .setup-bar { height: 4px; border-radius: 2px; background: var(--fill); margin: 10px 0 8px; overflow: hidden; }
  .setup-bar i { display: block; height: 100%; background: var(--accent); border-radius: 2px; transition: width .5s cubic-bezier(.2,.7,.2,1); }
  .setup-note { font-size: 11.5px; color: var(--ink-3); line-height: 1.5; }
  .ready { display: inline-flex; align-items: center; gap: 7px; font-size: 11.5px; color: var(--ink-3); white-space: nowrap; }
  .keyheld { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 12px; padding: 9px 12px;
    border-radius: 8px; background: var(--panel-2); border: 1px solid var(--line); }
  .kh-l { font-family: var(--mono); font-size: 10.5px; letter-spacing: .05em; text-transform: uppercase; color: var(--ink-3); }
  .kh-v { font-family: var(--mono); font-size: 13px; font-weight: 600; letter-spacing: .02em; }
  .kh-n { font-size: 11px; color: var(--ink-3); }
  .mtable { padding: 4px; max-height: 460px; overflow-y: auto; }
  .mrow { display: grid; grid-template-columns: minmax(0,1fr) 104px 52px 104px 118px 62px; align-items: center; gap: 12px;
    padding: 8px 11px; border-radius: 7px; cursor: pointer; font-size: 12.5px; border: 1px solid transparent;
    transition: background var(--t), border-color var(--t); }
  .mrow:hover, .mrow.kb { background: var(--hover); border-color: var(--line); }
  .mrow.kb { border-color: var(--accent-line); }
  .mrow.cur { background: var(--accent-soft); }
  .mrow .mn { font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mrow.cur .mn { color: var(--accent); font-weight: 600; }
  .mrow.locked .mn { color: var(--ink-2); }
  .mrow .mp { color: var(--ink-3); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mrow .mctx { color: var(--ink-2); font-size: 11.5px; text-align: right; font-variant-numeric: tabular-nums; font-family: var(--mono); }
  .mrow .mcaps { display: flex; gap: 4px; }
  .cap { font-size: 9.5px; letter-spacing: .04em; text-transform: uppercase; font-weight: 600; padding: 2px 6px; border-radius: 4px;
    background: var(--fill); color: var(--ink-3); }
  .cap.ok { background: var(--ok-soft); color: var(--ok); }
  .cap.bad { background: var(--bad-soft); color: var(--bad); }
  .cap.amb { background: var(--warn-soft); color: var(--warn); }
  .mrow .mgo { font-size: 11px; color: var(--ink-3); text-align: right; white-space: nowrap; }
  .mrow:hover .mgo { color: var(--accent); }
  .mrow.locked .mgo { color: var(--warn); }
  .mrow .mprice { color: var(--ink-2); font-size: 11px; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; font-family: var(--mono); }
  .mrow .mprice.free { color: var(--ok); } .mrow .mprice.na { color: var(--ink-3); }
  .mfam { display: flex; align-items: center; gap: 9px; padding: 10px 11px 6px; font-family: var(--mono); font-size: 10px;
    font-weight: 600; letter-spacing: .1em; text-transform: uppercase; color: var(--ink-3); position: sticky; top: 0;
    background: var(--panel); z-index: 1; }
  .mfam .mark2 { width: 18px; height: 18px; border-radius: 5px; } .mfam .mark2 svg { width: 11px; height: 11px; }
  .mfam .cnt3 { font-weight: 400; letter-spacing: 0; text-transform: none; }
  .mhead { display: grid; grid-template-columns: minmax(0,1fr) 104px 52px 104px 118px 62px; gap: 12px; padding: 4px 11px 6px;
    font-family: var(--mono); font-size: 9.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); }
  .mhead span:nth-child(3), .mhead span:nth-child(4) { text-align: right; }
  .orow { display: grid; grid-template-columns: minmax(0,1fr) 90px 72px 92px 62px; gap: 12px; align-items: center; padding: 8px 11px;
    border-radius: 7px; font-size: 12.5px; cursor: pointer; border: 1px solid transparent; transition: background var(--t), border-color var(--t); }
  .orow:hover { background: var(--hover); border-color: var(--line); }
  .orow.cur { background: var(--accent-soft); }
  .orow .on2 { font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .orow .osz, .orow .opq { color: var(--ink-2); font-size: 11.5px; text-align: right; font-variant-numeric: tabular-nums;
    white-space: nowrap; font-family: var(--mono); }
  .orow .ost { font-size: 10px; }
  .orow .ogo { font-size: 11px; color: var(--ink-3); text-align: right; }
  .orow:hover .ogo { color: var(--accent); }

  /* ==========================================================================
     CONFIG
     ========================================================================== */
  .cfg { margin-bottom: 20px; padding: 4px; }
  .cfg .kv { display: grid; grid-template-columns: 220px 1fr; gap: 12px; padding: 8px 12px; border-radius: 6px; }
  .cfg .kv:nth-child(odd) { background: var(--panel-2); }
  .cfg .kv .ck { font-family: var(--mono); font-size: 12px; color: var(--ink-2); }
  .cfg .kv .cv { font-family: var(--mono); font-size: 12px; white-space: pre-wrap; word-break: break-word; }
  details.layer { margin-bottom: 8px; }
  details.layer summary { padding: 11px 14px; cursor: pointer; font-weight: 600; }
  details.layer pre { margin: 0; padding: 0 14px 12px; font-family: var(--mono); font-size: 12px; white-space: pre-wrap; word-break: break-word; }
  .layerpath { font-family: var(--mono); font-size: 11px; color: var(--ink-3); padding: 0 14px 8px; }
  .note-sec { font-size: 12px; color: var(--ink-3); margin: -14px 0 18px; }
  .now { display: flex; gap: 10px; align-items: center; margin-bottom: 18px; padding: 12px 14px; border-radius: var(--radius);
    background: var(--panel); border: 1px solid var(--line); }
  .now .k { font-size: 11px; color: var(--ink-3); text-transform: uppercase; letter-spacing: .06em; }
  .now .v { font-family: var(--mono); font-size: 14px; font-weight: 600; }

  /* ==========================================================================
     DEPLOY — provider cards, the Hub search, fit tables, the job sheet, the
     deployments table. Verdict chips are the one new word: fits / tight / no.
     ========================================================================== */
  .dp-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 10px; margin-bottom: 12px; }
  .dpc { padding: 12px 13px 11px; display: flex; flex-direction: column; gap: 7px; min-width: 0; position: relative;
    transition: border-color var(--t); }
  .dpc:hover { border-color: var(--line-2); }
  .dpc.on { border-color: var(--accent-line); }
  .dpc .fh { display: flex; align-items: center; gap: 8px; }
  .dpc .mark2 { width: 26px; height: 26px; } .dpc .mark2 svg { width: 16px; height: 16px; }
  .dpc .fn { font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dpc .fa { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--ink-2); white-space: nowrap; overflow: hidden; }
  .dpc .fa .mono { overflow: hidden; text-overflow: ellipsis; }
  .dpc .dp-err { color: var(--bad); font-size: 11px; overflow: hidden; text-overflow: ellipsis; }
  .dpc .chips { gap: 4px; } .dpc .chip { font-size: 10px; padding: 2px 6px; }
  .dpc .ff { display: flex; align-items: center; gap: 6px; margin-top: auto; flex-wrap: wrap; }
  .dpc .ff .b { padding: 4px 9px; font-size: 11.5px; }
  .dp-form { display: none; flex-direction: column; gap: 9px; margin-top: 4px; padding-top: 10px; border-top: 1px solid var(--line); }
  .dp-form.on { display: flex; }
  .dp-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
  .dp-field .kh-l { font-size: 10px; }
  .dp-field input.in, .dp-field select.in { width: 100%; }
  .dp-field .kh-n { line-height: 1.45; }
  .dp-form .cta { margin-top: 4px; gap: 8px; }
  .dp-mrow, .dp-mhead { grid-template-columns: minmax(0,1fr) 58px 64px 96px 118px 74px 72px; }
  .dp-mhead span:nth-child(2), .dp-mhead span:nth-child(6) { text-align: right; }
  .dp-mrow .mctx { text-align: right; }
  .dp-models { max-height: 380px; }
  .dp-mh { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin-bottom: 8px; }
  .dp-mid { font-family: var(--mono); font-weight: 600; font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .dp-eng { display: inline-flex; align-items: center; gap: 7px; font-size: 11px; color: var(--ink-3); font-family: var(--mono); }
  .dp-eng select.in { padding: 4px 8px; }
  .dp-fitbox { margin-top: 10px; }
  .dp-loading { color: var(--ink-3); font-family: var(--mono); font-size: 12.5px; }
  .dp-ftab { margin-top: 8px; }
  .dp-frow { display: grid; grid-template-columns: minmax(0,1.3fr) 118px 84px 64px minmax(0,1fr) 92px; gap: 12px; align-items: center;
    padding: 7px 10px; border-radius: 7px; font-size: 12.5px; border: 1px solid transparent; transition: background var(--t), border-color var(--t); }
  .dp-frow:hover { background: var(--hover); border-color: var(--line); }
  .dp-frow.head { font-family: var(--mono); font-size: 9.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); padding: 2px 10px 6px; }
  .dp-frow.head:hover { background: none; border-color: transparent; }
  .dp-frow.no { color: var(--ink-3); }
  .dp-frow .dp-gn { font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dp-frow .dp-gn small { color: var(--ink-3); font-size: 10.5px; }
  .dp-frow .dp-gv, .dp-frow .dp-gp { color: var(--ink-2); font-size: 11.5px; white-space: nowrap; font-variant-numeric: tabular-nums; font-family: var(--mono); }
  .dp-frow .dp-gp { font-weight: 600; color: var(--ink); }
  .dp-frow.no .dp-gp { color: var(--ink-3); font-weight: 400; }
  .dp-frow .dp-gc { color: var(--ink-3); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dp-frow .dp-ga { display: flex; justify-content: flex-end; }
  .dp-frow .dp-ga .b { padding: 4px 10px; font-size: 11.5px; }
  .dp-frow .dp-ga .mgo { font-size: 10.5px; text-align: right; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--ink-3); }
  .vd { font-size: 10px; font-weight: 600; letter-spacing: .03em; text-transform: uppercase; padding: 2px 7px; border-radius: 5px;
    background: var(--fill); color: var(--ink-3); text-align: center; }
  .vd.fits { background: var(--ok-soft); color: var(--ok); }
  .vd.tight { background: var(--warn-soft); color: var(--warn); }
  .vd.no { background: var(--fill); color: var(--ink-3); }
  .dp-adv { margin: 6px 0 2px; }
  .dp-adv summary { font-family: var(--mono); font-size: 11px; color: var(--ink-3); cursor: pointer; padding: 3px 0; }
  .dp-adv summary:hover { color: var(--ink); }
  .dp-advgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px 12px; padding: 8px 0 4px; align-items: end; }
  .dp-advgrid input.in, .dp-advgrid select.in { width: 100%; padding: 5px 8px; }
  .dp-cost { font-family: var(--mono); font-size: 12.5px; color: var(--ink-2); background: var(--panel-2); border: 1px solid var(--line);
    border-radius: 8px; padding: 9px 12px; margin: 12px 0 4px; }
  .dp-cost b { color: var(--ink); }
  .dp-log { max-height: 260px; min-height: 80px; margin-top: 10px; }
  .sheet .banner { margin: 12px 0 0; }
  .dp-drow { display: grid; align-items: center; gap: 10px; padding: 8px 12px; border-radius: 7px; font-size: 12px;
    grid-template-columns: 22px minmax(0,.9fr) minmax(0,1.2fr) 118px 116px minmax(0,1fr) 96px 56px auto;
    border: 1px solid transparent; transition: background var(--t), border-color var(--t); }
  .dp-drow:hover { background: var(--hover); border-color: var(--line); }
  .dp-drow.head { font-family: var(--mono); font-size: 9.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); padding: 4px 12px 6px; }
  .dp-drow.head:hover { background: none; border-color: transparent; }
  .dp-drow.off { color: var(--ink-3); }
  .dp-drow .mark2 { width: 20px; height: 20px; border-radius: 5px; } .dp-drow .mark2 svg { width: 12px; height: 12px; }
  .dp-drow .dp-dn { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dp-drow .dp-dm, .dp-drow .dp-dg { color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11.5px; font-family: var(--mono); }
  .dp-drow .dp-ds { display: inline-flex; align-items: center; gap: 6px; font-size: 11px; white-space: nowrap; }
  .dp-drow .dp-de { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 11px; }
  .dp-drow .dp-de .clk { cursor: pointer; } .dp-drow .dp-de .clk:hover { color: var(--accent); }
  .dp-drow .dp-dc { font-size: 11.5px; text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; font-family: var(--mono); }
  .dp-drow .dp-da { color: var(--ink-3); font-size: 11px; text-align: right; white-space: nowrap; }
  .dp-drow .dp-dx { display: flex; gap: 4px; justify-content: flex-end; }
  .dp-drow .dp-dx .b { padding: 3px 8px; font-size: 11px; }
  .dp-steps { display: flex; gap: 14px; justify-content: center; flex-wrap: wrap; margin-top: 12px; font-size: 12px; color: var(--ink-2); }
  .dp-steps b { color: var(--accent); margin-right: 5px; font-family: var(--mono); }

  /* ---- responsive ---- */
  @media (max-width: 1100px) {
    .dp-drow { grid-template-columns: 22px minmax(0,1fr) 116px minmax(0,1fr) 56px auto; }
    .dp-drow .dp-dm, .dp-drow .dp-dg, .dp-drow .dp-dc { display: none; }
    .dp-mrow, .dp-mhead { grid-template-columns: minmax(0,1fr) 58px 118px 74px 72px; }
    .dp-mrow .mp, .dp-mhead span:nth-child(3), .dp-mhead span:nth-child(4) { display: none; }
    .dp-frow { grid-template-columns: minmax(0,1fr) 84px 64px 92px; }
    .dp-frow .dp-gv, .dp-frow .dp-gc { display: none; }
    #sessions.on { grid-template-columns: 240px 280px minmax(0,1fr); }
  }
  @media (max-width: 900px) {
    .railfoot { display: none; }
    .tb span { display: none; }
    #nav button .k { display: none; }
    .page { padding: 18px 14px 60px; }
    #sessions.on { grid-template-columns: 1fr; }
    .col { display: none; border-right: 0; } .col.mobile-on { display: block; }
    .arow { grid-template-columns: 14px minmax(0,1fr) 78px 76px; }
    .arow .ak, .arow .at { display: none; }
    .mrow, .mhead { grid-template-columns: minmax(0,1fr) 52px 96px 62px; }
    .mrow .mp, .mrow .mcaps, .mhead span:nth-child(2), .mhead span:nth-child(5) { display: none; }
    .orow { grid-template-columns: minmax(0,1fr) 72px 62px; }
    .orow .osz, .orow .opq { display: none; }
    #transcript { padding: 14px 12px; }
    .msg { grid-template-columns: 1fr; gap: 4px; }
    .who { flex-direction: row; align-items: center; gap: 8px; }
  }
</style>
</head>
<body>
<header id="top">
  <div class="brand"><img src="/mantis.svg" alt=""> <span>mantis</span></div>
  <nav id="nav">
    <button data-v="home" class="on">overview<span class="k">1</span></button>
    <button data-v="sessions">sessions<span class="k">2</span></button>
    <button data-v="models">models<span class="k">3</span></button>
    <button data-v="deploy">deploy<span class="k">4</span></button>
    <button data-v="mcp">mcp<span class="k">5</span></button>
    <button data-v="skills">skills<span class="k">6</span></button>
    <button data-v="config">config<span class="k">7</span></button>
  </nav>
  <div class="topr">
    <div class="railfoot" id="railfoot"></div>
    <button class="tb" id="cmdk" title="command palette"><span>search or jump…</span><kbd>⌘K</kbd></button>
    <button class="tb icon" id="themebtn" title="theme">◐</button>
    <span class="lan" id="lanind">local</span>
  </div>
</header>
<main>
  <section id="home" class="view on"><div class="scroll"><div class="page wide" id="homepad"></div></div></section>
  <section id="skills" class="view"><div class="scroll"><div class="page" id="skillspad"></div></div></section>
  <section id="mcp" class="view"><div class="scroll"><div class="page" id="mcppad"></div></div></section>
  <section id="sessions" class="view">
    <div class="col" id="projects"><div class="col-head">Projects</div><div class="cards" id="projcards"></div></div>
    <div class="col" id="sessionlist"><div class="col-head">Sessions</div>
      <div class="colfind"><input id="sessfind" type="search" placeholder="filter sessions  ( / )"></div>
      <div class="cards" id="sesscards"></div></div>
    <div class="col" id="convcol"><div id="transcript"><div class="empty">Pick a session.</div></div></div>
  </section>
  <section id="models" class="view"><div class="scroll"><div class="page" id="modelspad"></div></div></section>
  <section id="deploy" class="view"><div class="scroll"><div class="page wide" id="deploypad"></div></div></section>
  <section id="config" class="view"><div class="scroll"><div class="page" id="configpad"></div></div></section>
</main>
<div id="modal"><div class="sheet"><button class="x" onclick="hideModal()">✕</button><div id="sheet"></div></div></div>
<div id="palette"><div class="pal"><input id="palin" placeholder="Jump to a page, project, session, deployment — or run an action…" autocomplete="off">
  <div class="pal-list" id="pallist"></div>
  <div class="pal-f"><span><b>↑↓</b> move</span><span><b>↵</b> open</span><span><b>esc</b> close</span><span><b>g</b> <b>o</b>/<b>s</b>/<b>m</b>/<b>d</b> pages</span><span><b>/</b> search</span></div></div></div>
<div id="toast"></div>

<script>
const TOKEN = "__TOKEN__";
async function api(path) {
  const h = {};
  if (TOKEN) h["X-Mantis-Token"] = TOKEN;
  const r = await fetch(path, { headers: h });
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
async function post(path, body) {
  const h = { "Content-Type": "application/json" };
  if (TOKEN) h["X-Mantis-Token"] = TOKEN;
  const r = await fetch(path, { method: "POST", headers: h, body: JSON.stringify(body) });
  const j = await r.json().catch(() => ({ error: r.statusText }));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}
let toastT;
function toast(msg, err) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.className = "on" + (err ? " err" : "");
  clearTimeout(toastT); toastT = setTimeout(() => (t.className = ""), 3000);
}
const el = (t, c, txt) => { const e = document.createElement(t); if (c) e.className = c; if (txt != null) e.textContent = txt; return e; };
// ---- theme: system by default, a toggle that remembers, ?theme= for tests ----
const THEME_KEY = "mantis-theme";
const getTheme = () => { try { return localStorage.getItem(THEME_KEY) || ""; } catch (e) { return ""; } };
function applyTheme(t) {
  const r = document.documentElement;
  if (t === "light" || t === "dark") r.dataset.theme = t; else delete r.dataset.theme;
  const b = document.getElementById("themebtn");
  if (b) { b.textContent = t === "dark" ? "☾" : t === "light" ? "☀" : "◐"; b.title = "theme · " + (t || "system") + " — click to change"; }
}
function cycleTheme() {
  const cur = getTheme(); const next = cur === "dark" ? "light" : cur === "light" ? "" : "dark";
  try { if (next) localStorage.setItem(THEME_KEY, next); else localStorage.removeItem(THEME_KEY); } catch (e) { /* private mode */ }
  applyTheme(next); toast("theme · " + (next || "system"));
}
applyTheme(new URLSearchParams(location.search).get("theme") || getTheme());
document.getElementById("themebtn").onclick = cycleTheme;
document.getElementById("lanind").className = "lan" + (TOKEN ? " on" : "");
document.getElementById("lanind").textContent = TOKEN ? "lan · token" : "local";
document.getElementById("lanind").title = TOKEN ? "bound to the network — the URL token authenticates every request" : "loopback only";
// ---- diff-render: keep DOM nodes keyed by id, patch only what changed ----
// A refresh must never reset scroll or close a drawer, so every live list is
// rendered through this: existing nodes stay, new ones are inserted in order,
// gone ones are removed, and a node is re-filled only when its `sig` moved.
function patchList(container, items, key, sig, render) {
  const have = new Map();
  for (const n of [...container.children]) if (n.dataset.key != null) have.set(n.dataset.key, n);
  let cursor = container.firstChild;
  for (const it of items) {
    const k = String(key(it)); const sg = sig ? JSON.stringify(sig(it)) : null;
    let n = have.get(k);
    if (n) { have.delete(k); if (sg !== n.dataset.sig) { n.dataset.sig = sg; render(n, it); } }
    else { n = render(null, it); n.dataset.key = k; if (sg != null) n.dataset.sig = sg; }
    if (n === cursor) cursor = cursor.nextSibling; else container.insertBefore(n, cursor);
  }
  have.forEach(n => n.remove());
}
function skeleton(pad, n) {
  pad.innerHTML = "";
  const a = el("div","sk"); a.style.width = "38%"; a.style.height = "20px"; pad.append(a);
  const b = el("div","sk"); b.style.width = "62%"; pad.append(b);
  const g = el("div","skgrid"); for (let i = 0; i < (n || 6); i++) g.append(el("div","sk card")); pad.append(g);
}
// ---- a tiny markdown renderer for assistant text: code, bold, lists, headings ----
function mdInline(t) {
  return esc(t)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[\s(])\*([^*\n]+)\*(?=[\s).,;:!?]|$)/g, "$1<i>$2</i>")
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
}
function md(src) {
  const lines = String(src || "").split("\n"); const out = []; let i = 0;
  const isBlock = l => /^(```|#{1,4}\s|\s*[-*•]\s|\s*\d+[.)]\s|>)/.test(l);
  while (i < lines.length) {
    const l = lines[i];
    if (/^```/.test(l)) { const buf = []; i++; while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]); i++;
      out.push("<pre><code>" + esc(buf.join("\n")) + "</code></pre>"); continue; }
    const h = /^(#{1,4})\s+(.*)$/.exec(l);
    if (h) { out.push("<h" + h[1].length + ">" + mdInline(h[2]) + "</h" + h[1].length + ">"); i++; continue; }
    if (/^\s*[-*•]\s+/.test(l)) { const it = []; while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) it.push("<li>" + mdInline(lines[i++].replace(/^\s*[-*•]\s+/, "")) + "</li>");
      out.push("<ul>" + it.join("") + "</ul>"); continue; }
    if (/^\s*\d+[.)]\s+/.test(l)) { const it = []; while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) it.push("<li>" + mdInline(lines[i++].replace(/^\s*\d+[.)]\s+/, "")) + "</li>");
      out.push("<ol>" + it.join("") + "</ol>"); continue; }
    if (/^>\s?/.test(l)) { const it = []; while (i < lines.length && /^>\s?/.test(lines[i])) it.push(mdInline(lines[i++].replace(/^>\s?/, "")));
      out.push("<blockquote>" + it.join("<br>") + "</blockquote>"); continue; }
    if (!l.trim()) { i++; continue; }
    const para = []; while (i < lines.length && lines[i].trim() && !isBlock(lines[i])) para.push(lines[i++]);
    out.push("<p>" + mdInline(para.join("\n")).replace(/\n/g, "<br>") + "</p>");
  }
  return out.join("");
}
// Meta content the runtime injects — <system-reminder>, <env>, [context] —
// is evidence, not conversation. It is split out and hidden behind a toggle.
const META_RE = /<(system-reminder|env)>[\s\S]*?<\/\1>|\[context\]/gi;
function splitMeta(text) {
  const meta = []; const t = String(text == null ? "" : text).replace(META_RE, m => { meta.push(m); return ""; }).trim();
  return { text: t, meta };
}
function ctxToggle(metas) {
  const w = el("div");
  const b = el("button","ctxtog", "show context (" + metas.length + ")");
  const body = el("div","ctxbody"); const pre = el("pre", null, metas.join("\n\n")); body.append(pre);
  b.onclick = () => { const on = body.classList.toggle("on"); b.textContent = (on ? "hide" : "show") + " context (" + metas.length + ")"; };
  w.append(b, body); return w;
}
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const input = (ph, pw) => { const i = el("input","in"); i.placeholder = ph; if (pw) i.type = "password"; return i; };
function saveKeyFn(p, inp, btn) {
  return async () => {
    if (!inp.value.trim()) { toast("paste a key first", true); return; }
    btn.disabled = true;
    try {
      const r = await post("/api/key", { provider: p.id, key: inp.value });
      if (r.ok) {
        if (r.valid === false) toast("saved, but the check failed: " + (r.detail || ""), true);
        else toast("✓ enabled " + (p.label || p.id) + (r.detail && r.detail !== "saved" ? " · " + r.detail : ""));
        loadOverview(); loadModels();
      } else toast(r.error || "failed", true);
    } catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  };
}
function removeKeyFn(p, btn) {
  return async () => {
    btn.disabled = true;
    try { await post("/api/key", { provider: p.id, key: "" }); toast("removed key for " + (p.label || p.id)); loadOverview(); loadModels(); }
    catch (e) { toast(e.message, true); } finally { btn.disabled = false; }
  };
}
// Switch the current model. Passing the provider's base_url as backend keeps
// routing correct for a cross-provider pick. Takes effect on the next launch.
async function useModel(model, backend) {
  try {
    const r = await post("/api/use", { model, backend: backend || "" });
    if (r.ok) { toast("current model → " + (r.model || model)); loadOverview(); loadModels(); }
    else toast(r.error || "failed", true);
  } catch (e) { toast(e.message, true); }
}
// Clicking a locked model should land you in that provider's setup, not just
// near it: open the row, scroll it into view, focus the key field.
function focusProvider(pid) {
  const row = document.getElementById("prov-" + pid);
  if (!row) return;
  if (row.openDrawer) row.openDrawer();
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.add("flash");
  setTimeout(() => row.classList.remove("flash"), 1200);
  const inp = row.querySelector(".lbody input");
  if (inp) setTimeout(() => inp.focus(), 380);
}
function showModal(wide) {
  document.getElementById("modal").className = "on";
  document.querySelector("#modal .sheet").classList.toggle("wide", !!wide);
}
function hideModal() { document.getElementById("modal").className = ""; }
document.getElementById("modal").addEventListener("click", e => { if (e.target.id === "modal") hideModal(); });
window.addEventListener("keydown", e => { if (e.key === "Escape") { hideModal(); hidePalette(); } });
function extLink(cls, text, href) {
  const a = el(href ? "a" : "span", cls, text);
  if (href) { a.href = href; a.target = "_blank"; a.rel = "noopener"; }
  return a;
}
function openGuide(p) {
  const g = p.guide; if (!g) return;
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(el("h3", null, "Get your " + (g.name || p.label) + " key"));
  s.append(el("div","sub", g.env_var || ""));
  const ol = el("ol"); (g.steps || []).forEach(x => ol.append(el("li", null, x))); s.append(ol);
  if (g.free_note) s.append(el("div","free", g.free_note));
  const cta = el("div","cta");
  cta.append(extLink("btn big", "Open key page ↗", g.keys_url));
  if (g.pricing_url) cta.append(extLink("a-link", "Pricing ↗", g.pricing_url));
  if (p.docs_url) cta.append(extLink("a-link", "Full docs ↗", p.docs_url));
  s.append(cta);
  showModal();
}
function openSelfhostGuide(g, docs) {
  if (!g) return;
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(el("h3", null, "Self-host a model"));
  s.append(el("div","sub", g.intro || ""));
  s.append(el("h4", null, "Pick a runtime"));
  (g.runtimes || []).forEach(rt => {
    const box = el("div","rt");
    box.append(el("div","rn", rt.name));
    if (rt.note) box.append(el("div","rnote", rt.note));
    if (rt.command) box.append(el("code", null, rt.command));
    box.append(el("div","rnote", "base URL · " + rt.base_url));
    s.append(box);
  });
  s.append(el("h4", null, "Then in mantis"));
  const ol = el("ol"); (g.steps || []).forEach(x => ol.append(el("li", null, x))); s.append(ol);
  if (g.notes && g.notes.length) {
    const ul = el("ul","notes"); g.notes.forEach(n => ul.append(el("li", null, n))); s.append(ul);
  }
  // Agent skill — hand a SKILL.md to an AI agent to do the hosting for you.
  if (g.skill) {
    const box = el("div","skill-box");
    box.append(el("div","skill-t", "🤖 Or let an agent do it"));
    box.append(el("div","skill-b", g.skill.blurb));
    const cta = el("div","cta"); cta.append(extLink("btn big", "Open selfhost skill ↗", g.skill.url));
    box.append(cta); s.append(box);
  }
  // Remote GPU / sandbox platforms — each with a guide + its own agent skill.
  if (g.platforms && g.platforms.length) {
    s.append(el("h4", null, "Remote GPU platforms"));
    g.platforms.forEach(pl => {
      const row = el("div","plat");
      const top = el("div","plat-top");
      top.append(el("span","plat-n", pl.name));
      if (pl.kind) top.append(el("span","plat-k", pl.kind));
      row.append(top);
      if (pl.note) row.append(el("div","rnote", pl.note));
      const links = el("div","plat-links");
      if (pl.docs_url) links.append(extLink("a-link", "guide ↗", pl.docs_url));
      if (pl.skill_url) links.append(extLink("a-link", "agent skill ↗", pl.skill_url));
      row.append(links); s.append(row);
    });
  }
  if (docs) { const cta = el("div","cta"); cta.append(extLink("btn big", "Full self-host docs ↗", docs)); s.append(cta); }
  showModal();
}
const ago = (sec) => {
  const d = Date.now()/1000 - sec;
  if (d < 60) return "just now";
  if (d < 3600) return Math.floor(d/60) + "m ago";
  if (d < 86400) return Math.floor(d/3600) + "h ago";
  return Math.floor(d/86400) + "d ago";
};
const q = (o) => Object.entries(o).map(([k,v]) => k+"="+encodeURIComponent(v)).join("&");

// ---- activity ----
// The page is a record of you working with an agent on this machine, so it is
// built like an instrument read-out rather than a BI dashboard: one trace, one
// punchcard, one spectrum, one ledger of projects. Every number here is
// computed from local transcripts — nothing leaves the machine.
const fmt = n => (n || 0).toLocaleString();
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
const WD = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
function dkey(d) { return d.getFullYear() + "-" + String(d.getMonth()+1).padStart(2,"0") + "-" + String(d.getDate()).padStart(2,"0"); }
function calcStreak(daily) {
  let s = 0; const d = new Date(); d.setHours(0,0,0,0);
  if (!(daily[dkey(d)] && daily[dkey(d)].msgs > 0)) d.setDate(d.getDate()-1);  // today may still be empty
  while (daily[dkey(d)] && daily[dkey(d)].msgs > 0) { s++; d.setDate(d.getDate()-1); }
  return s;
}
function dailySeries(daily, n) {
  const out = []; const d = new Date(); d.setHours(0,0,0,0); d.setDate(d.getDate()-(n-1));
  for (let i = 0; i < n; i++) { const k = dkey(d); out.push({ date: k, msgs: (daily[k] && daily[k].msgs) || 0 }); d.setDate(d.getDate()+1); }
  return out;
}
// THE TRACE — raw daily volume as a faint envelope, a 7-day mean as the signal
// line on top, the peak annotated in place. Spiky data plus a calm mean reads
// the way an instrument does: the noise and the trend at once.
function traceSVG(series, box) {
  const W = 1000, H = 190, PL = 6, PR = 6, PT = 14, PB = 20;
  const n = series.length, iw = W - PL - PR, ih = H - PT - PB;
  const vals = series.map(s => s.msgs);
  const mean = vals.map((_, i) => {
    const a = Math.max(0, i-3), b = Math.min(n-1, i+3);
    let t = 0; for (let j = a; j <= b; j++) t += vals[j];
    return t / (b - a + 1);
  });
  const max = Math.max(1, ...vals);
  const X = i => PL + (n <= 1 ? 0 : i/(n-1)*iw);
  const Y = v => PT + ih - (v/max)*ih;
  const path = (arr) => "M" + arr.map((v,i) => X(i).toFixed(1)+","+Y(v).toFixed(1)).join(" L");
  const rawLine = path(vals), sigLine = path(mean);
  const area = sigLine + ` L${(PL+iw).toFixed(1)},${(PT+ih).toFixed(1)} L${PL.toFixed(1)},${(PT+ih).toFixed(1)} Z`;
  const pi = vals.reduce((b,v,i) => v > vals[b] ? i : b, 0);
  // Month ticks, but never two labels on top of each other at the left edge
  // where the series starts mid-month.
  let ticks = "", lastM = -1, lastX = -999;
  series.forEach((s,i) => {
    const dt = new Date(s.date+"T00:00:00"), m = dt.getMonth(), x = X(i);
    if (m !== lastM && i > 3 && i < n-3 && x - lastX > 60) {
      lastM = m; lastX = x;
      ticks += `<text x="${x.toFixed(1)}" y="${H-5}" text-anchor="middle">${MON[m]}</text>`;
    } else if (m !== lastM) { lastM = m; }
  });
  const peak = vals[pi] ? `<line class="pk" x1="${X(pi).toFixed(1)}" y1="${(PT-6).toFixed(1)}" x2="${X(pi).toFixed(1)}" y2="${(PT+ih).toFixed(1)}"/>` +
    `<circle class="pkd" cx="${X(pi).toFixed(1)}" cy="${Y(vals[pi]).toFixed(1)}" r="3"/>` : "";
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="messages per day">` +
    `<line class="base" x1="${PL}" y1="${PT+ih}" x2="${PL+iw}" y2="${PT+ih}"/>` +
    `<path class="env" d="${area}"/><path class="raw" d="${rawLine}"/>${peak}` +
    `<path class="sig" d="${sigLine}"/>${ticks}</svg>`;
  const sig = box.querySelector(".sig");
  try { const L = sig.getTotalLength(); sig.style.strokeDasharray = L; sig.style.strokeDashoffset = L; }
  catch (e) { /* jsdom / no layout — the line just appears */ }
  return { peakIdx: pi, peakVal: vals[pi] };
}
// PUNCHCARD — weekday × hour, area-encoded. by_hour and by_weekday separately
// can't tell you about Sunday nights; the joint distribution can.
function punchSVG(grid) {
  const cell = 15, left = 26, top = 15, W = left + 24*cell + 4, H = top + 7*cell + 6;
  let max = 1;
  grid.forEach(r => r.forEach(v => { if (v > max) max = v; }));
  let cells = "", labels = "";
  for (let d = 0; d < 7; d++) {
    labels += `<text x="0" y="${top + d*cell + cell/2 + 3}">${WD[d][0]}</text>`;
    for (let h = 0; h < 24; h++) {
      const v = grid[d][h];
      if (!v) continue;
      const r = 1.6 + Math.sqrt(v/max) * (cell/2 - 1.4);
      cells += `<circle class="cell" cx="${left + h*cell + cell/2}" cy="${top + d*cell + cell/2}" r="${r.toFixed(2)}" opacity="${(0.45 + 0.55*v/max).toFixed(2)}"><title>${WD[d]} ${h}:00 — ${v} messages</title></circle>`;
    }
  }
  for (let h = 0; h < 24; h += 3)
    labels += `<text x="${left + h*cell + cell/2}" y="9" text-anchor="middle">${h}</text>`;
  return `<svg class="punch" viewBox="0 0 ${W} ${H}">${labels}${cells}</svg>`;
}
function loadHomeSpectrum(card, top, total) {
  const shown = top.slice(0, 6);
  const rest = total - shown.reduce((a,t) => a + t.count, 0);
  const parts = shown.map((t,i) => ({ name: t.name, count: t.count, op: 1 - i*0.13 }));
  if (rest > 0) parts.push({ name: "everything else", count: rest, op: 0.18 });
  const bar = el("div","spec");
  parts.forEach(p => {
    const i = el("i"); i.style.width = (p.count/Math.max(1,total)*100).toFixed(2) + "%";
    i.style.opacity = p.op; i.title = p.name + " · " + fmt(p.count);
    bar.append(i);
  });
  card.append(bar);
  const list = el("div","tl");
  parts.forEach(p => {
    const n = el("div","tn");
    const sw = el("span","sw"); sw.style.opacity = p.op; n.append(sw);
    n.append(document.createTextNode(p.name));
    list.append(n);
    list.append(el("div","tc", fmt(p.count)));
    list.append(el("div","tp", Math.round(p.count/Math.max(1,total)*100) + "%"));
  });
  card.append(list);
}
// ---- formatting for readings: tokens, dollars, durations, bytes ----
const fmtTok = n => { n = n || 0; return n >= 1e6 ? (n/1e6).toFixed(n % 1e6 ? 1 : 0) + "m"
                    : n >= 1e3 ? (n/1e3).toFixed(n >= 1e5 ? 0 : 1) + "k" : String(n); };
const fmtUsd = v => v == null ? "—" : (v > 0 && v < 0.01 ? "$" + v.toFixed(4)
                    : v < 1 ? "$" + v.toFixed(3) : "$" + v.toFixed(2));
const fmtDur = s => { if (s == null) return "—"; s = Math.max(0, Math.round(s));
  if (s < 60) return s + "s"; if (s < 3600) return Math.floor(s/60) + "m " + (s%60) + "s";
  return Math.floor(s/3600) + "h " + Math.floor(s%3600/60) + "m"; };
const fmtBytes = b => { if (!b) return "—"; const u = ["B","KB","MB","GB","TB"]; let i = 0;
  while (b >= 1024 && i < u.length-1) { b /= 1024; i++; } return b.toFixed(b >= 10 || i === 0 ? 0 : 1) + " " + u[i]; };
const AUTH_LABEL = { saved: "key saved", env: "key from env", oauth: "oauth token", none: "no key" };
const AUTH_CLS = { saved: "acc", env: "blu", oauth: "vio", none: "" };
function statusClass(status, active) {
  if (active || status === "running" || status === "starting") return "run";
  if (["done","completed","ok","succeeded"].includes(status)) return "ok";
  if (["error","failed","timeout","cancelled","canceled"].includes(status)) return "bad";
  if (["pending","queued","blocked","orphaned","adopted","paused"].includes(status)) return "pend";
  return "";
}
function lcdCell(lcd, v, label, cls) {
  const d = el("div", cls || null); d.append(el("i", null, v)); d.append(document.createTextNode(label)); lcd.append(d);
}

// ---- PROVIDERS — the five families ----
// One card per family: its mark, how it's authed (saved key / env / OAuth
// token / nothing), the last model you used from it, and a one-click probe
// that hits the same /models endpoint the TUI would. Clicking the card lands
// in that family's setup on the models page.
let FAMS = [];
function renderFamilies(box, g) {
  FAMS = g.families || [];
  const sig = f => [f.ready, f.is_current, f.last_model, (f.providers || []).map(p => [p.auth, p.key_masked, p.enabled]),
                    f.local && [f.local.reachable, f.local.model_count, f.local.loaded_count]];
  patchList(box, FAMS, f => f.id, sig, (card, f) => {
    const provs = f.providers || [];
    const target = provs.find(x => x.enabled) || provs[0];
    card = card || el("div"); card.innerHTML = ""; card.className = "fam" + (f.is_current ? " cur" : "");
    const fh = el("div","fh");
    fh.append(providerMark(f.logo, f.label));
    fh.append(el("span","fn", f.label));
    const sp = el("span"); sp.style.flex = "1"; fh.append(sp);
    const d = el("span","dot2 " + (f.ready ? "ok" : "")); d.title = f.ready ? "ready to run" : "not set up"; fh.append(d);
    card.append(fh);
    const fa = el("div","fa");
    if (f.id === "oss") {
      const loc = f.local || {};
      fa.append(el("span","t2 " + (loc.reachable ? "acc" : ""), loc.reachable ? "ollama up" : "ollama off"));
      fa.append(document.createTextNode(provs.filter(p => p.enabled).length + "/" + provs.length + " hosted keys"));
    } else if (!provs.length) {
      fa.append(el("span","t2 amb","not in catalog"));
    } else {
      fa.append(el("span","t2 " + (AUTH_CLS[target.auth] || ""), AUTH_LABEL[target.auth] || target.auth));
      if (target.key_masked) fa.append(el("span","mono", target.key_masked));
    }
    card.append(fa);
    card.append(el("div","fm" + (f.last_model ? "" : " none"), f.last_model || "no model used yet"));
    if (f.id === "oss" && f.local && f.local.reachable)
      card.append(el("div","sub2", f.local.model_count + " local model" + (f.local.model_count===1?"":"s") + " · " + f.local.loaded_count + " loaded"));
    else if (f.id === "oss") card.append(el("div","sub2", provs.slice(0, 3).map(p => p.label).join(" · ")));
    else if (provs.length) card.append(el("div","sub2", (target.base_url || "").replace(/^https?:\/\//, "")));
    const ff = el("div","ff");
    const pr = el("span","pr");
    const tb = btn("test", "", async (e) => {
      e.stopPropagation();
      let body;
      if (f.id === "oss" && f.local && f.local.reachable) body = { backend: f.local.base_url + "/v1" };
      else if (target) body = { provider: target.id };
      else { toast("nothing to test in this family yet", true); return; }
      tb.disabled = true; pr.className = "pr"; pr.textContent = "reaching…";
      try {
        const r = await post("/api/model/test", body);
        pr.className = "pr " + (r.ok ? "ok" : "bad");
        pr.textContent = r.ok
          ? "ok · " + r.ms + "ms" + (r.count != null ? " · " + r.count + " models" : "")
          : (r.status ? "HTTP " + r.status : "unreachable") + (r.ms != null ? " · " + r.ms + "ms" : "");
        pr.title = r.error || r.label || "";
      } catch (e2) { pr.className = "pr bad"; pr.textContent = e2.message; }
      finally { tb.disabled = false; }
    });
    tb.title = "GET /models with the key mantis would use";
    ff.append(tb, pr); card.append(ff);
    card.onclick = () => { showTab("models"); if (target) setTimeout(() => focusProvider(target.id), 260); };
    return card;
  });
}
async function testProviderQuick(target) {
  toast("reaching " + (target.label || target.id) + "…");
  try {
    const r = await post("/api/model/test", { provider: target.id });
    toast(r.ok ? "✓ " + (r.label || target.id) + " · " + r.ms + "ms" + (r.count != null ? " · " + r.count + " models" : "")
               : "✗ " + (r.label || target.id) + " · " + (r.error || "unreachable"), !r.ok);
  } catch (e) { toast(e.message, true); }
}

// ---- SPEND — estimated (sessions) vs recorded (workflow runs) ----
// Transcripts store messages, not the provider's usage record, so session
// tokens are an estimate from size (≈4 chars/token, context re-billed each
// turn) priced at the current model. Workflow runs record real usage. The
// two are drawn differently on purpose: hatched is a guess, solid is a reading.
let SPEND_WIN = 7;
function spendBars(days) {
  const W = 1000, H = 150, PL = 6, PR = 6, PT = 12, PB = 18, n = days.length;
  const iw = W-PL-PR, ih = H-PT-PB, bw = iw/Math.max(1, n);
  const tot = d => d.est_in + d.est_out + d.rec_in + d.rec_out;
  const max = Math.max(1, ...days.map(tot));
  let bars = "", ticks = "";
  days.forEach((d, i) => {
    const x = PL + i*bw + bw*0.15, w = bw*0.7, y0 = PT + ih;
    const hr = (d.rec_in + d.rec_out)/max*ih, he = (d.est_in + d.est_out)/max*ih;
    const title = `<title>${d.date} · est ${fmtTok(d.est_in+d.est_out)} tok${d.est_usd != null ? " ≈ " + fmtUsd(d.est_usd) : ""}` +
      `${(d.rec_in+d.rec_out) ? " · recorded " + fmtTok(d.rec_in+d.rec_out) + " tok " + fmtUsd(d.rec_usd) : ""}</title>`;
    if (hr) bars += `<rect class="rec" x="${x.toFixed(1)}" y="${(y0-hr).toFixed(1)}" width="${w.toFixed(1)}" height="${hr.toFixed(1)}" rx="1">${title}</rect>`;
    if (he) bars += `<rect class="est" x="${x.toFixed(1)}" y="${(y0-hr-he).toFixed(1)}" width="${w.toFixed(1)}" height="${he.toFixed(1)}" rx="1">${title}</rect>`;
    if (!hr && !he) bars += `<rect class="hl" x="${x.toFixed(1)}" y="${(y0-1.5).toFixed(1)}" width="${w.toFixed(1)}" height="1.5"/>`;
    const dt = new Date(d.date + "T00:00:00");
    if (n <= 7 || dt.getDay() === 1 || i === n-1)
      ticks += `<text x="${(x+w/2).toFixed(1)}" y="${H-5}" text-anchor="middle">${n <= 7 ? WD[(dt.getDay()+6)%7] : (dt.getMonth()+1) + "/" + dt.getDate()}</text>`;
  });
  return `<svg class="bars" viewBox="0 0 ${W} ${H}" role="img" aria-label="tokens per day">` +
    `<defs><pattern id="hatch" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">` +
    `<rect width="2.5" height="6" style="fill:var(--accent)"/></pattern></defs>` +
    `<line class="base" x1="${PL}" y1="${PT+ih}" x2="${PL+iw}" y2="${PT+ih}"/>${bars}${ticks}</svg>`;
}
function renderSpend(box, sp, win) {
  box.innerHTML = "";
  win = win || SPEND_WIN;
  const t = (sp.totals || {})[String(win)] || {};
  const h = el("div","spend-h");
  h.append(el("h3", null, "spend · last " + win + " days"));
  const chips = el("div","fchips");
  [7, 30].forEach(n => {
    const c = el("button","fchip" + (n === win ? " on" : ""), n + "d");
    c.onclick = () => { SPEND_WIN = n; renderSpend(box, sp, n); };
    chips.append(c);
  });
  h.append(chips); box.append(h);
  const pr = sp.pricing || {};
  const n2 = el("div","note2");
  n2.innerHTML = pr.known
    ? "Session tokens are <b>estimated</b> from transcript size and priced at <b>" + esc(pr.model) + "</b> (" +
      esc(fmtUsd(pr.prompt_per_million)) + " / " + esc(fmtUsd(pr.completion_per_million)) + " per 1M in / out). " +
      "Workflow runs are recorded by the provider."
    : "Session tokens are <b>estimated</b> from transcript size; " + (pr.model
      ? "no price-table row for <b>" + esc(pr.model) + "</b>, so dollars are shown for recorded workflow runs only."
      : "pick a model to price them.");
  box.append(n2);
  const lcd = el("div","lcd tight");
  lcdCell(lcd, fmtTok(t.est_tokens), "est. tokens", "");
  lcdCell(lcd, fmtTok(t.est_in) + "↑ " + fmtTok(t.est_out) + "↓", "in / out", "dim");
  if (t.est_usd != null) lcdCell(lcd, "≈" + fmtUsd(t.est_usd), "est. spend", "hot");
  if ((t.rec_in || 0) + (t.rec_out || 0)) {
    lcdCell(lcd, fmtTok(t.rec_in + t.rec_out), "recorded tokens", "");
    lcdCell(lcd, fmtUsd(t.rec_usd), "recorded spend", "hot");
  }
  lcdCell(lcd, fmt(t.msgs), "messages", "dim");
  box.append(lcd);
  const leg = el("div","leg");
  leg.innerHTML = '<span><i class="est"></i>estimated · sessions</span><span><i></i>recorded · workflow runs</span>';
  box.append(leg);
  const sv = el("div"); sv.innerHTML = spendBars((sp.days || []).slice(-win)); box.append(sv);
  const provs = sp.by_provider || [];
  if (provs.length) {
    const pt = el("div","note2"); pt.style.margin = "10px 0 6px"; pt.textContent = "by provider · last 30 days";
    box.append(pt);
    const list = el("div","plist");
    const maxT = Math.max(1, ...provs.map(p => p.in + p.out));
    provs.forEach(p => {
      const row = el("div","prow");
      const f = el("div","fillbar"); f.style.width = ((p.in+p.out)/maxT*100).toFixed(1) + "%"; row.append(f);
      const pf = el("span","pf"); pf.append(providerMark(p.id === "local" ? "ollama" : p.id, p.label)); row.append(pf);
      row.append(el("span","pn", p.label));
      row.append(el("span","t2 " + (p.source === "recorded" ? "acc" : ""), p.source));
      row.append(el("span","pv", fmtTok(p.in) + "↑ " + fmtTok(p.out) + "↓ · " +
        (p.usd == null ? "unpriced" : (p.source === "estimated" ? "≈" : "") + fmtUsd(p.usd)) +
        (p.runs ? " · " + p.runs + " run" + (p.runs===1?"":"s") : "")));
      list.append(row);
    });
    box.append(list);
  }
}

// ---- LIVE — background jobs and workflow runs ----
function renderActivity(box, act) {
  const rows = [];
  (act.runs || []).forEach(r => { const u = r.usage || {}; rows.push({
    id: "run:" + r.run_id, kind: "workflow", ts: r.saved_at, status: r.status, active: !!r.active,
    desc: r.name || r.definition || r.run_id, extra: (u.agents_done||0) + "/" + (u.agents||0) + " agents",
    elapsed: u.elapsed_s, tokens: u.tokens, usd: u.usd, open: () => openRun(r.run_id) }); });
  (act.jobs || []).forEach(j => rows.push({
    id: "job:" + j.job_id, kind: j.kind || "job", ts: j.ended_at || j.started_at || j.created_at, status: j.status, active: !j.terminal,
    desc: j.desc || j.job_id, extra: (j.turn_count||0) + " turns · " + (j.tool_count||0) + " tools" + (j.last_tool ? " · " + j.last_tool : ""),
    elapsed: j.elapsed_s, tokens: null, err: j.error,
    open: () => j.workflow_id ? openRun(j.workflow_id) : jumpToSession(j.cwd, j.session_id) }));
  rows.sort((a, b) => (b.active - a.active) || ((b.ts||0) - (a.ts||0)));
  if (!rows.length) {
    box.innerHTML = "";
    box.append(zero("Nothing running, nothing recorded",
      "Background jobs (sub-agents, workers, shells) and workflow runs land here with status, " +
      "elapsed time and token use — and each opens into its run or session."));
    return;
  }
  let list = box.querySelector(".act");
  if (!list) { box.innerHTML = ""; list = el("div","act"); box.append(list); }
  patchList(list, rows.slice(0, 14), r => r.id,
    r => [r.status, r.active, r.desc, r.extra, r.elapsed, r.tokens, r.usd, r.ts],
    (row, r) => {
      row = row || el("div"); row.innerHTML = ""; row.className = "arow";
      row.append(el("span","dot2 " + statusClass(r.status, r.active)));
      row.append(el("span","ak", r.kind));
      const d = el("span","ad", r.desc); d.title = r.err || r.desc;
      if (r.extra) d.append(el("small", null, r.extra));
      row.append(d);
      row.append(el("span","ae", (r.active ? "▶ " : "") + fmtDur(r.elapsed)));
      row.append(el("span","at", r.tokens != null ? fmtTok(r.tokens) + " tok" + (r.usd ? " · " + fmtUsd(r.usd) : "") : ""));
      row.append(el("span","ag", r.status + (r.ts ? " · " + ago(r.ts) : "")));
      row.onclick = r.open;
      return row;
    });
}
async function openRun(runId) {
  let r;
  try { r = await api("/api/workflow?" + q({ id: runId })); } catch (e) { toast(e.message, true); return; }
  if (!r.ok) { toast(r.error || "run not found", true); return; }
  const s = document.getElementById("sheet"); s.innerHTML = "";
  const run = r.run || {}, u = r.usage || {};
  s.append(el("h3", null, run.name || r.definition || r.run_id));
  s.append(el("div","sub", (r.definition ? r.definition + " · " : "") + r.run_id + " · " + (r.path || "")));
  const lcd = el("div","lcd tight");
  lcdCell(lcd, r.status || "?", "status", statusClass(r.status) === "bad" ? "" : "hot");
  lcdCell(lcd, fmtDur(u.elapsed_s), "elapsed", "dim");
  lcdCell(lcd, fmtTok(u.tokens), "tokens", "");
  lcdCell(lcd, fmtUsd(u.usd), "recorded spend", "hot");
  lcdCell(lcd, (u.agents_done||0) + "/" + (u.agents||0), "agents done", "dim");
  s.append(lcd);
  const inputs = Object.entries(r.inputs || {});
  if (inputs.length) { const dl = el("dl","kvs"); inputs.forEach(([k, v]) => kvRow(dl, k, String(v))); s.append(dl); }
  (run.phases || []).forEach((ph, i) => {
    const h = el("div","run-ph");
    h.append(document.createTextNode("phase " + (i+1) + " · " + (ph.title || "")));
    const sc = statusClass(ph.status);
    h.append(el("span","t2 " + (sc === "ok" ? "acc" : sc === "bad" ? "amb" : ""), ph.status || ""));
    s.append(h);
    if (ph.detail) s.append(el("div","note2", ph.detail));
    (ph.agents || []).forEach(a => {
      const box = el("div","run-ag");
      const rh = el("div","rh");
      rh.append(el("span","dot2 " + statusClass(a.status)));
      rh.append(el("b", null, a.label || a.id || "agent"));
      if (a.model) rh.append(el("span","chip", a.model));
      rh.append(el("span","sp"));
      const au = a.usage || {};
      rh.append(el("span", null, fmtTok((au.inputTokens||0) + (au.outputTokens||0)) + " tok · " +
        fmtUsd(a.cost_usd != null ? a.cost_usd : au.costUSD) + (a.turns ? " · " + a.turns + " turns" : "") +
        (a.tool_count ? " · " + a.tool_count + " tools" : "")));
      box.append(rh);
      if (a.summary || a.result) box.append(el("div","rs", a.summary || a.result));
      if (a.error) box.append(el("div","re", a.error));
      s.append(box);
    });
  });
  if ((run.log_lines || []).length) {
    s.append(el("h4", null, "log · last " + run.log_lines.length + " lines"));
    s.append(el("div","log", run.log_lines.join("\n")));
  }
  showModal(true);
}
async function jumpToSession(cwd, sid) {
  showTab("sessions");
  if (!cwd) return;
  await loadProjects();
  const tail = String(cwd).replace(/^~/, "");
  const hit = [...document.querySelectorAll("#projects .row")].find(r => {
    const p = r.querySelector(".s"); return p && (p.textContent === cwd || p.textContent.endsWith(tail));
  });
  if (!hit) { toast("that job's project isn't in the sessions list", true); return; }
  hit.click();
  if (sid) setTimeout(() => {
    const s = [...document.querySelectorAll("#sessionlist .row")].find(r => r.dataset.sid === sid);
    if (s) s.click(); else toast("session " + sid.slice(0, 8) + " has no transcript here");
  }, 450);
}

let homeReq = 0;
async function loadHome() {
  const pad = document.getElementById("homepad");
  const my = ++homeReq;
  if (!pad.childElementCount) skeleton(pad);
  const [a, g, sp, act, ov] = await Promise.all([
    api("/api/analytics"),
    api("/api/providers").catch(() => ({ families: [] })),
    api("/api/spend").catch(() => null),
    api("/api/activity").catch(() => ({ jobs: [], runs: [] })),
    api("/api/overview").catch(() => ({})),
  ]);
  if (my !== homeReq) return;
  pad.innerHTML = "";
  const t = a.totals;
  const ref = el("span","refresh"); ref.id = "refresh-ind";
  ref.append(el("span","live"), document.createTextNode(EVENTS_OK ? "live" : "live · 15s"));
  ref.title = "re-renders when something on disk changes (long-poll), 15s timer as fallback";
  pageHead(pad, "overview", null, null, [ref]);
  const fams = g.families || [];
  const readyN = fams.filter(f => f.ready).length;
  const active = (act.active_jobs || 0) + (act.active_runs || 0);
  const s7 = (sp && sp.totals && sp.totals["7"]) || {};
  const curModel = g.current && g.current.model;
  signalPath(pad, [
    { value: readyN + "/" + fams.length, label: "families ready", state: readyN ? "" : "warn", view: "models" },
    { label: curModel || "no model set", state: curModel ? "" : "warn", view: "models" },
    { value: active, label: "running", state: active ? "" : "dim",
      title: (act.active_jobs||0) + " jobs · " + (act.active_runs||0) + " workflow runs" },
    { value: t.sessions, label: "sessions", view: "sessions", state: "dim" },
    // a live GPU deployment is part of the wiring — it only shows when there is one
    ...(ov.deployments_live ? [{ value: ov.deployments_live, label: "deployed", view: "deploy",
                                 title: "live GPU deployments" }] : []),
    s7.est_usd != null || s7.rec_usd
      ? { value: "≈" + fmtUsd((s7.est_usd || 0) + (s7.rec_usd || 0)), label: "7d", state: "dim",
          title: "estimated + recorded spend, last 7 days" }
      : { value: fmtTok((s7.est_tokens || 0) + (s7.rec_in || 0) + (s7.rec_out || 0)), label: "tok · 7d", state: "dim" },
  ]);

  const famSec = section(pad, "providers · five families", "~/.mantis-agent/models.json");
  const grid = el("div","fam-grid"); grid.id = "fam-grid"; renderFamilies(grid, g); famSec.append(grid);

  const liveSec = section(pad, "live · jobs & workflow runs", act.runs_dir || "");
  const live = el("div","card2"); live.id = "live-act"; renderActivity(live, act); liveSec.append(live);

  if (sp) {
    const spSec = section(pad, "spend & usage");
    const card = el("div","card2"); card.id = "spend-card"; renderSpend(card, sp); spSec.append(card);
  }

  const actSec = section(pad, "activity · last 26 weeks");
  if (!t.messages) {
    actSec.append(zero("Nothing recorded yet",
      "Run mantis in a project and come back — every session is logged locally, and this section " +
      "turns into your trace, your working hours, and the tools the agent actually reaches for."));
    return;
  }

  // the trace
  const streak = calcStreak(a.daily);
  const series = dailySeries(a.daily, 182);
  const box = el("div","trace");
  const th = el("div","trace-h");
  th.append(el("span","trace-t", "trace · last 26 weeks"));
  const pk = el("div","trace-pk"); th.append(pk);
  box.append(th);
  const svgWrap = el("div");
  box.append(svgWrap);
  pad.append(box);
  const info = traceSVG(series, svgWrap);
  pk.innerHTML = info.peakVal
    ? "peak <b>" + fmt(info.peakVal) + "</b> messages · " + series[info.peakIdx].date
    : "";

  // the read-out strip
  const lcd = el("div","lcd");
  const cell = (v, label, hot) => {
    const d = el("div", hot ? "hot" : null);
    d.append(el("i", null, v)); d.append(document.createTextNode(label));
    lcd.append(d);
  };
  cell(fmt(t.sessions), "sessions");
  cell(t.avg_msgs_per_session, "msgs / session");
  cell(fmt(t.active_days), "active days");
  if (streak) cell(streak, "day streak", true);
  cell(Math.round(t.user_messages / Math.max(1, t.messages) * 100) + "%", "you, " +
    (100 - Math.round(t.user_messages / Math.max(1, t.messages) * 100)) + "% agent");
  cell(t.unique_tools, "distinct tools");
  pad.append(lcd);

  // when · what
  const duo = el("div","duo");
  const when = el("div","card2");
  when.append(el("h3", null, "when you work"));
  const ph = a.by_hour.indexOf(Math.max(...a.by_hour));
  const pw = a.by_weekday.indexOf(Math.max(...a.by_weekday));
  const n2 = el("div","note2");
  n2.innerHTML = "Busiest at <b>" + ph + ":00</b> on <b>" + WD[pw] + "</b> · one dot per hour, sized by volume";
  when.append(n2);
  const pw2 = el("div"); pw2.innerHTML = punchSVG(a.punchcard || [[]]); when.append(pw2);
  duo.append(when);

  const what = el("div","card2");
  what.append(el("h3", null, "what it reaches for"));
  const n3 = el("div","note2");
  n3.innerHTML = "<b>" + fmt(t.tool_calls) + "</b> tool calls across <b>" + t.unique_tools + "</b> tools";
  what.append(n3);
  loadHomeSpectrum(what, a.top_tools || [], t.tool_total || t.tool_calls || 1);
  duo.append(what);
  pad.append(duo);

  // projects ledger
  if ((a.top_projects || []).length) {
    const sec = section(pad, "projects · by volume");
    const card = el("div","card2");
    const maxM = Math.max(1, ...a.top_projects.map(p => p.msgs));
    const list = el("div","plist");
    a.top_projects.forEach(p => {
      const row = el("div","prow");
      const f = el("div","fillbar"); f.style.width = (p.msgs/maxM*100).toFixed(1) + "%"; row.append(f);
      row.append(el("span","pn", p.name));
      row.append(el("span","pp", p.cwd || ""));
      row.append(el("span","pv", fmt(p.msgs) + " msgs · " + p.sessions + " sessions" +
        ((p.in_est || p.out_est) ? " · ≈" + fmtTok((p.in_est||0) + (p.out_est||0)) + " tok" : "")));
      list.append(row);
    });
    card.append(list);
    sec.append(card);
  }
}


// ==========================================================================
// DEPLOY — bring your own GPU provider.
// Add a provider key once, search any open model, see which GPUs fit and what
// they cost, deploy with one click, watch it come up, then "Use this model" so
// the SDK and the terminal point at it. Every number here is a reading off
// the provider's own API (catalogue prices, account balance, deployment
// status); the page never guesses a dollar figure it wasn't given.
// ==========================================================================
const DEPLOY = { providers: [], deployments: [], model: null, inspect: null, q: "", sort: "trending" };
const DEP_STATE = { running: "ok", scaled_to_zero: "ok", starting: "run", pending: "run", building: "run",
                    deleting: "run", paused: "pend", failed: "bad", deleted: "", unknown: "" };
const fmtGb = g => g == null ? "—" : (Number.isInteger(g) ? g : Number(g).toFixed(1)) + " GB";
const fmtRate = v => v == null ? "—" : "$" + (v < 1 ? v.toFixed(3) : v.toFixed(2)) + "/h";
const fmtParams = b => b == null ? "—" : (b >= 1000 ? (b/1000).toFixed(1) + "T" : b >= 10 ? Math.round(b) + "B" : Number(b).toFixed(1) + "B");
const depEnv = d => d.auth_env ? d.auth_env + " (bearer)" : Object.keys(d.auth_headers || {}).length ? Object.keys(d.auth_headers).join(", ") : "none";
const shellLine = d => "MANTIS_AGENT_MODEL=" + (d.served_model_name || d.model) + " MANTIS_AGENT_BASE_URL=" + d.endpoint_url + " mantis";
const pyLine = d => 'MantisAgentOptions(model="' + (d.served_model_name || d.model) + '", backend="' + d.endpoint_url + '")';
const errText = r => (r.error || "failed") + (r.hint ? " — " + r.hint : "");
async function copyText(s, label) {
  try { await navigator.clipboard.writeText(s); toast("copied " + (label || "")); return; } catch (e) { /* fall through */ }
  const ta = el("textarea"); ta.value = s; document.body.append(ta); ta.select();
  try { document.execCommand("copy"); toast("copied " + (label || "")); } catch (e2) { toast("copy failed", true); }
  ta.remove();
}
function tag2(text, cls, title) { const t = el("span","t2 " + (cls || ""), text); if (title) t.title = title; return t; }

let deployReq = 0;
async function loadDeploy() {
  const pad = document.getElementById("deploypad");
  const my = ++deployReq;
  if (!pad.childElementCount) skeleton(pad);
  const [pv, ls] = await Promise.all([
    api("/api/deploy/providers").catch(e => ({ ok: false, error: e.message, providers: [] })),
    api("/api/deploy/list").catch(e => ({ ok: false, error: e.message, deployments: [] })),
  ]);
  if (my !== deployReq) return;
  DEPLOY.providers = pv.providers || [];
  DEPLOY.deployments = ls.deployments || [];
  pad.innerHTML = "";
  const live = DEPLOY.deployments.filter(d => d.is_live);
  const configured = DEPLOY.providers.filter(p => p.configured);
  const ref = el("span","refresh"); ref.append(el("span","live"), document.createTextNode("live · 15s"));
  ref.title = "deployments refresh every 15s while this tab is visible";
  pageHead(pad, "deploy", live.length || null,
    "Bring your own GPU cloud. Add a provider key once, pick any open model, see which GPUs fit and " +
    "what they cost, deploy with one click — then <b>use this model</b> and the SDK and terminal point at it.",
    [ref]);
  const cur = (OVERVIEW.current && OVERVIEW.current.model) || null;
  signalPath(pad, [
    { value: configured.length + "/" + DEPLOY.providers.length, label: "providers", state: configured.length ? "" : "warn",
      title: "GPU clouds with a saved credential" },
    { label: DEPLOY.model || "pick a model", state: DEPLOY.model ? "" : "dim" },
    { value: live.length, label: "deployed", state: live.length ? "" : "dim" },
    { label: cur || "no model set", state: cur ? "dim" : "warn", view: "models",
      title: "the model the SDK and terminal use now" },
  ]);
  if (pv.ok === false) {
    const b = el("div","banner"); const t = el("div","sp");
    t.innerHTML = "<b>Deploy isn't available:</b> " + esc(errText(pv)); b.append(t); pad.append(b);
  }

  const pSec = section(pad, "gpu providers", "keys → user settings env");
  const strip = el("div","dp-grid"); strip.id = "dp-grid"; renderDpProviders(strip); pSec.append(strip);

  const mSec = section(pad, "pick a model", "huggingface.co");
  if (!configured.length) {
    mSec.append(zero("Add a GPU provider to deploy any model",
      "Paste one provider key above. Then this turns into a search over every open model on the Hub — " +
      "with size, dtype, license and whether vLLM can serve it — and each one shows the GPUs that fit."));
  } else renderDpPicker(mSec);

  const fSec = section(pad, "fit & deploy"); fSec.id = "dp-fit"; renderFit(fSec);

  const dSec = section(pad, "deployments");
  const tbl = el("div"); tbl.id = "dp-deps"; renderDeployments(tbl); dSec.append(tbl);
}

// ---- providers strip ----
// One card per adapter: its mark, whether a key is saved and whether it
// validated (with the balance when the provider says), what it can do
// (scale to zero, public endpoint), and the inline key form generated from
// the adapter's own credential_fields. Values go up; only names come back.
function renderDpProviders(box) {
  if (!DEPLOY.providers.length) {
    box.innerHTML = "";
    box.append(zero("No deploy providers registered", "This build has no GPU adapters — update mantis-agent-sdk."));
    return;
  }
  patchList(box, DEPLOY.providers, p => p.id, p => [p.configured, p.account, p.display_name, p.engines], (card, p) => {
    const acct = p.account;
    const ok = !!(acct && acct.ok);
    card = card || el("div"); card.innerHTML = ""; card.className = "dpc" + (p.configured ? " on" : ""); card.id = "dpc-" + p.id;
    const fh = el("div","fh");
    fh.append(providerMark(p.logo || p.id, p.display_name));
    fh.append(el("span","fn", p.display_name || p.id));
    const sp = el("span"); sp.style.flex = "1"; fh.append(sp);
    const d = el("span","dot2 " + (ok ? "ok" : p.configured ? "warn" : ""));
    d.title = ok ? "validated" : p.configured ? "key saved, not yet validated" : "no key"; fh.append(d);
    card.append(fh);
    const fa = el("div","fa");
    fa.append(tag2(ok ? "validated" : p.configured ? "key saved" : "no key", ok ? "acc" : p.configured ? "amb" : ""));
    if (ok) {
      const bits = [];
      if (acct.user) bits.push(acct.user);
      if (acct.balance_usd != null) bits.push(fmtUsd(acct.balance_usd) + " balance");
      if (acct.credits_usd != null) bits.push(fmtUsd(acct.credits_usd) + " credits");
      if (bits.length) { const m = el("span","mono", bits.join(" · ")); m.title = bits.join(" · "); fa.append(m); }
    } else if (acct && acct.message) { const e = el("span","dp-err", acct.message); e.title = acct.message; fa.append(e); }
    card.append(fa);
    const badges = el("div","chips");
    badges.append(tag2(p.scale_to_zero ? "scale to zero" : "always warm", p.scale_to_zero ? "acc" : "amb",
      p.scale_to_zero ? "min_replicas=0 is honoured — idle costs nothing" : "a warm replica bills while idle"));
    if (p.public_by_default) badges.append(tag2("public endpoint", "amb", "reachable by anyone with the URL — keep the auth env set"));
    if (p.id === "vastai") badges.append(tag2("plain http", "amb", "traffic to this endpoint is not encrypted"));
    (p.engines || []).forEach(e => badges.append(el("span","chip", e)));
    card.append(badges);
    const ff = el("div","ff");
    const form = el("div","dp-form");
    const addB = btn(p.configured ? "Replace key" : "Add key", p.configured ? "" : "pri", () => {
      const on = form.classList.toggle("on");
      if (on) { credForm(p, form); const i = form.querySelector("input"); if (i) i.focus(); }
    });
    ff.append(addB);
    if (p.configured) {
      const vb = btn("Validate", "", async () => {
        vb.disabled = true; vb.textContent = "Checking…";
        try {
          const r = await post("/api/deploy/validate", { provider: p.id });
          if (r.ok) toast("✓ " + (p.display_name || p.id) + (r.account && r.account.balance_usd != null ? " · " + fmtUsd(r.account.balance_usd) + " balance" : " validated"));
          else toast(errText(r), true);
          p.account = r.account || { ok: false, message: r.error };
          renderDpProviders(box);
        } catch (e) { toast(e.message, true); vb.disabled = false; vb.textContent = "Validate"; }
      });
      ff.append(vb);
    }
    if (p.console_url) ff.append(extLink("b gho", "console ↗", p.console_url));
    card.append(ff, form);
    return card;
  });
}
function credForm(p, form) {
  form.innerHTML = "";
  const inputs = {};
  (p.credential_fields || []).forEach(f => {
    const row = el("div","dp-field");
    const lab = el("span","kh-l", f.env + (f.required === false ? " · optional" : ""));
    const inp = input(f.label || f.env, f.secret !== false); inp.autocomplete = "off"; inputs[f.env] = inp;
    row.append(lab, inp);
    if (f.help) row.append(el("div","kh-n", f.help));
    form.append(row);
  });
  const foot = el("div","cta");
  const save = btn("Save & validate", "pri", async () => {
    const values = {};
    Object.entries(inputs).forEach(([k, i]) => { if (i.value.trim()) values[k] = i.value.trim(); });
    const missing = (p.credential_fields || []).filter(f => f.required !== false && !values[f.env]);
    if (missing.length) { toast("fill in " + missing[0].env, true); inputs[missing[0].env].focus(); return; }
    save.disabled = true;
    try {
      const r = await post("/api/deploy/creds", { provider: p.id, values });
      if (r.ok) {
        const a = r.account || {};
        toast(a.ok ? "✓ " + (p.display_name || p.id) + " validated" : "saved · " + (a.message || "validation failed"), !a.ok);
        Object.values(inputs).forEach(i => (i.value = ""));
        loadDeploy(); loadOverview();
      } else toast(errText(r), true);
    } catch (e) { toast(e.message, true); } finally { save.disabled = false; }
  });
  foot.append(save, btn("Cancel", "gho", () => form.classList.remove("on")));
  if (p.console_url) foot.append(extLink("a-link", "get a key ↗", p.console_url));
  form.append(foot);
  form.querySelectorAll("input").forEach(i => (i.onkeydown = e => { if (e.key === "Enter") save.click(); }));
}

// ---- model picker: the Hub, in the model-table shape ----
let modelSearchReq = 0;
function renderDpPicker(sec) {
  const bar = el("div","filters");
  const find = findBox("Search the Hub — llama, qwen, gemma, deepseek…  ( / )");
  find.wrap.style.marginBottom = "0"; find.wrap.style.flex = "1"; find.input.value = DEPLOY.q;
  bar.append(find.wrap);
  const chips = el("div","fchips");
  [["trending","trending"], ["downloads","downloads"], ["likes","likes"]].forEach(([k, lab]) => {
    const c = el("button","fchip" + (k === DEPLOY.sort ? " on" : ""), lab);
    c.onclick = () => { DEPLOY.sort = k; chips.querySelectorAll(".fchip").forEach(x => x.classList.toggle("on", x === c)); runSearch(); };
    chips.append(c);
  });
  bar.append(chips); sec.append(bar);
  const list = el("div","mtable dp-models"); list.id = "dp-models"; sec.append(list);
  let t = null;
  const runSearch = async () => {
    DEPLOY.q = find.input.value.trim();
    list.innerHTML = ""; list.append(el("div","browse-empty", DEPLOY.q ? "searching the Hub…" : "loading curated models…"));
    const my = ++modelSearchReq;
    let r;
    try { r = await api("/api/deploy/models?" + q({ q: DEPLOY.q, sort: DEPLOY.sort, limit: 30 })); }
    catch (e) { r = { ok: false, error: e.message, models: [] }; }
    if (my !== modelSearchReq) return;
    renderModelRows(list, r);
  };
  find.input.oninput = () => { clearTimeout(t); t = setTimeout(runSearch, 320); };
  find.input.onkeydown = e => {
    if (e.key === "Enter") { clearTimeout(t); runSearch(); }
    else if (e.key === "Escape") { find.input.value = ""; runSearch(); }
  };
  runSearch();
}
function renderModelRows(list, r) {
  list.innerHTML = "";
  const head = el("div","mhead dp-mhead");
  ["model", "params", "dtype", "license", "serving", "est. vram", ""].forEach(x => head.append(el("span", null, x)));
  list.append(head);
  if (r.ok === false) { list.append(el("div","browse-empty", errText(r))); return; }
  if (r.curated) { const fh = el("div","mfam"); fh.append(document.createTextNode("curated · good first deploys")); list.append(fh); }
  if (!(r.models || []).length) {
    list.append(el("div","browse-empty", r.curated ? "Nothing curated yet — type to search the Hub."
                                                   : "No text-generation model matches “" + r.query + "”."));
    return;
  }
  r.models.forEach(m => {
    const row = el("div","mrow dp-mrow" + (m.id === DEPLOY.model ? " cur" : ""));
    row.dataset.model = m.id;
    const mn = el("span","mn", m.id); mn.title = m.id; row.append(mn);
    row.append(el("span","mctx", fmtParams(m.params_b)));
    row.append(el("span","mp", m.dtype || "—"));
    const lic = el("span","mp", m.license || "—"); lic.title = m.license || ""; row.append(lic);
    const caps = el("span","mcaps");
    if (m.gated) { const c = el("span","cap amb","gated"); c.title = "needs an HF token with access"; caps.append(c); }
    if (m.vllm_ok === true) { const c = el("span","cap ok","vllm ✓"); c.title = "architecture served by vLLM"; caps.append(c); }
    else if (m.vllm_ok === false) { const c = el("span","cap bad","vllm ✗"); c.title = m.reason || "not servable by vLLM"; caps.append(c); }
    else { const c = el("span","cap","vllm ?"); c.title = "architecture not in the table"; caps.append(c); }
    row.append(caps);
    row.append(el("span","mctx", m.est_vram_gb != null ? fmtGb(m.est_vram_gb) : "—"));
    row.append(el("span","mgo", m.id === DEPLOY.model ? "selected" : "inspect →"));
    row.onclick = () => pickModel(m.id);
    list.append(row);
  });
}
async function pickModel(id) {
  DEPLOY.model = id; DEPLOY.inspect = null;
  document.querySelectorAll("#dp-models .dp-mrow").forEach(r => {
    const on = r.dataset.model === id;
    r.classList.toggle("cur", on);
    const g = r.querySelector(".mgo"); if (g) g.textContent = on ? "selected" : "inspect →";
  });
  const sec = document.getElementById("dp-fit"); if (!sec) return;
  renderFit(sec, true);
  sec.scrollIntoView({ behavior: "smooth", block: "start" });
  let r;
  try { r = await api("/api/deploy/inspect?" + q({ model: id })); }
  catch (e) { r = { ok: false, error: e.message }; }
  if (DEPLOY.model !== id) return;
  DEPLOY.inspect = r; renderFit(sec);
}

// ---- fit & deploy ----
// For the selected model: what it is (from the Hub), then one table per
// configured provider — GPU, VRAM, $/h, a fits/tight/no verdict, cold-start
// behaviour — with a Deploy button per row that names the cost before it
// commits anything.
function renderFit(sec, loading) {
  [...sec.children].forEach(x => { if (!x.classList.contains("sec-t")) x.remove(); });
  if (!DEPLOY.model) {
    sec.append(zero("Pick a model above",
      "Its architecture, size and dtype come from the Hub; every configured provider's GPU catalogue " +
      "is checked against it and priced per hour. Deploying is one click on a row."));
    return;
  }
  if (loading || !DEPLOY.inspect) { sec.append(el("div","card2 dp-loading", "inspecting " + DEPLOY.model + "…")); return; }
  const r = DEPLOY.inspect;
  if (r.ok === false) {
    const p = el("div","probe bad"); const h = el("div","ph2");
    h.append(el("span","dot2 bad"), document.createTextNode("Couldn't inspect " + DEPLOY.model));
    p.append(h, el("div","pe", errText(r))); sec.append(p); return;
  }
  const m = r.model || {};
  const card = el("div","card2");
  const h = el("div","dp-mh");
  h.append(el("span","dp-mid", m.id));
  if (m.gated) h.append(tag2("gated", "amb", "needs an HF token with access"));
  h.append(tag2(m.vllm_ok ? "vllm ok" : m.vllm_ok === false ? "vllm: " + (m.reason || "unsupported") : "vllm unknown",
                m.vllm_ok ? "acc" : m.vllm_ok === false ? "amb" : "", m.reason || ""));
  card.append(h);
  const lcd = el("div","lcd tight");
  lcdCell(lcd, fmtParams(m.params_b), "params", "");
  lcdCell(lcd, m.dtype || "—", "dtype", "dim");
  lcdCell(lcd, m.context_len ? fmtCtx(m.context_len) : "—", "context", "dim");
  lcdCell(lcd, m.est_vram_gb != null ? fmtGb(m.est_vram_gb) : "—", "est. vram", "hot");
  lcdCell(lcd, m.license || "—", "license", "dim");
  if (m.downloads != null) lcdCell(lcd, fmtTok(m.downloads), "downloads", "dim");
  card.append(lcd);
  if ((m.architectures || []).length) card.append(el("div","note2", m.architectures.join(", ")));
  sec.append(card);
  if (!(r.fits || []).length) {
    sec.append(zero("No configured provider",
      "Add a key to a GPU provider above and this fills with every GPU that fits, priced per hour."));
    return;
  }
  r.fits.forEach(f => sec.append(fitTable(f, m)));
}
function fitTable(f, m) {
  const p = DEPLOY.providers.find(x => x.id === f.provider) || {};
  const box = el("div","card2 dp-fitbox");
  const head = el("div","dp-mh");
  head.append(providerMark(p.logo || f.provider, f.display_name));
  head.append(el("span","dp-mid", f.display_name || f.provider));
  head.append(tag2(f.scale_to_zero ? "scale to zero" : "always warm", f.scale_to_zero ? "acc" : "amb"));
  if (f.public_by_default) head.append(tag2("public endpoint", "amb", "reachable by anyone with the URL"));
  const sp = el("span"); sp.style.flex = "1"; head.append(sp);
  const eng = el("select","in");
  (f.engines && f.engines.length ? f.engines : ["vllm"]).forEach(e => { const o = el("option", null, e); o.value = e; eng.append(o); });
  const engL = el("label","dp-eng"); engL.append(document.createTextNode("engine"), eng); head.append(engL);
  box.append(head);
  if (f.error) { box.append(el("div","pe", f.error)); return box; }

  // advanced — every knob DeployOpts has; blank means the adapter's default
  const A = {};
  const adv = el("details","dp-adv"); adv.append(el("summary", null, "advanced · context, parallelism, replicas, gated token"));
  const grid = el("div","dp-advgrid");
  const field = (k, node, label) => { A[k] = node; const w = el("label","dp-field"); w.append(el("span","kh-l", label || k), node); grid.append(w); };
  const num = (k, ph, val, label) => { const i = input(ph); i.type = "number"; i.min = "0"; if (val != null) i.value = val; field(k, i, label); };
  num("max_model_len", m.context_len ? "≤ " + m.context_len : "tokens");
  num("tensor_parallel", "defaults to gpu count");
  const quant = el("select","in");
  ["", "fp8", "awq", "gptq", "int8", "bitsandbytes"].forEach(v => { const o = el("option", null, v || "none"); o.value = v; quant.append(o); });
  field("quantization", quant);
  num("min_replicas", "0 = scale to zero", f.scale_to_zero ? 0 : 1);
  num("max_replicas", "", 1);
  num("idle_timeout_s", "seconds", 300, "idle timeout (s)");
  if (m.gated) { const hf = input("HF token with access to this repo", true); hf.autocomplete = "off"; field("hf_token", hf, "HF_TOKEN · gated repo"); }
  const trc = document.createElement("input"); trc.type = "checkbox"; A.trust_remote_code = trc;
  const tl = el("label","chk"); tl.append(trc, document.createTextNode("trust_remote_code")); grid.append(tl);
  adv.append(grid); box.append(adv);

  const gpus = f.gpus || [];
  if (!gpus.length) { box.append(el("div","browse-empty", "No GPU in this catalogue is large enough for this model.")); return box; }
  const tbl = el("div","dp-ftab");
  const th = el("div","dp-frow head");
  ["gpu", "vram", "$ / hour", "fit", "cold start", ""].forEach(x => th.append(el("span", null, x)));
  tbl.append(th);
  gpus.forEach(g => {
    const row = el("div","dp-frow" + (g.verdict === "no" ? " no" : ""));
    const nm = el("span","dp-gn", g.display || g.provider_id);
    if (g.region) nm.append(el("small", null, " " + g.region));
    row.append(nm);
    row.append(el("span","dp-gv", fmtGb(g.total_vram_gb) + (g.count > 1 ? " · " + g.count + "×" + g.vram_gb : "")));
    row.append(el("span","dp-gp", fmtRate(g.price_per_hour)));
    const vd = el("span","vd " + g.verdict, g.verdict);
    vd.title = g.reason || (g.verdict === "tight" ? "under 15% headroom" : g.verdict === "fits" ? "fits with headroom" : "");
    row.append(vd);
    row.append(el("span","dp-gc", f.scale_to_zero ? "from zero · first request waits" : "warm · billed while idle"));
    const act = el("span","dp-ga");
    if (g.verdict !== "no") {
      const b = btn("Deploy", "pri", () => confirmDeploy(f, g, m, eng.value, A));
      if (g.available === false) { b.disabled = true; b.title = "no capacity right now"; }
      act.append(b);
    } else act.append(el("span","mgo", g.reason || "too small"));
    row.append(act);
    if (g.reason) row.title = g.reason;
    tbl.append(row);
  });
  box.append(tbl);
  return box;
}
function collectOpts(A) {
  const o = {};
  Object.entries(A).forEach(([k, i]) => {
    if (i.type === "checkbox") { if (i.checked) o[k] = true; }
    else if (i.value !== "" && i.value != null) o[k] = i.value;
  });
  return o;
}
// The cost line, before anything is created: "$X/h while running · $Y/h idle".
function confirmDeploy(f, g, m, engine, A) {
  const opts = collectOpts(A);
  const mx = Math.max(1, parseInt(opts.max_replicas || "1", 10) || 1);
  const mn = Math.max(0, parseInt(opts.min_replicas || "0", 10) || 0);
  const rate = g.price_per_hour;
  const run = rate == null ? null : rate * mx;
  const idle = f.scale_to_zero && mn === 0 ? 0 : (rate == null ? null : rate * Math.max(1, mn));
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(el("h3", null, "Deploy " + m.id));
  s.append(el("div","sub", (f.display_name || f.provider) + " · " + (g.display || g.provider_id) + " · " + engine));
  const dl = el("dl","kvs");
  kvRow(dl, "gpu", (g.display || g.provider_id) + " · " + fmtGb(g.total_vram_gb) + (g.region ? " · " + g.region : ""));
  kvRow(dl, "engine", engine);
  kvRow(dl, "replicas", mn + " – " + mx + (f.scale_to_zero && mn === 0 ? " (scales to zero)" : ""));
  if (opts.max_model_len) kvRow(dl, "max len", String(opts.max_model_len));
  if (opts.quantization) kvRow(dl, "quant", opts.quantization);
  if (opts.tensor_parallel) kvRow(dl, "tensor par.", String(opts.tensor_parallel));
  if (m.gated) kvRow(dl, "hf token", opts.hf_token ? "provided" : "none — a gated repo will fail to download");
  s.append(dl);
  const c = el("div","dp-cost");
  c.innerHTML = "<b>" + esc(run == null ? "unknown" : fmtRate(run)) + "</b> while running · <b>" +
    esc(idle == null ? "unknown" : fmtRate(idle)) + "</b> idle" +
    (f.scale_to_zero && mn === 0 ? " — nothing while scaled to zero" : " — a warm pool keeps billing");
  s.append(c);
  if (f.public_by_default) {
    const b = el("div","banner");
    b.append(document.createTextNode("This provider's endpoint is reachable by anyone with the URL. Keep the auth env var set and tear down when you're done."));
    s.append(b);
  }
  const foot = el("div","cta");
  const go = btn("Deploy", "pri", async () => {
    go.disabled = true; go.textContent = "Starting…";
    try {
      const r = await post("/api/deploy/up", { provider: f.provider, model: m.id, gpu: g.provider_id, engine, opts });
      if (r.ok) openJobSheet(r.job, { kind: "deploy", model: m.id, provider: f.display_name || f.provider, gpu: g.display || g.provider_id });
      else { toast(errText(r), true); go.disabled = false; go.textContent = "Deploy"; }
    } catch (e) { toast(e.message, true); go.disabled = false; go.textContent = "Deploy"; }
  });
  foot.append(go, btn("Cancel", "gho", hideModal));
  s.append(foot);
  showModal();
}

// ---- the job sheet: progress lines streamed from a background job ----
let jobPollT = null;
function stopJobPoll() { if (jobPollT) { clearInterval(jobPollT); jobPollT = null; } }
function openJobSheet(jobId, ctx) {
  stopJobPoll();
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(el("h3", null, (ctx.kind === "teardown" ? "Tearing down " : "Deploying ") + ctx.model));
  s.append(el("div","sub", [ctx.provider, ctx.gpu].filter(Boolean).join(" · ") + " · job " + jobId));
  const lcd = el("div","lcd tight");
  const st = el("div","hot"); const stI = el("i", null, "running"); st.append(stI, document.createTextNode("status"));
  const ep = el("div","dim"); const elI = el("i", null, "0s"); ep.append(elI, document.createTextNode("elapsed"));
  lcd.append(st, ep); s.append(lcd);
  const log = el("div","log dp-log", "starting…"); s.append(log);
  const done = el("div"); s.append(done);
  const tick = async () => {
    let j;
    try { j = await api("/api/deploy/job?" + q({ id: jobId })); } catch (e) { return; }
    if (!j.ok) { stopJobPoll(); stI.textContent = "lost"; st.className = ""; log.textContent = j.error || "job not found"; return; }
    elI.textContent = fmtDur(j.elapsed_s);
    log.textContent = (j.lines || []).join("\n") || "waiting for the provider…";
    log.scrollTop = log.scrollHeight;
    if (j.status === "running") return;
    stopJobPoll();
    stI.textContent = j.status; st.className = j.status === "done" ? "hot" : "";
    done.innerHTML = "";
    if (j.status === "error") {
      done.append(el("div","pe", (j.error || "failed") + (j.hint ? " — " + j.hint : "")));
      const f = el("div","cta"); f.append(btn("Close", "gho", hideModal)); done.append(f);
    } else if (ctx.kind === "teardown") {
      done.append(el("div","note2", "Deleted on the provider and marked deleted here. Billing for it has stopped."));
      const f = el("div","cta"); f.append(btn("Close", "pri", hideModal)); done.append(f);
    } else renderDeployDone(done, j.result || {});
    refreshDeployments(false); loadOverview();
  };
  tick(); jobPollT = setInterval(tick, 1500);
  showModal(true);
}
function renderDeployDone(box, d) {
  box.innerHTML = "";
  const dl = el("dl","kvs");
  kvRow(dl, "status", (d.status || "?").replace(/_/g, " "));
  kvRow(dl, "endpoint", d.endpoint_url || "pending");
  kvRow(dl, "model=", d.served_model_name || d.model || "—");
  kvRow(dl, "auth", depEnv(d));
  box.append(dl);
  if (d.message) box.append(el("div","note2", d.message));
  const f = el("div","cta");
  f.append(btn("Use this model", "pri", () => useDeployment(d, box)));
  if (d.endpoint_url) {
    f.append(btn("Copy shell", "", () => copyText(shellLine(d), "shell line")));
    f.append(btn("Copy Python", "", () => copyText(pyLine(d), "python snippet")));
  }
  f.append(btn("Close", "gho", hideModal));
  box.append(f);
}
// Connect: the server re-checks /models (retrying a cold start), then makes
// this endpoint the current model + backend for the SDK and the terminal.
async function useDeployment(d, box) {
  // optimistic: the top bar shows the new model at once; a failure puts it back
  const prev = OVERVIEW.current, prevHost = OVERVIEW.hosting;
  OVERVIEW.current = { model: d.served_model_name || d.model, backend: d.endpoint_url };
  OVERVIEW.hosting = { kind: "selfhost", label: "deployment · " + (d.name || d.provider) };
  renderTopStatus(OVERVIEW);
  let r;
  try { r = await post("/api/deploy/connect", { id: d.id }); }
  catch (e) { OVERVIEW.current = prev; OVERVIEW.hosting = prevHost; renderTopStatus(OVERVIEW); toast(e.message, true); return; }
  if (!r.ok) { OVERVIEW.current = prev; OVERVIEW.hosting = prevHost; renderTopStatus(OVERVIEW); toast(errText(r), true); return; }
  toast("current model → " + r.model);
  loadOverview();
  if (!box) return;
  box.innerHTML = "";
  const dl = el("dl","kvs");
  kvRow(dl, "model", r.model || "—"); kvRow(dl, "backend", r.backend || "—");
  kvRow(dl, "auth env", r.api_key_env || "none");
  box.append(dl);
  box.append(el("div","note2", "The next mantis launch and any MantisAgentOptions() without a model use this."));
  const jb = jsonBox("paste into a shell", {}, null); jb.pre.textContent = r.shell || shellLine({ ...d, ...r, endpoint_url: r.backend }); box.append(jb.box);
  const jp = jsonBox("or in python", {}, null); jp.pre.textContent = r.python || pyLine({ ...d, endpoint_url: r.backend, served_model_name: r.model }); box.append(jp.box);
  const f = el("div","cta");
  f.append(btn("Copy shell", "", () => copyText(jb.pre.textContent, "shell line")));
  f.append(btn("Copy Python", "", () => copyText(jp.pre.textContent, "python snippet")));
  f.append(btn("Close", "gho", hideModal));
  box.append(f);
}

// ---- deployments table ----
async function refreshDeployments(refresh) {
  const tbl = document.getElementById("dp-deps"); if (!tbl) return;
  let r;
  try { r = await api("/api/deploy/list" + (refresh ? "?refresh=1" : "")); } catch (e) { return; }
  DEPLOY.deployments = r.deployments || [];
  renderDeployments(tbl);
}
function renderDeployments(tbl) {
  const deps = DEPLOY.deployments;
  if (!deps.length) {
    tbl.innerHTML = "";
    const z = zero("Nothing deployed yet", "Three steps, all on this page:");
    const steps = el("div","dp-steps");
    [["1", "add a GPU provider key"], ["2", "pick a model and a GPU that fits"], ["3", "Deploy, then “Use this model”"]].forEach(([n, t]) => {
      const s = el("span"); s.append(el("b", null, n), document.createTextNode(t)); steps.append(s);
    });
    z.append(steps); tbl.append(z);
    return;
  }
  let rows = tbl.querySelector(".dp-rows");
  if (!rows) {
    tbl.innerHTML = "";
    const list = el("div","list");
    const head = el("div","dp-drow head");
    ["", "name", "model", "gpu", "status", "endpoint", "$ / h", "age", ""].forEach(x => head.append(el("span", null, x)));
    rows = el("div","dp-rows"); list.append(head, rows); tbl.append(list);
  }
  patchList(rows, deps, d => d.id, d => [d.status, d.endpoint_url, d.cost, d.message, d.name, d.updated_at, d.is_live], (row, d) => {
    const p = DEPLOY.providers.find(x => x.id === d.provider) || {};
    row = row || el("div"); row.innerHTML = ""; row.className = "dp-drow" + (d.is_live ? "" : " off");
    const mk = el("span"); mk.append(providerMark(p.logo || d.provider, p.display_name || d.provider)); row.append(mk);
    const nm = el("span","dp-dn", d.name || d.id); nm.title = d.id + " · " + (p.display_name || d.provider); row.append(nm);
    const mdl = el("span","dp-dm", d.model); mdl.title = "model= " + (d.served_model_name || d.model); row.append(mdl);
    row.append(el("span","dp-dg", d.gpu ? (d.gpu.display || d.gpu.provider_id) + " · " + fmtGb(d.gpu.total_vram_gb) : "—"));
    const stw = el("span","dp-ds");
    stw.append(el("span","dot2 " + (DEP_STATE[d.status] || "")), document.createTextNode(String(d.status || "?").replace(/_/g, " ")));
    stw.title = d.message || ""; row.append(stw);
    const ep = el("span","dp-de");
    if (d.endpoint_url) {
      const a = el("span","mono clk", d.endpoint_url.replace(/^https?:\/\//, ""));
      a.title = "copy " + d.endpoint_url; a.onclick = () => copyText(d.endpoint_url, "endpoint"); ep.append(a);
    } else ep.append(el("span","mgo", "—"));
    row.append(ep);
    const c = d.cost || {};
    const listRate = d.gpu && d.gpu.price_per_hour != null ? d.gpu.price_per_hour : null;
    const ce = el("span","dp-dc", c.per_hour_usd != null
      ? fmtRate(c.per_hour_usd) + (c.accrued_usd != null ? " · " + fmtUsd(c.accrued_usd) : "")
      : (listRate != null && d.is_live ? fmtRate(listRate) + "*" : "—"));
    ce.title = c.basis || (listRate != null ? "* list price of the GPU" : "the provider didn't say");
    row.append(ce);
    row.append(el("span","dp-da", d.created_at ? ago(d.created_at) : ""));
    const acts = el("span","dp-dx");
    if (d.is_live) acts.append(btn("Use", "pri", () => useDeployment(d)));
    acts.append(btn("Logs", "gho", () => openLogs(d)));
    if (d.status !== "deleted" && d.status !== "deleting") acts.append(btn("Teardown", "gho dan", () => confirmTeardown(d)));
    row.append(acts);
    return row;
  });
}
async function openLogs(d) {
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(el("h3", null, "Logs · " + (d.name || d.id)));
  s.append(el("div","sub", d.provider + " · " + d.model));
  const bar = el("div"); bar.style = "display:flex;gap:8px;align-items:center;margin-bottom:8px";
  const tail = el("select","in"); [100, 200, 500, 1000].forEach(n => { const o = el("option", null, "last " + n); o.value = n; tail.append(o); }); tail.value = "200";
  const rb = btn("Refresh", "", () => load());
  const st = el("span","refresh"); bar.append(tail, rb, st); s.append(bar);
  const log = el("div","log dp-log", "loading…"); log.style.maxHeight = "60vh"; s.append(log);
  const load = async () => {
    rb.disabled = true; st.textContent = "fetching…";
    try {
      const r = await api("/api/deploy/logs?" + q({ id: d.id, tail: tail.value }));
      if (r.ok) { log.textContent = (r.lines || []).join("\n") || "(no output yet)"; st.textContent = (r.lines || []).length + " lines"; log.scrollTop = log.scrollHeight; }
      else { log.textContent = r.supported === false ? "This provider has no logs API. " + (r.hint || "") : errText(r); st.textContent = ""; }
    } catch (e) { log.textContent = e.message; }
    finally { rb.disabled = false; }
  };
  tail.onchange = load;
  showModal(true); load();
}
function confirmTeardown(d) {
  const c = d.cost || {};
  const rate = c.per_hour_usd != null ? c.per_hour_usd : (d.gpu && d.gpu.price_per_hour != null ? d.gpu.price_per_hour : null);
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(el("h3", null, "Tear down " + (d.name || d.id) + "?"));
  s.append(el("div","sub", d.provider + " · " + d.model + (d.gpu ? " · " + (d.gpu.display || d.gpu.provider_id) : "")));
  const cost = el("div","dp-cost");
  cost.innerHTML = rate != null
    ? "This stops <b>" + esc(fmtRate(rate)) + "</b>" + (c.accrued_usd != null ? " · " + esc(fmtUsd(c.accrued_usd)) + " accrued so far" : "") + "."
    : "The provider didn't report a rate for this deployment; it stops whatever it was billing.";
  s.append(cost);
  s.append(el("div","note2", "Deletes the endpoint on the provider and marks it deleted here. The model weights are not affected."));
  const foot = el("div","cta");
  const go = btn("Tear down", "dan", async () => {
    go.disabled = true;
    // optimistic: the row reads "deleting" now; rolled back if the request fails
    const prev = { status: d.status, is_live: d.is_live };
    d.status = "deleting"; d.is_live = false;
    const tbl = document.getElementById("dp-deps"); if (tbl) renderDeployments(tbl);
    const rollback = () => { d.status = prev.status; d.is_live = prev.is_live; if (tbl) renderDeployments(tbl); go.disabled = false; };
    try {
      const r = await post("/api/deploy/down", { id: d.id });
      if (r.ok) openJobSheet(r.job, { kind: "teardown", model: d.name || d.model, provider: d.provider });
      else { toast(errText(r), true); rollback(); }
    } catch (e) { toast(e.message, true); rollback(); }
  });
  go.classList.add("armed");
  foot.append(go, btn("Cancel", "gho", hideModal));
  s.append(foot);
  showModal();
}

// ---- top bar + nav ----
// The rail's foot is the one always-visible answer to "what is this agent
// wired to right now" — the model it will use, how that model is reached, and
// how much is plugged in. Every number is a link to the page that changes it.
let OVERVIEW = {};
function renderTopStatus(o) {
  const f = document.getElementById("railfoot");
  const cur = (o.current && o.current.model) ? o.current.model : "no model set";
  const h = o.hosting || {};
  f.innerHTML = "";
  f.append(el("span","live" + (h.label || h.kind === "selfhost" ? "" : " off")));
  const v = el("span","rf-v", cur); v.title = "current model · click for models"; f.append(v);
  const s = el("span","rf-s", "via " + (h.label || (h.kind === "selfhost" ? "your server" : "no provider")) +
    (o.deployments_live ? " · " + o.deployments_live + " deployed" : "") +
    ((o.active_jobs || 0) + (o.active_runs || 0) ? " · " + ((o.active_jobs||0) + (o.active_runs||0)) + " running" : ""));
  f.append(s);
  f.style.cursor = "pointer"; f.onclick = () => showTab(o.deployments_live && !h.label ? "deploy" : "models");
}
async function loadOverview() {
  const o = await api("/api/overview");
  OVERVIEW = o;
  renderTopStatus(o);
}
const VIEWS = ["home","sessions","models","deploy","mcp","skills","config"];
let curView = "home";
function showTab(name) {
  const b = document.querySelector('#nav button[data-v="' + name + '"]');
  if (!b) return;
  curView = name;
  document.querySelectorAll("#nav button").forEach(x => x.classList.toggle("on", x === b));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("on", v.id === name));
  hideModal();                       // a sheet must never outlive its page
  if (location.hash !== "#" + name) location.hash = name;  // fires hashchange; guarded below
  if (name === "home") loadHome();
  if (name === "models") loadModels();
  if (name === "deploy") loadDeploy();
  if (name === "skills") loadSkills();
  if (name === "mcp") loadMcp();
  if (name === "config") loadConfig();
}
document.getElementById("nav").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  showTab(b.dataset.v);
});
// Keyboard: 1–7 jump between pages (the tabs show each key), `g` then a
// letter does the same by name (g o · g s · g m · g d · g p · g k · g c), `/`
// drops into whatever search the current page has, ⌘K opens the palette.
let chord = null, chordT = null;
const CHORDS = { o: "home", s: "sessions", m: "models", d: "deploy", p: "mcp", k: "skills", c: "config" };
function focusSearch() {
  const v = document.querySelector(".view.on");
  const inp = curView === "sessions" ? document.getElementById("sessfind")
            : (v && v.querySelector(".find input, input[type=search]"));
  if (!inp) return false;
  inp.focus(); if (inp.select) inp.select();
  return true;
}
window.addEventListener("keydown", e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
  if (document.getElementById("modal").className || document.getElementById("palette").className) return;
  if (chord === "g") {
    chord = null; clearTimeout(chordT);
    if (CHORDS[e.key]) { showTab(CHORDS[e.key]); e.preventDefault(); }
    return;
  }
  if (e.key === "g") { chord = "g"; chordT = setTimeout(() => (chord = null), 900); return; }
  if (e.key === "/") { if (focusSearch()) e.preventDefault(); return; }
  const i = "1234567".indexOf(e.key);
  if (i >= 0) { showTab(VIEWS[i]); e.preventDefault(); }
});
// ---- reactive refresh ----
// /api/events is a version counter over everything the pages render. The page
// long-polls it and re-renders (through patchList — no scroll reset, no closed
// drawer) only when the version moves. The 15s timer stays as the fallback
// when the long-poll can't connect. Both pause while the tab is hidden.
let refreshT = null, EVENTS_OK = false, EVER = null, refreshing = false;
async function refreshLive(force) {
  if (document.hidden || refreshing) return;
  if (!force && EVENTS_OK) return;          // the event stream drives refreshes
  refreshing = true;
  try {
    const p = loadOverview();
    if (curView === "home") {
      const [g, act] = await Promise.all([api("/api/providers"), api("/api/activity")]);
      const grid = document.getElementById("fam-grid");
      if (grid) renderFamilies(grid, g);
      const live = document.getElementById("live-act");
      if (live) renderActivity(live, act);
    }
    if (curView === "sessions") { await loadProjects(); if (curProject) await loadSessions(curProject); }
    if (curView === "deploy" && !document.querySelector("#dp-grid .dp-form.on")) await refreshDeployments(false);
    await p;
  } catch (e) { /* transient — the next tick retries */ }
  finally { refreshing = false; }
}
function startRefresh() { if (refreshT) clearInterval(refreshT); refreshT = setInterval(() => refreshLive(false), 15000); }
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function watchEvents() {
  for (;;) {
    if (document.hidden) { await sleep(1500); continue; }
    try {
      const r = await api("/api/events?" + q({ since: EVER || "", timeout: 25 }));
      EVENTS_OK = true;
      const moved = EVER && r.version !== EVER;
      EVER = r.version;
      if (moved) await refreshLive(true);
    } catch (e) { EVENTS_OK = false; await sleep(5000); }
  }
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { clearInterval(refreshT); refreshT = null; }
  else { refreshLive(true); startRefresh(); }
});
startRefresh();
// ?live=0 opts out of the long-poll (headless checks, a tab you want quiet); the timer still runs
if (new URLSearchParams(location.search).get("live") !== "0") watchEvents();
// ---- command palette (⌘K): pages, projects, sessions, deployments, actions ----
let PAL = { idx: 0, items: [] };
function paletteItems(qs) {
  const items = [];
  const chordFor = v => { const k = Object.keys(CHORDS).find(k => CHORDS[k] === v); return k ? "g " + k : ""; };
  VIEWS.forEach(v => items.push({ g: "pages", t: v === "home" ? "overview" : v, k: chordFor(v), run: () => showTab(v) }));
  (PROJECTS || []).forEach(p => items.push({ g: "projects", t: p.title || p.name, s: p.session_count + " session" + (p.session_count===1?"":"s"),
    run: () => { showTab("sessions"); setTimeout(() => selectProject(p.digest), 60); } }));
  (SESSIONS || []).forEach(x => items.push({ g: "sessions", t: x.display_title, s: ago(x.modified_at),
    run: () => { showTab("sessions"); setTimeout(() => selectSession(x.session_id), 60); } }));
  (DEPLOY.deployments || []).filter(d => d.is_live).forEach(d => items.push({ g: "deployments", t: "connect " + (d.name || d.id),
    s: d.model, run: () => useDeployment(d) }));
  (FAMS || []).forEach(f => { const target = (f.providers || []).find(x => x.enabled) || (f.providers || [])[0];
    if (target) items.push({ g: "actions", t: "test " + f.label + " provider", s: target.id, run: () => testProviderQuick(target) }); });
  items.push({ g: "actions", t: "toggle theme", s: getTheme() || "system", run: cycleTheme });
  items.push({ g: "actions", t: "refresh now", s: EVENTS_OK ? "live" : "timer", run: () => refreshLive(true) });
  const ql = qs.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const hay = it => (it.t + " " + (it.s || "") + " " + it.g).toLowerCase();
  return (ql.length ? items.filter(it => ql.every(w => hay(it).includes(w))) : items).slice(0, 60);
}
function renderPalette() {
  const list = document.getElementById("pallist"); list.innerHTML = "";
  const items = PAL.items;
  if (!items.length) { list.append(el("div","pal-none","Nothing matches.")); return; }
  let g = null;
  items.forEach((it, i) => {
    if (it.g !== g) { g = it.g; list.append(el("div","pal-g", g)); }
    const row = el("div","pal-i" + (i === PAL.idx ? " on" : ""));
    if (it.k) row.append(el("span","pk", it.k));
    row.append(el("span","pt", it.t));
    if (it.s) row.append(el("span","ps", it.s));
    row.onmouseenter = () => { PAL.idx = i; list.querySelectorAll(".pal-i").forEach((x, j) => x.classList.toggle("on", j === i)); };
    row.onclick = () => runPalette(i);
    list.append(row);
  });
  const on = list.querySelector(".pal-i.on"); if (on) on.scrollIntoView({ block: "nearest" });
}
function openPalette() {
  const pal = document.getElementById("palette"); pal.className = "on";
  const inp = document.getElementById("palin"); inp.value = "";
  PAL.idx = 0; PAL.items = paletteItems(""); renderPalette();
  setTimeout(() => inp.focus(), 20);
}
function hidePalette() { document.getElementById("palette").className = ""; }
function runPalette(i) { const it = PAL.items[i]; hidePalette(); if (it) it.run(); }
document.getElementById("cmdk").onclick = openPalette;
document.getElementById("palette").addEventListener("click", e => { if (e.target.id === "palette") hidePalette(); });
document.getElementById("palin").addEventListener("input", e => { PAL.idx = 0; PAL.items = paletteItems(e.target.value); renderPalette(); });
document.getElementById("palin").addEventListener("keydown", e => {
  if (e.key === "Escape") { hidePalette(); e.preventDefault(); }
  else if (e.key === "ArrowDown") { PAL.idx = Math.min(PAL.items.length - 1, PAL.idx + 1); renderPalette(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { PAL.idx = Math.max(0, PAL.idx - 1); renderPalette(); e.preventDefault(); }
  else if (e.key === "Enter") { runPalette(PAL.idx); e.preventDefault(); }
});
window.addEventListener("hashchange", () => {
  const t = location.hash.slice(1);
  // Only react to a REAL change (back/forward, manual edit) — showTab already
  // handled the tab it set the hash to, so don't reload it a second time.
  if (VIEWS.includes(t) && t !== curView) showTab(t);
});

// ---- sessions ----
// Projects and sessions are cards: a friendly title (never a bare id — the
// id is a caption), then pills for the numbers. Both lists diff-render, so a
// refresh while you're reading leaves the scroll and the selection alone.
let curProject = null, curSession = null, PROJECTS = [], SESSIONS = [];
const UUIDISH = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$|^[0-9a-f]{12,}$/i;
const pill = (v, label, cls) => { const p = el("span","pill" + (cls ? " " + cls : "")); p.append(el("b", null, String(v))); if (label) p.append(document.createTextNode(label)); return p; };
async function loadProjects() {
  const { projects } = await api("/api/projects");
  PROJECTS = projects;
  const c = document.getElementById("projcards");
  document.querySelector("#projects .col-head").textContent = "Projects · " + projects.length;
  if (!projects.length) {
    c.innerHTML = ""; c.append(zero("No sessions yet", "Run mantis in a project and it appears here."));
    document.querySelector("#sessionlist .col-head").textContent = "Sessions";
    return;
  }
  if (!document.querySelector("#sesscards .pcard")) { const sc = document.getElementById("sesscards"); if (!sc.querySelector(".zero")) { sc.innerHTML = ""; sc.append(zero("Pick a project", "Its sessions list here, newest first.")); } }
  patchList(c, projects, p => p.digest, p => [p.title, p.session_count, p.last_activity, p.usd_est, p.tokens_est, p.path],
    (card, pr) => {
      card = card || el("div"); card.innerHTML = ""; card.className = "pcard" + (curProject && curProject.digest === pr.digest ? " on" : "");
      const title = pr.title && !UUIDISH.test(pr.title) ? pr.title : (pr.first_prompt || "project · " + pr.digest.slice(0, 8));
      const t = el("div","t", title); t.title = title; card.append(t);
      const s = el("div","s", pr.path || pr.digest); s.title = pr.path || pr.digest; card.append(s);
      const m = el("div","m");
      m.append(pill(pr.session_count, " session" + (pr.session_count===1?"":"s")));
      m.append(pill(ago(pr.last_activity), ""));
      if (pr.usd_est != null) m.append(pill("≈" + fmtUsd(pr.usd_est), "", "acc"));
      else if (pr.tokens_est) m.append(pill("≈" + fmtTok(pr.tokens_est), " tok"));
      card.append(m);
      card.onclick = () => selectProject(pr.digest);
      return card;
    });
}
function selectProject(digest) {
  const pr = PROJECTS.find(p => p.digest === digest); if (!pr) return;
  curProject = pr; curSession = null;
  document.querySelectorAll("#projcards .pcard").forEach(x => x.classList.toggle("on", x.dataset.key === digest));
  document.querySelectorAll(".col").forEach(x => x.classList.remove("mobile-on")); document.getElementById("sessionlist").classList.add("mobile-on");
  loadSessions(pr);
}
let sessionsReq = 0;
async function loadSessions(pr) {
  const c = document.getElementById("sesscards");
  const my = ++sessionsReq;
  if (!pr.cwd) { c.innerHTML = ""; c.append(zero("Path unknown", "This project's transcripts don't record a working directory.")); return; }
  const { sessions } = await api("/api/sessions?" + q({ cwd: pr.cwd }));
  if (my !== sessionsReq) return;   // a newer project selection superseded this
  SESSIONS = sessions;
  document.querySelector("#sessionlist .col-head").textContent = "Sessions · " + sessions.length;
  if (!sessions.length) { c.innerHTML = ""; c.append(zero("No sessions", "Nothing recorded in this project yet.")); return; }
  patchList(c, sessions, s => s.session_id, s => [s.display_title, s.last_prompt, s.message_count, s.modified_at, s.usd_est, s.tokens_est, s.model],
    (card, s) => {
      card = card || el("div"); card.innerHTML = "";
      card.className = "pcard" + (curSession === s.session_id ? " on" : "");
      card.dataset.q = ((s.display_title || "") + " " + (s.first_prompt || "") + " " + (s.last_prompt || "") + " " + s.session_id).toLowerCase();
      const title = s.display_title && !UUIDISH.test(s.display_title) ? s.display_title : "session · " + s.session_id.slice(0, 8);
      const t = el("div","t", title); t.title = title; card.append(t);
      if (s.last_prompt && s.last_prompt !== s.first_prompt) { const lp = el("div","s sans", s.last_prompt); lp.title = s.last_prompt; card.append(lp); }
      const m = el("div","m");
      m.append(pill(s.message_count || 0, " msg"));
      m.append(pill(ago(s.modified_at), ""));
      if (s.usd_est != null) m.append(pill("≈" + fmtUsd(s.usd_est), "", "acc"));
      else if (s.tokens_est) m.append(pill("≈" + fmtTok(s.tokens_est), " tok"));
      if (s.model) { const mp = pill(s.model, "", "mono"); mp.title = "priced as the current model — transcripts record no model"; m.append(mp); }
      card.append(m);
      card.append(el("div","s", s.session_id));
      card.onclick = () => selectSession(s.session_id);
      return card;
    });
  applySessionFilter();
}
function selectSession(sid) {
  const s = SESSIONS.find(x => x.session_id === sid); if (!s || !curProject) return;
  curSession = sid;
  document.querySelectorAll("#sesscards .pcard").forEach(x => x.classList.toggle("on", x.dataset.key === sid));
  document.querySelectorAll(".col").forEach(x => x.classList.remove("mobile-on")); document.getElementById("convcol").classList.add("mobile-on");
  loadConv(curProject.cwd, s);
}
function applySessionFilter() {
  const q = (document.getElementById("sessfind").value || "").trim().toLowerCase();
  document.querySelectorAll("#sesscards .pcard").forEach(r => {
    r.style.display = !q || q.split(/\s+/).every(t => (r.dataset.q || "").includes(t)) ? "" : "none";
  });
}
document.getElementById("sessfind").addEventListener("input", applySessionFilter);
async function jumpToSession(cwd, sid) {
  showTab("sessions");
  if (!cwd) return;
  await loadProjects();
  const tail = String(cwd).replace(/^~/, "");
  const hit = PROJECTS.find(p => p.path === cwd || (p.path || "").endsWith(tail));
  if (!hit) { toast("that job's project isn't in the sessions list", true); return; }
  selectProject(hit.digest);
  if (sid) setTimeout(() => {
    if (SESSIONS.find(x => x.session_id === sid)) selectSession(sid); else toast("session " + sid.slice(0, 8) + " has no transcript here");
  }, 450);
}
// ---- the conversation as a timeline ----
// Header readings (turns, est. tokens, est. cost, peak context), a bar per
// assistant turn showing how full the window was — the compaction cliff is
// visible as a drop — with cumulative cost drawn over it, then the messages.
// Tool results start collapsed: the call is the story, the payload is
// evidence you open when you need it.
function ctxChart(turns, st) {
  const box = el("div","ctxbox");
  const h = el("div","ch");
  h.append(el("span", null, "context fill per turn"));
  h.append(el("span", null, (st.ctx_window ? "window " + fmtCtx(st.ctx_window) + " · " : "") +
    (st.pricing && st.pricing.known ? "line = cumulative est. cost" : "unpriced model")));
  box.append(h);
  const W = 1000, H = 120, PL = 6, PR = 6, PT = 10, PB = 16, n = turns.length;
  const iw = W-PL-PR, ih = H-PT-PB, bw = iw/Math.max(1, n);
  const maxCtx = Math.max(1, st.ctx_window || 0, ...turns.map(t => t.ctx_est));
  const maxUsd = Math.max(1e-9, ...turns.map(t => t.cum_usd_est || 0));
  let bars = "", line = "";
  turns.forEach((t, i) => {
    const x = PL + i*bw + bw*0.12, w = Math.max(1, bw*0.76), hh = t.ctx_est/maxCtx*ih;
    bars += `<rect class="bar" data-i="${t.i}" x="${x.toFixed(1)}" y="${(PT+ih-hh).toFixed(1)}" width="${w.toFixed(1)}" height="${Math.max(1, hh).toFixed(1)}" rx="1">` +
      `<title>turn ${i+1} · ${fmtTok(t.ctx_est)} tok in context${t.usd_est != null ? " · " + fmtUsd(t.usd_est) + " this turn" : ""}` +
      `${(t.tools||[]).length ? " · " + esc(t.tools.join(", ")) : ""}</title></rect>`;
    if (t.cum_usd_est != null) line += (i ? " L" : "M") + (x + w/2).toFixed(1) + "," + (PT + ih - (t.cum_usd_est/maxUsd)*ih).toFixed(1);
  });
  const capY = st.ctx_window ? (PT + ih - st.ctx_window/maxCtx*ih).toFixed(1) : null;
  const cap = capY != null ? `<line class="cap" x1="${PL}" x2="${PL+iw}" y1="${capY}" y2="${capY}"/>` : "";
  const svg = el("div");
  svg.innerHTML = `<svg class="ctxsvg" viewBox="0 0 ${W} ${H}" role="img" aria-label="context per turn">${bars}${cap}` +
    `${line ? `<path class="cost" d="${line}"/>` : ""}<text x="${PL}" y="${H-4}">turn 1</text>` +
    `<text x="${PL+iw}" y="${H-4}" text-anchor="end">turn ${n}</text></svg>`;
  svg.querySelectorAll(".bar").forEach(r => r.onclick = () => {
    const m = document.querySelector('.msg[data-i="' + r.dataset.i + '"]');
    if (m) { m.scrollIntoView({ behavior: "smooth", block: "center" }); m.classList.add("flash"); setTimeout(() => m.classList.remove("flash"), 1200); }
  });
  box.append(svg);
  return box;
}
let convReq = 0;
async function loadConv(cwd, s) {
  const box = document.getElementById("transcript");
  const my = ++convReq;
  let data;
  try { data = await api("/api/session?" + q({ cwd, id: s.session_id })); }
  catch (e) { if (my !== convReq) return; box.innerHTML = ""; box.append(el("div","empty","Failed to load: " + e.message)); return; }
  if (my !== convReq) return;   // a newer session click superseded this
  box.innerHTML = "";
  const head = el("div","conv-head");
  head.append(el("h2", null, s.display_title || s.title || s.first_prompt || "session · " + s.session_id.slice(0, 8)));
  head.append(el("div","sub", (s.message_count||0) + " messages · " + ago(s.modified_at) + " · " + s.session_id));
  box.append(head);
  if (!data.messages || !data.messages.length) { box.append(el("div","empty","(empty)")); return; }
  const st = data.stats || {};
  const turns = data.turns || [];
  const lcd = el("div","lcd tight conv-stats");
  lcdCell(lcd, st.turns || 0, "turns", "");
  lcdCell(lcd, fmtTok(st.tokens_est), "est. tokens", "");
  lcdCell(lcd, fmtTok(st.in_est) + "↑ " + fmtTok(st.out_est) + "↓", "in / out", "dim");
  if (st.usd_est != null) lcdCell(lcd, "≈" + fmtUsd(st.usd_est), "est. cost", "hot");
  else lcdCell(lcd, "—", "unpriced", "dim");
  if (st.ctx_window && st.peak_ctx_est) {
    const pct = st.peak_ctx_est / st.ctx_window;
    lcdCell(lcd, Math.round(pct * 100) + "%", "peak of " + fmtCtx(st.ctx_window), pct > 0.8 ? "hot" : "dim");
  }
  box.append(lcd);
  if (turns.length > 1) box.append(ctxChart(turns, st));
  // tool results live in the user message AFTER the call; pair them up so each
  // call renders as one collapsed row with its result inside
  const results = {};
  data.messages.forEach(m => { if (Array.isArray(m.content)) m.content.forEach(b => { if (b && b.type === "tool_result" && b.tool_use_id) results[b.tool_use_id] = b; }); });
  data.messages.forEach((m, i) => { const n = renderMsg(m, i, turns, results); if (n) box.append(n); });
  if (st.note) { const n = el("div","note2", st.note); n.style.marginTop = "8px"; box.append(n); }
}
function renderMsg(m, i, turns, results) {
  results = results || {};
  const role = m.role || "assistant";
  const wrap = el("div", "msg " + role); wrap.dataset.i = i;
  const who = el("div","who");
  if (role === "system" && m.compact) {
    who.append(el("span","rc","compaction"));
    wrap.append(who);
    const body = el("div","mbody");
    body.append(el("div","compact", "context compacted · " + (m.compacted_count || 0) + " earlier messages folded into a summary"));
    if (m.content) body.append(el("div","thinking", m.content));
    wrap.append(body); return wrap;
  }
  who.append(el("span","rc", role === "user" ? "user" : role === "assistant" ? "assistant" : role));
  if (m.ts) who.append(el("span","ts", new Date(m.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
  const t = m.turn != null && turns ? turns[m.turn] : null;
  if (t) who.append(el("span","tc", fmtTok(t.ctx_est) + " ctx" + (t.usd_est != null ? " · " + fmtUsd(t.usd_est) : "")));
  wrap.append(who);
  const body = el("div","mbody"); wrap.append(body);
  const metas = [];
  let visible = 0;
  const addText = (txt) => {
    const sp = splitMeta(txt); sp.meta.forEach(x => metas.push(x));
    if (!sp.text) return;
    visible++;
    if (role === "assistant") { const d = el("div","md"); d.innerHTML = md(sp.text); body.append(d); }
    else body.append(el("div","text", sp.text));
  };
  if (m.isMeta) { const sp = splitMeta(typeof m.content === "string" ? m.content : JSON.stringify(m.content)); metas.push(...(sp.meta.length ? sp.meta : [sp.text || "[context]"])); }
  else if (typeof m.content === "string") addText(m.content);
  else (m.content || []).forEach(b => {
    if (!b || typeof b !== "object") return;
    if (b.type === "text") { addText(b.text || ""); return; }
    if (b.type === "tool_result") { if (b.tool_use_id && results[b.tool_use_id] === b && results[b.tool_use_id].__paired) return;
      if (b.tool_use_id && results[b.tool_use_id]) return;   // rendered inside its call
      const n = renderBlock(b); if (n) { body.append(n); visible++; } return; }
    if (b.type === "tool_use") { const r = b.id ? results[b.id] : null; if (r) r.__paired = true; body.append(toolCall(b, r)); visible++; return; }
    const n = renderBlock(b); if (n) { body.append(n); visible++; }
  });
  if (metas.length) body.append(ctxToggle(metas));
  if (!visible && !metas.length) return null;      // a user turn that only carried tool results
  return wrap;
}
// One tool call as a collapsed row: ⚒ name · the argument that names the
// target (path, command, pattern), the result's size, and the caret. Open it
// for the exact args and the result. Errors open by default.
function toolCall(b, r) {
  const inp = b.input || {};
  const first = ["path","file_path","command","cmd","pattern","query","url","name","description","prompt"].map(k => inp[k]).find(v => typeof v === "string" && v.trim())
    || Object.values(inp).find(v => typeof v === "string" && v.trim()) || "";
  let res = r ? r.content : null;
  if (Array.isArray(res)) res = res.map(x => x && x.type === "text" ? x.text : JSON.stringify(x)).join("\n");
  const resText = res == null ? null : (typeof res === "string" ? res : JSON.stringify(res, null, 2));
  const isErr = !!(r && r.is_error);
  const box = el("div","tcall" + (isErr ? " err open" : ""));
  const th = el("div","th");
  th.append(el("span","tg","▶"), el("span",null,"⚒"), el("span","tn", b.name || "tool"));
  if (first) { const a = el("span","ta", "· " + String(first).slice(0, 120)); a.title = String(first); th.append(a); }
  const sz = el("span","sz", resText == null ? "no result" : (isErr ? "error · " : "") + fmtBytes(resText.length) + " · " + resText.split("\n").length + " lines");
  th.append(sz);
  th.onclick = () => box.classList.toggle("open");
  box.append(th);
  const tb = el("div","tb2");
  tb.append(el("div","tl2","args · " + (b.id || "").slice(0, 8)));
  tb.append(el("pre", null, JSON.stringify(inp, null, 2)));
  if (resText != null) { tb.append(el("div","tl2", isErr ? "error" : "result")); tb.append(el("pre","res", resText.length > 20000 ? resText.slice(0, 20000) + "\n… (" + fmtBytes(resText.length) + " total)" : resText)); }
  box.append(tb);
  return box;
}
function renderBlock(b) {
  const t = b.type;
  if (t === "text") return el("div","text", b.text || "");
  if (t === "thinking") return el("div","thinking", b.thinking || "");
  if (t === "tool_use") return toolCall(b, null);
  if (t === "tool_result") {
    let c = b.content;
    if (Array.isArray(c)) c = c.map(x => x.type === "text" ? x.text : JSON.stringify(x)).join("\n");
    const text = typeof c === "string" ? c : JSON.stringify(c, null, 2);
    const blk = el("div","tcall" + (b.is_error ? " err open" : ""));
    const th = el("div","th"); th.append(el("span","tg","▶"), el("span","tn", b.is_error ? "error" : "result"), el("span","ta", (b.tool_use_id||"").slice(0,8)));
    th.append(el("span","sz", fmtBytes((text || "").length))); th.onclick = () => blk.classList.toggle("open"); blk.append(th);
    const tb = el("div","tb2"); tb.append(el("pre","res", text)); blk.append(tb);
    return blk;
  }
  if (t === "image") {
    const blk = el("div","block"); blk.append(el("div","bh","🖼 image"));
    try {
      const src = b.source || {};
      if (src.data && src.media_type) {
        const img = document.createElement("img");
        img.src = "data:" + src.media_type + ";base64," + src.data;
        img.style = "max-width:100%;display:block"; blk.append(img);
      }
    } catch (e) {}
    return blk;
  }
  return null;
}

// ---- provider marks ----------------------------------------------------
// The vendors' real logos, inlined at build time (see serve_logos.py). They
// ship with the wheel rather than loading from a CDN: a local dashboard
// shouldn't tell twelve companies which of them you're looking at, and the
// page has to work with the wifi off. Monochrome marks carry a tint; the
// colour ones are used as their owners draw them.
const MARKS = __LOGOS__;
function providerMark(pid, label) {
  const m = MARKS[pid];
  const w = el("span","mark2");
  if (m && m.svg) {
    w.innerHTML = m.svg;
    if (m.tint) w.style.color = m.tint;
  } else {
    w.textContent = (label || pid || "?").slice(0, 1).toUpperCase();
  }
  return w;
}

// ---- models & hosting ----
// Context windows read as "200k", not "200000" — the unit people actually say.
const fmtCtx = (n) => n >= 1000000 ? (n/1000000).toFixed(n % 1000000 ? 1 : 0) + "m"
                    : n >= 1000 ? Math.round(n/1000) + "k" : String(n);
let modelsReq = 0;
async function loadModels() {
  const pad = document.getElementById("modelspad");
  const my = ++modelsReq;
  if (!pad.childElementCount) skeleton(pad);
  const m = await api("/api/models");
  if (my !== modelsReq) return;   // a newer loadModels() superseded this one
  pad.innerHTML = "";
  const cur = (m.current && m.current.model) || "—";
  const h = m.hosting || {};

  pageHead(pad, "models", null,
    "Any model, any provider, any self-host. Enable a provider with its key, or point mantis " +
    "at a server you run. Picking a model here sets it as current for the next session.");
  const nModels = m.providers.reduce((a, p) => a + ((p.models || []).length), 0);
  signalPath(pad, [
    { label: "mantis", state: "dim" },
    { label: h.label || (h.kind === "selfhost" ? "your server" : "no provider"),
      state: h.label || h.kind === "selfhost" ? "" : "warn" },
    { label: cur, state: cur === "—" ? "warn" : "" },
    { value: nModels, label: "models available", state: "dim",
      title: m.enabled_count + " of " + m.providers.length + " providers enabled" },
  ]);

  // The route, provable. Same promise the MCP page makes: don't just show the
  // wiring, let the user check it.
  const routeWrap = el("div"); pad.append(routeWrap);
  const routeBtn = btn("Test this route", "", async () => {
    routeBtn.disabled = true; routeBtn.textContent = "Reaching…";
    routeWrap.innerHTML = "";
    try {
      const body = h.kind === "selfhost" ? { backend: h.backend }
                                         : { provider: (m.providers.find(p => p.is_current) || {}).id };
      if (!body.provider && !body.backend) { toast("nothing to test yet — enable a provider first", true); return; }
      const r = await post("/api/model/test", body);
      const p = el("div","probe " + (r.ok ? "ok" : "bad"));
      const hd = el("div","ph2");
      hd.append(el("span","dot2 " + (r.ok ? "ok" : "bad")));
      hd.append(document.createTextNode(r.ok
        ? "Reached " + (r.label || "endpoint") + (r.count != null ? " · " + r.count + " models live" : "") + " · " + r.ms + "ms"
        : "Couldn't reach " + (r.label || "endpoint") + (r.ms != null ? " · " + r.ms + "ms" : "")));
      p.append(hd);
      if (!r.ok) p.append(el("div","pe", r.error || "unknown error"));
      routeWrap.append(p);
    } catch (e) { toast(e.message, true); }
    finally { routeBtn.disabled = false; routeBtn.textContent = "Test this route"; }
  });
  const routeBar = el("div"); routeBar.style = "display:flex;gap:8px;align-items:center;margin:-12px 0 24px";
  routeBar.append(routeBtn);
  if (m.recent && m.recent.length > 1) {
    const r = el("div","recent"); r.style.margin = "0";
    r.append(el("span","hero-lbl", "recent"));
    m.recent.slice(0, 4).forEach(x => {
      const c = el("span","chip clk" + (x===cur?" cur":""), x);
      c.onclick = () => useModel(x, "");
      r.append(c);
    });
    routeBar.append(r);
  }
  pad.insertBefore(routeBar, routeWrap);

  // ---- the model table ------------------------------------------------
  // Not a list of strings: what each model can do, side by side, from the
  // SDK's own capability table. Filter chips answer the three questions people
  // actually arrive with — what can I use now, what's free, what's locked.
  // Grouped by the five families, with what each model costs per 1M tokens
  // beside its window — from budget.py's table, and a dash (never a guess)
  // where the table has no row. Local Ollama models join the open-source
  // family with their size on disk and whether they're loaded right now.
  const info = m.model_info || {};
  const famOrder = (m.families || []).map(f => f.id);
  const famLabel = {}, famLogo = {};
  (m.families || []).forEach(f => { famLabel[f.id] = f.label; famLogo[f.id] = f.logo; });
  const famIdx = f => { const i = famOrder.indexOf(f); return i < 0 ? 99 : i; };
  const oll = m.ollama || {};
  const allModels = [];
  m.providers.forEach(p => (p.models || []).forEach(x =>
    allModels.push({ model: x, label: p.label || p.id, pid: p.id, fam: p.family || "oss",
                     backend: p.base_url, enabled: p.enabled, info: info[x] || {},
                     price: (p.prices || {})[x] || null })));
  (oll.models || []).forEach(om => allModels.push({
    model: om.name, label: "local · " + fmtBytes(om.size) + (om.loaded ? " · loaded" : ""), pid: "ollama",
    fam: "oss", backend: oll.base_url, enabled: true, info: info[om.name] || {}, local: om,
    price: (info[om.name] || {}).price || { in: 0, out: 0, free: true } }));
  const price2 = v => v >= 10 ? v.toFixed(0) : v.toFixed(2);
  const priceCell = (p) => {
    if (!p) { const s = el("span","mprice na", "—"); s.title = "no row in the price table"; return s; }
    if (p.free) { const s = el("span","mprice free", "free"); s.title = "your hardware, no API charge"; return s; }
    const s = el("span","mprice", "$" + price2(p.in) + " · $" + price2(p.out));
    s.title = "$" + p.in + " in · $" + p.out + " out, per 1M tokens" + (p.cache_read != null ? " · cache read $" + p.cache_read : "");
    return s;
  };
  if (allModels.length) {
    const nFam = new Set(allModels.map(a => a.fam)).size;
    const sec = section(pad, "choose a model", allModels.length + " across " + nFam + " famil" + (nFam===1?"y":"ies"));
    const bar = el("div","filters");
    const find = findBox("Filter — gpt, claude, grok, 200k, free, local…  ( / )");
    find.wrap.style.marginBottom = "0"; find.wrap.style.flex = "1";
    bar.append(find.wrap);
    const FILTERS = [["all","all"], ["ready","ready to use"], ["locked","needs a key"], ["free","free / local"]];
    let mode = "all";
    const chips = el("div","fchips");
    FILTERS.forEach(([k, lab]) => {
      const c = el("button","fchip" + (k === mode ? " on" : ""), lab);
      c.onclick = () => {
        mode = k;
        chips.querySelectorAll(".fchip").forEach(x => x.classList.toggle("on", x === c));
        apply();
      };
      chips.append(c);
    });
    bar.append(chips);
    sec.append(bar);

    const list = el("div","mtable");
    const head = el("div","mhead");
    ["model", "provider", "window", "$ / 1M in · out", "capabilities", ""].forEach(x => head.append(el("span", null, x)));
    list.append(head);
    famOrder.concat([...new Set(allModels.map(a => a.fam))].filter(f => !famOrder.includes(f))).forEach(fid => {
      const rows = allModels.filter(a => a.fam === fid);
      if (!rows.length) return;
      const fh = el("div","mfam"); fh.dataset.fam = fid;
      fh.append(providerMark(famLogo[fid] || fid, famLabel[fid] || fid));
      fh.append(document.createTextNode(famLabel[fid] || fid));
      fh.append(el("span","cnt3", rows.length + " model" + (rows.length===1?"":"s")));
      list.append(fh);
      rows.forEach(a => {
        const row = el("div","mrow" + (a.model===cur ? " cur" : "") + (a.enabled ? "" : " locked"));
        const pr = a.price;
        row.dataset.fam = fid;
        row.dataset.q = (a.model + " " + a.label + " " + (famLabel[fid] || fid) + " " + a.pid + " " +
          (a.info.ctx ? Math.round(a.info.ctx/1000) + "k" : "") + (pr && pr.free ? " free" : "") +
          (a.local ? " local ollama" + (a.local.loaded ? " loaded" : "") : "")).toLowerCase();
        row.dataset.state = a.enabled ? "ready" : "locked";
        row.dataset.free = (pr && pr.free) || a.local ? "1" : "";
        row.append(el("span","mn", a.model));
        row.append(el("span","mp", a.label));
        const ctx = el("span","mctx", a.info.ctx ? fmtCtx(a.info.ctx) : "");
        if (a.info.ctx_learned) { ctx.title = "ceiling learned from the endpoint: " + fmtCtx(a.info.ctx_learned); ctx.textContent += "*"; }
        row.append(ctx);
        row.append(priceCell(pr));
        const caps = el("span","mcaps");
        if (a.info.tools) { const c = el("span","cap","tools"); c.title = "native tool calling"; caps.append(c); }
        if (a.info.effort) { const c = el("span","cap","effort"); c.title = "reasoning-effort control"; caps.append(c); }
        if (a.info.thinking) { const c = el("span","cap","thinks"); c.title = "emits reasoning"; caps.append(c); }
        if (a.local && a.local.loaded) { const c = el("span","cap","loaded"); c.title = "in memory now"; caps.append(c); }
        row.append(caps);
        row.append(el("span","mgo", a.model===cur ? "current" : (a.enabled ? "use →" : "unlock")));
        row.onclick = () => a.enabled ? useModel(a.model, a.backend) : focusProvider(a.pid);
        list.append(row);
      });
    });
    sec.append(list);
    const apply = () => {
      const q = find.input.value.trim().toLowerCase();
      let shown = 0;
      const perFam = {};
      list.querySelectorAll(".mrow").forEach(r => {
        const okQ = !q || q.split(/\s+/).every(t => r.dataset.q.includes(t));
        const okF = mode === "all" || (mode === "free" ? !!r.dataset.free : r.dataset.state === mode);
        const on = okQ && okF;
        r.style.display = on ? "" : "none";
        r.classList.remove("kb");
        if (on) { shown++; perFam[r.dataset.fam] = (perFam[r.dataset.fam] || 0) + 1; }
      });
      list.querySelectorAll(".mfam").forEach(h => { h.style.display = perFam[h.dataset.fam] ? "" : "none"; });
      let e = list.querySelector(".find-none");
      if (!shown) { if (!e) { e = el("div","find-none empty",
        "Nothing matches. Self-host below to run something that isn't on this list."); list.append(e); } }
      else if (e) e.remove();
    };
    find.input.oninput = apply;
    // Keyboard: / focuses (global handler), ↑↓ walk the visible rows, Enter switches to one.
    let kbi = -1;
    const visible = () => [...list.querySelectorAll(".mrow")].filter(r => r.style.display !== "none");
    find.input.onkeydown = (e) => {
      const rows = visible();
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        kbi = Math.max(0, Math.min(rows.length - 1, kbi + (e.key === "ArrowDown" ? 1 : -1)));
        rows.forEach((r,i) => r.classList.toggle("kb", i === kbi));
        if (rows[kbi]) rows[kbi].scrollIntoView({ block: "nearest" });
        e.preventDefault();
      } else if (e.key === "Enter" && rows[kbi]) { rows[kbi].click(); }
      else if (e.key === "Escape") { find.input.value = ""; kbi = -1; apply(); }
    };
  }

  // ---- local models — what Ollama has on disk, and what's in memory ----
  const oSec = section(pad, "local models · ollama", (oll.base_url || "").replace(/^https?:\/\//, ""));
  if (!oll.reachable) {
    oSec.append(zero("Ollama isn't answering" + (oll.base_url ? " at " + oll.base_url.replace(/^https?:\/\//, "") : ""),
      "Start it with `ollama serve` (or point OLLAMA_HOST at the box that runs it) and this list fills " +
      "with every pulled model, its size on disk, and whether it's loaded in memory right now."));
  } else if (!(oll.models || []).length) {
    oSec.append(zero("Ollama is up, nothing pulled yet",
      "`ollama pull gpt-oss:20b` (or any tag) and it appears here, ready to use with no key."));
  } else {
    const box = el("div","list");
    oll.models.forEach(om => {
      const row = el("div","orow" + (om.name===cur ? " cur" : ""));
      row.append(el("span","on2", om.name));
      row.append(el("span","osz", fmtBytes(om.size)));
      row.append(el("span","opq", [om.param, om.quant].filter(Boolean).join(" ")));
      const st = el("span","ost");
      st.append(el("span","t2 " + (om.loaded ? "acc" : ""), om.loaded ? "loaded" + (om.vram ? " · " + fmtBytes(om.vram) : "") : "on disk"));
      row.append(st);
      row.append(el("span","ogo", om.name===cur ? "current" : "use →"));
      row.title = (om.family ? om.family + " · " : "") + (om.modified_at ? "pulled " + String(om.modified_at).slice(0, 10) : "");
      row.onclick = () => useModel(om.name, oll.base_url);
      box.append(row);
    });
    oSec.append(box);
  }

  // ---- connect a provider ----------------------------------------------
  // This is setup, so it reads as setup: a progress bar over the twelve, the
  // connected ones first, and every unconnected row offering the one action
  // that changes its state. Expanding a row IS the setup form.
  const provSec = section(pad, "connect a provider");
  const prog = el("div","setup");
  const ph2 = el("div","setup-h");
  ph2.append(el("span","setup-n", m.enabled_count + " of " + m.providers.length + " connected"));
  const need = el("span","setup-s", m.enabled_count
    ? "Add another to switch between them mid-session."
    : "Paste one key and mantis is ready to run.");
  ph2.append(need);
  prog.append(ph2);
  const track = el("div","setup-bar");
  const fillp = el("i");
  fillp.style.width = Math.round(m.enabled_count / Math.max(1, m.providers.length) * 100) + "%";
  track.append(fillp); prog.append(track);
  prog.append(el("div","setup-note",
    "Keys are written to ~/.mantis-agent (chmod 600) on this machine and are only ever shown " +
    "masked. Nothing is sent anywhere except the provider you're calling."));
  provSec.append(prog);

  const plist = el("div","list");
  // Family order first (OpenAI · Claude · Gemini · Grok · open source), then
  // connected before not-yet — so the five kinds read as five groups.
  const fIdx = p => { const i = (m.families || []).findIndex(f => f.id === (p.family || "oss")); return i < 0 ? 99 : i; };
  const ordered = [...m.providers].sort((a, b) => (fIdx(a) - fIdx(b)) || ((b.enabled ? 1 : 0) - (a.enabled ? 1 : 0)));
  const famName = {}; (m.families || []).forEach(f => famName[f.id] = f.label);
  ordered.forEach(p => {
    const tags = [];
    if (famName[p.family]) tags.push({ text: famName[p.family], cls: "" });
    if (p.is_current) tags.push({ text: "in use", cls: "acc" });
    if (p.auth === "oauth") tags.push({ text: "oauth", cls: "vio" });
    const acts = [];
    if (p.enabled) {
      const st = el("span","ready");
      st.append(el("span","dot2 ok"));
      st.append(document.createTextNode("connected" + (p.key_source === "env" ? " · from env" : p.auth === "oauth" ? " · token" : "")));
      acts.push(st);
    } else {
      acts.push(btn("Add key", "pri", null));   // click bubbles to the row → opens setup
    }
    const row = listRow({
      name: p.label || p.id,
      sub: (p.base_url || "").replace(/^https?:\/\//, ""),
      tags, actions: acts, mark: providerMark(p.id, p.label),
      build: (body) => providerDetail(p, body, cur, m),
    });
    row.id = "prov-" + p.id;
    row.dataset.q = ((p.label || "") + " " + p.id + " " + (p.base_url || "")).toLowerCase();
    // The "Add key" button and the row open the same drawer, then focus the field.
    if (!p.enabled) {
      row.querySelector(".acts").onclick = (e) => {
        e.stopPropagation(); row.openDrawer();
        const i = row.querySelector(".lbody input"); if (i) i.focus();
      };
    }
    plist.append(row);
  });
  provSec.append(plist);

  // self-host / custom endpoint — a first-class card in the same visual system
  const shSec = section(pad, "or bring your own server");
  const sh = el("div","card selfhost-card");
  const shNote = el("div","note");
  shNote.append(document.createTextNode("Point mantis at any OpenAI-compatible URL you run — vLLM, llama.cpp, a Modal/RunPod box. Sets it as your current model.  "));
  const shGuide = el("button","guide-link","How to self-host ↗");
  shGuide.onclick = () => openSelfhostGuide(m.selfhost_guide, m.selfhost_docs_url);
  shNote.append(shGuide);
  if (m.selfhost_guide && m.selfhost_guide.skill) {
    shNote.append(document.createTextNode("   ·   "));
    shNote.append(extLink("a-link", "Agent skill ↗", m.selfhost_guide.skill.url));
  }
  sh.append(shNote);
  const inUrl = input("https://my-gpu-box:8000/v1");
  if (h.kind === "selfhost") inUrl.value = h.backend || "";
  const inModel = input("model id  ·  e.g. zai-org/GLM-4-9B-0414");
  if (h.kind === "selfhost") inModel.value = h.model || "";
  const inKey = input("API key (optional — most local servers need none)", true);
  const connectBtn = el("button","btn","Connect");
  connectBtn.onclick = async () => {
    connectBtn.disabled = true;
    try {
      const r = await post("/api/connect", { backend: inUrl.value, model: inModel.value, key: inKey.value });
      if (r.ok) { toast("connected · " + r.model + (r.warning ? " (" + r.warning + ")" : "")); loadOverview(); loadModels(); }
      else toast(r.error || "failed", true);
    } catch (e) { toast(e.message, true); } finally { connectBtn.disabled = false; }
  };
  const f = el("div","fields");
  const r1 = el("div","r"); r1.append(inUrl, inModel);
  const r2 = el("div","r"); r2.append(inKey, connectBtn);
  f.append(r1, r2); sh.append(f); shSec.append(sh);

  // deep-link: /?guide=<provider|selfhost> opens that guide directly
  const gp = new URLSearchParams(location.search).get("guide");
  if (gp === "selfhost") openSelfhostGuide(m.selfhost_guide, m.selfhost_docs_url);
  else if (gp) { const pp = m.providers.find(x => x.id === gp); if (pp) openGuide(pp); }
}

// One provider, expanded: where it points, what key it's using, whether that
// key actually works, and its models one click from being current.
function providerDetail(p, body, cur, m) {
  const dl = el("dl","kvs");
  kvRow(dl, "endpoint", p.base_url || "—");
  kvRow(dl, "key env", p.api_key_env || "—");
  if (p.note) kvRow(dl, "note", p.note, false);
  body.append(dl);

  // A provider that already has a key shows the key — masked, with where it
  // came from — and replacing it is an opt-in. Only an unconnected provider
  // gets a paste field up front, because that's the only one that needs one.
  const keyRow = el("div"); keyRow.style = "display:flex;gap:8px;margin-top:14px";
  const inp = input("paste your " + (p.api_key_env || "API key"), true);
  const save = el("button","b pri", p.key_masked ? "Save new key" : "Enable provider");
  const doSave = saveKeyFn(p, inp, save);
  save.onclick = doSave;
  inp.onkeydown = e => { if (e.key === "Enter") doSave(); };
  keyRow.append(inp, save);

  if (p.key_masked) {
    const held = el("div","keyheld");
    held.append(el("span","kh-l", p.api_key_env || "key"));
    held.append(el("span","kh-v", p.key_masked));
    held.append(el("span","t2 " + (p.key_source === "env" ? "blu" : "acc"),
      p.key_source === "env" ? "from your environment" : "saved on this machine"));
    const sp = el("span"); sp.style.flex = "1"; held.append(sp);
    keyRow.style.display = "none";
    const rep = btn("Replace", "gho", () => {
      const open = keyRow.style.display === "none";
      keyRow.style.display = open ? "flex" : "none";
      rep.textContent = open ? "Cancel" : "Replace";
      rep.classList.toggle("on", open);
      if (open) inp.focus();
    });
    held.append(rep);
    if (p.key_source === "saved") {
      const rm = btn("Forget", "gho dan", null);
      rm.onclick = () => armDelete(rm, removeKeyFn(p, rm));
      held.append(rm);
    } else {
      held.append(el("span","kh-n", "unset the env var to change it"));
    }
    body.append(held);
  }
  body.append(keyRow);

  const acts = el("div"); acts.style = "display:flex;gap:8px;margin-top:12px;flex-wrap:wrap";
  const out = el("div");
  const test = btn("Check reachability", "", async () => {
    test.disabled = true; test.textContent = "Reaching…"; out.innerHTML = "";
    try {
      const r = await post("/api/model/test", { provider: p.id });
      const box = el("div","probe " + (r.ok ? "ok" : "bad"));
      const hd = el("div","ph2");
      hd.append(el("span","dot2 " + (r.ok ? "ok" : "bad")));
      hd.append(document.createTextNode(r.ok
        ? "Reachable · " + (r.count != null ? r.count + " models live · " : "") + r.ms + "ms"
        : "Not reachable · " + r.ms + "ms"));
      box.append(hd);
      if (!r.ok) box.append(el("div","pe", r.error || "unknown error"));
      out.append(box);
    } catch (e) { toast(e.message, true); }
    finally { test.disabled = false; test.textContent = "Check reachability"; }
  });
  acts.append(test);
  if (p.docs_url) acts.append(extLink("b gho", "Provider docs ↗", p.docs_url));
  body.append(acts, out);

  const models = p.models || [];
  if (models.length) {
    const lab = el("div","kvs"); lab.style.marginTop = "14px";
    kvRow(lab, "models", String(models.length) + (p.live_count ? " listed · " + p.live_count + " live" : ""));
    body.append(lab);
    const chips = el("div","chips"); chips.style.marginTop = "8px";
    models.forEach(x => {
      const c = el("span","chip" + (p.enabled ? " clk" : "") + (x===cur ? " cur" : ""), x);
      if (p.enabled) c.onclick = () => useModel(x, p.base_url);
      else { c.title = "enable " + (p.label||p.id) + " first"; c.onclick = () => inp.focus(); }
      chips.append(c);
    });
    body.append(chips);
  }
}

// ---- shared page furniture ----
// Every non-session view is built from these four helpers, so Models, Skills,
// MCP and Config share one header rhythm, one row shape and one empty state.
function pageHead(pad, title, count, desc, actions) {
  const h = el("div","page-h");
  const t = el("h1","page-t"); t.append(document.createTextNode(title));
  if (count != null) t.append(el("span","count", String(count)));
  h.append(t);
  if (actions && actions.length) { const a = el("div","page-a"); actions.forEach(x => a.append(x)); h.append(a); }
  pad.append(h);
  if (desc) { const d = el("p","page-d"); if (desc.nodeType) d.append(desc); else d.innerHTML = desc; pad.append(d); }
}
// The signal path: what this page's subject is actually plugged into, drawn as
// the chain it really is. Nodes carry live state (a dead server is red here,
// an untrusted file is amber) so the summary can never disagree with the list
// below it. `nodes` = [{label, value, state, view}].
function signalPath(pad, nodes) {
  const p = el("div","path");
  nodes.forEach((n, i) => {
    if (i) p.append(el("span","arw","──▶"));
    const node = el("span","n" + (n.state ? " " + n.state : "") + (n.view ? " clk" : ""));
    if (n.value != null) node.append(el("b", null, String(n.value)));
    node.append(document.createTextNode((n.value != null ? " " : "") + n.label));
    if (n.view) node.onclick = () => showTab(n.view);
    if (n.title) node.title = n.title;
    p.append(node);
  });
  pad.append(p);
}
function section(pad, title, filePath) {
  const s = el("div","sec");
  const h = el("div","sec-t"); h.append(document.createTextNode(title));
  if (filePath) { const f = el("span","fp", filePath); f.title = filePath; h.append(f); }
  s.append(h);
  pad.append(s);
  return s;
}
function zero(title, detail) {
  const z = el("div","zero"); z.append(el("div","zt", title)); z.append(el("div","zd", detail));
  return z;
}
function btn(label, cls, onclick) {
  const b = el("button", "b" + (cls ? " " + cls : ""), label);
  if (onclick) b.onclick = onclick;
  return b;
}
// A row that can expand in place. `build(body)` fills the drawer the first time
// it opens, so inspecting one server never renders the other twenty.
function listRow(opts) {
  const row = el("div","lrow");
  const top = el("div","lrow-top");
  top.append(el("span","caret","▶"));
  if (opts.mark) top.append(opts.mark);
  if (opts.dot) top.append(el("span","dot2 " + opts.dot));
  top.append(el("span","nm", opts.name));
  (opts.tags || []).forEach(t => top.append(el("span","t2 " + (t.cls||""), t.text)));
  const sub = el("span","sub" + (opts.subSans ? " sans" : ""), opts.sub || "");
  sub.title = opts.sub || ""; top.append(sub);
  const acts = el("div","acts"); (opts.actions || []).forEach(a => acts.append(a)); top.append(acts);
  acts.onclick = e => e.stopPropagation();
  const body = el("div","lbody");
  let built = false;
  top.onclick = () => {
    const open = !row.classList.contains("open");
    if (open && !built) { built = true; opts.build && opts.build(body); }
    row.classList.toggle("open", open);
  };
  row.append(top, body);
  row.openDrawer = () => { if (!built) { built = true; opts.build && opts.build(body); } row.classList.add("open"); };
  return row;
}
function armDelete(btn, onConfirm) {
  // Two-step delete: first click arms ("Confirm?"), second within 3s deletes.
  if (btn.dataset.armed) { onConfirm(); return; }
  const orig = btn.textContent;
  btn.textContent = "Confirm delete?"; btn.classList.add("armed"); btn.dataset.armed = "1";
  const reset = () => { btn.textContent = orig; btn.classList.remove("armed"); delete btn.dataset.armed; };
  btn._resetT = setTimeout(reset, 3000);
}
// Live filter over rows carrying a data-q haystack.
function wireFind(input, container, emptyText) {
  const apply = () => {
    const q = input.value.trim().toLowerCase();
    let shown = 0;
    container.querySelectorAll("[data-q]").forEach(r => {
      const ok = !q || q.split(/\s+/).every(t => r.dataset.q.includes(t));
      r.style.display = ok ? "" : "none"; if (ok) shown++;
    });
    let e = container.querySelector(".find-none");
    if (!shown) { if (!e) { e = el("div","find-none empty", emptyText || "Nothing matches that search."); container.append(e); } }
    else if (e) e.remove();
  };
  input.oninput = apply;
  return apply;
}
function findBox(placeholder) {
  const w = el("div","find"); const i = document.createElement("input");
  i.placeholder = placeholder; i.type = "search"; w.append(i);
  return { wrap: w, input: i };
}
// ---- skills ----
// A skill is a SKILL.md the agent pulls in on demand. The page shows what each
// one tells the agent (expand to read it) and lets you write one right here —
// the same file the terminal reads, no round trip through an editor.
function skillDetail(sk, scope, body, reload) {
  const dl = el("dl","kvs");
  kvRow(dl, "file", sk.path);
  if (sk.category) kvRow(dl, "category", sk.category);
  kvRow(dl, "loading", sk.always_load ? "always — injected into every session"
                                      : "on demand — the agent opens it when relevant");
  body.append(dl);
  const pre = el("div","jsonbox");
  const h = el("div","jh"); h.append(el("span","jt", "SKILL.md"));
  pre.append(h);
  const p = el("pre"); p.style.whiteSpace = "pre-wrap";
  p.textContent = sk.body || "(empty)"; pre.append(p);
  body.append(pre);
  const acts = el("div"); acts.style = "display:flex;gap:8px;margin-top:13px";
  acts.append(btn("Edit skill", "", () => openSkillEditor(sk, scope, reload)));
  body.append(acts);
}
function openSkillEditor(sk, scope, reload) {
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(el("h3", null, sk ? "Edit " + sk.name : "New skill"));
  s.append(el("div","sub", sk ? sk.path : "written to " + scope + " skills"));
  const name = input("skill name  ·  e.g. deploy-checklist"); name.value = sk ? sk.name : "";
  const desc = input("one line — when should the agent reach for this?");
  desc.value = sk ? (sk.description || "") : "";
  const cat = input("category (optional)"); cat.value = sk ? (sk.category || "") : "";
  const always = document.createElement("input"); always.type = "checkbox";
  always.checked = !!(sk && sk.always_load);
  const alwaysL = el("label","chk"); alwaysL.append(always, document.createTextNode("always load"));
  const ta = el("textarea","in");
  ta.style = "width:100%;min-height:240px;line-height:1.6;resize:vertical;margin-top:9px";
  ta.placeholder = "The how-to the agent reads. Markdown: steps, commands, gotchas.";
  ta.value = sk ? (sk.body || "") : "";
  const r1 = el("div"); r1.style = "display:flex;gap:8px;margin:14px 0 8px"; r1.append(name, cat);
  const r2 = el("div"); r2.style = "display:flex;gap:12px;align-items:center"; r2.append(desc, alwaysL);
  s.append(r1, r2, ta);
  const foot = el("div","cta");
  const save = btn(sk ? "Save changes" : "Create skill", "pri", async () => {
    if (!name.value.trim()) { toast("name required", true); name.focus(); return; }
    save.disabled = true;
    try {
      const r = await post("/api/skill", { scope, name: name.value, description: desc.value,
        body: ta.value, category: cat.value, always_load: always.checked,
        slug: sk ? sk.slug : undefined });
      if (r.ok) { toast(sk ? "saved " + name.value.trim() : "created " + name.value.trim()); hideModal(); reload(); }
      else toast(r.error || "failed", true);
    } catch (e) { toast(e.message, true); } finally { save.disabled = false; }
  });
  foot.append(save, btn("Cancel", "gho", hideModal));
  s.append(foot);
  showModal(true);
  setTimeout(() => (sk ? ta : name).focus(), 60);
}
// ---- MCP: an inspector, not a list ----
// Each row expands into the server's real configuration — command, args, env
// keys, url, headers, plus the raw JSON entry exactly as it sits on disk.
// Credentials arrive masked from the server; "Reveal" re-fetches the raw entry
// on demand so a screenshot of this page never leaks a token by default.
const SCOPE_TAG = { global: "", project: "acc", settings: "blu" };
function jsonBox(title, obj, extra) {
  const w = el("div","jsonbox");
  const h = el("div","jh"); h.append(el("span","jt", title));
  if (extra) h.append(extra);
  w.append(h);
  const pre = el("pre"); pre.textContent = JSON.stringify(obj, null, 2); w.append(pre);
  return { box: w, pre };
}
function kvRow(dl, key, value, mono) {
  dl.append(el("dt", null, key));
  const dd = el("dd", mono === false ? "wrap" : null);
  if (value && value.nodeType) dd.append(value); else dd.textContent = value;
  dl.append(dd);
}
function mcpDetail(sv, body, reload) {
  let revealed = null;                       // raw entry once the user asks
  const dl = el("dl","kvs");
  const render = () => {
    const e = revealed || sv.entry || {};
    dl.innerHTML = "";
    kvRow(dl, "transport", sv.transport);
    kvRow(dl, "defined in", sv.display_path || sv.path);
    if (e.command) kvRow(dl, "command", String(e.command));
    if (e.args && e.args.length) kvRow(dl, "args", e.args.map(String).join(" "));
    if (e.url) kvRow(dl, "url", String(e.url));
    ["env","headers"].forEach(k => {
      const v = e[k];
      if (v && typeof v === "object" && Object.keys(v).length) {
        const box = el("div");
        Object.entries(v).forEach(([kk, vv]) => {
          const line = el("div");
          line.append(document.createTextNode(kk + " = "));
          line.append(el("span", revealed ? null : "secret", String(vv)));
          box.append(line);
        });
        kvRow(dl, k, box);
      }
    });
    const known = { command:1, args:1, url:1, env:1, headers:1, type:1 };
    Object.keys(e).forEach(k => { if (!known[k]) kvRow(dl, k, typeof e[k] === "string" ? e[k] : JSON.stringify(e[k])); });
    jb.pre.textContent = JSON.stringify(e, null, 2);
    revBtn.textContent = revealed ? "Hide secrets" : "Reveal secrets";
    revBtn.classList.toggle("on", !!revealed);
  };
  const revBtn = btn("Reveal secrets", "gho", async () => {
    if (revealed) { revealed = null; render(); return; }
    try {
      const r = await api("/api/mcp/entry?" + q({ name: sv.name, scope: sv.scope }));
      if (!r.ok) { toast(r.error || "cannot read that entry", true); return; }
      revealed = r.entry; render();
    } catch (e) { toast(e.message, true); }
  });
  const jb = jsonBox("config json", sv.entry || {}, sv.secrets && sv.secrets.length ? revBtn : null);
  body.append(dl, jb.box);
  render();

  // Live probe: connect for real and show what the server exposes. The buttons
  // sit above their own output, so a result never reads as belonging to the row
  // below it.
  const acts = el("div"); acts.style = "display:flex;gap:8px;margin-top:14px;flex-wrap:wrap";
  const probeWrap = el("div");
  body.append(acts, probeWrap);
  const testBtn = btn("Test connection", "", async () => {
    testBtn.disabled = true; testBtn.textContent = "Connecting…";
    probeWrap.innerHTML = "";
    try {
      const r = await post("/api/mcp/test", { name: sv.name });
      const p = el("div","probe " + (r.ok ? "ok" : "bad"));
      const h = el("div","ph2");
      h.append(el("span","dot2 " + (r.ok ? "ok" : "bad")));
      h.append(document.createTextNode(r.ok
        ? "Connected · " + (r.tools || []).length + " tool" + ((r.tools||[]).length===1?"":"s") + " · " + r.ms + "ms"
        : "Failed to connect" + (r.ms != null ? " · " + r.ms + "ms" : "")));
      p.append(h);
      if (r.ok) {
        const g = el("div","toolgrid");
        (r.tools || []).forEach(t => { const k = el("span","tk", t.name); if (t.description) k.title = t.description; g.append(k); });
        p.append(g);
        if (!(r.tools || []).length) p.append(el("div","zd","The server connected but exposes no tools."));
      } else p.append(el("div","pe", r.error || "unknown error"));
      probeWrap.append(p);
    } catch (e) { toast(e.message, true); }
    finally { testBtn.disabled = false; testBtn.textContent = "Test connection"; }
  });
  acts.append(testBtn);
  if (sv.editable) acts.append(btn("Edit JSON", "", () => openMcpEditor(sv, reload)));
}
async function openMcpEditor(sv, reload) {
  let entry = sv.entry || {};
  try {
    const r = await api("/api/mcp/entry?" + q({ name: sv.name, scope: sv.scope }));
    if (r.ok) entry = r.entry;                     // edit the real thing, not the mask
  } catch (e) { /* fall back to the redacted copy */ }
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(el("h3", null, "Edit " + sv.name));
  s.append(el("div","sub", sv.display_path || sv.path));
  const ta = el("textarea","in");
  ta.style = "width:100%;min-height:230px;line-height:1.55;resize:vertical";
  ta.value = JSON.stringify(entry, null, 2);
  s.append(ta);
  const foot = el("div","cta");
  const save = btn("Save changes", "pri", async () => {
    let parsed;
    try { parsed = JSON.parse(ta.value); }
    catch (e) { toast("that isn't valid JSON: " + e.message, true); return; }
    save.disabled = true;
    try {
      const r = await post("/api/mcp", { scope: sv.scope, name: sv.name, entry: parsed });
      if (r.ok) { toast("saved " + sv.name); hideModal(); reload(); }
      else toast(r.error || "failed", true);
    } catch (e) { toast(e.message, true); } finally { save.disabled = false; }
  });
  foot.append(save, btn("Cancel", "gho", hideModal));
  s.append(foot);
  showModal(true);
  setTimeout(() => ta.focus(), 60);
}
// One field that takes whatever the user already has on their clipboard: the
// {"mcpServers": …} blob every MCP README ships, a lone entry object, a shell
// command, or a URL. The server parses it (same code path as the terminal's
// /mcp add), so the two surfaces can never disagree about what's valid.
function mcpComposer(onDone) {
  const f = el("div","comp");
  const scope = el("select","in fit");
  scope.innerHTML = '<option value="global">global · every project</option>' +
                    '<option value="project">project · this repo</option>';
  const name = input("name (only needed if your paste has none)");
  const r1 = el("div","r"); r1.append(name, scope); f.append(r1);
  const ta = el("textarea","in");
  ta.placeholder = '{\n  "mcpServers": {\n    "github": {\n      "command": "npx",\n      "args": ["-y", "@modelcontextprotocol/server-github"],\n      "env": { "GITHUB_TOKEN": "ghp_…" }\n    }\n  }\n}';
  f.append(ta);
  const add = btn("Add server", "pri", async () => {
    const text = ta.value.trim();
    if (!text) { toast("paste a config, a command, or a URL", true); return; }
    add.disabled = true;
    try {
      let r = await post("/api/mcp/paste", { scope: scope.value, text });
      if (!r.ok && r.needs_name) {
        // A bare command/URL: name it here and post it as a single entry.
        const nm = name.value.trim();
        if (!nm) { toast("give it a name — the paste doesn't include one", true); name.focus(); return; }
        r = await post("/api/mcp", { scope: scope.value, name: nm, entry: entryFromText(text) });
        if (r.ok) r.added = [nm];
      }
      if (r.ok) { toast("added " + (r.added || []).join(", ")); ta.value = ""; name.value = ""; onDone(); }
      else toast(r.error || "failed", true);
    } catch (e) { toast(e.message, true); } finally { add.disabled = false; }
  });
  const foot = el("div","foot");
  const hint = el("div","hint");
  hint.innerHTML = "Takes a whole <code>mcpServers</code> blob, one entry object, a command " +
    "(<code>npx -y pkg</code>) or an <code>https://</code> URL. Comments and trailing commas are fine.";
  foot.append(hint, add);
  f.append(foot);
  return f;
}
// Mirror of the server's quick-parse for the "needs a name" case.
function entryFromText(s) {
  s = s.trim();
  if (s.startsWith("{")) { try { return JSON.parse(s); } catch (e) { return {}; } }
  if (/^https?:\/\//.test(s)) return { type: s.replace(/\/$/,"").endsWith("/sse") ? "sse" : "http", url: s };
  const p = s.split(/\s+/);
  return { command: p[0], args: p.slice(1) };
}
let skillsReq = 0;
async function loadSkills() {
  const pad = document.getElementById("skillspad");
  const my = ++skillsReq;
  if (!pad.childElementCount) skeleton(pad);
  let sk;
  try { sk = await api("/api/skills"); }
  catch (e) { if (my !== skillsReq) return; pad.innerHTML = ""; pad.append(el("div","empty","Error: " + e.message)); return; }
  if (my !== skillsReq) return;
  pad.innerHTML = "";
  const reload = () => loadSkills();
  const total = sk.global.length + sk.project.length;

  const newG = btn("+ New global skill", "pri", () => openSkillEditor(null, "global", reload));
  const newP = btn("+ project", "", () => openSkillEditor(null, "project", reload));
  pageHead(pad, "skills", total,
    "Playbooks the agent reads when a task matches — a deploy checklist, your review rules, " +
    "how to talk to a flaky internal API. Global ones follow you everywhere; project ones " +
    "live in the repo and travel with it.", [newP, newG]);
  const always = [...sk.global, ...sk.project].filter(x => x.always_load).length;
  signalPath(pad, [
    { label: "session", state: "dim" },
    { value: always, label: "always loaded",
      title: "injected into every session's context" },
    { value: total - always, label: "on demand", state: "dim",
      title: "opened when the task matches" },
    { value: sk.project.length, label: "from this repo", state: "dim" },
  ]);

  if (!total) {
    pad.append(zero("No skills yet",
      "Write down something you explain to the agent twice a week — the steps, the commands, " +
      "the gotchas. It'll pull the file in the next time the task looks like that one."));
    return;
  }
  const find = findBox("Filter skills — name, description, category…");
  if (total > 5) pad.append(find.wrap);

  [["global", sk.global, sk.global_dir, "global · every project"],
   ["project", sk.project, sk.project_dir, "project · this repo"]].forEach(([scope, list, dir, label]) => {
    const sec = section(pad, label, dir);
    if (!list.length) { sec.append(zero("No " + scope + " skills",
      "New ones land in " + dir + ".")); return; }
    const box = el("div","list");
    list.forEach(s => {
      const tags = [];
      if (s.always_load) tags.push({ text: "always", cls: "vio" });
      if (s.category) tags.push({ text: s.category, cls: "" });
      const del = btn("Delete", "gho dan", null);
      del.onclick = () => armDelete(del, async () => {
        try { const r = await post("/api/skill/delete", { scope, slug: s.slug });
          if (r.ok) { toast("deleted " + s.name); reload(); } else toast(r.error||"failed", true); }
        catch (e) { toast(e.message, true); }
      });
      const row = listRow({
        name: s.name, sub: s.description || "(no description)", subSans: true, tags,
        actions: [btn("Edit", "gho", () => openSkillEditor(s, scope, reload)), del],
        build: (body) => skillDetail(s, scope, body, reload),
      });
      row.dataset.q = (s.name + " " + (s.description||"") + " " + (s.category||"")).toLowerCase();
      box.append(row);
    });
    sec.append(box);
  });
  wireFind(find.input, pad, "No skill matches that filter.");
}
let mcpReq = 0;
async function loadMcp() {
  const pad = document.getElementById("mcppad");
  const my = ++mcpReq;
  if (!pad.childElementCount) skeleton(pad);
  let mc;
  try { mc = await api("/api/mcp"); }
  catch (e) { if (my !== mcpReq) return; pad.innerHTML = ""; pad.append(el("div","empty","Error: " + e.message)); return; }
  if (my !== mcpReq) return;
  pad.innerHTML = "";
  const reload = () => loadMcp();

  const composer = mcpComposer(reload);
  const addBtn = btn("+ Add server", "pri", () => {
    const on = composer.classList.toggle("on");
    addBtn.textContent = on ? "Cancel" : "+ Add server";
    addBtn.classList.toggle("pri", !on);
    if (on) composer.querySelector("textarea").focus();
  });
  pageHead(pad, "mcp servers", mc.servers.length,
    "Servers that hand the agent extra tools. Paste any <code>mcpServers</code> config to add one, " +
    "expand a row to see exactly what it runs, and test it live before you rely on it.", [addBtn]);
  const stdio = mc.servers.filter(s => s.transport === "stdio").length;
  const held = (mc.withheld || []).length;
  signalPath(pad, [
    { label: "agent", state: "dim" },
    { value: mc.servers.length, label: "server" + (mc.servers.length===1?"":"s") },
    { value: stdio, label: "local", state: "dim",
      title: stdio + " run a command on this machine" },
    { value: mc.servers.length - stdio, label: "remote", state: "dim" },
    held ? { value: held, label: "withheld", state: "warn", title: "untrusted .mcp.json" }
         : { label: "all trusted", state: "dim" },
  ]);
  pad.append(composer);

  // Project .mcp.json is attacker-controlled data — offer the trust gate here
  // rather than making the user go find the terminal command.
  if (mc.project_exists && !mc.project_trusted) {
    const b = el("div","banner");
    const txt = el("div","sp");
    txt.innerHTML = "<b>This project's <code>.mcp.json</code> isn't trusted yet.</b> " +
      "Its stdio servers won't start until you approve the file — they run local commands.";
    b.append(txt);
    b.append(btn("Trust this file", "", async () => {
      try { const r = await post("/api/mcp/trust", {});
        if (r.ok) { toast("trusted this project's .mcp.json"); reload(); } else toast(r.error||"failed", true); }
      catch (e) { toast(e.message, true); }
    }));
    pad.append(b);
  }

  if (!mc.servers.length) {
    pad.append(zero("No MCP servers configured",
      "Add one to give the agent tools it doesn't ship with — GitHub, a database, your " +
      "internal API. Paste a server's config blob straight from its README."));
    return;
  }

  const find = findBox("Filter servers — name, command, url…");
  if (mc.servers.length > 5) pad.append(find.wrap);

  const byScope = { global: [], project: [], settings: [] };
  mc.servers.forEach(sv => (byScope[sv.scope] || (byScope[sv.scope] = [])).push(sv));
  const files = { global: mc.global_file, project: mc.project_file, settings: "settings.json" };
  const labels = { global: "global · every project", project: "project · this repo",
                   settings: "settings.json · read-only here" };
  ["global","project","settings"].forEach(scope => {
    const list = byScope[scope] || [];
    if (!list.length && scope === "settings") return;
    const sec = section(pad, labels[scope], files[scope]);
    if (!list.length) { sec.append(zero("Nothing here yet", "Servers added with the “" + scope +
      "” scope land in " + files[scope] + ".")); return; }
    const box = el("div","list");
    list.forEach(sv => {
      const withheld = (mc.withheld || []).includes(sv.name);
      const tags = [{ text: sv.transport, cls: sv.transport === "stdio" ? "" : "blu" }];
      if (withheld) tags.push({ text: "needs trust", cls: "amb" });
      const acts = [];
      if (sv.editable) {
        acts.push(btn("Edit", "gho", () => openMcpEditor(sv, reload)));
        const del = btn("Delete", "gho dan", null);
        del.onclick = () => armDelete(del, async () => {
          try { const r = await post("/api/mcp/delete", { scope: sv.scope, name: sv.name });
            if (r.ok) { toast("removed " + sv.name); reload(); } else toast(r.error||"failed", true); }
          catch (e) { toast(e.message, true); }
        });
        acts.push(del);
      }
      const row = listRow({
        name: sv.name, sub: sv.detail, dot: withheld ? "warn" : "ok", tags, actions: acts,
        build: (body) => mcpDetail(sv, body, reload),
      });
      row.dataset.q = (sv.name + " " + sv.detail + " " + sv.transport + " " + sv.scope).toLowerCase();
      box.append(row);
    });
    sec.append(box);
  });
  wireFind(find.input, pad, "No server matches that filter.");
}

// ---- config ----
function fmtVal(v) {
  if (v == null) return "—";
  if (typeof v === "object") return JSON.stringify(v, null, 2);
  return String(v);
}
let configReq = 0;
async function loadConfig() {
  const pad = document.getElementById("configpad");
  const my = ++configReq;
  if (!pad.childElementCount) skeleton(pad);
  const c = await api("/api/config");
  if (my !== configReq) return;   // superseded by a newer loadConfig()
  pad.innerHTML = "";
  const merged = c.merged || {};
  const keys = Object.keys(merged).sort();

  pageHead(pad, "config", keys.length,
    "The settings mantis is actually running with, and which file each one came from. " +
    "Secrets are redacted here.");
  const lc = (src) => Object.keys((c.layers || {})[src] || {}).length;
  signalPath(pad, [
    { label: "defaults", state: "dim" },
    { value: lc("user"), label: "user" },
    { value: lc("project"), label: "project" },
    { value: lc("local"), label: "local" },
    { value: keys.length, label: "effective", state: "dim" },
  ]);
  if (!keys.length) {
    pad.append(zero("Running on defaults",
      "No settings files found — mantis is using its built-in defaults. Anything you set in " +
      "settings.json will show up here with the layer it came from."));
  } else {
    const t = el("div","cfg");
    keys.forEach(k => {
      const row = el("div","kv");
      row.append(el("div","ck", k));
      row.append(el("div","cv", fmtVal(merged[k])));
      t.append(row);
    });
    pad.append(t);
  }

  const laySec = section(pad, "Layers · later overrides earlier");
  ["user","project","local"].forEach(src => {
    const layer = (c.layers||{})[src] || {};
    const d = el("details","layer");
    const n = Object.keys(layer).length;
    d.append(el("summary", null, src + "  ·  " + n + " setting" + (n===1?"":"s")));
    if ((c.paths||{})[src]) d.append(el("div","layerpath", (c.paths)[src]));
    d.append(el("pre", null, JSON.stringify(layer, null, 2)));
    laySec.append(d);
  });
}

loadOverview().catch(e => console.error(e));
loadProjects().catch(e => document.getElementById("projects").append(el("div","empty","Error: " + e.message)));
{
  // Deep link into one conversation: /?cwd=<project path>&session=<id>. The
  // live-activity rows use it to land in a job's session; it also makes a
  // session URL something you can paste to the other device on the LAN.
  const qs = new URLSearchParams(location.search);
  const t = location.hash.slice(1);
  if (qs.get("session") && qs.get("cwd")) {
    // land with the project and session cards selected, not just the transcript
    jumpToSession(qs.get("cwd"), qs.get("session"));
  }
  else if (["sessions","models","deploy","skills","mcp","config"].includes(t)) showTab(t);
  else loadHome();   // default landing
}
</script>
</body>
</html>
"""

# The mantis mark (side-profile praying mantis) — served at /mantis.svg and
# used in the header + favicon. Kept in-package so it ships in the wheel.
MANTIS_SVG = r"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 128 128" width="128" height="128" role="img" aria-label="praying mantis">
<defs>
<linearGradient id="back" x1="0.3" y1="0" x2="0.7" y2="1">
    <stop offset="0" stop-color="#2c5e38"/><stop offset="0.45" stop-color="#468950"/><stop offset="1" stop-color="#83c87b"/>
  </linearGradient>
  <linearGradient id="neck" x1="0" y1="0" x2="0.6" y2="1">
    <stop offset="0" stop-color="#74b96f"/><stop offset="1" stop-color="#356f41"/>
  </linearGradient>
  <linearGradient id="wing" x1="0.15" y1="0.05" x2="0.85" y2="1">
    <stop offset="0" stop-color="#69b064"/><stop offset="0.55" stop-color="#3f7d48"/><stop offset="1" stop-color="#274f31"/>
  </linearGradient>
  <linearGradient id="wingsheen" x1="0" y1="0" x2="0.3" y2="1">
    <stop offset="0" stop-color="#ffffff" stop-opacity="0.38"/><stop offset="0.55" stop-color="#ffffff" stop-opacity="0"/>
  </linearGradient>
  <linearGradient id="femur" x1="0" y1="0" x2="0.5" y2="1">
    <stop offset="0" stop-color="#84c67c"/><stop offset="1" stop-color="#3a7a49"/>
  </linearGradient>
  <linearGradient id="leg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#58995c"/><stop offset="1" stop-color="#295232"/>
  </linearGradient>
  <linearGradient id="legfar" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#3a6f43"/><stop offset="1" stop-color="#234b2e"/>
  </linearGradient>
  <linearGradient id="headg" x1="0.2" y1="0" x2="0.7" y2="1">
    <stop offset="0" stop-color="#6cb066"/><stop offset="1" stop-color="#346e40"/>
  </linearGradient>
  <radialGradient id="eye" cx="0.36" cy="0.28" r="0.9">
    <stop offset="0" stop-color="#eaf2d0"/><stop offset="0.38" stop-color="#a9d184"/>
    <stop offset="0.72" stop-color="#5a9a54"/><stop offset="1" stop-color="#31663c"/>
  </radialGradient>
  <radialGradient id="gshadow" cx="0.5" cy="0.5" r="0.5">
    <stop offset="0" stop-color="#1c1a15" stop-opacity="0.28"/><stop offset="1" stop-color="#1c1a15" stop-opacity="0"/>
  </radialGradient>
  <linearGradient id="rim" x1="0" y1="0" x2="0" y2="1">
    <stop offset="0" stop-color="#c7ecb8" stop-opacity="0.9"/><stop offset="1" stop-color="#c7ecb8" stop-opacity="0"/>
  </linearGradient>
</defs>
<g id="mx" stroke-linecap="round" stroke-linejoin="round">
    <!-- ground contact shadow -->
    <ellipse cx="76" cy="122" rx="46" ry="6.5" fill="url(#gshadow)"/>

    <!-- ===== FAR legs ===== -->
    <g fill="none" stroke="url(#legfar)">
      <path stroke-width="2.3" d="M64 68 L53 92 L64 112 L58 122"/>
      <path stroke-width="2.3" d="M75 76 L91 94 L103 114 L113 122"/>
      <g stroke-width="1.2"><path d="M64 112 L61 121"/><path d="M103 114 L106 122"/></g>
    </g>

    <!-- ===== ABDOMEN (segmented, curled tip with cerci) ===== -->
    <path fill="url(#back)" stroke="#244d2f" stroke-width="0.8"
      d="M58 62 C69 63 85 74 99 92 C108 104 112 111 108 113 C104 114.5 97 108 90 99
         C78 86 63 79 56 72 C52 67 53 61 58 62 Z"/>
    <g stroke="#244d2f" stroke-width="0.6" opacity="0.5" fill="none">
      <path d="M64 69 C68 73 70 77 70 81"/>
      <path d="M72 75 C76 80 78 84 78 89"/>
      <path d="M81 83 C85 88 87 92 87 97"/>
      <path d="M90 92 C94 97 96 101 95 105"/>
    </g>
    <!-- cerci at abdomen tip -->
    <g fill="none" stroke="#2f6339" stroke-width="1"><path d="M108 113 L113 116"/><path d="M106 114 L110 119"/></g>

    <!-- ===== WING (tegmen) — tapered, veined, costal edge ===== -->
    <path fill="url(#wing)" stroke="#244d2f" stroke-width="0.85"
      d="M55 57 C74 57 97 70 114 94 C119 101 117 106 112 104 C102 100 89 92 78 82
         C65 71 57 68 51 63 C48 60 51 56 55 57 Z"/>
    <!-- costal (leading) edge, darker -->
    <path fill="none" stroke="#1f4429" stroke-width="1.1" opacity="0.6"
      d="M55 57.5 C74 57.5 96 70 113 93.5"/>
    <path fill="url(#wingsheen)"
      d="M56 59 C72 59 92 70 107 90 C110 95 109 99 105 97 C96 93 86 86 77 78
         C66 68 58 66 53 63 C50 61 52 58 56 59 Z"/>
    <!-- venation -->
    <g fill="none" stroke="#274f31" stroke-width="0.5" opacity="0.55">
      <path d="M57 60 C71 63 88 74 103 93"/>
      <path d="M56 64 C69 68 84 79 98 97"/>
      <path d="M56 69 C67 73 80 84 92 100"/>
      <path d="M58 74 C67 78 77 87 87 101"/>
      <!-- cross veins -->
      <path d="M66 63 L64 68" stroke-width="0.4"/><path d="M78 71 L75 77" stroke-width="0.4"/><path d="M90 82 L86 89" stroke-width="0.4"/>
    </g>

    <!-- wing mottling -->
    <g fill="#254f31" opacity="0.28">
      <ellipse cx="74" cy="73" rx="2.4" ry="1.5" transform="rotate(38 74 73)"/>
      <ellipse cx="88" cy="86" rx="2" ry="1.2" transform="rotate(40 88 86)"/>
      <ellipse cx="64" cy="67" rx="1.6" ry="1" transform="rotate(35 64 67)"/>
    </g>
    <!-- ===== PROTHORAX (long neck) with rim light ===== -->
    <path fill="url(#neck)" stroke="#244d2f" stroke-width="0.8"
      d="M36 41 C41 40 47 45 54 54 C58 59 60 63 58 65 C56 67 52 64 48 59
         C42 51 37 46 34 44 C32 42 34 41 36 41 Z"/>
    <path fill="none" stroke="url(#rim)" stroke-width="1" d="M37 41.5 C42 41 48 46 55 55"/>

    <!-- ===== NEAR walking legs (jointed, spurs) ===== -->
    <g fill="none" stroke="url(#leg)">
      <path stroke-width="2.9" d="M60 64 L48 88 L57 110 L50 121"/>
      <path stroke-width="2.9" d="M71 72 L88 92 L100 114 L110 123"/>
      <g stroke-width="1.5"><path d="M57 110 L47 118"/><path d="M100 114 L112 120"/></g>
    </g>
    <g fill="none" stroke="#2c5636" stroke-width="0.55" opacity="0.75">
      <path d="M53 76 L51 78"/><path d="M55 82 L53 84"/><path d="M80 84 L82 82"/><path d="M84 90 L86 88"/>
    </g>

    <!-- ===== HEAD ===== -->
    <path fill="url(#headg)" stroke="#244d2f" stroke-width="0.8"
      d="M35 30 C39.5 30 42.5 33 42.5 38 C42.5 43.4 39 47.4 33 48.4 C26.5 49.4 21.5 46 21.5 42.6
         C21.5 38.6 26 32.6 35 30 Z"/>
    <path fill="none" stroke="url(#rim)" stroke-width="0.9" d="M35 30.6 C39 30.6 41.8 33.4 42 37.6"/>
    <!-- mouth / palps -->
    <path fill="#2b5a35" d="M22.5 43.6 C20.4 44.6 19.6 46.6 21.6 46.8 C23.6 47 25.6 45.6 25.4 43.8 Z"/>
    <path fill="none" stroke="#2b5a35" stroke-width="0.8" d="M23.5 47 L22 50 M25.5 47.4 L24.8 50.6"/>
    <!-- compound eye -->
    <ellipse cx="33" cy="36" rx="5.6" ry="6.4" fill="url(#eye)" transform="rotate(-18 33 36)"/>
    <ellipse cx="34.6" cy="38.6" rx="1.7" ry="2.2" fill="#20391f" opacity="0.92" transform="rotate(-18 34.6 38.6)"/>
    <circle cx="30.5" cy="32.6" r="1.25" fill="#fff" opacity="0.92"/>
    <circle cx="35.8" cy="34.4" r="0.5" fill="#fff" opacity="0.6"/>
    <!-- antennae -->
    <g fill="none" stroke="#356f41" stroke-width="1.25">
      <path d="M34 29 C26 19 18 12 7 8"/>
      <path d="M37.5 30 C31 20 24 12 15 5.5"/>
    </g>

    <!-- ===== RAPTORIAL FORELEGS ===== -->
    <!-- far foreleg -->
    <g fill="none" stroke="#3a7047">
      <path stroke-width="3" d="M55 60 L45 49 L33 43 L43.5 39.5"/>
    </g>
    <!-- near : coxa -->
    <path fill="url(#femur)" stroke="#244d2f" stroke-width="0.7"
      d="M54 61 C51 57 48 53 44 50 C41 48 39 49 40 51 C42 55 46 59 50 62 C52 63 55 63 54 61 Z"/>
    <!-- femur -->
    <path fill="url(#femur)" stroke="#244d2f" stroke-width="0.7"
      d="M44 50 C40 47 33.5 43.6 28.5 42.6 C26 42.1 25 43.8 27 45.3 C31.6 48.8 38 52 42 54 C44.2 55.1 46 51.5 44 50 Z"/>
    <path fill="none" stroke="#2c6a3c" stroke-width="0.5" opacity="0.6" d="M30 44.5 C34 47 39 49.5 43 51.5"/>
    <!-- tibia (folded blade + hook) -->
    <path fill="url(#femur)" stroke="#244d2f" stroke-width="0.7"
      d="M28.5 42.6 C33 40 39.5 38.4 44.6 38.6 C47.2 38.7 47.6 40.9 45.4 41.9 C41 44 34.5 45.8 30.8 46.2 C28.6 46.4 26 43.9 28.5 42.6 Z"/>
    <!-- hook tip -->
    <path fill="none" stroke="#244d2f" stroke-width="1.4" d="M44.8 38.8 C47 38 48 39.5 46.6 40.8"/>
    <!-- double spine rows -->
    <g fill="none" stroke="#dc9265" stroke-width="0.75" opacity="0.95">
      <path d="M31 45.4 L31.9 47.2"/><path d="M34.6 46.9 L35.4 48.7"/><path d="M38 48.4 L38.7 50.2"/><path d="M41 49.9 L41.6 51.7"/>
    </g>
    <g fill="none" stroke="#b5d99f" stroke-width="0.55" opacity="0.8">
      <path d="M32 43.2 L32.6 41.7"/><path d="M35.4 42.4 L36 40.9"/><path d="M38.6 41.7 L39.2 40.3"/>
    </g>
  </g>
</svg>
"""
