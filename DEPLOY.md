# Hospedar o SGE online (Render + disco persistente)

O SGE continua a usar SQLite. Por isso precisa de **disco persistente** e de **1 só worker**
(já configurado em `gunicorn.conf.py`). A Vercel não serve: não guarda ficheiros entre pedidos.

## 0. Preparar o código (uma vez)

Na pasta do projeto, com os ficheiros deste pacote copiados para lá:

```bash
python aplicar_modo_servidor.py --simular   # mostra o que vai alterar
python aplicar_modo_servidor.py             # aplica (pode repetir sem problema)
git diff                                    # reveja antes do commit
```

O script só acrescenta pequenos blocos em `app.py`, `database.py`, `templates/base.html` e
`static/js/app.js`. Se disser `[ANCORA?]` para algum ponto, é porque esse trecho do seu ficheiro
já foi alterado: envie-me o ficheiro e eu ajusto. Se já existir um `requirements.txt` no repositório,
compare e junte `gunicorn`, `Pillow`, `reportlab`, `openpyxl` e `pdfplumber`.

Sem `SGE_MODO_SERVIDOR=1`, o SGE continua a funcionar como versão desktop.

## 1. Variáveis de ambiente

| Variável | Para quê |
|---|---|
| `SGE_MODO_SERVIDOR=1` | Ativa o modo servidor (cookies seguros, proxy, sem "Encerrar"/atualizações .sgeu) |
| `SGE_BASE_DIR=/var/data` | Pasta do disco persistente: `dados/`, `backups/`, logo, chave |
| `SGE_SECRET_KEY` | Chave das sessões (o Render gera uma) |
| `SGE_ADMIN_PASSWORD` | Senha do `admin` **na primeira criação da base**. Se a base já existir, mude a senha na aplicação |
| `SGE_COOKIE_SECURE=0` | Só se NÃO houver HTTPS (não recomendado) |

## 2. Render (passo a passo)

1. Faça push para `folestechlda/Project-SGE` (com `render.yaml`, `requirements.txt`, `Procfile`, `gunicorn.conf.py`).
2. No Render: **New → Blueprint** → escolha o repositório. Ele lê o `render.yaml`.
3. Quando pedir `SGE_ADMIN_PASSWORD`, escreva uma senha forte.
4. Aguarde o deploy. O endereço `https://sge-xxxx.onrender.com` abre o login.
5. Entre com `admin` e a senha definida; vá a **Configurações** e preencha os dados da empresa.

## 3. Levar a base de dados atual da loja

1. No SGE da loja: **Sistema → Configurações**, cartão *Backup e Restauração* → **⬇ Baixar backup actual** (ficheiro `.db`).
2. No SGE online (como Admin): **Sistema → Configurações → ↺ Restaurar backup** e escolha o ficheiro `.db`.
   (o ficheiro pode ser grande: o limite está em 256 MB).

## 4. Rotina de segurança

- Descarregue um backup regularmente (o disco é único: se for apagado, perde tudo).
- Não deixe `admin123`. Crie utilizadores Gerente/Operador para o dia a dia.
- Dados reais nunca vão para o Git (o `.gitignore` já ignora `dados/` e `*.db`).

## Outras opções com o mesmo código
- **Railway / Fly.io**: crie um *volume* montado em `/data` e use `SGE_BASE_DIR=/data`; arranque com o `Procfile`.
- **VPS** (Hetzner, DigitalOcean…): `gunicorn app:app -c gunicorn.conf.py` atrás do Nginx/Caddy com HTTPS, com as mesmas variáveis.
