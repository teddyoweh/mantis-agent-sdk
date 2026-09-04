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
     Everything on these pages mirrors a file on this machine, so the design
     leans on the product's own materials: monospace as the DISPLAY face (the
     nouns here are `mcp.json`, `npx -y …`, `claude-opus-5`), an olive-cast
     paper/ink palette taken from the mantis mark rather than neutral grey,
     and one signature device — the signal path: a live wiring diagram of what
     the agent is actually plugged into, drawn at the top of every page.
     ========================================================================== */
  :root {
    --bg: #efece5; --panel: #ffffff; --panel-2: #f7f5f0; --fill: #e8e4da; --line: #e4e0d6;
    --ink: #14160f; --ink-2: #5c6055; --ink-3: #8b8f82;
    --accent: #3e6b24; --accent-soft: #e6eeda;
    --caution: #a8720f; --caution-soft: #f7ebd5;
    --user: #2d5fa8; --tool: #6b4fb0; --err: #b23f2b;
    --shadow: 0 1px 2px rgba(24,26,16,.05), 0 8px 24px rgba(24,26,16,.06);
    --radius: 12px; --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0d0f0a; --panel: #1a1d15; --panel-2: #15180f; --fill: #242819; --line: #22261a;
      --ink: #e9ece1; --ink-2: #9ba091; --ink-3: #6b7062;
      --accent: #a6d96a; --accent-soft: #1d2614;
      --caution: #e0a94e; --caution-soft: #2c2413;
      --user: #7db0f2; --tool: #b79cf0; --err: #e8756a;
      --shadow: 0 1px 2px rgba(0,0,0,.32), 0 8px 24px rgba(0,0,0,.24);
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: var(--sans); background: var(--bg); color: var(--ink);
    font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased;
    display: grid; grid-template-columns: 214px 1fr; height: 100vh; overflow: hidden;
  }
  a { color: var(--accent); text-decoration: none; }
  :focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

  /* left rail — nav plus a live readout of what the agent is wired to */
  #rail { background: var(--panel-2);
    display: flex; flex-direction: column; padding: 15px 11px 11px; overflow: hidden; }
  .mark { display: flex; align-items: center; gap: 9px; padding: 2px 7px 17px; }
  .mark img { width: 25px; height: 25px; display: block; }
  .mark span { font-family: var(--mono); font-weight: 700; font-size: 14px; letter-spacing: -.03em; }
  #nav { display: flex; flex-direction: column; gap: 1px; }
  #nav button { display: flex; align-items: center; width: 100%; text-align: left; font: inherit;
    font-family: var(--mono); font-size: 12.5px; letter-spacing: -.01em; padding: 8px 10px;
    border: 0; border-radius: 8px; background: transparent; color: var(--ink-2); cursor: pointer;
    transition: background .12s, color .12s; }
  #nav button .k { margin-left: auto; font-size: 10.5px; color: var(--ink-3); opacity: 0; }
  #nav button:hover { background: var(--fill); color: var(--ink); }
  #nav button:hover .k { opacity: 1; }
  #nav button.on { background: var(--fill); color: var(--ink); font-weight: 700; }
  #nav button.on:hover .k { opacity: 1; color: var(--accent); }
  .railfoot { margin-top: auto; padding: 15px 10px 4px; }
  .rf-l { font-family: var(--mono); font-size: 9.5px; letter-spacing: .14em; text-transform: uppercase;
    color: var(--ink-3); }
  .rf-v { font-family: var(--mono); font-size: 12.5px; font-weight: 700; letter-spacing: -.02em;
    margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .rf-s { font-size: 11.5px; color: var(--ink-2); display: flex; align-items: center; gap: 6px;
    margin-top: 2px; }
  .rf-c { display: flex; flex-wrap: wrap; gap: 4px 12px; margin-top: 12px; font-size: 11.5px;
    color: var(--ink-3); }
  .rf-c span { cursor: pointer; white-space: nowrap; }
  .rf-c span:hover { color: var(--ink); }
  .rf-c b { font-family: var(--mono); font-weight: 700; color: var(--ink-2); }
  .rf-c span:hover b { color: var(--accent); }
  /* a live dot that breathes, so "connected" reads as present tense */
  .live { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); flex: none;
    animation: pulse 2.6s ease-in-out infinite; }
  .live.off { background: var(--ink-3); animation: none; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }

  main { overflow: hidden; }
  .view { display: none; height: 100%; }
  .view.on { display: block; }

  @media (prefers-reduced-motion: reduce) {
    * { animation-duration: .001ms !important; transition-duration: .001ms !important; }
  }

  /* sessions: three panes */
  #sessions.on { display: grid; grid-template-columns: 256px 336px 1fr; height: 100%; }
  .col { overflow-y: auto; height: 100%; background: var(--panel-2); }
  .col:last-child { background: var(--bg); }
  .col-head {
    position: sticky; top: 0; background: inherit; z-index: 1;
    padding: 14px 16px 9px; font-family: var(--mono); font-size: 10px; letter-spacing: .13em;
    text-transform: uppercase; color: var(--ink-3);
  }
  .row { padding: 10px 12px; cursor: pointer; border-radius: 9px; margin: 0 8px 2px; }
  .row:hover { background: var(--fill); }
  .row.on { background: var(--accent-soft); }
  .row .t { font-weight: 600; font-size: 13px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .row .s { color: var(--ink-2); font-size: 12px; margin-top: 2px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .row .m { color: var(--ink-3); font-size: 11px; margin-top: 3px;
    display: flex; gap: 8px; }
  .path { font-family: var(--mono); font-size: 11px; }

  /* transcript */
  #transcript { padding: 22px 26px; max-width: 900px; margin: 0 auto; }
  .conv-head { margin-bottom: 18px; }
  .conv-head h2 { font-size: 17px; margin: 0 0 4px; }
  .conv-head .sub { color: var(--ink-2); font-size: 12px; }
  .msg { margin: 0 0 20px; }
  .who { font-size: 11px; letter-spacing: .06em; text-transform: uppercase;
    color: var(--ink-3); margin-bottom: 6px; font-weight: 600; }
  .msg.user .who { color: var(--user); }
  .msg.assistant .who { color: var(--accent); }
  .text { white-space: pre-wrap; word-wrap: break-word; }
  .thinking { box-shadow: inset 2px 0 0 var(--fill); padding: 2px 0 2px 12px;
    color: var(--ink-2); font-style: italic; white-space: pre-wrap; margin: 8px 0; }
  .block { border-radius: 9px; margin: 8px 0; background: var(--panel-2); overflow: hidden; }
  .block .bh { padding: 7px 12px; font-family: var(--mono); font-size: 12px;
    display: flex; gap: 8px; align-items: center; }
  .block.tool .bh { color: var(--tool); }
  .block.result .bh { color: var(--ink-2); }
  .block.result.err .bh { color: var(--err); }
  .block pre { margin: 0; padding: 10px 12px; overflow-x: auto; font-family: var(--mono);
    font-size: 12px; white-space: pre-wrap; word-break: break-word; max-height: 340px; }
  .badge { font-size: 10px; padding: 1px 6px; border-radius: 20px;
    background: var(--fill); color: var(--ink-3); font-family: var(--mono); }

  /* models + config */
  .pad { padding: 22px 26px; overflow-y: auto; height: 100%; }
  .pad h2 { font-size: 13px; letter-spacing: .06em; text-transform: uppercase;
    color: var(--ink-3); margin: 0 0 14px; }
  .now { display: flex; gap: 10px; align-items: center; margin-bottom: 22px;
    padding: 14px 16px; border-radius: var(--radius);
    background: var(--panel); box-shadow: var(--shadow); }
  .now .k { font-size: 11px; color: var(--ink-3); text-transform: uppercase; letter-spacing: .06em; }
  .now .v { font-family: var(--mono); font-size: 14px; font-weight: 600; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(288px, 1fr)); gap: 14px; }
  .card { border-radius: 14px; background: var(--panel); box-shadow: var(--shadow);
    padding: 17px 18px; display: flex; flex-direction: column; gap: 12px; }
  .card .head { display: flex; align-items: center; gap: 8px; }
  .card .name { font-weight: 700; font-size: 14.5px; letter-spacing: -.01em; }
  .status { margin-left: auto; font-size: 11px; font-weight: 600; letter-spacing: .01em;
    display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 20px;
    white-space: nowrap; }
  .status.on { background: var(--accent-soft); color: var(--accent); }
  .status.off { color: var(--ink-3); background: var(--fill); }
  .status .d { width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
  .card .url { font-family: var(--mono); font-size: 11px; color: var(--ink-3);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-top: -4px; }
  .card .note { font-size: 12px; color: var(--ink-3); line-height: 1.45; }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { font-family: var(--mono); font-size: 11px; padding: 4px 9px; border-radius: 7px;
    background: var(--fill); color: var(--ink-2); }
  .chip.cur { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
  .chip.more { color: var(--ink-3); }
  .chip.clk { cursor: pointer; transition: background .12s, color .12s, box-shadow .12s; }
  .chip.clk:hover { background: var(--accent-soft); color: var(--accent);
    box-shadow: inset 0 0 0 1px var(--accent); }
  .recent { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 26px; }
  details.layer { border-radius: var(--radius); box-shadow: var(--shadow);
    margin-bottom: 10px; background: var(--panel); }
  details.layer summary { padding: 12px 16px; cursor: pointer; font-weight: 600; }
  details.layer pre { margin: 0; padding: 0 16px 14px; font-family: var(--mono); font-size: 12px;
    white-space: pre-wrap; word-break: break-word; }

  /* hosting banner */
  .host { display: flex; align-items: center; gap: 14px; flex-wrap: wrap;
    padding: 17px 19px; border-radius: var(--radius);
    background: var(--panel); box-shadow: var(--shadow); margin-bottom: 24px; }
  .host .kbadge { font-size: 11px; font-weight: 700; letter-spacing: .04em;
    text-transform: uppercase; padding: 4px 10px; border-radius: 20px; }
  .kbadge.selfhost { background: #f0e9ff; color: #6b3fd0; }
  .kbadge.provider { background: var(--accent-soft); color: var(--accent); }
  .kbadge.local { background: #e6f3f8; color: #2277a0; }
  .kbadge.default { background: var(--line); color: var(--ink-2); }
  @media (prefers-color-scheme: dark) {
    .kbadge.selfhost { background: #2a2140; color: #b79cf0; }
    .kbadge.local { background: #16303a; color: #74c0e0; }
  }
  .host .hm { font-family: var(--mono); font-weight: 700; font-size: 15px; }
  .host .hb { font-family: var(--mono); font-size: 12px; color: var(--ink-2);
    margin-left: auto; word-break: break-all; }

  /* hero — "any model, any provider, any self-host" */
  .hero { padding: 20px 22px; border-radius: var(--radius); background: var(--panel);
    box-shadow: var(--shadow); margin-bottom: 24px; }
  .hero-t { font-size: 21px; font-weight: 800; letter-spacing: -.02em; margin: 0 0 5px; }
  .hero-s { font-size: 12.5px; color: var(--ink-2); line-height: 1.5; max-width: 62ch; }
  .hero-now { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .hero-lbl { font-size: 10px; text-transform: uppercase; letter-spacing: .09em; color: var(--ink-3); font-weight: 700; }
  .hero-m { font-family: var(--mono); font-weight: 700; font-size: 15px; }
  .hero-via { font-size: 12px; color: var(--ink-2); }
  .hero .hb { font-family: var(--mono); font-size: 12px; color: var(--ink-3);
    margin-left: auto; word-break: break-all; }

  /* browse all models — one flat, searchable list */
  .browse-head { display: flex; align-items: baseline; gap: 11px; margin-bottom: 12px; }
  .browse-head h2 { margin: 0; }
  .cnt2 { font-size: 11.5px; color: var(--ink-2); font-variant-numeric: tabular-nums;
    background: var(--fill); padding: 1px 9px; border-radius: 20px; }
  input.in.search { width: 100%; margin-bottom: 12px; padding: 11px 13px; font-family: var(--sans); font-size: 13px; }
  .browse { border-radius: var(--radius); overflow: hidden auto; max-height: 340px;
    box-shadow: var(--shadow); background: var(--panel); margin-bottom: 26px; }
  .brow { display: flex; align-items: center; gap: 12px; padding: 9px 13px; cursor: pointer;
    border-radius: 8px; margin: 1px 4px; }
  .brow:hover { background: var(--panel-2); }
  .brow.cur { background: var(--accent-soft); }
  .brow .bm { font-family: var(--mono); font-size: 12.5px; color: var(--ink);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .brow.cur .bm { color: var(--accent); font-weight: 600; }
  .brow .bp { font-size: 11.5px; color: var(--ink-3); white-space: nowrap; }
  .brow .bx { font-size: 11px; color: var(--ink-3); white-space: nowrap; min-width: 62px; text-align: right; }
  .brow:hover .bx { color: var(--accent); }
  .brow.locked { cursor: pointer; }
  .brow.locked .bm { color: var(--ink-2); }
  .browse-empty { padding: 14px 15px; font-size: 12.5px; color: var(--ink-3); font-style: italic; }
  .selfhost-card { gap: 10px; margin-bottom: 24px; }
  .selfhost-card .fields { display: flex; flex-direction: column; gap: 8px; }
  .selfhost-card .fields .r { display: flex; gap: 8px; }
  .card.flash, .lrow.flash, .msg.flash { box-shadow: 0 0 0 2px var(--accent); transition: box-shadow .3s;
    border-radius: 9px; }

  /* key status (enabled provider) */
  .keyline { display: flex; align-items: center; gap: 8px; font-family: var(--mono);
    font-size: 12px; }
  .keyline .env { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .keyline .val { color: var(--ink); font-weight: 600; }
  .src { font-size: 9px; text-transform: uppercase; letter-spacing: .05em; font-weight: 700;
    padding: 2px 6px; border-radius: 4px; font-family: var(--sans); }
  .src.env { background: #e6f3f8; color: #2277a0; }
  .src.saved { background: var(--accent-soft); color: var(--accent); }
  @media (prefers-color-scheme: dark) { .src.env { background: #16303a; color: #74c0e0; } }
  .card.cur { box-shadow: var(--shadow), inset 0 0 0 1.5px var(--accent); }
  .actions { display: flex; gap: 16px; }
  .actions button { background: none; border: 0; padding: 0; font: inherit; font-size: 12px;
    cursor: pointer; color: var(--ink-2); }
  .actions button:hover { color: var(--ink); }
  .actions button.danger:hover { color: var(--err); }

  /* config table */
  .cfg { border-radius: var(--radius); overflow: hidden; box-shadow: var(--shadow);
    background: var(--panel); margin-bottom: 24px; padding: 5px; }
  .cfg .kv { display: grid; grid-template-columns: 200px 1fr; gap: 12px; padding: 10px 16px;
    border-radius: 8px; }
  .cfg .kv:nth-child(odd) { background: var(--panel-2); }
  .cfg .kv .ck { font-family: var(--mono); font-size: 12px; color: var(--ink-2); }
  .cfg .kv .cv { font-family: var(--mono); font-size: 12px; white-space: pre-wrap;
    word-break: break-word; }
  .layerpath { font-family: var(--mono); font-size: 11px; color: var(--ink-3);
    padding: 0 16px 10px; }
  .note-sec { font-size: 12px; color: var(--ink-3); margin: -14px 0 20px; }

  /* forms / actions */
  .enable { display: flex; gap: 8px; }
  input.in, textarea.in { flex: 1; min-width: 0; font: inherit; font-family: var(--mono); font-size: 12px;
    padding: 9px 11px; border: 0; border-radius: 8px;
    background: var(--fill); color: var(--ink); }
  input.in:focus, textarea.in:focus { outline: none; box-shadow: 0 0 0 2px var(--accent-soft), 0 0 0 1px var(--accent); }
  input.in::placeholder, textarea.in::placeholder { color: var(--ink-3); }
  .btn { font: inherit; font-size: 12px; font-weight: 600; padding: 8px 15px; border: 0;
    border-radius: 7px; background: var(--accent); color: #fff; cursor: pointer; white-space: nowrap; }
  @media (prefers-color-scheme: dark) { .btn { color: #10220a; } }
  .btn:hover { filter: brightness(1.06); }
  .btn:disabled { opacity: .5; cursor: default; }
  .selfhost { border-radius: var(--radius); padding: 17px 19px;
    margin-bottom: 24px; background: var(--accent-soft); }
  .selfhost h3 { margin: 0 0 4px; font-size: 14px; }
  .selfhost .fields { display: flex; flex-direction: column; gap: 8px; margin-top: 12px; }
  .selfhost .fields .r { display: flex; gap: 8px; }
  .selfhost input.in { background: var(--panel); }
  .guide-link { font-size: 12px; color: var(--accent); cursor: pointer; background: none;
    border: 0; padding: 0; text-align: left; font: inherit; font-size: 12px; }
  .guide-link:hover { text-decoration: underline; }

  /* guide modal */
  #modal { position: fixed; inset: 0; background: rgba(0,0,0,.55); display: none;
    align-items: center; justify-content: center; padding: 20px; z-index: 30; }
  #modal.on { display: flex; }
  .sheet { position: relative; background: var(--panel);
    border-radius: 18px; max-width: 540px; width: 100%; max-height: 86vh; overflow-y: auto;
    padding: 26px 28px; box-shadow: 0 24px 70px rgba(0,0,0,.4); }
  .sheet.wide { max-width: 780px; }
  .sheet h3 { margin: 0 0 3px; font-size: 19px; letter-spacing: -.01em; }
  .sheet h4 { margin: 18px 0 6px; font-size: 12px; text-transform: uppercase;
    letter-spacing: .06em; color: var(--ink-3); }
  .sheet .sub { color: var(--ink-2); font-size: 12px; font-family: var(--mono); margin-bottom: 18px; }
  .sheet ol { margin: 0; padding-left: 20px; }
  .sheet ol li { margin: 8px 0; font-size: 13.5px; line-height: 1.5; }
  .sheet .free { font-size: 12.5px; color: var(--ink-2); background: var(--fill);
    border-radius: 9px; padding: 11px 13px; margin: 16px 0; }
  .sheet .cta { display: flex; gap: 14px; flex-wrap: wrap; align-items: center; margin-top: 18px; }
  .sheet .notes { list-style: none; padding: 0; margin: 12px 0 0; }
  .sheet .notes li { font-size: 12.5px; color: var(--ink-2); padding: 4px 0 4px 16px;
    position: relative; }
  .sheet .notes li::before { content: "·"; position: absolute; left: 4px; color: var(--accent); }
  .rt { background: var(--panel-2); border-radius: 10px; padding: 12px 14px; margin: 9px 0; }
  .rt .rn { font-weight: 700; font-size: 13px; }
  .rt .rnote { color: var(--ink-3); font-size: 12px; margin: 2px 0; }
  .rt code { display: block; font-family: var(--mono); font-size: 11.5px; background: var(--fill);
    padding: 7px 9px; border-radius: 6px; overflow-x: auto; white-space: pre; margin: 6px 0 2px; }
  .btn.big { padding: 10px 18px; font-size: 13px; text-decoration: none; display: inline-block; }
  .a-link { color: var(--accent); font-size: 12.5px; text-decoration: none; }
  .a-link:hover { text-decoration: underline; }
  .skill-box { background: var(--accent-soft); border-radius: 11px; padding: 14px 16px; margin: 18px 0; }
  .skill-t { font-weight: 700; font-size: 13px; margin-bottom: 5px; }
  .skill-b { font-size: 12.5px; color: var(--ink-2); line-height: 1.5; margin-bottom: 12px; }
  .plat { background: var(--panel-2); border-radius: 10px; padding: 12px 14px; margin: 8px 0; }
  .plat-top { display: flex; align-items: center; gap: 9px; }
  .plat-n { font-weight: 700; font-size: 13px; }
  .plat-k { font-size: 10px; color: var(--ink-3); background: var(--fill); padding: 2px 8px; border-radius: 20px; }
  .plat-links { display: flex; gap: 16px; margin-top: 8px; }
  .sheet .x { position: absolute; top: 14px; right: 16px; background: none; border: 0;
    font-size: 20px; color: var(--ink-3); cursor: pointer; line-height: 1; }
  .sheet .x:hover { color: var(--ink); }

  #toast { position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%);
    background: var(--ink); color: var(--bg); padding: 11px 20px; border-radius: 9px;
    font-size: 13px; opacity: 0; transition: opacity .18s; pointer-events: none; z-index: 20;
    max-width: 80vw; box-shadow: 0 6px 24px rgba(0,0,0,.25); }
  #toast.on { opacity: 1; }
  #toast.err { background: var(--err); color: #fff; }

  /* ==========================================================================
     ACTIVITY — the record of you working with an agent on this machine.
     One hero (the trace), one honest answer to "when do I do this" (the
     punchcard), one to "what does it actually do" (the tool spectrum). The
     six-panel grid this replaced showed the same numbers four times over.
     ========================================================================== */
  .lcd { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 26px;
    font-family: var(--mono); font-size: 12.5px; color: var(--ink-3); margin: 0 0 22px; }
  .lcd i { font-style: normal; color: var(--ink); font-weight: 700; font-size: 15px;
    letter-spacing: -.03em; font-variant-numeric: tabular-nums; margin-right: 6px; }
  .lcd .hot i { color: var(--accent); }

  .trace { position: relative; background: var(--panel); border-radius: var(--radius);
    box-shadow: var(--shadow); padding: 16px 18px 10px; margin-bottom: 14px; }
  .trace-h { display: flex; align-items: baseline; gap: 12px; margin-bottom: 4px; }
  .trace-t { font-family: var(--mono); font-size: 10.5px; font-weight: 700; letter-spacing: .13em;
    text-transform: uppercase; color: var(--ink-3); }
  .trace-pk { margin-left: auto; font-family: var(--mono); font-size: 11.5px; color: var(--ink-2); }
  .trace-pk b { color: var(--ink); }
  .trace svg { display: block; width: 100%; height: auto; }
  .trace .env { fill: var(--accent); opacity: .1; }
  .trace .sig { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round;
    stroke-linecap: round; vector-effect: non-scaling-stroke; }
  .trace .raw { fill: none; stroke: var(--accent); stroke-width: 1; opacity: .34;
    vector-effect: non-scaling-stroke; }
  .trace .pk { stroke: var(--ink-3); stroke-width: 1; stroke-dasharray: 2 3; }
  .trace .pkd { fill: var(--accent); }
  .trace .base { stroke: var(--fill); stroke-width: 1; }
  .trace text { fill: var(--ink-3); font-family: var(--mono); font-size: 9px; }
  .trace .lbl { fill: var(--ink-2); font-weight: 700; }
  @media (prefers-reduced-motion: no-preference) {
    .trace .sig { animation: draw 1.15s cubic-bezier(.22,.7,.2,1) forwards; }
    .trace .env, .trace .raw { animation: fadein .8s .35s both ease-out; }
    @keyframes draw { to { stroke-dashoffset: 0; } }
    @keyframes fadein { from { opacity: 0; } }
  }

  .duo { display: grid; grid-template-columns: 1.15fr 1fr; gap: 14px; }
  @media (max-width: 980px) { .duo { grid-template-columns: 1fr; } }
  .card2 { background: var(--panel); border-radius: var(--radius); box-shadow: var(--shadow);
    padding: 16px 18px 18px; }
  .card2 h3 { font-family: var(--mono); font-size: 10.5px; font-weight: 700; letter-spacing: .13em;
    text-transform: uppercase; color: var(--ink-3); margin: 0 0 3px; }
  .card2 .note2 { font-size: 11.5px; color: var(--ink-3); margin-bottom: 14px; }
  .card2 .note2 b { color: var(--ink-2); font-family: var(--mono); }

  /* punchcard — weekday × hour, area-encoded. Marginal bar charts couldn't
     say "Sunday night", which is exactly what people want to know. */
  .punch { width: 100%; height: auto; display: block; }
  .punch .cell { fill: var(--accent); }
  .punch text { fill: var(--ink-3); font-family: var(--mono); font-size: 8.5px; }
  .punch .pknow { fill: var(--accent); font-weight: 700; }

  /* tool spectrum — one bar that is the whole hand, then the parts */
  .spec { display: flex; height: 12px; border-radius: 6px; overflow: hidden; gap: 2px;
    margin-bottom: 15px; }
  .spec i { display: block; background: var(--accent); }
  .tl { display: grid; grid-template-columns: 1fr auto auto; gap: 3px 12px; align-items: baseline;
    font-family: var(--mono); font-size: 12px; }
  .tl .tn { color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tl .tc { color: var(--ink-3); font-variant-numeric: tabular-nums; }
  .tl .tp { color: var(--ink-2); font-variant-numeric: tabular-nums; text-align: right;
    min-width: 34px; }
  .tl .sw { width: 8px; height: 8px; border-radius: 2px; background: var(--accent);
    display: inline-block; margin-right: 8px; }

  /* projects — a share bar behind mono text, no chartjunk */
  .plist { display: flex; flex-direction: column; gap: 2px; margin-top: 2px; }
  .prow { position: relative; display: flex; align-items: center; gap: 12px; padding: 9px 12px;
    border-radius: 9px; font-family: var(--mono); font-size: 12.5px; overflow: hidden; }
  /* the share bar is anchored left and square-ended, so it reads as a
     measurement rather than as a highlighted chip */
  .prow .fillbar { position: absolute; left: 0; top: 0; bottom: 0; background: var(--accent);
    opacity: .09; border-radius: 9px 2px 2px 9px; }
  .prow:hover .fillbar { opacity: .16; }
  .prow .pn { position: relative; font-weight: 700; letter-spacing: -.02em; }
  .prow .pp { position: relative; color: var(--ink-3); font-size: 11px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .prow .pv { position: relative; margin-left: auto; color: var(--ink-2); white-space: nowrap;
    font-variant-numeric: tabular-nums; }

  select.in { font: inherit; font-family: var(--mono); font-size: 12px; padding: 8px 10px;
    border: 0; border-radius: 8px; background: var(--fill); color: var(--ink); cursor: pointer; }

  /* ==========================================================================
     Page system — every non-session view is a `.page`: a width-capped column
     with one header (title · count · description · actions) and a stack of
     sections. Before this each view invented its own headings and spacing;
     sharing these makes Models, Skills and MCP read as one product.
     ========================================================================== */
  .scroll { overflow-y: auto; height: 100%; }
  .page { max-width: 1000px; margin: 0 auto; padding: 34px 30px 70px; }
  .page.wide { max-width: 1280px; }
  .page-h { display: flex; align-items: flex-start; gap: 14px; margin-bottom: 7px; }
  /* Mono as the display face: this product's proper nouns are file paths and
     model ids, so the titles are set in the same tongue. */
  .page-t { font-family: var(--mono); font-size: 20px; font-weight: 700; letter-spacing: -.035em;
    margin: 0; display: flex; align-items: center; gap: 10px; }
  .count { font-family: var(--mono); font-size: 11px; font-weight: 700; color: var(--ink-2);
    background: var(--fill); padding: 2px 8px; border-radius: 6px;
    font-variant-numeric: tabular-nums; letter-spacing: 0; }
  .page-d { color: var(--ink-2); font-size: 13px; line-height: 1.6; max-width: 68ch; margin: 0 0 22px; }
  .page-d code, .mono { font-family: var(--mono); font-size: 12px; color: var(--ink-2);
    background: var(--fill); padding: 1px 6px; border-radius: 5px; }
  .page-a { margin-left: auto; display: flex; gap: 8px; align-items: center; flex: none; }

  /* THE SIGNATURE — signal path. Not decoration: each node is a real count of
     what's wired up right now, and its colour is that link's actual state. */
  .path { display: flex; align-items: center; gap: 11px; flex-wrap: wrap;
    font-family: var(--mono); font-size: 12px; padding: 13px 16px; margin-bottom: 26px;
    border-radius: var(--radius); background: var(--panel); box-shadow: var(--shadow); }
  .path .n { display: inline-flex; align-items: center; gap: 7px; color: var(--ink);
    letter-spacing: -.01em; }
  .path .n b { font-weight: 700; }
  .path .n.dim { color: var(--ink-3); }
  .path .n.warn { color: var(--caution); }
  .path .n.bad { color: var(--err); }
  .path .arw { color: var(--ink-3); opacity: .5; letter-spacing: .18em; font-size: 11px; }
  .path .n.clk { cursor: pointer; }
  .path .n.clk:hover { color: var(--accent); }

  .sec { margin-top: 30px; }
  .sec-t { font-family: var(--mono); font-size: 10.5px; font-weight: 700; letter-spacing: .13em;
    text-transform: uppercase; color: var(--ink-3); margin: 0 0 11px;
    display: flex; align-items: center; gap: 11px; }
  /* a hairline that runs out to the edge — a labelled channel on a bench */
  .sec-t .fp { font-family: var(--mono); font-size: 10.5px; letter-spacing: .02em;
    text-transform: none; color: var(--ink-3); font-weight: 400; flex: none; max-width: 46%;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sec-t .fp + ::after { flex: 1; }

  /* buttons — one shape, three intents */
  .b { font: inherit; font-size: 12.5px; font-weight: 600; padding: 7px 13px; border-radius: 8px;
    border: 0; cursor: pointer; white-space: nowrap; line-height: 1.2;
    background: var(--fill); color: var(--ink-2); transition: background .12s, color .12s; }
  .b:hover { background: var(--accent-soft); color: var(--accent); }
  .b.pri { background: var(--accent); color: #fff; border-color: transparent; }
  .b.pri:hover { filter: brightness(1.07); background: var(--accent); color: #fff; }
  @media (prefers-color-scheme: dark) { .b.pri, .b.pri:hover { color: #10220a; } }
  .b.gho { background: transparent; color: var(--ink-2); }
  .b.gho:hover { background: var(--fill); color: var(--ink); }
  .b.dan:hover { color: #fff; background: var(--err); }
  .b.armed { color: #fff; background: var(--err); }
  .b:disabled { opacity: .5; cursor: default; filter: none; }
  .b.on { background: var(--accent-soft); color: var(--accent); }

  /* search field */
  .find { position: relative; margin-bottom: 14px; }
  .find input { width: 100%; font: inherit; font-size: 13px; padding: 11px 12px 11px 34px;
    border: 0; border-radius: 10px; background: var(--fill); color: var(--ink); }
  .find input:focus { outline: none; background: var(--panel); box-shadow: var(--shadow), 0 0 0 2px var(--accent-soft); }
  .find::before { content: "⌕"; position: absolute; left: 13px; top: 50%; transform: translateY(-50%);
    color: var(--ink-3); font-size: 15px; pointer-events: none; }

  /* list rows — the shared shape for servers, skills, anything enumerable.
     The green tick on hover is the only movement: it reads as selecting a
     channel on the bench rather than as a generic hover state. */
  .list { border-radius: var(--radius); background: var(--panel); overflow: hidden;
    box-shadow: var(--shadow); padding: 4px; }
  .lrow { border-radius: 9px; }
  .lrow-top { display: flex; align-items: center; gap: 11px; padding: 13px 15px 13px 17px;
    cursor: pointer; position: relative; }
  .lrow-top::before { content: ""; position: absolute; left: 0; top: 7px; bottom: 7px; width: 2px;
    background: var(--accent); border-radius: 0 2px 2px 0; opacity: 0; transition: opacity .12s; }
  .lrow:hover .lrow-top::before, .lrow.open .lrow-top::before { opacity: 1; }
  .lrow-top:hover { background: var(--panel-2); }
  .lrow.open .lrow-top { background: var(--panel-2); }
  .lrow .nm { font-family: var(--mono); font-weight: 700; font-size: 13px; letter-spacing: -.025em;
    flex: none; }
  .lrow .sub { color: var(--ink-3); font-size: 12px; font-family: var(--mono); flex: 1;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .lrow .sub.sans { font-family: var(--sans); color: var(--ink-2); }
  .lrow .acts { display: flex; gap: 6px; flex: none; opacity: .55; transition: opacity .12s; }
  .lrow:hover .acts, .lrow.open .acts { opacity: 1; }
  .lrow .caret { color: var(--ink-3); font-size: 9px; width: 9px; flex: none; transition: transform .15s; }
  .lrow.open .caret { transform: rotate(90deg); }
  .lbody { display: none; padding: 4px 17px 16px 37px; }
  .lrow.open .lbody { display: block; animation: drawer .16s ease-out; }
  @keyframes drawer { from { opacity: 0; transform: translateY(-3px); } to { opacity: 1; transform: none; } }

  /* dot + tag vocabulary shared by every list */
  .dot2 { width: 7px; height: 7px; border-radius: 50%; flex: none; background: var(--ink-3); }
  .dot2.ok { background: var(--accent); } .dot2.bad { background: var(--err); }
  .dot2.warn { background: #d69d34; }
  .t2 { font-size: 10px; font-weight: 700; letter-spacing: .04em; text-transform: uppercase;
    padding: 2px 7px; border-radius: 5px; background: var(--fill); color: var(--ink-2); flex: none; }
  .t2.acc { background: var(--accent-soft); color: var(--accent); }
  .t2.vio { background: #f0e9ff; color: #6b3fd0; }
  .t2.blu { background: #e6f3f8; color: #2277a0; }
  .t2.amb { background: #fbf0d9; color: #966a10; }
  @media (prefers-color-scheme: dark) {
    .t2.vio { background: #2a2140; color: #b79cf0; }
    .t2.blu { background: #16303a; color: #74c0e0; }
    .t2.amb { background: #382d13; color: #e0b957; }
  }

  /* key/value detail — "what is this thing actually configured as" */
  .kvs { display: grid; grid-template-columns: 104px 1fr; gap: 4px 14px; align-items: baseline;
    font-size: 12.5px; margin: 10px 0 0; }
  .kvs dt { color: var(--ink-3); font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em;
    font-weight: 700; padding-top: 2px; }
  .kvs dd { margin: 0; font-family: var(--mono); font-size: 12px; word-break: break-word; }
  .kvs dd.wrap { white-space: pre-wrap; font-family: var(--sans); }
  .secret { color: var(--ink-3); letter-spacing: .12em; }
  .jsonbox { margin-top: 14px; }
  .jsonbox pre { margin: 0; background: var(--panel-2); border-radius: 10px; padding: 13px 14px; font-family: var(--mono); font-size: 11.5px;
    line-height: 1.55; overflow: auto; max-height: 320px; white-space: pre; }
  .jsonbox .jh { display: flex; align-items: center; gap: 10px; margin-bottom: 7px; }
  .jsonbox .jt { font-size: 10.5px; font-weight: 700; letter-spacing: .07em; text-transform: uppercase;
    color: var(--ink-3); }

  /* live probe result */
  .probe { margin-top: 14px; border-radius: 10px; padding: 12px 14px; font-size: 12.5px;
    background: var(--panel-2); box-shadow: inset 3px 0 0 var(--ink-3); }
  .probe.ok { box-shadow: inset 3px 0 0 var(--accent); }
  .probe.bad { box-shadow: inset 3px 0 0 var(--err); }
  .probe .ph2 { display: flex; align-items: center; gap: 8px; font-weight: 700; }
  .probe .pe { font-family: var(--mono); font-size: 11.5px; color: var(--err); margin-top: 7px;
    word-break: break-word; }
  .toolgrid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .toolgrid .tk { font-family: var(--mono); font-size: 11px; background: var(--fill);
    color: var(--ink-2); padding: 3px 8px; border-radius: 6px; }

  /* banner (trust prompt, warnings) */
  .banner { display: flex; align-items: center; gap: 13px; padding: 13px 16px; border-radius: 12px;
    margin-bottom: 18px; font-size: 13px; background: #fbf0d9; color: #6d4d0c; }
  @media (prefers-color-scheme: dark) { .banner { background: #382d13; color: #e0b957; } }
  .banner b { font-weight: 700; }
  .banner code { font-family: var(--mono); font-size: 12px; }
  .banner .sp { flex: 1; }

  /* composer — the add/paste form */
  .comp { border-radius: var(--radius); background: var(--panel); box-shadow: var(--shadow);
    padding: 16px 17px; margin-bottom: 18px; display: none; }
  .comp.on { display: block; }
  .comp .r { display: flex; gap: 8px; margin-bottom: 9px; flex-wrap: wrap; }
  .comp .r > * { flex: 1; min-width: 150px; }
  .comp .r > .fit { flex: none; min-width: 0; }
  .comp textarea.in { width: 100%; min-height: 118px; resize: vertical; line-height: 1.55; }
  .comp .foot { display: flex; align-items: center; gap: 12px; margin-top: 11px; }
  .comp .hint { font-size: 11.5px; color: var(--ink-3); flex: 1; line-height: 1.5; }
  .comp .hint code { font-family: var(--mono); background: var(--fill); padding: 1px 5px; border-radius: 4px; }
  label.chk { display: inline-flex; align-items: center; gap: 7px; font-size: 12.5px;
    color: var(--ink-2); cursor: pointer; white-space: nowrap; }

  /* provider marks — the vendor's own logo on a neutral 22px tile */
  .mark2 { width: 22px; height: 22px; border-radius: 6px; flex: none; display: inline-flex;
    align-items: center; justify-content: center; background: var(--fill); color: var(--ink);
    font-family: var(--mono); font-size: 11px; font-weight: 700; overflow: hidden; }
  .mark2 svg { width: 14px; height: 14px; display: block; }

  /* setup progress — this section is a task, so it shows how far along it is */
  .setup { background: var(--panel); border-radius: var(--radius); box-shadow: var(--shadow);
    padding: 15px 17px 16px; margin-bottom: 12px; }
  .setup-h { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .setup-n { font-family: var(--mono); font-size: 13px; font-weight: 700; letter-spacing: -.02em; }
  .setup-s { font-size: 12.5px; color: var(--ink-2); }
  .setup-bar { height: 5px; border-radius: 3px; background: var(--fill); margin: 11px 0 10px;
    overflow: hidden; }
  .setup-bar i { display: block; height: 100%; background: var(--accent); border-radius: 3px;
    transition: width .5s cubic-bezier(.2,.7,.2,1); }
  .setup-note { font-size: 11.5px; color: var(--ink-3); line-height: 1.55; }
  .ready { display: inline-flex; align-items: center; gap: 7px; font-size: 11.5px;
    color: var(--ink-3); font-family: var(--mono); white-space: nowrap; }

  /* the key you already have — shown, not asked for again */
  .keyheld { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 14px;
    padding: 10px 13px; border-radius: 10px; background: var(--panel-2); }
  .kh-l { font-family: var(--mono); font-size: 10.5px; letter-spacing: .06em; text-transform: uppercase;
    color: var(--ink-3); }
  .kh-v { font-family: var(--mono); font-size: 13px; font-weight: 700; letter-spacing: .02em; }
  .kh-n { font-size: 11px; color: var(--ink-3); }

  /* ---- model table: a comparison, not a list of strings ---- */
  .filters { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
  .fchips { display: flex; gap: 4px; flex: none; }
  .fchip { font: inherit; font-family: var(--mono); font-size: 11.5px; padding: 7px 11px;
    border: 0; border-radius: 8px; background: var(--fill); color: var(--ink-3); cursor: pointer;
    white-space: nowrap; }
  .fchip:hover { color: var(--ink); }
  .fchip.on { background: var(--accent-soft); color: var(--accent); font-weight: 700; }
  .mtable { background: var(--panel); border-radius: var(--radius); box-shadow: var(--shadow);
    padding: 4px; max-height: 420px; overflow-y: auto; }
  .mrow { display: grid; grid-template-columns: minmax(0,1fr) 116px 52px 132px 62px;
    align-items: center; gap: 12px; padding: 9px 12px; border-radius: 9px; cursor: pointer;
    font-family: var(--mono); font-size: 12.5px; }
  .mrow:hover, .mrow.kb { background: var(--fill); }
  .mrow.kb { box-shadow: inset 2px 0 0 var(--accent); }
  .mrow.cur { background: var(--accent-soft); }
  .mrow .mn { color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    letter-spacing: -.02em; }
  .mrow.cur .mn { color: var(--accent); font-weight: 700; }
  .mrow.locked .mn { color: var(--ink-2); }
  .mrow .mp { color: var(--ink-3); font-size: 11.5px; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; }
  .mrow .mctx { color: var(--ink-2); font-size: 11.5px; text-align: right;
    font-variant-numeric: tabular-nums; }
  .mrow .mcaps { display: flex; gap: 4px; }
  .cap { font-size: 9.5px; letter-spacing: .04em; text-transform: uppercase; font-weight: 700;
    padding: 2px 6px; border-radius: 4px; background: var(--fill); color: var(--ink-3);
    font-family: var(--sans); }
  .mrow.cur .cap { background: var(--panel); }
  .mrow .mgo { font-size: 11px; color: var(--ink-3); text-align: right; white-space: nowrap; }
  .mrow:hover .mgo { color: var(--accent); }
  .mrow.locked .mgo { color: var(--caution); }

  /* empty states that suggest, rather than shrug */
  .zero { border-radius: var(--radius); background: var(--panel-2); padding: 32px 22px; text-align: center; }
  .zero .zt { font-weight: 700; font-size: 13.5px; margin-bottom: 5px; }
  .zero .zd { font-size: 12.5px; color: var(--ink-3); max-width: 52ch; margin: 0 auto; line-height: 1.6; }

  .empty { color: var(--ink-3); padding: 40px 20px; text-align: center; font-size: 13px; }
  .col::-webkit-scrollbar, .pad::-webkit-scrollbar { width: 10px; }
  .col::-webkit-scrollbar-thumb, .pad::-webkit-scrollbar-thumb {
    background: var(--line); border-radius: 6px; }

  /* ==========================================================================
     OVERVIEW additions — the five provider families, the spend ledger, and
     the live activity channel. Same bench: mono display face, olive ink, and
     every number a real reading off a file on this machine.
     ========================================================================== */
  .fam-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px;
    margin-bottom: 14px; }
  @media (max-width: 1100px) { .fam-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  @media (max-width: 720px) { .fam-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  .fam { background: var(--panel); border-radius: var(--radius); box-shadow: var(--shadow);
    padding: 13px 14px 12px; display: flex; flex-direction: column; gap: 7px; min-width: 0;
    position: relative; cursor: pointer; transition: box-shadow .12s; }
  .fam:hover { box-shadow: var(--shadow), inset 0 0 0 1px var(--line); }
  .fam.cur { box-shadow: var(--shadow), inset 0 0 0 1.5px var(--accent); }
  .fam .fh { display: flex; align-items: center; gap: 8px; }
  .fam .mark2 { width: 26px; height: 26px; }
  .fam .mark2 svg { width: 16px; height: 16px; }
  .fam .fn { font-family: var(--mono); font-weight: 700; font-size: 13px; letter-spacing: -.02em;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fam .fa { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--ink-2);
    white-space: nowrap; overflow: hidden; }
  .fam .fa .t2 { flex: none; }
  .fam .fm { font-family: var(--mono); font-size: 11.5px; color: var(--ink-2); overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .fam .fm.none { color: var(--ink-3); font-style: italic; font-family: var(--sans); }
  .fam .ff { display: flex; align-items: center; gap: 6px; margin-top: auto; }
  .fam .ff .b { padding: 5px 10px; font-size: 11.5px; }
  .fam .ff .pr { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3);
    display: inline-flex; align-items: center; gap: 5px; overflow: hidden; white-space: nowrap;
    text-overflow: ellipsis; }
  .fam .ff .pr.ok { color: var(--accent); } .fam .ff .pr.bad { color: var(--err); }
  .fam .sub2 { font-size: 10.5px; color: var(--ink-3); font-family: var(--mono); overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }

  /* spend ledger — estimated bars are hatched, recorded bars are solid: the
     eye should never mistake a guess for a reading */
  .spend-h { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; flex-wrap: wrap; }
  .spend-h h3 { margin: 0; }
  .spend-h .fchips { margin-left: auto; }
  .bars { width: 100%; height: auto; display: block; margin: 6px 0 4px; }
  .bars .est { fill: url(#hatch); }
  .bars .rec { fill: var(--accent); }
  .bars .hl { fill: var(--accent); opacity: .18; }
  .bars text { fill: var(--ink-3); font-family: var(--mono); font-size: 9px; }
  .bars .base { stroke: var(--fill); stroke-width: 1; }
  .bars .today { fill: var(--accent); }
  .leg { display: flex; gap: 14px; font-size: 11px; color: var(--ink-3); font-family: var(--mono);
    margin-bottom: 8px; flex-wrap: wrap; }
  .leg i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; vertical-align: -1px;
    margin-right: 5px; background: var(--accent); }
  .leg i.est { background: repeating-linear-gradient(135deg, var(--accent) 0 2px, transparent 2px 4px); }
  .lcd.tight { margin: 4px 0 12px; gap: 0 20px; }
  .lcd .dim i { color: var(--ink-2); }
  .prow .pf { position: relative; }
  .prow .pf .mark2 { width: 18px; height: 18px; border-radius: 5px; }
  .prow .pf .mark2 svg { width: 11px; height: 11px; }
  .prow .t2 { position: relative; }

  /* live activity — a channel list. Running rows breathe; finished ones sit. */
  .act { display: flex; flex-direction: column; gap: 2px; }
  .arow { display: grid; grid-template-columns: 14px 64px minmax(0,1fr) 78px 84px 64px; gap: 10px;
    align-items: center; padding: 9px 12px; border-radius: 9px; font-family: var(--mono);
    font-size: 12px; cursor: pointer; }
  .arow:hover { background: var(--fill); }
  .arow .ad { font-size: 12.5px; color: var(--ink); overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; letter-spacing: -.01em; }
  .arow .ad small { color: var(--ink-3); font-size: 11px; margin-left: 6px; }
  .arow .ak { color: var(--ink-2); font-size: 10.5px; text-transform: uppercase; letter-spacing: .05em;
    font-weight: 700; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .arow .ae, .arow .at { color: var(--ink-2); text-align: right; font-variant-numeric: tabular-nums;
    white-space: nowrap; font-size: 11.5px; }
  .arow .ag { color: var(--ink-3); text-align: right; font-size: 11px; white-space: nowrap; }
  .arow:hover .ag { color: var(--accent); }
  .dot2.run { background: var(--accent); animation: pulse 1.6s ease-in-out infinite; }
  .dot2.pend { background: var(--caution); }
  @media (max-width: 900px) {
    .arow { grid-template-columns: 14px minmax(0,1fr) 78px 64px; }
    .arow .ak, .arow .at { display: none; }
  }
  .run-ph { margin: 14px 0 6px; font-family: var(--mono); font-size: 10.5px; font-weight: 700;
    letter-spacing: .13em; text-transform: uppercase; color: var(--ink-3); display: flex; gap: 10px;
    align-items: center; }
  .run-ag { background: var(--panel-2); border-radius: 10px; padding: 10px 12px; margin: 6px 0;
    font-size: 12.5px; }
  .run-ag .rh { display: flex; align-items: center; gap: 8px; font-family: var(--mono); font-size: 12px; }
  .run-ag .rh b { font-weight: 700; }
  .run-ag .rh .sp { flex: 1; }
  .run-ag .rs { color: var(--ink-2); margin-top: 5px; white-space: pre-wrap; word-break: break-word;
    max-height: 160px; overflow: auto; font-size: 12px; }
  .run-ag .re { color: var(--err); font-family: var(--mono); font-size: 11.5px; margin-top: 5px; }
  .log { background: var(--panel-2); border-radius: 10px; padding: 10px 12px; font-family: var(--mono);
    font-size: 11px; line-height: 1.55; max-height: 220px; overflow: auto; white-space: pre-wrap;
    word-break: break-word; color: var(--ink-2); }

  /* session timeline — context fill per turn and cost per turn beside it */
  .conv-stats { margin: 0 0 14px; }
  .ctxbox { background: var(--panel); border-radius: var(--radius); box-shadow: var(--shadow);
    padding: 12px 14px 8px; margin-bottom: 20px; }
  .ctxbox .ch { display: flex; align-items: baseline; gap: 10px; font-family: var(--mono);
    font-size: 10.5px; letter-spacing: .13em; text-transform: uppercase; color: var(--ink-3);
    font-weight: 700; }
  .ctxbox .ch span:last-child { margin-left: auto; text-transform: none; letter-spacing: 0;
    font-weight: 400; }
  .ctxsvg { width: 100%; height: auto; display: block; margin-top: 6px; }
  .ctxsvg .bar { fill: var(--accent); opacity: .55; }
  .ctxsvg .bar:hover, .ctxsvg .bar.on { opacity: 1; }
  .ctxsvg .cap { stroke: var(--err); stroke-width: 1; stroke-dasharray: 3 3; }
  .ctxsvg .cost { fill: none; stroke: var(--user); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
  .ctxsvg text { fill: var(--ink-3); font-family: var(--mono); font-size: 9px; }
  .who .ts { margin-left: 8px; color: var(--ink-3); font-weight: 400; letter-spacing: 0;
    text-transform: none; }
  .who .tc { margin-left: 8px; color: var(--ink-3); font-weight: 400; letter-spacing: 0;
    text-transform: none; font-family: var(--mono); }
  .block .bh .sz { margin-left: auto; color: var(--ink-3); font-size: 11px; }
  .block .bh .tg { color: var(--ink-3); font-size: 10px; }
  .block.result .bh { cursor: pointer; user-select: none; }
  .block.result.closed pre { display: none; }
  .block.result .bh:hover { background: var(--fill); }
  .msg.system .who { color: var(--caution); }
  .compact { border-radius: 9px; background: var(--caution-soft); color: var(--caution);
    padding: 8px 12px; font-size: 12px; font-family: var(--mono); }
  .colfind { padding: 0 8px 6px; position: sticky; top: 38px; background: inherit; z-index: 1; }
  .colfind input { width: 100%; font: inherit; font-size: 12px; padding: 7px 10px; border: 0;
    border-radius: 8px; background: var(--fill); color: var(--ink); }
  .colfind input:focus { outline: none; box-shadow: 0 0 0 2px var(--accent-soft), 0 0 0 1px var(--accent); }

  /* models — grouped by family; price per 1M beside the window */
  .mrow { grid-template-columns: minmax(0,1fr) 104px 52px 104px 118px 62px; }
  .mrow .mprice { color: var(--ink-2); font-size: 11px; text-align: right;
    font-variant-numeric: tabular-nums; white-space: nowrap; }
  .mrow .mprice.free { color: var(--accent); }
  .mrow .mprice.na { color: var(--ink-3); }
  .mfam { display: flex; align-items: center; gap: 9px; padding: 10px 12px 6px; font-family: var(--mono);
    font-size: 10.5px; font-weight: 700; letter-spacing: .13em; text-transform: uppercase;
    color: var(--ink-3); position: sticky; top: 0; background: var(--panel); z-index: 1; }
  .mfam .mark2 { width: 18px; height: 18px; border-radius: 5px; }
  .mfam .mark2 svg { width: 11px; height: 11px; }
  .mfam .cnt3 { font-weight: 400; letter-spacing: 0; text-transform: none; }
  .mhead { display: grid; grid-template-columns: minmax(0,1fr) 104px 52px 104px 118px 62px; gap: 12px;
    padding: 4px 12px 6px; font-family: var(--mono); font-size: 9.5px; letter-spacing: .1em;
    text-transform: uppercase; color: var(--ink-3); }
  .mhead span:nth-child(3), .mhead span:nth-child(4) { text-align: right; }
  .orow { display: grid; grid-template-columns: minmax(0,1fr) 90px 72px 92px 62px; gap: 12px;
    align-items: center; padding: 9px 12px; border-radius: 9px; font-family: var(--mono);
    font-size: 12.5px; cursor: pointer; }
  .orow:hover { background: var(--fill); }
  .orow.cur { background: var(--accent-soft); }
  .orow .on2 { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; letter-spacing: -.02em; }
  .orow .osz, .orow .opq { color: var(--ink-2); font-size: 11.5px; text-align: right;
    font-variant-numeric: tabular-nums; white-space: nowrap; }
  .orow .ost { font-size: 10px; }
  .orow .ogo { font-size: 11px; color: var(--ink-3); text-align: right; }
  .orow:hover .ogo { color: var(--accent); }
  .kbd { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); margin-top: 12px;
    line-height: 1.7; }
  .kbd b { font-weight: 700; color: var(--ink-2); background: var(--fill); padding: 0 5px;
    border-radius: 4px; }
  .refresh { display: inline-flex; align-items: center; gap: 6px; font-family: var(--mono);
    font-size: 10.5px; color: var(--ink-3); }

  @media (max-width: 900px) {
    body { grid-template-columns: 1fr; grid-template-rows: auto 1fr; }
    #rail { flex-direction: row; align-items: center; gap: 10px; overflow-x: auto;
      padding: 8px 10px; }
    .mark { padding: 0 6px 0 2px; }
    #nav { flex-direction: row; }
    #nav button .k { display: none; }
    .railfoot { display: none; }
    #sessions.on { grid-template-columns: 1fr; }
    .col { display: none; } .col.mobile-on { display: block; }
    .page { padding: 20px 14px 60px; }
    .mrow, .mhead { grid-template-columns: minmax(0,1fr) 52px 96px 62px; }
    .mrow .mp, .mrow .mcaps, .mhead span:nth-child(2), .mhead span:nth-child(5) { display: none; }
    .orow { grid-template-columns: minmax(0,1fr) 72px 62px; }
    .orow .osz, .orow .opq { display: none; }
    #transcript { padding: 16px 12px; }
  }
</style>
</head>
<body>
<aside id="rail">
  <div class="mark"><img src="/mantis.svg" alt=""> <span>mantis</span></div>
  <nav id="nav">
    <button data-v="home" class="on">overview<span class="k">1</span></button>
    <button data-v="sessions">sessions<span class="k">2</span></button>
    <button data-v="models">models<span class="k">3</span></button>
    <button data-v="mcp">mcp<span class="k">4</span></button>
    <button data-v="skills">skills<span class="k">5</span></button>
    <button data-v="config">config<span class="k">6</span></button>
  </nav>
  <div class="railfoot" id="railfoot"></div>
</aside>
<main>
  <section id="home" class="view on"><div class="scroll"><div class="page wide" id="homepad"></div></div></section>
  <section id="skills" class="view"><div class="scroll"><div class="page" id="skillspad"></div></div></section>
  <section id="mcp" class="view"><div class="scroll"><div class="page" id="mcppad"></div></div></section>
  <section id="sessions" class="view">
    <div class="col" id="projects"><div class="col-head">Projects</div></div>
    <div class="col" id="sessionlist"><div class="col-head">Sessions</div>
      <div class="colfind"><input id="sessfind" type="search" placeholder="filter sessions  ( / )"></div></div>
    <div class="col" id="convcol"><div id="transcript"><div class="empty">Pick a session.</div></div></div>
  </section>
  <section id="models" class="view"><div class="scroll"><div class="page" id="modelspad"></div></div></section>
  <section id="config" class="view"><div class="scroll"><div class="page" id="configpad"></div></div></section>
</main>
<div id="modal"><div class="sheet"><button class="x" onclick="hideModal()">✕</button><div id="sheet"></div></div></div>
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
window.addEventListener("keydown", e => { if (e.key === "Escape") hideModal(); });
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
function renderFamilies(box, g) {
  box.innerHTML = "";
  (g.families || []).forEach(f => {
    const provs = f.providers || [];
    const target = provs.find(x => x.enabled) || provs[0];
    const card = el("div", "fam" + (f.is_current ? " cur" : ""));
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
    box.append(card);
  });
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
  box.innerHTML = "";
  const rows = [];
  (act.runs || []).forEach(r => { const u = r.usage || {}; rows.push({
    kind: "workflow", ts: r.saved_at, status: r.status, active: !!r.active,
    desc: r.name || r.definition || r.run_id, extra: (u.agents_done||0) + "/" + (u.agents||0) + " agents",
    elapsed: u.elapsed_s, tokens: u.tokens, usd: u.usd, open: () => openRun(r.run_id) }); });
  (act.jobs || []).forEach(j => rows.push({
    kind: j.kind || "job", ts: j.ended_at || j.started_at || j.created_at, status: j.status, active: !j.terminal,
    desc: j.desc || j.job_id, extra: (j.turn_count||0) + " turns · " + (j.tool_count||0) + " tools" + (j.last_tool ? " · " + j.last_tool : ""),
    elapsed: j.elapsed_s, tokens: null, err: j.error,
    open: () => j.workflow_id ? openRun(j.workflow_id) : jumpToSession(j.cwd, j.session_id) }));
  rows.sort((a, b) => (b.active - a.active) || ((b.ts||0) - (a.ts||0)));
  if (!rows.length) {
    box.append(zero("Nothing running, nothing recorded",
      "Background jobs (sub-agents, workers, shells) and workflow runs land here with status, " +
      "elapsed time and token use — and each opens into its run or session."));
    return;
  }
  const list = el("div","act");
  rows.slice(0, 14).forEach(r => {
    const row = el("div","arow");
    row.append(el("span","dot2 " + statusClass(r.status, r.active)));
    row.append(el("span","ak", r.kind));
    const d = el("span","ad", r.desc); d.title = r.err || r.desc;
    if (r.extra) d.append(el("small", null, r.extra));
    row.append(d);
    row.append(el("span","ae", (r.active ? "▶ " : "") + fmtDur(r.elapsed)));
    row.append(el("span","at", r.tokens != null ? fmtTok(r.tokens) + " tok" + (r.usd ? " · " + fmtUsd(r.usd) : "") : ""));
    row.append(el("span","ag", r.status + (r.ts ? " · " + ago(r.ts) : "")));
    row.onclick = r.open;
    list.append(row);
  });
  box.append(list);
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
  const [a, g, sp, act] = await Promise.all([
    api("/api/analytics"),
    api("/api/providers").catch(() => ({ families: [] })),
    api("/api/spend").catch(() => null),
    api("/api/activity").catch(() => ({ jobs: [], runs: [] })),
  ]);
  if (my !== homeReq) return;
  pad.innerHTML = "";
  const t = a.totals;
  const ref = el("span","refresh"); ref.id = "refresh-ind";
  ref.append(el("span","live"), document.createTextNode("live · 15s"));
  ref.title = "providers and activity refresh every 15s while this tab is visible";
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

// ---- top bar + nav ----
// The rail's foot is the one always-visible answer to "what is this agent
// wired to right now" — the model it will use, how that model is reached, and
// how much is plugged in. Every number is a link to the page that changes it.
let OVERVIEW = {};
async function loadOverview() {
  const o = await api("/api/overview");
  OVERVIEW = o;
  const f = document.getElementById("railfoot");
  const cur = (o.current && o.current.model) ? o.current.model : "not set";
  const h = o.hosting || {};
  f.innerHTML = "";
  f.append(el("div","rf-l","model"));
  const v = el("div","rf-v", cur); v.title = cur; f.append(v);
  const s = el("div","rf-s");
  s.append(el("span","live" + (h.label ? "" : " off")));
  s.append(document.createTextNode(h.label || (h.kind === "selfhost" ? "self-hosted" : "no provider")));
  f.append(s);
  const c = el("div","rf-c");
  const stat = (label, val, view) => {
    const x = el("span"); x.append(el("b", null, String(val)));
    x.append(document.createTextNode(" " + label));
    x.onclick = () => showTab(view); c.append(x);
  };
  stat("providers", o.enabled_providers + "/" + o.provider_count, "models");
  stat("servers", o.mcp_count == null ? "—" : o.mcp_count, "mcp");
  stat("skills", o.skill_count == null ? "—" : o.skill_count, "skills");
  stat("sessions", o.session_count, "sessions");
  if (o.family_ready_count != null) stat("families", o.family_ready_count + "/5", "home");
  if (o.active_jobs || o.active_runs) stat("running", (o.active_jobs || 0) + (o.active_runs || 0), "home");
  f.append(c);
  const k = el("div","kbd");
  k.innerHTML = "<b>g</b> <b>o</b> overview · <b>g</b> <b>s</b> sessions · <b>g</b> <b>m</b> models · <b>/</b> search";
  f.append(k);
}
const VIEWS = ["home","sessions","models","mcp","skills","config"];
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
  if (name === "skills") loadSkills();
  if (name === "mcp") loadMcp();
  if (name === "config") loadConfig();
}
document.getElementById("nav").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  showTab(b.dataset.v);
});
// Keyboard: 1–6 jump between pages (the rail shows each key), `g` then a
// letter does the same by name (g o · g s · g m · g p · g k · g c), and `/`
// drops into whatever search the current page has.
let chord = null, chordT = null;
const CHORDS = { o: "home", s: "sessions", m: "models", p: "mcp", k: "skills", c: "config" };
function focusSearch() {
  const v = document.querySelector(".view.on");
  const inp = curView === "sessions" ? document.getElementById("sessfind")
            : (v && v.querySelector(".find input, input[type=search]"));
  if (!inp) return false;
  inp.focus(); if (inp.select) inp.select();
  return true;
}
window.addEventListener("keydown", e => {
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT")) return;
  if (document.getElementById("modal").className) return;
  if (chord === "g") {
    chord = null; clearTimeout(chordT);
    if (CHORDS[e.key]) { showTab(CHORDS[e.key]); e.preventDefault(); }
    return;
  }
  if (e.key === "g") { chord = "g"; chordT = setTimeout(() => (chord = null), 900); return; }
  if (e.key === "/") { if (focusSearch()) e.preventDefault(); return; }
  const i = "123456".indexOf(e.key);
  if (i >= 0) { showTab(VIEWS[i]); e.preventDefault(); }
});
// Auto-refresh: the rail readout every 15s, plus the overview's live panels
// (providers, jobs, runs) re-rendered in place. Paused while the tab is
// hidden — a dashboard nobody is looking at shouldn't poll — and caught up
// the moment it's visible again. An in-flight provider test is never wiped.
let refreshT = null;
async function refreshLive() {
  if (document.hidden) return;
  try {
    const p = loadOverview();
    if (curView === "home") {
      const [g, act] = await Promise.all([api("/api/providers"), api("/api/activity")]);
      const grid = document.getElementById("fam-grid");
      if (grid && !grid.querySelector("button:disabled")) renderFamilies(grid, g);
      const live = document.getElementById("live-act");
      if (live) renderActivity(live, act);
    }
    await p;
  } catch (e) { /* transient — the next tick retries */ }
}
function startRefresh() { if (refreshT) clearInterval(refreshT); refreshT = setInterval(refreshLive, 15000); }
document.addEventListener("visibilitychange", () => {
  if (document.hidden) { clearInterval(refreshT); refreshT = null; }
  else { refreshLive(); startRefresh(); }
});
startRefresh();
window.addEventListener("hashchange", () => {
  const t = location.hash.slice(1);
  // Only react to a REAL change (back/forward, manual edit) — showTab already
  // handled the tab it set the hash to, so don't reload it a second time.
  if (VIEWS.includes(t) && t !== curView) showTab(t);
});

// ---- sessions ----
let curProject = null;
async function loadProjects() {
  const { projects } = await api("/api/projects");
  const c = document.getElementById("projects");
  c.querySelectorAll(".row, .empty").forEach(r => r.remove());
  c.querySelector(".col-head").textContent = "Projects · " + projects.length;
  if (!projects.length) {
    c.append(el("div","empty","No sessions recorded yet."));
    document.querySelector("#sessionlist .col-head").textContent = "Sessions";
    return;
  }
  const hint = document.getElementById("sessionlist");
  if (!hint.querySelector(".row")) hint.append(el("div","empty","Pick a project."));
  projects.forEach(pr => {
    const row = el("div","row");
    row.append(el("div","t", pr.name || pr.digest));
    row.append(el("div","s path", pr.path));
    const m = el("div","m");
    m.append(el("span",null, pr.session_count + " session" + (pr.session_count===1?"":"s")));
    m.append(el("span",null, ago(pr.last_activity)));
    row.append(m);
    row.onclick = () => {
      document.querySelectorAll("#projects .row").forEach(r => r.classList.remove("on"));
      row.classList.add("on"); curProject = pr; loadSessions(pr);
    };
    c.append(row);
  });
}
let sessionsReq = 0;
async function loadSessions(pr) {
  const c = document.getElementById("sessionlist");
  const my = ++sessionsReq;
  const clear = () => c.querySelectorAll(".row, .empty").forEach(r => r.remove());
  if (!pr.cwd) { clear(); c.append(el("div","empty","(path unknown)")); return; }
  const { sessions } = await api("/api/sessions?" + q({ cwd: pr.cwd }));
  if (my !== sessionsReq) return;   // a newer project selection superseded this
  clear();
  c.querySelector(".col-head").textContent = "Sessions · " + sessions.length;
  if (!sessions.length) { c.append(el("div","empty","No sessions.")); return; }
  sessions.forEach(s => {
    const row = el("div","row");
    row.dataset.sid = s.session_id;
    row.dataset.q = ((s.title || "") + " " + (s.first_prompt || "") + " " + (s.last_prompt || "") + " " + s.session_id).toLowerCase();
    row.append(el("div","t", s.title || s.first_prompt || "(untitled)"));
    row.append(el("div","s", s.last_prompt || ""));
    const m = el("div","m");
    m.append(el("span",null, (s.message_count||0) + " msg"));
    m.append(el("span",null, ago(s.modified_at)));
    row.append(m);
    row.onclick = () => {
      document.querySelectorAll("#sessionlist .row").forEach(r => r.classList.remove("on"));
      row.classList.add("on"); loadConv(pr.cwd, s);
    };
    c.append(row);
  });
  applySessionFilter();
}
function applySessionFilter() {
  const q = (document.getElementById("sessfind").value || "").trim().toLowerCase();
  document.querySelectorAll("#sessionlist .row").forEach(r => {
    r.style.display = !q || q.split(/\s+/).every(t => (r.dataset.q || "").includes(t)) ? "" : "none";
  });
}
document.getElementById("sessfind").addEventListener("input", applySessionFilter);
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
  head.append(el("h2", null, s.title || s.first_prompt || "(untitled)"));
  head.append(el("div","sub", (s.message_count||0) + " messages · " + ago(s.modified_at) + " · " + s.session_id.slice(0,8)));
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
  data.messages.forEach((m, i) => box.append(renderMsg(m, i, turns)));
  if (st.note) { const n = el("div","note2", st.note); n.style.marginTop = "8px"; box.append(n); }
}
function renderMsg(m, i, turns) {
  const role = m.role || "assistant";
  if (role === "system" && m.compact) {
    const w = el("div","msg system"); w.dataset.i = i;
    w.append(el("div","who","compaction"));
    w.append(el("div","compact", "context compacted · " + (m.compacted_count || 0) + " earlier messages folded into a summary"));
    if (m.content) w.append(el("div","thinking", m.content));
    return w;
  }
  if (role === "user" && m.isMeta) return renderMeta(m);
  const wrap = el("div", "msg " + role);
  wrap.dataset.i = i;
  const who = el("div","who", role);
  if (m.ts) who.append(el("span","ts", new Date(m.ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })));
  const t = m.turn != null && turns ? turns[m.turn] : null;
  if (t) who.append(el("span","tc", fmtTok(t.ctx_est) + " ctx" + (t.usd_est != null ? " · " + fmtUsd(t.usd_est) : "")));
  wrap.append(who);
  const content = m.content;
  if (typeof content === "string") { wrap.append(el("div","text", content)); return wrap; }
  (content || []).forEach(b => { const n = renderBlock(b); if (n) wrap.append(n); });
  return wrap;
}
function renderMeta(m) {
  const wrap = el("div","msg");
  const d = el("div","thinking", typeof m.content === "string" ? m.content : "[context]");
  wrap.append(d); return wrap;
}
function renderBlock(b) {
  const t = b.type;
  if (t === "text") return el("div","text", b.text || "");
  if (t === "thinking") return el("div","thinking", b.thinking || "");
  if (t === "tool_use") {
    const blk = el("div","block tool");
    const bh = el("div","bh"); bh.append(el("span",null,"→ " + (b.name||"tool")));
    bh.append(el("span","badge", (b.id||"").slice(0,8))); blk.append(bh);
    blk.append(el("pre", null, JSON.stringify(b.input || {}, null, 2)));
    return blk;
  }
  if (t === "tool_result") {
    let c = b.content;
    if (Array.isArray(c)) c = c.map(x => x.type === "text" ? x.text : JSON.stringify(x)).join("\n");
    const text = typeof c === "string" ? c : JSON.stringify(c, null, 2);
    const lines = (text || "").split("\n");
    // errors open by default — they're the thing you came to read
    const blk = el("div","block result" + (b.is_error ? " err" : " closed"));
    const bh = el("div","bh"); bh.append(el("span",null, b.is_error ? "⎿ error" : "⎿ result"));
    bh.append(el("span","badge", (b.tool_use_id||"").slice(0,8)));
    bh.append(el("span","tg", lines[0].slice(0, 100)));
    const sz = el("span","sz"); bh.append(sz);
    const setSz = () => { sz.textContent = fmtBytes((text || "").length) + " · " + lines.length + " line" + (lines.length===1?"":"s") + (blk.classList.contains("closed") ? " ▸" : " ▾"); };
    setSz();
    bh.onclick = () => { blk.classList.toggle("closed"); setSz(); };
    blk.append(bh);
    blk.append(el("pre", null, text));
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
    showTab("sessions");
    loadConv(qs.get("cwd"), { session_id: qs.get("session"), title: qs.get("title") || "", modified_at: Date.now()/1000 });
  }
  else if (["sessions","models","skills","mcp","config"].includes(t)) showTab(t);
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
