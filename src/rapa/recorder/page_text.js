// Texto visível de um quadro, para candidatos a pós-condição. Valores digitados
// em campos não entram (innerText não inclui o valor de <input>).
(maxChars) => {
  const clean = (s) => String(s || '')
    .replace(/[ \t\f\v ]+/g, ' ')
    .replace(/ *\n[\s]*/g, '\n')
    .trim();
  const visible = (el) => {
    if (el.closest('[aria-hidden="true"]')) return false;
    if (typeof el.checkVisibility === 'function') return el.checkVisibility({ visibilityProperty: true });
    return el.getClientRects().length > 0;
  };
  const headings = [];
  for (const el of document.querySelectorAll('h1, h2, h3, h4, h5, h6, [role=heading]')) {
    if (!visible(el)) continue;
    const text = clean(el.innerText).replace(/\s+/g, ' ').slice(0, 500);
    if (!text) continue;
    let level = /^H[1-6]$/.test(el.tagName) ? Number(el.tagName[1]) : Number(el.getAttribute('aria-level') || 2);
    if (!(level >= 1 && level <= 6)) level = 2;
    headings.push({ level, text });
    if (headings.length >= 50) break;
  }
  const body = document.body;
  return {
    url: location.href,
    title: String(document.title || '').slice(0, 500),
    headings,
    text: body ? clean(body.innerText).slice(0, maxChars) : '',
  };
}
