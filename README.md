# gh-pages: redirect only

This branch exists so links to the old GitHub Pages URL keep working. It holds
no reports. The site is built from `docs/` on `main` and served by Vercel:

  https://fiba-ireland.vercel.app/

`404.html` forwards every unmatched path, preserving it, so deep links survive.
`index.html` is the same file, for the root.
