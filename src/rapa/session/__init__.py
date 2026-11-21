"""Sessão de navegador ao vivo para a demonstração (passo "Demonstrar").

Um processo hospedeiro (``python -m rapa.session.host``) roda um Chromium sem
janela com o gravador (:class:`rapa.recorder.Recorder`) ligado ao contexto e abre
um canal de controle local (websocket), autenticado por um token aleatório da
sessão. Pelo canal saem as imagens da tela (screencast do CDP), o endereço e o
título da página; entram o mouse, a roda, o teclado (com acentos, pelo
``Input.insertText``), o tamanho da janela, a navegação e o pedido de parar, que
devolve a gravação já limpa de segredos.

- :mod:`rapa.session.protocol`: mensagens do canal e o envio de quadros sem acúmulo;
- :mod:`rapa.session.keys`: tradução das mensagens de entrada em comandos do CDP;
- :mod:`rapa.session.host`: o processo hospedeiro (depende do Playwright e do
  pacote ``websockets``).

O plano de controle inicia o hospedeiro por um lançador
(:mod:`rapa.control.sessions.launcher`) e repassa as mensagens entre o navegador
da pessoa e o hospedeiro (:mod:`rapa.control.sessions.relay`).
"""
