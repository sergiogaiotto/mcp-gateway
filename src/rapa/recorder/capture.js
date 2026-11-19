// Gravador do RaPA: roda em todos os quadros da página e envia ao Python, por
// uma binding do Playwright, um descritor de cada ação da pessoa.
//
// Regras: só eventos confiáveis (isTrusted); nunca envia o valor de campos de
// senha (inclusive campo de texto mascarado por CSS, com rótulo de senha ou cujo
// nome acessível fala em senha), código de uso único, usuário de login (com ou
// sem <form>) ou do campo logo antes de uma senha de transação; o valor final de
// cada campo sai no change/blur (ou antes da próxima ação), com há quanto tempo a
// edição começou.
(() => {
  'use strict';
  const CONFIG = __RAPA_CONFIG__;
  const W = window;
  const INSTALLED = Symbol.for('rapa.recorder.installed');
  if (W[INSTALLED]) return;
  Object.defineProperty(W, INSTALLED, { value: true });

  const MAX_VALUE = 4000;
  const MAX_TEXT = 300;
  const MAX_NAME = 300;
  const MAX_SCAN = 6000;
  // Relógio guardado antes dos scripts da página (que podem trocar o performance.now).
  const clock = (() => {
    try { const p = W.performance; const now = p.now.bind(p); now(); return now; } catch (e) { return Date.now; }
  })();

  // -- canal com o Python -----------------------------------------------------

  let bindingFn = null;
  let stopped = false;
  const grabBinding = () => {
    if (bindingFn) return bindingFn;
    const fn = W[CONFIG.binding];
    if (typeof fn === 'function') {
      bindingFn = fn;
      // Some do escopo global para que a página não chame a binding.
      try { delete W[CONFIG.binding]; } catch (e) { /* ignora */ }
    }
    return bindingFn;
  };
  grabBinding();

  const send = (payload) => {
    if (stopped) return;
    const fn = grabBinding();
    if (!fn) return;
    payload.nonce = CONFIG.nonce;
    try {
      const result = fn(payload);
      if (result && typeof result.catch === 'function') result.catch(() => {});
    } catch (e) { /* a página pode estar saindo */ }
  };

  // -- texto ------------------------------------------------------------------

  const norm = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
  const clip = (s, n) => {
    const t = norm(s);
    return t.length > n ? t.slice(0, n) : t;
  };
  const orNull = (s) => (s ? s : null);

  const styleOf = (el) => {
    try { return el.ownerDocument.defaultView.getComputedStyle(el); } catch (e) { return null; }
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
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') {
        // Controles embutidos num rótulo não contribuem com o valor digitado.
        continue;
      }
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

  // -- papéis e nomes acessíveis (aproximação do accname/HTML-AAM) ------------

  const TEXT_TYPES = new Set(['text', 'email', 'tel', 'url', 'search', 'number', '']);
  const BUTTON_TYPES = new Set(['button', 'submit', 'reset', 'image']);
  const NAME_FROM_CONTENT = new Set([
    'button', 'link', 'heading', 'cell', 'gridcell', 'columnheader', 'rowheader', 'row',
    'menuitem', 'option', 'tab', 'checkbox', 'radio', 'switch', 'listitem', 'treeitem',
  ]);

  const inputType = (el) => (el.tagName === 'INPUT' ? String(el.type || 'text').toLowerCase() : null);

  const implicitRole = (el) => {
    const tag = el.tagName;
    switch (tag) {
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
        // Como o Playwright: senha é caixa de texto e arquivo é botão.
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
    const doc = el.ownerDocument;
    return norm(ids.split(' ').map((id) => {
      const ref = doc.getElementById(id);
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

  // Texto do rótulo: <label>, aria-labelledby ou aria-label (o que getByLabel usa).
  const labelText = (el) => labelElementsText(el) || labelledByText(el) || norm(el.getAttribute('aria-label'));

  const hiddenForAria = (el) => {
    if (el.closest('[aria-hidden="true"], [inert]')) return true;
    if (typeof el.checkVisibility === 'function') return !el.checkVisibility({ visibilityProperty: true });
    return el.getClientRects().length === 0;
  };

  // -- campos sensíveis --------------------------------------------------------

  const OTP_NAMES = new Set([
    'otp', 'totp', 'mfa', '2fa', 'token', 'tokenotp', 'codigo', 'codigootp', 'codigoverificacao',
    'codverificacao', 'codigoautenticacao', 'codigoseguranca', 'codigoacesso', 'codigosms',
    'verificationcode', 'onetimecode', 'otpcode', 'authcode', 'smscode', 'mfacode', 'totpcode',
  ]);
  const OTP_LABEL = /(c[oó]digo\s+de\s+(verifica[cç][aã]o|seguran[cç]a|autentica[cç][aã]o|acesso)|c[oó]digo\s+(otp|totp|sms)|token|autentica[cç][aã]o\s+em\s+duas|dois\s+fatores|\b2fa\b|one[-\s]?time|verification\s+code)/i;
  const squash = (s) => norm(s).toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/[^a-z0-9]/g, '');

  const isOneTimeCode = (el) => {
    if (el.tagName !== 'INPUT') return false;
    const type = inputType(el);
    if (!TEXT_TYPES.has(type) && type !== 'password') return false;
    const ac = norm(el.getAttribute('autocomplete')).toLowerCase();
    if (ac.split(' ').indexOf('one-time-code') >= 0) return true;
    if (OTP_NAMES.has(squash(el.getAttribute('name'))) || OTP_NAMES.has(squash(el.id))) return true;
    return OTP_LABEL.test(labelText(el) || norm(el.getAttribute('placeholder')));
  };

  // Senha em campo de texto (portais de banco mascaram por CSS): máscara
  // -webkit-text-security, autocomplete de senha, ou rótulo, título, placeholder,
  // nome ou id que falam em senha. A mesma regra (menos a máscara, que só a
  // página conhece) está em rapa/recorder/sensitivity.py.
  const PASSWORD_WORDS = /(?<!re)senha|password|passwd|passphrase|(?:^|[^a-z0-9])(?:pwd|pin)(?:[^a-z0-9]|$)/;
  const PASSWORD_LABEL_TYPES = new Set(['text', '', 'tel', 'number']);
  const plainWords = (s) => norm(s).replace(/([a-z])([A-Z])/g, '$1 $2')
    .normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
  const mentionsPassword = (s) => {
    const t = plainWords(s);
    return !!t && PASSWORD_WORDS.test(t);
  };

  const isMaskedText = (el) => {
    const st = styleOf(el);
    if (!st) return false;
    const mask = norm(st.getPropertyValue('-webkit-text-security') || st.webkitTextSecurity).toLowerCase();
    return !!mask && mask !== 'none';
  };

  const isPasswordLikeText = (el) => {
    if (el.tagName !== 'INPUT') return false;
    const type = inputType(el);
    if (!TEXT_TYPES.has(type) || isOneTimeCode(el)) return false;
    if (isMaskedText(el)) return true;
    const ac = norm(el.getAttribute('autocomplete')).toLowerCase().split(' ');
    if (ac.indexOf('current-password') >= 0 || ac.indexOf('new-password') >= 0) return true;
    if (ac.indexOf('username') >= 0 || ac.indexOf('email') >= 0 || !PASSWORD_LABEL_TYPES.has(type)) return false;
    return [labelText(el), el.getAttribute('title'), el.getAttribute('placeholder'), el.getAttribute('name'), el.id]
      .some(mentionsPassword);
  };

  const isPassword = (el) => el.tagName === 'INPUT' && (inputType(el) === 'password' || isPasswordLikeText(el));

  // Campo que só o nome acessível (aria-labelledby, aria-label) diz ser senha: o
  // valor não sai, mas o campo não vira senha (rapa/recorder/sensitivity.py,
  // password_by_name) e o usuário marcado pelo grupo continua usuário.
  const isPasswordByName = (el) => {
    if (el.tagName !== 'INPUT' || isPassword(el) || isOneTimeCode(el)) return false;
    if (!PASSWORD_LABEL_TYPES.has(inputType(el))) return false;
    const ac = norm(el.getAttribute('autocomplete')).toLowerCase().split(' ');
    if (ac.indexOf('username') >= 0 || ac.indexOf('email') >= 0) return false;
    return mentionsPassword(accessibleName(el, roleOf(el)));
  };

  // Senha de transação (PIN, assinatura eletrônica, senha de pagamento) e verbos
  // irreversíveis: as mesmas regras de rapa/recorder/sensitivity.py.
  const TRANSACTION_WORDS = /transac|transfer|assinatura|assinar|autoriza|pagamento|operac|cartao|(?:^|[^a-z0-9])pin(?:[^a-z0-9]|$)/;
  const IRREVERSIBLE = /\b(emitir|emita|enviar|envie|transmitir|transmita|pagar|pague|confirmar pagamento|confirmar transferencia|transferir|transfira|excluir|exclua|apagar|apague|assinar|assine|finalizar|finalize|concluir|conclua|protocolar|protocole|autorizar|autorize|aprovar|aprove|solicitar|solicite|cancelar|cancele)\b/;
  const verbText = (s) => norm(s).normalize('NFKD').replace(/[\u0300-\u036f]/g, '').toLowerCase();

  const isTransactionSecret = (el) => [
    labelText(el), accessibleName(el, roleOf(el)), el.getAttribute('placeholder'),
    el.getAttribute('title'), el.getAttribute('name'), el.id,
  ].some((s) => { const t = plainWords(s); return !!t && TRANSACTION_WORDS.test(t); });

  // O botão padrão do formulário (o que o Enter aciona) tem verbo irreversível.
  const irreversibleSubmit = (el) => {
    const form = formOf(el);
    if (!form) return false;
    for (const f of Array.from(form.elements)) {
      const type = f.tagName === 'BUTTON' ? String(f.type || 'submit').toLowerCase() : inputType(f);
      if (type !== 'submit' && !(f.tagName === 'INPUT' && type === 'image')) continue;
      const text = accessibleName(f, roleOf(f)) || norm(f.textContent);
      return IRREVERSIBLE.test(verbText(text));
    }
    return false;
  };

  const isTextLike = (el) => (el.tagName === 'INPUT' && TEXT_TYPES.has(inputType(el))) || el.tagName === 'TEXTAREA';

  const formOf = (el) => el.form || (el.closest ? el.closest('form') : null);

  // Campos de texto e de senha que vão junto com o campo, em ordem: os do
  // formulário ou, sem <form> (login em SPA), os campos soltos e visíveis da
  // mesma raiz (documento ou shadow root).
  const groupFields = (el) => {
    const form = formOf(el);
    let all;
    if (form) {
      all = Array.from(form.elements);
    } else {
      const root = el.getRootNode ? el.getRootNode() : el.ownerDocument;
      let found;
      try { found = root.querySelectorAll('input, textarea'); } catch (e) { return []; }
      if (found.length > MAX_SCAN) return [];
      all = Array.from(found).filter((f) => !formOf(f) && !hiddenForAria(f));
    }
    return all.filter((f) => isTextLike(f) || isPassword(f));
  };

  // 'username': o usuário do login; 'hidden': o campo antes de uma senha de
  // transação (ou de uma senha num formulário com botão irreversível), que não
  // vai na gravação mas também não é o usuário; null: campo comum.
  const loginRole = (el) => {
    if (!isTextLike(el) || el.tagName === 'TEXTAREA' || isOneTimeCode(el)) return null;
    const ac = norm(el.getAttribute('autocomplete')).toLowerCase().split(' ');
    if (ac.indexOf('username') >= 0) return 'username';
    const fields = groupFields(el);
    const firstPassword = fields.findIndex(isPassword);
    if (firstPassword <= 0 || fields.filter((f) => isTextLike(f) && !isPassword(f)).length > 3) return null;
    // Só o campo de texto imediatamente antes da primeira senha.
    if (fields[firstPassword - 1] !== el) return null;
    const password = fields[firstPassword];
    return isTransactionSecret(password) || irreversibleSubmit(password) ? 'hidden' : 'username';
  };

  // -- seletores ---------------------------------------------------------------

  const GENERATED_ID = /(^|[-_:])(ember|ext-gen|ext-comp|yui|gwt-uid|j_idt|react-select|mui|radix|headlessui|__bvid__|vaadin|cdk|mat-|ui-id|tippy|rc-|rc_)|:|[0-9]{3,}|[0-9a-f]{8,}/i;
  const stableId = (id) => !!id && id.length <= 40 && !GENERATED_ID.test(id);
  const cssEscape = (s) => (W.CSS && W.CSS.escape ? W.CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, '\\$&'));

  const cssPath = (el) => {
    const root = el.getRootNode ? el.getRootNode() : el.ownerDocument;
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 6) {
      if (stableId(cur.id)) {
        let unique = false;
        try { unique = root.querySelectorAll('#' + cssEscape(cur.id)).length === 1; } catch (e) { /* ignora */ }
        if (unique) { parts.unshift('#' + cssEscape(cur.id)); break; }
      }
      let part = cur.tagName.toLowerCase();
      const parent = cur.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
      }
      parts.unshift(part);
      if (cur.tagName === 'BODY') break;
      cur = parent;
    }
    return parts.join(' > ');
  };

  const framePath = () => {
    const path = [];
    try {
      let w = W;
      while (w !== w.top) {
        const fe = w.frameElement;
        if (!fe) return null; // quadro de outra origem: o Python completa
        path.unshift({
          tag: fe.tagName.toLowerCase(),
          id: orNull(fe.id),
          name: orNull(fe.getAttribute('name')),
          title: orNull(clip(fe.getAttribute('title'), MAX_NAME)),
          src: orNull(clip(fe.getAttribute('src'), 500)),
          css: orNull(cssPath(fe)),
        });
        w = w.parent;
      }
    } catch (e) {
      return null;
    }
    return path;
  };

  // -- contagens (para escolher localizadores únicos) --------------------------

  const ROLE_SELECTORS = {
    button: 'button, input[type=button], input[type=submit], input[type=reset], input[type=image], input[type=file], [role]',
    link: 'a[href], area[href], [role]',
    textbox: 'input, textarea, [role]',
    searchbox: 'input, [role]',
    combobox: 'select, input, [role]',
    listbox: 'select, [role]',
    checkbox: 'input[type=checkbox], [role]',
    radio: 'input[type=radio], [role]',
    spinbutton: 'input, [role]',
    heading: 'h1, h2, h3, h4, h5, h6, [role]',
    row: 'tr, [role]',
    cell: 'td, [role]',
    option: 'option, [role]',
  };

  const scan = (root, selector) => {
    let all;
    try { all = root.querySelectorAll(selector || '*'); } catch (e) { return []; }
    return all.length > MAX_SCAN ? null : Array.from(all);
  };

  const countRoleName = (root, role, name) => {
    if (!role || !name) return null;
    const all = scan(root, ROLE_SELECTORS[role]);
    if (!all) return null;
    let n = 0;
    for (const el of all) {
      if (roleOf(el) !== role || hiddenForAria(el)) continue;
      if (accessibleName(el, role) === name) n++;
    }
    return n;
  };

  const countLabel = (root, label) => {
    if (!label) return null;
    const all = scan(root, 'input, select, textarea, button, meter, output, progress, [aria-label], [aria-labelledby]');
    if (!all) return null;
    let n = 0;
    for (const el of all) if (labelText(el) === label) n++;
    return n;
  };

  const countAttr = (root, attr, value) => {
    if (!value) return null;
    const all = scan(root, '[' + attr + ']');
    if (!all) return null;
    return all.filter((el) => el.getAttribute(attr) === value).length;
  };

  const countText = (root, text) => {
    if (!text) return null;
    const all = scan(root, '*');
    if (!all) return null;
    let n = 0;
    for (const el of all) {
      const tag = el.tagName;
      if (tag === 'SCRIPT' || tag === 'STYLE' || tag === 'HTML' || tag === 'HEAD') continue;
      if (norm(el.textContent) !== text) continue;
      let childMatches = false;
      for (const child of el.children) if (norm(child.textContent) === text) { childMatches = true; break; }
      if (!childMatches) n++;
    }
    return n;
  };

  // -- linha de tabela ----------------------------------------------------------

  // Texto de um elemento como o Playwright o compara em getByText: o texto do DOM
  // (textContent, sem script e estilo, com o valor de botões <input> e o shadow
  // root), sem U+200B e U+00AD e com os brancos juntados. Não é o innerText, que
  // passa pelo CSS (text-transform, elementos escondidos, <br>).
  const SKIP_TEXT = new Set(['SCRIPT', 'NOSCRIPT', 'STYLE']);
  const pwNormalize = (s) => String(s).replace(/[\u200b\u00ad]/g, '').trim().replace(/\s+/g, ' ');
  const domText = (node, cache, depth) => {
    let out = cache.get(node);
    if (out !== undefined) return out;
    out = '';
    if (node.nodeType === 1 && SKIP_TEXT.has(node.nodeName)) {
      out = '';
    } else if (node.nodeType === 1 && node.tagName === 'INPUT' && ['submit', 'button', 'reset'].indexOf(node.type) >= 0) {
      out = String(node.value);
    } else if (depth < 60) {
      for (let child = node.firstChild; child; child = child.nextSibling) {
        if (child.nodeType === 3) out += child.nodeValue || '';
        else if (child.nodeType === 1) out += domText(child, cache, depth + 1);
      }
      if (node.shadowRoot) out += domText(node.shadowRoot, cache, depth + 1);
    }
    cache.set(node, out);
    return out;
  };

  // Chave da linha: o texto de uma célula que, comparado inteiro e com maiúsculas
  // (como o executor compara), só aparece numa linha visível do documento.
  const rowKey = (row, cells) => {
    const root = row.getRootNode ? row.getRootNode() : row.ownerDocument;
    const rows = scan(root, 'tr, [role=row]');
    if (!rows) return null;
    const visible = rows.filter((r) => roleOf(r) === 'row' && !hiddenForAria(r));
    if (visible.indexOf(row) < 0) return null;
    let budget = MAX_SCAN;
    const inside = new Map();
    for (const r of visible) {
      const all = r.querySelectorAll('*');
      budget -= all.length;
      if (budget < 0) return null;
      inside.set(r, all);
    }
    const cache = new Map();
    const normalized = new Map();
    const textOf = (node) => {
      let t = normalized.get(node);
      if (t === undefined) { t = pwNormalize(domText(node, cache, 0)); normalized.set(node, t); }
      return t;
    };
    for (const cell of cells) {
      const key = textOf(cell);
      if (key.length < 2 || key.length >= 200) continue;
      let hits = 0;
      let mine = false;
      for (const r of visible) {
        let found = false;
        for (const child of inside.get(r)) {
          if (textOf(child) === key) { found = true; break; }
        }
        if (!found) continue;
        hits += 1;
        if (r === row) mine = true;
        if (hits > 1) break;
      }
      if (hits === 1 && mine) return key;
    }
    return null;
  };

  const rowInfo = (el, role, name, text) => {
    const row = el.closest('tr, [role=row]');
    if (!row) return null;
    const cellElements = Array.from(row.querySelectorAll('td, th, [role=cell], [role=gridcell], [role=rowheader], [role=columnheader]'))
      .slice(0, 20);
    const cells = cellElements.map((c) => clip(c.innerText || c.textContent, 200));
    const key = rowKey(row, cellElements);
    return {
      text: clip(row.innerText || row.textContent, 500),
      cells,
      key,
      role_name_count: countRoleName(row, role, name),
      text_count: countText(row, text),
    };
  };

  // -- descritor ----------------------------------------------------------------

  const INTERACTIVE = 'button, a[href], area[href], input, select, textarea, label, summary, option, ' +
    '[role=button], [role=link], [role=menuitem], [role=menuitemcheckbox], [role=menuitemradio], ' +
    '[role=tab], [role=checkbox], [role=radio], [role=switch], [role=option], [role=treeitem], ' +
    '[onclick], [contenteditable=""], [contenteditable=true]';

  const actionTarget = (event) => {
    const path = event.composedPath ? event.composedPath() : [];
    const raw = path.length && path[0].nodeType === 1 ? path[0] : event.target;
    if (!raw || raw.nodeType !== 1) return null;
    return raw.closest(INTERACTIVE) || raw;
  };

  const formInfo = (el) => {
    const form = formOf(el);
    if (!form) return null;
    const fields = Array.from(form.elements);
    let action = null;
    try {
      const url = new URL(form.getAttribute('action') || '', el.ownerDocument.baseURI);
      action = url.origin + url.pathname;
    } catch (e) { /* ignora */ }
    return {
      action,
      method: norm(form.getAttribute('method')).toLowerCase() || 'get',
      has_password: fields.some(isPassword),
      has_one_time_code: fields.some(isOneTimeCode),
      text_fields: fields.filter((f) => isTextLike(f) && !isOneTimeCode(f)).length,
    };
  };

  const describe = (el) => {
    const role = roleOf(el);
    const name = clip(accessibleName(el, role), MAX_NAME);
    const label = clip(labelText(el), MAX_NAME);
    const placeholder = clip(el.getAttribute('placeholder'), MAX_NAME);
    const testId = clip(el.getAttribute('data-testid'), MAX_NAME);
    const nameAttr = clip(el.getAttribute('name'), MAX_NAME);
    const id = clip(el.id, MAX_NAME);
    const tag = el.tagName.toLowerCase();
    const type = inputType(el);
    const password = isPassword(el);
    const otp = isOneTimeCode(el);
    const login = !password && !otp ? loginRole(el) : null;
    const byName = !password && !otp && login === null && isPasswordByName(el);
    const isControl = tag === 'input' || tag === 'select' || tag === 'textarea';
    let text = '';
    if (!isControl) text = clip(el.innerText || el.textContent, MAX_TEXT);
    else if (tag === 'input' && BUTTON_TYPES.has(type)) text = clip(el.getAttribute('value'), MAX_TEXT);
    const root = el.getRootNode ? el.getRootNode() : el.ownerDocument;
    const textForCount = text && text.length <= 100 ? text : null;
    return {
      tag,
      input_type: type,
      role: orNull(role),
      name: orNull(name),
      label: orNull(label),
      placeholder: orNull(placeholder),
      test_id: orNull(testId),
      name_attr: orNull(nameAttr),
      id: orNull(id),
      css: cssPath(el),
      text: orNull(text),
      title: orNull(clip(el.getAttribute('title'), MAX_NAME)),
      href: tag === 'a' && el.getAttribute('href') ? clip(el.href, 500) : null,
      autocomplete: orNull(clip(el.getAttribute('autocomplete'), 100)),
      frame_path: framePath(),
      is_password: password,
      is_one_time_code: otp,
      is_username: login === 'username',
      sensitive: password || otp || login !== null || byName,
      row: rowInfo(el, role, name, textForCount),
      form: formInfo(el),
      counts: {
        role_name: countRoleName(root, role, name),
        label: countLabel(root, label),
        placeholder: countAttr(root, 'placeholder', placeholder),
        test_id: countAttr(root, 'data-testid', testId),
        text: countText(root, textForCount),
        id: countAttr(root, 'id', id),
        name_attr: countAttr(root, 'name', nameAttr),
      },
    };
  };

  // -- máscaras e valores pendentes ---------------------------------------------

  // elemento -> {session, typed, typedKnown, scriptSet, lastSent, since}. Uma "sessão" vai da
  // primeira tecla até o envio do valor; `typed` só vale se ela começou com o campo
  // vazio ou com todo o texto selecionado (substituição).
  const fields = new Map();
  let editing = null;

  const fieldState = (el) => {
    let st = fields.get(el);
    if (!st) {
      st = { session: false, typed: '', typedKnown: true, scriptSet: false, lastSent: undefined, since: null };
      fields.set(el, st);
    }
    return st;
  };

  const allSelected = (el) => {
    try {
      return el.selectionStart === 0 && el.selectionEnd === String(el.value).length;
    } catch (e) {
      return false;
    }
  };

  const startSession = (el, st, known) => {
    if (st.session) return;
    st.session = true;
    st.typed = '';
    st.typedKnown = known;
    // Começo da edição: o que a página gravar daqui até o valor sair (salvamento
    // automático com atraso) conta no efeito do passo.
    st.since = clock();
  };

  const hookValueSetter = (proto) => {
    try {
      const desc = Object.getOwnPropertyDescriptor(proto, 'value');
      if (!desc || !desc.set || !desc.get) return;
      Object.defineProperty(proto, 'value', {
        configurable: true,
        enumerable: desc.enumerable,
        get() { return desc.get.call(this); },
        set(v) {
          if (this === editing) { const st = fields.get(this); if (st) st.scriptSet = true; }
          desc.set.call(this, v);
        },
      });
    } catch (e) { /* ignora */ }
  };
  hookValueSetter(W.HTMLInputElement && W.HTMLInputElement.prototype);
  hookValueSetter(W.HTMLTextAreaElement && W.HTMLTextAreaElement.prototype);

  const currentValue = (el) => {
    if (el.isContentEditable) return el.innerText;
    return el.value;
  };

  const sendField = (el) => {
    const st = fields.get(el);
    if (!st || !el.isConnected) { fields.delete(el); return; }
    const value = currentValue(el);
    if (value === st.lastSent) return;
    const desc = describe(el);
    const sensitive = desc.sensitive;
    send({
      kind: 'input',
      element: desc,
      value: sensitive ? null : String(value).slice(0, MAX_VALUE),
      // Só para o gravador apagar este valor dos textos capturados; nunca é gravado.
      secret: sensitive ? String(value).slice(0, MAX_VALUE) : null,
      typed: sensitive || !st.typedKnown ? null : st.typed.slice(0, MAX_VALUE),
      script_modified: sensitive ? false : st.scriptSet,
      // Há quanto tempo a pessoa começou a editar (o gravador converte no relógio dele).
      editing_ms: st.session && typeof st.since === 'number'
        ? Math.max(0, Math.round(clock() - st.since)) : null,
    });
    st.lastSent = value;
    st.session = false;
    st.since = null;
    st.typed = '';
    st.typedKnown = true;
    st.scriptSet = false;
  };

  const flushPending = (except) => {
    for (const el of Array.from(fields.keys())) if (el !== except) sendField(el);
  };

  const isEditable = (el) => isTextLike(el) || isPassword(el) || el.isContentEditable ||
    (el.tagName === 'INPUT' && ['date', 'time', 'datetime-local', 'month', 'week', 'color', 'range'].indexOf(inputType(el)) >= 0);

  const onBeforeInput = (event) => {
    if (!event.isTrusted) return;
    const el = event.target;
    if (!el || el.nodeType !== 1 || !isEditable(el)) return;
    const st = fieldState(el);
    startSession(el, st, el.isContentEditable ? false : currentValue(el) === '' || allSelected(el));
  };

  const onInput = (event) => {
    if (!event.isTrusted) return;
    const el = event.target;
    if (!el || el.nodeType !== 1 || !isEditable(el)) return;
    const st = fieldState(el);
    startSession(el, st, false);
    const type = event.inputType || '';
    if (type.indexOf('insert') === 0 && typeof event.data === 'string') st.typed += event.data;
    else st.typedKnown = false;
    editing = el;
    const before = currentValue(el);
    setTimeout(() => {
      if (editing === el) editing = null;
      if (currentValue(el) !== before) st.scriptSet = true;
    }, 0);
  };

  const onKeyFlag = (event) => {
    const el = event.target;
    if (el && el.nodeType === 1 && isEditable(el)) {
      editing = el;
      setTimeout(() => { if (editing === el) editing = null; }, 0);
    }
  };
  const clearEditing = () => { editing = null; };

  const selectSent = new WeakMap();
  const onChange = (event) => {
    const el = event.target;
    if (!el || el.nodeType !== 1) return;
    const tag = el.tagName;
    const type = inputType(el);
    // Listas e arquivos aceitam eventos sintéticos: é assim que a automação (e a
    // visão ao vivo) escolhe opções e envia arquivos.
    if (!event.isTrusted && tag !== 'SELECT' && type !== 'file') return;
    if (tag === 'SELECT') {
      const options = Array.from(el.selectedOptions || []);
      const first = options[0];
      const key = options.map((o) => o.value).join('\u0000');
      if (selectSent.get(el) === key) return;
      selectSent.set(el, key);
      flushPending(el);
      send({
        kind: 'select',
        element: describe(el),
        label: first ? clip(first.label || first.text, 500) : '',
        value: first ? String(first.value).slice(0, 500) : '',
        multiple: options.length > 1,
      });
      return;
    }
    if (type === 'checkbox' || type === 'radio') {
      flushPending(el);
      send({ kind: 'check', element: describe(el), checked: !!el.checked });
      return;
    }
    if (type === 'file') {
      flushPending(el);
      const names = Array.from(el.files || []).slice(0, 10).map((f) => String(f.name).slice(0, 255));
      send({ kind: 'file', element: describe(el), file_names: names });
      return;
    }
    if (isEditable(el)) { fieldState(el); sendField(el); }
  };

  const onFocusOut = (event) => {
    const el = event.target;
    if (el && fields.has(el)) sendField(el);
  };

  const SKIP_CLICK_TYPES = new Set(['checkbox', 'radio', 'file']);
  const onClick = (event) => {
    if (!event.isTrusted || event.button !== 0) return;
    const el = actionTarget(event);
    if (!el) return;
    const tag = el.tagName;
    // Cliques que só focam ou ativam um campo: o change correspondente basta.
    if (tag === 'LABEL' && el.control) return;
    if (tag === 'SELECT' || tag === 'OPTION' || tag === 'TEXTAREA' || el.isContentEditable) return;
    if (tag === 'INPUT') {
      const type = inputType(el);
      if (!BUTTON_TYPES.has(type) && (isEditable(el) || SKIP_CLICK_TYPES.has(type))) return;
    }
    flushPending(null);
    send({ kind: 'click', element: describe(el), detail: event.detail | 0 });
  };

  const KEYS = new Set(['Enter', 'Tab', 'Escape']);
  const onKeyDown = (event) => {
    onKeyFlag(event);
    if (!event.isTrusted || event.isComposing || !KEYS.has(event.key)) return;
    const el = event.target && event.target.nodeType === 1 ? event.target : null;
    flushPending(null);
    const onPage = !el || el === el.ownerDocument.body || el === el.ownerDocument.documentElement;
    send({ kind: 'key', key: event.key, element: onPage ? null : describe(el) });
  };

  const opts = { capture: true, passive: true };
  W.addEventListener('beforeinput', onBeforeInput, opts);
  W.addEventListener('input', onInput, opts);
  W.addEventListener('keyup', onKeyFlag, opts);
  W.addEventListener('keypress', onKeyFlag, opts);
  W.addEventListener('keydown', onKeyDown, opts);
  W.addEventListener('change', onChange, opts);
  W.addEventListener('focusout', onFocusOut, opts);
  W.addEventListener('click', onClick, opts);
  // Depois de todos os ouvintes da página: fim da janela em que ela pode mascarar o valor.
  W.addEventListener('input', clearEditing, { capture: false, passive: true });
  W.addEventListener('pagehide', () => flushPending(null), opts);
  W.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flushPending(null); }, opts);

  Object.defineProperty(W, Symbol.for('rapa.recorder.stop'), {
    configurable: true,
    value: () => { flushPending(null); stopped = true; },
  });
})();
