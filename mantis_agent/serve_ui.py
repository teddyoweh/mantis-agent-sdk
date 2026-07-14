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
  :root {
    --bg: #f2f1ef; --panel: #ffffff; --panel-2: #f6f5f3; --fill: #eeedea; --line: #e9e8e5;
    --ink: #1c1b19; --ink-2: #6b6a66; --ink-3: #9b9993;
    --accent: #4f7a2f; --accent-soft: #eaf1e0;
    --user: #2d5fa8; --tool: #7a5cc0; --err: #c0392b;
    --shadow: 0 1px 2px rgba(20,18,10,.04), 0 2px 8px rgba(20,18,10,.05);
    --radius: 14px; --mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #131209; --panel: #1d1c15; --panel-2: #191811; --fill: #272519; --line: #2a2820;
      --ink: #eceae3; --ink-2: #a5a299; --ink-3: #72706a;
      --accent: #9ccc65; --accent-soft: #262f1a;
      --user: #7db0f2; --tool: #b79cf0; --err: #e8756a;
      --shadow: 0 1px 2px rgba(0,0,0,.3), 0 2px 10px rgba(0,0,0,.22);
    }
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: var(--sans); background: var(--bg); color: var(--ink);
    font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased;
    display: flex; flex-direction: column; height: 100vh; overflow: hidden;
  }
  a { color: var(--accent); text-decoration: none; }

  /* top bar */
  header {
    display: flex; align-items: center; gap: 18px; padding: 10px 18px;
    border-bottom: 1px solid var(--line); background: var(--panel); flex: none;
  }
  .brand { font-weight: 700; letter-spacing: .04em; font-size: 15px;
    display: inline-flex; align-items: center; gap: 8px; }
  .brand .logo { width: 24px; height: 24px; display: block; }
  .brand b { color: var(--accent); }
  .pills { display: flex; gap: 14px; flex-wrap: wrap; }
  .pill { font-size: 12px; color: var(--ink-2); white-space: nowrap; }
  .pill b { color: var(--ink); font-weight: 600; }
  nav { margin-left: auto; display: flex; gap: 4px; }
  nav button {
    font: inherit; font-size: 13px; padding: 6px 14px; border: 0; cursor: pointer;
    background: transparent; color: var(--ink-2); border-radius: 8px;
  }
  nav button:hover { background: var(--panel-2); color: var(--ink); }
  nav button.on { background: var(--accent-soft); color: var(--accent); font-weight: 600; }

  main { flex: 1; overflow: hidden; }
  .view { display: none; height: 100%; }
  .view.on { display: block; }

  /* sessions: three panes */
  #sessions.on { display: grid; grid-template-columns: 256px 336px 1fr; height: 100%; }
  .col { overflow-y: auto; border-right: 1px solid var(--line); height: 100%; }
  .col:last-child { border-right: 0; }
  .col-head {
    position: sticky; top: 0; background: var(--panel); z-index: 1;
    padding: 12px 14px 8px; font-size: 11px; letter-spacing: .08em;
    text-transform: uppercase; color: var(--ink-3); border-bottom: 1px solid var(--line);
  }
  .row {
    padding: 10px 14px; border-bottom: 1px solid var(--line); cursor: pointer;
  }
  .row:hover { background: var(--panel-2); }
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
  .thinking { border-left: 2px solid var(--line); padding: 2px 0 2px 12px;
    color: var(--ink-2); font-style: italic; white-space: pre-wrap; margin: 8px 0; }
  .block {
    border: 1px solid var(--line); border-radius: 8px; margin: 8px 0;
    background: var(--panel-2); overflow: hidden;
  }
  .block .bh { padding: 6px 12px; font-family: var(--mono); font-size: 12px;
    border-bottom: 1px solid var(--line); display: flex; gap: 8px; align-items: center; }
  .block.tool .bh { color: var(--tool); }
  .block.result .bh { color: var(--ink-2); }
  .block.result.err .bh { color: var(--err); }
  .block pre { margin: 0; padding: 10px 12px; overflow-x: auto; font-family: var(--mono);
    font-size: 12px; white-space: pre-wrap; word-break: break-word; max-height: 340px; }
  .badge { font-size: 10px; padding: 1px 6px; border-radius: 20px;
    background: var(--line); color: var(--ink-2); font-family: var(--mono); }

  /* models + config */
  .pad { padding: 22px 26px; overflow-y: auto; height: 100%; }
  .pad h2 { font-size: 13px; letter-spacing: .06em; text-transform: uppercase;
    color: var(--ink-3); margin: 0 0 14px; }
  .now { display: flex; gap: 10px; align-items: center; margin-bottom: 22px;
    padding: 14px 16px; border: 1px solid var(--line); border-radius: var(--radius);
    background: var(--panel); }
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
    background: var(--panel); margin-bottom: 24px; }
  .cfg .kv { display: grid; grid-template-columns: 200px 1fr; gap: 12px;
    padding: 11px 16px; border-bottom: 1px solid var(--line); }
  .cfg .kv:last-child { border-bottom: 0; }
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
    border-radius: 7px; background: var(--accent); color: #12240a; cursor: pointer; white-space: nowrap; }
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

  /* ---- home / analytics ---- */
  .home-wrap { padding: 20px 22px 36px; overflow-y: auto; height: 100%; }
  .hero-h { font-size: 20px; font-weight: 800; letter-spacing: -.02em; margin: 0 0 3px; }
  .hero-s { color: var(--ink-2); font-size: 12.5px; }
  .hero-s b { color: var(--ink); font-weight: 700; }
  .insight { font-size: 12.5px; color: var(--ink-3); margin: 6px 0 16px; }
  .insight b { color: var(--accent); font-weight: 600; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(128px, 1fr)); gap: 11px; }
  .tile { background: var(--panel); box-shadow: var(--shadow); border-radius: 13px; padding: 13px 15px; }
  .tile .n { font-size: 25px; font-weight: 800; letter-spacing: -.03em; line-height: 1;
    font-variant-numeric: tabular-nums; }
  .tile .l { font-size: 10.5px; color: var(--ink-3); text-transform: uppercase; letter-spacing: .06em; margin-top: 7px; }
  .tile .sub { font-size: 10.5px; color: var(--accent); margin-top: 3px; }
  .tile .spark { display: block; width: 100%; height: 24px; margin-top: 9px; }
  .panels { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; margin-top: 12px; }
  .panel { background: var(--panel); box-shadow: var(--shadow); border-radius: 13px; padding: 15px 17px; overflow: hidden; }
  .panel .ph { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-bottom: 13px; }
  .panel h3 { font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--ink-3); margin: 0; }
  .panel .ph-sub { font-size: 11.5px; color: var(--ink-2); font-variant-numeric: tabular-nums; }
  .span8 { grid-column: span 8; } .span7 { grid-column: span 7; } .span6 { grid-column: span 6; }
  .span5 { grid-column: span 5; } .span4 { grid-column: span 4; }
  @media (max-width: 920px) { .panels > .panel { grid-column: span 12 !important; } }

  .hm { width: 100%; height: auto; display: block; }
  .hm rect.c { rx: 2.5; }
  .hc0 { fill: var(--fill); } .hc1 { fill: var(--accent); opacity: .32; }
  .hc2 { fill: var(--accent); opacity: .54; } .hc3 { fill: var(--accent); opacity: .76; }
  .hc4 { fill: var(--accent); opacity: 1; }
  .hm rect.c:hover { stroke: var(--ink); stroke-width: 1.2; }
  .hm text { fill: var(--ink-3); font-size: 9px; font-family: var(--sans); }
  .hm-legend { display: flex; align-items: center; gap: 4px; font-size: 10.5px; color: var(--ink-3); }
  .hm-legend i { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }

  .area { width: 100%; display: block; }
  .area .ln { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
  .area .dot { fill: var(--accent); stroke: var(--panel); stroke-width: 2; }
  .area text { fill: var(--ink-3); font-size: 8.5px; font-family: var(--mono); }

  .radial .spoke { stroke: var(--accent); stroke-linecap: round; }
  .radial .ring { fill: none; stroke: var(--line); }
  .radial .spoke:hover { stroke: var(--ink); }
  .radial text { fill: var(--ink-3); font-size: 9px; font-family: var(--mono); }
  .radial .ctr { fill: var(--ink); font-size: 16px; font-weight: 800; font-family: var(--sans); }
  .radial .ctr2 { fill: var(--ink-3); font-size: 8px; }

  .bars { display: flex; flex-direction: column; gap: 8px; }
  .bar { display: grid; grid-template-columns: 90px 1fr auto; gap: 10px; align-items: center; font-size: 12px; }
  .bar .bn { font-family: var(--mono); color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .bar .bt { height: 7px; border-radius: 4px; background: var(--fill); overflow: hidden; }
  .bar .bt i { display: block; height: 100%; background: var(--accent); border-radius: 4px; }
  .bar .bv { font-variant-numeric: tabular-nums; color: var(--ink-3); font-size: 11px; white-space: nowrap; }

  .wk { display: flex; align-items: flex-end; gap: 7px; height: 96px; }
  .wk .col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 6px; height: 100%; justify-content: flex-end; }
  .wk .col i { width: 100%; max-width: 26px; background: var(--accent); border-radius: 4px 4px 0 0; min-height: 3px; }
  .wk .col.dim i { opacity: .32; }
  .wk .col span { font-size: 10px; color: var(--ink-3); font-family: var(--mono); }

  .donut-wrap { display: flex; align-items: center; gap: 16px; }
  .legend { font-size: 12px; color: var(--ink-2); }
  .legend .dot { width: 9px; height: 9px; border-radius: 3px; display: inline-block; margin-right: 7px; }

  /* ---- skills & mcp ---- */
  .sec-head { display: flex; align-items: center; gap: 11px; margin: 2px 0 3px; }
  .sec-head.mt { margin-top: 32px; }
  .sec-head h2 { font-size: 15px; font-weight: 800; letter-spacing: -.01em; margin: 0; color: var(--ink); text-transform: none; }
  .sec-head .cnt { font-size: 11.5px; color: var(--ink-2); font-variant-numeric: tabular-nums;
    background: var(--fill); padding: 1px 9px; border-radius: 20px; }
  .sec-head .sp { flex: 1; }
  .addbtn { font: inherit; font-size: 12px; font-weight: 600; padding: 7px 13px; border-radius: 8px;
    background: var(--fill); color: var(--ink-2); border: 0; cursor: pointer; }
  .addbtn:hover, .addbtn.on { background: var(--accent-soft); color: var(--accent); }
  .sec-note { font-size: 12px; color: var(--ink-3); margin: 0 0 14px; }
  .addform { background: var(--panel); box-shadow: var(--shadow); border-radius: 13px; padding: 15px 16px; margin-bottom: 16px; }
  .addform .r { display: flex; gap: 8px; margin-bottom: 8px; }
  .addform textarea.in { min-height: 78px; resize: vertical; line-height: 1.5; width: 100%; }
  .addform .foot { display: flex; justify-content: flex-end; gap: 8px; margin-top: 4px; }
  select.in { font: inherit; font-family: var(--mono); font-size: 12px; padding: 8px 10px;
    border: 0; border-radius: 8px; background: var(--fill); color: var(--ink); cursor: pointer; }
  .grp-label { font-size: 10.5px; text-transform: uppercase; letter-spacing: .07em; color: var(--ink-3);
    font-weight: 700; margin: 15px 0 8px; }
  .grp-label span { color: var(--ink-3); font-weight: 400; letter-spacing: 0; text-transform: none;
    font-family: var(--mono); margin-left: 8px; }
  .grp-empty { font-size: 12px; color: var(--ink-3); font-style: italic; padding: 2px 0 4px; }
  .item { background: var(--panel); box-shadow: var(--shadow); border-radius: 12px;
    padding: 13px 15px; margin-bottom: 9px; display: flex; align-items: flex-start; gap: 12px; }
  .item .main { flex: 1; min-width: 0; }
  .item .tt { font-weight: 700; font-size: 13.5px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .item .dd { font-size: 12px; color: var(--ink-2); margin-top: 3px; line-height: 1.45; }
  .item .meta { font-family: var(--mono); font-size: 11px; color: var(--ink-3); margin-top: 5px; }
  .tag { font-size: 9.5px; text-transform: uppercase; letter-spacing: .04em; font-weight: 700;
    padding: 2px 7px; border-radius: 5px; font-family: var(--sans); }
  .tag.global { background: var(--fill); color: var(--ink-2); }
  .tag.project { background: var(--accent-soft); color: var(--accent); }
  .tag.settings { background: #e6f3f8; color: #2277a0; }
  .tag.al { background: #f0e9ff; color: #6b3fd0; }
  .tag.tp { background: var(--fill); color: var(--ink-2); }
  @media (prefers-color-scheme: dark) { .tag.settings { background:#16303a; color:#74c0e0; } .tag.al { background:#2a2140; color:#b79cf0; } }
  .item .acts { display: flex; gap: 6px; flex: none; }
  .iconbtn { background: none; border: 0; font: inherit; font-size: 12px; color: var(--ink-3);
    cursor: pointer; padding: 4px 8px; border-radius: 6px; }
  .iconbtn:hover { background: var(--fill); color: var(--ink); }
  .iconbtn.danger:hover { color: var(--err); }
  .iconbtn.armed { color: var(--err); background: var(--fill); font-weight: 600; }

  .empty { color: var(--ink-3); padding: 40px 20px; text-align: center; font-size: 13px; }
  .col::-webkit-scrollbar, .pad::-webkit-scrollbar { width: 10px; }
  .col::-webkit-scrollbar-thumb, .pad::-webkit-scrollbar-thumb {
    background: var(--line); border-radius: 6px; }

  @media (max-width: 860px) {
    #sessions.on { grid-template-columns: 1fr; }
    .col { display: none; } .col.mobile-on { display: block; }
  }
</style>
</head>
<body>
<header>
  <div class="brand"><img src="/mantis.svg" class="logo" alt="mantis"> <b>mantis</b></div>
  <div class="pills" id="pills"></div>
  <nav id="nav">
    <button data-v="home" class="on">Home</button>
    <button data-v="sessions">Sessions</button>
    <button data-v="models">Models</button>
    <button data-v="skills">Skills</button>
    <button data-v="mcp">MCP</button>
    <button data-v="config">Config</button>
  </nav>
</header>
<main>
  <section id="home" class="view on"><div class="home-wrap" id="homepad"></div></section>
  <section id="skills" class="view"><div class="pad" id="skillspad"></div></section>
  <section id="mcp" class="view"><div class="pad" id="mcppad"></div></section>
  <section id="sessions" class="view">
    <div class="col" id="projects"><div class="col-head">Projects</div></div>
    <div class="col" id="sessionlist"><div class="col-head">Sessions</div></div>
    <div class="col" id="convcol"><div id="transcript"><div class="empty">Pick a session.</div></div></div>
  </section>
  <section id="models" class="view"><div class="pad" id="modelspad"></div></section>
  <section id="config" class="view"><div class="pad" id="configpad"></div></section>
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
function showModal() { document.getElementById("modal").className = "on"; }
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

// ---- home / analytics ----
const fmt = n => (n || 0).toLocaleString();
const MON = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
function dkey(d) { return d.getFullYear() + "-" + String(d.getMonth()+1).padStart(2,"0") + "-" + String(d.getDate()).padStart(2,"0"); }
function calcStreak(daily) {
  // consecutive days with activity, ending at the most recent active day
  let s = 0; const d = new Date(); d.setHours(0,0,0,0);
  if (!(daily[dkey(d)] && daily[dkey(d)].msgs > 0)) d.setDate(d.getDate()-1);  // grace: today may be empty
  while (daily[dkey(d)] && daily[dkey(d)].msgs > 0) { s++; d.setDate(d.getDate()-1); }
  return s;
}
function dailySeries(daily, n) {
  const out = []; const d = new Date(); d.setHours(0,0,0,0); d.setDate(d.getDate()-(n-1));
  for (let i = 0; i < n; i++) { const k = dkey(d); out.push({ date: k, msgs: (daily[k] && daily[k].msgs) || 0 }); d.setDate(d.getDate()+1); }
  return out;
}
const WD = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"];
function insight(a) {
  const t = a.totals, bits = [];
  if (a.by_weekday.some(x => x)) {
    const wi = a.by_weekday.indexOf(Math.max(...a.by_weekday));
    const ph = a.by_hour.indexOf(Math.max(...a.by_hour));
    const part = ph < 12 ? "mornings" : ph < 17 ? "afternoons" : "evenings";
    bits.push(`Most active <b>${WD[wi]} ${part}</b>`);
  }
  if (a.top_tools[0] && t.tool_calls) {
    bits.push(`<b>${esc(a.top_tools[0].name)}</b> is ${Math.round(a.top_tools[0].count/t.tool_calls*100)}% of your tool calls`);
  }
  bits.push(`<b>${t.avg_msgs_per_session}</b> messages per session`);
  return bits.join("&nbsp;&nbsp;·&nbsp;&nbsp;");
}
function sparkSVG(vals) {
  if (!vals.length) return "";
  const w = 130, h = 26, max = Math.max(1, ...vals), n = vals.length;
  const pts = vals.map((v,i) => [(n<=1?0:i/(n-1))*w, h - (v/max)*(h-3) - 1.5]);
  const d = "M" + pts.map(p => p[0].toFixed(1)+","+p[1].toFixed(1)).join(" L");
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">` +
    `<path d="${d} L${w},${h} L0,${h} Z" style="fill:var(--accent);opacity:.12"/>` +
    `<path d="${d}" style="fill:none;stroke:var(--accent);stroke-width:1.5;vector-effect:non-scaling-stroke"/></svg>`;
}
function areaSVG(series) {
  const w = 660, h = 148, pl = 4, pb = 15, pt = 8;
  const iw = w - pl*2, ih = h - pt - pb, n = series.length;
  const max = Math.max(1, ...series.map(s => s.msgs));
  const X = i => pl + (n<=1?0:i/(n-1)*iw), Y = v => pt + ih - (v/max)*ih;
  const pts = series.map((s,i) => [X(i), Y(s.msgs)]);
  const line = "M" + pts.map(p => p[0].toFixed(1)+","+p[1].toFixed(1)).join(" L");
  const area = line + ` L${(pl+iw).toFixed(1)},${(pt+ih).toFixed(1)} L${pl},${(pt+ih).toFixed(1)} Z`;
  let ticks = "", lastM = -1;
  series.forEach((s,i) => { const dt = new Date(s.date+"T00:00:00"); const m = dt.getMonth();
    if (m !== lastM && i > 2) { lastM = m; ticks += `<text x="${X(i).toFixed(1)}" y="${h-3}" text-anchor="middle">${MON[m]}</text>`; } });
  const pi = series.reduce((b,s,i) => s.msgs > series[b].msgs ? i : b, 0);
  const peak = series[pi].msgs ? `<circle class="dot" cx="${X(pi).toFixed(1)}" cy="${Y(series[pi].msgs).toFixed(1)}" r="3.2"><title>${series[pi].date}: ${series[pi].msgs} msgs</title></circle>` : "";
  return `<svg class="area" viewBox="0 0 ${w} ${h}"><defs><linearGradient id="ag" x1="0" y1="0" x2="0" y2="1">` +
    `<stop offset="0" style="stop-color:var(--accent);stop-opacity:.26"/><stop offset="1" style="stop-color:var(--accent);stop-opacity:0"/>` +
    `</linearGradient></defs><path d="${area}" fill="url(#ag)"/><path class="ln" d="${line}"/>${peak}${ticks}</svg>`;
}
function heatmapSVG(daily) {
  const weeks = 18, cell = 13, gap = 4, top = 16, left = 20;
  const today = new Date(); today.setHours(0,0,0,0);
  const start = new Date(today); start.setDate(start.getDate()-(weeks*7-1)); start.setDate(start.getDate()-start.getDay());
  let max = 1; for (const k in daily) max = Math.max(max, daily[k].msgs);
  const lvl = v => v===0?0 : v<=max*0.25?1 : v<=max*0.5?2 : v<=max*0.75?3 : 4;
  let cells = "", months = "", lastM = -1;
  for (let w = 0; w < weeks; w++) for (let dow = 0; dow < 7; dow++) {
    const d = new Date(start); d.setDate(start.getDate()+w*7+dow);
    if (d > today) continue;
    const k = dkey(d), v = (daily[k] && daily[k].msgs) || 0;
    const x = left + w*(cell+gap), y = top + dow*(cell+gap);
    cells += `<rect class="c hc${lvl(v)}" x="${x}" y="${y}" width="${cell}" height="${cell}"><title>${k}: ${v} msgs</title></rect>`;
    if (dow===0) { const m = d.getMonth(); if (m!==lastM) { lastM = m; months += `<text x="${x}" y="${top-4}">${MON[m]}</text>`; } }
  }
  const wl = `<text x="0" y="${top+1*(cell+gap)+cell-3}">M</text><text x="0" y="${top+3*(cell+gap)+cell-3}">W</text><text x="0" y="${top+5*(cell+gap)+cell-3}">F</text>`;
  const W = left + weeks*(cell+gap), H = top + 7*(cell+gap);
  return `<svg class="hm" viewBox="0 0 ${W} ${H}">${months}${wl}${cells}</svg>`;
}
function heatmapLegend() {
  const sw = o => `<i style="background:var(--accent);opacity:${o}"></i>`;
  return `<div class="hm-legend">less <i style="background:var(--fill)"></i>${sw(.32)}${sw(.54)}${sw(.76)}${sw(1)} more</div>`;
}
function radialSVG(bh) {
  const max = Math.max(...bh, 1), cx = 110, cy = 108, r0 = 30, rmax = 96;
  let spokes = "";
  for (let h = 0; h < 24; h++) {
    const ang = (h/24)*2*Math.PI - Math.PI/2, len = r0 + (bh[h]/max)*(rmax-r0);
    const x1 = cx+Math.cos(ang)*r0, y1 = cy+Math.sin(ang)*r0, x2 = cx+Math.cos(ang)*len, y2 = cy+Math.sin(ang)*len;
    spokes += `<line class="spoke" x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke-width="5" opacity="${(0.35+0.65*bh[h]/max).toFixed(2)}"><title>${h}:00 — ${bh[h]} msgs</title></line>`;
  }
  const labels = [0,6,12,18].map(h => { const a = (h/24)*2*Math.PI-Math.PI/2, r = rmax+9; return `<text text-anchor="middle" x="${(cx+Math.cos(a)*r).toFixed(1)}" y="${(cy+Math.sin(a)*r+3).toFixed(1)}">${h}</text>`; }).join("");
  const peak = bh.indexOf(max);
  return `<svg class="radial" viewBox="0 0 220 220" style="max-height:230px"><circle class="ring" cx="${cx}" cy="${cy}" r="${r0}"/>${spokes}${labels}<text class="ctr" text-anchor="middle" x="${cx}" y="${cy-1}">${peak}:00</text><text class="ctr2" text-anchor="middle" x="${cx}" y="${cy+11}">peak hour</text></svg>`;
}
function barsHTML(items) {
  const max = Math.max(1, ...items.map(i => i.val));
  return `<div class="bars">` + items.map(i =>
    `<div class="bar" title="${esc(i.name)} — ${fmt(i.val)}"><span class="bn">${esc(i.name)}</span>` +
    `<span class="bt"><i style="width:${Math.max(3, i.val/max*100)}%"></i></span>` +
    `<span class="bv">${fmt(i.val)}${i.sub ? " · " + i.sub : ""}</span></div>`).join("") + `</div>`;
}
function weekdayHTML(bw) {
  const L = ["M","T","W","T","F","S","S"], max = Math.max(1, ...bw);
  return `<div class="wk">` + bw.map((v,i) =>
    `<div class="col${v===max?"":" dim"}"><i style="height:${Math.max(3, v/max*100)}%" title="${L[i]}: ${v} msgs"></i><span>${L[i]}</span></div>`).join("") + `</div>`;
}
function donutHTML(u, a) {
  const tot = Math.max(1, u+a), C = 2*Math.PI*42, uLen = u/tot*C, aLen = a/tot*C;
  return `<div class="donut-wrap"><svg width="122" height="122" viewBox="0 0 120 120">
    <circle cx="60" cy="60" r="42" style="fill:none;stroke:var(--fill);stroke-width:14"/>
    <circle cx="60" cy="60" r="42" transform="rotate(-90 60 60)" stroke-dasharray="${uLen.toFixed(1)} ${C.toFixed(1)}" style="fill:none;stroke:var(--user);stroke-width:14;stroke-linecap:round"/>
    <circle cx="60" cy="60" r="42" transform="rotate(-90 60 60)" stroke-dasharray="${aLen.toFixed(1)} ${C.toFixed(1)}" stroke-dashoffset="${(-uLen).toFixed(1)}" style="fill:none;stroke:var(--accent);stroke-width:14;stroke-linecap:round"/>
    <text x="60" y="57" text-anchor="middle" style="fill:var(--ink);font-weight:800" font-size="18">${Math.round(u/tot*100)}%</text>
    <text x="60" y="73" text-anchor="middle" style="fill:var(--ink-3)" font-size="9">human</text>
  </svg><div class="legend"><div><span class="dot" style="background:var(--user)"></span>You · ${fmt(u)}</div><div style="margin-top:8px"><span class="dot" style="background:var(--accent)"></span>Agent · ${fmt(a)}</div></div></div>`;
}
function panel(title, sub, innerHTML, span) {
  const p = el("div","panel " + span);
  const ph = el("div","ph"); ph.append(el("h3", null, title));
  if (sub != null) { const s = el("div","ph-sub"); if (sub && sub.nodeType) s.append(sub); else s.textContent = sub || ""; ph.append(s); }
  p.append(ph);
  const body = el("div"); body.innerHTML = innerHTML; p.append(body);
  return p;
}
let homeReq = 0;
async function loadHome() {
  const pad = document.getElementById("homepad");
  const my = ++homeReq;
  const a = await api("/api/analytics");
  if (my !== homeReq) return;
  pad.innerHTML = "";
  const t = a.totals;
  pad.append(el("div","hero-h", "Your mantis activity"));
  const hs = el("div","hero-s");
  hs.innerHTML = `<b>${fmt(t.tool_calls)}</b> tool calls · <b>${fmt(t.messages)}</b> messages · <b>${fmt(t.sessions)}</b> sessions · <b>${fmt(t.projects)}</b> project${t.projects===1?"":"s"}`;
  pad.append(hs);
  const ins = el("div","insight"); ins.innerHTML = insight(a); pad.append(ins);

  const streak = calcStreak(a.daily);
  const spark = dailySeries(a.daily, 21).map(x => x.msgs);
  const tiles = el("div","tiles");
  const tile = (n, l, sub, sparkHTML) => {
    const d = el("div","tile"); d.append(el("div","n", n)); d.append(el("div","l", l));
    if (sub) d.append(el("div","sub", sub));
    if (sparkHTML) { const w = el("div"); w.innerHTML = sparkHTML; if (w.firstElementChild) d.append(w.firstElementChild); }
    return d;
  };
  tiles.append(tile(fmt(t.sessions), "sessions", t.avg_msgs_per_session + " avg"));
  tiles.append(tile(fmt(t.messages), "messages", null, sparkSVG(spark)));
  tiles.append(tile(fmt(t.tool_calls), "tool calls", t.unique_tools + " tools"));
  tiles.append(tile(fmt(t.projects), "projects"));
  tiles.append(tile(fmt(t.active_days), "active days", streak ? streak + "🔥 streak" : null));
  tiles.append(tile(fmt(t.busiest_day_msgs), "peak day", t.busiest_day || "—"));
  pad.append(tiles);

  const peakH = a.by_hour.indexOf(Math.max(...a.by_hour));
  const topShare = a.top_tools[0] && t.tool_calls ? Math.round(a.top_tools[0].count/t.tool_calls*100) : 0;
  const panels = el("div","panels");

  // Activity heatmap — legend in the header row (compact)
  const actLeg = document.createElement("div"); actLeg.innerHTML = heatmapLegend();
  panels.append(panel("Activity · last 18 weeks", actLeg.firstElementChild, heatmapSVG(a.daily), "span8"));
  panels.append(panel("When you work", "peak " + peakH + ":00", radialSVG(a.by_hour), "span4"));
  panels.append(panel("Messages / day", "last 8 weeks", areaSVG(dailySeries(a.daily, 56)), "span8"));
  panels.append(panel("Human vs agent", null, donutHTML(t.user_messages, t.assistant_messages), "span4"));
  panels.append(panel("Top tools", topShare + "% " + (a.top_tools[0] ? a.top_tools[0].name : ""), barsHTML(a.top_tools.map(x => ({ name: x.name, val: x.count }))), "span7"));
  panels.append(panel("By weekday", "Mon–Sun", weekdayHTML(a.by_weekday), "span5"));
  panels.append(panel("Top projects", t.projects + " active", barsHTML(a.top_projects.map(p => ({ name: p.name, val: p.msgs, sub: p.sessions + " sess" }))), "span12"));
  pad.append(panels);
}

// ---- top bar + nav ----
async function loadOverview() {
  const o = await api("/api/overview");
  const p = document.getElementById("pills");
  const cur = (o.current && o.current.model) ? o.current.model : "—";
  p.innerHTML = "";
  const add = (label, val) => { const s = el("span","pill"); s.append(label+" "); s.append(el("b",null,val)); p.append(s); };
  add("projects", o.project_count);
  add("sessions", o.session_count);
  add("model", cur);
  add("providers", o.enabled_providers + "/" + o.provider_count);
}
const VIEWS = ["home","sessions","models","skills","mcp","config"];
let curView = "home";
function showTab(name) {
  const b = document.querySelector('#nav button[data-v="' + name + '"]');
  if (!b) return;
  curView = name;
  document.querySelectorAll("#nav button").forEach(x => x.classList.toggle("on", x === b));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("on", v.id === name));
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
  c.querySelectorAll(".row").forEach(r => r.remove());
  if (!projects.length) { c.append(el("div","empty","No sessions yet.")); return; }
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
  if (!sessions.length) { c.append(el("div","empty","No sessions.")); return; }
  sessions.forEach(s => {
    const row = el("div","row");
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
  data.messages.forEach(m => box.append(renderMsg(m)));
}
function renderMsg(m) {
  const role = m.role || "assistant";
  if (role === "user" && m.isMeta) return renderMeta(m);
  const wrap = el("div", "msg " + role);
  wrap.append(el("div","who", role));
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
    const blk = el("div","block result" + (b.is_error ? " err" : ""));
    const bh = el("div","bh"); bh.append(el("span",null, b.is_error ? "⎿ error" : "⎿ result"));
    bh.append(el("span","badge", (b.tool_use_id||"").slice(0,8))); blk.append(bh);
    let c = b.content;
    if (Array.isArray(c)) c = c.map(x => x.type === "text" ? x.text : JSON.stringify(x)).join("\n");
    blk.append(el("pre", null, typeof c === "string" ? c : JSON.stringify(c, null, 2)));
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

// ---- models & hosting ----
let modelsReq = 0;
async function loadModels() {
  const pad = document.getElementById("modelspad");
  const my = ++modelsReq;
  const m = await api("/api/models");
  if (my !== modelsReq) return;   // a newer loadModels() superseded this one
  pad.innerHTML = "";
  const cur = (m.current && m.current.model) || "—";
  const h = m.hosting || {};

  // hosting banner
  pad.append(el("h2", null, "Now serving"));
  const host = el("div","host");
  host.append(el("span","kbadge " + (h.kind||"default"), h.kind || "default"));
  host.append(el("span","hm", cur));
  host.append(el("span",null, h.label ? "via " + h.label : ""));
  if (h.backend) host.append(el("span","hb", h.backend));
  pad.append(host);

  if (m.recent && m.recent.length) {
    pad.append(el("h2", null, "Recent"));
    const r = el("div","recent");
    m.recent.forEach(x => r.append(el("span","chip" + (x===cur?" cur":""), x)));
    pad.append(r);
  }

  // self-host / custom endpoint form
  pad.append(el("h2", null, "Self-host / custom endpoint"));
  const sh = el("div","selfhost");
  const shNote = el("div","note-sec");
  shNote.append(document.createTextNode("Point mantis at any OpenAI-compatible URL you run — vLLM, llama.cpp, a Modal/RunPod box. This sets it as your current model.  "));
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
  f.append(r1, r2); sh.append(f); pad.append(sh);

  pad.append(el("h2", null, "Providers · " + m.enabled_count + " of " + m.providers.length + " enabled"));
  pad.append(el("div","note-sec", "Paste a key to enable a provider. Keys are stored locally (chmod 600) and only ever shown masked."));
  const g = el("div","grid");
  m.providers.forEach(p => {
    const card = el("div","card" + (p.is_current ? " cur" : ""));

    // header: name + status pill
    const head = el("div","head");
    head.append(el("span","name", p.label || p.id));
    const st = el("span","status " + (p.enabled ? "on" : "off"));
    st.append(el("span","d"));
    st.append(document.createTextNode(p.enabled ? "enabled" : "off"));
    head.append(st);
    card.append(head);

    // endpoint (scheme stripped, single line, full URL on hover)
    if (p.base_url) {
      const u = el("div","url", p.base_url.replace(/^https?:\/\//, ""));
      u.title = p.base_url; card.append(u);
    }

    // key area
    if (p.enabled && p.key_masked) {
      const kl = el("div","keyline");
      kl.append(el("span","env", p.api_key_env || "API key"));
      kl.append(el("span","val", p.key_masked));
      kl.append(el("span","src " + (p.key_source || ""), p.key_source || ""));
      card.append(kl);

      const acts = el("div","actions");
      const upd = el("button", null, "Update key");
      acts.append(upd);
      let rm = null;
      if (p.key_source === "saved") { rm = el("button","danger","Remove"); acts.append(rm); }
      card.append(acts);

      const ed = el("div","enable"); ed.style.display = "none";
      const inp = input("paste new key", true);
      const save = el("button","btn","Save");
      ed.append(inp, save); card.append(ed);
      const doSave = saveKeyFn(p, inp, save);
      upd.onclick = () => {
        const open = ed.style.display === "none";
        ed.style.display = open ? "flex" : "none";
        if (open) inp.focus();
      };
      save.onclick = doSave;
      inp.onkeydown = e => { if (e.key === "Enter") doSave(); };
      if (rm) rm.onclick = removeKeyFn(p, rm);
    } else {
      const en = el("div","enable");
      const inp = input("paste " + (p.api_key_env || "API key"), true);
      const btn = el("button","btn","Enable");
      en.append(inp, btn); card.append(en);
      const doSave = saveKeyFn(p, inp, btn);
      btn.onclick = doSave;
      inp.onkeydown = e => { if (e.key === "Enter") doSave(); };
    }

    // "how to get a key" — opens the guide modal
    if (p.guide) {
      const gl = el("button","guide-link", "How to get a key ↗");
      gl.onclick = () => openGuide(p);
      card.append(gl);
    }

    if (p.note) card.append(el("div","note", p.note));

    const models = p.models || [];
    if (models.length) {
      const chips = el("div","chips");
      models.slice(0,6).forEach(x => chips.append(el("span","chip" + (x===cur?" cur":""), x)));
      if (models.length > 6) chips.append(el("span","chip more", "+" + (models.length-6) + " more"));
      card.append(chips);
    }
    g.append(card);
  });
  pad.append(g);

  // deep-link: /?guide=<provider|selfhost> opens that guide directly
  const gp = new URLSearchParams(location.search).get("guide");
  if (gp === "selfhost") openSelfhostGuide(m.selfhost_guide, m.selfhost_docs_url);
  else if (gp) { const pp = m.providers.find(x => x.id === gp); if (pp) openGuide(pp); }
}

// ---- skills & mcp ----
function armDelete(btn, onConfirm) {
  // Two-step delete: first click arms ("Confirm?"), second within 3s deletes.
  if (btn.dataset.armed) { onConfirm(); return; }
  const orig = btn.textContent;
  btn.textContent = "Confirm delete?"; btn.classList.add("armed"); btn.dataset.armed = "1";
  const reset = () => { btn.textContent = orig; btn.classList.remove("armed"); delete btn.dataset.armed; };
  btn._resetT = setTimeout(reset, 3000);
}
function skillItem(sk, scope) {
  const it = el("div","item");
  const main = el("div","main");
  const tt = el("div","tt"); tt.append(document.createTextNode(sk.name));
  tt.append(el("span","tag " + scope, scope));
  if (sk.always_load) tt.append(el("span","tag al", "always"));
  if (sk.category) tt.append(el("span","tag tp", sk.category));
  main.append(tt);
  if (sk.description) main.append(el("div","dd", sk.description));
  main.append(el("div","meta", ".mantis" + (scope==="global"?"":"/…") + "/skills/" + sk.slug + "/SKILL.md"));
  it.append(main);
  const acts = el("div","acts");
  const view = el("button","iconbtn","View");
  view.onclick = () => openSkillBody(sk);
  const del = el("button","iconbtn danger","Delete");
  del.onclick = () => armDelete(del, async () => {
    try { const r = await post("/api/skill/delete", { scope, slug: sk.slug });
      if (r.ok) { toast("deleted " + sk.name); loadSkills(); } else toast(r.error||"failed", true); }
    catch(e){ toast(e.message, true); }
  });
  acts.append(view, del); it.append(acts);
  return it;
}
function openSkillBody(sk) {
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(el("h3", null, sk.name));
  s.append(el("div","sub", sk.description || ""));
  const pre = el("pre"); pre.style = "white-space:pre-wrap;word-break:break-word;font-family:var(--mono);font-size:12px;background:var(--fill);padding:14px;border-radius:9px;max-height:60vh;overflow:auto";
  pre.textContent = sk.body || "(empty)"; s.append(pre);
  showModal();
}
function mcpItem(sv) {
  const it = el("div","item");
  const main = el("div","main");
  const tt = el("div","tt"); tt.append(document.createTextNode(sv.name));
  tt.append(el("span","tag tp", sv.transport));
  tt.append(el("span","tag " + sv.scope, sv.scope));
  main.append(tt);
  if (sv.detail) main.append(el("div","meta", sv.detail));
  it.append(main);
  const acts = el("div","acts");
  if (sv.scope === "global" || sv.scope === "project") {
    const del = el("button","iconbtn danger","Delete");
    del.onclick = () => armDelete(del, async () => {
      try { const r = await post("/api/mcp/delete", { scope: sv.scope, name: sv.name });
        if (r.ok) { toast("removed " + sv.name); loadMcp(); } else toast(r.error||"failed", true); }
      catch(e){ toast(e.message, true); }
    });
    acts.append(del);
  } else {
    acts.append(el("span","meta","edit settings.json"));
  }
  it.append(acts);
  return it;
}
function sectionHead(title, count, addLabel, mt) {
  const head = el("div","sec-head" + (mt ? " mt" : ""));
  head.append(el("h2", null, title));
  head.append(el("span","cnt", String(count)));
  head.append(el("div","sp"));
  const btn = el("button","addbtn", "+ " + addLabel);
  head.append(btn);
  return { head, btn };
}
function wireToggle(btn, form, addLabel) {
  form.style.display = "none";
  btn.onclick = () => {
    const open = form.style.display === "none";
    form.style.display = open ? "" : "none";
    btn.textContent = open ? "Cancel" : "+ " + addLabel;
    btn.classList.toggle("on", open);
    if (open) { const f = form.querySelector("input,textarea"); if (f) f.focus(); }
  };
}
function skillForm(onDone) {
  const f = el("div","addform");
  const name = input("skill name  ·  e.g. deploy-checklist");
  const desc = input("one-line description — what it's for");
  const scope = el("select","in"); scope.innerHTML = '<option value="global">global</option><option value="project">project</option>';
  const body = el("textarea","in"); body.placeholder = "SKILL.md body — the how-to the agent reads (markdown)";
  const create = el("button","btn","Create skill");
  create.onclick = async () => {
    if (!name.value.trim()) { toast("name required", true); return; }
    create.disabled = true;
    try { const r = await post("/api/skill", { scope: scope.value, name: name.value, description: desc.value, body: body.value });
      if (r.ok) { toast("created " + name.value.trim()); onDone(); } else toast(r.error||"failed", true); }
    catch(e){ toast(e.message, true); } finally { create.disabled = false; }
  };
  const r1 = el("div","r"); r1.append(name, desc, scope);
  const foot = el("div","foot"); foot.append(create);
  f.append(r1, body, foot);
  return f;
}
function mcpForm(onDone) {
  const f = el("div","addform");
  const name = input("server name  ·  e.g. github");
  const type = el("select","in"); type.innerHTML = '<option value="stdio">stdio · command</option><option value="http">http · url</option><option value="sse">sse · url</option>';
  const scope = el("select","in"); scope.innerHTML = '<option value="global">global</option><option value="project">project</option>';
  const cmd = input("command + args  ·  npx -y @modelcontextprotocol/server-github");
  const url = input("https://server.example.com/mcp"); url.style.display = "none";
  type.onchange = () => { const stdio = type.value === "stdio"; cmd.style.display = stdio?"":"none"; url.style.display = stdio?"none":""; };
  const btn = el("button","btn","Add server");
  btn.onclick = async () => {
    if (!name.value.trim()) { toast("name required", true); return; }
    let entry;
    if (type.value === "stdio") { const p = cmd.value.trim().split(/\s+/); if (!p[0]) { toast("command required", true); return; } entry = { command: p[0], args: p.slice(1) }; }
    else { if (!url.value.trim()) { toast("url required", true); return; } entry = { type: type.value, url: url.value.trim() }; }
    btn.disabled = true;
    try { const r = await post("/api/mcp", { scope: scope.value, name: name.value, entry });
      if (r.ok) { toast("added " + name.value.trim()); onDone(); } else toast(r.error||"failed", true); }
    catch(e){ toast(e.message, true); } finally { btn.disabled = false; }
  };
  const r1 = el("div","r"); r1.append(name, type, scope);
  const r2 = el("div","r"); r2.append(cmd, url);
  const foot = el("div","foot"); foot.append(btn);
  f.append(r1, r2, foot);
  return f;
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

  const skH = sectionHead("Skills", sk.global.length + sk.project.length, "New skill", false);
  pad.append(skH.head);
  pad.append(el("div","sec-note", "Reusable SKILL.md the agent pulls in on demand — global (every project) or project-only."));
  const skf = skillForm(() => loadSkills()); wireToggle(skH.btn, skf, "New skill"); pad.append(skf);
  [["global", sk.global, sk.global_dir], ["project", sk.project, sk.project_dir]].forEach(([scope, list, dir]) => {
    const g = el("div","grp-label"); g.append(document.createTextNode(scope + " · " + list.length)); g.append(el("span", null, dir)); pad.append(g);
    if (!list.length) pad.append(el("div","grp-empty", "no " + scope + " skills yet"));
    else list.forEach(s => pad.append(skillItem(s, scope)));
  });
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

  const mcH = sectionHead("MCP servers", mc.servers.length, "Add server", false);
  pad.append(mcH.head);
  pad.append(el("div","sec-note", "Model Context Protocol servers add tools to the agent. Global: ~/.mantis-agent/mcp.json · project: ./.mcp.json"));
  const mcf = mcpForm(() => loadMcp()); wireToggle(mcH.btn, mcf, "Add server"); pad.append(mcf);

  const byScope = { global: [], project: [], settings: [] };
  mc.servers.forEach(sv => (byScope[sv.scope] || (byScope[sv.scope] = [])).push(sv));
  const files = { global: mc.global_file, project: mc.project_file, settings: "settings.json" };
  ["global","project","settings"].forEach(scope => {
    const list = byScope[scope] || [];
    if (scope === "settings" && !list.length) return;  // only show settings group if used
    const g = el("div","grp-label"); g.append(document.createTextNode(scope + " · " + list.length));
    g.append(el("span", null, files[scope])); pad.append(g);
    if (!list.length) pad.append(el("div","grp-empty", "no " + scope + " servers yet"));
    else list.forEach(sv => pad.append(mcpItem(sv)));
  });
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

  pad.append(el("h2", null, "Effective config · merged"));
  if (!keys.length) {
    pad.append(el("div","note-sec", "No settings set — mantis runs on built-in defaults."));
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

  pad.append(el("h2", null, "Layers"));
  pad.append(el("div","note-sec", "Later layers override earlier ones: user → project → local."));
  ["user","project","local"].forEach(src => {
    const layer = (c.layers||{})[src] || {};
    const d = el("details","layer");
    const n = Object.keys(layer).length;
    d.append(el("summary", null, src + "  ·  " + n + " setting" + (n===1?"":"s")));
    if ((c.paths||{})[src]) d.append(el("div","layerpath", (c.paths)[src]));
    d.append(el("pre", null, JSON.stringify(layer, null, 2)));
    pad.append(d);
  });
}

loadOverview().catch(e => console.error(e));
loadProjects().catch(e => document.getElementById("projects").append(el("div","empty","Error: " + e.message)));
{
  const t = location.hash.slice(1);
  if (["sessions","models","skills","mcp","config"].includes(t)) showTab(t);
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
