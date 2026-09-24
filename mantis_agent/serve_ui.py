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
    --bg: #f3f4f6; --panel: #ffffff; --panel-2: #f8f9fa; --fill: #eef0f2; --hover: #f8f9fa; --fill-2: #e3e6ea;
    --line: rgba(0,0,0,.08);
    --ink: #0e0f11; --ink-2: #4a4f57; --ink-3: #6b7080;
    --accent: #2f855a; --accent-ink: #ffffff; --accent-soft: rgba(47,133,90,.10); --accent-soft-2: rgba(47,133,90,.18);
    --ok: #2f855a; --warn: #c27a10; --bad: #d23f31; --info: #2f6fdd;
    --s1: #2f855a; --s2: #3b6fd4; --seg-on: #ffffff;
    --ok-soft: rgba(47,133,90,.13); --warn-soft: rgba(194,122,16,.14); --bad-soft: rgba(210,63,49,.12);
    --info-soft: rgba(47,111,221,.12);
    --user: #2f6fdd; --tool: #6b7280; --err: #d23f31; --caution: #c27a10; --caution-soft: rgba(194,122,16,.14);
    --radius: 10px; --r-sm: 6px; --dim: rgba(20,22,26,.35);
    --sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI Variable Display", "Segoe UI", Inter, Helvetica, Arial, sans-serif;
    --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
    --t: 140ms cubic-bezier(.2,.7,.2,1);
    color-scheme: light;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #191919; --panel: #202020; --panel-2: #262626; --fill: #2c2c2c; --hover: #262626; --fill-2: #373737;
      --line: rgba(255,255,255,.08);
      --ink: #e6e6e4; --ink-2: #a5a5a2; --ink-3: #8a8a87;
      --accent: #5cb982; --accent-ink: #08130a; --accent-soft: rgba(92,185,130,.12); --accent-soft-2: rgba(92,185,130,.20);
      --ok: #5cb982; --warn: #e0a24a; --bad: #ee6a5e; --info: #6ea2ff;
      --s1: #48a870; --s2: #648ce6; --seg-on: #3a3a3a;
      --ok-soft: rgba(92,185,130,.14); --warn-soft: rgba(224,162,74,.15); --bad-soft: rgba(238,106,94,.14);
      --info-soft: rgba(110,162,255,.14);
      --user: #6ea2ff; --tool: #9a9ea6; --err: #ee6a5e; --caution: #e0a24a; --caution-soft: rgba(224,162,74,.15);
      --dim: rgba(0,0,0,.45);
      color-scheme: dark;
    }
  }
  :root[data-theme="dark"] {
    --bg: #191919; --panel: #202020; --panel-2: #262626; --fill: #2c2c2c; --hover: #262626; --fill-2: #373737;
    --line: rgba(255,255,255,.08);
    --ink: #e6e6e4; --ink-2: #a5a5a2; --ink-3: #8a8a87;
    --accent: #5cb982; --accent-ink: #08130a; --accent-soft: rgba(92,185,130,.12); --accent-soft-2: rgba(92,185,130,.20);
    --ok: #5cb982; --warn: #e0a24a; --bad: #ee6a5e; --info: #6ea2ff;
    --s1: #48a870; --s2: #648ce6; --seg-on: #3a3a3a;
    --ok-soft: rgba(92,185,130,.14); --warn-soft: rgba(224,162,74,.15); --bad-soft: rgba(238,106,94,.14);
    --info-soft: rgba(110,162,255,.14);
    --user: #6ea2ff; --tool: #9a9ea6; --err: #ee6a5e; --caution: #e0a24a; --caution-soft: rgba(224,162,74,.15);
    --dim: rgba(0,0,0,.45);
    color-scheme: dark;
  }
  * { box-sizing: border-box; }
  html, body { height: 100%; margin: 0; }
  body {
    font-family: var(--sans); background: var(--bg); color: var(--ink);
    font-size: 14px; line-height: 1.5; -webkit-font-smoothing: antialiased;
    display: grid; grid-template-columns: var(--rail-w) minmax(0, 1fr); grid-template-rows: 100%;
    height: 100vh; overflow: hidden;
  }
  a { color: var(--accent); text-decoration: none; }
  :focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent); border-radius: 6px; }
  ::selection { background: var(--accent-soft-2); }
  @media (prefers-reduced-motion: reduce) {
    * { animation-duration: .001ms !important; transition-duration: .001ms !important; }
  }

  /* ==========================================================================
     THE SHELL — a rail that names the machine, a bar that says where you are.

     The rail is the only surface that is on screen on every page, so it
     carries the things you navigate BY: the eight pages, each with its own
     mark; the count that says whether a page has anything in it; and, under
     whichever page you are on, the rows you were going to click next anyway
     — the five families under My models, your recent projects under Sessions,
     what is live under Deploy. Nothing about a page is hidden behind a menu
     that the rail could have said out loud.
     It sits on --panel while the content sits on --bg, which is the same
     background step every card on this page already uses: the split reads
     without a rule, exactly like the cards do. Collapsed (⌘\ or the chevron)
     the rail keeps the marks and drops the words; the choice is remembered.
     ========================================================================== */
  :root { --rail-w: 248px; }
  body[data-rail="min"] { --rail-w: 60px; }
  #rail { display: grid; grid-template-rows: auto minmax(0, 1fr) auto; min-width: 0; background: var(--panel); }
  .rail-h { display: flex; align-items: center; gap: 10px; padding: 14px 14px 10px; min-width: 0; }
  .brand { display: flex; align-items: center; gap: 9px; min-width: 0; flex: 1; }
  .brand img { width: 27px; height: 27px; flex: none; display: block; padding: 3px; border-radius: 8px; background: var(--fill); }
  .brand .bt { min-width: 0; display: flex; flex-direction: column; line-height: 1.25; }
  .brand .bn { font-weight: 600; font-size: 14px; letter-spacing: -.02em; }
  /* the machine, not the product: which box's sessions you are looking at */
  .brand .bs { font-family: var(--mono); font-size: 12px; color: var(--ink-3); overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .railtog { flex: none; width: 24px; height: 24px; display: inline-flex; align-items: center; justify-content: center;
    border: 0; border-radius: 6px; background: transparent; color: var(--ink-3); font-size: 14px; cursor: pointer;
    transition: background var(--t), color var(--t); }
  .railtog:hover { background: var(--fill); color: var(--ink); }

  /* ---- the pages ---- */
  #nav { display: flex; flex-direction: column; gap: 1px; padding: 4px 8px 12px; overflow-y: auto; overflow-x: hidden;
    scrollbar-width: none; min-height: 0; }
  #nav::-webkit-scrollbar { display: none; }
  /* A group heading is a control: it says what the section is and folds it
     away. It stays quiet — the same uppercase micro-label it always was, with
     a chevron that only shows itself on hover or focus, so five of them do not
     read as five buttons. */
  .ngrp { display: flex; flex-direction: column; }
  button.ng { display: flex; align-items: center; gap: 6px; width: 100%; margin: 0;
    padding: 18px 10px 4px; font: inherit; font-size: 12px; font-weight: 500; letter-spacing: 0; color: var(--ink-3); background: transparent; border: 0; border-radius: 8px;
    cursor: pointer; white-space: nowrap; text-align: left; transition: color var(--t); }
  button.ng:hover { color: var(--ink-2); }
  button.ng > span { flex: 1; min-width: 0; }
  .ngc { flex: none; font-size: 8px; font-style: normal; color: var(--ink-3); opacity: 0;
    transform: rotate(90deg); transition: opacity var(--t), transform var(--t); }
  button.ng:hover .ngc, button.ng:focus-visible .ngc { opacity: 1; }
  .ngrp.shut .ngc { transform: rotate(0deg); opacity: 1; }
  .ngi { display: flex; flex-direction: column; gap: 1px; overflow: hidden; }
  .ngrp.shut .ngi { display: none; }
  #nav .ngi > button { display: flex; align-items: center; gap: 10px; width: 100%; height: 30px; font: inherit; font-size: 14px;
    font-weight: 500; margin: 0; padding: 6px 10px; border: 0; border-radius: 6px; background: transparent; color: var(--ink-2);
    cursor: pointer; white-space: nowrap; text-align: left; transition: background var(--t), color var(--t); }
  #nav .ngi > button:hover { background: var(--fill); color: var(--ink); }
  /* active = colour + fill only; the weight never changes, so the group never shifts */
  #nav .ngi > button.on { background: var(--fill); color: var(--ink); }
  /* Every page has a mark, and the mark is the only thing left when the rail
     is collapsed — so it has to carry the page on its own. They are drawn on
     one 24-unit grid at one stroke weight, in currentColor, so a row's icon
     and its label always change colour together. */
  .ic { display: inline-flex; align-items: center; justify-content: center; flex: none; width: 18px; height: 18px;
    color: var(--ink-3); transition: color var(--t); }
  .ic svg { width: 18px; height: 18px; display: block; }
  #nav .ngi > button:hover .ic { color: var(--ink-2); }
  #nav .ngi > button.on .ic { color: var(--accent); }
  .nl { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  /* the count is a reading, not a badge: mono, tabular, no chrome — until it
     is something that is happening RIGHT NOW, which gets the live tint */
  .nc { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); font-variant-numeric: tabular-nums; flex: none; }
  #nav .ngi > button.on .nc { color: var(--ink-2); }
  #nav .ngi > button .nc:not(.hot):not(.due):not(.dot) { display: none; }
  .nc.hot { color: var(--ok); font-weight: 600; }
  /* A reading you should act on is not a quieter reading — it is a filled
     badge, so it is the one thing in the rail that carries a surface. */
  .nc.due { min-width: 17px; height: 16px; padding: 0 5px; border-radius: 8px; display: inline-flex;
    align-items: center; justify-content: center; background: var(--bad); color: var(--accent-ink);
    font-family: var(--sans); font-size: 12px; font-weight: 600; }
  #nav .ngi > button.on .nc.due { color: var(--accent-ink); }
  .nc.dot { display: inline-flex; align-items: center; gap: 5px; }
  /* the caret only exists on a page that has sub-rows, and only when it is
     the page you are on — a rail full of carets is a filing cabinet */
  .ncar { display: none; flex: none; width: 12px; color: var(--ink-3); font-size: 8px; text-align: right;
    transition: transform var(--t); }
  /* the caret rendered as a stray dot at 8px; the open sub-list says it on its own */
  #nav .ngi > button.has-sub .ncar { display: none; }
  #nav .ngi > button.sub-open .ncar { transform: rotate(90deg); }

  /* ---- the sub-rows: what you would have clicked next ---- */
  /* The branch. The reference draws a hairline down the children; this sheet
     draws no lines at all, so it is a run of 1px pixels on a 3px pitch — the
     card motif's material at its finest grain — laid as a background, and a
     stub of the same run reaches across to each row. The stub under the row
     you are on takes the accent, which is how the tree says which leaf. */
  .nsub { display: none; flex-direction: column; gap: 1px; margin: 1px 0 4px;
    background-image: repeating-linear-gradient(to bottom, var(--fill-2) 0 1px, transparent 1px 3px);
    background-size: 1px 100%; background-position: 16px 0; background-repeat: no-repeat; }
  .nsub.on { display: flex; animation: drawer .16s ease-out; }
  .nsr { position: relative; display: flex; align-items: center; gap: 9px; width: 100%; height: 27px;
    padding: 0 10px 0 26px; border: 0;
    border-radius: 8px; background: transparent; font: inherit; font-size: 14px; color: var(--ink-2); cursor: pointer;
    text-align: left; white-space: nowrap; transition: background var(--t), color var(--t); }
  .nsr::before { content: ""; position: absolute; left: 17px; top: 13px; width: 5px; height: 1px;
    background-image: repeating-linear-gradient(to right, var(--fill-2) 0 1px, transparent 1px 3px); }
  .nsr:hover { background: var(--fill); color: var(--ink); }
  .nsr.on { color: var(--ink); background: var(--fill); }
  .nsr.on::before { width: 7px;
    background-image: repeating-linear-gradient(to right, var(--accent) 0 1px, transparent 1px 2px); }
  .nsr .nl { font-size: 14px; }
  .nsr .mark2 { width: 17px; height: 17px; border-radius: 6px; background: none; flex: none; }
  .nsr .mark2 svg { width: 13px; height: 13px; }
  .nsr .ic { width: 15px; height: 15px; } .nsr .ic svg { width: 15px; height: 15px; }
  .nsr .dot2 { width: 6px; height: 6px; }
  .nsr .nc { font-size: 12px; }

  /* ---- the foot: what this dashboard is pointed at right now ---- */
  .rail-f { padding: 8px; display: flex; flex-direction: column; gap: 6px; min-width: 0; }
  .railfoot { display: flex; align-items: center; gap: 8px; padding: 9px 10px; border-radius: 8px; background: var(--panel-2);
    cursor: pointer; min-width: 0; transition: background var(--t); }
  .railfoot:hover { background: var(--fill); }
  .rf-t { min-width: 0; flex: 1; display: flex; flex-direction: column; line-height: 1.3; }
  .rf-v { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 13px; font-weight: 600; color: var(--ink); overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .rf-s { font-size: 12px; color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .rail-b { display: flex; align-items: center; gap: 6px; padding: 0 2px; }
  .rail-b .lan { margin-left: auto; }
  .ver { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); white-space: nowrap; }

  /* ---- collapsed: marks only, and the rail stops being a list of words ---- */
  body[data-rail="min"] .nl, body[data-rail="min"] .nc, body[data-rail="min"] .ncar,
  body[data-rail="min"] .ng, body[data-rail="min"] .nsub, body[data-rail="min"] .brand .bt,
  body[data-rail="min"] .rf-t, body[data-rail="min"] .rail-b .ver, body[data-rail="min"] .rail-b .lan { display: none; }
  body[data-rail="min"] #nav .ngi > button { justify-content: center; padding: 6px; }
  /* folded, a group is a gap between marks, and it can never be shut away */
  body[data-rail="min"] .ngrp.shut .ngi { display: flex; }
  body[data-rail="min"] .rail-h { padding: 13px 0 8px; justify-content: center; }
  body[data-rail="min"] .brand { justify-content: center; flex: none; }
  body[data-rail="min"] .railtog { position: absolute; opacity: 0; pointer-events: none; }
  body[data-rail="min"] .railfoot { justify-content: center; padding: 9px 0; }
  body[data-rail="min"] .rail-b { justify-content: center; }
  body[data-rail="min"] button.ng { display: block; height: 9px; padding: 0; font-size: 0; pointer-events: none; }

  /* ---- narrow: the rail leaves the flow and becomes a drawer ----
     A 60px strip of marks with no words is not navigation on a phone, and a
     236px column eats the page. Below 1000px the rail slides over the content
     instead, with a scrim behind it, and the bar grows the one control that
     opens it. */
  .ham { display: none; flex: none; width: 30px; height: 30px; align-items: center; justify-content: center;
    padding: 0; border: 0; border-radius: 8px; background: var(--fill); color: var(--ink-2); cursor: pointer;
    transition: background var(--t), color var(--t); }
  .ham:hover { background: var(--fill-2); color: var(--ink); }
  #scrim { display: none; position: fixed; inset: 0; z-index: 55; background: var(--dim); }
  @media (max-width: 1000px) {
    body { grid-template-columns: minmax(0, 1fr); }
    #rail { position: fixed; z-index: 60; top: 0; bottom: 0; left: 0; width: 236px;
      transform: translateX(-100%); transition: transform var(--t); }
    body[data-drawer="on"] #rail { transform: none; }
    body[data-drawer="on"] #scrim { display: block; }
    .ham { display: inline-flex; }
    /* The drawer is always the FULL rail — a 60px strip of wordless marks
       sliding over the page is the worst of both — so the folded preference
       simply does not apply here. railFit() clears it below the breakpoint;
       this keeps the width honest if it is ever set some other way. */
    body[data-rail="min"] { --rail-w: 236px; }
    .railtog { display: none; }
  }
  @media (prefers-reduced-motion: reduce) { #rail { transition: none; } }

  /* ---- the bar: where you are, and the two controls that are always live ---- */
  #shell { display: grid; grid-template-rows: 46px minmax(0, 1fr); min-width: 0; overflow: hidden; }
  #top { display: flex; align-items: center; gap: 12px; padding: 0 16px; min-width: 0; }
  .crumb { display: flex; align-items: center; gap: 8px; min-width: 0; font-size: 14px; color: var(--ink-2); }
  .crumb .ic { width: 16px; height: 16px; color: var(--ink-3); } .crumb .ic svg { width: 16px; height: 16px; }
  .crumb b { font-weight: 500; color: var(--ink); letter-spacing: 0; }
  .crumb .sep { color: var(--ink-3); font-size: 12px; }
  /* the step back, and the steps you can jump to: text controls, not chrome */
  .crumb-b { flex: none; width: 22px; height: 22px; margin-right: -2px; display: inline-flex; align-items: center;
    justify-content: center; padding: 0; border: 0; border-radius: 6px; background: var(--fill); color: var(--ink-2);
    font: inherit; font-size: 16px; line-height: 1; cursor: pointer; transition: background var(--t), color var(--t); }
  .crumb-b:hover { background: var(--fill-2); color: var(--ink); }
  .crumb-l { padding: 0; border: 0; background: none; font: inherit; font-size: 14px; color: var(--ink-3);
    cursor: pointer; white-space: nowrap; transition: color var(--t); }
  .crumb-l:hover { color: var(--ink); }
  .crumb .cs { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .topr { margin-left: auto; display: flex; align-items: center; gap: 8px; flex: none; min-width: 0; }
  .rf-c, .rf-l, .kbd { display: none; }
  .tb { display: inline-flex; align-items: center; gap: 7px; height: 30px; padding: 0 10px; font: inherit; font-size: 14px;
    color: var(--ink-2); background: var(--fill); border: 0; border-radius: 8px; cursor: pointer; white-space: nowrap;
    transition: background var(--t), color var(--t); }
  .tb:hover { background: var(--fill-2); color: var(--ink); }
  .tb kbd { font-family: var(--mono); font-size: 12px; color: var(--ink-3); background: var(--panel); border-radius: 6px;
    padding: 1px 5px; line-height: 1.5; }
  .tb.icon { width: 30px; padding: 0; justify-content: center; font-size: 15px; }
  .lan { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; font-weight: 600; letter-spacing: 0;
    padding: 4px 8px; border-radius: 6px; background: var(--fill); color: var(--ink-3); white-space: nowrap; }
  .lan.on { color: var(--ink-2); background: var(--fill); }
  .live { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); flex: none; animation: pulse 2.6s ease-in-out infinite; }
  .live.off { background: var(--ink-3); animation: none; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }

  main { overflow: hidden; min-height: 0; }
  .view { display: none; height: 100%; }
  .view.on { display: block; }
  .scroll { overflow-y: auto; height: 100%; }
  /* A page that reads like a document (Skills) scrolls as one: the top bar
     goes up with it instead of staying pinned over the text. */
  #shell.flow { overflow-y: auto; grid-template-rows: 46px auto; }
  #shell.flow main { overflow: visible; }
  #shell.flow .view.on, #shell.flow .view.on > .scroll { height: auto; overflow: visible; }
  .page { max-width: 1240px; margin: 0 auto; padding: 18px 48px 88px; }
  .page.wide { max-width: 1560px; }

  /* ---- page furniture ---- */
  .page-h { display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }
  .page-t { font-size: 32px; font-weight: 600; letter-spacing: -.025em; line-height: 1.15; margin: 0; display: flex; align-items: center; gap: 10px; }
  .count { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; font-weight: 600; color: var(--ink-2); background: var(--fill);
    padding: 1px 7px; border-radius: 6px; font-variant-numeric: tabular-nums; }
  .page-d { color: var(--ink-2); font-size: 14px; line-height: 1.55; max-width: 72ch; margin: 0 0 18px; }
  /* A page states its situation as READINGS, not as a sentence. The facts a
     paragraph was carrying — 184 sessions, 42 projects, since Jul 2026 — are
     each their own value-and-label, on one line, and the line never wraps: it
     is nowrap with the overflow clipped, and the readings that matter least
     are the ones that leave when there is no room. What is left of the prose
     is at most one short clause. */
  .page-r { display: flex; align-items: baseline; gap: 20px; flex-wrap: nowrap; overflow: hidden;
    margin: 0 0 16px; min-width: 0; }
  .page-rd { display: inline-flex; align-items: baseline; gap: 5px; white-space: nowrap; flex: none; }
  .page-rd b { font-weight: 600; font-size: 14px; color: var(--ink); font-variant-numeric: tabular-nums; }
  .page-rd span { font-size: 14px; color: var(--ink-3); }
  /* the one clause of prose that survives, if any */
  .page-rc { font-size: 14px; color: var(--ink-3); white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; min-width: 0; flex: 0 1 auto; }
  /* an honesty note is a footnote on the readings, not half the paragraph */
  .page-rn { font-size: 13px; color: var(--ink-3); flex: none; cursor: help; }
  /* what goes first when the line runs out */
  @media (max-width: 1200px) { .page-rd.opt2 { display: none; } }
  @media (max-width: 1000px) { .page-rd.opt1, .page-rc { display: none; } }
  .page-d code, .mono { font-family: var(--mono); font-size: 13px; color: var(--ink-2); background: var(--fill);
    padding: 1px 5px; border-radius: 6px; }
  .page-a { margin-left: auto; display: flex; gap: 8px; align-items: center; flex: none; }
  .sec { margin-top: 40px; }
  .sec-t { font-size: 16px; font-weight: 600; color: var(--ink); letter-spacing: -.01em; margin: 0 0 12px; display: flex; align-items: center;
    gap: 10px; flex-wrap: wrap; row-gap: 8px; }
  .sec-t .ic { width: 16px; height: 16px; } .sec-t .ic svg { width: 16px; height: 16px; }
  .sec-t .fp { font-family: var(--sans); font-size: 13px; font-weight: 400; color: var(--ink-3); flex: none; max-width: 46%;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

  /* ---- cards & lists: filled surfaces; hover one step; selected = accent tint ---- */
  .card, .card2, .dpc, .setup, .trace, .ctxbox, .hero, .host, .selfhost-card, .comp, details.layer,
  .cfg, .list, .browse, .mcard { background: var(--panel); border-radius: var(--radius); }
  .card { padding: 16px; display: flex; flex-direction: column; gap: 10px; }
  .card2 { padding: 14px 16px 16px; }
  .card2 h3 { font-size: 14px; font-weight: 600; color: var(--ink-2); margin: 0 0 3px; }
  .card2 .note2 { font-size: 13px; color: var(--ink-3); margin-bottom: 12px; }
  .card2 .note2 b, .note2 b { color: var(--ink-2); font-family: var(--mono); font-weight: 600; }
  .note2 { font-size: 13px; color: var(--ink-3); }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; }
  .card .head { display: flex; align-items: center; gap: 8px; }
  .card .name { font-weight: 600; font-size: 14px; }
  .card .url { font-family: var(--mono); font-size: 12px; color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .card .note { font-size: 13px; color: var(--ink-3); line-height: 1.45; }
  .card.cur, .dpc.cur { background: var(--panel); }
  .card.flash, .lrow.flash, .msg.flash { box-shadow: 0 0 0 2px var(--accent); }
  .list { padding: 4px; }
  .lrow { border-radius: var(--r-sm); }
  .lrow + .lrow { margin-top: 2px; }
  .lrow-top { display: flex; align-items: center; gap: 10px; padding: 11px 12px; cursor: pointer; border-radius: var(--r-sm);
    transition: background var(--t); }
  .lrow-top:hover, .lrow.open .lrow-top { background: var(--panel-2); }
  .lrow .nm { font-weight: 600; font-size: 14px; flex: none; }
  .lrow .sub { color: var(--ink-3); font-size: 13px; font-family: var(--mono); flex: 1; overflow: hidden; text-overflow: ellipsis;
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
  .t2 { font-size: 12px; font-weight: 500; letter-spacing: 0; padding: 2px 6px; border-radius: 6px;
    background: var(--fill); color: var(--ink-2); flex: none; white-space: nowrap; }
  .t2.acc { background: var(--ok-soft); color: var(--ok); }
  .t2.vio, .t2.blu { background: var(--fill); color: var(--ink-2); }
  .t2.amb { background: var(--warn-soft); color: var(--warn); }
  .t2.red { background: var(--bad-soft); color: var(--bad); }
  .chips { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; padding: 3px 8px; border-radius: 6px; background: var(--fill); color: var(--ink-2); }
  .chip.cur { background: var(--fill-2); color: var(--ink); font-weight: 500; }
  .chip.more { color: var(--ink-3); }
  .chip.clk { cursor: pointer; transition: background var(--t), color var(--t); }
  .chip.clk:hover { background: var(--accent-soft); color: var(--accent); }
  .pill { display: inline-flex; align-items: center; gap: 5px; font-size: 12px; color: var(--ink-2); background: var(--fill);
    border-radius: 6px; padding: 2px 7px; white-space: nowrap; font-variant-numeric: tabular-nums; }
  .pill b { font-weight: 600; color: var(--ink); }
  .pill.acc { background: var(--ok-soft); color: var(--ok); } .pill.acc b { color: var(--ok); }
  .pill.amb { background: var(--warn-soft); color: var(--warn); } .pill.amb b { color: var(--warn); }
  .pill.red { background: var(--bad-soft); color: var(--bad); } .pill.red b { color: var(--bad); }
  .pill.mono { font-family: var(--mono); }
  .b { font: inherit; font-size: 14px; font-weight: 500; padding: 6px 12px; border-radius: 6px; border: 0; cursor: pointer;
    white-space: nowrap; line-height: 1.2; background: var(--fill); color: var(--ink-2);
    transition: background var(--t), color var(--t); }
  .b:hover { background: var(--fill-2); color: var(--ink); }
  .b.pri { background: var(--accent); color: var(--accent-ink); font-weight: 500; }
  .b.pri:hover { filter: brightness(1.07); background: var(--accent); color: var(--accent-ink); }
  .b.gho { background: transparent; }
  .b.gho:hover { background: var(--fill); color: var(--ink); }
  .b.dan:hover { color: var(--bad); background: var(--bad-soft); }
  .b.dan.pri, .b.armed { color: #fff; background: var(--bad); }
  .b:disabled { opacity: .5; cursor: default; filter: none; }
  .b.on { background: var(--fill-2); color: var(--ink); }
  .btn { font: inherit; font-size: 14px; font-weight: 500; padding: 6px 14px; border: 0; border-radius: 6px; background: var(--accent);
    color: var(--accent-ink); cursor: pointer; white-space: nowrap; }
  .btn:hover { filter: brightness(1.07); }
  .btn:disabled { opacity: .5; cursor: default; }
  .btn.big { padding: 9px 16px; font-size: 14px; text-decoration: none; display: inline-block; }
  .a-link { color: var(--accent); font-size: 14px; }
  .a-link:hover { text-decoration: underline; }
  .guide-link { font: inherit; font-size: 13px; color: var(--accent); cursor: pointer; background: none; border: 0; padding: 0; text-align: left; }
  .guide-link:hover { text-decoration: underline; }
  .actions { display: flex; gap: 14px; }
  .actions button { background: none; border: 0; padding: 0; font: inherit; font-size: 13px; cursor: pointer; color: var(--ink-2); }
  .actions button:hover { color: var(--ink); }
  .actions button.danger:hover { color: var(--bad); }

  /* inputs — filled, no line */
  input.in, textarea.in, select.in { font: inherit; font-size: 14px; padding: 8px 10px; border: 0; border-radius: 6px;
    background: var(--fill); color: var(--ink); min-width: 0; flex: 1; transition: background var(--t); }
  input.in[type=password], .mono-in { font-family: var(--mono); }
  input.in:hover, textarea.in:hover, select.in:hover { background: var(--fill-2); }
  input.in:focus, textarea.in:focus, select.in:focus { outline: none; background: var(--bg); box-shadow: 0 0 0 1.5px var(--accent), 0 0 0 4px var(--accent-soft); background: var(--panel); }
  input.in::placeholder, textarea.in::placeholder { color: var(--ink-3); }
  select.in { cursor: pointer; flex: none; }
  input.in.search { width: 100%; margin-bottom: 12px; }
  .find { position: relative; margin-bottom: 12px; }
  .find input { width: 100%; font: inherit; font-size: 14px; padding: 9px 12px 9px 34px; border: 0; border-radius: 8px;
    background: var(--fill); color: var(--ink); transition: background var(--t); }
  .find input:hover { background: var(--fill-2); }
  .find input:focus { outline: none; background: var(--bg); box-shadow: 0 0 0 1.5px var(--accent), 0 0 0 4px var(--accent-soft); background: var(--panel); }
  .find::before { content: "⌕"; position: absolute; left: 11px; top: 50%; transform: translateY(-50%); color: var(--ink-3);
    font-size: 15px; pointer-events: none; }
  label.chk { display: inline-flex; align-items: center; gap: 7px; font-size: 14px; color: var(--ink-2); cursor: pointer; white-space: nowrap; }
  .enable { display: flex; gap: 8px; }
  .filters { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
  /* the source selector: three places models can come from, stated before
     you search so the curated list never reads as the whole world */
  .dp-src { display: inline-flex; gap: 2px; padding: 3px; margin-bottom: 12px;
    background: var(--fill); border-radius: 8px; }
  .dp-srcb { height: 26px; padding: 0 11px; border: 0; border-radius: 6px; background: transparent;
    font: inherit; font-size: 13px; line-height: 1; color: var(--ink-2); cursor: pointer;
    white-space: nowrap; transition: background var(--t), color var(--t); }
  .dp-srcb:hover { background: var(--panel-2); color: var(--ink); }
  .dp-srcb.on { background: var(--panel); color: var(--ink); font-weight: 500; }
  .dp-srcb:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent); }
  /* the deploy picker: search first, then the filter row under it */
  .dp-find { display: flex; align-items: center; gap: 10px; margin-bottom: 9px; flex-wrap: wrap; }
  .dp-sub { display: flex; align-items: center; gap: 10px; margin-bottom: 11px; min-width: 0; }
  /* segmented control — pills, like the tabs */
  .fchips { display: flex; gap: 2px; flex: none; }
  .fchip { font: inherit; font-size: 13px; padding: 6px 12px; border: 0; border-radius: 6px; background: transparent;
    color: var(--ink-2); cursor: pointer; white-space: nowrap; transition: background var(--t), color var(--t); }
  .fchip:hover { background: var(--fill); color: var(--ink); }
  .fchip.on { background: var(--panel); color: var(--ink); font-weight: 500; }
  /* family tabs over the model list — the nav's pill treatment, with counts */
  .mtabs { display: flex; align-items: center; gap: 3px; flex-wrap: wrap; margin-bottom: 12px; }
  .mtabs .fchip { display: inline-flex; align-items: center; gap: 7px; font-size: 14px; font-weight: 500; padding: 6px 10px; }
  .mtabs .fchip.on { font-weight: 500; }
  /* the family's own mark, sized exactly as the Deploy page's org pills size
     theirs — 20px box, 17px of ink, no square behind it, so the glyph carries
     the vendor's colour and nothing else does. You read these rows by logo
     first and name second, and at 13px the name was outweighing the mark;
     20px puts them level. */
  .mtabs .mark2 { width: 20px; height: 20px; border-radius: 6px; background: none; font-size: 12px; }
  .mtabs .mark2 svg { width: 17px; height: 17px; }
  .mtabs .tn2 { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
  .mtabs .fchip.on .tn2 { color: var(--ink-2); }
  /* the company filter — one line of org pills, scrolls rather than wraps */
  /* with an overflow control the row no longer has to scroll sideways */
  .dp-orgs { display: flex; align-items: center; gap: 4px; min-width: 0; flex-wrap: wrap; }
  .dp-more { font-weight: 500; color: var(--ink-3); }
  /* no text-transform: it is what turned "zai-org" into "Zai-Org". The names
     are already cased correctly, and an unknown slug stays as the Hub spells
     it rather than being mangled into a word that is not a company. */
  .dp-orgs .fchip { display: inline-flex; align-items: center; gap: 7px; padding: 5px 10px; flex: none;
    font-size: 14px; font-weight: 500; }
  .dp-orgs .fchip.on { font-weight: 600; }
  /* You pick a company by its logo before you read its name, so the mark is
     sized to the label rather than tucked beside it: a 20px box reads at the
     same weight as 13px text, and it is the one size an avatar fetched from
     the Hub is still legible at. */
  .dp-orgs .omark { width: 20px; height: 20px; border-radius: 6px; background: none; font-size: 12px; }
  .dp-orgs .omark svg { width: 17px; height: 17px; }
  .dp-orgs .tn2 { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); }
  .dp-orgs .fchip.on .tn2 { color: var(--ink-2); }
  /* skills — a library of cards, each with its own identity glyph */
  .sec-empty { font-size: 14px; color: var(--ink-3); padding: 14px 16px; border-radius: var(--radius); background: var(--panel-2); }
  .sk-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
  .skcard { background: var(--panel); border-radius: var(--radius); padding: 16px; cursor: pointer;
    display: flex; flex-direction: column; gap: 10px; min-width: 0; transition: background var(--t); }
  .skcard:hover { background: var(--panel-2); }
  .sk-h { display: flex; align-items: center; gap: 11px; min-width: 0; }
  .sglyph { width: 36px; height: 36px; border-radius: 8px; flex: none; display: inline-flex; align-items: center;
    justify-content: center; overflow: hidden; }
  .sglyph svg { width: 25px; height: 25px; display: block; }
  .sk-t { min-width: 0; flex: 1; }
  .sk-n { font-weight: 600; font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sk-p { font-family: var(--mono); font-size: 12px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sk-d { font-size: 14px; color: var(--ink-2); line-height: 1.45; display: -webkit-box; -webkit-line-clamp: 2;
    -webkit-box-orient: vertical; overflow: hidden; }
  .sk-tags { display: flex; gap: 5px; flex-wrap: wrap; }
  .sk-tools { display: flex; gap: 4px; flex-wrap: wrap; }
  .sk-tools .chip { font-size: 12px; padding: 2px 7px; }
  .sk-acts { display: flex; gap: 6px; margin-top: auto; padding-top: 4px; opacity: 0; transition: opacity var(--t); }
  .skcard:hover .sk-acts, .skcard:focus-within .sk-acts { opacity: 1; }
  .sk-acts .b { padding: 4px 10px; font-size: 13px; }
  .sk-body { margin: 14px 0 4px; font-size: 14px; line-height: 1.6; max-height: 40vh; overflow: auto; }
  .sk-raw { margin: 0; font-family: var(--mono); font-size: 13px; white-space: pre-wrap; word-break: break-word;
    color: var(--ink-2); max-height: 40vh; overflow: auto; }
  .sk-frow { display: grid; grid-template-columns: 1fr 1fr; gap: 10px 12px; margin-bottom: 10px; }
  .sk-frow .dp-field input.in, .sk-frow .dp-field select.in { width: 100%; background: var(--panel-2); }
  .sk-err { color: var(--bad); font-size: 13px; margin: -6px 0 8px; }
  .sk-tsel { display: flex; gap: 4px; flex-wrap: wrap; }
  .sk-tsel .fchip { padding: 4px 9px; font-size: 13px; }
  .sk-split { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .sk-ed { width: 100%; min-height: 220px; resize: vertical; font-family: var(--mono); font-size: 13px; line-height: 1.55;
    background: var(--panel-2); }
  .sk-prev { background: var(--panel-2); border-radius: 8px; padding: 10px 12px; font-size: 14px; overflow: auto;
    min-height: 220px; max-height: 40vh; }
  @media (max-width: 1100px) { .sk-split, .sk-frow { grid-template-columns: 1fr; } }

  /* provider setup — one card per provider, its auth types as a toggle */
  .auth-glabel { font-size: 14px; font-weight: 600; color: var(--ink-2); margin: 18px 0 8px; }
  /* start-aligned so a card that opens a form never stretches its neighbours */
  .auth-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 10px; align-items: start; }
  /* Collapsed, a card says four things: whose it is, what it's called, what
     state it's in, and the one action that changes that. The surface is the
     same neutral panel in every state — never a wash, never a rail, never an
     outline. Everything else waits until it's opened. */
  /* THE ACTIVE MARKER — the badge, and nothing else.
     A ring of pixel blocks, a dither spread across the card, an edge rail, a
     colour wash: each was tried and each was the same mistake, which is
     decorating a card to say something one word already says. The state badge
     on the head row names the state outright, in four shapes as well as four
     colours. That is the marker. */
  .acard { position: relative; overflow: hidden; background: var(--panel); border-radius: var(--radius);
    padding: 12px 14px; display: flex; flex-direction: column; gap: 10px; min-width: 0;
    transition: background var(--t); }
  /* collapsed cards are one height BY CONSTRUCTION — the grid is a matrix,
     and an opened card grows inside its own cell (the grid is align-items:
     start, so no sibling is ever stretched by its neighbour) */
  .acard:not(.open) { height: 63px; }
  .acard .ac-h { height: 39px; }
  /* No rail: a stripe that four cards have and three don't reads as a
     rendering fault. One badge carries the state instead — same element, same
     place, on every card. */
  .acard:hover { background: var(--panel-2); }
  .acard.open { background: var(--panel-2); }
  /* the container the panel is measured against */
  #auth-cards { position: relative; }
  /* THE FLOATING PANEL. It sits above the grid, exactly over the card it came
     from, so the rows behind it never move. A layered surface is the one
     place a raise is honest — everything flat still has no shadow — and it is
     kept subtle: a background step does most of the work. */
  .ac-panel { position: absolute; z-index: 20; background: var(--panel-2);
    box-shadow: 0 12px 32px -8px var(--dim); overflow: auto;
    animation: ac-rise 140ms cubic-bezier(.2,.7,.2,1); }
  @media (prefers-reduced-motion: reduce) { .ac-panel { animation: none; } }
  @keyframes ac-rise { from { opacity: 0; transform: translateY(-3px); } to { opacity: 1; transform: none; } }
  /* the card underneath keeps its 63px footprint and simply waits */
  .ac-under { visibility: hidden; }
  .ac-h { display: flex; align-items: center; gap: 11px; min-width: 0; cursor: pointer; }
  /* an inline span puts a LETTER stand-in on the text baseline, which is why
     it used to sit high and left of every real glyph. The container centres
     both cases identically, so a mark is a mark. */
  .ac-h .bigmark { width: 32px; height: 32px; border-radius: 8px; flex: none;
    display: inline-flex; align-items: center; justify-content: center;
    overflow: hidden; line-height: 1; }
  .ac-h .bigmark svg { width: 32px; height: 32px; display: block; }
  /* the letter stand-in matches the glyphs' optical weight: the fit viewBox
     puts every real mark's ink at .62 of the 32px box (19.8px), and a 600
     capital at 26px has a cap height in the same place */
  /* 27px is where a 600 capital's measured cap height lands on 19.8px, the
     same ink the fit viewBox gives every real glyph. The nudge is the
     measured gap between the glyph's ink centre and the box centre —
     capitals carry no descender, so uncorrected they ride low. */
  .ac-h .bigmark.letter { font-family: var(--sans); font-size: 30px; font-weight: 600;
    color: var(--ink-2); transform: translateY(-0.9px); }
  .bigmark.letter { font-family: var(--sans); font-size: 16px; font-weight: 600; color: var(--ink-2); }
  .ac-h .ft { min-width: 0; flex: 1; }
  .ac-top { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .ac-h .fn { font-weight: 600; font-size: 16px; line-height: 1.3; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; min-width: 0; display: flex; align-items: baseline; gap: 5px; }
  .ac-nm { flex: none; }
  .ac-nv { font-weight: 400; font-size: 14px; color: var(--ink-3); min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* two weights on the card: 600 for the name, regular for everything else */
  .ac-act { flex: none; font-weight: 400; font-size: 14px; color: var(--ink-2); }
  .acard:hover .ac-act { color: var(--ink); }
  .ac-h .fd { font-size: 12px; line-height: 1.35; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; background: none; padding: 0; font-family: var(--mono); }
  .ac-h .fd.sans { font-family: var(--sans); font-style: italic; }
  /* the connection state is a chip on the name row, not a row of its own */
  .ac-via { font-size: 14px; color: var(--ink-3); white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; margin-top: 1px; }
  /* The state badge. Four states told apart by surface, colour AND the shape
     of the glyph — filled dot, hollow ring, amber diamond, faint dot — so it
     survives a colour-blind reading and a glance. */
  .ac-st { display: inline-flex; align-items: center; gap: 5px; flex: none; height: 20px; padding: 0 8px;
    border-radius: 6px; font-size: 12px; font-weight: 600; white-space: nowrap; }
  .ac-stg { width: 7px; height: 7px; border-radius: 50%; flex: none; box-sizing: border-box; }
  /* Current is the only slanted badge on the page — a tag pinned to the card
     rather than a word sitting in the row. The label is counter-skewed by
     the same angle so it reads upright instead of italicised, and the skew
     is bounded (8deg over a 20px tall pill is ~2.8px of lean) so it stays
     inside the card and cannot reach the name. Ready, Not active and Not
     connected stay square, so Current differs in SHAPE as well as colour. */
  .ac-st.cur { background: var(--accent); color: var(--accent-ink);
    transform: skewX(-8deg); padding: 0 9px; margin-right: 2px; }
  .ac-st.cur > * { transform: skewX(8deg); }
  /* THE CORNER TAG — one rule, every card that carries a Current tag.
     Pinned to the card's top-right, it keeps the 8deg slant and rounds its
     top-right to the card's own radius so it follows the curve instead of
     looking pasted on. It bleeds 4px past the edge and the card's overflow
     clips it flat: the slant then reads as deliberate rather than as a pill
     that drifted into the corner. Picked over a tucked pill and a flush tag
     with no overhang, rendered side by side at 1x and 2x in both themes. */
  .cornertag { position: absolute; top: 0; right: -4px; z-index: 2; height: 22px;
    padding: 0 13px 0 11px; margin-right: 0; border-radius: 0 var(--radius) 0 9px; }
  .ac-st.cur .ac-stg { background: currentColor; }
  .ac-st.rdy { background: var(--ok-soft); color: var(--ok); }
  .ac-st.rdy .ac-stg { border: 1.5px solid currentColor; }
  .ac-st.idle { background: var(--warn-soft); color: var(--warn); }
  .ac-st.idle .ac-stg { border: 1.5px solid currentColor; border-radius: 1px; width: 6px; height: 6px;
    transform: rotate(45deg); }
  .ac-st.off { background: var(--fill); color: var(--ink-3); font-weight: 500; }
  .ac-st.off .ac-stg { background: currentColor; opacity: .45; width: 5px; height: 5px; }
  /* what a connected card earns its height with */
  .ac-meta { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 14px; color: var(--ink-3); margin-top: 9px; }
  .ac-mb { white-space: nowrap; min-width: 0; }
  .ac-mb b { color: var(--ink-2); font-weight: 600; }
  .ac-mb code { font-family: var(--mono); font-size: 13px; color: var(--ink-2); background: none; padding: 0; }
  .ac-mb i { font-style: normal; font-family: var(--mono); font-size: 13px; color: var(--ink-3);
    margin-left: 6px; max-width: 16ch; overflow: hidden; text-overflow: ellipsis; display: inline-block;
    vertical-align: bottom; }
  /* The auth-type control: one filled track, equal segments, two aligned
     rows when a family offers five ways in. Segments are their own class —
     never .fchip/.live, whose widths and fills belong to other controls. */
  .ac-types { display: flex; flex-wrap: wrap; gap: 3px; padding: 3px; margin-top: 1px;
    background: var(--fill); border-radius: 8px; }
  .acard.open .ac-types { background: var(--fill); }
  .ac-seg { display: inline-flex; align-items: center; gap: 5px; position: relative; flex: none;
    height: 26px; padding: 0 10px; border: 0; border-radius: 6px; background: transparent;
    font: inherit; font-size: 13px; line-height: 1; color: var(--ink-2); cursor: pointer;
    white-space: nowrap; max-width: 100%; transition: background var(--t), color var(--t); }
  .ac-seg:hover { background: var(--panel-2); color: var(--ink); }
  .acard.open .ac-seg:hover { background: var(--panel); }
  .ac-seg.on { background: var(--panel); color: var(--ink); font-weight: 500; }
  .ac-seg.on:hover { filter: brightness(1.06); }
  .ac-seg:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent); }
  .ac-seg .ac-lb { overflow: hidden; text-overflow: ellipsis; }
  /* The marker is a child of its segment, so it cannot land in the gap.
     Single-purpose class names on purpose: a shared modifier like .cfg or
     .act would inherit padding from the Config table and burst the dot. */
  .ac-mkdot { flex: none; width: 6px; height: 6px; padding: 0; border-radius: 50%;
    background: var(--warn); align-self: center; }
  .ac-seg.on .ac-mkdot { background: var(--accent-ink); }
  .ac-mktick { flex: none; font-size: 12px; line-height: 1; padding: 0; align-self: center; }
  .ac-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ink-3); flex: none; }
  .ac-dot.cfg { background: var(--warn); }
  .ac-one { font-size: 14px; color: var(--ink-3); }
  .ac-d { font-size: 14px; line-height: 1.45; color: var(--ink-3); margin-bottom: 8px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  /* switching method crossfades the block instead of jumping */
  .ac-body { transition: opacity 140ms ease; }
  .ac-body.fade { opacity: 0; }
  .ac-models { margin-top: 9px; }
  .ac-mh { font-size: 14px; color: var(--ink-3); margin-bottom: 6px; }
  .ac-models .chip { font-size: 12px; padding: 2px 7px; }
  /* two rows of models, then +N */
  /* one row by default; +N opens the rest */
  .ac-models .chips.clamp { max-height: 21px; overflow: hidden; }
  .ac-models .chip.more { cursor: pointer; border: 0; font: inherit; font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px;
    color: var(--ink-3); background: var(--fill); border-radius: 6px; padding: 2px 7px; margin-top: 4px; }
  .ac-models .chip.more:hover { color: var(--accent); }
  .ac-models .chip { background: var(--fill); }
  .ap-form { margin-top: 6px; }
  .ap-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 7px 10px; margin-bottom: 7px; }
  .ap-fields .dp-field { gap: 3px; }
  .ap-fields .kh-l { font-size: 12px; }
  .ap-fields input.in { padding: 6px 9px; }
  .ap-fields .dp-field input.in { background: var(--panel); width: 100%; }
  .ap-fields .kh-n { font-size: 12px; line-height: 1.4; }
  .ap-fields .envn { font-family: var(--mono); font-size: 12px; color: var(--ink-3); }
  .ap-note { font-size: 13px; color: var(--ink-2); background: var(--panel); border-radius: 8px; padding: 6px 9px; margin-bottom: 7px; }
  /* one action row: Save is the only filled button, the rest are quiet */
  .ap-acts { display: flex; align-items: center; gap: 6px; flex-wrap: nowrap; }
  .ap-acts { margin-top: 2px; }
  .ap-acts .b { padding: 6px 11px; font-size: 14px; }
  .ap-acts .b.gho { background: transparent; color: var(--ink-3); }
  .ap-acts .b.gho:hover { background: var(--fill); color: var(--ink); }
  .ac-doc { margin-left: auto; color: var(--ink-3); font-size: 14px; text-decoration: none; padding: 0 2px; }
  .ac-doc:hover { color: var(--accent); }
  .oauth-paste { display: none; gap: 8px; margin-top: 10px; max-width: 560px; }
  .oauth-paste.on { display: flex; }
  .oauth-paste input.in { background: var(--panel); }

  /* the gated-model notice and its token field */
  /* The filter row carries filters and nothing else. A standing "Hugging Face
     token: not set" was a status nobody asked for, parked in the whitespace
     beside the org pills — and it was answering a question that only comes up
     on a gated model, where .hf-notice already asks it in context, with the
     model's name and the reason. The HF card up on this same page still has
     its own Add key. */
  .dp-sub .dp-status { margin-left: auto; }
  .hf-form .hf-row { display: flex; gap: 8px; align-items: center; max-width: 460px; }
  .hf-form .hf-row input.in { background: var(--panel); }
  .hf-form .hf-foot { margin-top: 6px; font-size: 13px; }

  /* the finish: an arrival, not a spec dump */

  /* dual-mode search: the mode is a control you can see and flip */
  .dp-mode { display: inline-flex; align-items: center; gap: 6px; flex: none; height: 32px;
    padding: 0 11px; border: 0; border-radius: 8px; background: var(--fill); font: inherit;
    font-size: 13px; color: var(--ink-2); cursor: pointer; white-space: nowrap;
    transition: background var(--t), color var(--t); }
  .dp-mode:hover { background: var(--panel-2); color: var(--ink); }
  .dp-mode.on { background: var(--accent); color: var(--accent-ink); font-weight: 600; }
  .dp-mode:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent); }
  /* the marker is a filled pip when the agent will answer, a hollow one when
     the Hub will — shape, not only colour */
  .dp-modem { width: 7px; height: 7px; border-radius: 50%; flex: none;
    box-shadow: inset 0 0 0 2px var(--ink-3); }
  .dp-mode.on .dp-modem { background: currentColor; box-shadow: none; }
  /* pinned: the user chose, so it is no longer a guess */
  .dp-mode.pinned { box-shadow: inset 0 0 0 2px var(--accent); }

  /* the agent's answer */
  .dp-groups { display: block; }
  .ask-head { margin: 0 0 14px; }
  .ask-int { font-size: 15px; color: var(--ink); line-height: 1.5; margin-bottom: 7px; max-width: 82ch; }
  .ask-src { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; font-size: 13px;
    color: var(--ink-3); margin-bottom: 8px; }
  .ask-badge { height: 19px; padding: 0 8px; border-radius: 6px; background: var(--fill);
    color: var(--ink-2); font-size: 12px; font-weight: 600; display: inline-flex; align-items: center; }
  .ask-badge.on { background: var(--accent); color: var(--accent-ink); }
  .ask-srcx { min-width: 0; }
  .ask-link { border: 0; background: none; padding: 0; font: inherit; font-size: 13px;
    color: var(--accent); cursor: pointer; text-decoration: underline; }
  .ask-note { font-size: 13px; color: var(--warn); line-height: 1.5; margin-bottom: 5px; max-width: 82ch; }
  .ask-filters { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 4px; }
  .ask-flbl { font-size: 13px; color: var(--ink-3); }
  .ask-chip { display: inline-flex; align-items: center; gap: 5px; height: 24px; padding: 0 4px 0 9px;
    border-radius: 8px; background: var(--fill); font-size: 13px; color: var(--ink-2); }
  .ask-chip b { font-weight: 600; color: var(--ink-3); font-size: 12px; }
  .ask-x { border: 0; background: none; padding: 0 5px; font: inherit; font-size: 15px; line-height: 1;
    color: var(--ink-3); cursor: pointer; border-radius: 6px; }
  .ask-x:hover { color: var(--ink); background: var(--panel-2); }
  .ask-x:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent); }
  /* a group: its title, why it is a group, how many are in it */
  .dp-gh { display: flex; align-items: baseline; gap: 9px; margin: 16px 0 8px; flex-wrap: wrap; }
  .dp-gt { font-size: 14px; font-weight: 600; color: var(--ink); }
  .dp-gr { font-size: 13px; color: var(--ink-3); min-width: 0; }
  .dp-gn { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); margin-left: auto; }
  .ask-best { margin-left: auto; height: 19px; padding: 0 8px; border-radius: 6px; flex: none;
    background: var(--accent); color: var(--accent-ink); font-size: 12px; font-weight: 600;
    display: inline-flex; align-items: center; }
  .mcard.best { background: var(--panel-2); }
  .sk-mcard { height: 104px; border-radius: var(--radius); }
  .dp-asking .dp-asksteps { margin-bottom: 14px; }
  /* the deploy job as steps: what has happened, what is happening now */
  .dp-steps { list-style: none; margin: 4px 0 12px; padding: 0; display: flex; flex-direction: column;
    align-items: stretch; gap: 2px; }
  /* width and flex stated outright rather than leaning on stretch: a step
     that collapses to its marker renders its label one character per line */
  .dp-step { display: flex; align-items: flex-start; gap: 9px; padding: 5px 0; font-size: 14px;
    color: var(--ink-3); line-height: 1.45; width: 100%; align-self: stretch; }
  .dp-step.live { color: var(--ink); }
  .dp-stepm { width: 14px; height: 14px; border-radius: 50%; flex: none; margin-top: 2px;
    background: var(--fill); position: relative; }
  /* done: a filled accent pip. live: a ring that breathes. The two differ in
     shape as well as colour, like every other state on the page. */
  .dp-step.did .dp-stepm { background: var(--accent); }
  .dp-step.live .dp-stepm { background: transparent; box-shadow: inset 0 0 0 2px var(--accent); }
  @media (prefers-reduced-motion: no-preference) {
    .dp-step.live .dp-stepm { animation: steppulse 1.5s ease-in-out infinite; }
  }
  @keyframes steppulse { 0%, 100% { opacity: 1; } 50% { opacity: .45; } }
  .dp-stept { flex: 1 1 auto; min-width: 0; word-break: break-word; }
  /* the fit step's waiting shape — each piece is the size of the thing it
     stands in for, so nothing moves when the data lands */
  .dp-skel .sk { animation: skpulse 1.4s ease-in-out infinite; }
  .dp-skel .lcd .sk { display: inline-block; vertical-align: baseline; }
  .sk-num { width: 42px; height: 15px; border-radius: 6px; margin-right: 5px; }
  .sk-lbl { width: 34px; height: 11px; border-radius: 6px; }
  .sk-name { width: 128px; height: 15px; border-radius: 6px; }
  @keyframes skpulse { 0%, 100% { opacity: 1; } 50% { opacity: .55; } }
  @media (prefers-reduced-motion: reduce) { .dp-skel .sk { animation: none; } }
  /* empty states & skeletons */
  .zero { background: var(--panel-2); border-radius: var(--radius); padding: 30px 22px 32px; text-align: center; }
  .zero .zart { color: var(--ink-2); margin: 0 auto 12px; width: 140px; }
  .zero .zart svg { width: 140px; height: 100px; display: block; }
  .zero .zt { font-weight: 600; font-size: 15px; margin-bottom: 4px; }
  .zero .zd { font-size: 14px; color: var(--ink-3); max-width: 54ch; margin: 0 auto; line-height: 1.55; }
  .zero .zact { margin-top: 14px; display: flex; justify-content: center; gap: 8px; }
  .empty { color: var(--ink-3); padding: 36px 20px; text-align: center; font-size: 14px; }
  .sk { border-radius: 8px; background: linear-gradient(90deg, var(--panel) 25%, var(--panel-2) 50%, var(--panel) 75%);
    background-size: 200% 100%; animation: shimmer 1.2s linear infinite; height: 14px; margin: 8px 0; }
  .sk.card { height: 84px; margin: 0; }
  .skgrid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 12px; margin-top: 12px; }
  @keyframes shimmer { from { background-position: 200% 0; } to { background-position: -200% 0; } }

  /* key/value detail, code boxes, probes, banners */
  .kvs { display: grid; grid-template-columns: 104px 1fr; gap: 4px 14px; align-items: baseline; font-size: 14px; margin: 10px 0 0; }
  .kvs dt { color: var(--ink-3); font-size: 12px; letter-spacing: 0; font-weight: 600; padding-top: 2px; }
  .kvs dd { margin: 0; font-family: var(--mono); font-size: 13px; word-break: break-word; }
  .kvs dd.wrap { white-space: pre-wrap; font-family: var(--sans); }
  .secret { color: var(--ink-3); letter-spacing: 0; }
  .jsonbox { margin-top: 12px; }
  .jsonbox pre { margin: 0; background: var(--panel-2); border-radius: 8px; padding: 11px 12px; font-family: var(--mono);
    font-size: 13px; line-height: 1.55; overflow: auto; max-height: 320px; white-space: pre; }
  .jsonbox .jh { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
  .jsonbox .jt { font-size: 12px; font-weight: 600; letter-spacing: 0; color: var(--ink-3); }
  .probe { margin-top: 12px; border-radius: 8px; padding: 11px 13px; font-size: 14px; background: var(--panel-2); }
  .probe.ok { background: var(--ok-soft); }
  .probe.bad { background: var(--bad-soft); }
  .probe .ph2 { display: flex; align-items: center; gap: 8px; font-weight: 600; }
  .probe .pe, .pe { font-family: var(--mono); font-size: 13px; color: var(--bad); margin-top: 6px; word-break: break-word; }
  .toolgrid { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .toolgrid .tk { font-family: var(--mono); font-size: 12px; background: var(--fill); color: var(--ink-2); padding: 3px 8px; border-radius: 6px; }
  .banner { display: flex; align-items: center; gap: 12px; padding: 11px 14px; border-radius: 8px; margin-bottom: 16px; font-size: 14px;
    background: var(--warn-soft); color: var(--warn); }
  .banner b { font-weight: 600; }
  .banner code { font-family: var(--mono); font-size: 13px; }
  .banner .sp { flex: 1; }
  .comp { padding: 14px 16px; margin-bottom: 16px; display: none; }
  .comp.on { display: block; }
  .comp .r { display: flex; gap: 8px; margin-bottom: 8px; flex-wrap: wrap; }
  .comp .r > * { flex: 1; min-width: 150px; }
  .comp .r > .fit { flex: none; min-width: 0; }
  .comp textarea.in { width: 100%; min-height: 118px; resize: vertical; line-height: 1.55; font-family: var(--mono); }
  .comp .foot { display: flex; align-items: center; gap: 12px; margin-top: 10px; }
  .comp .hint { font-size: 13px; color: var(--ink-3); flex: 1; line-height: 1.5; }
  .comp .hint code { font-family: var(--mono); background: var(--fill); padding: 1px 5px; border-radius: 6px; }
  /* One square, one glyph size, everywhere. The ink inside is normalised by
     the fit viewBox, so a 62%-of-box glyph is 62% for every vendor. */
  .mark2 { width: 22px; height: 22px; border-radius: 6px; flex: none; display: inline-flex; align-items: center; justify-content: center;
    background: var(--fill); color: var(--ink); overflow: hidden; }
  .mark2 svg { width: 22px; height: 22px; display: block; }
  .mark2.letter { font-family: var(--sans); font-size: 13px; font-weight: 600; color: var(--ink-2); }
  .refresh { display: inline-flex; align-items: center; gap: 6px; font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); }

  /* ---- modal sheet ---- */
  #modal { position: fixed; inset: 0; background: var(--dim); display: none; align-items: center; justify-content: center;
    padding: 20px; z-index: 30; backdrop-filter: blur(3px); }
  #modal.on { display: flex; animation: fade 140ms ease-out; }
  @keyframes fade { from { opacity: 0; } }
  .sheet { position: relative; background: var(--panel); border-radius: 12px; max-width: 560px; width: 100%; max-height: 86vh;
    overflow-y: auto; padding: 22px 24px; animation: rise .16s ease-out; outline: none; }
  /* the pairing: model → provider, both with their real marks */
  .pair { display: grid; grid-template-columns: minmax(0,1fr) auto minmax(0,1fr); gap: 12px; align-items: center; margin: 2px 0 16px; }
  .pair.sm { margin: 0; gap: 8px; grid-template-columns: auto auto auto; justify-content: start; }
  .pair.sm .omark, .pair.sm .bigmark { width: 20px; height: 20px; border-radius: 6px; font-size: 12px; }
  .pair.sm .omark svg, .pair.sm .bigmark svg { width: 12px; height: 12px; }
  /* spec tiles */

  .tile { background: var(--panel-2); border-radius: 8px; padding: 9px 11px; min-width: 0; }
  .tile .tl { font-size: 12px; color: var(--ink-3); letter-spacing: 0; font-weight: 600; }
  /* cost — the headline number */

  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }
  .sheet.wide { max-width: 800px; }
  .sheet:focus-visible { box-shadow: none; }
  .sheet h3 { margin: 0 0 3px; font-size: 16px; font-weight: 600; letter-spacing: -.01em; }
  .sheet h4 { margin: 16px 0 6px; font-size: 12px; letter-spacing: 0; color: var(--ink-3); }
  .sheet .sub { color: var(--ink-3); font-size: 13px; font-family: var(--sans); font-variant-numeric: tabular-nums; margin-bottom: 14px; word-break: break-all; }
  .sheet ol { margin: 0; padding-left: 20px; }
  .sheet ol li { margin: 7px 0; font-size: 14px; line-height: 1.5; }
  .sheet .free { font-size: 14px; color: var(--ink-2); background: var(--panel-2); border-radius: 8px; padding: 10px 12px; margin: 14px 0; }
  .sheet .cta { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; margin-top: 16px; }
  .sheet .notes { list-style: none; padding: 0; margin: 12px 0 0; }
  .sheet .notes li { font-size: 14px; color: var(--ink-2); padding: 4px 0 4px 16px; position: relative; }
  .sheet .notes li::before { content: "·"; position: absolute; left: 4px; color: var(--accent); }
  .rt, .plat { background: var(--panel-2); border-radius: 8px; padding: 11px 13px; margin: 8px 0; }
  .rt .rn, .plat-n { font-weight: 600; font-size: 14px; }
  .rt .rnote, .rnote { color: var(--ink-3); font-size: 13px; margin: 2px 0; }
  .rt code { display: block; font-family: var(--mono); font-size: 13px; background: var(--fill); padding: 7px 9px; border-radius: 6px;
    overflow-x: auto; white-space: pre; margin: 6px 0 2px; }
  .skill-box { background: var(--accent-soft); border-radius: 8px; padding: 13px 15px; margin: 16px 0; }
  .skill-t { font-weight: 600; font-size: 14px; margin-bottom: 4px; }
  .skill-b { font-size: 14px; color: var(--ink-2); line-height: 1.5; margin-bottom: 10px; }
  .plat-top { display: flex; align-items: center; gap: 9px; }
  .plat-k { font-size: 12px; color: var(--ink-3); background: var(--fill); padding: 2px 8px; border-radius: 6px; }
  .plat-links { display: flex; gap: 16px; margin-top: 8px; }
  .sheet .x { position: absolute; top: 12px; right: 14px; background: none; border: 0; font-size: 24px; color: var(--ink-3);
    cursor: pointer; line-height: 1; }
  .sheet .x:hover { color: var(--ink); }
  #toast { position: fixed; bottom: 22px; left: 50%; transform: translateX(-50%) translateY(6px); background: var(--ink); color: var(--bg);
    padding: 10px 16px; border-radius: 8px; font-size: 14px; opacity: 0; transition: opacity var(--t), transform var(--t);
    pointer-events: none; z-index: 40; max-width: 80vw; }
  #toast.on { opacity: 1; transform: translateX(-50%); }
  #toast.err { background: var(--bad); color: #fff; }

  /* ---- command palette (⌘K) ---- */
  #palette { position: fixed; inset: 0; background: var(--dim); display: none; align-items: flex-start; justify-content: center;
    padding: 12vh 16px 0; z-index: 35; backdrop-filter: blur(3px); }
  #palette.on { display: flex; animation: fade 140ms ease-out; }
  .pal { width: 100%; max-width: 620px; background: var(--panel); border-radius: 12px; overflow: hidden; animation: rise .14s ease-out; }
  .pal input { width: 100%; font: inherit; font-size: 16px; padding: 14px 16px; border: 0; background: var(--panel-2); color: var(--ink); }
  .pal input:focus { outline: none; box-shadow: none; }
  .pal-list { max-height: 52vh; overflow-y: auto; padding: 6px; }
  .pal-g { font-size: 13px; font-weight: 600; color: var(--ink-3); padding: 8px 10px 4px; }
  .pal-i { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 8px; cursor: pointer; font-size: 14px; }
  .pal-i.on, .pal-i:hover { background: var(--fill); }
  .pal-i .pk { font-family: var(--mono); font-size: 12px; color: var(--ink-3); background: var(--fill); padding: 1px 6px; border-radius: 6px; flex: none; }
  .pal-i .pt { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .pal-i .ps { color: var(--ink-3); font-size: 13px; margin-left: auto; white-space: nowrap; font-family: var(--mono); }
  .pal-f { display: flex; gap: 14px; padding: 8px 14px; background: var(--panel-2); font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); }
  .pal-f b { color: var(--ink-2); background: var(--fill); padding: 0 5px; border-radius: 6px; font-weight: 600; }
  .pal-none { padding: 22px; text-align: center; color: var(--ink-3); font-size: 14px; }

  /* ==========================================================================
     SESSIONS — three columns; projects and sessions are cards you can scan.
     ========================================================================== */
  #sessions.on { display: grid; grid-template-columns: 280px 320px minmax(0,1fr); height: 100%; }
  /* the extra width goes to the transcript, not the two card columns */
  #transcript { max-width: 1080px; }
  .col { overflow-y: auto; height: 100%; min-width: 0; background: var(--bg); }
  .col-head { position: sticky; top: 0; z-index: 2; background: var(--bg); padding: 14px 14px 8px; font-size: 14px; font-weight: 600;
    color: var(--ink-2); }
  .colfind { padding: 0 12px 8px; position: sticky; top: 34px; background: var(--bg); z-index: 2; }
  .colfind input { width: 100%; font: inherit; font-size: 14px; padding: 8px 10px; border: 0; border-radius: 8px;
    background: var(--fill); color: var(--ink); }
  .colfind input:focus { outline: none; box-shadow: 0 0 0 2px var(--accent); }
  .cards { display: flex; flex-direction: column; gap: 8px; padding: 0 12px 16px; }
  .pcard { background: var(--panel); border-radius: var(--radius); padding: 12px 14px; cursor: pointer; transition: background var(--t); min-width: 0; }
  .pcard:hover { background: var(--panel-2); }
  .pcard.on { background: var(--fill); }
  .pcard.on .t { color: var(--ink); }
  .pcard .t { font-weight: 500; font-size: 14px; line-height: 1.35; overflow: hidden; text-overflow: ellipsis; display: -webkit-box;
    -webkit-line-clamp: 2; -webkit-box-orient: vertical; word-break: break-word; }
  .pcard .s { color: var(--ink-3); font-size: 12px; margin-top: 3px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); }
  .pcard .s.sans { font-family: var(--sans); color: var(--ink-2); font-size: 13px; }
  .pcard .m { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 8px; }
  .pcard .mmeta { margin-top: 6px; font-size: 13px; }
  .pcard.on .pill { background: var(--panel); }
  .row { display: none; }

  /* transcript */
  #transcript { padding: 20px 26px 60px; margin: 0 auto; }
  .conv-head { margin-bottom: 14px; }
  .conv-head h2 { font-size: 16px; font-weight: 600; margin: 0 0 3px; letter-spacing: -.01em; }
  .conv-head .sub { color: var(--ink-3); font-size: 13px; font-family: var(--mono); }
  .msg { display: grid; grid-template-columns: 84px minmax(0,1fr); gap: 8px 12px; padding: 12px 12px; border-radius: var(--r-sm);
    margin: 0 -12px; transition: background var(--t); }
  .msg:hover { background: var(--panel); }
  .who { display: flex; flex-direction: column; align-items: flex-start; gap: 4px; font-size: 12px; color: var(--ink-3); }
  .rc { font-size: 12px; font-weight: 600; letter-spacing: 0; padding: 2px 7px; border-radius: 6px;
    background: var(--fill); color: var(--ink-2); }
  .msg.user .rc { background: var(--info-soft); color: var(--user); }
  .msg.assistant .rc { background: var(--ok-soft); color: var(--ok); }
  .msg.system .rc { background: var(--warn-soft); color: var(--warn); }
  .who .ts, .who .tc { font-size: 12px; color: var(--ink-3); font-family: var(--sans); font-variant-numeric: tabular-nums; }
  .mbody { min-width: 0; font-size: 14px; line-height: 1.6; }
  .text { white-space: pre-wrap; word-wrap: break-word; }
  .md p { margin: 0 0 8px; } .md p:last-child { margin: 0; }
  .md ul, .md ol { margin: 4px 0 8px; padding-left: 22px; }
  .md li { margin: 2px 0; }
  .md h1, .md h2, .md h3, .md h4 { font-size: 14px; font-weight: 600; margin: 10px 0 4px; }
  .md code { font-family: var(--mono); font-size: 13px; background: var(--fill); padding: 1px 5px; border-radius: 6px; }
  .md pre { margin: 6px 0 8px; background: var(--panel-2); border-radius: 8px; padding: 10px 12px; overflow-x: auto; }
  .md pre code { background: none; padding: 0; font-size: 13px; line-height: 1.5; white-space: pre; }
  .md blockquote { margin: 4px 0 8px; padding: 6px 12px; background: var(--panel-2); border-radius: 6px; color: var(--ink-2); }
  .thinking { background: var(--panel-2); border-radius: 8px; padding: 8px 12px; color: var(--ink-2); font-style: italic;
    white-space: pre-wrap; margin: 6px 0; font-size: 14px; }
  .ctxtog { font: inherit; font-size: 12px; color: var(--ink-3); background: var(--fill); border: 0; border-radius: 6px; padding: 2px 8px;
    cursor: pointer; margin: 4px 0; }
  .ctxtog:hover { color: var(--ink); background: var(--fill-2); }
  .ctxbody { display: none; margin: 6px 0 4px; }
  .ctxbody.on { display: block; }
  .ctxbody pre { margin: 0; font-family: var(--mono); font-size: 13px; white-space: pre-wrap; word-break: break-word; color: var(--ink-3);
    background: var(--panel-2); border-radius: 8px; padding: 10px 12px; max-height: 300px; overflow: auto; }
  .tcall { border-radius: 8px; margin: 6px 0; overflow: hidden; background: var(--panel-2); }
  .tcall .th { display: flex; align-items: center; gap: 8px; padding: 7px 11px; font-family: var(--mono); font-size: 13px; cursor: pointer;
    user-select: none; color: var(--ink-2); transition: background var(--t); }
  .tcall .th:hover { background: var(--fill); }
  .tcall .th .tn { color: var(--ink); font-weight: 600; }
  .tcall .th .ta { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .tcall .th .sz { margin-left: auto; color: var(--ink-3); font-size: 12px; white-space: nowrap; }
  .tcall .th .tg { font-size: 9px; color: var(--ink-3); transition: transform var(--t); }
  .tcall.open .th .tg { transform: rotate(90deg); }
  .tcall.err .th .tn { color: var(--bad); }
  .tcall .tb2 { display: none; }
  .tcall.open .tb2 { display: block; }
  .tcall .tl2 { font-family: var(--mono); font-size: 12px; letter-spacing: 0; color: var(--ink-3); padding: 7px 11px 0; }
  .tcall pre { margin: 0; padding: 6px 11px 10px; overflow-x: auto; font-family: var(--mono); font-size: 13px; white-space: pre-wrap;
    word-break: break-word; max-height: 340px; color: var(--ink-2); }
  .tcall.err pre.res { color: var(--bad); }
  .block { border-radius: 8px; margin: 6px 0; background: var(--panel-2); overflow: hidden; }
  .block .bh { padding: 6px 11px; font-family: var(--mono); font-size: 13px; display: flex; gap: 8px; align-items: center; }
  .block pre { margin: 0; padding: 8px 11px; overflow-x: auto; font-family: var(--mono); font-size: 13px; white-space: pre-wrap;
    word-break: break-word; max-height: 340px; }
  .badge { font-size: 12px; padding: 1px 6px; border-radius: 6px; background: var(--fill); color: var(--ink-3); font-family: var(--sans); font-variant-numeric: tabular-nums; }
  .compact { border-radius: 8px; background: var(--warn-soft); color: var(--warn); padding: 8px 12px; font-size: 13px; font-family: var(--sans); font-variant-numeric: tabular-nums; }
  .conv-stats { margin: 0 0 12px; }
  .ctxbox { padding: 12px 14px 8px; margin-bottom: 16px; }
  .ctxbox .ch { display: flex; align-items: baseline; gap: 10px; font-size: 14px; color: var(--ink-2); font-weight: 600; }
  .ctxbox .ch span:last-child { margin-left: auto; font-weight: 400; font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); }
  .ctxsvg { width: 100%; height: auto; display: block; margin-top: 6px; }
  .ctxsvg .bar { fill: var(--accent); opacity: .55; transition: opacity var(--t); }
  .ctxsvg .bar:hover, .ctxsvg .bar.on { opacity: 1; }
  .ctxsvg .cap { stroke: var(--bad); stroke-width: 1; stroke-dasharray: 3 3; }
  .ctxsvg .cost { fill: none; stroke: var(--info); stroke-width: 1.5; vector-effect: non-scaling-stroke; }
  .ctxsvg text { fill: var(--ink-3); font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 9px; }

  /* ==========================================================================
     OVERVIEW — readouts, the trace, the punchcard, the spectrum, the ledgers.
     ========================================================================== */
  /* ---- the four readings, across the top -----------------------------------
     A tile is one number you would otherwise have to go and compute: the
     seven days you just had, what moved since the seven before it, and the
     shape of the fortnight under it. The chart is not decoration — the number
     is a level and the chart is what got it there, so they are always drawn
     from the same series. It bleeds to both card edges because a sparkline
     with side padding reads as a small chart in a big box; flush, it reads as
     the floor of the tile. */
  .tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 2px 0 16px; }
  .tile { position: relative; overflow: hidden; background: var(--panel); border-radius: var(--radius);
    padding: 13px 15px 0; display: flex; flex-direction: column; min-width: 0; min-height: 124px; }
  .tile-h { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--ink-2); min-width: 0; }
  .tile-h .ic { width: 15px; height: 15px; } .tile-h .ic svg { width: 15px; height: 15px; }
  .tile-h .nm { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tile-v { font-size: 30px; font-weight: 600; letter-spacing: -.035em; font-variant-numeric: tabular-nums;
    line-height: 1.15; margin-top: 7px; }
  .tile-v small { font-size: 16px; font-weight: 600; color: var(--ink-3); letter-spacing: -.02em; margin-left: 3px; }
  .tile-s { font-size: 13px; color: var(--ink-3); margin-top: 2px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* a delta is a direction first and a number second: the arrow is the read,
     the percentage is the detail. Neutral when the move is under a point —
     tinting noise green is how a dashboard starts lying. */
  .dlt { display: inline-flex; align-items: center; gap: 3px; font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; font-weight: 600;
    color: var(--ink-3); font-variant-numeric: tabular-nums; flex: none; }
  /* a delta is information, not an alarm: a quiet week is not an outage */
  .dlt.up, .dlt.dn { color: var(--ink-3); }
  .tile-c { margin: auto -15px 0; height: 38px; display: block; }
  .tile-c svg { display: block; width: 100%; height: 38px; }
  .tile-c .ln { fill: none; stroke: var(--accent); stroke-width: 2; vector-effect: non-scaling-stroke; stroke-linejoin: round; stroke-linecap: round;
    vector-effect: non-scaling-stroke; }
  .tile-c .ar { opacity: 1; }
  .tile-c .br { fill: var(--accent); opacity: .55; }
  .tile-c .br.hi { opacity: 1; }
  .tile-c .fl { fill: var(--fill-2); }
  /* the streak tile draws the fortnight on the card motif's own grid — a day
     you worked is a block, a day you didn't is the ghost of one */
  .tile-c .blk { fill: var(--accent); } .tile-c .blk.off { fill: var(--fill-2); }
  .tiles.tiles-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  /* a tile with no series is a figure and its context, so it does not have to
     reserve a chart's worth of height */
  .tile.flat { min-height: 0; padding-bottom: 13px; }
  /* Four tiles fold to two; three stay three, because 2 + 1 leaves a hole
     where the fourth tile isn't. Both end as one column when a column is all
     there is room for. */
  @media (max-width: 1240px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (max-width: 760px) { .tiles, .tiles.tiles-3 { grid-template-columns: minmax(0, 1fr); } }
  @media (max-width: 620px) { .tiles { grid-template-columns: minmax(0, 1fr); } }

  /* ---- the two columns: instruments on the left, standings on the right ----
     Everything on the left is a series over time (the trace, spend, the
     projects ledger). Everything on the right is a state right now (which
     families are ready, when you work, what the agent reaches for). Reading
     down one column never changes the question you are asking. */
  .ov { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr); gap: 12px; align-items: start; }
  .ov-c { display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  @media (max-width: 1180px) { .ov { grid-template-columns: minmax(0, 1fr); } }
  /* a card that owns its own header, so a section label is not spent on it */
  .c-h { display: flex; align-items: center; gap: 9px; margin-bottom: 10px; min-width: 0; }
  .c-h .ic { width: 16px; height: 16px; } .c-h .ic svg { width: 16px; height: 16px; }
  .c-h h3 { font-size: 14px; font-weight: 600; color: var(--ink); margin: 0; white-space: nowrap; }
  .c-h .cn { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
  .c-h .sp { flex: 1; }
  .c-h .fchips { flex: none; }
  /* ---- the five families, as a standings list rather than five squat cards -- */
  .provs { display: flex; flex-direction: column; gap: 1px; margin: 0 -6px -4px; }
  .fam { display: grid; grid-template-columns: 26px minmax(0, 1fr) auto; grid-template-rows: auto auto; gap: 1px 10px;
    align-items: center; padding: 8px 10px; border-radius: 8px; cursor: pointer; min-width: 0; position: relative;
    background: transparent; transition: background var(--t); }
  .fam:hover { background: var(--panel-2); }
  .fam.cur { background: var(--panel-2); }
  .fam.cur:hover { background: var(--accent-soft-2); }
  .fam .mark2 { grid-column: 1; grid-row: 1 / 3; width: 26px; height: 26px; }
  .fam .mark2 svg { width: 16px; height: 16px; }
  .fam .fn { grid-column: 2; grid-row: 1; font-weight: 600; font-size: 14px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .fam.cur .fn { color: var(--accent); }
  .fam .fm { grid-column: 2; grid-row: 2; font-family: var(--mono); font-size: 12px; color: var(--ink-3);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .fam .fm.none { font-family: var(--sans); font-style: italic; }
  /* the right edge of the row is one thing at a time: the state, until you
     hover, when it becomes the action that changes the state */
  .fam .fr { grid-column: 3; grid-row: 1 / 3; display: flex; align-items: center; justify-content: flex-end;
    gap: 7px; flex: none; }
  .fam .ff { display: none; align-items: center; gap: 6px; }
  .fam:hover .ff, .fam:focus-within .ff { display: flex; }
  .fam:hover .fst, .fam:focus-within .fst { display: none; }
  .fam .ff .b { padding: 3px 9px; font-size: 12px; }
  .fam .fst { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-3); white-space: nowrap; }
  .fam .fst .t2 { font-size: 12px; padding: 2px 5px; }
  .fam .pr { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); white-space: nowrap; max-width: 96px;
    overflow: hidden; text-overflow: ellipsis; }
  .fam .pr.ok { color: var(--ok); } .fam .pr.bad { color: var(--bad); }
  /* a run, at a glance: state, what it was, how long ago. The full ledger
     is one click away, so this row says the least that still identifies it. */
  .arow { display: flex; align-items: center; gap: 9px; padding: 7px 10px; border-radius: 8px; cursor: pointer;
    font-size: 14px; min-width: 0; transition: background var(--t); }
  .arow:hover { background: var(--panel-2); }
  .arow .an { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .arow .aa { font-family: var(--mono); font-size: 12px; color: var(--ink-3); flex: none; }
  /* ---- the ledger, on the Activity page: one line per run or job ----------
     Nine columns on one grid so the header and every row line up by
     construction, and the numeric ones are mono and right-aligned: a column
     of durations you cannot compare down the page is a list, not a ledger. */
  .xlist { display: flex; flex-direction: column; gap: 1px; }
  .xrow { display: grid; grid-template-columns: 8px 76px minmax(0, 1fr) 58px 78px 66px 62px 84px 62px;
    gap: 10px; align-items: center; padding: 8px 11px; border-radius: 8px; font-size: 14px; cursor: pointer;
    min-width: 0; transition: background var(--t); }
  .xrow:hover { background: var(--panel); }
  .xrow.head { padding: 2px 11px 6px; font-size: 12px; font-weight: 600; letter-spacing: 0;
    color: var(--ink-3); cursor: default; }
  .xrow.head:hover { background: transparent; }
  .xrow.head span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .xrow.head span:nth-child(n+4) { text-align: right; }
  .xk { font-size: 12px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .xn { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .xv { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 13px; color: var(--ink-2); text-align: right;
    font-variant-numeric: tabular-nums; white-space: nowrap; }
  .xs { text-align: right; font-size: 13px; color: var(--ink-2); } .xs.bad { color: var(--bad); font-weight: 500; }
  .xa { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); text-align: right; white-space: nowrap; }
  .xmore { margin-top: 12px; }
  @media (max-width: 1200px) {
    .xrow { grid-template-columns: 8px 70px minmax(0, 1fr) 78px 84px 62px; }
    .xrow .xag, .xrow .xtok, .xrow .xusd, .xrow.head span:nth-child(4),
    .xrow.head span:nth-child(6), .xrow.head span:nth-child(7) { display: none; }
  }
  /* the one way out of a card, in the card's own bottom padding */
  .c-more { display: block; width: 100%; margin: 6px 0 -4px; padding: 7px 10px; border: 0; border-radius: 8px;
    background: transparent; font: inherit; font-size: 13px; color: var(--ink-3); text-align: left; cursor: pointer;
    transition: background var(--t), color var(--t); }
  .c-more:hover { background: var(--panel-2); color: var(--accent); }
  .lcd { display: flex; flex-wrap: wrap; align-items: baseline; gap: 0 22px; font-size: 13px; color: var(--ink-3); margin: 0 0 18px; }
  .lcd i { font-style: normal; color: var(--ink); font-weight: 600; font-size: 16px; letter-spacing: -.02em; font-variant-numeric: tabular-nums;
    margin-right: 5px; font-family: var(--sans); font-variant-numeric: tabular-nums; }
  .lcd .hot i { color: var(--accent); }
  .lcd.tight { margin: 4px 0 10px; gap: 0 18px; }
  .lcd .dim i { color: var(--ink-2); }
  .trace { position: relative; padding: 14px 16px 8px; margin-bottom: 12px; }
  .trace-h { display: flex; align-items: baseline; gap: 12px; margin-bottom: 4px; }
  .trace-t { font-size: 14px; font-weight: 600; color: var(--ink-2); }
  .trace-pk { margin-left: auto; font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 13px; color: var(--ink-2); }
  .trace-pk b { color: var(--ink); }
  .trace svg { display: block; width: 100%; height: auto; }
  .trace { position: relative; }
  .trace .env { opacity: 1; }
  .trace .sig { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
  /* the hover: a hairline crosshair and a small card with the day's number */
  .trace .xh { stroke: var(--ink-3); stroke-width: 1; stroke-dasharray: 3 3; vector-effect: non-scaling-stroke; }
  .trace .tip, .bars-wrap .tip, .us-chart .tip { position: absolute; top: 40px; pointer-events: none; padding: 6px 10px; border-radius: 8px;
    background: var(--ink); color: var(--bg); font-size: 13px; white-space: nowrap; box-shadow: 0 6px 20px -8px var(--dim); }
  .trace .tip b { font-weight: 600; margin-right: 4px; }
  .trace .tip span { margin-left: 8px; opacity: .65; }
  .trace .pk { stroke: var(--ink-3); stroke-width: 1; stroke-dasharray: 2 3; }
  .trace .pkd { fill: var(--accent); }
  .trace .base { stroke: var(--fill-2); stroke-width: 1; vector-effect: non-scaling-stroke; }
  /* in viewBox units: the chart is 1000 wide and renders ~650px, so 17 ≈ 11px */
  .trace text { fill: var(--ink-3); font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 17px; }
  @media (prefers-reduced-motion: no-preference) {
    .trace .sig { animation: draw 1.15s cubic-bezier(.22,.7,.2,1) forwards; }
    .trace .env { animation: fadein .8s .35s both ease-out; }
    @keyframes draw { to { stroke-dashoffset: 0; } }
    @keyframes fadein { from { opacity: 0; } }
  }
  .duo { display: grid; grid-template-columns: 1.15fr 1fr; gap: 12px; }
  @media (max-width: 980px) { .duo { grid-template-columns: 1fr; } }
  .punch { width: 100%; height: auto; display: block; }
  .punch .cell { fill: var(--accent); }
  .punch text { fill: var(--ink-3); font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 8.5px; }
  .spec { display: flex; height: 10px; border-radius: 6px; overflow: hidden; gap: 2px; margin-bottom: 14px; }
  .spec i { display: block; background: var(--accent); }
  .tl { display: grid; grid-template-columns: 1fr auto auto; gap: 3px 12px; align-items: baseline; font-size: 13px; }
  .tl .tn { color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--sans); font-variant-numeric: tabular-nums; }
  .tl .tc { color: var(--ink-3); font-variant-numeric: tabular-nums; }
  .tl .tp { color: var(--ink-2); font-variant-numeric: tabular-nums; text-align: right; min-width: 34px; }
  .tl .sw { width: 8px; height: 8px; border-radius: 2px; background: var(--accent); display: inline-block; margin-right: 8px; }
  .plist { display: flex; flex-direction: column; gap: 4px; margin-top: 2px; }
  .prow { position: relative; display: flex; align-items: center; gap: 10px; padding: 9px 12px; border-radius: 8px; font-size: 14px;
    overflow: hidden; background: var(--panel-2); }
  .prow .fillbar { position: absolute; left: 0; top: 0; bottom: 0; background: var(--accent); opacity: .1; }
  .prow .pn { position: relative; font-weight: 600; }
  .prow .pp { position: relative; color: var(--ink-3); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono); }
  .prow .pv { position: relative; margin-left: auto; color: var(--ink-2); white-space: nowrap; font-variant-numeric: tabular-nums;
    font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 13px; }
  .prow .pf { position: relative; } .prow .pf .mark2 { width: 18px; height: 18px; border-radius: 6px; }
  .prow .pf .mark2 svg { width: 11px; height: 11px; }
  .prow .t2 { position: relative; }
  .bars { width: 100%; height: auto; display: block; margin: 6px 0 4px; }
  .bars .est { fill: var(--accent); opacity: .32; }
  .bars .rec { fill: var(--accent); }
  .bars text { fill: var(--ink-3); font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 17px; }
  .bars path:hover { opacity: .85; }
  .bars .base { stroke: var(--fill-2); stroke-width: 1; }
  .leg { display: flex; gap: 14px; font-size: 12px; color: var(--ink-3); margin-bottom: 8px; flex-wrap: wrap; }
  .leg i { display: inline-block; width: 10px; height: 10px; border-radius: 2px; vertical-align: -1px; margin-right: 5px; background: var(--accent); }
  .leg i.est { background: var(--accent); opacity: .32; }
  .run-ph { margin: 14px 0 6px; font-size: 14px; font-weight: 600; color: var(--ink-2); display: flex; gap: 10px; align-items: center; }
  .run-ag { background: var(--panel-2); border-radius: 8px; padding: 10px 12px; margin: 6px 0; font-size: 14px; }
  .run-ag .rh { display: flex; align-items: center; gap: 8px; font-size: 13px; }
  .run-ag .rh b { font-weight: 600; }
  .run-ag .rh .sp { flex: 1; }
  .run-ag .rs { color: var(--ink-2); margin-top: 5px; white-space: pre-wrap; word-break: break-word; max-height: 160px; overflow: auto; font-size: 13px; }
  .run-ag .re { color: var(--bad); font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 13px; margin-top: 5px; }
  .log { background: var(--panel-2); border-radius: 8px; padding: 10px 12px; font-family: var(--mono); font-size: 12px; line-height: 1.55;
    max-height: 220px; overflow: auto; white-space: pre-wrap; word-break: break-word; color: var(--ink-2); }

  /* ==========================================================================
     MODELS — a comparison table, local models, provider setup, self-host.
     ========================================================================== */
  .hero { padding: 18px 20px; margin-bottom: 20px; }
  .hero-t { font-size: 24px; font-weight: 600; letter-spacing: -.02em; margin: 0 0 4px; }
  .hero-s { font-size: 14px; color: var(--ink-2); line-height: 1.5; max-width: 62ch; }
  .hero-m { font-family: var(--mono); font-weight: 600; font-size: 15px; }
  .host { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; padding: 14px 16px; margin-bottom: 20px; }
  .host .kbadge { font-size: 12px; font-weight: 600; letter-spacing: 0; padding: 3px 9px; border-radius: 6px; }
  .kbadge.selfhost, .kbadge.local { background: var(--info-soft); color: var(--info); }
  .kbadge.provider { background: var(--ok-soft); color: var(--ok); }
  .kbadge.default { background: var(--fill); color: var(--ink-2); }
  .host .hm { font-family: var(--mono); font-weight: 600; font-size: 15px; }
  .host .hb, .hero .hb { font-family: var(--mono); font-size: 13px; color: var(--ink-3); margin-left: auto; word-break: break-all; }
  .browse-head { display: flex; align-items: baseline; gap: 11px; margin-bottom: 10px; }
  .cnt2 { font-size: 13px; color: var(--ink-2); background: var(--fill); padding: 1px 8px; border-radius: 6px; }
  .browse { overflow: hidden auto; max-height: 340px; margin-bottom: 22px; }
  .brow { display: flex; align-items: center; gap: 12px; padding: 8px 12px; cursor: pointer; border-radius: 8px; margin: 1px 4px; }
  .brow:hover { background: var(--panel-2); }
  .brow.cur { background: var(--fill); }
  .brow .bm { font-family: var(--mono); font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
  .brow .bp { font-size: 13px; color: var(--ink-3); white-space: nowrap; }
  .brow .bx { font-size: 12px; color: var(--ink-3); white-space: nowrap; min-width: 62px; text-align: right; }
  .browse-empty { padding: 14px 15px; font-size: 14px; color: var(--ink-3); font-style: italic; }
  .selfhost-card { gap: 10px; margin-bottom: 20px; }
  .selfhost-card .fields, .selfhost .fields { display: flex; flex-direction: column; gap: 8px; }
  .selfhost-card .fields .r, .selfhost .fields .r { display: flex; gap: 8px; }
  .selfhost { border-radius: var(--radius); padding: 15px 17px; margin-bottom: 20px; background: var(--accent-soft); }
  .selfhost h3 { margin: 0 0 4px; font-size: 15px; }
  .keyline { display: flex; align-items: center; gap: 8px; font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 13px; }
  .keyline .env { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .keyline .val { font-weight: 600; }
  .src { font-size: 9px; letter-spacing: 0; font-weight: 600; padding: 2px 6px; border-radius: 6px; }
  .src.env { background: var(--info-soft); color: var(--info); }
  .src.saved { background: var(--ok-soft); color: var(--ok); }
  .setup { padding: 13px 15px 14px; margin-bottom: 10px; }
  .setup-h { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; }
  .setup-n { font-weight: 600; font-size: 14px; }
  .setup-s { font-size: 14px; color: var(--ink-2); }
  .setup-note { font-size: 13px; color: var(--ink-3); line-height: 1.5; margin-top: 8px; }
  /* a card that changed group fades in where it landed — 140ms, the same
     step as every other transition on the page, and no motion for anyone
     who has asked for none */
  @media (prefers-reduced-motion: no-preference) {
    .ac-moved { animation: ac-land 140ms ease-out; }
  }
  @keyframes ac-land { from { opacity: 0; } to { opacity: 1; } }
  .ready { display: inline-flex; align-items: center; gap: 7px; font-size: 13px; color: var(--ink-3); white-space: nowrap; }
  .keyheld { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 12px; padding: 9px 12px; border-radius: 8px; background: var(--panel-2); }
  .kh-l { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; letter-spacing: 0; color: var(--ink-3); }
  .kh-v { font-family: var(--mono); font-size: 14px; font-weight: 600; letter-spacing: .02em; }
  .kh-n { font-size: 12px; color: var(--ink-3); }
  /* WHAT YOU ARE RUNNING — the hero of this page.
     The question this page exists to answer is "what am I running, and what
     else could I run". The answer to the first half was a word in the third
     of three stat tiles; it is now the first thing on the page, at a size you
     read without meaning to, with everything you would otherwise have gone
     looking for beside it: the route in, the window, the price, what it can
     do, and whether anyone has actually checked that it answers. */
  .nowcard { position: relative; background: var(--panel); border-radius: var(--radius);
    padding: 16px 18px 14px; margin: 2px 0 16px; display: flex; flex-direction: column; gap: 12px; min-width: 0; }
  .now-top { display: flex; align-items: center; gap: 13px; min-width: 0; }
  .now-top .bigmark { width: 40px; height: 40px; border-radius: 12px; flex: none; display: inline-flex;
    align-items: center; justify-content: center; background: var(--fill); overflow: hidden; }
  .now-top .bigmark svg { width: 40px; height: 40px; display: block; }
  .now-t { min-width: 0; flex: 1; }
  .now-id { font-family: var(--mono); font-size: 24px; font-weight: 600; letter-spacing: -.02em;
    line-height: 1.2; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .now-via { font-size: 14px; color: var(--ink-3); margin-top: 2px; overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .now-a { flex: none; display: flex; align-items: center; gap: 8px; }
  .now-facts { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  /* Reachability is a claim, so it is only made when something checked. Until
     then it says so and offers the check — a page that shows a green dot for
     "a key is saved" is telling you something it does not know. */
  .now-reach { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--ink-3);
    flex-wrap: wrap; }
  .now-reach b { font-weight: 600; color: var(--ink-2); }
  .now-reach.ok b { color: var(--ok); }
  .now-reach.bad b { color: var(--bad); }
  .now-none { font-size: 14px; color: var(--ink-2); }

  /* SETUP, ONE CLICK AWAY — the provider grid is the same subject at a lower
     altitude, and 13 rows of "Not connected" above the models made the page
     lead with what you have not done. It folds to a strip that names what IS
     connected and opens on demand — and it starts open when nothing is
     connected, because then setup IS the job. */
  .provsum { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; width: 100%; margin: 0;
    padding: 11px 14px; border: 0; border-radius: var(--radius); background: var(--panel); color: var(--ink-2);
    font: inherit; font-size: 14px; text-align: left; cursor: pointer; transition: background var(--t); }
  .provsum:hover { background: var(--panel-2); }
  .provsum b { font-weight: 600; color: var(--ink); }
  .provsum-m { display: flex; align-items: center; gap: 5px; flex: none; }
  .provsum-m .mark2 { width: 20px; height: 20px; border-radius: 6px; background: none; }
  .provsum-m .mark2 svg { width: 16px; height: 16px; }
  .provsum-x { margin-left: auto; flex: none; font-size: 14px; color: var(--ink-3); }
  .provsum:hover .provsum-x { color: var(--accent); }
  .provbox { display: none; margin-top: 10px; }
  .provbox.on { display: block; }

  /* MY MODELS — the same card the Deploy picker draws, filled with what the
     SDK knows about a model you can already reach. One component, two pages:
     .mcard, .mh, .omark, .mt, .mo, .mp2 and .pill are shared outright, so the
     two model surfaces cannot drift into two design eras. */
  /* A MENU — one small floating list, used by the controls that would
     otherwise be a row of pills each. It is the third and last surface on
     this page that genuinely floats over the content, so it is the third that
     earns a raise; everything flat still gets its depth from a background
     step. */
  .pmenu-w { position: relative; flex: none; }
  .pmenu-b { display: inline-flex; align-items: center; gap: 6px; height: 32px; padding: 0 11px; border: 0;
    border-radius: 8px; background: var(--fill); font: inherit; font-size: 14px; color: var(--ink-2);
    cursor: pointer; white-space: nowrap; transition: background var(--t), color var(--t); }
  .pmenu-b:hover { background: var(--fill-2); color: var(--ink); }
  .pmenu-b i { font-style: normal; font-size: 8px; color: var(--ink-3); }
  .pmenu-b b { font-weight: 600; color: var(--ink); }
  .pmenu { position: absolute; z-index: 40; top: calc(100% + 6px); right: 0; min-width: 190px; padding: 5px;
    border-radius: 8px; background: var(--panel-2); box-shadow: 0 12px 32px -8px var(--dim);
    display: flex; flex-direction: column; gap: 1px; }
  .pmenu.left { right: auto; left: 0; }
  .pmenu-i { display: flex; align-items: center; gap: 9px; width: 100%; padding: 7px 9px; border: 0;
    border-radius: 8px; background: transparent; font: inherit; font-size: 14px; color: var(--ink-2);
    text-align: left; cursor: pointer; white-space: nowrap; transition: background var(--t), color var(--t); }
  .pmenu-i:hover { background: var(--fill); color: var(--ink); }
  .pmenu-i.on { color: var(--accent); }
  .pmenu-i .pmenu-t { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .pmenu-i .pmenu-s { font-size: 12px; color: var(--ink-3); flex: none; }
  .pmenu-i .omark { width: 18px; height: 18px; border-radius: 6px; background: none; flex: none; }
  .pmenu-i .omark svg { width: 15px; height: 15px; }
  .pmenu-h { padding: 6px 9px 3px; font-size: 12px; font-weight: 600; letter-spacing: 0; color: var(--ink-3); }

  .mm-flat { display: none; }
  .mm-flat.on { display: block; }
  /* a group the SORT has put away, as opposed to one a filter has emptied */
  .mm-off { display: none !important; }

  /* a labelled grid per source */
  .mm-glabel { display: flex; align-items: center; gap: 9px; padding: 12px 2px 8px; font-size: 14px;
    font-weight: 600; color: var(--ink-2); }
  .mm-glabel .mark2 { width: 18px; height: 18px; border-radius: 6px; }
  .mm-glabel .mark2 svg { width: 11px; height: 11px; }
  /* the stand-in for a family that is not a vendor: the page's own pixel
     grid, in neutral ink, so it cannot be mistaken for somebody's logo */
  .mm-anymark { color: var(--ink-3); }
  .now-top .omark { width: 40px; height: 40px; border-radius: 12px; }
  .now-top .omark svg { width: 40px; height: 40px; }
  .mm-hostmark { color: var(--accent); }
  .mm-hostmark svg { fill: currentColor; }
  .omark.mm-hostmark svg, .bigmark.mm-hostmark svg { width: 44%; height: 44%; }
  .mm-anymark svg { fill: currentColor; }
  .mm-gn { font-weight: 400; color: var(--ink-3); font-size: 13px; }
  /* The head gives its 70px back: nothing sits top-right unless the card is
     the current one, and reserving that space would truncate a long model id
     for no reason. */
  .mmcard.on .mh { padding-right: 84px; }
  /* THE CURRENT MODEL'S CARD — the badge, and nothing else.
     .mcard.on carries an accent wash on the Deploy picker, where it means
     "the one you just clicked". Here it would mean "the one you are running",
     and a tinted card is exactly the decoration this page kept being told to
     stop adding: the slanted Current tag already says it in a word. So the
     card keeps the neutral surface its neighbours have. */
  .mcard.mmcard.on { background: var(--panel); }
  .mcard.mmcard.on:hover { background: var(--panel-2); }
  .mcard.mmcard.on .pill, .mcard.mmcard.on .cap { background: var(--fill); }
  /* keyboard focus is a background step, like every other state on the page */
  .mmcard.kb { background: var(--fill); }
  .mmcard.locked .mt { color: var(--ink-2); }
  /* The foot: the compare pin, and the one action. */
  /* the tag is clipped by the card it sits on */
  .mmcard, .nowcard { overflow: hidden; }
  /* the head keeps clear of it; so does the hero's whole top row */
  .nowcard.hastag .now-top { padding-right: 104px; }
  .cap { font-size: 12px; letter-spacing: 0; font-weight: 600; padding: 2px 6px; border-radius: 6px;
    background: var(--fill); color: var(--ink-3); }
  .cap.ok { background: var(--ok-soft); color: var(--ok); }
  .cap.bad { background: var(--bad-soft); color: var(--bad); }
  .cap.amb { background: var(--warn-soft); color: var(--warn); }
  .mcard.on .cap { background: var(--panel); }
  .orow { display: grid; grid-template-columns: minmax(0,1fr) 90px 72px 92px 62px; gap: 12px; align-items: center; padding: 8px 11px;
    border-radius: 8px; font-size: 14px; cursor: pointer; transition: background var(--t); }
  .orow:hover { background: var(--panel-2); }
  .orow.cur { background: var(--fill); }
  .orow .on2 { font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .orow .osz, .orow .opq { color: var(--ink-2); font-size: 13px; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; font-family: var(--sans); font-variant-numeric: tabular-nums; }
  .orow .ost { font-size: 12px; }
  .orow .ogo { font-size: 12px; color: var(--ink-3); text-align: right; }
  .orow:hover .ogo { color: var(--accent); }

  /* ==========================================================================
     CONFIG
     ========================================================================== */
  /* The key column is sized by the longest key that is actually here, not by a
     guess: the grid lives on the table so every row shares one track, and the
     rows hand their cells straight to it. A short key like `env` no longer
     leaves 200px of nothing before its value. */
  .cfg { margin-bottom: 20px; padding: 4px; display: grid;
    grid-template-columns: minmax(0, max-content) minmax(0, 1fr); }
  .cfg .kv { display: contents; }
  .cfg .kv > * { padding: 8px 12px; min-width: 0; }
  .cfg .kv .ck { padding-right: 24px; }
  .cfg .kv:nth-child(odd) > * { background: var(--panel-2); }
  .cfg .kv:nth-child(odd) > :first-child { border-radius: 6px 0 0 6px; }
  .cfg .kv:nth-child(odd) > :last-child { border-radius: 0 6px 6px 0; }
  .cfg .kv .ck { font-family: var(--mono); font-size: 13px; color: var(--ink-2); }
  .cfg .kv .cv { font-family: var(--mono); font-size: 13px; white-space: pre-wrap; word-break: break-word; }
  details.layer { margin-bottom: 8px; }
  details.layer summary { padding: 11px 14px; cursor: pointer; display: flex; align-items: center; gap: 9px;
    list-style: none; }
  details.layer summary::-webkit-details-marker { display: none; }
  details.layer summary::before { content: "▸"; color: var(--ink-3); font-size: 12px; }
  details.layer[open] summary::before { content: "▾"; }
  details.layer summary:hover { color: var(--ink); }
  .lay-n { font-weight: 600; }
  .lay-c { color: var(--ink-3); font-size: 14px; }
  details.layer pre { margin: 0; padding: 0 14px 12px; font-family: var(--mono); font-size: 13px; white-space: pre-wrap; word-break: break-word; }
  .layerpath { font-family: var(--mono); font-size: 12px; color: var(--ink-3); padding: 0 14px 8px; }
  .note-sec { font-size: 13px; color: var(--ink-3); margin: -14px 0 18px; }
  .now { display: flex; gap: 10px; align-items: center; margin-bottom: 18px; padding: 12px 14px; border-radius: var(--radius); background: var(--panel); }
  .now .k { font-size: 12px; color: var(--ink-3); letter-spacing: 0; }
  .now .v { font-family: var(--mono); font-size: 15px; font-weight: 600; }

  /* ==========================================================================
     DEPLOY — provider cards, model cards, GPU cards, the job sheet, the
     deployments rows. Verdict pills are the one new word: fits / tight / no.
     ========================================================================== */
  .dp-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 12px; margin-bottom: 12px; }
  .dpc { padding: 16px 16px 14px; display: flex; flex-direction: column; gap: 10px; min-width: 0; position: relative; transition: background var(--t); }
  .dpc:hover { background: var(--panel-2); }
  /* A connected GPU provider is marked the way every other connected thing
     on this dashboard is: by what its own status line says, not by a wash. */
  .dpc { position: relative; }
  .dpc.on { background: var(--panel); }
  .dpc.on:hover { background: var(--panel-2); }
  .dpc.blocked .fs.warn b { color: var(--warn); font-weight: 600; }
  .dpc.blocked .bigmark { opacity: .75; }
  .dpc .fh { display: flex; align-items: center; gap: 12px; }
  .dpc .bigmark { width: 40px; height: 40px; border-radius: 12px; flex: none; display: inline-flex; align-items: center;
    justify-content: center; background: var(--fill); color: var(--ink); overflow: hidden; }
  .dpc .bigmark svg { width: 40px; height: 40px; display: block; }
  .dpc .ft { min-width: 0; flex: 1; }
  .dpc .fn { font-weight: 600; font-size: 15px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dpc.on .fn { color: var(--accent); }
  .dpc .fd { font-size: 13px; color: var(--ink-3); line-height: 1.35; margin-top: 1px; }
  /* A state reads as one sentence, and it breaks between its clauses, never
     inside them: the name of the state holds its line, and the clause that
     qualifies it drops to the next line whole rather than being cut mid-word. */
  .dpc .fs { display: flex; align-items: center; flex-wrap: wrap; gap: 4px 8px; font-size: 14px;
    color: var(--ink-2); min-width: 0; }
  .dpc .fs .dot2 { width: 8px; height: 8px; }
  .dpc .fs b { font-weight: 600; color: var(--ink); white-space: nowrap; }
  .dpc .fs .fsx { flex: 1 1 auto; min-width: 0; color: var(--ink-3); overflow: hidden;
    text-overflow: ellipsis; white-space: nowrap; }
  .dpc .fs.warn .fsx { color: var(--warn); }
  .dpc .chips { gap: 5px; }
  .dpc .chip { font-size: 12px; padding: 2px 7px; }
  .dpc.on .chip, .dpc.on .pill { background: var(--panel); }
  .dpc .ff { display: flex; align-items: center; gap: 6px; margin-top: auto; flex-wrap: wrap; }
  .dpc .ff .b { padding: 6px 11px; font-size: 13px; }
  .dpc .ff .b.gho { background: var(--fill); }
  .dpc.on .ff .b.gho { background: var(--panel); }
  .dpc .ff .lnk { margin-left: auto; font-size: 13px; color: var(--ink-3); }
  .dpc .ff .lnk:hover { color: var(--accent); }
  /* the credential sheet: fields first, guide collapsed, actions pinned */
  .cs-h { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .cn-h { display: flex; align-items: center; gap: 13px; margin: 2px 0 18px; padding-right: 34px; }
  .cn-h .bigmark { width: 40px; height: 40px; border-radius: 12px; flex: none; display: inline-flex;
    align-items: center; justify-content: center; background: var(--fill); overflow: hidden; }
  .cn-h .bigmark svg { width: 40px; height: 40px; display: block; }
  .cn-h .fn { font-size: 16px; font-weight: 600; letter-spacing: -.01em; }
  .cn-h .fd { font-size: 13px; color: var(--ink-3); margin-top: 2px; }
  .cn-list { display: flex; flex-direction: column; gap: 6px; }
  .cn-row { background: var(--panel-2); border-radius: 10px; transition: background var(--t); }
  .cn-row.on { background: var(--bg); }
  .cn-top { all: unset; box-sizing: border-box; width: 100%; display: flex; align-items: center; gap: 12px;
    padding: 11px 12px; cursor: pointer; border-radius: 10px; }
  .cn-top:hover { background: var(--fill); }
  .cn-row.on .cn-top:hover { background: none; }
  .cn-top:focus-visible { outline: none; box-shadow: inset 0 0 0 2px var(--accent); }
  .cn-top .omark { width: 32px; height: 32px; }
  .cn-top .omark svg { width: 32px; height: 32px; }
  .cn-tt { display: flex; flex-direction: column; min-width: 0; flex: 1; }
  .cn-l { font-size: 14px; font-weight: 600; color: var(--ink); }
  .cn-d { font-size: 13px; color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .cn-tag { flex: none; font-size: 12px; font-weight: 500; padding: 2px 8px; border-radius: 999px;
    background: var(--fill); color: var(--ink-2); }
  .cn-tag.ok { background: var(--accent); color: #fff; }
  .cn-tag.found { color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, transparent); }
  .cn-body { padding: 0 12px 14px 56px; }
  .cn-more { all: unset; cursor: pointer; margin-top: 12px; font-size: 13px; color: var(--ink-2); padding: 4px 2px; border-radius: 6px; }
  .cn-more:hover { color: var(--ink); }
  .cn-more:focus-visible { outline: none; box-shadow: 0 0 0 2px var(--accent); }
  @media (max-width: 560px) { .cn-body { padding-left: 12px; } .cn-d { white-space: normal; } }
  .cs-h .bigmark { width: 40px; height: 40px; border-radius: 12px; }
  .cs-h .bigmark svg { width: 22px; height: 22px; }
  .cs-h .fn { font-weight: 600; font-size: 16px; }
  .cs-h .fd { font-size: 13px; color: var(--ink-3); margin-top: 1px; }
  .cs-fields { display: flex; flex-direction: column; gap: 12px; }
  .cs-fields .dp-field { display: flex; flex-direction: column; gap: 5px; }
  .cs-fields .dp-field input.in { width: 100%; background: var(--panel-2); }
  .cs-fields .kh-n { font-size: 13px; color: var(--ink-3); line-height: 1.45; }
  .cs-guide { margin-top: 16px; background: var(--panel-2); border-radius: 8px; padding: 4px 12px; }
  .cs-guide summary { font-size: 14px; color: var(--ink-2); cursor: pointer; padding: 8px 0; list-style: none; }
  .cs-guide summary::-webkit-details-marker { display: none; }
  .cs-guide summary::before { content: "▸ "; color: var(--ink-3); font-size: 12px; }
  .cs-guide[open] summary::before { content: "▾ "; }
  .cs-guide summary:hover { color: var(--ink); }
  .cs-gb { font-size: 14px; color: var(--ink-2); padding: 2px 0 12px; }
  .cs-gb .gi { color: var(--ink); margin-bottom: 6px; }
  .cs-gb ol { margin: 0 0 10px; padding-left: 18px; }
  .cs-gb li { margin: 3px 0; line-height: 1.45; }
  .cs-gb .gr { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .cs-gb .gr .b { text-decoration: none; }
  .cs-gb .gn { font-size: 13px; color: var(--ink-3); margin-top: 8px; }
  .cs-foot { display: flex; gap: 10px; align-items: center; position: sticky; bottom: -22px; margin: 18px -24px -22px;
    padding: 14px 24px; background: var(--panel); }

  .dp-field { display: flex; flex-direction: column; gap: 4px; font-size: 13px; }
  .dp-field .kh-l { font-size: 12px; }
  .dp-field input.in, .dp-field select.in { width: 100%; background: var(--panel-2); }
  .dpc.on .dp-field input.in { background: var(--panel); }
  .dp-field .kh-n { line-height: 1.45; }
  /* the Hub search — model cards */
  /* Both model grids are things you scan, not lists you read: four to a row,
     so a family or a search fits in a glance instead of a scroll. A fixed
     count rather than auto-fill, because auto-fill sizes to the widest card
     it can fit and lands on three at these page widths — the count steps down
     by breakpoint instead, which is also the only way four neighbours can be
     relied on to line their readings up. */
  .dp-mgrid, .mm-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 12px; align-items: stretch; }
  @media (max-width: 1360px) { .dp-mgrid, .mm-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
  @media (max-width: 860px) { .dp-mgrid, .mm-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (max-width: 600px) { .dp-mgrid, .mm-grid { grid-template-columns: minmax(0, 1fr); } }
  .dp-status { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 12px; color: var(--ink-3); margin-left: auto; white-space: nowrap; }
  /* ==========================================================================
     A MODEL CARD — four to a row, so it is read at a glance or not at all.

     Every card used to be a bag of identical grey pills: 17B, BF16, mit,
     vllm ✓, 162k downloads, all the same weight, so nothing was findable and
     two cards could not be compared without reading every pill on both. A
     card now says its facts in three registers instead of one:

       WHO   the logo, the name (two lines, never cut mid-word), the company
       HOW MUCH  two or three READINGS — a tabular number over a small label,
             on the same grid line in every card, so a column of numbers can
             be compared straight down the grid by eye
       WHAT ELSE  one quiet line of categorical facts, and one action

     The head is a fixed height so the readings of four neighbours line up
     even when one name wraps and another does not, and the foot is pushed to
     the bottom, so the row's cards end together however much is in them.
     ========================================================================== */
  .mcard { padding: 16px; cursor: pointer; display: flex; flex-direction: column; gap: 10px; min-width: 0; position: relative;
    height: 100%; transition: background var(--t); }
  .mcard:hover { background: var(--panel-2); }
  .mcard.on { background: var(--panel-2); }
  .mcard.on:hover { background: var(--accent-soft-2); }
  .mcard .mh { display: flex; align-items: flex-start; gap: 10px; min-width: 0; min-height: 50px; }
  .mcard .mh .mwhen { flex: none; margin-left: auto; font-size: 12px; color: var(--ink-3); white-space: nowrap; padding-top: 2px; }
  .mcard .mh .mwhen.fresh { color: var(--accent); }
  .mwarn { display: flex; flex-wrap: wrap; gap: 4px 12px; font-size: 13px; font-weight: 500; }
  .mwarn .bad { color: var(--bad); } .mwarn .amb { color: var(--warn); }
  .mcard .mh .mtt { min-width: 0; flex: 1; }

  /* the readings: value over label, tabular, one grid line per card */
  /* Each reading is its own small tile inside the card: the three numbers
     read as a set you compare, not as loose text between two lines. */
  .mstats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 6px; align-items: stretch; }
  .mstat { min-width: 0; display: flex; flex-direction: column; gap: 2px; padding: 9px 10px 8px;
    border-radius: 8px; background: var(--bg); }
  .mstat b { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 14px; font-weight: 600; letter-spacing: -.02em;
    color: var(--ink); font-variant-numeric: tabular-nums; white-space: nowrap; overflow: hidden;
    text-overflow: ellipsis; }
  .mstat span { font-size: 12px; color: var(--ink-3); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .mstat.acc b { color: var(--accent); }
  .mstat.amb b { color: var(--warn); }
  .mstat.red b { color: var(--bad); }
  .mstat.mute b { color: var(--ink-3); font-weight: 500; }
  .mstat.span2 { grid-column: span 2; }

  /* the quiet line: categorical facts, separated by middots, never boxed */
  .mmeta { display: flex; align-items: baseline; flex-wrap: wrap; min-width: 0; font-size: 12px;
    color: var(--ink-3); line-height: 1.5; }
  .mmeta > * { white-space: nowrap; }
  .mmeta > *:not(:last-child)::after { content: "\00b7"; margin: 0 5px; opacity: .55; }
  .mmeta .mono { font-family: var(--mono); font-size: 12px; background: none; padding: 0; color: var(--ink-3); }
  .mmeta .ok { color: var(--ok); } .mmeta .bad { color: var(--bad); } .mmeta .amb { color: var(--warn); }
  .mmeta .fresh { color: var(--accent); }

  /* The foot: pushed down, so a row of cards ends level — and set off from
     the facts above it by the sheet's own material. This page draws no lines,
     so the division is a run of single pixels, the rail's branch motif at a
     sparser 4px pitch so it reads as texture rather than as a rule. On the
     card under the cursor the run takes the accent, the way the rail's stub
     does under the row you are on. */
  .mfoot { display: flex; align-items: center; gap: 8px; min-height: 28px; margin-top: auto; padding-top: 11px;
    background-image: repeating-linear-gradient(to right, var(--fill-2) 0 1px, transparent 1px 4px);
    background-size: 100% 1px; background-repeat: no-repeat; background-position: 0 0; }
  .mcard.on .mfoot {
    background-image: repeating-linear-gradient(to right, var(--accent) 0 1px, transparent 1px 4px); }
  .omark { width: 34px; height: 34px; border-radius: 8px; flex: none; display: inline-flex; align-items: center; justify-content: center;
    background: var(--fill); color: var(--ink-2); overflow: hidden; position: relative; }
  .omark svg { width: 34px; height: 34px; display: block; }
  .omark.letter { font-family: var(--sans); font-size: 16px; font-weight: 600; }
  .omark img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; opacity: 0; transition: opacity var(--t); }
  .omark.img img { opacity: 1; }
  .omark.img { color: transparent; }
  .mcard.on .omark { background: var(--panel); }
  /* Repo ids are long — Llama-4-Scout-17B-16E-Instruct does not fit one line
     of a quarter-width card — so the name takes two and breaks on its own
     separators rather than being cut with an ellipsis at "Instru…". */
  .mcard .mt { font-family: var(--mono); font-weight: 600; font-size: 14px; line-height: 1.3; letter-spacing: -.01em;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
    overflow-wrap: anywhere; }
  .mcard.on .mt { color: var(--accent); }
  .mcard .mo { font-size: 12px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 1px; }
  .mcard .mp2 { display: flex; flex-wrap: wrap; gap: 5px; }
  .mcard.on .pill { background: var(--panel); }
  /* THE ACTION — a real button, right-aligned in the foot. It sits in the
     foot rather than the head's corner, which is what used to force a 70px
     hole into every title. At rest it is a quiet fill so a grid of forty does
     not shout forty times; the card under the cursor lights its own. */
  .mbtn { margin-left: auto; flex: none; font: inherit; font-size: 13px; font-weight: 600; white-space: nowrap;
    padding: 5px 12px; border: 0; border-radius: 8px; background: var(--fill); color: var(--ink-2);
    cursor: pointer; transition: background var(--t), color var(--t); }
  /* Hovering a card is not choosing it: the button darkens a step and turns
     green only under the pointer itself. A green button on whichever card the
     mouse happened to cross read as "this one is recommended". */
  .mcard:hover .mbtn { background: var(--fill-2); color: var(--ink); }
  .mbtn:hover, .mbtn:focus-visible { background: var(--accent); color: var(--accent-ink); }
  .mbtn.on { background: var(--accent-soft-2); color: var(--accent); }
  .mbtn.amb { background: var(--warn-soft); color: var(--warn); }
  .mcard:hover .mbtn.amb { background: var(--warn-soft); color: var(--warn); }
  .mbtn.amb:hover { background: var(--warn); color: var(--accent-ink); }
  /* A family that isn't connected says so ONCE, in the strip over its grid;
     its cards drop the per-card warning and wear the plain button. Sorted
     flat, the strip is gone and each card speaks for itself again. */
  .mm-grid.famlock .mmeta .amb { display: none; }
  .mm-grid.famlock .mmeta:not(:has(> :not(.amb))) { display: none; }
  .mm-grid.famlock .mbtn.amb, .mm-grid.famlock .mcard:hover .mbtn.amb { background: var(--fill); color: var(--ink-2); }
  .mm-grid.famlock .mcard:hover .mbtn.amb { background: var(--fill-2); color: var(--ink); }
  .mm-grid.famlock .mbtn.amb:hover { background: var(--accent); color: var(--accent-ink); }
  .mm-lock { display: flex; align-items: center; gap: 14px; background: var(--panel); border-radius: 12px;
    padding: 14px 16px; margin: 0 0 12px; min-width: 0; }
  .mm-lock .ml-marks { display: flex; flex: none; }
  .mm-lock .ml-marks .omark { width: 36px; height: 36px; border-radius: 10px; }
  .mm-lock .ml-marks .omark svg { width: 36px; height: 36px; }
  .mm-lock .ml-t { flex: 1; min-width: 0; }
  .mm-lock .ml-h { font-size: 14px; font-weight: 600; color: var(--ink); }
  .mm-lock .ml-d { font-size: 13px; color: var(--ink-3); margin-top: 1px; }
  .mm-lock .ml-d b { font-weight: 600; color: var(--accent); }
  @media (max-width: 640px) { .mm-lock { flex-wrap: wrap; } .mm-lock .b { width: 100%; } }
  /* fit & deploy — one group per provider, GPUs as cards */

  .sheet .banner { margin: 12px 0 0; }
  /* deployments — filled rows */

  .dp-none { display: flex; align-items: center; gap: 8px; padding: 10px 14px; border-radius: var(--r-sm); background: var(--panel);
    font-size: 14px; color: var(--ink-2); }

  /* ---- responsive ---- */
  @media (max-width: 1100px) {
    #sessions.on { grid-template-columns: 240px 280px minmax(0,1fr); }
  }
  @media (max-width: 900px) {
    .tb span { display: none; }
    #top { padding: 0 14px; }
    .crumb .cs, .crumb .sep { display: none; }
    .page { padding: 18px 14px 60px; }
    #sessions.on { grid-template-columns: 1fr; }
    .col { display: none; } .col.mobile-on { display: block; }
    .orow { grid-template-columns: minmax(0,1fr) 72px 62px; }
    .orow .osz, .orow .opq { display: none; }
    #transcript { padding: 14px 12px; }
    .msg { grid-template-columns: 1fr; gap: 4px; margin: 0; }
    .who { flex-direction: row; align-items: center; gap: 8px; }
  }

  /* ==========================================================================
     DEPLOY — the sheet, the Active cards, the logs.
     ========================================================================== */
  /* the deploy sheet: what it is, where it runs, what it costs, one button */
  .ds-head { display: flex; align-items: center; gap: 12px; margin: 2px 0 16px; min-width: 0; }
  .ds-head .omark { width: 40px; height: 40px; border-radius: 12px; }
  .ds-ht { min-width: 0; }
  .ds-name { font-family: var(--mono); font-weight: 600; font-size: 16px; letter-spacing: -.01em;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ds-org { font-size: 13px; color: var(--ink-3); margin-top: 1px; }
  .ds-facts { margin: 0 0 18px; }
  .ds-lbl { font-size: 12px; font-weight: 600; color: var(--ink-3); margin: 0 0 8px; }
  .ds-gpus { display: flex; flex-direction: column; gap: 4px; }
  .ds-gpu { display: flex; align-items: center; gap: 11px; width: 100%; padding: 10px 12px; border: 0;
    border-radius: 8px; background: var(--panel-2); font: inherit; color: var(--ink); text-align: left;
    cursor: pointer; transition: background var(--t); }
  .ds-gpu:hover { background: var(--fill); }
  .ds-gpu.on { background: var(--fill); }
  .ds-radio { width: 15px; height: 15px; border-radius: 50%; flex: none; background: var(--panel);
    box-shadow: inset 0 0 0 2px var(--fill-2); transition: box-shadow var(--t); }
  .ds-gpu.on .ds-radio { box-shadow: inset 0 0 0 5px var(--accent); }
  .ds-gpu .mark2 { width: 20px; height: 20px; flex: none; }
  .ds-gt { flex: 1; min-width: 0; display: flex; flex-direction: column; line-height: 1.3; }
  .ds-gt b { font-size: 14px; font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ds-gt span { font-size: 13px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ds-best { flex: none; font-size: 12px; font-weight: 600; color: var(--accent); background: var(--accent-soft);
    padding: 2px 8px; border-radius: 999px; }
  .ds-gpu.on .ds-best { background: var(--panel); }
  .ds-tight { flex: none; font-size: 12px; color: var(--warn); }
  .ds-price { flex: none; min-width: 72px; text-align: right; font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 14px;
    font-weight: 600; font-variant-numeric: tabular-nums; }
  .ds-more { align-self: flex-start; border: 0; background: none; padding: 6px 2px 0; font: inherit;
    font-size: 13px; color: var(--ink-3); cursor: pointer; }
  .ds-more:hover { color: var(--ink); }
  .ds-adv { margin-top: 14px; }
  .ds-adv summary { cursor: pointer; font-size: 14px; color: var(--ink-3); list-style: none; width: max-content; }
  .ds-adv summary::-webkit-details-marker { display: none; }
  .ds-adv summary::after { content: " ▾"; font-size: 12px; }
  .ds-adv[open] summary::after { content: " ▴"; }
  .ds-adv summary:hover { color: var(--ink); }
  .ds-advgrid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px 12px; margin-top: 10px; }
  .ds-field { display: flex; flex-direction: column; gap: 5px; min-width: 0; }
  .ds-fl { font-size: 13px; color: var(--ink-3); }
  .ds-chk { grid-column: 1 / -1; font-size: 14px; }
  .ds-use { display: flex; align-items: center; gap: 9px; margin-top: 16px; font-size: 14px; cursor: pointer; }
  .ds-use input { accent-color: var(--accent); width: 15px; height: 15px; }
  .ds-note { font-size: 13px; color: var(--ink-3); margin-top: 8px; line-height: 1.5; }
  .ds-note.warn { color: var(--warn); }
  .ds-foot { display: flex; justify-content: flex-end; align-items: center; gap: 8px; margin-top: 20px; }
  .ds-go { min-width: 168px; padding: 9px 16px; font-size: 14px; }
  .ds-gate { background: var(--warn-soft); border-radius: 8px; padding: 12px 14px; margin-top: 14px;
    display: flex; flex-direction: column; gap: 6px; }
  .ds-gate b { font-size: 14px; }
  .ds-gsub { font-size: 14px; color: var(--ink-2); line-height: 1.5; }
  .ds-caution { font-size: 13px; color: var(--warn); margin: -8px 0 14px; }
  .ds-cold { font-size: 13px; color: var(--ink-2); line-height: 1.5; margin: -6px 0 14px; padding: 10px 12px;
    border-radius: 8px; background: var(--warn-soft); }
  .ds-cold b { color: var(--warn); font-weight: 600; }
  .ds-err { font-size: 14px; color: var(--bad); line-height: 1.5; }
  .ds-none { display: flex; flex-direction: column; gap: 6px; }
  .ds-none > b { font-size: 15px; }
  .ds-provs { display: flex; flex-direction: column; gap: 4px; margin-top: 10px; }
  .ds-prov { display: flex; align-items: center; gap: 11px; padding: 9px 12px; border-radius: 8px; background: var(--panel-2); }
  .ds-prov .mark2 { width: 20px; height: 20px; flex: none; }
  .ds-prov .b { padding: 5px 11px; font-size: 13px; }
  .ds-skrow { height: 52px; border-radius: 8px; margin-bottom: 4px; }
  .ds-stopsub { line-height: 1.55; margin-bottom: 4px; }

  /* the Active cards: one per deployment, the job acting on it folded in */
  /* USAGE — what a deployment did, from its own log. Two chart series,
     validated for CVD + contrast on both surfaces: --s1 generation, --s2 prompt. */
  .us-bar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin: 0 0 12px; }
  .us-bar .sp { flex: 1; }
  .us-deps .fchip .omark { width: 18px; height: 18px; border-radius: 5px; margin-right: 6px; }
  .us-deps .fchip .omark svg { width: 18px; height: 18px; }
  .us-grid { display: grid; grid-template-columns: minmax(0, 3fr) minmax(0, 2fr); gap: 12px; }
  .us-grid .card { min-width: 0; }
  .us-full { margin-bottom: 12px; }
  .us-chart { position: relative; }
  .us-chart svg { display: block; width: 100%; height: 132px; }
  .us-chart.tl svg { height: 44px; }
  .us-chart .base { stroke: var(--fill-2); stroke-width: 1; vector-effect: non-scaling-stroke; }
  .us-chart .grid { stroke: var(--fill); stroke-width: 1; vector-effect: non-scaling-stroke; }
  .us-chart .l1, .us-chart .l2 { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; vector-effect: non-scaling-stroke; }
  .us-chart .l1 { stroke: var(--s1); } .us-chart .l2 { stroke: var(--s2); }
  .us-chart .a1 { fill: var(--s1); opacity: .10; }
  .us-chart .bt { fill: var(--s1); opacity: .30; }
  .us-chart .sv { fill: var(--s1); }
  .us-chart .rq { stroke: var(--ink-2); stroke-width: 2; vector-effect: non-scaling-stroke; }
  .us-chart .rq.bad { stroke: var(--bad); }
  .us-chart .br { fill: var(--s1); }
  .us-chart .hit { fill: transparent; }
  .us-chart .xh { stroke: var(--ink-3); stroke-width: 1; stroke-dasharray: 3 3; vector-effect: non-scaling-stroke; }
  .us-ax { position: relative; height: 18px; margin-top: 6px; font-size: 12px; color: var(--ink-3); font-variant-numeric: tabular-nums; }
  .us-ax span { position: absolute; transform: translateX(-50%); white-space: nowrap; }
  .us-ax span:first-child { transform: none; } .us-ax span:last-child { transform: translateX(-100%); }
  .us-y { position: absolute; top: -2px; right: 0; font-size: 12px; color: var(--ink-3); font-variant-numeric: tabular-nums;
    background: var(--panel); padding: 0 0 2px 6px; }
  .us-chart .tip { top: -8px; z-index: 2; }
  .us-chart .tip b { font-weight: 600; } .us-chart .tip span { opacity: .65; margin-left: 8px; }
  .us-chart .tip i { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin: 0 6px 0 10px; }
  .us-chart .tip i:first-child { margin-left: 0; }
  .us-leg { display: flex; gap: 14px; flex-wrap: wrap; font-size: 12px; color: var(--ink-2); margin-top: 4px; }
  .us-leg span { display: inline-flex; align-items: center; gap: 6px; }
  .us-leg i { width: 10px; height: 10px; border-radius: 3px; background: var(--s1); }
  .us-leg i.bt { opacity: .3; } .us-leg i.p { background: var(--s2); }
  .us-leg i.ln { height: 2px; border-radius: 1px; }
  .us-leg i.rq { width: 2px; height: 10px; border-radius: 1px; background: var(--ink-2); }
  .us-empty { font-size: 13px; color: var(--ink-3); padding: 18px 0 8px; }
  .us-note { font-size: 13px; color: var(--ink-3); margin: -4px 0 10px; }
  .us-note b { color: var(--ink-2); font-weight: 600; }
  @media (max-width: 900px) { .us-grid { grid-template-columns: minmax(0, 1fr); } }
  /* SKILLS — a page list and the open page, edited in place like a document.
     No borders anywhere: the list is a panel, the page is a panel, and a row
     or a property lifts with a fill on hover. */
  .skx { display: grid; grid-template-columns: 264px minmax(0, 1fr); gap: 12px; align-items: start; }
  .skx-list { background: var(--panel); border-radius: var(--radius); padding: 8px; position: sticky; top: 16px;
    max-height: calc(100vh - 32px); overflow: auto; }
  .skx-top { display: flex; gap: 6px; margin-bottom: 6px; }
  .skx-find { flex: 1; display: flex; align-items: center; gap: 7px; height: 32px; padding: 0 9px; border-radius: 7px;
    background: var(--fill); color: var(--ink-3); min-width: 0; }
  .skx-find input { all: unset; flex: 1; min-width: 0; font-size: 13px; color: var(--ink); }
  .skx-find input::placeholder { color: var(--ink-3); }
  .skx-fi { font-size: 13px; }
  .skx-add, .skx-gadd { all: unset; cursor: pointer; display: inline-flex; align-items: center; justify-content: center;
    width: 32px; height: 32px; border-radius: 7px; color: var(--ink-2); font-size: 17px; }
  .skx-add { background: var(--fill); }
  .skx-add:hover, .skx-gadd:hover { background: var(--fill-2); color: var(--ink); }
  .skx-add:focus-visible, .skx-gadd:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
  .skx-g { margin-top: 8px; }
  .skx-gh { display: flex; align-items: center; gap: 6px; padding: 4px 8px 4px; font-size: 12px; font-weight: 600; color: var(--ink-3); }
  .skx-gh .skx-gn { font-weight: 500; }
  .skx-gadd { width: 22px; height: 22px; margin-left: auto; font-size: 15px; opacity: 0; }
  .skx-g:hover .skx-gadd, .skx-gadd:focus-visible { opacity: 1; }
  .skx-i { all: unset; box-sizing: border-box; cursor: pointer; width: 100%; display: flex; align-items: center; gap: 9px;
    height: 32px; padding: 0 8px; border-radius: 6px; font-size: 14px; color: var(--ink-2); }
  .skx-i:hover { background: var(--fill); color: var(--ink); }
  .skx-i.on { background: var(--fill); color: var(--ink); font-weight: 600; }
  .skx-i:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
  .skx-i .sglyph { width: 20px; height: 20px; border-radius: 5px; }
  .skx-i .sglyph svg { width: 14px; height: 14px; }
  .skx-in { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .skx-in.ph { color: var(--ink-3); font-weight: 500; }
  .skx-al { width: 6px; height: 6px; border-radius: 50%; background: var(--accent); flex: none; }
  .skx-none { font-size: 13px; color: var(--ink-3); padding: 4px 8px 6px; }

  .skx-doc { background: var(--panel); border-radius: var(--radius); min-height: 72vh; min-width: 0; }
  .skx-bar { display: flex; align-items: center; gap: 10px; height: 44px; padding: 0 10px 0 18px; }
  .skx-bar .sp { flex: 1; }
  .skx-crumb { font-size: 13px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .skx-status { font-size: 12px; color: var(--ink-3); white-space: nowrap; }
  .skx-status.ok { color: var(--ink-3); }
  .skx-status.bad { color: var(--bad); }
  .skx-more .pmenu-b { height: 28px; padding: 0 8px; background: none; }
  .skx-more .pmenu-b:hover { background: var(--fill); }
  .skx-more .pmenu-b i { display: none; }
  .skx-more .pmenu-b b { font-size: 16px; color: var(--ink-2); font-weight: 600; }
  .skx-confirm { display: flex; align-items: center; gap: 10px; margin: 0 14px; padding: 10px 12px; border-radius: 8px;
    background: var(--fill); font-size: 13px; color: var(--ink); }
  .skx-confirm .sp { flex: 1; }
  .skx-page { max-width: 760px; margin: 0 auto; padding: 26px 56px 40px; }
  .skx-page > .sglyph { width: 52px; height: 52px; border-radius: 12px; margin-bottom: 14px; }
  .skx-page > .sglyph svg { width: 36px; height: 36px; }
  .skx-title { font-size: 34px; font-weight: 600; letter-spacing: -.025em; line-height: 1.2; color: var(--ink); outline: none;
    word-break: break-word; }
  .skx-desc { font-size: 16px; color: var(--ink-2); line-height: 1.5; margin-top: 6px; outline: none; word-break: break-word; }
  .skx-title:empty::before, .skx-desc:empty::before { content: attr(data-ph); color: var(--ink-3); opacity: .6; pointer-events: none; }
  .skx-props { margin: 18px 0 20px; display: flex; flex-direction: column; gap: 2px; }
  .skx-pr { display: grid; grid-template-columns: 132px minmax(0, 1fr); align-items: center; min-height: 34px; gap: 8px; }
  .skx-pl { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--ink-3); padding: 0 6px; }
  .skx-pl .ic { width: 15px; height: 15px; opacity: .85; } .skx-pl .ic svg { width: 15px; height: 15px; }
  .skx-pv { min-width: 0; display: flex; align-items: center; font-size: 14px; color: var(--ink); }
  .skx-seg { display: inline-flex; gap: 2px; padding: 2px; border-radius: 7px; background: var(--fill); }
  .skx-seg button { all: unset; cursor: pointer; padding: 3px 10px; border-radius: 5px; font-size: 13px; color: var(--ink-2); }
  .skx-seg button:hover { color: var(--ink); }
  .skx-seg button.on { background: var(--seg-on); color: var(--ink); font-weight: 600; }
  .skx-seg button:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
  .skx-ro { padding: 4px 6px; color: var(--ink-2); }
  .skx-inp { all: unset; box-sizing: border-box; width: 100%; max-width: 320px; padding: 5px 6px; border-radius: 6px; font-size: 14px; color: var(--ink); }
  .skx-inp:hover { background: var(--fill); }
  .skx-inp:focus { background: var(--fill); }
  .skx-inp::placeholder { color: var(--ink-3); opacity: .7; }
  .skx-tools { display: flex; flex-wrap: wrap; align-items: center; gap: 5px; }
  .skx-tag { display: inline-flex; align-items: center; gap: 3px; height: 24px; padding: 0 4px 0 8px; border-radius: 5px;
    background: var(--fill); font-size: 13px; color: var(--ink); }
  .skx-tx { all: unset; cursor: pointer; width: 16px; height: 16px; display: inline-flex; align-items: center; justify-content: center;
    border-radius: 4px; color: var(--ink-3); font-size: 13px; }
  .skx-tx:hover { background: var(--fill-2); color: var(--ink); }
  .skx-tadd .pmenu-b { height: 24px; padding: 0 7px; background: none; font-size: 13px; }
  .skx-tadd .pmenu-b:hover { background: var(--fill); }
  .skx-tadd .pmenu-b i { display: none; }
  .skx-tadd .pmenu-b b { font-weight: 500; color: var(--ink-3); }
  .skx-tadd .pmenu { max-height: 280px; overflow: auto; }
  .skx-file { font-family: var(--mono); font-size: 12px; color: var(--ink-3); padding: 4px 6px; border-radius: 6px; cursor: pointer;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .skx-file:hover { background: var(--fill); color: var(--ink-2); }

  .skx-body { position: relative; }
  .skx-b { position: relative; border-radius: 6px; margin: 0 -8px 4px; padding: 3px 8px; cursor: text; }
  .skx-bc ul, .skx-bc ol { padding-left: 22px; margin: 0; }
  .skx-bc li { margin: 2px 0; }
  .skx-bc li.todo { list-style: none; margin-left: -22px; display: flex; align-items: flex-start; gap: 9px; }
  .skx-bc li.todo.done .skx-tt { color: var(--ink-3); text-decoration: line-through; text-decoration-color: var(--ink-3); }
  .skx-chk { all: unset; cursor: pointer; flex: none; width: 16px; height: 16px; margin-top: 5px; border-radius: 4px;
    background: var(--fill-2); display: inline-flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 600; color: #fff; }
  .skx-chk:hover { background: var(--ink-3); }
  .skx-chk.on { background: var(--accent); }
  .skx-chk:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
  .skx-b:hover { background: var(--panel-2); }
  .skx-b.editing { background: none; }
  .skx-plus { all: unset; position: absolute; left: -28px; top: 4px; width: 22px; height: 22px; border-radius: 5px; cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center; font-size: 16px; color: var(--ink-3); opacity: 0; }
  .skx-b:hover .skx-plus { opacity: 1; }
  .skx-plus:hover { background: var(--fill); color: var(--ink); }
  .skx-bc { font-size: 15px; line-height: 1.65; color: var(--ink); min-height: 1.65em; }
  .skx-bc > :first-child { margin-top: 0; } .skx-bc > :last-child { margin-bottom: 0; }
  .skx-bc.ph { color: var(--ink-3); opacity: .7; }
  .skx-b.k-h1 .skx-bc h1, .skx-ta.k-h1 { font-size: 26px; font-weight: 600; letter-spacing: -.02em; line-height: 1.3; }
  .skx-b.k-h2 .skx-bc h2, .skx-ta.k-h2 { font-size: 21px; font-weight: 600; letter-spacing: -.015em; line-height: 1.35; }
  .skx-b.k-h3 .skx-bc h3, .skx-ta.k-h3 { font-size: 17px; font-weight: 600; line-height: 1.4; }
  .skx-b.k-h1, .skx-b.k-h2 { margin-top: 14px; }
  .skx-b.k-h1 .skx-bc h1, .skx-b.k-h2 .skx-bc h2, .skx-b.k-h3 .skx-bc h3 { margin: 0; }
  .skx-ta { all: unset; box-sizing: border-box; display: block; width: 100%; resize: none; overflow: hidden; white-space: pre-wrap;
    font-size: 15px; line-height: 1.65; color: var(--ink); caret-color: var(--accent); min-height: 1.65em; word-break: break-word; }
  .skx-ta.k-code { font-family: var(--mono); font-size: 13px; line-height: 1.55; background: var(--fill); border-radius: 6px; padding: 10px 12px; }
  .skx-ta.k-quote { color: var(--ink-2); }
  .skx-raw { all: unset; box-sizing: border-box; display: block; width: 100%; resize: none; overflow: hidden; white-space: pre-wrap;
    font-family: var(--mono); font-size: 13px; line-height: 1.6; color: var(--ink); background: var(--panel-2); border-radius: 8px; padding: 14px 16px; }
  .skx-tail { min-height: 120px; cursor: text; }
  .skx-slash { top: calc(100% + 2px); min-width: 220px; }
  .skx-slash .pmenu-i.on { background: var(--fill); color: var(--ink); }
  .mem-mk { color: var(--ink-2); background: var(--fill); }
  .skx-i .mem-mk .ic, .skx-i .mem-mk .ic svg { width: 13px; height: 13px; }
  .skx-page > .mem-mk .ic, .skx-page > .mem-mk .ic svg { width: 26px; height: 26px; }
  .skx-i.ghost .skx-in { color: var(--ink-3); }
  .mem-new { font-size: 12px; color: var(--ink-3); opacity: 0; }
  .skx-i:hover .mem-new { opacity: 1; }
  .mem-side { font-size: 12px; color: var(--ink-3); flex: none; }
  .skx-ro.ok { color: var(--ink); } .skx-ro.warn { color: var(--warn); }
  .mem-ro { font-size: 15px; line-height: 1.65; }
  .skx-empty { padding: 60px 24px; text-align: center; font-size: 14px; color: var(--ink-3); }
  @media (max-width: 900px) {
    .skx { grid-template-columns: minmax(0, 1fr); }
    .skx-list { position: static; max-height: none; }
    .skx-page { padding: 18px 20px 32px; }
    .skx-plus { display: none; }
    .skx-pr { grid-template-columns: 104px minmax(0, 1fr); }
  }
  /* MCP — a board of connections: a card per server (status, where it runs,
     the tools it gives the agent), and a sheet with the tools in full. */
  .mcx-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 12px; }
  .mcx { background: var(--panel); border-radius: var(--radius); padding: 16px 16px 12px; display: flex; flex-direction: column;
    gap: 12px; min-width: 0; cursor: pointer; transition: background var(--t); }
  .mcx:hover { background: var(--panel-2); }
  .mcx:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
  .mcx-h, .mcs-h { display: flex; align-items: center; gap: 12px; min-width: 0; }
  .mcx-tt { flex: 1; min-width: 0; }
  .mcx-n { font-size: 15px; font-weight: 600; color: var(--ink); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mcx-w { font-family: var(--mono); font-size: 12px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 2px; }
  .mcx-st { flex: none; display: inline-flex; align-items: center; gap: 6px; height: 22px; padding: 0 9px; border-radius: 999px;
    font-size: 12px; font-weight: 600; background: var(--fill); color: var(--ink-2); }
  .mcx-st i { width: 6px; height: 6px; border-radius: 50%; background: var(--ink-3); }
  .mcx-st.ok { color: var(--ok); } .mcx-st.ok i { background: var(--ok); }
  .mcx-st.bad { color: var(--bad); } .mcx-st.bad i { background: var(--bad); }
  .mcx-st.warn { color: var(--warn); } .mcx-st.warn i { background: var(--warn); }
  .mcx-st.run i { animation: skpulse 1.1s ease-in-out infinite; }
  .mcx-b { min-height: 52px; display: flex; flex-direction: column; gap: 4px; }
  .mcx-chips { display: flex; flex-wrap: wrap; gap: 5px; }
  .mcx-chip { font-family: var(--mono); font-size: 12px; color: var(--ink-2); background: var(--fill); border-radius: 5px; padding: 2px 7px;
    max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mcx-chip.more { color: var(--ink-3); }
  .mcx-chip.sk { width: 88px; height: 20px; animation: skpulse 1.4s ease-in-out infinite; }
  .mcx-note { font-size: 13px; color: var(--ink-3); line-height: 1.45; }
  .mcx-note.bad { color: var(--ink); }
  .mcx-err { font-family: var(--mono); font-size: 12px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .mcx-f { display: flex; align-items: center; gap: 4px; padding-top: 10px; margin-top: auto; }
  .mcx-f .b { flex: none; }
  .mcx-f .sp { flex: 1; }
  .mcx-facts { font-size: 12px; color: var(--ink-3); font-variant-numeric: tabular-nums; white-space: nowrap;
    overflow: hidden; text-overflow: ellipsis; min-width: 0; }
  .mcx-f .b { height: 28px; padding: 0 10px; font-size: 13px; }
  .mcx-mono { font-family: var(--mono); font-size: 12px; color: var(--ink-2); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .mcx-link { all: unset; cursor: pointer; font-size: 13px; color: var(--ink-2); margin-left: 10px; padding: 1px 6px; border-radius: 5px; }
  .mcx-link:hover { background: var(--fill); color: var(--ink); }
  .mcx-link:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
  /* the sheet */
  .mcs-h { padding-right: 34px; }
  .mcs-h .sglyph { width: 40px; height: 40px; border-radius: 10px; }
  .mcs-n { font-size: 18px; font-weight: 600; color: var(--ink); letter-spacing: -.01em; }
  .mcs-line { font-size: 13px; color: var(--ink-2); margin: 10px 0 14px; line-height: 1.5; }
  .mcs-tabs { margin-bottom: 14px; }
  .mcs-tabs button { display: inline-flex; align-items: center; gap: 7px; padding: 5px 14px; font-size: 13px; }
  .mcs-tn2 { font-size: 11px; font-weight: 600; min-width: 18px; height: 17px; padding: 0 5px; border-radius: 999px; box-sizing: border-box;
    display: inline-flex; align-items: center; justify-content: center; background: var(--fill-2); color: var(--ink-2); }
  .mcs-tabs button.on .mcs-tn2 { background: var(--accent); color: var(--accent-ink); }
  .mcs-body { min-height: 220px; }
  .mcs-find { height: 32px; margin-bottom: 8px; }
  .mcs-tools { display: flex; flex-direction: column; gap: 2px; max-height: 56vh; overflow: auto; margin: 0 -8px; }
  .mcs-tool { padding: 10px 8px; border-radius: 8px; display: flex; flex-direction: column; gap: 4px; }
  .mcs-tool:hover { background: var(--panel-2); }
  .mcs-tn { font-family: var(--mono); font-size: 13px; font-weight: 600; color: var(--ink); }
  .mcs-td { font-size: 13px; color: var(--ink-2); line-height: 1.5; }
  .mcs-tool.sk .mcs-tn, .mcs-tool.sk .mcs-td { height: 12px; border-radius: 4px; background: var(--fill); animation: skpulse 1.4s ease-in-out infinite; }
  .mcs-tool.sk .mcs-tn { width: 160px; }
  .mcs-ps { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 2px; }
  .mcs-p { font-family: var(--mono); font-size: 11px; color: var(--ink-3); background: var(--fill); border-radius: 4px; padding: 1px 6px; }
  .mcs-p.req { color: var(--ink); }
  .mcs-p.req::after { content: "*"; color: var(--accent); margin-left: 1px; }
  .mcs-p i { font-style: normal; opacity: .7; margin-left: 5px; }
  .mcs-props { margin: 0 0 12px; }
  .mcs-props .skx-pl { padding: 0; }
  .mcs-file { cursor: pointer; }
  .mcs-foot { position: static; margin: 14px 0 0; padding: 0; background: none; }
  .mcs-foot .sp { flex: 1; }
  .mcs-comp { padding: 0; margin: 0; background: none; }
  .mcs-ex { margin-top: 18px; }
  .mcs-exh { font-size: 13px; font-weight: 600; color: var(--ink-2); margin-bottom: 4px; }
  .mcp-kv { display: flex; flex-direction: column; gap: 3px; min-width: 0; width: 100%; }
  .mcp-kvr { display: flex; gap: 10px; font-family: var(--mono); font-size: 12px; min-width: 0; }
  .mcp-k { color: var(--ink-2); flex: none; }
  .mcp-v { color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
  .mcp-v.secret { letter-spacing: .08em; }
  .mcp-err { display: flex; flex-direction: column; gap: 4px; background: var(--fill); border-radius: 8px; padding: 12px 14px; font-size: 13px; }
  .mcp-err b { color: var(--bad); font-weight: 600; }
  .mcp-err span { color: var(--ink-2); font-family: var(--mono); font-size: 12px; word-break: break-word; }
  .mcp-err .mcp-hint { font-family: var(--sans); font-size: 13px; color: var(--ink); }
  .mcp-json { margin: 0; font-family: var(--mono); font-size: 12px; line-height: 1.6; color: var(--ink-2); background: var(--panel-2);
    border-radius: 8px; padding: 12px 14px; overflow: auto; white-space: pre; max-height: 260px; }
  .mcp-jerr { font-size: 13px; color: var(--bad); margin-top: 6px; }
  .mcp-ex { all: unset; box-sizing: border-box; cursor: pointer; display: flex; flex-direction: column; gap: 2px; width: 100%;
    padding: 7px 10px; margin: 0 -10px; border-radius: 6px; }
  .mcp-ex:hover { background: var(--panel-2); }
  .mcp-ex:focus-visible { box-shadow: 0 0 0 2px var(--accent); }
  .mcp-exl { font-size: 13px; color: var(--ink-2); }
  .mcp-ext { color: var(--ink-3); }
  @media (max-width: 640px) { .mcx-grid { grid-template-columns: minmax(0, 1fr); } .mcx-st { padding: 0 7px; } }
  .dcards { display: grid; grid-template-columns: repeat(auto-fill, minmax(360px, 1fr)); gap: 12px; }
  .dcard { background: var(--panel); border-radius: var(--radius); padding: 16px; display: flex;
    flex-direction: column; gap: 12px; min-width: 0; }
  .dcard.bad { background: color-mix(in srgb, var(--bad-soft) 55%, var(--panel)); }
  .dc-h { display: flex; align-items: center; gap: 11px; min-width: 0; }
  .dc-h .omark { width: 36px; height: 36px; border-radius: 8px; }
  .dc-t { flex: 1; min-width: 0; }
  .dc-n { font-family: var(--mono); font-weight: 600; font-size: 14px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dc-s { display: flex; align-items: center; gap: 6px; margin-top: 2px; font-size: 13px; color: var(--ink-3); min-width: 0; }
  .dc-s span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dc-s .mark2 { width: 14px; height: 14px; flex: none; background: none; }
  .dc-s .mark2 svg { width: 12px; height: 12px; }
  .dc-st { flex: none; display: inline-flex; align-items: center; gap: 6px; padding: 3px 10px; border-radius: 999px;
    font-size: 13px; font-weight: 600; background: var(--fill); color: var(--ink-2); }
  .dc-st i { width: 6px; height: 6px; border-radius: 50%; background: var(--ink-3); }
  .dc-st.ok { background: var(--ok-soft); color: var(--ok); } .dc-st.ok i { background: var(--ok); }
  .dc-st.run { background: var(--info-soft); color: var(--info); }
  .dc-st.run i { background: var(--info); animation: pulse 1.4s ease-in-out infinite; }
  .dc-st.bad { background: var(--bad-soft); color: var(--bad); } .dc-st.bad i { background: var(--bad); }
  .dc-st.pend { background: var(--warn-soft); color: var(--warn); } .dc-st.pend i { background: var(--warn); }
  /* The stage track. Four stops, joined the way this sheet joins everything
     it will not draw a line for — a run of single pixels — which fills in
     with the accent as each stage is passed. */
  .dc-track { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); }
  .dc-step { position: relative; display: flex; flex-direction: column; gap: 7px; font-size: 12px; color: var(--ink-3); }
  .dc-step i { position: relative; z-index: 1; width: 9px; height: 9px; border-radius: 50%; background: var(--fill-2); }
  .dc-step::before { content: ""; position: absolute; left: 13px; right: 4px; top: 4px; height: 1px;
    background-image: repeating-linear-gradient(to right, var(--fill-2) 0 1px, transparent 1px 3px); }
  .dc-step:last-child::before { display: none; }
  .dc-step.did i { background: var(--accent); }
  .dc-step.did::before { background-image: repeating-linear-gradient(to right, var(--accent) 0 1px, transparent 1px 3px); }
  .dc-step.did span { color: var(--ink-2); }
  .dc-step.cur i { background: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft-2); animation: pulse 1.6s ease-in-out infinite; }
  .dc-step.cur span { color: var(--ink); font-weight: 600; }
  .dc-now { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; margin-top: 12px;
    font-size: 14px; color: var(--ink-2); }
  .dc-clock { font-family: var(--sans); font-variant-numeric: tabular-nums; font-size: 14px; font-weight: 600; color: var(--ink); font-variant-numeric: tabular-nums; }
  .dc-last { margin-top: 4px; font-family: var(--mono); font-size: 12px; color: var(--ink-3);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dc-line { font-size: 14px; color: var(--ink-2); }
  .dc-ep { display: flex; align-items: center; gap: 8px; min-width: 0; padding: 6px 6px 6px 11px; border-radius: 8px; background: var(--panel-2); }
  .dc-url { flex: 1; min-width: 0; font-family: var(--mono); font-size: 13px; color: var(--ink-2);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dc-url.dim { font-family: var(--sans); color: var(--ink-3); }
  .dc-copy { padding: 3px 10px; font-size: 13px; }
  .dc-facts { display: flex; flex-wrap: wrap; gap: 4px 14px; font-size: 13px; color: var(--ink-3); }
  .dc-facts .amb { color: var(--warn); }
  .dc-err { display: flex; flex-direction: column; gap: 3px; }
  .dc-err b { font-size: 14px; color: var(--bad); font-weight: 600; }
  .dc-err span { font-size: 13px; color: var(--ink-2); line-height: 1.45; }
  .dc-warn { font-size: 13px; color: var(--warn); line-height: 1.45; }
  .dc-foot { display: flex; align-items: center; gap: 6px; margin-top: auto; padding-top: 11px;
    background-image: repeating-linear-gradient(to right, var(--fill-2) 0 1px, transparent 1px 4px);
    background-size: 100% 1px; background-repeat: no-repeat; }
  .dc-foot .b { padding: 5px 11px; font-size: 13px; }
  .dc-sp { flex: 1; }
  .dc-inuse { display: inline-flex; align-items: center; gap: 7px; font-size: 13px; font-weight: 600; color: var(--accent); }
  .dc-inuse::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--accent); }

  /* the providers, folded to one line once one of them can deploy */
  .dp-provsum { display: flex; align-items: center; gap: 12px; width: 100%; padding: 12px 16px; border: 0;
    border-radius: var(--radius); background: var(--panel); font: inherit; color: var(--ink); text-align: left;
    cursor: pointer; transition: background var(--t); }
  .dp-provsum:hover { background: var(--panel-2); }
  .dp-provmarks { display: flex; gap: 5px; flex: none; }
  .dp-provmarks .mark2 { width: 20px; height: 20px; background: none; }
  .dp-provtxt { flex: 1; min-width: 0; font-size: 14px; color: var(--ink-3); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .dp-provtxt b { color: var(--ink); font-weight: 600; }
  .dp-provx { flex: none; font-size: 14px; font-weight: 600; color: var(--ink-3); }
  .dp-provsum:hover .dp-provx { color: var(--accent); }
  #dp-provsec #dp-grid { margin-top: 12px; }

  /* try it: one real request, the answer, and how fast it came */
  .tr-row { display: flex; gap: 8px; }
  .tr-row .in { flex: 1; min-width: 0; }
  .tr-out { margin-top: 12px; min-height: 84px; padding: 14px 16px; border-radius: 8px; background: var(--panel-2);
    font-size: 15px; line-height: 1.55; color: var(--ink); white-space: pre-wrap; word-break: break-word; }
  .tr-out:empty { display: none; }
  .tr-out.wait { color: var(--ink-3); font-size: 14px; }
  .tr-out.bad { color: var(--bad); font-size: 14px; }
  .tr-stats { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 12px; }
  .tr-stats:empty { display: none; }
  /* logs: a pane that follows the output */
  .lg-h { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; min-width: 0; padding-right: 34px; }
  .lg-h h3 { margin: 0; }
  .lg-sub { font-size: 13px; color: var(--ink-3); font-family: var(--mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .lg-st { font-size: 13px; color: var(--ink-3); font-family: var(--sans); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .lg-follow { font-size: 14px; }
  .in-log { height: 34vh; margin-bottom: 12px; font-size: 12px; }
  .lg-body { margin: 0; height: 62vh; overflow: auto; padding: 12px 14px; border-radius: 8px; background: var(--panel-2);
    font-family: var(--mono); font-size: 13px; line-height: 1.6; color: var(--ink-2); white-space: pre-wrap; word-break: break-word; }
</style>
</head>
<body>
<aside id="rail">
  <div class="rail-h">
    <div class="brand"><img src="/mantis.svg" alt="">
      <span class="bt"><span class="bn">mantis</span><span class="bs" id="railhost">local</span></span></div>
    <button class="railtog" id="railtog" title="collapse the rail  (⌘\)" aria-label="collapse the rail">&#171;</button>
  </div>
  <nav id="nav">
    <div class="ngrp" data-g="workspace">
      <button class="ng" aria-expanded="true"><span>Workspace</span><i class="ngc">&#9656;</i></button>
      <div class="ngi">
        <button data-v="home" class="on"><i class="ic" data-i="home"></i><span class="nl">Overview</span></button>
      </div>
    </div>
    <div class="ngrp" data-g="models">
      <button class="ng" aria-expanded="true"><span>Models</span><i class="ngc">&#9656;</i></button>
      <div class="ngi">
        <button data-v="models"><i class="ic" data-i="models"></i><span class="nl">My models</span><span class="nc" id="n-models"></span><span class="ncar">&#9656;</span></button>
        <div class="nsub" id="sub-models"></div>
        <button data-v="deploy"><i class="ic" data-i="deploy"></i><span class="nl">Deploy</span><span class="nc" id="n-deploy"></span><span class="ncar">&#9656;</span></button>
        <div class="nsub" id="sub-deploy"></div>
      </div>
    </div>
    <div class="ngrp" data-g="work">
      <button class="ng" aria-expanded="true"><span>Work</span><i class="ngc">&#9656;</i></button>
      <div class="ngi">
        <button data-v="sessions"><i class="ic" data-i="sessions"></i><span class="nl">Sessions</span><span class="nc" id="n-sessions"></span><span class="ncar">&#9656;</span></button>
        <div class="nsub" id="sub-sessions"></div>
        <button data-v="activity"><i class="ic" data-i="activity"></i><span class="nl">Activity</span><span class="nc" id="n-activity"></span><span class="ncar">&#9656;</span></button>
        <div class="nsub" id="sub-activity"></div>
      </div>
    </div>
    <div class="ngrp" data-g="extend">
      <button class="ng" aria-expanded="true"><span>Extend</span><i class="ngc">&#9656;</i></button>
      <div class="ngi">
        <button data-v="mcp"><i class="ic" data-i="mcp"></i><span class="nl">MCP</span><span class="nc" id="n-mcp"></span></button>
        <button data-v="skills"><i class="ic" data-i="skills"></i><span class="nl">Skills</span><span class="nc" id="n-skills"></span></button>
        <button data-v="memory"><i class="ic" data-i="memory"></i><span class="nl">Memory</span><span class="nc" id="n-memory"></span></button>
      </div>
    </div>
    <div class="ngrp" data-g="system">
      <button class="ng" aria-expanded="true"><span>System</span><i class="ngc">&#9656;</i></button>
      <div class="ngi">
        <button data-v="config"><i class="ic" data-i="config"></i><span class="nl">Config</span><span class="nc" id="n-config"></span></button>
      </div>
    </div>
  </nav>
  <div class="rail-f">
    <div class="railfoot" id="railfoot"></div>
    <div class="rail-b">
      <button class="tb icon" id="themebtn" title="theme">&#9680;</button>
      <span class="ver" id="railver"></span>
      <span class="lan" id="lanind">local</span>
    </div>
  </div>
</aside>
<div id="shell">
<div id="scrim"></div>
<header id="top">
  <button class="ham" id="ham" aria-label="open navigation" aria-expanded="false"><i class="ic" data-i="menu"></i></button>
  <div class="crumb" id="crumb"></div>
  <div class="topr">
    <button class="tb" id="cmdk" title="command palette"><span>search or jump…</span><kbd>⌘K</kbd></button>
  </div>
</header>
<main>
  <section id="home" class="view on"><div class="scroll"><div class="page wide" id="homepad"></div></div></section>
  <section id="skills" class="view"><div class="scroll"><div class="page" id="skillspad"></div></div></section>
  <section id="memory" class="view"><div class="scroll"><div class="page" id="memorypad"></div></div></section>
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
</div>
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
// ==========================================================================
// THE MARKS — one icon system for the whole dashboard.
//
// Every glyph is drawn on the SAME 24-unit grid at the SAME 1.7 stroke, in
// currentColor with no fills except where a fill is the point (a live dot, a
// pixel block). That is what lets an icon sit in a 31px rail row, a 15px tile
// header and a 16px section label without ever being redrawn: the row sets a
// colour and a box, the glyph inherits both.
// They are literal about what this program does rather than generic — a model
// is a chip with pins, a deployment is a card in a rack with its power light
// on, MCP is two tools handed across a dashed link. A rail collapsed to marks
// only is still navigable, which is the test each one had to pass.
// ==========================================================================
const IC0 = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
const ICONS = {
  // a brain: two hemispheres and their folds (after Lucide's, ISC) on the same 24 grid and stroke
  memory: IC0 + '<path d="M12 5a3 3 0 1 0-6 .13 4 4 0 0 0-2.52 5.77 4 4 0 0 0 .55 6.59A4 4 0 1 0 12 18z"/>' +
    '<path d="M12 5a3 3 0 1 1 6 .13 4 4 0 0 1 2.52 5.77 4 4 0 0 1-.55 6.59A4 4 0 1 1 12 18z"/>' +
    '<path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/><path d="M12 5v13"/></svg>',
  // OVERVIEW — the instrument panel itself: four readings, one of them live
  menu: IC0 + '<path d="M3.5 6.5h17M3.5 12h17M3.5 17.5h17"/></svg>',
  home: IC0 + '<rect x="3" y="3" width="8" height="8.5" rx="2"/><rect x="13" y="3" width="8" height="5" rx="2"/>' +
    '<rect x="13" y="10" width="8" height="11" rx="2"/><rect x="3" y="13.5" width="8" height="7.5" rx="2"/>' +
    '<rect x="5.5" y="6" width="3" height="3" rx=".7" fill="currentColor" stroke="none"/></svg>',
  // MY MODELS — a chip with pins: the thing you seat in the socket
  models: IC0 + '<rect x="6.5" y="6.5" width="11" height="11" rx="2.5"/>' +
    '<rect x="10.2" y="10.2" width="3.6" height="3.6" rx="1" fill="currentColor" stroke="none"/>' +
    '<path d="M9.5 3v3.5M14.5 3v3.5M9.5 17.5V21M14.5 17.5V21M3 9.5h3.5M3 14.5h3.5M17.5 9.5H21M17.5 14.5H21"/></svg>',
  // DEPLOY — a rented card in its rack, power light lit
  deploy: IC0 + '<rect x="2.5" y="6.5" width="19" height="11" rx="2.5"/><path d="M6.5 10.5h6M6.5 13.5h4"/>' +
    '<circle cx="17.5" cy="12" r="1.4" fill="currentColor" stroke="none"/><path d="M7 17.5V20M17 17.5V20"/></svg>',
  // SESSIONS — a transcript: your turn, then its turn
  sessions: IC0 + '<rect x="2.5" y="4" width="13" height="8" rx="3"/><path d="M6 8h6"/>' +
    '<rect x="8.5" y="14" width="13" height="7" rx="3"/></svg>',
  // ACTIVITY — a trace with something happening on it
  activity: IC0 + '<path d="M2.5 13h3.6l2.4-7 3.4 13 2.4-8 1.5 2h6.3"/></svg>',
  // MCP — a server handing tools across a dashed link
  mcp: IC0 + '<rect x="2.5" y="7.5" width="8" height="9" rx="2"/><path d="M5.2 10.5h2.6M5.2 13.5h2"/>' +
    '<path d="M10.5 12h3.4" stroke-dasharray="2 2.4"/><circle cx="18" cy="8.4" r="2.7"/><circle cx="18" cy="15.6" r="2.7"/>' +
    '<path d="M13.9 12l1.9-2.1M13.9 12l1.9 2.1"/></svg>',
  // SKILLS — a playbook the agent opens when the task matches
  skills: IC0 + '<path d="M12 7.2C9.9 5.6 7.3 5 4.8 5.4v12.2c2.5-.4 5.1.2 7.2 1.8 2.1-1.6 4.7-2.2 7.2-1.8V5.4C16.7 5 14.1 5.6 12 7.2z"/>' +
    '<path d="M12 7.2v12.2"/></svg>',
  // CONFIG — the layers, each with its own setting
  config: IC0 + '<path d="M3 7.5h8M15.5 7.5H21M3 16.5h4.5M12 16.5h9"/><circle cx="13" cy="7.5" r="2.5"/><circle cx="9.5" cy="16.5" r="2.5"/></svg>',
  // ---- readings and card headers ----
  msg: IC0 + '<path d="M20.5 15.5a2.5 2.5 0 0 1-2.5 2.5H8l-4.5 3.5V5.5A2.5 2.5 0 0 1 6 3h12a2.5 2.5 0 0 1 2.5 2.5z"/><path d="M8 8.5h8M8 12.5h5"/></svg>',
  tool: IC0 + '<path d="M14.8 6.4a4.2 4.2 0 0 0-5.6 5.6L3.5 17.7V20.5H6.3l5.7-5.7a4.2 4.2 0 0 0 5.6-5.6l-2.6 2.6-2.2-2.2z"/></svg>',
  spend: IC0 + '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5v9M9.9 9.7c0-1 .9-1.8 2.1-1.8s2.1.8 2.1 1.8c0 2.4-4.2 1.4-4.2 3.9 0 1.1 1 1.9 2.1 1.9s2.1-.8 2.1-1.9"/></svg>',
  streak: IC0 + '<rect x="3" y="5" width="18" height="16" rx="2.5"/><path d="M3 9.8h18M8 2.8v4M16 2.8v4"/>' +
    '<rect x="7" y="13" width="3" height="3" rx=".7" fill="currentColor" stroke="none"/>' +
    '<rect x="14" y="13" width="3" height="3" rx=".7" fill="currentColor" stroke="none"/></svg>',
  key: IC0 + '<circle cx="8" cy="12" r="4.5"/><path d="M12.5 12H21M18 12v3.5M15.5 12v2.5"/></svg>',
  clock: IC0 + '<circle cx="12" cy="12" r="8.5"/><path d="M12 7v5.3l3.4 2"/></svg>',
  folder: IC0 + '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5h4.2l2 2.6h8.8A1.5 1.5 0 0 1 21 9.1v9.4a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5z"/></svg>',
  trace: IC0 + '<path d="M3.5 3.5v17h17"/><path d="M6.5 15.5l4-5 3 3 5.5-7.5"/></svg>',
};
// One helper, three sizes handled by CSS: never inline a width here.
function icon(name, cls) { const i = el("i", "ic" + (cls ? " " + cls : "")); i.innerHTML = ICONS[name] || ""; return i; }
// The rail's marks are in the served markup (so the shell is legible without
// running any of this); this is what fills them in.
function paintIcons(root) {
  (root || document).querySelectorAll("i.ic[data-i]").forEach(n => { if (!n.firstChild) n.innerHTML = ICONS[n.dataset.i] || ""; });
}
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
function hideModal() {
  const wasConnect = typeof CONNECT !== "undefined" && CONNECT.fam && connectOpen();
  document.getElementById("modal").className = "";
  if (wasConnect) { const d = CONNECT.dirty; CONNECT.fam = null; if (d && curView === "models") loadModels(); }
}
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
  const W = 1000, H = 200, PL = 6, PR = 6, PT = 14, PB = 30;
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
  const sigLine = path(mean);
  const area = sigLine + ` L${(PL+iw).toFixed(1)},${(PT+ih).toFixed(1)} L${PL.toFixed(1)},${(PT+ih).toFixed(1)} Z`;
  const pi = vals.reduce((b,v,i) => v > vals[b] ? i : b, 0);
  // Month ticks, but never two labels on top of each other at the left edge
  // where the series starts mid-month.
  let ticks = "", lastM = -1, lastX = -999;
  series.forEach((s,i) => {
    const dt = new Date(s.date+"T00:00:00"), m = dt.getMonth(), x = X(i);
    if (m !== lastM && i > 3 && i < n-3 && x - lastX > 60) {
      lastM = m; lastX = x;
      ticks += `<text x="${x.toFixed(1)}" y="${H-3}" text-anchor="middle">${MON[m]}</text>`;
    } else if (m !== lastM) { lastM = m; }
  });
  const peak = vals[pi] ? `<line class="pk" x1="${X(pi).toFixed(1)}" y1="${(PT-6).toFixed(1)}" x2="${X(pi).toFixed(1)}" y2="${(PT+ih).toFixed(1)}"/>` +
    `<circle class="pkd" cx="${X(pi).toFixed(1)}" cy="${Y(vals[pi]).toFixed(1)}" r="3"/>` : "";
  // one line — the 7-day mean — over a gradient; the raw daily series was a
  // second, jagged line under it that only added noise. The hover says the
  // day's real number.
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="messages per day">` +
    `<defs><linearGradient id="trg" x1="0" y1="0" x2="0" y2="1">` +
    `<stop offset="0" stop-color="var(--accent)" stop-opacity=".20"/>` +
    `<stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>` +
    `<line class="base" x1="${PL}" y1="${PT+ih}" x2="${PL+iw}" y2="${PT+ih}"/>` +
    `<path class="env" fill="url(#trg)" d="${area}"/>${peak}` +
    `<path class="sig" d="${sigLine}"/>${ticks}` +
    `<line class="xh" x1="0" y1="${PT}" x2="0" y2="${PT+ih}" style="display:none"/></svg>` +
    `<div class="tip" style="display:none"></div>`;
  const svg = box.querySelector("svg"), xh = box.querySelector(".xh"), tip = box.querySelector(".tip");
  svg.onmousemove = ev => {
    const r = svg.getBoundingClientRect(); if (!r.width) return;
    const fx = (ev.clientX - r.left) / r.width * W;
    const i = Math.max(0, Math.min(n - 1, Math.round((fx - PL) / Math.max(1, iw) * (n - 1))));
    const x = X(i);
    xh.setAttribute("x1", x.toFixed(1)); xh.setAttribute("x2", x.toFixed(1)); xh.style.display = "";
    const dt = new Date(series[i].date + "T00:00:00");
    tip.innerHTML = "<b>" + fmt(vals[i]) + "</b> messages<span>" + dt.toLocaleDateString([], { month: "short", day: "numeric" }) + "</span>";
    tip.style.display = "";
    const px = x / W * r.width, tw = tip.offsetWidth || 120;
    tip.style.left = Math.max(0, Math.min(r.width - tw, px - tw / 2)) + "px";
  };
  svg.onmouseleave = () => { xh.style.display = "none"; tip.style.display = "none"; };
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
  if (s < 48 * 3600) return Math.floor(s/3600) + "h " + Math.floor(s%3600/60) + "m";
  return Math.floor(s/86400) + "d " + Math.floor(s%86400/3600) + "h"; };   // "16d 6h", not "390h 24m"
const fmtBytes = b => { if (!b) return "—"; const u = ["B","KB","MB","GB","TB"]; let i = 0;
  while (b >= 1024 && i < u.length-1) { b /= 1024; i++; } return b.toFixed(b >= 10 || i === 0 ? 0 : 1) + " " + u[i]; };
// the same words the Connect sheet uses: how it is connected, or that it isn't
const AUTH_LABEL = { saved: "API key", env: "API key", oauth: "Subscription", none: "Not connected" };
// connected is one state, whichever way in — three colours for it read as three states
const AUTH_CLS = { saved: "acc", env: "acc", oauth: "acc", none: "" };
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
    card.append(famMark(f.id, f.logo, f.label));
    card.append(el("div","fn", f.label));
    // the second line is the most specific true thing about this family: the
    // model you last ran from it, or — before you ever have — where it points
    const sub = f.last_model ? f.last_model
      : f.id === "oss" && f.local && f.local.reachable
        ? f.local.model_count + " local model" + (f.local.model_count === 1 ? "" : "s") + " · " + f.local.loaded_count + " loaded"
      : target ? "no model used yet"
      : "not in catalog";
    // an endpoint URL is not a caption; it is still there on hover
    const sl = el("div","fm" + (f.last_model ? "" : " none"), sub);
    sl.title = target && target.base_url ? target.base_url : sub;
    card.append(sl);
    const fr = el("div","fr");
    // state, until you hover — then the one action that changes the state
    const st = el("div","fst");
    if (f.id === "oss") {
      // a family of many vendors: connected is "any one of them answers",
      // and Ollama running is the only thing worth naming on its own
      const loc = f.local || {};
      st.append(el("span","t2 " + (f.ready || loc.reachable ? "acc" : ""),
        loc.reachable ? "Ollama running" : f.ready ? "Connected" : "Not connected"));
    } else if (!provs.length) {
      st.append(el("span","t2 amb","no catalog"));
    } else {
      st.append(el("span","t2 " + (AUTH_CLS[target.auth] || ""), AUTH_LABEL[target.auth] || target.auth));
    }
    fr.append(st);
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
    ff.append(pr, tb); fr.append(ff);
    card.append(fr);
    card.title = f.label + (f.is_current ? " · the family you are running" : "") +
      (target && target.key_masked ? " · " + target.key_masked : "");
    card.onclick = () => unlockFamily(f.id);
    return card;
  });
  paintSubs();
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
// two are drawn differently on purpose: a tint is a guess, solid is a reading.
let SPEND_WIN = 7, SPEND_TOUCHED = false;
function spendBars(days) {
  const W = 1000, H = 170, PL = 6, PR = 6, PT = 12, PB = 30, n = days.length;
  const iw = W-PL-PR, ih = H-PT-PB, bw = iw/Math.max(1, n);
  const tot = d => d.est_in + d.est_out + d.rec_in + d.rec_out;
  const max = Math.max(1, ...days.map(tot));
  let bars = "", ticks = "";
  days.forEach((d, i) => {
    const x = PL + i*bw + bw*0.15, w = bw*0.7, y0 = PT + ih;
    const hr = (d.rec_in + d.rec_out)/max*ih, he = (d.est_in + d.est_out)/max*ih;
    const title = `<title>${d.date} · est ${fmtTok(d.est_in+d.est_out)} tok${d.est_usd != null ? " ≈ " + fmtUsd(d.est_usd) : ""}` +
      `${(d.rec_in+d.rec_out) ? " · recorded " + fmtTok(d.rec_in+d.rec_out) + " tok " + fmtUsd(d.rec_usd) : ""}</title>`;
    // stacked: recorded (a reading) at the base, estimated (a guess) above it
    // in a tint of the same hue, 2px of surface between them; only the top
    // segment is rounded, and every bar stands on the baseline
    const seg = (cls, top, h, round) => {
      if (h <= 0) return "";
      const r = round ? Math.min(4, w / 2, h) : 0, x1 = x + w, yb = top + h;
      return `<path class="${cls}" d="M${x.toFixed(1)},${yb.toFixed(1)} V${(top + r).toFixed(1)} Q${x.toFixed(1)},${top.toFixed(1)} ${(x + r).toFixed(1)},${top.toFixed(1)} ` +
        `H${(x1 - r).toFixed(1)} Q${x1.toFixed(1)},${top.toFixed(1)} ${x1.toFixed(1)},${(top + r).toFixed(1)} V${yb.toFixed(1)} Z">${title}</path>`;
    };
    const gap = hr && he ? 2 : 0;
    bars += seg("rec", y0 - hr, hr, !he);
    bars += seg("est", y0 - hr - he - gap, he - (he > gap ? 0 : 0), true);
    const dt = new Date(d.date + "T00:00:00");
    if (n <= 7 || dt.getDay() === 1 || i === n-1)
      ticks += `<text x="${(x+w/2).toFixed(1)}" y="${H-3}" text-anchor="middle">${n <= 7 ? WD[(dt.getDay()+6)%7] : (dt.getMonth()+1) + "/" + dt.getDate()}</text>`;
  });
  return `<svg class="bars" viewBox="0 0 ${W} ${H}" role="img" aria-label="tokens per day">` +
    `<line class="base" x1="${PL}" y1="${PT+ih}" x2="${PL+iw}" y2="${PT+ih}"/>${bars}${ticks}</svg>`;
}
function renderSpend(box, sp, win) {
  box.innerHTML = "";
  win = win || SPEND_WIN;
  const t = (sp.totals || {})[String(win)] || {};
  const chips = el("div","fchips");
  [7, 30].forEach(n => {
    const c = el("button","fchip" + (n === win ? " on" : ""), n + "d");
    c.onclick = () => { SPEND_WIN = n; SPEND_TOUCHED = true; renderSpend(box, sp, n); };
    chips.append(c);
  });
  cardHead(box, "spend", "Spend & usage", "last " + win + " days", [chips]);
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
  leg.innerHTML = '<span><i></i>Recorded</span><span><i class="est"></i>Estimated</span>';
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
  pageHead(pad, "Activity", (act.jobs || []).length + (act.runs || []).length || null, null, [ref]);
  pageReads(pad, [
    { v: c7.running || 0, k: "running" },
    { v: c7.done || 0, k: "done" },
    { v: c7.error || 0, k: "failed" },
    { v: "7 days", k: "window", opt: 2 },
  ], null, "Jobs and workflow runs this machine recorded, newest first.");
  // The readings line already says running / done / failed; three tiles
  // repeating it at 28px was the loudest thing on the page and said nothing new.
  const bar = el("div","filters");
  const find = findBox("Filter — name, kind, status…  ( / )");
  find.wrap.style.marginBottom = "0"; find.wrap.style.flex = "1"; find.input.value = ACT.q;
  find.input.oninput = () => { ACT.q = find.input.value; ACT.shown = 50; renderActivityPage(); };
  bar.append(find.wrap);
  const chips = el("div","fchips");
  ACT_FILTERS.forEach(([k, lab]) => {
    const ch = el("button","fchip" + (k === ACT.filter ? " on" : ""), lab);
    ch.onclick = () => { ACT.filter = k; ACT.shown = 50; chips.querySelectorAll(".fchip").forEach(x => x.classList.toggle("on", x === ch)); renderActivityPage(); refreshCrumb(); };
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
    // the dot already carries the colour; the word is just the word, and only
    // a failure is allowed to be loud
    const sc = statusClass(r.status, r.active), word = String(r.status || "?");
    const st = el("span","xs" + (sc === "bad" ? " bad" : ""), word.charAt(0).toUpperCase() + word.slice(1)); row.append(st);
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
// ---- the four readings ----------------------------------------------------
// Three helpers, one grammar: a level over time is a line, a count per day is
// a column, and a yes/no per day is a block. All three are drawn edge to edge
// on a 100-unit box with preserveAspectRatio="none", so a tile can be any
// width and the shape still lands on the card's own padding.
let SPARK_ID = 0;
// Every tile's chart is the same mark: a 2px line over a gradient that fades
// to nothing at the baseline. Four tiles in four chart styles read as four
// different products; one shape reads as one instrument.
function sparkLine(vals) {
  const n = vals.length, max = Math.max(1, ...vals), W = 100, H = 38, PB = 2;
  const X = i => (n === 1 ? W / 2 : i / (n - 1) * W);
  const Y = v => H - PB - v / max * (H - PB - 4);
  const d = vals.map((v, i) => X(i).toFixed(2) + "," + Y(v).toFixed(2)).join(" L");
  const id = "sg" + (++SPARK_ID);
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" aria-hidden="true">' +
    '<defs><linearGradient id="' + id + '" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0" stop-color="var(--accent)" stop-opacity=".22"/>' +
    '<stop offset="1" stop-color="var(--accent)" stop-opacity="0"/></linearGradient></defs>' +
    '<path class="ar" fill="url(#' + id + ')" d="M' + d + ' L' + W + ',' + H + ' L0,' + H + ' Z"/>' +
    '<path class="ln" d="M' + d + '"/></svg>';
}
function sparkBars(vals) {
  const n = vals.length, max = Math.max(1, ...vals), W = 100, H = 38, bw = W / n;
  let out = "";
  vals.forEach((v, i) => {
    const h = Math.max(v > 0 ? 1.5 : 0.8, v / max * (H - 4));
    out += '<rect class="br' + (i === n - 1 ? " hi" : "") + (v ? "" : " fl") + '" x="' + (i * bw + bw * 0.16).toFixed(2) +
      '" y="' + (H - h).toFixed(2) + '" width="' + (bw * 0.68).toFixed(2) + '" height="' + h.toFixed(2) + '" rx=".6"/>';
  });
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" aria-hidden="true">' + out + '</svg>';
}
// a day you worked is a block, a day you didn't is the ghost of one — the
// same pixel grid the provider cards are marked on, read as a fortnight
function dayBlocks(vals) {
  const n = vals.length, W = 100, H = 38, bw = W / n, r = Math.min(bw * 0.3, 3.2);
  let out = "";
  vals.forEach((v, i) => {
    out += '<circle class="blk' + (v ? "" : " off") + '" cx="' + (i * bw + bw / 2).toFixed(2) + '" cy="' + (H / 2 + 4) +
      '" r="' + r.toFixed(2) + '"/>';
  });
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="xMidYMid meet" aria-hidden="true">' + out + '</svg>';
}
// A delta is only a delta if there was a previous window to compare with, and
// only a direction if the move clears a point. Anything under that is noise
// and gets the neutral ink — a dashboard that tints noise is lying quietly.
function deltaEl(cur, prev, unit) {
  if (!prev) return null;
  const pc = (cur - prev) / prev * 100;
  const cls = pc >= 1 ? "up" : pc <= -1 ? "dn" : "";
  const d = el("span","dlt" + (cls ? " " + cls : ""));
  d.append(document.createTextNode((pc >= 1 ? "↗" : pc <= -1 ? "↘" : "→") + " " + Math.abs(pc).toFixed(pc < 10 ? 1 : 0) + "%"));
  d.title = "vs the " + (unit || "7 days") + " before";
  return d;
}
function tile(box, iconName, label, value, small, sub, chart, dlt) {
  const t = el("div","tile");
  const h = el("div","tile-h");
  h.append(icon(iconName)); h.append(el("span","nm", label));
  if (dlt) { const sp = el("span"); sp.style.flex = "1"; h.append(sp); h.append(dlt); }
  t.append(h);
  const v = el("div","tile-v", value);
  if (small) v.append(el("small", null, small));
  t.append(v);
  if (sub) { const sb = el("div","tile-s", sub); sb.title = sub; t.append(sb); }
  if (chart) { const c = el("div","tile-c"); c.innerHTML = chart; t.append(c); }
  else t.classList.add("flat");
  box.append(t);
  return t;
}
// A page's stat row: the SAME tile the Overview reads with, so a figure means
// the same thing wherever you meet it. Two rules keep it honest — a tile only
// draws a chart when there is a real series behind it, and only carries a
// delta when there is a previous period to compare with. Everything else gets
// a figure and a line of context, which is what most of these actually are.
function statRow(pad, stats) {
  const box = el("div","tiles");
  stats.forEach(x => {
    const t = tile(box, x.icon, x.label, x.value, x.small, x.sub, x.chart || "", x.delta || null);
    if (x.id) t.id = x.id;
  });
  box.classList.toggle("tiles-3", stats.length === 3);
  pad.append(box);
  return box;
}
function cardHead(card, iconName, title, note, right) {
  const h = el("div","c-h");
  if (iconName) h.append(icon(iconName));
  h.append(el("h3", null, title));
  if (note) h.append(el("span","cn", note));
  h.append(el("span","sp"));
  (right || []).forEach(x => h.append(x));
  card.append(h);
  return h;
}
const sumOf = (days, k) => days.reduce((a, d) => a + (d[k] || 0), 0);

// ==========================================================================
// OVERVIEW — four readings, then instruments on the left and standings on
// the right.
//
// The question the page answers, in order: what has this machine been doing
// this week (the tiles), what does that look like over six months (the
// trace), what did it cost (spend), where did it happen (projects) — and, on
// the right, the state you could change right now: which families are ready,
// when you actually work, what the agent reaches for.
// ==========================================================================
async function loadHome() {
  const pad = document.getElementById("homepad");
  const my = ++homeReq;
  if (!pad.childElementCount) skeleton(pad);
  const [a, g, sp, act] = await Promise.all([
    api("/api/analytics"),
    api("/api/providers").catch(() => ({ families: [] })),
    api("/api/spend").catch(() => null),
    api("/api/activity?" + q({ limit: 12 })).catch(() => null),
  ]);
  if (my !== homeReq) return;
  pad.innerHTML = "";
  const t = a.totals;
  const ref = el("span","refresh"); ref.id = "refresh-ind";
  ref.append(el("span","live"), document.createTextNode(EVENTS_OK ? "live" : "live · 15s"));
  ref.title = "re-renders when something on disk changes (long-poll), 15s timer as fallback";
  const fams = g.families || [];
  const readyN = fams.filter(f => f.ready).length;
  const since = t.first_seen ? new Date(t.first_seen * 1000).toLocaleDateString([], { month: "short", year: "numeric" }) : null;
  pageHead(pad, "Overview", null, null, [ref]);
  if (t.messages) {
    pageReads(pad, [
      { v: fmt(t.sessions), k: "sessions" },
      { v: fmt(t.projects || (a.top_projects || []).length), k: "projects" },
      { v: since, k: "first run", opt: 2 },
      { v: fmt(t.messages), k: "messages", opt: 1 },
    ], null, "Every number on this page is read off this machine's own transcripts.");
  } else {
    pad.append(el("p","page-d",
      "Nothing has run on this machine yet. Set up a family below, run mantis in a project, and this page fills itself in."));
  }

  // ---- the four readings ----
  // The window is chosen by the data, and the label always says which one it
  // is: a week if you worked this week, a month if you didn't, all time if
  // this machine has been quiet for a month. A dashboard that reports four
  // zeroes because you took a holiday has told you nothing; one that says
  // "7d" over a month of numbers is lying. Naming the window does both jobs.
  const dayOf = k => a.daily[k] || {};
  const sumWin = (n, off, key) => sumOf(dailySeries(a.daily, n + off).slice(0, n).map(d => dayOf(d.date)), key);
  const WIN = sumWin(7, 0, "msgs") ? 7 : sumWin(30, 0, "msgs") ? 30 : 0;
  const wlab = WIN ? " · " + WIN + "d" : " · all time";
  const span = WIN || 30;
  const recent = dailySeries(a.daily, span).map(d => dayOf(d.date));
  const msgsN = WIN ? sumWin(WIN, 0, "msgs") : t.messages;
  const msgsP = WIN ? sumWin(WIN, WIN, "msgs") : 0;
  const toolsN = WIN ? sumWin(WIN, 0, "tools") : t.tool_calls;
  const toolsP = WIN ? sumWin(WIN, WIN, "tools") : 0;
  const streak = calcStreak(a.daily);
  const lastSeen = t.last_seen ? ago(t.last_seen) : null;
  const quiet = WIN === 0 && lastSeen ? "quiet for a month · last run " + lastSeen : null;
  const tiles = el("div","tiles"); tiles.id = "ov-tiles";
  tile(tiles, "msg", "Messages" + wlab, fmt(msgsN), null,
    quiet || (WIN ? fmt(t.messages) + " all time · " + t.avg_msgs_per_session + " per session"
                  : t.avg_msgs_per_session + " per session across " + fmt(t.sessions) + " sessions"),
    sparkLine(recent.map(d => d.msgs || 0)), deltaEl(msgsN, msgsP, WIN + " days"));
  tile(tiles, "tool", "Tool calls" + wlab, fmt(toolsN), null,
    t.unique_tools + " distinct tools · " + fmt(t.tool_calls) + " all time",
    sparkLine(recent.map(d => d.tools || 0)), deltaEl(toolsN, toolsP, WIN + " days"));
  if (sp) {
    const days = sp.days || [];
    const cut = days.slice(-span), prevCut = days.slice(-2 * span, -span);
    const usd = ds => ds.reduce((x, d) => x + (d.est_usd || 0) + (d.rec_usd || 0), 0);
    const tok = d => (d.est_in || 0) + (d.est_out || 0) + (d.rec_in || 0) + (d.rec_out || 0);
    const priced = (sp.pricing || {}).known;
    const toks = cut.reduce((x, d) => x + tok(d), 0);
    tile(tiles, "spend", "Spend" + (WIN ? wlab : " · " + span + "d"), priced ? "≈" + fmtUsd(usd(cut)) : fmtTok(toks),
      priced ? null : " tok",
      priced ? fmtTok(toks) + " tokens at " + ((sp.pricing || {}).model || "current") + " rates"
             : "no price row for this model — tokens only",
      sparkLine(cut.map(tok)), priced ? deltaEl(usd(cut), usd(prevCut), span + " days") : null);
    if (!SPEND_TOUCHED) SPEND_WIN = WIN === 7 ? 7 : 30;
  }
  tile(tiles, "streak", streak ? "Streak" : "Active days", streak ? String(streak) : String(t.active_days),
    streak ? (streak === 1 ? " day" : " days") : " days",
    t.busiest_day ? "busiest " + t.busiest_day + " · " + fmt(t.busiest_day_msgs) + " messages" : fmt(t.active_days) + " days with work",
    dayBlocks(dailySeries(a.daily, span).map(d => (dayOf(d.date).msgs || 0) > 0 ? 1 : 0)));
  pad.append(tiles);

  // ---- instruments left, standings right ----
  const ov = el("div","ov");
  const L = el("div","ov-c"), R = el("div","ov-c");
  ov.append(L, R); pad.append(ov);

  // THE TRACE — six months of daily volume, with the read-out under it
  if (t.messages) {
    const box = el("div","trace");
    const pk = el("span","cn");
    cardHead(box, "trace", "Trace", "last 26 weeks", [pk]);
    const svgWrap = el("div"); box.append(svgWrap);
    const info = traceSVG(dailySeries(a.daily, 182), svgWrap);
    const series = dailySeries(a.daily, 182);
    pk.innerHTML = info.peakVal ? "peak <b>" + fmt(info.peakVal) + "</b> · " + series[info.peakIdx].date : "";
    const lcd = el("div","lcd tight"); lcd.style.margin = "10px 0 0";
    lcdCell(lcd, fmt(t.sessions), "sessions");
    lcdCell(lcd, String(t.avg_msgs_per_session), "msgs / session", "dim");
    lcdCell(lcd, fmt(t.active_days), "active days", "dim");
    lcdCell(lcd, Math.round(t.user_messages / Math.max(1, t.messages) * 100) + "%",
      "you, " + (100 - Math.round(t.user_messages / Math.max(1, t.messages) * 100)) + "% agent", "dim");
    box.append(lcd);
    L.append(box);
  }

  // SPEND — estimated sessions against recorded runs
  if (sp) { const card = el("div","card2"); card.id = "spend-card"; renderSpend(card, sp); L.append(card); }

  // PROJECTS — where the work actually happened
  if ((a.top_projects || []).length) {
    const card = el("div","card2");
    cardHead(card, "folder", "Projects", "by volume");
    const maxM = Math.max(1, ...a.top_projects.map(p => p.msgs));
    const list = el("div","plist");
    a.top_projects.slice(0, 8).forEach(p => {
      const row = el("div","prow");
      const f = el("div","fillbar"); f.style.width = (p.msgs/maxM*100).toFixed(1) + "%"; row.append(f);
      row.append(el("span","pn", p.name));
      row.append(el("span","pp", p.cwd || ""));
      row.append(el("span","pv", fmt(p.msgs) + " msgs · " + p.sessions + " sessions" +
        ((p.in_est || p.out_est) ? " · ≈" + fmtTok((p.in_est||0) + (p.out_est||0)) + " tok" : "")));
      list.append(row);
    });
    card.append(list);
    L.append(card);
  }

  // THE FIVE FAMILIES — the one thing on this page you can act on
  const pcard = el("div","card2");
  cardHead(pcard, "key", "Providers", readyN + "/" + fams.length + " ready",
    [extLink("cn", "~/.mantis-agent/models.json")]);
  const grid = el("div","provs"); grid.id = "fam-grid"; renderFamilies(grid, g); pcard.append(grid);
  const more = el("button","c-more", readyN < fams.length ? "Set up another family →" : "Manage keys and models →");
  more.onclick = () => showTab("models"); pcard.append(more);
  R.append(pcard);

  // WHAT RAN — only if anything has. An empty ledger card teaches nothing the
  // Activity page's own empty state doesn't say better.
  const rows = act ? activityRows(act) : [];
  if (rows.length) {
    const acard = el("div","card2");
    const running = rows.filter(r => statusClass(r.status, r.active) === "run").length;
    cardHead(acard, "activity", "What ran", running ? running + " running now" : "last " + Math.min(6, rows.length));
    rows.slice(0, 6).forEach(r => {
      const row = el("div","arow");
      row.append(el("span","dot2 " + statusClass(r.status, r.active)));
      const n = el("span","an", r.desc); n.title = r.err || r.extra || r.desc; row.append(n);
      row.append(el("span","aa", r.ts ? ago(r.ts) : ""));
      row.onclick = r.open;
      acard.append(row);
    });
    const am = el("button","c-more", "The whole ledger →");
    am.onclick = () => showTab("activity"); acard.append(am);
    R.append(acard);
  }

  if (!t.messages) {
    L.append(emptyState("activity", "Nothing recorded yet",
      "Run mantis in a project and come back — every session is logged locally, and this page turns into " +
      "your trace, your working hours, and the tools the agent actually reaches for."));
    return;
  }

  // WHEN — the joint distribution, because by-hour and by-weekday separately
  // cannot tell you about Sunday nights
  const when = el("div","card2");
  cardHead(when, "clock", "When you work");
  const ph = a.by_hour.indexOf(Math.max(...a.by_hour));
  const pw = a.by_weekday.indexOf(Math.max(...a.by_weekday));
  const n2 = el("div","note2");
  n2.innerHTML = "Busiest at <b>" + ph + ":00</b> on <b>" + WD[pw] + "</b> · one dot per hour, sized by volume";
  when.append(n2);
  const pw2 = el("div"); pw2.innerHTML = punchSVG(a.punchcard || [[]]); when.append(pw2);
  R.append(when);

  // WHAT — the tool spectrum
  const what = el("div","card2");
  cardHead(what, "tool", "What it reaches for");
  const n3 = el("div","note2");
  n3.innerHTML = "<b>" + fmt(t.tool_calls) + "</b> tool calls across <b>" + t.unique_tools + "</b> tools";
  what.append(n3);
  loadHomeSpectrum(what, a.top_tools || [], t.tool_total || t.tool_calls || 1);
  R.append(what);
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
                 provider: "all", org: "all", orgsOpen: false, fresh: false, hfToken: false,
                 source: "curated", mode: "auto", find: null, findJob: null,
                 gpuMax: {}, results: [] };
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
  if (grid) { grid.querySelectorAll(".mcard").forEach(c => { c.dataset.sig = ""; }); paintModels(); }
}
// Which GPU provider the page is scoped to. The hash (#deploy/modal) wins,
// then the last choice in this browser, then — when exactly one provider is
// configured — that one, so the common case needs no clicking at all.
const DEPLOY_PROV_KEY = "mantis-deploy-provider";
const PROV_SHORT = { runpod: "RunPod", hf: "HF", modal: "Modal", deepinfra: "DeepInfra", baseten: "Baseten", vastai: "Vast.ai",
                     "fireworks-dedicated": "Fireworks" };
// the company filter rides in the hash as #deploy/all/<name>, URL-encoded —
// a company's name can have spaces ("Kimi (Moonshot)")
const hashOrg = () => { const raw = location.hash.slice(1).split("/")[2] || "";
  try { return decodeURIComponent(raw); } catch (e) { return raw; } };
function initDeployProvider(configured) {
  // The page is never scoped to one provider any more: the deploy sheet looks
  // across every provider that can deploy and picks the best card. What the
  // hash still carries is the company filter on the model grid.
  DEPLOY.org = hashOrg() || DEPLOY.org || "all";
  DEPLOY.provider = "all";
  try { localStorage.removeItem(DEPLOY_PROV_KEY); } catch (e) { /* private mode */ }
}
function writeDeployHash() {
  const o = DEPLOY.org === "all" ? "" : encodeURIComponent(DEPLOY.org);
  const want = "deploy" + (o ? "/all/" + o : "");
  if (location.hash !== "#" + want) location.hash = want;
}
function openAddKey(pid) {
  const p = DEPLOY.providers.find(x => x.id === pid);
  if (p) openCredSheet(p);
}
const DEP_STATE = { running: "ok", scaled_to_zero: "ok", starting: "run", pending: "run", building: "run",
                    deleting: "run", paused: "pend", failed: "bad", deleted: "", unknown: "" };
// what a weights format means, for the tooltip on the letters
function dtypeWords(dt) {
  const t = String(dt || "").toUpperCase();
  const w = /^BF16$/.test(t) ? "16-bit weights (bfloat16) — full quality"
    : /^F16|FP16$/.test(t) ? "16-bit weights (float16) — full quality"
    : /^F32|FP32$/.test(t) ? "32-bit weights — twice the memory of 16-bit"
    : /^F8|FP8/.test(t) ? "8-bit float weights — about half the memory of 16-bit"
    : /^(U8|I8|INT8)$/.test(t) ? "8-bit integers — usually a quantized model"
    : /^(U32|I32)$/.test(t) ? "packed integers — usually a 4-bit quantized model"
    : /^(U4|I4|INT4|Q4)/.test(t) ? "4-bit quantized weights" : "";
  return (w ? w + " · " : "") + "the format the weights are stored in";
}
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
  const [pv, ls, jr] = await Promise.all([
    api("/api/deploy/providers").catch(e => ({ ok: false, error: e.message, providers: [] })),
    api("/api/deploy/list").catch(e => ({ ok: false, error: e.message, deployments: [] })),
    api("/api/deploy/jobs").catch(() => ({ ok: false, jobs: [] })),
  ]);
  if (my !== deployReq) return;
  DEPLOY.providers = pv.providers || [];
  DEPLOY.deployments = ls.deployments || [];
  DEPLOY.jobs = jr.jobs || [];
  pad.innerHTML = "";
  const configured = DEPLOY.providers.filter(p => p.configured);
  const ready = configured.filter(p => provReady(p));
  pageHead(pad, "Deploy", null, null);
  const reads = el("div"); reads.id = "dp-reads"; pad.append(reads); paintDeployReads();
  if (pv.ok === false) {
    const b = el("div","banner"); const t = el("div","sp");
    t.innerHTML = "<b>Deploy isn't available:</b> " + esc(errText(pv)); b.append(t); pad.append(b);
  }
  // What is running, and what is on its way there — the first thing on the
  // page whenever there is any, and not on the page at all when there isn't.
  const aSec = section(pad, "Active"); aSec.id = "dp-active";
  const cards = el("div","dcards"); cards.id = "dp-cards"; aSec.append(cards);
  const uSec = section(pad, "Usage"); uSec.id = "dp-usage";
  loadUsage();
  // The providers are setup: the page leads with them only while nothing can
  // deploy yet. Once one can, they fold to one line under the models.
  const pSec = el("div","sec dp-prov"); pSec.id = "dp-provsec";
  if (!ready.length) pad.append(pSec);
  initDeployProvider(configured);
  const mSec = el("div","sec"); pad.append(mSec);
  renderDpPicker(mSec);
  if (ready.length) pad.append(pSec);
  renderProvSection(pSec, !ready.length);
  renderActive();
  kickDeployPoll(false);
  // the store's state can be minutes old — ask the providers once, behind the paint
  setTimeout(() => { if (curView === "deploy" && !document.hidden) syncDeploy(true); }, 900);
  // deep link: /?model=<hf id>#deploy opens straight into that model's sheet
  const want = new URLSearchParams(location.search).get("model");
  if (want) openDeploySheet(want);
}
// The providers as one line — which are ready, and the way into setup —
// opening onto the full cards. Open by default only when none is ready.
function renderProvSection(sec, open) {
  sec.innerHTML = "";
  const configured = DEPLOY.providers.filter(p => p.configured);
  const ready = configured.filter(p => provReady(p));
  const sum = el("button","dp-provsum"); sum.type = "button";
  const marks = el("span","dp-provmarks");
  (ready.length ? ready : DEPLOY.providers).slice(0, 6).forEach(p => marks.append(providerMark(p.logo || p.id, p.display_name)));
  const txt = el("span","dp-provtxt");
  if (ready.length)
    txt.append(el("b", null, ready.length + " of " + DEPLOY.providers.length + " GPU providers ready"),
               el("span", null, " · " + ready.map(p => PROV_SHORT[p.id] || p.display_name).join(", ")));
  else if (configured.length)
    txt.append(el("b", null, "Finish setting up " + configured.map(p => PROV_SHORT[p.id] || p.display_name).join(", ")),
               el("span", null, " · " + configured.map(p => reqShort(p).toLowerCase()).join(", ") + " — or connect another provider"));
  else
    txt.append(el("b", null, "Connect a GPU provider"),
               el("span", null, " · paste one API key and any model below is two clicks from running"));
  const x = el("span","dp-provx");
  sum.append(marks, txt, x);
  const box = el("div","dp-grid"); box.id = "dp-grid";
  const set = on => {
    box.style.display = on ? "" : "none";
    x.textContent = on ? "Hide" : (ready.length ? "Manage" : "Set up");
    sum.setAttribute("aria-expanded", on ? "true" : "false");
    if (on && !box.childElementCount) renderDpProviders(box);
  };
  sum.onclick = () => set(box.style.display === "none");
  sec.append(sum, box);
  set(open);
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
  // Fireworks scales to zero, but what it rents you is dedicated GPUs
  const kind = p.id === "vastai" ? "marketplace" : p.id === "fireworks-dedicated" ? "dedicated GPUs"
    : p.scale_to_zero ? "serverless" : "dedicated";
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
  ht.append(el("div","fd", (reqPkg(p) ? "`" + reqPkg(p) + "` goes into the dashboard's own Python environment. " : "")
    + "One click; it takes about a minute."));
  head.append(ht);
  s.append(head);
  const log = el("pre","lg-body in-log"); log.style.display = "none"; s.append(log);
  const out = el("div"); s.append(out);
  const foot = el("div","cs-foot");
  const go = btn("Install now", "pri", async () => {
    go.disabled = true; go.textContent = "Installing…"; out.innerHTML = "";
    log.style.display = ""; log.textContent = "starting…";
    let r;
    try { r = await post("/api/deploy/install", { provider: p.id }); } catch (e) { r = { ok: false, error: e.message }; }
    if (!r.ok) { out.append(probeBox(false, errText(r))); go.disabled = false; go.textContent = "Install now"; return; }
    if (r.already) { toast("✓ " + (p.display_name || p.id) + " is ready"); hideModal(); loadDeploy(); return; }
    const open = () => document.getElementById("modal").className && s.firstChild === head;
    for (;;) {
      await sleep(900);
      if (!open()) return;
      let j;
      try { j = await api("/api/deploy/job?" + q({ id: r.job })); } catch (e) { continue; }
      log.textContent = (j.lines || []).join("\n") || "starting…"; log.scrollTop = log.scrollHeight;
      if (j.status === "running") continue;
      if (j.status === "done") {
        toast("✓ " + (p.display_name || p.id) + " can deploy now");
        const pr = await api("/api/deploy/providers").catch(() => null);
        if (pr) DEPLOY.providers = pr.providers || [];
        hideModal(); loadDeploy(); return;
      }
      out.append(probeBox(false, (j.error || "install failed") + (j.hint ? " — " + j.hint : "")));
      go.disabled = false; go.textContent = "Try again";
      return;
    }
  });
  // the manual route stays one line away, for a mantis installed as a uv tool
  const alt = el("details","ds-adv"); alt.append(el("summary", null, "Or run it yourself"));
  const cmd = reqCmd(p);
  const box = el("div","jsonbox");
  const h2 = el("div","jh");
  h2.append(el("span","jt", "in your shell"));
  const cp = btn("Copy", "gho", () => copyText(cmd, "command")); cp.style.marginLeft = "auto"; h2.append(cp);
  box.append(h2);
  const pre = el("pre"); pre.textContent = cmd; box.append(pre);
  alt.append(box);
  alt.append(el("div","note2", "Installed mantis as a uv tool? Use " +
    "uv tool install --force 'mantis-agent-sdk[" + (reqPkg(p) || "modal") + "]' instead, then re-check."));
  s.append(alt);
  const re = btn("Re-check", "gho", async () => {
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
  foot.append(go, re, btn("Close", "gho", hideModal));
  s.append(foot);
  showModal();
  trapFocus(s.parentElement);
  setTimeout(() => go.focus(), 30);
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
  // You search, THEN you narrow. The search row comes first and carries the
  // sort and the token state with it; the org pills sit underneath, where a
  // filter belongs.
  // WHERE the models come from, stated before you search. The curated list
  // was reading as the whole world — "30 curated" and nothing else on screen
  // — so the sources are now a control you can see and switch.
  const srcRow = el("div","dp-src"); srcRow.setAttribute("role", "tablist");
  srcRow.setAttribute("aria-label", "Model source");
  [["curated", "Curated", "a short starting list — good first deploys"],
   ["hub", "Hugging Face", "search every public model on the Hub"],
   ["ollama", "Ollama", "models already pulled on this machine"]].forEach(([k, lab, why]) => {
    const c = el("button","dp-srcb" + (k === DEPLOY.source ? " on" : ""), lab);
    c.setAttribute("role", "tab");
    c.setAttribute("aria-selected", k === DEPLOY.source ? "true" : "false");
    c.title = why;
    c.onclick = () => {
      if (DEPLOY.source === k) return;
      DEPLOY.source = k; DEPLOY.org = "all"; DEPLOY.orgsOpen = false;
      srcRow.querySelectorAll(".dp-srcb").forEach(x => {
        x.classList.toggle("on", x === c);
        x.setAttribute("aria-selected", x === c ? "true" : "false");
      });
      runSearch();
    };
    srcRow.append(c);
  });
  sec.append(srcRow);

  const bar = el("div","dp-find");
  const find = findBox("Search every model on Hugging Face — llama, qwen, gemma, deepseek…  ( / )");
  find.wrap.style.marginBottom = "0"; find.wrap.style.flex = "1"; find.input.value = DEPLOY.q;
  bar.append(find.wrap);
  // The mode is a visible control, not a guess made behind your back. It
  // shows what WOULD run for what is typed, and clicking it pins the choice.
  const modeBtn = el("button","dp-mode");
  const modeLbl = el("span","dp-model");
  modeBtn.append(el("span","dp-modem"), modeLbl);
  const paintMode = () => {
    const on = askMode(find.input.value);
    modeBtn.classList.toggle("on", on);
    modeLbl.textContent = on ? "Ask the agent" : "Keyword search";
    modeBtn.title = DEPLOY.mode === "auto"
      ? (on ? "This reads as a question — Enter asks the agent. Click to force keyword search."
            : "This reads as keywords — Enter searches the Hub. Click to ask the agent instead.")
      : DEPLOY.mode === "ask" ? "Pinned to Ask. Click to go back to automatic."
      : "Pinned to keyword search. Click to go back to automatic.";
    modeBtn.setAttribute("aria-pressed", on ? "true" : "false");
    modeBtn.classList.toggle("pinned", DEPLOY.mode !== "auto");
  };
  modeBtn.onclick = () => {
    // one click pins the opposite of what is showing; a second returns to auto
    const on = askMode(find.input.value);
    DEPLOY.mode = DEPLOY.mode !== "auto" ? "auto" : (on ? "keyword" : "ask");
    paintMode();
  };
  find.input.addEventListener("input", paintMode);
  paintMode();
  bar.append(modeBtn);
  // Four always-on pills to answer a question most people ask once, and then
  // only to change their mind. The ordering is one choice, so it reads as one
  // labelled control that states it without being opened — the same Sort
  // control the model grid already uses. "Last 30 days" stays a pill: it is a filter
  // that rides ALONGSIDE any ordering, not a fifth way to order.
  const HUB_SORTS = [["trending", "Trending", "what the Hub is pushing"],
                     ["downloads", "Downloads", "most pulled"],
                     ["likes", "Likes", "most liked"],
                     ["recent", "Recent", "last updated"]];
  const hubSort = () => HUB_SORTS.find(([k]) => k === DEPLOY.sort) || HUB_SORTS[0];
  const sortBtn = popMenu(() => hubSort()[1], () => HUB_SORTS.map(([k, lab, hint]) => ({
    label: lab, on: k === DEPLOY.sort, side: hint,
    // an ordering is a question for the whole Hub — the curated list is in
    // the order it was picked — so choosing one goes there, like typing does
    run: () => { DEPLOY.sort = k; sortBtn.repaint(); if (DEPLOY.source === "curated") setSource("hub"); runSearch(); },
  })), { prefix: "Sort by", title: "how the Hub orders these results" });
  sortBtn.id = "dp-sort";
  bar.append(sortBtn);
  const chips = el("div","fchips");
  // a filter, so it is named for what it keeps — "New" read as a create button
  const fresh = el("button","fchip" + (DEPLOY.fresh ? " on" : ""), "Last 30 days");
  fresh.title = "released or updated in the last 30 days";
  fresh.onclick = () => { DEPLOY.fresh = !DEPLOY.fresh; fresh.classList.toggle("on", DEPLOY.fresh); paintModels(); };
  chips.append(fresh);
  bar.append(chips); sec.append(bar);
  // second row: how the results are filtered — the company pills, and the
  // result count when a search is what produced them. Nothing else.
  const sub = el("div","dp-sub");
  const orgRow = el("div","dp-orgs"); orgRow.id = "dp-orgs"; sub.append(orgRow);
  const status = el("div","dp-status"); status.id = "dp-mstatus"; sub.append(status);
  sec.append(sub);
  const grid = el("div","dp-mgrid"); grid.id = "dp-models"; sec.append(grid);
  let t = null;
  // deep link: /?ask=<question>#deploy lands on the agent's answer
  const ask0 = new URLSearchParams(location.search).get("ask");
  if (ask0 && !DEPLOY.q) { find.input.value = ask0; DEPLOY.q = ask0; DEPLOY.mode = "ask"; paintMode();
    setTimeout(() => runAsk(grid, status), 0); }
  const runSearch = async () => {
    DEPLOY.q = find.input.value.trim();
    paintMode();
    // a question goes to the agent; everything else takes the path it always
    // took, untouched
    if (DEPLOY.q && DEPLOY.source !== "ollama" && askMode(DEPLOY.q)) { runAsk(grid, status); return; }
    DEPLOY.find = null;
    // typing always means the Hub: the curated list is a starting point, not
    // a filter you have to escape from
    if (DEPLOY.q && DEPLOY.source === "curated") setSource("hub");
    setStatus(searchingLine());
    const my = ++modelSearchReq;
    let r;
    if (DEPLOY.source === "ollama") {
      try {
        const o = await api("/api/ollama");
        r = { ok: true, source: "ollama", reachable: !!o.reachable, error: o.error,
              models: (o.models || []).map(x => ({
                id: x.name, org: "ollama", local: true, params_b: parseParams(x.param),
                dtype: x.quant, size_b: x.size, loaded: x.loaded, lastModified: x.modified_at })) };
      } catch (e) { r = { ok: false, error: e.message, models: [] }; }
    } else {
      try { r = await api("/api/deploy/models?" + q({ q: DEPLOY.q, sort: DEPLOY.sort, limit: 30,
                                                          source: DEPLOY.source === "curated" ? "curated" : "hub" })); }
      catch (e) { r = { ok: false, error: e.message, models: [] }; }
    }
    if (my !== modelSearchReq) return;
    if (r.hf_token_set != null) DEPLOY.hfToken = !!r.hf_token_set;
    (r.models || []).forEach(m => { if ((r.pending || []).includes(m.id)) m._pending = true; });
    DEPLOY.results = r;
    await gpuCeiling();
    renderOrgPills();
    renderModelRows(grid, r);
    setStatus(resultLine(r));
    if (r.partial) enrichLoop(r, grid, status, my);
  };
  // the status is a sentence about REACH, not a bare number: how many, out of
  // what, and — while a query is running — what is being searched
  const setSource = k => {
    DEPLOY.source = k;
    srcRow.querySelectorAll(".dp-srcb").forEach((x, i) => {
      const on = ["curated", "hub", "ollama"][i] === k;
      x.classList.toggle("on", on); x.setAttribute("aria-selected", on ? "true" : "false");
    });
  };
  const setStatus = txt => { status.textContent = txt; };
  const searchingLine = () => DEPLOY.source === "ollama" ? "reading local models…"
    : DEPLOY.q ? "searching all of Hugging Face…"
    : DEPLOY.source === "hub" ? "loading what's trending on Hugging Face…" : "loading the curated list…";
  const resultLine = r => {
    if (r.ok === false) return "";
    const n = (r.models || []).length;
    if (DEPLOY.source === "ollama")
      return r.reachable === false ? "Ollama is not running on this machine"
        : n + " pulled locally";
    if (DEPLOY.q) return n + " of all Hugging Face, for “" + DEPLOY.q + "”";
    // The curated list needs no caption: the "Curated" tab is already selected
    // above it, and the count is on the tab. Saying it again under the grid was
    // just noise.
    return "";
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
// Ollama reports a parameter size as a string ("8.0B", "70B"); the cards
// carry it as a number, so it is parsed once here rather than at each use.
// ---- the agent path --------------------------------------------------------
// The search is a background job because it may call a model. Its progress
// arrives as the same step treatment the deploy job uses — not a spinner —
// and the results area holds the SHAPE of grouped results while it works, so
// the page does not jump when the answer lands.
let askReq = 0;
async function runAsk(grid, status) {
  const my = ++askReq;
  const mine = ++modelSearchReq;               // cancels any in-flight keyword search
  DEPLOY.find = null;
  status.textContent = "asking…";
  grid.innerHTML = ""; grid.className = "dp-groups";
  const steps = el("ol","dp-steps dp-asksteps");
  const wrap = el("div","dp-asking");
  wrap.append(steps, askSkeleton());
  grid.append(wrap);
  let drawn = 0;
  const paint = (lines, running) => {
    for (let i = drawn; i < lines.length; i++) {
      const li = el("li","dp-step");
      li.append(el("span","dp-stepm"), el("span","dp-stept", lines[i]));
      steps.append(li);
    }
    drawn = lines.length;
    [...steps.children].forEach((li, i) => {
      const live = running && i === lines.length - 1;
      li.classList.toggle("live", live); li.classList.toggle("did", !live);
    });
  };
  paint(["reading your question…"], true);
  let start;
  try { start = await api("/api/deploy/find?" + q({ q: DEPLOY.q, provider: DEPLOY.provider === "all" ? "" : DEPLOY.provider, limit: 24,
                                                    agent: DEPLOY.mode === "keyword" ? 0 : 1 })); }
  catch (e) { start = { ok: false, error: e.message }; }
  if (my !== askReq) return;
  if (!start.ok) { askFailed(grid, status, start); return; }
  DEPLOY.findJob = start.job;
  for (let i = 0; i < 90; i++) {
    await sleep(i < 6 ? 350 : 800);
    if (my !== askReq) return;
    let j;
    try { j = await api("/api/deploy/job?" + q({ id: start.job })); } catch (e) { continue; }
    if (my !== askReq) return;
    paint((j.lines || []).length ? j.lines : ["reading your question…"], j.status === "running");
    if (j.status === "running") continue;
    if (j.status === "error") { askFailed(grid, status, j); return; }
    DEPLOY.find = j.result || null;
    DEPLOY.results = { ok: true, models: askModels(j.result) };
    await gpuCeiling();
    renderOrgPills();
    renderAskResults(grid, j.result);
    status.textContent = askCount(j.result);
    return;
  }
  askFailed(grid, status, { error: "the search took too long" });
}
const askModels = r => (r && r.groups || []).reduce((a, g) => a.concat(g.models || []), []);
function askCount(r) {
  const n = askModels(r).length, g = ((r && r.groups) || []).length;
  if (!n) return "nothing matched";
  return n + " model" + (n === 1 ? "" : "s") + " in " + g + " group" + (g === 1 ? "" : "s");
}
function askFailed(grid, status, r) {
  grid.innerHTML = ""; grid.className = "dp-mgrid";
  status.textContent = "";
  grid.append(emptyState("search", "That search didn't finish",
    errText(r) + " — try plain keywords, or ask again.",
    btn("Search keywords instead", "pri", () => {
      DEPLOY.mode = "keyword";
      const i = document.querySelector("#deploypad .find input"); if (i) { i.focus(); }
      const ev = new Event("keydown"); document.dispatchEvent(ev);
      runSearchAgain();
    })));
}
function runSearchAgain() {
  const i = document.querySelector("#deploypad .find input");
  if (i) { i.dispatchEvent(new Event("input")); i.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter" })); }
}
// The waiting shape: two labelled groups of cards, so the answer lands into
// the space it will occupy instead of pushing the page around.
function askSkeleton() {
  const w = el("div","dp-skel");
  for (let gi = 0; gi < 2; gi++) {
    const h = el("div","dp-gh");
    h.append(el("span","sk sk-name"));
    w.append(h);
    const g = el("div","dp-mgrid");
    for (let i = 0; i < (gi ? 2 : 3); i++) g.append(el("div","sk sk-mcard"));
    w.append(g);
  }
  return w;
}
// The answer: what it understood, what it derived, then the groups. The
// derived filters are chips you can drop, because the fastest way to fix a
// misreading is to remove the bit it got wrong and let it run again.
function renderAskResults(grid, r) {
  grid.innerHTML = ""; grid.className = "dp-groups";
  if (!r) return;
  const head = el("div","ask-head");
  if (r.interpretation) head.append(el("div","ask-int", r.interpretation));
  // WHERE the answer came from, always — a rules answer never poses as an
  // agent one
  const src = el("div","ask-src");
  if (r.source === "agent") {
    src.append(el("span","ask-badge on", "Agent"));
    src.append(el("span","ask-srcx", "interpreted by the model you have connected."));
  } else {
    src.append(el("span","ask-badge", "Rules"));
    src.append(el("span","ask-srcx", "matched by keyword rules — no model answered this."));
    const a = el("button","ask-link", "Connect a model provider");
    a.onclick = () => { location.hash = "#models"; showTab("models"); };
    src.append(a);
  }
  head.append(src);
  (r.notes || []).forEach(n => head.append(el("div","ask-note", n)));
  const fk = Object.keys(r.filters || {}).filter(k => r.filters[k] != null && r.filters[k] !== "");
  if (fk.length) {
    const fr = el("div","ask-filters");
    fr.append(el("span","ask-flbl", "reading that as"));
    fk.forEach(k => {
      const c = el("span","ask-chip");
      c.append(el("b", null, k.replace(/_/g, " ")), el("span", null, fmtFilter(r.filters[k])));
      const x = el("button","ask-x", "×");
      x.title = "drop this and ask again";
      x.setAttribute("aria-label", "Remove filter " + k);
      x.onclick = () => {
        const next = Object.assign({}, r.filters); delete next[k];
        DEPLOY.q = (DEPLOY.q + " (without " + k.replace(/_/g, " ") + ")").trim();
        const i = document.querySelector("#deploypad .find input");
        if (i) i.value = DEPLOY.q;
        runSearchAgain();
      };
      c.append(x);
      fr.append(c);
    });
    head.append(fr);
  }
  grid.append(head);
  const cols = r.columns || [];
  (r.groups || []).forEach(g => {
    const gh = el("div","dp-gh");
    gh.append(el("b","dp-gt", g.title || "Results"));
    if (g.reason) gh.append(el("span","dp-gr", g.reason));
    gh.append(el("span","dp-gn", String((g.models || []).length)));
    grid.append(gh);
    const box = el("div","dp-mgrid");
    (g.models || []).forEach(m => box.append(askCard(m, cols, g.best === m.id)));
    grid.append(box);
  });
}
function fmtFilter(v) {
  if (Array.isArray(v)) return v.join(", ");
  if (v && typeof v === "object") return Object.keys(v).map(k => k + " " + v[k]).join(" · ");
  return String(v);
}
// One card, showing only the facts this QUERY cares about. The standout in a
// group is marked once, on the card, and never repeated in the group header.
function askCard(m, cols, best) {
  const card = el("div","mcard ask-card" + (m.id === DEPLOY.model ? " on" : "") + (best ? " best" : ""));
  card.dataset.model = m.id;
  const slash = m.id.indexOf("/");
  const mh = el("div","mh");
  mh.append(orgMark(m.org || (slash > 0 ? m.id.slice(0, slash) : "")));
  const mtt = el("div","mtt");
  const t = el("div","mt", slash > 0 ? m.id.slice(slash + 1) : m.id); t.title = m.id; mtt.append(t);
  if (slash > 0) { const og = m.id.slice(0, slash); const o2 = el("div","mo", orgName(og));
    if (orgName(og) !== og) o2.title = og; mtt.append(o2); }
  mh.append(mtt);
  if (best) mh.append(el("span","ask-best", "pick"));
  card.append(mh);
  const lcd = el("div","lcd tight");
  let any = false;
  cols.forEach(c => {
    const v = colValue(m, c);
    if (v == null) return;
    any = true;
    lcdCell(lcd, v, COL_LABEL[c] || c, c === "vram" ? "hot" : "dim");
  });
  if (any) card.append(lcd);
  card.onclick = () => openDeploySheet(m.id);
  return card;
}
// ---- dual-mode search ------------------------------------------------------
// A keyword goes to the Hub the way it always has. A QUESTION goes to the
// agent, which answers with grouped models and the columns that matter. The
// guess is never silent: the mode is shown as a control you can flip before
// you press Enter, so nobody is surprised by which one ran.
const ASK_WORDS = /\b(what|which|who|whats|what's|why|how|can|should|find|show|recommend|suggest|best|cheapest|fastest|smallest|biggest|smarter|better|good|great|need|want|looking|help|compare|vs|versus|instead|under|over|less|more|fits?|run)\b/i;
const ASK_UNITS = /\b\d+(\.\d+)?\s?(gb|gib|tb|b|m|k)\b|\bparams?\b|\bvram\b|\bcontext\b|\btokens?\b/i;
// A question is a sentence, not a token: several words, or a word that asks
// something, or a comparative, or a real unit.
function looksLikeQuestion(q) {
  const t = String(q || "").trim();
  if (!t) return false;
  if (/^[\w.\-]+\/[\w.\-]+$/.test(t)) return false;      // a bare repo id is never a question
  const words = t.split(/\s+/).filter(Boolean);
  if (t.endsWith("?")) return true;
  if (words.length >= 5) return true;
  return words.length >= 3 && (ASK_WORDS.test(t) || ASK_UNITS.test(t));
}
// "auto" follows the guess; "ask" and "keyword" are the user overriding it.
function askMode(q) {
  if (DEPLOY.mode === "ask") return true;
  if (DEPLOY.mode === "keyword") return false;
  return looksLikeQuestion(q);
}
// Which facts a card shows is decided by the query, not by the card. Nine
// columns on every card is how a result grid stops being readable.
const COL_LABEL = { params: "params", dtype: "weights", vram: "GPU memory", license: "license",
                    downloads: "downloads", updated: "updated", context: "context",
                    fit: "fits", price: "$/h" };
function colValue(m, c) {
  if (c === "params") return m.params_b != null ? fmtParams(m.params_b) : null;
  if (c === "dtype") return m.dtype || null;
  if (c === "vram") return m.est_vram_gb != null ? fmtGb(m.est_vram_gb) : null;
  if (c === "license") return m.license || null;
  if (c === "downloads") return m.downloads != null ? fmtTok(m.downloads) : null;
  if (c === "updated") return whenText(m) || null;
  if (c === "context") return m.context_len ? fmtCtx(m.context_len) : null;
  if (c === "fit") return m.vllm_ok === true ? "vllm ok" : m.vllm_ok === false ? "no vllm" : null;
  if (c === "price") return m.price_per_hour != null ? fmtUsd(m.price_per_hour) + "/h" : null;
  return null;
}
function parseParams(v) {
  const m = /^\s*([\d.]+)\s*([BbMm])/.exec(String(v || ""));
  if (!m) return null;
  const n = parseFloat(m[1]);
  return isNaN(n) ? null : (m[2].toLowerCase() === "m" ? n / 1000 : n);
}
// A company is its NAME, not its Hub namespace: openai and openai-community
// are both OpenAI, and two "OpenAI" pills side by side read as a bug. The pill
// and the filter both key on the display name; the first namespace seen
// supplies the mark.
const orgKey = slug => orgName(String(slug || "").toLowerCase()).toLowerCase();
function renderOrgPills() {
  const row = document.getElementById("dp-orgs"); if (!row) return;
  const models = (DEPLOY.results && DEPLOY.results.models) || [];
  const counts = {}, slugOf = {};
  models.forEach(m => {
    const slug = (m.org || _orgOf(m.id) || "").toLowerCase(); if (!slug) return;
    const o = orgKey(slug);
    counts[o] = (counts[o] || 0) + 1;
    if (!slugOf[o]) slugOf[o] = slug;
  });
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
  // A dozen-plus pills on one scrolling line is a strip nobody reads. Show
  // the busiest few — plus whichever one is selected, so the active filter is
  // never hidden behind "More" — and put the rest behind one overflow.
  const TOP = 6;
  let head = orgs.slice(0, TOP);
  if (DEPLOY.org !== "all" && orgs.includes(DEPLOY.org) && !head.includes(DEPLOY.org))
    head = head.slice(0, TOP - 1).concat(DEPLOY.org);
  head.forEach(o => add(o, orgName(slugOf[o]), orgMark(slugOf[o]), counts[o]));
  const rest = orgs.filter(o => !head.includes(o));
  if (!rest.length) return;
  if (DEPLOY.orgsOpen) { rest.forEach(o => add(o, orgName(slugOf[o]), orgMark(slugOf[o]), counts[o])); }
  const more = el("button","fchip dp-more",
    DEPLOY.orgsOpen ? "Fewer" : "+" + rest.length + " more");
  more.setAttribute("aria-expanded", DEPLOY.orgsOpen ? "true" : "false");
  more.onclick = () => { DEPLOY.orgsOpen = !DEPLOY.orgsOpen; renderOrgPills(); };
  row.append(more);
}
function modelShown(m) {
  if (DEPLOY.org !== "all" && orgKey(m.org || _orgOf(m.id)) !== DEPLOY.org) return false;
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
    // Ollama answers with no query at all — "No model matches “undefined”"
    // was that absence printed. It gets its own words.
    if (DEPLOY.source === "ollama")
      grid.append(emptyState("search", r.reachable === false ? "Ollama isn't running here" : "No models pulled yet",
        r.reachable === false ? "Start it with `ollama serve`, or pick Curated or Hugging Face above."
                              : "Pull one with `ollama pull <name>` and it shows up here."));
    else grid.append(emptyState("search", r.curated ? "Nothing curated yet" : "No model matches “" + (r.query || DEPLOY.q || "") + "”",
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
  shown.forEach(m => items.push(m));
  patchList(grid, items, m => m.id, m => [m.params_b, m.dtype, m.license, m.gated, m.gated_kind, DEPLOY.hfToken,
                                          m.vllm_ok, m.est_vram_gb, m.reason, m.downloads, m._pending, m.last_modified,
                                          DEPLOY.gpuMax[DEPLOY.provider] || 0], (card, m) => {
    card = card || el("div"); card.innerHTML = "";
    card.className = "mcard" + (m.id === DEPLOY.model ? " on" : ""); card.dataset.model = m.id;
    const slash = m.id.indexOf("/");
    const mh = el("div","mh");
    mh.append(orgMark(m.org || (slash > 0 ? m.id.slice(0, slash) : "")));
    const mtt = el("div","mtt");
    const t = el("div","mt", slash > 0 ? m.id.slice(slash + 1) : m.id); t.title = m.id; mtt.append(t);
    // the card's second line is the company, so it takes the real name too —
    // and keeps the slug as its tooltip, since that is what the Hub URL says
    if (slash > 0) { const og = m.id.slice(0, slash); const o2 = el("div","mo", orgName(og));
      if (orgName(og) !== og) o2.title = og; mtt.append(o2); }
    mh.append(mtt); card.append(mh);
    const pend = !!(m._pending && m.params_b == null && m.vllm_ok == null);
    // THE READINGS. How big it is, what it costs you in hardware, how many
    // other people run it — the three questions asked of an open model, in
    // the order they get asked, in the same place on every card.
    const stats = el("div","mstats");
    stats.append(stat(m.params_b != null ? fmtParams(m.params_b) : pend ? "…" : "—",
                      "params", m.params_b == null ? "mute" : ""));
    // VRAM is measured against the biggest card the chosen provider rents, so
    // the verdict — and the colour — is about what you can actually book.
    const ceil = DEPLOY.gpuMax[DEPLOY.provider] || (DEPLOY.provider === "all"
      ? Math.max(1024, ...Object.values(DEPLOY.gpuMax).concat([0])) : 1024);
    let vcls = "";
    const vs = stat(m.est_vram_gb != null ? fmtGb(m.est_vram_gb) : pend ? "…" : "—", "memory", "mute");
    if (m.est_vram_gb != null) {
      const frac = m.est_vram_gb / ceil;
      vcls = frac > 1 ? "red" : frac > 0.85 ? "amb" : "";
      vs.className = "mstat" + (vcls ? " " + vcls : "");
      vs.title = fmtGb(m.est_vram_gb) + " of " + fmtGb(ceil) + " available"
               + (frac > 1 ? " — larger than anything on offer" : frac > 0.85 ? " — tight" : "");
    }
    stats.append(vs);
    const dl = stat(m.downloads != null ? fmtTok(m.downloads) : "—", "pulls", m.downloads == null ? "mute" : "");
    dl.title = "downloads from the Hugging Face Hub";
    stats.append(dl);
    card.append(stats);
    // No line of small print under the numbers. Precision and licence are the
    // card's tooltip; the date sits beside the name. A line appears only when
    // something is WRONG — vLLM can't serve it, or it won't fit the GPUs on
    // offer — because that is the one thing worth reading before you click.
    card.title = [m.dtype && (m.dtype + " — " + dtypeWords(m.dtype)), m.license && ("license " + m.license)]
      .filter(Boolean).join("\n");
    const when = whenText(m.last_modified);
    if (when) {
      // always a short month and year here — "10 months ago" crowds the name
      const t = Date.parse(m.last_modified);
      const short = isNaN(t) ? when.replace(/^updated /, "")
        : new Date(t).toLocaleDateString([], { month: "short", year: "numeric" });
      const w = el("span","mwhen" + (isFresh(m) ? " fresh" : ""), short);
      w.title = when; mh.append(w);
    }
    const warn = el("div","mwarn");
    if (m.vllm_ok === false) { const v = el("span","bad", "vLLM can't serve it"); v.title = m.reason || ""; warn.append(v); }
    if (vcls) warn.append(el("span", vcls === "red" ? "bad" : "amb", vcls === "red" ? "Won't fit any GPU on offer" : "Tight fit"));
    if (warn.childElementCount) card.append(warn);
    const foot = el("div","mfoot");
    const gc = gatedChip(m); if (gc) foot.append(gc);
    const sel = m.id === DEPLOY.model;
    const go = el("button","mbtn" + (sel ? " on" : ""), sel ? "Selected ✓" : "Deploy");
    go.title = sel ? "this is the model below" : "pick a GPU for it and deploy";
    foot.append(go);
    card.append(foot);
    card.onclick = () => openDeploySheet(m.id);
    return card;
  });
}
// ==========================================================================
// DEPLOY — two clicks from a model to a running endpoint, and everything that
// happens after it on one surface.
//
// The old flow was three "Deploy" buttons deep: the card's button selected
// the model and scrolled 2,000px to a Fit section, which listed every GPU on
// every provider (72 of them for an 8B model), each with its own Deploy that
// opened a confirm sheet that Enter would submit — and once it was running,
// the progress lived in a modal that closing or reloading threw away.
//
// Now: the card's Deploy opens ONE sheet with the best-value GPU already
// chosen (the server picks it: cheapest card that fits and is in stock) and
// a button that says what it costs. Press it and the sheet gets out of the
// way; the deploy becomes a card at the top of the page with its stage, a
// running clock, the provider's latest line and — the moment something is
// billable — Logs and Stop. The card is the server's job, not the page's, so
// a reload, another tab or a closed laptop lid never loses it.
// ==========================================================================
const _orgOf = id => { const i = String(id || "").indexOf("/"); return i > 0 ? String(id).slice(0, i) : ""; };
const shortId = id => { const s = String(id || ""), i = s.indexOf("/"); return i > 0 ? s.slice(i + 1) : s; };
const gpuName = g => !g ? "" : (g.family && g.family !== "other" ? g.family : (g.display || g.label || g.provider_id))
  + (g.count > 1 ? " ×" + g.count : "");
const provName = pid => { const p = DEPLOY.providers.find(x => x.id === pid); return (p && p.display_name) || pid || ""; };
const depRate = d => { const c = (d && d.cost) || {};
  return c.per_hour_usd != null ? c.per_hour_usd : (d && d.gpu && d.gpu.price_per_hour != null ? d.gpu.price_per_hour : null); };
const fmtClock = s => { s = Math.max(0, Math.floor(s || 0)); const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(s % 60).padStart(2, "0"); };
const STAGES = [["prepare", "Check"], ["create", "Create"], ["boot", "Boot"], ["ready", "Ready"]];
const STAGE_TEXT = { prepare: "Checking the model and the GPU", create: "Creating the endpoint",
                     boot: "Booting — pulling weights onto the GPU", ready: "Ready" };

// ---- the deploy sheet ------------------------------------------------------
async function openDeploySheet(id) {
  DEPLOY.model = id;
  const s = document.getElementById("sheet"); s.innerHTML = "";
  const head = el("div","ds-head");
  head.append(orgMark(_orgOf(id)));
  const ht = el("div","ds-ht");
  const nm = el("div","ds-name", shortId(id)); nm.title = id; ht.append(nm);
  if (_orgOf(id)) ht.append(el("div","ds-org", orgName(_orgOf(id))));
  head.append(ht);
  s.append(head);
  const body = el("div","ds-body"); s.append(body);
  // the waiting state is the shape of the answer: three readings, three rows
  const sk = el("div","ds-skel");
  const f0 = el("div","mstats ds-facts");
  for (let i = 0; i < 3; i++) { const c = el("div","mstat"); c.append(el("i","sk sk-num"), el("b","sk sk-lbl")); f0.append(c); }
  sk.append(f0, el("div","ds-lbl", "Finding the best GPU for it…"));
  for (let i = 0; i < 3; i++) sk.append(el("div","sk ds-skrow"));
  body.append(sk);
  showModal();
  let r;
  try { r = await api("/api/deploy/inspect?" + q({ model: id })); } catch (e) { r = { ok: false, error: e.message }; }
  // closed, or another model opened while this one was being inspected
  if (DEPLOY.model !== id || !document.getElementById("modal").className || s.firstChild !== head) return;
  DEPLOY.inspect = r;
  paintDeploySheet(body, id, r);
}
function paintDeploySheet(body, id, r) {
  body.innerHTML = "";
  if (r.ok === false) {
    body.append(el("div","ds-err", "Couldn't read " + id + " from the Hub — " + errText(r)));
    const f = el("div","ds-foot"); f.append(btn("Close", "gho", hideModal), btn("Try again", "pri", () => openDeploySheet(id)));
    body.append(f); return;
  }
  const m = r.model || {};
  if (r.hf_token_set != null) DEPLOY.hfToken = !!r.hf_token_set;
  const facts = el("div","mstats ds-facts");
  facts.append(stat(fmtParams(m.params_b), "params"),
               stat(m.est_vram_gb != null ? fmtGb(m.est_vram_gb) : "—", "GPU memory"),
               stat(m.context_len ? fmtCtx(m.context_len) : "—", "context"));
  body.append(facts);
  if (m.vllm_ok === false)
    body.append(el("div","ds-caution", "vLLM doesn't list this architecture (" + (m.reason || "unsupported") + ") — the deploy may fail."));
  // A big model's first start is mostly a download, and the GPU bills while
  // it happens. Say so before the button, in minutes and dollars, not after.
  if (m.est_vram_gb != null && m.est_vram_gb >= 60) {
    const gb = Math.round(m.est_vram_gb * 0.85);           // weights are most of the estimate
    const lo = Math.max(3, Math.round(gb / 25)), hi = Math.max(lo + 2, Math.round(gb / 10));
    const cold = el("div","ds-cold"); cold.id = "ds-cold";
    cold.append(el("b", null, "First start: ~" + gb + " GB of weights to pull onto the GPU"),
                el("span", null, " — expect " + lo + "–" + hi + " minutes before it answers, billed at the GPU rate. "
                  + "Every cold start after scaling to zero pays that again."));
    body.append(cold);
  }

  // Where it can run. Only providers that can actually deploy offer a GPU,
  // only cards that hold it and are in stock, and only the shardings vLLM
  // can use (1, 2, 4, 8) — a T4 ×7 is a price, not an option.
  const runnable = (r.fits || []).filter(f => provReady(DEPLOY.providers.find(x => x.id === f.provider)));
  const opts = [];
  runnable.forEach(f => (f.gpus || []).forEach(g => {
    if (g.verdict === "no" || g.available === false) return;
    if (g.count > 1 && ![2, 4, 8].includes(g.count)) return;
    opts.push({ f, g });
  }));
  const isRec = o => !!(r.recommended && o.f.provider === r.recommended.provider && o.g.provider_id === r.recommended.gpu);
  const price = o => o.g.price_per_hour == null ? 1e9 : o.g.price_per_hour;
  opts.sort((a, b) => (isRec(b) - isRec(a)) || ((a.g.verdict === "fits" ? 0 : 1) - (b.g.verdict === "fits" ? 0 : 1))
    || (price(a) - price(b)) || ((b.g.total_vram_gb || 0) - (a.g.total_vram_gb || 0)));
  if (!opts.length) { body.append(noGpuPanel(m, r, runnable)); return; }

  let sel = opts[0];
  const sec = el("div","ds-sec");
  sec.append(el("div","ds-lbl", "Runs on"));
  const list = el("div","ds-gpus"); sec.append(list);
  body.append(sec);
  const SHOW = 3; let all = false;
  const paintList = () => {
    list.innerHTML = "";
    let shown = all ? opts : opts.slice(0, SHOW);
    if (!shown.includes(sel)) shown = shown.concat([sel]);
    shown.forEach(o => {
      const p = DEPLOY.providers.find(x => x.id === o.f.provider) || {};
      const row = el("button","ds-gpu" + (o === sel ? " on" : "")); row.type = "button";
      row.setAttribute("aria-pressed", o === sel ? "true" : "false");
      row.append(el("span","ds-radio"), providerMark(p.logo || o.f.provider, o.f.display_name));
      const t = el("span","ds-gt");
      t.append(el("b", null, gpuName(o.g)), el("span", null, (o.f.display_name || o.f.provider) + " · " + fmtGb(o.g.total_vram_gb)));
      row.append(t);
      if (isRec(o)) row.append(el("span","ds-best", "best value"));
      if (o.g.verdict === "tight") { const v = el("span","ds-tight", "tight fit"); v.title = o.g.reason || "under 15% headroom"; row.append(v); }
      row.append(el("span","ds-price", o.g.price_per_hour == null ? "—" : fmtRate(o.g.price_per_hour)));
      row.onclick = () => { sel = o; paintList(); paintGo(); };
      list.append(row);
    });
    if (opts.length > SHOW) {
      const more = el("button","ds-more", all ? "Fewer" : "Show " + (opts.length - SHOW) + " more"); more.type = "button";
      more.onclick = () => { all = !all; paintList(); };
      list.append(more);
    }
  };

  // a gated repo: the one place the token is asked for, right where it blocks
  let gate = null;
  if (gatedBlocked(m)) {
    gate = el("div","ds-gate");
    gate.append(el("b", null, "This model is gated"));
    gate.append(el("div","ds-gsub", m.gated_kind === "manual"
      ? "The owner approves access by hand. Request it on the model page, then paste a Hugging Face token."
      : "Click Agree on the model page while signed in — access is instant — then paste a read token."));
    gate.append(extLink("a-link", "Open the model page ↗", "https://huggingface.co/" + m.id));
    gate.append(hfTokenForm(() => { gate.remove(); gate = null; refreshGating(); paintGo(); }));
    body.append(gate);
  }

  // the rest is optional, and labelled in words
  const adv = el("details","ds-adv"); adv.append(el("summary", null, "Options"));
  const ag = el("div","ds-advgrid");
  const field = (label, node) => { const w = el("label","ds-field"); w.append(el("span","ds-fl", label), node); ag.append(w); };
  const ctxI = input(m.context_len ? "model default (" + fmtCtx(m.context_len) + ")" : "model default");
  ctxI.type = "number"; ctxI.min = "512"; field("Max context (tokens)", ctxI);
  const engS = el("select","in"); field("Serving engine", engS);
  const trc = document.createElement("input"); trc.type = "checkbox";
  const tl = el("label","chk ds-chk"); tl.append(trc, document.createTextNode("Trust remote code"));
  tl.title = "only for models that ship their own Python code"; ag.append(tl);
  adv.append(ag);
  body.append(adv);

  // Opt-in, never assumed: switching the model mantis uses is a change to
  // the whole tool, and a deploy can come up and still not answer. When it is
  // ticked, the server only switches after the model answers a real prompt.
  const useC = document.createElement("input"); useC.type = "checkbox"; useC.checked = false;
  const ul = el("label","ds-use"); ul.append(useC, el("span", null, "Switch mantis to it once it answers a test prompt"));
  body.append(ul);
  const note = el("div","ds-note"); body.append(note);

  const foot = el("div","ds-foot");
  const go = btn("Deploy", "pri", () => doDeploy()); go.classList.add("ds-go");
  foot.append(btn("Cancel", "gho", hideModal), go);
  body.append(foot);

  const paintEngines = () => {
    const cur = engS.value;
    engS.innerHTML = "";
    (sel.f.engines && sel.f.engines.length ? sel.f.engines : ["vllm"]).forEach(e => { const o = el("option", null, e); o.value = e; engS.append(o); });
    if ([...engS.options].some(o => o.value === cur)) engS.value = cur;
  };
  const paintGo = () => {
    const rate = sel.g.price_per_hour;
    go.textContent = "Deploy" + (rate != null ? " · " + fmtRate(rate) : "");
    go.disabled = !!gate;
    go.title = gate ? "save a Hugging Face token first" : "";
    note.innerHTML = "";
    note.classList.toggle("warn", !sel.f.scale_to_zero);
    note.append(el("span", null, sel.f.scale_to_zero
      ? "Scales to zero when idle — you pay only while it serves; the first request after a pause waits for a cold start."
      : "Always on — it bills every hour until you stop it."));
    if (sel.f.public_by_default) note.append(el("span", null, " Its URL is reachable by anyone who has it."));
    paintEngines();
  };
  async function doDeploy() {
    if (go.disabled) return;
    go.disabled = true; go.textContent = "Starting…";
    const o = {};
    if (ctxI.value) o.max_model_len = ctxI.value;
    if (trc.checked) o.trust_remote_code = true;
    const engine = engS.value || "vllm";
    const display = { gpu_label: gpuName(sel.g) + " " + fmtGb(sel.g.total_vram_gb), price_per_hour: sel.g.price_per_hour,
                      provider_name: sel.f.display_name || sel.f.provider };
    let res;
    try {
      res = await post("/api/deploy/up", { provider: sel.f.provider, model: m.id, gpu: sel.g.provider_id, engine,
                                           opts: o, use_when_ready: useC.checked, display });
    } catch (e) { res = { ok: false, error: e.message }; }
    if (!res.ok) { toast(errText(res), true); paintGo(); return; }
    hideModal();
    trackJob({ id: res.job, kind: "deploy", status: "running", stage: "prepare", started_at: Date.now() / 1000,
               meta: { provider: sel.f.provider, model: m.id, gpu: sel.g.provider_id, engine,
                       use_when_ready: useC.checked, ...display } });
    toast(res.existing ? shortId(m.id) + " is already deploying" : "Deploying " + shortId(m.id));
    const sc = document.querySelector("#deploy .scroll"); if (sc) sc.scrollTo({ top: 0, behavior: "smooth" });
  }
  paintList(); paintGo();
  setTimeout(() => go.focus(), 30);
}
// Nothing to offer: say which of the three reasons it is, and the one action
// that fixes it — never a bare "nothing fits".
function noGpuPanel(m, r, runnable) {
  const w = el("div","ds-none");
  const ready = DEPLOY.providers.filter(p => p.configured && provReady(p));
  if (!ready.length) {
    w.append(el("b", null, "Connect a GPU provider to deploy it"));
    w.append(el("div","ds-gsub", "Paste one API key — prices and the best GPU for this model show up right here."));
    const rows = el("div","ds-provs");
    // the one you have already started setting up is the shortest way there
    const order = DEPLOY.providers.slice().sort((a, b) => (b.configured ? 1 : 0) - (a.configured ? 1 : 0));
    order.forEach(p => {
      const row = el("div","ds-prov");
      row.append(providerMark(p.logo || p.id, p.display_name));
      const t = el("span","ds-gt"); t.append(el("b", null, p.display_name || p.id), el("span", null, providerDescriptor(p))); row.append(t);
      if (p.configured && !provReady(p)) row.append(btn("Install", "pri", () => openInstallSheet(p)));
      else row.append(btn(p.configured ? "Check key" : "Add key", p.configured ? "" : "pri", () => openAddKey(p.id)));
      rows.append(row);
    });
    w.append(rows);
    return w;
  }
  const errs = (r.fits || []).filter(f => f.error);
  if (!runnable.length || errs.length === (r.fits || []).length) {
    w.append(el("b", null, "Couldn't get GPU prices"));
    w.append(el("div","ds-gsub", errs.map(f => (f.display_name || f.provider) + ": " + f.error).join(" · ") || "The providers didn't answer."));
    const f = el("div","ds-foot"); f.append(btn("Try again", "pri", () => openDeploySheet(m.id))); w.append(f);
    return w;
  }
  const biggest = Math.max(0, ...runnable.flatMap(f => (f.gpus || []).map(g => g.total_vram_gb || 0)));
  w.append(el("b", null, "Nothing on offer can hold it"));
  w.append(el("div","ds-gsub", "It needs about " + fmtGb(m.est_vram_gb) + "; the largest card your providers rent right now has "
    + fmtGb(biggest) + ". A quantized version of the model, or another provider, would fit."));
  return w;
}

// ---- jobs and the Active cards ---------------------------------------------
const DEP_DISMISS_KEY = "mantis-deploy-dismissed";
function dismissedJobs() {
  try { return new Set(JSON.parse(sessionStorage.getItem(DEP_DISMISS_KEY) || "[]")); } catch (e) { return new Set(); }
}
function dismissJob(id) {
  const s = dismissedJobs(); s.add(id);
  try { sessionStorage.setItem(DEP_DISMISS_KEY, JSON.stringify([...s])); } catch (e) { /* private mode */ }
  renderActive();
}
function trackJob(j) {
  DEPLOY.jobs = [j].concat((DEPLOY.jobs || []).filter(x => x.id !== j.id));
  renderActive(); paintDeployReads(); kickDeployPoll(true);
}
// One card per deployment, with whatever job is acting on it folded in; a
// deploy that has not created anything yet is a card of its own.
function activeItems() {
  const gone = dismissedJobs();
  const items = [], byDep = new Map();
  (DEPLOY.deployments || []).forEach(d => {
    if (d.status === "deleted") return;
    const it = { key: d.id, d, job: null, down: null, conn: null };
    byDep.set(d.id, it); items.push(it);
  });
  (DEPLOY.jobs || []).forEach(j => {
    if (gone.has(j.id)) return;
    const it = j.deployment_id ? byDep.get(j.deployment_id) : null;
    if (j.kind === "teardown") { if (it && j.status === "running") it.down = j; return; }
    if (j.kind === "connect") { if (it && (j.status === "running" || (j.status === "error" && !it.conn))) it.conn = j; return; }
    if (j.kind !== "deploy") return;
    if (j.status === "cancelled") return;          // asked to stop, and stopped
    if (it) { if (!it.job || j.status === "running") it.job = j; return; }
    if (j.status === "done") return;
    items.push({ key: "job:" + j.id, d: null, job: j, down: null });
  });
  // what needs you first: a failure, then anything moving, then what's up
  const rank = it => it.job && it.job.status === "error" ? 0
    : (it.job && it.job.status === "running") || it.down || (it.conn && it.conn.status === "running") ? 1 : it.d && it.d.is_live ? 2 : 3;
  const when = it => (it.job && it.job.started_at) || (it.d && it.d.created_at) || 0;
  items.sort((a, b) => rank(a) - rank(b) || when(b) - when(a));
  return items;
}
function renderActive() {
  const sec = document.getElementById("dp-active"); if (!sec) return;
  const box = document.getElementById("dp-cards");
  const items = activeItems();
  sec.style.display = items.length ? "" : "none";
  patchList(box, items, it => it.key, it => {
    const d = it.d || {}, j = it.job || {}, dn = it.down || {};
    const cn = it.conn || {};
    return [d.status, d.endpoint_url, d.in_use, d.is_live, JSON.stringify(d.cost || null), d.message,
            j.status, j.stage, j.deployment_id, j.last_line, j.error, j.connected, j.connect_error, dn.status,
            cn.status, cn.last_line, cn.error, cn.id];
  }, (card, it) => depCard(card, it));
  paintSubs();
}
// ---- usage ------------------------------------------------------------
// What each deployment did: throughput, requests, boot vs serving, and what
// the GPU time cost — from the container's own log, which the provider serves
// without touching the endpoint. A chart that scraped the endpoint would wake
// a scaled-to-zero GPU, or keep an idle one billing for as long as the tab
// stayed open.
const USAGE = { id: null, hours: 24, data: null, req: 0, timer: null };
const USAGE_WINDOWS = [[6, "6h"], [24, "24h"], [168, "7d"]];
const usageDeps = () => (DEPLOY.deployments || []).filter(d => d.endpoint_url && d.status !== "deleted");
async function loadUsage() {
  const sec = document.getElementById("dp-usage"); if (!sec) return;
  const deps = usageDeps();
  if (!deps.length) { sec.style.display = "none"; return; }
  sec.style.display = "";
  if (!deps.some(d => d.id === USAGE.id)) USAGE.id = (deps.find(d => d.in_use) || deps[0]).id;
  const my = ++USAGE.req;
  if (!USAGE.data || USAGE.data.id !== USAGE.id || USAGE.data.hours !== USAGE.hours) paintUsage(sec, null);
  let r;
  try { r = await api("/api/deploy/usage?" + q({ id: USAGE.id, hours: USAGE.hours })); }
  catch (e) { r = { ok: false, error: e.message }; }
  if (my !== USAGE.req || !document.getElementById("dp-usage")) return;
  USAGE.data = r;
  paintUsage(sec, r);
  clearTimeout(USAGE.timer);
  USAGE.timer = setTimeout(() => { if (curView === "deploy" && !document.hidden) loadUsage(); }, 60000);
}
function paintUsage(sec, r) {
  [...sec.children].forEach(c => { if (!c.classList.contains("sec-t")) c.remove(); });
  const deps = usageDeps();
  const bar = el("div","us-bar");
  if (deps.length > 1) {
    const dc = el("div","fchips us-deps");
    deps.forEach(d => {
      const c = el("button","fchip" + (d.id === USAGE.id ? " on" : ""));
      c.append(_orgOf(d.model) ? orgMark(_orgOf(d.model)) : fillMark(el("span","omark"), "selfhost", d.model),
               document.createTextNode(shortId(d.model)));
      c.onclick = () => { USAGE.id = d.id; loadUsage(); };
      dc.append(c);
    });
    bar.append(dc);
  } else {
    const d = deps[0];
    bar.append(el("span","us-note", "From " + shortId(d.model) + "'s own container log — reading it never wakes the GPU."));
  }
  bar.append(el("span","sp"));
  const wc = el("div","fchips");
  USAGE_WINDOWS.forEach(([h, lab]) => {
    const c = el("button","fchip" + (h === USAGE.hours ? " on" : ""), lab);
    c.onclick = () => { USAGE.hours = h; loadUsage(); };
    wc.append(c);
  });
  bar.append(wc);
  sec.append(bar);
  if (!r) { const sk = el("div","tiles"); for (let i = 0; i < 4; i++) sk.append(el("div","tile flat us-sk")); sec.append(sk); return; }
  if (!r.ok) {
    sec.append(el("div","us-empty", r.supported === false
      ? "This provider has no log API, so there's nothing to chart here — its own console has the numbers."
      : "Couldn't read the log: " + errText(r)));
    return;
  }
  const S = r.summary || {};
  const since = r.since, until = r.until, span = until - since;
  const winLab = (USAGE_WINDOWS.find(([h]) => h === USAGE.hours) || [0, USAGE.hours + "h"])[1];
  // one binning for every chart on the panel, so a spike lines up across them
  const BINS = USAGE.hours <= 6 ? 36 : USAGE.hours <= 24 ? 48 : 56;
  const binOf = t => Math.max(0, Math.min(BINS - 1, Math.floor((t - since) / span * BINS)));
  const genPk = new Array(BINS).fill(0), promptPk = new Array(BINS).fill(0);
  const reqN = new Array(BINS).fill(0), errN = new Array(BINS).fill(0);
  (r.stats || []).forEach(x => { const b = binOf(x[0]); genPk[b] = Math.max(genPk[b], x[2]); promptPk[b] = Math.max(promptPk[b], x[1]); });
  (r.requests || []).forEach(x => { const b = binOf(x[0]); reqN[b]++; if (x[1] >= 400) errN[b]++; });
  const tps = v => v >= 100 ? Math.round(v) : +v.toFixed(1);
  const plural = (n, w) => n + " " + w + (n === 1 ? "" : "s");

  // ---- the four readings ----------------------------------------------
  const tiles = el("div","tiles");
  tile(tiles, "trace", "Generating", S.gen_tps_now ? String(tps(S.gen_tps_now)) : "Idle", S.gen_tps_now ? "tok/s" : null,
    S.gen_tps_peak ? "peak " + tps(S.gen_tps_peak) + " tok/s · " + fmtTok(S.tokens_out) + " out, " + fmtTok(S.tokens_in) + " in"
                   : "nothing generated in " + winLab,
    (r.stats || []).length ? sparkLine(genPk) : "");
  tile(tiles, "msg", "Requests · " + winLab, fmt(S.requests), null,
    (S.requests ? (S.errors ? S.errors + " failed" : "none failed") : "nothing served") + " · " + plural(S.wakes || 0, "wake"),
    S.requests ? sparkBars(reqN) : "");
  const readyPct = S.gpu_s ? Math.round(100 * S.serving_s / S.gpu_s) : 0;
  tile(tiles, "clock", "GPU time · " + winLab, S.gpu_s ? fmtDur(S.gpu_s) : "0m", null,
    S.gpu_s ? fmtDur(S.booting_s) + " booting · " + readyPct + "% ready" : "no container ran", "");
  tile(tiles, "spend", "Billed · " + winLab, S.cost_usd != null ? fmtUsd(S.cost_usd) : "—", null,
    S.usd_per_m_out != null ? fmtUsd(S.usd_per_m_out) + " per 1M tokens out"
      : S.rate_per_hour != null ? "at " + fmtRate(S.rate_per_hour) + " while a container is up" : "no rate on record", "");
  sec.append(tiles);

  // ---- containers: booting vs ready, and every request on top ----------
  const tl = el("div","card us-full");
  cardHead(tl, "deploy", "Containers", plural(S.wakes || 0, "start") +
    (S.median_boot_s != null ? " · boot median " + fmtDur(S.median_boot_s) : ""));
  // the one conclusion the chart supports, said once: GPU time that served
  // almost nothing was paid for by wakes and idle windows, not by work
  if (S.wakes > 1 && S.requests <= S.wakes * 2 && S.gpu_s > 1800) {
    const n = el("div","us-note");
    n.innerHTML = "<b>" + plural(S.requests, "request") + " across " + plural(S.wakes, "wake") + ".</b> " +
      "Each wake boots for ~" + esc(fmtDur(S.median_boot_s || 0)) + " and then idles until the scale-down window runs out — that is most of the bill.";
    tl.append(n);
  }
  tl.append(usageTimeline(r, since, until));
  const lg = el("div","us-leg");
  lg.innerHTML = '<span><i class="bt"></i>Booting</span><span><i></i>Ready to serve</span><span><i class="rq"></i>Request</span>';
  tl.append(lg);
  sec.append(tl);

  // ---- throughput and requests ------------------------------------------
  const slot = fmtDur(span / BINS);
  const g = el("div","us-grid");
  const tc = el("div","card");
  cardHead(tc, "trace", "Throughput", "tokens/s · peak per " + slot);
  if ((r.stats || []).length) {
    tc.append(usageLines(genPk, promptPk, since, until));
    const l2 = el("div","us-leg");
    l2.innerHTML = '<span><i class="ln"></i>Generation</span><span><i class="ln p"></i>Prompt</span>';
    tc.append(l2);
  } else tc.append(el("div","us-empty", "Nothing generated in " + winLab + " — vLLM logs throughput only while it is working."));
  const rc = el("div","card");
  cardHead(rc, "msg", "Requests", "per " + slot);
  if (S.requests) rc.append(usageBars(reqN, errN, since, until));
  else rc.append(el("div","us-empty", "No requests in " + winLab + "."));
  g.append(tc, rc);
  sec.append(g);
}
// The time axis is HTML, not SVG text: the plots stretch to any width
// (preserveAspectRatio none), which would stretch their lettering with them.
function usageAxis(since, until) {
  const ax = el("div","us-ax");
  const span = until - since, n = 4;
  const lab = t => { const d = new Date(t * 1000);
    return span > 2 * 86400 ? d.toLocaleDateString([], { weekday: "short", day: "numeric" })
                            : d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); };
  for (let i = 0; i <= n; i++) {
    const s = el("span", null, i === n ? "now" : lab(since + span * i / n));
    s.style.left = (100 * i / n) + "%"; ax.append(s);
  }
  return ax;
}
function usageTip(box) { const t = el("div","tip"); t.style.display = "none"; box.append(t); return t; }
function placeTip(box, tip, px) {
  const w = box.clientWidth, tw = tip.offsetWidth || 160;
  tip.style.left = Math.max(0, Math.min(w - tw, px - tw / 2)) + "px";
}
const hhmm = t => new Date(t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
function usageTimeline(r, since, until) {
  const W = 1000, H = 44, lane = 22, top = 16, span = until - since;
  const X = t => Math.max(0, Math.min(W, (t - since) / span * W));
  let out = "";
  (r.sessions || []).forEach((s, i) => {
    const a = X(s.start), b = X(s.ready != null ? s.ready : s.end), c = X(s.end);
    // booting and ready are one container, split by a 1-unit surface gap
    if (b - a > 0.3) out += `<rect class="bt" x="${a.toFixed(1)}" y="${top}" width="${Math.max(0.8, b - a - (s.ready != null ? 1 : 0)).toFixed(1)}" height="${lane}" rx="2"/>`;
    if (s.ready != null && c - b > 0.3) out += `<rect class="sv" x="${(b + 1).toFixed(1)}" y="${top}" width="${Math.max(0.8, c - b - 1).toFixed(1)}" height="${lane}" rx="2"/>`;
    out += `<rect class="hit" data-i="${i}" x="${Math.max(0, a - 3).toFixed(1)}" y="0" width="${Math.max(8, c - a + 6).toFixed(1)}" height="${H}"/>`;
  });
  (r.requests || []).forEach(x => {
    const px = X(x[0]).toFixed(1);
    out += `<line class="rq${x[1] >= 400 ? " bad" : ""}" x1="${px}" y1="3" x2="${px}" y2="${top - 3}"/>`;
  });
  const box = el("div","us-chart tl");
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="containers over time, booting and ready">${out}</svg>`;
  const tip = usageTip(box);
  box.querySelectorAll(".hit").forEach(h => {
    h.onmousemove = ev => {
      const s = r.sessions[+h.dataset.i];
      tip.innerHTML = "<b>" + hhmm(s.start) + (s.open ? " → now" : " → " + hhmm(s.end)) + "</b>" +
        "<span>" + (s.boot_s != null ? "booted in " + fmtDur(s.boot_s) : "still booting") +
        " · up " + fmtDur(s.end - s.start) + " · " + s.requests + " request" + (s.requests === 1 ? "" : "s") + "</span>";
      tip.style.display = "";
      placeTip(box, tip, ev.clientX - box.getBoundingClientRect().left);
    };
    h.onmouseleave = () => { tip.style.display = "none"; };
  });
  const wrap = el("div"); wrap.append(box, usageAxis(since, until));
  return wrap;
}
function usageLines(gen, prompt, since, until) {
  const W = 1000, H = 132, PT = 10, PB = 1, n = gen.length, span = until - since;
  const max = Math.max(1, ...gen, ...prompt);
  const X = i => (i + 0.5) / n * W, Y = v => PT + (H - PT - PB) * (1 - v / max);
  const path = a => "M" + a.map((v, i) => X(i).toFixed(1) + "," + Y(v).toFixed(1)).join(" L");
  const box = el("div","us-chart");
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="tokens per second over time">` +
    `<line class="grid" x1="0" y1="${Y(max).toFixed(1)}" x2="${W}" y2="${Y(max).toFixed(1)}"/>` +
    `<line class="grid" x1="0" y1="${Y(max / 2).toFixed(1)}" x2="${W}" y2="${Y(max / 2).toFixed(1)}"/>` +
    `<line class="base" x1="0" y1="${H - PB}" x2="${W}" y2="${H - PB}"/>` +
    `<path class="a1" d="${path(gen)} L${X(n - 1).toFixed(1)},${H - PB} L${X(0).toFixed(1)},${H - PB} Z"/>` +
    `<path class="l2" d="${path(prompt)}"/><path class="l1" d="${path(gen)}"/>` +
    `<line class="xh" x1="0" y1="${PT}" x2="0" y2="${H - PB}" style="display:none"/></svg>`;
  box.append(el("div","us-y", Math.round(max) + " tok/s"));
  const tip = usageTip(box), svg = box.querySelector("svg"), xh = box.querySelector(".xh");
  svg.onmousemove = ev => {
    const rc = svg.getBoundingClientRect(); if (!rc.width) return;
    const i = Math.max(0, Math.min(n - 1, Math.floor((ev.clientX - rc.left) / rc.width * n)));
    xh.setAttribute("x1", X(i).toFixed(1)); xh.setAttribute("x2", X(i).toFixed(1)); xh.style.display = "";
    tip.innerHTML = '<i style="background:var(--s1)"></i><b>' + (+gen[i].toFixed(1)) + "</b> gen" +
      '<i style="background:var(--s2)"></i><b>' + (+prompt[i].toFixed(1)) + "</b> prompt" +
      "<span>" + hhmm(since + span * i / n) + "</span>";
    tip.style.display = ""; placeTip(box, tip, X(i) / W * rc.width);
  };
  svg.onmouseleave = () => { xh.style.display = "none"; tip.style.display = "none"; };
  const wrap = el("div"); wrap.append(box, usageAxis(since, until));
  return wrap;
}
function usageBars(reqN, errN, since, until) {
  const W = 1000, H = 132, PT = 10, n = reqN.length, bw = W / n, span = until - since;
  const max = Math.max(1, ...reqN);
  let bars = "";
  reqN.forEach((v, i) => {
    if (!v) return;
    const h = Math.max(4, v / max * (H - PT));
    bars += `<rect class="br" x="${(i * bw + 1).toFixed(1)}" y="${(H - h).toFixed(1)}" width="${Math.max(1, bw - 2).toFixed(1)}" height="${h.toFixed(1)}" rx="2"/>`;
  });
  const box = el("div","us-chart");
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="requests per time slot">` +
    `<line class="base" x1="0" y1="${H}" x2="${W}" y2="${H}"/>${bars}</svg>`;
  box.append(el("div","us-y", max + " max"));
  const tip = usageTip(box), svg = box.querySelector("svg");
  svg.onmousemove = ev => {
    const rc = svg.getBoundingClientRect(); if (!rc.width) return;
    const i = Math.max(0, Math.min(n - 1, Math.floor((ev.clientX - rc.left) / rc.width * n)));
    tip.innerHTML = "<b>" + reqN[i] + "</b> request" + (reqN[i] === 1 ? "" : "s") +
      (errN[i] ? " · " + errN[i] + " failed" : "") + "<span>" + hhmm(since + span * i / n) + "</span>";
    tip.style.display = ""; placeTip(box, tip, (i + 0.5) / n * rc.width);
  };
  svg.onmouseleave = () => { tip.style.display = "none"; };
  const wrap = el("div"); wrap.append(box, usageAxis(since, until));
  return wrap;
}
const DC_STATE = {
  launching: ["run", "Deploying"], stopping: ["run", "Stopping"], failed: ["bad", "Failed"], waking: ["run", "Waking"],
  running: ["ok", "Running"], scaled_to_zero: ["ok", "Idle"], starting: ["run", "Starting"],
  pending: ["run", "Pending"], building: ["run", "Building"], deleting: ["run", "Stopping"],
  paused: ["pend", "Paused"], unknown: ["", "Unknown"],
};
function depCard(card, it) {
  card = card || el("div"); card.innerHTML = "";
  const d = it.d, j = it.job, down = it.down, conn = it.conn;
  const waking = !!(conn && conn.status === "running");
  const meta = (j && j.meta) || {};
  const model = d ? d.model : (meta.model || (j && j.target) || "");
  const pid = d ? d.provider : meta.provider;
  const p = DEPLOY.providers.find(x => x.id === pid) || {};
  const flying = !!(j && j.status === "running");
  const failed = !!(j && j.status === "error");
  const state = down ? "stopping" : flying ? "launching" : failed ? "failed" : waking ? "waking" : (d ? d.status : "unknown");
  card.className = "dcard" + (failed || state === "failed" ? " bad" : "") + (d && d.in_use ? " inuse" : "");
  card.dataset.key = it.key;

  const hd = el("div","dc-h");
  // no org in the id (an adopted app mantis never recorded a model for):
  // the server glyph, not a question mark
  hd.append(_orgOf(model) ? orgMark(_orgOf(model)) : fillMark(el("span","omark"), "selfhost", model));
  const tt = el("div","dc-t");
  const nm = el("div","dc-n", shortId(model)); nm.title = model; tt.append(nm);
  const rate = d ? depRate(d) : (meta.price_per_hour != null ? meta.price_per_hour : null);
  // a GPU nobody recorded is left out, not printed as "unknown 0 GB"
  const knownGpu = d && d.gpu && d.gpu.provider_id !== "unknown" && d.gpu.total_vram_gb;
  const gpuTxt = knownGpu ? gpuName(d.gpu) + " " + fmtGb(d.gpu.total_vram_gb) : d ? "" : (meta.gpu_label || meta.gpu || "");
  const sub = el("div","dc-s");
  sub.append(providerMark(p.logo || pid, p.display_name || pid));
  sub.append(el("span", null, [p.display_name || meta.provider_name || pid, gpuTxt, rate != null ? fmtRate(rate) : null]
    .filter(Boolean).join(" · ")));
  tt.append(sub); hd.append(tt);
  const [cls, label] = DC_STATE[state] || ["", String(state || "?").replace(/_/g, " ")];
  const pill2 = el("span","dc-st " + cls); pill2.append(el("i"), document.createTextNode(label));
  if (d && d.message && !flying) pill2.title = d.message;
  hd.append(pill2);
  card.append(hd);

  if (flying) card.append(stageTrack(j));
  else if (waking) {
    // a cold start, said as one: what is happening, how long so far
    const w = el("div","dc-now");
    w.append(el("span", null, "Waking a replica so mantis can use it — a cold start"));
    const clock = el("b","dc-clock", fmtClock(Date.now() / 1000 - (conn.started_at || Date.now() / 1000)));
    clock.dataset.t0 = String(conn.started_at || Date.now() / 1000); w.append(clock);
    card.append(w);
    if (conn.last_line) { const ll = el("div","dc-last", conn.last_line); ll.title = conn.last_line; card.append(ll); }
  }
  else if (down) card.append(el("div","dc-line", "Deleting it on " + (p.display_name || pid) + "…"));
  else if (failed) {
    const e = el("div","dc-err"); e.append(el("b", null, j.error || "The deploy failed"));
    if (j.hint) e.append(el("span", null, j.hint));
    card.append(e);
    if (j.deployment_id && d && d.status !== "deleted")
      card.append(el("div","dc-warn", "It was created on " + (p.display_name || pid) + " and may still be billing — stop it unless you'll retry."));
  } else if (d) {
    const ep = el("div","dc-ep");
    if (d.endpoint_url) {
      const u = el("span","dc-url", d.endpoint_url.replace(/^https?:\/\//, "")); u.title = d.endpoint_url;
      const cp = btn("Copy", "gho", () => copyText(d.endpoint_url, "endpoint")); cp.classList.add("dc-copy");
      ep.append(u, cp);
    } else ep.append(el("span","dc-url dim", d.message === "no endpoint URL recorded"
      ? "No endpoint on record \u2014 mantis can't call it. Forget it here, or stop it on " + (p.display_name || pid) + "."
      : d.message || "no endpoint yet"));
    card.append(ep);
    const facts = el("div","dc-facts");
    if (d.created_at) facts.append(el("span", null, (d.is_live ? "up " : "created ") + fmtDur(Date.now() / 1000 - d.created_at)));
    if (d.engine) facts.append(el("span", null, d.engine));
    if (j && j.connect_error) facts.append(el("span","amb", j.connect_error));
    if (conn && conn.status === "error") facts.append(el("span","amb", "couldn't switch mantis to it — " + (conn.error || "it didn't answer")));
    card.append(facts);
  }

  const foot = el("div","dc-foot");
  const usable = !!(d && d.is_live && !down && !flying && !waking);
  if (d && d.in_use && !down) foot.append(el("span","dc-inuse", "In use by mantis"));
  else if (usable) { const u = btn("Use in mantis", "pri", () => useDeployment(d, u)); foot.append(u); }
  if (failed && meta.model) foot.append(btn("Try again", "pri", () => retryJob(j)));
  if (usable) foot.append(btn("Try it", "gho", () => openTry(d)));
  const logId = d ? d.id : (j && j.deployment_id);
  if (logId && !down) foot.append(btn("Logs", "gho", () => openLogs(d || { id: logId, provider: pid, model })));
  foot.append(el("span","dc-sp"));
  const dead = d && !d.endpoint_url && ["unknown", "failed", "paused", "deleted"].includes(d.status) && !flying && !waking;
  if (dead) foot.append(btn("Forget", "gho", async () => {
    let r; try { r = await post("/api/deploy/forget", { id: d.id }); } catch (e) { r = { ok: false, error: e.message }; }
    if (!r.ok) { toast(errText(r), true); return; }
    toast("Forgot " + shortId(d.model || d.id)); syncDeploy(false);
  }));
  else if (d && !down && d.status !== "deleting") foot.append(btn("Stop", "gho dan", () => confirmTeardown(d)));
  else if (flying && !d) {
    // nothing rented yet: stopping costs nothing and needs no confirmation
    const c = btn("Cancel", "gho", () => cancelJob(j, c)); foot.append(c);
  }
  else if (failed && !d) foot.append(btn("Dismiss", "gho", () => dismissJob(j.id)));
  card.append(foot);
  return card;
}
function stageTrack(j) {
  const w = el("div","dc-stage");
  const cur = Math.max(0, STAGES.findIndex(([k]) => k === (j.stage || "prepare")));
  const tr = el("div","dc-track");
  STAGES.forEach(([k, lab], i) => {
    const s = el("div","dc-step" + (i < cur ? " did" : i === cur ? " cur" : ""));
    s.append(el("i"), el("span", null, lab));
    tr.append(s);
  });
  w.append(tr);
  const now = el("div","dc-now");
  now.append(el("span", null, STAGE_TEXT[j.stage || "prepare"] || "Working"));
  const clock = el("b","dc-clock", fmtClock(Date.now() / 1000 - (j.started_at || Date.now() / 1000)));
  clock.dataset.t0 = String(j.started_at || Date.now() / 1000);
  now.append(clock);
  w.append(now);
  // while it boots, the container's own latest line beats the manager's
  // "waiting for the endpoint…" — that is what tells you weights are loading
  const line = j.boot_line || j.last_line;
  if (line) { const ll = el("div","dc-last", line); ll.title = line; w.append(ll); }
  return w;
}
async function cancelJob(j, b) {
  if (b) { b.disabled = true; b.textContent = "Cancelling…"; }
  let r;
  try { r = await post("/api/deploy/cancel", { job: j.id }); } catch (e) { r = { ok: false, error: e.message }; }
  if (!r.ok) { toast(errText(r), true); if (b) { b.disabled = false; b.textContent = "Cancel"; } return; }
  j.status = "cancelled";
  if (r.teardown) trackJob({ id: r.teardown, kind: "teardown", status: "running", deployment_id: j.deployment_id,
                             started_at: Date.now() / 1000, meta: {} });
  else { renderActive(); paintDeployReads(); }
  toast("Cancelled " + shortId((j.meta || {}).model || j.target));
}
async function retryJob(j) {
  const m = j.meta || {};
  let r;
  try {
    r = await post("/api/deploy/up", { provider: m.provider, model: m.model, gpu: m.gpu, engine: m.engine || "vllm",
      opts: {}, use_when_ready: !!m.use_when_ready,
      display: { gpu_label: m.gpu_label, price_per_hour: m.price_per_hour, provider_name: m.provider_name } });
  } catch (e) { r = { ok: false, error: e.message }; }
  if (!r.ok) { toast(errText(r), true); return; }
  dismissJob(j.id);
  trackJob({ id: r.job, kind: "deploy", status: "running", stage: "prepare", started_at: Date.now() / 1000, meta: { ...m } });
}

// ---- keeping it current ----------------------------------------------------
// While anything is moving the page asks every 2.5s (the event stream usually
// beats it); at rest, every 20s. A provider can change a deployment's state
// on its own — scale it to zero, fail a replica — so the page also asks the
// providers themselves, once on arrival and every minute while something is
// in a transitional state.
let depPollT = null, depTickT = null, depProvAt = 0;
const DEP_MOVING = ["pending", "building", "starting", "deleting"];
function kickDeployPoll(now) {
  if (depPollT) { if (!now) return; clearTimeout(depPollT); depPollT = null; }
  const loop = async () => {
    depPollT = null;
    if (curView !== "deploy") return;
    if (!document.hidden) {
      const moving = (DEPLOY.deployments || []).some(d => DEP_MOVING.includes(d.status));
      await syncDeploy(moving && Date.now() - depProvAt > 60000);
    }
    const busy = (DEPLOY.jobs || []).some(j => j.status === "running");
    depPollT = setTimeout(loop, busy ? 2500 : 20000);
  };
  depPollT = setTimeout(loop, now ? 600 : 2500);
  if (!depTickT) depTickT = setInterval(() => {
    if (curView !== "deploy" || document.hidden) return;
    const t = Date.now() / 1000;
    document.querySelectorAll(".dc-clock[data-t0]").forEach(c => { c.textContent = fmtClock(t - parseFloat(c.dataset.t0)); });
  }, 1000);
}
async function syncDeploy(providers) {
  if (providers) depProvAt = Date.now();
  let jr, lr;
  try {
    [jr, lr] = await Promise.all([api("/api/deploy/jobs"), api("/api/deploy/list" + (providers ? "?refresh=1" : ""))]);
  } catch (e) { return; }
  const was = new Map((DEPLOY.jobs || []).map(j => [j.id, j.status]));
  if (jr && jr.ok) DEPLOY.jobs = jr.jobs || [];
  if (lr && lr.deployments) DEPLOY.deployments = lr.deployments;
  renderActive(); paintDeployReads();
  // a deploy that just finished: say so wherever the user is looking
  (DEPLOY.jobs || []).forEach(j => {
    if (was.get(j.id) !== "running" || j.status === "running") return;
    const name = shortId((j.meta || {}).model || j.target);
    if (j.kind === "deploy" && j.status === "done") { toast("✓ " + name + " is live" + (j.connected ? " — mantis is using it" : "")); loadOverview(); }
    else if (j.kind === "deploy" && j.status === "error") toast(name + " failed — " + (j.error || "see its card"), true);
    else if (j.kind === "teardown" && j.status === "done") toast("Stopped " + name);
    else if (j.kind === "connect" && j.status === "done") { toast("mantis now uses " + ((j.meta || {}).model || "it")); loadOverview(); }
    else if (j.kind === "connect" && j.status === "error") toast("couldn't switch — " + (j.error || "it didn't answer"), true);
  });
}
function paintDeployReads() {
  const w = document.getElementById("dp-reads"); if (!w) return;
  w.innerHTML = "";
  const deps = DEPLOY.deployments || [];
  const up = deps.filter(d => d.status === "running");
  const booting = deps.filter(d => d.status === "starting");
  const asleep = deps.filter(d => d.status === "scaled_to_zero");
  // The burn is what is billing NOW: a container serving or booting. An app
  // scaled to zero costs nothing until something wakes it, so it is counted
  // as asleep, never as burn. Rates are the providers' own; an endpoint with
  // no rate is unpriced, never free.
  const billing = up.concat(booting);
  const priced = billing.filter(d => depRate(d) != null);
  const burn = priced.reduce((a, d) => a + depRate(d), 0);
  const unpriced = billing.length - priced.length;
  const live = billing;
  const ready = DEPLOY.providers.filter(p => p.configured && provReady(p)).length;
  const flying = (DEPLOY.jobs || []).filter(j => j.kind === "deploy" && j.status === "running").length;
  const reads = [{ v: up.length, k: "running" }];
  if (booting.length) reads.push({ v: booting.length, k: "booting" });
  if (asleep.length) reads.push({ v: asleep.length, k: "asleep" });
  if (flying) reads.push({ v: flying, k: "deploying" });
  if (priced.length) reads.push({ v: fmtRate(burn), k: unpriced ? "burn · " + unpriced + " unpriced" : "burn" });
  else if (live.length) reads.push({ v: "—", k: "burn · unpriced" });
  reads.push({ v: ready + " of " + DEPLOY.providers.length, k: "providers ready" });
  pageReads(w, reads, "Pick a model — it's two clicks to a running endpoint.",
    "Rates are the providers' own list prices; nothing here is billed by mantis.");
}

// ---- use, logs, stop -------------------------------------------------------
// Connect: the server re-checks /models (retrying a cold start), then makes
// this endpoint the current model + backend for the SDK and the terminal.
async function useDeployment(d, b) {
  // Connect is a job: a scaled-to-zero replica takes a cold start to wake,
  // and the card shows that wake with a clock — a button cannot.
  if (b) { b.disabled = true; b.textContent = "Waking…"; }
  let r;
  try { r = await post("/api/deploy/connect", { id: d.id, job: true }); } catch (e) { r = { ok: false, error: e.message }; }
  if (!r.ok) { toast(errText(r), true); if (b) { b.disabled = false; b.textContent = "Use in mantis"; } return; }
  trackJob({ id: r.job, kind: "connect", status: "running", deployment_id: d.id, started_at: Date.now() / 1000, meta: {} });
  if (d.status === "scaled_to_zero") toast("Waking " + shortId(d.model) + " — a cold start takes a few minutes");
}
// "Does it actually answer?" — one real request against the endpoint, with
// the reply, how long it took and how fast it generated. Nothing else on the
// page proves the thing works; the green dot only says the provider thinks so.
function openTry(d) {
  const s = document.getElementById("sheet"); s.innerHTML = "";
  const h = el("div","lg-h");
  h.append(el("h3", null, "Try it"), el("span","lg-sub", shortId(d.model) + " · " + provName(d.provider)));
  s.append(h);
  const row = el("div","tr-row");
  const i = input("Ask it something"); i.value = "Say hello in one short sentence.";
  const go = btn("Send", "pri", () => send());
  row.append(i, go); s.append(row);
  const out = el("div","tr-out"); s.append(out);
  const stamp = el("div","tr-stats"); s.append(stamp);
  const open = () => document.getElementById("modal").className && s.firstChild === h;
  let t0 = 0, tick = null;
  async function send() {
    if (go.disabled) return;
    go.disabled = true; go.textContent = "Waiting…";
    out.className = "tr-out wait"; stamp.innerHTML = "";
    t0 = Date.now();
    out.textContent = "Waiting for " + shortId(d.model) + " to answer…";
    const idleNote = d.status === "scaled_to_zero" ? " It's scaled to zero, so a replica has to wake first — that can take a few minutes." : "";
    tick = setInterval(() => { if (!open()) { clearInterval(tick); return; }
      out.textContent = "Waiting for " + shortId(d.model) + " to answer… " + fmtClock((Date.now() - t0) / 1000) + idleNote; }, 500);
    let r;
    try { r = await post("/api/deploy/try", { id: d.id, prompt: i.value }); } catch (e) { r = { ok: false, error: e.message }; }
    clearInterval(tick);
    if (!open()) return;
    go.disabled = false; go.textContent = "Send";
    if (!r.ok) { out.className = "tr-out bad"; out.textContent = errText(r); return; }
    out.className = "tr-out"; out.textContent = r.reply || "(an empty reply)";
    stamp.append(stat(r.latency_s != null ? r.latency_s.toFixed(r.latency_s < 10 ? 2 : 1) + "s" : "—", "to answer"),
                 stat(r.tokens_per_s != null ? r.tokens_per_s : "—", "tokens / s"),
                 stat(r.completion_tokens != null ? r.completion_tokens : "—", "tokens"));
  }
  i.onkeydown = e => { if (e.key === "Enter") send(); };
  showModal(true);
  setTimeout(() => { i.focus(); i.select(); }, 30);
}
async function openLogs(d) {
  const s = document.getElementById("sheet"); s.innerHTML = "";
  const h = el("div","lg-h");
  h.append(el("h3", null, "Logs"), el("span","lg-sub", shortId(d.model || d.name || d.id) + " · " + provName(d.provider)));
  h.append(el("span","dc-sp"));
  const st = el("span","lg-st", "");
  const follow = document.createElement("input"); follow.type = "checkbox"; follow.checked = true;
  const fl = el("label","chk lg-follow"); fl.append(follow, document.createTextNode("Follow"));
  fl.title = "fetch new lines every few seconds and stay at the bottom";
  const log = el("pre","lg-body", "Loading…");
  h.append(st, fl, btn("Copy", "gho", () => copyText(log.textContent, "logs")));
  s.append(h, log);
  let t = null, first = true;
  const open = () => document.getElementById("modal").className && s.firstChild === h;
  const load = async () => {
    clearTimeout(t);
    if (!open()) return;
    let r;
    try { r = await api("/api/deploy/logs?" + q({ id: d.id, tail: 500 })); } catch (e) { r = { ok: false, error: e.message }; }
    if (!open()) return;
    if (r.ok) {
      const pinned = first || log.scrollHeight - log.scrollTop - log.clientHeight < 40;
      log.textContent = (r.lines || []).join("\n") || "No output yet — a booting container can take a minute to say anything.";
      if (pinned) log.scrollTop = log.scrollHeight;
      first = false;
      st.textContent = (r.lines || []).length + " lines · " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    } else {
      log.textContent = r.supported === false
        ? "This provider doesn't expose logs through its API. " + (r.hint || "") : errText(r);
      follow.checked = false; fl.style.display = "none"; st.textContent = "";
    }
    if (follow.checked) t = setTimeout(load, 4000);
  };
  follow.onchange = () => { if (follow.checked) load(); else clearTimeout(t); };
  showModal(true); load();
}
function confirmTeardown(d) {
  const rate = depRate(d);
  const s = document.getElementById("sheet"); s.innerHTML = "";
  s.append(el("h3", null, "Stop " + shortId(d.model) + "?"));
  const bits = ["This deletes the endpoint on " + provName(d.provider)
    + (rate != null ? " and stops billing " + fmtRate(rate) + "." : ".")];
  if (d.in_use) bits.push("mantis is using it right now — pick another model afterwards.");
  bits.push("The model's weights are untouched; deploying it again takes a couple of minutes.");
  s.append(el("div","sub ds-stopsub", bits.join(" ")));
  const foot = el("div","ds-foot");
  const go = btn("Stop and delete", "dan", async () => {
    go.disabled = true; go.textContent = "Stopping…";
    let r;
    try { r = await post("/api/deploy/down", { id: d.id }); } catch (e) { r = { ok: false, error: e.message }; }
    if (!r.ok) { toast(errText(r), true); go.disabled = false; go.textContent = "Stop and delete"; return; }
    hideModal();
    trackJob({ id: r.job, kind: "teardown", status: "running", deployment_id: d.id, started_at: Date.now() / 1000, meta: {} });
  });
  foot.append(btn("Cancel", "gho", hideModal), go);
  s.append(foot);
  showModal();
}

// ---- top bar + nav ----
// The rail's foot is the one always-visible answer to "what is this agent
// wired to right now" — the model it will use, how that model is reached, and
// how much is plugged in. Every number is a link to the page that changes it.
let OVERVIEW = {};
// The rail's foot is the one line that is true on every page: which model
// this dashboard is pointed at, who is serving it, and whether anything is
// running right now. It is a target, not a label — clicking it goes to the
// page that would change the answer.
function renderTopStatus(o) {
  const f = document.getElementById("railfoot");
  const cur = (o.current && o.current.model) ? o.current.model : "no model set";
  const h = o.hosting || {};
  const live = (o.active_jobs || 0) + (o.active_runs || 0);
  f.innerHTML = "";
  f.append(el("span","live" + (h.label || h.kind === "selfhost" ? "" : " off")));
  const t = el("span","rf-t");
  const v = el("span","rf-v", cur); t.append(v);
  t.append(el("span","rf-s", "via " + (h.label || (h.kind === "selfhost" ? "your server" : "no provider")) +
    (o.deployments_live ? " · " + o.deployments_live + " deployed" : "") +
    (live ? " · " + live + " running" : "")));
  f.append(t);
  f.title = cur + " · via " + (h.label || "no provider") + " — click for models";
  f.onclick = () => showTab(o.deployments_live && !h.label ? "deploy" : "models");
  const hostEl = document.getElementById("railhost");
  if (hostEl && o.home) hostEl.textContent = (o.cwd || o.home).replace(/^\/Users\/[^/]+/, "~").split("/").pop() || "local";
  if (hostEl) hostEl.title = o.cwd || o.home || "";
  if (curView === "home") setCrumb();
  const ver = document.getElementById("railver");
  if (ver && o.version) { ver.textContent = "v" + o.version; ver.title = "mantis " + o.version; }
  railCounts(o);
}
// A count in the rail answers "is there anything in there" before you click.
// Zero is not printed: an empty page says so once you open it, and a column
// of dashes is noise. What is happening NOW gets the live tint and a dot.
function railCounts(o) {
  const set = (v, txt, cls) => {
    const n = document.getElementById("n-" + v); if (!n) return;
    n.innerHTML = ""; n.className = "nc" + (cls ? " " + cls : "");
    if (txt) n.append(document.createTextNode(txt));
  };
  const fams = Object.keys(o.families_ready || {}).length;
  set("models", fams ? o.family_ready_count + "/" + fams : "");
  set("deploy", o.deployments_live ? String(o.deployments_live) : "", o.deployments_live ? "hot" : "");
  set("sessions", o.session_count ? fmt(o.session_count) : "");
  const live = (o.active_jobs || 0) + (o.active_runs || 0);
  const act = document.getElementById("n-activity");
  if (act) {
    act.innerHTML = ""; act.className = "nc" + (live ? " hot dot" : "");
    if (live) { act.append(el("span","live")); act.append(document.createTextNode(String(live))); }
  }
  set("mcp", o.mcp_count ? String(o.mcp_count) : "");
  set("skills", o.skill_count ? String(o.skill_count) : "");
  // A run that failed is the one reading in the rail you are meant to act on,
  // so it is the one that gets a surface. It only replaces the live count when
  // nothing is running — two numbers on one row is a row nobody reads.
  if (act && !live && (o.failed_7d || 0) > 0) {
    act.className = "nc due";
    act.title = o.failed_7d + " failed in the last 7 days";
    act.append(document.createTextNode(String(o.failed_7d)));
  }
  paintSubs();
}
async function loadOverview() {
  const o = await api("/api/overview");
  OVERVIEW = o;
  renderTopStatus(o);
}
// ==========================================================================
// THE RAIL — eight pages, and under the page you are on, the rows you were
// about to click anyway.
//
// Sub-rows are drawn ONLY under the active page. A rail that expands every
// section at once is a filing cabinet: you read all of it to find one thing.
// One open section is a breadcrumb you can click sideways — the five
// families while you are in Models, your recent projects while you are in
// Sessions, what is live while you are in Deploy. They are painted from data
// the pages already fetched, so opening the rail costs no request.
// ==========================================================================
const VIEWS = ["home","models","deploy","sessions","activity","mcp","skills","memory","config"];
const PAGE_NAMES = { home: "Overview", models: "My models", deploy: "Deploy", sessions: "Sessions",
                     activity: "Activity", mcp: "MCP", skills: "Skills", memory: "Memory", config: "Config" };
let curView = "home", SUB_OPEN = true;
// what each expandable page has under it, if it has anything yet
function subRows(v) {
  if (v === "models") {
    const rows = (FAMS || []).map(f => ({
      key: f.id, label: f.label, mark: famMark(f.id, f.logo, f.label),
      dot: f.ready ? "ok" : "", cur: f.is_current, go: () => openFamily(f.id) }));
    // what you deployed yourself leads, the way it leads the grid
    const deps = (MSTATE && MSTATE.deployments) || [];
    if (deps.length) rows.unshift({ key: "selfhost", label: "Self-hosted", mark: hostMark(),
      dot: "run", cur: deps.some(d => d.in_use), go: () => openFamily("selfhost") });
    return rows;
  }
  if (v === "sessions") return (PROJECTS || []).slice(0, 5).map(p => ({
    // the directory is what you recognise a project by; a prompt-derived
    // title is the fallback for the ones that are only a session folder
    key: p.digest, label: (p.name && !UUIDISH.test(p.name) ? p.name : p.title) || p.digest.slice(0, 8),
    icon: "folder", count: p.session_count, cur: curProject && curProject.digest === p.digest,
    go: () => { showTab("sessions"); selectProject(p.digest); } }));
  if (v === "deploy") return (DEPLOY.deployments || []).filter(d => d.is_live).slice(0, 5).map(d => ({
    key: d.id, label: d.name || d.model || d.id, icon: "deploy", dot: "run",
    go: () => showTab("deploy") }));
  // Activity's children are the states its own filter already has. A state
  // with nothing in it is not listed: an empty row is a promise of something
  // to look at that isn't there.
  if (v === "activity") {
    const c = (OVERVIEW && OVERVIEW.counts_7d) || {};
    return [["running", "Running", "run"], ["done", "Done", "ok"], ["error", "Failed", "bad"]]
      .filter(([k]) => (c[k] || 0) > 0)
      .map(([k, label, dot]) => ({
        key: k, label, dot, count: c[k], cur: ACT.filter === k,
        go: () => { ACT.filter = k; showTab("activity"); if (ACT.act) renderActivityPage(); } }));
  }
  return [];
}
let railFamReq = false;
function paintSubs() {
  if (curView === "models" && !(FAMS || []).length && !railFamReq) {
    railFamReq = true;
    api("/api/providers").then(g => { FAMS = g.families || []; paintSubs(); })
      .catch(() => { railFamReq = false; });
  }
  VIEWS.forEach(v => {
    const box = document.getElementById("sub-" + v); if (!box) return;
    const btn = document.querySelector('#nav button[data-v="' + v + '"]');
    const rows = v === curView ? subRows(v) : [];
    const open = rows.length > 0 && SUB_OPEN;
    box.classList.toggle("on", open);
    if (btn) { btn.classList.toggle("sub-open", open); btn.classList.toggle("has-sub", v === curView && rows.length > 0); }
    if (!open) { box.innerHTML = ""; return; }
    box.innerHTML = "";
    rows.forEach(r => {
      const b = el("button","nsr" + (r.cur ? " on" : ""));
      if (r.mark) b.append(r.mark); else if (r.icon) b.append(icon(r.icon));
      b.append(el("span","nl", r.label));
      if (r.count != null) b.append(el("span","nc", String(r.count)));
      if (r.dot) b.append(el("span","dot2 " + r.dot));
      b.title = r.label;
      b.onclick = r.go;
      box.append(b);
    });
  });
}
// The bar says where you are in the same words the rail does, plus whatever
// the page has narrowed to — the project you picked, the family you filtered.
// The bar says where you are. A page on its own is one word; a page you have
// narrowed is a trail, and a trail you can walk back up — the chevron drops
// the last step, which is the step you took to get here.
//
// `extra` is either a plain summary of the page (a string, which is not a
// level and gets no back affordance) or the levels below it, each with the
// thing to run to leave it.
function setCrumb(extra) {
  const c = document.getElementById("crumb"); if (!c) return;
  c.innerHTML = "";
  const levels = Array.isArray(extra) ? extra.filter(Boolean) : [];
  if (levels.length) {
    const back = el("button","crumb-b", "\u2039");
    back.title = "back to " + (levels.length > 1 ? levels[levels.length - 2].label : (PAGE_NAMES[curView] || curView));
    back.setAttribute("aria-label", back.title);
    const up = levels.length > 1 ? levels[levels.length - 2].go : () => showTab(curView);
    back.onclick = () => { if (up) up(); };
    c.append(back);
  }
  c.append(icon(curView));
  const home = el("b", null, PAGE_NAMES[curView] || curView);
  c.append(home);
  levels.forEach((lv, i) => {
    c.append(el("span","sep", "\u203a"));
    if (i === levels.length - 1) { c.append(el("span","cs", lv.label)); return; }
    const b = el("button","crumb-l", lv.label);
    b.onclick = () => { if (lv.go) lv.go(); };
    c.append(b);
  });
  if (!levels.length && extra) { c.append(el("span","sep","/")); c.append(el("span","cs", extra)); }
}
// The trail for whatever the page has been narrowed to. Only a narrowing you
// can actually undo becomes a level: a breadcrumb whose back button does
// nothing is furniture.
const ACT_LABEL = { running: "Running", done: "Done", error: "Failed",
                    workflows: "Workflows", jobs: "Jobs" };
function refreshCrumb() {
  if (curView === "models" && MODEL_TAB && MODEL_TAB !== "all") {
    const lab = MODEL_TAB === "local" ? "Local" : (TAB_LABEL_FOR[MODEL_TAB] || MODEL_TAB);
    return setCrumb([{ label: lab, go: () => openFamily("all") }]);
  }
  if (curView === "activity" && ACT.filter !== "all") {
    return setCrumb([{ label: ACT_LABEL[ACT.filter] || ACT.filter,
                       go: () => { ACT.filter = "all"; renderActivityPage(); refreshCrumb(); } }]);
  }
  if (curView === "sessions" && curProject) {
    const pn = (curProject.name && !UUIDISH.test(curProject.name) ? curProject.name : curProject.title)
      || curProject.digest.slice(0, 8);
    const levels = [{ label: pn, go: () => { curSession = null; selectProject(curProject.digest); } }];
    const sr = curSession && (SESSIONS || []).find(x => x.session_id === curSession);
    if (sr) levels.push({ label: (sr.display_title && !UUIDISH.test(sr.display_title))
                                 ? sr.display_title : "session · " + sr.session_id.slice(0, 8), go: null });
    return setCrumb(levels);
  }
  setCrumb();
}
function openFamily(id) {
  const want = "models" + (id === "all" ? "" : "/" + (TAB_SLUG[id] || id));
  MODEL_TAB = id;
  if (location.hash !== "#" + want) location.hash = want;
  showTab("models");
}
function showTab(name) {
  revealGroup(name);         // never leave the page you asked for folded away
  const b = document.querySelector('#nav button[data-v="' + name + '"]');
  if (!b) return;
  const same = curView === name, prev = curView;
  curView = name;
  const DOCS = ["skills", "memory"];
  document.getElementById("shell").classList.toggle("flow", DOCS.includes(name));
  // the two document pages share one editor and its element ids: only the
  // page on screen keeps its DOM (a pending edit is saved first)
  if (!same && [name, prev].some(v => DOCS.includes(v))) {
    flushSkillSave();
    DOCS.filter(v => v !== name).forEach(v => { const p = document.getElementById(v + "pad"); if (p) p.innerHTML = ""; });
  }
  if (!same) SUB_OPEN = true;        // moving to a page opens its rows
  document.querySelectorAll("#nav button").forEach(x => x.classList.toggle("on", x === b));
  document.querySelectorAll(".view").forEach(v => v.classList.toggle("on", v.id === name));
  hideModal();                       // a sheet must never outlive its page
  refreshCrumb(); paintSubs();
  // a sub-route (#models/claude) survives a re-selection of its own tab
  const base = location.hash.slice(1).split("/")[0];
  if (base !== name) location.hash = name;  // fires hashchange; guarded below
  if (name === "home") loadHome();
  if (name === "models") loadModels();
  if (name === "activity") loadActivity();
  if (name === "deploy") loadDeploy();
  if (name === "skills") loadSkills();
  if (name === "memory") loadMemory();
  if (name === "mcp") loadMcp();
  if (name === "config") loadConfig();
}
document.getElementById("nav").addEventListener("click", e => {
  const g = e.target.closest("button.ng");
  if (g) { toggleGroup(g.parentElement); return; }
  const b = e.target.closest("button"); if (!b || !b.dataset.v) return;
  // the caret on the page you are already on folds its rows away instead of
  // reloading the page under you
  if (b.dataset.v === curView && e.target.closest(".ncar")) { SUB_OPEN = !SUB_OPEN; paintSubs(); return; }
  showTab(b.dataset.v);
  closeDrawer();                     // on a phone the rail is over the page
});
// ---- groups fold, and stay folded. Which sections you care about is a
// property of how you work, not of this visit. ----
const GROUP_KEY = "mantis-nav-shut";
function shutGroups() {
  try { return new Set((localStorage.getItem(GROUP_KEY) || "").split(",").filter(Boolean)); }
  catch (e) { return new Set(); }
}
function applyGroups() {
  const shut = shutGroups();
  document.querySelectorAll(".ngrp").forEach(g => {
    const off = shut.has(g.dataset.g);
    g.classList.toggle("shut", off);
    const h = g.querySelector("button.ng");
    if (h) h.setAttribute("aria-expanded", off ? "false" : "true");
  });
}
function toggleGroup(g) {
  const shut = shutGroups();
  if (shut.has(g.dataset.g)) shut.delete(g.dataset.g); else shut.add(g.dataset.g);
  try { localStorage.setItem(GROUP_KEY, [...shut].join(",")); } catch (e) { /* private window */ }
  applyGroups();
}
// A page you navigate to must never be hidden inside a folded group: opening
// it is the honest answer to "where did my page go".
function revealGroup(v) {
  const b = document.querySelector('#nav .ngi > button[data-v="' + v + '"]');
  const g = b && b.closest(".ngrp");
  if (g && g.classList.contains("shut")) toggleGroup(g);
}
applyGroups();
// ---- the drawer: below 1000px the rail is over the page, not beside it ----
const DRAWER = window.matchMedia("(max-width: 1000px)");
function closeDrawer() {
  document.body.dataset.drawer = "";
  const h = document.getElementById("ham");
  if (h) h.setAttribute("aria-expanded", "false");
}
function openDrawer() {
  document.body.dataset.drawer = "on";
  const h = document.getElementById("ham");
  if (h) h.setAttribute("aria-expanded", "true");
  const first = document.querySelector("#nav .ngi > button");
  if (first) first.focus();
}
document.getElementById("ham").onclick = () =>
  (document.body.dataset.drawer === "on" ? closeDrawer() : openDrawer());
document.getElementById("scrim").onclick = closeDrawer;
if (DRAWER.addEventListener) DRAWER.addEventListener("change", closeDrawer);
// ---- up and down walk the rail, wherever the focus already is in it ----
document.getElementById("nav").addEventListener("keydown", e => {
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
  const rows = [...document.querySelectorAll("#nav .ngi > button, #nav .nsub.on .nsr")]
    .filter(b => b.offsetParent !== null);
  if (!rows.length) return;
  const i = rows.indexOf(document.activeElement);
  const next = rows[Math.max(0, Math.min(rows.length - 1, (i < 0 ? 0 : i + (e.key === "ArrowDown" ? 1 : -1))))];
  if (next) { next.focus(); e.preventDefault(); }
});
// ---- collapse: marks only. Remembered, because it is a size preference for
// this screen, not a mode you should have to re-choose every visit. ----
const RAIL_KEY = "mantis-rail";
// The same breakpoint the drawer uses: below it the rail is not a column at
// all, so "folded" has nothing to mean.
const NARROW = window.matchMedia("(max-width: 1000px)");
function applyRail(min, remember) {
  document.body.dataset.rail = min ? "min" : "";
  const t = document.getElementById("railtog");
  if (t) { t.innerHTML = min ? "&#187;" : "&#171;"; t.title = (min ? "expand" : "collapse") + " the rail  (⌘\\)"; }
  if (remember !== false) { try { localStorage.setItem(RAIL_KEY, min ? "min" : ""); } catch (e) { /* private window */ } }
}
const railPref = () => { try { return localStorage.getItem(RAIL_KEY) === "min"; } catch (e) { return false; } };
// Below the breakpoint the rail becomes a drawer, which is always full width;
// the folded choice is remembered and handed back the moment there is a column
// to fold again.
function railFit() { applyRail(!NARROW.matches && railPref(), false); }
if (NARROW.addEventListener) NARROW.addEventListener("change", railFit);
function toggleRail() {
  const min = document.body.dataset.rail !== "min";
  applyRail(min, true);
}
document.getElementById("railtog").onclick = toggleRail;
document.getElementById("rail").addEventListener("click", e => {
  // collapsed, the brand is the only thing wide enough to click back open
  if (document.body.dataset.rail === "min" && e.target.closest(".brand")) toggleRail();
});
railFit();
paintIcons();
// Keyboard: 1–8 jump between pages (the tabs show each key), `g` then a
// letter does the same by name (g o · g s · g a · g m · g d · g p · g k · g c), `/`
// drops into whatever search the current page has, ⌘K opens the palette.
let chord = null, chordT = null;
const CHORDS = { o: "home", s: "sessions", a: "activity", m: "models", d: "deploy", p: "mcp", k: "skills", y: "memory", c: "config" };
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
  if ((e.metaKey || e.ctrlKey) && e.key === "\\") { e.preventDefault(); toggleRail(); return; }
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
  const i = "123456789".indexOf(e.key);
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
    if (curView === "deploy") await syncDeploy(false);
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
    const org = hashOrg() || "all";
    if (org !== DEPLOY.org) { DEPLOY.org = org; renderOrgPills(); paintModels(); }
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
// A READING: a number you compare against the same number on the card beside
// this one. Tabular, so the digits line up down the column; the label under it
// rather than beside it, so the value is what the eye lands on first.
// ---- dated snapshots -------------------------------------------------------
// OpenAI ships gpt-5.4 AND gpt-5.4-2026-03-05, gpt-4o AND three dated 4o's:
// the alias and the pin it currently points at. Listing both as peers doubled
// the grid with cards that are, today, the same model — you scan past six
// near-identical gpt-4o tiles to reach anything else.
//
// A model is a snapshot of another ONLY when stripping a trailing date leaves
// an id THE SAME PROVIDER also serves. That test is what keeps Anthropic's
// claude-opus-4-5-20251101 a model in its own right — there is no undated
// claude-opus-4-5 to fold it under — and it leaves ids that merely contain
// digits, like grok-4.20-0309-non-reasoning or kimi-k2-0905-preview, alone.
const SNAP_RE = /^(.+?)-(\d{4}-\d{2}-\d{2}|\d{8})$/;
const snapKey = a => a.model + "\u0000" + a.pid;
function foldSnapshots(rows) {
  const byKey = new Map();
  rows.forEach(r => byKey.set(snapKey(r), r));
  rows.forEach(r => {
    const mt = SNAP_RE.exec(r.model);
    if (!mt) return;
    const base = byKey.get(mt[1] + "\u0000" + r.pid);
    if (!base || base === r) return;
    r.snapOf = snapKey(base);
    (base.snaps = base.snaps || []).push(r);
  });
  // each base is immediately followed by its own snapshots, so opening one
  // grows the grid in place instead of scattering cards down the page
  const out = [];
  rows.forEach(r => {
    if (r.snapOf) return;
    out.push(r);
    (r.snaps || []).forEach(sn => out.push(sn));
  });
  return out;
}
const stat = (v, label, cls) => {
  const w = el("div","mstat" + (cls ? " " + cls : ""));
  w.append(el("b", null, String(v)), el("span", null, label));
  return w;
};
const pill = (v, label, cls) => { const p = el("span","pill" + (cls ? " " + cls : "")); p.append(el("b", null, String(v))); if (label) p.append(document.createTextNode(label)); return p; };
async function loadProjects() {
  const { projects } = await api("/api/projects");
  PROJECTS = projects;
  paintSubs();
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
      const t = el("div","t", title); card.append(t);
      // the path is how you tell two same-named projects apart — on hover
      card.title = pr.path || pr.digest;
      const m = el("div","mmeta");
      m.append(el("span", null, pr.session_count + " session" + (pr.session_count===1?"":"s")),
               el("span", null, ago(pr.last_activity)));
      if (pr.usd_est != null) m.append(el("span", null, "≈" + fmtUsd(pr.usd_est)));
      else if (pr.tokens_est) m.append(el("span", null, "≈" + fmtTok(pr.tokens_est) + " tokens"));
      card.append(m);
      card.onclick = () => selectProject(pr.digest);
      return card;
    });
  // an empty middle pane saying "pick a project" is a click the page can make
  if (!curProject && projects.length && window.innerWidth > 1000) selectProject(projects[0].digest);
}
function selectProject(digest) {
  const pr = PROJECTS.find(p => p.digest === digest); if (!pr) return;
  curProject = pr; curSession = null;
  paintSubs(); refreshCrumb();
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
      const m = el("div","mmeta");
      m.append(el("span", null, (s.message_count || 0) + " messages"), el("span", null, ago(s.modified_at)));
      if (s.usd_est != null) m.append(el("span", null, "≈" + fmtUsd(s.usd_est)));
      else if (s.tokens_est) m.append(el("span", null, "≈" + fmtTok(s.tokens_est) + " tokens"));
      card.append(m);
      card.title = s.session_id + (s.model ? " · priced as " + s.model : "");
      card.onclick = () => selectSession(s.session_id);
      return card;
    });
  applySessionFilter();
}
function selectSession(sid) {
  const s = SESSIONS.find(x => x.session_id === sid); if (!s || !curProject) return;
  curSession = sid;
  refreshCrumb();
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
// Hub org ids are slugs. "zai-org" title-cased is "Zai-Org" and "ifm" is not
// a company at all — an org we do not know keeps its slug UNMODIFIED.
const ORG_NAMES = __ORGNAMES__;
const orgName = o => ORG_NAMES[String(o || "").toLowerCase()] || String(o || "");
// An org's mark for a model card: the JSON set, else the same-origin Hub
// avatar proxy (lazy, never blocks the card; 204 → the letter shows), else
// the letter. Only same-origin URLs are ever requested.
function orgMark(org) {
  const w = el("span","omark");
  const key = (org || "").toLowerCase();
  const m = ORG_MARKS[key];
  w.textContent = (org || "?").slice(0, 1).toUpperCase();
  w.classList.add("letter");
  if (m && m.svg) {
    w.textContent = ""; w.classList.remove("letter"); w.innerHTML = markSvg(m);
    if (m.tint) w.style.color = m.tint;
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
// Every mark renders in the same neutral square at the same optical weight.
// Each vendor drew on its own grid — measured across the set, a mark's ink
// covers 50%–100% of its declared viewBox — so the page swaps in the `fit`
// viewBox computed from that mark's measured ink bounds (serve_logos.py).
// The square stays neutral for all of them: the glyph carries the vendor's
// colour, and one card glowing while its neighbour doesn't reads as broken.
function markSvg(m) {
  let svg = String(m.svg).replace(/<svg\b/, '<svg preserveAspectRatio="xMidYMid meet"');
  if (m.fit) svg = svg.replace(/viewBox="[^"]*"/, 'viewBox="' + m.fit + '"');
  return svg;
}
function fillMark(w, pid, label) {
  const m = MARKS[pid];
  if (pid === "selfhost" && !(m && m.svg)) {
    // your own server has no vendor: it wears the Self-hosted family's glyph
    w.innerHTML = HOST_SVG; w.classList.add("mm-hostmark");
  } else if (m && m.svg) {
    w.innerHTML = markSvg(m);
    if (m.tint) w.style.color = m.tint;
  } else {
    w.textContent = (label || pid || "?").slice(0, 1).toUpperCase();
    w.classList.add("letter");
  }
  return w;
}
function bigMark(pid, label) { return fillMark(el("span","bigmark"), pid, label); }
function providerMark(pid, label) { return fillMark(el("span","mark2"), pid, label); }
// A family's mark. Four of the five ARE a vendor and wear its glyph. "Open
// models" is not: the catalogue names Ollama as its logo, which is right for
// a runtime and wrong for a family of thirty vendors, so it gets a neutral
// four-block glyph drawn on the same pixel grid as the card motif. "All" is
// not a family at all and gets nothing, the way the Deploy page's All pill
// gets nothing.
function famMark(fid, logo, label) {
  return fid === "oss" ? anyMark() : fid === "selfhost" ? hostMark() : providerMark(logo || fid, label);
}
// Yours, on your GPUs: three stacked server blades on the same pixel grid,
// in the accent — the one family on the page you are paying by the hour for.
const HOST_SVG = '<svg viewBox="0 0 13 13" shape-rendering="crispEdges" aria-hidden="true">' +
  '<rect x="0" y="0" width="13" height="3"/><rect x="0" y="5" width="13" height="3"/>' +
  '<rect x="0" y="10" width="13" height="3"/></svg>';
function hostMark() {
  const w = el("span","mark2 mm-hostmark");
  w.innerHTML = HOST_SVG;
  return w;
}
// Not a vendor: four blocks, 6px on a 7px pitch, 13px of ink — the same ink
// every real mark is normalised to — in neutral ink rather than a colour.
function anyMark() {
  const w = el("span","mark2 mm-anymark");
  w.innerHTML = '<svg viewBox="0 0 13 13" shape-rendering="crispEdges" aria-hidden="true">' +
    '<rect x="0" y="0" width="6" height="6"/><rect x="7" y="0" width="6" height="6"/>' +
    '<rect x="0" y="7" width="6" height="6"/><rect x="7" y="7" width="6" height="6"/></svg>';
  return w;
}

// ---- provider setup: every way to authenticate each family ----------------
// One card per family; opening one reveals its methods as selectable rows.
// Several methods can be configured at once — exactly one is active, and
// switching is a single click. Values only ever travel inward: what comes
// back is env var names and the contract's masked hints.
const AUTH = { families: [], open: null, method: {}, latency: {}, group: null, drop: null, reflow: null };
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
  // a method just changed under an open Connect sheet: repaint it in place,
  // and let the model grid pick up what is now usable
  if (CONNECT.fam && connectOpen()) { paintConnect(); CONNECT.dirty = true; }
  fillLockStrips();
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
    // "Self-hosted endpoint" says "endpoint" twice over: the card's whole body
    // is a URL field. The shorter name fits the card without truncating.
    const lbl = mm.label === "Self-hosted endpoint" ? "Self-hosted" : mm.label;
    out.push({ key: "oss/" + mm.id, family: "oss", label: lbl, full: mm.label, logo: mm.id, methods: [mm],
               active: mm.status.active ? mm.id : null, ui: "oss", kind: "method" });
  });
  return out;
}
// "4 models · claude-opus-5" / "validated 118ms" / "key saved" — label and
// value in the order they're spoken, the value carrying the weight.
function provMeta(e) {
  const provs = MSTATE.providers || [];
  if (e.kind === "method") return provs.find(x => x.id === e.logo) || null;
  return provs.find(x => x.family === e.ui) || null;
}
function renderAuthCards(box, r) {
  if (AUTH.drop) { AUTH.drop(); AUTH.drop = null; }
  if (AUTH.reflow) { window.removeEventListener("resize", AUTH.reflow); AUTH.reflow = null; }
  box.innerHTML = "";
  if (r.ok === false && !(r.families || []).length) {
    const b = el("div","banner"); const t = el("div","sp");
    t.innerHTML = "<b>Provider setup isn't available:</b> " + esc(r.error || "unknown error");
    b.append(t); box.append(b); return;
  }
  const entries = authEntries();
  // deep link: /?openprov=<provider>#models opens that card straight away
  const want = new URLSearchParams(location.search).get("openprov");
  if (want && AUTH.open == null && entries.some(x => x.key === want)) AUTH.open = want;
  // ONE predicate decides both the Connected group and the tally, so the
  // header can never disagree with the cards again. The old count asked
  // "has an active auth method", which misses a provider that is current
  // from the environment and carries no method of its own.
  const isConnected = e => ["cur", "rdy"].includes(cardState(e).cls);
  // Current first, then Ready; sort is stable, so within a state the cards
  // keep the catalogue's order.
  const conn = entries.filter(isConnected)
    .sort((a, b) => (cardState(a).cls === "cur" ? 0 : 1) - (cardState(b).cls === "cur" ? 0 : 1));
  const connected = conn.length;
  const prog = el("div","setup");
  const ph = el("div","setup-h");
  ph.append(el("span","setup-n", connected + " of " + entries.length + " connected"));
  // The folded strip is written from HERE, by the same predicate that draws
  // the grid, so the summary and the thing it summarises can never disagree.
  // It also decides its own initial state: nothing connected means setup is
  // the job, so it opens itself.
  const sum = document.getElementById("provsum");
  if (sum) {
    sum.innerHTML = "";
    const marks = el("span","provsum-m");
    conn.slice(0, 6).forEach(e => { const mk = providerMark(e.logo || e.family, e.label); mk.title = e.label; marks.append(mk); });
    if (connected) sum.append(marks);
    const txt = el("span");
    txt.append(el("b", null, connected ? String(connected) + " connected" : "Nothing connected yet"));
    txt.append(document.createTextNode(connected
      ? " \u00b7 " + conn.slice(0, 3).map(e => e.label).join(", ") +
        (connected > 3 ? " and " + (connected - 3) + " more" : "")
      : " \u00b7 add a key and this machine can run models"));
    sum.append(txt);
    sum.append(el("span","provsum-x", "Manage providers \u2192"));
    if (AUTH.setProv) {
      // Setup opens itself when it IS the job (nothing connected), when a
      // deep link asked for a particular provider — a ?openprov= that landed
      // inside a folded section would silently do nothing — or when ?prov=open
      // asks for the section itself. Otherwise it keeps whatever you set.
      const qp = new URLSearchParams(location.search);
      const forced = !connected || !!AUTH.open || qp.get("prov") === "open";
      AUTH.setProv(forced || document.getElementById("auth-cards").classList.contains("on"));
    }
  }
  ph.append(el("span","setup-s", connected
    ? "Add another to switch between them mid-session."
    : "Connect one and mantis is ready to run."));
  prog.append(ph);
  // The progress bar is gone: the Connected group below IS the filled part of
  // it, at full size and with names on it. Drawing the same ratio twice made
  // the smaller, wordless copy the redundant one.
  prog.append(el("div","setup-note",
    "Keys are written to ~/.mantis-agent (chmod 600) on this machine and are only ever shown masked."));
  box.append(prog);
  // Connected providers leave their family group and gather at the top: what
  // you can use right now is one block, and the families below are the menu
  // of what you have not set up. A provider is in exactly one group.
  const groups = [["Connected", conn],
                  ["First-party", entries.filter(e => e.kind === "family" && !isConnected(e))],
                  ["Open-source & self-host", entries.filter(e => e.kind === "method" && !isConnected(e))]];
  const was = AUTH.group, now = {};
  groups.forEach(([label, list]) => list.forEach(e => { now[e.key] = label; }));
  groups.forEach(([label, list]) => {
    if (!list.length) return;                 // an empty group shows no label
    box.append(el("div","auth-glabel", label));
    const grid = el("div","auth-grid");
    list.forEach(e => {
      const c = authCard(e);
      // connecting moves a card between groups; a short fade is enough to
      // show it landed somewhere new without animating a flight path
      if (was && was[e.key] && was[e.key] !== label) c.classList.add("ac-moved");
      grid.append(c);
    });
    box.append(grid);
  });
  AUTH.group = now;
  // the panel is a sibling of the grids, positioned against its own card
  const oe = entries.find(x => x.key === AUTH.open);
  if (!oe) return;
  const card = document.getElementById("auth-" + oe.key.replace("/", "-"));
  const pan = authCard(oe, true);
  box.append(pan);
  if (card) placeAuthPanel(box, card, pan);
  // focus lands on the first FIELD if there is one — a querySelector over
  // "input, button" would hand it to Close, which comes first in the markup
  // but is the last thing anyone opening a card wants to press
  const first = pan.querySelector("input, textarea, select")
    || pan.querySelector(".ac-seg.on, .ap-form button, button:not(.ac-act)");
  setTimeout(() => { if (first) first.focus(); else { pan.tabIndex = -1; pan.focus(); } }, 0);
  // click outside, Escape, or opening another card — one panel at a time
  const away = ev => { if (!pan.contains(ev.target) && !(card && card.contains(ev.target))) closeAuthPanel(); };
  const esc = ev => { if (ev.key === "Escape") { ev.stopPropagation(); closeAuthPanel(); } };
  setTimeout(() => document.addEventListener("mousedown", away), 0);
  pan.addEventListener("keydown", esc);
  document.addEventListener("keydown", esc);
  AUTH.drop = () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  const reflow = () => { if (card) placeAuthPanel(box, card, pan); };
  window.addEventListener("resize", reflow);
  AUTH.reflow = reflow;
}
// A vendor's own colour, for the marks drawn in full colour whose logo
// carries no single tint. Everything else takes its hex from the logo set.
// It appears in exactly one place on a card: the mark's square.
const VENDOR_TINT = { anthropic: "#d97757", gemini: "#3186ff", hf: "#ffd21e" };
function vendorTint(pid) {
  const t = VENDOR_TINT[pid] || ((MARKS[pid] || {}).tint || "");
  return /^#/.test(t) ? t : null;
}
// Which provider actually backs the model the SDK will use. Having an active
// auth method is a DIFFERENT fact — several providers can be ready at once,
// but only one serves `model=` — so the two are never conflated.
function isCurrentProvider(e) {
  const provs = MSTATE.providers || [];
  const host = MSTATE.hosting || {};
  if (e.kind === "method") {
    if (e.logo === "ollama") return host.kind === "local";
    if (e.logo === "selfhost") return host.kind === "selfhost";
    const p = provs.find(x => x.id === e.logo);
    return !!(p && p.is_current);
  }
  return provs.some(p => p.family === e.ui && p.is_current);
}
// Four states, in descending order of what they let you do. Exactly one card
// can be Current; any number can be Ready.
function cardState(e) {
  if (isCurrentProvider(e)) return { cls: "cur", badge: "Current", tone: "cur",
    hint: "serving the model mantis will use" };
  if (e.active) return { cls: "rdy", badge: "Ready", tone: "rdy",
    hint: "connected — pick one of its models to use it" };
  if ((e.methods || []).some(m => m.status.configured)) return { cls: "idle", badge: "Not active", tone: "idle",
    hint: "credentials saved, but no method is active" };
  return { cls: "off", badge: "Not connected", tone: "off", hint: "no credential saved yet" };
}
// One provider, collapsed to what you need at a glance: its mark, its name,
// its state, and the one action that changes it. Everything else — how it
// authenticates, the fields, the endpoint, the models it serves — appears
// when you open it. The surface stays neutral in both themes; the only
// colour is the vendor's mark and, for the provider in use, a thin rail.
// ---- the floating panel ---------------------------------------------------
// Opening a provider used to expand it inside its grid cell, which left a
// hole beside it and shoved every later row down. The expansion is now a
// panel layered ABOVE the grid, anchored to the card it came from, so the
// grid's geometry is identical open or closed — provable by measuring a card
// in another row before and after.
function openAuthPanel(key) {
  AUTH.open = key;
  renderAuthCards(document.getElementById("auth-cards"), { ok: true, families: AUTH.families });
}
function closeAuthPanel() {
  const back = AUTH.open;
  AUTH.open = null;
  renderAuthCards(document.getElementById("auth-cards"), { ok: true, families: AUTH.families });
  // focus goes back to the card it came from, not to the top of the document
  const c = back && document.getElementById("auth-" + back.replace("/", "-"));
  const b = c && c.querySelector(".ac-act");
  if (b) b.focus();
}
// Place the panel over its card: same left edge, same width, growing down.
// If it would run off the bottom it grows upward instead, and if it cannot
// fit either way it keeps its own scroll rather than escaping the viewport.
function placeAuthPanel(box, card, pan) {
  const M = 12;
  const b = box.getBoundingClientRect(), a = card.getBoundingClientRect();
  pan.style.left = (a.left - b.left) + "px";
  pan.style.width = a.width + "px";
  pan.style.maxHeight = "";
  const h = pan.offsetHeight, vh = window.innerHeight;
  let top = a.top - b.top;                       // grow down from the card top
  if (a.top + h > vh - M) {
    const up = a.bottom - h;                     // grow up from the card foot
    if (up >= M) top = up - b.top;
    else { top = M - b.top; pan.style.maxHeight = (vh - 2 * M) + "px"; }
  }
  pan.style.top = top + "px";
}
// Two shapes from one description: the 63px card that lives in the grid, and
// — with `panel` — the same card expanded, which is drawn ABOVE the grid so
// opening one never moves another. The grid's geometry is fixed for good.
function authCard(e, panel) {
  const meta = provMeta(e);
  const st8 = cardState(e);
  const open = AUTH.open === e.key;
  const card = el("div","acard " + st8.cls + (panel ? " open ac-panel" : open ? " ac-under" : ""));
  card.id = (panel ? "authp-" : "auth-") + e.key.replace("/", "-");
  const head = el("div","ac-h");
  head.append(bigMark(e.logo || e.family, e.label));
  const ht = el("div","ft");
  // "Qwen (DashScope)" is a name plus its vendor. Printed as one string it
  // truncates from the right and you lose the name — "Qwen (DashSco…". Split
  // it so the stem always survives and only the vendor degrades.
  const nm = el("div","fn");
  const par = /^(.+?)\s*\(([^()]+)\)\s*$/.exec(e.label);
  if (par) {
    nm.append(el("span","ac-nm", par[1]));
    nm.append(el("span","ac-nv", par[2]));
    // only the vendor half can truncate, so only it earns a tooltip
    nm.title = e.label;
  } else nm.textContent = e.label;
  ht.append(nm);
  // what this provider is FOR, not what state it's in — the badge says that
  const am0 = e.methods.find(x => x.id === e.active);
  const only = (e.methods[0] || {}).label || "";
  // never the card's own name again: an open-source card IS its method, so
  // there is nothing to add on that line
  const via = am0 ? "via " + am0.label
    : (e.methods || []).some(m => m.status.configured) ? "credentials saved"
    : (only && only !== e.label && only !== e.full ? only : "");
  // An open multi-method card shows the toggle right below, with the live
  // method checked. Saying it again here is the same fact twice.
  if (via && !(open && e.methods.length > 1)) ht.append(el("div","ac-via", via));
  head.append(ht);
  const stw = el("span","ac-st " + st8.cls);
  stw.append(el("span","ac-stg"));
  // an element, not a text node: only an element can be counter-skewed back
  // upright inside the slanted Current tag
  stw.append(el("span","ac-stl", st8.badge));
  stw.title = st8.hint;
  head.append(stw);
  const act = btn(open ? "Close" : st8.cls === "off" ? "Connect" : "Manage", "gho", ev => {
    ev.stopPropagation();
    if (open) closeAuthPanel(); else openAuthPanel(e.key);
  });
  act.className = "b gho ac-act";
  head.append(act);
  head.onclick = () => act.click();
  card.append(head);
  if (!panel) return card;

  // ---- opened: how it authenticates, then the fields, then what it serves
  const body = el("div","ac-body");
  let sel = AUTH.method[e.key] || e.active || (e.methods.find(x => x.recommended) || e.methods[0] || {}).id;
  const draw = () => {
    body.innerHTML = "";
    const m = e.methods.find(x => x.id === sel) || e.methods[0];
    if (!m) return;
    // never restate the card's own name in its body
    const desc = (m.description || "").replace(new RegExp("^" + e.label.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\s*[—·:-]?\\s*", "i"), "").trim();
    if (desc) { const d = el("div","ac-d", desc); d.title = desc; body.append(d); }
    body.append(authMethodForm({ label: e.label, logo: e.logo, active: e.active }, m));
    const bits = el("div","ac-meta");
    const ep = (meta && meta.base_url) || m.backend || "";
    if (ep) {
      const tpl = /[{}]/.test(String(ep));
      bits.append(metaBit("endpoint", tpl ? "the URL you set above" : String(ep).replace(/^https?:\/\//, ""), null, !tpl));
    }
    if (e.active) {
      const cur = (MSTATE.current || {}).model;
      const n = meta ? (meta.models || []).length : 0;
      if (n) bits.append(metaBit("serves", n + " model" + (n === 1 ? "" : "s"), (meta.models || []).includes(cur) ? cur : null));
      const lat = AUTH.latency[e.key];
      if (lat != null) bits.append(metaBit("validated", lat + "ms"));
      const src = m.status.source;
      if (src) bits.append(metaBit("key", src === "cli" ? "detected" : src === "env" ? "from env" : src));
    }
    if (bits.childElementCount) body.append(bits);
    if (e.active && meta && (meta.models || []).length) {
      const foot = el("div","ac-models");
      const chips = el("div","chips clamp");
      const cur = (MSTATE.current || {}).model;
      meta.models.forEach(x => {
        const c = el("span","chip clk" + (x === cur ? " cur" : ""), x);
        c.onclick = ev => { ev.stopPropagation(); useModel(x, meta.base_url); };
        chips.append(c);
      });
      foot.append(chips);
      const hidden = Math.max(0, meta.models.length - 2);
      if (hidden) {
        const more = el("button","chip more", "+" + hidden + " more");
        more.onclick = ev => { ev.stopPropagation(); chips.classList.toggle("clamp");
          more.textContent = chips.classList.contains("clamp") ? "+" + hidden + " more" : "Show fewer"; };
        foot.append(more);
      }
      body.append(foot);
    }
  };
  const drawFade = () => { body.classList.add("fade"); draw(); requestAnimationFrame(() => body.classList.remove("fade")); };

  if (e.methods.length > 1) {
    // One track of equal segments: every way in is visible and its readiness
    // with it, so nobody has to open a menu to learn what is set up.
    const seg = el("div","ac-types"); seg.setAttribute("role", "tablist");
    seg.setAttribute("aria-label", e.label + " authentication method");
    const segs = [];
    e.methods.forEach(m => {
      const on = m.id === sel;
      const c = el("button","ac-seg" + (on ? " on" : ""));
      c.setAttribute("role", "tab");
      c.setAttribute("aria-selected", on ? "true" : "false");
      c.tabIndex = on ? 0 : -1;
      if (m.status.active) c.append(el("span","ac-mktick", "✓"));
      else if (m.status.configured) c.append(el("span","ac-mkdot"));
      c.append(el("span","ac-lb", m.label));
      c.title = m.description || m.label;
      const pick = () => {
        sel = m.id; AUTH.method[e.key] = m.id;
        segs.forEach(x => {
          const isOn = x === c;
          x.classList.toggle("on", isOn);
          x.setAttribute("aria-selected", isOn ? "true" : "false");
          x.tabIndex = isOn ? 0 : -1;
        });
        drawFade();
      };
      c.onclick = ev => { ev.stopPropagation(); pick(); };
      c.onkeydown = ev => {
        const i3 = segs.indexOf(c);
        let k = null;
        if (ev.key === "ArrowRight" || ev.key === "ArrowDown") k = (i3 + 1) % segs.length;
        else if (ev.key === "ArrowLeft" || ev.key === "ArrowUp") k = (i3 - 1 + segs.length) % segs.length;
        else if (ev.key === "Home") k = 0;
        else if (ev.key === "End") k = segs.length - 1;
        else if (ev.key === " " || ev.key === "Enter") { pick(); ev.preventDefault(); return; }
        if (k == null) return;
        ev.preventDefault(); segs[k].focus(); segs[k].click();
      };
      segs.push(c); seg.append(c);
    });
    card.append(seg);
  }
  card.append(body);
  draw();
  return card;
}
function metaBit(label, value, sub, mono) {
  const w = el("span","ac-mb");
  if (label) w.append(document.createTextNode(label + " "));
  w.append(el(mono ? "code" : "b", null, String(value)));
  if (sub) { const t = el("i", null, sub); t.title = sub; w.append(t); }
  return w;
}
// One method's fields + its action. OAuth swaps Save for a two-step sign-in.
function authMethodForm(d, m) {
  const f = el("div","ap-form");
  const out = el("div");
  if (m.cli_login) { f.append(cliLoginFlow(d, m, out), out); return f; }
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
  // credentials already on this machine: the one thing to do is use them, so
  // the override fields wait behind a link instead of filling the panel
  const ambient = m.kind === "cloud" && m.status.source === "cli";
  if (ambient)
    f.append(el("div","ap-note", "Found on this machine — " + (m.status.hint || "no fields to fill")));
  if ((m.fields || []).length) {
    if (ambient) {
      grid.style.display = "none";
      const alt = el("button","cn-more", "Use different credentials");
      alt.onclick = e => { e.stopPropagation(); grid.style.display = ""; alt.remove(); };
      f.append(alt);
    }
    f.append(grid);
  }
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
      if (v.ok && v.latency_ms != null) AUTH.latency[m.family === "oss" ? "oss/" + m.id : m.family] = v.latency_ms;
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
// A login another CLI already holds (Codex's ChatGPT sign-in): nothing to type
// here — use it when it's found, otherwise say the one command that makes it.
function cliLoginFlow(d, m, out) {
  const w = el("div");
  const step = el("div","ap-note", m.status.hint || "");
  step.style.display = step.textContent ? "" : "none";
  w.append(step);
  const acts = el("div","ap-acts");
  if (m.status.configured) {
    const use = btn(m.status.active ? "Test" : "Use this", "pri", async () => {
      use.disabled = true; out.innerHTML = "";
      try {
        if (!m.status.active) {
          const r = await post("/api/auth/set", { family: m.family, method: m.id, values: {} });
          if (!r.ok) { out.append(probeBox(false, r.message || errText(r))); return; }
        }
        const v = await post("/api/auth/validate", { family: m.family, method: m.id });
        out.append(probeBox(v.ok, v.ok
          ? "Connected" + (v.latency_ms != null ? " · " + v.latency_ms + "ms" : "") + ((v.models || []).length ? " · " + v.models.slice(0, 3).join(", ") : "")
          : (v.message || errText(v))));
        loadAuthFamilies(document.getElementById("auth-cards")); loadOverview();
      } catch (e) { out.append(probeBox(false, e.message)); }
      finally { use.disabled = false; }
    });
    acts.append(use);
  } else {
    const cmd = el("code", null, m.cli_login);
    acts.append(document.createTextNode("Run "), cmd, document.createTextNode(" in a terminal, then reopen this."));
  }
  if (m.docs_url) { const dl = extLink("ac-doc", "↗", m.docs_url); dl.title = "Provider docs"; acts.append(dl); }
  w.append(acts);
  return w;
}
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
// A locked model lands on the Connect sheet: every way this family can be
// reached, not just a key field. Claude alone has five — a subscription, an
// API key, Bedrock, Vertex, Azure — and the one you already have credentials
// for is the one that opens first.
const CONNECT = { fam: null, sel: null, want: null, dirty: false };
const connectOpen = () => !!document.getElementById("modal").className &&
  !!document.querySelector("#sheet .cn-h");
// the maker of the pipe, where it is one — the cloud a method goes through
const METHOD_ORG = { bedrock: "amazon", vertex: "google", azure: "microsoft", azure_openai: "microsoft" };
const METHOD_BLURB = {
  oauth: "Use your Claude Pro or Max plan — sign in once, no key to manage.",
  chatgpt: "Use the ChatGPT plan the Codex CLI is signed in with — no key to manage.",
  api_key: "Pay per token with a key from the provider's console.",
  bedrock: "Bill through your AWS account, with your own IAM and region.",
  vertex: "Bill through Google Cloud, in your own project and region.",
  azure: "Bill through Azure AI Foundry on your own endpoint.",
  azure_openai: "Bill through your Azure OpenAI resource.",
};
function methodMark(f, m) {
  const org = METHOD_ORG[m.id];
  if (org) return orgMark(org);
  // an open-model provider is its own vendor and wears its own mark
  const own = MARKS[m.id] || m.id === "selfhost" || m.id === "ollama";
  return fillMark(el("span","omark"), own ? m.id : (f.logo || f.family), own ? m.label : f.label);
}
function methodRank(f, m) {
  return m.id === CONNECT.want ? -1 : m.status.active ? 0 : m.status.configured ? 1 : m.kind === "oauth" ? 2 : m.id === f.recommended ? 3 : 4;
}
// How each method is said in a sentence ("your Claude plan, an API key,
// Bedrock…"), as opposed to its row title in the sheet.
const METHOD_SAY = { oauth: "your Claude plan", chatgpt: "your ChatGPT plan", api_key: "an API key", bedrock: "Bedrock", vertex: "Vertex",
                     azure: "Azure", azure_openai: "Azure OpenAI" };
const orList = xs => xs.length < 2 ? xs.join("") : xs.slice(0, -1).join(", ") + " or " + xs[xs.length - 1];
// The strip over a family that isn't connected: every way in, in one line,
// and — when this machine already holds credentials for one of them — that
// one, named, because then connecting is a single click.
function fillLockStrips() {
  document.querySelectorAll(".mm-lock").forEach(w => {
    const fid = w.dataset.fam;
    const f = (AUTH.families || []).find(x => x.ui_family === fid || x.family === fid);
    const label = (f && f.label) || TAB_LABEL_FOR[fid] || fid;
    const ms = (f && f.methods) || [];
    w.innerHTML = "";
    // the family's own mark; the clouds you could go through are the sheet's
    // business — a row of other vendors' logos here read as a sponsor strip
    const marks = el("span","ml-marks");
    marks.append(fillMark(el("span","omark"), (f && f.logo) || fid, label));
    const t = el("div","ml-t");
    t.append(el("div","ml-h", label + " isn\u2019t connected"));
    const d = el("div","ml-d");
    const found = ms.find(m => m.status && m.status.configured && !m.status.active);
    if (found) {
      d.append(el("b", null, found.label + " credentials found"),
               document.createTextNode(" on this machine \u2014 nothing to paste, and every model below is ready."));
    } else {
      d.textContent = ms.length > 1
        ? "Connect with " + orList(ms.map(m => METHOD_SAY[m.id] || m.label)) + "."
        : "Add a key and every model below is ready.";
    }
    t.append(d);
    const go = btn(found ? "Use " + found.label.replace(/^Amazon |^Google /, "") : "Connect " + label, "pri",
                   () => unlockFamily(fid, found ? found.id : null));
    w.append(marks, t, go);
  });
}
async function openConnectSheet(uiFam, want) {
  // from Overview the methods have not been fetched yet — fetch, then open
  if (!(AUTH.families || []).length) {
    try { AUTH.families = (await api("/api/auth/families")).families || []; } catch (e) { /* toast below */ }
  }
  const f = AUTH.families.find(x => x.ui_family === uiFam || x.family === uiFam);
  if (!f) { toast("no setup for that family yet", true); return; }
  // a card for one provider inside a many-provider family (DeepSeek under
  // Open models) opens on that provider's own method, listed first
  CONNECT.want = (f.methods || []).some(m => m.id === want) ? want : null;
  CONNECT.all = false;
  const ms = (f.methods || []).slice().sort((a, b) => methodRank(f, a) - methodRank(f, b));
  CONNECT.fam = f.family; CONNECT.dirty = false;
  CONNECT.sel = (ms[0] || {}).id || null;
  paintConnect();
  showModal(true);
}
function paintConnect() {
  const f = AUTH.families.find(x => x.family === CONNECT.fam); if (!f) return;
  const s = document.getElementById("sheet");
  // what the last save/test said survives the repaint that follows it
  const kept = s.querySelector(".cn-row.on .probe");
  s.innerHTML = "";
  // Open models is eleven separate vendors, not five ways into one: a
  // DeepSeek card asks about DeepSeek, and the rest wait behind one link
  const one = f.family === "oss" && CONNECT.want && !CONNECT.all
    ? (f.methods || []).find(m => m.id === CONNECT.want) : null;
  const h = el("div","cn-h");
  h.append(one ? methodMark(f, one) : fillMark(el("span","bigmark"), f.logo || f.family, f.label));
  if (one) h.firstChild.classList.add("bigmark");
  const ht = el("div");
  ht.append(el("div","fn", "Connect " + (one ? one.label : f.label)));
  const n = (f.methods || []).length;
  ht.append(el("div","fd", one ? "One key, and every " + one.label + " model on this page is ready to use."
    : f.family === "oss" ? n + " providers \u2014 each one its own key, or your own machine."
    : n > 1 ? n + " ways in \u2014 pick the one you already pay for. One is active at a time; switch whenever."
    : "One way in."));
  h.append(ht);
  s.append(h);
  const list = el("div","cn-list");
  const rows = one ? [one] : (f.methods || []).slice().sort((a, b) => methodRank(f, a) - methodRank(f, b));
  rows.forEach(m => {
    const on = m.id === CONNECT.sel;
    const row = el("div","cn-row" + (on ? " on" : "") + (m.status.active ? " act" : ""));
    const top = el("button","cn-top");
    top.setAttribute("aria-expanded", on ? "true" : "false");
    top.append(methodMark(f, m));
    const tt = el("span","cn-tt");
    tt.append(el("span","cn-l", m.label));
    tt.append(el("span","cn-d", METHOD_BLURB[m.id] || m.description || ""));
    top.append(tt);
    const tag = m.status.active ? ["ok", "Active"] : m.status.configured ? ["found", "Credentials found"]
              : m.kind === "oauth" ? ["rec", "No key needed"] : m.id === f.recommended ? ["rec", "Recommended"] : null;
    if (tag) top.append(el("span","cn-tag " + tag[0], tag[1]));
    top.onclick = () => { CONNECT.sel = on ? null : m.id; paintConnect(); };
    row.append(top);
    if (on) {
      const body = el("div","cn-body");
      body.append(authMethodForm(f, m));
      if (kept) body.append(kept);
      row.append(body);
    }
    list.append(row);
  });
  s.append(list);
  if (one) {
    const more = el("button","cn-more", "Other open-model providers \u2192");
    more.onclick = () => { CONNECT.all = true; CONNECT.sel = null; paintConnect(); };
    s.append(more);
  }
}
function unlockFamily(uiFam, want) {
  openConnectSheet(uiFam, want);
}

// ---- models & hosting ----
// Context windows read as "200k", not "200000" — the unit people actually say.
// 1,048,576 is "1m", not "1.0m" — a window reads the way it is marketed
const fmtCtx = (n) => n >= 1000000 ? String(+(n/1000000).toFixed(1)) + "m"
                    : n >= 1000 ? Math.round(n/1000) + "k" : String(n);
// The selected family tab on the Models page, mirrored in the hash as
// #models/claude so a refresh or a shared link lands on the same tab. The
// URL carries the name people say; the code carries the catalog's family id.
const TAB_SLUG = { anthropic: "claude", google: "gemini", xai: "grok", oss: "open", selfhost: "self-hosted" };
// the words the tabs print, so the trail says a family the same way the tab does
const TAB_LABEL_FOR = { openai: "OpenAI", anthropic: "Claude", google: "Gemini",
                        xai: "Grok", oss: "Open models", selfhost: "Self-hosted" };
const TAB_FAM = Object.fromEntries(Object.entries(TAB_SLUG).map(([k, v]) => [v, k]));
const tabFromHash = () => { const [t, sub] = location.hash.slice(1).split("/"); return t === "models" && sub ? (TAB_FAM[sub] || sub) : "all"; };
let MODEL_TAB = tabFromHash();
// Which question the grid is answering. "family" is whose it is; the other
// two are orderings computed from tables this machine actually has.
let MODEL_SORT = "family";
let applyModelFilter = null;
let resort = () => {};

// ---- what you are running ------------------------------------------------
// The first thing on the page, because it is the first thing you came to find
// out. Everything here is read off state this machine already has: the id and
// the endpoint from the catalog, the window / price / capabilities from the
// SDK's own tables, the route from the provider that serves it.
//
// Reachability is the one claim that is NOT free. A saved key is not an
// answering endpoint, and a green dot for "configured" is a lie the moment
// the key expires — so until something actually checks, this says it has not
// been checked and offers the check.
const REACH = {};                       // model id -> what the last check found
function nowRunning(pad, m) {
  const cur = (m.current || {}).model;
  const card = el("div","nowcard"); card.id = "nowcard";
  if (!cur) {
    card.append(el("div","now-none",
      "No model is set on this machine yet. Pick one below and it becomes the one mantis runs."));
    pad.append(card);
    return card;
  }
  const provs = m.providers || [];
  const prov = provs.find(pv => (pv.models || []).includes(cur));
  const oll = m.ollama || {};
  const local = ((oll.models || []).some(o => o.name === cur));
  const info = (m.model_info || {})[cur] || {};
  const price = info.price || (prov ? (prov.prices || {})[cur] : null);

  // your own deployment: the model's org wears the mark, and the route is
  // where it runs and on what, not a provider you rent tokens from
  const dep = (m.deployments || []).find(d => d.in_use);
  const top = el("div","now-top");
  top.append(dep ? orgMark(_orgOf(cur)) : fillMark(el("span","bigmark"), local ? "ollama" : (prov ? prov.id : ""), cur));
  const t = el("div","now-t");
  const id = el("div","now-id", cur); id.title = cur; t.append(id);
  // the route in, said once: who serves it and how mantis authenticates there
  const via = [];
  if (dep) via.push("self-hosted on " + (PROV_SHORT[dep.provider] || dep.provider) + (dep.gpu ? " \u00b7 " + gpuName(dep.gpu) : ""));
  else if (local) via.push("local \u00b7 Ollama");
  else if (prov) via.push("via " + (prov.label || prov.id));
  if (prov && !local && !dep) {
    const how = prov.auth === "oauth" ? "subscription" : prov.auth === "env" ? "API key"
              : prov.key_source === "cli" ? "detected credentials"
              : prov.key_source ? "API key" : null;
    if (how) via.push(how);
  }
  const ep = (m.current || {}).backend;
  if (ep) via.push(String(ep).replace(/^https?:\/\//, ""));
  const v = el("div","now-via", via.join(" \u00b7 ")); v.title = v.textContent; t.append(v);
  top.append(t);
  // the tag is a corner tag on the card, not an item in the action lane
  const st = el("span","ac-st cur cornertag");
  st.append(el("span","ac-stg"), el("span","ac-stl", "Current"));
  st.title = "the model mantis runs right now";
  card.append(st);
  card.classList.add("hastag");
  const acts = el("div","now-a");
  // Switching back to what you were running a minute ago is the commonest
  // switch there is, and it belongs HERE — beside what you are running — not
  // as a permanent row of pills over the grid competing with the filters.
  const browse = () => {
    const g = document.getElementById("mm-grid-top");
    if (g) g.scrollIntoView({ behavior: "smooth", block: "start" });
    const i = document.querySelector("#modelspad .filters .find input");
    if (i) setTimeout(() => i.focus(), 220);
  };
  acts.append(popMenu("Switch", () => {
    const out = [];
    const recents = (m.recent || [])
      .map(id => (MM_ROWS || []).find(a => a.model === id)).filter(Boolean).slice(0, 5);
    if (recents.length) {
      out.push({ head: "Recently used" });
      recents.forEach(a => out.push({
        label: a.model, mark: fillMark(el("span","omark"), a.pid, a.label),
        side: a.enabled ? null : "not connected",
        title: a.enabled ? "switch to " + a.model + " \u00b7 " + a.label : a.label + " is not connected",
        run: () => a.enabled ? useModel(a.model, a.backend) : unlockFamily(a.fam),
      }));
      out.push({ head: "Everything else" });
    }
    out.push({ label: "Browse all models", run: browse });
    return out;
  }, { title: "switch back, or browse everything" }));
  top.append(acts);
  card.append(top);

  // the facts, in the same pills the model cards use
  const facts = el("div","now-facts");
  if (info.ctx) {
    const c = pill(fmtCtx(info.ctx), " context");
    if (info.ctx_learned) { c.title = "ceiling learned from the endpoint: " + fmtCtx(info.ctx_learned);
      c.querySelector("b").textContent += "*"; }
    facts.append(c);
  }
  if (dep) {
    const r = depRate(dep);
    const q = pill(fmtRate(r), r != null ? " GPU" : " rate not quoted", "mono");
    q.title = "billed by " + (PROV_SHORT[dep.provider] || dep.provider) + " while a replica is up \u2014 not per token";
    facts.append(q);
  } else facts.append(pricePill(price));
  if (info.tools) { const c = el("span","cap","tools"); c.title = "native tool calling"; facts.append(c); }
  if (info.effort) { const c = el("span","cap","effort"); c.title = "reasoning-effort control"; facts.append(c); }
  if (info.thinking) { const c = el("span","cap","thinks"); c.title = "emits reasoning"; facts.append(c); }
  if (!info.ctx && !info.tools && !info.effort && !info.thinking) {
    const q = pill("no capability row", "");
    q.title = "this id is not in the SDK's capability table — mantis will still call it";
    facts.append(q);
  }
  card.append(facts);
  card.append(reachRow(cur, prov, local));
  pad.append(card);
  return card;
}
// Says only what is known. Three states: never checked (and here is the
// button), checked and answering (with how long ago and how fast), checked
// and it did not (with what came back).
function reachRow(cur, prov, local) {
  const row = el("div","now-reach"); row.id = "now-reach";
  const r = REACH[cur];
  if (!r) {
    row.append(el("span", null, "Reachability not checked this session."));
    const b = btn("Check now", "gho", () => checkReach(cur, prov, local));
    b.id = "now-check";
    row.append(b);
    return row;
  }
  if (r.pending) { row.append(el("span", null, "Checking\u2026")); return row; }
  row.classList.add(r.ok ? "ok" : "bad");
  row.append(el("span","dot2 " + (r.ok ? "ok" : "bad")));
  row.append(el("b", null, r.ok ? "Answering" : "Did not answer"));
  const bits = [];
  if (r.ok && r.ms != null) bits.push(r.ms + "ms");
  bits.push("checked " + ago(r.at));
  row.append(el("span", null, "\u00b7 " + bits.join(" \u00b7 ")));
  if (!r.ok && r.error) { const e = el("span", null, "\u00b7 " + r.error); e.title = r.error; row.append(e); }
  const b = btn("Check again", "gho", () => checkReach(cur, prov, local));
  row.append(b);
  return row;
}
async function checkReach(cur, prov, local) {
  const paint = () => {
    const old = document.getElementById("now-reach");
    if (old) old.replaceWith(reachRow(cur, prov, local));
  };
  // A local runtime has no credential to validate: what "reachable" means
  // there is whether the daemon answered, which the page already asked.
  if (local) {
    const oll = (MSTATE || {}).ollama || {};
    REACH[cur] = { ok: !!oll.reachable, at: Date.now() / 1000,
                   error: oll.reachable ? null : "Ollama is not answering" };
    paint(); return;
  }
  const fam = (AUTH.families || []).find(f => (f.providers || []).some(x => x.id === (prov || {}).id))
           || (AUTH.families || []).find(f => f.id === (prov || {}).family);
  const meth = fam && (fam.methods || []).find(x => x.id === fam.active);
  if (!fam || !meth) {
    REACH[cur] = { ok: false, at: Date.now() / 1000, error: "no connected method to check" };
    paint(); return;
  }
  REACH[cur] = { pending: true }; paint();
  let r;
  try { r = await post("/api/auth/validate", { family: fam.id, method: meth.id, model: cur }); }
  catch (e) { r = { ok: false, error: e.message }; }
  REACH[cur] = { ok: !!r.ok, at: Date.now() / 1000, ms: r.latency_ms != null ? r.latency_ms : null,
                 error: r.ok ? null : errText(r) };
  paint();
}

// ---- a small menu ----------------------------------------------------------
// A control whose options would otherwise be a permanent row of pills. The
// trigger states the current choice, so the answer is readable without
// opening it, and the list is fully keyboard-driven: Enter or Space opens it,
// the arrows walk it, Escape closes it and hands focus back to where it came
// from — a menu that swallows focus is worse than the row it replaced.
function popMenu(label, items, opts) {
  opts = opts || {};
  const w = el("div","pmenu-w");
  const b = el("button","pmenu-b");
  const lab = el("span");
  const paintLabel = () => {
    lab.innerHTML = "";
    if (opts.prefix) lab.append(document.createTextNode(opts.prefix + " "));
    lab.append(el("b", null, typeof label === "function" ? label() : label));
  };
  b.append(lab, el("i", null, "\u25be"));
  b.setAttribute("aria-haspopup", "menu");
  b.setAttribute("aria-expanded", "false");
  if (opts.title) b.title = opts.title;
  paintLabel();
  w.append(b);
  let menu = null;
  const close = (refocus) => {
    if (!menu) return;
    menu.remove(); menu = null;
    b.setAttribute("aria-expanded", "false");
    document.removeEventListener("mousedown", away, true);
    if (refocus) b.focus();
  };
  const away = ev => { if (!w.contains(ev.target)) close(false); };
  const open = () => {
    if (menu) { close(true); return; }
    menu = el("div","pmenu" + (opts.align === "left" ? " left" : ""));
    menu.setAttribute("role", "menu");
    const rows = [];
    (typeof items === "function" ? items() : items).forEach(it => {
      if (it.head) { menu.append(el("div","pmenu-h", it.head)); return; }
      const mi = el("button","pmenu-i" + (it.on ? " on" : ""));
      mi.setAttribute("role", "menuitem");
      if (it.mark) mi.append(it.mark);
      mi.append(el("span","pmenu-t", it.label));
      if (it.side) mi.append(el("span","pmenu-s", it.side));
      if (it.title) mi.title = it.title;
      mi.onclick = () => { close(true); if (it.run) it.run(); paintLabel(); };
      menu.append(mi); rows.push(mi);
    });
    menu.addEventListener("keydown", ev => {
      if (ev.key === "Escape") { ev.stopPropagation(); close(true); return; }
      if (ev.key !== "ArrowDown" && ev.key !== "ArrowUp") return;
      const i = rows.indexOf(document.activeElement);
      const n = rows[Math.max(0, Math.min(rows.length - 1, (i < 0 ? 0 : i + (ev.key === "ArrowDown" ? 1 : -1))))];
      if (n) { n.focus(); ev.preventDefault(); }
    });
    w.append(menu);
    b.setAttribute("aria-expanded", "true");
    document.addEventListener("mousedown", away, true);
    if (rows[0]) rows[0].focus();
  };
  b.onclick = open;
  b.onkeydown = ev => {
    if (ev.key === "ArrowDown") { ev.preventDefault(); open(); }
    if (ev.key === "Escape") close(false);
  };
  w.repaint = paintLabel;
  return w;
}

// ---- compare ---------------------------------------------------------------
// Three at most: past that the columns stop being readable and the question
// stops being "which of these", which is what this is for. Nothing here
// scores or ranks — it lays the same facts out in the same order for each one
// and lets the reader compare, because a "best model" number is one this
// machine has no way to compute.
// Every model this page knows about, so the hero can offer them without
// rebuilding the list it already built.
let MM_ROWS = [];

// ---- one model, as a card -------------------------------------------------
// The SAME component the Deploy picker draws — .mcard with its mark square,
// its mono title, its caption and its quiet pills — so the two model surfaces
// are one design, not two. What differs is only the facts: a model you can
// already reach is described by what it costs and what it can do, never by
// how big its weights are.
const price2 = v => v >= 10 ? v.toFixed(0) : v.toFixed(2);
// Price per 1M tokens, in then out. `free` is not a price of zero — it means
// your own hardware, which is a different claim, so it gets its own pill.
function pricePill(p) {
  if (!p) { const q = pill("—", " / 1M"); q.title = "no row in the price table"; return q; }
  if (p.free) { const q = pill("free", "", "acc"); q.title = "your hardware, no API charge"; return q; }
  const q = pill("$" + price2(p.in) + " · $" + price2(p.out), " / 1M", "mono");
  q.title = "$" + p.in + " in · $" + p.out + " out, per 1M tokens" +
    (p.cache_read != null ? " · cache read $" + p.cache_read : "");
  return q;
}
function myModelCard(a, fid, cur, famName) {
  // a deployment is current when mantis points at ITS endpoint; a rented
  // model of the same id is not, even though the names match
  const on = a.dep ? !!a.dep.in_use : (a.model === cur && !MM_SELFCUR);
  const card = el("div","mcard mmcard" + (on ? " on" : "") + (a.enabled ? "" : " locked"));
  const pr = a.price;
  card.dataset.fam = fid;
  card.dataset.model = a.model;
  card.dataset.pid = a.pid;
  card.dataset.state = a.enabled ? "ready" : "locked";
  if (a.local) card.dataset.local = "1";
  card.dataset.free = (pr && pr.free) || a.local ? "1" : "";
  if (a.snapOf) { card.dataset.snapof = a.snapOf; card.dataset.snap = "1"; }
  if (a.dep) card.dataset.dep = a.dep.id;
  card.dataset.q = (a.model + " " + a.label + " " + (famName || fid) + " " + a.pid + " " +
    (a.dep ? "self-hosted selfhost deployed gpu " : "") +
    (a.info.ctx ? Math.round(a.info.ctx/1000) + "k" : "") + (pr && pr.free ? " free" : "") +
    (a.local ? " local ollama" + (a.local.loaded ? " loaded" : "") : "")).toLowerCase();

  // the model id is the title; who serves it is the caption under it
  const head = el("div","mh");
  // self-hosted: the model's maker wears the mark, since the provider only
  // rents the GPU — that part is the caption
  head.append(a.dep ? orgMark(_orgOf(a.model)) : fillMark(el("span","omark"), a.pid, a.label));
  const tt = el("div","mtt");
  const t = el("div","mt", a.model); t.title = a.model; tt.append(t);
  const o = el("div","mo", a.label); o.title = a.label; tt.append(o);
  head.append(tt);
  card.append(head);

  // THE READINGS. What it can hold, and what it charges going in and coming
  // out — the numbers you actually pick a model on, each in the same place on
  // every card so a row can be compared straight across.
  const stats = el("div","mstats");
  const cs = stat(a.info.ctx ? fmtCtx(a.info.ctx) : "—", "context", a.info.ctx ? "" : "mute");
  if (a.info.ctx_learned) {
    cs.querySelector("b").textContent += "*";
    cs.title = "ceiling learned from the endpoint: " + fmtCtx(a.info.ctx_learned);
  }
  stats.append(cs);
  // A card with one price-tile, not two, still fills its row: the tile spans
  // the space the in/out pair would take, so no card ends in a hole.
  if (a.dep) {
    const r = depRate(a.dep);
    const g = stat(fmtRate(r), r != null ? "GPU while running" : "rate not quoted", "span2" + (r != null ? "" : " mute"));
    g.title = "you pay " + (PROV_SHORT[a.dep.provider] || a.dep.provider) + " by the hour for the GPU, not per token";
    stats.append(g);
  } else if (pr && pr.free) {
    const f = stat("Free", a.local ? "runs on your machine" : "no API charge", "acc span2");
    f.title = "your hardware, no API charge";
    stats.append(f);
  } else if (pr) {
    const i = stat("$" + price2(pr.in), "in / 1M");
    const o = stat("$" + price2(pr.out), "out / 1M");
    i.title = "$" + pr.in + " per 1M input tokens"
            + (pr.cache_read != null ? " · cache read $" + pr.cache_read : "");
    o.title = "$" + pr.out + " per 1M output tokens";
    stats.append(i, o);
  } else {
    const q = stat("—", "no price listed", "mute span2"); q.title = "no row in the price table"; stats.append(q);
  }
  card.append(stats);

  // THE QUIET LINE — state, never a capability list. "tools · effort" was on
  // nearly every card in the grid, and a fact true of everything tells you
  // nothing about any one of them; unexplained, it just read as noise. What
  // is left here is only what differs card to card and changes what you do:
  // it is loaded, it needs a key, it has dated pins, or it is one.
  const meta = el("div","mmeta");
  if (a.local && a.local.loaded) { const c = el("span","ok", "loaded"); c.title = "in memory now"; meta.append(c); }
  // readiness is printed ONLY when it is not "ready": every card that can be
  // used already says so with its action, and a grid of "ready" marks would
  // drown the one card that needs a key
  if (a.dep && a.dep.status === "starting") { const z = el("span","amb", "booting"); z.title = "a container is loading the weights \u2014 billing now"; meta.append(z); }
  if (a.dep && a.dep.status === "scaled_to_zero") { const z = el("span", null, "asleep"); z.title = "scaled to zero \u2014 the first request wakes a replica"; meta.append(z); }
  if (!a.enabled && !on) { const w = el("span","amb", "not connected"); w.title = "subscription, API key or your cloud \u2014 connect this family to use it"; meta.append(w); }
  if (meta.childElementCount) card.append(meta);

  // The foot: one action, as a real button. The card is still clickable as a
  // whole — the button's click simply bubbles to it — but a word with an arrow
  // did not look like something you could press. The current model gets none:
  // its Current tag already says it.
  const foot = el("div","mfoot");
  if (!on) {
    const go = el("button","mbtn" + (a.enabled ? "" : " amb"), a.enabled ? "Use model" : "Connect");
    go.title = a.enabled ? "switch mantis to " + a.model : "choose how mantis reaches " + (famName || fid);
    foot.append(go);
  }
  card.append(foot);
  // the current model wears the same slanted tag the provider cards wear, so
  // the two pages mark "in use" in one language
  if (on) {
    const st = el("span","ac-st cur cornertag");
    st.append(el("span","ac-stg"), el("span","ac-stl", "Current"));
    st.title = "the model mantis is using right now";
    card.append(st);
  }
  // `unlock` deep-links into this family's setup with its recommended method
  // already chosen — the same link the locked row used to carry
  card.onclick = () => a.dep ? useSelfhosted(a, card.querySelector(".mbtn"))
    : a.enabled ? useModel(a.model, a.backend) : unlockFamily(fid, a.pid);
  return card;
}
// Switching to your own deployment goes through the same connect job the
// Deploy page uses — it re-checks the endpoint and waits out a cold start —
// and the wait is shown on the card's own button, with a clock.
let MM_SELFCUR = false;
async function useSelfhosted(a, b) {
  if (!b || b.disabled) return;
  const say = t => { b.textContent = t; };
  b.disabled = true; say("Connecting\u2026");
  let r;
  try { r = await post("/api/deploy/connect", { id: a.dep.id, job: true }); } catch (e) { r = { ok: false, error: e.message }; }
  if (!r.ok) { toast(errText(r), true); b.disabled = false; say("Use model"); return; }
  const t0 = Date.now();
  for (;;) {
    await sleep(1000);
    if (!b.isConnected) return;
    let j;
    try { j = await api("/api/deploy/job?" + q({ id: r.job })); } catch (e) { continue; }
    if (j.status === "running") {
      const s = (Date.now() - t0) / 1000;
      say((s > 8 ? "Waking " : "Connecting ") + fmtClock(s));
      continue;
    }
    if (j.status === "done") { toast("\u2713 mantis now runs " + shortId(a.model) + " on your " + gpuName(a.dep.gpu)); loadModels(); return; }
    toast((j.error || "couldn't reach the endpoint") + (j.hint ? " \u2014 " + j.hint : ""), true);
    b.disabled = false; say("Use model");
    return;
  }
}
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
  {
    const served = new Set();
    (m.providers || []).forEach(pv => (pv.models || []).forEach(x => served.add(x)));
    ((m.ollama || {}).models || []).forEach(o => served.add(o.name));
    (m.deployments || []).forEach(d => served.add(d.served_model_name || d.model));
    pageReads(pad, [
      { v: fmt(served.size), k: "models" },
      { v: (m.families || []).length + ((m.deployments || []).length ? 1 : 0), k: "families", opt: 2 },
    ], "What you are running, and what else you could.",
       "Windows and prices come from the SDK's own tables, not from the provider.");
  }

  MSTATE = m;
  paintSubs();
  // ---- what you are running ----------------------------------------------
  nowRunning(pad, m);

  // ---- setup, folded ------------------------------------------------------
  // The provider grid is this page's other altitude: it answers "why can't I
  // use that one", which is a question you ask after the models, not before
  // them. It keeps every part it had — the three groups, the overlay panel,
  // the motif — inside a strip that opens, and it opens itself when nothing
  // is connected, because then setup IS the job.
  const authSec = section(pad, "Providers");
  const sum = el("button","provsum"); sum.id = "provsum";
  const authBox = el("div","provbox"); authBox.id = "auth-cards";
  const setProv = on => {
    authBox.classList.toggle("on", on);
    sum.setAttribute("aria-expanded", on ? "true" : "false");
    const x = sum.querySelector(".provsum-x");
    if (x) x.textContent = on ? "Hide setup" : "Manage providers \u2192";
  };
  sum.onclick = () => setProv(!authBox.classList.contains("on"));
  AUTH.setProv = setProv;
  setProv(false);
  sum.setAttribute("aria-controls", "auth-cards");
  authSec.append(sum, authBox);
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
    model: om.name, label: "local · " + fmtBytes(om.size), pid: "ollama",
    fam: "oss", backend: oll.base_url, enabled: true, info: info[om.name] || {}, local: om,
    price: (info[om.name] || {}).price || { in: 0, out: 0, free: true } }));
  // what you deployed yourself: its own family, first, because it is the one
  // you are paying for by the hour whether you use it or not
  const deps = m.deployments || [];
  MM_SELFCUR = deps.some(d => d.in_use);
  if (deps.length) {
    famOrder.unshift("selfhost"); famLabel.selfhost = "Self-hosted";
    deps.forEach(d => { const id = d.served_model_name || d.model;
      allModels.push({ model: id, label: (PROV_SHORT[d.provider] || d.provider) + " \u00b7 " + gpuName(d.gpu),
        pid: d.provider, fam: "selfhost", backend: d.endpoint_url, enabled: true,
        info: info[id] || {}, price: null, dep: d }); });
  }
  const folded = foldSnapshots(allModels);
  allModels.length = 0; folded.forEach(x => allModels.push(x));
  // the grid's own counts describe the CARDS it shows, so they count the
  // aliases; the snapshots behind them are reached from the alias itself
  const nPrimary = allModels.filter(a => !a.snapOf).length;
  if (allModels.length) {
    const nFam = new Set(allModels.map(a => a.fam)).size;
    const sec = section(pad, "Choose a model", nPrimary + " across " + nFam + " famil" + (nFam===1?"y":"ies"));
    // Family tabs: one pill per family (plus Local for what Ollama has pulled),
    // each carrying its count. "All" keeps the grouped view; a family tab
    // narrows to that family and drops the group headers. The choice lives in
    // the hash (#models/claude) so a refresh or a pasted link keeps it.
    const TAB_LABEL = { oss: "Open models", selfhost: "Self-hosted" };
    const tabs = [{ id: "all", label: "All", n: nPrimary }];
    famOrder.concat([...new Set(allModels.map(a => a.fam))].filter(f => !famOrder.includes(f))).forEach(fid => {
      const n = allModels.filter(a => a.fam === fid && !a.snapOf).length;
      if (n) tabs.push({ id: fid, label: TAB_LABEL[fid] || famLabel[fid] || fid, n, logo: famLogo[fid] });
    });
    const nLocal = allModels.filter(a => a.local).length;
    // Local IS Ollama, so here the llama is the honest mark
    if (nLocal) tabs.push({ id: "local", label: "Local", n: nLocal, logo: "ollama" });
    if (!tabs.some(t => t.id === MODEL_TAB)) MODEL_TAB = "all";
    // Search is the primary act and the family filter narrows what it returns,
    // so the search row is built first and the families sit under it — the
    // same order the Deploy picker already uses.
    const tabRow = el("div","mtabs");
    tabs.forEach(t => {
      const c = el("button","fchip" + (t.id === MODEL_TAB ? " on" : ""));
      // All is every family at once, so it wears no mark — the same choice
      // the Deploy page's All pill makes
      if (t.id !== "all") c.append(famMark(t.id, t.logo, t.label));
      c.append(el("span", null, t.label));
      c.append(el("span","tn2", String(t.n)));
      c.onclick = () => {
        MODEL_TAB = t.id;
        tabRow.querySelectorAll(".fchip").forEach(x => x.classList.toggle("on", x === c));
        const want = "models" + (t.id === "all" ? "" : "/" + (TAB_SLUG[t.id] || t.id));
        if (location.hash !== "#" + want) location.hash = want;
        apply(); refreshCrumb();
      };
      tabRow.append(c);
    });
    const bar = el("div","filters");
    const find = findBox("Filter — gpt, claude, grok, 200k, free, local…  ( / )");
    find.wrap.style.marginBottom = "0"; find.wrap.style.flex = "1";
    bar.append(find.wrap);
    const FILTERS = [["all","all"], ["ready","ready to use"], ["locked","not connected"], ["free","free / local"]];
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
    // Sort: a labelled control that says the current ordering without being
    // opened, rather than three pills that are always on screen to answer a
    // question most people ask once.
    const SORTS = [["family", "By family"], ["cheap", "Cheapest"], ["ctx", "Biggest context"]];
    const sortLabel = () => (SORTS.find(([k]) => k === MODEL_SORT) || SORTS[0])[1];
    const sortBtn = popMenu(sortLabel, () => SORTS.map(([k, lab]) => ({
      label: lab, on: k === MODEL_SORT,
      side: k === "cheap" ? "$ in + out" : k === "ctx" ? "window" : null,
      run: () => { MODEL_SORT = k; resort(); sortBtn.repaint(); },
    })), { prefix: "Sort:", title: "how the grid is ordered" });
    sortBtn.id = "mm-sort";
    bar.append(sortBtn);
    sec.append(bar);
    sec.append(tabRow);

    // One labelled card grid per source, so "Open models · 34 models" reads
    // as a heading over its own cards rather than a stripe in a table.
    const list = el("div","mm-list"); list.id = "mm-grid-top";
    const groups = [];
    famOrder.concat([...new Set(allModels.map(a => a.fam))].filter(f => !famOrder.includes(f))).forEach(fid => {
      const rows = allModels.filter(a => a.fam === fid);
      if (!rows.length) return;
      const fh = el("div","mm-glabel"); fh.dataset.fam = fid;
      fh.append(famMark(fid, famLogo[fid], famLabel[fid] || fid));
      fh.append(document.createTextNode(famLabel[fid] || fid));
      const nr = rows.filter(a => !a.snapOf).length;
      fh.append(el("span","mm-gn", nr + " model" + (nr===1?"":"s")));
      const grid = el("div","mm-grid"); grid.dataset.fam = fid;
      rows.forEach(a => grid.append(myModelCard(a, fid, cur, famLabel[fid] || fid)));
      // a whole family behind one connection says so once, above its cards;
      // Open models is many vendors with a key each, so it never gets one
      let lock = null;
      if (fid !== "oss" && fid !== "selfhost" && rows.every(a => !a.enabled)) {
        lock = el("div","mm-lock"); lock.dataset.fam = fid;
        grid.classList.add("famlock");
      }
      list.append(fh);
      if (lock) list.append(lock);
      list.append(grid);
      groups.push([fh, grid, lock]);
    });
    // The same cards, in one grid, when the question is not "whose is it".
    // They are MOVED, never rebuilt, so a card keeps its band, its tag and
    // its handlers whichever ordering it is under.
    const flatH = el("div","mm-glabel mm-flat"); flatH.id = "mm-flath";
    const flat = el("div","mm-grid mm-flat"); flat.id = "mm-flat";
    list.append(flatH, flat);
    const cards = [];
    groups.forEach(([, g]) => [...g.children].forEach(c => cards.push(c)));
    const keyOf = a => {
      const pr = a.price;
      if (MODEL_SORT === "cheap") return (pr && pr.free) ? 0
        : (pr && pr.in != null) ? pr.in + pr.out : Infinity;      // unpriced sorts last
      if (MODEL_SORT === "ctx") return -(a.info.ctx || 0);        // unknown sorts last
      return 0;
    };
    const byId = {};
    allModels.forEach(a => { byId[a.model + "\u0000" + a.pid] = a; });
    resort = () => {
      const flatOn = MODEL_SORT !== "family";
      groups.forEach(([fh, g, lock]) => { fh.classList.toggle("mm-off", flatOn); g.classList.toggle("mm-off", flatOn);
        if (lock) lock.classList.toggle("mm-off", flatOn); });
      flatH.classList.toggle("on", flatOn); flat.classList.toggle("on", flatOn);
      if (!flatOn) {
        groups.forEach(([, g]) => {
          cards.forEach(c => { if (c.dataset.fam === g.dataset.fam) g.append(c); });
        });
      } else {
        const rows = cards.slice().sort((x, y) => {
          const a = byId[x.dataset.model + "\u0000" + x.dataset.pid];
          const b = byId[y.dataset.model + "\u0000" + y.dataset.pid];
          const d = keyOf(a) - keyOf(b);
          return d || x.dataset.model.localeCompare(y.dataset.model);
        });
        rows.forEach(c => flat.append(c));
        flatH.innerHTML = "";
        flatH.append(document.createTextNode(
          MODEL_SORT === "cheap" ? "Cheapest first" : "Biggest context first"));
        flatH.append(el("span","mm-gn", MODEL_SORT === "cheap"
          ? "by $ in + $ out per 1M \u00b7 free and local first, unpriced last"
          : "by the window the SDK records \u00b7 unknown last"));
      }
      apply();
    };
    sec.append(list);
    MM_ROWS = allModels;      // the hero's Switch menu reads these too
    const apply = () => {
      const q = find.input.value.trim().toLowerCase();
      let shown = 0;
      const perFam = {};
      list.querySelectorAll(".mmcard").forEach(r => {
        const okQ = !q || q.split(/\s+/).every(t => r.dataset.q.includes(t));
        const okF = mode === "all" || (mode === "free" ? !!r.dataset.free : r.dataset.state === mode);
        const okT = MODEL_TAB === "all" || (MODEL_TAB === "local" ? !!r.dataset.local : r.dataset.fam === MODEL_TAB);
        // A dated snapshot is never on the grid by itself — its alias stands
        // for it, with no label saying so. Only a search reaches one, because
        // typing a date has to be able to find it.
        const okS = !r.dataset.snap || !!q;
        const on = okQ && okF && okT && okS;
        r.style.display = on ? "" : "none";
        r.classList.remove("kb");
        if (on) { shown++; perFam[r.dataset.fam] = (perFam[r.dataset.fam] || 0) + 1; }
      });
      // one family selected → the group label is noise; "All" keeps it. A
      // group with nothing left in it takes its grid down too, or the page
      // keeps its gaps.
      groups.forEach(([fh, grid, lock]) => {
        const n = perFam[fh.dataset.fam] || 0;
        fh.style.display = (MODEL_TAB === "all" && n) ? "" : "none";
        grid.style.display = n ? "" : "none";
        if (lock) lock.style.display = n ? "" : "none";
      });
      // a flat ordering has no families to label
      const fh2 = document.getElementById("mm-flath");
      if (fh2) fh2.style.display = (MODEL_SORT !== "family" && shown) ? "" : "none";
      let e = list.querySelector(".find-none");
      if (!shown) { if (!e) { e = emptyState("search", "No model matches",
        "Try another family tab, clear the filter, or self-host something that isn't listed.");
        e.classList.add("find-none"); list.append(e); } }
      else if (e) e.remove();
    };
    applyModelFilter = apply;
    fillLockStrips();
    find.input.oninput = apply;
    resort();                 // lays the cards out, then filters them
    // Keyboard: / focuses (global handler), ↑↓ walk the visible cards, Enter switches to one.
    let kbi = -1;
    const visible = () => [...list.querySelectorAll(".mmcard")].filter(r => r.style.display !== "none");
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
  // the readings line under the title carries the count — a chip beside the
  // title said the same number a second time
  h.append(t);
  if (actions && actions.length) { const a = el("div","page-a"); actions.forEach(x => a.append(x)); h.append(a); }
  pad.append(h);
  if (desc) { const d = el("p","page-d"); if (desc.nodeType) d.append(desc); else d.innerHTML = desc; pad.append(d); }
}
// The readings line: what a page's description used to say in prose, as
// discrete values. `reads` is [{v, k, opt}] — value, label, and how early it
// leaves when the line runs out (1 goes at 1000px, 2 at 1200px, absent means
// it never goes). `clause` is the single short piece of prose allowed, and
// `note` is an honesty footnote that rides as a tooltip rather than as text.
function pageReads(pad, reads, clause, note) {
  const r = el("div","page-r");
  (reads || []).filter(x => x && x.v != null && x.v !== "").forEach(x => {
    const d = el("span","page-rd" + (x.opt ? " opt" + x.opt : ""));
    d.append(el("b", null, String(x.v)));
    if (x.k) d.append(el("span", null, x.k));
    if (x.title) d.title = x.title;
    r.append(d);
  });
  // No sentence under a title: the readings say the situation, and the
  // honesty note rides as the row's own tooltip instead of an ⓘ to hover.
  if (note || clause) r.title = [clause, note].filter(Boolean).join(" — ");
  if (r.childElementCount) pad.append(r);
  return r;
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
// ---- the skills workspace ------------------------------------------------
// A list of pages on the left, the open page on the right — edited in place
// the way a document is, not through a form in a modal. The title and the
// one-line description are typed where they are read; the properties the
// loader cares about sit in a property block under them; the body is blocks
// you click into, with "/" for a new kind of block. Every change saves on
// its own, a moment after you stop typing.
const SKW = { kind: "skill", cur: null, draft: null, blocks: [], raw: false, timer: null, saving: false, dirty: false, status: "" };
const SKILL_TOOLS = ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebFetch", "WebSearch", "Task"];
const slugify = t => (t || "").trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
const skKey = x => x.scope + "/" + x.slug;
async function loadSkills(want) {
  const pad = document.getElementById("skillspad");
  const my = ++skillsReq;
  if (!pad.childElementCount) skeleton(pad);
  let sk;
  try { sk = await api("/api/skills"); }
  catch (e) { if (my !== skillsReq) return; pad.innerHTML = ""; pad.append(el("div","empty","Error: " + e.message)); return; }
  if (my !== skillsReq) return;
  pad.innerHTML = "";
  SKILLS.all = [...(sk.global || []), ...(sk.project || [])];
  SKILLS.tools = sk.tools_seen || [];
  SKILLS.dirs = { global: sk.global_dir, project: sk.project_dir };
  const c = sk.counts || { total: SKILLS.all.length, always: 0, on_demand: 0 };
  pageHead(pad, "Skills", null, null, [btn("New skill", "pri", () => newSkill("global"))]);
  pageReads(pad, [
    { v: c.always || 0, k: "always loaded" },
    { v: c.on_demand || 0, k: "on demand" },
  ], "Playbooks the agent opens when a task matches.",
     "Always-loaded skills go into every prompt; on-demand ones are opened by name.");
  if (!c.total && !SKW.draft) {
    pad.append(emptyState("skill", "No skills yet",
      "A skill is a SKILL.md the agent reads when the task matches — your deploy steps, your review rules.",
      btn("New skill", "pri", () => newSkill("global"))));
    return;
  }
  const wrap = el("div","skx");
  const list = el("div","skx-list"); list.id = "skx-list";
  const doc = el("div","skx-doc"); doc.id = "skx-doc";
  wrap.append(list, doc);
  pad.append(wrap);
  // which page is open: what was asked for, else the hash, else what was open, else the first
  const fromHash = () => { const [, sc, sl] = location.hash.slice(1).split("/"); return sc && sl ? sc + "/" + sl : null; };
  const pick = want || fromHash() || (SKW.cur && skKey(SKW.cur));
  const hit = SKILLS.all.find(x => skKey(x) === pick);
  if (SKW.draft) { paintSkillList(); paintSkillDoc(); }
  else openSkillPage(hit || SKILLS.all[0]);
}
function skillMatchesQ(x) {
  const ql = (SKILLS.q || "").trim().toLowerCase();
  return !ql || ql.split(/\s+/).every(t =>
    (x.name + " " + (x.description || "") + " " + (x.category || "") + " " + (x.tools || []).join(" ") + " " + x.scope)
      .toLowerCase().includes(t));
}
function paintSkillList() {
  const list = document.getElementById("skx-list"); if (!list || curView !== "skills") return;
  const keepFocus = document.activeElement && document.activeElement.closest && document.activeElement.closest(".skx-find");
  list.innerHTML = "";
  const top = el("div","skx-top");
  const find = el("label","skx-find");
  find.append(el("span","skx-fi", "⌕"));
  const i = document.createElement("input"); i.placeholder = "Search skills"; i.value = SKILLS.q || "";
  i.oninput = () => { SKILLS.q = i.value; paintSkillList(); };
  find.append(i);
  const add = el("button","skx-add", "+"); add.title = "New skill"; add.onclick = () => newSkill("global");
  top.append(find, add);
  list.append(top);
  if (keepFocus) setTimeout(() => { i.focus(); i.setSelectionRange(i.value.length, i.value.length); }, 0);
  const curK = SKW.cur ? skKey(SKW.cur) : null;
  [["global", "Global"], ["project", "This project"]].forEach(([sc, lab]) => {
    const rows = SKILLS.all.filter(x => x.scope === sc && skillMatchesQ(x));
    const draftHere = SKW.draft && SKW.draft.scope === sc;
    if (!rows.length && !draftHere && (SKILLS.q || sc === "global")) return;
    const g = el("div","skx-g");
    const gh = el("div","skx-gh"); gh.append(el("span", null, lab), el("span","skx-gn", String(rows.length)));
    const ga = el("button","skx-gadd", "+"); ga.title = "New skill in " + lab.toLowerCase(); ga.onclick = () => newSkill(sc);
    gh.append(ga);
    g.append(gh);
    if (draftHere) {
      const d = el("button","skx-i on");
      d.append(skillGlyph(SKW.draft.name || "untitled"), el("span","skx-in" + (SKW.draft.name ? "" : " ph"), SKW.draft.name || "Untitled"));
      g.append(d);
    }
    rows.forEach(x => {
      const b = el("button","skx-i" + (!SKW.draft && skKey(x) === curK ? " on" : ""));
      b.append(skillGlyph(x.name), el("span","skx-in", x.name));
      if (x.always_load) { const dot = el("span","skx-al"); dot.title = "always loaded"; b.append(dot); }
      b.title = x.description || x.name;
      b.onclick = () => openSkillPage(x);
      g.append(b);
    });
    if (!rows.length && !draftHere) g.append(el("div","skx-none", "Nothing here yet"));
    list.append(g);
  });
}
function flushSkillSave() { if (SKW.dirty) { clearTimeout(SKW.timer); return saveSkillNow(); } return Promise.resolve(); }
async function openSkillPage(x) {
  await flushSkillSave();
  SKW.draft = null; SKW.cur = x || null; SKW.raw = false; SKW.kind = "skill";
  SKW.blocks = x ? splitBlocks(x.body || "") : [];
  if (x) { const h = "skills/" + skKey(x); if (location.hash !== "#" + h) history.replaceState(null, "", "#" + h); }
  paintSkillList(); paintSkillDoc();
}
async function newSkill(scope) {
  await flushSkillSave();
  showTab("skills");
  SKW.cur = null; SKW.raw = false; SKW.kind = "skill";
  SKW.draft = { scope, name: "", description: "", category: "", always_load: false, tools: [], body: "" };
  SKW.blocks = [""];
  if (!document.getElementById("skx-doc")) { await loadSkills(); return; }
  paintSkillList(); paintSkillDoc();
  const t = document.querySelector(".skx-title"); if (t) t.focus();
}
const skDoc = () => SKW.draft || SKW.cur;
function setSkillStatus(s) {
  SKW.status = s;
  const e = document.getElementById("skx-status"); if (!e) return;
  e.textContent = s; e.className = "skx-status" + (s === "Saved" ? " ok" : /n't|taken|failed/i.test(s) ? " bad" : "");
}
function touchSkill() {
  SKW.dirty = true; setSkillStatus("Editing…");
  clearTimeout(SKW.timer);
  SKW.timer = setTimeout(saveSkillNow, 700);
}
async function saveSkillNow() {
  // the Memory page edits on the same surface; its saves go to its own routes
  if (SKW.kind !== "skill") return saveMemNow();
  const d = skDoc(); if (!d || !SKW.dirty) return;
  if (!(d.name || "").trim()) { setSkillStatus("Give it a title to save"); return; }
  if (SKW.saving) { clearTimeout(SKW.timer); SKW.timer = setTimeout(saveSkillNow, 300); return; }
  SKW.saving = true; SKW.dirty = false; setSkillStatus("Saving…");
  const body = SKW.raw ? d.body : joinBlocks(SKW.blocks);
  d.body = body;
  let r;
  try {
    r = await post("/api/skill", { scope: d.scope, name: d.name.trim(), description: d.description || "", body,
      category: d.category || "", always_load: !!d.always_load, tools: d.tools || [], slug: SKW.draft ? undefined : d.slug });
  } catch (e) { r = { ok: false, error: e.message }; }
  SKW.saving = false;
  if (!r.ok) {
    SKW.dirty = true;
    setSkillStatus(r.exists ? "That name is taken — pick another" : "Couldn't save — " + (r.error || "error"));
    return;
  }
  if (SKW.draft) {
    // the draft is a real skill now: it keeps this slug from here on
    const made = { ...SKW.draft, slug: r.slug, path: r.path, raw: "" };
    SKILLS.all.push(made);
    SKW.draft = null; SKW.cur = made;
    const h = "skills/" + skKey(made); history.replaceState(null, "", "#" + h);
    paintSkillList();
    const fp = document.getElementById("skx-file"); if (fp) fp.textContent = made.path;
  } else paintSkillList();
  setSkillStatus(SKW.dirty ? "Editing…" : "Saved");
}

// ---- the page -----------------------------------------------------------
function paintSkillDoc() {
  const doc = document.getElementById("skx-doc"); if (!doc || curView !== "skills") return;
  doc.innerHTML = "";
  const d = skDoc();
  if (!d) { doc.append(el("div","skx-empty", "Pick a skill, or make a new one.")); return; }
  const draft = !!SKW.draft;
  // the bar: where this page lives, whether it is saved, and its menu
  const bar = el("div","skx-bar");
  bar.append(el("span","skx-crumb", (d.scope === "project" ? "This project" : "Global") + " / " + (d.name || "Untitled")));
  bar.append(el("span","sp"));
  const st = el("span","skx-status"); st.id = "skx-status"; bar.append(st);
  const more = popMenu("⋯", () => [
    { label: SKW.raw ? "Edit as blocks" : "Edit as markdown", run: () => toggleSkillRaw() },
    ...(SKW.draft ? [] : [
      { label: "Copy file path", run: () => copyText(skDoc().path, "path") },
      { label: "Delete skill…", run: () => confirmSkillDelete(), title: "removes its folder" },
    ]),
  ], { title: "more" });
  more.classList.add("skx-more");
  bar.append(more);
  doc.append(bar);

  const page = el("div","skx-page");
  page.append(skillGlyph(d.name || "untitled"));
  // title and description are typed where they are read
  const title = el("div","skx-title"); title.contentEditable = "plaintext-only";
  title.dataset.ph = "Untitled"; title.textContent = d.name || "";
  title.oninput = () => {
    d.name = title.textContent.replace(/\n/g, " ");
    const cr = bar.querySelector(".skx-crumb");
    if (cr) cr.textContent = (d.scope === "project" ? "This project" : "Global") + " / " + (d.name || "Untitled");
    const g = page.querySelector(".sglyph"); if (g) g.replaceWith(skillGlyph(d.name || "untitled"));
    touchSkill();
  };
  title.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); desc.focus(); } };
  const desc = el("div","skx-desc"); desc.contentEditable = "plaintext-only";
  desc.dataset.ph = "When should the agent reach for this? One line.";
  desc.textContent = d.description || "";
  desc.oninput = () => { d.description = desc.textContent.replace(/\n/g, " "); touchSkill(); };
  desc.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); editBlock(0, "start"); } };
  page.append(title, desc);
  page.append(skillProps(d, draft));
  const body = el("div","skx-body"); body.id = "skx-body";
  page.append(body);
  doc.append(page);
  paintBlocks();
  setSkillStatus(draft ? (d.name ? SKW.status : "Draft — saves once it has a title") : (SKW.dirty ? "Editing…" : "Saved"));
}
// Notion's property block: a label column, a value you edit in place.
function skillProps(d, draft) {
  const box = el("div","skx-props");
  const row = (ic, label, val) => {
    const r = el("div","skx-pr");
    const l = el("div","skx-pl"); l.append(icon(ic), document.createTextNode(label));
    const v = el("div","skx-pv"); v.append(val);
    r.append(l, v); box.append(r);
  };
  const seg = (opts, cur, set) => {
    const w = el("div","skx-seg");
    opts.forEach(([k, lab, tip]) => {
      const b = el("button", k === cur ? "on" : null, lab);
      if (tip) b.title = tip;
      b.onclick = () => { set(k); w.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); };
      w.append(b);
    });
    return w;
  };
  row("clock", "Loading", seg([[false, "On demand", "opened by name when a task matches"],
                               [true, "Always loaded", "injected into every session's prompt"]],
    !!d.always_load, v => { d.always_load = v; touchSkill(); paintSkillList(); }));
  if (draft) row("home", "Scope", seg([["global", "Global", "every project"], ["project", "This project", "travels with the repo"]],
    d.scope, v => { d.scope = v; paintSkillList(); touchSkill(); }));
  else row("home", "Scope", el("span","skx-ro", d.scope === "project" ? "This project" : "Global"));
  const cat = document.createElement("input"); cat.className = "skx-inp"; cat.placeholder = "Empty";
  cat.value = d.category || "";
  cat.oninput = () => { d.category = cat.value; touchSkill(); };
  row("skills", "Category", cat);
  // tools: the chosen ones as chips, the rest one click away
  const tw = el("div","skx-tools");
  const paintTools = () => {
    tw.innerHTML = "";
    (d.tools || []).forEach(t => {
      const c = el("span","skx-tag", t);
      const x = el("button","skx-tx", "×"); x.title = "remove " + t;
      x.onclick = () => { d.tools = d.tools.filter(y => y !== t); paintTools(); touchSkill(); };
      c.append(x); tw.append(c);
    });
    const all = [...new Set([...SKILL_TOOLS, ...(SKILLS.tools || [])])].filter(t => !(d.tools || []).includes(t));
    if (all.length) {
      const m = popMenu((d.tools || []).length ? "+" : "Any tool · restrict", () => all.map(t => ({
        label: t, run: () => { d.tools = [...(d.tools || []), t]; paintTools(); touchSkill(); } })),
        { title: "restrict which tools this skill may use", align: "left" });
      m.classList.add("skx-tadd");
      tw.append(m);
    }
  };
  paintTools();
  row("tool", "Tools", tw);
  const fp = el("span","skx-file", draft ? ((SKILLS.dirs || {})[d.scope] || "") + "/" + (slugify(d.name) || "…") + "/SKILL.md" : d.path);
  fp.id = "skx-file";
  if (!draft) { fp.title = "copy the path"; fp.onclick = () => copyText(d.path, "path"); }
  row("folder", "File", fp);
  return box;
}
function toggleSkillRaw() {
  const d = skDoc(); if (!d) return;
  if (SKW.raw) { SKW.blocks = splitBlocks(d.body || ""); SKW.raw = false; }
  else { d.body = joinBlocks(SKW.blocks); SKW.raw = true; }
  paintBlocks();
}
function confirmSkillDelete() {
  const d = SKW.cur; if (!d) return;
  const bar = document.querySelector(".skx-bar"); if (!bar) return;
  const c = el("div","skx-confirm");
  c.append(el("span", null, "Delete “" + d.name + "”? Its folder is removed from disk."));
  c.append(el("span","sp"));
  c.append(btn("Delete", "dan", async () => {
    const r = await post("/api/skill/delete", { scope: d.scope, slug: d.slug }).catch(e => ({ ok: false, error: e.message }));
    if (!r.ok) { toast(r.error || "failed", true); return; }
    toast("deleted " + d.name);
    SKW.cur = null; SKW.dirty = false; history.replaceState(null, "", "#skills");
    loadSkills();
  }), btn("Cancel", "gho", () => c.remove()));
  bar.after(c);
}

// ---- blocks ---------------------------------------------------------------
// The body is markdown, cut into the blocks you would see: a paragraph, a
// heading, a list, a fenced code block. A block renders until you click it;
// then it is its own text, in place, at the size it reads at.
function splitBlocks(src) {
  const out = []; let cur = [], fence = false;
  const flush = () => { if (cur.length) { out.push(cur.join("\n")); cur = []; } };
  String(src || "").replace(/\r/g, "").split("\n").forEach(line => {
    if (/^\s*```/.test(line)) {
      if (!fence) { flush(); cur.push(line); fence = true; return; }
      cur.push(line); fence = false; flush(); return;
    }
    if (fence) { cur.push(line); return; }
    if (!line.trim()) { flush(); return; }
    if (/^#{1,6}\s/.test(line) || /^(-{3,}|\*{3,})\s*$/.test(line)) { flush(); out.push(line); return; }
    cur.push(line);
  });
  flush();
  return out.length ? out : [""];
}
const joinBlocks = bs => bs.filter((b, i) => b.trim() || i < bs.length - 1).join("\n\n").replace(/\n{3,}/g, "\n\n").trim();
const blockKind = t => /^```/.test(t) ? "code" : /^# /.test(t) ? "h1" : /^## /.test(t) ? "h2" : /^#{3,6} /.test(t) ? "h3"
  : /^\s*([-*]|\d+\.)\s/.test(t) ? "list" : /^>/.test(t) ? "quote" : "p";
function paintBlocks() {
  const body = document.getElementById("skx-body"); if (!body) return;
  body.innerHTML = "";
  const d = skDoc();
  if (SKW.raw) {
    const ta = el("textarea","skx-raw"); ta.value = d.body || ""; ta.spellcheck = false;
    const grow = () => { ta.style.height = "auto"; ta.style.height = Math.max(240, ta.scrollHeight) + "px"; };
    ta.oninput = () => { d.body = ta.value; grow(); touchSkill(); };
    body.append(ta); setTimeout(grow, 0);
    return;
  }
  SKW.blocks.forEach((t, i) => body.append(blockView(t, i)));
  // the page's tail: clicking under the last block writes a new one
  const tail = el("div","skx-tail");
  tail.onclick = () => {
    const last = SKW.blocks.length - 1;
    if (SKW.blocks[last] && SKW.blocks[last].trim()) { SKW.blocks.push(""); paintBlocks(); editBlock(last + 1, "start"); }
    else editBlock(last, "end");
  };
  body.append(tail);
}
function blockView(t, i) {
  const w = el("div","skx-b k-" + blockKind(t));
  w.dataset.i = String(i);
  const add = el("button","skx-plus", "+"); add.title = "add a block below";
  add.onclick = e => { e.stopPropagation(); SKW.blocks.splice(i + 1, 0, ""); paintBlocks(); editBlock(i + 1, "start"); };
  w.append(add);
  const c = el("div","skx-bc md");
  if (t.trim()) {
    c.innerHTML = md(t);
    // "- [ ] x" is a to-do: a real checkbox, and ticking it edits the markdown
    let n = 0;
    c.querySelectorAll("li").forEach(li => {
      const f = li.firstChild;
      const m = f && f.nodeType === 3 && /^\s*\[( |x|X)\]\s?/.exec(f.textContent);
      if (!m) return;
      const k = n++, done = m[1] !== " ";
      f.textContent = f.textContent.slice(m[0].length);
      const box = el("button","skx-chk" + (done ? " on" : ""), done ? "\u2713" : "");
      box.title = done ? "mark not done" : "mark done";
      box.onclick = e => {
        e.stopPropagation();
        let j = -1;
        SKW.blocks[i] = SKW.blocks[i].replace(/^(\s*(?:[-*]|\d+\.)\s+)\[( |x|X)\]/gm, (all, pre, st) =>
          ++j === k ? pre + (st === " " ? "[x]" : "[ ]") : all);
        touchSkill(); paintBlocks();
      };
      li.classList.add("todo"); if (done) li.classList.add("done");
      // the text in its own span, so a done item strikes its words, not its box
      const tx = el("span","skx-tt"); while (li.firstChild) tx.append(li.firstChild);
      li.append(box, tx);
    });
  }
  else { c.classList.add("ph"); c.textContent = i === 0 && SKW.blocks.length === 1 ? "Write the playbook — steps, commands, gotchas. Type “/” for blocks." : ""; }
  w.append(c);
  w.onclick = e => { if (e.target.closest("a")) return; editBlock(i, "end"); };
  return w;
}
const SLASH = [
  ["Text", "", "p"], ["Heading 1", "# ", "h1"], ["Heading 2", "## ", "h2"], ["Heading 3", "### ", "h3"],
  ["Bulleted list", "- ", "list"], ["Numbered list", "1. ", "list"], ["To-do", "- [ ] ", "list"],
  ["Quote", "> ", "quote"], ["Code", "```\n\n```", "code"], ["Divider", "---", "p"],
];
function editBlock(i, where) {
  const body = document.getElementById("skx-body"); if (!body) return;
  if (i < 0 || i >= SKW.blocks.length) return;
  const w = body.querySelector('.skx-b[data-i="' + i + '"]'); if (!w) return;
  const ta = el("textarea","skx-ta k-" + blockKind(SKW.blocks[i]));
  ta.value = SKW.blocks[i]; ta.rows = 1; ta.spellcheck = true;
  const grow = () => { ta.style.height = "auto"; ta.style.height = ta.scrollHeight + "px"; };
  const w2 = el("div","skx-b editing"); w2.dataset.i = String(i); w2.append(ta);
  w.replaceWith(w2);
  let menu = null, done = false;
  const commit = () => {
    if (done) return; done = true;
    closeSlash();
    if (SKW.blocks[i] !== ta.value) { SKW.blocks[i] = ta.value; touchSkill(); }
    const v = blockView(SKW.blocks[i], i);
    if (w2.isConnected) w2.replaceWith(v);
  };
  const go = (j, at) => { commit(); editBlock(j, at); };
  const closeSlash = () => { if (menu) { menu.remove(); menu = null; } };
  const openSlash = () => {
    closeSlash();
    const qv = ta.value.slice(1).toLowerCase();
    const items = SLASH.filter(([l]) => l.toLowerCase().includes(qv));
    if (!items.length) return;
    menu = el("div","pmenu left skx-slash"); menu.setAttribute("role", "menu");
    menu.append(el("div","pmenu-h", "Blocks"));
    items.forEach(([l, pre], k) => {
      const b = el("button","pmenu-i" + (k === 0 ? " on" : ""));
      b.append(el("span","pmenu-t", l));
      b.onmousedown = e => { e.preventDefault(); choose(pre); };
      menu.append(b);
    });
    w2.append(menu);
  };
  const choose = pre => {
    closeSlash();
    ta.value = pre;
    ta.className = "skx-ta k-" + blockKind(pre);
    const at = pre.startsWith("```") ? 4 : pre.length;
    if (pre === "---") { SKW.blocks[i] = "---"; SKW.blocks.splice(i + 1, 0, ""); done = true; touchSkill(); paintBlocks(); editBlock(i + 1, "start"); return; }
    ta.setSelectionRange(at, at); grow(); touchSkill();
  };
  ta.oninput = () => {
    grow();
    ta.className = "skx-ta k-" + blockKind(ta.value);
    if (/^\/[\w ]*$/.test(ta.value)) openSlash(); else closeSlash();
    SKW.blocks[i] = ta.value; touchSkill();
  };
  ta.onkeydown = e => {
    const v = ta.value, s = ta.selectionStart, en = ta.selectionEnd;
    if (menu && (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter")) {
      const rows = [...menu.querySelectorAll(".pmenu-i")];
      let k = rows.findIndex(r => r.classList.contains("on"));
      if (e.key === "Enter") { e.preventDefault(); rows[k].onmousedown(new MouseEvent("mousedown")); return; }
      k = Math.max(0, Math.min(rows.length - 1, k + (e.key === "ArrowDown" ? 1 : -1)));
      rows.forEach((r, x) => r.classList.toggle("on", x === k)); e.preventDefault(); return;
    }
    if (e.key === "Escape") { e.preventDefault(); if (menu) closeSlash(); else { commit(); } return; }
    if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); SKW.blocks[i] = v; clearTimeout(SKW.timer); SKW.dirty = true; saveSkillNow(); return; }
    const kind = blockKind(v);
    if (e.key === "Enter" && !e.shiftKey && kind !== "code") {
      const lineStart = v.lastIndexOf("\n", s - 1) + 1;
      const line = v.slice(lineStart, v.indexOf("\n", s) < 0 ? v.length : v.indexOf("\n", s));
      const lm = /^(\s*)([-*]|\d+\.)(\s+\[[ xX]\])?\s/.exec(line);
      if (lm && kind === "list") {
        e.preventDefault();
        // an empty item ends the list and starts a paragraph under it
        if (!line.slice(lm[0].length).trim()) {
          ta.value = (v.slice(0, lineStart).replace(/\n$/, "") + v.slice(lineStart + line.length)).trimEnd();
          SKW.blocks[i] = ta.value; SKW.blocks.splice(i + 1, 0, ""); done = true; touchSkill(); paintBlocks(); editBlock(i + 1, "start");
          return;
        }
        const num = /^\d+/.test(lm[2]) ? (parseInt(lm[2], 10) + 1) + "." : lm[2];
        const next = "\n" + lm[1] + num + (lm[3] ? " [ ]" : "") + " ";
        ta.setRangeText(next, s, en, "end"); ta.oninput(); return;
      }
      // Enter splits the block at the caret, the way a document does
      e.preventDefault();
      SKW.blocks[i] = v.slice(0, s).trimEnd();
      SKW.blocks.splice(i + 1, 0, v.slice(en).trimStart());
      done = true; touchSkill(); paintBlocks(); editBlock(i + 1, "start");
      return;
    }
    if (e.key === "Backspace" && s === 0 && en === 0 && i > 0) {
      e.preventDefault();
      const prev = SKW.blocks[i - 1], at = prev.length;
      SKW.blocks[i - 1] = v.trim() ? (prev ? prev + "\n" + v : v) : prev;
      SKW.blocks.splice(i, 1); done = true; touchSkill(); paintBlocks(); editBlock(i - 1, at);
      return;
    }
    if (e.key === "ArrowUp" && v.lastIndexOf("\n", s - 1) < 0 && i > 0) { e.preventDefault(); go(i - 1, "end"); return; }
    if (e.key === "ArrowDown" && v.indexOf("\n", en) < 0 && i < SKW.blocks.length - 1) { e.preventDefault(); go(i + 1, "start"); return; }
  };
  ta.onblur = () => setTimeout(commit, 0);
  // focus NOW, not on the next tick: a fast typist's first keys after Enter
  // otherwise land in the block they just left
  grow(); ta.focus();
  const at = where === "start" ? 0 : typeof where === "number" ? where : ta.value.length;
  ta.setSelectionRange(at, at);
}
// ==========================================================================
// MEMORY — what the agent is told every session, and what it remembers.
// The same split Claude Code makes, on the same document surface as Skills:
//   Instructions — human-written, loaded into every session: your personal
//     MANTIS.md, the repo's AGENTS.md / MANTIS.md / rules, MANTIS.local.md.
//   Memory — what the agent wrote down: the MEMORY.md index it reads at the
//     start of each session, and one file per memory.
// A standard instruction file that doesn't exist yet is listed anyway, and
// typing into it creates it — "where do I put this?" answered by the list.
// ==========================================================================
const MEMW = { data: null, docs: [] };
const MEM_TYPES = [["user", "User", "who you are and how you like to work"],
                   ["feedback", "Feedback", "a correction or a confirmed approach"],
                   ["project", "Project", "ongoing work, goals, constraints"],
                   ["reference", "Reference", "where to find something"]];
const TIER_SAY = { user: "Personal · every project", project: "Project · shared with the team",
                   local: "Project · only you", managed: "Organization" };
let memReq = 0;
async function loadMemory(want) {
  const pad = document.getElementById("memorypad");
  const my = ++memReq;
  if (!pad.childElementCount) skeleton(pad);
  let m;
  try { m = await api("/api/memory"); }
  catch (e) { if (my !== memReq) return; pad.innerHTML = ""; pad.append(el("div","empty","Error: " + e.message)); return; }
  if (my !== memReq) return;
  MEMW.data = m;
  MEMW.docs = memDocs(m);
  pad.innerHTML = "";
  pageHead(pad, "Memory", null, null, [btn("New memory", "pri", () => newMemory())]);
  const loaded = (m.instructions || []).filter(x => x.loaded);
  const kb = loaded.reduce((a, x) => a + x.bytes, 0) + ((m.index || {}).content || "").length;
  pageReads(pad, [
    { v: loaded.length, k: "instruction file" + (loaded.length === 1 ? "" : "s") + " loaded" },
    { v: (m.entries || []).length, k: "memories" },
    { v: "~" + fmt(Math.round(kb / 4)), k: "tokens every session" },
  ], "What the agent is told every session, and what it remembers.",
     "Instructions and the MEMORY.md index are read at the start of every session; each memory is opened when it is relevant.");
  const wrap = el("div","skx");
  const list = el("div","skx-list"); list.id = "skx-list";
  const doc = el("div","skx-doc"); doc.id = "skx-doc";
  wrap.append(list, doc);
  pad.append(wrap);
  const fromHash = () => { const parts = location.hash.slice(1).split("/"); return parts[0] === "memory" && parts[1] ? decodeURIComponent(parts.slice(1).join("/")) : null; };
  const pick = want || fromHash() || (SKW.kind !== "skill" && SKW.cur && SKW.cur.key);
  const first = MEMW.docs.find(d => d.kind === "instr" && d.loaded) || MEMW.docs[0];
  openMemDoc(MEMW.docs.find(d => d.key === pick) || first);
}
function memDocs(m) {
  const out = [];
  (m.instructions || []).forEach(x => out.push({ ...x, key: "i:" + x.id, kind: "instr", name: x.label, body: x.content }));
  const ix = m.index || {};
  out.push({ key: "index", kind: "index", name: "MEMORY.md", path: ix.path, exists: ix.exists, body: ix.content || "", lines: ix.lines });
  (m.entries || []).forEach(e => out.push({ ...e, key: "m:" + e.slug, kind: "entry" }));
  return out;
}
function memMatches(d) {
  const ql = (MEMW.q || "").trim().toLowerCase();
  return !ql || ql.split(/\s+/).every(t => ((d.name || "") + " " + (d.description || "") + " " + (d.type || "") + " " + (d.body || "")).toLowerCase().includes(t));
}
function paintMemList() {
  const list = document.getElementById("skx-list"); if (!list || curView !== "memory") return;
  const keepFocus = document.activeElement && document.activeElement.closest && document.activeElement.closest(".skx-find");
  list.innerHTML = "";
  const top = el("div","skx-top");
  const find = el("label","skx-find");
  find.append(el("span","skx-fi", "⌕"));
  const i = document.createElement("input"); i.placeholder = "Search memory"; i.value = MEMW.q || "";
  i.oninput = () => { MEMW.q = i.value; paintMemList(); };
  find.append(i);
  const add = el("button","skx-add", "+"); add.title = "New memory"; add.onclick = () => newMemory();
  top.append(find, add);
  list.append(top);
  if (keepFocus) setTimeout(() => { i.focus(); i.setSelectionRange(i.value.length, i.value.length); }, 0);
  const curK = SKW.draft ? "draft" : SKW.cur && SKW.cur.key;
  const item = (d, lab, side, cls) => {
    const b = el("button","skx-i mem-i" + (d.key === curK ? " on" : "") + (cls ? " " + cls : ""));
    b.append(memMark(d), el("span","skx-in", lab));
    if (side) b.append(side);
    b.title = d.kind === "instr" ? d.path : (d.description || d.name);
    b.onclick = () => openMemDoc(d);
    return b;
  };
  const group = (title, note) => {
    const g = el("div","skx-g");
    const gh = el("div","skx-gh"); gh.append(el("span", null, title));
    if (note) gh.append(el("span","skx-gn", note));
    g.append(gh); list.append(g);
    return g;
  };
  const ins = MEMW.docs.filter(d => d.kind === "instr" && memMatches(d));
  if (ins.length) {
    const g = group("Instructions", "every session");
    ins.forEach(d => {
      let side = null;
      if (d.loaded) { side = el("span","skx-al"); side.title = "loaded into every session"; }
      else if (!d.exists) { side = el("span","mem-new", "create"); }
      g.append(item(d, d.name, side, d.exists ? "" : "ghost"));
    });
  }
  const ents = MEMW.docs.filter(d => d.kind === "entry" && memMatches(d));
  const ix = MEMW.docs.find(d => d.kind === "index");
  const g2 = group("Memory", String(ents.length));
  const ga = el("button","skx-gadd", "+"); ga.title = "New memory"; ga.onclick = () => newMemory();
  g2.querySelector(".skx-gh").append(ga);
  if (ix && memMatches(ix)) { const s = el("span","mem-side", (ix.lines || 0) + " lines"); g2.append(item(ix, "MEMORY.md", s)); }
  if (SKW.draft && SKW.kind === "entry") {
    const b = el("button","skx-i on"); b.append(skillGlyph(SKW.draft.name || "untitled"),
      el("span","skx-in" + (SKW.draft.name ? "" : " ph"), SKW.draft.name || "Untitled")); g2.append(b);
  }
  ents.forEach(d => g2.append(item(d, d.name, el("span","mem-side", d.type))));
  if (!ents.length && !(SKW.draft && SKW.kind === "entry")) g2.append(el("div","skx-none", MEMW.q ? "No memory matches" : "Nothing remembered yet"));
}
function memMark(d) {
  if (d.kind === "entry") return skillGlyph(d.name || d.slug || "memory");
  const w = el("span","sglyph mem-mk");
  w.append(icon(d.kind === "index" ? "trace" : d.tier === "user" ? "home" : d.tier === "local" ? "key" : "folder"));
  return w;
}
async function openMemDoc(d) {
  await flushSkillSave();
  SKW.draft = null; SKW.cur = d || null; SKW.raw = false; SKW.dirty = false;
  SKW.kind = d ? d.kind : "entry";
  SKW.blocks = d ? splitBlocks(d.body || "") : [""];
  if (d) { const h = "memory/" + encodeURIComponent(d.key); if (location.hash !== "#" + h) history.replaceState(null, "", "#" + h); }
  paintMemList(); paintMemDoc();
}
async function newMemory() {
  await flushSkillSave();
  if (curView !== "memory") { showTab("memory"); await new Promise(r => setTimeout(r, 400)); }
  SKW.cur = null; SKW.raw = false; SKW.kind = "entry"; SKW.dirty = false;
  SKW.draft = { kind: "entry", name: "", description: "", type: "project", body: "" };
  SKW.blocks = [""];
  paintMemList(); paintMemDoc();
  const t = document.querySelector(".skx-title"); if (t) t.focus();
}
function paintMemDoc() {
  const doc = document.getElementById("skx-doc"); if (!doc || curView !== "memory") return;
  doc.innerHTML = "";
  const d = skDoc();
  if (!d) { doc.append(el("div","skx-empty", "Pick a file or a memory.")); return; }
  const entry = d.kind === "entry", draft = !!SKW.draft;
  const ro = d.kind === "instr" && !d.writable;
  const bar = el("div","skx-bar");
  const where = d.kind === "instr" ? "Instructions" : "Memory";
  const crumb = el("span","skx-crumb", where + " / " + (d.name || "Untitled"));
  bar.append(crumb, el("span","sp"));
  const st = el("span","skx-status"); st.id = "skx-status"; bar.append(st);
  const more = popMenu("⋯", () => [
    ...(ro ? [] : [{ label: SKW.raw ? "Edit as blocks" : "Edit as markdown", run: () => toggleSkillRaw() }]),
    ...(skDoc() && skDoc().path ? [{ label: "Copy file path", run: () => copyText(skDoc().path, "path") }] : []),
    // read at open time: a draft becomes a real memory on its first save
    ...(entry && !SKW.draft ? [{ label: "Delete memory…", run: () => confirmMemDelete() }] : []),
  ], { title: "more" });
  more.classList.add("skx-more");
  bar.append(more);
  doc.append(bar);

  const page = el("div","skx-page");
  page.append(memMark(d));
  const title = el("div","skx-title");
  const desc = el("div","skx-desc");
  if (entry) {
    // a memory is named and hooked like a skill: both are typed in place
    title.contentEditable = "plaintext-only"; title.dataset.ph = "Untitled"; title.textContent = d.name || "";
    title.oninput = () => { d.name = title.textContent.replace(/\n/g, " "); crumb.textContent = "Memory / " + (d.name || "Untitled"); touchSkill(); };
    title.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); desc.focus(); } };
    desc.contentEditable = "plaintext-only"; desc.dataset.ph = "One-line hook — how the agent decides this is relevant later.";
    desc.textContent = d.description || "";
    desc.oninput = () => { d.description = desc.textContent.replace(/\n/g, " "); touchSkill(); };
    desc.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); editBlock(0, "start"); } };
  } else {
    title.textContent = d.name;
    desc.textContent = d.kind === "index"
      ? "The index the agent reads at the start of every session — one line per memory, linking to its file."
      : d.about;
  }
  page.append(title, desc);
  page.append(memProps(d, draft));
  if (ro) {
    const body = el("div","skx-body md mem-ro"); body.innerHTML = md(d.body || "*(empty)*"); page.append(body);
  } else {
    const body = el("div","skx-body"); body.id = "skx-body"; page.append(body);
  }
  doc.append(page);
  if (!ro) paintBlocks();
  if (!ro && d.kind === "instr" && !d.exists) {
    const ph = document.querySelector("#skx-body .skx-bc.ph");
    if (ph) ph.textContent = "Empty — start typing and " + d.name + " is created. Type “/” for blocks.";
  }
  setSkillStatus(ro ? "Read-only" : draft ? (d.name ? SKW.status : "Draft — saves once it has a title")
    : d.kind === "instr" && !d.exists ? "Not created yet" : "Saved");
}
function memProps(d, draft) {
  const box = el("div","skx-props");
  const row = (ic, label, val) => {
    const r = el("div","skx-pr");
    const l = el("div","skx-pl"); l.append(icon(ic), document.createTextNode(label));
    const v = el("div","skx-pv"); v.append(val);
    r.append(l, v); box.append(r);
  };
  const say = (t, cls) => el("span","skx-ro" + (cls ? " " + cls : ""), t);
  const tok = b => "~" + fmt(Math.round((b || 0) / 4)) + " tokens";
  if (d.kind === "instr") {
    row("clock", "In context", d.loaded ? say("Loaded every session · " + tok(d.bytes), "ok")
      : d.exists ? say("Not loaded from this folder") : say("Not created yet"));
    row("home", "Scope", say(TIER_SAY[d.tier] || d.tier));
    if (d.imported_by) row("skills", "Imported by", say(d.imported_by));
  } else if (d.kind === "index") {
    const n = (d.body || "").split("\n").filter(x => x.trim()).length;
    row("clock", "In context", say("Loaded every session · " + tok((d.body || "").length), "ok"));
    row("trace", "Lines", say(n + (n > 150 ? " — past ~150 it stops being cheap to load; fold topics into sub-indexes" : ""), n > 150 ? "warn" : ""));
  } else {
    const w = el("div","skx-seg");
    MEM_TYPES.forEach(([k, lab, tip]) => {
      const b = el("button", k === (d.type || "project") ? "on" : null, lab); b.title = tip;
      b.onclick = () => { d.type = k; w.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); touchSkill(); paintMemList(); };
      w.append(b);
    });
    row("skills", "Type", w);
    const ix = (MEMW.docs.find(x => x.kind === "index") || {}).body || "";
    if (!draft) row("trace", "In the index", say(ix.includes(d.slug + ".md") ? "Listed in MEMORY.md" : "Not listed — the agent won't see it", ix.includes(d.slug + ".md") ? "ok" : "warn"));
    else row("trace", "In the index", say("Added to MEMORY.md when it saves"));
  }
  const fp = el("span","skx-file", d.path || (MEMW.data ? MEMW.data.memory_dir + "/" + (slugify(d.name) || "…") + ".md" : ""));
  fp.id = "skx-file";
  if (d.path) { fp.title = "copy the path"; fp.onclick = () => copyText(d.path, "path"); }
  row("folder", "File", fp);
  return box;
}
async function saveMemNow() {
  const d = skDoc(); if (!d || !SKW.dirty) return;
  if (d.kind === "entry" && !(d.name || "").trim()) { setSkillStatus("Give it a title to save"); return; }
  if (SKW.saving) { clearTimeout(SKW.timer); SKW.timer = setTimeout(saveSkillNow, 300); return; }
  SKW.saving = true; SKW.dirty = false; setSkillStatus("Saving…");
  const body = SKW.raw ? d.body : joinBlocks(SKW.blocks);
  d.body = body;
  let r;
  try {
    if (d.kind === "instr") r = await post("/api/memory/file", { id: d.id, content: body });
    else if (d.kind === "index") r = await post("/api/memory/index", { content: body });
    else r = await post("/api/memory/entry", { slug: SKW.draft ? undefined : d.slug, name: d.name.trim(),
      description: d.description || "", type: d.type || "project", body });
  } catch (e) { r = { ok: false, error: e.message }; }
  SKW.saving = false;
  if (!r.ok) {
    SKW.dirty = true;
    setSkillStatus(r.exists ? "That name is taken — pick another" : "Couldn't save — " + (r.error || "error"));
    return;
  }
  if (d.kind === "instr" && !d.exists) { d.exists = true; paintMemList(); }
  if (SKW.draft) {
    // a new memory is real now, with its line in the index
    const made = { ...SKW.draft, slug: r.slug, path: r.path, key: "m:" + r.slug };
    MEMW.docs.push(made);
    const ix = MEMW.docs.find(x => x.kind === "index");
    if (ix) ix.body = (ix.body.trim() ? ix.body.trimEnd() + "\n" : "") + "- [" + made.name.trim() + "](memory/" + r.slug + ".md)" +
      (made.description ? " — " + made.description : "") + "\n";
    SKW.draft = null; SKW.cur = made;
    history.replaceState(null, "", "#memory/" + encodeURIComponent(made.key));
    const fp = document.getElementById("skx-file"); if (fp) fp.textContent = r.path;
  }
  paintMemList();
  setSkillStatus(SKW.dirty ? "Editing…" : "Saved");
}
function confirmMemDelete() {
  const d = SKW.cur; if (!d || d.kind !== "entry") return;
  const bar = document.querySelector("#memorypad .skx-bar"); if (!bar) return;
  const c = el("div","skx-confirm");
  c.append(el("span", null, "Forget “" + d.name + "”? Its file and its line in MEMORY.md are removed."), el("span","sp"));
  c.append(btn("Delete", "dan", async () => {
    const r = await post("/api/memory/entry/delete", { slug: d.slug }).catch(e => ({ ok: false, error: e.message }));
    if (!r.ok) { toast(r.error || "failed", true); return; }
    toast("forgot " + d.name);
    SKW.cur = null; SKW.dirty = false; history.replaceState(null, "", "#memory");
    loadMemory();
  }), btn("Cancel", "gho", () => c.remove()));
  bar.after(c);
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
function kvRow(dl, key, value, mono) {
  dl.append(el("dt", null, key));
  const dd = el("dd", mono === false ? "wrap" : null);
  if (value && value.nodeType) dd.append(value); else dd.textContent = value;
  dl.append(dd);
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
      if (r.ok) { toast("added " + (r.added || []).join(", ")); ta.value = ""; name.value = ""; onDone(r.added || []); }
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
// ---- MCP: a board of connections --------------------------------------------
// An MCP server is a connection, and the page reads like one: a card per
// server that says whether it answers, how fast, and which tools it hands
// the agent — then a sheet with the tools in full and the configuration.
// Every server is connected once when the page opens (one real handshake +
// tools/list, torn straight down) so each card's status is an answer, not a
// guess. A project's stdio servers are left alone until the file is trusted:
// connecting would run their command.
const MCPW = { data: null, tests: {}, q: "", revealed: {}, tab: "tools", toolQ: "", open: null };
const MCP_SCOPE_SAY = { global: "Global · every project", project: "This project", settings: "From settings.json · read-only here" };
let mcpReq = 0;
async function loadMcp() {
  const pad = document.getElementById("mcppad");
  const my = ++mcpReq;
  if (!pad.childElementCount) skeleton(pad);
  let mc;
  try { mc = await api("/api/mcp"); }
  catch (e) { if (my !== mcpReq) return; pad.innerHTML = ""; pad.append(el("div","empty","Error: " + e.message)); return; }
  if (my !== mcpReq) return;
  MCPW.data = mc;
  pad.innerHTML = "";
  pageHead(pad, "MCP servers", null, null, [btn("Add server", mc.servers.length ? "pri" : "gho", () => openMcpAdd())]);
  const reads = el("div"); reads.id = "mcp-reads"; pad.append(reads);
  paintMcpReads();
  if (mc.project_exists && !mc.project_trusted) {
    const b = el("div","banner");
    const txt = el("div","sp");
    txt.innerHTML = "<b>This project's <code>.mcp.json</code> isn't trusted yet.</b> " +
      "Its stdio servers won't start until you approve the file — they run local commands.";
    b.append(txt);
    b.append(btn("Trust this file", "", async () => {
      try { const r = await post("/api/mcp/trust", {});
        if (r.ok) { toast("trusted this project's .mcp.json"); loadMcp(); } else toast(r.error || "failed", true); }
      catch (e) { toast(e.message, true); }
    }));
    pad.append(b);
  }
  if (!mc.servers.length) {
    pad.append(emptyState("mcp", "No MCP servers configured",
      "Add one to give the agent tools it doesn't ship with — GitHub, a database, your API.",
      btn("Add server", "pri", () => openMcpAdd())));
    return;
  }
  if (mc.servers.length > 4) {
    const find = findBox("Search servers and tools");
    find.input.value = MCPW.q;
    find.input.oninput = () => { MCPW.q = find.input.value; paintMcpCards(); };
    pad.append(find.wrap);
  }
  const board = el("div"); board.id = "mcx-board"; pad.append(board);
  paintMcpCards();
  // deep link: #mcp/<name> opens that server's sheet
  const want = decodeURIComponent(location.hash.slice(1).split("/")[1] || "");
  const hit = want && mc.servers.find(s => s.name === want);
  if (hit) openMcpSheet(hit);
  // connect to everything once, three at a time
  const todo = mc.servers.filter(s => !MCPW.tests[s.name] && !mcpWithheld(s));
  let k = 0;
  const next = async () => { const s = todo[k++]; if (!s) return; await testMcp(s.name); return next(); };
  Promise.all([next(), next(), next()]);
}
const mcpWithheld = s => ((MCPW.data || {}).withheld || []).includes(s.name);
function paintMcpReads() {
  const w = document.getElementById("mcp-reads"); if (!w || !MCPW.data) return;
  w.innerHTML = "";
  const sv = MCPW.data.servers || [];
  const done = sv.map(s => MCPW.tests[s.name]).filter(t => t && !t.pending);
  const ok = done.filter(t => t.ok);
  const tools = ok.reduce((a, t) => a + (t.tools || []).length, 0);
  const reads = [{ v: sv.length, k: "server" + (sv.length === 1 ? "" : "s") }];
  if (done.length) reads.push({ v: ok.length + " of " + sv.length, k: "connected" });
  if (ok.length) reads.push({ v: tools, k: "tools for the agent" });
  pageReads(w, reads, null, "Each server is connected once when this page opens: one handshake and tools/list, then closed.");
}
async function testMcp(name, force) {
  if (!force && MCPW.tests[name] && !MCPW.tests[name].pending) return MCPW.tests[name];
  MCPW.tests[name] = { pending: true };
  repaintMcp(name);
  let r;
  try { r = await post("/api/mcp/test", { name }); } catch (e) { r = { ok: false, error: e.message }; }
  MCPW.tests[name] = r;
  repaintMcp(name);
  return r;
}
function repaintMcp(name) {
  if (curView !== "mcp") return;
  paintMcpReads();
  const s = (MCPW.data.servers || []).find(x => x.name === name);
  const old = document.querySelector('.mcx[data-name="' + CSS.escape(name) + '"]');
  if (s && old) old.replaceWith(mcpCard(s));
  if (MCPW.open === name && mcpSheetOpen()) paintMcpSheet(s);
}
// what a server IS, in one short line: the host it talks to, or the command it runs
function mcpWhere(s) {
  const e = s.entry || {};
  if (e.url) { try { const u = new URL(String(e.url)); return u.host + (u.pathname !== "/" ? u.pathname : ""); } catch (x) { return String(e.url); } }
  if (e.command) {
    const base = String(e.command).split("/").pop();
    const arg = (e.args || []).map(String).find(a => !a.startsWith("-"));
    return base + (arg ? " " + (arg.includes("/") && !arg.startsWith("@") ? arg.split("/").pop() : arg) : "");
  }
  return s.detail || "";
}
function mcpState(s) {
  if (mcpWithheld(s)) return ["warn", "Needs trust"];
  const t = MCPW.tests[s.name];
  if (!t) return ["", "Not checked"];
  if (t.pending) return ["run", "Connecting"];
  return t.ok ? ["ok", "Connected"] : ["bad", "Failed"];
}
function mcpHint(err) {
  return /401|403|unauthor|forbidden/i.test(err) ? "The server refused the credentials — check the key in its headers or env."
    : /ENOENT|not found|No such file/i.test(err) ? "The command isn't installed on this machine, or isn't on PATH."
    : /timed? ?out|Timeout/i.test(err) ? "Nothing answered in time — is it running, and is the URL right?"
    : /connect|refused|ECONN/i.test(err) ? "Nothing is listening there — start the server, or fix the URL."
    : "It didn't answer the MCP handshake.";
}
function mcpPill(s) {
  const [cls, lab] = mcpState(s);
  const p = el("span","mcx-st " + cls);
  p.append(el("i"), document.createTextNode(lab));
  return p;
}
function paintMcpCards() {
  const board = document.getElementById("mcx-board"); if (!board) return;
  board.innerHTML = "";
  const ql = MCPW.q.trim().toLowerCase();
  const match = s => !ql || ql.split(/\s+/).every(t => (s.name + " " + s.detail + " " + s.transport + " " +
    ((MCPW.tests[s.name] || {}).tools || []).map(x => x.name).join(" ")).toLowerCase().includes(t));
  let shown = 0;
  ["global", "project", "settings"].forEach(sc => {
    const rows = (MCPW.data.servers || []).filter(s => s.scope === sc && match(s));
    if (!rows.length) return;
    shown += rows.length;
    const sec = section(board, MCP_SCOPE_SAY[sc]);
    const file = sc === "global" ? MCPW.data.global_file : sc === "project" ? MCPW.data.project_file : "settings.json";
    sec.querySelector(".sec-t").title = file;
    const grid = el("div","mcx-grid");
    rows.forEach(s => grid.append(mcpCard(s)));
    sec.append(grid);
  });
  if (!shown) board.append(emptyState("search", "No server matches", "Try a server name, a host, or a tool name."));
}
function mcpCard(s) {
  const c = el("div","mcx"); c.dataset.name = s.name;
  c.tabIndex = 0; c.setAttribute("role", "button");
  const t = MCPW.tests[s.name];
  const h = el("div","mcx-h");
  h.append(skillGlyph(s.name));
  const tt = el("div","mcx-tt");
  tt.append(el("div","mcx-n", s.name));
  const w = el("div","mcx-w", mcpWhere(s)); w.title = s.detail; tt.append(w);
  h.append(tt, mcpPill(s));
  c.append(h);
  const b = el("div","mcx-b");
  if (mcpWithheld(s)) b.append(el("div","mcx-note", "From this project's .mcp.json — trust the file to run it."));
  else if (!t || t.pending) { const r = el("div","mcx-chips"); for (let i = 0; i < 4; i++) r.append(el("span","mcx-chip sk")); b.append(r); }
  else if (!t.ok) { b.append(el("div","mcx-note bad", mcpHint(t.error || ""))); const e = el("div","mcx-err", t.error || ""); e.title = t.error || ""; b.append(e); }
  else {
    const tools = t.tools || [];
    const r = el("div","mcx-chips");
    tools.slice(0, 5).forEach(x => { const ch = el("span","mcx-chip", x.name); ch.title = x.description || x.name; r.append(ch); });
    if (tools.length > 5) r.append(el("span","mcx-chip more", "+" + (tools.length - 5)));
    if (!tools.length) r.append(el("span","mcx-note", "Connected, but it exposes no tools."));
    b.append(r);
  }
  c.append(b);
  const f = el("div","mcx-f");
  const facts = [s.transport === "stdio" ? "local command" : s.transport.toUpperCase()];
  if (t && t.ok) facts.push((t.tools || []).length + " tool" + ((t.tools || []).length === 1 ? "" : "s"), t.ms + " ms");
  f.append(el("span","mcx-facts", facts.join(" · ")));
  f.append(el("span","sp"));
  if (!mcpWithheld(s)) {
    const tb = btn(t && !t.pending ? "Test again" : "Test", "gho", e => { e.stopPropagation(); testMcp(s.name, true); });
    if (t && t.pending) tb.disabled = true;
    f.append(tb);
  }
  f.append(btn("Open", "gho", e => { e.stopPropagation(); openMcpSheet(s); }));
  c.append(f);
  c.onclick = () => openMcpSheet(s);
  c.onkeydown = e => { if (e.key === "Enter") openMcpSheet(s); };
  return c;
}

// ---- the sheet: one server, its tools and its configuration -------------
const mcpSheetOpen = () => !!document.getElementById("modal").className && !!document.querySelector("#sheet .mcs-h");
function openMcpSheet(s, tab) {
  MCPW.open = s.name; MCPW.tab = tab || "tools"; MCPW.toolQ = ""; MCPW.editing = false;
  history.replaceState(null, "", "#mcp/" + encodeURIComponent(s.name));
  paintMcpSheet(s);
  showModal(true);
  if (!MCPW.tests[s.name] && !mcpWithheld(s)) testMcp(s.name);
}
function paintMcpSheet(s) {
  if (!s) return;
  const sh = document.getElementById("sheet");
  const keepQ = sh.querySelector(".mcs-find input");
  const hadFocus = keepQ && document.activeElement === keepQ;
  sh.innerHTML = "";
  const t = MCPW.tests[s.name];
  const h = el("div","mcs-h");
  h.append(skillGlyph(s.name));
  const tt = el("div","mcx-tt");
  tt.append(el("div","mcs-n", s.name));
  const w = el("div","mcx-w", s.detail); w.title = s.detail; tt.append(w);
  h.append(tt, mcpPill(s));
  sh.append(h);
  // the line under the title says the result of the last handshake
  const line = el("div","mcs-line");
  if (t && t.ok) line.textContent = "Connected in " + t.ms + " ms · " + (t.tools || []).length + " tools · " + (s.transport === "stdio" ? "runs a local command" : s.transport.toUpperCase() + " · remote");
  else if (t && !t.pending) line.textContent = mcpHint(t.error || "");
  else if (mcpWithheld(s)) line.textContent = "Waiting for you to trust this project's .mcp.json.";
  else line.textContent = "Connecting…";
  if (!mcpWithheld(s) && !(t && t.pending)) {
    const again = el("button","mcx-link", "Test again"); again.onclick = () => testMcp(s.name, true); line.append(again);
  }
  sh.append(line);
  // a segmented control, not two words: which view is open has to be obvious
  const tabs = el("div","skx-seg mcs-tabs"); tabs.setAttribute("role", "tablist");
  [["tools", "Tools", t && t.ok ? (t.tools || []).length : null], ["config", "Configuration", null]].forEach(([k, lab, n]) => {
    const c = el("button", MCPW.tab === k ? "on" : null, lab);
    c.setAttribute("role", "tab"); c.setAttribute("aria-selected", MCPW.tab === k ? "true" : "false");
    if (n != null) c.append(el("span","mcs-tn2", String(n)));
    c.onclick = () => { MCPW.tab = k; MCPW.editing = false; paintMcpSheet(s); };
    tabs.append(c);
  });
  sh.append(tabs);
  const body = el("div","mcs-body");
  sh.append(body);
  if (MCPW.tab === "tools") mcpToolsTab(body, s, t, hadFocus);
  else mcpConfigTab(body, s);
}
function mcpToolsTab(body, s, t, hadFocus) {
  if (mcpWithheld(s)) { body.append(el("div","us-empty", "Trust the project's .mcp.json to connect and see its tools.")); return; }
  if (!t || t.pending) { for (let i = 0; i < 4; i++) { const r = el("div","mcs-tool sk"); r.append(el("span","mcs-tn"), el("span","mcs-td")); body.append(r); } return; }
  if (!t.ok) {
    const e = el("div","mcp-err");
    e.append(el("b", null, "It didn't connect"), el("span", null, t.error || "unknown error"), el("span","mcp-hint", mcpHint(t.error || "")));
    body.append(e);
    return;
  }
  const tools = t.tools || [];
  if (!tools.length) { body.append(el("div","us-empty", "It connected, but it exposes no tools.")); return; }
  const list = el("div","mcs-tools");
  if (tools.length > 6) {
    const f = el("label","skx-find mcs-find");
    f.append(el("span","skx-fi", "⌕"));
    const i = document.createElement("input"); i.placeholder = "Filter " + tools.length + " tools"; i.value = MCPW.toolQ;
    i.oninput = () => { MCPW.toolQ = i.value; fill(); };
    f.append(i); body.append(f);
    if (hadFocus) setTimeout(() => i.focus(), 0);
  }
  body.append(list);
  const fill = () => {
    list.innerHTML = "";
    const ql = MCPW.toolQ.trim().toLowerCase();
    tools.filter(x => !ql || (x.name + " " + (x.description || "")).toLowerCase().includes(ql)).forEach(x => {
      const r = el("div","mcs-tool");
      r.append(el("div","mcs-tn", x.name));
      if (x.description) r.append(el("div","mcs-td", x.description));
      if ((x.params || []).length) {
        const ps = el("div","mcs-ps");
        x.params.forEach(p => {
          const c = el("span","mcs-p" + (p.required ? " req" : ""));
          c.append(document.createTextNode(p.name));
          if (p.type) c.append(el("i", null, p.type));
          c.title = p.required ? "required" : "optional";
          ps.append(c);
        });
        r.append(ps);
      }
      list.append(r);
    });
    if (!list.childElementCount) list.append(el("div","skx-none", "No tool matches"));
  };
  fill();
}
function mcpConfigTab(body, s) {
  const raw = MCPW.revealed[s.name];
  const e = raw || s.entry || {};
  if (MCPW.editing) { body.append(mcpEditor(s)); return; }
  const dl = el("div","skx-props mcs-props");
  const row = (label, val) => {
    const r = el("div","skx-pr");
    r.append(el("div","skx-pl", label));
    const v = el("div","skx-pv"); v.append(val); r.append(v); dl.append(r);
  };
  const mono = t => { const x = el("span","mcx-mono", t); x.title = t; return x; };
  row("Transport", el("span", null, s.transport === "stdio" ? "stdio · runs a local command" : s.transport === "sse" ? "SSE · remote" : "HTTP · remote"));
  if (e.url) row("URL", mono(String(e.url)));
  if (e.command) row("Command", mono([e.command].concat(e.args || []).map(String).join(" ")));
  ["headers", "env"].forEach(k => {
    const v = e[k];
    if (!v || typeof v !== "object" || !Object.keys(v).length) return;
    const w = el("div","mcp-kv");
    Object.entries(v).forEach(([kk, vv]) => {
      const line = el("div","mcp-kvr");
      line.append(el("span","mcp-k", kk), el("span","mcp-v" + (raw ? "" : " secret"), String(vv)));
      w.append(line);
    });
    row(k === "env" ? "Environment" : "Headers", w);
  });
  const fp = mono(s.display_path || s.path); fp.classList.add("mcs-file"); fp.onclick = () => copyText(s.display_path || s.path, "path");
  row("File", fp);
  body.append(dl);
  const pre = el("pre","mcp-json"); pre.textContent = JSON.stringify(e, null, 2);
  body.append(pre);
  const acts = el("div","cs-foot mcs-foot");
  if (s.editable) acts.append(btn("Edit", "pri", () => { MCPW.editing = true; paintMcpSheet(s); }));
  if ((s.secrets || []).length && s.editable) acts.append(btn(raw ? "Hide secrets" : "Reveal secrets", "gho", () => toggleMcpReveal(s)));
  acts.append(btn("Copy JSON", "gho", () => copyText(JSON.stringify({ [s.name]: e }, null, 2), "configuration")));
  acts.append(el("span","sp"));
  if (s.editable) {
    const del = btn("Remove server", "gho dan", null);
    del.onclick = () => armDelete(del, async () => {
      try { const r = await post("/api/mcp/delete", { scope: s.scope, name: s.name });
        if (r.ok) { toast("removed " + s.name); delete MCPW.tests[s.name]; hideModal(); history.replaceState(null, "", "#mcp"); loadMcp(); }
        else toast(r.error || "failed", true); }
      catch (err) { toast(err.message, true); }
    });
    acts.append(del);
  } else acts.append(el("span","mcx-note", "Defined in settings.json — edit it there."));
  body.append(acts);
}
async function toggleMcpReveal(s) {
  if (MCPW.revealed[s.name]) { delete MCPW.revealed[s.name]; paintMcpSheet(s); return; }
  try {
    const r = await api("/api/mcp/entry?" + q({ name: s.name, scope: s.scope }));
    if (!r.ok) { toast(r.error || "cannot read that entry", true); return; }
    MCPW.revealed[s.name] = r.entry; paintMcpSheet(s);
  } catch (e) { toast(e.message, true); }
}
function mcpEditor(s) {
  const w = el("div","mcp-ed");
  const ta = el("textarea","skx-raw mcp-ta"); ta.spellcheck = false;
  ta.value = JSON.stringify(MCPW.revealed[s.name] || s.entry || {}, null, 2);
  const grow = () => { ta.style.height = "auto"; ta.style.height = Math.max(180, ta.scrollHeight) + "px"; };
  // edit the real entry, not the mask: a save must never write "••••" back
  api("/api/mcp/entry?" + q({ name: s.name, scope: s.scope })).then(r => { if (r.ok) { ta.value = JSON.stringify(r.entry, null, 2); grow(); } }).catch(() => {});
  ta.oninput = () => { grow(); err.textContent = ""; };
  const err = el("div","mcp-jerr");
  const acts = el("div","cs-foot mcs-foot");
  const save = btn("Save", "pri", async () => {
    let parsed;
    try { parsed = JSON.parse(ta.value); } catch (e) { err.textContent = "That isn't valid JSON — " + e.message; return; }
    save.disabled = true;
    try {
      const r = await post("/api/mcp", { scope: s.scope, name: s.name, entry: parsed });
      if (r.ok) {
        toast("saved " + s.name); MCPW.editing = false; delete MCPW.revealed[s.name]; delete MCPW.tests[s.name];
        await loadMcp();
        const fresh = MCPW.data.servers.find(x => x.name === s.name);
        if (fresh) { MCPW.tab = "config"; paintMcpSheet(fresh); }
      } else err.textContent = r.error || "failed";
    } catch (e) { err.textContent = e.message; } finally { save.disabled = false; }
  });
  acts.append(save, btn("Cancel", "gho", () => { MCPW.editing = false; paintMcpSheet(s); }));
  ta.onkeydown = ev => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "s") { ev.preventDefault(); save.click(); }
    if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); MCPW.editing = false; paintMcpSheet(s); }
  };
  w.append(ta, err, acts);
  setTimeout(() => { grow(); ta.focus(); }, 0);
  return w;
}
function openMcpAdd() {
  if (curView !== "mcp") showTab("mcp");
  MCPW.open = null;
  const sh = document.getElementById("sheet"); sh.innerHTML = "";
  const h = el("div","mcs-h");
  h.append(el("div","mcs-n", "Add a server"));
  sh.append(h);
  sh.append(el("div","mcs-line", "Paste whatever the server's README gives you — a whole mcpServers block, one entry, a command, or a URL."));
  const comp = mcpComposer(async added => {
    hideModal();
    await loadMcp();
    const s = added && added[0] && MCPW.data.servers.find(x => x.name === added[0]);
    if (s) openMcpSheet(s);
  });
  comp.classList.add("on", "mcs-comp");
  sh.append(comp);
  const ex = el("div","mcs-ex");
  ex.append(el("div","mcs-exh", "Or start from an example"));
  [["Remote server", "https://mcp.example.com/mcp"],
   ["Local command", "npx -y @modelcontextprotocol/server-filesystem ~/Documents"],
   ["Config block", '{ "mcpServers": { "github": { "command": "npx", "args": ["-y", "@modelcontextprotocol/server-github"] } } }']
  ].forEach(([lab, text]) => {
    const r = el("button","mcp-ex");
    r.append(el("span","mcp-exl", lab), el("span","mcx-mono mcp-ext", text));
    r.onclick = () => { const ta = comp.querySelector("textarea"); ta.value = text; ta.focus(); };
    ex.append(r);
  });
  sh.append(ex);
  showModal(true);
  setTimeout(() => { const ta = comp.querySelector("textarea"); if (ta) ta.focus(); }, 30);
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
  // the per-layer counts are stated once, on the layers themselves
  pageHead(pad, "Config", keys.length, null);
  pageReads(pad, [{ v: keys.length, k: "settings" }],
    "What mantis runs with.", "Every value that looks like a secret is masked before it leaves the machine.");
  if (!keys.length) {
    pad.append(zero("Running on defaults",
      "No settings files found — mantis is using its built-in defaults. Anything you set in " +
      "settings.json will show up here with the layer it came from."));
  } else {
    const t = el("div","cfg");
    const addRow = (k, v) => {
      const row = el("div","kv");
      row.append(el("div","ck", k), el("div","cv", fmtVal(v)));
      t.append(row);
    };
    keys.forEach(k => {
      const v = merged[k];
      // a flat object (env, permissions) reads as its own keys, one per row —
      // a JSON block with a lone "env" in the gutter reads as a dump
      if (v && typeof v === "object" && !Array.isArray(v) && Object.values(v).every(x => x == null || typeof x !== "object")) {
        Object.keys(v).sort().forEach(sub => addRow(k + "." + sub, v[sub]));
      } else addRow(k, v);
    });
    pad.append(t);
  }

  const laySec = section(pad, "Layers");
  laySec.querySelector(".sec-t").title = "user, then project, then local — a later layer overrides an earlier one";
  ["user","project","local"].forEach(src => {
    const layer = (c.layers||{})[src] || {};
    const d = el("details","layer");
    const n = Object.keys(layer).length;
    const sum = el("summary");
    sum.append(el("span","lay-n", src));
    sum.append(el("span","lay-c", n + " setting" + (n === 1 ? "" : "s")));
    d.append(sum);
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
  else if (["sessions","activity","models","deploy","skills","memory","mcp","config"].includes(t.split("/")[0])) showTab(t.split("/")[0]);
  else { refreshCrumb(); loadHome(); }   // default landing
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
