// Retrato da página para o conserto assistido (rapa.runner.snapshot).
//
// Roda uma vez, num quadro, depois que um passo já falhou, e só lê o DOM: nenhum
// evento, nenhum foco, nenhuma rolagem, nenhum valor de campo (nem o value de um
// <input> ou <textarea>, nem a opção escolhida de uma lista; o value de um botão
// <input type=submit> é o texto dele, não um dado). Devolve os elementos visíveis
// cujo papel está em config.roles (e, com config.testIds, os que têm data-testid),
// com papel, nome acessível, rótulo, placeholder, texto, ids, a linha de tabela em
// volta e as contagens no documento e nos shadow roots abertos, como o gravador
// (capture.js) mede: as mesmas regras de papel, nome e rótulo, e as mesmas marcas
// de senha e de código de uso único. As regras de sigilo do gravador
// (rapa.recorder.sensitivity) entram depois, no Python, sobre esses campos.
(config) => {
  'use strict';
  const MAX_TEXT = 300;
  const MAX_SCAN = Math.max(100, Math.min(Number(config.maxScan) || 6000, 20000));
  const MAX_ELEMENTS = Math.max(1, Math.min(Number(config.max) || 150, 300));
  const ROLES = new Set(config.roles || []);
  const W = window;

  const norm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const clip = (s, n) => {
    const t = norm(s);
    return t.length > n ? t.slice(0, n) : t;
  };
  const orNull = (s) => (s ? s : null);
  const styleOf = (el) => {
    try { return el.ownerDocument.defaultView.getComputedStyle(el); } catch (e) { return null; }
  };
  const rootOf = (node) => (node.getRootNode ? node.getRootNode() : node.ownerDocument);
  const hostOf = (root) =>
    (root && root.nodeType === 11 && root.host && root.host.nodeType === 1 ? root.host : null);
  const closestComposed = (el, selector) => {
    let cur = el;
    for (let depth = 0; cur && depth < 64; depth++) {
      let found = null;
      try { found = cur.closest(selector); } catch (e) { return null; }
      if (found) return found;
      cur = hostOf(rootOf(cur));
    }
    return null;
  };
  // O documento e os shadow roots abertos (também os aninhados), ou null se grandes demais.
  const rootsUnder = (node) => {
    const roots = [node];
    let budget = MAX_SCAN;
    for (let i = 0; i < roots.length; i++) {
      let all;
      try { all = roots[i].querySelectorAll('*'); } catch (e) { return null; }
      budget -= all.length;
      if (budget < 0) return null;
      for (const child of all) if (child.shadowRoot) roots.push(child.shadowRoot);
    }
    return roots;
  };

  const contentText = (node, skip, depth) => {
    if (depth > 25) return '';
    let out = '';
    for (const child of node.childNodes) {
      if (child.nodeType === 3) { out += child.data; continue; }
      if (child.nodeType !== 1 || child === skip) continue;
      const tag = child.tagName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'TEMPLATE' || tag === 'NOSCRIPT') continue;
      if (child.getAttribute('aria-hidden') === 'true') continue;
      // Controles embutidos num rótulo nunca contribuem com o valor.
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') continue;
      const st = styleOf(child);
      if (st && (st.display === 'none' || st.visibility === 'hidden')) continue;
      if (tag === 'IMG') { out += ' ' + (child.getAttribute('alt') || '') + ' '; continue; }
      const aria = norm(child.getAttribute('aria-label'));
      const inner = aria || contentText(child, skip, depth + 1);
      const inline = st && st.display && st.display.indexOf('inline') === 0;
      out += inline ? inner : ' ' + inner + ' ';
    }
    return out;
  };

  // -- papéis e nomes (os mesmos de capture.js) -----------------------------------

  const TEXT_TYPES = new Set(['text', 'email', 'tel', 'url', 'search', 'number', '']);
  const BUTTON_TYPES = new Set(['button', 'submit', 'reset', 'image']);
  const NAME_FROM_CONTENT = new Set([
    'button', 'link', 'heading', 'cell', 'gridcell', 'columnheader', 'rowheader', 'row',
    'menuitem', 'option', 'tab', 'checkbox', 'radio', 'switch', 'listitem', 'treeitem',
  ]);
  const inputType = (el) => (el.tagName === 'INPUT' ? String(el.type || 'text').toLowerCase() : null);
  const implicitRole = (el) => {
    switch (el.tagName) {
      case 'A': case 'AREA': return el.hasAttribute('href') ? 'link' : null;
      case 'BUTTON': return 'button';
      case 'INPUT': {
        const type = inputType(el);
        if (BUTTON_TYPES.has(type)) return 'button';
        if (type === 'checkbox') return 'checkbox';
        if (type === 'radio') return 'radio';
        if (type === 'range') return 'slider';
        if (type === 'number') return 'spinbutton';
        if (type === 'search') return el.hasAttribute('list') ? 'combobox' : 'searchbox';
        if (TEXT_TYPES.has(type)) return el.hasAttribute('list') ? 'combobox' : 'textbox';
        if (type === 'password') return 'textbox';
        if (type === 'file') return 'button';
        return null;
      }
      case 'SELECT': return el.multiple || el.size > 1 ? 'listbox' : 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'H1': case 'H2': case 'H3': case 'H4': case 'H5': case 'H6': return 'heading';
      case 'TABLE': return 'table';
      case 'TR': return 'row';
      case 'TD': return 'cell';
      case 'TH': return el.getAttribute('scope') === 'row' ? 'rowheader' : 'columnheader';
      case 'UL': case 'OL': return 'list';
      case 'LI': return 'listitem';
      case 'IMG': return el.getAttribute('alt') === '' ? null : 'img';
      case 'NAV': return 'navigation';
      case 'OPTION': return 'option';
      case 'DIALOG': return 'dialog';
      default: return null;
    }
  };
  const roleOf = (el) => {
    const explicit = norm(el.getAttribute('role')).split(' ')[0];
    if (explicit && explicit !== 'none' && explicit !== 'presentation') return explicit;
    return implicitRole(el);
  };
  const isLabelable = (el) => {
    const tag = el.tagName;
    return (tag === 'INPUT' && inputType(el) !== 'hidden') || tag === 'SELECT' ||
      tag === 'TEXTAREA' || tag === 'BUTTON' || tag === 'METER' || tag === 'OUTPUT' ||
      tag === 'PROGRESS';
  };
  const labelElementsText = (el) => {
    if (!isLabelable(el) || !el.labels) return '';
    const parts = [];
    for (const label of el.labels) parts.push(contentText(label, el, 0));
    return norm(parts.join(' '));
  };
  const labelledByText = (el) => {
    const ids = norm(el.getAttribute('aria-labelledby'));
    if (!ids) return '';
    const root = rootOf(el);
    const scope = root && typeof root.getElementById === 'function' ? root : el.ownerDocument;
    return norm(ids.split(' ').map((id) => {
      const ref = scope.getElementById(id);
      return ref ? (norm(ref.getAttribute('aria-label')) || contentText(ref, null, 0)) : '';
    }).join(' '));
  };
  const accessibleName = (el, role) => {
    const byRef = labelledByText(el);
    if (byRef) return byRef;
    const aria = norm(el.getAttribute('aria-label'));
    if (aria) return aria;
    const tag = el.tagName;
    const type = inputType(el);
    if (tag === 'INPUT' && (type === 'submit' || type === 'button' || type === 'reset')) {
      const value = norm(el.getAttribute('value'));
      if (value) return value;
      if (type === 'submit') return 'Submit';
      if (type === 'reset') return 'Reset';
      return norm(el.getAttribute('title'));
    }
    if (tag === 'INPUT' && type === 'image') {
      return norm(el.getAttribute('alt')) || norm(el.getAttribute('value')) || norm(el.getAttribute('title')) || 'Submit';
    }
    const labels = labelElementsText(el);
    if (labels) return labels;
    if (tag === 'TABLE') {
      const caption = el.querySelector(':scope > caption');
      if (caption) { const t = norm(contentText(caption, null, 0)); if (t) return t; }
    }
    if (tag === 'FIELDSET') {
      const legend = el.querySelector(':scope > legend');
      if (legend) { const t = norm(contentText(legend, null, 0)); if (t) return t; }
    }
    if (tag === 'IMG') return norm(el.getAttribute('alt')) || norm(el.getAttribute('title'));
    if (role && NAME_FROM_CONTENT.has(role) && tag !== 'INPUT' && tag !== 'SELECT' && tag !== 'TEXTAREA') {
      const t = norm(contentText(el, null, 0));
      if (t) return t;
    }
    const title = norm(el.getAttribute('title'));
    if (title) return title;
    return norm(el.getAttribute('placeholder'));
  };
  const labelText = (el) => labelElementsText(el) || labelledByText(el) || norm(el.getAttribute('aria-label'));
  const hiddenForAria = (el) => {
    if (el.closest('[aria-hidden="true"], [inert]')) return true;
    if (typeof el.checkVisibility === 'function') return !el.checkVisibility({ visibilityProperty: true });
    return el.getClientRects().length === 0;
  };
  const disabled = (el) => {
    try {
      if (el.matches(':disabled')) return true;
    } catch (e) { /* ignora */ }
    return norm(el.getAttribute('aria-disabled')).toLowerCase() === 'true';
  };

  // -- marcas de senha e de código de uso único (as de capture.js) -------------------

  const OTP_NAMES = new Set([
    'otp', 'totp', 'mfa', '2fa', 'token', 'tokenotp', 'codigo', 'codigootp', 'codigoverificacao',
    'codverificacao', 'codigoautenticacao', 'codigoseguranca', 'codigoacesso', 'codigosms',
    'verificationcode', 'onetimecode', 'otpcode', 'authcode', 'smscode', 'mfacode', 'totpcode',
  ]);
  const OTP_LABEL = /(c[oó]digo\s+de\s+(verifica[cç][aã]o|seguran[cç]a|autentica[cç][aã]o|acesso)|c[oó]digo\s+(otp|totp|sms)|token|autentica[cç][aã]o\s+em\s+duas|dois\s+fatores|\b2fa\b|one[-\s]?time|verification\s+code|c[oó]digo\s[^.]{0,40}?\b(?:sms|enviad[oa]|recebid[oa]|e-?mail|celular|whatsapp)\b)/i;
  const OTP_TOKEN = /(?:^|[^a-z0-9])(?:otp|totp|mfa|2fa)(?:[^a-z0-9]|$)/;
  const squash = (s) => norm(s).toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9]/g, '');
  const isOneTimeCode = (el) => {
    if (el.tagName !== 'INPUT') return false;
    const type = inputType(el);
    if (!TEXT_TYPES.has(type) && type !== 'password') return false;
    const ac = norm(el.getAttribute('autocomplete')).toLowerCase();
    if (ac.split(' ').indexOf('one-time-code') >= 0) return true;
    if (OTP_NAMES.has(squash(el.getAttribute('name'))) || OTP_NAMES.has(squash(el.id))) return true;
    if (OTP_TOKEN.test(norm(el.getAttribute('name')).toLowerCase()) || OTP_TOKEN.test(norm(el.id).toLowerCase())) return true;
    return OTP_LABEL.test(labelText(el) || norm(el.getAttribute('placeholder')));
  };
  const isMaskedText = (el) => {
    const st = styleOf(el);
    if (!st) return false;
    const mask = norm(st.getPropertyValue('-webkit-text-security') || st.webkitTextSecurity).toLowerCase();
    return !!mask && mask !== 'none';
  };
  const isPassword = (el) => el.tagName === 'INPUT' &&
    (inputType(el) === 'password' || (TEXT_TYPES.has(inputType(el)) && !isOneTimeCode(el) && isMaskedText(el)));

  // -- contagens (uma passada por quadro, como o executor procura) -----------------------

  const scanAll = (roots, selector) => {
    if (!roots) return null;
    let out = [];
    for (const root of roots) {
      let found;
      try { found = root.querySelectorAll(selector); } catch (e) { return null; }
      out = out.concat(Array.from(found));
      if (out.length > MAX_SCAN) return null;
    }
    return out;
  };
  const bump = (map, key) => { if (key) map.set(key, (map.get(key) || 0) + 1); };
  const roots = rootsUnder(document);
  const counts = (() => {
    const all = scanAll(roots, '*');
    if (!all) return null;
    const roleName = new Map();
    const label = new Map();
    const placeholder = new Map();
    const testId = new Map();
    const text = new Map();
    const id = new Map();
    const nameAttr = new Map();
    const normText = new Map();
    const textOf = (el) => {
      let t = normText.get(el);
      if (t === undefined) { t = norm(el.textContent); normText.set(el, t); }
      return t;
    };
    for (const el of all) {
      const tag = el.tagName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'HTML' || tag === 'HEAD') continue;
      const role = roleOf(el);
      if (role && !hiddenForAria(el)) bump(roleName, role + '\u0000' + accessibleName(el, role));
      if (isLabelable(el) || el.hasAttribute('aria-label') || el.hasAttribute('aria-labelledby')) {
        bump(label, labelText(el));
      }
      bump(placeholder, el.getAttribute('placeholder'));
      bump(testId, el.getAttribute('data-testid'));
      bump(id, el.getAttribute('id'));
      bump(nameAttr, el.getAttribute('name'));
      const t = textOf(el);
      if (t && t.length <= 100) {
        let childMatches = false;
        for (const child of el.children) if (textOf(child) === t) { childMatches = true; break; }
        if (!childMatches) bump(text, t);
      }
    }
    return { roleName, label, placeholder, testId, text, id, nameAttr };
  })();
  const countIn = (map, key) => (counts && key ? (counts[map].get(key) || 0) : null);

  // -- linha de tabela -----------------------------------------------------------------

  const SKIP_TEXT = new Set(['SCRIPT', 'NOSCRIPT', 'STYLE']);
  const pwNormalize = (s) => String(s).replace(/[​­]/g, '').trim().replace(/\s+/g, ' ');
  const domCache = new Map();
  const domText = (node, depth) => {
    let out = domCache.get(node);
    if (out !== undefined) return out;
    out = '';
    if (node.nodeType === 1 && SKIP_TEXT.has(node.nodeName)) {
      out = '';
    } else if (node.nodeType === 1 && node.tagName === 'INPUT' && ['submit', 'button', 'reset'].indexOf(String(node.type)) >= 0) {
      out = String(node.getAttribute('value') || '');
    } else if (depth < 60) {
      for (let child = node.firstChild; child; child = child.nextSibling) {
        if (child.nodeType === 3) out += child.nodeValue || '';
        else if (child.nodeType === 1) out += domText(child, depth + 1);
      }
      if (node.shadowRoot) out += domText(node.shadowRoot, depth + 1);
    }
    domCache.set(node, out);
    return out;
  };
  const normCache = new Map();
  const rowText = (node) => {
    let t = normCache.get(node);
    if (t === undefined) { t = pwNormalize(domText(node, 0)); normCache.set(node, t); }
    return t;
  };
  const keyCache = new Map();
  const rowKey = (row, cells) => {
    if (keyCache.has(row)) return keyCache.get(row);
    let key = null;
    const root = rootOf(row);
    let rows = [];
    try { rows = Array.from(root.querySelectorAll('tr, [role=row]')); } catch (e) { rows = []; }
    const visible = rows.length <= MAX_SCAN ? rows.filter((r) => roleOf(r) === 'row' && !hiddenForAria(r)) : [];
    if (visible.indexOf(row) >= 0) {
      let budget = MAX_SCAN;
      const inside = new Map();
      for (const r of visible) {
        const all = r.querySelectorAll('*');
        budget -= all.length;
        if (budget < 0) { keyCache.set(row, null); return null; }
        inside.set(r, all);
      }
      for (const cell of cells) {
        const candidate = rowText(cell);
        if (candidate.length < 2 || candidate.length >= 200) continue;
        let hits = 0;
        let mine = false;
        for (const r of visible) {
          let found = false;
          for (const child of inside.get(r)) {
            if (rowText(child) === candidate) { found = true; break; }
          }
          if (!found) continue;
          hits += 1;
          if (r === row) mine = true;
          if (hits > 1) break;
        }
        if (hits === 1 && mine) { key = candidate; break; }
      }
    }
    keyCache.set(row, key);
    return key;
  };
  const rowInfo = (el, role, name, text) => {
    const row = closestComposed(el, 'tr, [role=row]');
    if (!row || row === el) return null;
    const cellElements = Array.from(row.querySelectorAll('td, th, [role=cell], [role=gridcell], [role=rowheader], [role=columnheader]')).slice(0, 20);
    const inside = rootsUnder(row);
    let roleNameCount = null;
    let textCount = null;
    if (inside) {
      const all = scanAll(inside, '*');
      if (all) {
        if (role && name) {
          roleNameCount = all.filter((x) => roleOf(x) === role && !hiddenForAria(x) && accessibleName(x, role) === name).length;
        }
        if (text && text.length <= 100) {
          textCount = all.filter((x) => {
            if (norm(x.textContent) !== text) return false;
            for (const child of x.children) if (norm(child.textContent) === text) return false;
            return true;
          }).length;
        }
      }
    }
    return {
      text: clip(row.innerText || row.textContent, 500),
      cells: cellElements.map((c) => clip(c.innerText || c.textContent, 200)),
      key: rowKey(row, cellElements),
      role_name_count: roleNameCount,
      text_count: textCount,
    };
  };

  // -- elementos ---------------------------------------------------------------------

  const CANDIDATES = 'a[href], area[href], button, input, select, textarea, summary, option, ' +
    'h1, h2, h3, h4, h5, h6, table, td, th, li, img, dialog, [role], [data-testid], ' +
    '[contenteditable=""], [contenteditable=true]';
  const all = scanAll(roots || [document], CANDIDATES) || [];
  const elements = [];
  let truncated = !roots || !counts;
  for (const el of all) {
    if (el.tagName === 'INPUT' && inputType(el) === 'hidden') continue;
    const role = roleOf(el);
    const testId = el.getAttribute('data-testid');
    if (!(role && ROLES.has(role)) && !(config.testIds && testId)) continue;
    if (hiddenForAria(el)) continue;
    if (elements.length >= MAX_ELEMENTS) { truncated = true; break; }
    const tag = el.tagName.toLowerCase();
    const type = inputType(el);
    const name = clip(accessibleName(el, role), MAX_TEXT);
    const label = clip(labelText(el), MAX_TEXT);
    const placeholder = clip(el.getAttribute('placeholder'), MAX_TEXT);
    const isControl = tag === 'input' || tag === 'select' || tag === 'textarea';
    let text = '';
    if (!isControl) text = clip(el.innerText || el.textContent, MAX_TEXT);
    else if (tag === 'input' && BUTTON_TYPES.has(type)) text = clip(el.getAttribute('value'), MAX_TEXT);
    const nameAttr = clip(el.getAttribute('name'), MAX_TEXT);
    const id = clip(el.getAttribute('id'), MAX_TEXT);
    const textForCount = text && text.length <= 100 ? text : null;
    elements.push({
      tag,
      input_type: type,
      role: orNull(role),
      name: orNull(name),
      label: orNull(label),
      placeholder: orNull(placeholder),
      test_id: orNull(clip(testId, MAX_TEXT)),
      name_attr: orNull(nameAttr),
      id: orNull(id),
      title: orNull(clip(el.getAttribute('title'), MAX_TEXT)),
      text: orNull(text),
      autocomplete: orNull(clip(el.getAttribute('autocomplete'), 100)),
      in_shadow: hostOf(rootOf(el)) !== null,
      enabled: !disabled(el),
      is_password: isPassword(el),
      is_one_time_code: isOneTimeCode(el),
      row: rowInfo(el, role, name, textForCount),
      counts: {
        role_name: role && name ? countIn('roleName', role + '\u0000' + name) : null,
        label: countIn('label', label),
        placeholder: countIn('placeholder', placeholder),
        test_id: countIn('testId', testId),
        text: countIn('text', textForCount),
        id: countIn('id', el.getAttribute('id')),
        name_attr: countIn('nameAttr', el.getAttribute('name')),
      },
    });
  }
  let title = '';
  try { title = clip(document.title, MAX_TEXT); } catch (e) { title = ''; }
  return { title, elements, truncated, url: String(W.location.origin || '') + String(W.location.pathname || '') };
}
