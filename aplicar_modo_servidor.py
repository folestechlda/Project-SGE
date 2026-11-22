# -*- coding: utf-8 -*-
"""aplicar_modo_servidor.py — acrescenta o "modo servidor" ao SGE (hospedagem online).

Uso (na pasta do projeto, onde estão app.py e database.py):
    python aplicar_modo_servidor.py --simular    # só mostra o que faria
    python aplicar_modo_servidor.py              # aplica

* Idempotente: pode correr várias vezes; o que já estiver aplicado é ignorado.
* Só acrescenta pequenos blocos; não substitui ficheiros inteiros, por isso preserva o seu trabalho.
* Sem a variável SGE_MODO_SERVIDOR=1 o SGE continua a comportar-se como a versão desktop.
* Se alguma âncora não for encontrada (ficheiro já alterado nesse ponto), o script avisa e
  diz qual alteração fazer à mão; nada é gravado nesse ficheiro até estar tudo certo.
"""
import os
import sys

EDITS = [
    ('database.py',
     'pasta base configurável (SGE_BASE_DIR)',
     'def get_base_dir():\n    if getattr(sys, "frozen", False):',
     'def get_base_dir():\n    # Modo servidor: SGE_BASE_DIR aponta para o disco persistente (ex.: /var/data); aí ficam\n    # dados/, backups/, o logo carregado e a chave secreta.\n    env = os.environ.get("SGE_BASE_DIR")\n    if env:\n        os.makedirs(env, exist_ok=True)\n        return env\n    if getattr(sys, "frozen", False):'),
    ('database.py',
     'senha inicial do admin por variável (SGE_ADMIN_PASSWORD)',
     '            ("Administrador", "admin", generate_password_hash("admin123"), "Admin"),',
     '            ("Administrador", "admin",\n             generate_password_hash(os.environ.get("SGE_ADMIN_PASSWORD") or "admin123"), "Admin"),'),
    ('app.py',
     'import ProxyFix',
     'from werkzeug.utils import secure_filename\n',
     'from werkzeug.utils import secure_filename\nfrom werkzeug.middleware.proxy_fix import ProxyFix\n'),
    ('app.py',
     'constante SERVIDOR',
     'APP_PORT = 5701\nAPP_NAME = "SGE"\n',
     'APP_PORT = 5701\nAPP_NAME = "SGE"\n\n# Modo servidor (hospedagem online): SGE_MODO_SERVIDOR=1. Sem esta variável o SGE comporta-se\n# exactamente como a versão desktop.\nSERVIDOR = os.environ.get("SGE_MODO_SERVIDOR") == "1"\n'),
    ('app.py',
     'chave secreta por variável (SGE_SECRET_KEY)',
     'def _get_or_create_secret_key():\n    key_path',
     'def _get_or_create_secret_key():\n    env_key = (os.environ.get("SGE_SECRET_KEY") or "").strip()\n    if env_key:\n        return env_key\n    key_path'),
    ('app.py',
     'configuração Flask / logs em modo servidor',
     'app.config["TEMPLATES_AUTO_RELOAD"] = True\napp.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024  # cabe um backup grande\n\nlogging.basicConfig(\n    filename=os.path.join(get_base_dir(), "app_errors.log"),\n    level=logging.ERROR,\n    format="%(asctime)s %(levelname)s %(message)s",\n)\n',
     'app.config["TEMPLATES_AUTO_RELOAD"] = not SERVIDOR\napp.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024  # cabe um backup grande\n\nif SERVIDOR:\n    # Atrás do proxy do serviço (HTTPS): usa o IP real do cliente e o esquema https\n    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)\n    app.config.update(\n        SESSION_COOKIE_HTTPONLY=True,\n        SESSION_COOKIE_SAMESITE="Lax",\n        SESSION_COOKIE_SECURE=os.environ.get("SGE_COOKIE_SECURE", "1") != "0",\n    )\n    logging.basicConfig(level=logging.WARNING,       # logs vão para a consola do serviço\n                        format="%(asctime)s %(levelname)s %(message)s")\nelse:\n    logging.basicConfig(\n        filename=os.path.join(get_base_dir(), "app_errors.log"),\n        level=logging.ERROR,\n        format="%(asctime)s %(levelname)s %(message)s",\n    )\n'),
    ('app.py',
     'sinais, bloqueios de desktop e /healthz',
     'atexit.register(_encerrar_tudo)\ntry:\n    signal.signal(signal.SIGINT, lambda s, f: (_encerrar_tudo(), sys.exit(0)))\n    signal.signal(signal.SIGTERM, lambda s, f: (_encerrar_tudo(), sys.exit(0)))\nexcept Exception:\n    pass\n',
     'if not SERVIDOR:   # num servidor quem gere o ciclo de vida é o gunicorn\n    atexit.register(_encerrar_tudo)\n    try:\n        signal.signal(signal.SIGINT, lambda s, f: (_encerrar_tudo(), sys.exit(0)))\n        signal.signal(signal.SIGTERM, lambda s, f: (_encerrar_tudo(), sys.exit(0)))\n    except Exception:\n        pass\n\n\n@app.before_request\ndef _bloquear_funcoes_de_desktop():\n    """Em modo servidor não há encerrar/actualizar pacotes .sgeu (o código vem do deploy)."""\n    if SERVIDOR and (request.path == "/api/shutdown" or request.path.startswith(\n            ("/atualizacoes", "/api/atualizacoes"))):\n        abort(404)\n\n\n@app.route("/healthz")\ndef healthz():\n    """Verificação de saúde para o serviço de hospedagem (sem login)."""\n    try:\n        conn = get_conn()\n        conn.execute("SELECT 1").fetchone()\n        conn.close()\n    except Exception:\n        return ("erro", 503)\n    return ("ok", 200)\n'),
    ('app.py',
     'variável modo_servidor nos templates',
     '        "app_name": APP_NAME,\n        "hoje": datetime.now().date().isoformat(),',
     '        "app_name": APP_NAME,\n        "modo_servidor": SERVIDOR,\n        "hoje": datetime.now().date().isoformat(),'),
    ('templates/base.html',
     'esconder "Atualizações" online',
     '<a href="{{ url_for(\'atualizacoes_view\') }}" class="{{ \'active\' if ep==\'atualizacoes_view\' }}">Atualizações</a>',
     '{% if not modo_servidor %}<a href="{{ url_for(\'atualizacoes_view\') }}" class="{{ \'active\' if ep==\'atualizacoes_view\' }}">Atualizações</a>{% endif %}'),
    ('templates/base.html',
     'botão Encerrar → Sair online',
     'title="Fechar sessão e encerrar app" data-confirmar="Deseja encerrar o sistema? A sessão será fechada.">\n        ⏻ Encerrar',
     'title="{{ \'Terminar sessão\' if modo_servidor else \'Fechar sessão e encerrar app\' }}" data-confirmar="{{ \'Deseja terminar a sessão?\' if modo_servidor else \'Deseja encerrar o sistema? A sessão será fechada.\' }}">\n        ⏻ {{ \'Sair\' if modo_servidor else \'Encerrar\' }}'),
    ('templates/base.html',
     'flag JS SGE_SERVIDOR',
     '<script src="{{ url_for(\'static\', filename=\'js/app.js\') }}"></script>',
     '<script>window.SGE_SERVIDOR = {{ \'true\' if modo_servidor else \'false\' }};</script>\n<script src="{{ url_for(\'static\', filename=\'js/app.js\') }}"></script>'),
    ('static/js/app.js',
     'Sair online só termina a sessão',
     'function sairEncerrarApp() {\n  try {',
     "function sairEncerrarApp() {\n  if (window.SGE_SERVIDOR) { window.location.href = '/logout'; return; }   // online: só termina a sessão\n  try {"),
]


def main():
    simular = "--simular" in sys.argv
    base = os.path.dirname(os.path.abspath(__file__))
    por_ficheiro = {}
    for f, nome, old, new in EDITS:
        por_ficheiro.setdefault(f, []).append((nome, old, new))

    problemas = 0
    for f, lista in por_ficheiro.items():
        caminho = os.path.join(base, f)
        if not os.path.isfile(caminho):
            print(f"[FALTA]    {f}: ficheiro não encontrado"); problemas += 1; continue
        bruto = open(caminho, encoding="utf-8", newline="").read()
        crlf = "\r\n" in bruto
        s = bruto.replace("\r\n", "\n")
        alterou, falhou = False, False
        for nome, old, new in lista:
            if new in s:
                print(f"[JA FEITO] {f}: {nome}"); continue
            if old not in s:
                print(f"[ANCORA?]  {f}: {nome}  -> não encontrei o ponto de inserção; faça à mão (ver DEPLOY.md)")
                problemas += 1; falhou = True; continue
            s = s.replace(old, new, 1); alterou = True
            print(f"[{'SIMULA' if simular else 'OK'}]      {f}: {nome}")
        if alterou and not simular and not falhou:
            open(caminho, "w", encoding="utf-8", newline="").write(s.replace("\n", "\r\n") if crlf else s)
    if problemas:
        print(f"\n{problemas} ponto(s) por resolver. Os ficheiros com problemas NÃO foram gravados.")
        sys.exit(1)
    print("\nConcluído." + ("  (simulação: nada foi gravado)" if simular else ""))


if __name__ == "__main__":
    main()
