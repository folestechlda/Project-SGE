# -*- coding: utf-8 -*-
"""resetar_senha.py — redefine a senha de um utilizador (ex.: o Admin que a esqueceu).

Uso (na pasta do projeto; no Render: separador «Shell» do serviço):
    python resetar_senha.py              # redefine o utilizador «admin»
    python resetar_senha.py joao         # redefine o utilizador «joao»

Pede a nova senha no terminal (não aparece no ecrã). Regra: mínimo 8 caracteres, com letra,
número e símbolo. Também apaga qualquer pedido de recuperação pendente e desbloqueia o acesso.
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from werkzeug.security import generate_password_hash  # noqa: E402

import services  # noqa: E402
from database import get_conn  # noqa: E402


def main():
    login = (sys.argv[1] if len(sys.argv) > 1 else "admin").strip()
    conn = get_conn()
    user = conn.execute("SELECT id_usuario, nome, perfil, ativo FROM usuarios WHERE login=?", (login,)).fetchone()
    if not user:
        print(f"Não existe o utilizador «{login}».")
        sys.exit(1)
    print(f"Utilizador: {user['nome']} ({user['perfil']}){'' if user['ativo'] else '  [DESATIVADO]'}")
    nova = getpass.getpass("Nova senha: ")
    if nova != getpass.getpass("Repita a nova senha: "):
        print("As senhas não coincidem. Nada foi alterado.")
        sys.exit(1)
    if not services.validar_senha(nova):
        print("Senha fraca: mínimo 8 caracteres, com letra, número e símbolo. Nada foi alterado.")
        sys.exit(1)
    conn.execute("UPDATE usuarios SET senha_hash=?, token_recuperacao=NULL, token_expira=NULL WHERE id_usuario=?",
                 (generate_password_hash(nova), user["id_usuario"]))
    try:
        conn.execute("DELETE FROM login_attempts WHERE login=?", (login,))
    except Exception:
        pass
    conn.commit()
    conn.close()
    print("Senha redefinida com sucesso.")


if __name__ == "__main__":
    main()
