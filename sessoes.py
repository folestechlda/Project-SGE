# -*- coding: utf-8 -*-
"""Sessões do SGE: controlo no servidor.

O cookie do Flask só diz quem é o utilizador; quem manda é a tabela `sessoes`.
Cada início de sessão cria uma linha com a hora da última actividade, e é isso que
permite terminar a sessão por inatividade, impedir duas sessões do mesmo utilizador
e invalidar sessões quando a senha muda ou o utilizador é desactivado.
"""
import secrets
import time

from database import get_config

# Tempo limite de inatividade: valores aceites na interface (minutos).
OPCOES_INATIVIDADE = (15, 30, 45, 60)
PADRAO_INATIVIDADE_MIN = 30
# Margem mínima aceite pelo servidor (permite testes com 1 minuto).
MINIMO_INATIVIDADE_MIN = 1
MAXIMO_INATIVIDADE_MIN = 480
# Para não gravar na base de dados a cada pedido.
INTERVALO_GRAVACAO_S = 5

MENSAGENS = {
    "inatividade": "Sessão terminada por inatividade ({min} min sem utilização). Inicie sessão novamente.",
    "substituida": "A sua sessão foi encerrada porque iniciou sessão noutro dispositivo ou janela.",
    "senha": "A senha desta conta foi alterada. Inicie sessão novamente.",
    "desativado": "Esta conta foi desactivada.",
    "manual": "Sessão terminada.",
    "terminada": "A sessão terminou. Inicie sessão novamente.",
}


def normalizar_minutos(valor):
    """Valor vindo do formulário -> uma das opções permitidas (senão, o padrão)."""
    try:
        m = int(valor)
    except (TypeError, ValueError):
        return PADRAO_INATIVIDADE_MIN
    return m if m in OPCOES_INATIVIDADE else PADRAO_INATIVIDADE_MIN


def limite_minutos():
    try:
        m = int(float(get_config("sessao_inatividade_min", str(PADRAO_INATIVIDADE_MIN))))
    except (TypeError, ValueError):
        m = PADRAO_INATIVIDADE_MIN
    return max(MINIMO_INATIVIDADE_MIN, min(MAXIMO_INATIVIDADE_MIN, m))


def limite_segundos():
    return limite_minutos() * 60


def sessao_unica():
    return get_config("sessao_unica", "1") == "1"


def janela_unica():
    return get_config("janela_unica", "1") == "1"


def mensagem(motivo):
    return MENSAGENS.get(motivo, MENSAGENS["terminada"]).format(min=limite_minutos())


def criar(conn, id_usuario, ip=None, agente=None, agora=None):
    """Abre uma sessão. Com `sessao_unica`, termina as outras sessões activas do utilizador."""
    agora = agora if agora is not None else time.time()
    if sessao_unica():
        terminar_outras(conn, id_usuario, None, "substituida", agora)
    # limpeza barata: sessões terminadas há mais de 30 dias
    conn.execute("DELETE FROM sessoes WHERE terminada_em IS NOT NULL AND terminada_em < ?",
                 (agora - 30 * 86400,))
    token = secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO sessoes (id_sessao, id_usuario, criada_em, ultima_atividade, ip, agente) "
        "VALUES (?,?,?,?,?,?)",
        (token, id_usuario, agora, agora, ip, (agente or "")[:200]))
    return token


def validar(conn, token, agora=None):
    """(estado, motivo, linha). estado: 'ok' | 'invalida'. Termina a sessão se expirou."""
    agora = agora if agora is not None else time.time()
    if not token:
        return "invalida", "terminada", None
    row = conn.execute("SELECT * FROM sessoes WHERE id_sessao=?", (token,)).fetchone()
    if row is None:
        return "invalida", "terminada", None
    if row["terminada_em"] is not None:
        return "invalida", row["motivo_fim"] or "terminada", row
    if agora - float(row["ultima_atividade"]) > limite_segundos():
        terminar(conn, token, "inatividade", agora)
        return "invalida", "inatividade", row
    return "ok", None, row


def tocar(conn, token, agora=None, row=None):
    """Regista actividade (com intervalo mínimo entre gravações)."""
    agora = agora if agora is not None else time.time()
    if row is not None and agora - float(row["ultima_atividade"]) < INTERVALO_GRAVACAO_S:
        return
    conn.execute("UPDATE sessoes SET ultima_atividade=? WHERE id_sessao=? AND terminada_em IS NULL",
                 (agora, token))


def restante_segundos(row, agora=None):
    agora = agora if agora is not None else time.time()
    return max(0, int(limite_segundos() - (agora - float(row["ultima_atividade"]))))


def terminar(conn, token, motivo, agora=None):
    agora = agora if agora is not None else time.time()
    conn.execute("UPDATE sessoes SET terminada_em=?, motivo_fim=? WHERE id_sessao=? AND terminada_em IS NULL",
                 (agora, motivo, token))


def terminar_outras(conn, id_usuario, token_atual, motivo, agora=None):
    """Termina as sessões activas do utilizador, excepto `token_atual`."""
    agora = agora if agora is not None else time.time()
    conn.execute(
        "UPDATE sessoes SET terminada_em=?, motivo_fim=? "
        "WHERE id_usuario=? AND terminada_em IS NULL AND id_sessao != ?",
        (agora, motivo, id_usuario, token_atual or ""))
