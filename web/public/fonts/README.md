# Google Sans (self-hosted)

Drop your Google Sans font files here with these exact names. The
@font-face block in `app/globals.css` picks them up automatically —
no code change needed. `.woff2` is preferred; `.ttf` / `.otf` also work.

    GoogleSans-Regular.woff2        400 normal   (required)
    GoogleSans-Medium.woff2         500 normal
    GoogleSans-Bold.woff2           700 normal
    GoogleSans-Italic.woff2         400 italic   (optional)
    GoogleSans-MediumItalic.woff2   500 italic   (optional)

Only have a single variable font? Name it `GoogleSans-Regular.woff2`
(or .ttf) — it will cover all weights.

After dropping files, rebuild (or the dev server picks them up on reload).
Until then the site falls back to the Google Sans loaded via next/font,
so nothing breaks.
