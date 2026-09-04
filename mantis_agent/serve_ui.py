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
     Neutral surfaces, one accent, NO LINES. Elevation is a background step:
     bg → panel → panel-2 → fill. Cards are filled rounded surfaces; hover is
     one step lighter; selected is the accent tint. Sans for the UI, mono only
     for the things that are literally text on this machine: ids, paths,
     model names, code. Tokens first; everything below reads them.
     ========================================================================== */
  :root {
    --bg: #eff1f4; --panel: #ffffff; --panel-2: #f4f5f7; --fill: #e7e9ee; --hover: #f7f8fa; --fill-2: #dde0e6;
    --line: rgba(0,0,0,.08);
    --ink: #111111; --ink-2: #4b5058; --ink-3: #7d8290;
    --accent: #2f8f3a; --accent-ink: #ffffff; --accent-soft: rgba(47,143,58,.13); --accent-soft-2: rgba(47,143,58,.22);
    --ok: #2f8f3a; --warn: #c27a10; --bad: #d23f31; --info: #2f6fdd;
    --ok-soft: rgba(47,143,58,.13); --warn-soft: rgba(194,122,16,.14); --bad-soft: rgba(210,63,49,.12);
    --info-soft: rgba(47,111,221,.12);
    --user: #2f6fdd; --tool: #6b7280; --err: #d23f31; --caution: #c27a10; --caution-soft: rgba(194,122,16,.14);
    --radius: 12px; --r-sm: 8px; --dim: rgba(20,22,26,.35);
    --sans: -apple-system, BlinkMacSystemFont, Inter, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    --t: 140ms cubic-bezier(.2,.7,.2,1);
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0a0b0d; --panel: #111316; --panel-2: #16181c; --fill: #1c1f24; --hover: #191c21; --fill-2: #262a30;
      --line: rgba(255,255,255,.08);
      --ink: #ededed; --ink-2: #9a9ea6; --ink-3: #6e7380;
      --accent: #58c467; --accent-ink: #08130a; --accent-soft: rgba(88,196,103,.14); --accent-soft-2: rgba(88,196,103,.24);
      --ok: #58c467; --warn: #e0a24a; --bad: #ee6a5e; --info: #6ea2ff;
      --ok-soft: rgba(88,196,103,.14); --warn-soft: rgba(224,162,74,.15); --bad-soft: rgba(238,106,94,.14);
      --info-soft: rgba(110,162,255,.14);
      --user: #6ea2ff; --tool: #9a9ea6; --err: #ee6a5e; --caution: #e0a24a; --caution-soft: rgba(224,162,74,.15);
      --dim: rgba(0,0,0,.45);
      color-scheme: dark;
    }
  }
  :root[data-theme="dark"] {
    --bg: #0a0b0d; --panel: #111316; --panel-2: #16181c; --fill: #1c1f24; --hover: #191c21; --fill-2: #262a30;
    --line: rgba(255,255,255,.08);
    --ink: #ededed; --ink-2: #9a9ea6; --ink-3: #6e7380;
    --accent: #58c467; --accent-ink: #08130a; --accent-soft: rgba(88,196,103,.14); --accent-soft-2: rgba(88,196,103,.24);
    --ok: #58c467; --warn: #e0a24a; --bad: #ee6a5e; --info: #6ea2ff;
    --ok-soft: rgba(88,196,103,.14); --warn-soft: rgba(224,162,74,.15); --bad-soft: rgba(238,106,94,.14);
    --info-soft: rgba(110,162,255,.14);
    --user: #6ea2ff; --tool: #9a9ea6; --err: #ee6a5e; --caution: #e0a24a; --caution-soft: rgba(224,162,74,.15);
    --dim: rgba(0,0,0,.45);
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: var(--sans); background: var(--bg); color: var(--ink);
    font-size: 13px; line-height: 1.5; -webkit-font-smoothing: antialiased;
    display: grid; grid-template-rows: 50px 1fr; height: 100vh; overflow: hidden;
  }
  a { color: var(--accent); text-decoration: none; }
  :focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent); border-radius: 6px; }
  ::selection { background: var(--accent-soft-2); }
  @media (prefers-reduced-motion: reduce) {
    * { animation-duration: .001ms !important; transition-duration: .001ms !important; }
  }

  /* ---- shell: one slim top bar, tabs as pills ---- */
  #top { display: flex; align-items: center; gap: 20px; padding: 0 16px; background: var(--panel); min-width: 0; }
  .brand { display: flex; align-items: center; gap: 8px; flex: none; }
  .brand img { width: 22px; height: 22px; display: block; }
  .brand span { font-weight: 700; font-size: 13.5px; letter-spacing: -.02em; }
  #nav { display: flex; align-items: center; gap: 3px; flex: 0 1 auto; min-width: 0; overflow-x: auto; scrollbar-width: none; }
  #nav::-webkit-scrollbar { display: none; }
  #nav button { display: inline-flex; align-items: center; font: inherit; font-size: 13px; font-weight: 500; margin: 0;
    padding: 6px 10px; border: 0; border-radius: 6px; background: transparent; color: var(--ink-2); cursor: pointer;
    white-space: nowrap; flex: none; transition: background var(--t), color var(--t); }
  #nav button:hover { background: var(--fill); color: var(--ink); }
  /* active = colour + fill only; the weight never changes, so the group never shifts */
  #nav button.on { background: var(--accent-soft); color: var(--accent); }
  .topr { margin-left: auto; display: flex; align-items: center; gap: 8px; flex: none; min-width: 0; }
  .railfoot { display: flex; align-items: center; gap: 7px; font-size: 12px; color: var(--ink-2); min-width: 0; max-width: 34vw; }
  .rf-v { font-family: var(--mono); font-size: 12px; font-weight: 600; color: var(--ink); overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .rf-s { color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .rf-c, .rf-l, .kbd { display: none; }
  .tb { display: inline-flex; align-items: center; gap: 7px; height: 30px; padding: 0 10px; font: inherit; font-size: 12.5px;
    color: var(--ink-2); background: var(--fill); border: 0; border-radius: 8px; cursor: pointer; white-space: nowrap;
    transition: background var(--t), color var(--t); }
  .tb:hover { background: var(--fill-2); color: var(--ink); }
  .tb kbd { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); background: var(--panel); border-radius: 4px;
    padding: 1px 5px; line-height: 1.5; }
  .tb.icon { width: 30px; padding: 0; justify-content: center; font-size: 14px; }
  .lan { font-family: var(--mono); font-size: 10.5px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase;
    padding: 4px 8px; border-radius: 6px; background: var(--fill); color: var(--ink-3); white-space: nowrap; }
  .lan.on { color: var(--warn); background: var(--warn-soft); }
  .live { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); flex: none; animation: pulse 2.6s ease-in-out infinite; }
  .live.off { background: var(--ink-3); animation: none; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }

  main { overflow: hidden; min-height: 0; }
  .view { display: none; height: 100%; }
  .view.on { display: block; }
  .scroll { overflow-y: auto; height: 100%; }
  .page { max-width: 1280px; margin: 0 auto; padding: 26px 24px 70px; }
  .page.wide { max-width: 1560px; }

  /* ---- page furniture ---- */
  .page-h { display: flex; align-items: center; gap: 12px; margin-bottom: 4px; }
  .page-t { font-size: 18px; font-weight: 600; letter-spacing: -.02em; margin: 0; display: flex; align-items: center; gap: 10px; }
  .count { font-family: var(--mono); font-size: 11px; font-weight: 600; color: var(--ink-2); background: var(--fill);
    padding: 1px 7px; border-radius: 6px; font-variant-numeric: tabular-nums; }
  .page-d { color: var(--ink-2); font-size: 13px; line-height: 1.55; max-width: 72ch; margin: 0 0 18px; }
  .page-d code, .mono { font-family: var(--mono); font-size: 11.5px; color: var(--ink-2); background: var(--fill);
    padding: 1px 5px; border-radius: 4px; }
  .page-a { margin-left: auto; display: flex; gap: 8px; align-items: center; flex: none; }
  .sec { margin-top: 28px; }
  .sec-t { font-size: 13px; font-weight: 600; color: var(--ink-2); margin: 0 0 10px; display: flex; align-items: center;
    gap: 10px; flex-wrap: wrap; row-gap: 8px; }
  @media (max-width: 1200px) {
    /* the provider toggle and the token state drop to their own line */
    .sec-t .dp-ptoggle { order: 3; flex-basis: 100%; margin-left: 0; }
    .sec-t .hf-state { order: 4; margin-left: 0; }
  }
  .sec-t .fp { font-family: var(--mono); font-size: 11px; font-weight: 400; color: var(--ink-3); flex: none; max-width: 46%;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* ---- cards & lists: filled surfaces; hover one step; selected = accent tint ---- */
  .card, .card2, .fam, .dpc, .setup, .trace, .ctxbox, .hero, .host, .selfhost-card, .comp, details.layer,
  .cfg, .list, .mtable, .browse, .mcard, .gcard { background: var(--panel); border-radius: var(--radius); }
  .card { padding: 15px 16px; display: flex; flex-direction: column; gap: 10px; }
  .card2 { padding: 14px 16px 16px; }
  .card2 h3 { font-size: 13px; font-weight: 600; color: var(--ink-2); margin: 0 0 3px; }
  .card2 .note2 { font-size: 12px; color: var(--ink-3); margin-bottom: 12px; }
  .card2 .note2 b, .note2 b { color: var(--ink-2); font-family: var(--mono); font-weight: 600; }
  .note2 { font-size: 12px; color: var(--ink-3); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
  .card .head { display: flex; align-items: center; gap: 8px; }
  .card .name { font-weight: 600; font-size: 13.5px; }
  .card .url { font-family: var(--mono); font-size: 11px; color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card .note { font-size: 12px; color: var(--ink-3); line-height: 1.45; }
  .card.cur, .fam.cur, .dpc.cur { background: var(--accent-soft); }
  .card.flash, .lrow.flash, .msg.flash { box-shadow: 0 0 0 2px var(--accent); }
  .list { padding: 4px; }
  .lrow { border-radius: var(--r-sm); }
  .lrow + .lrow { margin-top: 2px; }
  .lrow-top { display: flex; align-items: center; gap: 10px; padding: 11px 12px; cursor: pointer; border-radius: var(--r-sm);
    transition: background var(--t); }
  .lrow-top:hover, .lrow.open .lrow-top { background: var(--panel-2); }
  .lrow .nm { font-weight: 600; font-size: 13px; flex: none; }
  .lrow .sub { color: var(--ink-3); font-size: 12px; font-family: var(--mono); flex: 1; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; min-width: 0; }
  .lrow .sub.sans { font-family: var(--sans); color: var(--ink-2); }
  .lrow .acts { display: flex; gap: 6px; flex: none; opacity: .7; transition: opacity var(--t); }
  .lrow:hover .acts, .lrow.open .acts { opacity: 1; }
  .lrow .caret { color: var(--ink-3); font-size: 8px; width: 9px; flex: none; transition: transform var(--t); }
  .lrow.open .caret { transform: rotate(90deg); }
  .lbody { display: none; padding: 6px 14px 16px 32px; }
  .lrow.open .lbody { display: block; animation: drawer .16s ease-out; }
  @keyframes drawer { from { opacity: 0; transform: translateY(-3px); } to { opacity: 1; transform: none; } }

  /* dots · tags · chips · pills · buttons — one vocabulary */
  .dot2 { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--ink-3); }
  .dot2.ok { background: var(--ok); } .dot2.bad { background: var(--bad); } .dot2.warn { background: var(--warn); }
  .dot2.run { background: var(--ok); animation: pulse 1.6s ease-in-out infinite; }
  .dot2.pend { background: var(--warn); }
  .t2 { font-size: 10px; font-weight: 600; letter-spacing: .03em; text-transform: uppercase; padding: 2px 6px; border-radius: 5px;
    background: var(--fill); color: var(--ink-2); flex: none; white-space: nowrap; }
  .t2.acc { background: var(--ok-soft); color: var(--ok); }
  .t2.vio, .t2.blu { background: var(--info-soft); color: var(--info); }
  .t2.amb { background: var(--warn-soft); color: var(--warn); }
  .t2.red { background: var(--bad-soft); color: var(--bad); }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { font-family: var(--mono); font-size: 11px; padding: 3px 8px; border-radius: 6px; background: var(--fill); color: var(--ink-2); }
  .chip.cur { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
  .chip.more { color: var(--ink-3); }
  .chip.clk { cursor: pointer; transition: background var(--t), color var(--t); }
  .chip.clk:hover { background: var(--accent-soft); color: var(--accent); }
  .pill { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; color: var(--ink-2); background: var(--fill);
    border-radius: 6px; padding: 2px 7px; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .pill b { font-weight: 600; color: var(--ink); }
  .pill.acc { background: var(--ok-soft); color: var(--ok); } .pill.acc b { color: var(--ok); }
  .pill.amb { background: var(--warn-soft); color: var(--warn); } .pill.amb b { color: var(--warn); }
  .pill.red { background: var(--bad-soft); color: var(--bad); } .pill.red b { color: var(--bad); }
  .pill.mono { font-family: var(--mono); }
  .b { font: inherit; font-size: 12.5px; font-weight: 500; padding: 7px 12px; border-radius: 7px; border: 0; cursor: pointer;
    white-space: nowrap; line-height: 1.2; background: var(--fill); color: var(--ink-2);
    transition: background var(--t), color var(--t); }
  .b:hover { background: var(--fill-2); color: var(--ink); }
  .b.pri { background: var(--accent); color: var(--accent-ink); font-weight: 600; }
  .b.pri:hover { filter: brightness(1.07); background: var(--accent); color: var(--accent-ink); }
  .b.gho { background: transparent; }
  .b.gho:hover { background: var(--fill); color: var(--ink); }
  .b.dan:hover { color: var(--bad); background: var(--bad-soft); }
  .b.dan.pri, .b.armed { color: #fff; background: var(--bad); }
  .b:disabled { opacity: .5; cursor: default; filter: none; }
  .b.on { background: var(--accent-soft); color: var(--accent); }
  .btn { font: inherit; font-size: 12.5px; font-weight: 600; padding: 7px 14px; border: 0; border-radius: 7px; background: var(--accent);
    color: var(--accent-ink); cursor: pointer; white-space: nowrap; }
  .btn:hover { filter: brightness(1.07); }
  .btn:disabled { opacity: .5; cursor: default; }
  .btn.big { padding: 9px 16px; font-size: 13px; text-decoration: none; display: inline-block; }
  .a-link { color: var(--accent); font-size: 12.5px; }
  .a-link:hover { text-decoration: underline; }
  .guide-link { font: inherit; font-size: 12px; color: var(--accent); cursor: pointer; background: none; border: 0; padding: 0; text-align: left; }
  .guide-link:hover { text-decoration: underline; }
  .actions { display: flex; gap: 14px; }
  .actions button { background: none; border: 0; padding: 0; font: inherit; font-size: 12px; cursor: pointer; color: var(--ink-2); }
  .actions button:hover { color: var(--ink); }
  .actions button.danger:hover { color: var(--bad); }

  /* inputs — filled, no line */
  input.in, textarea.in, select.in { font: inherit; font-size: 12.5px; padding: 8px 10px; border: 0; border-radius: 7px;
    background: var(--fill); color: var(--ink); min-width: 0; flex: 1; transition: background var(--t); }
  input.in[type=password], .mono-in { font-family: var(--mono); }
  input.in:hover, textarea.in:hover, select.in:hover { background: var(--fill-2); }
  input.in:focus, textarea.in:focus, select.in:focus { outline: none; background: var(--fill); box-shadow: 0 0 0 2px var(--accent); }
  input.in::placeholder, textarea.in::placeholder { color: var(--ink-3); }
  select.in { cursor: pointer; flex: none; }
  input.in.search { width: 100%; margin-bottom: 12px; }
  .find { position: relative; margin-bottom: 12px; }
  .find input { width: 100%; font: inherit; font-size: 13px; padding: 10px 12px 10px 32px; border: 0; border-radius: 8px;
    background: var(--fill); color: var(--ink); transition: background var(--t); }
  .find input:hover { background: var(--fill-2); }
  .find input:focus { outline: none; background: var(--fill); box-shadow: 0 0 0 2px var(--accent); }
  .find::before { content: "⌕"; position: absolute; left: 11px; top: 50%; transform: translateY(-50%); color: var(--ink-3);
    font-size: 14px; pointer-events: none; }
  label.chk { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px; color: var(--ink-2); cursor: pointer; white-space: nowrap; }
  .enable { display: flex; gap: 8px; }
  .filters { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
  /* segmented control — pills, like the tabs */
  .fchips { display: flex; gap: 2px; flex: none; }
  .fchip { font: inherit; font-size: 12px; padding: 6px 12px; border: 0; border-radius: 6px; background: transparent;
    color: var(--ink-2); cursor: pointer; white-space: nowrap; transition: background var(--t), color var(--t); }
  .fchip:hover { background: var(--fill); color: var(--ink); }
  .fchip.on { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
  /* family tabs over the model list — the nav's pill treatment, with counts */
  .mtabs { display: flex; align-items: center; gap: 3px; flex-wrap: wrap; margin-bottom: 12px; }
  .mtabs .fchip { display: inline-flex; align-items: center; gap: 6px; font-weight: 500; padding: 6px 10px; }
  .mtabs .fchip.on { font-weight: 500; }
  .mtabs .tn2 { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
  .mtabs .fchip.on .tn2 { color: var(--accent); }
  /* provider toggle on the Deploy page — the same pills, with the real marks */
  .dp-ptoggle { display: flex; align-items: center; gap: 3px; margin-left: 6px; min-width: 0; overflow-x: auto;
    flex-wrap: nowrap; scrollbar-width: none; }
  .dp-ptoggle::-webkit-scrollbar { display: none; }
  .dp-ptoggle .fchip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 9px; flex: none; font-weight: 500; }
  .dp-ptoggle .fchip.on { font-weight: 600; }
  .dp-ptoggle .fchip.dim { opacity: .5; }
  .dp-ptoggle .fchip.dim:hover { opacity: 1; }
  .dp-ptoggle .mark2 { width: 16px; height: 16px; border-radius: 4px; background: none; }
  .dp-ptoggle .mark2 svg { width: 13px; height: 13px; }
  /* the company filter — one line of org pills, scrolls rather than wraps */
  .dp-orgs { display: flex; align-items: center; gap: 3px; margin-left: 6px; min-width: 0; overflow-x: auto;
    flex-wrap: nowrap; scrollbar-width: none; }
  .dp-orgs::-webkit-scrollbar { display: none; }
  .dp-orgs .fchip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 9px; flex: none; font-weight: 500;
    text-transform: capitalize; }
  .dp-orgs .fchip.on { font-weight: 600; }
  .dp-orgs .omark { width: 16px; height: 16px; border-radius: 4px; background: none; font-size: 9px; }
  .dp-orgs .omark svg { width: 13px; height: 13px; }
  .dp-orgs .tn2 { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); }
  .dp-orgs .fchip.on .tn2 { color: var(--accent); }
  .mcard .mwhen { font-size: 11px; color: var(--ink-3); }
  .mcard .mwhen.fresh { color: var(--accent); }
  .mcard .vr .vbar.fits i { background: var(--ok); }
  .mcard .vr .vbar.tight i { background: var(--warn); }
  .mcard .vr .vbar.no i { background: var(--ink-3); }
  /* skills — a library of cards, each with its own identity glyph */
  .sk-state { display: flex; gap: 6px; flex-wrap: wrap; margin: -8px 0 16px; }
  .sk-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .skcard { background: var(--panel); border-radius: var(--radius); padding: 14px 15px 12px; cursor: pointer;
    display: flex; flex-direction: column; gap: 9px; min-width: 0; transition: background var(--t); }
  .skcard:hover { background: var(--panel-2); }
  .sk-h { display: flex; align-items: center; gap: 11px; min-width: 0; }
  .sglyph { width: 36px; height: 36px; border-radius: 10px; flex: none; display: inline-flex; align-items: center;
    justify-content: center; overflow: hidden; }
  .sglyph svg { width: 25px; height: 25px; display: block; }
  .sk-t { min-width: 0; flex: 1; }
  .sk-n { font-weight: 600; font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sk-p { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sk-d { font-size: 12.5px; color: var(--ink-2); line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2;
    -webkit-box-orient: vertical; overflow: hidden; }
  .sk-tags { display: flex; gap: 5px; flex-wrap: wrap; }
  .sk-tools { display: flex; gap: 4px; flex-wrap: wrap; }
  .sk-tools .chip { font-size: 10.5px; padding: 2px 7px; }
  .sk-acts { display: flex; gap: 6px; margin-top: auto; padding-top: 4px; opacity: 0; transition: opacity var(--t); }
  .skcard:hover .sk-acts, .skcard:focus-within .sk-acts { opacity: 1; }
  .sk-acts .b { padding: 4px 10px; font-size: 11.5px; }
  .sk-body { margin: 14px 0 4px; font-size: 13px; line-height: 1.6; max-height: 40vh; overflow: auto; }
  .sk-raw { margin: 0; font-family: var(--mono); font-size: 11.5px; white-space: pre-wrap; word-break: break-word;
    color: var(--ink-2); max-height: 40vh; overflow: auto; }
  .sk-frow { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 12px; margin-bottom: 10px; }
  .sk-frow .dp-field input.in, .sk-frow .dp-field select.in { width: 100%; background: var(--panel-2); }
  .sk-err { color: var(--bad); font-size: 11.5px; margin: -6px 0 8px; }
  .sk-tsel { display: flex; gap: 4px; flex-wrap: wrap; }
  .sk-tsel .fchip { padding: 4px 9px; font-size: 11.5px; }
  .sk-split { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .sk-ed { width: 100%; min-height: 220px; resize: vertical; font-family: var(--mono); font-size: 12px; line-height: 1.55;
    background: var(--panel-2); }
  .sk-prev { background: var(--panel-2); border-radius: 8px; padding: 10px 12px; font-size: 12.5px; overflow: auto;
    min-height: 220px; max-height: 40vh; }
  @media (max-width: 1100px) { .sk-split, .sk-frow { grid-template-columns: 1fr; } }

  /* provider setup — one card per provider, its auth types as a toggle */
  .auth-glabel { font-size: 12.5px; font-weight: 600; color: var(--ink-2); margin: 18px 0 8px; }
  /* start-aligned so a card that opens a form never stretches its neighbours */
  .auth-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 10px; align-items: start; }
  .acard { background: var(--panel); border-radius: var(--radius); padding: 12px 13px 11px; display: flex;
    flex-direction: column; gap: 7px; min-width: 0; min-height: 160px; transition: background var(--t); }
  .acard:hover { background: var(--panel-2); }
  .acard.on { background: var(--accent-soft); }
  .acard.on:hover { background: var(--accent-soft-2); }
  .ac-h { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .ac-h .bigmark { width: 32px; height: 32px; border-radius: 9px; }
  .ac-h .bigmark svg { width: 18px; height: 18px; }
  .ac-h .ft { min-width: 0; flex: 1; }
  .ac-top { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .ac-h .fn { font-weight: 600; font-size: 13.5px; line-height: 1.25; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; flex: 1; min-width: 0; }
  .acard.on .ac-h .fn { color: var(--accent); }
  .ac-h .fd { font-size: 10.5px; line-height: 1.35; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; background: none; padding: 0; }
  /* the connection state is a chip on the name row, not a row of its own */
  .ac-s { display: inline-flex; align-items: center; gap: 5px; flex: none; font-size: 10.5px; font-weight: 600;
    color: var(--ink-3); background: var(--fill); border-radius: 5px; padding: 2px 7px; white-space: nowrap; }
  .ac-s.ok { background: var(--ok-soft); color: var(--ok); }
  .ac-s.warn { background: var(--warn-soft); color: var(--warn); }
  .acard.on .ac-s.ok { background: var(--panel); }
  /* the toggle is one control: equal pills, at most two rows */
  .ac-types { display: flex; gap: 4px; flex-wrap: wrap; margin-top: 1px; }
  /* flex: none — a pill must never shrink its label to a sliver */
  .ac-types .fchip { display: inline-flex; align-items: center; gap: 6px; height: 26px; padding: 0 10px;
    font-size: 12px; line-height: 1; flex: none; white-space: nowrap; max-width: 100%; }
  /* NB: not ".live" — that class is the 6px status dot, and its width would
     collapse the button to a sliver */
  .ac-types .fchip.ac-live { background: var(--accent); color: var(--accent-ink); font-weight: 600; }
  .ac-types .fchip.ac-live:hover { background: var(--accent); filter: brightness(1.06); }
  .acard.on .ac-types .fchip.on:not(.ac-live) { background: var(--panel); color: var(--accent); }
  .ac-tick { font-size: 10px; line-height: 1; }
  .ac-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ink-3); flex: none; }
  .ac-dot.cfg { background: var(--warn); }
  .ac-one { font-size: 11px; color: var(--ink-3); margin-top: 1px; }
  .ac-d { font-size: 11.5px; line-height: 1.4; color: var(--ink-3); margin-bottom: 5px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .ac-models { margin-top: 9px; }
  .ac-mh { font-size: 10px; color: var(--ink-3); margin-bottom: 4px; }
  .ac-models .chip { font-size: 10.5px; padding: 2px 7px; }
  /* two rows of models, then +N */
  /* one row by default; +N opens the rest */
  .ac-models .chips.clamp { max-height: 21px; overflow: hidden; }
  .ac-models .chip.more { cursor: pointer; border: 0; font: inherit; font-family: var(--mono); font-size: 10.5px;
    color: var(--ink-3); background: var(--fill); border-radius: 6px; padding: 2px 7px; margin-top: 4px; }
  .ac-models .chip.more:hover { color: var(--accent); }
  .acard.on .ac-models .chip { background: var(--panel); }
  .ap-form { margin-top: 6px; }
  .ap-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 7px 10px; margin-bottom: 7px; }
  .ap-fields .dp-field { gap: 3px; }
  .ap-fields .kh-l { font-size: 9.5px; }
  .ap-fields input.in { padding: 6px 9px; }
  .ap-fields .dp-field input.in { background: var(--panel); width: 100%; }
  .ap-fields .kh-n { font-size: 11px; line-height: 1.4; }
  .ap-fields .envn { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); }
  .ap-note { font-size: 11.5px; color: var(--ink-2); background: var(--panel); border-radius: 7px; padding: 6px 9px; margin-bottom: 7px; }
  /* one action row: Save is the only filled button, the rest are quiet */
  .ap-acts { display: flex; align-items: center; gap: 6px; flex-wrap: nowrap; }
  .ap-acts .b { padding: 6px 11px; font-size: 12px; }
  .ap-acts .b.gho { background: transparent; color: var(--ink-3); }
  .ap-acts .b.gho:hover { background: var(--fill); color: var(--ink); }
  .ac-doc { margin-left: auto; color: var(--ink-3); font-size: 13px; text-decoration: none; padding: 0 2px; }
  .ac-doc:hover { color: var(--accent); }
  .oauth-paste { display: none; gap: 8px; margin-top: 10px; max-width: 560px; }
  .oauth-paste.on { display: flex; }
  .oauth-paste input.in { background: var(--panel); }

  /* the gated-model notice and its token field */
  .hf-state { display: inline-flex; align-items: center; gap: 6px; margin-left: auto; font-size: 11.5px; color: var(--ink-3); white-space: nowrap; }
  .hf-add { font: inherit; font-size: 11.5px; color: var(--accent); background: none; border: 0; padding: 0 0 0 2px; cursor: pointer; }
  .hf-add:hover { text-decoration: underline; }
  .hf-notice { display: none; background: var(--warn-soft); border-radius: var(--radius); padding: 14px 16px; margin-bottom: 12px; }
  .hf-notice.on { display: block; }
  .hf-notice .hn-h { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
  .hf-notice .hn-h b { font-size: 13.5px; font-weight: 600; color: var(--ink); }
  .hf-notice .hn-b { font-size: 12.5px; color: var(--ink-2); margin: 6px 0 10px; line-height: 1.5; max-width: 78ch; }
  .hf-notice .hn-l { margin-bottom: 10px; }
  .hf-notice .hn-l .b { text-decoration: none; }
  .hf-form .hf-row { display: flex; gap: 8px; align-items: center; max-width: 460px; }
  .hf-form .hf-row input.in { background: var(--panel); }
  .hf-form .hf-foot { margin-top: 6px; font-size: 11.5px; }

  /* empty states & skeletons */
  .zero { background: var(--panel-2); border-radius: var(--radius); padding: 30px 22px 32px; text-align: center; }
  .zero .zart { color: var(--ink-2); margin: 0 auto 12px; width: 140px; }
  .zero .zart svg { width: 140px; height: 100px; display: block; }
  .zero .zt { font-weight: 600; font-size: 14px; margin-bottom: 4px; }
  .zero .zd { font-size: 12.5px; color: var(--ink-3); max-width: 54ch; margin: 0 auto; line-height: 1.55; }
  .zero .zact { margin-top: 14px; display: flex; justify-content: center; gap: 8px; }
  .empty { color: var(--ink-3); padding: 36px 20px; text-align: center; font-size: 13px; }
  .sk { border-radius: 8px; background: linear-gradient(90deg, var(--panel) 25%, var(--panel-2) 50%, var(--panel) 75%);
    background-size: 200% 100%; animation: shimmer 1.2s linear infinite; height: 14px; margin: 8px 0; }
  .sk.card { height: 84px; margin: 0; }
  .skgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; margin-top: 12px; }
  @keyframes shimmer { from { background-position: 200% 0; } to { background-position: -200% 0; } }

  /* key/value detail, code boxes, probes, banners */
  .kvs { display: grid; grid-template-columns: 104px 1fr; gap: 4px 14px; align-items: baseline; font-size: 12.5px; margin: 10px 0 0; }
  .kvs dt { color: var(--ink-3); font-size: 10.5px; text-transform: uppercase; letter-spacing: .04em; font-weight: 600; padding-top: 2px; }
  .kvs dd { margin: 0; font-family: var(--mono); font-size: 12px; word-break: break-word; }
  .kvs dd.wrap { white-space: pre-wrap; font-family: var(--sans); }
  .secret { color: var(--ink-3); letter-spacing: .12em; }
  .jsonbox { margin-top: 12px; }
  .jsonbox pre { margin: 0; background: var(--panel-2); border-radius: 8px; padding: 11px 12px; font-family: var(--mono);
    font-size: 11.5px; line-height: 1.55; overflow: auto; max-height: 320px; white-space: pre; }
  .jsonbox .jh { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
  .jsonbox .jt { font-size: 10.5px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: var(--ink-3); }
  .probe { margin-top: 12px; border-radius: 8px; padding: 11px 13px; font-size: 12.5px; background: var(--panel-2); }
  .probe.ok { background: var(--ok-soft); }
  .probe.bad { background: var(--bad-soft); }
  .probe .ph2 { display: flex; align-items: center; gap: 8px; font-weight: 600; }
  .probe .pe, .pe { font-family: var(--mono); font-size: 11.5px; color: var(--bad); margin-top: 6px; word-break: break-word; }
  .toolgrid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .toolgrid .tk { font-family: var(--mono); font-size: 11px; background: var(--fill); color: var(--ink-2); padding: 3px 8px; border-radius: 6px; }
  .banner { display: flex; align-items: center; gap: 12px; padding: 11px 14px; border-radius: 9px; margin-bottom: 16px; font-size: 13px;
    background: var(--warn-soft); color: var(--warn); }
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
  .mark2 { width: 22px; height: 22px; border-radius: 6px; flex: none; display: inline-flex; align-items: center; justify-content: center;
    background: var(--fill); color: var(--ink); font-family: var(--mono); font-size: 11px; font-weight: 700; overflow: hidden; }
  .mark2 svg { width: 14px; height: 14px; display: block; }
  .refresh { display: inline-flex; align-items: center; gap: 6px; font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); }

  /* ---- modal sheet ---- */
  #modal { position: fixed; inset: 0; background: var(--dim); display: none; align-items: center; justify-content: center;
    padding: 20px; z-index: 30; backdrop-filter: blur(3px); }
  #modal.on { display: flex; animation: fade 140ms ease-out; }
  @keyframes fade { from { opacity: 0; } }
  .sheet { position: relative; background: var(--panel); border-radius: 16px; max-width: 560px; width: 100%; max-height: 86vh;
    overflow-y: auto; padding: 22px 24px; animation: rise .16s ease-out; outline: none; }
  /* the pairing: model → provider, both with their real marks */
  .pair { display: grid; grid-template-columns: minmax(0,1fr) auto minmax(0,1fr); gap: 12px; align-items: center; margin: 2px 0 16px; }
  .pside { display: flex; align-items: center; gap: 10px; min-width: 0; }
  .pside .omark, .pside .bigmark { width: 40px; height: 40px; border-radius: 11px; font-size: 15px; }
  .pside .omark svg, .pside .bigmark svg { width: 22px; height: 22px; }
  .pside .pt { min-width: 0; }
  .pside .pn { font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pside .pn.mono { font-family: var(--mono); background: none; padding: 0; color: var(--ink); font-size: 13.5px; }
  .pside .pc { font-size: 11.5px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .parrow { color: var(--ink-3); font-size: 11px; text-align: center; display: flex; flex-direction: column; align-items: center; gap: 1px; }
  .parrow b { color: var(--accent); font-size: 18px; line-height: 1; font-weight: 400; }
  .pair.sm { margin: 0; gap: 8px; grid-template-columns: auto auto auto; justify-content: start; }
  .pair.sm .omark, .pair.sm .bigmark { width: 20px; height: 20px; border-radius: 5px; font-size: 10px; }
  .pair.sm .omark svg, .pair.sm .bigmark svg { width: 12px; height: 12px; }
  .pair.sm .parrow b { font-size: 11px; }
  /* spec tiles */
  .spec-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr)); gap: 8px; margin: 0 0 14px; }
  .tile { background: var(--panel-2); border-radius: 9px; padding: 9px 11px; min-width: 0; }
  .tile .tl { font-size: 10.5px; color: var(--ink-3); text-transform: uppercase; letter-spacing: .04em; font-weight: 600; }
  .tile .tv { font-family: var(--mono); font-size: 13px; font-weight: 600; margin-top: 2px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; display: flex; align-items: center; gap: 6px; }
  .tile .tv .pill { font-family: var(--sans); font-weight: 500; }
  .tile .th2 { font-size: 11px; color: var(--ink-3); margin-top: 1px; }
  /* cost — the headline number */
  .cost-big { display: flex; align-items: baseline; gap: 8px; margin: 2px 0 2px; }
  .cost-big b { font-family: var(--mono); font-size: 26px; font-weight: 700; letter-spacing: -.03em; color: var(--ink); }
  .cost-big span { font-size: 12.5px; color: var(--ink-2); }
  .cost-sub { font-size: 12px; color: var(--ink-3); margin-bottom: 14px; display: flex; gap: 12px; flex-wrap: wrap; }
  .cost-sub .warnline { color: var(--warn); }
  .sheet details.dp-adv { background: var(--panel-2); border-radius: 9px; padding: 8px 12px; margin: 0 0 14px; }
  .sheet details.dp-adv .dp-advgrid input.in, .sheet details.dp-adv .dp-advgrid select.in { background: var(--panel); }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .sheet.wide { max-width: 800px; }
  .sheet:focus-visible { box-shadow: none; }
  .sheet h3 { margin: 0 0 3px; font-size: 16px; font-weight: 600; letter-spacing: -.01em; }
  .sheet h4 { margin: 16px 0 6px; font-size: 10.5px; text-transform: uppercase; letter-spacing: .06em; color: var(--ink-3); }
  .sheet .sub { color: var(--ink-3); font-size: 12px; font-family: var(--mono); margin-bottom: 14px; word-break: break-all; }
  .sheet ol { margin: 0; padding-left: 20px; }
  .sheet ol li { margin: 7px 0; font-size: 13px; line-height: 1.5; }
  .sheet .free { font-size: 12.5px; color: var(--ink-2); background: var(--panel-2); border-radius: 8px; padding: 10px 12px; margin: 14px 0; }
  .sheet .cta { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 16px; }
  .sheet .notes { list-style: none; padding: 0; margin: 12px 0 0; }
  .sheet .notes li { font-size: 12.5px; color: var(--ink-2); padding: 4px 0 4px 16px; position: relative; }
  .sheet .notes li::before { content: "·"; position: absolute; left: 4px; color: var(--accent); }
  .rt, .plat { background: var(--panel-2); border-radius: 8px; padding: 11px 13px; margin: 8px 0; }
  .rt .rn, .plat-n { font-weight: 600; font-size: 13px; }
  .rt .rnote, .rnote { color: var(--ink-3); font-size: 12px; margin: 2px 0; }
  .rt code { display: block; font-family: var(--mono); font-size: 11.5px; background: var(--fill); padding: 7px 9px; border-radius: 6px;
    overflow-x: auto; white-space: pre; margin: 6px 0 2px; }
  .skill-box { background: var(--accent-soft); border-radius: 9px; padding: 13px 15px; margin: 16px 0; }
  .skill-t { font-weight: 600; font-size: 13px; margin-bottom: 4px; }
  .skill-b { font-size: 12.5px; color: var(--ink-2); line-height: 1.5; margin-bottom: 10px; }
  .plat-top { display: flex; align-items: center; gap: 9px; }
  .plat-k { font-size: 10px; color: var(--ink-3); background: var(--fill); padding: 2px 8px; border-radius: 6px; }
  .plat-links { display: flex; gap: 16px; margin-top: 8px; }
  .sheet .x { position: absolute; top: 12px; right: 14px; background: none; border: 0; font-size: 18px; color: var(--ink-3);
    cursor: pointer; line-height: 1; }
  .sheet .x:hover { color: var(--ink); }
  #toast { position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%) translateY(6px); background: var(--ink); color: var(--bg);
    padding: 10px 16px; border-radius: 8px; font-size: 13px; opacity: 0; transition: opacity var(--t), transform var(--t);
    pointer-events: none; z-index: 40; max-width: 80vw; }
  #toast.on { opacity: 1; transform: translateX(-50%); }
  #toast.err { background: var(--bad); color: #fff; }

  /* ---- command palette (⌘K) ---- */
  #palette { position: fixed; inset: 0; background: var(--dim); display: none; align-items: flex-start; justify-content: center;
    padding: 12vh 16px 0; z-index: 35; backdrop-filter: blur(3px); }
  #palette.on { display: flex; animation: fade 140ms ease-out; }
  .pal { width: 100%; max-width: 620px; background: var(--panel); border-radius: 14px; overflow: hidden; animation: rise .14s ease-out; }
  .pal input { width: 100%; font: inherit; font-size: 15px; padding: 14px 16px; border: 0; background: var(--panel-2); color: var(--ink); }
  .pal input:focus { outline: none; box-shadow: none; }
  .pal-list { max-height: 52vh; overflow-y: auto; padding: 6px; }
  .pal-g { font-size: 11.5px; font-weight: 600; color: var(--ink-3); padding: 8px 10px 4px; }
  .pal-i { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 7px; cursor: pointer; font-size: 13px; }
  .pal-i.on, .pal-i:hover { background: var(--accent-soft); }
  .pal-i .pk { font-family: var(--mono); font-size: 10px; color: var(--ink-3); background: var(--fill); padding: 1px 6px; border-radius: 4px; flex: none; }
  .pal-i .pt { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pal-i .ps { color: var(--ink-3); font-size: 11.5px; margin-left: auto; white-space: nowrap; font-family: var(--mono); }
  .pal-f { display: flex; gap: 14px; padding: 8px 14px; background: var(--panel-2); font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); }
  .pal-f b { color: var(--ink-2); background: var(--fill); padding: 0 5px; border-radius: 4px; font-weight: 600; }
  .pal-none { padding: 22px; text-align: center; color: var(--ink-3); font-size: 12.5px; }

  /* ==========================================================================
     SESSIONS — three columns; projects and sessions are cards you can scan.
     ========================================================================== */
  #sessions.on { display: grid; grid-template-columns: 280px 320px minmax(0,1fr); height: 100%; }
  /* the extra width goes to the transcript, not the two card columns */
  #transcript { max-width: 1080px; }
  .col { overflow-y: auto; height: 100%; min-width: 0; background: var(--bg); }
  .col-head { position: sticky; top: 0; z-index: 2; background: var(--bg); padding: 14px 14px 8px; font-size: 13px; font-weight: 600;
    color: var(--ink-2); }
  .colfind { padding: 0 12px 8px; position: sticky; top: 34px; background: var(--bg); z-index: 2; }
  .colfind input { width: 100%; font: inherit; font-size: 12.5px; padding: 8px 10px; border: 0; border-radius: 7px;
    background: var(--fill); color: var(--ink); }
  .colfind input:focus { outline: none; box-shadow: 0 0 0 2px var(--accent); }
  .cards { display: flex; flex-direction: column; gap: 8px; padding: 0 12px 16px; }
  .pcard { background: var(--panel); border-radius: var(--radius); padding: 12px 13px; cursor: pointer; transition: background var(--t); min-width: 0; }
  .pcard:hover { background: var(--panel-2); }
  .pcard.on { background: var(--accent-soft); }
  .pcard.on .t { color: var(--accent); }
  .pcard .t { font-weight: 600; font-size: 13px; line-height: 1.35; overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
    -webkit-line-clamp: 2; -webkit-box-orient: vertical; word-break: break-word; }
  .pcard .s { color: var(--ink-3); font-size: 11px; margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); }
  .pcard .s.sans { font-family: var(--sans); color: var(--ink-2); font-size: 12px; }
  .pcard .m { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
  .pcard.on .pill { background: var(--panel); }
  .row { display: none; }

  /* transcript */
  #transcript { padding: 20px 26px 60px; margin: 0 auto; }
  .conv-head { margin-bottom: 14px; }
  .conv-head h2 { font-size: 16px; font-weight: 600; margin: 0 0 3px; letter-spacing: -.01em; }
  .conv-head .sub { color: var(--ink-3); font-size: 12px; font-family: var(--mono); }
  .msg { display: grid; grid-template-columns: 84px minmax(0,1fr); gap: 8px 12px; padding: 12px 12px; border-radius: var(--r-sm);
    margin: 0 -12px; transition: background var(--t); }
  .msg:hover { background: var(--panel); }
  .who { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; font-size: 11px; color: var(--ink-3); }
  .rc { font-size: 10px; font-weight: 700; letter-spacing: .05em; text-transform: uppercase; padding: 2px 7px; border-radius: 5px;
    background: var(--fill); color: var(--ink-2); }
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
  .md pre { margin: 6px 0 8px; background: var(--panel-2); border-radius: 8px; padding: 10px 12px; overflow-x: auto; }
  .md pre code { background: none; padding: 0; font-size: 12px; line-height: 1.5; white-space: pre; }
  .md blockquote { margin: 4px 0 8px; padding: 6px 12px; background: var(--panel-2); border-radius: 6px; color: var(--ink-2); }
  .thinking { background: var(--panel-2); border-radius: 8px; padding: 8px 12px; color: var(--ink-2); font-style: italic;
    white-space: pre-wrap; margin: 6px 0; font-size: 12.5px; }
  .ctxtog { font: inherit; font-size: 11px; color: var(--ink-3); background: var(--fill); border: 0; border-radius: 5px; padding: 2px 8px;
    cursor: pointer; margin: 4px 0; }
  .ctxtog:hover { color: var(--ink); background: var(--fill-2); }
  .ctxbody { display: none; margin: 6px 0 4px; }
  .ctxbody.on { display: block; }
  .ctxbody pre { margin: 0; font-family: var(--mono); font-size: 11.5px; white-space: pre-wrap; word-break: break-word; color: var(--ink-3);
    background: var(--panel-2); border-radius: 8px; padding: 10px 12px; max-height: 300px; overflow: auto; }
  .tcall { border-radius: 8px; margin: 6px 0; overflow: hidden; background: var(--panel-2); }
  .tcall .th { display: flex; align-items: center; gap: 8px; padding: 7px 11px; font-family: var(--mono); font-size: 12px; cursor: pointer;
    user-select: none; color: var(--ink-2); transition: background var(--t); }
  .tcall .th:hover { background: var(--fill); }
  .tcall .th .tn { color: var(--ink); font-weight: 600; }
  .tcall .th .ta { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .tcall .th .sz { margin-left: auto; color: var(--ink-3); font-size: 11px; white-space: nowrap; }
  .tcall .th .tg { font-size: 9px; color: var(--ink-3); transition: transform var(--t); }
  .tcall.open .th .tg { transform: rotate(90deg); }
  .tcall.err .th .tn { color: var(--bad); }
  .tcall .tb2 { display: none; }
  .tcall.open .tb2 { display: block; }
  .tcall .tl2 { font-family: var(--mono); font-size: 10px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); padding: 7px 11px 0; }
  .tcall pre { margin: 0; padding: 6px 11px 10px; overflow-x: auto; font-family: var(--mono); font-size: 11.5px; white-space: pre-wrap;
    word-break: break-word; max-height: 340px; color: var(--ink-2); }
  .tcall.err pre.res { color: var(--bad); }
  .block { border-radius: 8px; margin: 6px 0; background: var(--panel-2); overflow: hidden; }
  .block .bh { padding: 6px 11px; font-family: var(--mono); font-size: 12px; display: flex; gap: 8px; align-items: center; }
  .block pre { margin: 0; padding: 8px 11px; overflow-x: auto; font-family: var(--mono); font-size: 11.5px; white-space: pre-wrap;
    word-break: break-word; max-height: 340px; }
  .badge { font-size: 10px; padding: 1px 6px; border-radius: 5px; background: var(--fill); color: var(--ink-3); font-family: var(--mono); }
  .compact { border-radius: 8px; background: var(--warn-soft); color: var(--warn); padding: 8px 12px; font-size: 12px; font-family: var(--mono); }
  .conv-stats { margin: 0 0 12px; }
  .ctxbox { padding: 12px 14px 8px; margin-bottom: 16px; }
  .ctxbox .ch { display: flex; align-items: baseline; gap: 10px; font-size: 12.5px; color: var(--ink-2); font-weight: 600; }
  .ctxbox .ch span:last-child { margin-left: auto; font-weight: 400; font-family: var(--mono); font-size: 11px; color: var(--ink-3); }
  .ctxsvg { width: 100%; height: auto; display: block; margin-top: 6px; }
  .ctxsvg .bar { fill: var(--accent); opacity: .55; transition: opacity var(--t); }
  .ctxsvg .bar:hover, .ctxsvg .bar.on { opacity: 1; }
  .ctxsvg .cap { stroke: var(--bad); stroke-width: 1; stroke-dasharray: 3 3; }
  .ctxsvg .cost { fill: none; stroke: var(--info); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
  .ctxsvg text { fill: var(--ink-3); font-family: var(--mono); font-size: 9px; }

  /* ==========================================================================
     OVERVIEW — readouts, the trace, the punchcard, the spectrum, the ledgers.
     ========================================================================== */
  .lcd { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 22px; font-size: 12px; color: var(--ink-3); margin: 0 0 18px; }
  .lcd i { font-style: normal; color: var(--ink); font-weight: 600; font-size: 15px; letter-spacing: -.02em; font-variant-numeric: tabular-nums;
    margin-right: 5px; font-family: var(--mono); }
  .lcd .hot i { color: var(--accent); }
  .lcd.tight { margin: 4px 0 10px; gap: 0 18px; }
  .lcd .dim i { color: var(--ink-2); }
  .trace { position: relative; padding: 14px 16px 8px; margin-bottom: 12px; }
  .trace-h { display: flex; align-items: baseline; gap: 12px; margin-bottom: 4px; }
  .trace-t { font-size: 13px; font-weight: 600; color: var(--ink-2); }
  .trace-pk { margin-left: auto; font-family: var(--mono); font-size: 11.5px; color: var(--ink-2); }
  .trace-pk b { color: var(--ink); }
  .trace svg { display: block; width: 100%; height: auto; }
  .trace .env { fill: var(--accent); opacity: .1; }
  .trace .sig { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
  .trace .raw { fill: none; stroke: var(--accent); stroke-width: 1; opacity: .3; vector-effect: non-scaling-stroke; }
  .trace .pk { stroke: var(--ink-3); stroke-width: 1; stroke-dasharray: 2 3; }
  .trace .pkd { fill: var(--accent); }
  .trace .base { stroke: var(--fill-2); stroke-width: 1; }
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
  .prow { position: relative; display: flex; align-items: center; gap: 10px; padding: 9px 12px; border-radius: 8px; font-size: 12.5px;
    overflow: hidden; background: var(--panel-2); }
  .prow .fillbar { position: absolute; left: 0; top: 0; bottom: 0; background: var(--accent); opacity: .1; }
  .prow .pn { position: relative; font-weight: 600; }
  .prow .pp { position: relative; color: var(--ink-3); font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); }
  .prow .pv { position: relative; margin-left: auto; color: var(--ink-2); white-space: nowrap; font-variant-numeric: tabular-nums;
    font-family: var(--mono); font-size: 11.5px; }
  .prow .pf { position: relative; } .prow .pf .mark2 { width: 18px; height: 18px; border-radius: 5px; }
  .prow .pf .mark2 svg { width: 11px; height: 11px; }
  .prow .t2 { position: relative; }
  .fam-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 10px; margin-bottom: 12px; }
  .fam { padding: 13px 14px 12px; display: flex; flex-direction: column; gap: 6px; min-width: 0; position: relative; cursor: pointer;
    transition: background var(--t); }
  .fam:hover { background: var(--panel-2); }
  .fam.cur:hover { background: var(--accent-soft-2); }
  .fam .fh { display: flex; align-items: center; gap: 8px; }
  .fam .mark2 { width: 26px; height: 26px; } .fam .mark2 svg { width: 16px; height: 16px; }
  .fam .fn { font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fam.cur .fn { color: var(--accent); }
  .fam .fa { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--ink-2); white-space: nowrap; overflow: hidden; }
  .fam .fm { font-family: var(--mono); font-size: 11.5px; color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fam .fm.none { color: var(--ink-3); font-style: italic; font-family: var(--sans); }
  .fam .ff { display: flex; align-items: center; gap: 6px; margin-top: auto; }
  .fam .ff .b { padding: 5px 10px; font-size: 11.5px; }
  .fam .ff .pr { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); display: inline-flex; align-items: center; gap: 5px;
    overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
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
  .bars .base { stroke: var(--fill-2); stroke-width: 1; }
  .leg { display: flex; gap: 14px; font-size: 11px; color: var(--ink-3); margin-bottom: 8px; flex-wrap: wrap; }
  .leg i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; vertical-align: -1px; margin-right: 5px; background: var(--accent); }
  .leg i.est { background: repeating-linear-gradient(135deg, var(--accent) 0 2px, transparent 2px 4px); }
  .run-ph { margin: 14px 0 6px; font-size: 12.5px; font-weight: 600; color: var(--ink-2); display: flex; gap: 10px; align-items: center; }
  .run-ag { background: var(--panel-2); border-radius: 8px; padding: 10px 12px; margin: 6px 0; font-size: 12.5px; }
  .run-ag .rh { display: flex; align-items: center; gap: 8px; font-size: 12px; }
  .run-ag .rh b { font-weight: 600; }
  .run-ag .rh .sp { flex: 1; }
  .run-ag .rs { color: var(--ink-2); margin-top: 5px; white-space: pre-wrap; word-break: break-word; max-height: 160px; overflow: auto; font-size: 12px; }
  .run-ag .re { color: var(--bad); font-family: var(--mono); font-size: 11.5px; margin-top: 5px; }
  .log { background: var(--panel-2); border-radius: 8px; padding: 10px 12px; font-family: var(--mono); font-size: 11px; line-height: 1.55;
    max-height: 220px; overflow: auto; white-space: pre-wrap; word-break: break-word; color: var(--ink-2); }

  /* ==========================================================================
     MODELS — a comparison table, local models, provider setup, self-host.
     ========================================================================== */
  .hero { padding: 18px 20px; margin-bottom: 20px; }
  .hero-t { font-size: 18px; font-weight: 600; letter-spacing: -.02em; margin: 0 0 4px; }
  .hero-s { font-size: 12.5px; color: var(--ink-2); line-height: 1.5; max-width: 62ch; }
  .hero-m { font-family: var(--mono); font-weight: 600; font-size: 14px; }
  .host { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 14px 16px; margin-bottom: 20px; }
  .host .kbadge { font-size: 10.5px; font-weight: 600; letter-spacing: .04em; text-transform: uppercase; padding: 3px 9px; border-radius: 6px; }
  .kbadge.selfhost, .kbadge.local { background: var(--info-soft); color: var(--info); }
  .kbadge.provider { background: var(--ok-soft); color: var(--ok); }
  .kbadge.default { background: var(--fill); color: var(--ink-2); }
  .host .hm { font-family: var(--mono); font-weight: 600; font-size: 14px; }
  .host .hb, .hero .hb { font-family: var(--mono); font-size: 12px; color: var(--ink-3); margin-left: auto; word-break: break-all; }
  .browse-head { display: flex; align-items: baseline; gap: 11px; margin-bottom: 10px; }
  .cnt2 { font-size: 11.5px; color: var(--ink-2); background: var(--fill); padding: 1px 8px; border-radius: 6px; }
  .browse { overflow: hidden auto; max-height: 340px; margin-bottom: 22px; }
  .brow { display: flex; align-items: center; gap: 12px; padding: 8px 12px; cursor: pointer; border-radius: 7px; margin: 1px 4px; }
  .brow:hover { background: var(--panel-2); }
  .brow.cur { background: var(--accent-soft); }
  .brow .bm { font-family: var(--mono); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .brow .bp { font-size: 11.5px; color: var(--ink-3); white-space: nowrap; }
  .brow .bx { font-size: 11px; color: var(--ink-3); white-space: nowrap; min-width: 62px; text-align: right; }
  .browse-empty { padding: 14px 15px; font-size: 12.5px; color: var(--ink-3); font-style: italic; }
  .selfhost-card { gap: 10px; margin-bottom: 20px; }
  .selfhost-card .fields, .selfhost .fields { display: flex; flex-direction: column; gap: 8px; }
  .selfhost-card .fields .r, .selfhost .fields .r { display: flex; gap: 8px; }
  .selfhost { border-radius: var(--radius); padding: 15px 17px; margin-bottom: 20px; background: var(--accent-soft); }
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
  .keyheld { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 12px; padding: 9px 12px; border-radius: 8px; background: var(--panel-2); }
  .kh-l { font-family: var(--mono); font-size: 10.5px; letter-spacing: .05em; text-transform: uppercase; color: var(--ink-3); }
  .kh-v { font-family: var(--mono); font-size: 13px; font-weight: 600; letter-spacing: .02em; }
  .kh-n { font-size: 11px; color: var(--ink-3); }
  .mtable { padding: 4px; max-height: 460px; overflow-y: auto; }
  .mrow { display: grid; grid-template-columns: minmax(0,1fr) 104px 52px 104px 118px 62px; align-items: center; gap: 12px; padding: 8px 11px;
    border-radius: 7px; cursor: pointer; font-size: 12.5px; transition: background var(--t); }
  .mrow:hover, .mrow.kb { background: var(--panel-2); }
  .mrow.kb { background: var(--fill); }
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
  .mfam { display: flex; align-items: center; gap: 9px; padding: 10px 11px 6px; font-size: 12.5px; font-weight: 600; color: var(--ink-2);
    position: sticky; top: 0; background: var(--panel); z-index: 1; }
  .mfam .mark2 { width: 18px; height: 18px; border-radius: 5px; } .mfam .mark2 svg { width: 11px; height: 11px; }
  .mfam .cnt3 { font-weight: 400; color: var(--ink-3); font-size: 11.5px; }
  .mhead { display: grid; grid-template-columns: minmax(0,1fr) 104px 52px 104px 118px 62px; gap: 12px; padding: 4px 11px 6px;
    font-family: var(--mono); font-size: 9.5px; letter-spacing: .08em; text-transform: uppercase; color: var(--ink-3); }
  .mhead span:nth-child(3), .mhead span:nth-child(4) { text-align: right; }
  .orow { display: grid; grid-template-columns: minmax(0,1fr) 90px 72px 92px 62px; gap: 12px; align-items: center; padding: 8px 11px;
    border-radius: 7px; font-size: 12.5px; cursor: pointer; transition: background var(--t); }
  .orow:hover { background: var(--panel-2); }
  .orow.cur { background: var(--accent-soft); }
  .orow .on2 { font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .orow .osz, .orow .opq { color: var(--ink-2); font-size: 11.5px; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; font-family: var(--mono); }
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
  .now { display: flex; gap: 10px; align-items: center; margin-bottom: 18px; padding: 12px 14px; border-radius: var(--radius); background: var(--panel); }
  .now .k { font-size: 11px; color: var(--ink-3); text-transform: uppercase; letter-spacing: .06em; }
  .now .v { font-family: var(--mono); font-size: 14px; font-weight: 600; }

  /* ==========================================================================
     DEPLOY — provider cards, model cards, GPU cards, the job sheet, the
     deployments rows. Verdict pills are the one new word: fits / tight / no.
     ========================================================================== */
  .dp-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; margin-bottom: 12px; }
  .dpc { padding: 16px 16px 14px; display: flex; flex-direction: column; gap: 10px; min-width: 0; position: relative; transition: background var(--t); }
  .dpc:hover { background: var(--panel-2); }
  .dpc.on { background: var(--accent-soft); }
  .dpc.on:hover { background: var(--accent-soft-2); }
  .dpc.blocked .fs.warn b { color: var(--warn); font-weight: 600; }
  .dpc.blocked .bigmark { opacity: .75; }
  .dpc .fh { display: flex; align-items: center; gap: 12px; }
  .dpc .bigmark { width: 40px; height: 40px; border-radius: 11px; flex: none; display: inline-flex; align-items: center; justify-content: center;
    background: var(--fill); color: var(--ink); font-family: var(--mono); font-weight: 700; font-size: 15px; overflow: hidden; }
  .dpc .bigmark svg { width: 22px; height: 22px; display: block; }
  .dpc .ft { min-width: 0; flex: 1; }
  .dpc .fn { font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dpc.on .fn { color: var(--accent); }
  .dpc .fd { font-size: 11.5px; color: var(--ink-3); line-height: 1.35; margin-top: 1px; }
  .dpc .fs { display: flex; align-items: center; gap: 8px; font-size: 12.5px; color: var(--ink-2); min-width: 0; }
  .dpc .fs .dot2 { width: 8px; height: 8px; }
  .dpc .fs b { font-weight: 600; color: var(--ink); }
  .dpc .fs .fsx { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dpc .fs.warn .fsx { color: var(--warn); }
  .dpc .chips { gap: 5px; }
  .dpc .chip { font-size: 10.5px; padding: 2px 7px; }
  .dpc.on .chip, .dpc.on .pill { background: var(--panel); }
  .dpc .ff { display: flex; align-items: center; gap: 6px; margin-top: auto; flex-wrap: wrap; }
  .dpc .ff .b { padding: 6px 11px; font-size: 12px; }
  .dpc .ff .b.gho { background: var(--fill); }
  .dpc.on .ff .b.gho { background: var(--panel); }
  .dpc .ff .lnk { margin-left: auto; font-size: 12px; color: var(--ink-3); }
  .dpc .ff .lnk:hover { color: var(--accent); }
  /* the credential sheet: fields first, guide collapsed, actions pinned */
  .cs-h { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .cs-h .bigmark { width: 40px; height: 40px; border-radius: 11px; }
  .cs-h .bigmark svg { width: 22px; height: 22px; }
  .cs-h .fn { font-weight: 600; font-size: 15px; }
  .cs-h .fd { font-size: 12px; color: var(--ink-3); margin-top: 1px; }
  .cs-fields { display: flex; flex-direction: column; gap: 12px; }
  .cs-fields .dp-field { display: flex; flex-direction: column; gap: 5px; }
  .cs-fields .dp-field input.in { width: 100%; background: var(--panel-2); }
  .cs-fields .kh-n { font-size: 11.5px; color: var(--ink-3); line-height: 1.45; }
  .cs-guide { margin-top: 16px; background: var(--panel-2); border-radius: 9px; padding: 4px 12px; }
  .cs-guide summary { font-size: 12.5px; color: var(--ink-2); cursor: pointer; padding: 8px 0; list-style: none; }
  .cs-guide summary::-webkit-details-marker { display: none; }
  .cs-guide summary::before { content: "▸ "; color: var(--ink-3); font-size: 10px; }
  .cs-guide[open] summary::before { content: "▾ "; }
  .cs-guide summary:hover { color: var(--ink); }
  .cs-gb { font-size: 12.5px; color: var(--ink-2); padding: 2px 0 12px; }
  .cs-gb .gi { color: var(--ink); margin-bottom: 6px; }
  .cs-gb ol { margin: 0 0 10px; padding-left: 18px; }
  .cs-gb li { margin: 3px 0; line-height: 1.45; }
  .cs-gb .gr { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .cs-gb .gr .b { text-decoration: none; }
  .cs-gb .gn { font-size: 11.5px; color: var(--ink-3); margin-top: 8px; }
  .cs-foot { display: flex; gap: 10px; align-items: center; position: sticky; bottom: -22px; margin: 18px -24px -22px;
    padding: 14px 24px; background: var(--panel); }

  .dp-field { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
  .dp-field .kh-l { font-size: 10px; }
  .dp-field input.in, .dp-field select.in { width: 100%; background: var(--panel-2); }
  .dpc.on .dp-field input.in { background: var(--panel); }
  .dp-field .kh-n { line-height: 1.45; }
  /* the Hub search — model cards */
  .dp-mgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; }
  .dp-glabel { grid-column: 1 / -1; font-size: 12.5px; font-weight: 600; color: var(--ink-2); padding: 4px 2px 0; }
  .dp-status { font-family: var(--mono); font-size: 11px; color: var(--ink-3); margin-left: auto; white-space: nowrap; }
  .mcard { padding: 14px 15px 13px; cursor: pointer; display: flex; flex-direction: column; gap: 9px; min-width: 0; position: relative;
    transition: background var(--t); }
  .mcard:hover { background: var(--panel-2); }
  .mcard.on { background: var(--accent-soft); }
  .mcard.on:hover { background: var(--accent-soft-2); }
  .mcard .mh { display: flex; align-items: center; gap: 10px; min-width: 0; padding-right: 70px; }
  .mcard .mh .mtt { min-width: 0; flex: 1; }
  .omark { width: 34px; height: 34px; border-radius: 9px; flex: none; display: inline-flex; align-items: center; justify-content: center;
    background: var(--fill); color: var(--ink-2); font-family: var(--mono); font-weight: 700; font-size: 14px; overflow: hidden; position: relative; }
  .omark svg { width: 20px; height: 20px; display: block; }
  .omark img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity var(--t); }
  .omark.img img { opacity: 1; }
  .omark.img { color: transparent; }
  .mcard.on .omark { background: var(--panel); }
  .mcard .mt { font-family: var(--mono); font-weight: 600; font-size: 13.5px; letter-spacing: -.01em; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; }
  .mcard.on .mt { color: var(--accent); }
  .mcard .mo { font-size: 11.5px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mcard .vr.pend { opacity: .55; }
  .mcard .mp2 { display: flex; flex-wrap: wrap; gap: 5px; }
  .mcard.on .pill { background: var(--panel); }
  .mcard .vr { display: flex; align-items: center; gap: 8px; font-size: 11px; color: var(--ink-3); font-family: var(--mono); }
  .mcard .vr .vbar { flex: 1; height: 5px; border-radius: 3px; background: var(--fill); overflow: hidden; }
  .mcard.on .vr .vbar { background: var(--panel); }
  .mcard .vr .vbar i { display: block; height: 100%; background: var(--accent); border-radius: 3px; }
  .mcard .vr b { color: var(--ink); font-weight: 600; white-space: nowrap; }
  .mcard .go { position: absolute; top: 13px; right: 14px; font-size: 11.5px; color: var(--accent); opacity: 0; transition: opacity var(--t); }
  .mcard:hover .go, .mcard.on .go { opacity: 1; }
  /* fit & deploy — one group per provider, GPUs as cards */
  .dp-mh { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; margin-bottom: 8px; }
  .dp-mid { font-family: var(--mono); font-weight: 600; font-size: 13.5px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .dp-eng { display: inline-flex; align-items: center; gap: 7px; font-size: 11px; color: var(--ink-3); font-family: var(--mono); }
  .dp-eng select.in { padding: 4px 8px; }
  .dp-fitbox { margin-top: 12px; }
  .dp-loading { color: var(--ink-3); font-family: var(--mono); font-size: 12.5px; }
  .dp-ggrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 10px; margin-top: 10px; }
  .gcard { background: var(--panel-2); padding: 13px 14px; display: grid; grid-template-columns: minmax(0,1fr) auto;
    grid-template-rows: auto auto; gap: 6px 12px; align-items: center; transition: background var(--t); }
  .gcard .gt { grid-column: 1; grid-row: 1; }
  .gcard .gm { grid-column: 1; grid-row: 2; }
  .gcard:hover { background: var(--fill); }
  .gcard.no { opacity: .6; }
  .gcard .gt { font-weight: 600; font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gcard .gt small { color: var(--ink-3); font-weight: 400; font-family: var(--mono); font-size: 11px; margin-left: 6px; }
  .gcard .gp { grid-column: 2; grid-row: 1; font-family: var(--mono); font-weight: 700; font-size: 17px; letter-spacing: -.02em; text-align: right; white-space: nowrap; }
  .gcard .gp small { font-size: 11px; font-weight: 400; color: var(--ink-3); }
  .gcard.no .gp { color: var(--ink-3); font-weight: 500; }
  .gcard .gm { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--ink-3); min-width: 0; overflow: hidden; }
  .gcard .gm .vd { flex: none; }
  .gcard .gm span:last-child { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gcard .ga { grid-column: 2; grid-row: 2; display: flex; justify-content: flex-end; }
  .gcard .ga .b { padding: 6px 12px; font-size: 12px; }
  .gcard .ga .mgo { font-size: 10.5px; color: var(--ink-3); text-align: right; }
  .vd { font-size: 10px; font-weight: 600; letter-spacing: .03em; text-transform: uppercase; padding: 2px 7px; border-radius: 5px;
    background: var(--fill); color: var(--ink-3); text-align: center; }
  .vd.fits { background: var(--ok-soft); color: var(--ok); }
  .vd.tight { background: var(--warn-soft); color: var(--warn); }
  .vd.no { background: var(--fill); color: var(--ink-3); }
  .dp-adv { margin: 6px 0 2px; }
  .dp-adv summary { font-family: var(--mono); font-size: 11px; color: var(--ink-3); cursor: pointer; padding: 3px 0; }
  .dp-adv summary:hover { color: var(--ink); }
  .dp-advgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 8px 12px; padding: 8px 0 4px; align-items: end; }
  .dp-advgrid input.in, .dp-advgrid select.in { width: 100%; padding: 5px 8px; background: var(--panel-2); }
  .dp-cost { font-family: var(--mono); font-size: 12.5px; color: var(--ink-2); background: var(--panel-2); border-radius: 8px; padding: 9px 12px; margin: 12px 0 4px; }
  .dp-cost b { color: var(--ink); }
  .dp-log { max-height: 260px; min-height: 80px; margin-top: 10px; }
  .sheet .banner { margin: 12px 0 0; }
  /* deployments — filled rows */
  .dp-rows { display: flex; flex-direction: column; gap: 4px; }
  .dp-drow { display: grid; align-items: center; gap: 10px; padding: 9px 12px; border-radius: 8px; font-size: 12px; background: var(--panel-2);
    grid-template-columns: 58px minmax(0,.9fr) minmax(0,1.2fr) 118px 116px minmax(0,1fr) 96px 56px auto; transition: background var(--t); }
  .dp-drow:hover { background: var(--fill); }
  .dp-drow.head { background: transparent; font-family: var(--mono); font-size: 9.5px; letter-spacing: .08em; text-transform: uppercase;
    color: var(--ink-3); padding: 4px 12px 2px; }
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
  .dp-drow .dp-dx .b { padding: 4px 9px; font-size: 11px; }
  .dp-drow .dp-dx .b.gho { background: transparent; }
  .dp-none { display: flex; align-items: center; gap: 8px; padding: 10px 14px; border-radius: var(--r-sm); background: var(--panel);
    font-size: 12.5px; color: var(--ink-2); }
  .dp-drow.flash { box-shadow: 0 0 0 2px var(--accent); }

  /* ---- responsive ---- */
  @media (max-width: 1100px) {
    .dp-drow { grid-template-columns: 58px minmax(0,1fr) 116px minmax(0,1fr) 56px auto; }
    .dp-drow .dp-dm, .dp-drow .dp-dg, .dp-drow .dp-dc { display: none; }
    #sessions.on { grid-template-columns: 240px 280px minmax(0,1fr); }
  }
  @media (max-width: 900px) {
    .railfoot { display: none; }
    .tb span { display: none; }
    .page { padding: 18px 14px 60px; }
    #sessions.on { grid-template-columns: 1fr; }
    .col { display: none; } .col.mobile-on { display: block; }
    .mrow, .mhead { grid-template-columns: minmax(0,1fr) 52px 96px 62px; }
    .mrow .mp, .mrow .mcaps, .mhead span:nth-child(2), .mhead span:nth-child(5) { display: none; }
    .orow { grid-template-columns: minmax(0,1fr) 72px 62px; }
    .orow .osz, .orow .opq { display: none; }
    #transcript { padding: 14px 12px; }
    .msg { grid-template-columns: 1fr; gap: 4px; margin: 0; }
    .who { flex-direction: row; align-items: center; gap: 8px; }
  }
</style>
</head>
<body>
<header id="top">
  <div class="brand"><img src="/mantis.svg" alt=""> <span>mantis</span></div>
  <nav id="nav">
    <button data-v="home" class="on">Overview</button>
    <button data-v="models">My models</button>
    <button data-v="sessions">Sessions</button>
    <button data-v="activity">Activity</button>
    <button data-v="deploy">Deploy</button>
    <button data-v="mcp">MCP</button>
    <button data-v="skills">Skills</button>
    <button data-v="config">Config</button>
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
  <section id="activity" class="view"><div class="scroll"><div class="page wide" id="activitypad"></div></div></section>
  <section id="models" class="view"><div class="scroll"><div class="page" id="modelspad"></div></div></section>
  <section id="deploy" class="view"><div class="scroll"><div class="page wide" id="deploypad"></div></div></section>
  <section id="config" class="view"><div class="scroll"><div class="page" id="configpad"></div></div></section>
</main>
<div id="modal"><div class="sheet"><button class="x" onclick="hideModal()">✕</button><div id="sheet"></div></div></div>
<div id="palette"><div class="pal"><input id="palin" placeholder="Jump to a page, project, session, deployment — or run an action…" autocomplete="off">
  <div class="pal-list" id="pallist"></div>
  <div class="pal-f"><span><b>↑↓</b> move</span><span><b>↵</b> open</span><span><b>esc</b> close</span><span><b>g</b> <b>o</b>/<b>s</b>/<b>a</b>/<b>m</b>/<b>d</b> pages</span><span><b>/</b> search</span></div></div></div>
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
// Switch the current model. Passing the provider's base_url as backend keeps
// routing correct for a cross-provider pick. Takes effect on the next launch.
async function useModel(model, backend) {
  try {
    const r = await post("/api/use", { model, backend: backend || "" });
    if (r.ok) { toast("current model → " + (r.model || model)); loadOverview(); loadModels(); }
    else toast(r.error || "failed", true);
  } catch (e) { toast(e.message, true); }
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
    card.onclick = () => unlockFamily(f.id);
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
  h.append(el("h3", null, "Spend · last " + win + " days"));
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
    ? "Sessions <b>estimated</b> at <b>" + esc(pr.model) + "</b> rates · runs recorded"
    : "Sessions <b>estimated</b>" + (pr.model ? " · no price row for <b>" + esc(pr.model) + "</b>" : " · pick a model to price them");
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
    const pt = el("div","note2"); pt.style.margin = "10px 0 6px"; pt.textContent = "By provider · last 30 days";
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
function activityRows(act) {
  const rows = [];
  (act.runs || []).forEach(r => { const u = r.usage || {}; rows.push({
    id: "run:" + r.run_id, kind: "workflow", ts: r.saved_at, status: r.status, active: !!r.active,
    desc: r.name || r.definition || r.run_id, extra: (u.agents_done||0) + "/" + (u.agents||0) + " agents", agents: u.agents,
    elapsed: u.elapsed_s, tokens: u.tokens, usd: u.usd, open: () => openRun(r.run_id) }); });
  (act.jobs || []).forEach(j => rows.push({
    id: "job:" + j.job_id, kind: j.kind || "job", ts: j.ended_at || j.started_at || j.created_at, status: j.status, active: !j.terminal,
    desc: j.desc || j.job_id, extra: (j.turn_count||0) + " turns · " + (j.tool_count||0) + " tools" + (j.last_tool ? " · " + j.last_tool : ""),
    elapsed: j.elapsed_s, tokens: null, err: j.error,
    open: () => j.workflow_id ? openRun(j.workflow_id) : jumpToSession(j.cwd, j.session_id) }));
  rows.sort((a, b) => (b.active - a.active) || ((b.ts||0) - (a.ts||0)));
  return rows;
}
const rowSig = r => [r.status, r.active, r.desc, r.extra, r.elapsed, r.tokens, r.usd, r.ts];
// ---- ACTIVITY — the full ledger of jobs and workflow runs ----
const ACT = { filter: "all", q: "", shown: 50, limit: 200, act: null };
const ACT_FILTERS = [["all","all"], ["running","running"], ["done","done"], ["error","error"], ["workflows","workflows"], ["jobs","jobs"]];
function actMatch(r) {
  const f = ACT.filter, sc = statusClass(r.status, r.active);
  if (f === "running" && sc !== "run") return false;
  if (f === "done" && sc !== "ok") return false;
  if (f === "error" && sc !== "bad") return false;
  if (f === "workflows" && r.kind !== "workflow") return false;
  if (f === "jobs" && r.kind === "workflow") return false;
  const ql = ACT.q.trim().toLowerCase();
  return !ql || ql.split(/\s+/).every(w => (r.desc + " " + r.kind + " " + r.status + " " + (r.extra||"")).toLowerCase().includes(w));
}
let activityReq = 0;
async function loadActivity(refreshOnly) {
  const pad = document.getElementById("activitypad");
  const my = ++activityReq;
  if (!pad.childElementCount) skeleton(pad);
  let act;
  try { act = await api("/api/activity?" + q({ limit: ACT.limit })); }
  catch (e) { if (my !== activityReq) return; pad.innerHTML = ""; pad.append(el("div","empty","Error: " + e.message)); return; }
  if (my !== activityReq) return;
  ACT.act = act;
  if (refreshOnly && document.getElementById("act-list")) { renderActivityPage(); return; }
  pad.innerHTML = "";
  const ref = el("span","refresh"); ref.append(el("span","live"), document.createTextNode(EVENTS_OK ? "live" : "live · 15s"));
  const c7 = act.counts_7d || {};
  pageHead(pad, "Activity", (act.jobs || []).length + (act.runs || []).length || null,
    (c7.running || 0) + " running · " + (c7.done || 0) + " done · " + (c7.error || 0) + " error · last 7 days", [ref]);
  const bar = el("div","filters");
  const find = findBox("Filter — name, kind, status…  ( / )");
  find.wrap.style.marginBottom = "0"; find.wrap.style.flex = "1"; find.input.value = ACT.q;
  find.input.oninput = () => { ACT.q = find.input.value; ACT.shown = 50; renderActivityPage(); };
  bar.append(find.wrap);
  const chips = el("div","fchips");
  ACT_FILTERS.forEach(([k, lab]) => {
    const ch = el("button","fchip" + (k === ACT.filter ? " on" : ""), lab);
    ch.onclick = () => { ACT.filter = k; ACT.shown = 50; chips.querySelectorAll(".fchip").forEach(x => x.classList.toggle("on", x === ch)); renderActivityPage(); };
    chips.append(ch);
  });
  bar.append(chips); pad.append(bar);
  const wrap = el("div"); wrap.id = "act-list"; pad.append(wrap);
  renderActivityPage();
}
function renderActivityPage() {
  const wrap = document.getElementById("act-list"); if (!wrap || !ACT.act) return;
  const all = activityRows(ACT.act).filter(actMatch);
  let list = wrap.querySelector(".xlist"), more = wrap.querySelector(".xmore");
  if (!all.length) { wrap.innerHTML = ""; wrap.append(emptyState("activity",
    ACT.filter === "all" && !ACT.q ? "Nothing has run yet" : "Nothing matches",
    ACT.filter === "all" && !ACT.q
      ? "Run mantis, spawn a sub-agent or start a workflow and it lands here." : "Try another filter or a shorter search.")); return; }
  if (!list) {
    wrap.innerHTML = "";
    const head = el("div","xrow head");
    ["", "kind", "name", "agents", "elapsed", "tokens", "usd", "status", "age"].forEach(x => head.append(el("span", null, x)));
    list = el("div","xlist"); more = el("div","xmore");
    wrap.append(head, list, more);
  }
  patchList(list, all.slice(0, ACT.shown), r => r.id, rowSig, (row, r) => {
    row = row || el("div"); row.innerHTML = ""; row.className = "xrow";
    row.append(el("span","dot2 " + statusClass(r.status, r.active)));
    row.append(el("span","xk", r.kind));
    const d = el("span","xn", r.desc); d.title = r.err || r.extra || r.desc; row.append(d);
    row.append(el("span","xv xag", r.kind === "workflow" ? r.extra.split(" ")[0] : ""));
    row.append(el("span","xv", (r.active ? "▶ " : "") + fmtDur(r.elapsed)));
    row.append(el("span","xv xtok", r.tokens != null ? fmtTok(r.tokens) : ""));
    row.append(el("span","xv xusd", r.usd ? fmtUsd(r.usd) : ""));
    const st = el("span","xs"); st.append(el("span","t2 " + ({ run: "acc", ok: "acc", bad: "red", pend: "amb" }[statusClass(r.status, r.active)] || ""), r.status || "?")); row.append(st);
    row.append(el("span","xa", r.ts ? ago(r.ts) : ""));
    row.onclick = r.open;
    return row;
  });
  more.innerHTML = "";
  const fetched = (ACT.act.jobs || []).length + (ACT.act.runs || []).length;
  if (all.length > ACT.shown) more.append(btn("Load more · " + (all.length - ACT.shown) + " remaining", "", () => { ACT.shown += 50; renderActivityPage(); }));
  else if (fetched >= ACT.limit && ACT.limit < 500) more.append(btn("Load older", "gho", async () => { ACT.limit = Math.min(500, ACT.limit + 200); ACT.shown += 50; await loadActivity(true); }));
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
    h.append(document.createTextNode("Phase " + (i+1) + " · " + (ph.title || "")));
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
  const [a, g, sp] = await Promise.all([
    api("/api/analytics"),
    api("/api/providers").catch(() => ({ families: [] })),
    api("/api/spend").catch(() => null),
  ]);
  if (my !== homeReq) return;
  pad.innerHTML = "";
  const t = a.totals;
  const ref = el("span","refresh"); ref.id = "refresh-ind";
  ref.append(el("span","live"), document.createTextNode(EVENTS_OK ? "live" : "live · 15s"));
  ref.title = "re-renders when something on disk changes (long-poll), 15s timer as fallback";
  pageHead(pad, "Overview", null, null, [ref]);
  const fams = g.families || [];
  const readyN = fams.filter(f => f.ready).length;
  const s7 = (sp && sp.totals && sp.totals["7"]) || {};
  const curModel = g.current && g.current.model;
  const famSec = section(pad, "Providers · " + readyN + "/" + fams.length + " ready", "~/.mantis-agent/models.json");
  const grid = el("div","fam-grid"); grid.id = "fam-grid"; renderFamilies(grid, g); famSec.append(grid);

  if (sp) {
    const spSec = section(pad, "Spend & usage");
    const card = el("div","card2"); card.id = "spend-card"; renderSpend(card, sp); spSec.append(card);
  }

  const actSec = section(pad, "Activity · last 26 weeks");
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
  th.append(el("span","trace-t", "Trace · last 26 weeks"));
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
  when.append(el("h3", null, "When you work"));
  const ph = a.by_hour.indexOf(Math.max(...a.by_hour));
  const pw = a.by_weekday.indexOf(Math.max(...a.by_weekday));
  const n2 = el("div","note2");
  n2.innerHTML = "Busiest at <b>" + ph + ":00</b> on <b>" + WD[pw] + "</b> · one dot per hour, sized by volume";
  when.append(n2);
  const pw2 = el("div"); pw2.innerHTML = punchSVG(a.punchcard || [[]]); when.append(pw2);
  duo.append(when);

  const what = el("div","card2");
  what.append(el("h3", null, "What it reaches for"));
  const n3 = el("div","note2");
  n3.innerHTML = "<b>" + fmt(t.tool_calls) + "</b> tool calls across <b>" + t.unique_tools + "</b> tools";
  what.append(n3);
  loadHomeSpectrum(what, a.top_tools || [], t.tool_total || t.tool_calls || 1);
  duo.append(what);
  pad.append(duo);

  // projects ledger
  if ((a.top_projects || []).length) {
    const sec = section(pad, "Projects · by volume");
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
const DEPLOY = { providers: [], deployments: [], model: null, inspect: null, q: "", sort: "trending",
                 provider: "all", org: "all", fresh: false, hfToken: false, gpuMax: {}, results: [] };
const DAY = 86400000;
// How current a model is. Inside a year people think in "3 days ago"; beyond
// it, the month and year say more than "14 months ago".
function whenText(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  if (isNaN(t)) return null;
  const age = Date.now() - t;
  if (age > 365 * DAY) return "updated " + new Date(t).toLocaleDateString([], { month: "short", year: "numeric" });
  if (age < DAY) return "updated today";
  const d = Math.round(age / DAY);
  if (d < 30) return "updated " + d + " day" + (d === 1 ? "" : "s") + " ago";
  const mo = Math.round(d / 30);
  return "updated " + mo + " month" + (mo === 1 ? "" : "s") + " ago";
}
const isFresh = m => { const t = Date.parse(m && m.last_modified); return !isNaN(t) && Date.now() - t < 30 * DAY; };
// The biggest card the chosen provider actually rents — what an est-VRAM bar
// should be measured against. "All" falls back to a 1 TB ceiling.
async function gpuCeiling() {
  const pid = DEPLOY.provider;
  if (pid === "all") {
    const cfg = DEPLOY.providers.filter(p => p.configured && provReady(p));
    const known = cfg.map(p => DEPLOY.gpuMax[p.id]).filter(v => v);
    return known.length ? Math.max(...known) : 1024;
  }
  if (DEPLOY.gpuMax[pid]) return DEPLOY.gpuMax[pid];
  try {
    const r = await api("/api/deploy/gpus?" + q({ provider: pid }));
    const mx = Math.max(0, ...(r.gpus || []).map(g => g.total_vram_gb || 0));
    if (mx) DEPLOY.gpuMax[pid] = mx;
    return mx || 1024;
  } catch (e) { return 1024; }
}
// A gated repo needs a Hugging Face token before anything is worth deploying.
// The Hub tells us HOW it gates: "auto" grants access the moment you click
// Agree while signed in; "manual" waits on the repo owner, which can take
// days. Both are answered here, before a GPU is ever paid for.
const gatedBlocked = m => !!(m && m.gated) && !DEPLOY.hfToken;
function gatedChip(m) {
  if (!m || !m.gated) return null;
  if (DEPLOY.hfToken) { const c = pill("gated", " · token set", "acc"); c.title = "HF_TOKEN is configured — this repo can be pulled"; return c; }
  const kind = m.gated_kind === "manual" ? " · manual" : m.gated_kind === "auto" ? " · auto" : "";
  const c = pill("🔒 gated", kind, "amb");
  c.title = m.gated_kind === "manual"
    ? "the repo owner approves access by hand — can take days"
    : "click Agree on the repo page while signed in and access is instant";
  return c;
}
// The token form: masked input, Save, and the link to make one. Saving goes
// through the same creds path as any provider key (the `hf` provider's
// HF_TOKEN), then re-inspects the model so Deploy lights up without a reload.
function hfTokenForm(onSaved) {
  const w = el("div","hf-form");
  const i = input("hf_… (a read token is enough)", true); i.autocomplete = "off";
  const save = btn("Save token", "pri", async () => {
    if (!i.value.trim()) { toast("paste a token first", true); i.focus(); return; }
    save.disabled = true; save.textContent = "Saving…";
    try {
      const r = await post("/api/deploy/creds", { provider: "hf", values: { HF_TOKEN: i.value.trim() } });
      if (r.ok) {
        DEPLOY.hfToken = true; i.value = "";
        toast("✓ Hugging Face token saved");
        if (onSaved) onSaved();
      } else toast(errText(r), true);
    } catch (e) { toast(e.message, true); }
    finally { save.disabled = false; save.textContent = "Save token"; }
  });
  i.onkeydown = e => { if (e.key === "Enter") save.click(); };
  const row = el("div","hf-row"); row.append(i, save);
  w.append(row);
  const foot = el("div","hf-foot");
  foot.append(extLink("a-link", "Create a read token ↗", "https://huggingface.co/settings/tokens"));
  w.append(foot);
  return w;
}
// Re-inspect the selected model and repaint the cards, so a saved token
// turns every lock into "token set" and re-enables Deploy in place.
async function refreshGating() {
  const grid = document.getElementById("dp-models");
  if (grid) grid.querySelectorAll(".mcard").forEach(c => { c.dataset.sig = ""; });
  const id = DEPLOY.model;
  if (id) { DEPLOY.inspect = null; const sec = document.getElementById("dp-fit"); if (sec) renderFit(sec, true); await pickModel(id); }
  const st = document.getElementById("hf-state"); if (st) renderHfState(st);
}
function renderHfState(box) {
  box.innerHTML = "";
  box.append(el("span","dot2 " + (DEPLOY.hfToken ? "ok" : "")));
  box.append(document.createTextNode("Hugging Face token: " + (DEPLOY.hfToken ? "set" : "not set")));
  if (!DEPLOY.hfToken) {
    const a = el("button","hf-add","· add");
    a.onclick = () => { const n = document.getElementById("hf-notice"); if (n) { n.classList.add("on"); const i = n.querySelector("input"); if (i) i.focus(); n.scrollIntoView({ behavior: "smooth", block: "center" }); }
      else openAddKey("hf"); };
    box.append(a);
  }
}
// Which GPU provider the page is scoped to. The hash (#deploy/modal) wins,
// then the last choice in this browser, then — when exactly one provider is
// configured — that one, so the common case needs no clicking at all.
const DEPLOY_PROV_KEY = "mantis-deploy-provider";
const PROV_SHORT = { runpod: "RunPod", hf: "HF", modal: "Modal", deepinfra: "DeepInfra", baseten: "Baseten", vastai: "Vast.ai" };
const provFromHash = () => { const [t, sub] = location.hash.slice(1).split("/"); return t === "deploy" && sub ? sub : null; };
function initDeployProvider(configured) {
  const known = id => id === "all" || DEPLOY.providers.some(p => p.id === id);
  DEPLOY.org = location.hash.slice(1).split("/")[2] || DEPLOY.org || "all";
  let want = provFromHash();
  if (!want) { try { want = localStorage.getItem(DEPLOY_PROV_KEY); } catch (e) { want = null; } }
  if (!want || !known(want)) want = configured.length === 1 ? configured[0].id : "all";
  DEPLOY.provider = want;
}
function writeDeployHash() {
  const p = DEPLOY.provider === "all" ? "" : DEPLOY.provider;
  const o = DEPLOY.org === "all" ? "" : DEPLOY.org;
  const want = "deploy" + (p || o ? "/" + (p || "all") : "") + (o ? "/" + o : "");
  if (location.hash !== "#" + want) location.hash = want;
}
function setDeployProvider(id) {
  DEPLOY.provider = id;
  try { if (id === "all") localStorage.removeItem(DEPLOY_PROV_KEY); else localStorage.setItem(DEPLOY_PROV_KEY, id); } catch (e) { /* private mode */ }
  writeDeployHash();
  document.querySelectorAll("#dp-ptoggle .fchip").forEach(x => x.classList.toggle("on", x.dataset.prov === id));
  const sec = document.getElementById("dp-fit"); if (sec) renderFit(sec);
  const grid = document.getElementById("dp-models");
  if (grid) gpuCeiling().then(() => { grid.querySelectorAll(".mcard").forEach(c => (c.dataset.sig = "")); paintModels(); });
}
// The segmented control itself: every provider, its real mark and short name.
// An unconfigured one is dimmed and, clicked, opens its Add-key form rather
// than selecting something that can't deploy yet.
function providerToggle() {
  const row = el("div","dp-ptoggle"); row.id = "dp-ptoggle";
  const item = (id, label, mark, dim) => {
    const c = el("button","fchip" + (DEPLOY.provider === id ? " on" : "") + (dim ? " dim" : ""));
    c.dataset.prov = id;
    if (mark) c.append(mark);
    c.append(el("span", null, label));
    row.append(c);
    return c;
  };
  item("all", "All", null, false).onclick = () => setDeployProvider("all");
  DEPLOY.providers.forEach(p => {
    const short = PROV_SHORT[p.id] || p.display_name || p.id;
    const ready = provReady(p);
    const c = item(p.id, short, providerMark(p.logo || p.id, short), !p.configured || !ready);
    // a few words here; the full hint lives on the card
    c.title = !ready ? reqShort(p) : p.configured ? "scope to " + (p.display_name || p.id) : "no key yet — click to add one";
    c.onclick = () => {
      if (!ready) openInstallSheet(p);
      else if (p.configured) setDeployProvider(p.id);
      else openAddKey(p.id);
    };
  });
  return row;
}
function openAddKey(pid) {
  const p = DEPLOY.providers.find(x => x.id === pid);
  if (p) openCredSheet(p);
}
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
  pageHead(pad, "Deploy", live.length || null, "Add a GPU key, pick a model, deploy — then use it.", [ref]);
  if (pv.ok === false) {
    const b = el("div","banner"); const t = el("div","sp");
    t.innerHTML = "<b>Deploy isn't available:</b> " + esc(errText(pv)); b.append(t); pad.append(b);
  }

  const pSec = section(pad, "GPU providers · " + configured.length + "/" + DEPLOY.providers.length + " configured", "keys → user settings env");
  const strip = el("div","dp-grid"); strip.id = "dp-grid"; renderDpProviders(strip); pSec.append(strip);

  // what's running sits right under the providers; picking and fitting follow
  const dSec = section(pad, "Deployments" + (live.length ? " · " + live.length + " live" : ""));
  const tbl = el("div"); tbl.id = "dp-deps"; renderDeployments(tbl); dSec.append(tbl);

  initDeployProvider(configured);
  const mSec = section(pad, "Pick a model", "huggingface.co");
  const secT = mSec.querySelector(".sec-t");
  // the org filter belongs to choosing a model; the GPU-provider toggle is a
  // deploy target and now lives on Fit & deploy, where it scopes the table
  const orgRow = el("div","dp-orgs"); orgRow.id = "dp-orgs"; secT.append(orgRow);
  const hfState = el("div","hf-state"); hfState.id = "hf-state"; renderHfState(hfState); secT.append(hfState);
  if (!configured.length) {
    mSec.append(emptyState("socket", "Add a GPU provider to deploy any model",
      "Paste one provider key above, then search every open model on the Hub."));
  } else renderDpPicker(mSec);

  const fSec = section(pad, "Fit & deploy"); fSec.id = "dp-fit";
  fSec.querySelector(".sec-t").append(providerToggle());
  renderFit(fSec);
  // deep link: /?model=<hf id>#deploy lands with that model inspected
  const want = new URLSearchParams(location.search).get("model");
  if (want && configured.length && DEPLOY.model !== want) pickModel(want);
}

// ---- providers strip ----
// One card per adapter: its mark, whether a key is saved and whether it
// validated (with the balance when the provider says), what it can do
// (scale to zero, public endpoint), and the inline key form generated from
// the adapter's own credential_fields. Values go up; only names come back.
// An adapter can be fully keyed and still unable to run: Modal deploys by
// driving its own SDK, so the package is a hard requirement. The check is
// offline, and the full hint is shown once — on the card.
const provReady = p => p && p.requirements_ok !== false;
const reqPkg = p => { const m = /`([^`]+)`/.exec((p && p.requirements_hint) || ""); return m ? m[1] : null; };
const reqShort = p => { const pkg = reqPkg(p); return pkg ? "Needs the " + pkg + " package" : "Needs a package installed"; };
const reqCmd = p => {
  const h = (p && p.requirements_hint) || "";
  const m = /(pip install [^\s].*)$/.exec(h.trim());
  if (m) return m[1].trim();
  const pkg = reqPkg(p);
  return "pip install mantis-agent-sdk" + (pkg ? "[" + pkg + "]" : "");
};
function providerDescriptor(p) {
  const kind = p.id === "vastai" ? "marketplace" : p.scale_to_zero ? "serverless" : "dedicated";
  return kind + " · " + (p.scale_to_zero ? "scale to zero" : "always warm") +
    (p.public_by_default ? " · public endpoint" : "") + (p.id === "vastai" ? " · plain http" : "");
}
function renderDpProviders(box) {
  if (!DEPLOY.providers.length) {
    box.innerHTML = "";
    box.append(emptyState("socket", "No deploy providers registered", "This build has no GPU adapters — update mantis-agent-sdk."));
    return;
  }
  patchList(box, DEPLOY.providers, p => p.id, p => [p.configured, p.account, p.display_name, p.engines], (card, p) => {
    const acct = p.account;
    const ok = !!(acct && acct.ok);
    const ready = provReady(p);
    card = card || el("div"); card.innerHTML = "";
    // configured but unrunnable must not read as ready
    card.className = "dpc" + (p.configured && ready ? " on" : "") + (ready ? "" : " blocked");
    card.id = "dpc-" + p.id;
    const fh = el("div","fh");
    fh.append(bigMark(p.logo || p.id, p.display_name));
    const ft = el("div","ft");
    ft.append(el("div","fn", p.display_name || p.id));
    const fd = el("div","fd", providerDescriptor(p)); fd.title = fd.textContent; ft.append(fd);
    fh.append(ft);
    card.append(fh);
    // the requirement is its own state, above the key state — a provider can need both
    if (!ready) {
      const rq = el("div","fs warn");
      rq.append(el("span","dot2 warn"), el("b", null, reqShort(p)));
      rq.append(el("span","fsx", "· can't deploy until it's installed"));
      card.append(rq);
    }
    // one state, read as a sentence
    const fs = el("div","fs" + (acct && !acct.ok ? " warn" : ""));
    fs.append(el("span","dot2 " + (ok ? "ok" : p.configured ? "warn" : "")));
    if (ok) {
      fs.append(el("b", null, "Validated"));
      const bits = [];
      if (acct.user) bits.push(acct.user);
      if (acct.balance_usd != null) bits.push(fmtUsd(acct.balance_usd) + " balance");
      if (acct.credits_usd != null) bits.push(fmtUsd(acct.credits_usd) + " credits");
      if (bits.length) { const x = el("span","fsx", "· " + bits.join(" · ")); x.title = bits.join(" · "); fs.append(x); }
    } else if (p.configured) {
      fs.append(el("b", null, "Key saved"));
      const x = el("span","fsx", acct && acct.message ? "· " + acct.message : "· not validated yet"); x.title = x.textContent; fs.append(x);
    } else fs.append(el("b", null, "No key"), el("span","fsx", "· add one to deploy here"));
    card.append(fs);
    const chips = el("div","chips");
    (p.engines || []).forEach(e => chips.append(el("span","chip", e)));
    card.append(chips);
    const ff = el("div","ff");
    // the key form is a sheet, never an in-card panel: a card that grew to
    // fit a guide stretched its whole grid row and hollowed out its neighbours
    // an adapter that can't run gets the install action as its primary one
    if (!ready) ff.append(btn("Install", "pri", () => openInstallSheet(p)));
    const addB = btn(p.configured ? "Replace key" : "Add key", p.configured || !ready ? "gho" : "pri", () => openCredSheet(p));
    ff.append(addB);
    if (p.configured) {
      const vb = btn("Validate", "gho", async () => {
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
    const keysUrl = (p.guide && p.guide.keys_url) || p.console_url;
    if (keysUrl) ff.append(extLink("lnk", "↗ " + (p.guide && p.guide.keys_url ? "api keys" : "console"), keysUrl));
    card.append(ff);
    // deep link: /?addkey=<provider>#deploy opens that provider's key sheet
    if (new URLSearchParams(location.search).get("addkey") === p.id && !document.getElementById("modal").className)
      setTimeout(() => openCredSheet(p), 40);
    return card;
  });
}
// What to run, and a way to prove it worked without leaving the page. The
// server never runs pip — this is a copyable command and a re-check.
function openInstallSheet(p) {
  const s = document.getElementById("sheet"); s.innerHTML = "";
  const head = el("div","cs-h");
  head.append(bigMark(p.logo || p.id, p.display_name));
  const ht = el("div","ft");
  ht.append(el("div","fn", "Install " + (p.display_name || p.id) + "'s package"));
  ht.append(el("div","fd", p.requirements_hint || reqShort(p)));
  head.append(ht);
  s.append(head);
  const cmd = reqCmd(p);
  const box = el("div","jsonbox");
  const h2 = el("div","jh");
  h2.append(el("span","jt", "run this in your shell"));
  const cp = btn("Copy", "gho", () => copyText(cmd, "command")); cp.style.marginLeft = "auto"; h2.append(cp);
  box.append(h2);
  const pre = el("pre"); pre.textContent = cmd; box.append(pre);
  s.append(box);
  s.append(el("div","note2", "Installed mantis as a uv tool? Use " +
    "uv tool install --force 'mantis-agent-sdk[" + (reqPkg(p) || "modal") + "]' instead, then re-check."));
  const out = el("div"); s.append(out);
  const foot = el("div","cs-foot");
  const re = btn("Re-check", "pri", async () => {
    re.disabled = true; re.textContent = "Checking…"; out.innerHTML = "";
    try {
      const r = await api("/api/deploy/providers");
      DEPLOY.providers = r.providers || [];
      const now = DEPLOY.providers.find(x => x.id === p.id) || {};
      const grid = document.getElementById("dp-grid"); if (grid) renderDpProviders(grid);
      if (provReady(now)) { toast("✓ " + (p.display_name || p.id) + " is ready"); hideModal(); loadDeploy(); }
      else out.append(probeBox(false, "Still missing — " + (now.requirements_hint || reqShort(now))));
    } catch (e) { out.append(probeBox(false, e.message)); }
    finally { re.disabled = false; re.textContent = "Re-check"; }
  });
  foot.append(re, btn("Close", "gho", hideModal));
  s.append(foot);
  showModal();
  trapFocus(s.parentElement);
}
// The credential sheet. What the user came for is first — the fields — with
// the how-to-get-a-key guide collapsed underneath for whoever needs it, and
// the actions pinned to the bottom. Every fact appears exactly once.
function openCredSheet(p) {
  const g = p.guide;
  const s = document.getElementById("sheet"); s.innerHTML = "";
  const head = el("div","cs-h");
  head.append(bigMark(p.logo || p.id, p.display_name));
  const ht = el("div","ft");
  ht.append(el("div","fn", p.display_name || p.id));
  ht.append(el("div","fd", providerDescriptor(p)));
  head.append(ht);
  s.append(head);

  const inputs = {};
  const fields = el("div","cs-fields");
  (p.credential_fields || []).forEach(f => {
    const row = el("div","dp-field");
    row.append(el("span","kh-l", f.label + (f.required === false ? " · optional" : "")));
    const inp = input(f.env, f.secret !== false); inp.autocomplete = "off";
    inputs[f.env] = inp;
    row.append(inp);
    if (f.help) row.append(el("div","kh-n", f.help));
    fields.append(row);
  });
  s.append(fields);
  const out = el("div"); s.append(out);

  // the guide: one place, collapsed, and it never repeats the fields' help
  if (g) {
    const det = el("details","cs-guide");
    det.append(el("summary", null, "How to get a key"));
    const b = el("div","cs-gb");
    if (g.intro) b.append(el("div","gi", g.intro));
    if ((g.steps || []).length) { const ol = el("ol"); g.steps.forEach(x => ol.append(el("li", null, x))); b.append(ol); }
    const row = el("div","gr");
    if (g.keys_url) row.append(extLink("b pri", "Open " + (g.name || p.display_name || p.id) + " API keys ↗", g.keys_url));
    if (g.key_hint) { const h = el("span","mono", g.key_hint); h.title = "what the key looks like"; row.append(h); }
    b.append(row);
    if (g.free_note) b.append(el("div","gn", g.free_note));
    det.append(b);
    s.append(det);
  } else if (p.console_url) {
    const d2 = el("div","cs-gb"); d2.append(extLink("a-link", "Where to get a key ↗", p.console_url)); s.append(d2);
  }

  const foot = el("div","cs-foot");
  const save = btn("Save & validate", "pri", async () => {
    const values = {};
    Object.entries(inputs).forEach(([k, i]) => { if (i.value.trim()) values[k] = i.value.trim(); });
    const missing = (p.credential_fields || []).filter(f => f.required !== false && !values[f.env]);
    if (missing.length) { toast("fill in " + missing[0].env, true); inputs[missing[0].env].focus(); return; }
    save.disabled = true; save.textContent = "Saving…";
    out.innerHTML = "";
    try {
      const r = await post("/api/deploy/creds", { provider: p.id, values });
      if (r.ok) {
        const a = r.account || {};
        toast(a.ok ? "✓ " + (p.display_name || p.id) + " validated" : "saved · " + (a.message || "validation failed"), !a.ok);
        Object.values(inputs).forEach(i => (i.value = ""));
        hideModal(); loadDeploy(); loadOverview();
      } else out.append(probeBox(false, errText(r)));
    } catch (e) { out.append(probeBox(false, e.message)); }
    finally { save.disabled = false; save.textContent = "Save & validate"; }
  });
  foot.append(save, btn("Cancel", "gho", hideModal));
  s.append(foot);
  Object.values(inputs).forEach(i => (i.onkeydown = e => { if (e.key === "Enter") save.click(); }));
  showModal();
  const sheet = s.parentElement;
  trapFocus(sheet);
  const first = s.querySelector("input"); if (first) setTimeout(() => first.focus(), 40);
}
// Tab stays inside an open sheet — it is modal, so the page behind it is not
// reachable until it closes.
function trapFocus(sheet) {
  sheet.onkeydown = e => {
    if (e.key !== "Tab") return;
    const f = [...sheet.querySelectorAll('input,button,select,textarea,a[href],[tabindex]:not([tabindex="-1"])')]
      .filter(x => !x.disabled && x.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { last.focus(); e.preventDefault(); }
    else if (!e.shiftKey && document.activeElement === last) { first.focus(); e.preventDefault(); }
  };
}

// ---- model picker: the Hub, in the model-table shape ----
let modelSearchReq = 0;
function renderDpPicker(sec) {
  const bar = el("div","filters");
  const find = findBox("Search the Hub — llama, qwen, gemma, deepseek…  ( / )");
  find.wrap.style.marginBottom = "0"; find.wrap.style.flex = "1"; find.input.value = DEPLOY.q;
  bar.append(find.wrap);
  const chips = el("div","fchips");
  [["trending","Trending"], ["downloads","Downloads"], ["likes","Likes"], ["recent","Recent"]].forEach(([k, lab]) => {
    const c = el("button","fchip" + (k === DEPLOY.sort ? " on" : ""), lab);
    c.onclick = () => { DEPLOY.sort = k; chips.querySelectorAll(".fchip").forEach(x => x.classList.toggle("on", x === c)); runSearch(); };
    chips.append(c);
  });
  const fresh = el("button","fchip" + (DEPLOY.fresh ? " on" : ""), "New");
  fresh.title = "released or updated in the last 30 days";
  fresh.onclick = () => { DEPLOY.fresh = !DEPLOY.fresh; fresh.classList.toggle("on", DEPLOY.fresh); paintModels(); };
  const chipWrap = el("div","fchips"); chipWrap.append(fresh);
  bar.append(chips, chipWrap); sec.append(bar);
  const status = el("div","dp-status"); status.id = "dp-mstatus";
  const head = sec.querySelector(".sec-t"); if (head) head.append(status);
  const grid = el("div","dp-mgrid"); grid.id = "dp-models"; sec.append(grid);
  let t = null;
  const runSearch = async () => {
    DEPLOY.q = find.input.value.trim();
    status.textContent = DEPLOY.q ? "searching the Hub…" : "loading curated…";
    const my = ++modelSearchReq;
    let r;
    try { r = await api("/api/deploy/models?" + q({ q: DEPLOY.q, sort: DEPLOY.sort, limit: 30 })); }
    catch (e) { r = { ok: false, error: e.message, models: [] }; }
    if (my !== modelSearchReq) return;
    if (r.hf_token_set != null) DEPLOY.hfToken = !!r.hf_token_set;
    (r.models || []).forEach(m => { if ((r.pending || []).includes(m.id)) m._pending = true; });
    DEPLOY.results = r;
    await gpuCeiling();
    renderOrgPills();
    renderModelRows(grid, r);
    status.textContent = r.ok === false ? "" : (r.models || []).length + (r.curated ? " curated" : " results");
    if (r.partial) enrichLoop(r, grid, status, my);
  };
  // Progressive enrichment: bare ids paint first, then params / dtype /
  // license / vLLM / VRAM fill in as the server's lookups land (diff-render,
  // so nothing flashes). Stops when nothing is pending or after ~40s.
  async function enrichLoop(r, grid, status, my) {
    let pending = (r.pending || []).slice();
    for (let i = 0; i < 26 && pending.length; i++) {
      await sleep(i < 4 ? 900 : 1600);
      if (my !== modelSearchReq) return;
      let e;
      try { e = await api("/api/deploy/models/enrich?" + q({ ids: pending.join(",") })); } catch (err) { return; }
      if (my !== modelSearchReq) return;
      let changed = false;
      (r.models || []).forEach(m => {
        const info = e.models && e.models[m.id];
        if (!info) return;
        if (!info.error) Object.keys(info).forEach(k => { if (info[k] != null) m[k] = info[k]; });
        m._pending = false; changed = true;
      });
      pending = e.pending || [];
      if (changed) { renderOrgPills(); renderModelRows(grid, r); }
      status.textContent = (r.models || []).length + (r.curated ? " curated" : " results") + (pending.length ? " · " + pending.length + " looking up" : "");
    }
    (r.models || []).forEach(m => { m._pending = false; });
    renderModelRows(grid, r);
  }
  find.input.oninput = () => { clearTimeout(t); t = setTimeout(runSearch, 320); };
  find.input.onkeydown = e => {
    if (e.key === "Enter") { clearTimeout(t); runSearch(); }
    else if (e.key === "Escape") { find.input.value = ""; runSearch(); }
  };
  runSearch();
}
// One model as a card: name over its org, a row of soft pills for the
// pre-flight facts, the VRAM estimate as a bar against an 80 GB card, and
// "inspect →" on hover. Results replace the grid in place — keyed, no flash.
const VRAM_CAP_GB = 80;
// One pill per company present in the results, with its real mark and count.
function renderOrgPills() {
  const row = document.getElementById("dp-orgs"); if (!row) return;
  const models = (DEPLOY.results && DEPLOY.results.models) || [];
  const counts = {};
  models.forEach(m => { const o = (m.org || _orgOf(m.id) || "").toLowerCase(); if (o) counts[o] = (counts[o] || 0) + 1; });
  const orgs = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
  if (!orgs.some(o => o === DEPLOY.org)) DEPLOY.org = DEPLOY.org === "all" ? "all" : "all";
  row.innerHTML = "";
  const add = (id, label, mark, n) => {
    const c = el("button","fchip" + (DEPLOY.org === id ? " on" : ""));
    c.dataset.org = id;
    if (mark) c.append(mark);
    c.append(el("span", null, label));
    if (n != null) c.append(el("span","tn2", String(n)));
    c.onclick = () => {
      DEPLOY.org = id;
      row.querySelectorAll(".fchip").forEach(x => x.classList.toggle("on", x === c));
      writeDeployHash();
      paintModels();
    };
    row.append(c);
  };
  add("all", "All", null, models.length);
  orgs.forEach(o => add(o, o, orgMark(o), counts[o]));
}
function modelShown(m) {
  if (m._label) return true;
  if (DEPLOY.org !== "all" && (m.org || _orgOf(m.id) || "").toLowerCase() !== DEPLOY.org) return false;
  if (DEPLOY.fresh && !isFresh(m)) return false;
  return true;
}
function paintModels() {
  const grid = document.getElementById("dp-models");
  if (grid && DEPLOY.results) renderModelRows(grid, DEPLOY.results);
}
function renderModelRows(grid, r) {
  const items = [];
  if (r.ok === false) { grid.innerHTML = ""; grid.append(el("div","browse-empty", errText(r))); return; }
  if (!(r.models || []).length) {
    grid.innerHTML = "";
    grid.append(emptyState("search", r.curated ? "Nothing curated yet" : "No model matches “" + r.query + "”",
      r.curated ? "Type to search the Hub." : "Try the org name, or a family — llama, qwen, gemma, mistral."));
    return;
  }
  grid.querySelectorAll(".zero, .browse-empty").forEach(x => x.remove());
  const shown = (r.models || []).filter(modelShown);
  if (!shown.length) {
    grid.innerHTML = "";
    grid.append(emptyState("search", "No model matches those filters",
      "Clear the company pill or the New filter, or search the Hub for something else."));
    return;
  }
  if (r.curated) items.push({ _label: "Curated · good first deploys", id: "__label" });
  shown.forEach(m => items.push(m));
  patchList(grid, items, m => m.id, m => [m._label, m.params_b, m.dtype, m.license, m.gated, m.gated_kind, DEPLOY.hfToken,
                                          m.vllm_ok, m.est_vram_gb, m.reason, m.downloads, m._pending, m.last_modified,
                                          DEPLOY.gpuMax[DEPLOY.provider] || 0], (card, m) => {
    if (m._label) { card = card || el("div"); card.innerHTML = ""; card.className = "dp-glabel"; card.textContent = m._label; return card; }
    card = card || el("div"); card.innerHTML = "";
    card.className = "mcard" + (m.id === DEPLOY.model ? " on" : ""); card.dataset.model = m.id;
    const slash = m.id.indexOf("/");
    const mh = el("div","mh");
    mh.append(orgMark(m.org || (slash > 0 ? m.id.slice(0, slash) : "")));
    const mtt = el("div","mtt");
    const t = el("div","mt", slash > 0 ? m.id.slice(slash + 1) : m.id); t.title = m.id; mtt.append(t);
    if (slash > 0) mtt.append(el("div","mo", m.id.slice(0, slash)));
    mh.append(mtt); card.append(mh);
    const pend = !!(m._pending && m.params_b == null && m.vllm_ok == null);
    const pills = el("div","mp2");
    if (m.params_b != null) pills.append(pill(fmtParams(m.params_b), " params"));
    if (m.dtype) pills.append(pill(m.dtype, "", "mono"));
    if (m.license) { const lp = pill(m.license, ""); lp.title = "license"; pills.append(lp); }
    const gc = gatedChip(m); if (gc) pills.append(gc);
    if (m.vllm_ok === true) { const v = pill("vllm ✓", "", "acc"); v.title = "architecture served by vLLM"; pills.append(v); }
    else if (m.vllm_ok === false) { const v = pill("vllm ✗", "", "red"); v.title = m.reason || "not servable by vLLM"; pills.append(v); }
    else if (pend) { const v = pill("…", " looking up"); v.title = "reading the Hub's metadata"; pills.append(v); }
    else { const v = pill("vllm ?", "");
      v.title = (m.architectures || []).length
        ? (m.architectures[0] + " is not in the vLLM support list — deploy may still work")
        : "architecture not in the vLLM support list — deploy may still work";
      pills.append(v); }
    if (m.downloads != null) pills.append(pill(fmtTok(m.downloads), " downloads"));
    card.append(pills);
    // the bar is measured against the biggest card the chosen provider rents,
    // so 744 GB and 730 GB no longer look identical — and its colour says
    // whether anything available can actually hold it
    const vr = el("div","vr" + (pend ? " pend" : ""));
    const ceil = DEPLOY.gpuMax[DEPLOY.provider] || (DEPLOY.provider === "all"
      ? Math.max(1024, ...Object.values(DEPLOY.gpuMax).concat([0])) : 1024);
    if (m.est_vram_gb == null && pend) vr.append(el("span", null, "sizing…"));
    else if (m.est_vram_gb != null) {
      const frac = m.est_vram_gb / ceil;
      const cls = frac > 1 ? " no" : frac > 0.85 ? " tight" : " fits";
      const bar = el("div","vbar" + cls); const fill = el("i");
      fill.style.width = Math.max(3, Math.min(100, frac * 100)).toFixed(0) + "%"; bar.append(fill);
      const b = el("b", null, fmtGb(m.est_vram_gb));
      vr.append(bar, b, document.createTextNode("est. vram"));
      vr.title = fmtGb(m.est_vram_gb) + " of " + fmtGb(ceil) + " available" +
        (frac > 1 ? " — larger than anything on offer" : frac > 0.85 ? " — tight" : "");
    } else if (m.params_b == null) vr.append(el("span", null, "size unknown"));
    else vr.append(el("span", null, "vram unknown"));
    card.append(vr);
    const when = whenText(m.last_modified);
    if (when) { const w = el("div","mwhen", when); if (isFresh(m)) w.classList.add("fresh"); card.append(w); }
    card.append(el("span","go", m.id === DEPLOY.model ? "selected ✓" : "inspect →"));
    card.onclick = () => pickModel(m.id);
    return card;
  });
}
async function pickModel(id) {
  DEPLOY.model = id; DEPLOY.inspect = null;
  document.querySelectorAll("#dp-models .mcard").forEach(r => {
    const on = r.dataset.model === id;
    r.classList.toggle("on", on);
    const g = r.querySelector(".go"); if (g) g.textContent = on ? "selected ✓" : "inspect →";
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
    sec.append(emptyState("search", "Pick a model above",
      "Every configured provider's GPUs get checked against it and priced per hour."));
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
  if (r.hf_token_set != null) DEPLOY.hfToken = !!r.hf_token_set;
  if (gatedBlocked(m)) {
    const n = el("div","hf-notice on"); n.id = "hf-notice";
    const h = el("div","hn-h");
    h.append(el("span","t2 amb", "gated · " + (m.gated_kind === "manual" ? "manual" : "auto")));
    h.append(el("b", null, m.id + " needs a Hugging Face token"));
    n.append(h);
    n.append(el("div","hn-b", m.gated_kind === "manual"
      ? "The repo owner approves access by hand — request it on the model page, which can take days, then paste a token here."
      : "Open the model page, click Agree while signed in — access is instant — then paste a read token here."));
    const links = el("div","hn-l");
    links.append(extLink("b", "Open the model page ↗", "https://huggingface.co/" + m.id));
    n.append(links);
    n.append(hfTokenForm(refreshGating));
    sec.append(n);
  }
  const card = el("div","card2");
  const h = el("div","dp-mh");
  h.append(el("span","dp-mid", m.id));
  const gc2 = gatedChip(m); if (gc2) h.append(gc2);
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
  // scoped to one provider → one table, no grouping; "All" keeps the groups
  // a provider that can't run is not offered a GPU group at all
  const runnable = r.fits.filter(f => provReady(DEPLOY.providers.find(x => x.id === f.provider)));
  const fits = DEPLOY.provider === "all" ? runnable : runnable.filter(f => f.provider === DEPLOY.provider);
  const blocked = r.fits.length - runnable.length;
  if (!fits.length) {
    const p = DEPLOY.providers.find(x => x.id === DEPLOY.provider) || {};
    if (!provReady(p)) {
      sec.append(emptyState("socket", reqShort(p), "Install it to deploy here — see the provider card above.",
        btn("Install", "pri", () => openInstallSheet(p))));
      return;
    }
    sec.append(zero("Nothing to fit on " + (p.display_name || DEPLOY.provider),
      "This provider has no key yet, or its catalogue didn't answer. Pick another above, or choose All."));
    return;
  }
  if (blocked && DEPLOY.provider === "all")
    sec.append(el("div","note2", blocked + " provider" + (blocked === 1 ? "" : "s") + " hidden — a package needs installing (see the cards above)."));
  fits.forEach(f => sec.append(fitTable(f, m)));
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
  const gg = el("div","dp-ggrid");
  gpus.forEach(g => {
    const card = el("div","gcard" + (g.verdict === "no" ? " no" : ""));
    const gt = el("div","gt", g.family && g.family !== "other" ? g.family : (g.display || g.provider_id));
    gt.append(el("small", null, fmtGb(g.total_vram_gb) + (g.count > 1 ? " · " + g.count + "×" + g.vram_gb : "") + (g.region ? " · " + g.region : "")));
    gt.title = g.display || g.provider_id; card.append(gt);
    const gp = el("div","gp"); gp.append(document.createTextNode(g.price_per_hour == null ? "—" : "$" + (g.price_per_hour < 1 ? g.price_per_hour.toFixed(3) : g.price_per_hour.toFixed(2))));
    if (g.price_per_hour != null) gp.append(el("small", null, " /h")); card.append(gp);
    const gm = el("div","gm");
    const vd = el("span","vd " + g.verdict, g.verdict);
    vd.title = g.reason || (g.verdict === "tight" ? "under 15% headroom" : g.verdict === "fits" ? "fits with headroom" : "");
    gm.append(vd, el("span", null, g.verdict === "no" ? (g.reason || "too small") : (f.scale_to_zero ? "from zero · first request waits" : "warm · billed while idle")));
    card.append(gm);
    const ga = el("div","ga");
    if (g.verdict !== "no") {
      const b = btn("Deploy", "pri", () => confirmDeploy(f, g, m, eng.value, A));
      if (g.available === false) { b.disabled = true; b.title = "no capacity right now"; }
      // a gated repo with no token would fail the moment the GPU is paid for —
      // the button says so instead of the job dying at 0s
      if (gatedBlocked(m)) { b.disabled = true; b.textContent = "Needs HF token"; b.title = "gated repo · save a Hugging Face token above to deploy"; }
      else if (!provReady(p)) { b.disabled = true; b.textContent = "Needs package"; b.title = reqShort(p); }
      ga.append(b);
      if (new URLSearchParams(location.search).get("confirm") === g.provider_id && !document.getElementById("modal").className)
        setTimeout(() => confirmDeploy(f, g, m, eng.value, A), 60);
    } else ga.append(el("span","mgo", "won't fit"));
    card.append(ga);
    gg.append(card);
  });
  box.append(gg);
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
const _orgOf = id => { const i = String(id || "").indexOf("/"); return i > 0 ? String(id).slice(0, i) : ""; };
// The pairing: model (org mark) → provider (vendor mark). Used by the confirm
// sheet, the progress sheet and, small, by every deployment row.
function pairHeader(modelId, providerId, providerName, caption, small) {
  const h = el("div","pair" + (small ? " sm" : ""));
  const L = el("div","pside");
  L.append(orgMark(_orgOf(modelId)));
  if (!small) { const lt = el("div","pt"); const slash = String(modelId).indexOf("/");
    lt.append(el("div","pn mono", slash > 0 ? modelId.slice(slash + 1) : modelId)); if (slash > 0) lt.append(el("div","pc", modelId.slice(0, slash))); L.append(lt); }
  const C = el("div","parrow"); C.append(el("b", null, "→")); if (!small) C.append(document.createTextNode("deploys to"));
  const p = DEPLOY.providers.find(x => x.id === providerId) || {};
  const R = el("div","pside");
  R.append(bigMark(p.logo || providerId, providerName));
  if (!small) { const rt = el("div","pt"); rt.append(el("div","pn", providerName || providerId)); if (caption) rt.append(el("div","pc", caption)); R.append(rt); }
  h.append(L, C, R);
  return h;
}
const fmtIdle = s => { s = parseInt(s || "0", 10) || 0; return s >= 60 ? Math.round(s / 60) + " min" : s + " s"; };
// The confirmation sheet — the pairing as the headline, the spec as tiles,
// the cost as the number you can't miss, advanced knobs behind a disclosure.
function confirmDeploy(f, g, m, engine, A) {
  const base = collectOpts(A);
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(pairHeader(m.id, f.provider, f.display_name || f.provider, [g.region, engine].filter(Boolean).join(" · ")));
  // sheet-local advanced inputs, pre-filled from the panel's values
  const S = {};
  const adv = el("details","dp-adv"); adv.append(el("summary", null, "Advanced · parallelism, quantization, idle timeout, token"));
  const grid = el("div","dp-advgrid");
  const field = (k, node, label) => { S[k] = node; const w = el("label","dp-field"); w.append(el("span","kh-l", label || k), node); grid.append(w); };
  const num = (k, ph, label) => { const i = input(ph); i.type = "number"; i.min = "0"; if (base[k] != null) i.value = base[k]; field(k, i, label); };
  num("tensor_parallel", "defaults to " + (g.count || 1), "tensor parallel");
  const quant = el("select","in"); ["", "fp8", "awq", "gptq", "int8", "bitsandbytes"].forEach(v => { const o = el("option", null, v || "none"); o.value = v; quant.append(o); });
  quant.value = base.quantization || ""; field("quantization", quant);
  num("idle_timeout_s", "seconds", "idle timeout (s)"); if (S.idle_timeout_s.value === "") S.idle_timeout_s.value = 300;
  num("max_model_len", m.context_len ? "≤ " + m.context_len : "tokens", "max model len");
  if (m.gated) {
    const hf = input("HF token with access to this repo", true); hf.autocomplete = "off"; if (base.hf_token) hf.value = base.hf_token;
    field("hf_token", hf, "HF_TOKEN · gated repo");
    const sv = el("div","dp-field"); sv.style.gridColumn = "1 / -1";
    sv.append(hfTokenForm(() => { refreshGating(); go.disabled = false; go.textContent = "Deploy to " + (f.display_name || f.provider); gateNote.remove(); }));
    grid.append(sv);
  }
  const trc = document.createElement("input"); trc.type = "checkbox"; trc.checked = !!base.trust_remote_code; S.trust_remote_code = trc;
  const tl = el("label","chk"); tl.append(trc, document.createTextNode("trust_remote_code")); grid.append(tl);
  adv.append(grid);
  const mn = Math.max(0, parseInt(base.min_replicas || (f.scale_to_zero ? "0" : "1"), 10) || 0);
  const mx = Math.max(1, parseInt(base.max_replicas || "1", 10) || 1);
  const z = f.scale_to_zero && mn === 0;
  // spec tiles
  const tiles = el("div","spec-grid");
  const tile = (label, value, hint, pillNode) => { const t = el("div","tile"); t.append(el("div","tl", label)); const v = el("div","tv", value); if (pillNode) v.append(pillNode); t.append(v); if (hint) t.append(el("div","th2", hint)); tiles.append(t); return t; };
  tile("GPU", (g.family && g.family !== "other" ? g.family : (g.display || g.provider_id)) + (g.count > 1 ? " ×" + g.count : ""), fmtGb(g.total_vram_gb) + (g.count > 1 ? " total · " + g.vram_gb + " GB each" : ""));
  tile("Engine", engine, base.engine_version ? "pinned " + base.engine_version : "latest image");
  const rt = tile("Replicas", mn + "–" + mx, z ? null : "always warm"); if (z) { const h = el("div","th2"); h.append(pill("scales to zero", "", "acc")); rt.append(h); }
  const ctxTile = tile("Context", base.max_model_len ? fmtCtx(parseInt(base.max_model_len, 10)) : "model default", m.context_len ? "model max " + fmtCtx(m.context_len) : null);
  s.append(tiles);
  // cost — the headline
  const rate = g.price_per_hour;
  const run = rate == null ? null : rate * mx;
  const idle = z ? 0 : (rate == null ? null : rate * Math.max(1, mn));
  const cb = el("div","cost-big"); cb.append(el("b", null, run == null ? "—" : fmtRate(run).replace("/h", "")), el("span", null, "/h while running")); s.append(cb);
  const sub = el("div","cost-sub");
  const idleLine = el("span", null, "");
  const setIdle = () => { idleLine.textContent = z ? "idle $0.00/h · scales to zero after " + fmtIdle(S.idle_timeout_s.value) : "idle " + (idle == null ? "unknown" : fmtRate(idle)) + " · always warm, keeps billing"; idleLine.className = z ? "" : "warnline"; };
  setIdle(); S.idle_timeout_s.oninput = setIdle;
  sub.append(idleLine, el("span", null, f.scale_to_zero ? "~2–4 min first request (cold start)" : "no cold start · warm"));
  s.append(sub);
  s.append(adv);
  S.max_model_len.oninput = () => { ctxTile.querySelector(".tv").textContent = S.max_model_len.value ? fmtCtx(parseInt(S.max_model_len.value, 10) || 0) : "model default"; };
  if (f.public_by_default) {
    const b = el("div","banner"); b.style.margin = "0 0 14px";
    b.append(document.createTextNode("This provider's endpoint is reachable by anyone with the URL. Keep the auth env var set and tear down when you're done."));
    s.append(b);
  }
  const gateNote = el("div","banner"); gateNote.style.margin = "0 0 14px";
  if (gatedBlocked(m)) {
    gateNote.append(document.createTextNode(m.id + " is gated (" + (m.gated_kind === "manual" ? "owner approval" : "click Agree, instant") +
      ") — save a Hugging Face token under Advanced first."));
    s.insertBefore(gateNote, adv);
  }
  const foot = el("div","cta"); foot.style.marginTop = "4px";
  const go = btn("Deploy to " + (f.display_name || f.provider), "pri", async () => {
    go.disabled = true; go.textContent = "Starting…";
    const opts = { ...base, ...collectOpts(S) };
    if (!S.trust_remote_code.checked) delete opts.trust_remote_code;
    try {
      const r = await post("/api/deploy/up", { provider: f.provider, model: m.id, gpu: g.provider_id, engine, opts });
      if (r.ok) openJobSheet(r.job, { kind: "deploy", model: m.id, provider: f.display_name || f.provider, providerId: f.provider, gpu: g.display || g.provider_id, caption: [g.display || g.provider_id, g.region, engine].filter(Boolean).join(" · ") });
      else { toast(errText(r), true); go.disabled = false; go.textContent = "Deploy to " + (f.display_name || f.provider); }
    } catch (e) { toast(e.message, true); go.disabled = false; go.textContent = "Deploy to " + (f.display_name || f.provider); }
  });
  if (gatedBlocked(m)) { go.disabled = true; go.title = "gated repo · save a Hugging Face token first"; adv.open = true; }
  foot.append(go, btn("Cancel", "gho", hideModal));
  s.append(foot);
  showModal();
  const sheet = s.parentElement; sheet.tabIndex = -1;
  sheet.onkeydown = e => { if (e.key === "Enter" && !["TEXTAREA","SELECT"].includes(e.target.tagName) && !go.disabled) { e.preventDefault(); go.click(); } };
  setTimeout(() => sheet.focus(), 30);
}

// ---- the job sheet: progress lines streamed from a background job ----
let jobPollT = null;
function stopJobPoll() { if (jobPollT) { clearInterval(jobPollT); jobPollT = null; } }
function openJobSheet(jobId, ctx) {
  stopJobPoll();
  const s = document.getElementById("sheet"); s.innerHTML = "";
  if (ctx.kind === "deploy" && ctx.providerId) {
    s.append(el("h3", null, "Deploying"));
    s.append(pairHeader(ctx.model, ctx.providerId, ctx.provider, ctx.caption || ctx.gpu));
  } else {
    s.append(el("h3", null, (ctx.kind === "teardown" ? "Tearing down " : "Deploying ") + ctx.model));
    s.append(el("div","sub", [ctx.provider, ctx.gpu].filter(Boolean).join(" · ") + " · job " + jobId));
  }
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
    refreshDeployments(false).then(() => { if (ctx.kind === "deploy" && j.result) highlightDeployment(j.result.id); });
    loadOverview();
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
function highlightDeployment(id) {
  const row = document.querySelector('#dp-deps [data-key="' + CSS.escape(String(id)) + '"]');
  if (!row) return;
  row.scrollIntoView({ behavior: "smooth", block: "center" });
  row.classList.add("flash"); setTimeout(() => row.classList.remove("flash"), 1600);
}
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
    tbl.append(emptyState("deploy", "No deployments yet",
      "Pick a model below, choose a GPU that fits, and it runs here.",
      btn("Pick a model", "pri", () => {
        const m = document.getElementById("dp-models");
        if (m) m.scrollIntoView({ behavior: "smooth", block: "start" });
        const i = document.querySelector("#deploy .find input"); if (i) setTimeout(() => i.focus(), 320);
      })));
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
    const mk = el("span","pair sm"); mk.append(orgMark(_orgOf(d.model)), el("span","parrow"), bigMark(p.logo || d.provider, p.display_name || d.provider));
    mk.querySelector(".parrow").append(el("b", null, "→")); mk.title = d.model + " → " + (p.display_name || d.provider); row.append(mk);
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
const VIEWS = ["home","models","sessions","activity","deploy","mcp","skills","config"];
let curView = "home";
function showTab(name) {
  const b = document.querySelector('#nav button[data-v="' + name + '"]');
  if (!b) return;
  curView = name;
  document.querySelectorAll("#nav button").forEach(x => x.classList.toggle("on", x === b));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("on", v.id === name));
  hideModal();                       // a sheet must never outlive its page
  // a sub-route (#models/claude) survives a re-selection of its own tab
  const base = location.hash.slice(1).split("/")[0];
  if (base !== name) location.hash = name;  // fires hashchange; guarded below
  if (name === "home") loadHome();
  if (name === "models") loadModels();
  if (name === "activity") loadActivity();
  if (name === "deploy") loadDeploy();
  if (name === "skills") loadSkills();
  if (name === "mcp") loadMcp();
  if (name === "config") loadConfig();
}
document.getElementById("nav").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  showTab(b.dataset.v);
});
// Keyboard: 1–8 jump between pages (the tabs show each key), `g` then a
// letter does the same by name (g o · g s · g a · g m · g d · g p · g k · g c), `/`
// drops into whatever search the current page has, ⌘K opens the palette.
let chord = null, chordT = null;
const CHORDS = { o: "home", s: "sessions", a: "activity", m: "models", d: "deploy", p: "mcp", k: "skills", c: "config" };
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
  const i = "12345678".indexOf(e.key);
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
      const g = await api("/api/providers");
      const grid = document.getElementById("fam-grid");
      if (grid) renderFamilies(grid, g);
    }
    if (curView === "activity") await loadActivity(true);
    if (curView === "sessions") { await loadProjects(); if (curProject) await loadSessions(curProject); }
    if (curView === "deploy" && !document.getElementById("modal").className) await refreshDeployments(false);
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
  const chordFor = v => { const k = Object.keys(CHORDS).find(k => CHORDS[k] === v); const n = VIEWS.indexOf(v) + 1; return (n ? n + " · " : "") + (k ? "g " + k : ""); };
  const PAGE_NAMES = { home: "Overview", models: "My models", sessions: "Sessions", activity: "Activity", deploy: "Deploy", mcp: "MCP", skills: "Skills", config: "Config" };
  VIEWS.forEach(v => items.push({ g: "Pages", t: PAGE_NAMES[v] || v, k: chordFor(v), run: () => showTab(v) }));
  (PROJECTS || []).forEach(p => items.push({ g: "Projects", t: p.title || p.name, s: p.session_count + " session" + (p.session_count===1?"":"s"),
    run: () => { showTab("sessions"); setTimeout(() => selectProject(p.digest), 60); } }));
  (SESSIONS || []).forEach(x => items.push({ g: "Sessions", t: x.display_title, s: ago(x.modified_at),
    run: () => { showTab("sessions"); setTimeout(() => selectSession(x.session_id), 60); } }));
  (DEPLOY.deployments || []).filter(d => d.is_live).forEach(d => items.push({ g: "Deployments", t: "connect " + (d.name || d.id),
    s: d.model, run: () => useDeployment(d) }));
  (FAMS || []).forEach(f => { const target = (f.providers || []).find(x => x.enabled) || (f.providers || [])[0];
    if (target) items.push({ g: "Actions", t: "test " + f.label + " provider", s: target.id, run: () => testProviderQuick(target) }); });
  items.push({ g: "Actions", t: "toggle theme", s: getTheme() || "system", run: cycleTheme });
  items.push({ g: "Actions", t: "refresh now", s: EVENTS_OK ? "live" : "timer", run: () => refreshLive(true) });
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
  const [t, sub] = location.hash.slice(1).split("/");
  // Only react to a REAL change (back/forward, manual edit) — showTab already
  // handled the tab it set the hash to, so don't reload it a second time.
  if (VIEWS.includes(t) && t !== curView) { if (t === "models") MODEL_TAB = tabFromHash(); showTab(t); return; }
  if (t === "deploy" && curView === "deploy") {
    const org = location.hash.slice(1).split("/")[2] || "all";
    if (org !== DEPLOY.org) { DEPLOY.org = org; renderOrgPills(); paintModels(); }
    if ((sub || "all") !== DEPLOY.provider) setDeployProvider(sub || "all");
    return;
  }
  // a family tab picked from the URL while Models is already open
  if (t === "models" && curView === "models" && tabFromHash() !== MODEL_TAB) {
    MODEL_TAB = tabFromHash();
    loadModels();     // re-renders the tab row and the list from MODEL_TAB
  }
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
    c.innerHTML = ""; c.append(emptyState("session", "No sessions yet", "Run mantis in a project and it appears here."));
    document.querySelector("#sessionlist .col-head").textContent = "Sessions";
    return;
  }
  if (!document.querySelector("#sesscards .pcard")) { const sc = document.getElementById("sesscards"); if (!sc.querySelector(".zero")) { sc.innerHTML = ""; sc.append(emptyState("session", "Pick a project", "Its sessions list here, newest first.")); } }
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
  if (!sessions.length) { c.innerHTML = ""; c.append(emptyState("session", "No sessions", "Nothing recorded in this project yet.")); return; }
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
  h.append(el("span", null, "Context fill per turn"));
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
const ORG_MARKS = __ORGLOGOS__;
// An org's mark for a model card: the JSON set, else the same-origin Hub
// avatar proxy (lazy, never blocks the card; 204 → the letter shows), else
// the letter. Only same-origin URLs are ever requested.
function orgMark(org) {
  const w = el("span","omark");
  const key = (org || "").toLowerCase();
  const m = ORG_MARKS[key];
  w.textContent = (org || "?").slice(0, 1).toUpperCase();
  if (m && m.svg) {
    w.textContent = ""; w.innerHTML = m.svg;
    if (m.tint) { w.style.color = m.tint; w.style.background = "color-mix(in srgb, " + m.tint + " 16%, transparent)"; }
    return w;
  }
  if (key) {
    const img = document.createElement("img");
    img.loading = "lazy"; img.alt = "";
    img.src = "/api/deploy/org-avatar?" + q({ org: key }) + (TOKEN ? "&k=" + encodeURIComponent(TOKEN) : "");
    img.onerror = () => img.remove();
    img.onload = () => w.classList.add("img");
    w.append(img);
  }
  return w;
}
// One square, one inset, one alpha — whatever grid the vendor drew on. The
// square is tinted only when the vendor's own colour is known (a hex); a
// monochrome mark inherits the ink and sits on the neutral square, so no
// provider gets a stray grey box while its neighbour glows.
const TINT_ALPHA = "12%";
function markSvg(svg) {
  return String(svg).replace(/<svg\b/, '<svg preserveAspectRatio="xMidYMid meet"');
}
function bigMark(pid, label) {
  const m = MARKS[pid];
  const w = el("span","bigmark");
  if (m && m.svg) {
    w.innerHTML = markSvg(m.svg);
    if (m.tint) w.style.color = m.tint;
    if (m.tint && /^#/.test(m.tint)) w.style.background = "color-mix(in srgb, " + m.tint + " " + TINT_ALPHA + ", transparent)";
  } else w.textContent = (label || pid || "?").slice(0, 1).toUpperCase();
  return w;
}
function providerMark(pid, label) {
  const m = MARKS[pid];
  const w = el("span","mark2");
  if (m && m.svg) {
    w.innerHTML = markSvg(m.svg);
    if (m.tint) w.style.color = m.tint;
  } else {
    w.textContent = (label || pid || "?").slice(0, 1).toUpperCase();
  }
  return w;
}

// ---- provider setup: every way to authenticate each family ----------------
// One card per family; opening one reveals its methods as selectable rows.
// Several methods can be configured at once — exactly one is active, and
// switching is a single click. Values only ever travel inward: what comes
// back is env var names and the contract's masked hints.
const AUTH = { families: [], open: null, method: {} };
function authStatusLine(f) {
  const w = el("div","fa");
  w.append(el("span","dot2 " + (f.connected ? "ok" : f.configured && f.configured.length ? "warn" : "")));
  w.append(el("span","fsx", f.status_line || "Not connected"));
  return w;
}
async function loadAuthFamilies(box) {
  let r;
  try { r = await api("/api/auth/families"); }
  catch (e) { r = { ok: false, error: e.message, families: [] }; }
  AUTH.families = r.families || [];
  renderAuthCards(box, r);
}
// What the models page knows: endpoints, key envs, live model lists.
let MSTATE = {};
const FIRST_PARTY = ["anthropic", "openai", "gemini", "xai"];
// The connectable things: each first-party family is one card (its methods
// are the auth-type toggle inside it), and every open-source method — a
// hosted provider, Ollama, your own server — is a card of its own.
function authEntries() {
  const out = [];
  FIRST_PARTY.forEach(fid => {
    const f = AUTH.families.find(x => x.family === fid);
    if (f) out.push({ key: fid, family: fid, label: f.label, logo: f.logo, methods: f.methods || [],
                      active: f.active, ui: f.ui_family, kind: "family" });
  });
  const oss = AUTH.families.find(x => x.family === "oss");
  (oss ? oss.methods || [] : []).forEach(mm => {
    out.push({ key: "oss/" + mm.id, family: "oss", label: mm.label, logo: mm.id, methods: [mm],
               active: mm.status.active ? mm.id : null, ui: "oss", kind: "method" });
  });
  return out;
}
function provMeta(e) {
  const provs = MSTATE.providers || [];
  if (e.kind === "method") return provs.find(x => x.id === e.logo) || null;
  return provs.find(x => x.family === e.ui) || null;
}
function renderAuthCards(box, r) {
  box.innerHTML = "";
  if (r.ok === false && !(r.families || []).length) {
    const b = el("div","banner"); const t = el("div","sp");
    t.innerHTML = "<b>Provider setup isn't available:</b> " + esc(r.error || "unknown error");
    b.append(t); box.append(b); return;
  }
  const entries = authEntries();
  const connected = entries.filter(e => e.active).length;
  const prog = el("div","setup");
  const ph = el("div","setup-h");
  ph.append(el("span","setup-n", connected + " of " + entries.length + " connected"));
  ph.append(el("span","setup-s", connected
    ? "Add another to switch between them mid-session."
    : "Connect one and mantis is ready to run."));
  prog.append(ph);
  const track = el("div","setup-bar"); const fill = el("i");
  fill.style.width = Math.round(connected / Math.max(1, entries.length) * 100) + "%";
  track.append(fill); prog.append(track);
  prog.append(el("div","setup-note",
    "Keys are written to ~/.mantis-agent (chmod 600) on this machine and are only ever shown masked."));
  box.append(prog);
  [["First-party", entries.filter(e => e.kind === "family")],
   ["Open-source & self-host", entries.filter(e => e.kind === "method")]].forEach(([label, list]) => {
    if (!list.length) return;
    box.append(el("div","auth-glabel", label));
    const grid = el("div","auth-grid");
    list.forEach(e => grid.append(authCard(e)));
    box.append(grid);
  });
}
// One provider: the mark and name as the headline, endpoint and key env as
// quiet metadata, the auth-type toggle as the interactive core, and — only
// once connected — the models it serves as a footer.
function authCard(e) {
  const meta = provMeta(e);
  const card = el("div","acard" + (e.active ? " on" : ""));
  card.id = "auth-" + e.key.replace("/", "-");
  const am = e.methods.find(x => x.id === e.active);
  const cfg = e.methods.filter(x => x.status.configured);
  const head = el("div","ac-h");
  head.append(bigMark(e.logo || e.family, e.label));
  const ht = el("div","ft");
  // name and status share one line; the endpoint is the caption under it
  const top = el("div","ac-top");
  top.append(el("div","fn", e.label));
  const st = el("span","ac-s" + (e.active ? " ok" : cfg.length ? " warn" : ""));
  st.append(el("span","dot2 " + (e.active ? "ok" : cfg.length ? "warn" : "")));
  if (am) {
    const masked = Object.values(am.status.masked || {}).find(Boolean);
    st.append(document.createTextNode(am.status.source === "env" ? "From env" : am.label));
    st.title = (am.status.source === "env" ? "Connected from env · " : "Connected via " + am.label) + (masked ? " · " + masked : "");
  } else if (cfg.length) { st.append(document.createTextNode("Not active")); st.title = "Configured, but not the active method"; }
  else { st.append(document.createTextNode("Not connected")); st.title = "No credential saved yet"; }
  top.append(st);
  ht.append(top);
  const ep = (meta && meta.base_url) || (e.methods[0] && e.methods[0].backend) || "";
  const epl = el("div","fd mono", String(ep).replace(/^https?:\/\//, "") || "—"); epl.title = ep; ht.append(epl);
  head.append(ht);
  card.append(head);
  // the auth-type toggle — a lone method is a label, not a lonely pill
  let sel = AUTH.method[e.key] || e.active || (e.methods.find(x => x.recommended) || e.methods[0] || {}).id;
  const body = el("div","ac-body");
  const draw = () => {
    body.innerHTML = "";
    const m = e.methods.find(x => x.id === sel) || e.methods[0];
    if (!m) return;
    const d1 = el("div","ac-d", m.description || ""); d1.title = m.description || ""; body.append(d1);
    body.append(authMethodForm({ label: e.label, logo: e.logo, active: e.active }, m));
    if (meta && (meta.models || []).length) {
      const foot = el("div","ac-models");
      foot.append(el("div","ac-mh", (meta.models.length + " listed") + (meta.live_count ? " · " + meta.live_count + " live" : "")));
      const chips = el("div","chips clamp");
      const cur = (MSTATE.current || {}).model;
      meta.models.forEach(x => {
        const c = el("span","chip" + (e.active ? " clk" : "") + (x === cur ? " cur" : ""), x);
        if (e.active) c.onclick = ev => { ev.stopPropagation(); useModel(x, meta.base_url); };
        chips.append(c);
      });
      foot.append(chips);
      // two rows, then a +N that opens the rest in place
      const hidden = Math.max(0, meta.models.length - 2);
      const more = el("button","chip more", "+" + hidden + " more");
      more.onclick = ev => { ev.stopPropagation(); chips.classList.toggle("clamp");
        more.textContent = chips.classList.contains("clamp") ? "+" + hidden + " more" : "Show fewer"; };
      if (meta.models.length > 2) foot.append(more);
      body.append(foot);
    }
  };
  if (e.methods.length > 1) {
    const seg = el("div","ac-types");
    e.methods.forEach(m => {
      const c = el("button","fchip" + (m.id === sel ? " on" : "") + (m.status.active ? " ac-live" : ""), m.label);
      // the active method is the filled one with a tick; configured-but-idle
      // carries a dot; unconfigured stays plain
      if (m.status.active) c.append(el("span","ac-tick", "✓"));
      else if (m.status.configured) c.append(el("span","ac-dot cfg"));
      c.onclick = () => {
        sel = m.id; AUTH.method[e.key] = m.id;
        seg.querySelectorAll(".fchip").forEach(x => x.classList.toggle("on", x === c));
        draw();
      };
      seg.append(c);
    });
    card.append(seg);
  } else if (e.methods.length === 1) {
    card.append(el("div","ac-one", e.methods[0].label));
  }
  card.append(body);
  draw();
  return card;
}
// One method's fields + its action. OAuth swaps Save for a two-step sign-in.
function authMethodForm(d, m) {
  const f = el("div","ap-form");
  const out = el("div");
  if (m.kind === "oauth") { f.append(oauthFlow(d, m, out), out); return f; }
  const inputs = {};
  const grid = el("div","ap-fields");
  (m.fields || []).forEach(fd => {
    const w = el("label","dp-field");
    w.append(el("span","kh-l", fd.label + (fd.required ? "" : " · optional")));
    const i = input(fd.placeholder || fd.label, fd.secret); i.autocomplete = "off";
    const held = (m.status.masked || {})[fd.env];
    if (held) i.placeholder = held + " · saved, type to replace";
    inputs[fd.env] = i;
    w.append(i);
    const help = el("div","kh-n", fd.help || "");
    if (fd.env) help.append(el("span","envn", " " + fd.env));
    w.append(help);
    grid.append(w);
  });
  if ((m.fields || []).length) f.append(grid);
  if (m.kind === "cloud" && m.status.source === "cli")
    f.append(el("div","ap-note", "Ambient credentials detected — " + (m.status.hint || "leave the fields blank to use them")));
  const acts = el("div","ap-acts");
  const check = btn("Check reachability", "gho", async () => {
    check.disabled = true; check.textContent = "Reaching…"; out.innerHTML = "";
    try {
      const v = await post("/api/auth/validate", { family: m.family, method: m.id });
      out.append(probeBox(v.ok, v.ok
        ? "Reachable · " + (v.latency_ms != null ? v.latency_ms + "ms" : "") + ((v.models || []).length ? " · " + v.models.length + " models live" : "")
        : (v.message || errText(v))));
    } catch (e) { out.append(probeBox(false, e.message)); }
    finally { check.disabled = false; check.textContent = "Check reachability"; }
  });
  const save = btn(m.status.active ? "Save & test" : m.status.configured ? "Make active & test" : "Save & test", "pri", async () => {
    const values = {};
    Object.entries(inputs).forEach(([k, i]) => { if (i.value.trim()) values[k] = i.value.trim(); });
    const missing = (m.fields || []).filter(x => x.required && !values[x.env] && !(m.status.masked || {})[x.env]);
    if (missing.length && !m.status.configured) { toast("fill in " + missing[0].label, true); inputs[missing[0].env].focus(); return; }
    save.disabled = true; save.textContent = "Saving…";
    out.innerHTML = "";
    try {
      const r = await post("/api/auth/set", { family: m.family, method: m.id, values });
      if (!r.ok) { out.append(probeBox(false, r.message || errText(r))); return; }
      Object.values(inputs).forEach(i => (i.value = ""));
      save.textContent = "Testing…";
      const v = await post("/api/auth/validate", { family: m.family, method: m.id });
      out.append(probeBox(v.ok, v.ok
        ? "Connected · " + (v.latency_ms != null ? v.latency_ms + "ms" : "") + ((v.models || []).length ? " · " + v.models.slice(0, 3).join(", ") : "")
        : (v.message || errText(v))));
      toast(r.ok ? "✓ " + d.label + " · " + m.label : "saved", false);
      loadAuthFamilies(document.getElementById("auth-cards"));
      loadOverview();
    } catch (e) { out.append(probeBox(false, e.message)); }
    finally { save.disabled = false; save.textContent = "Save & test"; }
  });
  acts.append(save);
  if (m.status.configured) acts.append(check);
  if (m.status.configured && m.status.source !== "cli") {
    const del = btn("Forget", "gho dan", null);
    del.onclick = () => armDelete(del, async () => {
      try {
        const r = await post("/api/auth/clear", { family: m.family, method: m.id });
        if (r.ok !== false) { toast("forgot " + m.label); loadAuthFamilies(document.getElementById("auth-cards")); }
        else toast(r.message || errText(r), true);
      } catch (e) { toast(e.message, true); }
    });
    acts.append(del);
  }
  if (m.docs_url) { const dl = extLink("ac-doc", "↗", m.docs_url); dl.title = "Provider docs"; acts.append(dl); }
  f.append(acts, out);
  return f;
}
function probeBox(ok, text) {
  const p = el("div","probe " + (ok ? "ok" : "bad"));
  const h = el("div","ph2"); h.append(el("span","dot2 " + (ok ? "ok" : "bad")), document.createTextNode(ok ? "Works" : "Didn't connect"));
  p.append(h);
  if (text) p.append(el("div", ok ? "note2" : "pe", text));
  return p;
}
// OAuth: start → open the URL → paste what comes back → finish.
function oauthFlow(d, m, out) {
  const w = el("div");
  // the card already states what this method is; only say what's NEW here
  const step = el("div","ap-note");
  step.textContent = m.status.active ? "Signed in." + (m.status.hint ? " " + m.status.hint : "") : "";
  step.style.display = step.textContent ? "" : "none";
  w.append(step);
  const acts = el("div","ap-acts");
  const paste = el("div","oauth-paste");
  const code = input("Paste the code or the whole redirect URL", false);
  const finish = btn("Finish sign-in", "pri", async () => {
    if (!code.value.trim()) { toast("paste the code first", true); code.focus(); return; }
    finish.disabled = true; finish.textContent = "Exchanging…";
    try {
      const r = await post("/api/auth/oauth/finish", { handle: paste.dataset.handle, code: code.value.trim() });
      out.innerHTML = "";
      if (r.ok !== false) {
        toast("✓ signed in with Claude");
        code.value = ""; paste.classList.remove("on");
        loadAuthFamilies(document.getElementById("auth-cards")); loadOverview();
      } else out.append(probeBox(false, r.message || errText(r)));
    } catch (e) { out.append(probeBox(false, e.message)); }
    finally { finish.disabled = false; finish.textContent = "Finish sign-in"; }
  });
  code.onkeydown = e => { if (e.key === "Enter") finish.click(); };
  paste.append(code, finish);
  const go = btn(m.status.active ? "Sign in again" : "Sign in with " + (d.label === "Claude" ? "Claude" : d.label), "pri", async () => {
    go.disabled = true; go.textContent = "Opening…";
    out.innerHTML = "";
    try {
      const r = await post("/api/auth/oauth/start", { family: m.family });
      if (!r.ok) { out.append(probeBox(false, errText(r))); return; }
      if (r.url) window.open(r.url, "_blank", "noopener");
      step.textContent = r.instructions || "A browser tab opened — approve there, then paste the code you get back.";
      step.style.display = "";
      paste.classList.add("on"); paste.dataset.handle = r.handle || "";
      setTimeout(() => code.focus(), 60);
      if (r.url) { const a = extLink("a-link", "Open the sign-in page again ↗", r.url); a.style.marginLeft = "10px"; acts.append(a); }
    } catch (e) { out.append(probeBox(false, e.message)); }
    finally { go.disabled = false; go.textContent = m.status.active ? "Sign in again" : "Sign in with Claude"; }
  });
  acts.append(go);
  if (m.status.configured) {
    const del = btn("Sign out", "gho dan", null);
    del.onclick = () => armDelete(del, async () => {
      try { await post("/api/auth/clear", { family: m.family, method: m.id }); toast("signed out");
        loadAuthFamilies(document.getElementById("auth-cards")); }
      catch (e) { toast(e.message, true); }
    });
    acts.append(del);
  }
  if (m.docs_url) { const dl = extLink("ac-doc", "↗", m.docs_url); dl.title = "Provider docs"; acts.append(dl); }
  w.append(acts, paste);
  return w;
}
// A locked model row lands here with the family's recommended method open.
function unlockFamily(uiFam) {
  showTab("models");
  const f = AUTH.families.find(x => x.ui_family === uiFam || x.family === uiFam);
  if (!f) { toast("no setup for that family yet", true); return; }
  AUTH.method[f.family] = f.recommended || f.active || null;
  const land = () => {
    const card = document.getElementById("auth-" + f.family);
    if (!card) return false;
    card.scrollIntoView({ behavior: "smooth", block: "center" });
    card.classList.add("flash"); setTimeout(() => card.classList.remove("flash"), 1400);
    const i = card.querySelector("input"); if (i) setTimeout(() => i.focus(), 340);
    return true;
  };
  if (!land()) setTimeout(land, 400);
}

// ---- models & hosting ----
// Context windows read as "200k", not "200000" — the unit people actually say.
const fmtCtx = (n) => n >= 1000000 ? (n/1000000).toFixed(n % 1000000 ? 1 : 0) + "m"
                    : n >= 1000 ? Math.round(n/1000) + "k" : String(n);
// The selected family tab on the Models page, mirrored in the hash as
// #models/claude so a refresh or a shared link lands on the same tab. The
// URL carries the name people say; the code carries the catalog's family id.
const TAB_SLUG = { anthropic: "claude", google: "gemini", xai: "grok", oss: "open" };
const TAB_FAM = Object.fromEntries(Object.entries(TAB_SLUG).map(([k, v]) => [v, k]));
const tabFromHash = () => { const [t, sub] = location.hash.slice(1).split("/"); return t === "models" && sub ? (TAB_FAM[sub] || sub) : "all"; };
let MODEL_TAB = tabFromHash();
let applyModelFilter = null;
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

  // Reachability is a per-provider action on the provider's own card below —
  // a page-level "test this route" strip said less and sat in the way.
  pageHead(pad, "My models", null, null);

  // Providers first: a model list means nothing until one is connected. This
  // is the ONLY way to connect a provider — every auth type each family
  // offers, not just an API key.
  MSTATE = m;
  const authSec = section(pad, "Providers");
  const authBox = el("div"); authBox.id = "auth-cards"; authSec.append(authBox);
  loadAuthFamilies(authBox);

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
    const sec = section(pad, "Choose a model", allModels.length + " across " + nFam + " famil" + (nFam===1?"y":"ies"));
    // Family tabs: one pill per family (plus Local for what Ollama has pulled),
    // each carrying its count. "All" keeps the grouped view; a family tab
    // narrows to that family and drops the group headers. The choice lives in
    // the hash (#models/claude) so a refresh or a pasted link keeps it.
    const TAB_LABEL = { oss: "Open models" };
    const tabs = [{ id: "all", label: "All", n: allModels.length }];
    famOrder.concat([...new Set(allModels.map(a => a.fam))].filter(f => !famOrder.includes(f))).forEach(fid => {
      const n = allModels.filter(a => a.fam === fid).length;
      if (n) tabs.push({ id: fid, label: TAB_LABEL[fid] || famLabel[fid] || fid, n });
    });
    const nLocal = allModels.filter(a => a.local).length;
    if (nLocal) tabs.push({ id: "local", label: "Local", n: nLocal });
    if (!tabs.some(t => t.id === MODEL_TAB)) MODEL_TAB = "all";
    const tabRow = el("div","mtabs");
    tabs.forEach(t => {
      const c = el("button","fchip" + (t.id === MODEL_TAB ? " on" : ""), t.label);
      c.append(el("span","tn2", String(t.n)));
      c.onclick = () => {
        MODEL_TAB = t.id;
        tabRow.querySelectorAll(".fchip").forEach(x => x.classList.toggle("on", x === c));
        const want = "models" + (t.id === "all" ? "" : "/" + (TAB_SLUG[t.id] || t.id));
        if (location.hash !== "#" + want) location.hash = want;
        apply();
      };
      tabRow.append(c);
    });
    sec.append(tabRow);
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
        if (a.local) row.dataset.local = "1";
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
        row.onclick = () => a.enabled ? useModel(a.model, a.backend) : unlockFamily(fid);
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
        const okT = MODEL_TAB === "all" || (MODEL_TAB === "local" ? !!r.dataset.local : r.dataset.fam === MODEL_TAB);
        const on = okQ && okF && okT;
        r.style.display = on ? "" : "none";
        r.classList.remove("kb");
        if (on) { shown++; perFam[r.dataset.fam] = (perFam[r.dataset.fam] || 0) + 1; }
      });
      // one family selected → the group header is noise; "All" keeps it
      list.querySelectorAll(".mfam").forEach(h => {
        h.style.display = (MODEL_TAB === "all" && perFam[h.dataset.fam]) ? "" : "none";
      });
      let e = list.querySelector(".find-none");
      if (!shown) { if (!e) { e = emptyState("search", "No model matches",
        "Try another family tab, clear the filter, or self-host something that isn't listed.");
        e.classList.add("find-none"); list.append(e); } }
      else if (e) e.remove();
    };
    applyModelFilter = apply;
    find.input.oninput = apply;
    apply();
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
  const oSec = section(pad, "Local models · Ollama", (oll.base_url || "").replace(/^https?:\/\//, ""));
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
  // self-host / custom endpoint — a first-class card in the same visual system
  const shSec = section(pad, "Or bring your own server");
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
function section(pad, title, filePath) {
  const s = el("div","sec");
  const h = el("div","sec-t"); h.append(document.createTextNode(title));
  if (filePath) { const f = el("span","fp", filePath); f.title = filePath; h.append(f); }
  s.append(h);
  pad.append(s);
  return s;
}
// ---- empty states -------------------------------------------------------
// Each one gets its own small drawing on the same 24-unit-grid discipline as
// the provider marks: currentColor for the structure, the accent token for
// the one live detail. They say what this place is FOR, so an empty page
// still teaches instead of shrugging.
const ART = {
  // an idle GPU card in its rack, power line dark until something runs
  deploy: '<svg viewBox="0 0 140 100" fill="none" aria-hidden="true">' +
    '<rect x="18" y="26" width="104" height="46" rx="7" stroke="currentColor" stroke-width="2" opacity=".28"/>' +
    '<rect x="28" y="36" width="60" height="26" rx="4" stroke="currentColor" stroke-width="2" opacity=".45"/>' +
    '<path d="M34 44h20M34 50h28M34 56h14" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".35"/>' +
    '<circle cx="103" cy="42" r="4" stroke="currentColor" stroke-width="2" opacity=".45"/>' +
    '<circle cx="103" cy="56" r="4" fill="var(--accent)" opacity=".9"/>' +
    '<path d="M18 49H4M122 49h14" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".3"/>' +
    '<path d="M52 72v10M88 72v10M40 82h60" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".22"/>' +
    '</svg>',
  // an empty socket waiting for a model to be plugged in
  socket: '<svg viewBox="0 0 140 100" fill="none" aria-hidden="true">' +
    '<rect x="40" y="24" width="60" height="52" rx="10" stroke="currentColor" stroke-width="2" opacity=".3"/>' +
    '<circle cx="58" cy="44" r="5" stroke="currentColor" stroke-width="2" opacity=".45"/>' +
    '<circle cx="82" cy="44" r="5" stroke="currentColor" stroke-width="2" opacity=".45"/>' +
    '<path d="M56 60h28" stroke="var(--accent)" stroke-width="2" stroke-linecap="round"/>' +
    '<path d="M40 50H16M124 50h-24" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-dasharray="4 5" opacity=".35"/>' +
    '</svg>',
  // a transcript: two turns and a cursor waiting for the first prompt
  session: '<svg viewBox="0 0 140 100" fill="none" aria-hidden="true">' +
    '<rect x="22" y="20" width="70" height="20" rx="7" stroke="currentColor" stroke-width="2" opacity=".3"/>' +
    '<rect x="48" y="48" width="70" height="20" rx="7" stroke="currentColor" stroke-width="2" opacity=".3"/>' +
    '<path d="M32 30h34M58 58h34" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".3"/>' +
    '<path d="M24 78h16" stroke="var(--accent)" stroke-width="2" stroke-linecap="round"/>' +
    '<path d="M46 74v8" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" opacity=".7"/>' +
    '</svg>',
  // a server handing tools across a dashed link
  mcp: '<svg viewBox="0 0 140 100" fill="none" aria-hidden="true">' +
    '<rect x="14" y="30" width="40" height="40" rx="8" stroke="currentColor" stroke-width="2" opacity=".32"/>' +
    '<path d="M24 42h20M24 50h20M24 58h12" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".32"/>' +
    '<path d="M54 50h32" stroke="var(--accent)" stroke-width="2" stroke-linecap="round" stroke-dasharray="5 5"/>' +
    '<circle cx="104" cy="36" r="9" stroke="currentColor" stroke-width="2" opacity=".4"/>' +
    '<circle cx="104" cy="64" r="9" stroke="currentColor" stroke-width="2" opacity=".4"/>' +
    '<path d="M86 50l10-9M86 50l10 9" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".4"/>' +
    '</svg>',
  // a playbook the agent opens when the task matches
  skill: '<svg viewBox="0 0 140 100" fill="none" aria-hidden="true">' +
    '<path d="M70 28c-8-6-18-8-28-6v46c10-2 20 0 28 6 8-6 18-8 28-6V22c-10-2-20 0-28 6z" stroke="currentColor" stroke-width="2" opacity=".32"/>' +
    '<path d="M70 28v46" stroke="currentColor" stroke-width="2" opacity=".32"/>' +
    '<path d="M52 42h10M52 52h10M78 42h10M78 52h10" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".3"/>' +
    '<path d="M70 78v8M62 84h16" stroke="var(--accent)" stroke-width="2" stroke-linecap="round"/>' +
    '</svg>',
  // a search with nothing under it
  search: '<svg viewBox="0 0 140 100" fill="none" aria-hidden="true">' +
    '<circle cx="64" cy="44" r="20" stroke="currentColor" stroke-width="2" opacity=".35"/>' +
    '<path d="M79 59l14 14" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".35"/>' +
    '<path d="M56 44h16" stroke="var(--accent)" stroke-width="2" stroke-linecap="round"/>' +
    '<path d="M34 84h72" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-dasharray="4 6" opacity=".25"/>' +
    '</svg>',
  // a quiet channel: a flat trace with one waiting pulse
  activity: '<svg viewBox="0 0 140 100" fill="none" aria-hidden="true">' +
    '<path d="M12 56h34l8-16 10 32 9-22 7 6h48" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity=".3"/>' +
    '<circle cx="70" cy="78" r="4" fill="var(--accent)" opacity=".85"/>' +
    '<path d="M12 78h44M84 78h44" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".2"/>' +
    '</svg>',
};
// icon · title · one line · optional action button
function emptyState(icon, title, line, action) {
  const z = el("div","zero");
  if (icon && ART[icon]) { const a = el("div","zart"); a.innerHTML = ART[icon]; z.append(a); }
  z.append(el("div","zt", title));
  if (line) z.append(el("div","zd", line));
  if (action) { const w = el("div","zact"); w.append(action); z.append(w); }
  return z;
}
function zero(title, detail) { return emptyState(null, title, detail); }
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
// ---- skills: a library, not a list -------------------------------------
// A skill is a SKILL.md the agent opens when a task matches. The page is a
// card grid: each card carries a generated identity glyph (a deterministic
// pattern from the name, so the grid is scannable), what the skill tells the
// agent, where it lives, and when it loads.
const SKILLS = { all: [], q: "", filter: "all", tools: [] };
const SKILL_FILTERS = [["all", "All"], ["global", "Global"], ["project", "This project"],
                       ["always", "Always loaded"], ["ondemand", "On demand"]];
// A tiny FNV-1a: same name → same glyph, on every machine and every reload.
function hashStr(t) {
  let h = 0x811c9dc5;
  for (let i = 0; i < t.length; i++) { h ^= t.charCodeAt(i); h = (h * 0x01000193) >>> 0; }
  return h >>> 0;
}
// The identity mark: a symmetric 4×4 of accent cells on the card's own grid.
// Symmetry is what stops it reading as noise — it looks drawn, not hashed.
function skillGlyph(name) {
  const h = hashStr(name || "?");
  const w = el("span","sglyph");
  const cells = [];
  for (let y = 0; y < 4; y++) {
    for (let x = 0; x < 2; x++) {
      const on = (h >> (y * 2 + x)) & 1;
      const strong = (h >> (8 + y * 2 + x)) & 1;
      if (!on) continue;
      // strong cells are solid squares, weak ones faint — the contrast is
      // what makes two glyphs tell apart at 24px
      const op = strong ? 1 : 0.3;
      const r = strong ? 1 : 2.4;
      cells.push('<rect x="' + (x * 6 + 1) + '" y="' + (y * 6 + 1) + '" width="4.8" height="4.8" rx="' + r + '" opacity="' + op + '"/>');
      cells.push('<rect x="' + ((3 - x) * 6 + 1) + '" y="' + (y * 6 + 1) + '" width="4.8" height="4.8" rx="' + r + '" opacity="' + op + '"/>');
    }
  }
  w.innerHTML = '<svg viewBox="0 0 24 24" fill="var(--accent)" aria-hidden="true">' + cells.join("") + "</svg>";
  w.style.background = "color-mix(in srgb, var(--accent) " + (8 + (h % 7)) + "%, transparent)";
  return w;
}
function skillMatches(sk) {
  const f = SKILLS.filter;
  if (f === "global" && sk.scope !== "global") return false;
  if (f === "project" && sk.scope !== "project") return false;
  if (f === "always" && !sk.always_load) return false;
  if (f === "ondemand" && sk.always_load) return false;
  const ql = SKILLS.q.trim().toLowerCase();
  return !ql || ql.split(/\s+/).every(t =>
    (sk.name + " " + (sk.description || "") + " " + (sk.category || "") + " " + (sk.tools || []).join(" ") + " " + sk.scope)
      .toLowerCase().includes(t));
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
  SKILLS.all = [...(sk.global || []), ...(sk.project || [])];
  SKILLS.tools = sk.tools_seen || [];
  SKILLS.dirs = { global: sk.global_dir, project: sk.project_dir };
  const c = sk.counts || { total: SKILLS.all.length, always: 0, on_demand: 0, global: 0, project: 0 };

  pageHead(pad, "Skills", c.total || null, "Playbooks the agent opens when a task matches.",
    [btn("New skill", "pri", () => openSkillEditor(null, "global", reload))]);
  const pills = el("div","sk-state");
  pills.append(pill(c.always, " always loaded", c.always ? "acc" : ""));
  pills.append(pill(c.on_demand, " on demand"));
  pills.append(pill(c.global, " global"));
  pills.append(pill(c.project, " from this repo", c.project ? "blu" : ""));
  pad.append(pills);

  if (!c.total) {
    pad.append(emptyState("skill", "No skills yet",
      "A skill is a SKILL.md the agent reads when the task matches — your deploy steps, your review rules.",
      btn("New skill", "pri", () => openSkillEditor(null, "global", reload))));
    return;
  }
  const bar = el("div","filters");
  const find = findBox("Search skills — name, description, tool…  ( / )");
  find.wrap.style.marginBottom = "0"; find.wrap.style.flex = "1"; find.input.value = SKILLS.q;
  find.input.oninput = () => { SKILLS.q = find.input.value; paint(); };
  bar.append(find.wrap);
  const chips = el("div","fchips");
  SKILL_FILTERS.forEach(([k, lab]) => {
    const ch = el("button","fchip" + (k === SKILLS.filter ? " on" : ""), lab);
    ch.onclick = () => { SKILLS.filter = k; chips.querySelectorAll(".fchip").forEach(x => x.classList.toggle("on", x === ch)); paint(); };
    chips.append(ch);
  });
  bar.append(chips); pad.append(bar);
  const grid = el("div","sk-grid"); pad.append(grid);
  const paint = () => {
    const rows = SKILLS.all.filter(skillMatches);
    if (!rows.length) {
      grid.innerHTML = "";
      grid.append(emptyState("search", "No skill matches", "Try another filter, or a shorter search."));
      return;
    }
    grid.querySelectorAll(".zero").forEach(x => x.remove());
    patchList(grid, rows, x => x.scope + "/" + x.slug,
      x => [x.name, x.description, x.always_load, (x.tools || []).join(","), x.path],
      (card, x) => {
        card = card || el("div"); card.innerHTML = ""; card.className = "skcard";
        const h = el("div","sk-h");
        h.append(skillGlyph(x.name));
        const t = el("div","sk-t");
        t.append(el("div","sk-n", x.name));
        t.append(el("div","sk-p", x.path));
        h.append(t);
        card.append(h);
        card.append(el("div","sk-d", x.description || "(no description)"));
        const tags = el("div","sk-tags");
        tags.append(tag2(x.scope === "project" ? "This project" : "Global", x.scope === "project" ? "blu" : ""));
        tags.append(tag2(x.always_load ? "Always loaded" : "On demand", x.always_load ? "vio" : ""));
        if (x.category) tags.append(tag2(x.category, ""));
        card.append(tags);
        if ((x.tools || []).length) {
          const tl = el("div","sk-tools");
          x.tools.slice(0, 5).forEach(tool => tl.append(el("span","chip", tool)));
          if (x.tools.length > 5) tl.append(el("span","chip more", "+" + (x.tools.length - 5)));
          card.append(tl);
        }
        const acts = el("div","sk-acts");
        acts.append(btn("Edit", "gho", e => { e.stopPropagation(); openSkillEditor(x, x.scope, reload); }));
        const del = btn("Delete", "gho dan", null);
        del.onclick = e => { e.stopPropagation(); armDelete(del, () => deleteSkill(x, reload)); };
        acts.append(del);
        card.append(acts);
        card.onclick = () => openSkillSheet(x, reload);
        return card;
      });
  };
  paint();
}
async function deleteSkill(sk, reload) {
  try {
    const r = await post("/api/skill/delete", { scope: sk.scope, slug: sk.slug });
    if (r.ok) { toast("deleted " + sk.name); hideModal(); reload(); } else toast(r.error || "failed", true);
  } catch (e) { toast(e.message, true); }
}
// The detail sheet: what the agent is told, then the raw file behind a toggle.
function openSkillSheet(sk, reload) {
  const s = document.getElementById("sheet"); s.innerHTML = "";
  const h = el("div","cs-h");
  h.append(skillGlyph(sk.name));
  const ht = el("div","ft");
  ht.append(el("div","fn", sk.name));
  ht.append(el("div","fd", sk.path));
  h.append(ht);
  s.append(h);
  const tags = el("div","sk-tags"); tags.style.marginBottom = "12px";
  tags.append(tag2(sk.scope === "project" ? "This project" : "Global", sk.scope === "project" ? "blu" : ""));
  tags.append(tag2(sk.always_load ? "Always loaded" : "On demand", sk.always_load ? "vio" : ""));
  if (sk.category) tags.append(tag2(sk.category, ""));
  (sk.tools || []).forEach(t => tags.append(el("span","chip", t)));
  s.append(tags);
  const dl = el("dl","kvs");
  kvRow(dl, "description", sk.description || "—", false);
  kvRow(dl, "loading", sk.always_load ? "Injected into every session" : "Opened when the task matches", false);
  if ((sk.tools || []).length) kvRow(dl, "tools", sk.tools.join(", "));
  s.append(dl);
  const bodyBox = el("div","sk-body md");
  bodyBox.innerHTML = md(sk.body || "*(empty)*");
  s.append(bodyBox);
  const det = el("details","cs-guide");
  det.append(el("summary", null, "Source"));
  const pre = el("pre","sk-raw"); pre.textContent = sk.raw || sk.body || "";
  const gb = el("div","cs-gb"); gb.append(pre); det.append(gb);
  s.append(det);
  const foot = el("div","cs-foot");
  foot.append(btn("Edit", "pri", () => openSkillEditor(sk, sk.scope, reload)));
  const del = btn("Delete", "gho dan", null);
  del.onclick = () => armDelete(del, () => deleteSkill(sk, reload));
  foot.append(del, btn("Close", "gho", hideModal));
  s.append(foot);
  showModal(true);
  trapFocus(s.parentElement);
}
const SKILL_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task"];
const slugify = t => (t || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
// Create / edit: the fields, then the body with a live preview beside it.
function openSkillEditor(sk, scope, reload) {
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(el("h3", null, sk ? "Edit skill" : "New skill"));
  const pathLine = el("div","sub");
  s.append(pathLine);
  const scopeSel = el("select","in");
  [["global", "Global · every project"], ["project", "This project · travels with the repo"]].forEach(([v, l]) => {
    const o = el("option", null, l); o.value = v; scopeSel.append(o);
  });
  scopeSel.value = sk ? sk.scope : scope || "global";
  const name = input("Deploy checklist"); name.value = sk ? sk.name : "";
  const desc = input("One line — when should the agent reach for this?");
  desc.value = sk ? (sk.description || "") : "";
  const cat = input("Category (optional)"); cat.value = sk ? (sk.category || "") : "";
  const always = document.createElement("input"); always.type = "checkbox";
  always.checked = !!(sk && sk.always_load);
  const alwaysL = el("label","chk"); alwaysL.append(always, document.createTextNode("Always load into every session"));
  const nameErr = el("div","sk-err");
  const showPath = () => {
    const sl = sk ? sk.slug : slugify(name.value);
    const dir = (SKILLS.dirs || {})[scopeSel.value] || (scopeSel.value === "project" ? ".mantis/skills" : "~/.mantis-agent/skills");
    pathLine.textContent = sl ? dir + "/" + sl + "/SKILL.md" : dir + "/…/SKILL.md";
    const bad = !sk && name.value.trim() && !sl;
    nameErr.textContent = bad ? "Use letters, numbers, spaces or dashes — that name has no slug." : "";
    return !bad;
  };
  name.oninput = showPath; scopeSel.onchange = showPath;
  const f1 = el("div","sk-frow"); f1.append(fieldWrap("Name", name), fieldWrap("Scope", scopeSel));
  const f2 = el("div","sk-frow"); f2.append(fieldWrap("Description", desc), fieldWrap("Category", cat));
  s.append(f1, nameErr, f2);
  const toolSet = new Set((sk && sk.tools) || []);
  const tools = el("div","sk-tsel");
  [...new Set([...SKILL_TOOLS, ...(SKILLS.tools || [])])].forEach(t => {
    const c = el("button","fchip" + (toolSet.has(t) ? " on" : ""), t);
    c.onclick = () => { if (toolSet.has(t)) toolSet.delete(t); else toolSet.add(t); c.classList.toggle("on"); };
    tools.append(c);
  });
  s.append(fieldWrap("Allowed tools", tools));
  const ta = el("textarea","in sk-ed");
  ta.placeholder = "The how-to the agent reads. Markdown: steps, commands, gotchas.";
  ta.value = sk ? (sk.body || "") : "";
  const prev = el("div","sk-prev md");
  const draw = () => { prev.innerHTML = md(ta.value || "*Nothing yet — the preview shows what the agent will read.*"); };
  ta.oninput = draw; draw();
  const split = el("div","sk-split"); split.append(ta, prev);
  s.append(fieldWrap("Body", split));
  const foot = el("div","cs-foot");
  const save = btn(sk ? "Save changes" : "Create skill", "pri", async () => {
    if (!name.value.trim()) { toast("name required", true); name.focus(); return; }
    if (!showPath()) { name.focus(); return; }
    save.disabled = true;
    try {
      const r = await post("/api/skill", { scope: scopeSel.value, name: name.value, description: desc.value,
        body: ta.value, category: cat.value, always_load: always.checked,
        tools: [...toolSet], slug: sk ? sk.slug : undefined });
      if (r.ok) { toast(sk ? "saved " + name.value.trim() : "created " + name.value.trim()); hideModal(); reload(); }
      else toast(r.error || "failed", true);
    } catch (e) { toast(e.message, true); } finally { save.disabled = false; }
  });
  foot.append(save, alwaysL, btn("Cancel", "gho", hideModal));
  s.append(foot);
  showPath();
  showModal(true);
  trapFocus(s.parentElement);
  setTimeout(() => (sk ? ta : name).focus(), 60);
}
function fieldWrap(label, node) {
  const w = el("label","dp-field");
  w.append(el("span","kh-l", label), node);
  return w;
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
  const stdioN = mc.servers.filter(s => s.transport === "stdio").length;
  pageHead(pad, "MCP servers", mc.servers.length, mc.servers.length
    ? stdioN + " local · " + (mc.servers.length - stdioN) + " remote" : null, [addBtn]);
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
    pad.append(emptyState("mcp", "No MCP servers configured",
      "Add one to give the agent tools it doesn't ship with — GitHub, a database, your API.",
      btn("Add a server", "pri", () => { composer.classList.add("on"); composer.querySelector("textarea").focus(); })));
    return;
  }

  const find = findBox("Filter servers — name, command, url…");
  if (mc.servers.length > 5) pad.append(find.wrap);

  const byScope = { global: [], project: [], settings: [] };
  mc.servers.forEach(sv => (byScope[sv.scope] || (byScope[sv.scope] = [])).push(sv));
  const files = { global: mc.global_file, project: mc.project_file, settings: "settings.json" };
  const labels = { global: "Global · every project", project: "Project · this repo",
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

  const lc = (src) => Object.keys((c.layers || {})[src] || {}).length;
  pageHead(pad, "Config", keys.length, "user " + lc("user") + " · project " + lc("project") + " · local " + lc("local") + " · secrets redacted");
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
  // activity rows use it to land in a job's session; it also makes a
  // session URL something you can paste to the other device on the LAN.
  const qs = new URLSearchParams(location.search);
  const t = location.hash.slice(1);
  if (qs.get("session") && qs.get("cwd")) {
    // land with the project and session cards selected, not just the transcript
    jumpToSession(qs.get("cwd"), qs.get("session"));
  }
  else if (["sessions","activity","models","deploy","skills","mcp","config"].includes(t.split("/")[0])) showTab(t.split("/")[0]);
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
