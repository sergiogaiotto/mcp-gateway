// Gravador do RaPA: roda em todos os quadros da página e envia ao Python, por
// uma binding do Playwright, um descritor de cada ação da pessoa.
//
// Regras: só eventos confiáveis (isTrusted); nunca envia o valor de campos de
// senha (inclusive campo de texto mascarado por CSS, com rótulo de senha ou cujo
// aria-label ou aria-labelledby fala em senha, mesmo que ele também diga ser o
// usuário), de código de uso único, nem de nenhum outro campo de texto do grupo
// (o <form> ou, sem <form>, os campos soltos visíveis do documento e dos shadow
// roots abertos) que tem senha ou código: o usuário (pelo próprio campo) e os
// outros; o valor final de cada campo sai no change/blur (ou antes da próxima
// ação), com há quanto tempo a edição começou. Campos dentro de shadow roots
// abertos também contam: o alvo é o primeiro elemento do composedPath, listas,
// caixas de seleção e arquivos de um shadow root saem pelo evento input (que,
// ao contrário do change, atravessa o shadow root), os campos de texto saem no
// change e no focusout do próprio shadow root (cada shadow root que aparece no
// composedPath de um evento ganha ouvintes de change, blur e focusout, uma vez),
// e, no blur na captura da janela e do shadow root (também quando a pessoa sai
// para um iframe) e antes da próxima ação noutro elemento (pointerdown,
// keydown, beforeinput), o script só tira um retrato do campo e decide num
// setTimeout, depois dos ouvintes de blur do componente (o campo que virou
// senha ou código só sai como segredo; o que saiu da página sai com o retrato).
// O campo que o componente já tirou da página quando o change, o blur ou o
// focusout chega (ou quando a janela perde o foco, só campos de shadow root)
// sai com o retrato: o que o gravador tirou ao guardar o campo (na próxima ação
// ou no blur, com o campo na página) vale como o valor; com só o retrato de
// depois da última tecla (num setTimeout, com a tecla aplicada), o passo fica
// com o alvo, mas o valor só sai como segredo: o robô nunca o repete (pede o
// valor numa entrada) e ele nunca é gravado. O que saiu antes desse retrato é
// esquecido. As marcas de sigilo de um campo só sobem
// até o valor sair, e só os retratos de depois de uma tecla contam (o foco
// sozinho não tira retrato: o mesmo input que a página reaproveita de "Senha"
// para "Valor" não leva a marca de senha). O elemento leva o caminho dos hospedeiros (shadow_path) e um
// seletor CSS que atravessa os shadow roots abertos ("#hospedeiro > #campo"),
// para que dois componentes iguais (o mesmo id por dentro) nunca virem o mesmo
// campo. Cada elemento também leva um número (serial) que dois elementos nunca
// têm igual. Nenhum método de shadow DOM ou de eventos é embrulhado para isso; o
// único nativo trocado é a propriedade value de HTMLInputElement e
// HTMLTextAreaElement (hookValueSetter, o getter e o setter): o getter só
// repassa, e o setter só marca o valor que a página muda enquanto a pessoa
// digita.
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

  // -- shadow roots -------------------------------------------------------------

  const MAX_SHADOW_DEPTH = 16;
  // A raiz do nó (o documento ou um shadow root) e o hospedeiro de um shadow root.
  const rootOf = (node) => (node.getRootNode ? node.getRootNode() : node.ownerDocument);
  const hostOf = (root) =>
    (root && root.nodeType === 11 && root.host && root.host.nodeType === 1 ? root.host : null);
  const inShadow = (el) => hostOf(rootOf(el)) !== null;
  // Os hospedeiros dos shadow roots em volta do elemento, do mais externo ao mais interno.
  const shadowHosts = (el) => {
    const hosts = [];
    let host = hostOf(rootOf(el));
    while (host && hosts.length < 64) {
      hosts.unshift(host);
      host = hostOf(rootOf(host));
    }
    return hosts;
  };
  // O ancestral mais próximo que casa com o seletor, atravessando os hospedeiros
  // (a linha da tabela de uma caixa de seleção que está num componente).
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
  // O nó e os shadow roots abertos abaixo dele (também os aninhados), ou null se
  // forem grandes demais para olhar.
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
    // Os ids valem na mesma árvore (o documento ou o shadow root do campo).
    const root = el.getRootNode ? el.getRootNode() : el.ownerDocument;
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
  // No nome do campo (aria-label, aria-labelledby ou o nome acessível), a assinatura
  // eletrônica ou digital (a senha de transação dos bancos) também é um segredo: o valor
  // nunca sai, e o campo não vira senha do cofre (isPasswordByName). A mesma regra está
  // em rapa/recorder/sensitivity.py (mentions_secret_name).
  const SIGNATURE_WORDS = /assinatura (?:eletronica|digital)/;
  const mentionsSecretName = (s) => {
    const t = plainWords(s);
    return !!t && (PASSWORD_WORDS.test(t) || SIGNATURE_WORDS.test(t));
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

  // O aria-label ou o texto do aria-labelledby fala em senha: os dois, porque o
  // nome acessível guarda só um deles (o aria-labelledby, quando há).
  const ariaMentionsPassword = (el) =>
    mentionsSecretName(el.getAttribute('aria-label')) || mentionsSecretName(labelledByText(el));

  // Campo que só o nome (aria-label, aria-labelledby ou o nome acessível) diz ser
  // senha: o valor nunca sai, mas o campo não vira senha nem usuário, mesmo que o
  // próprio campo também diga ser o usuário (rapa/recorder/sensitivity.py,
  // password_by_name).
  const USERNAME_TYPES = new Set(['text', 'email', 'tel', 'number', '']);
  const isPasswordByName = (el) => {
    if (el.tagName !== 'INPUT' || isPassword(el) || isOneTimeCode(el)) return false;
    const type = inputType(el);
    if (!PASSWORD_LABEL_TYPES.has(type) && !USERNAME_TYPES.has(type)) return false;
    return ariaMentionsPassword(el) || mentionsSecretName(clip(accessibleName(el, roleOf(el)), MAX_NAME));
  };

  // Os textos do próprio campo falam de outra pessoa ou de um cadastro ("CPF do
  // favorecido", "Chave Pix (CPF)", "Login do novo usuário"): não é o usuário
  // (rapa/recorder/sensitivity.py, names_another_party).
  const OTHER_PARTY_WORDS = /(?:^|[^a-z0-9])(?:favorecid[oa]s?|destinatari[oa]s?|beneficiari[oa]s?|pagador(?:es|as?)?|recebedor(?:es|as?)?|titular(?:es)?|clientes?|contatos?|convidad[oa]s?|nov[oa]s?|cadastr(?:os?|ar|e)|chaves?)(?:[^a-z0-9]|$)/;
  const namesAnotherParty = (el) =>
    [labelText(el), accessibleName(el, roleOf(el)), el.getAttribute('title'),
      el.getAttribute('placeholder'), el.getAttribute('name'), el.id]
      .some((s) => { const t = plainWords(clip(s, MAX_NAME)); return !!t && OTHER_PARTY_WORDS.test(t); });

  // Usuário só pelo próprio campo (rapa/recorder/sensitivity.py,
  // username_by_signal): autocomplete de usuário ou e-mail, type=email, ou rótulo,
  // atributo name, id ou placeholder com uma palavra inteira de usuário. O texto
  // da página (títulos, parágrafos) não conta; o campo que o nome diz ser senha e o
  // que fala de outra pessoa não são o usuário.
  const USERNAME_WORDS = /(?:^|[^a-z0-9])(?:usuario|user|username|login|e-mail|email|cpf|cnpj|matricula|codigo do usuario)(?:[^a-z0-9]|$)/;
  const isUsernameField = (el) => {
    if (el.tagName !== 'INPUT' || !USERNAME_TYPES.has(inputType(el))) return false;
    if (isPassword(el) || isOneTimeCode(el) || isPasswordByName(el) || namesAnotherParty(el)) return false;
    const ac = norm(el.getAttribute('autocomplete')).toLowerCase().split(' ');
    if (ac.indexOf('username') >= 0 || ac.indexOf('email') >= 0 || inputType(el) === 'email') return true;
    return [labelText(el), el.getAttribute('name'), el.id, el.getAttribute('placeholder')]
      .some((s) => { const t = plainWords(s); return !!t && USERNAME_WORDS.test(t); });
  };

  const isSecretField = (el) => isPassword(el) || isOneTimeCode(el) || isPasswordByName(el);

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

  // Um campo visível de senha ou código nas raízes do documento (o documento e os
  // shadow roots abertos), fora da raiz ``skip``; ``loose``: só os sem <form>.
  const secretIn = (el, loose, skip) => {
    const roots = rootsUnder(el.ownerDocument);
    if (!roots) return false;
    for (const root of roots) {
      if (root === skip) continue;
      let found;
      try { found = root.querySelectorAll('input'); } catch (e) { continue; }
      for (const f of found) {
        if (f !== el && (!loose || !formOf(f)) && !hiddenForAria(f) && isSecretField(f)) return true;
      }
    }
    return false;
  };

  // Campo sem <form>: um campo solto e visível de senha ou código noutra raiz do
  // documento (o documento ou outro shadow root aberto) também conta.
  const looseSecretElsewhere = (el) => !formOf(el) && secretIn(el, true, rootOf(el));

  // 'username': o usuário pelo próprio campo; 'hidden': outro campo de texto do
  // grupo, que não vai na gravação mas também não é o usuário; null: campo comum
  // (o grupo não tem senha nem código). O campo que a página declara como usuário
  // (autocomplete=username) nunca vai na gravação: é o usuário, a menos que o nome
  // dele fale de outra pessoa ou de um cadastro. O campo de um shadow root sai da
  // gravação com qualquer senha ou código visível no documento, em qualquer raiz e
  // em qualquer <form> (ele nunca é credencial de login: o compilador pede o valor
  // numa entrada).
  const loginRole = (el) => {
    if (!isTextLike(el) || isSecretField(el)) return null;
    const ac = norm(el.getAttribute('autocomplete')).toLowerCase().split(' ');
    const declared = ac.indexOf('username') >= 0;
    if (!declared && !groupFields(el).some((f) => f !== el && isSecretField(f)) &&
        !looseSecretElsewhere(el) && !(inShadow(el) && secretIn(el, false, null))) return null;
    return isUsernameField(el) ? 'username' : 'hidden';
  };

  // -- seletores ---------------------------------------------------------------

  const GENERATED_ID = /(^|[-_:])(ember|ext-gen|ext-comp|yui|gwt-uid|j_idt|react-select|mui|radix|headlessui|__bvid__|vaadin|cdk|mat-|ui-id|tippy|rc-|rc_)|:|[0-9]{3,}|[0-9a-f]{8,}/i;
  const stableId = (id) => !!id && id.length <= 40 && !GENERATED_ID.test(id);
  const cssEscape = (s) => (W.CSS && W.CSS.escape ? W.CSS.escape(s) : s.replace(/[^a-zA-Z0-9_-]/g, '\\$&'));

  const MAX_CSS = 1000;
  // Caminho do elemento dentro da raiz dele. No documento, como sempre: até um id
  // estável e único, o <body> ou seis níveis. Num shadow root, até o topo dele (o
  // id estável e único na raiz entra no lugar da etiqueta, mas o caminho continua):
  // ``anchored`` diz que ele começa no topo do shadow root (ou num id único do
  // documento, ou no <body>).
  const segment = (el) => {
    const root = rootOf(el);
    const shadow = hostOf(root) !== null;
    const parts = [];
    let anchored = false;
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < (shadow ? 10 : 6)) {
      let idPart = null;
      if (stableId(cur.id)) {
        let unique = false;
        try { unique = root.querySelectorAll('#' + cssEscape(cur.id)).length === 1; } catch (e) { /* ignora */ }
        if (unique) idPart = '#' + cssEscape(cur.id);
      }
      if (idPart && !shadow) { parts.unshift(idPart); anchored = true; break; }
      const parent = cur.parentElement;
      const top = !parent && shadow && cur.parentNode === root;
      let part = idPart;
      if (!part) {
        part = cur.tagName.toLowerCase();
        const siblings = parent || (top ? root : null);
        if (siblings) {
          const same = Array.from(siblings.children).filter((c) => c.tagName === cur.tagName);
          if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
        }
      }
      parts.unshift(part);
      if (cur.tagName === 'BODY') { anchored = true; break; }
      if (top) { anchored = true; break; }
      cur = parent;
    }
    return { parts, anchored };
  };

  // Junta os caminhos dos hospedeiros e o do elemento num seletor que atravessa os
  // shadow roots abertos (o CSS do Playwright passa do hospedeiro para os filhos do
  // shadow root com ">"); o que não começa no topo do shadow root vai como
  // descendente. Longo demais: sobram os trechos de dentro (o seletor deixa de ser
  // único e o executor recusa, em vez de errar o elemento).
  const joinSegments = (segs) => {
    let css = '';
    segs.forEach((seg, i) => {
      const path = seg.parts.join(' > ');
      css = i === 0 ? path : css + (seg.anchored ? ' > ' : ' ') + path;
    });
    return css;
  };
  const fitCss = (segs) => {
    let rest = segs;
    let css = joinSegments(rest);
    while (css.length > MAX_CSS && rest.length > 1) {
      rest = rest.slice(1);
      css = joinSegments(rest);
    }
    if (css.length > MAX_CSS) {
      const parts = rest[0].parts.slice();
      while (parts.length > 1 && parts.join(' > ').length > MAX_CSS) parts.shift();
      css = parts.join(' > ').slice(0, MAX_CSS);
    }
    return css;
  };

  const cssPath = (el) => fitCss(shadowHosts(el).concat([el]).map(segment));

  // O caminho de cada hospedeiro, cada um dentro da sua raiz, do mais externo ao
  // mais interno (null fora de shadow roots): a identidade do campo leva isso.
  const shadowPath = (el) => {
    const hosts = shadowHosts(el);
    if (!hosts.length) return null;
    return hosts.slice(-MAX_SHADOW_DEPTH).map((host) => fitCss([segment(host)]));
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

  // O mesmo em várias raízes (os localizadores do Playwright atravessam os shadow
  // roots abertos); null se alguma for grande demais ou se não há raízes.
  const scanAll = (roots, selector) => {
    if (!roots) return null;
    let out = [];
    for (const root of roots) {
      const found = scan(root, selector);
      if (!found) return null;
      out = out.concat(found);
      if (out.length > MAX_SCAN) return null;
    }
    return out;
  };

  const countRoleName = (roots, role, name) => {
    if (!role || !name) return null;
    const all = scanAll(roots, ROLE_SELECTORS[role]);
    if (!all) return null;
    let n = 0;
    for (const el of all) {
      if (roleOf(el) !== role || hiddenForAria(el)) continue;
      if (accessibleName(el, role) === name) n++;
    }
    return n;
  };

  const countLabel = (roots, label) => {
    if (!label) return null;
    const all = scanAll(roots, 'input, select, textarea, button, meter, output, progress, [aria-label], [aria-labelledby]');
    if (!all) return null;
    let n = 0;
    for (const el of all) if (labelText(el) === label) n++;
    return n;
  };

  const countAttr = (roots, attr, value) => {
    if (!value) return null;
    const all = scanAll(roots, '[' + attr + ']');
    if (!all) return null;
    return all.filter((el) => el.getAttribute(attr) === value).length;
  };

  const countText = (roots, text) => {
    if (!text) return null;
    const all = scanAll(roots, '*');
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

  // A linha também pode estar fora do shadow root do elemento (a caixa de seleção de
  // um componente numa célula): as contagens dentro dela atravessam os shadow roots.
  const rowInfo = (el, role, name, text) => {
    const row = closestComposed(el, 'tr, [role=row]');
    if (!row) return null;
    const cellElements = Array.from(row.querySelectorAll('td, th, [role=cell], [role=gridcell], [role=rowheader], [role=columnheader]'))
      .slice(0, 20);
    const cells = cellElements.map((c) => clip(c.innerText || c.textContent, 200));
    const key = rowKey(row, cellElements);
    const inside = rootsUnder(row);
    return {
      text: clip(row.innerText || row.textContent, 500),
      cells,
      key,
      role_name_count: countRoleName(inside, role, name),
      text_count: countText(inside, text),
    };
  };

  // -- descritor ----------------------------------------------------------------

  const INTERACTIVE = 'button, a[href], area[href], input, select, textarea, label, summary, option, ' +
    '[role=button], [role=link], [role=menuitem], [role=menuitemcheckbox], [role=menuitemradio], ' +
    '[role=tab], [role=checkbox], [role=radio], [role=switch], [role=option], [role=treeitem], ' +
    '[onclick], [contenteditable=""], [contenteditable=true]';

  // O elemento em que o evento aconteceu: dentro de um shadow root aberto, o
  // primeiro do composedPath (o event.target da janela é o hospedeiro).
  const realTarget = (event) => {
    const path = event.composedPath ? event.composedPath() : [];
    const first = path.length ? path[0] : null;
    return first && first.nodeType === 1 ? first : event.target;
  };

  const actionTarget = (event) => {
    const raw = realTarget(event);
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

  // Identidade do elemento na gravação: um número por elemento do DOM, o mesmo em
  // todos os eventos dele (dois elementos nunca têm o mesmo). A parte alta é
  // sorteada por documento (cada quadro e cada carga da página tem o seu script),
  // a baixa conta os elementos; passado o limite, o elemento fica sem número (só a
  // descrição separa os campos, como nas gravações antigas).
  const ELEMENT_BASE = (() => {
    try { const a = new Uint32Array(1); W.crypto.getRandomValues(a); return a[0] & 0x7fffffff; } catch (e) {
      return Math.floor(Math.random() * 0x7fffffff);
    }
  })();
  const MAX_SERIALS = 0x1fffff;
  const serials = new WeakMap();
  let serialCount = 0;
  const serialOf = (el) => {
    let serial = serials.get(el);
    if (serial === undefined) {
      if (serialCount >= MAX_SERIALS) return null;
      serialCount += 1;
      serial = ELEMENT_BASE * (MAX_SERIALS + 1) + serialCount;
      serials.set(el, serial);
    }
    return serial;
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
    const byName = isPasswordByName(el);
    const login = loginRole(el);
    const isControl = tag === 'input' || tag === 'select' || tag === 'textarea';
    let text = '';
    if (!isControl) text = clip(el.innerText || el.textContent, MAX_TEXT);
    else if (tag === 'input' && BUTTON_TYPES.has(type)) text = clip(el.getAttribute('value'), MAX_TEXT);
    // As contagens valem no documento e nos shadow roots abertos dele, como o
    // executor procura. Documento grande demais: o do documento conta só nele, como
    // antes; o de um shadow root fica sem contagem (a conferência do Playwright decide).
    const shadow = inShadow(el);
    const root = rootsUnder(el.ownerDocument) || (shadow ? null : [el.ownerDocument]);
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
      shadow_path: shadow ? shadowPath(el) : null,
      serial: serialOf(el),
      text: orNull(text),
      title: orNull(clip(el.getAttribute('title'), MAX_NAME)),
      href: tag === 'a' && el.getAttribute('href') ? clip(el.href, 500) : null,
      autocomplete: orNull(clip(el.getAttribute('autocomplete'), 100)),
      frame_path: framePath(),
      is_password: password,
      is_one_time_code: otp,
      is_username: login === 'username',
      sensitive: password || otp || login !== null || byName,
      aria_mentions_password: tag === 'input' && ariaMentionsPassword(el),
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

  // elemento -> {session, typed, typedKnown, scriptSet, lastSent, since, deferred}. Uma
  // "sessão" vai da primeira tecla até o envio do valor; `typed` só vale se ela começou
  // com o campo vazio ou com todo o texto selecionado (substituição); `deferred`: o
  // gravador guardou o campo e decide depois dos ouvintes do componente (deferField).
  const fields = new Map();
  let editing = null;

  const fieldState = (el) => {
    let st = fields.get(el);
    if (!st) {
      st = {
        session: false, typed: '', typedKnown: true, scriptSet: false, lastSent: undefined, since: null,
        deferred: false,
      };
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

  // Troca a propriedade value (o getter só repassa ao nativo; o setter marca o
  // valor que a página muda enquanto a pessoa digita e chama o nativo).
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
          // Um valor posto por script depois do retrato: o retrato deixa de valer como o
          // valor do campo (goneDescriptor).
          const seen = lastSeen.get(this);
          if (seen) seen.taken = false;
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

  // elemento -> {desc, value, fresh, pending}: o retrato do campo, o descritor e o
  // valor tirados juntos, com o campo na página: depois de cada tecla confiável
  // (beforeinput ou input, num setTimeout, com a tecla já aplicada) e quando o
  // gravador guarda o campo para decidir depois (blur, pointerdown, keydown,
  // beforeinput), nunca só no foco. Na tecla, o valor fica desconhecido até o
  // retrato seguinte: o campo que sai da página antes dele é esquecido (nunca sai
  // com um valor de antes da tecla). As marcas de sigilo do retrato só sobem
  // (raise) até o valor sair; depois do envio, o retrato recomeça (o próximo valor é
  // outra edição). O retrato só é usado pelo campo que já saiu da página quando o
  // change, o blur ou o focusout chega (o componente trocou o input antes do ouvinte
  // do gravador), e então o valor só sai como segredo (goneDescriptor).
  const lastSeen = new WeakMap();
  // O último campo em que a pessoa digitou (beforeinput ou input confiável).
  let lastTyped = null;
  const SECRET_MARKS = ['is_password', 'is_one_time_code', 'is_username', 'sensitive', 'aria_mentions_password'];
  // O descritor novo com as marcas de sigilo que o campo já teve nesta edição.
  const raise = (desc, old) => {
    if (old) for (const mark of SECRET_MARKS) if (old[mark]) desc[mark] = true;
    return desc;
  };
  const seenOf = (el) => {
    let seen = lastSeen.get(el);
    if (!seen) {
      seen = { desc: null, value: undefined, fresh: false, taken: false, pending: false };
      lastSeen.set(el, seen);
    }
    return seen;
  };
  // Retrato do campo na página; se o descritor não sai (exceção), fica o anterior.
  // ``taken``: o retrato que o gravador tirou ao guardar o campo para decidir depois
  // (deferField), com o campo na página, depois das teclas da pessoa; uma tecla ou um
  // valor posto por script depois dele o desfazem.
  const snapshot = (el, taken) => {
    const seen = seenOf(el);
    if (!el.isConnected) return;
    try {
      const desc = raise(describe(el), seen.desc);
      const value = currentValue(el);
      seen.desc = desc;
      seen.value = value;
      seen.fresh = true;
      seen.taken = taken === true;
    } catch (e) { /* ignora */ }
  };
  // Uma tecla da pessoa: o valor guardado fica desconhecido e o retrato sai depois
  // da tecla, se o campo ainda estiver na página.
  const keyPressed = (el) => {
    const seen = seenOf(el);
    seen.value = undefined;
    seen.fresh = false;
    seen.taken = false;
    if (seen.pending) return;
    seen.pending = true;
    setTimeout(() => { seen.pending = false; if (el.isConnected) snapshot(el); }, 0);
  };

  // O descritor guardado de um campo que saiu da página antes de o valor sair, com as
  // marcas de sigilo do retrato. O valor só vale como o que a pessoa digitou quando o
  // retrato é o que o gravador tirou ao guardar o campo para decidir depois
  // (``taken``: na próxima ação noutro elemento ou no blur, com o campo na página e
  // nenhuma tecla nem valor de script depois dele); então o campo leva pelo menos a
  // marca de sigilo que um envio com o campo na página teria agora (o próprio campo é
  // senha ou código, ou é o usuário ou outro campo de um login, ou há uma senha ou um
  // código em qualquer raiz do documento, ou o documento é grande demais para olhar).
  // Com só o retrato de depois da última tecla (o componente tirou o campo antes de o
  // gravador guardá-lo), o campo é sempre sigiloso: a página pode tê-lo mudado depois
  // do retrato (uma máscara que só aparece depois, uma tecla que ela aplica num
  // temporizador). O valor sigiloso só sai como segredo (para a limpeza dos textos
  // capturados) e nunca é repetido nem gravado: o robô pede o valor numa entrada, ou
  // usa a credencial do cofre se o retrato diz que o campo é senha, código ou usuário.
  const goneDescriptor = (el, seen) => {
    const out = Object.assign({}, seen.desc);
    let secret = true;
    try {
      if (isPassword(el)) out.is_password = true;
      if (isOneTimeCode(el)) out.is_one_time_code = true;
      secret = !seen.taken || out.sensitive || out.is_password || out.is_one_time_code || isSecretField(el) ||
        loginRole(el) !== null || !rootsUnder(el.ownerDocument) || secretIn(el, false, null);
    } catch (e) { /* na dúvida, sigiloso */ }
    if (secret) out.sensitive = true;
    return out;
  };

  // ``gone``: o campo pode ter saído da página (change, blur ou focusout dele, ou a
  // janela perdeu o foco); então o valor sai com o descritor e o valor guardados
  // (o retrato de depois da última tecla, ou o que o gravador tirou para decidir
  // depois: ``deferred``), se a pessoa digitou nele desde o último envio. Fora
  // disso, o campo que saiu da página é esquecido, como antes. Com o campo na
  // página, o descritor é o de agora, com as marcas de sigilo do retrato.
  const sendField = (el, gone) => {
    const st = fields.get(el);
    if (!st) return;
    const deferred = st.deferred;
    st.deferred = false;
    let value;
    let desc;
    if (el.isConnected) {
      value = currentValue(el);
      if (value === st.lastSent) return;
      const seen = lastSeen.get(el);
      desc = raise(describe(el), seen ? seen.desc : null);
    } else {
      fields.delete(el);
      const seen = gone || deferred ? lastSeen.get(el) : null;
      if (!seen || !seen.desc || !st.session || seen.value === undefined || seen.value === st.lastSent) return;
      value = seen.value;
      desc = goneDescriptor(el, seen);
    }
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
    // A próxima edição começa outro retrato (as marcas valem para o valor que saiu).
    const seen = lastSeen.get(el);
    if (seen) { seen.desc = null; seen.value = undefined; seen.fresh = false; seen.taken = false; }
  };

  const flushPending = (except) => {
    for (const el of Array.from(fields.keys())) if (el !== except) sendField(el);
  };

  // Guarda o campo (retrato) e decide num setTimeout, depois dos ouvintes do
  // componente (o blur dele pode trocar o campo por um "chip" ou virar
  // type=password): o campo na página sai com o descritor de agora (e as marcas de
  // sigilo do retrato: o que virou senha ou código só sai como segredo); o que saiu
  // da página sai com o retrato. Antes disso, um clique, um Enter ou um Tab
  // (flushPending) decidem na hora, do mesmo jeito. Só guarda o campo com um valor
  // que ainda não saiu; o que já saiu da página antes do evento segue ``gone``.
  const deferField = (el, gone) => {
    const st = fields.get(el);
    if (!st) return;
    if (!el.isConnected) { sendField(el, gone); return; }
    let value;
    try { value = currentValue(el); } catch (e) { return; }
    if (value === st.lastSent || st.deferred) return;
    st.deferred = true;
    snapshot(el, true);
    setTimeout(() => { if (fields.get(el) === st && st.deferred) sendField(el, true); }, 0);
  };

  // Antes de uma ação noutro elemento (pointerdown, keydown ou beforeinput), o
  // valor pendente dos campos de shadow roots sai, na ordem da pessoa: o gravador
  // guarda o campo e decide depois dos ouvintes de blur do componente (deferField).
  // Não sai o do próprio campo, nem o do campo que contém o alvo (um trecho de um
  // contenteditable). Os campos da luz já saem no change da janela.
  const flushShadowOthers = (event) => {
    if (!event.isTrusted) return;
    const target = realTarget(event);
    const el = target && target.nodeType === 1 ? target : null;
    for (const f of Array.from(fields.keys())) {
      if (f === el || (el && f.contains(el)) || !inShadow(f)) continue;
      deferField(f, false);
    }
  };

  const isEditable = (el) => isTextLike(el) || isPassword(el) || el.isContentEditable ||
    (el.tagName === 'INPUT' && ['date', 'time', 'datetime-local', 'month', 'week', 'color', 'range'].indexOf(inputType(el)) >= 0);

  const onBeforeInput = (event) => {
    if (!event.isTrusted) return;
    const el = realTarget(event);
    if (!el || el.nodeType !== 1 || !isEditable(el)) return;
    const st = fieldState(el);
    startSession(el, st, el.isContentEditable ? false : currentValue(el) === '' || allSelected(el));
    keyPressed(el);
    lastTyped = el;
  };

  const onInput = (event) => {
    const el = realTarget(event);
    if (!el || el.nodeType !== 1) return;
    // O change não sai do shadow root (não é composed); o input, sim, e o
    // navegador o dispara antes do change em listas, caixas de seleção e arquivos.
    if (isChoice(el) && inShadow(el)) { onChoice(el, event); return; }
    if (!event.isTrusted || !isEditable(el)) return;
    const st = fieldState(el);
    startSession(el, st, false);
    const type = event.inputType || '';
    if (type.indexOf('insert') === 0 && typeof event.data === 'string') st.typed += event.data;
    else st.typedKnown = false;
    editing = el;
    keyPressed(el);
    lastTyped = el;
    const before = currentValue(el);
    setTimeout(() => {
      if (editing === el) editing = null;
      if (currentValue(el) !== before) st.scriptSet = true;
    }, 0);
  };

  const onKeyFlag = (event) => {
    const el = realTarget(event);
    if (el && el.nodeType === 1 && isEditable(el)) {
      editing = el;
      setTimeout(() => { if (editing === el) editing = null; }, 0);
    }
  };
  const clearEditing = () => { editing = null; };

  const selectSent = new WeakMap();
  // Lista, caixa de seleção, opção ou arquivo: o valor sai na hora (change ou, num
  // shadow root, input).
  const isChoice = (el) => el.tagName === 'SELECT' ||
    (el.tagName === 'INPUT' && ['checkbox', 'radio', 'file'].indexOf(inputType(el)) >= 0);

  const onChoice = (el, event) => {
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
    }
  };

  const onChange = (event) => {
    const el = realTarget(event);
    if (!el || el.nodeType !== 1) return;
    if (isChoice(el)) { onChoice(el, event); return; }
    if (event.isTrusted && isEditable(el)) { fieldState(el); sendField(el, true); }
  };

  // focusout: o valor sai na hora (depois dos ouvintes de blur do campo).
  const onFocusOut = (event) => {
    const el = realTarget(event);
    if (el && fields.has(el)) sendField(el, event.isTrusted);
  };

  // O blur confiável de um campo de shadow root, na fase de captura da janela e do
  // shadow root: chega antes do ouvinte de blur do próprio campo (que pode trocá-lo
  // por um "chip" ou virar type=password) e não depende de um pointerdown ou keydown
  // neste documento, que não há quando a pessoa sai para um iframe. O gravador só
  // guarda o campo e decide depois desses ouvintes (deferField).
  const onBlurField = (event) => {
    if (!event.isTrusted) return;
    const el = realTarget(event);
    if (el && el.nodeType === 1 && fields.has(el)) deferField(el, true);
  };

  // O blur na captura da janela vale para os campos de shadow roots (o change de um
  // campo da luz já chega primeiro ao gravador, na janela; ele continua saindo no
  // change ou no focusout, como antes). O blur da própria janela (o foco foi para
  // um iframe ou para fora): o campo de shadow root em que a pessoa digitou por
  // último, se já saiu da página sem change, blur nem focusout que o gravador visse
  // (o componente o trocou no próprio change, antes do gravador), sai com o retrato
  // de depois da última tecla. O campo da luz que saiu assim é esquecido, como antes.
  const onBlur = (event) => {
    if (event.target !== W) {
      const el = realTarget(event);
      if (el && el.nodeType === 1 && inShadow(el)) onBlurField(event);
      return;
    }
    if (!event.isTrusted) return;
    const el = lastTyped;
    if (!el || el.isConnected || !fields.has(el)) return;
    const seen = lastSeen.get(el);
    if (seen && seen.desc && seen.desc.shadow_path) sendField(el, true);
  };

  // O change de um campo de texto num shadow root aberto: ele não sai do shadow
  // root (não é composed), e o navegador o dispara antes do blur, quando o campo
  // ainda existe. Listas, caixas de seleção e arquivos de shadow roots já saem
  // pelo input (onInput).
  const onShadowChange = (event) => {
    const el = realTarget(event);
    if (!el || el.nodeType !== 1 || isChoice(el)) return;
    if (event.isTrusted && isEditable(el)) { fieldState(el); sendField(el, true); }
  };

  // Shadow roots abertos: cada um que aparece no composedPath de um evento que
  // atravessa a fronteira (focusin, pointerdown, mousedown, keydown, beforeinput,
  // input) ganha, uma vez só, ouvintes de change, blur e focusout na fase de
  // captura (o blur e o focusout entre dois campos do mesmo shadow root não chegam
  // à janela). Vale para os shadow roots declarativos e os criados antes deste
  // script; nenhum método nativo é embrulhado.
  const watchedRoots = new WeakSet();
  const watchRoots = (event) => {
    let path;
    try { path = event.composedPath(); } catch (e) { return; }
    for (const node of path) {
      if (!node || node.nodeType !== 11 || !hostOf(node) || watchedRoots.has(node)) continue;
      watchedRoots.add(node);
      try {
        node.addEventListener('change', onShadowChange, { capture: true, passive: true });
        node.addEventListener('blur', onBlurField, { capture: true, passive: true });
        node.addEventListener('focusout', onFocusOut, { capture: true, passive: true });
      } catch (e) { /* ignora */ }
    }
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
    const target = realTarget(event);
    const el = target && target.nodeType === 1 ? target : null;
    flushPending(null);
    const onPage = !el || el === el.ownerDocument.body || el === el.ownerDocument.documentElement;
    send({ kind: 'key', key: event.key, element: onPage ? null : describe(el) });
  };

  const opts = { capture: true, passive: true };
  // Primeiro os shadow roots do caminho e o valor pendente de outro campo; depois a ação.
  for (const type of ['focusin', 'pointerdown', 'mousedown', 'keydown', 'beforeinput', 'input']) {
    W.addEventListener(type, watchRoots, opts);
  }
  for (const type of ['pointerdown', 'keydown', 'beforeinput']) {
    W.addEventListener(type, flushShadowOthers, opts);
  }
  W.addEventListener('beforeinput', onBeforeInput, opts);
  W.addEventListener('input', onInput, opts);
  W.addEventListener('keyup', onKeyFlag, opts);
  W.addEventListener('keypress', onKeyFlag, opts);
  W.addEventListener('keydown', onKeyDown, opts);
  W.addEventListener('change', onChange, opts);
  W.addEventListener('blur', onBlur, opts);
  W.addEventListener('focusout', onFocusOut, opts);
  W.addEventListener('click', onClick, opts);
  // Depois de todos os ouvintes da página: fim da janela em que ela pode mascarar o valor.
  W.addEventListener('input', clearEditing, { capture: false, passive: true });
  W.addEventListener('pagehide', () => flushPending(null), opts);
  W.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flushPending(null); }, opts);
  // Shadow roots: os eventos que atravessam a fronteira chegam à janela com o
  // composedPath; o change, o blur e o focusout que ficam dentro de um shadow root
  // aberto chegam pelos ouvintes que watchRoots põe nele. Nenhum método da página é
  // embrulhado.

  Object.defineProperty(W, Symbol.for('rapa.recorder.stop'), {
    configurable: true,
    value: () => { flushPending(null); stopped = true; },
  });
})();
