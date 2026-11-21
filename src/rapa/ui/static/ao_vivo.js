/*
 * Visão ao vivo da gravação (RaPA): o único JavaScript que o time mantém na interface.
 *
 * Desenha num <canvas> as imagens da tela do navegador da nuvem e manda o mouse, a roda
 * e o teclado da pessoa pelo websocket da API (/v1/sessoes/{id}/ao-vivo), aberto com um
 * bilhete de uso único que a página recebe do servidor (window.RAPA_AO_VIVO). O token de
 * acesso nunca chega aqui.
 *
 * Teclado: um <textarea> invisível sobre a tela recebe o foco. O texto (letras, acentos,
 * teclas mortas, composição do IME e o que se cola com Ctrl+V) sai do próprio campo, já
 * composto pelo navegador da pessoa, como uma mensagem "texto" (no navegador da nuvem,
 * Input.insertText). Só as teclas especiais (Enter, Tab, setas, Backspace...) e os atalhos
 * com Ctrl, Alt ou Meta vão como "tecla". Ctrl+V cola daqui, nunca da área de transferência
 * do navegador da nuvem.
 *
 * Se a conexão cair, pede um bilhete novo ao servidor e reconecta uma vez; uma segunda
 * janela da mesma gravação substitui esta (código 4001) e esta não reconecta.
 */
(function () {
  "use strict";

  var SPECIAL = {
    Enter: 1, Tab: 1, Backspace: 1, Delete: 1, Escape: 1, ArrowUp: 1, ArrowDown: 1,
    ArrowLeft: 1, ArrowRight: 1, Home: 1, End: 1, PageUp: 1, PageDown: 1, Insert: 1,
    F1: 1, F2: 1, F3: 1, F4: 1, F5: 1, F6: 1, F7: 1, F8: 1, F9: 1, F10: 1, F11: 1, F12: 1
  };
  var BUTTONS = { 0: "esquerdo", 1: "meio", 2: "direito" };
  var MAX_TEXT = 10000;
  var IDLE_WARNING_MS = 8 * 60 * 1000;

  function modifiers(e) {
    var altGraph = e.getModifierState && e.getModifierState("AltGraph");
    var value = 0;
    if (e.altKey && !altGraph) value |= 1;
    if (e.ctrlKey && !altGraph) value |= 2;
    if (e.metaKey) value |= 4;
    if (e.shiftKey) value |= 8;
    return value;
  }

  function clamp(value, low, high) {
    return Math.max(low, Math.min(high, Math.round(value)));
  }

  function el(tag, attrs, parent) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      if (key === "text") node.textContent = attrs[key];
      else if (key === "style") node.style.cssText = attrs[key];
      else node.setAttribute(key, attrs[key]);
    });
    if (parent) parent.appendChild(node);
    return node;
  }

  function emit(name, value) {
    if (typeof window.emitEvent === "function") window.emitEvent(name, value);
  }

  function LiveView(root, config) {
    this.root = root;
    this.config = config;
    this.ticket = config.bilhete;
    this.screen = { w: 1280, h: 800 };
    this.ws = null;
    this.reconnected = false;
    this.ended = false;
    this.decoding = false;
    this.pendingFrame = null;
    this.pendingMove = null;
    this.composing = false;
    this.lastInput = Date.now();
    this.build();
    this.connect();
  }

  LiveView.prototype.build = function () {
    var self = this;
    var bar = el("form", { style: "display:flex;gap:8px;margin-bottom:8px", "aria-label": "Endereço" }, this.root);
    this.address = el("input", {
      type: "text", "aria-label": "Endereço da página", autocomplete: "off",
      style: "flex:1;padding:6px 8px;border:1px solid #bbb;border-radius:4px;font:inherit"
    }, bar);
    el("button", { type: "submit", text: "Ir", style: "padding:6px 16px" }, bar);
    bar.addEventListener("submit", function (e) {
      e.preventDefault();
      var url = self.address.value.trim();
      if (!url) return;
      if (!/^https?:\/\//i.test(url)) url = "https://" + url;
      self.send({ tipo: "navegar", url: url });
      self.touch();
    });
    this.status = el("div", { role: "status", "aria-live": "polite", style: "min-height:1.4em;font-size:0.85em;color:#8a5300" }, this.root);
    var stage = el("div", { style: "position:relative;width:100%;background:#222;line-height:0" }, this.root);
    this.canvas = el("canvas", {
      width: String(this.screen.w), height: String(this.screen.h), tabindex: "-1",
      "aria-label": "Tela do navegador da gravação",
      style: "width:100%;height:auto;display:block;cursor:default;outline:none"
    }, stage);
    this.ctx = this.canvas.getContext("2d");
    this.keys = el("textarea", {
      "aria-label": "Teclado da gravação", autocomplete: "off", autocorrect: "off",
      autocapitalize: "off", spellcheck: "false",
      style: "position:absolute;left:0;top:0;width:1px;height:1px;opacity:0;border:0;padding:0;resize:none;overflow:hidden"
    }, stage);
    this.bindMouse();
    this.bindKeyboard();
    window.addEventListener("resize", function () { self.scheduleResize(); });
    setInterval(function () { self.checkIdle(); }, 15000);
  };

  LiveView.prototype.say = function (text) {
    this.status.textContent = text || "";
  };

  LiveView.prototype.touch = function () {
    this.lastInput = Date.now();
    if (this.idleWarned) {
      this.idleWarned = false;
      this.say("");
    }
  };

  LiveView.prototype.checkIdle = function () {
    if (!this.ended && Date.now() - this.lastInput > IDLE_WARNING_MS && !this.idleWarned) {
      this.idleWarned = true;
      this.say("Sem atividade: a gravação termina em cerca de 2 minutos se nada for feito.");
    }
  };

  // -- conexão -------------------------------------------------------------------

  LiveView.prototype.url = function () {
    var base = this.config.base;
    if (!base) base = (location.protocol === "https:" ? "wss://" : "ws://") + location.host;
    return base + this.config.caminho + "?bilhete=" + encodeURIComponent(this.ticket);
  };

  LiveView.prototype.connect = function () {
    var self = this;
    var ws = new WebSocket(this.url());
    ws.binaryType = "blob";
    this.ws = ws;
    ws.onopen = function () {
      self.say("");
      self.scheduleResize();
    };
    ws.onmessage = function (event) {
      if (typeof event.data === "string") self.onEvent(event.data);
      else self.onFrame(event.data);
    };
    ws.onclose = function (event) {
      if (self.ws !== ws) return;
      self.ws = null;
      if (self.ended) return;
      if (event.code === 4001) {
        self.ended = true;
        self.say("Esta gravação foi aberta em outra janela.");
        return;
      }
      if (!self.reconnected) {
        self.reconnected = true;
        self.say("A conexão caiu; reconectando…");
        emit("rapa_novo_bilhete");
        return;
      }
      self.say("A conexão com a gravação caiu. Recarregue a página para continuar.");
    };
  };

  LiveView.prototype.newTicket = function (ticket) {
    if (this.ended) return;
    this.ticket = ticket;
    this.connect();
  };

  LiveView.prototype.noTicket = function () {
    this.say("Não foi possível reconectar. Recarregue a página para continuar.");
  };

  LiveView.prototype.send = function (message) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message));
  };

  LiveView.prototype.onEvent = function (raw) {
    var event;
    try { event = JSON.parse(raw); } catch (e) { return; }
    switch (event.tipo) {
      case "pronta":
      case "tela":
        this.setScreen(event.largura, event.altura);
        break;
      case "pagina":
        if (document.activeElement !== this.address) this.address.value = event.url || "";
        break;
      case "aviso":
        this.say(event.mensagem);
        break;
      case "dialogo":
        this.answerDialog(event);
        break;
      case "fim":
        this.ended = true;
        this.say(event.motivo === "parada" ? "Gravação parada." : (event.mensagem || "A gravação terminou."));
        emit("rapa_sessao_fim", event.motivo || "");
        break;
      default:
        break;
    }
  };

  LiveView.prototype.answerDialog = function (event) {
    var self = this;
    // Um diálogo da página do portal: a pessoa responde aqui mesmo.
    setTimeout(function () {
      var kind = event.tipo_dialogo;
      if (kind === "prompt") {
        var text = window.prompt(event.texto || "", event.padrao || "");
        self.send({ tipo: "dialogo", aceitar: text !== null, texto: text });
      } else if (kind === "alert") {
        window.alert(event.texto || "");
        self.send({ tipo: "dialogo", aceitar: true });
      } else {
        self.send({ tipo: "dialogo", aceitar: window.confirm(event.texto || "") });
      }
      self.touch();
    }, 0);
  };

  LiveView.prototype.setScreen = function (w, h) {
    if (!w || !h) return;
    this.screen = { w: w, h: h };
    if (this.canvas.width !== w) this.canvas.width = w;
    if (this.canvas.height !== h) this.canvas.height = h;
  };

  LiveView.prototype.onFrame = function (blob) {
    // Sem fila: enquanto um quadro é decodificado, só o mais novo espera.
    if (this.decoding) {
      this.pendingFrame = blob;
      return;
    }
    var self = this;
    this.decoding = true;
    createImageBitmap(blob).then(function (bitmap) {
      self.ctx.drawImage(bitmap, 0, 0, self.canvas.width, self.canvas.height);
      if (bitmap.close) bitmap.close();
    }).catch(function () {}).then(function () {
      self.decoding = false;
      var next = self.pendingFrame;
      self.pendingFrame = null;
      if (next) self.onFrame(next);
    });
  };

  // -- tamanho -------------------------------------------------------------------

  LiveView.prototype.scheduleResize = function () {
    var self = this;
    clearTimeout(this.resizeTimer);
    this.resizeTimer = setTimeout(function () {
      var width = clamp(self.root.clientWidth || 1280, 320, 1920);
      var height = clamp((window.innerHeight || 800) * 0.72, 240, 1200);
      self.send({ tipo: "tamanho", largura: width, altura: height });
    }, 300);
  };

  // -- mouse ---------------------------------------------------------------------

  LiveView.prototype.point = function (e) {
    var rect = this.canvas.getBoundingClientRect();
    var x = (e.clientX - rect.left) * (this.screen.w / Math.max(rect.width, 1));
    var y = (e.clientY - rect.top) * (this.screen.h / Math.max(rect.height, 1));
    return { x: Math.max(0, Math.min(this.screen.w - 1, x)), y: Math.max(0, Math.min(this.screen.h - 1, y)) };
  };

  LiveView.prototype.bindMouse = function () {
    var self = this;
    var canvas = this.canvas;
    canvas.addEventListener("mousemove", function (e) {
      var p = self.point(e);
      var first = self.pendingMove === null;
      self.pendingMove = { tipo: "mouse", acao: "mover", x: p.x, y: p.y, modificadores: modifiers(e) };
      if (first) {
        requestAnimationFrame(function () {
          var move = self.pendingMove;
          self.pendingMove = null;
          if (move) self.send(move);
        });
      }
    });
    canvas.addEventListener("mousedown", function (e) {
      e.preventDefault();
      self.keys.focus({ preventScroll: true });
      var p = self.point(e);
      self.flushMove();
      self.send({ tipo: "mouse", acao: "apertar", x: p.x, y: p.y, botao: BUTTONS[e.button] || "esquerdo", cliques: Math.min(e.detail || 1, 3), modificadores: modifiers(e) });
      self.touch();
    });
    canvas.addEventListener("mouseup", function (e) {
      e.preventDefault();
      var p = self.point(e);
      self.flushMove();
      self.send({ tipo: "mouse", acao: "soltar", x: p.x, y: p.y, botao: BUTTONS[e.button] || "esquerdo", cliques: Math.min(e.detail || 1, 3), modificadores: modifiers(e) });
      self.touch();
    });
    canvas.addEventListener("wheel", function (e) {
      e.preventDefault();
      var p = self.point(e);
      var scale = e.deltaMode === 1 ? 40 : (e.deltaMode === 2 ? 800 : 1);
      self.send({ tipo: "roda", x: p.x, y: p.y, dx: clamp(e.deltaX * scale, -5000, 5000), dy: clamp(e.deltaY * scale, -5000, 5000), modificadores: modifiers(e) });
      self.touch();
    }, { passive: false });
    canvas.addEventListener("contextmenu", function (e) { e.preventDefault(); });
  };

  LiveView.prototype.flushMove = function () {
    var move = this.pendingMove;
    this.pendingMove = null;
    if (move) this.send(move);
  };

  // -- teclado -------------------------------------------------------------------

  LiveView.prototype.flushText = function () {
    if (this.composing) return;
    var text = this.keys.value;
    if (!text) return;
    this.keys.value = "";
    for (var i = 0; i < text.length; i += MAX_TEXT) {
      this.send({ tipo: "texto", texto: text.slice(i, i + MAX_TEXT) });
    }
    this.touch();
  };

  LiveView.prototype.bindKeyboard = function () {
    var self = this;
    var keys = this.keys;
    function key(e, action) {
      if (e.isComposing || self.composing || e.key === "Dead" || e.key === "Process") return;
      var mods = modifiers(e);
      var shortcut = (mods & 7) !== 0 && e.key.length === 1;
      if (shortcut && (mods & 6) && e.key.toLowerCase() === "v") return; // cola daqui
      if (!SPECIAL[e.key] && !shortcut) return; // o texto sai do campo (evento input)
      e.preventDefault();
      self.send({ tipo: "tecla", acao: action, tecla: e.key, codigo: e.code || "", modificadores: mods, repetida: !!e.repeat });
      self.touch();
    }
    keys.addEventListener("keydown", function (e) { self.flushText(); key(e, "descer"); });
    keys.addEventListener("keyup", function (e) { key(e, "subir"); });
    keys.addEventListener("compositionstart", function () { self.composing = true; });
    keys.addEventListener("compositionend", function () { self.composing = false; self.flushText(); });
    keys.addEventListener("input", function () { self.flushText(); });
    keys.addEventListener("focus", function () { self.canvas.style.outline = "2px solid #1976d2"; });
    keys.addEventListener("blur", function () { self.canvas.style.outline = "none"; });
  };

  function start() {
    var config = window.RAPA_AO_VIVO;
    var root = document.getElementById("rapa-ao-vivo");
    if (!config || !root) {
      // A página monta os elementos depois do script: espera o contêiner aparecer.
      setTimeout(start, 50);
      return;
    }
    if (root.getAttribute("data-iniciada")) return;
    root.setAttribute("data-iniciada", "1");
    var view = new LiveView(root, config);
    window.RapaAoVivo = {
      bilhete: function (ticket) { view.newTicket(ticket); },
      semBilhete: function () { view.noTicket(); }
    };
  }

  start();
})();
